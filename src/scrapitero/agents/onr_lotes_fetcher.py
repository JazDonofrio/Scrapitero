"""ONRLotesFetcher — descarga lotes urbanos de ciudades con cobertura en ONR.

Fuente: gis-mapas.onr.org.br ArcGIS REST → servicios de parcelamento do solo.
Routing geográfico: dada una coordenada, detecta automáticamente qué ciudad usar.
Ciudades con cobertura: São Paulo, Rio de Janeiro, Fortaleza, Recife, BH, Curitiba,
  Manaus, João Pessoa, Natal, Florianópolis, Campo Grande, Niterói, Santa Maria,
  Rio Branco, São Bernardo do Campo, Maringá.
"""

from __future__ import annotations

import time
import uuid
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from shapely.geometry import shape
from sqlalchemy import text

from scrapitero.db.engine import get_engine
from scrapitero.agents.onr_token import get_token
from scrapitero.agents.onr_sigef_fetcher import _wgs84_to_webmerc, _bbox_webmerc

GIS_BASE = "https://gis-mapas.onr.org.br/onrgisserver/rest/services/Hosted"
MAX_RECORDS = 500

# Ciudades con lotes disponibles en ONR
# bbox_wgs84: (south, west, north, east)
CIDADES = {
    "sp_capital": {
        "nome": "São Paulo (Capital)", "uf": "SP",
        "service": "sp_capital_lotes_20221227_0849_v2", "layer": 0,
        "bbox": (-23.70, -46.83, -23.43, -46.37),
        "campo_id": "st_qd_lo", "campo_area": "area_m2",
    },
    "rj_capital": {
        "nome": "Rio de Janeiro (Capital)", "uf": "RJ",
        "service": "parcelamento_de_solo_rj_lotes_2013_3", "layer": 0,
        "bbox": (-23.08, -43.80, -22.75, -43.10),
        "campo_id": None, "campo_area": "SHAPE__Area",
    },
    "fortaleza": {
        "nome": "Fortaleza", "uf": "CE",
        "service": "ce_fortaleza_Lotes", "layer": 0,
        "bbox": (-3.90, -38.65, -3.71, -38.42),
        "campo_id": None, "campo_area": "SHAPE__Area",
    },
    "recife": {
        "nome": "Recife", "uf": "PE",
        "service": "parcelamento_pe_recife_lotes", "layer": 0,
        "bbox": (-8.16, -35.02, -7.96, -34.87),
        "campo_id": None, "campo_area": "SHAPE__Area",
    },
    "belo_horizonte": {
        "nome": "Belo Horizonte", "uf": "MG",
        "service": "mg_bh_LotesCtm", "layer": 0,
        "bbox": (-20.06, -44.07, -19.77, -43.87),
        "campo_id": None, "campo_area": "SHAPE__Area",
    },
    "curitiba": {
        "nome": "Curitiba", "uf": "PR",
        "service": "parcelamento_solo_pr_curitiba", "layer": 0,
        "bbox": (-25.65, -49.41, -25.35, -49.18),
        "campo_id": None, "campo_area": "SHAPE__Area",
    },
    "manaus": {
        "nome": "Manaus", "uf": "AM",
        "service": "AM_MANAUS_PARCELAMENTO_SOLO", "layer": 0,
        "bbox": (-3.22, -60.10, -2.95, -59.85),
        "campo_id": None, "campo_area": "SHAPE__Area",
    },
    "joao_pessoa": {
        "nome": "João Pessoa", "uf": "PB",
        "service": "PB_JOAO_PESSOA_PARCELAMENTO_SOLO", "layer": 0,
        "bbox": (-7.21, -34.98, -7.03, -34.80),
        "campo_id": None, "campo_area": "SHAPE__Area",
    },
    "natal": {
        "nome": "Natal", "uf": "RN",
        "service": "RN_NATAL_PARCELAMENTO_SOLO", "layer": 0,
        "bbox": (-5.94, -35.30, -5.71, -35.14),
        "campo_id": None, "campo_area": "SHAPE__Area",
    },
    "florianopolis": {
        "nome": "Florianópolis", "uf": "SC",
        "service": "SC_FLORIANOPOLIS_PARCELAMENTO_SOLO", "layer": 0,
        "bbox": (-27.85, -48.65, -27.41, -48.37),
        "campo_id": None, "campo_area": "SHAPE__Area",
    },
    "campo_grande": {
        "nome": "Campo Grande", "uf": "MS",
        "service": "parcelamento_solo_campo_grande_MS", "layer": 0,
        "bbox": (-20.63, -54.77, -20.33, -54.49),
        "campo_id": None, "campo_area": "SHAPE__Area",
    },
    "niteroi": {
        "nome": "Niterói", "uf": "RJ",
        "service": "Niteroi_lotes2", "layer": 0,
        "bbox": (-23.02, -43.15, -22.85, -43.00),
        "campo_id": None, "campo_area": "SHAPE__Area",
    },
    "santa_maria": {
        "nome": "Santa Maria", "uf": "RS",
        "service": "rs_sta_maria_Lotes", "layer": 0,
        "bbox": (-29.75, -53.87, -29.59, -53.73),
        "campo_id": None, "campo_area": "SHAPE__Area",
    },
    "rio_branco": {
        "nome": "Rio Branco", "uf": "AC",
        "service": "AC_RIO_BRANCO_PARCELAMENTO_SOLO", "layer": 2,
        "bbox": (-10.05, -67.92, -9.84, -67.74),
        "campo_id": None, "campo_area": "SHAPE__Area",
    },
    "sao_bernardo": {
        "nome": "São Bernardo do Campo", "uf": "SP",
        "service": "SP_SBC_PARCELAMENTO_SOLO", "layer": 0,
        "bbox": (-23.77, -46.62, -23.60, -46.41),
        "campo_id": None, "campo_area": "SHAPE__Area",
    },
    "maringa": {
        "nome": "Maringá", "uf": "PR",
        "service": "Lotes_Maringa", "layer": 0,
        "bbox": (-23.47, -51.99, -23.34, -51.89),
        "campo_id": None, "campo_area": "SHAPE__Area",
    },
}


