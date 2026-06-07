"""SaltaCatastroFetcher — parcelas catastrales de la provincia de Salta, Argentina.

Fuentes WFS públicas (sin autenticación):

  Capital (ciudad de Salta) — IDEMSA:
    URL:   https://geocloud.municipalidadsalta.gob.ar/geoserver/wfs
    Layer: public:catastros_Ene2025
    Datos: ~125.920 parcelas · actualizado Ene 2025 · EPSG:4326

  Interior provincial — IDESA:
    URL:   http://geoportal.idesa.gob.ar/geoserver/wfs
    Layer: geonode:fc_parcelas_v20

Selección automática ("auto"):
  Si el centroide de la zona cae dentro del bbox de la ciudad capital → IDEMSA;
  en caso contrario → IDESA provincial.

Atributos mapeados a la tabla parcelas:
  nomenclatura_catastral  ← VINCULACIO  (ej: "01110P399A0020")
  cca_code                ← CATASTRO
  area_m2_terreno         ← calculada en UTM 20S (EPSG:32720)
  localidad               ← LOCALID
  municipio               ← "Capital" u otro según DEPARTA
  estado_provincia        ← "Salta"
  pais                    ← "Argentina"
  fuente_parcela          ← "salta_idemsa" | "salta_idesa"
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Generator, Optional

import httpx
import pyproj
from loguru import logger
from pydantic import BaseModel
from shapely.geometry import shape
from shapely.ops import transform as shp_transform, unary_union
from sqlalchemy import text

from scrapitero.db.engine import get_engine
from scrapitero.agents._run import agent_run
from scrapitero.agents.smartgis_fetcher import _geom_to_polygon


# ── Configuración de fuentes WFS ──────────────────────────────────────────────

@dataclass
class WFSSource:
    name: str
    fuente_parcela: str
    url: str
    layer: str
    # WFS BBOX axis order: True = lat/lon (south,west,north,east) | False = lon/lat
    bbox_latlon: bool
    # Listas de candidatos para cada campo (se prueba en orden)
    candidates_nomenclatura: list[str]
    candidates_catastro: list[str]
    candidates_localidad: list[str]
    candidates_depto: list[str]
    candidates_numero: list[str]   # número de puerta (si disponible en catastro)


SOURCES: dict[str, WFSSource] = {
    "capital": WFSSource(
        name="IDEMSA Capital",
        fuente_parcela="salta_idemsa",
        url="https://geocloud.municipalidadsalta.gob.ar/geoserver/wfs",
        layer="public:catastros_Ene2025",
        # GeoServer EPSG:4326 sin CRS explícito → acepta south,west,north,east (lat/lon)
        bbox_latlon=True,
        candidates_nomenclatura=["VINCULACIO", "NOMENCLATURA", "NOM_CAT", "vinculacio"],
        candidates_catastro=["CATASTRO", "catastro", "COD_CAT", "CODIGO"],
        candidates_localidad=["LOCALID", "LOCALIDAD", "localidad", "NOM_LOCAL"],
        candidates_depto=["DEPARTA", "DEPARTAMENTO", "departamento", "COD_DEPTO"],
        candidates_numero=["NROPUERTA", "NRO_PUERTA", "nropuerta", "NUM_PUERTA"],
    ),
    "provincia": WFSSource(
        name="IDESA Provincial",
        fuente_parcela="salta_idesa",
        url="http://geoportal.idesa.gob.ar/geoserver/wfs",
        layer="geonode:fc_parcelas_v20",
        # IDESA: asumir lat/lon hasta confirmar con test real
        bbox_latlon=True,
        candidates_nomenclatura=["VINCULACIO", "NOMENCLATURA", "NOM_CAT", "vinculacio"],
        candidates_catastro=["CATASTRO", "catastro", "COD_CAT", "CODIGO"],
        candidates_localidad=["LOCALID", "LOCALIDAD", "localidad", "NOM_LOCAL"],
        candidates_depto=["DEPARTA", "DEPARTAMENTO", "departamento", "COD_DEPTO"],
        candidates_numero=["NROPUERTA", "NRO_PUERTA", "nropuerta", "NUM_PUERTA"],
    ),
}

# Bbox aproximado de la ciudad de Salta Capital (lat/lng WGS84)
# (south, west, north, east)
_CAPITAL_BBOX = (-24.95, -65.55, -24.70, -65.30)

# Proyector UTM Zona 20S — sistema métrico para Salta (EPSG:32720)
_PROJ_UTM20S = pyproj.Transformer.from_crs(
    "EPSG:4326", "EPSG:32720", always_xy=True
).transform

# Departamentos de Salta → nombre legible
_DEPARTA_NOMBRES: dict[str, str] = {
    "01": "Capital", "02": "Rosario de Lerma", "03": "La Caldera",
    "04": "Chicoana", "05": "La Viña", "06": "Guachipas", "07": "Cerrillos",
    "08": "Rosario de Cachi", "09": "Molinos", "10": "San Carlos",
    "11": "Cafayate", "12": "Anta", "13": "Orán", "14": "Rivadavia",
    "15": "San Martín", "16": "Iruya", "17": "Santa Victoria",
    "18": "Metán", "19": "Rosario de la Frontera", "20": "Candelaria",
    "21": "Güemes", "22": "General Mosconi", "23": "Rivadavia",
}


# ── Pydantic I/O ──────────────────────────────────────────────────────────────

class SaltaCatastroInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None
    fuente: str = "auto"           # "auto" | "capital" | "provincia"
    batch_size: int = 500          # parcelas por página WFS
    delay_ms: int = 300            # pausa entre páginas WFS
    # Override de bbox — si se omite, se deriva de zone_geojson/bbox_wkt de la región.
    # Útil cuando zone_geojson tiene múltiples features en lugares distintos.
    bbox_south: Optional[float] = None
    bbox_west: Optional[float] = None
    bbox_north: Optional[float] = None
    bbox_east: Optional[float] = None


class SaltaCatastroOutput(BaseModel):
    ok: bool
    parcelas_insertadas: int = 0
    parcelas_actualizadas: int = 0
    fuera_zona: int = 0
    fuente_usada: str = ""
    bbox_usado: Optional[str] = None
    error: Optional[str] = None


# ── DB helpers ────────────────────────────────────────────────────────────────

def _load_zone(region_id: str) -> tuple[Optional[tuple], Optional[object]]:
    """Devuelve ((south, west, north, east), zone_polygon_or_None)."""
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT bbox_wkt, zone_geojson FROM regions WHERE region_id = :rid"
        ), {"rid": region_id}).fetchone()

    if not row:
        logger.error(f"SaltaCatastro: región '{region_id}' no existe en DB")
        return None, None

    bbox_wkt, zone_geojson_str = row
    zone_polygon = None

    if zone_geojson_str:
        try:
            gj = json.loads(zone_geojson_str)
            if gj.get("type") == "FeatureCollection":
                raw = [shape(f["geometry"]) for f in gj.get("features", []) if f.get("geometry")]
            elif gj.get("type") == "Feature":
                raw = [shape(gj["geometry"])]
            else:
                raw = [shape(gj)]
            polys = [_geom_to_polygon(g) for g in raw if g is not None]
            zone_polygon = unary_union(polys) if polys else None
        except Exception as e:
            logger.warning(f"SaltaCatastro: no se pudo parsear zone_geojson: {e}")

    if zone_polygon is not None:
        b = zone_polygon.bounds  # (minx=west, miny=south, maxx=east, maxy=north)
        return (b[1], b[0], b[3], b[2]), zone_polygon

    if bbox_wkt:
        try:
            from shapely import wkt as shp_wkt
            b = shp_wkt.loads(bbox_wkt).bounds
            return (b[1], b[0], b[3], b[2]), None
        except Exception as e:
            logger.warning(f"SaltaCatastro: no se pudo parsear bbox_wkt: {e}")

    return None, None


def _get_latest_survey(region_id: str) -> Optional[str]:
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT survey_id::text FROM surveys
            WHERE region_id = :rid ORDER BY started_at DESC LIMIT 1
        """), {"rid": region_id}).fetchone()
    return row[0] if row else None


