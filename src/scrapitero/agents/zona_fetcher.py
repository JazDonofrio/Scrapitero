"""ZonaFetcher — define una zona de relevamiento por coordenada + radio (Brasil).

Dado un punto central y un radio en metros:
1. Calcula el bounding box
2. Crea la región y el survey en la DB si no existen
3. Descarga footprints OSM dentro del bbox
4. Reporta qué agentes correr a continuación

No requiere conocer de antemano el municipio ni la nomenclatura catastral.
"""

from __future__ import annotations

import math
import uuid
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.db.engine import get_engine
from scrapitero.agents._run import agent_run
from scrapitero.agents.osm_building_fetcher import OSMInput, run as osm_run


class ZonaInput(BaseModel):
    lat: float                          # latitud centro (ej: -15.6468)
    lng: float                          # longitud centro (ej: -56.1195)
    radio_m: float                      # radio en metros (ej: 500)
    region_id: Optional[str] = None    # si no se pasa, se genera automáticamente
    region_nombre: Optional[str] = None  # nombre legible de la zona


class ZonaOutput(BaseModel):
    ok: bool
    region_id: str = ""
    survey_id: str = ""
    bbox: dict = {}                     # {south, west, north, east}
    edificios_insertados: int = 0
    proximos_pasos: list[str] = []
    error: Optional[str] = None


def _bbox_from_center(lat: float, lng: float, radio_m: float) -> dict:
    """Calcula bbox rectangular a partir de un punto central y radio en metros."""
    delta_lat = radio_m / 111_000
    delta_lng = radio_m / (111_000 * math.cos(math.radians(lat)))
    return {
        "south": round(lat - delta_lat, 7),
        "north": round(lat + delta_lat, 7),
        "west":  round(lng - delta_lng, 7),
        "east":  round(lng + delta_lng, 7),
    }


def _reverse_geocode(lat: float, lng: float) -> str:
    """Obtiene nombre de ciudad via Nominatim para nombrar la región."""
    try:
        r = httpx.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lng, "format": "json"},
            headers={"User-Agent": "Scrapitero/1.0"},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        addr = data.get("address", {})
        city = (addr.get("city") or addr.get("town") or
                addr.get("municipality") or addr.get("county") or "zona")
        state = addr.get("state", "")
        return f"{city}, {state}".strip(", ") if state else city
    except Exception:
        return f"zona-{lat:.4f}-{lng:.4f}"


def _slugify(text: str) -> str:
    import re
    s = text.lower()
    s = re.sub(r"[áàãâä]", "a", s)
    s = re.sub(r"[éèêë]", "e", s)
    s = re.sub(r"[íìîï]", "i", s)
    s = re.sub(r"[óòõôö]", "o", s)
    s = re.sub(r"[úùûü]", "u", s)
    s = re.sub(r"[ç]", "c", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _ensure_region(conn, region_id: str, nombre: str) -> None:
    exists = conn.execute(
        text("SELECT 1 FROM regions WHERE region_id = :rid"),
        {"rid": region_id}
    ).fetchone()
    if not exists:
        conn.execute(text("""
            INSERT INTO regions (region_id, name, country_code)
            VALUES (:rid, :name, 'BRA')
            ON CONFLICT (region_id) DO NOTHING
        """), {"rid": region_id, "name": nombre})
        logger.info(f"Región creada: {region_id} ({nombre})")


@agent_run
def run(input: ZonaInput) -> ZonaOutput:
    bbox = _bbox_from_center(input.lat, input.lng, input.radio_m)
    logger.info(f"Zona: centro=({input.lat},{input.lng}) radio={input.radio_m}m → bbox={bbox}")

    # Nombre y region_id
    nombre = input.region_nombre or _reverse_geocode(input.lat, input.lng)
    region_id = input.region_id or f"zona-{_slugify(nombre)}-br"
    logger.info(f"Región: {region_id} ({nombre})")

    engine = get_engine()

    # Crear región y reusar o crear survey
    try:
        with engine.begin() as conn:
            _ensure_region(conn, region_id, nombre)
            # Reusar el survey más reciente si ya existe para esta región
            existing = conn.execute(text("""
                SELECT survey_id::text FROM surveys
                WHERE region_id = :rid
                ORDER BY started_at DESC LIMIT 1
            """), {"rid": region_id}).fetchone()
            if existing:
                survey_id = existing[0]
                logger.info(f"Reusando survey existente: {survey_id}")
            else:
                survey_id = str(uuid.uuid4())
                conn.execute(text("""
                    INSERT INTO surveys (survey_id, region_id)
                    VALUES (:sid, :rid)
                """), {"sid": survey_id, "rid": region_id})
                logger.info(f"Survey creado: {survey_id}")
    except Exception as e:
        return ZonaOutput(ok=False, region_id=region_id, error=f"Error creando survey: {e}")

    # Descargar footprints OSM con el bbox calculado
    logger.info("Descargando footprints OSM...")
    osm_result = osm_run(OSMInput(
        region_id=region_id,
        survey_id=survey_id,
        bbox_south=bbox["south"],
        bbox_west=bbox["west"],
        bbox_north=bbox["north"],
        bbox_east=bbox["east"],
    ))

    if not osm_result.ok:
        return ZonaOutput(
            ok=False, region_id=region_id, survey_id=survey_id,
            bbox=bbox, error=f"OSM falló: {osm_result.error}"
        )

    # Contar total de edificios en DB para este survey (incluye corridas previas)
    with engine.connect() as conn:
        total_edificios = conn.execute(text(
            "SELECT COUNT(*) FROM edificios WHERE survey_id = :sid"
        ), {"sid": survey_id}).scalar() or 0

    logger.info(f"OSM: {total_edificios} edificios en la zona (nuevos: {osm_result.edificios_insertados})")

    proximos = []
    if total_edificios > 0:
        proximos.append(
            f"address-resolver con region_id={region_id!r} survey_id={survey_id!r}"
        )
    proximos.append(
        f"coverage-reporter con region_id={region_id!r} survey_id={survey_id!r}"
    )

    return ZonaOutput(
        ok=True,
        region_id=region_id,
        survey_id=survey_id,
        bbox=bbox,
        edificios_insertados=total_edificios,
        proximos_pasos=proximos,
    )