class LotesInput(BaseModel):
    region_id: str
    survey_id: str
    cidade_slug: Optional[str] = None   # si None, auto-detecta por lat/lng
    lat: Optional[float] = None          # para auto-detección
    lng: Optional[float] = None
    bbox_south: Optional[float] = None  # bbox explícita (opcional)
    bbox_west: Optional[float] = None
    bbox_north: Optional[float] = None
    bbox_east: Optional[float] = None
    delay_ms: int = 500


class LotesOutput(BaseModel):
    ok: bool
    cidade: str = ""
    parcelas_inseridas: int = 0
    parcelas_atualizadas: int = 0
    total_features: int = 0
    error: Optional[str] = None


def detectar_cidade(lat: float, lng: float) -> Optional[str]:
    """Retorna el slug de la ciudad que contiene el punto, o None."""
    for slug, info in CIDADES.items():
        s, w, n, e = info["bbox"]
        if s <= lat <= n and w <= lng <= e:
            return slug
    return None


def listar_cidades() -> list[dict]:
    return [{"slug": k, "nome": v["nome"], "uf": v["uf"]} for k, v in CIDADES.items()]


def _query_lotes(token: str, service: str, layer: int,
                 bbox_wm: dict, offset: int = 0) -> list[dict]:
    import json
    url = f"{GIS_BASE}/{service}/FeatureServer/{layer}/query"
    params = {
        "f": "json",
        "token": token,
        "geometry": json.dumps(bbox_wm),
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "outSR": "4326",
        "returnGeometry": "true",
        "resultRecordCount": MAX_RECORDS,
        "resultOffset": offset,
    }
    with httpx.Client(timeout=30) as client:
        r = client.get(url, params=params)
        r.raise_for_status()
    return r.json().get("features", [])


