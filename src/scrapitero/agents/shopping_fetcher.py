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
import re
import unicodedata
import uuid
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.baseline_geocoder import _HEADERS, _tg
from scrapitero.db.engine import get_engine

# Palabras genéricas que no distinguen un shopping de otro ("Shopping Iguatemi" vs
# "Iguatemi Shopping Center" deben matchear por el núcleo "iguatemi").
_GENERICOS_SHOPPING = {
    "shopping", "shoppings", "mall", "center", "centro", "galeria", "galería",
    "de", "da", "do", "dos", "das", "e",
}


def _norm(s: Optional[str]) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", str(s or ""))
                if not unicodedata.combining(c))
    return " ".join(s.lower().split())


# Igual que en `hotel_fetcher`: la puntuación viaja pegada al token y arruina el match
# (`Hotel "El Mesidor"` vs `El Mesidor` daban {'"el','mesidor"'} vs {'el','mesidor'}). Se saca
# sólo para comparar; el nombre guardado queda como lo dio la fuente.
_PUNTUACION_NOMBRE = re.compile(r"""["'“”‘’()\[\]{}.,;:!¡?¿/\\|_*+~`^<>–—-]+""")


def _tokens_sig(n: str) -> set:
    return {t for t in _PUNTUACION_NOMBRE.sub(" ", n).split()
            if t not in _GENERICOS_SHOPPING and len(t) > 1}


def _nombre_similar(a: Optional[str], b: Optional[str]) -> bool:
    """¿Mismo shopping? Sin nombre en alguno de los dos, no hay señal — quien llama
    decide el fallback (a distancia sola). Igual que hotel_fetcher._nombre_similar:
    NO alcanza compartir una sola palabra genérica."""
    import difflib
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    sa, sb = _tokens_sig(na), _tokens_sig(nb)
    if sa and sb and (sa == sb or len(sa & sb) >= 2):
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.82


def _nombre_fuerte(a: Optional[str], b: Optional[str]) -> bool:
    """Nombre IDÉNTICO (normalizado) o mismo conjunto de tokens significativos.

    Habilita el radio amplio del dedupe. Es más estricto que `_nombre_similar`: no le
    alcanza el 0.82 de difflib ni compartir 2 tokens — 'Plaza Oeste' y 'Plaza Norte'
    comparten uno y se parecen, y son shoppings distintos.
    """
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    sa, sb = _tokens_sig(na), _tokens_sig(nb)
    return bool(sa) and sa == sb


class ShoppingFetcherInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None
    # `shoppings_ar` = directorio shoppings.com.ar (gratis, solo Argentina). No entra en el
    # default para no pegarle al sitio en cada corrida de una región brasilera.
    fuentes: list[str] = ["osm", "google"]
    max_requests: int = 40              # tope de teselas Google (pago)
    merge_dist_m: float = 150.0         # dedupe entre fuentes
    # Radio amplio cuando el nombre es FUERTE (idéntico o mismos tokens significativos).
    # Un shopping ocupa una manzana entera y cada fuente lo apunta donde quiere: OSM da el
    # centro del polígono del edificio, Overture el POI de una tienda ancla o la entrada.
    # Medido en Malvinas (11-ago-2026): 'Terrazas de Mayo Shopping' entró dos veces —OSM y
    # Overture— con el nombre IDÉNTICO y a 175 m, o sea 25 m por encima del corte. Mismo
    # criterio escalonado que `hotel_fetcher` (merge_dist_fuerte_m), con un radio acorde a
    # la huella de un mall en vez de a la dirección fiscal de un hotel.
    merge_dist_fuerte_m: float = 600.0
    shoppings_ar_urls: list[str] = []           # [] = la página de provincia de Buenos Aires
    shoppings_ar_provincia: str = "Buenos Aires"
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
    # fuente → motivo del fallo (Overpass caído, Google sin key…). Iba sólo a un
    # `logger.warning`, así que el output decía "0 shoppings" sin distinguirlo de una zona
    # que no tiene ninguno — el mismo agujero que tapamos en `hotel_fetcher`. Las filas de
    # una fuente caída NO se pierden: `fuentes_run` sale de `por_fuente`, que sólo se llena
    # cuando la fuente respondió, así que no entran al DELETE y vuelven como semillas.
    fuentes_fallidas: dict = {}
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
            out.fuentes_fallidas["osm"] = str(e)
            logger.warning(f"ShoppingFetcher OSM falló: {e}")
    if "google" in input.fuentes:
        try:
            g = _fetch_google_malls(input.region_id, input.survey_id, input.max_requests)
            out.por_fuente["google"] = len(g)
            crudos.extend(g)
        except Exception as e:  # noqa: BLE001
            out.fuentes_fallidas["google"] = str(e)
            logger.warning(f"ShoppingFetcher Google falló: {e}")
    if "shoppings_ar" in input.fuentes:
        # Directorio curado de shoppings argentinos (shoppings.com.ar). Aporta el NOMBRE
        # comercial real, que OSM/Google suelen tener incompleto o mal tageado. Gratis: se
        # geocodifica con georef-ar y, lo que no tiene dirección postal (accesos de ruta),
        # por nombre en OSM validando el partido. Cubre toda la provincia, así que el
        # recorte a la zona de abajo es el que deja los que corresponden.
        try:
            from scrapitero.agents.shoppings_ar import fetch_shoppings_ar
            d = fetch_shoppings_ar(urls=input.shoppings_ar_urls or None,
                                   provincia=input.shoppings_ar_provincia)
            out.por_fuente["shoppings_ar"] = len(d)
            crudos.extend(d)
        except Exception as e:  # noqa: BLE001
            out.fuentes_fallidas["shoppings_ar"] = str(e)
            logger.warning(f"ShoppingFetcher shoppings.com.ar falló: {e}")

    # recorte a zona (buffereada)
    ubicados = [h for h in crudos if poly_clip.contains(Point(h["lng"], h["lat"]))]
    fuentes_run = list(out.por_fuente.keys())

    # Semillas cross-run: shoppings ya guardados en DB de fuentes que NO corren hoy (p.ej.
    # Google, pago, de una corrida anterior; el paso gratis del pipeline solo corre OSM).
    # Sin esto, cada combinación de fuentes distinta duplicaba el mismo shopping físico.
    with engine.connect() as conn:
        semillas = [dict(r._mapping) for r in conn.execute(text("""
            SELECT poi_id::text, nombre, lat, lng, fuente FROM establecimientos_poi
            WHERE region_id=:r AND categoria='E' AND descripcion='SHOPPING'
              AND NOT (fuente = ANY(:f))
        """), {"r": input.region_id, "f": fuentes_run})]
    for s in semillas:
        s["_seed_id"] = s.pop("poi_id")

    # Dedupe por nombre + distancia (si ambos tienen nombre) o por sola distancia (si a
    # alguno le falta, típico de un nodo OSM shop=mall sin tag name) — nunca por la sola
    # cercanía cuando los dos nombres están y son distintos, para no fusionar dos
    # shoppings/galerías reales que casualmente están cerca.
    final: list[dict] = list(semillas)
    for h in ubicados:
        destino = None
        for f in final:
            d = _dist_m(h["lat"], h["lng"], f["lat"], f["lng"])
            if d > input.merge_dist_fuerte_m:
                continue
            if h.get("nombre") and f.get("nombre"):
                # Nombre fuerte → radio amplio (la huella del mall); nombre apenas parecido
                # → hay que estar cerca, para no fusionar dos galerías distintas.
                if _nombre_fuerte(h["nombre"], f["nombre"]) or (
                        d <= input.merge_dist_m and _nombre_similar(h["nombre"], f["nombre"])):
                    destino = f
                    break
            elif d <= input.merge_dist_m:
                # Sin nombre en alguno (nodo OSM shop=mall sin tag `name`): sólo distancia,
                # y por eso se exige el radio corto — no hay nada que confirme que es el mismo.
                destino = f
                break
        if destino is None:
            final.append(h)
        else:
            if not destino.get("nombre") and h.get("nombre"):
                destino["nombre"] = h["nombre"]
            destino["_dirty"] = True
    out.en_zona = len(final)

    # Ninguna fuente respondió: lo que hay en `final` son las semillas de corridas viejas, no
    # el resultado de ésta. Devolverlo como éxito diría "la zona tiene N shoppings" cuando en
    # realidad no se pudo mirar. No se escribe nada (el DELETE tendría `fuentes_run` vacío,
    # pero igual conviene salir sin tocar la tabla).
    if out.fuentes_fallidas and not out.por_fuente:
        motivo = "; ".join(f"{k}: {v}" for k, v in out.fuentes_fallidas.items())
        return ShoppingFetcherOutput(
            ok=False, region_id=input.region_id, por_fuente=out.por_fuente,
            fuentes_fallidas=out.fuentes_fallidas, en_zona=0,
            error=f"ninguna fuente de shoppings respondió ({motivo})")

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM establecimientos_poi WHERE region_id=:r AND fuente=ANY(:f)"),
                     {"r": input.region_id, "f": fuentes_run})
        absorbidas = [f["_seed_id"] for f in final if f.get("_seed_id") and f.get("_dirty")]
        if absorbidas:
            conn.execute(text("DELETE FROM establecimientos_poi WHERE poi_id::text = ANY(:ids)"),
                         {"ids": absorbidas})
        for h in final:
            if h.get("_seed_id") and not h.get("_dirty"):
                continue        # semilla intacta: no se toca
            conn.execute(text("""
                INSERT INTO establecimientos_poi (poi_id, region_id, fuente, categoria, descripcion, nombre, lat, lng)
                VALUES (:id, :r, :f, 'E', 'SHOPPING', :n, :lat, :lng)
            """), {"id": str(uuid.uuid4()), "r": input.region_id, "f": h["fuente"],
                   "n": h.get("nombre"), "lat": h["lat"], "lng": h["lng"]})

    _tg(f"🛍️ <b>Shoppings</b> ({input.region_id}): {out.en_zona} en zona ({out.por_fuente}).")
    logger.info(f"ShoppingFetcher {input.region_id}: {out.en_zona} en zona {out.por_fuente}")
    return out