def _find_existing(conn, region_id: str, nomenclatura: str) -> Optional[str]:
    row = conn.execute(text("""
        SELECT parcela_id::text FROM parcelas
        WHERE region_id = :rid AND nomenclatura_catastral = :nom
    """), {"rid": region_id, "nom": nomenclatura}).fetchone()
    return row[0] if row else None


# ── Geometría y área ──────────────────────────────────────────────────────────

def _to_polygon(geom):
    """Normaliza cualquier geometría a Polygon. MultiPolygon → polígono más grande."""
    if not geom.is_valid:
        geom = geom.buffer(0)
    if geom.geom_type == "MultiPolygon":
        geom = max(geom.geoms, key=lambda g: g.area)
    return geom


def _area_m2(geom) -> Optional[float]:
    try:
        return round(shp_transform(_PROJ_UTM20S, geom).area, 2)
    except Exception:
        return None


def _centroid(geom) -> tuple[float, float]:
    c = geom.centroid
    return round(c.y, 7), round(c.x, 7)  # lat, lng


# ── Resolución de campos WFS ──────────────────────────────────────────────────

def _resolve(props: dict, candidates: list[str]) -> Optional[str]:
    for name in candidates:
        val = props.get(name)
        if val is not None:
            s = str(val).strip()
            if s:
                return s
    return None


