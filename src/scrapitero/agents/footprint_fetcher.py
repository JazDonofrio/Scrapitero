"""FootprintFetcher — footprints de edificios para una capa de REVISIÓN visual (no toca el
relevamiento).

Objetivo: comparar lo que dice el catastro (BCI en Brasil, exacto pero puede estar
desactualizado si hubo una construcción no declarada) contra un footprint independiente
derivado de imagen satelital, para que el operador lo revise a ojo antes de decidir si
hace falta actuar. No alimenta `edificios` (esa tabla la usa `UnidadesEstimator` en
Salta/PBA para estimar UF) ni `parcelas` — escribe en `footprints_revision` (mig. 044),
de solo lectura para el mapa.

Prioridad de fuente (por bbox de la región/survey):
  1. **Google Open Buildings** — vía el mirror de VIDA en FlatGeobuf
     (`source.coop/vida/google-microsoft-open-buildings`, republica el mismo dataset de
     Google partido por país con una columna `bf_source` que permite filtrar solo los
     edificios de origen Google). Se consulta con DuckDB (`httpfs`+`spatial`, ya son
     dependencias del proyecto) usando el índice espacial de FlatGeobuf — no descarga el
     archivo completo del país. El país (ISO-3) se autodetecta del centroide del bbox con
     `geo.detect_country`, así queda genérico para cualquier región del mundo.
  2. **OSM (fallback)** — si el archivo de ese país no existe, la query falla, o no hay
     resultados en el bbox: reusa tal cual los helpers de `osm_building_fetcher.py`
     (Overpass).

Idempotente por survey: borra + reinserta (mismo patrón que ShoppingFetcher/HotelFetcher/
CountryFetcher). Vincula cada footprint a su parcela por `ST_Contains` (igual que
OSMBuildingFetcher).
"""

from __future__ import annotations

import uuid
from typing import Optional

from loguru import logger
from pydantic import BaseModel
from shapely import wkb
from shapely.geometry.base import BaseGeometry
from sqlalchemy import text

from scrapitero.agents import geo
from scrapitero.agents._run import agent_run
from scrapitero.agents.osm_building_fetcher import (
    _bbox_from_db,
    _fetch_overpass,
    _nodes_to_dict,
    _overpass_query,
    _way_to_polygon,
)
from scrapitero.db.engine import get_engine

_VIDA_BUCKET = "s3://us-west-2.opendata.source.coop/vida/google-microsoft-open-buildings/flageobuf/by_country"


class FootprintInput(BaseModel):
    region_id: str
    survey_id: str
    bbox_buffer_deg: float = 0.001
    min_confidence: float = 0.65      # piso de Google Open Buildings (rango 0.65-1.0)


class FootprintOutput(BaseModel):
    ok: bool
    fuente_usada: Optional[str] = None      # google_open_buildings | osm
    footprints_insertados: int = 0
    vinculados_a_parcela: int = 0
    parcelas_con_huella: int = 0            # con construcción detectada (mig. 058)
    bbox_usado: Optional[str] = None
    pais: Optional[str] = None
    error: Optional[str] = None


# ── Google Open Buildings (vía mirror de VIDA en FlatGeobuf) ──────────────────────────

def _google_open_buildings(bbox: tuple, iso3: str, min_confidence: float) -> list[tuple]:
    """(external_id, poligono_shapely, area_m2, confidence) desde el fgb de VIDA para ese país.

    Devuelve [] si el archivo no existe para ese país o si no hay edificios en el bbox
    (ambos casos son "sin cobertura", el caller cae a OSM)."""
    import duckdb

    south, west, north, east = bbox
    url = f"{_VIDA_BUCKET}/country_iso={iso3}/{iso3}.fgb"

    con = duckdb.connect()
    con.execute("INSTALL httpfs"); con.execute("LOAD httpfs")
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    con.execute("SET s3_region='us-west-2'")
    con.execute("SET s3_url_style='path'")     # el estilo virtual-host rompe TLS (bucket con puntos)

    rows = con.execute(
        """
        SELECT OGC_FID, confidence, area_in_meters, geom
        FROM ST_Read(?)
        WHERE bf_source = 'google'
          AND confidence >= ?
          AND ST_Intersects(geom, ST_MakeEnvelope(?, ?, ?, ?))
        """,
        [url, min_confidence, west, south, east, north],
    ).fetchall()

    out = []
    for fid, confidence, area_m2, geom_wkb in rows:
        try:
            geom = wkb.loads(bytes(geom_wkb))
        except Exception:
            continue
        poly = _as_polygon(geom)
        if poly is None:
            continue
        out.append((str(fid), poly, float(area_m2), float(confidence)))
    return out


