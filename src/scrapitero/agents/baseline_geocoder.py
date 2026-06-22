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
_HEADERS = {"User-Agent": "ScrapiteroResearch/1.0 (+https://github.com/Meter0r0/Scrapitero)"}

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
    país). El CEP es la señal de mayor precisión en Brasil."""
    base = " ".join(x for x in (calle or "", numero or "") if x).strip()
    extra = ", ".join(x for x in (barrio or "", ciudad or "", estado or "", cep or "")
                      if x and x.strip())
    if base and extra:
        return f"{base}, {extra}"
    return base or extra


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
                    conf = {"ROOFTOP": 0.95, "RANGE_INTERPOLATED": 0.8,
                            "GEOMETRIC_CENTER": 0.6, "APPROXIMATE": 0.4}.get(loc_type, 0.5)
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


@agent_run
def run(input: BaselineGeocoderInput) -> BaselineGeocoderOutput:
    engine = get_engine()
    with engine.connect() as conn:
        meta = conn.execute(text(
            "SELECT b.nombre, r.country_code, b.ciudad, r.municipio_codigo, r.zone_geojson "
            "FROM baselines b JOIN regions r ON r.region_id = b.region_id "
            "WHERE b.baseline_id = :bid"),
            {"bid": input.baseline_id}).fetchone()
        if not meta:
            return BaselineGeocoderOutput(ok=False, baseline_id=input.baseline_id,
                                          error="baseline no encontrado")
        nombre, country_code, ciudad, municipio_codigo = meta[0], meta[1], meta[2], meta[3]
        zone_geojson = meta[4]

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

    # ── Paso 0 (Brasil): geocodebr — CNEFE/IBGE, gratis y offline. Resuelve la mayoría
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
                FROM geocode_cache WHERE clave = ANY(:claves)
            """), {"claves": list(claves)}):
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
        if ip.ok and (ip.interpoladas or ip.por_osm):
            logger.info(f"BaselineGeocoder: interpolación repositionó {ip.interpoladas} "
                        f"(+{ip.por_osm} por OSM) direcciones apiladas")
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