def _resolve_numero(props: dict, candidates: list[str]) -> Optional[str]:
    """Igual que _resolve pero descarta '0' y '00' que el catastro usa como null."""
    val = _resolve(props, candidates)
    if val and val.lstrip("0"):
        return val
    return None


def _municipio_from_depto(depto_code: Optional[str]) -> str:
    if not depto_code:
        return "Salta"
    return _DEPARTA_NOMBRES.get(depto_code.zfill(2), f"Departamento {depto_code}")


# ── WFS fetch con paginación ──────────────────────────────────────────────────

def _wfs_features(
    source: WFSSource,
    west: float, south: float,
    east: float, north: float,
    batch_size: int,
    delay_ms: int,
) -> Generator[dict, None, None]:
    """Descarga features del WFS con paginación startIndex."""
    start = 0
    total = 0

    # GeoServer con EPSG:4326 sin CRS explícito acepta lat/lon (south,west,north,east).
    # Especificar el CRS en el BBOX (ej: '...,EPSG:4326') puede romper la consulta
    # en algunas versiones de GeoServer — no incluirlo es más portable.
    bbox_str = (
        f"{south},{west},{north},{east}"
        if source.bbox_latlon
        else f"{west},{south},{east},{north}"
    )

    with httpx.Client(timeout=60, follow_redirects=True) as client:
        while True:
            params = {
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeNames": source.layer,
                "BBOX": bbox_str,
                "count": batch_size,
                "startIndex": start,
                "outputFormat": "application/json",
                "srsName": "EPSG:4326",
            }
            logger.debug(f"  WFS GET startIndex={start} BBOX={bbox_str}")

            try:
                r = client.get(source.url, params=params)
                r.raise_for_status()
                data = r.json()
            except httpx.TimeoutException:
                raise RuntimeError(
                    f"Timeout consultando WFS {source.name} (startIndex={start}). "
                    "El servicio puede estar lento — reintentá en unos minutos."
                )
            except httpx.HTTPStatusError as e:
                raise RuntimeError(
                    f"WFS {source.name} HTTP {e.response.status_code}: {e.response.text[:300]}"
                )

            features = data.get("features", [])
            if not features:
                logger.info(f"  WFS: sin más features en startIndex={start} — paginación completa")
                break

            for feat in features:
                yield feat

            total += len(features)
            logger.info(f"  WFS: página startIndex={start} → {len(features)} features ({total} total)")

            if len(features) < batch_size:
                break

            start += batch_size
            if delay_ms > 0:
                time.sleep(delay_ms / 1000)


