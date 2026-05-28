"""ARBACadastralFetcher — descarga parcelas catastrales de Buenos Aires Province via ARBA WFS.

Fuente:
  - ARBA (Agencia de Recaudación de la Provincia de Buenos Aires)
  - Servicio WFS público: https://geo.arba.gov.ar/geoserver/irisas/ows

Permite filtrar por partido, circunscripción, sección y/o manzana.
Output: filas insertadas en tabla `parcelas`.
"""

from __future__ import annotations

import uuid
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from shapely.geometry import shape, mapping
from shapely.ops import transform
import pyproj
from sqlalchemy import text

from scrapitero.db.engine import get_engine


# ── Configuración ARBA WFS ────────────────────────────────────────────────────

ARBA_WFS_URL = "https://geo.arba.gov.ar/geoserver/irisas/ows"
ARBA_LAYER = "irisas:parcelas"

# Nombres de campos en el WFS de ARBA (probados contra GetCapabilities)
# Si el WFS cambia de nombres, ajustar aquí.
FIELD_PARTIDO     = "partido_id"
FIELD_CIRC        = "circ_id"
FIELD_SECC        = "secc_id"
FIELD_MANZ        = "manz_id"
FIELD_PARCELA_NUM = "parce_id"
FIELD_SUP_M2      = "sup"       # superficie en m² (puede ser None)


# ── Pydantic I/O ──────────────────────────────────────────────────────────────

class ARBAInput(BaseModel):
    region_id: str                          # "ituzaingo-ba-ar"
    survey_id: str                          # UUID del survey activo
    partido_id: str                         # "0136"
    circunscripcion: Optional[str] = None   # "2"
    seccion: Optional[str] = None           # "C"
    manzana: Optional[str] = None           # "0184"  (con ceros a izquierda o sin ellos)


class ARBAOutput(BaseModel):
    ok: bool
    parcelas_insertadas: int
    parcelas_actualizadas: int
    fuentes: list[str]
    error: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pad(value: str, length: int) -> str:
    """Rellena con ceros a izquierda (ej: '136' → '0136')."""
    return value.strip().zfill(length)


def _build_cql_filter(partido_id: str, circunscripcion: Optional[str],
                      seccion: Optional[str], manzana: Optional[str]) -> str:
    """Construye el filtro CQL para el WFS de ARBA."""
    parts = [f"{FIELD_PARTIDO}='{_pad(partido_id, 4)}'"]
    if circunscripcion:
        parts.append(f"{FIELD_CIRC}='{circunscripcion.strip()}'")
    if seccion:
        parts.append(f"{FIELD_SECC}='{seccion.strip().upper()}'")
    if manzana:
        parts.append(f"{FIELD_MANZ}='{_pad(manzana, 4)}'")
    return " AND ".join(parts)


def _fetch_wfs(cql_filter: str) -> list[dict]:
    """Llama al WFS de ARBA y devuelve lista de features GeoJSON."""
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": ARBA_LAYER,
        "outputFormat": "application/json",
        "CQL_FILTER": cql_filter,
        "srsName": "EPSG:4326",
    }
    logger.info(f"Consultando ARBA WFS: {cql_filter}")
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        r = client.get(ARBA_WFS_URL, params=params)
        r.raise_for_status()
    data = r.json()
    features = data.get("features", [])
    logger.info(f"ARBA WFS devolvió {len(features)} parcelas")
    return features


def _centroid(geom) -> tuple[Optional[float], Optional[float]]:
    """Devuelve (lat, lng) del centroide de una geometría Shapely."""
    try:
        c = geom.centroid
        return round(c.y, 7), round(c.x, 7)
    except Exception:
        return None, None


def _area_m2(geom) -> Optional[float]:
    """Calcula área en m² proyectando a UTM zona 21S (Buenos Aires)."""
    try:
        project = pyproj.Transformer.from_crs(
            "EPSG:4326", "EPSG:32721", always_xy=True
        ).transform
        from shapely.ops import transform as shp_transform
        projected = shp_transform(project, geom)
        return round(projected.area, 2)
    except Exception:
        return None


# ── Inserción en DB ───────────────────────────────────────────────────────────

