"""UsoClassifier — clasifica parcelas como residencial/comercial/mixto.

Paso 1 (ARBA): arba_carto_fetcher deja en `uf_vivienda` la UF TOTAL del lote
               (subparcelas ≥ COCHERA_M2). Ojo: ARBA NO dice el destino de cada
               subparcela — el campo `sp` es el número de subparcela, no el uso —
               así que de ARBA sale el CUÁNTAS, nunca el vivienda-vs-comercio.
Paso 2 (Google Places): única señal de comercio (radio 15 m ≈ la propia parcela).
               Lo que confirma se DESCUENTA del total de ARBA, no se suma encima.

Este agente es el que persiste el reparto final en uf_vivienda/uf_comercio
(`uf_fuente='clasificador'`) además de uso_principal: es el último paso de UF del
flujo PBA, y sin él la web y el CSV muestran 0 UF.
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
from scrapitero.agents.precedencia import UF_FUENTES_PROTEGIDAS as _UF_FUENTES_PROTEGIDAS


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


def _desglose_uf(uf_vivienda_arba: int, uf_comercio_arba: int,
                 comercios_places: int) -> tuple[int, int, str]:
    """
    Reparte la UF del lote entre vivienda y comercio, y deriva uso_principal.

    ARBA no dice el DESTINO de cada subparcela, así que ARBACartoFetcher carga la
    UF TOTAL del lote en `uf_vivienda`. Places (radio 15 m ≈ la propia parcela) es
    lo único que distingue comercio, así que la parte comercial se DESCUENTA de ese
    total en vez de sumarse encima: una casa con local al frente sigue teniendo las
    UF que declara ARBA, no una más.

    `comercios_places = -1` significa que la API no respondió: no se toca el reparto.

    Devuelve (uf_vivienda, uf_comercio, uso_principal).
    """
    total = uf_vivienda_arba + uf_comercio_arba

    if comercios_places > 0:
        # Sin UF de ARBA (parcela sin subparcelas) el comercio es lo único que hay.
        uf_comercio = comercios_places if total == 0 else min(comercios_places, total)
    else:
        uf_comercio = uf_comercio_arba

    uf_vivienda = max(total - uf_comercio, 0)

    if uf_vivienda + uf_comercio == 0:
        uso = "sin_datos"
    elif uf_comercio == 0:
        uso = "residencial"
    elif uf_vivienda == 0:
        uso = "comercial"
    else:
        uso = "mixto"

    return uf_vivienda, uf_comercio, uso


@agent_run
def run(input: ClassifierInput) -> ClassifierOutput:
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        return ClassifierOutput(ok=False, error="GOOGLE_MAPS_API_KEY no configurada")

    engine = get_engine()

    with engine.begin() as conn:
        q = """
            SELECT parcela_id::text, centroid_lat, centroid_lng,
                   -- Total de UF del lote. Si uf_vivienda/uf_comercio están sin
                   -- poblar (survey corrido con la versión de ARBACartoFetcher que
                   -- sólo escribía unidades_funcionales_estimadas), se cae a ese
                   -- campo en vez de clasificar todo como sin_datos.
                   CASE WHEN COALESCE(uf_vivienda, 0) + COALESCE(uf_comercio, 0) = 0
                        THEN COALESCE(unidades_funcionales_estimadas, 0)
                        ELSE COALESCE(uf_vivienda, 0) END AS uf_vivienda,
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
            parcela_id, lat, lng, uf_vivienda_arba, uf_comercio_arba = row

            comercios_places = _places_comercios(lat, lng, api_key, client)
            uf_vivienda, uf_comercio, uso = _desglose_uf(
                uf_vivienda_arba, uf_comercio_arba, comercios_places)
            counts[uso] += 1
            procesadas += 1

            with engine.begin() as conn:
                # `manual` = corrección del operador desde el panel de incidencias.
                # Es el único origen irreconstruible, así que nunca se pisa (ver la regla de
                # precedencia de fuentes en CLAUDE.md). El reparto se persiste además del
                # uso: sin esto la UF de ARBA no llegaba a la web ni al CSV.
                conn.execute(text(
                    "UPDATE parcelas SET uso_principal = :uso, uso_fuente = 'clasificador' "
                    "WHERE parcela_id = :pid AND COALESCE(uso_fuente, '') <> 'manual'"
                ), {"uso": uso, "pid": parcela_id})
                conn.execute(text(
                    "UPDATE parcelas SET uf_vivienda = :viv, uf_comercio = :com, "
                    "uf_fuente = 'clasificador' "
                    "WHERE parcela_id = :pid "
                    f"AND COALESCE(uf_fuente, '') NOT IN {_UF_FUENTES_PROTEGIDAS}"
                ), {"viv": uf_vivienda, "com": uf_comercio, "pid": parcela_id})

            logger.debug(
                f"Parcela {parcela_id[:8]}… ARBA(total={uf_vivienda_arba + uf_comercio_arba}) "
                f"Places={comercios_places} → {uso} (v={uf_vivienda},c={uf_comercio})"
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