# ── Selección de fuente ───────────────────────────────────────────────────────

def _select_source_key(fuente: str, south: float, west: float, north: float, east: float) -> str:
    if fuente in ("capital", "provincia"):
        return fuente
    # auto: centroide de la zona dentro del bbox de la ciudad capital?
    center_lat = (south + north) / 2
    center_lng = (west + east) / 2
    cs, cw, cn, ce = _CAPITAL_BBOX
    if cs <= center_lat <= cn and cw <= center_lng <= ce:
        logger.info(f"SaltaCatastro: centroide ({center_lat:.4f},{center_lng:.4f}) → fuente 'capital' (IDEMSA)")
        return "capital"
    logger.info(f"SaltaCatastro: centroide ({center_lat:.4f},{center_lng:.4f}) → fuente 'provincia' (IDESA)")
    return "provincia"


# ── Upsert en DB ──────────────────────────────────────────────────────────────

def _upsert(
    conn,
    region_id: str,
    survey_id: str,
    source: WFSSource,
    geom,
    lat: float,
    lng: float,
    area: Optional[float],
    nomenclatura: str,
    catastro: Optional[str],
    localidad: Optional[str],
    municipio: str,
    numero: Optional[str],
) -> str:
    """Devuelve 'inserted' | 'updated'."""
    geom_wkt = geom.wkt

    existing_id = _find_existing(conn, region_id, nomenclatura)
    if existing_id:
        conn.execute(text("""
            UPDATE parcelas SET
                geometry            = ST_GeomFromText(:geom, 4326),
                centroid_lat        = :lat,
                centroid_lng        = :lng,
                area_m2_terreno     = COALESCE(:area, area_m2_terreno),
                cca_code            = COALESCE(:cca, cca_code),
                localidad           = COALESCE(:localidad, localidad),
                municipio           = COALESCE(:municipio, municipio),
                numero              = COALESCE(:numero, numero),
                estado_provincia    = 'Salta',
                pais                = 'Argentina',
                fuente_parcela      = :fuente
            WHERE parcela_id = :pid
        """), {
            "geom": geom_wkt, "lat": lat, "lng": lng, "area": area,
            "cca": catastro, "localidad": localidad, "municipio": municipio,
            "numero": numero, "fuente": source.fuente_parcela, "pid": existing_id,
        })
        return "updated"

    conn.execute(text("""
        INSERT INTO parcelas (
            parcela_id, survey_id, region_id,
            geometry, centroid_lat, centroid_lng,
            area_m2_terreno,
            numero, localidad, municipio, estado_provincia, pais,
            fuente_parcela, nomenclatura_catastral, cca_code
        ) VALUES (
            :pid, :sid, :region,
            ST_GeomFromText(:geom, 4326), :lat, :lng,
            :area,
            :numero, :localidad, :municipio, 'Salta', 'Argentina',
            :fuente, :nom, :cca
        )
    """), {
        "pid": str(uuid.uuid4()), "sid": survey_id, "region": region_id,
        "geom": geom_wkt, "lat": lat, "lng": lng, "area": area,
        "numero": numero, "localidad": localidad, "municipio": municipio,
        "fuente": source.fuente_parcela, "nom": nomenclatura, "cca": catastro,
    })
    return "inserted"


# ── Entry point ───────────────────────────────────────────────────────────────

