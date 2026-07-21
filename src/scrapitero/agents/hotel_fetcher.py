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
    # Radio amplio cuando el nombre es FUERTE (igual o núcleo idéntico): las coordenadas de
    # Cadastur/Receita salen de geocodificar la dirección fiscal y caen lejos del pin real
    # (en VG: Casa Nova 303 m, Express 573 m, Ceolatto 1566 m, Las Velas 2135 m).
    merge_dist_fuerte_m: float = 2500.0
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


# Palabras genéricas/de ruido que NO distinguen un hotel de otro (PT/ES): se ignoran al
# comparar nombres para que el núcleo distintivo matchee (ej. "HOTEL SLAVIERO SLIM" vs
# "Slaviero Slim Aeroporto" → ambos núcleo {slaviero, slim}).
_GENERICOS_HOTEL = {
    "hotel", "hoteis", "hotels", "motel", "pousada", "pousadas", "flat", "apart", "aparthotel",
    "pensao", "resort", "hostel", "albergue", "inn", "suites", "suite", "hospedagem",
    "ltda", "me", "epp", "eireli", "da", "de", "do", "dos", "das", "e",
}


def _tokens_sig(n: str) -> set:
    """Tokens distintivos del nombre (sin palabras genéricas ni de 1 letra)."""
    return {t for t in n.split() if t not in _GENERICOS_HOTEL and len(t) > 1}


