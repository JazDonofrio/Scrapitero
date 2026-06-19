"""HotelFetcher — hoteles del relevamiento (multi-fuente).

Lo que Google solo no da: **cantidad de habitaciones** (UHs) y si el hotel **cerró
definitivamente**. Fuentes:

  - **Cadastur** "Meios de Hospedagem" (Ministério do Turismo, Brasil): oficial, trae
    UHs/leitos/CNPJ/tipo/situação. Vía API CKAN (recurso resuelto en runtime). El portal
    a veces está caído (502) → no rompe: se sigue con las otras fuentes.
  - **Receita** (universo CNPJ de hospedagem, tabla `receita_estabelecimentos_hospedagem`):
    cobertura mucho mayor que Cadastur + **situação cadastral** (abierto/cerrado gratis). No
    trae habitaciones. Mergea por CNPJ con Cadastur. Coordenadas geocodificadas gratis por
    `GeocodebrFetcher` (CNEFE/IBGE); las que faltan caen al geocoding de abajo (nominatim/Google).
  - **OSM** (`tourism=hotel|motel|hostel|guest_house|apartment`): gratis y siempre arriba;
    trae `rooms`/`stars` cuando están tageados. Cubre cuando Cadastur no responde.

Para cada hotel: geocoding si no trae coordenadas (Cadastur) → recorte a la zona →
vinculación a parcela (`ST_Contains`) → enriquecer abierto/cerrado con el `business_status`
de Google (de `comercios`). Para hoteles **abiertos**, sus habitaciones (UHs) cuentan como
`uf_comercio` de la parcela. Escribe la tabla `hoteles` (migración 025).
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import time
import unicodedata
import uuid
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.baseline_geocoder import _HEADERS, _google, _mapbox, _nominatim, _tg
from scrapitero.db.engine import get_engine

_CKAN_PACKAGE = "https://dados.turismo.gov.br/api/3/action/package_show?id=meios-de-hospedagem"
_IBGE_MUNICIPIO = "https://servicodados.ibge.gov.br/api/v1/localidades/municipios/{cod}"
_OSM_TOURISM = "hotel|motel|hostel|guest_house|apartment|resort"


class HotelFetcherInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None
    fuentes: list[str] = ["cadastur", "receita", "osm", "google"]   # orden de fuentes a consultar
    set_uf: bool = True
    delay_ms: int = 1100
    max_hoteles: Optional[int] = None
    # Estimación de habitaciones por área del BCI cuando no hay dato exacto (Cadastur/OSM):
    # habitaciones ≈ area_m2_construida / m2_por_habitacion. Es proxy, NO exacto.
    # DESACTIVADA por default (2026-06-17): solo se muestran habitaciones de fuente exacta
    # (Cadastur UHs / OSM rooms); sin dato → "s/d". Poner True para reactivar el proxy.
    estimar_habitaciones: bool = False
    m2_por_habitacion: float = 35.0
    # Cordura del proxy: NO estimar si el área de la parcela es desproporcionada para un
    # solo hotel (el punto cayó en una parcela enorme = manzana/predio, no el hotel), y
    # capar el resultado. Evita disparates tipo 2248 habitaciones.
    proxy_area_max_m2: float = 20000.0
    proxy_hab_max: int = 400
    # Dedupe cross-fuente sin CNPJ (Google no trae): unir por nombre similar + proximidad.
    merge_dist_m: float = 200.0
    # Buffer al recorte de zona: incluye hoteles pegados al límite (geocoding ±metros)
    # que de otro modo caerían justo afuera del polígono.
    borde_buffer_m: float = 40.0
    # geocodebr (CNEFE/IBGE, gratis) es la PRIMERA opción para geocodificar hoteles sin
    # coordenadas (Cadastur/Receita); escribe coord solo si el desvío es ≤ esto, el resto
    # cae a Nominatim/Google. Ver `geocode_forward`.
    geocodebr_max_desvio_m: float = 300.0


class HotelFetcherOutput(BaseModel):
    ok: bool
    error: Optional[str] = None
    region_id: str = ""
    municipio: str = ""
    por_fuente: dict = {}            # fuente → cantidad cruda hallada
    fuentes_fallidas: dict = {}      # fuente → motivo (p.ej. Cadastur 502)
    hoteles_en_zona: int = 0
    vinculados_parcela: int = 0
    cerrados: int = 0
    parcelas_con_hotel: int = 0
    total_uf_comercio: int = 0
    sin_habitaciones: int = 0        # abiertos sin habitaciones → asistencia humana


def _norm(s: Optional[str]) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", str(s or ""))
                if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def _entero(s) -> Optional[int]:
    m = re.search(r"\d+", str(s or ""))
    return int(m.group(0)) if m else None


def _dist_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Distancia haversine en metros entre dos puntos (lat/lng)."""
    from math import asin, cos, radians, sin, sqrt
    dlat, dlng = radians(lat2 - lat1), radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * 6371000.0 * asin(sqrt(a))


