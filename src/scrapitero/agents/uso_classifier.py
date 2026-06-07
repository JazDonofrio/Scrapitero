"""UsoClassifier — clasifica parcelas como residencial/comercial/mixto.

Paso 1 (ARBA): la clasificación viene del campo sp de arba_carto_fetcher
               (uf_vivienda y uf_comercio ya guardados en parcelas).
Paso 2 (Google Places): valida con negocios reales activos en el lugar.
               Si Places encuentra comercios donde ARBA no los vio → ajusta.
               Si ARBA vio comercios pero Places no → confía en ARBA.
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
from scrapitero.agents._run import agent_run


PLACES_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
PLACES_RADIUS_M = 15

COMMERCIAL_TYPES = {
    "store", "restaurant", "bar", "cafe", "food", "bakery", "pharmacy",
    "supermarket", "shopping_mall", "clothing_store", "electronics_store",
    "furniture_store", "hardware_store", "jewelry_store", "shoe_store",
    "beauty_salon", "hair_care", "spa", "gym", "laundry",
    "bank", "atm", "insurance_agency", "real_estate_agency", "lawyer",
    "accounting", "doctor", "dentist", "hospital", "veterinary_care",
    "travel_agency", "car_dealer", "car_repair", "gas_station",
    "lodging", "night_club",
    "establishment",  # genérico de negocio activo
}

# No cuentan como "comercio" para nuestro propósito
EXCLUDE_TYPES = {"point_of_interest", "premise", "street_address", "route"}


class ClassifierInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None
    delay_ms: int = 500         # delay entre requests a Places
    batch_notify: int = 10      # avisar cada N parcelas procesadas


class ClassifierOutput(BaseModel):
    ok: bool
    parcelas_procesadas: int = 0
    residencial: int = 0
    comercial: int = 0
    mixto: int = 0
    sin_datos: int = 0
    error: Optional[str] = None


def _places_comercios(lat: float, lng: float, api_key: str, client: httpx.Client) -> int:
    """Cuenta establecimientos comerciales en un radio de PLACES_RADIUS_M metros."""
    try:
        r = client.get(PLACES_URL, params={
            "location": f"{lat},{lng}",
            "radius": PLACES_RADIUS_M,
            "key": api_key,
        }, timeout=10)
        r.raise_for_status()
        results = r.json().get("results", [])
        count = 0
        for place in results:
            types = set(place.get("types", []))
            if types & COMMERCIAL_TYPES and not (types <= EXCLUDE_TYPES):
                count += 1
        return count
    except Exception as e:
        logger.warning(f"Places API error en ({lat},{lng}): {e}")
        return -1  # -1 = no se pudo consultar


def _uso_final(uf_vivienda: int, uf_comercio_arba: int, comercios_places: int) -> str:
    """
    Combina ARBA + Google Places para determinar uso_principal.

    Reglas:
    - Si Places encuentra comercios Y ARBA no los vio → ajustar a mixto/comercial
    - Si ARBA vio comercios Y Places no → confiar en ARBA (puede estar sin ficha)
    - Si ambos coinciden → usar esa clasificación
    """
    uf_comercio = uf_comercio_arba

    # Places encontró comercios que ARBA no clasificó → sumar
    if comercios_places > 0 and uf_comercio_arba == 0:
        uf_comercio = comercios_places

    total = uf_vivienda + uf_comercio
    if total == 0:
        return "sin_datos"
    if uf_comercio == 0:
        return "residencial"
    if uf_vivienda == 0:
        return "comercial"
    return "mixto"


@agent_run
def run(input: ClassifierInput) -> ClassifierOutput:
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        return ClassifierOutput(ok=False, error="GOOGLE_MAPS_API_KEY no configurada")

    engine = get_engine()

    with engine.begin() as conn:
        q = """
            SELECT parcela_id::text, centroid_lat, centroid_lng,
                   COALESCE(uf_vivienda, 0) AS uf_vivienda,
                   COALESCE(uf_comercio, 0) AS uf_comercio
            FROM parcelas
            WHERE region_id = :region
              AND centroid_lat IS NOT NULL
        """
        params: dict = {"region": input.region_id}
        if input.survey_id:
            q += " AND survey_id = :sid"
            params["sid"] = input.survey_id

        parcelas = conn.execute(text(q), params).fetchall()

    if not parcelas:
        return ClassifierOutput(ok=False, error="No hay parcelas con coordenadas para clasificar")

    logger.info(f"Clasificando uso en {len(parcelas)} parcelas...")

    counts = {"residencial": 0, "comercial": 0, "mixto": 0, "sin_datos": 0}
    procesadas = 0

    with httpx.Client() as client:
        for row in parcelas:
            parcela_id, lat, lng, uf_vivienda, uf_comercio_arba = row

            comercios_places = _places_comercios(lat, lng, api_key, client)
            uso = _uso_final(uf_vivienda, uf_comercio_arba, comercios_places)
            counts[uso] += 1
            procesadas += 1

            with engine.begin() as conn:
                conn.execute(text(
                    "UPDATE parcelas SET uso_principal = :uso, uso_fuente = 'clasificador' "
                    "WHERE parcela_id = :pid"
                ), {"uso": uso, "pid": parcela_id})

            logger.debug(
                f"Parcela {parcela_id[:8]}… ARBA(v={uf_vivienda},c={uf_comercio_arba}) "
                f"Places={comercios_places} → {uso}"
            )

            if procesadas % input.batch_notify == 0:
                logger.info(
                    f"Progreso: {procesadas}/{len(parcelas)} parcelas — "
                    f"residencial={counts['residencial']} mixto={counts['mixto']} "
                    f"comercial={counts['comercial']}"
                )

            time.sleep(input.delay_ms / 1000)

    return ClassifierOutput(
        ok=True,
        parcelas_procesadas=procesadas,
        residencial=counts["residencial"],
        comercial=counts["comercial"],
        mixto=counts["mixto"],
        sin_datos=counts["sin_datos"],
    )