def _as_polygon(geom: BaseGeometry):
    """Normaliza a Polygon simple (la columna `footprint` es POLYGON, no MULTIPOLYGON)."""
    if geom.geom_type == "Polygon":
        return geom if geom.is_valid else geom.buffer(0)
    if geom.geom_type == "MultiPolygon" and len(geom.geoms) > 0:
        mayor = max(geom.geoms, key=lambda g: g.area)
        return mayor if mayor.is_valid else mayor.buffer(0)
    return None


# ── OSM (fallback) ──────────────────────────────────────────────────────────────────────

def _osm_footprints(bbox: tuple) -> list[tuple]:
    """(external_id, poligono_shapely, area_m2, confidence=None) desde Overpass."""
    south, west, north, east = bbox
    query = _overpass_query(south, west, north, east)
    data = _fetch_overpass(query)
    elements = data.get("elements", [])
    nodes = _nodes_to_dict(elements)

    out = []
    for way in elements:
        if way.get("type") != "way":
            continue
        poly = _way_to_polygon(way, nodes)
        if not poly:
            continue
        out.append((str(way["id"]), poly, geo.area_m2(poly), None))
    return out


# ── Persistencia ──────────────────────────────────────────────────────────────────────

def _guardar(rows: list[tuple], fuente: str, region_id: str, survey_id: str) -> int:
    """Borra los footprints previos del survey y reinserta (idempotente)."""
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM footprints_revision WHERE survey_id = :sid"),
                     {"sid": survey_id})
        for external_id, poly, area_m2, confidence in rows:
            conn.execute(text("""
                INSERT INTO footprints_revision
                    (footprint_id, region_id, survey_id, source, external_id,
                     footprint, centroid, area_m2, confidence)
                VALUES
                    (:fid, :rid, :sid, :source, :eid,
                     ST_GeomFromText(:geom, 4326), ST_GeomFromText(:ctr, 4326),
                     :area, :confidence)
            """), {
                "fid": str(uuid.uuid4()),
                "rid": region_id,
                "sid": survey_id,
                "source": fuente,
                "eid": external_id,
                "geom": poly.wkt,
                "ctr": poly.centroid.wkt,
                "area": area_m2,
                "confidence": confidence,
            })
    return len(rows)


