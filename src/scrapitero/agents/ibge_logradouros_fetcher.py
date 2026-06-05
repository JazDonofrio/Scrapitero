"""IBGELogradourosFetcher — carga la Base de Faces de Logradouros 2022 del IBGE.

Fuente:
  geoftp.ibge.gov.br/.../base_de_faces_de_logradouros_versao_2022_censo_demografico/shp/
  Arquivo: {UF}_faces_de_logradouros_2022_shp.zip

Cada feature es un segmento de calle con:
  - Geometría LineString (eje de la calle)
  - Nombre, tipo y título del logradouro
  - Rango de numeración izquierda y derecha
  - CEP izquierdo y derecho
  - Código de setor censitário

Con estos datos el AddressResolver puede geocodificar gratis por interpolación
sin necesidad de llamar a Google Maps.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path
from typing import Optional

import geopandas as gpd
import httpx
from loguru import logger
from pydantic import BaseModel, model_validator
from sqlalchemy import text

from scrapitero.db.engine import get_engine


BASE_URL = (
    "https://geoftp.ibge.gov.br/recortes_para_fins_estatisticos/"
    "malha_de_setores_censitarios/censo_2022/"
    "base_de_faces_de_logradouros_versao_2022_censo_demografico/shp/"
    "{UF}_faces_de_logradouros_2022_shp.zip"
)

# Mapeo de columnas del SHP (puede variar entre versiones; intentar en orden)
COL_MAP = {
    "cod_face":     ["CD_FACE", "cod_face", "COD_FACE", "id"],
    "tipo_logr":    ["NM_TIP_LOG", "tipo_logr", "TIPO_LOGR", "tipo"],
    "tit_logr":     ["NM_TIT_LOG", "tit_logr", "TIT_LOGR", "titulo"],
    "nom_logr":     ["NM_LOG", "nom_logr", "NOM_LOGR", "nome_logr", "nome"],
    "nro_ini_e":    ["nro_ini_e", "NRO_INI_E", "num_ini_e"],
    "nro_fin_e":    ["nro_fin_e", "NRO_FIN_E", "num_fin_e"],
    "nro_ini_d":    ["nro_ini_d", "NRO_INI_D", "num_ini_d"],
    "nro_fin_d":    ["nro_fin_d", "NRO_FIN_D", "num_fin_d"],
    "cep_e":        ["cep_e", "CEP_E"],
    "cep_d":        ["cep_d", "CEP_D"],
    "cod_munic":    ["cod_munic", "COD_MUNIC", "cod_ibge"],
    "nom_munic":    ["nom_munic", "NOM_MUNIC", "municipio"],
    "cod_setor":    ["CD_SETOR", "cod_setor", "COD_SETOR"],
}


# ── Pydantic I/O ──────────────────────────────────────────────────────────────

class LogradourosInput(BaseModel):
    region_id: str                  # "vg-mt-br"
    municipio_codigo: str           # "5108402"
    estado_uf: Optional[str] = None  # "mt" — si falta, se deriva de municipio_codigo

    @model_validator(mode="after")
    def _derivar_estado_uf(self) -> "LogradourosInput":
        """Si no viene estado_uf, lo deriva del prefijo del código de município IBGE."""
        if not self.estado_uf:
            from scrapitero.agents.ibge_census_fetcher import _UF_POR_CODIGO
            prefijo = (self.municipio_codigo or "").strip()[:2]
            uf = _UF_POR_CODIGO.get(prefijo)
            if not uf:
                raise ValueError(
                    f"falta estado_uf y no se pudo derivar de municipio_codigo="
                    f"{self.municipio_codigo!r} (prefijo {prefijo!r} no es una UF IBGE válida)"
                )
            self.estado_uf = uf
        return self


class LogradourosOutput(BaseModel):
    ok: bool
    logradouros_insertados: int = 0
    logradouros_actualizados: int = 0
    fuentes: list[str] = []
    error: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _col(gdf: gpd.GeoDataFrame, key: str) -> Optional[str]:
    """Devuelve el nombre real de la columna buscando entre variantes."""
    for candidate in COL_MAP.get(key, []):
        if candidate in gdf.columns:
            return candidate
    return None


def _safe_int(val) -> Optional[int]:
    try:
        v = int(float(str(val).replace(",", "").strip()))
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def _safe_str(val, maxlen: int = 200) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s[:maxlen] if s and s.lower() not in ("nan", "none", "") else None


# ── Descarga ──────────────────────────────────────────────────────────────────

def _download(estado_uf: str, cache_dir: Path) -> Path:
    uf = estado_uf.upper()
    url = BASE_URL.format(UF=uf)
    zip_path = cache_dir / f"{uf}_faces_logradouros_2022.zip"

    if not zip_path.exists():
        logger.info(f"Descargando Faces de Logradouros {uf}: {url}")
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            r = client.get(url)
            r.raise_for_status()
        zip_path.write_bytes(r.content)
        logger.info(f"Descargado: {len(r.content) / 1_000_000:.1f} MB")
    else:
        logger.info(f"Usando caché: {zip_path}")

    shp_dir = cache_dir / f"{uf}_faces_logradouros_shp"
    if not shp_dir.exists():
        shp_dir.mkdir()
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(shp_dir)

    return shp_dir


def _load_shp(shp_dir: Path, municipio_codigo: str) -> gpd.GeoDataFrame:
    # Cada municipio tiene su propio SHP nombrado con el código IBGE
    shp_file = next(shp_dir.rglob(f"{municipio_codigo}_*.shp"), None)
    if not shp_file:
        # Fallback: buscar cualquier SHP cuyo nombre empiece con el código
        candidates = [f for f in shp_dir.rglob("*.shp")
                      if f.stem.startswith(municipio_codigo)]
        shp_file = candidates[0] if candidates else None
    if not shp_file:
        raise FileNotFoundError(
            f"No se encontró SHP para municipio {municipio_codigo} en {shp_dir}"
        )
    logger.info(f"Leyendo SHP: {shp_file}")
    gdf = gpd.read_file(shp_file, engine="pyogrio")
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    logger.info(f"Logradouros en municipio {municipio_codigo}: {len(gdf)}")
    return gdf


# ── Upsert en DB ──────────────────────────────────────────────────────────────

def _upsert(gdf: gpd.GeoDataFrame, region_id: str) -> tuple[int, int]:
    engine = get_engine()
    insertados = 0
    actualizados = 0

    c_id      = _col(gdf, "cod_face")
    c_tipo    = _col(gdf, "tipo_logr")
    c_titulo  = _col(gdf, "tit_logr")
    c_nome    = _col(gdf, "nom_logr")
    c_ini_e   = _col(gdf, "nro_ini_e")
    c_fin_e   = _col(gdf, "nro_fin_e")
    c_ini_d   = _col(gdf, "nro_ini_d")
    c_fin_d   = _col(gdf, "nro_fin_d")
    c_cep_e   = _col(gdf, "cep_e")
    c_cep_d   = _col(gdf, "cep_d")
    c_munic   = _col(gdf, "cod_munic")
    c_nom_mun = _col(gdf, "nom_munic")
    c_setor   = _col(gdf, "cod_setor")

    logger.info(f"Insertando {len(gdf)} logradouros en DB...")

    # Pre-cargar setores válidos para evitar FK violations
    with engine.connect() as conn:
        valid_setores = {
            r[0] for r in conn.execute(
                text("SELECT setor_id FROM setores_censitarios WHERE region_id = :rid"),
                {"rid": region_id}
            ).fetchall()
        }

    with engine.begin() as conn:
        for idx, row in gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue

            lid = _safe_str(row[c_id]) if c_id else None
            # Fallback: CD_FACE puede venir NaN en el SHP 2022 → usar setor+índice
            if not lid:
                setor_val = _safe_str(row[c_setor], 20) if c_setor else "X"
                lid = f"{setor_val or 'X'}_{idx}"

            geom_wkt = geom.wkt
            # El SHP puede traer sufijos como "P" en el cod_setor → stripear no-numéricos
            setor_raw = _safe_str(row[c_setor], 20) if c_setor else None
            setor_id = setor_raw.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ") if setor_raw else None
            if setor_id and setor_id not in valid_setores:
                setor_id = None

            existing = conn.execute(
                text("SELECT logradouro_id FROM logradouros WHERE logradouro_id = :lid"),
                {"lid": lid}
            ).fetchone()

            params = {
                "lid": lid, "region": region_id,
                "setor": setor_id,
                "geom": geom_wkt,
                "tipo": _safe_str(row[c_tipo], 50) if c_tipo else None,
                "titulo": _safe_str(row[c_titulo], 50) if c_titulo else None,
                "nome": _safe_str(row[c_nome]) if c_nome else None,
                "ini_e": _safe_int(row[c_ini_e]) if c_ini_e else None,
                "fin_e": _safe_int(row[c_fin_e]) if c_fin_e else None,
                "ini_d": _safe_int(row[c_ini_d]) if c_ini_d else None,
                "fin_d": _safe_int(row[c_fin_d]) if c_fin_d else None,
                "cep_e": _safe_str(row[c_cep_e], 9) if c_cep_e else None,
                "cep_d": _safe_str(row[c_cep_d], 9) if c_cep_d else None,
                "cod_mun": _safe_str(row[c_munic], 7) if c_munic else None,
                "nom_mun": _safe_str(row[c_nom_mun], 100) if c_nom_mun else None,
            }

            if existing:
                conn.execute(text("""
                    UPDATE logradouros SET
                        geometry = ST_GeomFromText(:geom, 4326),
                        tipo_logradouro = :tipo,
                        titulo_logradouro = :titulo,
                        nome_logradouro = :nome,
                        nro_inicial_esq = :ini_e, nro_final_esq = :fin_e,
                        nro_inicial_dir = :ini_d, nro_final_dir = :fin_d,
                        cep_esq = :cep_e, cep_dir = :cep_d,
                        cod_municipio = :cod_mun, nom_municipio = :nom_mun,
                        setor_censitario_id = :setor
                    WHERE logradouro_id = :lid
                """), params)
                actualizados += 1
            else:
                conn.execute(text("""
                    INSERT INTO logradouros (
                        logradouro_id, region_id, setor_censitario_id,
                        geometry, tipo_logradouro, titulo_logradouro, nome_logradouro,
                        nro_inicial_esq, nro_final_esq, nro_inicial_dir, nro_final_dir,
                        cep_esq, cep_dir, cod_municipio, nom_municipio
                    ) VALUES (
                        :lid, :region, :setor,
                        ST_GeomFromText(:geom, 4326), :tipo, :titulo, :nome,
                        :ini_e, :fin_e, :ini_d, :fin_d,
                        :cep_e, :cep_d, :cod_mun, :nom_mun
                    )
                """), params)
                insertados += 1

    logger.info(f"Logradouros DB: {insertados} insertados, {actualizados} actualizados")
    return insertados, actualizados


# ── Entry point ───────────────────────────────────────────────────────────────

def run(input: LogradourosInput) -> LogradourosOutput:
    cache_dir = Path(os.environ.get("SCRAPITERO_CACHE", "/tmp/scrapitero_cache"))
    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        shp_dir = _download(input.estado_uf, cache_dir)
        gdf = _load_shp(shp_dir, input.municipio_codigo)

        if gdf.empty:
            return LogradourosOutput(
                ok=False,
                error=f"No se encontraron logradouros para municipio {input.municipio_codigo}"
            )

        insertados, actualizados = _upsert(gdf, input.region_id)

        return LogradourosOutput(
            ok=True,
            logradouros_insertados=insertados,
            logradouros_actualizados=actualizados,
            fuentes=[f"ibge_faces_logradouros_2022_{input.estado_uf.upper()}"],
        )

    except Exception as e:
        logger.exception("IBGELogradourosFetcher falló")
        return LogradourosOutput(ok=False, error=str(e))