def _nombre_similar(a: Optional[str], b: Optional[str]) -> bool:
    """¿Son el mismo nombre de hotel? Igual normalizado, o uno contenido en el otro por
    tokens (p.ej. 'fly hotel' ⊆ 'fly hotel mt'), o ratio difflib ≥ 0.82."""
    import difflib
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    if ta and tb and (ta <= tb or tb <= ta):
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.82


def _mismo_hotel(a: dict, b: dict, max_dist_m: float) -> bool:
    """Dos registros = el mismo hotel. Mismo CNPJ (cuando ambos lo tienen) ⇒ sí. Si no
    (Google no trae CNPJ), nombre similar + a menos de `max_dist_m` metros."""
    if a.get("cnpj") and b.get("cnpj"):
        return a["cnpj"] == b["cnpj"]
    if a.get("lat") is None or b.get("lat") is None:
        return False
    return (_nombre_similar(a.get("nombre"), b.get("nombre"))
            and _dist_m(a["lat"], a["lng"], b["lat"], b["lng"]) <= max_dist_m)


def _municipio_nombre_uf(cod: str, client: httpx.Client) -> tuple[Optional[str], Optional[str]]:
    try:
        r = client.get(_IBGE_MUNICIPIO.format(cod=cod))
        if r.status_code == 200:
            d = r.json()
            uf = (((d.get("microrregiao") or {}).get("mesorregiao") or {})
                  .get("UF") or {}).get("sigla")
            return d.get("nome"), uf
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        pass
    return None, None


# ── Fuente: Cadastur (oficial Brasil; UHs/leitos/CNPJ/situação) ────────────────

def _resolver_csv_url(client: httpx.Client) -> Optional[str]:
    r = client.get(_CKAN_PACKAGE)
    if r.status_code != 200:
        return None
    resources = ((r.json().get("result") or {}).get("resources") or [])
    csvs = [x for x in resources if "csv" in (x.get("format") or "").lower()]
    csvs.sort(key=lambda x: x.get("last_modified") or x.get("created") or "", reverse=True)
    elegidos = csvs or resources
    return elegidos[0].get("url") if elegidos else None


def _col(headers_norm: dict, *claves: str) -> Optional[int]:
    for h, i in headers_norm.items():
        if any(k in h for k in claves):
            return i
    return None


