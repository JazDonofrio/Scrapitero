"""VGBCIFetcher — descarga BCIs (Boletim de Cadastro Imobiliário) de Várzea Grande.

Fuente: https://vg.abaco.com.br/eagata/servlet/hwloginusuario?55
        Portal IPTU/Catastro de la Prefeitura de Várzea Grande.

Flujo:
1. Lee parcelas del DB con cca_code (CODIGO_IMOVEL_AGRUPADO) para la región.
2. Filtra por el polígono de zona (zone_geojson de la región).
3. Verifica cuáles tienen PDF ya descargado → los saltea.
4. Descarga BCIs faltantes vía Playwright.
5. PDF guardado como pdf_dir/reporte_{codigo}.pdf (mismo nombre que el scraper legacy).

El número de inscripción para el formulario = f"{codigo:015d}"
  Ejemplo: CODIGO=30011 → "000000000030011" → reporte_30011.pdf
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from pydantic import BaseModel
from shapely.geometry import Point, shape
from shapely.ops import unary_union
from sqlalchemy import text

from scrapitero.db.engine import get_engine

BASE_URL = "https://vg.abaco.com.br/eagata/servlet/hwloginusuario?55"

# Directorio de PDFs BCI — fuente única compartida con BCIParser. ABSOLUTO y el MISMO
# para ambos (este agente escribe, BCIParser lee). Configurable con SCRAPITERO_PDF_DIR.
# Antes el default era relativo ("pdf_downloads") → dependía del CWD y, corriendo en el
# container Hermes, escribía en otra carpeta que la que leía el parser (inconsistencia).
DEFAULT_PDF_DIR = os.environ.get("SCRAPITERO_PDF_DIR", "/opt/scrapitero/pdf_downloads")

# Subcarpeta por ciudad dentro de pdf_dir: los PDFs BCI se guardan en
# pdf_dir/<ciudad>/reporte_*.pdf para poder reusarlos la próxima vez que se releve la
# misma ciudad (zonas distintas comparten parcelas). vg.abaco.com.br es exclusivo de
# Várzea Grande, así que "varzea-grande" es el default seguro cuando la región no tiene
# municipio_codigo cargado.
_MUNICIPIO_SLUG = {"5108402": "varzea-grande"}


def city_slug(region_id: str) -> str:
    """Slug estable de ciudad para nombrar la subcarpeta de PDFs. Estable por ciudad
    (no por zona) para que el reuso funcione entre relevamientos."""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT municipio_codigo FROM regions WHERE region_id = :r"
            ), {"r": region_id}).fetchone()
        if row and row[0]:
            code = str(row[0]).strip()
            return _MUNICIPIO_SLUG.get(code, f"municipio-{code}")
    except Exception as e:
        logger.warning(f"city_slug: no se pudo resolver la ciudad de '{region_id}': {e}")
    return "varzea-grande"


def resolve_city_pdf_dir(base_pdf_dir: str, region_id: str) -> Path:
    """Devuelve base_pdf_dir/<ciudad>/ (creándola si no existe). VGBCIFetcher (escribe) y
    BCIParser (lee) la usan ambos, así escriben y leen en la MISMA carpeta por ciudad."""
    d = Path(base_pdf_dir) / city_slug(region_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


# Stop flags por region_id
_stop_regions: set[str] = set()


def request_stop(region_id: str) -> None:
    _stop_regions.add(region_id)


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class BCIInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None
    pdf_dir: str = DEFAULT_PDF_DIR
    batch_size: int = 0            # 0 = todos los pendientes
    min_delay_secs: float = 1.5
    max_delay_secs: float = 6.0
    pausa_cada_n: int = 20         # Pausa larga cada N descargas
    pausa_minutos: int = 2
    headless: bool = True


class BCIOutput(BaseModel):
    ok: bool
    pdfs_descargados: int = 0
    pdfs_ya_existentes: int = 0
    pdfs_fallidos: int = 0
    parcelas_procesadas: int = 0
    error: Optional[str] = None


# ── Helpers de DB ──────────────────────────────────────────────────────────────

def _load_zone_polygon(region_id: str) -> Optional[object]:
    from scrapitero.agents.smartgis_fetcher import _geom_to_polygon
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT zone_geojson FROM regions WHERE region_id = :rid"
        ), {"rid": region_id}).fetchone()
    if not row or not row[0]:
        logger.info(f"BCI: región '{region_id}' sin zone_geojson — sin filtro de zona")
        return None
    try:
        gj = json.loads(row[0])
        raw_geoms = []
        if gj.get("type") == "FeatureCollection":
            raw_geoms = [shape(f["geometry"]) for f in gj.get("features", []) if f.get("geometry")]
        elif gj.get("type") == "Feature":
            raw_geoms = [shape(gj["geometry"])]
        else:
            raw_geoms = [shape(gj)]
        polys = [_geom_to_polygon(g) for g in raw_geoms if g is not None]
        poly = unary_union(polys) if polys else None
        if poly:
            b = poly.bounds
            logger.info(
                f"BCI zona '{region_id}': {poly.geom_type} "
                f"lng[{b[0]:.4f},{b[2]:.4f}] lat[{b[1]:.4f},{b[3]:.4f}]"
            )
        return poly
    except Exception as e:
        logger.warning(f"No se pudo parsear zone_geojson: {e}")
        return None


def _get_pending_inscripciones(region_id: str,
                                zone_polygon: Optional[object],
                                batch_size: int) -> list[int]:
    """Devuelve lista de codigos (int) para descargar, dentro de la zona."""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT cca_code, centroid_lat, centroid_lng
            FROM parcelas
            WHERE region_id = :rid
              AND cca_code IS NOT NULL
              AND centroid_lat IS NOT NULL
        """), {"rid": region_id}).fetchall()

    logger.info(f"BCI: {len(rows)} parcelas con cca_code en región '{region_id}'")

    codigos = []
    fuera = 0
    for cca, lat, lng in rows:
        try:
            codigo = int(cca.strip())
        except (ValueError, AttributeError):
            continue
        if zone_polygon and not zone_polygon.contains(Point(lng, lat)):
            fuera += 1
            continue
        codigos.append(codigo)

    if zone_polygon and fuera:
        logger.info(f"BCI: {len(codigos)} dentro de zona, {fuera} fuera")

    if batch_size > 0:
        codigos = codigos[:batch_size]

    logger.info(f"BCI: {len(codigos)} inscripciones a procesar")
    return codigos


