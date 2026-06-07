"""GeoJSONZoneFetcher — define una zona de relevamiento a partir de un GeoJSON.

Dado un GeoJSON con uno o más polígonos:
1. Calcula el bounding box
2. Crea la región y el survey en la DB
3. Descarga footprints OSM dentro del bbox
4. Reporta edificios insertados y próximos pasos
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Optional

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.db.engine import get_engine
from scrapitero.agents._run import agent_run
from scrapitero.agents import geo
from scrapitero.agents.osm_building_fetcher import OSMInput, run as osm_run


class GeoJSONZoneInput(BaseModel):
    region_nombre: str
    geojson_str: str           # GeoJSON FeatureCollection, Feature o Polygon
    region_id: Optional[str] = None
    country_code: Optional[str] = None   # autodetectado del centroide si no se pasa


class GeoJSONZoneOutput(BaseModel):
    ok: bool
    region_id: str = ""
    survey_id: str = ""
    bbox: dict = {}
    edificios_insertados: int = 0
    proximos_pasos: list[str] = []
    error: Optional[str] = None


def _slugify(s: str) -> str:
    for src, dst in [("áàãâä","a"),("éèêë","e"),("íìîï","i"),("óòõôö","o"),("úùûü","u"),("ç","c"),("ñ","n")]:
        for c in src:
            s = s.lower().replace(c, dst)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _bbox_from_geojson(data: dict) -> dict:
    coords: list[tuple[float, float]] = []

    def extract(obj: object) -> None:
        if isinstance(obj, dict):
            t = obj.get("type")
            if t == "FeatureCollection":
                for f in obj.get("features", []):
                    extract(f)
            elif t == "Feature":
                extract(obj.get("geometry") or {})
            elif t in ("Polygon", "MultiPolygon", "LineString", "MultiLineString", "Point"):
                extract(obj.get("coordinates", []))
        elif isinstance(obj, list):
            for item in obj:
                if (isinstance(item, list) and len(item) >= 2
                        and isinstance(item[0], (int, float))):
                    coords.append((item[0], item[1]))
                else:
                    extract(item)

    extract(data)
    if not coords:
        raise ValueError("Sin coordenadas en el GeoJSON")
    lngs = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return {"south": min(lats), "north": max(lats), "west": min(lngs), "east": max(lngs)}


def _bbox_to_wkt(b: dict) -> str:
    s, n, w, e = b["south"], b["north"], b["west"], b["east"]
    return f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"


@agent_run
def run(input: GeoJSONZoneInput) -> GeoJSONZoneOutput:
    try:
        geojson_data = json.loads(input.geojson_str)
        bbox = _bbox_from_geojson(geojson_data)
    except (json.JSONDecodeError, ValueError) as e:
        return GeoJSONZoneOutput(ok=False, error=str(e))

    logger.info(f"GeoJSON zona: bbox={bbox}")

    # País: autodetectado del centroide de la zona (cualquier país) salvo override explícito.
    country = input.country_code or geo.detect_country(
        (bbox["south"] + bbox["north"]) / 2.0,
        (bbox["west"] + bbox["east"]) / 2.0,
    )
    if not country:
        return GeoJSONZoneOutput(ok=False, error=(
            "No se pudo autodetectar el país del GeoJSON (reverse-geocoding falló). "
            "Reintentá en unos segundos o pasá country_code explícito."
        ))
    logger.info(f"País de la zona: {country}"
                f"{' (autodetectado)' if not input.country_code else ' (explícito)'}")

    region_id = input.region_id or f"zona-{_slugify(input.region_nombre)}"
    bbox_wkt = _bbox_to_wkt(bbox)
    engine = get_engine()

    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO regions (region_id, name, country_code, zone_geojson, bbox_wkt)
                VALUES (:rid, :name, :cc, :geojson, :bbox)
                ON CONFLICT (region_id) DO UPDATE
                    SET name = EXCLUDED.name,
                        zone_geojson = EXCLUDED.zone_geojson,
                        bbox_wkt = EXCLUDED.bbox_wkt
            """), {"rid": region_id, "name": input.region_nombre,
                   "cc": country, "geojson": input.geojson_str,
                   "bbox": bbox_wkt})

            survey_id = str(uuid.uuid4())
            conn.execute(text("""
                INSERT INTO surveys (survey_id, region_id, status)
                VALUES (:sid, :rid, 'running')
            """), {"sid": survey_id, "rid": region_id})
            logger.info(f"Survey creado: {survey_id} para región {region_id}")
    except Exception as e:
        return GeoJSONZoneOutput(ok=False, region_id=region_id, error=f"Error DB: {e}")

    logger.info("Descargando footprints OSM...")
    osm_result = osm_run(OSMInput(
        region_id=region_id,
        survey_id=survey_id,
        bbox_south=bbox["south"],
        bbox_west=bbox["west"],
        bbox_north=bbox["north"],
        bbox_east=bbox["east"],
    ))

    with engine.begin() as conn:
        status = "partial" if osm_result.ok else "failed"
        conn.execute(text(
            "UPDATE surveys SET status=:s, finished_at=NOW() WHERE survey_id=:sid"
        ), {"s": status, "sid": survey_id})

    if not osm_result.ok:
        return GeoJSONZoneOutput(
            ok=False, region_id=region_id, survey_id=survey_id,
            bbox=bbox, error=f"OSM falló: {osm_result.error}",
        )

    proximos = [
        f"address-resolver con region_id={region_id!r} survey_id={survey_id!r}",
        f"coverage-reporter con region_id={region_id!r} survey_id={survey_id!r}",
    ]
    return GeoJSONZoneOutput(
        ok=True,
        region_id=region_id,
        survey_id=survey_id,
        bbox=bbox,
        edificios_insertados=osm_result.edificios_insertados,
        proximos_pasos=proximos,
    )
