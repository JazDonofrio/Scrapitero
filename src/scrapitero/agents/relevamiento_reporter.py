"""RelevamientoReporter — genera reporte de un survey catastral.

Devuelve info general + listado de parcelas con dirección, UF y nomenclatura.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.db.engine import get_engine


class ReporterInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None


class ParcelaResumen(BaseModel):
    parcela_id: str
    direccion: str
    calle: Optional[str]
    numero: Optional[str]
    area_m2: Optional[float]
    unidades_funcionales: Optional[int]
    nomenclatura_catastral: Optional[str]
    partida_inmobiliaria: Optional[str]
    cca_code: Optional[str]
    fuente: str


class RelevamientoReport(BaseModel):
    survey_id: str
    region_id: str
    region_nombre: str
    total_parcelas: int
    parcelas_con_direccion: int
    parcelas_con_uf: int
    parcelas_con_nomenclatura: int
    total_uf: int
    total_cocheras_estimadas: int
    area_total_m2: Optional[float]
    parcelas: list[ParcelaResumen]
    error: Optional[str] = None


def run(input: ReporterInput) -> RelevamientoReport:
    engine = get_engine()

    with engine.connect() as conn:
        # Resolver survey_id
        survey_id = input.survey_id
        if not survey_id:
            row = conn.execute(text("""
                SELECT survey_id FROM surveys
                WHERE region_id = :rid
                ORDER BY started_at DESC LIMIT 1
            """), {"rid": input.region_id}).fetchone()
            if not row:
                return RelevamientoReport(
                    survey_id="none", region_id=input.region_id,
                    region_nombre="", total_parcelas=0,
                    parcelas_con_direccion=0, parcelas_con_uf=0,
                    parcelas_con_nomenclatura=0, total_uf=0,
                    total_cocheras_estimadas=0, area_total_m2=None,
                    parcelas=[], error="No se encontró survey para esta región."
                )
            survey_id = str(row[0])

        # Nombre de región
        region_row = conn.execute(text(
            "SELECT name FROM regions WHERE region_id = :rid"
        ), {"rid": input.region_id}).fetchone()
        region_nombre = region_row[0] if region_row else input.region_id

        # Parcelas
        rows = conn.execute(text("""
            SELECT
                parcela_id::text,
                calle, numero,
                area_m2_terreno,
                unidades_funcionales_estimadas,
                nomenclatura_catastral,
                partida_inmobiliaria,
                cca_code,
                fuente_parcela
            FROM parcelas
            WHERE survey_id = :sid
            ORDER BY calle NULLS LAST, numero NULLS LAST
        """), {"sid": survey_id}).fetchall()

    parcelas_out = []
    total_uf = 0
    area_total = 0.0

    for r in rows:
        pid, calle, numero, area, uf, nomencla, partida, cca, fuente = r
        calle = calle or ""
        numero = numero or ""
        direccion = f"{calle} {numero}".strip() if calle else "(sin dirección)"
        total_uf += uf or 0
        area_total += area or 0.0

        parcelas_out.append(ParcelaResumen(
            parcela_id=pid,
            direccion=direccion,
            calle=calle or None,
            numero=numero or None,
            area_m2=area,
            unidades_funcionales=uf,
            nomenclatura_catastral=nomencla,
            partida_inmobiliaria=partida,
            cca_code=cca,
            fuente=fuente or "",
        ))

    total = len(rows)
    con_dir = sum(1 for p in parcelas_out if p.calle)
    con_uf = sum(1 for p in parcelas_out if p.unidades_funcionales is not None)
    con_nomencla = sum(1 for p in parcelas_out if p.nomenclatura_catastral)

    return RelevamientoReport(
        survey_id=survey_id,
        region_id=input.region_id,
        region_nombre=region_nombre,
        total_parcelas=total,
        parcelas_con_direccion=con_dir,
        parcelas_con_uf=con_uf,
        parcelas_con_nomenclatura=con_nomencla,
        total_uf=total_uf,
        total_cocheras_estimadas=0,
        area_total_m2=round(area_total, 2) if area_total else None,
        parcelas=parcelas_out,
    )
