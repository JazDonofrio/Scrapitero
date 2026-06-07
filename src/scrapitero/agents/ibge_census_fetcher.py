"""IBGECensusFetcher — descarga setores censitários IBGE + tabla SIDRA.

Fuentes:
  - Malha de setores censitários 2022 (SHP, IBGE geoftp)
  - Tabla SIDRA 9596: domicilios por tipo de setor (CSV via API SIDRA)

Output: filas insertadas en tabla `setores_censitarios`.
"""

from __future__ import annotations

import io
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

import geopandas as gpd
import httpx
import pandas as pd
from loguru import logger
from pydantic import BaseModel, model_validator
from sqlalchemy import text

from scrapitero.db.engine import get_engine
from scrapitero.agents._run import agent_run

# ── Pydantic I/O ──────────────────────────────────────────────────────────────

# Código IBGE de UF (2 primeros dígitos del municipio_codigo) → sigla.
_UF_POR_CODIGO = {
    "11": "RO", "12": "AC", "13": "AM", "14": "RR", "15": "PA", "16": "AP", "17": "TO",
    "21": "MA", "22": "PI", "23": "CE", "24": "RN", "25": "PB", "26": "PE", "27": "AL",
    "28": "SE", "29": "BA",
    "31": "MG", "32": "ES", "33": "RJ", "35": "SP",
    "41": "PR", "42": "SC", "43": "RS",
    "50": "MS", "51": "MT", "52": "GO", "53": "DF",
}


class IBGEInput(BaseModel):
    region_id: str                        # "vg-mt-br"
    municipio_codigo: str                 # "5108402" para Várzea Grande
    estado_uf: Optional[str] = None      # "mt" — si falta, se deriva de municipio_codigo
    survey_id: Optional[str] = None      # UUID del survey activo (opcional)

    @model_validator(mode="after")
    def _derivar_estado_uf(self) -> "IBGEInput":
        """Si no viene estado_uf, lo deriva del prefijo del código de município IBGE."""
        if not self.estado_uf:
            prefijo = (self.municipio_codigo or "").strip()[:2]
            uf = _UF_POR_CODIGO.get(prefijo)
            if not uf:
                raise ValueError(
                    f"falta estado_uf y no se pudo derivar de municipio_codigo="
                    f"{self.municipio_codigo!r} (prefijo {prefijo!r} no es una UF IBGE válida)"
                )
            self.estado_uf = uf
        return self


class IBGEOutput(BaseModel):
    ok: bool
    setores_insertados: int
    setores_atualizados: int
    pop_total: int
    domicilios_total: int
    fuentes: list[str]
    error: Optional[str] = None


# ── URLs ──────────────────────────────────────────────────────────────────────

def _malha_url(estado_uf: str) -> str:
    """URL del SHP de setores censitários 2022 para un estado."""
    uf = estado_uf.upper()
    return (
        "https://geoftp.ibge.gov.br/organizacao_do_territorio/"
        "malhas_territoriais/malhas_de_setores_censitarios__divisoes_intramunicipais/"
        f"censo_2022/setores/shp/UF/{uf}_setores_CD2022.zip"
    )


# Agregados por Setores Censitários (Censo 2022) — archivo "basico" nacional.
# Es la fuente correcta de población y domicilios POR SETOR (SIDRA no expone n322).
# Columnas relevantes del CSV: CD_SETOR, CD_MUN, v0001 (total de pessoas),
# v0002 (total de domicílios). ~15 MB, se cachea.
AGREGADOS_BASICO_URL = (
    "https://ftp.ibge.gov.br/Censos/Censo_Demografico_2022/"
    "Agregados_por_Setores_Censitarios/Agregados_por_Setor_csv/"
    "Agregados_por_setores_basico_BR_20260520.zip"
)


# ── Descarga y procesamiento ──────────────────────────────────────────────────

def _download_malha(estado_uf: str, cache_dir: Path) -> gpd.GeoDataFrame:
    """Descarga y descomprime el SHP de setores, devuelve GeoDataFrame en WGS84."""
    url = _malha_url(estado_uf)
    zip_path = cache_dir / f"{estado_uf}_setores.zip"

    if not zip_path.exists():
        logger.info(f"Descargando malha IBGE: {url}")
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            r = client.get(url)
            r.raise_for_status()
        zip_path.write_bytes(r.content)
        logger.info(f"Descargado: {len(r.content) / 1_000_000:.1f} MB")
    else:
        logger.info(f"Usando caché: {zip_path}")

    shp_dir = cache_dir / f"{estado_uf}_setores_shp"
    if not shp_dir.exists():
        shp_dir.mkdir()
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(shp_dir)

    shp_files = list(shp_dir.rglob("*.shp"))
    if not shp_files:
        raise FileNotFoundError(f"No se encontró .shp en {shp_dir}")

    logger.info(f"Leyendo SHP: {shp_files[0]}")
    gdf = gpd.read_file(shp_files[0], engine="pyogrio")
    logger.info(f"Total setores en estado: {len(gdf)}")

    # Reproyectar a WGS84 si hace falta
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    return gdf


