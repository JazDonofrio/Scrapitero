"""BCIParser — extrae datos estructurados de los PDFs del BCI de Várzea Grande.

Fuente: pdf_downloads/reporte_{cca_code}.pdf
Formato: Boletim de Cadastramento Imobiliário — Prefeitura de Várzea Grande (GeneXus)

Extrae sin LLM usando regex sobre el texto del PDF:
  - Tipo do imóvel (Predial / Territorial)
  - Unidades funcionales: cantidad, uso (RESIDENCIAL / COMERCIAL), tipología
  - Área construída total
  - Dirección (logradouro, número, CEP, bairro)
  - Número de matrícula del registro de imóveis
"""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path
from typing import Optional

import pdfplumber
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.db.engine import get_engine

# Directorio de PDFs BCI — fuente única compartida con VGBCIFetcher. Debe ser ABSOLUTO
# y el MISMO para ambos agentes (el fetcher escribe, el parser lee). Configurable con
# SCRAPITERO_PDF_DIR para que un solo setting (p.ej. en el container Hermes) los alinee.
DEFAULT_PDF_DIR = os.environ.get("SCRAPITERO_PDF_DIR", "/opt/scrapitero/pdf_downloads")


class BCIParserInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None
    pdf_dir: str = DEFAULT_PDF_DIR
    batch_size: int = 0   # 0 = todas las parcelas con PDF disponible


class BCIParserOutput(BaseModel):
    ok: bool
    procesadas: int = 0
    actualizadas: int = 0
    sin_pdf: int = 0
    errores: int = 0
    error: Optional[str] = None


# ── Extracción de texto ────────────────────────────────────────────────────────

def _pdf_text(path: Path) -> str:
    pages = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                pages.append(t)
    return "\n".join(pages)


# ── Parser de campos ───────────────────────────────────────────────────────────

def _parse_bci(text: str) -> dict:
    """Extrae campos estructurados del texto del BCI."""
    r: dict = {
        "tipo_imovel": None,
        "uso_principal": None,
        "uf_vivienda": 0,
        "uf_comercio": 0,
        "uf_total": 0,
        "area_m2_construida": None,
        "calle": None,
        "numero": None,
        "barrio": None,
        "codigo_postal": None,
        "partida_inmobiliaria": None,
        "tipologia": None,
    }

    # ── Tipo do imóvel ─────────────────────────────────────────────────────────
    m = re.search(r'TIPO\s+IM[OÓ]VEL\s+CATEGORIA\s+TIPO\s+PROPRIEDADE\s*\n(\S+)', text, re.I)
    if m:
        r["tipo_imovel"] = m.group(1).strip().lower()

    # ── Unidades funcionales (sólo para Predial) ──────────────────────────────
    if r["tipo_imovel"] == "territorial":
        r["uso_principal"] = "vacante"
    else:
        unit_rows = re.findall(
            r'UNIDADE\s+C[OÓ]DIGO\s+DA\s+UNIDADE.*?TOTAL\s*\n\s*(\d+)\s+(\d+)\s+([\d,]+)\s+([\d,]+)',
            text, re.I | re.DOTALL,
        )
        usos = re.findall(r'\bUSO\s+(RESIDENCIAL|COMERCIAL|INDUSTRIAL|MISTO)\b', text, re.I)
        r["uf_vivienda"] = sum(1 for u in usos if u.upper() == "RESIDENCIAL")
        r["uf_comercio"] = sum(1 for u in usos if u.upper() == "COMERCIAL")
        r["uf_total"] = len(unit_rows) or len(usos)

        if r["uf_comercio"] > 0 and r["uf_vivienda"] > 0:
            r["uso_principal"] = "mixto"
        elif r["uf_comercio"] > 0:
            r["uso_principal"] = "comercial"
        elif re.search(r'\bUSO\s+INDUSTRIAL\b', text, re.I):
            r["uso_principal"] = "industrial"
        else:
            r["uso_principal"] = "residencial"

        if unit_rows:
            try:
                r["area_m2_construida"] = float(unit_rows[0][3].replace(",", "."))
            except ValueError:
                pass

        tipologias = re.findall(r'\bTIPOLOGIA\s+(\S+)', text, re.I)
        if tipologias:
            r["tipologia"] = Counter(t.upper() for t in tipologias).most_common(1)[0][0]

    # ── Dirección ──────────────────────────────────────────────────────────────
    # Línea: "CODE  LOGRADOURO  [NUMBER]  NN.NNN-NNN"
    m = re.search(r'C[OÓ]DIGO\s+LOGRADOURO\s+N[UÚ]MERO\s+CEP\s*\n(.+)', text, re.I)
    if m:
        line = m.group(1).strip()
        # Buscar CEP al final (NN.NNN-NNN)
        cep_m = re.search(r'([\d]{2}\.?[\d]{3}-[\d]{3})\s*$', line)
        if cep_m:
            r["codigo_postal"] = re.sub(r'[.\-]', '', cep_m.group(1))
            before = line[:cep_m.start()].strip()
            # Número de puerta = último token numérico antes del CEP
            num_m = re.search(r'\s(\d{1,5})\s*$', before)
            if num_m:
                r["numero"] = num_m.group(1)
                logradouro_raw = before[:num_m.start()].strip()
            else:
                logradouro_raw = before
            # Quitar el código de logradouro al inicio (número)
            r["calle"] = re.sub(r'^\d+\s+', '', logradouro_raw).strip() or None

    # ── Bairro ─────────────────────────────────────────────────────────────────
    m = re.search(r'\n\s*\d+\s*-\s*(?:BAIRRO\s+)?(.+?)\s*\nQUADRA', text, re.I)
    if m:
        r["barrio"] = re.sub(r'^BAIRRO\s+', '', m.group(1).strip(), flags=re.I)

    # ── Número de matrícula ────────────────────────────────────────────────────
    # Header: "N° MATRÍCULA FRENTE (M)"  →  data row: "0,00  1,00  119.441  0,00"
    m = re.search(r'N[°O]\s+MATR[IÍ]CULA\s+FRENTE\s*\(M\)\s*\n(.*)', text, re.I)
    if m:
        # Matrículas reales tienen formato "119.441" (dígitos con punto)
        mats = re.findall(r'\b(\d{1,6}\.\d{3})\b', m.group(1))
        if mats:
            r["partida_inmobiliaria"] = mats[0]

    return r


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _buscar_en_dirs_alternativos(codigos: list[int], pdf_dir: Path) -> dict[str, int]:
    """Busca los PDFs faltantes en ubicaciones alternativas conocidas para detectar
    un desajuste de `pdf_dir` entre VGBCIFetcher (que descarga) y BCIParser (que lee).

    Devuelve {directorio: cuántos de los faltantes están ahí}. Esto convierte el caso
    '10 PDFs faltantes' (silencioso) en un aviso accionable que nombra dónde SÍ están.
    """
    if not codigos:
        return {}
    candidatos = [
        Path.cwd() / "pdf_downloads",                          # default relativo del fetcher
        Path("/opt/scrapitero/pdf_downloads"),                 # default histórico del host
        Path("/docker/hermes-agent-wgnq/data/pdf_downloads"),  # data dir del container Hermes
    ]
    env = os.environ.get("SCRAPITERO_PDF_DIR")
    if env:
        candidatos.insert(0, Path(env))
    encontrados: dict[str, int] = {}
    vistos = {pdf_dir.resolve()}
    for d in candidatos:
        try:
            rd = d.resolve()
        except OSError:
            continue
        if rd in vistos:
            continue
        vistos.add(rd)
        n = sum(1 for c in codigos if (d / f"reporte_{c}.pdf").exists())
        if n:
            encontrados[str(d)] = n
    return encontrados


