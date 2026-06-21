"""GeocodeCheck — control de calidad del geocoding de un baseline (relevamiento anterior).

Cruza la coordenada **ya guardada** de cada dirección (la que puso Google/Nominatim al
geocodificar el baseline) contra una **segunda fuente independiente y gratis**:
`geocodebr` (CNEFE/IBGE, offline, batch). Si las dos difieren más de `threshold_m`
(default 50 m), la dirección queda marcada para **revisar** — es señal de que el geocoding
guardado probablemente cayó en el lugar equivocado.

Es **solo lectura / reporte**: NO toca la DB. Devuelve los conteos + la lista de las
direcciones discrepantes (con ambas coordenadas y la distancia), ordenada por distancia.

Limitaciones:
- Brasil-only: geocodebr cubre Brasil. Fuera de Brasil no hay una segunda fuente gratis
  (Nominatim sería la única, y Google es pago), así que el cruce no aplica.
- Solo se puede verificar una dirección si geocodebr la ubica con precisión útil. Por
  default solo se **marca** (revisar) cuando geocodebr la resolvió a nivel de **número**
  (`solo_numero=True`); las que geocodebr ubica más grueso (calle/CEP) se cuentan aparte
  como "no verificables" para no generar falsos positivos.
- Discrepancia ≠ saber cuál de las dos está bien: >threshold significa "necesita revisión".
"""

from __future__ import annotations

import math
from typing import Optional

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents import geo
from scrapitero.agents._run import agent_run
from scrapitero.agents.geocode_forward import geocodebr_lote, uf_de_municipio_codigo
from scrapitero.db.engine import get_engine


class GeocodeCheckInput(BaseModel):
    baseline_id: str
    threshold_m: float = 50.0          # distancia a partir de la cual se marca "revisar"
    geocodebr_max_desvio_m: float = 300.0  # aceptar la coord de geocodebr solo si desvío ≤ esto
    solo_numero: bool = True           # marcar solo si geocodebr resolvió a nivel de número
    max_detalle: int = 100             # tope de filas en la lista de detalle (cuenta total igual)


class GeocodeCheckOutput(BaseModel):
    ok: bool = False
    error: Optional[str] = None
    baseline_id: str = ""
    threshold_m: float = 50.0
    total_con_coord: int = 0           # direcciones con coordenada guardada (candidatas a cruce)
    geocodebr_resolvio: int = 0        # cuántas ubicó geocodebr con precisión útil
    comparadas: int = 0                # geocodebr a nivel número (cruce válido)
    coinciden: int = 0                 # distancia ≤ threshold
    revisar: int = 0                   # distancia > threshold
    no_verificable: int = 0            # geocodebr grueso (calle/CEP) — no concluyente
    sin_segunda_fuente: int = 0        # geocodebr no ubicó la dirección
    por_fuente_guardada: dict = {}     # {google: {revisar, comparadas}, nominatim: {...}}
    detalle: list = []                 # top max_detalle por distancia desc


