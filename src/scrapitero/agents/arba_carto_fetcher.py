"""ARBACartoFetcher — descarga parcelas desde el portal Carto de ARBA.

Fuente: https://carto.arba.gov.ar/cartoArba/
App Java con sesión jsessionid. Los endpoints requieren cookie activa.

Flujo:
  1. Intentar con cookies guardadas en ARBA_SESSION_FILE
  2. Si falla (no cookies / sesión expirada) → devolver needs_cookies=True
  3. Hermes pide las cookies al usuario por Telegram
  4. El usuario ejecuta este agente con las cookies nuevas (campo `jsessionid`)
  5. Las cookies se guardan y se usa el resultado

El agente intenta varios endpoints conocidos del portal (fallback automático).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from shapely.geometry import shape
from shapely.ops import transform as shp_transform
import pyproj
from sqlalchemy import text

from scrapitero.db.engine import get_engine


# ── Configuración ─────────────────────────────────────────────────────────────

CARTO_BASE = "https://carto.arba.gov.ar/cartoArba"
ARBA_SESSION_FILE = Path(os.environ.get("ARBA_SESSION_FILE",
                                         "/opt/scrapitero/.arba_session.json"))

# Endpoints a intentar en orden (el servidor puede cambiarlos sin aviso)
SEARCH_ENDPOINTS = [
    "/getParcelasByNomenclatura",
    "/BusquedaParcelaAction",
    "/busquedaNomenclatura",
    "/SearchParcelaAction",
    "/parcela/buscar",
    "/rest/parcela/nomenclatura",
]


# ── Pydantic I/O ──────────────────────────────────────────────────────────────

class ARBACartoInput(BaseModel):
    region_id: str                          # "ituzaingo-ba-ar"
    survey_id: str
    partido_id: str                         # "136"
    circunscripcion: Optional[str] = None   # "2"
    seccion: Optional[str] = None           # "C"
    manzana: Optional[str] = None           # "184"
    parcela: Optional[str] = None           # número de parcela (opcional)

    # Si el usuario envió el jsessionid por Telegram, pasarlo acá
    jsessionid: Optional[str] = None

    # Alternativa: pegar el header Cookie completo copiado de DevTools
    cookie_header: Optional[str] = None


class ARBACartoOutput(BaseModel):
    ok: bool
    parcelas_insertadas: int = 0
    parcelas_actualizadas: int = 0
    endpoint_usado: Optional[str] = None
    fuentes: list[str] = []

    # Señal para Hermes: necesita que el usuario provea la sesión
    needs_cookies: bool = False
    cookie_instructions: Optional[str] = None

    error: Optional[str] = None


# ── Manejo de cookies ─────────────────────────────────────────────────────────

def _load_session() -> Optional[str]:
    """Carga jsessionid guardado en disco. Devuelve None si no existe."""
    if ARBA_SESSION_FILE.exists():
        try:
            data = json.loads(ARBA_SESSION_FILE.read_text())
            return data.get("jsessionid")
        except Exception:
            pass
    return None


def _save_session(jsessionid: str) -> None:
    """Persiste jsessionid en disco para reutilización."""
    ARBA_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    ARBA_SESSION_FILE.write_text(json.dumps({"jsessionid": jsessionid}))
    logger.info(f"Sesión ARBA guardada en {ARBA_SESSION_FILE}")


def _build_cookies(jsessionid: str) -> dict:
    return {"JSESSIONID": jsessionid}


def _parse_cookie_header(header: str) -> Optional[str]:
    """Extrae JSESSIONID del header Cookie completo (copiado de DevTools)."""
    for part in header.split(";"):
        part = part.strip()
        if part.upper().startswith("JSESSIONID="):
            return part.split("=", 1)[1].strip()
    return None


# ── Parámetros de búsqueda ────────────────────────────────────────────────────

def _search_params(input: ARBACartoInput) -> dict:
    params: dict = {"partido": input.partido_id.zfill(3)}
    if input.circunscripcion:
        params["circuns"] = input.circunscripcion.strip()
    if input.seccion:
        params["seccion"] = input.seccion.strip().upper()
    if input.manzana:
        params["manzana"] = input.manzana.strip().zfill(4)
    if input.parcela:
        params["parcela"] = input.parcela.strip().zfill(4)
    return params


# ── Descubrimiento de endpoint ────────────────────────────────────────────────

def _try_endpoints(params: dict, cookies: dict) -> tuple[Optional[str], Optional[list]]:
    """
    Intenta cada endpoint conocido con GET y POST.
    Devuelve (endpoint_url, features_list) o (None, None) si todos fallan.
    """
    headers = {
        "Accept": "application/json, text/javascript, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{CARTO_BASE}/",
    }

    with httpx.Client(timeout=30, follow_redirects=False, cookies=cookies,
                      headers=headers) as client:
        for path in SEARCH_ENDPOINTS:
            url = CARTO_BASE + path

            # Intentar GET
            try:
                r = client.get(url, params=params)
                if r.status_code == 200 and r.text.strip():
                    data = _parse_response(r)
                    if data is not None:
                        logger.info(f"Endpoint encontrado: GET {url}")
                        return url, data
            except Exception as e:
                logger.debug(f"GET {url} → {e}")

            # Intentar POST
            try:
                r = client.post(url, data=params)
                if r.status_code == 200 and r.text.strip():
                    data = _parse_response(r)
                    if data is not None:
                        logger.info(f"Endpoint encontrado: POST {url}")
                        return url, data
            except Exception as e:
                logger.debug(f"POST {url} → {e}")

    return None, None


def _parse_response(r: httpx.Response) -> Optional[list]:
    """
    Intenta parsear la respuesta como GeoJSON features o lista de dicts.
    Devuelve lista de features o None si no es parseable como datos de parcelas.
    """
    ct = r.headers.get("content-type", "")
    try:
        data = r.json()
    except Exception:
        # Puede ser XML/GML — ignorar por ahora
        logger.debug(f"Respuesta no es JSON: {r.text[:100]}")
        return None

    # GeoJSON FeatureCollection
    if isinstance(data, dict):
        if data.get("type") == "FeatureCollection":
            return data.get("features", [])
        # Puede ser {"parcelas": [...]} u otro wrapper
        for key in ("features", "parcelas", "results", "data"):
            if key in data and isinstance(data[key], list):
                return data[key]
        # Dict único con geometría → envolver en lista
        if "geometry" in data:
            return [data]

    if isinstance(data, list) and len(data) > 0:
        return data

    return None


# ── Upsert en DB ──────────────────────────────────────────────────────────────

def _upsert_parcelas(features: list, region_id: str, survey_id: str) -> tuple[int, int]:
    engine = get_engine()
    insertadas = 0
    actualizadas = 0

    def _area(geom) -> Optional[float]:
        try:
            proj = pyproj.Transformer.from_crs(
                "EPSG:4326", "EPSG:32721", always_xy=True
            ).transform
            return round(shp_transform(proj, geom).area, 2)
        except Exception:
            return None

    with engine.begin() as conn:
        for feat in features:
            # Soportar GeoJSON feature o dict plano con geometry
            if isinstance(feat, dict) and "geometry" in feat:
                geom_raw = feat["geometry"]
                props = feat.get("properties") or feat
            else:
                continue

            if not geom_raw:
                continue

            try:
                geom = shape(geom_raw)
            except Exception:
                continue

            centroid = geom.centroid
            lat, lng = round(centroid.y, 7), round(centroid.x, 7)
            area = _area(geom)
            geom_wkt = geom.wkt

            # Dedup por coordenadas del centroide
            existing = conn.execute(text("""
                SELECT parcela_id FROM parcelas
                WHERE region_id = :region
                  AND fuente_parcela = 'arba_carto'
                  AND ABS(centroid_lat - :lat) < 0.0001
                  AND ABS(centroid_lng - :lng) < 0.0001
            """), {"region": region_id, "lat": lat, "lng": lng}).fetchone()

            if existing:
                conn.execute(text("""
                    UPDATE parcelas SET
                        geometry = ST_GeomFromText(:geom, 4326),
                        area_m2_terreno = :area
                    WHERE parcela_id = :pid
                """), {"geom": geom_wkt, "area": area, "pid": str(existing[0])})
                actualizadas += 1
            else:
                new_id = uuid.uuid4()
                conn.execute(text("""
                    INSERT INTO parcelas
                        (parcela_id, survey_id, region_id,
                         geometry, centroid_lat, centroid_lng,
                         area_m2_terreno, fuente_parcela)
                    VALUES
                        (:pid, :sid, :region,
                         ST_GeomFromText(:geom, 4326), :lat, :lng,
                         :area, 'arba_carto')
                """), {
                    "pid": str(new_id), "sid": survey_id, "region": region_id,
                    "geom": geom_wkt, "lat": lat, "lng": lng, "area": area,
                })
                insertadas += 1

    return insertadas, actualizadas


# ── Entry point ───────────────────────────────────────────────────────────────

COOKIE_INSTRUCTIONS = (
    "Necesito que me pases el JSESSIONID de carto.arba.gov.ar.\n\n"
    "Pasos:\n"
    "1. Abrí Chrome → https://carto.arba.gov.ar/cartoArba/\n"
    "2. Abrí DevTools (F12) → pestaña Network\n"
    "3. Hacé una búsqueda: Partido=136, Circunscripción=2, Sección=C, Manzana=184\n"
    "4. Buscá en la lista de requests uno que diga 'getParcelasByNomenclatura' "
    "o similar (tipo Fetch/XHR)\n"
    "5. Click en esa request → Headers → Request Headers → copiá el valor de 'Cookie:'\n"
    "6. Enviame el valor copiado por acá\n\n"
    "Ejemplo: JSESSIONID=ABC123DEF456"
)


def run(input: ARBACartoInput) -> ARBACartoOutput:
    # Resolver cookies: input > disco
    jsessionid = input.jsessionid

    if not jsessionid and input.cookie_header:
        jsessionid = _parse_cookie_header(input.cookie_header)
        if not jsessionid:
            logger.warning("No se pudo extraer JSESSIONID del cookie_header")

    if not jsessionid:
        jsessionid = _load_session()

    if not jsessionid:
        logger.info("No hay sesión ARBA disponible → needs_cookies")
        return ARBACartoOutput(
            ok=False,
            needs_cookies=True,
            cookie_instructions=COOKIE_INSTRUCTIONS,
            error="Sin sesión activa de carto.arba.gov.ar"
        )

    # Guardar/renovar la sesión en disco
    _save_session(jsessionid)
    cookies = _build_cookies(jsessionid)
    params = _search_params(input)

    try:
        endpoint, features = _try_endpoints(params, cookies)

        if endpoint is None:
            # Puede ser sesión expirada o endpoint desconocido
            logger.warning("Ningún endpoint respondió con datos")
            # Borrar sesión guardada para forzar nuevo handshake
            if ARBA_SESSION_FILE.exists():
                ARBA_SESSION_FILE.unlink()
            return ARBACartoOutput(
                ok=False,
                needs_cookies=True,
                cookie_instructions=(
                    "La sesión expiró o el endpoint cambió.\n"
                    + COOKIE_INSTRUCTIONS
                ),
                error="Sesión expirada o endpoints desconocidos"
            )

        if not features:
            return ARBACartoOutput(
                ok=True,
                parcelas_insertadas=0,
                parcelas_actualizadas=0,
                endpoint_usado=endpoint,
                fuentes=["arba_carto"],
                error="El endpoint respondió pero no devolvió parcelas. Verificar parámetros."
            )

        insertadas, actualizadas = _upsert_parcelas(features, input.region_id, input.survey_id)

        return ARBACartoOutput(
            ok=True,
            parcelas_insertadas=insertadas,
            parcelas_actualizadas=actualizadas,
            endpoint_usado=endpoint,
            fuentes=["arba_carto"],
        )

    except Exception as e:
        logger.exception("ARBACartoFetcher falló")
        return ARBACartoOutput(ok=False, error=str(e))
