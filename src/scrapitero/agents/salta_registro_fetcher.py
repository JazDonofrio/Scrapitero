"""SaltaRegistroFetcher — clasifica parcelas por TIPO del registro parcelario SIGSA.

Fuente: SIGSA ArcGIS REST público — sin autenticación.
  URL:   sigsa.inmuebles.gov.ar/server/rest/services/ServiciosApp/ConsultaParcelas/FeatureServer/4
  Tabla: DGI_GIS.CONSULTA_PARCELARIA (334.090 registros — TODA la provincia)
  Campo clave: TIPO ∈ {URBANO, RURAL, CLUB DE CAMPO}

Se consulta por VINCULACION (= nomenclatura_catastral en la DB), que es única por
parcela. Cobertura provincial completa, incluido el interior donde el CPUA (capital)
no llega.

Mapeo TIPO → uso_principal:
  RURAL          → vacante       (terreno rural sin edificación urbana)
  CLUB DE CAMPO  → residencial   (loteo residencial cerrado)
  URBANO         → (sin cambio)  (no aporta uso; lo resuelve SaltaZonificacionFetcher / CPUA)

Complementa a SaltaZonificacionFetcher:
  - CPUA cubre uso urbano de la Capital (residencial/comercial/mixto/...).
  - Este agente cubre la señal rural/club-de-campo del interior provincial.

Nota: el SIGSA usa SSL con renegociación legacy → se configura un SSLContext
con OP_LEGACY_SERVER_CONNECT (0x4).
"""

from __future__ import annotations

import ssl
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.db.engine import get_engine


_SIGSA_URL = (
    "https://sigsa.inmuebles.gov.ar/server/rest/services/"
    "ServiciosApp/ConsultaParcelas/FeatureServer/4/query"
)

# TIPO del registro → uso_principal. URBANO no mapea (no aporta uso).
_TIPO_MAP: dict[str, str] = {
    "RURAL": "vacante",
    "CLUB DE CAMPO": "residencial",
}


def _sigsa_ssl_context() -> ssl.SSLContext:
    """SSLContext que tolera la renegociación legacy del servidor SIGSA."""
    ctx = ssl.create_default_context()
    ctx.options |= 0x4  # OP_LEGACY_SERVER_CONNECT
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ── Pydantic I/O ──────────────────────────────────────────────────────────────

class SaltaRegistroInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None
    overwrite: bool = False      # si True, reclasifica parcelas ya clasificadas
    batch_size: int = 100        # nomenclaturas por request (cláusula IN)


class SaltaRegistroOutput(BaseModel):
    ok: bool
    parcelas_consultadas: int = 0
    parcelas_clasificadas: int = 0
    distribucion_tipo: dict[str, int] = {}
    distribucion_uso: dict[str, int] = {}
    sin_match: int = 0
    error: Optional[str] = None


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_parcelas(region_id: str, survey_id: Optional[str], overwrite: bool) -> list[tuple[str, str]]:
    """Devuelve (parcela_id, nomenclatura_catastral) de la región."""
    engine = get_engine()
    with engine.connect() as conn:
        base = """
            SELECT parcela_id::text, nomenclatura_catastral
            FROM parcelas
            WHERE region_id = :rid
              AND nomenclatura_catastral IS NOT NULL
              AND nomenclatura_catastral <> ''
        """
        params: dict = {"rid": region_id}
        if not overwrite:
            base += " AND (uso_principal IS NULL OR uso_principal = 'sin_datos')"
        if survey_id:
            base += " AND survey_id = :sid"
            params["sid"] = survey_id
        return conn.execute(text(base), params).fetchall()


def _update_batch(updates: list[tuple[str, str]]) -> None:
    """Setea uso_principal. Si queda residencial (CLUB DE CAMPO), cuenta siempre
    al menos 1 UF de vivienda; RURAL → vacante → 0 UF. GREATEST no pisa un conteo
    real mayor."""
    if not updates:
        return
    engine = get_engine()
    with engine.begin() as conn:
        # CAST(:uso AS text) en todas las apariciones: sin el cast, Postgres deduce
        # el parámetro como varchar (asignación) y text (comparación) → AmbiguousParameter.
        conn.execute(text("""
            UPDATE parcelas SET
                uso_principal = CAST(:uso AS text),
                unidades_funcionales_estimadas = CASE
                    WHEN CAST(:uso AS text) = 'residencial'
                        THEN GREATEST(COALESCE(unidades_funcionales_estimadas, 0), 1)
                    WHEN CAST(:uso AS text) = 'vacante' THEN 0
                    ELSE unidades_funcionales_estimadas
                END,
                uf_vivienda = CASE
                    WHEN CAST(:uso AS text) = 'residencial'
                        THEN GREATEST(COALESCE(uf_vivienda, 0), 1)
                    WHEN CAST(:uso AS text) = 'vacante' THEN 0
                    ELSE uf_vivienda
                END
            WHERE parcela_id = :pid
        """), [{"pid": pid, "uso": uso} for pid, uso in updates])


