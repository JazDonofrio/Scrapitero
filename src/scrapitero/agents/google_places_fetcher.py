"""GooglePlacesFetcher — comercios de Google Places → uf_comercio real por parcela.

Objetivo: aportar el conteo **real** de comercios por parcela. Cada comercio cuyo
punto cae dentro de una parcela suma **+1 a uf_comercio** (sin agrupar: un shopping
de 20 locales = 20 UF de comercio). Es la fuente autoritativa de uf_comercio —
pisa el proxy geométrico de UnidadesEstimator (uf_fuente='google').

Genérico (cualquier país). El idioma de respuesta se detecta del region_id
(-br → pt-BR, -ar → es-AR, otro → es), igual que AddressResolver.

Estrategia de costo (Google Places es caro, ~USD 0.032/búsqueda Nearby SKU Pro):
  - Busca por **teselas que cubren la zona** (no por parcela). El polígono de la
    región (zone_geojson/bbox_wkt) se cubre con una grilla de celdas de `cell_size_m`.
  - Celdas cuyo centro cae fuera del polígono se descartan (no se malgasta request).
  - **Quadtree adaptativo:** si una celda devuelve el máximo (20 = posible truncado),
    se subdivide en 4 y se recurre hasta `min_cell_m`. Así zonas densas se afinan y
    zonas ralas no se sobrepagan.
  - `max_requests` corta la corrida (resultado parcial) para acotar el gasto.
  - Avisos de costo estimado por Telegram (inicio, parcial al cortar, final).

Pasos:
  1. Tesselado adaptativo → llamadas places:searchNearby (API New, field mask acotado).
  2. Upsert de comercios en tabla `comercios` (dedup por (region_id, place_id)).
  3. Vinculación espacial a parcela (ST_Contains, igual que OSMBuildingFetcher).
  4. Agregación por parcela → uf_comercio = count, uf_fuente='google',
     recalcula unidades_funcionales_estimadas, y sube uso_principal a
     'comercial' (o 'mixto' si ya era residencial), uso_fuente='google'.

Requiere GOOGLE_MAPS_API_KEY con Places API (New) habilitada.
Correr DESPUÉS de UnidadesEstimator (que ya puso uf_vivienda) — solo sobrescribe
uf_comercio con el conteo real.
"""

from __future__ import annotations

import math
import os
import uuid
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from shapely.geometry import box, shape
from shapely.ops import unary_union
from sqlalchemy import text

from scrapitero.db.engine import get_engine
from scrapitero.agents._run import agent_run

_PLACES_URL = "https://places.googleapis.com/v1/places:searchNearby"
_FIELD_MASK = ("places.id,places.displayName,places.location,"
               "places.types,places.primaryType,places.businessStatus")
_MAX_RESULT_COUNT = 20            # tope duro de la API (New) por llamada
_COST_PER_REQUEST = 0.032        # USD por searchNearby (SKU Pro, aprox.)

# Tipos comerciales de Google Places (Table A) que cuentan como UF de comercio.
# Excluye equipamiento que no es comercio (escuelas, hospitales, culto, transporte,
# gobierno, parques) — esos no aportan uf_comercio.
_INCLUDED_TYPES = [
    # Comercio minorista y alimentos
    "store", "supermarket", "grocery_store", "convenience_store", "department_store",
    "clothing_store", "shoe_store", "jewelry_store", "book_store", "furniture_store",
    "home_goods_store", "hardware_store", "electronics_store", "bicycle_store",
    "pet_store", "liquor_store", "florist", "gift_shop", "market",
    "restaurant", "cafe", "bakery", "bar", "meal_takeaway", "meal_delivery",
    "fast_food_restaurant", "coffee_shop",
    # Servicios
    "pharmacy", "drugstore", "bank", "atm", "gym", "beauty_salon", "hair_salon",
    "spa", "laundry", "car_repair", "car_dealer", "car_wash", "gas_station",
    "real_estate_agency", "insurance_agency", "travel_agency", "lawyer",
    "accounting", "veterinary_care", "dental_clinic", "doctor", "lodging", "hotel",
]


# ── Pydantic I/O ──────────────────────────────────────────────────────────────

class GooglePlacesInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None     # si se omite → último survey de la región
    cell_size_m: float = 150.0          # lado de la celda inicial de búsqueda
    min_cell_m: float = 40.0            # no subdividir por debajo de esto
    max_requests: int = 400             # tope de llamadas (corta resultado parcial)
    included_types: Optional[list[str]] = None  # override de tipos comerciales
    set_uso: bool = True                # subir uso_principal a comercial/mixto
    overwrite: bool = False             # re-tesselar aunque ya haya comercios cargados
    # Override de bbox (cuando zone_geojson tiene features dispersos)
    bbox_south: Optional[float] = None
    bbox_west: Optional[float] = None
    bbox_north: Optional[float] = None
    bbox_east: Optional[float] = None


class GooglePlacesOutput(BaseModel):
    ok: bool
    requests_usados: int = 0
    requests_truncados: int = 0          # celdas que devolvieron 20 (se subdividieron)
    comercios_encontrados: int = 0       # distintos (dedup por place_id)
    comercios_vinculados: int = 0        # con parcela (ST_Contains)
    comercios_sin_parcela: int = 0       # fuera de toda parcela (calle, vereda)
    parcelas_con_comercio: int = 0
    total_uf_comercio: int = 0
    parcelas_uso_actualizado: int = 0
    costo_estimado_usd: float = 0.0
    cap_alcanzado: bool = False
    error: Optional[str] = None


# ── Telegram ──────────────────────────────────────────────────────────────────

def _tg(msg: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


# ── Idioma / zona ─────────────────────────────────────────────────────────────

def _detect_language(region_id: str) -> str:
    rid = region_id.lower()
    if rid.endswith("-br") or "-br-" in rid:
        return "pt-BR"
    if rid.endswith("-ar") or "-ar-" in rid:
        return "es-AR"
    return "es"


def _geom_to_polygon(g):
    """Polígono/multipolígono → geometría usable; ignora puntos/líneas."""
    if g is None or g.is_empty:
        return None
    if g.geom_type in ("Polygon", "MultiPolygon"):
        return g
    return None


def _load_zone(region_id: str,
               survey_id: Optional[str] = None) -> tuple[Optional[tuple], Optional[object]]:
    """Devuelve ((south, west, north, east), zone_polygon_or_None). Prefiere la
    subzona del survey (relevamiento parcial, migración 018) sobre la región."""
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT r.bbox_wkt, COALESCE(s.subzona_geojson, r.zone_geojson),
                   (s.subzona_geojson IS NOT NULL) AS es_subzona
            FROM regions r
            LEFT JOIN surveys s ON s.survey_id::text = :sid
                 AND s.region_id = r.region_id AND s.subzona_geojson IS NOT NULL
            WHERE r.region_id = :rid
        """), {"rid": region_id, "sid": str(survey_id or "")}).fetchone()
    if not row:
        return None, None
    if row[2]:
        logger.info("GooglePlaces: usando SUB-ZONA del survey (relevamiento parcial)")

    bbox_wkt, zone_geojson_str = row[0], row[1]
    zone_polygon = None
    if zone_geojson_str:
        try:
            import json
            gj = json.loads(zone_geojson_str)
            if gj.get("type") == "FeatureCollection":
                raw = [shape(f["geometry"]) for f in gj.get("features", []) if f.get("geometry")]
            elif gj.get("type") == "Feature":
                raw = [shape(gj["geometry"])]
            else:
                raw = [shape(gj)]
            polys = [_geom_to_polygon(g) for g in raw if g is not None]
            polys = [p for p in polys if p is not None]
            zone_polygon = unary_union(polys) if polys else None
        except Exception as e:
            logger.warning(f"GooglePlaces: no se pudo parsear zone_geojson: {e}")

    if zone_polygon is not None and not zone_polygon.is_empty:
        b = zone_polygon.bounds  # (west, south, east, north)
        return (b[1], b[0], b[3], b[2]), zone_polygon

    if bbox_wkt:
        try:
            from shapely import wkt as shp_wkt
            b = shp_wkt.loads(bbox_wkt).bounds
            return (b[1], b[0], b[3], b[2]), None
        except Exception as e:
            logger.warning(f"GooglePlaces: no se pudo parsear bbox_wkt: {e}")

    return None, None


def _get_latest_survey(region_id: str) -> Optional[str]:
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT survey_id::text FROM surveys
            WHERE region_id = :rid ORDER BY started_at DESC LIMIT 1
        """), {"rid": region_id}).fetchone()
    return row[0] if row else None


