"""ONRSigefFetcher — descarga predios rurales georreferenciados de SIGEF/ONR.

Fuente: gis-mapas.onr.org.br ArcGIS REST → Hosted/SIGEF_082025/FeatureServer/0
Cubre todo Brasil. Requiere token ArcGIS de mapa.onr.org.br (se obtiene automáticamente).

Campos disponibles:
  nome_area   → nombre del predio (Fazenda XYZ)
  cnm         → Código Nacional de Matrícula
  matricula   → número de matrícula en el RI
  cnscartorio → CNS del cartório responsable
  status      → CERTIFICADA / REGISTRADA
  municipio_  → código IBGE del municipio
  Geometry    → polígono georreferenciado
"""

from __future__ import annotations

import math
import time
import uuid
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from shapely.geometry import shape
from sqlalchemy import text

from scrapitero.db.engine import get_engine
from scrapitero.agents.onr_token import get_token

GIS_BASE = "https://gis-mapas.onr.org.br/onrgisserver/rest/services/Hosted"
SIGEF_SERVICE = "SIGEF_082025/FeatureServer/0"
MAX_RECORDS = 2000
DELAY_S = 0.5


class SigefInput(BaseModel):
    region_id: str
    survey_id: str
    bbox_south: float
    bbox_west: float
    bbox_north: float
    bbox_east: float
    delay_ms: int = 500


class SigefOutput(BaseModel):
    ok: bool
    parcelas_inseridas: int = 0
    parcelas_atualizadas: int = 0
    total_features: int = 0
    error: Optional[str] = None


def _wgs84_to_webmerc(lat: float, lng: float) -> tuple[float, float]:
    x = lng * 20037508.34 / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) / (math.pi / 180)
    y = y * 20037508.34 / 180
    return x, y


def _bbox_webmerc(south: float, west: float, north: float, east: float) -> dict:
    xmin, ymin = _wgs84_to_webmerc(south, west)
    xmax, ymax = _wgs84_to_webmerc(north, east)
    return {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax,
            "spatialReference": {"wkid": 102100}}


def _query_features(token: str, bbox_wm: dict, offset: int = 0) -> list[dict]:
    import json
    params = {
        "f": "json",
        "token": token,
        "geometry": json.dumps(bbox_wm),
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "nome_area,cnm,matricula,cnscartorio,status,situacao_i,municipio_,uf_id,SHAPE__Area",
        "outSR": "4326",
        "returnGeometry": "true",
        "resultRecordCount": MAX_RECORDS,
        "resultOffset": offset,
    }
    url = f"{GIS_BASE}/{SIGEF_SERVICE}/query"
    with httpx.Client(timeout=30) as client:
        r = client.get(url, params=params)
        r.raise_for_status()
    return r.json().get("features", [])


def _upsert(features: list[dict], region_id: str, survey_id: str) -> tuple[int, int]:
    engine = get_engine()
    inserted = 0
    updated = 0

    with engine.begin() as conn:
        for ft in features:
            geom_raw = ft.get("geometry")
            attrs = ft.get("attributes", {})
            if not geom_raw:
                continue

            try:
                geom = shape(geom_raw)
                if not geom.is_valid:
                    geom = geom.buffer(0)
                if geom.geom_type == "MultiPolygon":
                    geom = max(geom.geoms, key=lambda g: g.area)
                centroid = geom.centroid
                area = round(geom.area * (111320 ** 2), 2)  # deg² → m² approx
            except Exception:
                continue

            cnm = (attrs.get("cnm") or "").strip()
            nome = (attrs.get("nome_area") or "").strip()
            matricula = str(attrs.get("matricula") or "").strip()
            status = (attrs.get("status") or "").strip()
            municipio = str(attrs.get("municipio_") or "").strip()

            existing = conn.execute(text("""
                SELECT parcela_id FROM parcelas
                WHERE region_id = :rid AND fuente_parcela = 'sigef_onr'
                  AND nomenclatura_catastral = :cnm
            """), {"rid": region_id, "cnm": cnm}).fetchone() if cnm else None

            if existing:
                conn.execute(text("""
                    UPDATE parcelas SET
                        survey_id = :sid,
                        geometry = ST_GeomFromText(:geom, 4326),
                        centroid_lat = :clat, centroid_lng = :clng,
                        area_m2_terreno = :area,
                        partida_inmobiliaria = :matricula,
                        municipio = :municipio
                    WHERE parcela_id = :pid
                """), {
                    "sid": survey_id,
                    "geom": geom.wkt,
                    "clat": centroid.y, "clng": centroid.x,
                    "area": area,
                    "matricula": matricula or None,
                    "municipio": municipio or None,
                    "pid": str(existing[0]),
                })
                updated += 1
            else:
                pid = str(uuid.uuid4())
                conn.execute(text("""
                    INSERT INTO parcelas (
                        parcela_id, survey_id, region_id,
                        geometry, centroid_lat, centroid_lng, area_m2_terreno,
                        nomenclatura_catastral, partida_inmobiliaria,
                        complemento, municipio, fuente_parcela
                    ) VALUES (
                        :pid, :sid, :rid,
                        ST_GeomFromText(:geom, 4326), :clat, :clng, :area,
                        :cnm, :matricula,
                        :nome, :municipio, 'sigef_onr'
                    )
                """), {
                    "pid": pid, "sid": survey_id, "rid": region_id,
                    "geom": geom.wkt,
                    "clat": centroid.y, "clng": centroid.x,
                    "area": area,
                    "cnm": cnm or None,
                    "matricula": matricula or None,
                    "nome": nome or None,
                    "municipio": municipio or None,
                })
                inserted += 1

    return inserted, updated


def run(input: SigefInput) -> SigefOutput:
    try:
        token = get_token()
    except Exception as e:
        return SigefOutput(ok=False, error=f"No se pudo obtener token ONR: {e}")

    bbox_wm = _bbox_webmerc(input.bbox_south, input.bbox_west,
                             input.bbox_north, input.bbox_east)

    logger.info(f"SIGEF ONR: consultando bbox ({input.bbox_south},{input.bbox_west}) → ({input.bbox_north},{input.bbox_east})")

    all_features = []
    offset = 0
    while True:
        try:
            features = _query_features(token, bbox_wm, offset)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 498:
                logger.warning("ONR: token expirado, renovando...")
                token = get_token(force_refresh=True)
                features = _query_features(token, bbox_wm, offset)
            else:
                return SigefOutput(ok=False, error=str(e))

        all_features.extend(features)
        logger.info(f"SIGEF: {len(features)} features en offset={offset}, total={len(all_features)}")

        if len(features) < MAX_RECORDS:
            break
        offset += MAX_RECORDS
        time.sleep(input.delay_ms / 1000)

    if not all_features:
        return SigefOutput(ok=True, total_features=0,
                           error="No hay predios SIGEF en el área indicada")

    inserted, updated = _upsert(all_features, input.region_id, input.survey_id)
    logger.info(f"SIGEF ONR: {inserted} inseridos, {updated} actualizados")

    return SigefOutput(
        ok=True,
        parcelas_inseridas=inserted,
        parcelas_atualizadas=updated,
        total_features=len(all_features),
    )
