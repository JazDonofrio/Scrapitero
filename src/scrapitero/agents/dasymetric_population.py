"""DasymetricPopulation — estimación de habitantes por manzana (desagregación dasimétrica).

Estimación SECUNDARIA e independiente del relevamiento principal. Reparte la población
de los polígonos censales (`setores_censitarios.pop_total`) entre las parcelas usando un
peso de ocupación y agrega el resultado por MANZANA CATASTRAL. Es menos exacta que la UF
del relevamiento (de ahí que se muestre aparte, con su fecha de estimación).

Genérico: corre en cualquier zona que tenga los datos necesarios:
  - polígonos censales con `pop_total` que cubran la zona (join ESPACIAL por geometría,
    no por region_id — los setores pueden estar bajo otra región que englobe la zona),
  - parcelas con geometría y alguna señal de ocupación,
  - manzana catastral derivable de la fuente (ver `manzana_catastral.py`).

Método (dasimétrico ponderado, peso en cascada por parcela):
  1. peso = uf_vivienda  → si falta, volumen edificado Σ(area×pisos) → si falta,
     área de parcela residencial; vacante/comercial/industrial = 0 (sin residentes).
  2. corrección por cobertura: a cada setor se le asigna sólo la fracción de población
     proporcional al área de la zona que cae dentro del setor (evita volcar toda la
     población de un setor a unas pocas parcelas cuando la zona lo cubre en parte).
  3. habitantes_parcela = pop_asignable_setor × peso_parcela / Σ pesos del setor.
  4. agrega por manzana: Σ habitantes (con banda ±30 %), Σ uf_vivienda, Σ uf_comercio.

Output: filas en `manzanas_habitantes` (se reescriben por survey en cada corrida).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date
from typing import Optional

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.db.engine import get_engine
from scrapitero.agents._run import agent_run
from scrapitero.agents.manzana_catastral import manzana_codigo, fuentes_soportadas

# Banda de incertidumbre del reparto dasimétrico (±). Es una estimación gruesa.
_BAND = 0.30
_USOS_RESIDENCIALES = {"residencial", "mixto", None}


class DasymetricInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None   # si falta, se toma el survey más reciente de la región


class DasymetricOutput(BaseModel):
    ok: bool
    region_id: Optional[str] = None
    survey_id: Optional[str] = None
    manzanas: int = 0
    parcelas_procesadas: int = 0
    parcelas_sin_setor: int = 0          # sin polígono censal que las contenga
    parcelas_sin_manzana: int = 0        # fuente sin parser o código ilegible
    habitantes_total: float = 0.0
    pop_total_referencia: int = 0        # suma de pop de los setores que tocan la zona
    metodo_dominante: Optional[str] = None
    fecha_estimacion: Optional[str] = None
    error: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_survey(conn, region_id: str, survey_id: Optional[str]) -> Optional[str]:
    if survey_id:
        return survey_id
    row = conn.execute(text(
        "SELECT survey_id::text FROM surveys WHERE region_id = :r "
        "ORDER BY started_at DESC LIMIT 1"
    ), {"r": region_id}).fetchone()
    return row[0] if row else None


def _peso(uf_viv, uf_com, volumen, uso, parcela_area) -> tuple[float, str]:
    """Peso de ocupación de la parcela + método usado.

    Cascada:
      1. `uf_vivienda` es autoritativo cuando existe (incluido 0 → sin viviendas → sin
         residentes; en el sistema una parcela residencial siempre tiene uf_vivienda ≥ 1,
         así que uf=0 es señal fiable de no-residencial).
      2. señal no-residencial → peso 0 (vacante/comercial/industrial, o parcela con UF de
         comercio y sin UF de vivienda).
      3. volumen edificado Σ(area×pisos).
      4. área de la parcela, sólo si el uso es residencial/mixto/desconocido.
    """
    if uf_viv is not None:
        return float(uf_viv), "uf_vivienda"
    if uso in ("comercial", "industrial", "vacante"):
        return 0.0, "no_residencial"
    if uf_com is not None and uf_com > 0 and uso != "mixto":
        return 0.0, "no_residencial"
    if volumen and volumen > 0:
        return float(volumen), "volumen"
    if uso in _USOS_RESIDENCIALES:
        return float(parcela_area or 1.0), "area_residencial"
    return 0.0, "no_residencial"


_SQL_PARCELAS = """
SELECT p.parcela_id::text       AS parcela_id,
       p.uf_vivienda            AS uf_vivienda,
       p.uf_comercio            AS uf_comercio,
       p.uso_principal          AS uso_principal,
       p.fuente_parcela         AS fuente_parcela,
       p.cca_code               AS cca_code,
       p.nomenclatura_catastral AS nomenclatura_catastral,
       s.setor_id               AS setor_id,
       s.pop_total              AS pop_total,
       s.setor_area_m2          AS setor_area_m2,
       ST_Area(geography(p.geometry)) AS parcela_area_m2,
       COALESCE(vol.volumen, 0) AS volumen
