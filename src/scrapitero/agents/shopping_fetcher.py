"""ShoppingFetcher — shoppings reales (no salen del CNPJ) desde OSM + Google.

Receita no identifica shopping centers (el CNAE 6822 es "administração de propriedade
imobiliária" = todas las inmobiliarias). Las fuentes que sí los identifican:
  - **OSM** `shop=mall` (gratis, Overpass).
  - **Google Places** `shopping_mall` (pago, descubrimiento por teselas).

Carga `establecimientos_poi` (mig. 031) con categoria='E', descripcion='SHOPPING', recortados
al polígono de la zona. Después `ParcelaCategoria` los aterriza sobre las parcelas junto a los
establecimientos CNPJ (union por ST_Contains), y aparecen como tipo **SHOPPING** en la web.
Idempotente por región (borra los POIs de las fuentes corridas y reinserta).
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.baseline_geocoder import _HEADERS, _tg
from scrapitero.db.engine import get_engine


class ShoppingFetcherInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None
    fuentes: list[str] = ["osm", "google"]
    max_requests: int = 40              # tope de teselas Google (pago)
    merge_dist_m: float = 150.0         # dedupe entre fuentes
    # Buffer de la zona para el recorte: un shopping tiene HUELLA GRANDE y su punto (centro del
    # edificio) puede caer retirado de la calle → fuera de un corredor angosto (scope calle+rango).
    # Con un buffer, el POI sobrevive y `ParcelaCategoria` lo aterriza si cae dentro de una parcela
    # del survey (recorte preciso). Default 0 = sin buffer (zonas dibujadas normales).
    zona_buffer_m: float = 0.0


class ShoppingFetcherOutput(BaseModel):
    ok: bool = True
    error: Optional[str] = None
    region_id: str = ""
    por_fuente: dict = {}
    en_zona: int = 0


def _dist_m(a, b, c, d) -> float:
    from math import asin, cos, radians, sin, sqrt
    x, y = radians(c - a), radians(d - b)
    u = sin(x / 2) ** 2 + cos(radians(a)) * cos(radians(c)) * sin(y / 2) ** 2
    return 2 * 6371000.0 * asin(sqrt(u))


def _fetch_osm_malls(s, w, n, e) -> list[dict]:
    from scrapitero.agents.osm_building_fetcher import _fetch_overpass
    q = ("[out:json][timeout:90];(" +
         "".join(f'{t}["shop"="mall"]({s},{w},{n},{e});' for t in ("node", "way", "relation")) +
         ");out center tags;")
    out = []
    for el in _fetch_overpass(q).get("elements", []):
        t = el.get("tags", {}) or {}
        if el.get("type") == "node":
            lat, lng = el.get("lat"), el.get("lon")
        else:
            ctr = el.get("center") or {}
            lat, lng = ctr.get("lat"), ctr.get("lon")
        if lat is None or lng is None:
            continue
        out.append({"nombre": t.get("name"), "lat": float(lat), "lng": float(lng), "fuente": "osm"})
    return out


def _fetch_google_malls(region_id, survey_id, max_requests) -> list[dict]:
    from scrapitero.agents import google_places_fetcher as gp
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        raise RuntimeError("GOOGLE_MAPS_API_KEY no configurada")
    zone_bbox, zone_poly = gp._load_zone(region_id, survey_id)
    if not zone_bbox:
        raise RuntimeError("región sin zona para buscar")
    lang = gp._detect_language(region_id)
    with httpx.Client(timeout=30, headers=_HEADERS) as client:
        places, *_ = gp._collect_places(
            client, api_key, zone_bbox, zone_poly,
            cell_size_m=400, min_cell_m=150, max_requests=max_requests,
            included_types=["shopping_mall"], language=lang)
    out = []
    for pl in places.values():
        loc = pl.get("location") or {}
        lat, lng = loc.get("latitude"), loc.get("longitude")
        if lat is None or lng is None:
            continue
        prim = (pl.get("primaryType") or "").lower()
        if prim and "shopping" not in prim and "mall" not in prim:
            continue
        out.append({"nombre": (pl.get("displayName") or {}).get("text"),
                    "lat": float(lat), "lng": float(lng), "fuente": "google"})
    return out


@agent_run
def run(input: ShoppingFetcherInput) -> ShoppingFetcherOutput:
    out = ShoppingFetcherOutput(region_id=input.region_id)
    engine = get_engine()
    with engine.connect() as conn:
        zona_gj = conn.execute(text(
            "SELECT COALESCE((SELECT subzona_geojson FROM surveys WHERE survey_id::text=:sid), zone_geojson) "
            "FROM regions WHERE region_id=:r"),
            {"r": input.region_id, "sid": input.survey_id}).scalar()
    if not zona_gj:
        return ShoppingFetcherOutput(ok=False, region_id=input.region_id,
                                     error="la región no tiene zona (zone_geojson)")
    from shapely.geometry import Point, shape
    from shapely.ops import unary_union
    gj = json.loads(zona_gj)
    geoms = ([shape(f["geometry"]) for f in gj.get("features", []) if f.get("geometry")]
             if gj.get("type") == "FeatureCollection" else [shape(gj.get("geometry", gj))])
    poly = unary_union(geoms).buffer(0)
    minx, miny, maxx, maxy = poly.bounds
    # Polígono de recorte (opcionalmente buffereado para captar shoppings retirados del corredor).
    poly_clip = poly.buffer(input.zona_buffer_m / 111000.0) if input.zona_buffer_m > 0 else poly

    crudos: list[dict] = []
    if "osm" in input.fuentes:
        try:
            o = _fetch_osm_malls(miny, minx, maxy, maxx)
            out.por_fuente["osm"] = len(o)
            crudos.extend(o)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ShoppingFetcher OSM falló: {e}")
    if "google" in input.fuentes:
        try:
            g = _fetch_google_malls(input.region_id, input.survey_id, input.max_requests)
            out.por_fuente["google"] = len(g)
            crudos.extend(g)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ShoppingFetcher Google falló: {e}")

    # recorte a zona (buffereada) + dedupe por proximidad
    ubicados = [h for h in crudos if poly_clip.contains(Point(h["lng"], h["lat"]))]
    final: list[dict] = []
    for h in ubicados:
        if any(_dist_m(h["lat"], h["lng"], f["lat"], f["lng"]) <= input.merge_dist_m for f in final):
            continue
        final.append(h)
    out.en_zona = len(final)

    fuentes_run = list(out.por_fuente.keys())
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM establecimientos_poi WHERE region_id=:r AND fuente=ANY(:f)"),
                     {"r": input.region_id, "f": fuentes_run})
        for h in final:
            conn.execute(text("""
                INSERT INTO establecimientos_poi (poi_id, region_id, fuente, categoria, descripcion, nombre, lat, lng)
                VALUES (:id, :r, :f, 'E', 'SHOPPING', :n, :lat, :lng)
            """), {"id": str(uuid.uuid4()), "r": input.region_id, "f": h["fuente"],
                   "n": h.get("nombre"), "lat": h["lat"], "lng": h["lng"]})

    _tg(f"🛍️ <b>Shoppings</b> ({input.region_id}): {out.en_zona} en zona ({out.por_fuente}).")
    logger.info(f"ShoppingFetcher {input.region_id}: {out.en_zona} en zona {out.por_fuente}")
    return out
