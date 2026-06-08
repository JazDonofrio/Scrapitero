"""ARBACadastralFetcher — descarga geometrías de parcelas desde IDERA WFS.

Fuente: geo.arba.gov.ar/geoserver/idera/wfs (layer idera:Parcela)

Dos formas de filtrar (la espacial es la preferida — todo relevamiento parte del GeoJSON):
- **Espacial (default):** bbox del polígono de la zona (`regions.zone_geojson`) vía el
  parámetro WFS `bbox=...,EPSG:4326` (que reproyecta correctamente desde el CRS nativo
  Gauss-Krüger del layer) y luego recorte exacto al polígono con shapely. NO requiere
  nomenclatura catastral.
- **Por nomenclatura (opcional):** CQL `cca LIKE '{prefix}%'` con prefix armado desde
  partido/circ/secc/manzana. Útil para bajar una manzana puntual.

Nota: el CQL `INTERSECTS(geom, WKT)` NO se usa porque GeoServer interpreta el WKT en el
CRS nativo del layer (metros), no en lat/lon — por eso se filtra por `bbox` + recorte local.

Output: filas insertadas en tabla `parcelas` con geometría y centroide.
"""

from __future__ import annotations

import json
import uuid
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from shapely.geometry import shape
from shapely.ops import unary_union
from sqlalchemy import text

from scrapitero.db.engine import get_engine
from scrapitero.agents._run import agent_run
from scrapitero.agents import geo


IDERA_WFS = "https://geo.arba.gov.ar/geoserver/idera/wfs"


# ── Pydantic I/O ──────────────────────────────────────────────────────────────

class ARBAInput(BaseModel):
    region_id: str                          # "ituzaingo-ba-ar"
    survey_id: str
    # Nomenclatura catastral: OPCIONAL. Si se omite, se baja por filtro espacial
    # (polígono de la zona). Si se pasa completa, se filtra por prefijo CCA (una manzana).
    partido_id: Optional[str] = None        # "136"
    circunscripcion: Optional[str] = None    # "2"
    seccion: Optional[str] = None            # "C"
    manzana: Optional[str] = None            # "184"


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


# ── IDERA WFS — filtro espacial (desde el GeoJSON de la zona) ──────────────────

def _load_zone_polygon(region_id: str):
    """Devuelve el polígono (shapely) de la zona desde regions.zone_geojson.

    Acepta FeatureCollection / Feature / geometría. Devuelve None si la región
    no tiene zone_geojson (no se puede filtrar espacialmente sin polígono).
    """
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT zone_geojson FROM regions WHERE region_id = :r"),
            {"r": region_id},
        ).fetchone()
    if not row or not row[0]:
        return None
    gj = row[0]
    if isinstance(gj, str):
        gj = json.loads(gj)
    geoms = []
    if gj.get("type") == "FeatureCollection":
        for f in gj.get("features", []):
            if f.get("geometry"):
                geoms.append(shape(f["geometry"]))
    elif gj.get("type") == "Feature":
        if gj.get("geometry"):
            geoms.append(shape(gj["geometry"]))
    else:
        geoms.append(shape(gj))
    if not geoms:
        return None
    poly = unary_union(geoms)
    return poly if poly.is_valid else poly.buffer(0)