def _nombre_similar(a: Optional[str], b: Optional[str]) -> bool:
    """¿Son el mismo nombre de hotel? Igual normalizado; **núcleo distintivo idéntico**
    (ignorando palabras genéricas hotel/pousada/ltda/…: "HOTEL TAINA" ≡ "Tainá Hotel");
    ≥2 tokens distintivos en común; o ratio difflib ≥ 0.82.
    NO alcanza compartir UNA palabra: un comercio llamado "Amazon" no es el
    "Amazon Hotel Aeroporto" (era el bug del subset de tokens crudos)."""
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
    """Señal FUERTE de mismo nombre: igual normalizado o núcleo distintivo idéntico.
    Habilita el radio de merge amplio (las coordenadas de Cadastur/Receita salen de
    geocodificar la dirección fiscal y pueden caer a cientos de metros del hotel real)."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    sa, sb = _tokens_sig(na), _tokens_sig(nb)
    return bool(sa) and sa == sb


def _clave_dir(h: dict) -> Optional[str]:
    """Clave de dirección normalizada (calle|número) para identificar el MISMO hotel por
    **igual dirección** entre fuentes, sin importar la distancia del geocoding. None si no
    hay número (no se deduplica por calle sola, evita fusionar hoteles distintos de la calle)."""
    from scrapitero.agents.direccion_norm import clave_direccion, separar_numero
    logr, num = (h.get("logradouro") or "").strip(), (h.get("numero") or "").strip()
    if logr and num:
        calle, numero = logr, num
    else:
        # parsear calle+número de la dirección completa (separar_numero descarta el
        # complemento/ciudad que sigue al número → no contamina la calle)
        calle, numero = separar_numero(h.get("direccion") or logr)
    k = clave_direccion(calle, numero)
    return k if (k and k.rsplit("|", 1)[-1]) else None


def _mismo_hotel(a: dict, b: dict, max_dist_m: float, max_dist_fuerte_m: float = 0.0) -> bool:
    """Dos registros = el mismo hotel:
      1. mismo CNPJ (cuando ambos lo tienen);
      2. CNPJs DISTINTOS ya **no** descarta: el dueño cierra una empresa y abre otra para
         el mismo hotel (re-registro/matriz-filial). Se exige evidencia estricta: mismo
         nombre + misma dirección, o mismo nombre fuerte a ≤ `max_dist_m`;
      3. **igual dirección** (calle+número normalizados) — criterio principal cross-fuente,
         independiente de las coordenadas (cada fuente geocodifica distinto);
      4. nombre FUERTE (igual o núcleo distintivo idéntico) + ≤ `max_dist_fuerte_m`: las
         coords de Cadastur/Receita son la dirección fiscal geocodificada y caen a cientos
         de metros del pin real de Google/OSM;
      5. nombre similar (débil) + ≤ `max_dist_m`."""
    if a.get("cnpj") and b.get("cnpj") and a["cnpj"] == b["cnpj"]:
        return True
    cnpjs_distintos = bool(a.get("cnpj") and b.get("cnpj"))
    ka, kb = _clave_dir(a), _clave_dir(b)
    misma_dir = bool(ka and kb and ka == kb)
    if cnpjs_distintos:
        # dos personas jurídicas: solo si es evidentemente el MISMO hotel físico
        if misma_dir:
            return _nombre_similar(a.get("nombre"), b.get("nombre"))
        if a.get("lat") is None or b.get("lat") is None:
            return False
        return (_nombre_fuerte(a.get("nombre"), b.get("nombre"))
                and _dist_m(a["lat"], a["lng"], b["lat"], b["lng"]) <= max_dist_m)
    if misma_dir:                     # igual dirección ⇒ mismo hotel (sea cual sea la distancia)
        return True
    if a.get("lat") is None or b.get("lat") is None:
        return False
    d = _dist_m(a["lat"], a["lng"], b["lat"], b["lng"])
    if _nombre_fuerte(a.get("nombre"), b.get("nombre")):
        return d <= max(max_dist_fuerte_m, max_dist_m)
    return _nombre_similar(a.get("nombre"), b.get("nombre")) and d <= max_dist_m


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

    def _url(x) -> str:
        return (x.get("url") or "")

    # El portal etiqueta como format="CSV" recursos que en realidad son .xls/.xlsx
    # (binarios) — csv.reader los lee como basura. Quedarse SOLO con .csv reales.
    reales = [x for x in resources if _url(x).lower().endswith(".csv")]
    # De esos, preferir el registro Cadastur PJ (`...cadasturpj.csv`): es el que trae
    # UH / Total de Leitos / Atividade (las habitaciones, el valor de Cadastur). Los
    # otros .csv "meio-de-hospedagem" son listados tipo Receita, SIN habitaciones.
    ricos = [x for x in reales if "cadasturpj" in _url(x).lower()]
    pool = ricos or reales
    pool.sort(key=lambda x: x.get("last_modified") or x.get("created") or "", reverse=True)
    return _url(pool[0]) if pool else None


def _col(headers_norm: dict, *claves: str) -> Optional[int]:
    for h, i in headers_norm.items():
        if any(k in h for k in claves):
            return i
    return None


def _cadastur_desde_local(mun_nombre: str, uf: str) -> list[dict]:
    """Hoteles de Cadastur desde la tabla local `cadastur_hospedagem` (la carga
    CadasturLocalFetcher consolidando todos los trimestres). [] si no hay para esa ciudad o
    si la tabla aún no existe. Mismo dict-shape que el camino del portal."""
    mun_norm = _norm(mun_nombre)
    try:
        engine = get_engine()
        with engine.connect() as conn:
            filas = conn.execute(text("""
                SELECT nome_fantasia, razao_social, cnpj, tipo_hospedagem, uh, leitos,
                       situacao, logradouro, numero, bairro, municipio
                FROM cadastur_hospedagem
                WHERE (:uf = '' OR upper(uf) = upper(:uf))
            """), {"uf": uf or ""}).fetchall()
    except Exception:  # la tabla aún no existe → sin local
        return []
    hoteles = []
    for f in filas:
        if _norm(f[10] or "") != mun_norm:   # filtro de município sin depender de unaccent
            continue
        sit = f[6] or ""
        hoteles.append({
            "nombre": f[0] or f[1] or None,
            "cnpj": (re.sub(r"\D", "", f[2] or "")[:20] or None),
            "tipo": (f[3] or None),
            "direccion": " ".join(x for x in (f[7] or "", f[8] or "", f[9] or "") if x) or None,
            "logradouro": f[7] or None, "numero": f[8] or None, "bairro": f[9] or None,
            "lat": None, "lng": None,
            "uh": f[4], "leitos": f[5],
            "estrellas": None, "situacion": (sit[:40] or None),
            "cerrado": bool(sit) and any(k in _norm(sit) for k in
                                         ("inativo", "cancelado", "baixado", "encerrad")),
            "business_status": None,
            "hab_fuente": "cadastur" if f[4] else None,
            "fuente": "cadastur",
        })
    return hoteles


def _fetch_cadastur(mun_nombre: str, uf: str, client: httpx.Client) -> list[dict]:
    """Hoteles de Cadastur del município. **Tabla local primero** (cadastur_hospedagem,
    consolidada por CadasturLocalFetcher); si está vacía para esa ciudad, cae al portal CKAN.
    Sin coordenadas (se geocodifican luego). Lanza RuntimeError si el portal no responde."""
    locales = _cadastur_desde_local(mun_nombre, uf)
    if locales:
        logger.info(f"Cadastur: {len(locales)} hoteles de {mun_nombre} desde la tabla local "
                    f"(cadastur_hospedagem) — sin tocar el portal")
        return locales
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
        # el CSV cadasturpj llama "Localidade" al município (sin esto, el filtro no se aplica
        # y devuelve toda la UF)
        "mun": ("municipio", "localidade"), "uf": ("uf", "estado"),
        "uh": ("unidade habitacionais", "unidades habitacionais", "uh", "quantidade unidades"),
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


def _norm_q(q: str) -> str:
    """Normaliza la query para la clave de caché (sin acentos, lower, espacios colapsados)."""
    s = "".join(c for c in unicodedata.normalize("NFKD", q or "") if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower()).strip()


def _geocode_cached(query: str, client: httpx.Client, conn, iso2: str = "br") -> Optional[tuple]:
    """Geocodifica `query` con caché en `geocode_cache` y **Mapbox como primaria**.
    Devuelve (lat, lng, source, cache_hit) o None. geocodebr ya corrió antes (gratis/lote);
    acá va lo que quedó sin coords. Mapbox (preciso/rápido) → Nominatim (gratis) → Google."""
    clave = f"{iso2}|hotel|{_norm_q(query)}"
    row = conn.execute(text(
        "SELECT lat, lng, geocode_source FROM geocode_cache WHERE clave = :k"),
        {"k": clave}).fetchone()
    if row:
        return (float(row[0]), float(row[1]), row[2] or "cache", True)
    for src, fn in (("mapbox", _mapbox), ("nominatim", _nominatim), ("google", _google)):
        hit = fn(query, iso2, client)
        if hit:
            lat, lng = float(hit[0]), float(hit[1])
            conf = hit[2] if len(hit) > 2 else None
            conn.execute(text("""
                INSERT INTO geocode_cache (clave, query, lat, lng, geocode_source, geocode_confidence)
                VALUES (:k, :q, :lat, :lng, :src, :conf)
                ON CONFLICT (clave) DO NOTHING
            """), {"k": clave, "q": query, "lat": lat, "lng": lng, "src": src, "conf": conf})
            conn.commit()
            return (lat, lng, src, False)
    return None


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
                            f"(gratis); el resto va a Mapbox/Nominatim/Google (con caché)")
                _tg(f"📍 geocodebr (gratis): {n_gb}/{len(sin_coords)} hoteles ubicados; "
                    f"el resto va a Mapbox/Nominatim/Google (con caché).")

        ubicados = []
        geo_stats = {"cache": 0, "mapbox": 0, "nominatim": 0, "google": 0}
        with engine.connect() as gconn:
            for h in crudos:
                if h["lat"] is None or h["lng"] is None:
                    q = ", ".join(x for x in (h.get("direccion"),
                                              f"{mun_nombre} - {uf}" if mun_nombre else None, "Brasil") if x)
                    hit = _geocode_cached(q, client, gconn) if q else None
                    if not hit:
                        continue
                    h["lat"], h["lng"] = hit[0], hit[1]
                    cache_hit = hit[3]
                    geo_stats["cache" if cache_hit else hit[2]] = \
                        geo_stats.get("cache" if cache_hit else hit[2], 0) + 1
                    if not cache_hit:        # solo throttle cuando SÍ pegamos a una API
                        time.sleep(max(input.delay_ms, 0) / 1000.0)
                if zona_poly.contains(Point(h["lng"], h["lat"])):
                    ubicados.append(h)
        if sum(geo_stats.values()):
            logger.info("HotelFetcher geocoding: " + ", ".join(
                f"{k}={v}" for k, v in geo_stats.items() if v))
        # El cap se aplica ACÁ, a lo recién buscado — nunca a `hoteles` ya mezclado con
        # semillas de otras fuentes: aplicado después del merge, `hoteles[:max_hoteles]`
        # podía cortar justo los hoteles de esta corrida (las semillas quedan primero en
        # la lista) mientras el DELETE por fuente ya había borrado sus filas viejas →
        # desaparecían de la región sin dejar rastro.
        if input.max_hoteles:
            ubicados = ubicados[:input.max_hoteles]

    # Dedupe entre fuentes: por CNPJ cuando ambos lo tienen; si no (Google no trae CNPJ),
    # por dirección normalizada o nombre + proximidad (radio amplio si el nombre es fuerte:
    # las coords de Cadastur/Receita son la dirección fiscal geocodificada, no el hotel).
    _FUENTE_PRIO = {"cadastur": 3, "receita": 2, "osm": 1, "google": 0}
    # Para la UBICACIÓN el orden se invierte: Google/OSM traen el pin físico real;
    # Cadastur/Receita, una dirección (a veces fiscal) geocodificada.
    _GEO_PRIO = {"google": 3, "osm": 2, "cadastur": 1, "receita": 0}

    def _merge_into(g: dict, h: dict) -> None:
        h_mejor = _FUENTE_PRIO.get(h["fuente"], 0) > _FUENTE_PRIO.get(g["fuente"], 0)
        # Ubicación + dirección física: manda el pin real (Google/OSM). La dirección fiscal
        # de Receita geocodificada era lo que confundía a la asistencia humana.
        g_geo = g.get("geo_fuente") or g["fuente"]
        if h.get("lat") is not None and (
                g.get("lat") is None
                or _GEO_PRIO.get(h["fuente"], 0) > _GEO_PRIO.get(g_geo, 0)):
            g["lat"], g["lng"], g["geo_fuente"] = h["lat"], h["lng"], h["fuente"]
            if h.get("direccion") and h["fuente"] in ("google", "osm"):
                g["direccion"], g["dir_fisica"] = h["direccion"], True
        # Habitaciones: manda la fuente MÁS confiable (Cadastur UH real > estimación IA de
        # Google). Si la más confiable no tiene dato, se completa con la otra.
        if h["uh"] and (h_mejor or not g["uh"]):
            g["uh"], g["hab_fuente"] = h["uh"], h.get("hab_fuente")
            g["leitos"] = h["leitos"] or g["leitos"]
        else:
            g["leitos"] = g["leitos"] or h["leitos"]
        g["estrellas"] = g["estrellas"] or h["estrellas"]
        g["cnpj"] = g["cnpj"] or h["cnpj"]
        g["business_status"] = g.get("business_status") or h.get("business_status")
        g["telefono"] = g.get("telefono") or h.get("telefono")
        if h_mejor:
            # identidad de la fuente más confiable (Cadastur > Receita > OSM > Google);
            # la dirección solo si el grupo no tiene ya la física del pin real. `cerrado`
            # SIGUE a la fuente ganadora (no un OR acumulativo): si quedó cerrado por un
            # falso positivo de Google en una corrida vieja y hoy Cadastur (más confiable)
            # dice que está activo, tiene que poder reabrir — un OR nunca deja volver atrás.
            g["cerrado"] = h["cerrado"]
            for k in ("nombre", "tipo", "situacion"):
                if h.get(k):
                    g[k] = h[k]
            # `fuente` NO se pisa si `g` es una semilla (o ya absorbió una): esa columna es
            # lo único que decide, en la PRÓXIMA corrida, si la fila cuenta como semilla
            # protegida (`fuente != ANY(fuentes_run)`). Si se pisara con la fuente de HOY,
            # la próxima vez que se re-corra esa misma fuente la fila se borraría de nuevo
            # como "propia" y jamás volvería a entrar como semilla — el pin/negocio real
            # de Google/OSM rescatado hoy se perdería en el siguiente re-corte gratuito.
            if h.get("fuente") and not (g.get("_seed_id") or g.get("_dirty")):
                g["fuente"] = h["fuente"]
            if h.get("direccion") and not g.get("dir_fisica"):
                g["direccion"] = h["direccion"]
        else:
            g["direccion"] = g.get("direccion") or h.get("direccion")
            g["situacion"] = g["situacion"] or h["situacion"]

    # Semillas cross-run: filas ya en DB de fuentes que NO corren en este run (p.ej. Google,
    # paga, de una corrida anterior). Entran al dedupe como grupos para que las fuentes de
    # hoy se FUSIONEN con ellas en vez de duplicarlas ('demo' se conserva aparte, como hoy).
    # Si una semilla absorbe datos se borra y reinserta mergeada; intacta, queda como está.
    fuentes_run = list(out.por_fuente.keys())
    semillas: list[dict] = []
    with engine.connect() as conn:
        # Scope por survey (mismo patrón que /api/surveys/{id}/hoteles-asistencia): filas
        # SIN survey_id (comunes al survey grande y sus sub-zonas) + las del survey actual.
        # Sin esto, correr el paso de hoteles sobre UN survey podía absorber y reetiquetar
        # (línea de abajo, "sid": input.survey_id) los hoteles de Google de OTRO survey de
        # la misma región, haciéndolos desaparecer de ahí — "los relevamientos nunca se
        # pisan" (CLAUDE.md).
        for r in conn.execute(text("""
            SELECT hotel_id::text, nombre, cnpj, tipo, direccion, telefono,
                   ST_Y(location), ST_X(location), habitaciones, habitaciones_fuente,
                   leitos, estrellas, fuente, situacion_cadastur, business_status, cerrado_def,
                   survey_id::text
            FROM hoteles
            WHERE region_id = :r AND fuente != 'demo' AND NOT (fuente = ANY(:f))
              AND (survey_id IS NULL OR CAST(:sid AS uuid) IS NULL
                   OR survey_id = CAST(:sid AS uuid))
        """), {"r": input.region_id, "f": fuentes_run, "sid": input.survey_id}):
            semillas.append({
                "_seed_id": r[0], "nombre": r[1],
                "cnpj": (re.sub(r"\D", "", r[2] or "")[:20] or None), "tipo": r[3],
                "direccion": r[4], "telefono": r[5],
                "lat": float(r[6]) if r[6] is not None else None,
                "lng": float(r[7]) if r[7] is not None else None,
                "uh": r[8], "hab_fuente": r[9], "leitos": r[10], "estrellas": r[11],
                "fuente": r[12], "geo_fuente": r[12],
                "dir_fisica": r[12] in ("google", "osm"),
                "situacion": r[13], "business_status": r[14], "cerrado": bool(r[15]),
                "survey_id": r[16],
            })

    grupos: list[dict] = list(semillas)
    by_cnpj: dict = {s["cnpj"]: s for s in semillas if s.get("cnpj")}
    for h in ubicados:
        g = by_cnpj.get(h["cnpj"]) if h.get("cnpj") else None
        if g is None:
            for cand in grupos:
                if _mismo_hotel(h, cand, input.merge_dist_m, input.merge_dist_fuerte_m):
                    g = cand
                    break
        if g is None:
            g = dict(h)
            g["geo_fuente"] = h["fuente"]
            g["dir_fisica"] = h["fuente"] in ("google", "osm")
            g["survey_id"] = input.survey_id
            grupos.append(g)
        else:
            _merge_into(g, h)
            g["_dirty"] = True
        if g.get("cnpj") and g["cnpj"] not in by_cnpj:
            by_cnpj[g["cnpj"]] = g
    hoteles = grupos      # el cap de max_hoteles ya se aplicó sobre `ubicados` (arriba),
                           # no acá: acá `grupos` ya incluye semillas que no hay que cortar
    out.hoteles_en_zona = len(hoteles)
    if not hoteles:
        _tg(f"🏨 {out.municipio}: 0 hoteles dentro de la zona.")
        return out

    with engine.begin() as conn:
        # Idempotente: borra lo de estas fuentes para esta región y reinserta (la
        # fuente 'demo' u otras se conservan). Acotado al MISMO scope de survey que la
        # query de semillas (arriba): sin este filtro, correr el paso sobre un survey
        # chico borraba también las filas de esas fuentes que pertenecían a OTRO survey
        # de la misma región (survey grande / otra sub-zona) y nadie las reinsertaba.
        conn.execute(text("""
            DELETE FROM hoteles WHERE region_id=:r AND fuente=ANY(:f)
              AND (survey_id IS NULL OR CAST(:sid AS uuid) IS NULL
                   OR survey_id = CAST(:sid AS uuid))
        """), {"r": input.region_id, "f": fuentes_run, "sid": input.survey_id})
        # Semillas que absorbieron datos de este run: su fila vieja se reemplaza por el
        # grupo mergeado. Las intactas no se tocan (conservan hotel_id/parcela/estado).
        absorbidas = [h["_seed_id"] for h in hoteles if h.get("_seed_id") and h.get("_dirty")]
        if absorbidas:
            conn.execute(text("DELETE FROM hoteles WHERE hotel_id::text = ANY(:ids)"),
                         {"ids": absorbidas})
        insertados: list[str] = []      # hotel_ids escritos en ESTE run (para los updates)
        for h in hoteles:
            if h.get("_seed_id") and not h.get("_dirty"):
                continue
            hid = str(uuid.uuid4())
            # Semilla absorbida ⇒ conserva SU survey_id original (nunca lo cambia el merge,
            # así no le roba visibilidad a otro survey de la región); grupo nuevo ⇒ el de
            # esta corrida (ya seteado al crearlo, más arriba).
            sid = h["survey_id"] if "survey_id" in h else input.survey_id
            # ON CONFLICT sobre uq_hoteles_region_cnpj (mig. 025): si el mismo CNPJ ya existe
            # por una fila de OTRO survey que el scope de semillas no atrapó (dedupe en
            # memoria solo ve las filas de survey propio/compartido), esto actualiza el
            # contenido en vez de romper la transacción — y deliberadamente NO toca
            # survey_id en el UPDATE, para no robarle la fila a quien la creó. Con cnpj
            # NULL (OSM) el constraint nunca conflictúa (NULL≠NULL en Postgres), así que
            # siempre inserta.
            real_id = conn.execute(text("""
                INSERT INTO hoteles (hotel_id, survey_id, region_id, nombre, cnpj, tipo,
                    direccion, telefono, location, habitaciones, habitaciones_fuente, leitos,
                    estrellas, fuente, situacion_cadastur, business_status, cerrado_def)
                VALUES (:id, :sid, :rid, :nombre, :cnpj, :tipo, :dir, :tel,
                    ST_SetSRID(ST_MakePoint(:lng, :lat), 4326), :uh, :habf, :leitos, :est,
                    :fuente, :sit, :bs, :cerr)
                ON CONFLICT ON CONSTRAINT uq_hoteles_region_cnpj DO UPDATE SET
                    nombre=EXCLUDED.nombre, tipo=EXCLUDED.tipo, direccion=EXCLUDED.direccion,
                    telefono=EXCLUDED.telefono, location=EXCLUDED.location,
                    habitaciones=EXCLUDED.habitaciones,
                    habitaciones_fuente=EXCLUDED.habitaciones_fuente,
                    leitos=EXCLUDED.leitos, estrellas=EXCLUDED.estrellas,
                    fuente=EXCLUDED.fuente, situacion_cadastur=EXCLUDED.situacion_cadastur,
                    business_status=EXCLUDED.business_status, cerrado_def=EXCLUDED.cerrado_def
                RETURNING hotel_id::text
            """), {"id": hid, "sid": sid, "rid": input.region_id,
                   "nombre": h["nombre"], "cnpj": h["cnpj"], "tipo": h["tipo"],
                   "dir": h["direccion"], "tel": h.get("telefono"), "lng": h["lng"], "lat": h["lat"],
                   "uh": h["uh"], "habf": h.get("hab_fuente"), "leitos": h["leitos"],
                   "est": h["estrellas"], "fuente": h["fuente"], "sit": h["situacion"],
                   "bs": h.get("business_status"), "cerr": h["cerrado"]}).scalar()
            insertados.append(real_id)

        # Los updates de abajo van por hotel_id insertado (no por fuente): un grupo mergeado
        # puede conservar la fuente de una semilla que no corrió en este run (p.ej. cadastur).
        conn.execute(text("""
            UPDATE hoteles h SET parcela_id = p.parcela_id
            FROM parcelas p
            WHERE p.region_id = :rid AND h.hotel_id::text = ANY(:ids)
              AND p.geometry IS NOT NULL AND h.location IS NOT NULL
              AND ST_Contains(p.geometry, h.location)
        """), {"rid": input.region_id, "ids": insertados})

        # Enriquecer abierto/cerrado con el business_status de Google (comercios cercanos).
        # Exige NOMBRE similar además de proximidad: antes cualquier lodging a ≤60 m
        # pisaba el estado de un hotel ajeno (era otro vector de confusión hotel↔comercio).
        candidatos = conn.execute(text("""
            SELECT nombre, business_status, ST_Y(location), ST_X(location)
            FROM comercios
            WHERE region_id = :rid AND location IS NOT NULL AND business_status IS NOT NULL
              AND (rubro ILIKE '%hotel%' OR rubro ILIKE '%lodging%'
                   OR tipos ILIKE '%lodging%' OR tipos ILIKE '%hotel%')
        """), {"rid": input.region_id}).fetchall()
        if candidatos:
            filas = conn.execute(text("""
                SELECT hotel_id::text, nombre, ST_Y(location), ST_X(location)
                FROM hoteles WHERE hotel_id::text = ANY(:ids) AND location IS NOT NULL
            """), {"ids": insertados}).fetchall()
            for hid, hnombre, hlat, hlng in filas:
                mejor = None
                for cnombre, cbs, clat, clng in candidatos:
                    d = _dist_m(hlat, hlng, clat, clng)
                    if d <= 60 and _nombre_similar(hnombre, cnombre) \
                            and (mejor is None or d < mejor[0]):
                        mejor = (d, cbs)
                if mejor:
                    conn.execute(text(
                        "UPDATE hoteles SET business_status=:bs WHERE hotel_id::text=:id"),
                        {"bs": mejor[1], "id": hid})
        conn.execute(text("""
            UPDATE hoteles SET cerrado_def = TRUE
            WHERE hotel_id::text = ANY(:ids) AND business_status = 'CLOSED_PERMANENTLY'
        """), {"ids": insertados})

        # Habitaciones cargadas a MANO (asistencia humana): rellenan donde no hay dato
        # exacto (Cadastur UHs / OSM rooms). Sobreviven a re-cortes (tabla por CNPJ).
        # Prioridad: Cadastur/OSM (exacto) > manual > estimación BCI.
        conn.execute(text("""
            UPDATE hoteles h
            SET habitaciones = m.habitaciones, habitaciones_fuente = 'manual'
            FROM hotel_habitaciones_manual m
            WHERE h.hotel_id::text = ANY(:ids)
              AND m.region_id = :rid AND h.cnpj = m.cnpj AND h.habitaciones IS NULL
        """), {"rid": input.region_id, "ids": insertados})

        # ESTIMACIÓN de habitaciones por área del BCI cuando no hay dato exacto
        # (Cadastur UHs / OSM rooms). Proxy: area_construida / m2_por_habitacion.
        # Cuando Cadastur vuelva, el dato exacto pisa esta estimación (al re-correr).
        if input.estimar_habitaciones:
            conn.execute(text("""
                UPDATE hoteles h
                SET habitaciones = LEAST(GREATEST(ROUND(p.area_m2_construida / :m2)::int, 1), :habmax),
                    habitaciones_fuente = 'bci_proxy'
                FROM parcelas p
                WHERE h.hotel_id::text = ANY(:ids)
                  AND h.parcela_id = p.parcela_id AND h.habitaciones IS NULL
                  AND p.area_m2_construida IS NOT NULL AND p.area_m2_construida > 0
                  AND p.area_m2_construida <= :areamax
            """), {"ids": insertados, "m2": input.m2_por_habitacion,
                   "habmax": input.proxy_hab_max, "areamax": input.proxy_area_max_m2})

        out.vinculados_parcela = conn.execute(text(
            "SELECT COUNT(*) FROM hoteles WHERE hotel_id::text=ANY(:ids) AND parcela_id IS NOT NULL"),
            {"ids": insertados}).scalar() or 0
        out.cerrados = conn.execute(text(
            "SELECT COUNT(*) FROM hoteles WHERE hotel_id::text=ANY(:ids) AND cerrado_def"),
            {"ids": insertados}).scalar() or 0
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
            # Resetear las parcelas que en una corrida ANTERIOR tenían un hotel vinculado
            # (uf_fuente='cadastur', la marca que deja el UPDATE de abajo) y hoy ya NO
            # aparecen en filas_uf — el hotel cerró, se fusionó a otra parcela, o se
            # re-geocodificó a una vecina. Sin esto, uf_comercio/uso_principal quedaban
            # pegados para siempre con el valor viejo (y si el hotel se movió, terminaba
            # contado en DOS parcelas a la vez: la vieja con el valor stale + la nueva).
            vigentes = [pid for pid, _ in filas_uf]
            conn.execute(text("""
                UPDATE parcelas SET
                    uf_comercio = 0,
                    unidades_funcionales_estimadas = COALESCE(uf_vivienda, 0),
                    uf_fuente = NULL,
                    uso_principal = CASE WHEN uso_fuente = 'cadastur' THEN NULL
                                         ELSE uso_principal END,
                    uso_fuente = CASE WHEN uso_fuente = 'cadastur' THEN NULL
                                      ELSE uso_fuente END
                WHERE region_id = :r AND uf_fuente = 'cadastur'
                  AND NOT (parcela_id::text = ANY(:vigentes))
            """), {"r": input.region_id, "vigentes": vigentes})
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
