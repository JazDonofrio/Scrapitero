"""Relevamiento nuevo con polígono nuevo, pero arrancando de los datos ya corregidos.

El problema que resuelve: los dos flujos que existen obligan a elegir entre perder el
trabajo manual o partir el relevamiento viejo (ver la nota de "cambiar el polígono").

  · **Clonar** copia SÓLO el polígono: la región nueva arranca vacía y las correcciones
    —que están indexadas por `region_id + cca_code` y por `parcela_id`— no viajan.
  · **Reusar la región** conserva todo pero el upsert de SmartGIS hace
    `UPDATE parcelas SET survey_id`: las parcelas se MUDAN, el survey viejo queda vacío
    y se pierde el término de comparación.

Este script hace la tercera cosa: **región nueva + copia de los datos**. El relevamiento
viejo queda intacto y el nuevo nace con las parcelas, los sellos `manual`, las incidencias
ya resueltas, las alturas y el mapa base del cliente ya adentro. Después se le corre el
pipeline normal: SmartGIS reconoce lo copiado por `region_id + cca_code` (lo actualiza sin
pisar los sellos `manual`) e INSERTA sólo los lotes del área nueva.

    python scripts/portar_relevamiento.py \
        --origen 59d68f2e-a82b-4c30-8ceb-fe0954bba8cb \
        --nombre "Varzea Grande VAZ049" \
        --zona /ruta/zona_vaz049.geojson \
        [--todo] [--aplicar]

Sin `--aplicar` es un simulacro: dice cuántas filas copiaría por tabla y no escribe nada.
Por defecto copia SÓLO las parcelas cuyo centroide cae dentro del polígono nuevo; con
`--todo` copia las 567 aunque queden afuera (las parcelas cuelgan del survey, no del
polígono, así que las de afuera igual saldrían en el CSV y en el DXF).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid

from sqlalchemy import text

sys.path.insert(0, "/opt/scrapitero/src")
from scrapitero.db.engine import get_engine  # noqa: E402

# Serial/identity: se omiten de la lista de columnas para que el default los llene.
_OMITIR = {"ejes_calle": {"id"}, "mapa_base_cliente": {"id"}, "hotel_descartado": {"id"}}


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower().strip())
    return re.sub(r"-+", "-", s).strip("-")


def _cols(conn, tabla: str) -> list[str]:
    filas = conn.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = :t ORDER BY ordinal_position
    """), {"t": tabla}).fetchall()
    return [f[0] for f in filas if f[0] not in _OMITIR.get(tabla, set())]


def _pk_uuid_sueltas(conn, tabla: str) -> set[str]:
    """PKs uuid de una sola columna: hay que regenerarlas o chocan con las del origen.

    Se detectan solas en vez de enumerarlas a mano — `poi_id` y `footprint_id` no estaban
    en la lista escrita a ojo y reventaron el INSERT. Las que se remapean (`parcela_id`)
    ya vienen en `reglas` y no pasan por acá.
    """
    filas = conn.execute(text("""
        WITH pk AS (
            SELECT kcu.column_name,
                   count(*) OVER (PARTITION BY tc.constraint_name) AS n
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON kcu.constraint_name = tc.constraint_name
            WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_name = :t
        )
        SELECT pk.column_name FROM pk
        JOIN information_schema.columns c
          ON c.table_name = :t AND c.column_name = pk.column_name
        WHERE pk.n = 1 AND c.data_type = 'uuid'
    """), {"t": tabla}).fetchall()
    return {f[0] for f in filas}


