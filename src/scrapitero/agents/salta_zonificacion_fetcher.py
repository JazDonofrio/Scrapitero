"""SaltaZonificacionFetcher — clasifica uso_principal de parcelas por zonificación CPUA.

Fuente: IDEMSA WFS público — sin autenticación, sin costo.
  URL:   https://geocloud.municipalidadsalta.gob.ar/geoserver/wfs
  Layer: public:zonificaicon_usos_del_suelo102019
  Datos: 178 polígonos del Código de Planeamiento Urbano Ambiental (CPUA 2019)
         Cobertura: ciudad de Salta Capital.

Algoritmo:
  1. Descarga los 178 polígonos CPUA una sola vez.
  2. Construye un índice espacial STRtree.
  3. Para cada parcela de la región, localiza el polígono que contiene su centroide.
  4. Mapea el campo `distrito` al uso_principal y actualiza la DB.

Mapeo de distritos:
  R1-R6, Apto R6                → residencial
  R3/R5_Corredor Comercial      → mixto
  NC1-NC4                       → comercial
  M1-M6, MA, AC*, Corredores   → mixto
  PI                            → industrial
  AGR, Area Rural               → vacante
  AE-*, EP, PSM, Espacios Verdes, Red Vial → equipamiento

Parcelas fuera de la cobertura CPUA (interior provincial) quedan sin_datos.
"""

from __future__ import annotations

from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from shapely.geometry import Point, shape
from shapely.strtree import STRtree
from sqlalchemy import text

from scrapitero.db.engine import get_engine
from scrapitero.agents._run import agent_run


# ── WFS ───────────────────────────────────────────────────────────────────────

_WFS_URL = "https://geocloud.municipalidadsalta.gob.ar/geoserver/wfs"
_LAYER   = "public:zonificaicon_usos_del_suelo102019"

# ── Mapeo completo distrito CPUA → uso_principal ──────────────────────────────

_USO_MAP: dict[str, str] = {
    # Residencial
    "R1": "residencial",
    "R2": "residencial",
    "R3": "residencial",
    "R4": "residencial",
    "R5": "residencial",
    "R6": "residencial",
    "Apto R6": "residencial",
    "AR4_Zona Residencial Dominante_Tipología 2": "residencial",
    # Comercial
    "NC1": "comercial",
    "NC2": "comercial",
    "NC3": "comercial",
    "NC4": "comercial",
    "AR4_Zona Comercial Exclusiva_Tipología 1": "comercial",
    # Mixto (residencial + comercio conviven)
    "R3_Corredor Comercial": "mixto",
    "R5_Corredor Comercial": "mixto",
    "M1_Este": "mixto",
    "M1_Oeste": "mixto",
    "M2": "mixto",
    "M3": "mixto",
    "M4": "mixto",
    "M5": "mixto",
    "M6": "mixto",
    "MA": "mixto",
    "ACc": "mixto",
    "ACe": "mixto",
    "ACn": "mixto",
    "ACs": "mixto",
    "Corredor Avda. Belgrano": "mixto",
    "AR4_Zona Mixto_Tipología 1 y 2": "mixto",
    # Industrial
    "PI": "industrial",
    # Vacante / rural / sin uso urbano
    "AGR": "vacante",
    "Area Rural": "vacante",
    # Equipamiento (uso institucional/público — no residencial ni comercial)
    "AE-EP": "equipamiento",
    "AE-ES": "equipamiento",
    "AE-NA": "equipamiento",
    "AE-NG": "equipamiento",
    "AE-PN": "equipamiento",
    "AE-RE": "equipamiento",
    "AE-RN": "equipamiento",
    "EP": "equipamiento",
    "PSM": "equipamiento",
    "Espacios Verdes": "equipamiento",
    "Red Vial": "equipamiento",
}


# ── Pydantic I/O ──────────────────────────────────────────────────────────────

class SaltaZonifInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None
    overwrite: bool = False   # si True, reclasifica parcelas que ya tienen uso_principal