@agent_run
def run(inp: SaltaCatastroInput) -> SaltaCatastroOutput:
    # 1. Zona
    zone_bbox, zone_polygon = _load_zone(inp.region_id)
    if zone_bbox is None:
        return SaltaCatastroOutput(
            ok=False,
            error=f"Región '{inp.region_id}' sin bbox ni zone_geojson. "
                  "Creá la región primero con GeoJSONZoneFetcher."
        )

    # Override de bbox si el usuario lo especifica (útil cuando zone_geojson
    # tiene features en múltiples países y la bbox unión es demasiado grande)
    if all(v is not None for v in (inp.bbox_south, inp.bbox_west, inp.bbox_north, inp.bbox_east)):
        south, west, north, east = inp.bbox_south, inp.bbox_west, inp.bbox_north, inp.bbox_east
        logger.info(f"SaltaCatastroFetcher: bbox override manual [{west},{south},{east},{north}]")
    else:
        south, west, north, east = zone_bbox

    # 2. Survey
    survey_id = inp.survey_id or _get_latest_survey(inp.region_id)
    if not survey_id:
        return SaltaCatastroOutput(
            ok=False,
            error=f"No hay survey activo para '{inp.region_id}'. Creá un survey primero."
        )

    # 3. Fuente WFS
    source_key = _select_source_key(inp.fuente, south, west, north, east)
    source = SOURCES[source_key]
    logger.info(
        f"SaltaCatastroFetcher: '{inp.region_id}' → {source.name} "
        f"bbox=[{west:.4f},{south:.4f},{east:.4f},{north:.4f}]"
    )

    # 4. Descarga y upsert
    insertadas = actualizadas = fuera_zona = invalidas = 0
    engine = get_engine()

    try:
        feat_iter = _wfs_features(source, west, south, east, north, inp.batch_size, inp.delay_ms)

        with engine.begin() as conn:
            for i, feat in enumerate(feat_iter):
                props = feat.get("properties") or {}
                geom_raw = feat.get("geometry")

                if not geom_raw:
                    invalidas += 1
                    continue

                # Geometría
                try:
                    geom = _to_polygon(shape(geom_raw))
                except Exception as e:
                    logger.warning(f"  Geometría inválida (feature {i}): {e}")
                    invalidas += 1
                    continue

                # Filtro por zona exacta
                if zone_polygon is not None and not zone_polygon.intersects(geom):
                    fuera_zona += 1
                    continue

                # Campos
                nomenclatura = _resolve(props, source.candidates_nomenclatura)
                if not nomenclatura:
                    logger.debug(f"  Feature {i} sin nomenclatura_catastral — saltado")
                    invalidas += 1
                    continue

                catastro = _resolve(props, source.candidates_catastro)
                localidad = _resolve(props, source.candidates_localidad)
                depto = _resolve(props, source.candidates_depto)
                municipio = _municipio_from_depto(depto)
                numero = _resolve_numero(props, source.candidates_numero)

                lat, lng = _centroid(geom)
                area = _area_m2(geom)

                result = _upsert(
                    conn, inp.region_id, survey_id, source,
                    geom, lat, lng, area,
                    nomenclatura, catastro, localidad, municipio, numero,
                )
                if result == "inserted":
                    insertadas += 1
                else:
                    actualizadas += 1

                if (i + 1) % 500 == 0:
                    logger.info(
                        f"  Progreso: {i+1} features — "
                        f"{insertadas} nuevas, {actualizadas} act, "
                        f"{fuera_zona} fuera, {invalidas} inválidas"
                    )

    except RuntimeError as e:
        # Errores de WFS ya logueados por _wfs_features
        return SaltaCatastroOutput(ok=False, error=str(e))
    except Exception as e:
        logger.exception(f"SaltaCatastroFetcher: error inesperado")
        return SaltaCatastroOutput(ok=False, error=str(e))

    logger.info(
        f"SaltaCatastroFetcher completo — {source.name}: "
        f"{insertadas} insertadas, {actualizadas} actualizadas, "
        f"{fuera_zona} fuera de zona, {invalidas} inválidas"
    )

    if insertadas == 0 and actualizadas == 0:
        logger.warning(
            f"SaltaCatastroFetcher: 0 parcelas procesadas. "
            f"Verificar que el bbox de la zona esté dentro de la cobertura de {source.name}. "
            f"Si la zona es provincial y el WFS IDESA está caído, reintentá en unos minutos."
        )

    return SaltaCatastroOutput(
        ok=True,
        parcelas_insertadas=insertadas,
        parcelas_actualizadas=actualizadas,
        fuera_zona=fuera_zona,
        fuente_usada=source.name,
        bbox_usado=f"{west:.5f},{south:.5f},{east:.5f},{north:.5f}",
    )