# ── Playwright download ────────────────────────────────────────────────────────

async def _run_downloads(codigos: list[int], pdf_dir: str,
                          min_delay: float, max_delay: float,
                          pausa_cada_n: int, pausa_minutos: int,
                          headless: bool,
                          region_id: str = "") -> tuple[int, int, int]:
    """Descarga BCIs. Returns (descargados, ya_existentes, fallidos)."""
    os.makedirs(pdf_dir, exist_ok=True)
    descargados = ya_existentes = fallidos = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        await page.add_init_script(
            "const p = navigator.__proto__; delete p.webdriver; navigator.__proto__ = p;"
        )

        current: dict = {"codigo": None, "ok": False}

        # ── Estrategia 1: context.route() — intercepta ANTES de que el browser
        # procese la respuesta. route.fetch() descarga el body completo al proceso
        # Python; await resp.body() lee desde ese buffer local (inmediato).
        # Distinto a on_response donde response.body() falla porque el recurso CDP
        # se libera en Chromium antes de que el await pueda leerlo.
        async def handle_pdf_route(route, request):
            try:
                resp = await route.fetch()
                ct = resp.headers.get("content-type", "")
                if "application/pdf" in ct.lower():
                    body = await resp.body()
                    if body and len(body) > 500 and current["codigo"] is not None and not current["ok"]:
                        dest = Path(pdf_dir) / f"reporte_{current['codigo']}.pdf"
                        dest.write_bytes(body)
                        current["ok"] = True
                        logger.info(
                            f"    [route] Guardado reporte_{current['codigo']}.pdf "
                            f"({len(body)} bytes) — {request.url[-60:]}"
                        )
                await route.fulfill(response=resp)
            except Exception as e:
                logger.warning(f"    [route] Error en {request.url[-60:]}: {e}")
                try:
                    await route.continue_()
                except Exception:
                    pass

        # Interceptar el servlet BCI y cualquier PDF de este dominio
        await context.route(re.compile(r"arrimprimebci"), handle_pdf_route)
        await context.route(re.compile(r"vg\.abaco\.com\.br.*\.pdf"), handle_pdf_route)

        # ── Estrategia 2: on_response — solo para logging y registro de URL
        async def on_response(response):
            ct = response.headers.get("content-type", "").lower()
            url = response.url
            if not any(ext in url for ext in (".js", ".css", ".png", ".gif", ".ico")):
                logger.info(f"    [resp] {response.status} {ct[:40]} {url[-60:]}")

        # ── Estrategia 3: on_download — cuando el servidor envía
        # Content-Disposition: attachment (el browser no renderiza inline)
        async def on_download(download):
            if current["codigo"] is not None and not current["ok"]:
                dest = Path(pdf_dir) / f"reporte_{current['codigo']}.pdf"
                try:
                    await download.save_as(str(dest))
                    current["ok"] = True
                    logger.info(f"    [download] Guardado {dest.name}")
                except Exception as e:
                    logger.warning(f"    [download] Error: {e}")

        context.on("response", on_response)
        context.on("download", on_download)

        download_n = 0

        for codigo in codigos:
            # Chequear stop entre descargas
            if any(r in _stop_regions for r in (region_id, "")):
                _stop_regions.discard(region_id)
                logger.info(f"BCI: stop solicitado, deteniendo ({descargados} desc hasta ahora)")
                break

            dest = Path(pdf_dir) / f"reporte_{codigo}.pdf"

            if dest.exists() and dest.stat().st_size > 1000:
                ya_existentes += 1
                logger.debug(f"Saltando {codigo}: PDF ya existe")
                continue

            current["codigo"] = codigo
            current["ok"] = False
            formatted = f"{codigo:015d}"
            logger.info(f"BCI {codigo}: iniciando ({formatted})")

            try:
                await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
                logger.info(f"  Página cargada: {page.url}")
                await page.wait_for_selector("#vCONTRIBUINTEINSCRICAO", timeout=10000)

                await page.click("#vCONTRIBUINTEINSCRICAO")
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await page.keyboard.type(formatted, delay=70)
                await page.keyboard.press("Tab")
                await page.wait_for_timeout(700)

                # Esperar posible popup
                popup_page = None
                try:
                    async with page.expect_popup(timeout=10000) as popup_info:
                        await page.click("input[name='BTNCONSULTAR']")
                    popup_page = await popup_info.value
                    logger.info(f"  Popup abierto: {popup_page.url or 'about:blank'}")

                    # Esperar que la URL cambie de about:blank
                    for _ in range(20):
                        if popup_page.url and popup_page.url != "about:blank":
                            break
                        await asyncio.sleep(0.4)

                    logger.info(f"  Popup URL final: {popup_page.url}")
                    try:
                        await popup_page.wait_for_load_state("load", timeout=20000)
                    except PlaywrightTimeoutError:
                        pass
                    # Dar tiempo a handle_pdf_route y on_download para completar
                    await asyncio.sleep(2)
                    await popup_page.close()
                except PlaywrightTimeoutError:
                    logger.info(f"  Sin popup — esperando networkidle en página principal")
                    await page.wait_for_load_state("networkidle", timeout=12000)
                    await asyncio.sleep(2)

                if current["ok"]:
                    descargados += 1
                    download_n += 1
                    logger.info(f"  ✓ reporte_{codigo}.pdf OK")

                    if pausa_cada_n > 0 and download_n % pausa_cada_n == 0:
                        logger.info(f"Pausa de {pausa_minutos}min tras {download_n} descargas…")
                        await asyncio.sleep(pausa_minutos * 60)
                    else:
                        await asyncio.sleep(random.uniform(min_delay, max_delay))
                else:
                    fallidos += 1
                    logger.warning(f"  ✗ Sin PDF para {codigo} — revisar logs [resp] arriba")
                    await asyncio.sleep(random.uniform(min_delay, max_delay))

            except PlaywrightTimeoutError:
                fallidos += 1
                logger.warning(f"  Timeout en {codigo}")
                await asyncio.sleep(random.uniform(20, 45))
            except Exception as e:
                fallidos += 1
                logger.error(f"  Error en {codigo}: {e}")
                await asyncio.sleep(random.uniform(10, 30))

        await context.close()
        await browser.close()

    return descargados, ya_existentes, fallidos