def _filter_municipio(gdf: gpd.GeoDataFrame, municipio_codigo: str) -> gpd.GeoDataFrame:
    """Filtra el GeoDataFrame al municipio indicado."""
    # El campo varía según versión del SHP: CD_MUN, CD_GEOCMU, CD_GEOCODM
    for col in ["CD_MUN", "CD_GEOCMU", "CD_GEOCODM"]:
        if col in gdf.columns:
            filtered = gdf[gdf[col].astype(str).str.startswith(municipio_codigo)]
            logger.info(f"Setores en municipio {municipio_codigo}: {len(filtered)} (filtrado por {col})")
            return filtered

    # Fallback: filtrar por los primeros 7 dígitos del código de setor
    for col in ["CD_SETOR", "CD_GEOCODI"]:
        if col in gdf.columns:
            filtered = gdf[gdf[col].astype(str).str.startswith(municipio_codigo)]
            logger.info(f"Setores en municipio {municipio_codigo}: {len(filtered)} (filtrado por {col})")
            return filtered

    logger.warning(f"No se pudo filtrar por municipio, columnas disponibles: {list(gdf.columns)}")
    return gdf


def _fetch_agregados_basico(municipio_codigo: str, cache_dir: Path) -> dict[str, dict]:
    """Descarga (cacheado) el CSV nacional 'Agregados por setores — básico' del Censo
    2022 y devuelve {setor_id: {pop_total, domicilios_total}} filtrado al município.

    Reemplaza a la antigua consulta SIDRA (tabla 9596), que era la tabla equivocada y
    no tiene nivel setor censitário → 400. Esta es la fuente oficial de población y
    domicilios por setor.
    """
    zip_path = cache_dir / "agregados_basico_BR.zip"
    if not zip_path.exists():
        logger.info(f"Descargando Agregados básico IBGE: {AGREGADOS_BASICO_URL}")
        with httpx.Client(timeout=300, follow_redirects=True) as client:
            r = client.get(AGREGADOS_BASICO_URL)
            r.raise_for_status()
        zip_path.write_bytes(r.content)
        logger.info(f"Descargado: {len(r.content) / 1_000_000:.1f} MB")
    else:
        logger.info(f"Usando caché: {zip_path}")

    import csv as _csv

    result: dict[str, dict] = {}
    with zipfile.ZipFile(zip_path) as z:
        csv_name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        with z.open(csv_name) as f:
            reader = _csv.DictReader(io.TextIOWrapper(f, encoding="latin-1"), delimiter=";")
            for row in reader:
                if str(row.get("CD_MUN", "")).strip() != municipio_codigo:
                    continue
                setor_id = str(row.get("CD_SETOR", "")).strip()
                if not setor_id:
                    continue

                def _to_int(v: object) -> Optional[int]:
                    s = str(v or "").strip()
                    if not s or s in ("X", "."):  # IBGE usa 'X' para datos suprimidos
                        return None
                    try:
                        return int(float(s.replace(",", ".")))
                    except (ValueError, TypeError):
                        return None

                result[setor_id] = {
                    "pop_total": _to_int(row.get("v0001")),         # total de pessoas
                    "domicilios_total": _to_int(row.get("v0002")),  # total de domicílios
                }

    logger.info(f"Agregados básico: {len(result)} setores para município {municipio_codigo}")
    return result


# ── Inserción en DB ───────────────────────────────────────────────────────────