def _get_parcelas(region_id: str, batch_size: int) -> list[tuple[str, str]]:
    """Devuelve (parcela_id, cca_code) con PDF pendiente de parseo."""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT parcela_id::text, cca_code
            FROM parcelas
            WHERE region_id = :rid
              AND cca_code IS NOT NULL
              AND fuente_parcela IN ('smartgis_vg', 'catastro')
            ORDER BY cca_code
        """), {"rid": region_id}).fetchall()
    result = [(r[0], r[1]) for r in rows]
    if batch_size > 0:
        result = result[:batch_size]
    return result


def _update_parcela(parcela_id: str, d: dict) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE parcelas SET
                uso_principal               = COALESCE(:uso, uso_principal),
                uf_vivienda                 = :uf_viv,
                uf_comercio                 = :uf_com,
                uf_fuente                   = 'bci',
                unidades_funcionales_estimadas = :uf_tot,
                area_m2_construida          = COALESCE(:area, area_m2_construida),
                calle                       = COALESCE(:calle, calle),
                numero                      = COALESCE(:nro, numero),
                barrio                      = COALESCE(:barrio, barrio),
                codigo_postal               = COALESCE(:cep, codigo_postal),
                partida_inmobiliaria        = COALESCE(:mat, partida_inmobiliaria),
                direccion_source            = CASE WHEN :calle IS NOT NULL
                                             THEN 'bci_pdf' ELSE direccion_source END,
                direccion_confidence        = CASE WHEN :calle IS NOT NULL
                                             THEN 0.95 ELSE direccion_confidence END
            WHERE parcela_id = :pid
        """), {
            "pid": parcela_id,
            "uso": d["uso_principal"],
            "uf_viv": d["uf_vivienda"],
            "uf_com": d["uf_comercio"],
            "uf_tot": d["uf_total"] or None,
            "area": d["area_m2_construida"],
            "calle": d["calle"],
            "nro": d["numero"],
            "barrio": d["barrio"],
            "cep": d["codigo_postal"],
            "mat": d["partida_inmobiliaria"],
        })


# ── Entry point ────────────────────────────────────────────────────────────────

