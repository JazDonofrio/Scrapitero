#!/usr/bin/env python3
"""Chequeo de las parcelas donde conviven comercio y vivienda, cuadra por cuadra.

Nace de una verificación en la calle (Av. Couto Magalhães 324–434, Várzea Grande,
20-ago-2026): de seis inmuebles mirados a ojo, cuatro tenían algo mal. Ninguno era un
bug de programación — eran el catastro, el geocoding y la falta de un código "mixto"
apretando en el mismo lugar. Esto busca los mismos síntomas en el resto del relevamiento
para no depender de que alguien pase por la vereda.

Ocho chequeos, todos de SÓLO LECTURA, agrupados por cuadra (calle + centena del número):

  A  SELLO MUERTO      · la parcela lleva etiqueta de comercio por CNPJ y ninguno de los
                         establecimientos que caen adentro sigue operando. La nota separa los
                         dos niveles, con el corte que ya usa `HotelFetcher`: todos BAIXADA o
                         NULA es cierre definitivo; "sin ninguno activo" suma INAPTA y
                         SUSPENSA, que no son cierre pero tampoco son prueba de nada.
                         `ParcelaCategoria`
                         filtra por CNAE y por ubicación pero no por situação cadastral:
                         una lanchonete que cerró en 2018 sella igual que una farmacia
                         abierta hoy. El dato existe (`receita_estabelecimentos.situacao`)
                         y `HotelFetcher` sí lo mira — nunca se llevó al sello.
  B  SELLO AJENO       · el CNPJ que sella la parcela declara OTRO número de puerta. Es el
                         geocoding corriendo el punto unos metros y depositándolo en el
                         lote del vecino (Couto Magalhães 352, sellado LANCHONETE por un
                         bar declarado en el 374, a 12 m).
  C  COMERCIO SIN UF   · hay vivienda y un CNPJ **activo** adentro, pero `uf_comercio=0`.
                         Es la mixta candidata de verdad: el mapa muestra comercio y el
                         entregable dice 100% vivienda. Requiere ojo humano — un MEI que
                         factura desde la casa no es un local.
  D  MIXTA EMPATADA    · `uf_vivienda == uf_comercio`. Era un hallazgo mientras el plano
                         emitía un bloque por lote y elegía el tipo por mayoría: en el
                         empate ganaba residencial por el orden de los `if`. Desde el
                         20-ago-2026 `DXFEntrega` emite un bloque por edificación y cada
                         tipo va con el suyo, así que **el empate ya no decide nada** en el
                         entregable. Queda como informativo: sigue diciendo dónde conviven
                         vivienda y comercio en partes iguales.
  E  VIVIENDA MINÚSCULA· unidad declarada residencial de menos de 20 m². Una cochera de
                         12 m² contada como vivienda es un HP que se le entrega al cliente.
  F  NÚMERO EN CERO    · la parcela tiene UF pero su número es `0` o falta. El export lo
                         descarta y sale el estimado entre paréntesis: el hotel de 72
                         habitaciones de Várzea salió rotulado `(388)` teniendo el 400 en
                         el lote lindero.
  G  VACANTE CON UF    · el catastro la declara vacía y sin embargo trae unidades. El plano
                         la dibuja como lote vacío y le suma los HP igual.
  H  TIPO DIVERGENTE   · el tipo que el plano le pone al inmueble y la etiqueta que la app
                         muestra en el mapa no coinciden, porque salen de dos taxonomías: el
                         plano decide por uso y UF, y `_tipo_edificacion` manda todo lo mixto
                         a comercio y le da prioridad al rubro del CNPJ. Hasta el 20-ago-2026
                         esto viajaba DENTRO del entregable (el atributo `TIPOIMOVEL` contra
                         la etiqueta visible del mismo bloque); ahora que ese campo sale
                         vacío —como en el plano del cliente— la divergencia ya no se
                         entrega, pero sigue estando entre lo que se ve en pantalla y lo que
                         recibe el proyectista.

Lo que esto NO puede ver: si el comercio existe de verdad. Ninguno de los ocho es una
sentencia — son candidatos a mirar, y el orden por cuadra está para poder mirarlos de a
tandas geográficas en vez de uno por uno.

Uso:
    .venv/bin/python scripts/chequeo_comercio_vivienda.py --survey <UUID>
    .venv/bin/python scripts/chequeo_comercio_vivienda.py --todos
    .venv/bin/python scripts/chequeo_comercio_vivienda.py --survey <UUID> --detalle A,C
    .venv/bin/python scripts/chequeo_comercio_vivienda.py --survey <UUID> --csv /tmp/x.csv

Sale con código 1 si encuentra algo, para poder encadenarlo.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict

from sqlalchemy import text

from scrapitero.db.engine import get_engine

CHEQUEOS = {
    "A": "sello por CNPJ sin actividad (la nota dice si es baja o inapta)",
    "B": "sello de un CNPJ declarado en otra puerta",
    "C": "vivienda + comercio activo sin UF de comercio",
    "D": "mixta empatada (informativo desde el 20-ago)",
    "E": "unidad residencial de menos de 20 m²",
    "F": "parcela con UF y número en cero",
    "G": "vacante que igual trae UF",
    "H": "el mapa y el plano le dan tipos distintos",
}
_AREA_MIN_VIVIENDA = 20.0


def _num(s) -> str:
    """Número de puerta comparable: sólo dígitos, sin ceros a la izquierda."""
    d = re.sub(r"\D", "", str(s or ""))
    return d.lstrip("0")


def _cuadra(calle: str | None, numero) -> str:
    n = _num(numero)
    if not calle:
        return "(sin calle)"
    if not n:
        return f"{calle} · s/n"
    return f"{calle} · {int(n) // 100 * 100}"


def _parcelas(conn, survey_id: str) -> list:
    return conn.execute(text("""
        SELECT p.parcela_id, p.calle, p.numero, p.uso_principal, p.cca_code,
               COALESCE(p.uf_vivienda, 0) AS uv, COALESCE(p.uf_comercio, 0) AS uc,
               p.area_m2_construida, p.descripcion_uso, p.huella_m2, p.numero_estimado,
               (SELECT ptm.tipo_edificacion FROM parcela_tipo_manual ptm
                  WHERE ptm.parcela_id = p.parcela_id) AS manual,
               COALESCE((SELECT h.tipo FROM hoteles h
                  WHERE h.parcela_id = p.parcela_id AND NOT h.cerrado_def
                  ORDER BY h.habitaciones DESC NULLS LAST LIMIT 1), '') AS hotel
        FROM parcelas p WHERE p.survey_id = :sid AND p.geometry IS NOT NULL
    """), {"sid": survey_id}).fetchall()


def _cnpj_por_parcela(conn, survey_id: str) -> dict:
    """{parcela_id: [(situacao, numero_declarado)]}, con el mismo criterio de aterrizaje
    que `ParcelaCategoria` (ST_Contains + 5 m de tolerancia de borde) y acotado al bbox
    del relevamiento, sin el cual la consulta escanea el dump entero de la UF."""
    filas = conn.execute(text("""
        WITH bb AS (
            SELECT ST_Extent(geometry) AS box FROM parcelas
            WHERE survey_id = :sid AND geometry IS NOT NULL
        ), e AS (
            SELECT re.situacao, re.numero,
                   ST_SetSRID(ST_MakePoint(re.lng, re.lat), 4326) AS g
            FROM receita_estabelecimentos re, bb
            WHERE re.lat IS NOT NULL AND re.categoria IS NOT NULL
              AND ST_SetSRID(ST_MakePoint(re.lng, re.lat), 4326) && bb.box
        )
        SELECT p.parcela_id, e.situacao, e.numero
        FROM parcelas p JOIN e
          ON (ST_Contains(p.geometry, e.g)
              OR ST_DWithin(p.geometry::geography, e.g::geography, 5))
        WHERE p.survey_id = :sid AND p.descripcion_uso IS NOT NULL
    """), {"sid": survey_id}).fetchall()
    out: dict = defaultdict(list)
    for f in filas:
        out[f.parcela_id].append((f.situacao or "", f.numero))
    return out


def _unidades_chicas(conn, survey_id: str) -> dict:
    filas = conn.execute(text("""
        SELECT pu.parcela_id, pu.area_m2
        FROM parcela_unidades pu JOIN parcelas p ON p.parcela_id = pu.parcela_id
        WHERE p.survey_id = :sid AND pu.uso = 'residencial' AND pu.area_m2 < :a
    """), {"sid": survey_id, "a": _AREA_MIN_VIVIENDA}).fetchall()
    out: dict = defaultdict(list)
    for f in filas:
        out[f.parcela_id].append(float(f.area_m2))
    return out


def revisar(survey_id: str) -> list[dict]:
    """Un dict por hallazgo. Una parcela puede aparecer en varios chequeos."""
    from scrapitero.agents.dxf_entrega import _tipo_cliente
    from scrapitero.web.app import _tipo_edificacion

    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SET statement_timeout = '300s'"))
        parcelas = _parcelas(conn, survey_id)
        cnpj = _cnpj_por_parcela(conn, survey_id)
        chicas = _unidades_chicas(conn, survey_id)

    hallazgos = []

    def marca(codigo, p, nota):
        hallazgos.append({
            "chequeo": codigo, "cuadra": _cuadra(p.calle, p.numero),
            "calle": p.calle or "", "numero": p.numero or "", "insc": p.cca_code or "",
            "uf": f"{int(p.uv)}v+{int(p.uc)}c", "nota": nota,
            "parcela_id": str(p.parcela_id),
        })

    for p in parcelas:
        uv, uc = int(p.uv), int(p.uc)
        uso = (p.uso_principal or "").lower()
        establecimientos = cnpj.get(p.parcela_id, [])
        activos = [x for x in establecimientos if x[0] == "ATIVA"]

        # Cierre DEFINITIVO, con el mismo corte que `HotelFetcher`: BAIXADA/NULA cierran,
        # INAPTA/SUSPENSA no (la empresa sigue abierta, con la situação a la vista). Si
        # ninguno de los que sellan la parcela sigue en pie, la etiqueta es de un negocio
        # que ya no está.
        cerrados = [x for x in establecimientos if (x[0] or "").upper() in ("BAIXADA", "NULA")]
        if p.descripcion_uso and establecimientos and len(cerrados) == len(establecimientos):
            marca("A", p, f"{len(establecimientos)} establecimiento(s), todos dados de baja "
                          f"→ {p.descripcion_uso[:40]}")
        elif p.descripcion_uso and establecimientos and not activos:
            situaciones = ", ".join(sorted({s or "?" for s, _ in establecimientos}))
            marca("A", p, f"{len(establecimientos)} sin ninguno activo ({situaciones}) "
                          f"→ {p.descripcion_uso[:40]}")

        propio = _num(p.numero)
        ajenos = {_num(n) for _, n in establecimientos if _num(n)} - {propio}
        if p.descripcion_uso and propio and ajenos:
            marca("B", p, f"sellada por CNPJ declarado en el nº {', '.join(sorted(ajenos))}")

        if uv > 0 and uc == 0 and activos:
            marca("C", p, f"{len(activos)} CNPJ activo(s) → {(p.descripcion_uso or '')[:40]}")

        if uv > 0 and uv == uc:
            marca("D", p, f"{uv} vivienda(s) y {uv} comercio(s) en el mismo lote")

        if p.parcela_id in chicas:
            areas = sorted(chicas[p.parcela_id])
            medidas = ", ".join(f"{a:.1f}" for a in areas[:4])
            marca("E", p, f"{len(areas)} unidad(es) de {medidas} m²")

        if (uv + uc) > 0 and not propio:
            est = f" → sale como ({p.numero_estimado})" if p.numero_estimado else " → sale S/N"
            marca("F", p, f"número {p.numero!r} con {uv + uc} unidades{est}")

        if uso in ("vacante", "baldio") and (uv + uc) > 0:
            marca("G", p, f"declarada vacante con {uv + uc} unidad(es)")

        if (uv + uc) < 6:      # los MDU no llevan TIPOIMOVEL: no hay contradicción posible
            tp = _tipo_cliente(uso, uv, uc)
            etiqueta = _tipo_edificacion(p.uso_principal, uv, p.area_m2_construida,
                                         p.descripcion_uso, p.hotel or None, p.manual,
                                         uf_c=uc, huella=p.huella_m2) or ""
            e = etiqueta.upper()
            com = e.startswith(("COMÉRCIO", "COMERCIO"))
            res = e.startswith(("RESIDÊNCIA", "RESIDENCIA", "APARTAMENTO"))
            if (tp == "R" and com) or (tp == "C" and res):
                marca("H", p, f"el plano dice {tp} y TIPOIMOVEL dice {etiqueta[:30]}")

    return hallazgos


def informar(nombre: str, hallazgos: list[dict], detalle: set[str], top: int) -> None:
    print(f"\n=== {nombre} ===")
    if not hallazgos:
        print("  sin hallazgos")
        return
    por_chequeo = Counter(h["chequeo"] for h in hallazgos)
    for c, desc in CHEQUEOS.items():
        n = por_chequeo.get(c, 0)
        print(f"  {c}  {n:4}  {desc}" + ("" if n else "   ✓"))

    por_cuadra: dict = defaultdict(Counter)
    for h in hallazgos:
        por_cuadra[h["cuadra"]][h["chequeo"]] += 1
    ranking = sorted(por_cuadra.items(), key=lambda kv: -sum(kv[1].values()))
    print(f"\n  cuadras con más hallazgos (de {len(por_cuadra)} con al menos uno):")
    for cuadra, cnt in ranking[:top]:
        detalle_txt = " ".join(f"{k}×{v}" for k, v in sorted(cnt.items()))
        print(f"    {sum(cnt.values()):3}  {cuadra[:46]:48} {detalle_txt}")

    for c in sorted(detalle):
        filas = [h for h in hallazgos if h["chequeo"] == c]
        print(f"\n  — detalle {c}: {CHEQUEOS[c]} ({len(filas)}) —")
        for h in filas:
            print(f"    {h['calle'][:26]:28} {str(h['numero'] or 'S/N'):>7} "
                  f"insc {h['insc']:>7} {h['uf']:>9} · {h['nota']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--survey", help="UUID del relevamiento")
    ap.add_argument("--todos", action="store_true", help="todos los que tengan parcelas")
    ap.add_argument("--detalle", default="", help="listar los casos de estos chequeos, p.ej. A,C")
    ap.add_argument("--top", type=int, default=12, help="cuántas cuadras mostrar")
    ap.add_argument("--csv", help="volcar todos los hallazgos a un CSV")
    args = ap.parse_args()

    if not args.survey and not args.todos:
        ap.error("hace falta --survey o --todos")

    engine = get_engine()
    with engine.connect() as conn:
        if args.todos:
            surveys = conn.execute(text("""
                SELECT s.survey_id, r.name, COUNT(p.parcela_id) AS n
                FROM surveys s JOIN regions r ON r.region_id = s.region_id
                JOIN parcelas p ON p.survey_id = s.survey_id AND p.geometry IS NOT NULL
                GROUP BY 1, 2 HAVING COUNT(p.parcela_id) > 0
                ORDER BY MAX(s.started_at) DESC
            """)).fetchall()
        else:
            surveys = conn.execute(text("""
                SELECT s.survey_id, r.name, 0 FROM surveys s
                JOIN regions r ON r.region_id = s.region_id WHERE s.survey_id = :sid
            """), {"sid": args.survey}).fetchall()
    if not surveys:
        print("no hay relevamientos que revisar")
        sys.exit(1)

    detalle = {c.strip().upper() for c in args.detalle.split(",") if c.strip()}
    todos: list[dict] = []
    for sid, nombre, n in surveys:
        hallazgos = revisar(str(sid))
        for h in hallazgos:
            h["relevamiento"] = nombre
        titulo = f"{nombre} ({str(sid)[:8]}" + (f", {n} parcelas)" if n else ")")
        informar(titulo, hallazgos, detalle, args.top)
        todos.extend(hallazgos)

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["relevamiento", "chequeo", "cuadra", "calle",
                                               "numero", "insc", "uf", "nota", "parcela_id"])
            w.writeheader()
            w.writerows(todos)
        print(f"\n{len(todos)} hallazgos volcados en {args.csv}")

    sys.exit(1 if todos else 0)


if __name__ == "__main__":
    main()