def _link_a_parcelas(region_id: str, survey_id: str) -> int:
    """Vincula cada footprint sin parcela a la que contiene su centroide (ST_Contains).

    Filtra por `region_id` además de `survey_id`: varias regiones de prueba de VG se
    superponen geográficamente, y sin este filtro un footprint podía vincularse a una
    parcela de OTRA región solo porque su polígono también cubría ese punto."""
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE footprints_revision f SET parcela_id = p.parcela_id
            FROM parcelas p
            WHERE f.survey_id = :sid
              AND p.region_id = :rid
              AND p.geometry IS NOT NULL
              AND f.centroid IS NOT NULL
              AND ST_Contains(p.geometry, f.centroid)
        """), {"sid": survey_id, "rid": region_id})
        return result.rowcount or 0


# Solape mínimo (m²) para que una huella cuente como construcción DE esta parcela. Mismo
# umbral que las guardas de `OverturePlacesFetcher` y que `IncidenciasReporter`: por debajo
# es el roce de digitalización del edificio del vecino, no un edificio propio.
HUELLA_MIN_M2 = 25.0


def _calcular_huella_m2(survey_id: str) -> int:
    """Materializa en `parcelas.huella_m2` los m² construidos que ve el satélite (mig. 058).

    Es la única señal de "acá hay algo construido" que funciona en Argentina: ARBA no publica
    área construida y una parcela puramente comercial tiene `uf_vivienda=0`, así que sin esto
    `_tipo_edificacion` la rotula LOTE VAZIO (medido en Malvinas + Hurlingham el 13-ago-2026:
    59 rotuladas baldío, 52 con edificios adentro).

    Se calcula acá —y no al vuelo en cada consulta— porque el cálculo espacial cuesta ~700 ms
    por relevamiento y la etiqueta se pide desde el mapa, el panel, el DXF y el CSV.

    **Escribe 0, no NULL, cuando la parcela no tiene ninguna huella.** El NULL queda reservado
    para "este relevamiento no tiene footprints cargados", que es una afirmación distinta: es
    la misma distinción que `_link_to_parcelas` hace para decidir si aplicar sus guardas.

    Devuelve cuántas parcelas quedaron con construcción detectada.
    """
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE parcelas p SET huella_m2 = COALESCE(s.m2, 0)
            FROM parcelas base
            LEFT JOIN LATERAL (
                -- ST_Union antes de medir: dos huellas de la misma fuente pueden pisarse
                -- (un galpón digitalizado en dos piezas) y sumar los solapes por separado
                -- inflaría el área construida.
                SELECT ST_Area(ST_Union(ST_Intersection(base.geometry,
                                                        f.footprint))::geography) AS m2
                FROM footprints_revision f
                WHERE f.survey_id = base.survey_id
                  AND ST_Intersects(base.geometry, f.footprint)
                  AND ST_Area(ST_Intersection(base.geometry,
                                              f.footprint)::geography) >= :amin
            ) s ON TRUE
            WHERE base.survey_id = CAST(:sid AS uuid)
              AND base.geometry IS NOT NULL
              AND p.parcela_id = base.parcela_id
        """), {"sid": survey_id, "amin": HUELLA_MIN_M2})
        return conn.execute(text(
            "SELECT count(*) FROM parcelas "
            "WHERE survey_id = CAST(:sid AS uuid) AND COALESCE(huella_m2, 0) > 0"
        ), {"sid": survey_id}).scalar() or 0


# ── Entry point ───────────────────────────────────────────────────────────────────────

@agent_run
def run(input: FootprintInput) -> FootprintOutput:
    engine = get_engine()

    bbox = _bbox_from_db(engine, input.region_id, input.bbox_buffer_deg)
    if not bbox:
        return FootprintOutput(
            ok=False,
            error="No se encontraron parcelas con coordenadas para derivar el bbox.",
        )
    south, west, north, east = bbox
    bbox_str = f"{south},{west},{north},{east}"

    centro_lat, centro_lng = (south + north) / 2, (west + east) / 2
    iso3 = geo.detect_country(centro_lat, centro_lng)

    fuente = None
    rows: list[tuple] = []

    if iso3:
        try:
            rows = _google_open_buildings(bbox, iso3, input.min_confidence)
            if rows:
                fuente = "google_open_buildings"
        except Exception as exc:
            logger.warning(f"FootprintFetcher: Google Open Buildings falló para {iso3}: {exc!r}")
    else:
        logger.warning("FootprintFetcher: no se pudo determinar el país del bbox, se salta Google Open Buildings")

    if not rows:
        try:
            rows = _osm_footprints(bbox)
            fuente = "osm" if rows else fuente
        except Exception as exc:
            return FootprintOutput(
                ok=False, bbox_usado=bbox_str, pais=iso3,
                error=f"Google Open Buildings sin resultados y OSM falló: {exc!r}",
            )

    if not rows:
        return FootprintOutput(
            ok=True, fuente_usada=None, footprints_insertados=0,
            bbox_usado=bbox_str, pais=iso3,
        )

    insertados = _guardar(rows, fuente, input.region_id, input.survey_id)
    vinculados = _link_a_parcelas(input.region_id, input.survey_id)
    con_huella = _calcular_huella_m2(input.survey_id)

    logger.info(f"FootprintFetcher {input.region_id}: fuente={fuente} "
                f"insertados={insertados} vinculados={vinculados} "
                f"parcelas_con_huella={con_huella}")

    return FootprintOutput(
        ok=True,
        fuente_usada=fuente,
        footprints_insertados=insertados,
        vinculados_a_parcela=vinculados,
        parcelas_con_huella=con_huella,
        bbox_usado=bbox_str,
        pais=iso3,
    )