# ── Google Places (New) searchNearby ──────────────────────────────────────────

def _search_nearby(client: httpx.Client, api_key: str, lat: float, lng: float,
                   radius_m: float, included_types: list[str],
                   language: str) -> Optional[list[dict]]:
    """Una llamada searchNearby. Devuelve lista de places o None si error."""
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": _FIELD_MASK,
    }
    body = {
        "includedTypes": included_types,
        "maxResultCount": _MAX_RESULT_COUNT,
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": float(radius_m),
            }
        },
        "languageCode": language,
    }
    try:
        r = client.post(_PLACES_URL, headers=headers, json=body, timeout=20)
        if r.status_code != 200:
            logger.warning(f"Places status {r.status_code}: {r.text[:200]}")
            return None
        return r.json().get("places", [])
    except Exception as e:
        logger.warning(f"Error llamando Places: {e}")
        return None


def _m_to_deg(lat: float, dx_m: float, dy_m: float) -> tuple[float, float]:
    """Convierte metros a grados de lng/lat a una latitud dada."""
    dlat = dy_m / 111_320.0
    dlng = dx_m / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    return dlng, dlat


# ── Persistencia ──────────────────────────────────────────────────────────────

def _upsert_comercios(region_id: str, survey_id: str, places: dict[str, dict]) -> int:
    """Upsert por (region_id, place_id). Devuelve cantidad de places distintos."""
    if not places:
        return 0
    engine = get_engine()
    with engine.begin() as conn:
        for pid, pl in places.items():
            loc = pl.get("location", {})
            lat, lng = loc.get("latitude"), loc.get("longitude")
            if lat is None or lng is None:
                continue
            nombre = (pl.get("displayName") or {}).get("text")
            tipos = ",".join(pl.get("types", []) or [])
            conn.execute(text("""
                INSERT INTO comercios
                    (comercio_id, survey_id, region_id, place_id, nombre, rubro,
                     tipos, business_status, location, source)
                VALUES
                    (:cid, :sid, :rid, :pid, :nombre, :rubro, :tipos, :bs,
                     ST_SetSRID(ST_MakePoint(:lng, :lat), 4326), 'google_places')
                ON CONFLICT (region_id, place_id) DO UPDATE SET
                    survey_id = EXCLUDED.survey_id,
                    nombre = EXCLUDED.nombre,
                    rubro = EXCLUDED.rubro,
                    tipos = EXCLUDED.tipos,
                    business_status = EXCLUDED.business_status,
                    location = EXCLUDED.location
            """), {
                "cid": str(uuid.uuid4()), "sid": survey_id, "rid": region_id,
                "pid": pid, "nombre": nombre, "rubro": pl.get("primaryType"),
                "tipos": tipos, "bs": pl.get("businessStatus"),
                "lat": float(lat), "lng": float(lng),
            })
    return len(places)


def _link_to_parcelas(region_id: str) -> int:
    """Vincula cada comercio a la parcela que contiene su punto (ST_Contains)."""
    engine = get_engine()
    with engine.begin() as conn:
        res = conn.execute(text("""
            UPDATE comercios c SET parcela_id = p.parcela_id
            FROM parcelas p
            WHERE p.region_id = :rid
              AND c.region_id = :rid
              AND p.geometry IS NOT NULL
              AND c.location IS NOT NULL
              AND ST_Contains(p.geometry, c.location)
              AND (c.parcela_id IS NULL OR c.parcela_id <> p.parcela_id)
        """), {"rid": region_id})
        return res.rowcount or 0