# ── SIGSA query ───────────────────────────────────────────────────────────────

def _fetch_tipos(nomenclaturas: list[str], batch_size: int) -> dict[str, str]:
    """Devuelve {VINCULACION: TIPO} consultando el registro SIGSA por lotes."""
    result: dict[str, str] = {}
    ctx = _sigsa_ssl_context()

    with httpx.Client(verify=ctx, timeout=40) as client:
        for i in range(0, len(nomenclaturas), batch_size):
            chunk = nomenclaturas[i:i + batch_size]
            in_clause = ",".join(f"'{v}'" for v in chunk)
            params = {
                "where": f"VINCULACION IN ({in_clause})",
                "outFields": "VINCULACION,TIPO",
                "f": "json",
            }
            try:
                r = client.get(_SIGSA_URL, params=params)
                r.raise_for_status()
                data = r.json()
            except httpx.TimeoutException:
                raise RuntimeError(
                    f"Timeout consultando registro SIGSA (lote {i//batch_size + 1}). "
                    "Reintentá en unos minutos."
                )
            except httpx.HTTPStatusError as e:
                raise RuntimeError(f"SIGSA HTTP {e.response.status_code}: {e.response.text[:200]}")

            if data.get("error"):
                raise RuntimeError(f"SIGSA error: {data['error'].get('message')}")

            for feat in data.get("features", []):
                a = feat.get("attributes", {})
                vinc = a.get("VINCULACION")
                tipo = (a.get("TIPO") or "").strip().upper()
                if vinc:
                    result[vinc] = tipo

            logger.info(
                f"  Registro SIGSA: lote {i//batch_size + 1} — "
                f"{len(data.get('features', []))} matches ({len(result)} acumulados)"
            )

    return result


# ── Entry point ───────────────────────────────────────────────────────────────

def run(inp: SaltaRegistroInput) -> SaltaRegistroOutput:
    parcelas = _get_parcelas(inp.region_id, inp.survey_id, inp.overwrite)
    if not parcelas:
        msg = (
            f"Sin parcelas con nomenclatura_catastral "
            f"{'pendientes ' if not inp.overwrite else ''}en '{inp.region_id}'. "
            "Ejecutá SaltaCatastroFetcher primero."
        )
        logger.warning(f"SaltaRegistro: {msg}")
        return SaltaRegistroOutput(ok=True, error=msg)

    # nomenclatura → [parcela_id, ...] (puede haber duplicados de nomenclatura)
    by_vinc: dict[str, list[str]] = {}
    for pid, vinc in parcelas:
        by_vinc.setdefault(vinc, []).append(pid)

    logger.info(
        f"SaltaRegistro: {len(parcelas)} parcelas ({len(by_vinc)} nomenclaturas únicas) "
        f"en '{inp.region_id}'"
    )

    try:
        tipos = _fetch_tipos(list(by_vinc.keys()), inp.batch_size)
    except RuntimeError as e:
        return SaltaRegistroOutput(ok=False, error=str(e))
    except Exception as e:
        logger.exception("SaltaRegistro: error inesperado")
        return SaltaRegistroOutput(ok=False, error=str(e))

    # Construir updates
    updates: list[tuple[str, str]] = []
    dist_tipo: dict[str, int] = {}
    dist_uso: dict[str, int] = {}
    sin_match = 0

    for vinc, pids in by_vinc.items():
        tipo = tipos.get(vinc)
        if tipo is None:
            sin_match += len(pids)
            continue
        dist_tipo[tipo] = dist_tipo.get(tipo, 0) + len(pids)

        uso = _TIPO_MAP.get(tipo)
        if uso is None:
            # URBANO u otro: no aporta uso, se deja para CPUA
            continue
        for pid in pids:
            updates.append((pid, uso))
            dist_uso[uso] = dist_uso.get(uso, 0) + 1

    _update_batch(updates)

    logger.info(
        f"SaltaRegistro completo '{inp.region_id}': "
        f"{len(updates)} clasificadas, {sin_match} sin match en registro. "
        f"TIPO: {dist_tipo} | uso: {dist_uso}"
    )
    if dist_tipo.get("URBANO"):
        logger.info(
            f"SaltaRegistro: {dist_tipo['URBANO']} parcelas URBANO sin uso asignado "
            "→ usar SaltaZonificacionFetcher (CPUA) para clasificarlas."
        )

    return SaltaRegistroOutput(
        ok=True,
        parcelas_consultadas=len(parcelas),
        parcelas_clasificadas=len(updates),
        distribucion_tipo=dist_tipo,
        distribucion_uso=dist_uso,
        sin_match=sin_match,
    )