def _copiar(conn, tabla: str, where: str, reglas: dict[str, str], aplicar: bool) -> int:
    """INSERT ... SELECT con las columnas de identidad reescritas por `reglas`."""
    cols = _cols(conn, tabla)
    reglas = {c: "gen_random_uuid()" for c in _pk_uuid_sueltas(conn, tabla)
              if c not in reglas} | reglas
    sel = ", ".join(reglas.get(c, f"t.{c}") for c in cols)
    cnt = conn.execute(text(f"SELECT count(*) FROM {tabla} t WHERE {where}")).scalar() or 0
    if aplicar and cnt:
        conn.execute(text(
            f"INSERT INTO {tabla} ({', '.join(cols)}) SELECT {sel} FROM {tabla} t WHERE {where}"))
    return cnt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--origen", required=True, help="survey_id del relevamiento a portar")
    ap.add_argument("--nombre", required=True, help="nombre del relevamiento nuevo")
    ap.add_argument("--zona", required=True, help="GeoJSON del polígono nuevo")
    ap.add_argument("--todo", action="store_true",
                    help="copiar todas las parcelas, incluso las de afuera del polígono")
    ap.add_argument("--aplicar", action="store_true", help="escribir (sin esto es simulacro)")
    a = ap.parse_args()

    gj = json.load(open(a.zona))
    geom = json.dumps(gj["geometry"] if gj.get("type") == "Feature" else gj)

    engine = get_engine()
    with engine.begin() as conn:
        src = conn.execute(text("""
            SELECT s.region_id, r.country_code, r.municipio_codigo, s.baseline_id
            FROM surveys s JOIN regions r ON r.region_id = s.region_id
            WHERE s.survey_id = CAST(:sid AS uuid)
        """), {"sid": a.origen}).fetchone()
        if not src:
            print(f"✖ No existe el survey {a.origen}")
            return 1
        old_region, cc, muni, baseline_id = src

        new_region = f"zona-{_slug(a.nombre)}"
        new_survey = str(uuid.uuid4())
        if conn.execute(text("SELECT 1 FROM regions WHERE region_id = :r"),
                        {"r": new_region}).fetchone():
            print(f"✖ La región «{new_region}» ya existe — elegí otro nombre.\n"
                  f"  (el region_id sale del slug del nombre y reusarlo pisaría su polígono)")
            return 1
        if not muni:
            print("⚠ La región de origen no tiene municipio_codigo — las fuentes brasileras "
                  "van a devolver ok:true con cero resultados.")

        print(f"origen : {old_region}  ({a.origen})")
        print(f"destino: {new_region}  ({new_survey})")
        print(f"modo   : {'SIMULACRO — no escribe nada' if not a.aplicar else 'APLICANDO'}\n")

        conn.execute(text("CREATE TEMP TABLE _z (g geometry) ON COMMIT DROP"))
        conn.execute(text("INSERT INTO _z VALUES (ST_SetSRID(ST_GeomFromGeoJSON(:g), 4326))"),
                     {"g": geom})

        # ── qué parcelas entran ────────────────────────────────────────────────────
        filtro = "" if a.todo else """
            AND EXISTS (SELECT 1 FROM _z WHERE ST_Contains(_z.g,
                ST_SetSRID(ST_MakePoint(p.centroid_lng, p.centroid_lat), 4326)))"""
        conn.execute(text(f"""
            CREATE TEMP TABLE _map_p (old uuid PRIMARY KEY, new uuid) ON COMMIT DROP;
            INSERT INTO _map_p
            SELECT p.parcela_id, gen_random_uuid() FROM parcelas p
            WHERE p.region_id = '{old_region}'
              AND p.centroid_lat IS NOT NULL {filtro}
        """))
        n_p = conn.execute(text("SELECT count(*) FROM _map_p")).scalar()
        n_tot = conn.execute(text("SELECT count(*) FROM parcelas WHERE region_id = :r"),
                             {"r": old_region}).scalar()
        print(f"parcelas: {n_p} de {n_tot}"
              f"{'' if a.todo else f' (quedan {n_tot - n_p} afuera del polígono)'}\n")

        conn.execute(text(f"""
            CREATE TEMP TABLE _map_e (old uuid PRIMARY KEY, new uuid) ON COMMIT DROP;
            INSERT INTO _map_e
            SELECT establecimiento_id, gen_random_uuid() FROM establecimientos
            WHERE region_id = '{old_region}'
        """))

        P = "(SELECT new FROM _map_p WHERE old = t.parcela_id)"
        E = "(SELECT new FROM _map_e WHERE old = t.establecimiento_id)"
        R, S = f"'{new_region}'", f"CAST('{new_survey}' AS uuid)"
        EN_MAPA = "t.parcela_id IN (SELECT old FROM _map_p)"
        # `parcela_id` suelto: la fila viaja sólo si su parcela viajó (o si no apunta a ninguna).
        SUELTA = ("(t.parcela_id IS NULL OR t.parcela_id IN (SELECT old FROM _map_p))")

        if a.aplicar:
            conn.execute(text("""
                INSERT INTO regions (region_id, name, country_code, zone_geojson, bbox_wkt,
                                     municipio_codigo)
                SELECT :nr, :nom, :cc, :gj,
                       ST_AsText(ST_Envelope(ST_GeomFromGeoJSON(:g))), :mun
            """), {"nr": new_region, "nom": a.nombre, "cc": cc,
                   "gj": json.dumps(gj), "g": geom, "mun": muni})
            conn.execute(text("""
                INSERT INTO surveys (survey_id, region_id, status, baseline_id)
                VALUES (CAST(:sid AS uuid), :rid, 'stopped', :bid)
            """), {"sid": new_survey, "rid": new_region, "bid": baseline_id})

        # ── el orden respeta las FK ────────────────────────────────────────────────
        plan = [
            ("establecimientos",         f"t.region_id = '{old_region}'",
             {"establecimiento_id": E, "region_id": R, "survey_id": S}),
            ("parcelas",                 "t.parcela_id IN (SELECT old FROM _map_p)",
             {"parcela_id": "(SELECT new FROM _map_p WHERE old = t.parcela_id)",
              "establecimiento_id": E, "region_id": R, "survey_id": S}),
            ("parcela_altura",           EN_MAPA, {"parcela_id": P, "region_id": R, "survey_id": S}),
            ("parcela_tipo_manual",      EN_MAPA, {"parcela_id": P}),
            ("parcela_direccion_manual", f"t.region_id = \'{old_region}\' AND " + SUELTA, {"parcela_id": P, "region_id": R}),
            ("parcela_ubicacion_manual", f"t.region_id = \'{old_region}\' AND " + SUELTA, {"parcela_id": P, "region_id": R}),
            ("parcela_uso_manual",       f"t.region_id = \'{old_region}\' AND " + SUELTA, {"parcela_id": P, "region_id": R}),
            ("comercios",                EN_MAPA,
             {"comercio_id": "gen_random_uuid()", "parcela_id": P, "region_id": R, "survey_id": S}),
            ("establecimientos_poi",     f"t.region_id = \'{old_region}\' AND " + SUELTA, {"parcela_id": P, "region_id": R}),
            ("footprints_revision",      EN_MAPA, {"parcela_id": P, "region_id": R, "survey_id": S}),
            ("hoteles",                  f"t.region_id = '{old_region}'",
             {"hotel_id": "gen_random_uuid()", "parcela_id": P, "region_id": R, "survey_id": S}),
            ("hotel_descartado",         f"t.region_id = '{old_region}'", {"region_id": R, "survey_id": S}),
            ("hotel_cerrado_manual",     f"t.region_id = '{old_region}'", {"region_id": R}),
            ("hotel_habitaciones_manual", f"t.region_id = '{old_region}'", {"region_id": R}),
            ("hotel_ubicacion_manual",   f"t.region_id = '{old_region}'", {"region_id": R}),
            ("incidencias",              f"t.region_id = '{old_region}' AND " + SUELTA,
             {"incidencia_id": "gen_random_uuid()", "parcela_id": P, "region_id": R, "survey_id": S}),
            ("ejes_calle",               f"t.region_id = '{old_region}'",
             {"region_id": R, "fuente": f"replace(t.fuente, '{old_region}', '{new_region}')"}),
            ("mapa_base_cliente",        f"t.fuente LIKE '%{old_region}'",
             {"fuente": f"replace(t.fuente, '{old_region}', '{new_region}')"}),
            # ⚠ `fichas_cliente` no tiene region_id ni survey_id: se indexa por `fuente`,
            # así que no aparece si uno enumera las tablas por esas dos columnas. Sin ella
            # la ficha se centra en el lote en vez de ir al punto y la rotación del cliente.
            ("fichas_cliente",           f"t.fuente LIKE '%{old_region}'",
             {"fuente": f"replace(t.fuente, '{old_region}', '{new_region}')"}),
        ]

        total = 0
        for tabla, where, reglas in plan:
            n = _copiar(conn, tabla, where, reglas, a.aplicar)
            total += n
            print(f"  {'✔' if a.aplicar else '·'} {tabla:<28} {n:>6}")
        print(f"\n  {'copiadas' if a.aplicar else 'a copiar':<30} {total:>6} filas")

        if not a.aplicar:
            conn.rollback()
            print("\nSIMULACRO — nada escrito. Repetí con --aplicar.")
        else:
            print(f"\n✔ Listo. survey_id = {new_survey}")
            print("  Siguiente: correr smartgis_fetcher con max_runtime_s alto y después el "
                  "pipeline VG. Va a reconocer lo copiado por cca_code e insertar sólo lo nuevo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