# ── Telegram ──────────────────────────────────────────────────────────────────

def _telegram_notify(msg: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        import httpx as _httpx
        _httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


# ── Entry point ────────────────────────────────────────────────────────────────

def run(input: BCIInput) -> BCIOutput:
    zone_polygon = _load_zone_polygon(input.region_id)
    if zone_polygon is None:
        logger.warning(f"Sin zone_geojson para {input.region_id} — procesando toda la región")

    _stop_regions.discard(input.region_id)  # limpiar flag anterior
    codigos = _get_pending_inscripciones(input.region_id, zone_polygon, input.batch_size)

    if not codigos:
        return BCIOutput(ok=True, error="Sin parcelas con cca_code en la zona")

    # Contar cuántos PDF ya existen (sin arrancar el browser).
    # pdf_dir efectivo = base/<ciudad>/ para reusar entre relevamientos de la misma ciudad.
    pdf_dir = resolve_city_pdf_dir(input.pdf_dir, input.region_id)
    logger.info(f"BCI Fetcher: usando carpeta por ciudad → {pdf_dir}")
    codigos_faltantes = [c for c in codigos
                         if not (pdf_dir / f"reporte_{c}.pdf").exists()]
    ya_existentes_previo = len(codigos) - len(codigos_faltantes)

    logger.info(
        f"BCI Fetcher: {len(codigos)} inscripciones en zona — "
        f"{ya_existentes_previo} PDF ya existen, {len(codigos_faltantes)} a descargar"
    )
    _telegram_notify(
        f"<b>VG BCI Fetcher</b> — {input.region_id}\n"
        f"Total en zona: {len(codigos)}\n"
        f"Ya descargados: {ya_existentes_previo}\n"
        f"A descargar: {len(codigos_faltantes)}"
    )

    if not codigos_faltantes:
        return BCIOutput(
            ok=True,
            pdfs_ya_existentes=ya_existentes_previo,
            parcelas_procesadas=len(codigos),
        )

    descargados, ya_en_run, fallidos = asyncio.run(_run_downloads(
        codigos_faltantes,
        str(pdf_dir),
        input.min_delay_secs,
        input.max_delay_secs,
        input.pausa_cada_n,
        input.pausa_minutos,
        input.headless,
        input.region_id,
    ))

    total_existentes = ya_existentes_previo + ya_en_run

    _telegram_notify(
        f"<b>VG BCI Fetcher — COMPLETO</b> — {input.region_id}\n"
        f"✅ Descargados: {descargados}\n"
        f"📁 Ya existían: {total_existentes}\n"
        f"❌ Fallidos: {fallidos}"
    )

    return BCIOutput(
        ok=True,
        pdfs_descargados=descargados,
        pdfs_ya_existentes=total_existentes,
        pdfs_fallidos=fallidos,
        parcelas_procesadas=len(codigos),
    )