def fetch_idera_spatial(zone_poly, max_features: int = 50000) -> list[dict]:
    """Baja parcelas de IDERA por el bbox del polígono de la zona y las recorta
    exactamente al polígono con shapely.

    Se usa el parámetro WFS `bbox=minx,miny,maxx,maxy,EPSG:4326` (que reproyecta
    desde el CRS nativo del layer) en vez de CQL INTERSECTS (que falla por orden de
    ejes / CRS nativo). El recorte fino al polígono se hace localmente.
    """
    minx, miny, maxx, maxy = zone_poly.bounds
    bbox = f"{minx},{miny},{maxx},{maxy},EPSG:4326"
    logger.info(f"IDERA WFS — bbox espacial: {bbox}")

    with httpx.Client(timeout=60) as client:
        r = client.get(IDERA_WFS, params={
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": "idera:Parcela",
            "srsName": "EPSG:4326",
            "outputFormat": "application/json",
            "count": str(max_features),
            "bbox": bbox,
        })
        r.raise_for_status()

    raw = r.json().get("features", [])
    logger.info(f"IDERA WFS bbox devolvió {len(raw)} features (sin recortar)")
    if len(raw) >= max_features:
        logger.warning(
            f"IDERA WFS alcanzó el tope de {max_features} features — la zona puede "
            "estar truncada. Subdividir el GeoJSON o subir max_features."
        )

    # Recorte fino: quedarse solo con las parcelas que intersectan el polígono real.
    kept = []
    for feat in raw:
        g = feat.get("geometry")
        if not g:
            continue
        try:
            if shape(g).intersects(zone_poly):
                kept.append(feat)
        except Exception:
            continue
    logger.info(f"IDERA: {len(kept)} parcelas dentro del polígono de la zona")
    return kept


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
    # Huso UTM correcto según la posición (antes 21S fijo). Genérico vía geo.area_m2.
    return geo.area_m2(geom)


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
                        area_m2_terreno = :area,
                        cca_code = :cca
                    WHERE parcela_id = :pid
                """), {"geom": geom_wkt, "area": area, "cca": cca or None, "pid": str(existing[0])})
                actualizadas += 1
            else:
                new_id = uuid.uuid4()
                conn.execute(text("""
                    INSERT INTO parcelas
                        (parcela_id, survey_id, region_id,
                         geometry, centroid_lat, centroid_lng,
                         area_m2_terreno, fuente_parcela, cca_code)
                    VALUES
                        (:pid, :sid, :region,
                         ST_GeomFromText(:geom, 4326), :lat, :lng,
                         :area, 'arba_idera', :cca)
                """), {
                    "pid": str(new_id), "sid": survey_id, "region": region_id,
                    "geom": geom_wkt, "lat": lat, "lng": lng, "area": area,
                    "cca": cca or None,
                })
                insertadas += 1

    logger.info(f"DB: {insertadas} insertadas, {actualizadas} actualizadas")
    return insertadas, actualizadas


# ── Entry point ───────────────────────────────────────────────────────────────

@agent_run
def run(input: ARBAInput) -> ARBAOutput:
    # Camino por nomenclatura solo si viene COMPLETA; si no, filtro espacial (GeoJSON).
    por_nomenclatura = all([
        input.partido_id, input.circunscripcion, input.seccion, input.manzana
    ])
    try:
        if por_nomenclatura:
            features = fetch_idera(
                input.partido_id, input.circunscripcion,
                input.seccion, input.manzana
            )
            if not features:
                return ARBAOutput(ok=False, error=(
                    f"IDERA WFS no devolvió parcelas para "
                    f"Partido={input.partido_id} Circ={input.circunscripcion} "
                    f"Secc={input.seccion} Mza={input.manzana}. "
                    "Verificar nomenclatura."
                ))
            fuente = "idera_wfs"
        else:
            zone_poly = _load_zone_polygon(input.region_id)
            if zone_poly is None:
                return ARBAOutput(ok=False, error=(
                    f"La región '{input.region_id}' no tiene zone_geojson para filtrar "
                    "espacialmente. Creá la zona desde un GeoJSON (GeoJSONZoneFetcher) o "
                    "pasá la nomenclatura completa (partido/circunscripcion/seccion/manzana)."
                ))
            features = fetch_idera_spatial(zone_poly)
            if not features:
                return ARBAOutput(ok=False, error=(
                    f"IDERA WFS no devolvió parcelas dentro del polígono de "
                    f"'{input.region_id}'. Verificar que la zona esté en Provincia de "
                    "Buenos Aires y que el WFS de IDERA (geo.arba.gov.ar) esté disponible."
                ))
            fuente = "idera_wfs_espacial"

        insertadas, actualizadas = _upsert_parcelas(
            features, input.region_id, input.survey_id
        )
        return ARBAOutput(
            ok=True,
            parcelas_insertadas=insertadas,
            parcelas_actualizadas=actualizadas,
            fuentes=[fuente],
        )
    except httpx.HTTPStatusError as e:
        return ARBAOutput(ok=False, error=f"IDERA HTTP {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        logger.exception("ARBACadastralFetcher falló")
        return ARBAOutput(ok=False, error=str(e))
