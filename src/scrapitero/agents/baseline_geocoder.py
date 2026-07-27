"""BaselineGeocoder — geocodifica (dirección → coordenada) las direcciones de un
baseline importado (el relevamiento anterior del cliente, CSV externo).

El CSV anterior no trae coordenadas. Para poder **graficar** el relevamiento
anterior en el mapa al crear una *actualización* (y dibujar encima el polígono de
la nueva zona) hace falta ubicarlo. Este agente recorre `baseline_direcciones`
del baseline, geocodifica cada dirección y escribe `lat/lng/geocode_source/
geocode_confidence` (migración 019).

Estrategia (acordada): **Nominatim** forward (gratis, ~1 req/s) primero, **Google
Geocoding** como fallback (preciso, requiere `GOOGLE_MAPS_API_KEY`). IBGE
logradouros NO aplica acá: al crear la región todavía no se bajaron.

Idempotente / resumible: solo toca filas sin `lat`. Throttle + avisos por Telegram
(la audiencia es el operador técnico: progreso con números, error con la causa).
"""

from __future__ import annotations

import os
import re
import time
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents import geo
from scrapitero.agents._run import agent_run
from scrapitero.db.engine import get_engine

_NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
_HEADERS = {"User-Agent": "ScraperGIS/1.0 (+https://github.com/Meter0r0/Scrapitero)"}

# Radio máximo (km) entre una coord geocodificada y el centroide de la zona de la región.
# Generoso para cubrir el municipio + alrededores, pero atrapa los errores de "otro estado"
# (homónimos de calle/ciudad que caen a cientos/miles de km).
_GUARDA_KM = 120.0


def _dist_km(lat1, lng1, lat2, lng2) -> float:
    import math
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _centroide_zona(zone_geojson: Optional[str]) -> Optional[tuple]:
    """(lat, lng) del centroide del polígono de la región, o None si no hay/no parsea."""
    if not zone_geojson:
        return None
    try:
        import json as _json
        from shapely.geometry import shape
        gj = _json.loads(zone_geojson)
        if gj.get("type") == "FeatureCollection":
            from shapely.ops import unary_union
            geom = unary_union([shape(f["geometry"]) for f in gj["features"] if f.get("geometry")])
        else:
            geom = shape(gj.get("geometry", gj))
        c = geom.centroid
        return (c.y, c.x)
    except Exception:  # noqa: BLE001
        return None


class BaselineGeocoderInput(BaseModel):
    baseline_id: str
    delay_ms: int = 1100        # Nominatim pide ~1 req/s — respetarlo
    batch_size: Optional[int] = None   # tope opcional de direcciones por corrida
    usar_cache: bool = True     # False → NO reusar geocode_cache (geocodifica todo de nuevo)
    regeocodificar: bool = False  # True → resetea lat/lng del baseline para reprocesar TODO
    # Paso 0a: el CATASTRO como geocoder (match contra la dirección oficial de la parcela).
    # Es la fuente MÁS precisa y gratis; va antes que todo. Ver catastro_geocoder.py.
    usar_catastro: bool = True
    usar_geocodebr: bool = True   # Brasil: geocodebr (CNEFE, gratis/offline) como paso 0
    geocodebr_max_desvio_m: float = 300.0   # escribir coord de geocodebr solo si desvío ≤ esto
    usar_mapbox: bool = True       # capa paga barata Nominatim→**Mapbox**→Google
    mapbox_max_requests: int = 1000  # tope de llamadas a Mapbox (tramo gratis); el resto cae a Google


class BaselineGeocoderOutput(BaseModel):
    ok: bool
    error: Optional[str] = None
    baseline_id: str = ""
    total: int = 0
    geocodificadas: int = 0
    reusadas: int = 0           # de caché o duplicados (sin llamar a la API)
    fallidas: int = 0
    por_fuente: dict = {}


