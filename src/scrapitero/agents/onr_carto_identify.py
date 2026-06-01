"""ONRCartoIdentify — identifica el cartório responsable de una coordenada.

Fuente: gis-mapas.onr.org.br → Hosted/competencias_registrais_hml/FeatureServer/0
Devuelve el CNS (Código Nacional de Serventia) y nombre del cartório para el punto.

Ejemplo para Várzea Grande:
  lat=-15.65 lng=-56.10 → CNS=063446, "1º Registro de Imóveis de Várzea Grande"

Útil como fuente alternativa para identificar qué cartório registra una parcela
cuando SmartGIS no tiene cobertura para esa área.
"""

from __future__ import annotations

import json
import math
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel

from scrapitero.agents.onr_token import get_token

GIS_BASE = "https://gis-mapas.onr.org.br/onrgisserver/rest/services/Hosted"
SERVICE = "competencias_registrais_hml/FeatureServer/0"


class CartoInput(BaseModel):
    lat: float
    lng: float


class CartoOutput(BaseModel):
    ok: bool
    cns: Optional[str] = None
    cartorio: Optional[str] = None
    comarca: Optional[str] = None
    uf: Optional[str] = None
    abrangencia: Optional[str] = None
    error: Optional[str] = None


def _to_webmerc(lat: float, lng: float) -> tuple[float, float]:
    x = lng * 20037508.34 / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) / (math.pi / 180)
    return x, y * 20037508.34 / 180


def run(input: CartoInput) -> CartoOutput:
    try:
        token = get_token()
    except Exception as e:
        return CartoOutput(ok=False, error=f"Token ONR: {e}")

    x, y = _to_webmerc(input.lat, input.lng)
    radius = 500  # metros en Web Mercator
    bbox = {
        "xmin": x - radius, "ymin": y - radius,
        "xmax": x + radius, "ymax": y + radius,
        "spatialReference": {"wkid": 102100},
    }

    try:
        r = httpx.get(
            f"{GIS_BASE}/{SERVICE}/query",
            params={
                "f": "json",
                "token": token,
                "geometry": json.dumps(bbox),
                "geometryType": "esriGeometryEnvelope",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "codigo_cns,cartorio,comarca,uf,abrangen",
                "returnGeometry": "false",
                "resultRecordCount": 1,
            },
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 498:
            token = get_token(force_refresh=True)
            r = httpx.get(f"{GIS_BASE}/{SERVICE}/query",
                          params={"f":"json","token":token,
                                  "geometry":json.dumps(bbox),
                                  "geometryType":"esriGeometryEnvelope",
                                  "spatialRel":"esriSpatialRelIntersects",
                                  "outFields":"codigo_cns,cartorio,comarca,uf,abrangen",
                                  "returnGeometry":"false","resultRecordCount":1},
                          timeout=15, headers={"User-Agent":"Mozilla/5.0"})
        else:
            return CartoOutput(ok=False, error=str(e))
    except Exception as e:
        return CartoOutput(ok=False, error=str(e))

    features = r.json().get("features", [])
    if not features:
        return CartoOutput(ok=False, error="Sin cobertura ONR para estas coordenadas")

    attrs = features[0].get("attributes", {})
    logger.info(f"ONR: {attrs.get('cartorio')} (CNS {attrs.get('codigo_cns')})")
    return CartoOutput(
        ok=True,
        cns=str(attrs.get("codigo_cns") or ""),
        cartorio=str(attrs.get("cartorio") or ""),
        comarca=str(attrs.get("comarca") or ""),
        uf=str(attrs.get("uf") or ""),
        abrangencia=str(attrs.get("abrangen") or ""),
    )
