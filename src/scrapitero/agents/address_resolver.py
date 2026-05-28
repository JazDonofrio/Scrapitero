"""AddressResolver — reverse geocoding de parcelas sin dirección via Google Maps API.

Para cada parcela que tenga coordenadas (centroid_lat/lng) pero no tenga dirección
(calle IS NULL), llama a la Geocoding API de Google Maps y persiste la dirección
descompuesta en la tabla `parcelas`.

Requiere variable de entorno: GOOGLE_MAPS_API_KEY
"""

from __future__ import annotations

import os
import time
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.db.engine import get_engine


# ── Pydantic I/O ──────────────────────────────────────────────────────────────

class AddressResolverInput(BaseModel):
    region_id: str                          # "vg-mt-br"
    survey_id: Optional[str] = None        # UUID — si se omite procesa toda la región
    batch_size: int = 100                  # máximo de parcelas por corrida
    delay_ms: int = 50                     # delay entre llamadas para no superar quota


class AddressResolverOutput(BaseModel):
    ok: bool
    parcelas_procesadas: int               # total encontradas sin dirección
    parcelas_resueltas: int                # con dirección encontrada por Google
    parcelas_sin_resultado: int            # sin resultado en Google
    costo_estimado_usd: float              # ~0.005 USD por llamada (SKU: Geocoding)
    error: Optional[str] = None


# ── Componentes de dirección Google Maps ─────────────────────────────────────

_COMPONENT_MAP = {
    "route":                        "calle",
    "street_number":                "numero",
    "sublocality_level_1":          "barrio",
    "sublocality":                  "barrio",
    "neighborhood":                 "barrio",
    "administrative_area_level_2":  "municipio",
    "administrative_area_level_1":  "estado_provincia",
    "country":                      "pais",
    "postal_code":                  "codigo_postal",
}


def _parse_components(components: list[dict]) -> dict:
    """Extrae un dict con calle, numero, barrio, etc. desde los components de Google."""
    result: dict[str, str] = {}
    for comp in components:
        for gtype in comp.get("types", []):
            field = _COMPONENT_MAP.get(gtype)
            if field and field not in result:
                result[field] = comp.get("long_name", "")
    return result


def _reverse_geocode(lat: float, lng: float, api_key: str) -> Optional[dict]:
    """
    Llama a Google Maps Geocoding API con lat/lng.
    Devuelve el primer resultado o None si no hay resultados.
    Usa language=pt-BR para nombres en portugués (contexto Brasil).
    """
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "latlng": f"{lat},{lng}",
        "key": api_key,
        "language": "pt-BR",
        "result_type": "street_address|premise|subpremise",
    }
    try:
        with httpx.Client(timeout=15) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
        data = r.json()
        if data.get("status") == "OK" and data.get("results"):
            return data["results"][0]
        if data.get("status") not in ("OK", "ZERO_RESULTS"):
            logger.warning(f"Google Maps status inesperado: {data.get('status')} — {data.get('error_message','')}")
        return None
    except Exception as e:
        logger.warning(f"Error llamando Google Maps: {e}")
        return None


# ── Consulta y actualización en DB ────────────────────────────────────────────

def _fetch_parcelas_sin_direccion(
    engine, region_id: str, survey_id: Optional[str], batch_size: int
) -> list[dict]:
    """Devuelve parcelas con coordenadas pero sin calle asignada."""
    with engine.connect() as conn:
        q = """
            SELECT parcela_id::text, centroid_lat, centroid_lng
            FROM parcelas
            WHERE region_id = :region
              AND centroid_lat IS NOT NULL
              AND centroid_lng IS NOT NULL
              AND (calle IS NULL OR calle = '')
        """
        params: dict = {"region": region_id}
        if survey_id:
            q += " AND survey_id = :survey_id"
            params["survey_id"] = survey_id
        q += " LIMIT :batch"
        params["batch"] = batch_size

        rows = conn.execute(text(q), params).fetchall()
        return [{"parcela_id": r[0], "lat": r[1], "lng": r[2]} for r in rows]


def _update_parcela_direccion(conn, parcela_id: str, addr: dict) -> None:
    """Actualiza los campos de dirección de una parcela."""
    conn.execute(text("""
        UPDATE parcelas SET
            calle              = :calle,
            numero             = :numero,
            barrio             = :barrio,
            municipio          = :municipio,
            estado_provincia   = :estado,
            pais               = :pais,
            codigo_postal      = :cp,
            direccion_source   = 'google_maps',
            direccion_confidence = 0.85
        WHERE parcela_id = :pid
    """), {
        "calle":   addr.get("calle"),
        "numero":  addr.get("numero"),
        "barrio":  addr.get("barrio"),
        "municipio": addr.get("municipio"),
        "estado":  addr.get("estado_provincia"),
        "pais":    addr.get("pais"),
        "cp":      addr.get("codigo_postal"),
        "pid":     parcela_id,
    })


# ── Entry point ───────────────────────────────────────────────────────────────

def run(input: AddressResolverInput) -> AddressResolverOutput:
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        return AddressResolverOutput(
            ok=False, parcelas_procesadas=0, parcelas_resueltas=0,
            parcelas_sin_resultado=0, costo_estimado_usd=0.0,
            error="Variable GOOGLE_MAPS_API_KEY no configurada en .env"
        )

    engine = get_engine()

    try:
        parcelas = _fetch_parcelas_sin_direccion(
            engine, input.region_id, input.survey_id, input.batch_size
        )
        logger.info(f"Parcelas sin dirección encontradas: {len(parcelas)}")

        resueltas = 0
        sin_resultado = 0

        with engine.begin() as conn:
            for p in parcelas:
                resultado = _reverse_geocode(p["lat"], p["lng"], api_key)

                if resultado:
                    addr = _parse_components(resultado.get("address_components", []))
                    if addr.get("calle"):
                        _update_parcela_direccion(conn, p["parcela_id"], addr)
                        resueltas += 1
                        logger.debug(
                            f"✓ {p['parcela_id'][:8]}… → "
                            f"{addr.get('calle', '')} {addr.get('numero', '')}"
                        )
                    else:
                        # Resultado existe pero no tiene nombre de calle (ej: zona rural)
                        sin_resultado += 1
                else:
                    sin_resultado += 1

                if input.delay_ms > 0:
                    time.sleep(input.delay_ms / 1000)

        costo = len(parcelas) * 0.005  # USD por llamada Geocoding API

        logger.info(
            f"AddressResolver: {resueltas} resueltas, {sin_resultado} sin resultado. "
            f"Costo estimado: USD {costo:.2f}"
        )

        return AddressResolverOutput(
            ok=True,
            parcelas_procesadas=len(parcelas),
            parcelas_resueltas=resueltas,
            parcelas_sin_resultado=sin_resultado,
            costo_estimado_usd=round(costo, 4),
        )

    except Exception as e:
        logger.exception("AddressResolver falló")
        return AddressResolverOutput(
            ok=False, parcelas_procesadas=0, parcelas_resueltas=0,
            parcelas_sin_resultado=0, costo_estimado_usd=0.0,
            error=str(e)
        )