class SaltaZonifOutput(BaseModel):
    ok: bool
    parcelas_procesadas: int = 0
    parcelas_clasificadas: int = 0
    parcelas_sin_cobertura: int = 0
    distribucion: dict[str, int] = {}
    error: Optional[str] = None


# ── Descarga de polígonos CPUA ────────────────────────────────────────────────

def _fetch_cpua() -> list[tuple[object, str]]:
    """Devuelve lista de (shapely_polygon, uso_principal) para los 178 distritos."""
    logger.info(f"CPUA: descargando zonificación desde IDEMSA WFS…")
    with httpx.Client(timeout=30) as client:
        r = client.get(_WFS_URL, params={
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": _LAYER,
            "count": 200,
            "outputFormat": "application/json",
            "srsName": "EPSG:4326",
        })
        r.raise_for_status()

    features = r.json().get("features", [])
    logger.info(f"CPUA: {len(features)} polígonos descargados")

    result = []
    sin_mapa = set()
    for feat in features:
        distrito = feat.get("properties", {}).get("distrito") or ""
        uso = _USO_MAP.get(distrito)
        if uso is None:
            sin_mapa.add(distrito)
            uso = "sin_datos"
        geom_raw = feat.get("geometry")
        if geom_raw is None:
            logger.debug(f"CPUA: geometría nula en distrito '{distrito}' — saltado")
            continue
        try:
            geom = shape(geom_raw).buffer(0)
            result.append((geom, uso, distrito))
        except Exception as e:
            logger.warning(f"CPUA: geometría inválida en distrito '{distrito}': {e}")

    if sin_mapa:
        logger.warning(f"CPUA: distritos sin mapeo (quedan 'sin_datos'): {sorted(sin_mapa)}")

    return result  # list[(geom, uso, distrito)]


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_parcelas(region_id: str, survey_id: Optional[str], overwrite: bool) -> list[tuple]:
    """Devuelve (parcela_id, centroid_lat, centroid_lng) de la región."""
    engine = get_engine()
    with engine.connect() as conn:
        base = """
            SELECT parcela_id::text, centroid_lat, centroid_lng
            FROM parcelas
            WHERE region_id = :rid
              AND centroid_lat IS NOT NULL
              AND centroid_lng IS NOT NULL
        """
        params: dict = {"rid": region_id}
        if not overwrite:
            base += " AND (uso_principal IS NULL OR uso_principal = 'sin_datos')"
        if survey_id:
            base += " AND survey_id = :sid"
            params["sid"] = survey_id
        return conn.execute(text(base), params).fetchall()


def _update_batch(updates: list[tuple[str, str]]) -> None:
    """Actualiza uso_principal en lote. updates = [(parcela_id, uso), ...]

    Regla de UF mínimas:
      - residencial → siempre al menos 1 UF de vivienda (una vivienda mínima por
                      parcela). Setea tanto unidades_funcionales_estimadas como
                      uf_vivienda con GREATEST(…, 1) para no pisar un conteo real mayor.
      - vacante     → 0 UF (terreno baldío, sin unidad)
      - resto       → no se toca (lo resuelve otra fuente)
    """
    if not updates:
        return
    engine = get_engine()
    with engine.begin() as conn:
        # CAST(:uso AS text) en todas las apariciones: sin el cast, Postgres deduce
        # el parámetro como varchar (asignación) y text (comparación) → AmbiguousParameter.
        conn.execute(text("""
            UPDATE parcelas SET
                uso_principal = CAST(:uso AS text),
                uso_fuente = 'cpua',
                unidades_funcionales_estimadas = CASE
                    WHEN CAST(:uso AS text) = 'residencial'
                        THEN GREATEST(COALESCE(unidades_funcionales_estimadas, 0), 1)
                    WHEN CAST(:uso AS text) = 'vacante' THEN 0
                    ELSE unidades_funcionales_estimadas
                END,
                uf_vivienda = CASE
                    WHEN CAST(:uso AS text) = 'residencial'
                        THEN GREATEST(COALESCE(uf_vivienda, 0), 1)
                    WHEN CAST(:uso AS text) = 'vacante' THEN 0
                    ELSE uf_vivienda
                END
            WHERE parcela_id = :pid
        """), [{"pid": pid, "uso": uso} for pid, uso in updates])


