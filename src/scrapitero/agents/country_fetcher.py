"""CountryFetcher — detecta barrios cerrados / condomínios (loteamentos fechados) desde OSM y
marca las parcelas que caen dentro.

A diferencia de shoppings/hoteles (POIs puntuales), un "country" es un ÁREA que contiene muchas
parcelas. No hay fuente catastral/CNPJ que lo identifique, así que se toma de **OpenStreetMap**:
polígonos con `landuse=residential`+`residential=gated` (tag autoritativo) o `landuse=residential`
cuyo nombre matchee `condom…/loteamento fechado`. Se recortan a la zona y se **estampa**
`parcelas.es_country=true` (mig. 042) para las parcelas cuyo centroide cae dentro de algún área.

Alimenta la capa "🏘 Country" del mapa. Idempotente por survey/región (resetea el flag antes de
re-aplicar). Best-effort: si Overpass no responde, no rompe (deja las parcelas sin marcar).
Patrón: `ShoppingFetcher` (recorte a zona) + stamping tipo `ParcelaCategoria`.
"""

from __future__ import annotations

from typing import Optional

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.baseline_geocoder import _tg
from scrapitero.db.engine import get_engine


class CountryFetcherInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None
    # nombre que delata un condomínio/loteamento fechado (además del tag autoritativo residential=gated)
    name_regex: str = "condom|loteamento fechado|residencial.*fechad"


class CountryFetcherOutput(BaseModel):
    ok: bool = True
    error: Optional[str] = None
    region_id: str = ""
    areas: int = 0                 # áreas de country detectadas dentro de la zona
    parcelas_marcadas: int = 0


def _zona_poly(region_id: str, survey_id: Optional[str]):
    """Polígono de la zona (subzona del survey o zone_geojson de la región) como shapely."""
    import json
    from shapely.geometry import shape
    from shapely.ops import unary_union
    engine = get_engine()
    with engine.connect() as conn:
        zona_gj = conn.execute(text(
            "SELECT COALESCE((SELECT subzona_geojson FROM surveys WHERE survey_id::text=:sid), "
            "zone_geojson) FROM regions WHERE region_id=:r"),
            {"r": region_id, "sid": survey_id}).scalar()
    if not zona_gj:
        return None
    gj = json.loads(zona_gj)
    geoms = ([shape(f["geometry"]) for f in gj.get("features", []) if f.get("geometry")]
             if gj.get("type") == "FeatureCollection" else [shape(gj.get("geometry", gj))])
    return unary_union(geoms).buffer(0)


def _rings_de_geometry(geom: list):
    """Anillo cerrado (lista [(lon,lat)]) de una geometría Overpass `out geom`, o None."""
    pts = [(g["lon"], g["lat"]) for g in (geom or []) if "lon" in g and "lat" in g]
    if len(pts) < 4:
        return None
    if pts[0] != pts[-1]:
        pts.append(pts[0])          # cerrar el anillo si vino abierto
    return pts


def _areas_osm(s, w, n, e, name_regex: str):
    """Polígonos shapely de barrios cerrados/condomínios en el bbox (una consulta Overpass)."""
    from shapely.geometry import Polygon
    from scrapitero.agents.osm_building_fetcher import _fetch_overpass
    bbox = f"{s},{w},{n},{e}"
    # Dos familias: (1) tag autoritativo `residential=gated`; (2) cualquier área CON NOMBRE de
    # condomínio/loteamento fechado. No se restringe a landuse=residential porque en Brasil los
    # condomínios se taguean de varias formas (landuse/place/leisure) — la precisión la da el
    # filtro de ANILLO CERRADO (`_rings_de_geometry`): una vía abierta (calle "Condomínio X") no
    # forma polígono y se descarta; solo sobreviven las áreas cerradas.
    q = ("[out:json][timeout:90];("
         f'way["residential"="gated"]({bbox});'
         f'relation["residential"="gated"]({bbox});'
         f'way["name"~"{name_regex}",i]({bbox});'
         f'relation["name"~"{name_regex}",i]({bbox});'
         ");out geom;")
    data = _fetch_overpass(q, timeout=55)
    polys = []
    for el in (data.get("elements") if data else []) or []:
        nombre = (el.get("tags") or {}).get("name")
        if el.get("type") == "way":
            ring = _rings_de_geometry(el.get("geometry"))
            if ring:
                try:
                    polys.append((Polygon(ring).buffer(0), nombre))
                except Exception:  # noqa: BLE001
                    pass
        elif el.get("type") == "relation":
            # multipolígono: unir los anillos cerrados de sus miembros (best-effort)
            for mem in el.get("members", []):
                ring = _rings_de_geometry(mem.get("geometry"))
                if ring:
                    try:
                        polys.append((Polygon(ring).buffer(0), nombre))
                    except Exception:  # noqa: BLE001
                        pass
    return polys


@agent_run
def run(input: CountryFetcherInput) -> CountryFetcherOutput:
    from shapely.geometry import Point
    from shapely.ops import unary_union
    out = CountryFetcherOutput(region_id=input.region_id)
    engine = get_engine()

    poly = _zona_poly(input.region_id, input.survey_id)
    if poly is None or poly.is_empty:
        return CountryFetcherOutput(ok=False, region_id=input.region_id,
                                    error="la región no tiene zona (zone_geojson)")
    minx, miny, maxx, maxy = poly.bounds

    areas = _areas_osm(miny, minx, maxy, maxx, input.name_regex)
    # recortar a la zona (el área tiene que TOCAR la zona relevada)
    en_zona = [(g, nm) for (g, nm) in areas if g and not g.is_empty and g.intersects(poly)]
    out.areas = len(en_zona)
    country = unary_union([g for g, _ in en_zona]) if en_zona else None

    # Estampar es_country por centroide de parcela dentro de algún área. Idempotente: reset primero.
    scope_sql = "survey_id::text = :sid" if input.survey_id else "region_id = :r"
    params = {"sid": input.survey_id} if input.survey_id else {"r": input.region_id}
    with engine.begin() as conn:
        conn.execute(text(f"UPDATE parcelas SET es_country=false WHERE {scope_sql}"), params)
        if country is not None and not country.is_empty:
            rows = conn.execute(text(
                f"SELECT parcela_id::text, centroid_lng, centroid_lat FROM parcelas "
                f"WHERE {scope_sql} AND centroid_lat IS NOT NULL AND centroid_lng IS NOT NULL"),
                params).fetchall()
            dentro = [r[0] for r in rows if country.contains(Point(float(r[1]), float(r[2])))]
            for i in range(0, len(dentro), 500):
                conn.execute(text("UPDATE parcelas SET es_country=true WHERE parcela_id::text = ANY(:ids)"),
                             {"ids": dentro[i:i + 500]})
            out.parcelas_marcadas = len(dentro)

    _tg(f"🏘️ <b>Country</b> ({input.region_id}): {out.areas} área(s) cerrada(s) en zona, "
        f"{out.parcelas_marcadas} parcelas marcadas.")
    logger.info(f"CountryFetcher {input.region_id}: areas={out.areas} "
                f"parcelas_marcadas={out.parcelas_marcadas}")
    return out