def _upsert_setores(gdf: gpd.GeoDataFrame, agregados: dict,
                    region_id: str, source_file: str) -> tuple[int, int]:
    """Inserta o actualiza setores en la tabla setores_censitarios.

    `agregados` = {setor_id: {pop_total, domicilios_total}} del CSV de Agregados básico.
    La población y los domicilios salen de ahí (la malha SHP es solo geometría)."""
    engine = get_engine()
    insertados = 0
    actualizados = 0

    # Detectar columna de código de setor en el SHP (la malha solo trae geometría)
    col_setor = next((c for c in ["CD_SETOR", "CD_GEOCODI", "Cod_setor"] if c in gdf.columns), None)

    if not col_setor:
        raise ValueError(f"No se encontró columna de código de setor. Columnas: {list(gdf.columns)}")

    logger.info(f"Insertando {len(gdf)} setores en DB...")

    with engine.begin() as conn:
        for _, row in gdf.iterrows():
            setor_id = str(row[col_setor]).strip()
            geom_wkt = row.geometry.wkt if row.geometry else None
            area_km2 = float(row.geometry.area * 1e10 / 1e6) if row.geometry else None

            ag = agregados.get(setor_id, {})
            pop = ag.get("pop_total")

            # Calcular área en km² correctamente (geometría en grados → aproximación)
            if row.geometry:
                # Proyectar a SIRGAS 2000 / UTM para área precisa
                from shapely.ops import transform
                import pyproj
                project = pyproj.Transformer.from_crs(
                    "EPSG:4326", "EPSG:32721", always_xy=True
                ).transform
                try:
                    projected = transform(project, row.geometry)
                    area_km2 = projected.area / 1_000_000
                except Exception:
                    area_km2 = None

            existing = conn.execute(
                text("SELECT setor_id FROM setores_censitarios WHERE setor_id = :sid"),
                {"sid": setor_id}
            ).fetchone()

            if existing:
                conn.execute(text("""
                    UPDATE setores_censitarios SET
                        geometry = ST_GeomFromText(:geom, 4326),
                        pop_total = :pop,
                        domicilios_total = :dom_total,
                        domicilios_casas = :dom_casas,
                        domicilios_aptos = :dom_aptos,
                        area_km2 = :area,
                        source_file = :src
                    WHERE setor_id = :sid
                """), {
                    "geom": geom_wkt, "pop": pop,
                    "dom_total": ag.get("domicilios_total"),
                    "dom_casas": None,   # el desglose casas/aptos está en otro dataset
                    "dom_aptos": None,
                    "area": area_km2, "src": source_file, "sid": setor_id,
                })
                actualizados += 1
            else:
                conn.execute(text("""
                    INSERT INTO setores_censitarios
                        (setor_id, region_id, geometry, pop_total,
                         domicilios_total, domicilios_casas, domicilios_aptos,
                         area_km2, source_file)
                    VALUES
                        (:sid, :region, ST_GeomFromText(:geom, 4326), :pop,
                         :dom_total, :dom_casas, :dom_aptos, :area, :src)
                """), {
                    "sid": setor_id, "region": region_id, "geom": geom_wkt, "pop": pop,
                    "dom_total": ag.get("domicilios_total"),
                    "dom_casas": None,
                    "dom_aptos": None,
                    "area": area_km2, "src": source_file,
                })
                insertados += 1

    logger.info(f"DB: {insertados} insertados, {actualizados} actualizados")
    return insertados, actualizados


# ── Entry point principal ─────────────────────────────────────────────────────

@agent_run
def run(input: IBGEInput) -> IBGEOutput:
    """Ejecuta el agente completo."""
    cache_dir = Path(os.environ.get("SCRAPITERO_CACHE", "/tmp/scrapitero_cache"))
    cache_dir.mkdir(parents=True, exist_ok=True)

    fuentes = []
    try:
        # 1. Descargar y filtrar malha
        gdf_estado = _download_malha(input.estado_uf, cache_dir)
        gdf = _filter_municipio(gdf_estado, input.municipio_codigo)
        fuentes.append(f"ibge_malha_setores_2022_{input.estado_uf}")

        if gdf.empty:
            return IBGEOutput(
                ok=False, setores_insertados=0, setores_atualizados=0,
                pop_total=0, domicilios_total=0, fuentes=fuentes,
                error=f"No se encontraron setores para municipio {input.municipio_codigo}"
            )

        # 2. Población y domicilios por setor — Agregados básico (no crítico si falla)
        try:
            agregados = _fetch_agregados_basico(input.municipio_codigo, cache_dir)
            if agregados:
                fuentes.append("ibge_agregados_setores_basico_2022")
        except Exception as e:
            logger.warning(f"Agregados básico falló (no crítico): {e}")
            agregados = {}

        # 3. Insertar en DB
        insertados, actualizados = _upsert_setores(
            gdf, agregados, input.region_id,
            source_file=f"malha_setores_2022_{input.estado_uf}"
        )

        # 4. Totales para el output (agregados ya viene filtrado al município)
        pop_total = sum(v.get("pop_total") or 0 for v in agregados.values())
        dom_total = sum(v.get("domicilios_total") or 0 for v in agregados.values())

        return IBGEOutput(
            ok=True,
            setores_insertados=insertados,
            setores_atualizados=actualizados,
            pop_total=pop_total,
            domicilios_total=dom_total,
            fuentes=fuentes,
        )

    except Exception as e:
        logger.exception("IBGECensusFetcher falló")
        return IBGEOutput(
            ok=False, setores_insertados=0, setores_atualizados=0,
            pop_total=0, domicilios_total=0, fuentes=fuentes,
            error=str(e)
        )