def _upsert_parcelas(features: list[dict], region_id: str,
                     survey_id: str) -> tuple[int, int]:
    engine = get_engine()
    insertadas = 0
    actualizadas = 0

    with engine.begin() as conn:
        for feat in features:
            props = feat.get("properties", {})
            geom_raw = feat.get("geometry")

            if not geom_raw:
                continue

            try:
                geom = shape(geom_raw)
            except Exception as e:
                logger.warning(f"Geometría inválida: {e}")
                continue

            # Clave única: partido + circ + secc + manzana + parcela
            partido   = str(props.get(FIELD_PARTIDO, "")).strip()
            circ      = str(props.get(FIELD_CIRC, "")).strip()
            secc      = str(props.get(FIELD_SECC, "")).strip()
            manz      = str(props.get(FIELD_MANZ, "")).strip()
            parce_num = str(props.get(FIELD_PARCELA_NUM, "")).strip()

            # Usamos la clave catastral como identificador externo para dedup
            clave_catastral = f"{partido}-{circ}-{secc}-{manz}-{parce_num}"

            lat, lng = _centroid(geom)
            area = _area_m2(geom)
            geom_wkt = geom.wkt

            # Área de superficie desde ARBA si está disponible
            sup_arba = props.get(FIELD_SUP_M2)
            if sup_arba:
                try:
                    area = float(sup_arba)
                except (ValueError, TypeError):
                    pass

            # Buscar si ya existe (por clave catastral codificada en fuente_parcela)
            existing = conn.execute(
                text("""
                    SELECT parcela_id FROM parcelas
                    WHERE region_id = :region
                      AND fuente_parcela = 'arba_wfs'
                      AND centroid_lat = :lat
                      AND centroid_lng = :lng
                """),
                {"region": region_id, "lat": lat, "lng": lng}
            ).fetchone()

            if existing:
                conn.execute(text("""
                    UPDATE parcelas SET
                        geometry = ST_GeomFromText(:geom, 4326),
                        area_m2_terreno = :area
                    WHERE parcela_id = :pid
                """), {"geom": geom_wkt, "area": area, "pid": str(existing[0])})
                actualizadas += 1
            else:
                new_id = uuid.uuid4()
                conn.execute(text("""
                    INSERT INTO parcelas
                        (parcela_id, survey_id, region_id,
                         geometry, centroid_lat, centroid_lng,
                         area_m2_terreno, fuente_parcela)
                    VALUES
                        (:pid, :sid, :region,
                         ST_GeomFromText(:geom, 4326), :lat, :lng,
                         :area, 'arba_wfs')
                """), {
                    "pid": str(new_id),
                    "sid": survey_id,
                    "region": region_id,
                    "geom": geom_wkt,
                    "lat": lat,
                    "lng": lng,
                    "area": area,
                })
                insertadas += 1

    logger.info(f"DB: {insertadas} insertadas, {actualizadas} actualizadas")
    return insertadas, actualizadas


# ── Entry point ───────────────────────────────────────────────────────────────

def run(input: ARBAInput) -> ARBAOutput:
    fuentes = []
    try:
        cql = _build_cql_filter(
            input.partido_id, input.circunscripcion,
            input.seccion, input.manzana
        )
        features = _fetch_wfs(cql)
        fuentes.append("arba_wfs_parcelas")

        if not features:
            return ARBAOutput(
                ok=False,
                parcelas_insertadas=0,
                parcelas_actualizadas=0,
                fuentes=fuentes,
                error=(
                    f"ARBA WFS no devolvió parcelas para filtro: {cql}. "
                    "Verificar partido_id, circunscripcion, seccion, manzana."
                )
            )

        insertadas, actualizadas = _upsert_parcelas(
            features, input.region_id, input.survey_id
        )
        return ARBAOutput(
            ok=True,
            parcelas_insertadas=insertadas,
            parcelas_actualizadas=actualizadas,
            fuentes=fuentes,
        )

    except httpx.HTTPStatusError as e:
        logger.exception("ARBA WFS HTTP error")
        return ARBAOutput(
            ok=False, parcelas_insertadas=0, parcelas_actualizadas=0,
            fuentes=fuentes,
            error=f"ARBA WFS HTTP {e.response.status_code}: {e.response.text[:200]}"
        )
    except Exception as e:
        logger.exception("ARBACadastralFetcher falló")
        return ARBAOutput(
            ok=False, parcelas_insertadas=0, parcelas_actualizadas=0,
            fuentes=fuentes, error=str(e)
        )