def _tg(msg: str) -> None:
    """Aviso al operador por Telegram (best-effort, no bloquea)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_HOME_CHANNEL")
               or os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",")[0].strip())
    if not token or not chat_id:
        return
    try:
        httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                   json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                   timeout=5)
    except Exception:
        pass


def _query(calle: str, numero: Optional[str], barrio: Optional[str] = None,
           ciudad: Optional[str] = None, estado: Optional[str] = None,
           cep: Optional[str] = None) -> str:
    """Texto a geocodificar: 'calle número, bairro, ciudad, UF, CEP'. El bairro, la ciudad,
    el estado (UF) y el CEP desambiguan (sin ellos la dirección cae en cualquier parte del
    país). El CEP es la señal de mayor precisión en Brasil.

    **Saneo de la entrada** (medido: sin esto el 25% de las consultas iba sin número y el
    16,5% llevaba el loteamento pegado al nombre de la vía, y el geocoder devolvía el centro
    de la calle — que después cae en una parcela arbitraria):
      - la anotación entre paréntesis del CSV del cliente ("AV DA FEB(RES ALAMEDA)") se
        **saca del nombre de la calle** y se usa como bairro si no vino uno;
      - se ignoran valores basura ('none', 'null', 's/d') en cualquier componente.
    """
    from scrapitero.agents.direccion_norm import limpiar_calle_anotacion
    calle_limpia, anot = limpiar_calle_anotacion(calle)
    barrio = _limpio(barrio) or anot          # el loteamento es un barrio, no parte de la vía
    base = " ".join(x for x in (calle_limpia, _limpio(numero)) if x).strip()
    extra = ", ".join(x for x in (barrio, _limpio(ciudad), _limpio(estado), _limpio(cep)) if x)
    if base and extra:
        return f"{base}, {extra}"
    return base or extra


_BASURA = {"none", "null", "nan", "s/d", "sd", "-", "n/a", "na", "sem", "sin"}

# Piso de confianza para REUSAR una coordenada de `geocode_cache`: 0.8 = Google
# RANGE_INTERPOLATED / Mapbox high. Por debajo de eso la entrada ubica una calle o un barrio,
# no una dirección, y reusarla reintroduce el error que se acaba de eliminar.
_CACHE_CONF_MIN = 0.8
# Fuentes exactas que pueden tener `geocode_confidence` NULL y aun así son válidas
# (geocodebr a nivel de número, y el catastro).
_CACHE_SRC_EXACTAS = ("catastro", "g:numero")


def _limpio(v) -> str:
    """Componente de dirección usable, o '' — filtra los sentinelas que llegan del CSV
    (y el 'None' que produce interpolar un valor nulo en un f-string)."""
    s = str(v or "").strip()
    return "" if s.lower() in _BASURA else s


def _norm_ciudad(ciudad: Optional[str]) -> str:
    import unicodedata
    s = "".join(c for c in unicodedata.normalize("NFKD", str(ciudad or ""))
                if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def _clave(calle: str, numero: Optional[str], iso2: Optional[str],
           barrio: Optional[str] = None, ciudad: Optional[str] = None,
           estado: Optional[str] = None, cep: Optional[str] = None) -> Optional[str]:
    """Clave de caché: dirección normalizada + bairro + ciudad + UF + CEP + país. None si no
    hay calle normalizable. Incluye bairro/ciudad/estado/CEP para no mezclar la misma calle de
    distintos barrios/ciudades/estados."""
    from scrapitero.agents.direccion_norm import normalizar_calle, normalizar_numero
    calle_norm = normalizar_calle(calle or "")
    if not calle_norm:
        return None
    return (f"{iso2 or ''}|{_norm_ciudad(ciudad)}|{_norm_ciudad(barrio)}|"
            f"{(estado or '').strip().upper()}|{re.sub(r'[^0-9]', '', cep or '')}|"
            f"{calle_norm}|{normalizar_numero(numero or '') or ''}")


def _nominatim(query: str, iso2: Optional[str], client: httpx.Client):
    """(lat, lng, confidence) por Nominatim forward, o None."""
    params = {"format": "jsonv2", "q": query, "limit": 1, "addressdetails": 0}
    if iso2:
        params["countrycodes"] = iso2
    try:
        r = client.get(_NOMINATIM_SEARCH_URL, params=params)
        if r.status_code == 200:
            arr = r.json()
            if arr:
                hit = arr[0]
                imp = hit.get("importance")
                conf = float(imp) if isinstance(imp, (int, float)) else 0.5
                return float(hit["lat"]), float(hit["lon"]), conf
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        pass
    return None


def _google(query: str, iso2: Optional[str], client: httpx.Client):
    """(lat, lng, confidence) por Google Geocoding forward, o None."""
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        return None
    params = {"address": query, "key": key}
    if iso2:
        params["region"] = iso2
        params["components"] = f"country:{iso2.upper()}"
    try:
        r = client.get(geo._GOOGLE_GEOCODE_URL, params=params)
        if r.status_code == 200:
            data = r.json()
            for res in data.get("results", []):
                loc = (res.get("geometry") or {}).get("location") or {}
                if "lat" in loc and "lng" in loc:
                    # "ROOFTOP">"RANGE_INTERPOLATED">"GEOMETRIC_CENTER">"APPROXIMATE"
                    loc_type = (res.get("geometry") or {}).get("location_type", "")
                    # Solo se aceptan los tipos que ubican una DIRECCIÓN. `GEOMETRIC_CENTER`
                    # es el centro de la vía y `APPROXIMATE` el del barrio/localidad: no son
                    # la dirección pedida y, guardados como si lo fueran, caen en una parcela
                    # arbitraria (eran el 65% de lo que Google resolvía en VG). Devolver None
                    # los deja sin resolver para que escalen o queden como incidencia, en vez
                    # de contaminar el relevamiento con una ubicación inventada.
                    conf = {"ROOFTOP": 0.95, "RANGE_INTERPOLATED": 0.8}.get(loc_type)
                    if conf is None:
                        logger.debug(f"Google descartado por location_type={loc_type!r}: {query!r}")
                        continue
                    if res.get("partial_match"):
                        conf -= 0.1      # Google avisa que no matcheó la dirección completa
                    return float(loc["lat"]), float(loc["lng"]), conf
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        pass
    return None


_MAPBOX_FWD_URL = "https://api.mapbox.com/search/geocode/v6/forward"


def _mapbox_token() -> Optional[str]:
    return os.environ.get("MAPBOX_TOKEN") or os.environ.get("MAPBOX_ACCESS_TOKEN")


def _mapbox(query: str, iso2: Optional[str], client: httpx.Client):
    """(lat, lng, confidence) por Mapbox Geocoding v6 forward, o None. Capa paga barata."""
    key = _mapbox_token()
    if not key:
        return None
    params = {"q": query, "access_token": key, "limit": 1, "autocomplete": "false"}
    if iso2:
        params["country"] = iso2.lower()
    try:
        r = client.get(_MAPBOX_FWD_URL, params=params)
        if r.status_code == 200:
            feats = (r.json() or {}).get("features") or []
            if feats:
                props = feats[0].get("properties") or {}
                coords = (props.get("coordinates") or
                          (feats[0].get("geometry") or {}).get("coordinates") or {})
                if isinstance(coords, dict):
                    lat, lng = coords.get("latitude"), coords.get("longitude")
                else:  # geometry.coordinates = [lng, lat]
                    lng, lat = (coords + [None, None])[:2]
                # Solo aceptamos resultados a nivel DIRECCIÓN o CALLE. Si Mapbox no encontró
                # la calle cae a 'place'/'locality'/'region' (la CIUDAD) → NO es una ubicación
                # real de la dirección (aterrizaría en el centro de la ciudad) → descartar.
                ftype = props.get("feature_type", "")
                if ftype not in ("address", "street"):
                    return None
                if lat is not None and lng is not None:
                    # match_code.confidence (exact/high/medium/low) → conf; si no, por tipo.
                    mc = (props.get("match_code") or {}).get("confidence")
                    conf = {"exact": 0.95, "high": 0.85, "medium": 0.6, "low": 0.4}.get(
                        mc, {"address": 0.9, "street": 0.6}.get(ftype, 0.5))
                    return float(lat), float(lng), conf
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        pass
    return None


def _geocodebr_step(engine, pendientes: list, municipio_codigo: Optional[str],
                    ciudad_global: Optional[str], max_desvio_m: float) -> set:
    """Paso 0 (Brasil): geocodifica las pendientes con geocodebr (CNEFE, gratis/offline) vía
    el helper compartido `geocode_forward`. Escribe lat/lng de las que ubican con desvío
    aceptable y devuelve el set de ids resueltos, para sacarlos del resto. Si geocodebr no
    está disponible o falla, devuelve set() y el flujo sigue con Nominatim/Google."""
    from scrapitero.agents.geocode_forward import geocodebr_lote, uf_de_municipio_codigo
    uf = uf_de_municipio_codigo(municipio_codigo)
    # estado por fila (COD_UF del CSV) si vino; si no, la UF de la región (municipio_codigo).
    items = [{"id": pid, "logradouro": calle or "", "numero": numero or "",
              "bairro": barrio or "", "municipio": (ciu or ciudad_global or ""),
              "estado": (est or uf), "cep": cep or ""}
             for pid, calle, numero, barrio, ciu, est, cep in pendientes]
    res = geocodebr_lote(items, uf=uf, max_desvio_m=max_desvio_m)
    ids_ok: set = set()
    with engine.begin() as conn:
        for pid, (lat, lng, src) in res.items():
            conn.execute(text("""
                UPDATE baseline_direcciones
                SET lat=:lat, lng=:lng, geocode_source=:src, geocode_confidence=NULL
                WHERE id=:id
            """), {"lat": lat, "lng": lng, "src": src, "id": pid})
            ids_ok.add(pid)
    return ids_ok


def _catastro_step(engine, pendientes: list, region_id: Optional[str],
                   municipio_codigo: Optional[str], ciudad: Optional[str] = None) -> set:
    """Paso 0 — el CATASTRO como geocoder (lo más preciso y gratis que hay).

    En vez de dirección → API → coordenada → ¿qué parcela?, matchea la dirección contra la
    dirección oficial de la parcela (BCI) y usa el centroide de ESA parcela. Medido en VG:
    baja el error de "cae en parcela de otra calle" de **19,3% → 2,3%** (0,4% en el match
    exacto), y resuelve el 91% de las direcciones sin pegarle a ninguna API.

    Devuelve el set de ids resueltos. Si no hay catastro cargado para esa ciudad, devuelve
    set() y el flujo sigue con geocodebr/Nominatim/Mapbox/Google como antes."""
    from scrapitero.agents.catastro_geocoder import CatastroIndex
    try:
        idx = CatastroIndex(region_id=region_id, municipio_codigo=municipio_codigo,
                            ciudad=ciudad)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"CatastroIndex no disponible ({exc!r}) — se sigue con las APIs")
        return set()
    if not idx.por_calle:
        return set()

    ids_ok: set = set()
    with engine.begin() as conn:
        for pid, calle, numero, _barrio, _ciu, _est, _cep in pendientes:
            hit = idx.buscar(calle, numero)
            if not hit:
                continue
            conn.execute(text("""
                UPDATE baseline_direcciones
                SET lat=:lat, lng=:lng, geocode_source=:src, geocode_confidence=:conf
                WHERE id=:id
            """), {"lat": hit["lat"], "lng": hit["lng"], "src": hit["fuente"],
                   "conf": hit["confidence"], "id": pid})
            ids_ok.add(pid)
    return ids_ok


@agent_run
def run(input: BaselineGeocoderInput) -> BaselineGeocoderOutput:
    engine = get_engine()
    with engine.connect() as conn:
        meta = conn.execute(text(
            "SELECT b.nombre, r.country_code, b.ciudad, r.municipio_codigo, r.zone_geojson, "
            "       r.region_id "
            "FROM baselines b JOIN regions r ON r.region_id = b.region_id "
            "WHERE b.baseline_id = :bid"),
            {"bid": input.baseline_id}).fetchone()
        if not meta:
            return BaselineGeocoderOutput(ok=False, baseline_id=input.baseline_id,
                                          error="baseline no encontrado")
        nombre, country_code, ciudad, municipio_codigo = meta[0], meta[1], meta[2], meta[3]
        zone_geojson, region_id = meta[4], meta[5]

    # Guarda anti "otro estado": centroide de la zona de la región. Cualquier coordenada
    # geocodificada a más de `_GUARDA_KM` del centroide se descarta (típico cuando una calle
    # tiene homónimos en otra ciudad/estado — p.ej. "Várzea Grande" existe en MT y en PI, y
    # sin UF Nominatim/Google la ubican en el estado equivocado). Si no hay zona, no se aplica.
    centro = _centroide_zona(zone_geojson)
    def _en_rango(lat, lng) -> bool:
        if not centro or lat is None or lng is None:
            return True
        return _dist_km(centro[0], centro[1], lat, lng) <= _GUARDA_KM

    # Re-geocodificar: borrar las coordenadas previas para que se reprocese TODO el
    # baseline (el geocoder solo toca filas con lat NULL).
    if input.regeocodificar:
        with engine.begin() as conn:
            reset = conn.execute(text("""
                UPDATE baseline_direcciones
                SET lat = NULL, lng = NULL, geocode_source = NULL, geocode_confidence = NULL
                WHERE baseline_id = :bid
            """), {"bid": input.baseline_id}).rowcount
        logger.info(f"BaselineGeocoder: regeocodificar=on → {reset} direcciones reseteadas")

    with engine.connect() as conn:
        pendientes = conn.execute(text("""
            SELECT id::text, calle, numero, barrio, ciudad, estado, cep FROM baseline_direcciones
            WHERE baseline_id = :bid AND lat IS NULL AND calle IS NOT NULL
            ORDER BY calle, numero
        """), {"bid": input.baseline_id}).fetchall()

    iso2 = geo.country_iso2(country_code)
    total = len(pendientes)
    if input.batch_size:
        pendientes = pendientes[:input.batch_size]

    geocodificadas = fallidas = reusadas = 0
    por_fuente: dict = {}

    # ── Paso 0a: CATASTRO — la fuente más precisa (y gratis). Matchea la dirección contra la
    # dirección oficial de la parcela y usa SU centroide, en vez de geocodificar y después ver
    # dónde cayó el punto. Va PRIMERO porque su match exacto es, por construcción, la parcela
    # correcta (medido en VG: 0,4% de error vs 12-36% de las APIs).
    if input.usar_catastro and pendientes:
        ids_cat = _catastro_step(engine, pendientes, region_id, municipio_codigo, ciudad)
        if ids_cat:
            geocodificadas += len(ids_cat)
            por_fuente["catastro"] = len(ids_cat)
            pendientes = [t for t in pendientes if t[0] not in ids_cat]
            logger.info(f"catastro paso 0a: {len(ids_cat)} ubicadas por dirección catastral "
                        f"(exacto/interpolado), {len(pendientes)} restantes")
            _tg(f"📍 <b>Catastro</b> (gratis, el más preciso): {len(ids_cat)} ubicadas, "
                f"{len(pendientes)} siguen a geocodebr/APIs.")

    # ── Paso 0b (Brasil): geocodebr — CNEFE/IBGE, gratis y offline. Resuelve la mayoría
    # sin pegarle a Nominatim/Google; lo que no ubique con desvío aceptable cae al resto.
    if iso2 == "br" and input.usar_geocodebr and pendientes:
        ids_ok = _geocodebr_step(engine, pendientes, municipio_codigo, ciudad,
                                 input.geocodebr_max_desvio_m)
        if ids_ok:
            geocodificadas += len(ids_ok)
            por_fuente["geocodebr"] = len(ids_ok)
            pendientes = [t for t in pendientes if t[0] not in ids_ok]
            logger.info(f"geocodebr paso 0: {len(ids_ok)} ubicadas gratis (CNEFE), "
                        f"{len(pendientes)} restantes → Nominatim/Google")
            _tg(f"📍 geocodebr (gratis): {len(ids_ok)} ubicadas, "
                f"{len(pendientes)} van a Nominatim/Google.")

    # Caché de geocoding (migración 021): reusa coordenadas ya resueltas por
    # dirección normalizada + país, sin volver a pegarle a Nominatim/Google.
    # Con usar_cache=False se omite la precarga → se geocodifica todo de nuevo
    # (el dict `cache` sigue sirviendo de dedup dentro de ESTE run).
    claves = {c for c in (_clave(calle, numero, iso2, barrio, ciu or ciudad, est, cep)
                          for _, calle, numero, barrio, ciu, est, cep in pendientes) if c}
    cache: dict[str, tuple] = {}
    if claves and input.usar_cache:
        with engine.connect() as conn:
            for row in conn.execute(text("""
                SELECT clave, lat, lng, geocode_source, geocode_confidence
                FROM geocode_cache
                WHERE clave = ANY(:claves)
                  -- Solo se reusa lo que ubica una DIRECCIÓN. La caché acumuló coordenadas
                  -- de la estrategia anterior (en VG: 81% por debajo de este umbral — 949 de
                  -- Nominatim con conf 0,05 y 412 de Google que son GEOMETRIC_CENTER /
                  -- APPROXIMATE, hoy descartados en `_google`). Sin este filtro la caché
                  -- volvería a servir justo las coordenadas que estamos dejando de aceptar.
                  AND (geocode_confidence >= :conf_min
                       OR geocode_source = ANY(:src_exactas))
            """), {"claves": list(claves), "conf_min": _CACHE_CONF_MIN,
                   "src_exactas": list(_CACHE_SRC_EXACTAS)}):
                cache[row[0]] = (row[1], row[2], row[3], row[4])

    _tg(f"📍 <b>Geocodificando «{nombre}»</b>\n{len(pendientes)} direcciones del "
        f"relevamiento anterior (geocodebr + caché + Nominatim + Mapbox + Google)…")

    fallidas_claves: set = set()        # claves que ya fallaron en este run → no re-pegar
    delay = max(input.delay_ms, 0) / 1000.0

    # Mapbox: capa paga barata entre Nominatim y Google, con tope para no pasar el tramo gratis.
    mapbox_on = input.usar_mapbox and bool(_mapbox_token())
    mapbox_reqs = 0
    mapbox_aviso = False
    if input.usar_mapbox and not _mapbox_token():
        logger.info("BaselineGeocoder: usar_mapbox=True pero falta MAPBOX_TOKEN — se omite Mapbox")

    def _guardar(dir_id, lat, lng, src, conf):
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE baseline_direcciones
                SET lat = :lat, lng = :lng, geocode_source = :src, geocode_confidence = :conf
                WHERE id = :id
            """), {"lat": lat, "lng": lng, "src": src, "conf": conf, "id": dir_id})

    with httpx.Client(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
        for i, (dir_id, calle, numero, barrio, ciu_fila, est, cep) in enumerate(pendientes):
            ciu = ciu_fila or ciudad        # ciudad de la fila, o la global del baseline
            q = _query(calle, numero, barrio, ciu, est, cep)
            if not q:
                fallidas += 1
                continue
            clave = _clave(calle, numero, iso2, barrio, ciu, est, cep)

            # 1) Caché (DB o resuelto antes en este mismo run) → sin API. Se ignora la
            # entrada cacheada si cae fuera de la zona (coord vieja mala, p.ej. otro estado).
            if clave and clave in cache:
                lat, lng, src, conf = cache[clave]
                if _en_rango(lat, lng):
                    _guardar(dir_id, lat, lng, src, conf)
                    geocodificadas += 1
                    reusadas += 1
                    por_fuente["cache"] = por_fuente.get("cache", 0) + 1
                    continue
            # Dirección idéntica que ya falló en este run → no re-pegar a la API
            if clave and clave in fallidas_claves:
                fallidas += 1
                continue

            # 2) Geocoding real (Nominatim gratis → Mapbox pago barato → Google fallback).
            # Se DESCARTA cualquier hit fuera del rango de la zona (homónimos en otro
            # estado) y se sigue con la fuente siguiente.
            hit = None
            fuente = None
            descartado = False   # alguna fuente devolvió algo, pero caía lejos de la zona
            h = _nominatim(q, iso2, client)
            if h:
                if _en_rango(h[0], h[1]): hit, fuente = h, "nominatim"
                else: descartado = True
            if not hit and mapbox_on:
                if mapbox_reqs < input.mapbox_max_requests:
                    mapbox_reqs += 1
                    h = _mapbox(q, iso2, client)
                    if h:
                        if _en_rango(h[0], h[1]): hit, fuente = h, "mapbox"
                        else: descartado = True
                elif not mapbox_aviso:
                    mapbox_aviso = True
                    _tg(f"💳 «{nombre}»: Mapbox alcanzó el tope de "
                        f"{input.mapbox_max_requests} consultas — el resto cae a Google (pago).")
                    logger.info(f"BaselineGeocoder: tope Mapbox ({input.mapbox_max_requests}) "
                                "alcanzado — resto a Google")
            if not hit:
                h = _google(q, iso2, client)
                if h:
                    if _en_rango(h[0], h[1]): hit, fuente = h, "google"
                    else: descartado = True
            if not hit and descartado:
                logger.info(f"BaselineGeocoder: «{q}» descartada — geocode fuera de la zona "
                            f"(>{_GUARDA_KM:.0f} km del centro; probable homónimo en otro estado)")
            if hit:
                lat, lng, conf = hit
                _guardar(dir_id, lat, lng, fuente, conf)
                geocodificadas += 1
                por_fuente[fuente] = por_fuente.get(fuente, 0) + 1
                if clave:
                    cache[clave] = (lat, lng, fuente, conf)
                    with engine.begin() as conn:
                        conn.execute(text("""
                            INSERT INTO geocode_cache
                                (clave, query, lat, lng, geocode_source, geocode_confidence)
                            VALUES (:clave, :q, :lat, :lng, :src, :conf)
                            ON CONFLICT (clave) DO NOTHING
                        """), {"clave": clave, "q": q, "lat": lat, "lng": lng,
                               "src": fuente, "conf": conf})
            else:
                fallidas += 1
                if clave:
                    fallidas_claves.add(clave)

            if (i + 1) % 50 == 0:
                logger.info(f"Geocoding «{nombre}»: {i + 1}/{len(pendientes)} "
                            f"({geocodificadas} ubicadas, {reusadas} de caché, {fallidas} fallidas)")
                _tg(f"📍 «{nombre}»: {i + 1}/{len(pendientes)} "
                    f"({geocodificadas} ubicadas, {reusadas} reusadas)")
            time.sleep(delay)

    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE baselines
            SET geocoded_at = now(),
                n_geocodificadas = (SELECT COUNT(*) FROM baseline_direcciones
                                    WHERE baseline_id = :bid AND lat IS NOT NULL)
            WHERE baseline_id = :bid
        """), {"bid": input.baseline_id})

    # Pase final: reposicionar por interpolación las direcciones apiladas en un punto
    # (geocodebr devuelve un único punto aproximado para números que no están en el CNEFE).
    # Gratis (anclas exactas de la misma calle) + fallback a geometría OSM. Best-effort.
    try:
        from scrapitero.agents.baseline_interp import run as _interp_run, BaselineInterpInput
        ip = _interp_run(BaselineInterpInput(baseline_id=input.baseline_id))
        # (el output no tiene `interpoladas`: los contadores son por_mapbox/por_osm/por_ciudad)
        if ip.ok and (ip.por_mapbox or ip.por_osm or ip.por_ciudad):
            logger.info(f"BaselineGeocoder: interpolación ubicó {ip.por_mapbox} por Mapbox, "
                        f"{ip.por_osm} por eje OSM, {ip.por_ciudad} al centro de la ciudad")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"BaselineGeocoder: pase de interpolación falló (no crítico): {e}")

    _tg(f"✅ <b>«{nombre}» geocodificado</b>\n{geocodificadas} ubicadas "
        f"({reusadas} reusadas sin costo), {fallidas} sin ubicar (de {len(pendientes)}).")
    logger.info(f"Geocoding baseline «{nombre}» listo: {geocodificadas} ubicadas "
                f"({reusadas} de caché/duplicados), {fallidas} fallidas, fuentes={por_fuente}")
    return BaselineGeocoderOutput(
        ok=True, baseline_id=input.baseline_id, total=total,
        geocodificadas=geocodificadas, reusadas=reusadas, fallidas=fallidas,
        por_fuente=por_fuente)
