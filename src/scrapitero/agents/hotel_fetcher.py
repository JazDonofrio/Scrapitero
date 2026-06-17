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
from scrapitero.agents.baseline_geocoder import _HEADERS, _google, _nominatim, _tg
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
    m2_por_habitacion: float = 35.0


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


def _norm(s: Optional[str]) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", str(s or ""))
                if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def _entero(s) -> Optional[int]:
    m = re.search(r"\d+", str(s or ""))
    return int(m.group(0)) if m else None


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
                   logradouro, numero, bairro, municipio_nome, situacao, lat, lng
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
            "lat": float(r.lat) if r.lat is not None else None,
            "lng": float(r.lng) if r.lng is not None else None,
            "uh": None, "leitos": None, "estrellas": None,
            "situacion": (sit[:40] or None),
            # cerrado definitivo: situação BAIXADA (deu baixa) o NULA. INAPTA/SUSPENSA no
            # son cierre definitivo (quedan abiertas pero con la situação visible).
            "cerrado": _norm(sit) in ("baixada", "nula"),
            "business_status": None, "hab_fuente": None, "fuente": "receita",
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
            "direccion": None, "lat": float(lat), "lng": float(lng),
            "uh": None, "leitos": None, "estrellas": None, "situacion": None,
            "cerrado": (bs == "CLOSED_PERMANENTLY"), "business_status": bs,
            "hab_fuente": None, "fuente": "google",
        })
    return hoteles


def _geocode(query: str, client: httpx.Client) -> Optional[tuple]:
    return _nominatim(query, "br", client) or _google(query, "br", client)


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

        # Geocodificar los que no traen coordenadas (Cadastur) + recortar a zona
        from shapely.geometry import Point
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

    # Dedupe entre fuentes: por CNPJ, si no por nombre normalizado + grilla ~100m
    merged: dict = {}
    for h in ubicados:
        key = ("cnpj", h["cnpj"]) if h.get("cnpj") else \
              ("nom", _norm(h.get("nombre")), round(h["lat"], 3), round(h["lng"], 3))
        if key in merged:
            g = merged[key]
            if not g["uh"] and h["uh"]:
                g["hab_fuente"] = h.get("hab_fuente")
            g["uh"] = g["uh"] or h["uh"]
            g["leitos"] = g["leitos"] or h["leitos"]
            g["estrellas"] = g["estrellas"] or h["estrellas"]
            g["cnpj"] = g["cnpj"] or h["cnpj"]
            g["situacion"] = g["situacion"] or h["situacion"]
            g["business_status"] = g.get("business_status") or h.get("business_status")
            g["cerrado"] = g["cerrado"] or h["cerrado"]
            if h["fuente"] == "cadastur":     # Cadastur manda (oficial, tiene UHs)
                g["fuente"] = "cadastur"
        else:
            merged[key] = dict(h)
    hoteles = list(merged.values())
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
                    direccion, location, habitaciones, habitaciones_fuente, leitos,
                    estrellas, fuente, situacion_cadastur, business_status, cerrado_def)
                VALUES (:id, :sid, :rid, :nombre, :cnpj, :tipo, :dir,
                    ST_SetSRID(ST_MakePoint(:lng, :lat), 4326), :uh, :habf, :leitos, :est,
                    :fuente, :sit, :bs, :cerr)
            """), {"id": str(uuid.uuid4()), "sid": input.survey_id, "rid": input.region_id,
                   "nombre": h["nombre"], "cnpj": h["cnpj"], "tipo": h["tipo"],
                   "dir": h["direccion"], "lng": h["lng"], "lat": h["lat"],
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

        # ESTIMACIÓN de habitaciones por área del BCI cuando no hay dato exacto
        # (Cadastur UHs / OSM rooms). Proxy: area_construida / m2_por_habitacion.
        # Cuando Cadastur vuelva, el dato exacto pisa esta estimación (al re-correr).
        conn.execute(text("""
            UPDATE hoteles h
            SET habitaciones = GREATEST(ROUND(p.area_m2_construida / :m2)::int, 1),
                habitaciones_fuente = 'bci_proxy'
            FROM parcelas p
            WHERE h.region_id = :rid AND h.fuente = ANY(:f)
              AND h.parcela_id = p.parcela_id AND h.habitaciones IS NULL
              AND p.area_m2_construida IS NOT NULL AND p.area_m2_construida > 0
        """), {"rid": input.region_id, "f": fuentes_run, "m2": input.m2_por_habitacion})

        out.vinculados_parcela = conn.execute(text(
            "SELECT COUNT(*) FROM hoteles WHERE region_id=:r AND fuente=ANY(:f) AND parcela_id IS NOT NULL"),
            {"r": input.region_id, "f": fuentes_run}).scalar() or 0
        out.cerrados = conn.execute(text(
            "SELECT COUNT(*) FROM hoteles WHERE region_id=:r AND fuente=ANY(:f) AND cerrado_def"),
            {"r": input.region_id, "f": fuentes_run}).scalar() or 0

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
    logger.info(f"HotelFetcher {input.region_id}: zona={out.hoteles_en_zona} "
                f"por_fuente={out.por_fuente} fallidas={out.fuentes_fallidas} "
                f"vinculados={out.vinculados_parcela} cerrados={out.cerrados} uf={out.total_uf_comercio}")
    return out
