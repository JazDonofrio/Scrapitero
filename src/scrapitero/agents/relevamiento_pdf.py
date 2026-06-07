"""RelevamientoPDF - genera un PDF del reporte de relevamiento catastral."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fpdf import FPDF
from pydantic import BaseModel

from scrapitero.agents.relevamiento_reporter import ReporterInput, RelevamientoReport, run as get_report
from scrapitero.agents._run import agent_run


class PDFInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None
    output_path: Optional[str] = None  # si None, usa /tmp/


class PDFOutput(BaseModel):
    ok: bool
    pdf_path: Optional[str] = None
    total_parcelas: int = 0
    error: Optional[str] = None


class RelevamientoPDF(FPDF):

    def header(self):
        self.set_font("Helvetica", "B", 13)
        self.set_fill_color(30, 80, 160)
        self.set_text_color(255, 255, 255)
        self.cell(0, 10, "REPORTE DE RELEVAMIENTO CATASTRAL", align="C", fill=True, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.ln(2)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 8, f"Scrapitero - {datetime.now().strftime('%d/%m/%Y %H:%M')}  |  Página {self.page_no()}", align="C")


def _build_pdf(report: RelevamientoReport) -> FPDF:
    pdf = RelevamientoPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # ── Encabezado del reporte ────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_fill_color(240, 244, 255)
    pdf.cell(0, 8, f"Región: {report.region_nombre}", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)

    pdf.set_font("Helvetica", "", 9)
    col_w = 65
    data_rows = [
        ("Survey ID", report.survey_id),
        ("Total parcelas", str(report.total_parcelas)),
        ("Con dirección", str(report.parcelas_con_direccion)),
        ("Con UF", str(report.parcelas_con_uf)),
        ("Con nomenclatura", str(report.parcelas_con_nomenclatura)),
        ("Total UF", str(report.total_uf)),
        ("Área total relevada", f"{report.area_total_m2:,.1f} m²" if report.area_total_m2 else "-"),
        ("Fecha", datetime.now().strftime("%d/%m/%Y")),
    ]
    for i in range(0, len(data_rows), 4):
        chunk = data_rows[i:i+4]
        for label, value in chunk:
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(35, 6, f"{label}:", border=0)
            pdf.set_font("Helvetica", "", 9)
            pdf.cell(col_w - 35, 6, value, border=0)
        pdf.ln(6)
    pdf.ln(3)

    # ── Tabla de parcelas ─────────────────────────────────────────────────────
    headers = ["N°", "Dirección", "Área m²", "UF", "Nomenclatura Catastral", "Partida"]
    widths  = [10,    80,          22,         10,   120,                      25]

    # Header de tabla
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(30, 80, 160)
    pdf.set_text_color(255, 255, 255)
    for h, w in zip(headers, widths):
        pdf.cell(w, 7, h, border=1, fill=True, align="C")
    pdf.ln()
    pdf.set_text_color(0, 0, 0)

    # Filas
    pdf.set_font("Helvetica", "", 8)
    for i, p in enumerate(report.parcelas):
        fill = i % 2 == 0
        pdf.set_fill_color(248, 250, 255) if fill else pdf.set_fill_color(255, 255, 255)

        row = [
            str(i + 1),
            p.direccion or "-",
            f"{p.area_m2:,.1f}" if p.area_m2 else "-",
            str(p.unidades_funcionales) if p.unidades_funcionales is not None else "-",
            p.nomenclatura_catastral or "-",
            p.partida_inmobiliaria or "-",
        ]
        # Calcular altura necesaria para la fila (texto largo en nomenclatura)
        row_h = 6
        for val, w in zip(row, widths):
            lines = pdf.get_string_width(val) / (w - 2)
            if lines > 1:
                row_h = max(row_h, 8)

        for val, w in zip(row, widths):
            pdf.cell(w, row_h, val, border=1, fill=fill)
        pdf.ln()

    return pdf


@agent_run
def run(input: PDFInput) -> PDFOutput:
    try:
        report = get_report(ReporterInput(
            region_id=input.region_id,
            survey_id=input.survey_id,
        ))

        if report.error:
            return PDFOutput(ok=False, error=report.error)

        if not report.parcelas:
            return PDFOutput(ok=False, error="No hay parcelas en el relevamiento.")

        pdf = _build_pdf(report)

        # Determinar path de salida
        out_dir = Path(input.output_path) if input.output_path else Path("/tmp")
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        region_slug = input.region_id.replace("-", "_")
        pdf_path = out_dir / f"relevamiento_{region_slug}_{ts}.pdf"

        pdf.output(str(pdf_path))

        return PDFOutput(
            ok=True,
            pdf_path=str(pdf_path),
            total_parcelas=report.total_parcelas,
        )

    except Exception as e:
        return PDFOutput(ok=False, error=str(e))