def _upsert_lotes(features: list[dict], region_id: str, survey_id: str,
                  cidade_info: dict) -> tuple[int, int]:
    engine = get_engine()
    inserted = 0
    updated = 0
    campo_id = cidade_info.get("campo_id")
    campo_area = cidade_info.get("campo_area", "SHAPE__Area")
    fuente = f"onr_lotes_{cidade_info['uf'].lower()}"

    with engine.begin() as conn:
        for ft in features:
            geom_raw = ft.get("geometry")
            attrs = ft.get("attributes", {})
            if not geom_raw:
                continue
            try:
                geom = shape(geom_raw)
                if not geom.is_valid:
                    geom = geom.buffer(0)
                if geom.geom_type == "MultiPolygon":
                    geom = max(geom.geoms, key=lambda g: g.area)
                centroid = geom.centroid
            except Exception:
                continue

            cca = str(attrs.get(campo_id, "")).strip() if campo_id else None
            area_raw = attrs.get(campo_area) or attrs.get("area_m2")
            try:
                area = round(float(area_raw), 2) if area_raw else None
            except (TypeError, ValueError):
                area = None

            existing = conn.execute(text("""
                SELECT parcela_id FROM parcelas
                WHERE region_id = :rid AND fuente_parcela = :fuente
                  AND ABS(centroid_lat - :clat) < 0.00005
                  AND ABS(centroid_lng - :clng) < 0.00005
            """), {"rid": region_id, "fuente": fuente,
                   "clat": centroid.y, "clng": centroid.x}).fetchone()

            if existing:
                conn.execute(text("""
                    UPDATE parcelas SET
                        survey_id = :sid,
                        geometry = ST_GeomFromText(:geom, 4326),
                        area_m2_terreno = :area,
                        cca_code = :cca
                    WHERE parcela_id = :pid
                """), {"sid": survey_id, "geom": geom.wkt,
                       "area": area, "cca": cca, "pid": str(existing[0])})
                updated += 1
            else:
                conn.execute(text("""
                    INSERT INTO parcelas (
                        parcela_id, survey_id, region_id,
                        geometry, centroid_lat, centroid_lng,
                        area_m2_terreno, cca_code,
                        municipio, estado_provincia, fuente_parcela
                    ) VALUES (
                        :pid, :sid, :rid,
                        ST_GeomFromText(:geom, 4326), :clat, :clng,
                        :area, :cca,
                        :municipio, :uf, :fuente
                    )
                """), {
                    "pid": str(uuid.uuid4()), "sid": survey_id, "rid": region_id,
                    "geom": geom.wkt, "clat": centroid.y, "clng": centroid.x,
                    "area": area, "cca": cca,
                    "municipio": cidade_info["nome"],
                    "uf": cidade_info["uf"],
                    "fuente": fuente,
                })
                inserted += 1

    return inserted, updated


def run(input: LotesInput) -> LotesOutput:
    # Determinar ciudad
    slug = input.cidade_slug
    if not slug and input.lat is not None and input.lng is not None:
        slug = detectar_cidade(input.lat, input.lng)

    if not slug:
        available = ", ".join(CIDADES.keys())
        return LotesOutput(ok=False,
                           error=f"Ciudad no detectada. Ciudades disponibles: {available}")

    if slug not in CIDADES:
        return LotesOutput(ok=False, error=f"Ciudad '{slug}' no soportada")

    cidade = CIDADES[slug]
    logger.info(f"ONR Lotes: {cidade['nome']} ({slug})")

    # Determinar bbox de consulta
    if input.bbox_south is not None:
        s, w, n, e = input.bbox_south, input.bbox_west, input.bbox_north, input.bbox_east
    else:
        s, w, n, e = cidade["bbox"]

    try:
        token = get_token()
    except Exception as ex:
        return LotesOutput(ok=False, error=f"No se pudo obtener token ONR: {ex}")

    bbox_wm = _bbox_webmerc(s, w, n, e)

    all_features: list[dict] = []
    offset = 0
    while True:
        try:
            features = _query_lotes(token, cidade["service"], cidade["layer"],
                                     bbox_wm, offset)
        except httpx.HTTPStatusError as ex:
            if ex.response.status_code == 498:
                token = get_token(force_refresh=True)
                features = _query_lotes(token, cidade["service"], cidade["layer"],
                                         bbox_wm, offset)
            else:
                return LotesOutput(ok=False, cidade=cidade["nome"], error=str(ex))

        all_features.extend(features)
        logger.info(f"Lotes {slug}: {len(features)} en offset={offset}, total={len(all_features)}")
        if len(features) < MAX_RECORDS:
            break
        offset += MAX_RECORDS
        time.sleep(input.delay_ms / 1000)

    if not all_features:
        return LotesOutput(ok=True, cidade=cidade["nome"], total_features=0,
                           error="No hay lotes en el área consultada")

    inserted, updated = _upsert_lotes(all_features, input.region_id,
                                       input.survey_id, cidade)

    return LotesOutput(
        ok=True,
        cidade=cidade["nome"],
        parcelas_inseridas=inserted,
        parcelas_atualizadas=updated,
        total_features=len(all_features),
    )