FROM parcelas p
LEFT JOIN LATERAL (
    SELECT ss.setor_id, ss.pop_total,
           ST_Area(geography(ss.geometry)) AS setor_area_m2
    FROM setores_censitarios ss
    WHERE ss.pop_total IS NOT NULL AND ss.geometry IS NOT NULL
      AND ST_Contains(ss.geometry,
            ST_SetSRID(ST_MakePoint(p.centroid_lng, p.centroid_lat), 4326))
    LIMIT 1
) s ON TRUE
LEFT JOIN LATERAL (
    SELECT SUM(COALESCE(e.area_m2, 0) * GREATEST(COALESCE(e.pisos_estimados, 1), 1)) AS volumen
    FROM edificios e WHERE e.parcela_id = p.parcela_id
) vol ON TRUE
WHERE p.region_id = :rid
  AND (CAST(:sid AS uuid) IS NULL OR p.survey_id = CAST(:sid AS uuid))
  AND p.centroid_lat IS NOT NULL AND p.geometry IS NOT NULL
"""


# ── Entry point ───────────────────────────────────────────────────────────────

@agent_run
def run(inp: DasymetricInput) -> DasymetricOutput:
    engine = get_engine()
    with engine.connect() as conn:
        survey_id = _resolve_survey(conn, inp.region_id, inp.survey_id)
        if not survey_id:
            return DasymetricOutput(
                ok=False, region_id=inp.region_id,
                error=f"No hay survey para la región '{inp.region_id}'."
            )
        rows = conn.execute(text(_SQL_PARCELAS),
                            {"rid": inp.region_id, "sid": survey_id}).mappings().all()

    if not rows:
        return DasymetricOutput(
            ok=False, region_id=inp.region_id, survey_id=survey_id,
            error=f"No hay parcelas con geometría en la región '{inp.region_id}'."
        )

    # Precondición de datos: censo que cubra la zona.
    con_setor = [r for r in rows if r["setor_id"] is not None]
    if not con_setor:
        return DasymetricOutput(
            ok=False, region_id=inp.region_id, survey_id=survey_id,
            parcelas_procesadas=len(rows), parcelas_sin_setor=len(rows),
            error=("No hay polígonos censales con población que cubran la zona. "
                   "Cargá el censo primero (ibge_census_fetcher en Brasil, o el "
                   "equivalente del país) para poder repartir la población."),
        )

    # Precondición: manzana catastral derivable de la fuente.
    fuente = rows[0]["fuente_parcela"]
    if manzana_codigo(fuente, rows[0]["cca_code"], rows[0]["nomenclatura_catastral"]) is None \
            and fuente not in fuentes_soportadas():
        return DasymetricOutput(
            ok=False, region_id=inp.region_id, survey_id=survey_id,
            parcelas_procesadas=len(rows),
            error=(f"No hay parser de manzana catastral para la fuente '{fuente}'. "
                   f"Fuentes soportadas: {sorted(fuentes_soportadas())}. "
                   f"Agregá su parser en manzana_catastral.py."),
        )

    # 1) Acumular por setor: Σ peso (residencial) y Σ área (cobertura).
    setor_peso: dict = defaultdict(float)
    setor_area_zona: dict = defaultdict(float)
    setor_pop: dict = {}
    setor_area_total: dict = {}
    parcelas_sin_manzana = 0

    enriched = []
    for r in rows:
        peso, metodo = _peso(r["uf_vivienda"], r["uf_comercio"], r["volumen"],
                             r["uso_principal"], r["parcela_area_m2"])
        mz = manzana_codigo(r["fuente_parcela"], r["cca_code"], r["nomenclatura_catastral"])
        if mz is None:
            parcelas_sin_manzana += 1
        sid_setor = r["setor_id"]
        if sid_setor is not None:
            setor_peso[sid_setor] += peso
            setor_area_zona[sid_setor] += (r["parcela_area_m2"] or 0.0)
            setor_pop[sid_setor] = r["pop_total"] or 0
            setor_area_total[sid_setor] = r["setor_area_m2"] or 0.0
        enriched.append((r, peso, metodo, mz))

    # 2) Población asignable por setor (corrección por cobertura areal).
    setor_pop_asignable: dict = {}
    for sid_setor, pop in setor_pop.items():
        area_total = setor_area_total.get(sid_setor) or 0.0
        cobertura = 1.0
        if area_total > 0:
            cobertura = min(1.0, setor_area_zona[sid_setor] / area_total)
        setor_pop_asignable[sid_setor] = pop * cobertura

    # 3) Habitantes por parcela → agregación por manzana.
    mz_hab: dict = defaultdict(float)
    mz_ufv: dict = defaultdict(int)
    mz_ufc: dict = defaultdict(int)
    mz_n: dict = defaultdict(int)
    mz_pids: dict = defaultdict(list)
    mz_metodos: dict = defaultdict(lambda: defaultdict(int))

    for r, peso, metodo, mz in enriched:
        if mz is None:
            continue
        sid_setor = r["setor_id"]
        hab = 0.0
        if sid_setor is not None and setor_peso[sid_setor] > 0 and peso > 0:
            hab = setor_pop_asignable[sid_setor] * peso / setor_peso[sid_setor]
        mz_hab[mz] += hab
        mz_ufv[mz] += int(r["uf_vivienda"] or 0)
        mz_ufc[mz] += int(r["uf_comercio"] or 0)
        mz_n[mz] += 1
        mz_pids[mz].append(r["parcela_id"])
        mz_metodos[mz][metodo] += 1

    if not mz_hab:
        return DasymetricOutput(
            ok=False, region_id=inp.region_id, survey_id=survey_id,
            parcelas_procesadas=len(rows), parcelas_sin_manzana=parcelas_sin_manzana,
            error="No se pudo agregar ninguna manzana (sin manzana catastral derivable).",
        )

    fecha = date.today()
    metodo_global: dict = defaultdict(int)
    habitantes_total = 0.0

    # 4) Persistir (reescribe por survey).
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM manzanas_habitantes WHERE survey_id = CAST(:sid AS uuid)"),
                     {"sid": survey_id})
        for mz, hab in mz_hab.items():
            habitantes_total += hab
            metodo_mz = max(mz_metodos[mz].items(), key=lambda kv: kv[1])[0]
            metodo_global[metodo_mz] += 1
            conn.execute(text("""
                INSERT INTO manzanas_habitantes
                    (manzana_hab_id, survey_id, region_id, manzana_codigo, geometry,
                     habitantes_est, habitantes_low, habitantes_high,
                     uf_vivienda, uf_comercio, n_parcelas, metodo, fecha_estimacion)
                SELECT :id, CAST(:sid AS uuid), :rid, :codigo,
                       ST_Multi(ST_Union(p.geometry)),
                       :hab, :low, :high, :ufv, :ufc, :n, :metodo, :fecha
                FROM parcelas p
                WHERE p.parcela_id = ANY(CAST(:pids AS uuid[]))
            """), {
                "id": str(uuid.uuid4()), "sid": survey_id, "rid": inp.region_id,
                "codigo": mz, "hab": round(hab, 1),
                "low": round(hab * (1 - _BAND), 1), "high": round(hab * (1 + _BAND), 1),
                "ufv": mz_ufv[mz], "ufc": mz_ufc[mz], "n": mz_n[mz],
                "metodo": metodo_mz, "fecha": fecha,
                "pids": "{" + ",".join(mz_pids[mz]) + "}",
            })

    metodo_dominante = (max(metodo_global.items(), key=lambda kv: kv[1])[0]
                        if metodo_global else None)
    parcelas_sin_setor = len(rows) - len(con_setor)
    logger.info(
        f"Dasimétrico '{inp.region_id}': {len(mz_hab)} manzanas, "
        f"{round(habitantes_total)} hab estimados ({metodo_dominante}), "
        f"{parcelas_sin_setor} sin setor"
    )

    return DasymetricOutput(
        ok=True, region_id=inp.region_id, survey_id=survey_id,
        manzanas=len(mz_hab),
        parcelas_procesadas=len(rows),
        parcelas_sin_setor=parcelas_sin_setor,
        parcelas_sin_manzana=parcelas_sin_manzana,
        habitantes_total=round(habitantes_total, 1),
        pop_total_referencia=int(sum(setor_pop.values())),
        metodo_dominante=metodo_dominante,
        fecha_estimacion=fecha.isoformat(),
    )