def _aggregate_uf(region_id: str, survey_id: str, set_uso: bool) -> tuple[int, int, int]:
    """Escribe uf_comercio = #comercios por parcela. Devuelve
    (parcelas_con_comercio, total_uf_comercio, parcelas_uso_actualizado)."""
    engine = get_engine()
    with engine.begin() as conn:
        # Conteo por parcela (solo comercios operativos o sin estado declarado)
        counts = conn.execute(text("""
            SELECT c.parcela_id::text AS pid, COUNT(*) AS n
            FROM comercios c
            JOIN parcelas p ON p.parcela_id = c.parcela_id
            WHERE c.region_id = :rid
              AND c.parcela_id IS NOT NULL
              AND p.survey_id = :sid
              AND COALESCE(c.business_status, 'OPERATIONAL') <> 'CLOSED_PERMANENTLY'
            GROUP BY c.parcela_id
        """), {"rid": region_id, "sid": survey_id}).fetchall()

        parcelas_con = 0
        total_uf = 0
        uso_upd = 0
        for pid, n in counts:
            n = int(n)
            parcelas_con += 1
            total_uf += n
            # uf_comercio autoritativo (conteo real), recalcula total
            conn.execute(text("""
                UPDATE parcelas SET
                    uf_comercio = :n,
                    unidades_funcionales_estimadas = COALESCE(uf_vivienda, 0) + :n,
                    uf_fuente = 'google'
                WHERE parcela_id = :pid
            """), {"n": n, "pid": pid})

            if set_uso:
                # Parcela con comercio → comercial; si ya era residencial → mixto.
                res = conn.execute(text("""
                    UPDATE parcelas SET
                        uso_principal = CASE
                            WHEN uso_principal = 'residencial' THEN 'mixto'
                            WHEN uso_principal IN ('comercial', 'mixto') THEN uso_principal
                            ELSE 'comercial'
                        END,
                        uso_fuente = 'google'
                    WHERE parcela_id = :pid
                """), {"pid": pid})
                uso_upd += res.rowcount or 0

    return parcelas_con, total_uf, uso_upd


# ── Tesselado adaptativo (quadtree) ───────────────────────────────────────────

def _collect_places(
    client: httpx.Client, api_key: str, zone_bbox: tuple, zone_polygon,
    cell_size_m: float, min_cell_m: float, max_requests: int,
    included_types: list[str], language: str,
) -> tuple[dict[str, dict], int, int, bool]:
    """Recorre la zona con celdas adaptativas. Devuelve
    (places_por_id, requests_usados, requests_truncados, cap_alcanzado)."""
    south, west, north, east = zone_bbox
    places: dict[str, dict] = {}
    state = {"req": 0, "trunc": 0, "cap": False}

    # Genera la grilla inicial de centros de celda
    def iter_initial_cells():
        lat = south
        # paso en grados a la latitud media de la zona
        _, dlat = _m_to_deg((south + north) / 2, cell_size_m, cell_size_m)
        while lat < north + dlat:
            dlng, _ = _m_to_deg(lat, cell_size_m, cell_size_m)
            lng = west
            while lng < east + dlng:
                yield (lat + dlat / 2, lng + dlng / 2, cell_size_m)
                lng += dlng
            lat += dlat

    def visit(clat: float, clng: float, size_m: float) -> None:
        if state["cap"]:
            return
        # Saltar celdas que no intersectan el polígono de la zona
        if zone_polygon is not None:
            dlng, dlat = _m_to_deg(clat, size_m / 2, size_m / 2)
            cell_box = box(clng - dlng, clat - dlat, clng + dlng, clat + dlat)
            if not zone_polygon.intersects(cell_box):
                return
        if state["req"] >= max_requests:
            state["cap"] = True
            return

        radius = size_m * 0.71  # cubre la celda cuadrada con un círculo
        found = _search_nearby(client, api_key, clat, clng, radius, included_types, language)
        state["req"] += 1
        if found is None:
            return
        for pl in found:
            pid = pl.get("id")
            if pid:
                places.setdefault(pid, pl)

        # ¿Truncado? Subdividir si todavía hay margen de tamaño
        if len(found) >= _MAX_RESULT_COUNT and size_m / 2 >= min_cell_m:
            state["trunc"] += 1
            half = size_m / 2
            q = half / 2
            dlng, dlat = _m_to_deg(clat, q, q)
            for ddlat in (-dlat, dlat):
                for ddlng in (-dlng, dlng):
                    visit(clat + ddlat, clng + ddlng, half)

    for c in iter_initial_cells():
        visit(*c)
        if state["cap"]:
            break

    return places, state["req"], state["trunc"], state["cap"]


# ── Entry point ───────────────────────────────────────────────────────────────

