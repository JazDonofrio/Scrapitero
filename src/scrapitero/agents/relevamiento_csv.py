"""RelevamientoCSV - genera un CSV del reporte de relevamiento catastral (compatible Google Sheets)."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from scrapitero.agents.relevamiento_reporter import ReporterInput, RelevamientoReport, run as get_report


class CSVInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None
    output_path: Optional[str] = None  # si None, usa /tmp/


class CSVOutput(BaseModel):
    ok: bool
    csv_path: Optional[str] = None
    total_parcelas: int = 0
    error: Optional[str] = None


def _build_csv(report: RelevamientoReport, csv_path: Path) -> None:
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")

        # Encabezado del reporte
        writer.writerow(["REPORTE DE RELEVAMIENTO CATASTRAL"])
        writer.writerow(["Región", report.region_nombre])
        writer.writerow(["Survey ID", report.survey_id])
        writer.writerow(["Fecha", datetime.now().strftime("%d/%m/%Y %H:%M")])
        writer.writerow(["Total parcelas", report.total_parcelas])
        writer.writerow(["Con dirección", report.parcelas_con_direccion])
        writer.writerow(["Con UF", report.parcelas_con_uf])
        writer.writerow(["Con nomenclatura", report.parcelas_con_nomenclatura])
        writer.writerow(["Total UF", report.total_uf])
        writer.writerow(["Área total relevada (m²)", f"{report.area_total_m2:,.1f}" if report.area_total_m2 else "-"])
        writer.writerow([])

        # Tabla de parcelas
        writer.writerow(["N°", "Dirección", "Área m²", "UF", "Nomenclatura Catastral", "Partida Inmobiliaria"])
        for i, p in enumerate(report.parcelas, 1):
            writer.writerow([
                i,
                p.direccion or "-",
                f"{p.area_m2:.1f}" if p.area_m2 else "-",
                p.unidades_funcionales if p.unidades_funcionales is not None else "-",
                p.nomenclatura_catastral or "-",
                p.partida_inmobiliaria or "-",
            ])


def run(input: CSVInput) -> CSVOutput:
    try:
        report = get_report(ReporterInput(
            region_id=input.region_id,
            survey_id=input.survey_id,
        ))

        if report.error:
            return CSVOutput(ok=False, error=report.error)

        if not report.parcelas:
            return CSVOutput(ok=False, error="No hay parcelas en el relevamiento.")

        out_dir = Path(input.output_path) if input.output_path else Path("/tmp")
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        region_slug = input.region_id.replace("-", "_")
        csv_path = out_dir / f"relevamiento_{region_slug}_{ts}.csv"

        _build_csv(report, csv_path)

        return CSVOutput(
            ok=True,
            csv_path=str(csv_path),
            total_parcelas=report.total_parcelas,
        )

    except Exception as e:
        return CSVOutput(ok=False, error=str(e))
