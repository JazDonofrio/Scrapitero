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
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.db.engine import get_engine

# ── Pydantic I/O ──────────────────────────────────────────────────────────────

class IBGEInput(BaseModel):
    region_id: str                        # "vg-mt-br"
    municipio_codigo: str                 # "5108402" para Várzea Grande
    estado_uf: str                        # "mt"
    survey_id: Optional[str] = None      # UUID del survey activo (opcional)


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
    uf = estado_uf.lower()
    return (
        "https://geoftp.ibge.gov.br/organizacao_do_territorio/"
        "malhas_territoriais/malhas_de_setores_censitarios__divisoes_intramunicipais/"
        f"censo_2022/setores_censitarios_shp/{uf}/{uf}_setores_censitarios.zip"
    )


def _sidra_url(municipio_codigo: str) -> str:
    """URL de la API SIDRA — tabla 9596: domicilios por tipo, por setor censitário."""
    return (
        f"https://apisidra.ibge.gov.br/values/t/9596"
        f"/n322/{municipio_codigo}"   # n322 = setor censitário
        f"/v/allxp/p/last%201/c629/allxt"
        f"?formato=json"
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


def _fetch_sidra(municipio_codigo: str) -> pd.DataFrame:
    """Descarga tabla SIDRA 9596 — domicilios por tipo por setor."""
    url = _sidra_url(municipio_codigo)
    logger.info(f"Consultando SIDRA: {url}")
    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            r = client.get(url)
            r.raise_for_status()
        data = r.json()
        # La API SIDRA devuelve lista de dicts; primera fila es header
        if len(data) < 2:
            logger.warning("SIDRA devolvió datos vacíos")
            return pd.DataFrame()
        df = pd.DataFrame(data[1:], columns=[v for v in data[0].values()])
        logger.info(f"SIDRA: {len(df)} filas")
        return df
    except Exception as e:
        logger.warning(f"SIDRA falló (no crítico): {e}")
        return pd.DataFrame()


def _parse_sidra_domicilios(df: pd.DataFrame) -> dict[str, dict]:
    """
    Parsea tabla SIDRA y devuelve dict setor_id → {domicilios_total, casas, aptos}.
    """
    if df.empty:
        return {}

    result: dict[str, dict] = {}
    # Detectar columna de código de setor
    setor_col = next((c for c in df.columns if "setor" in c.lower() or "geocod" in c.lower()), None)
    val_col = next((c for c in df.columns if "valor" in c.lower() or "value" in c.lower()), None)
    tipo_col = next((c for c in df.columns if "tipo" in c.lower() or "espécie" in c.lower()
                     or "dom" in c.lower()), None)

    if not setor_col or not val_col:
        logger.warning(f"SIDRA: columnas no detectadas. Disponibles: {list(df.columns)}")
        return {}

    for _, row in df.iterrows():
        setor_id = str(row.get(setor_col, "")).strip()
        if not setor_id:
            continue
        try:
            val = int(str(row.get(val_col, "0")).replace(".", "").replace(",", "") or "0")
        except (ValueError, TypeError):
            val = 0

        if setor_id not in result:
            result[setor_id] = {"domicilios_total": 0, "casas": 0, "aptos": 0}

        tipo = str(row.get(tipo_col, "")).lower() if tipo_col else ""
        if "casa" in tipo and "vila" not in tipo:
            result[setor_id]["casas"] += val
        elif "apart" in tipo or "apto" in tipo:
            result[setor_id]["aptos"] += val
        result[setor_id]["domicilios_total"] += val

    return result


# ── Inserción en DB ───────────────────────────────────────────────────────────

def _upsert_setores(gdf: gpd.GeoDataFrame, sidra_data: dict,
                    region_id: str, source_file: str) -> tuple[int, int]:
    """Inserta o actualiza setores en la tabla setores_censitarios."""
    engine = get_engine()
    insertados = 0
    actualizados = 0

    # Detectar columnas del SHP
    col_setor = next((c for c in ["CD_SETOR", "CD_GEOCODI", "Cod_setor"] if c in gdf.columns), None)
    col_pop = next((c for c in ["POP", "POP_2022", "POPULACAO"] if c in gdf.columns), None)

    if not col_setor:
        raise ValueError(f"No se encontró columna de código de setor. Columnas: {list(gdf.columns)}")

    logger.info(f"Insertando {len(gdf)} setores en DB...")

    with engine.begin() as conn:
        for _, row in gdf.iterrows():
            setor_id = str(row[col_setor]).strip()
            geom_wkt = row.geometry.wkt if row.geometry else None
            pop = int(row[col_pop]) if col_pop and pd.notna(row.get(col_pop)) else None
            area_km2 = float(row.geometry.area * 1e10 / 1e6) if row.geometry else None

            sidra = sidra_data.get(setor_id, {})

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
                    "dom_total": sidra.get("domicilios_total"),
                    "dom_casas": sidra.get("casas"),
                    "dom_aptos": sidra.get("aptos"),
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
                    "dom_total": sidra.get("domicilios_total"),
                    "dom_casas": sidra.get("casas"),
                    "dom_aptos": sidra.get("aptos"),
                    "area": area_km2, "src": source_file,
                })
                insertados += 1

    logger.info(f"DB: {insertados} insertados, {actualizados} actualizados")
    return insertados, actualizados


# ── Entry point principal ─────────────────────────────────────────────────────

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

        # 2. Descargar SIDRA (no crítico si falla)
        sidra_df = _fetch_sidra(input.municipio_codigo)
        sidra_data = _parse_sidra_domicilios(sidra_df)
        if sidra_data:
            fuentes.append("ibge_sidra_t9596")

        # 3. Insertar en DB
        insertados, actualizados = _upsert_setores(
            gdf, sidra_data, input.region_id,
            source_file=f"malha_setores_2022_{input.estado_uf}"
        )

        # 4. Calcular totales para el output
        pop_total = sum(
            v.get("domicilios_total", 0) or 0 for v in sidra_data.values()
        ) if sidra_data else 0
        dom_total = pop_total  # SIDRA da domicilios, pop viene del SHP

        # Intentar obtener pop real del SHP
        col_pop = next((c for c in ["POP", "POP_2022"] if c in gdf.columns), None)
        if col_pop:
            pop_total = int(gdf[col_pop].sum())

        return IBGEOutput(
            ok=True,
            setores_insertados=insertados,
            setores_atualizados=actualizados,
            pop_total=pop_total,
            domicilios_total=sum(v.get("domicilios_total", 0) or 0 for v in sidra_data.values()),
            fuentes=fuentes,
        )

    except Exception as e:
        logger.exception("IBGECensusFetcher falló")
        return IBGEOutput(
            ok=False, setores_insertados=0, setores_atualizados=0,
            pop_total=0, domicilios_total=0, fuentes=fuentes,
            error=str(e)
        )