@agent_run
def run(inp: GooglePlacesInput) -> GooglePlacesOutput:
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        return GooglePlacesOutput(ok=False, error="GOOGLE_MAPS_API_KEY no configurada")

    survey_id = inp.survey_id or _get_latest_survey(inp.region_id)
    if not survey_id:
        return GooglePlacesOutput(
            ok=False, error=f"Región '{inp.region_id}' sin survey. Creá un survey primero."
        )

    zone_bbox, zone_polygon = _load_zone(inp.region_id, survey_id)
    # Override de bbox si se pasó explícito
    if None not in (inp.bbox_south, inp.bbox_west, inp.bbox_north, inp.bbox_east):
        zone_bbox = (inp.bbox_south, inp.bbox_west, inp.bbox_north, inp.bbox_east)
        zone_polygon = None
    if zone_bbox is None:
        return GooglePlacesOutput(
            ok=False,
            error=f"Región '{inp.region_id}' sin bbox ni zone_geojson. Pasá bbox_* o creá la zona.",
        )

    if not inp.overwrite:
        engine = get_engine()
        with engine.connect() as conn:
            ya = conn.execute(text(
                "SELECT COUNT(*) FROM comercios WHERE region_id = :rid"
            ), {"rid": inp.region_id}).scalar() or 0
        if ya > 0:
            logger.info(f"GooglePlaces: ya hay {ya} comercios en '{inp.region_id}'. "
                        "Re-vinculando y agregando sin re-tesselar (usá overwrite=true para re-bajar).")
            vinc = _link_to_parcelas(inp.region_id)
            con, tot, uso = _aggregate_uf(inp.region_id, survey_id, inp.set_uso)
            return GooglePlacesOutput(
                ok=True, comercios_encontrados=ya, comercios_vinculados=vinc,
                parcelas_con_comercio=con, total_uf_comercio=tot,
                parcelas_uso_actualizado=uso, costo_estimado_usd=0.0,
            )

    included = inp.included_types or _INCLUDED_TYPES
    language = _detect_language(inp.region_id)

    logger.info(
        f"GooglePlaces '{inp.region_id}': tesselando bbox={zone_bbox} "
        f"celda={inp.cell_size_m}m (min {inp.min_cell_m}m), cap={inp.max_requests} req"
    )
    _tg(f"🏪 GooglePlaces <b>{inp.region_id}</b>: buscando comercios "
        f"(hasta {inp.max_requests} req ≈ USD {inp.max_requests * _COST_PER_REQUEST:.1f}).")

    try:
        with httpx.Client() as client:
            places, req, trunc, cap = _collect_places(
                client, api_key, zone_bbox, zone_polygon,
                inp.cell_size_m, inp.min_cell_m, inp.max_requests,
                included, language,
            )

        n_places = _upsert_comercios(inp.region_id, survey_id, places)
        vinc = _link_to_parcelas(inp.region_id)
        con, tot, uso = _aggregate_uf(inp.region_id, survey_id, inp.set_uso)
        sin_parcela = n_places - vinc if n_places >= vinc else 0
        costo = round(req * _COST_PER_REQUEST, 2)

        msg = (
            f"🏪 GooglePlaces <b>{inp.region_id}</b>: {n_places} comercios "
            f"({vinc} en parcela), {con} parcelas con comercio, "
            f"{tot} UF de comercio. {req} req ≈ USD {costo}."
            + ("\n⚠️ Cap de requests alcanzado — cobertura parcial." if cap else "")
        )
        _tg(msg)
        logger.info(msg.replace("<b>", "").replace("</b>", ""))

        return GooglePlacesOutput(
            ok=True,
            requests_usados=req,
            requests_truncados=trunc,
            comercios_encontrados=n_places,
            comercios_vinculados=vinc,
            comercios_sin_parcela=sin_parcela,
            parcelas_con_comercio=con,
            total_uf_comercio=tot,
            parcelas_uso_actualizado=uso,
            costo_estimado_usd=costo,
            cap_alcanzado=cap,
        )

    except Exception as e:
        logger.exception("GooglePlacesFetcher falló")
        _tg(f"❌ GooglePlaces <b>{inp.region_id}</b> falló: {e}")
        return GooglePlacesOutput(ok=False, error=str(e))
