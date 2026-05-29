"""ARBACadastralFetcher — descarga geometrías de parcelas desde IDERA WFS.

Fuente: geo.arba.gov.ar/geoserver/idera/wfs (layer idera:Parcela)
Filtro: CQL cca LIKE '{prefix}%' donde prefix se construye desde partido/circ/secc/manzana.

Output: filas insertadas en tabla `parcelas` con geometría y centroide.
"""

from __future__ import annotations

import uuid
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from shapely.geometry import shape
from shapely.ops import transform as shp_transform
import pyproj
from sqlalchemy import text

from scrapitero.db.engine import get_engine


IDERA_WFS = "https://geo.arba.gov.ar/geoserver/idera/wfs"


# ── Pydantic I/O ──────────────────────────────────────────────────────────────

class ARBAInput(BaseModel):
    region_id: str                          # "ituzaingo-ba-ar"
    survey_id: str
    partido_id: str                         # "136"
    circunscripcion: str                    # "2"
    seccion: str                            # "C"
    manzana: str                            # "184"


class ARBAOutput(BaseModel):
    ok: bool
    parcelas_insertadas: int = 0
    parcelas_actualizadas: int = 0
    fuentes: list[str] = []
    error: Optional[str] = None


# ── CCA prefix ────────────────────────────────────────────────────────────────

def _cca_prefix(partido: str, circ: str, secc: str, mza: str) -> str:
    """Construye el prefijo CCA para filtrar en IDERA WFS."""
    return partido.zfill(3) + circ.zfill(2) + "0" + secc.upper() + "0" * 21 + mza.zfill(4)


def _parc_num_from_cca(cca: str) -> int:
    return int(cca[32:39].lstrip("0") or "0")


# ── IDERA WFS ─────────────────────────────────────────────────────────────────

def fetch_idera(partido: str, circ: str, secc: str, mza: str) -> list[dict]:
    """
    Llama al WFS de IDERA y devuelve lista de features GeoJSON para la manzana.
    Cada feature incluye cca, pda y geometría.
    """
    prefix = _cca_prefix(partido, circ, secc, mza)
    logger.info(f"IDERA WFS — CCA prefix: {prefix}")

    with httpx.Client(timeout=30) as client:
        r = client.get(IDERA_WFS, params={
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": "idera:Parcela",
            "CQL_FILTER": f"cca LIKE '{prefix}%'",
            "srsName": "EPSG:4326",
            "outputFormat": "application/json",
        })
        r.raise_for_status()

    features = r.json().get("features", [])
    logger.info(f"IDERA WFS devolvió {len(features)} features")
    return features


def _centroid_from_feature(feat: dict) -> tuple[Optional[float], Optional[float]]:
    """Calcula centroide promediando vértices del primer anillo."""
    geom = feat.get("geometry", {})
    gtype = geom.get("type", "")
    try:
        if gtype == "MultiPolygon":
            coords = geom["coordinates"][0][0]
        elif gtype == "Polygon":
            coords = geom["coordinates"][0]
        else:
            return None, None
        lon = sum(c[0] for c in coords) / len(coords)
        lat = sum(c[1] for c in coords) / len(coords)
        return round(lat, 7), round(lon, 7)
    except Exception:
        return None, None


def _area_m2(geom) -> Optional[float]:
    try:
        proj = pyproj.Transformer.from_crs(
            "EPSG:4326", "EPSG:32721", always_xy=True
        ).transform
        return round(shp_transform(proj, geom).area, 2)
    except Exception:
        return None


# ── Upsert en DB ──────────────────────────────────────────────────────────────

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

            cca = str(props.get("cca", "")).strip()
            pda = str(props.get("pda", "")).strip()

            try:
                geom = shape(geom_raw)
                if not geom.is_valid:
                    geom = geom.buffer(0)
                # PostGIS columna Polygon — convertir MultiPolygon al polígono mayor
                if geom.geom_type == "MultiPolygon":
                    geom = max(geom.geoms, key=lambda g: g.area)
            except Exception as e:
                logger.warning(f"Geometría inválida para cca={cca}: {e}")
                continue

            lat, lng = _centroid_from_feature(feat)
            area = _area_m2(geom)
            geom_wkt = geom.wkt

            existing = conn.execute(text("""
                SELECT parcela_id FROM parcelas
                WHERE region_id = :region AND fuente_parcela = 'arba_idera'
                  AND ABS(centroid_lat - :lat) < 0.00005
                  AND ABS(centroid_lng - :lng) < 0.00005
            """), {"region": region_id, "lat": lat, "lng": lng}).fetchone()

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
                         :area, 'arba_idera')
                """), {
                    "pid": str(new_id), "sid": survey_id, "region": region_id,
                    "geom": geom_wkt, "lat": lat, "lng": lng, "area": area,
                })
                insertadas += 1

    logger.info(f"DB: {insertadas} insertadas, {actualizadas} actualizadas")
    return insertadas, actualizadas


# ── Entry point ───────────────────────────────────────────────────────────────

def run(input: ARBAInput) -> ARBAOutput:
    try:
        features = fetch_idera(
            input.partido_id, input.circunscripcion,
            input.seccion, input.manzana
        )
        if not features:
            return ARBAOutput(
                ok=False,
                error=(
                    f"IDERA WFS no devolvió parcelas para "
                    f"Partido={input.partido_id} Circ={input.circunscripcion} "
                    f"Secc={input.seccion} Mza={input.manzana}. "
                    "Verificar nomenclatura."
                )
            )

        insertadas, actualizadas = _upsert_parcelas(
            features, input.region_id, input.survey_id
        )
        return ARBAOutput(
            ok=True,
            parcelas_insertadas=insertadas,
            parcelas_actualizadas=actualizadas,
            fuentes=["idera_wfs"],
        )
    except httpx.HTTPStatusError as e:
        return ARBAOutput(ok=False, error=f"IDERA HTTP {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        logger.exception("ARBACadastralFetcher falló")
        return ARBAOutput(ok=False, error=str(e))