def run(input: BCIParserInput) -> BCIParserOutput:
    parcelas = _get_parcelas(input.region_id, input.batch_size)
    if not parcelas:
        logger.warning(
            f"BCIParser: región '{input.region_id}' no tiene parcelas con cca_code "
            f"y fuente_parcela=smartgis_vg/catastro — ¿SmartGISFetcher fue ejecutado?"
        )
        return BCIParserOutput(ok=True, error="Sin parcelas para parsear en esta región")

    logger.info(f"BCIParser: {len(parcelas)} parcelas con cca_code en '{input.region_id}'")
    # pdf_dir efectivo = base/<ciudad>/ — la MISMA carpeta por ciudad que escribe VGBCIFetcher.
    from scrapitero.agents.varzea_bci_fetcher import resolve_city_pdf_dir
    pdf_dir = resolve_city_pdf_dir(input.pdf_dir, input.region_id)

    # Diagnóstico previo: cuántos PDFs existen antes de empezar
    codigos_ok = [int(cca.strip()) for _, cca in parcelas
                  if cca and cca.strip().isdigit()
                  and (pdf_dir / f"reporte_{int(cca.strip())}.pdf").exists()]
    codigos_faltantes = [int(cca.strip()) for _, cca in parcelas
                         if cca and cca.strip().isdigit()
                         and not (pdf_dir / f"reporte_{int(cca.strip())}.pdf").exists()]
    logger.info(
        f"BCIParser pre-check: {len(codigos_ok)} PDFs disponibles, "
        f"{len(codigos_faltantes)} sin PDF en '{pdf_dir}'"
    )
    if codigos_faltantes:
        sample = codigos_faltantes[:10]
        logger.warning(
            f"BCIParser: PDFs faltantes (muestra {len(sample)}/{len(codigos_faltantes)}): "
            f"{sample} — ¿VGBCIFetcher fue ejecutado para '{input.region_id}'?"
        )
        # ¿Están en otra carpeta? Detecta desajuste de pdf_dir fetcher↔parser.
        alt = _buscar_en_dirs_alternativos(codigos_faltantes, pdf_dir)
        if alt:
            detalle = ", ".join(f"{n} en {d}" for d, n in alt.items())
            logger.warning(
                f"BCIParser: ⚠ {len(codigos_faltantes)} PDFs 'faltantes' SÍ existen en otra "
                f"ubicación ({detalle}) pero NO en pdf_dir='{pdf_dir}'. Es un DESAJUSTE de "
                f"pdf_dir entre VGBCIFetcher (descarga) y BCIParser (lee): pasá el MISMO "
                f"pdf_dir absoluto a ambos (o seteá SCRAPITERO_PDF_DIR). No hace falta "
                f"re-descargar."
            )
    if not codigos_ok:
        msg = (
            f"BCIParser: NINGÚN PDF disponible para los {len(parcelas)} cca_codes de "
            f"'{input.region_id}'. Ejecutar VGBCIFetcher primero."
        )
        logger.error(msg)
        return BCIParserOutput(
            ok=False,
            procesadas=0,
            sin_pdf=len(parcelas),
            error=msg,
        )

    procesadas = actualizadas = sin_pdf = errores = 0

    for parcela_id, cca_code in parcelas:
        try:
            codigo = int(cca_code.strip())
        except (ValueError, AttributeError):
            errores += 1
            logger.warning(f"BCIParser: cca_code inválido '{cca_code}' para parcela {parcela_id}")
            continue

        pdf_path = pdf_dir / f"reporte_{codigo}.pdf"
        if not pdf_path.exists():
            sin_pdf += 1
            logger.debug(f"BCIParser: sin PDF para cca_code={codigo} (reporte_{codigo}.pdf)")
            continue

        procesadas += 1
        try:
            text = _pdf_text(pdf_path)
            data = _parse_bci(text)
            _update_parcela(parcela_id, data)
            actualizadas += 1
            logger.debug(
                f"BCIParser: {codigo} → uso={data['uso_principal']} "
                f"uf_viv={data['uf_vivienda']} uf_com={data['uf_comercio']}"
            )

            if procesadas % 50 == 0:
                logger.info(
                    f"BCIParser: {procesadas}/{len(parcelas)} — "
                    f"{actualizadas} actualizadas, {sin_pdf} sin PDF, {errores} errores"
                )
        except Exception as e:
            errores += 1
            logger.error(f"BCIParser: error parseando reporte_{codigo}.pdf: {e}", exc_info=True)

    logger.info(
        f"BCIParser completo '{input.region_id}': "
        f"{procesadas} procesadas, {actualizadas} actualizadas, "
        f"{sin_pdf} sin PDF, {errores} errores"
    )
    if errores > 0:
        logger.warning(
            f"BCIParser: {errores} errores de parseo — revisar PDFs individuales con logs DEBUG"
        )
    return BCIParserOutput(
        ok=True,
        procesadas=procesadas,
        actualizadas=actualizadas,
        sin_pdf=sin_pdf,
        errores=errores,
    )