def _fetch_cadastur(mun_nombre: str, uf: str, client: httpx.Client) -> list[dict]:
    """Hoteles de Cadastur del município (sin coordenadas; se geocodifican luego).
    Lanza RuntimeError si el portal no responde / no hay recurso."""
    url = _resolver_csv_url(client)
    if not url:
        raise RuntimeError("portal CKAN sin responder o sin recurso CSV (¿caído?)")
    r = client.get(url)
    if r.status_code != 200:
        raise RuntimeError(f"descarga del CSV HTTP {r.status_code}")
    try:
        texto = r.content.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = r.content.decode("latin-1")
    primera = texto.splitlines()[0] if texto.splitlines() else ""
    sep = ";" if primera.count(";") >= primera.count(",") else ","
    filas = list(csv.reader(io.StringIO(texto), delimiter=sep))
    if not filas:
        raise RuntimeError("CSV vacío")
    headers = [h.strip() for h in filas[0]]
    hn = {_norm(h): i for i, h in enumerate(headers)}
    c = {k: _col(hn, *v) for k, v in {
        "nombre": ("nome fantasia", "nome", "razao"), "cnpj": ("cnpj",),
        "mun": ("municipio",), "uf": ("uf", "estado"),
        "uh": ("unidades habitacionais", "uh", "quantidade unidades"),
        "leitos": ("leitos",), "tipo": ("atividade", "tipo", "categoria"),
        "sit": ("situacao", "situa"), "logr": ("logradouro", "endereco"),
        "num": ("numero",), "bairro": ("bairro",),
    }.items()}

    def cell(fila, k):
        i = c.get(k)
        return fila[i].strip() if (i is not None and i < len(fila)) else ""

    mun_norm = _norm(mun_nombre)
    hoteles = []
    for fila in filas[1:]:
        if c["mun"] is not None and mun_norm and _norm(cell(fila, "mun")) != mun_norm:
            continue
        if c["uf"] is not None and uf and _norm(cell(fila, "uf")) not in (_norm(uf), ""):
            continue
        sit = cell(fila, "sit")
        hoteles.append({
            "nombre": cell(fila, "nombre") or None,
            "cnpj": (re.sub(r"\D", "", cell(fila, "cnpj"))[:20] or None),
            "tipo": (cell(fila, "tipo")[:60] or None),
            "direccion": " ".join(x for x in (cell(fila, "logr"), cell(fila, "num"),
                                              cell(fila, "bairro")) if x) or None,
            # campos estructurados para geocodebr (geocoding gratis primero)
            "logradouro": cell(fila, "logr") or None, "numero": cell(fila, "num") or None,
            "bairro": cell(fila, "bairro") or None,
            "lat": None, "lng": None,
            "uh": _entero(cell(fila, "uh")), "leitos": _entero(cell(fila, "leitos")),
            "estrellas": None, "situacion": (sit[:40] or None),
            "cerrado": bool(sit) and _norm(sit) in ("inativo", "cancelado", "baixado"),
            "business_status": None,
            "hab_fuente": "cadastur" if _entero(cell(fila, "uh")) else None,
            "fuente": "cadastur",
        })
    return hoteles


# ── Fuente: Receita (CNPJ; universo + situação cadastral; sin habitaciones) ────

_CNAE_TIPO = {"5510801": "hotel", "5510802": "apart-hotel", "5510803": "motel",
              "5590601": "albergue", "5590602": "camping", "5590603": "pensao",
              "5590699": "outros"}