# ── Clasificación espacial ────────────────────────────────────────────────────

def _build_index(cpua: list[tuple]) -> tuple[STRtree, list[tuple]]:
    geoms = [item[0] for item in cpua]
    return STRtree(geoms), cpua


def _classify_point(lat: float, lng: float, tree: STRtree, cpua: list[tuple]) -> str:
    pt = Point(lng, lat)
    # 'intersects' es equivalente a 'contains' para puntos en Shapely STRtree
    idxs = tree.query(pt, predicate="intersects")
    if len(idxs) == 0:
        return "sin_datos"
    # Si hay múltiples (overlap en bordes), tomar el primer match
    return cpua[idxs[0]][1]


# ── Entry point ───────────────────────────────────────────────────────────────

@agent_run
def run(inp: SaltaZonifInput) -> SaltaZonifOutput:
    # 1. Descargar CPUA
    try:
        cpua = _fetch_cpua()
    except Exception as e:
        logger.exception("SaltaZonificacion: error descargando CPUA")
        return SaltaZonifOutput(ok=False, error=f"Error WFS IDEMSA: {e}")

    if not cpua:
        return SaltaZonifOutput(ok=False, error="CPUA: 0 polígonos recibidos del WFS")

    tree, cpua_list = _build_index(cpua)

    # 2. Parcelas a clasificar
    parcelas = _get_parcelas(inp.region_id, inp.survey_id, inp.overwrite)
    if not parcelas:
        msg = (
            f"Sin parcelas con coordenadas {'pendientes de clasificar ' if not inp.overwrite else ''}"
            f"en '{inp.region_id}'. "
            "Ejecutá SaltaCatastroFetcher primero, o usá overwrite=true para reclasificar."
        )
        logger.warning(f"SaltaZonificacion: {msg}")
        return SaltaZonifOutput(ok=True, error=msg)

    logger.info(
        f"SaltaZonificacion: {len(parcelas)} parcelas a clasificar en '{inp.region_id}' "
        f"({'overwrite' if inp.overwrite else 'solo sin clasificar'})"
    )

    # 3. Clasificar
    updates: list[tuple[str, str]] = []
    sin_cobertura = 0
    distribucion: dict[str, int] = {}

    for i, (parcela_id, lat, lng) in enumerate(parcelas):
        uso = _classify_point(lat, lng, tree, cpua_list)

        if uso == "sin_datos":
            sin_cobertura += 1
        else:
            updates.append((parcela_id, uso))
            distribucion[uso] = distribucion.get(uso, 0) + 1

        if (i + 1) % 500 == 0:
            logger.info(
                f"  {i+1}/{len(parcelas)} — "
                f"{len(updates)} clasificadas, {sin_cobertura} sin cobertura"
            )

    # 4. Persistir en lote
    _update_batch(updates)

    logger.info(
        f"SaltaZonificacion completo '{inp.region_id}': "
        f"{len(updates)} clasificadas, {sin_cobertura} fuera de cobertura CPUA. "
        f"Distribución: {distribucion}"
    )

    if sin_cobertura > 0:
        logger.warning(
            f"SaltaZonificacion: {sin_cobertura} parcelas fuera de cobertura CPUA. "
            "La capa CPUA cubre solo la ciudad de Salta Capital. "
            "Para el interior provincial no hay datos de zonificación disponibles."
        )

    return SaltaZonifOutput(
        ok=True,
        parcelas_procesadas=len(parcelas),
        parcelas_clasificadas=len(updates),
        parcelas_sin_cobertura=sin_cobertura,
        distribucion=distribucion,
    )
