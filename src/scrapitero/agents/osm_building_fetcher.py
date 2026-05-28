"""OSMBuildingFetcher — descarga footprints de edificios desde OpenStreetMap via Overpass API.

Genérico: funciona para cualquier región/país.
Filtra por bounding box (bbox) derivado de las parcelas ya cargadas en la DB,
o acepta un bbox explícito.

Output: filas insertadas en tabla `edificios`.
"""

from __future__ import annotations

import uuid
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from shapely.geometry import Polygon, mapping
from shapely.ops import transform as shp_transform
import pyproj
from sqlalchemy import text

from scrapitero.db.engine import get_engine


# ── Config ────────────────────────────────────────────────────────────────────

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT = 120  # segundos


# ── Pydantic I/O ──────────────────────────────────────────────────────────────

class OSMInput(BaseModel):
    region_id: str                          # "ituzaingo-ba-ar" o "vg-mt-br"
    survey_id: str                          # UUID del survey activo
    # BBox explícita (opcional — si no se pasa, se deriva de las parcelas en DB)
    bbox_south: Optional[float] = None      # lat mínima
    bbox_west:  Optional[float] = None      # lng mínima
    bbox_north: Optional[float] = None      # lat máxima
    bbox_east:  Optional[float] = None      # lng máxima
    # Margen adicional en grados alrededor del bbox de parcelas
    bbox_buffer_deg: float = 0.001


class OSMOutput(BaseModel):
    ok: bool
    edificios_insertados: int
    edificios_actualizados: int
    bbox_usado: Optional[str] = None        # "south,west,north,east"
    fuentes: list[str]
    error: Optional[str] = None


# ── Obtener bbox desde DB ─────────────────────────────────────────────────────

def _bbox_from_db(engine, region_id: str, buffer: float) -> Optional[tuple]:
    """Calcula bbox a partir de los centroides de parcelas del region."""
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT
                MIN(centroid_lat) - :buf,
                MIN(centroid_lng) - :buf,
                MAX(centroid_lat) + :buf,
                MAX(centroid_lng) + :buf
            FROM parcelas
            WHERE region_id = :region
              AND centroid_lat IS NOT NULL
        """), {"region": region_id, "buf": buffer}).fetchone()

    if row and row[0] is not None:
        south, west, north, east = row
        logger.info(f"BBox derivada de parcelas: {south:.5f},{west:.5f},{north:.5f},{east:.5f}")
        return float(south), float(west), float(north), float(east)
    return None


# ── Overpass query ────────────────────────────────────────────────────────────

def _overpass_query(south: float, west: float, north: float, east: float) -> str:
    """Genera query Overpass QL para edificios en el bbox."""
    bbox = f"{south},{west},{north},{east}"
    return f"""