def _fetch_receita(engine, mun_nombre: Optional[str], uf: Optional[str]) -> list[dict]:
    """Hoteles del universo CNPJ de Receita para el município (tabla
    `receita_estabelecimentos_hospedagem`, que pobló ReceitaCNPJFetcher). Las coordenadas
    salen ya geocodificadas por GeocodebrFetcher (las que faltan, lat=None, las geocodifica
    el flujo de HotelFetcher con nominatim/Google). `situação` da abierto/cerrado gratis.
    El match por município se hace normalizado (Receita guarda sin acento/mayúsculas)."""
    if not (mun_nombre and uf):
        return []
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT cnpj, nome_fantasia, razao_social, cnae_principal, tipo_logradouro,
                   logradouro, numero, bairro, municipio_nome, situacao, lat, lng, telefone
            FROM receita_estabelecimentos_hospedagem WHERE uf = :uf
        """), {"uf": uf}).fetchall()
    mn = _norm(mun_nombre)
    hoteles = []
    for r in rows:
        if _norm(r.municipio_nome) != mn:
            continue
        sit = r.situacao or ""
        logr = " ".join(y for y in (r.tipo_logradouro, r.logradouro) if y)
        hoteles.append({
            "nombre": r.nome_fantasia or r.razao_social or None,
            "cnpj": (re.sub(r"\D", "", r.cnpj)[:20] or None),
            "tipo": _CNAE_TIPO.get(r.cnae_principal, "hospedagem"),
            "direccion": " ".join(x for x in (logr, r.numero, r.bairro) if x) or None,
            # campos estructurados para geocodebr (geocoding gratis primero)
            "logradouro": logr or None, "numero": r.numero or None, "bairro": r.bairro or None,
            "lat": float(r.lat) if r.lat is not None else None,
            "lng": float(r.lng) if r.lng is not None else None,
            "uh": None, "leitos": None, "estrellas": None,
            "situacion": (sit[:40] or None),
            # cerrado definitivo: situação BAIXADA (deu baixa) o NULA. INAPTA/SUSPENSA no
            # son cierre definitivo (quedan abiertas pero con la situação visible).
            "cerrado": _norm(sit) in ("baixada", "nula"),
            "business_status": None, "hab_fuente": None, "fuente": "receita",
            "telefono": r.telefone or None,
        })
    return hoteles


# ── Fuente: OSM (tourism=hotel…); gratis, siempre arriba, trae rooms/stars ─────

def _fetch_osm(south: float, west: float, north: float, east: float) -> list[dict]:
    from scrapitero.agents.osm_building_fetcher import _fetch_overpass
    q = (f"[out:json][timeout:90];(" +
         "".join(f'{t}["tourism"~"^({_OSM_TOURISM})$"]({south},{west},{north},{east});'
                 for t in ("node", "way", "relation")) +
         ");out center tags;")
    data = _fetch_overpass(q)
    hoteles = []
    for el in data.get("elements", []):
        t = el.get("tags", {}) or {}
        if el.get("type") == "node":
            lat, lng = el.get("lat"), el.get("lon")
        else:
            ctr = el.get("center") or {}
            lat, lng = ctr.get("lat"), ctr.get("lon")
        if lat is None or lng is None:
            continue
        hoteles.append({
            "nombre": t.get("name"), "cnpj": None, "tipo": t.get("tourism"),
            "direccion": " ".join(x for x in (t.get("addr:street"),
                                              t.get("addr:housenumber")) if x) or None,
            "lat": float(lat), "lng": float(lng),
            "uh": _entero(t.get("rooms")), "leitos": _entero(t.get("beds")),
            "estrellas": _entero(t.get("stars")), "situacion": None, "cerrado": False,
            "business_status": None,
            "hab_fuente": "osm" if _entero(t.get("rooms")) else None, "fuente": "osm",
        })
    return hoteles


# ── Fuente: Google Places (lodging en el polígono; ubicación + abierto/cerrado) ─

def _fetch_google(region_id: str, survey_id: Optional[str]) -> list[dict]:
    """Hoteles de Google Places (tipo `lodging`) dentro del polígono de la zona.
    Trae ubicación + `businessStatus` (abierto/CLOSED_PERMANENTLY). No da habitaciones.
    Reusa la búsqueda por teselas adaptativas de GooglePlacesFetcher. Pago (~USD 0,032/req)."""
    import os

    from scrapitero.agents import google_places_fetcher as gp
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        raise RuntimeError("GOOGLE_MAPS_API_KEY no configurada")
    zone_bbox, zone_poly = gp._load_zone(region_id, survey_id)
    if not zone_bbox:
        raise RuntimeError("región sin zona (zone_geojson) para buscar")
    lang = gp._detect_language(region_id)
    with httpx.Client(timeout=30, headers=_HEADERS) as client:
        places, *_ = gp._collect_places(
            client, api_key, zone_bbox, zone_poly,
            cell_size_m=350, min_cell_m=120, max_requests=80,
            included_types=["lodging"], language=lang)
    _LODGING = ("hotel", "lodging", "motel", "resort", "hostel", "guest",
                "inn", "bed_and_breakfast", "pousada", "cottage")
    hoteles = []
    for pl in places.values():
        loc = pl.get("location") or {}
        lat, lng = loc.get("latitude"), loc.get("longitude")
        if lat is None or lng is None:
            continue
        # Filtrar falsos positivos: Google devuelve algunos con 'lodging' entre sus
        # types (p.ej. car_rental) pero cuyo primaryType NO es de hospedaje.
        prim = (pl.get("primaryType") or "").lower()
        if prim and not any(k in prim for k in _LODGING):
            continue
        if not prim and not any(k in " ".join(pl.get("types") or []).lower() for k in _LODGING):
            continue
        bs = pl.get("businessStatus")
        hoteles.append({
            "nombre": (pl.get("displayName") or {}).get("text"),
            "cnpj": None, "tipo": pl.get("primaryType") or "lodging",
            "direccion": pl.get("formattedAddress"), "lat": float(lat), "lng": float(lng),
            "uh": None, "leitos": None, "estrellas": None, "situacion": None,
            "cerrado": (bs == "CLOSED_PERMANENTLY"), "business_status": bs,
            "hab_fuente": None, "fuente": "google",
        })
    return hoteles


def _geocode(query: str, client: httpx.Client) -> Optional[tuple]:
    # Nominatim (gratis) → Mapbox (pago barato, si hay MAPBOX_TOKEN) → Google (pago caro).
    return (_nominatim(query, "br", client) or _mapbox(query, "br", client)
            or _google(query, "br", client))


@agent_run
def run(input: HotelFetcherInput) -> HotelFetcherOutput:
    engine = get_engine()
    out = HotelFetcherOutput(ok=True, region_id=input.region_id)

    with engine.connect() as conn:
        reg = conn.execute(text(
            "SELECT municipio_codigo, country_code, "
            "COALESCE((SELECT subzona_geojson FROM surveys WHERE survey_id::text=:sid), zone_geojson) "
            "FROM regions WHERE region_id=:r"),
            {"r": input.region_id, "sid": input.survey_id}).fetchone()
    if not reg:
        return HotelFetcherOutput(ok=False, region_id=input.region_id, error="región no encontrada")
    municipio_cod, country, zona_gj = reg

    # Polígono + bbox de la zona
    zona_poly = None
    if zona_gj:
        try:
            from shapely.geometry import shape
            from shapely.ops import unary_union
            gj = json.loads(zona_gj)
            geoms = ([shape(f["geometry"]) for f in gj.get("features", []) if f.get("geometry")]
                     if gj.get("type") == "FeatureCollection" else [shape(gj.get("geometry", gj))])
            zona_poly = unary_union(geoms).buffer(0)
            if input.borde_buffer_m:        # tolerancia de borde (grados ≈ m/111000)
                zona_poly = zona_poly.buffer(input.borde_buffer_m / 111000.0)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"HotelFetcher: zona ilegible: {e}")
    if zona_poly is None:
        return HotelFetcherOutput(ok=False, region_id=input.region_id,
                                  error="la región no tiene zona (zone_geojson) para acotar la búsqueda")

    crudos: list[dict] = []
    with httpx.Client(timeout=30, headers=_HEADERS, follow_redirects=True) as client:
        mun_nombre, uf = (None, None)
        if municipio_cod:
            mun_nombre, uf = _municipio_nombre_uf(municipio_cod, client)
        out.municipio = f"{mun_nombre or municipio_cod or '—'}/{uf or ''}"
        _tg(f"🏨 <b>Buscando hoteles</b> en {out.municipio} ({', '.join(input.fuentes)})…")

        # Cadastur (solo Brasil + con município)
        if "cadastur" in input.fuentes:
            if (country or "").upper() == "BRA" and municipio_cod and mun_nombre:
                try:
                    hc = _fetch_cadastur(mun_nombre, uf, client)
                    out.por_fuente["cadastur"] = len(hc)
                    crudos.extend(hc)
                except (httpx.HTTPError, RuntimeError) as e:
                    out.fuentes_fallidas["cadastur"] = str(e)
                    logger.warning(f"HotelFetcher: Cadastur no disponible: {e}")
            else:
                out.fuentes_fallidas["cadastur"] = "solo Brasil con municipio_codigo IBGE"

        # Receita (universo CNPJ + situação cadastral; solo Brasil + con município)
        if "receita" in input.fuentes:
            if (country or "").upper() == "BRA" and municipio_cod and mun_nombre:
                try:
                    hr = _fetch_receita(engine, mun_nombre, uf)
                    out.por_fuente["receita"] = len(hr)
                    crudos.extend(hr)
                except Exception as e:  # noqa: BLE001
                    out.fuentes_fallidas["receita"] = str(e)
                    logger.warning(f"HotelFetcher: Receita falló: {e}")
            else:
                out.fuentes_fallidas["receita"] = "solo Brasil con municipio_codigo IBGE"

        # OSM (siempre)
        if "osm" in input.fuentes:
            try:
                minx, miny, maxx, maxy = zona_poly.bounds
                ho = _fetch_osm(miny, minx, maxy, maxx)
                out.por_fuente["osm"] = len(ho)
                crudos.extend(ho)
            except Exception as e:  # noqa: BLE001
                out.fuentes_fallidas["osm"] = str(e)
                logger.warning(f"HotelFetcher: OSM falló: {e}")

        # Google Places (lodging en el polígono): ubicación + abierto/cerrado (pago)
        if "google" in input.fuentes:
            try:
                hg = _fetch_google(input.region_id, input.survey_id)
                out.por_fuente["google"] = len(hg)
                crudos.extend(hg)
            except Exception as e:  # noqa: BLE001
                out.fuentes_fallidas["google"] = str(e)
                logger.warning(f"HotelFetcher: Google falló: {e}")

        if not crudos:
            motivo = "; ".join(f"{k}: {v}" for k, v in out.fuentes_fallidas.items()) or "0 hoteles"
            if out.fuentes_fallidas and not out.por_fuente:
                return HotelFetcherOutput(ok=False, region_id=input.region_id, municipio=out.municipio,
                                          fuentes_fallidas=out.fuentes_fallidas,
                                          error=f"ninguna fuente devolvió hoteles ({motivo})")
            _tg(f"🏨 {out.municipio}: 0 hoteles.")
            return out

        # Geocodificar los que no traen coordenadas (Cadastur/Receita) + recortar a zona.
        # PRIMERA opción: geocodebr (CNEFE/IBGE, gratis) en LOTE para todos los sin-coords
        # de Brasil; lo que no ubique con desvío aceptable cae al loop Nominatim/Google.
        from shapely.geometry import Point
        sin_coords = [h for h in crudos if h["lat"] is None or h["lng"] is None]
        if sin_coords and (country or "").upper() == "BRA":
            from scrapitero.agents.geocode_forward import geocodebr_lote
            items = [{"id": str(idx), "logradouro": h.get("logradouro") or "",
                      "numero": h.get("numero") or "", "bairro": h.get("bairro") or "",
                      "municipio": mun_nombre or "", "estado": uf or ""}
                     for idx, h in enumerate(sin_coords)]
            res = geocodebr_lote(items, uf=uf, max_desvio_m=input.geocodebr_max_desvio_m)
            n_gb = 0
            for idx, h in enumerate(sin_coords):
                hit = res.get(str(idx))
                if hit:
                    h["lat"], h["lng"], h["geo_source"] = hit[0], hit[1], hit[2]
                    n_gb += 1
            if n_gb:
                logger.info(f"HotelFetcher: geocodebr ubicó {n_gb}/{len(sin_coords)} sin-coords "
                            f"(gratis); el resto va a Nominatim/Google")
                _tg(f"📍 geocodebr (gratis): {n_gb}/{len(sin_coords)} hoteles ubicados; "
                    f"el resto va a Nominatim/Google.")

        ubicados = []
        for h in crudos:
            if h["lat"] is None or h["lng"] is None:
                q = ", ".join(x for x in (h.get("direccion"),
                                          f"{mun_nombre} - {uf}" if mun_nombre else None, "Brasil") if x)
                hit = _geocode(q, client) if q else None
                if not hit:
                    continue
                h["lat"], h["lng"] = hit[0], hit[1]
                time.sleep(max(input.delay_ms, 0) / 1000.0)
            if zona_poly.contains(Point(h["lng"], h["lat"])):
                ubicados.append(h)

    # Dedupe entre fuentes: por CNPJ cuando ambos lo tienen; si no (Google no trae CNPJ),
    # por nombre similar + proximidad espacial (≤ merge_dist_m) para no contar 2 veces el
    # mismo hotel que aportan dos fuentes con coordenadas algo distintas.
    _FUENTE_PRIO = {"cadastur": 3, "receita": 2, "osm": 1, "google": 0}

    def _merge_into(g: dict, h: dict) -> None:
        if not g["uh"] and h["uh"]:
            g["hab_fuente"] = h.get("hab_fuente")
        g["uh"] = g["uh"] or h["uh"]
        g["leitos"] = g["leitos"] or h["leitos"]
        g["estrellas"] = g["estrellas"] or h["estrellas"]
        g["cnpj"] = g["cnpj"] or h["cnpj"]
        g["situacion"] = g["situacion"] or h["situacion"]
        g["business_status"] = g.get("business_status") or h.get("business_status")
        g["cerrado"] = g["cerrado"] or h["cerrado"]
        g["direccion"] = g.get("direccion") or h.get("direccion")
        g["telefono"] = g.get("telefono") or h.get("telefono")
        # la fuente de mayor prioridad manda (Cadastur tiene UHs; Receita situação oficial)
        if _FUENTE_PRIO.get(h["fuente"], 0) > _FUENTE_PRIO.get(g["fuente"], 0):
            g["fuente"] = h["fuente"]

    grupos: list[dict] = []
    by_cnpj: dict = {}
    for h in ubicados:
        g = by_cnpj.get(h["cnpj"]) if h.get("cnpj") else None
        if g is None:
            for cand in grupos:
                if _mismo_hotel(h, cand, input.merge_dist_m):
                    g = cand
                    break
        if g is None:
            g = dict(h)
            grupos.append(g)
        else:
            _merge_into(g, h)
        if g.get("cnpj") and g["cnpj"] not in by_cnpj:
            by_cnpj[g["cnpj"]] = g
    hoteles = grupos
    if input.max_hoteles:
        hoteles = hoteles[:input.max_hoteles]
    out.hoteles_en_zona = len(hoteles)
    if not hoteles:
        _tg(f"🏨 {out.municipio}: 0 hoteles dentro de la zona.")
        return out

    fuentes_run = list(out.por_fuente.keys())
    with engine.begin() as conn:
        # Idempotente: borra lo de estas fuentes para esta región y reinserta (la
        # fuente 'demo' u otras se conservan). OSM no tiene CNPJ → no sirve ON CONFLICT.
        conn.execute(text("DELETE FROM hoteles WHERE region_id=:r AND fuente=ANY(:f)"),
                     {"r": input.region_id, "f": fuentes_run})
        for h in hoteles:
            conn.execute(text("""
                INSERT INTO hoteles (hotel_id, survey_id, region_id, nombre, cnpj, tipo,
                    direccion, telefono, location, habitaciones, habitaciones_fuente, leitos,
                    estrellas, fuente, situacion_cadastur, business_status, cerrado_def)
                VALUES (:id, :sid, :rid, :nombre, :cnpj, :tipo, :dir, :tel,
                    ST_SetSRID(ST_MakePoint(:lng, :lat), 4326), :uh, :habf, :leitos, :est,
                    :fuente, :sit, :bs, :cerr)
            """), {"id": str(uuid.uuid4()), "sid": input.survey_id, "rid": input.region_id,
                   "nombre": h["nombre"], "cnpj": h["cnpj"], "tipo": h["tipo"],
                   "dir": h["direccion"], "tel": h.get("telefono"), "lng": h["lng"], "lat": h["lat"],
                   "uh": h["uh"], "habf": h.get("hab_fuente"), "leitos": h["leitos"],
                   "est": h["estrellas"], "fuente": h["fuente"], "sit": h["situacion"],
                   "bs": h.get("business_status"), "cerr": h["cerrado"]})

        conn.execute(text("""
            UPDATE hoteles h SET parcela_id = p.parcela_id
            FROM parcelas p
            WHERE p.region_id = :rid AND h.region_id = :rid AND h.fuente = ANY(:f)
              AND p.geometry IS NOT NULL AND h.location IS NOT NULL
              AND ST_Contains(p.geometry, h.location)
        """), {"rid": input.region_id, "f": fuentes_run})

        # Enriquecer abierto/cerrado con el business_status de Google (comercios cercanos)
        conn.execute(text("""
            UPDATE hoteles h SET business_status = c.business_status
            FROM comercios c
            WHERE c.region_id = :rid AND h.region_id = :rid AND h.fuente = ANY(:f)
              AND c.location IS NOT NULL AND h.location IS NOT NULL
              AND ST_DWithin(c.location::geography, h.location::geography, 60)
              AND (c.rubro ILIKE '%hotel%' OR c.rubro ILIKE '%lodging%'
                   OR c.tipos ILIKE '%lodging%' OR c.tipos ILIKE '%hotel%')
        """), {"rid": input.region_id, "f": fuentes_run})
        conn.execute(text("""
            UPDATE hoteles SET cerrado_def = TRUE
            WHERE region_id = :rid AND fuente = ANY(:f) AND business_status = 'CLOSED_PERMANENTLY'
        """), {"rid": input.region_id, "f": fuentes_run})

        # Habitaciones cargadas a MANO (asistencia humana): rellenan donde no hay dato
        # exacto (Cadastur UHs / OSM rooms). Sobreviven a re-cortes (tabla por CNPJ).
        # Prioridad: Cadastur/OSM (exacto) > manual > estimación BCI.
        conn.execute(text("""
            UPDATE hoteles h
            SET habitaciones = m.habitaciones, habitaciones_fuente = 'manual'
            FROM hotel_habitaciones_manual m
            WHERE h.region_id = :rid AND h.fuente = ANY(:f)
              AND m.region_id = :rid AND h.cnpj = m.cnpj AND h.habitaciones IS NULL
        """), {"rid": input.region_id, "f": fuentes_run})

        # ESTIMACIÓN de habitaciones por área del BCI cuando no hay dato exacto
        # (Cadastur UHs / OSM rooms). Proxy: area_construida / m2_por_habitacion.
        # Cuando Cadastur vuelva, el dato exacto pisa esta estimación (al re-correr).
        if input.estimar_habitaciones:
            conn.execute(text("""
                UPDATE hoteles h
                SET habitaciones = LEAST(GREATEST(ROUND(p.area_m2_construida / :m2)::int, 1), :habmax),
                    habitaciones_fuente = 'bci_proxy'
                FROM parcelas p
                WHERE h.region_id = :rid AND h.fuente = ANY(:f)
                  AND h.parcela_id = p.parcela_id AND h.habitaciones IS NULL
                  AND p.area_m2_construida IS NOT NULL AND p.area_m2_construida > 0
                  AND p.area_m2_construida <= :areamax
            """), {"rid": input.region_id, "f": fuentes_run, "m2": input.m2_por_habitacion,
                   "habmax": input.proxy_hab_max, "areamax": input.proxy_area_max_m2})

        out.vinculados_parcela = conn.execute(text(
            "SELECT COUNT(*) FROM hoteles WHERE region_id=:r AND fuente=ANY(:f) AND parcela_id IS NOT NULL"),
            {"r": input.region_id, "f": fuentes_run}).scalar() or 0
        out.cerrados = conn.execute(text(
            "SELECT COUNT(*) FROM hoteles WHERE region_id=:r AND fuente=ANY(:f) AND cerrado_def"),
            {"r": input.region_id, "f": fuentes_run}).scalar() or 0
        # Hoteles ABIERTOS sin habitaciones (ninguna fuente las tiene) → asistencia humana.
        # Cuenta TODAS las fuentes de la región (no solo las de esta corrida): un hotel de
        # Google sin habitaciones (que un re-corte receita+osm no toca) igual necesita ayuda.
        out.sin_habitaciones = conn.execute(text(
            "SELECT COUNT(*) FROM hoteles WHERE region_id=:r "
            "AND NOT cerrado_def AND habitaciones IS NULL"),
            {"r": input.region_id}).scalar() or 0

        if input.set_uf:
            filas_uf = conn.execute(text("""
                SELECT parcela_id::text, COALESCE(SUM(GREATEST(COALESCE(habitaciones,1),1)),0)
                FROM hoteles
                WHERE region_id=:r AND parcela_id IS NOT NULL AND NOT cerrado_def
                GROUP BY parcela_id
            """), {"r": input.region_id}).fetchall()
            for pid, uf in filas_uf:
                uf = int(uf)
                out.parcelas_con_hotel += 1
                out.total_uf_comercio += uf
                conn.execute(text("""
                    UPDATE parcelas SET
                        uf_comercio = :uf,
                        unidades_funcionales_estimadas = COALESCE(uf_vivienda, 0) + :uf,
                        uf_fuente = 'cadastur',
                        uso_principal = CASE
                            WHEN uso_principal = 'residencial' THEN 'mixto'
                            WHEN uso_principal IN ('comercial','mixto') THEN uso_principal
                            ELSE 'comercial' END,
                        uso_fuente = 'cadastur'
                    WHERE parcela_id = :pid
                """), {"uf": uf, "pid": pid})

    _tg(f"🏨 <b>Hoteles {out.municipio}</b>: {out.hoteles_en_zona} en zona "
        f"({out.por_fuente}), {out.vinculados_parcela} en parcela, {out.cerrados} cerrados. "
        f"UF comercio: {out.total_uf_comercio}."
        + (f" Fuentes caídas: {out.fuentes_fallidas}." if out.fuentes_fallidas else ""))

    # Asistencia humana: si quedan hoteles abiertos sin habitaciones, avisar con el link
    # a la página de carga manual (operador autenticado).
    if out.sin_habitaciones and input.survey_id:
        base = os.environ.get("WEB_BASE_URL", "http://localhost:8765").rstrip("/")
        link = f"{base}/asistencia-hoteles/{input.survey_id}"
        _tg(f"🆘 <b>Asistencia: {out.sin_habitaciones} hotel(es)</b> de {out.municipio} "
            "sin cantidad de habitaciones (Cadastur caído / sin dato).\n"
            "Hay que conseguirlas (llamando al hotel) y cargarlas a mano acá:\n"
            f"{link}")

    logger.info(f"HotelFetcher {input.region_id}: zona={out.hoteles_en_zona} "
                f"por_fuente={out.por_fuente} fallidas={out.fuentes_fallidas} "
                f"vinculados={out.vinculados_parcela} cerrados={out.cerrados} uf={out.total_uf_comercio}")
    return out
