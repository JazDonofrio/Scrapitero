"""OSMBuildingFetcher — descarga footprints de edificios desde OpenStreetMap via Overpass API.

Genérico: funciona para cualquier región/país del mundo (OSM tiene cobertura global).
Filtra por bounding box (bbox) que puede pasarse explícito en el input
(`bbox_south/west/north/east`) o derivarse de las parcelas ya cargadas en la DB.
El área de cada edificio se proyecta al huso UTM correcto según su posición, así
que el `area_m2` es válido en cualquier parte del planeta, no solo en Sudamérica.

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
from scrapitero.agents._run import agent_run


# ── Config ────────────────────────────────────────────────────────────────────

OVERPASS_MIRRORS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
TOR_SOCKS5 = "socks5://127.0.0.1:9050"
# Mirrors que funcionan via Tor cuando la IP directa está bloqueada
OVERPASS_MIRRORS_TOR = [
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]
OVERPASS_TIMEOUT = 120  # segundos

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ScrapiteroResearch/1.0; +https://github.com/scrapitero)",
    "Accept": "*/*",
}


def _tor_available() -> bool:
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", 9050), timeout=2)
        s.close()
        return True
    except OSError:
        return False


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
    edificios_vinculados: int = 0           # asociados a una parcela por join espacial
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


def _try_mirrors(query: str, mirrors: list, proxy: str = None) -> dict:
    last_error = ""
    kwargs = {"proxies": proxy} if proxy else {}
    for mirror in mirrors:
        try:
            with httpx.Client(follow_redirects=True, timeout=OVERPASS_TIMEOUT + 10, **kwargs) as client:
                r = client.post(mirror, data={"data": query}, headers=_HEADERS)
            if r.status_code == 200:
                via = f" via Tor" if proxy else ""
                logger.info(f"Overpass OK{via} — {mirror}")
                return r.json()
            last_error = f"HTTP {r.status_code} ({mirror})"
            logger.warning(f"Overpass {last_error}")
        except httpx.HTTPError as exc:
            last_error = f"HTTPError ({mirror}): {exc}"
            logger.warning(f"Overpass {last_error}")
    return None, last_error


def _fetch_overpass(query: str) -> dict:
    logger.info("Consultando Overpass API...")

    # Intento 1: mirrors directos
    result = _try_mirrors(query, OVERPASS_MIRRORS)
    if result and not isinstance(result, tuple):
        return result

    # Intento 2: via Tor (si está disponible)
    if _tor_available():
        logger.info("Mirrors directos fallaron — reintentando via Tor...")
        result = _try_mirrors(query, OVERPASS_MIRRORS_TOR, proxy=TOR_SOCKS5)
        if result and not isinstance(result, tuple):
            return result
        _, last_error = result
    else:
        _, last_error = result

    raise RuntimeError(f"Todos los mirrors fallaron (con y sin Tor). Último: {last_error}")


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


_utm_transformers: dict[int, "pyproj.Transformer"] = {}


def _utm_epsg(lon: float, lat: float) -> int:
    """EPSG del huso UTM que contiene (lon, lat). Global: cualquier país.

    Norte → 326xx, Sur → 327xx, donde xx es el huso 1..60.
    """
    zone = int((lon + 180.0) // 6.0) + 1
    zone = min(max(zone, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


def _area_m2(geom: Polygon) -> Optional[float]:
    """Área en m² proyectada al huso UTM correcto según el centroide del edificio.

    El huso se deriva de la posición real del polígono → válido en todo el mundo,
    no solo en la zona 21S (Argentina/Brasil sur)."""
    try:
        c = geom.centroid
        epsg = _utm_epsg(c.x, c.y)
        proj = _utm_transformers.get(epsg)
        if proj is None:
            proj = pyproj.Transformer.from_crs(
                "EPSG:4326", f"EPSG:{epsg}", always_xy=True
            ).transform
            _utm_transformers[epsg] = proj
        return round(shp_transform(proj, geom).area, 2)
    except Exception:
        return None


# ── Parseo de tags OSM (insumo para estimar unidades) ──────────────────────────

def _parse_int(val) -> Optional[int]:
    """Convierte un tag OSM numérico a int (tolera '3', '3;4', '3.0')."""
    if val is None:
        return None
    try:
        return int(float(str(val).split(";")[0].strip()))
    except (ValueError, TypeError):
        return None


def _parse_tags(tags: dict) -> tuple[Optional[str], Optional[int], Optional[int]]:
    """Extrae (tipo_osm, pisos, unidades) de los tags de un edificio OSM.

    - tipo_osm:  valor de building=* (apartments/house/commercial/retail/…). 'yes'→None.
    - pisos:     building:levels (entero).
    - unidades:  building:flats o addr:units — conteo real de UF cuando OSM lo trae.
    """
    tipo = tags.get("building")
    if tipo in (None, "yes", "true"):
        tipo = None
    pisos = _parse_int(tags.get("building:levels"))
    unidades = _parse_int(tags.get("building:flats")) or _parse_int(tags.get("addr:units"))
    return tipo, pisos, unidades


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
            tipo_osm, pisos, unidades = _parse_tags(way.get("tags", {}))

            existing = conn.execute(
                text("SELECT edificio_id FROM edificios WHERE external_id = :eid AND source = 'osm'"),
                {"eid": osm_id}
            ).fetchone()

            if existing:
                conn.execute(text("""
                    UPDATE edificios SET
                        footprint = ST_GeomFromText(:geom, 4326),
                        centroid  = ST_GeomFromText(:ctr, 4326),
                        area_m2   = :area,
                        tipo_osm  = :tipo,
                        pisos_estimados = :pisos,
                        unidades_osm = :unidades
                    WHERE edificio_id = :eid
                """), {"geom": geom_wkt, "ctr": centroid_wkt, "area": area,
                       "tipo": tipo_osm, "pisos": pisos, "unidades": unidades,
                       "eid": str(existing[0])})
                actualizados += 1
            else:
                new_id = uuid.uuid4()
                conn.execute(text("""
                    INSERT INTO edificios
                        (edificio_id, survey_id, footprint, centroid, area_m2,
                         tipo_osm, pisos_estimados, unidades_osm, source, external_id)
                    VALUES
                        (:eid, :sid,
                         ST_GeomFromText(:geom, 4326),
                         ST_GeomFromText(:ctr, 4326),
                         :area, :tipo, :pisos, :unidades, 'osm', :osm_id)
                """), {
                    "eid": str(new_id),
                    "sid": survey_id,
                    "geom": geom_wkt,
                    "ctr": centroid_wkt,
                    "area": area,
                    "tipo": tipo_osm,
                    "pisos": pisos,
                    "unidades": unidades,
                    "osm_id": osm_id,
                })
                insertados += 1

    logger.info(f"Edificios DB: {insertados} insertados, {actualizados} actualizados")
    return insertados, actualizados


def _link_to_parcelas(region_id: str) -> int:
    """Asocia cada edificio sin parcela a la parcela que contiene su centroide.

    Join espacial PostGIS (ST_Contains). Imprescindible para que UnidadesEstimator
    pueda agregar los edificios por parcela. Devuelve la cantidad vinculada.
    """
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE edificios e SET parcela_id = p.parcela_id
            FROM parcelas p
            WHERE p.region_id = :rid
              AND p.geometry IS NOT NULL
              AND e.centroid IS NOT NULL
              AND ST_Contains(p.geometry, e.centroid)
              AND (e.parcela_id IS NULL OR e.parcela_id <> p.parcela_id)
        """), {"rid": region_id})
        vinculados = result.rowcount or 0
    logger.info(f"Edificios vinculados a parcela (ST_Contains): {vinculados}")
    return vinculados


# ── Entry point ───────────────────────────────────────────────────────────────

@agent_run
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
        vinculados = _link_to_parcelas(input.region_id)

        return OSMOutput(
            ok=True,
            edificios_insertados=insertados,
            edificios_actualizados=actualizados,
            edificios_vinculados=vinculados,
            bbox_usado=bbox_str,
            fuentes=fuentes,
        )

    except Exception as e:
        logger.exception("OSMBuildingFetcher falló")
        return OSMOutput(
            ok=False, edificios_insertados=0, edificios_actualizados=0,
            fuentes=fuentes, error=str(e)
        )