def _haversine_m(lat1, lng1, lat2, lng2) -> float:
    """Distancia en metros entre dos puntos (lat/lng en grados)."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


@agent_run
def run(input: GeocodeCheckInput) -> GeocodeCheckOutput:
    out = GeocodeCheckOutput(baseline_id=input.baseline_id, threshold_m=input.threshold_m)
    engine = get_engine()

    with engine.connect() as conn:
        meta = conn.execute(text("""
            SELECT b.ciudad, r.municipio_codigo
            FROM baselines b LEFT JOIN regions r ON r.region_id = b.region_id
            WHERE b.baseline_id=:b"""), {"b": input.baseline_id}).first()
        ciudad_def = meta[0] if meta else None
        municipio_codigo = meta[1] if meta else None
        filas = conn.execute(text("""
            SELECT id::text, direccion_raw, calle, numero, barrio, ciudad, estado,
                   lat, lng, geocode_source, cep
            FROM baseline_direcciones
            WHERE baseline_id=:b AND lat IS NOT NULL AND lng IS NOT NULL
        """), {"b": input.baseline_id}).fetchall()

    out.total_con_coord = len(filas)
    if not filas:
        out.ok = True
        return out

    # UF para geocodebr: imprescindible (estado vacío crashea geocodebr). Primero del
    # código IBGE de la región; si está en NULL, se infiere de una coordenada guardada.
    uf = uf_de_municipio_codigo(municipio_codigo)
    if not uf:
        # Inferir del CENTROIDE de las coords guardadas (promedio): robusto a geocodes
        # sueltos que cayeron en otro estado (un punto solo daría una UF equivocada).
        clat = sum(f[7] for f in filas) / len(filas)
        clng = sum(f[8] for f in filas) / len(filas)
        uf = uf_de_municipio_codigo(geo.detect_municipio_br(clat, clng))
        logger.info(f"geocode_check: UF de región en NULL — inferida del centroide: '{uf}'")

    # Coord guardada + datos estructurados para geocodebr, indexados por id.
    guardado: dict[str, dict] = {}
    items: list[dict] = []
    for r in filas:
        rid = r[0]
        guardado[rid] = {
            "direccion_raw": r[1], "lat": r[7], "lng": r[8],
            "source": r[9] or "?",
        }
        items.append({
            "id": rid,
            "logradouro": r[2] or "",
            "numero": r[3] or "",
            "bairro": r[4] or "",
            "municipio": r[5] or ciudad_def or "",
            "estado": r[6] or uf,
            "cep": r[10] or "",
        })

    # Segunda fuente: geocodebr en lote (gratis/offline). {id: (lat, lng, "g:precisao")}
    segunda = geocodebr_lote(items, uf=uf, max_desvio_m=input.geocodebr_max_desvio_m)
    out.geocodebr_resolvio = len(segunda)
    if not segunda:
        logger.warning(
            "geocode_check: geocodebr no ubicó ninguna dirección "
            "(¿sin R/geocodebr, o direcciones no encontradas en CNEFE?)")
        out.ok = True
        return out

    flagged: list[dict] = []
    porf: dict[str, dict] = {}
    for rid, (glat, glng, gsrc) in segunda.items():
        g = guardado.get(rid)
        if not g:
            continue
        precision = gsrc.split(":", 1)[1] if ":" in gsrc else gsrc
        dist = _haversine_m(g["lat"], g["lng"], glat, glng)

        # geocodebr grueso (no a nivel de número) → no concluyente si solo_numero.
        if input.solo_numero and precision != "numero":
            out.no_verificable += 1
            continue

        out.comparadas += 1
        bucket = porf.setdefault(g["source"], {"comparadas": 0, "revisar": 0})
        bucket["comparadas"] += 1
        if dist > input.threshold_m:
            out.revisar += 1
            bucket["revisar"] += 1
            flagged.append({
                "id": rid,
                "direccion": g["direccion_raw"],
                "fuente_guardada": g["source"],
                "lat_guardada": round(g["lat"], 6), "lng_guardada": round(g["lng"], 6),
                "lat_geocodebr": round(glat, 6), "lng_geocodebr": round(glng, 6),
                "precision_geocodebr": precision,
                "distancia_m": round(dist, 1),
            })
        else:
            out.coinciden += 1

    out.sin_segunda_fuente = out.total_con_coord - out.geocodebr_resolvio
    out.por_fuente_guardada = porf
    flagged.sort(key=lambda d: d["distancia_m"], reverse=True)
    out.detalle = flagged[:input.max_detalle]
    out.ok = True
    logger.info(
        f"geocode_check {input.baseline_id}: comparadas={out.comparadas} "
        f"coinciden={out.coinciden} revisar={out.revisar} "
        f"no_verificable={out.no_verificable} sin_2da={out.sin_segunda_fuente}")
    return out