[out:json][timeout:{OVERPASS_TIMEOUT}];
(
  way["building"]({bbox});
  relation["building"]({bbox});
);
(._;>;);
out geom;
"""


def _fetch_overpass(query: str) -> dict:
    logger.info("Consultando Overpass API...")
    with httpx.Client(timeout=OVERPASS_TIMEOUT + 10) as client:
        r = client.post(OVERPASS_URL, data={"data": query})
        r.raise_for_status()
    return r.json()


# ── Parseo de geometría OSM ───────────────────────────────────────────────────

def _nodes_to_dict(elements: list[dict]) -> dict[int, tuple[float, float]]:
    """Construye dict node_id → (lng, lat) desde los elementos Overpass."""
    return {
        e["id"]: (e["lon"], e["lat"])
        for e in elements
        if e["type"] == "node" and "lon" in e and "lat" in e
    }


def _way_to_polygon(way: dict, nodes: dict) -> Optional[Polygon]:
    """Convierte un way de Overpass en Polygon Shapely."""
    refs = way.get("nodes", [])
    coords = [nodes[n] for n in refs if n in nodes]
    if len(coords) < 4:
        return None
    try:
        poly = Polygon(coords)
        return poly if poly.is_valid else poly.buffer(0)
    except Exception:
        return None


def _area_m2(geom: Polygon, epsg_utm: int = 32721) -> Optional[float]:
    """Área en m² proyectada a UTM (zona 21S para Argentina/Brasil sur)."""
    try:
        proj = pyproj.Transformer.from_crs(
            "EPSG:4326", f"EPSG:{epsg_utm}", always_xy=True
        ).transform
        return round(shp_transform(proj, geom).area, 2)
    except Exception:
        return None


# ── Inserción en DB ───────────────────────────────────────────────────────────

def _upsert_edificios(ways: list[dict], nodes: dict,
                      region_id: str, survey_id: str) -> tuple[int, int]:
    engine = get_engine()
    insertados = 0
    actualizados = 0

    with engine.begin() as conn:
        for way in ways:
            if way.get("type") != "way":
                continue

            osm_id = str(way["id"])
            poly = _way_to_polygon(way, nodes)
            if not poly:
                continue

            centroid = poly.centroid
            area = _area_m2(poly)
            geom_wkt = poly.wkt
            centroid_wkt = centroid.wkt

            existing = conn.execute(
                text("SELECT edificio_id FROM edificios WHERE external_id = :eid AND source = 'osm'"),
                {"eid": osm_id}
            ).fetchone()

            if existing:
                conn.execute(text("""
                    UPDATE edificios SET
                        footprint = ST_GeomFromText(:geom, 4326),
                        centroid  = ST_GeomFromText(:ctr, 4326),
                        area_m2   = :area
                    WHERE edificio_id = :eid
                """), {"geom": geom_wkt, "ctr": centroid_wkt, "area": area, "eid": str(existing[0])})
                actualizados += 1
            else:
                new_id = uuid.uuid4()
                conn.execute(text("""
                    INSERT INTO edificios
                        (edificio_id, survey_id, footprint, centroid, area_m2, source, external_id)
                    VALUES
                        (:eid, :sid,
                         ST_GeomFromText(:geom, 4326),
                         ST_GeomFromText(:ctr, 4326),
                         :area, 'osm', :osm_id)
                """), {
                    "eid": str(new_id),
                    "sid": survey_id,
                    "geom": geom_wkt,
                    "ctr": centroid_wkt,
                    "area": area,
                    "osm_id": osm_id,
                })
                insertados += 1

    logger.info(f"Edificios DB: {insertados} insertados, {actualizados} actualizados")
    return insertados, actualizados


# ── Entry point ───────────────────────────────────────────────────────────────

def run(input: OSMInput) -> OSMOutput:
    fuentes = []
    engine = get_engine()

    try:
        # Determinar bbox
        if all(v is not None for v in [input.bbox_south, input.bbox_west,
                                        input.bbox_north, input.bbox_east]):
            bbox = (input.bbox_south, input.bbox_west, input.bbox_north, input.bbox_east)
            logger.info("Usando bbox explícita del input")
        else:
            bbox = _bbox_from_db(engine, input.region_id, input.bbox_buffer_deg)
            if not bbox:
                return OSMOutput(
                    ok=False, edificios_insertados=0, edificios_actualizados=0,
                    fuentes=fuentes,
                    error=(
                        "No se encontraron parcelas con coordenadas para derivar el bbox. "
                        "Cargá parcelas primero con arba_cadastral_fetcher (o pasá bbox explícita)."
                    )
                )

        south, west, north, east = bbox
        bbox_str = f"{south},{west},{north},{east}"

        # Overpass query
        query = _overpass_query(south, west, north, east)
        data = _fetch_overpass(query)
        fuentes.append("osm_overpass")

        elements = data.get("elements", [])
        nodes = _nodes_to_dict(elements)
        ways = [e for e in elements if e.get("type") == "way"]
        logger.info(f"OSM: {len(ways)} edificios encontrados en el bbox")

        if not ways:
            return OSMOutput(
                ok=True, edificios_insertados=0, edificios_actualizados=0,
                bbox_usado=bbox_str, fuentes=fuentes
            )

        insertados, actualizados = _upsert_edificios(ways, nodes, input.region_id, input.survey_id)

        return OSMOutput(
            ok=True,
            edificios_insertados=insertados,
            edificios_actualizados=actualizados,
            bbox_usado=bbox_str,
            fuentes=fuentes,
        )

    except Exception as e:
        logger.exception("OSMBuildingFetcher falló")
        return OSMOutput(
            ok=False, edificios_insertados=0, edificios_actualizados=0,
            fuentes=fuentes, error=str(e)
        )
