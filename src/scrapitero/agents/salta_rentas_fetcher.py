"""SaltaRentasFetcher — detecta parcelas baldías por valor edificado (DGRM Salta).

Fuente: Dirección General de Rentas Municipal de Salta (Capital).
  Portal:   https://rentas.dgrmsalta.gov.ar
  Endpoint: POST /api/inmobiliario/login-inmobiliario  {unidad, catastro, recaptcha, impuesto, moratoria}
  Gratis, sin login. Requiere token reCAPTCHA v3 generado en el browser
  (site key 6LcO31EpAAAAACskh5BK2bB86lwBjRxTp5leeiz4) → por eso usa Playwright.

Devuelve por catastro: valorTerreno, valorEdificado, valorFiscal, categoria, zona.

Señal clave: `valorEdificado`
  ≈ 0   → parcela SIN construcción → uso_principal = 'vacante' (terreno baldío)
  > 0   → edificado → se deja el uso a SaltaZonificacionFetcher (no dice residencial/comercial)

Es autoritativo para detectar baldíos: corrige al CPUA, que asigna uso por zona
y no distingue un lote vacío de uno construido dentro de la misma zona.

NO aporta número de unidades funcionales/PH: el catastro de Salta modela cada UF
como una clave independiente; el agrupamiento solo está en la cédula paga.
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

import httpx
from loguru import logger
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.db.engine import get_engine

_PORTAL_URL = "https://rentas.dgrmsalta.gov.ar/#/inmobiliario/emision-boletas"
_API_URL = "https://rentas.dgrmsalta.gov.ar/api/inmobiliario/login-inmobiliario"
_RECAPTCHA_SITEKEY = "6LcO31EpAAAAACskh5BK2bB86lwBjRxTp5leeiz4"

# Umbral: valorEdificado por debajo de esto → se considera baldío
_EDIFICADO_MIN = 1.0

# Stop flags por region_id
_stop_regions: set[str] = set()


def request_stop(region_id: str) -> None:
    _stop_regions.add(region_id)


# ── Pydantic I/O ──────────────────────────────────────────────────────────────

class SaltaRentasInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None
    overwrite: bool = False        # si True, reconsulta parcelas ya marcadas vacante
    delay_ms: int = 1500           # throttle entre consultas (reCAPTCHA + cortesía)
    batch_size: int = 0            # 0 = todas las pendientes
    headless: bool = True


class SaltaRentasOutput(BaseModel):
    ok: bool
    parcelas_consultadas: int = 0
    baldios_detectados: int = 0
    edificados: int = 0
    sin_match: int = 0
    errores: int = 0
    error: Optional[str] = None


# ── Telegram ──────────────────────────────────────────────────────────────────

def _tg(msg: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_parcelas(region_id: str, survey_id: Optional[str],
                  overwrite: bool, batch_size: int) -> list[tuple[str, str]]:
    """Devuelve (parcela_id, cca_code) de la región."""
    engine = get_engine()
    with engine.connect() as conn:
        base = """
            SELECT parcela_id::text, cca_code
            FROM parcelas
            WHERE region_id = :rid
              AND cca_code IS NOT NULL AND cca_code <> ''
        """
        params: dict = {"rid": region_id}
        if not overwrite:
            # saltar las ya confirmadas baldías (no cambian); reconsultar el resto
            base += " AND (uso_principal IS DISTINCT FROM 'vacante')"
        if survey_id:
            base += " AND survey_id = :sid"
            params["sid"] = survey_id
        base += " ORDER BY cca_code"
        rows = conn.execute(text(base), params).fetchall()
    result = [(r[0], r[1]) for r in rows]
    if batch_size > 0:
        result = result[:batch_size]
    return result


def _set_vacante(parcela_id: str) -> None:
    """Marca la parcela como baldío: uso vacante y 0 UF (terreno vacío, sin unidad)."""
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE parcelas SET uso_principal = 'vacante', uso_fuente = 'rentas', "
            "unidades_funcionales_estimadas = 0 WHERE parcela_id = :pid"
        ), {"pid": parcela_id})


# ── Parsing ───────────────────────────────────────────────────────────────────

def _parse_money(s: Optional[str]) -> Optional[float]:
    """'15,673.60' → 15673.60 ; '0.00' → 0.0 ; None → None."""
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


# ── Consulta Playwright ───────────────────────────────────────────────────────

# JS que verifica que el script de reCAPTCHA v3 ya esté cargado y usable.
_GRECAPTCHA_READY_JS = (
    "() => typeof grecaptcha !== 'undefined' "
    "&& typeof grecaptcha.execute === 'function'"
)

# Consulta una parcela: genera token reCAPTCHA v3 esperando a grecaptcha.ready
# (en vez de asumir que el global ya existe) y postea al endpoint de rentas.
_CONSULTA_JS = """async (args) => {
    const [sitekey, catastro, apiUrl] = args;
    const tok = await new Promise((resolve, reject) => {
        if (typeof grecaptcha === 'undefined' || !grecaptcha.execute) {
            reject(new Error('grecaptcha no disponible'));
            return;
        }
        grecaptcha.ready(() => {
            grecaptcha.execute(sitekey, {action: 'submit'}).then(resolve).catch(reject);
        });
    });
    const r = await fetch(apiUrl, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            unidad: catastro, recaptcha: tok,
            impuesto: '0007', moratoria: false,
            catastro: String(catastro)
        })
    });
    return await r.json();
}"""


async def _ensure_grecaptcha(page, timeout_ms: int = 30000) -> bool:
    """Espera a que el script de reCAPTCHA v3 cargue y exponga grecaptcha.execute.

    Antes el código disparaba grecaptcha.execute tras un sleep fijo de 2,5 s; si el
    script aún no había cargado (SPA Angular + carga async), tiraba ReferenceError en
    CADA consulta → fallaba el 100% de las parcelas. Acá pooleamos hasta que el global
    esté listo, con un reload de cortesía si la primera espera se vence."""
    for intento in (1, 2):
        try:
            await page.wait_for_function(_GRECAPTCHA_READY_JS, timeout=timeout_ms)
            return True
        except PlaywrightTimeoutError:
            logger.warning(
                f"SaltaRentas: grecaptcha no cargó en {timeout_ms} ms "
                f"(intento {intento}/2)" + (" — recargando…" if intento == 1 else "")
            )
            if intento == 1:
                try:
                    await page.reload(wait_until="networkidle", timeout=40000)
                except PlaywrightTimeoutError:
                    pass
    return False


async def _run(parcelas: list[tuple[str, str]], region_id: str,
               delay_ms: int, headless: bool) -> tuple[int, int, int, int]:
    """Returns (baldios, edificados, sin_match, errores)."""
    baldios = edificados = sin_match = errores = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        page = await browser.new_page()
        await page.goto(_PORTAL_URL, wait_until="networkidle", timeout=40000)

        # Esperar a que grecaptcha esté realmente disponible antes de consultar.
        # Si no carga, no tiene sentido recorrer las parcelas (todas fallarían igual).
        if not await _ensure_grecaptcha(page):
            await browser.close()
            raise RuntimeError(
                "El script de reCAPTCHA (grecaptcha) no cargó en el portal de rentas; "
                "no se pudieron generar tokens. Reintentar más tarde."
            )

        for i, (parcela_id, cca) in enumerate(parcelas):
            if region_id in _stop_regions:
                _stop_regions.discard(region_id)
                logger.info(f"SaltaRentas: stop solicitado tras {i} consultas")
                break

            try:
                codigo = int(str(cca).strip())
            except (ValueError, AttributeError):
                errores += 1
                continue

            # Hasta 2 intentos por parcela: el token v3 a veces falla transitoriamente.
            data = None
            last_err: Optional[Exception] = None
            for intento in (1, 2):
                try:
                    data = await page.evaluate(
                        _CONSULTA_JS, [_RECAPTCHA_SITEKEY, codigo, _API_URL]
                    )
                    last_err = None
                    break
                except Exception as e:  # noqa: BLE001 — errores JS variados
                    last_err = e
                    if intento == 1:
                        await asyncio.sleep(min(delay_ms / 1000, 1.5))

            if last_err is not None:
                errores += 1
                logger.warning(f"  Catastro {codigo}: error de consulta: {last_err}")
                await asyncio.sleep(delay_ms / 1000)
                continue

            login = (data or {}).get("login") or {}
            ve = _parse_money(login.get("valorEdificado"))
            vt = _parse_money(login.get("valorTerreno"))

            if ve is None and vt is None:
                sin_match += 1
                logger.debug(f"  Catastro {codigo}: sin datos en rentas")
            elif ve is not None and ve < _EDIFICADO_MIN:
                _set_vacante(parcela_id)
                baldios += 1
                logger.info(f"  Catastro {codigo}: baldío (edificado={ve}) → vacante")
            else:
                edificados += 1
                logger.debug(f"  Catastro {codigo}: edificado={ve} (se deja a CPUA)")

            if (i + 1) % 25 == 0:
                logger.info(
                    f"  Progreso: {i+1}/{len(parcelas)} — "
                    f"{baldios} baldíos, {edificados} edificados, "
                    f"{sin_match} sin match, {errores} errores"
                )

            await asyncio.sleep(delay_ms / 1000)

        await browser.close()

    return baldios, edificados, sin_match, errores


# ── Entry point ───────────────────────────────────────────────────────────────

def run(inp: SaltaRentasInput) -> SaltaRentasOutput:
    _stop_regions.discard(inp.region_id)
    parcelas = _get_parcelas(inp.region_id, inp.survey_id, inp.overwrite, inp.batch_size)
    if not parcelas:
        msg = f"Sin parcelas con cca_code pendientes en '{inp.region_id}'."
        logger.warning(f"SaltaRentas: {msg}")
        return SaltaRentasOutput(ok=True, error=msg)

    logger.info(f"SaltaRentas: {len(parcelas)} parcelas a consultar en '{inp.region_id}'")
    _tg(
        f"<b>Salta Rentas</b> — {inp.region_id}\n"
        f"Consultando {len(parcelas)} parcelas para detectar baldíos…"
    )

    try:
        baldios, edificados, sin_match, errores = asyncio.run(
            _run(parcelas, inp.region_id, inp.delay_ms, inp.headless)
        )
    except Exception as e:
        logger.exception("SaltaRentas: error inesperado")
        return SaltaRentasOutput(ok=False, error=str(e))

    logger.info(
        f"SaltaRentas completo '{inp.region_id}': "
        f"{baldios} baldíos, {edificados} edificados, "
        f"{sin_match} sin match, {errores} errores"
    )

    # Si TODAS las consultas fallaron, no es un "completo": es un fallo (típicamente
    # reCAPTCHA). Reportarlo como ok=False para que el orquestador no lo dé por bueno.
    if errores == len(parcelas):
        msg = (
            f"<b>Salta Rentas — FALLÓ</b> — {inp.region_id}\n"
            f"⚠️ Las {errores} consultas fallaron (probable reCAPTCHA). "
            f"No se detectaron baldíos."
        )
        _tg(msg)
        return SaltaRentasOutput(
            ok=False,
            parcelas_consultadas=len(parcelas),
            errores=errores,
            error="Todas las consultas fallaron (probable reCAPTCHA).",
        )

    _tg(
        f"<b>Salta Rentas — COMPLETO</b> — {inp.region_id}\n"
        f"🏗️ Baldíos detectados: {baldios}\n"
        f"🏠 Edificados: {edificados}\n"
        f"❓ Sin match: {sin_match}{f' · ⚠️ {errores} errores' if errores else ''}"
    )

    return SaltaRentasOutput(
        ok=True,
        parcelas_consultadas=len(parcelas),
        baldios_detectados=baldios,
        edificados=edificados,
        sin_match=sin_match,
        errores=errores,
    )
