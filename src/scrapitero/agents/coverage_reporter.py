"""CoverageReporter — estado actual de cobertura de un survey.

Devuelve un JSON compacto (~150 tokens) con métricas del snapshot activo.
Es el único output que lee el LLM orquestador para decidir el próximo agente.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel
from sqlalchemy import text
from loguru import logger

from scrapitero.db.engine import get_engine


class CoverageInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None  # si None, usa el survey más reciente


class CoverageReport(BaseModel):
    survey_id: str
    region_id: str
    step: int
    # Conteos Brasil (edificios OSM como unidad principal)
    setores: int
    pop_total_ibge: int
    logradouros_count: int
    footprints: int                  # edificios OSM descargados
    footprints_con_setor: int
    edificios_con_direccion: int     # edificios con dirección resuelta
    # Conteos Argentina (parcelas catastrales)
    parcelas: int
    parcelas_con_direccion: int
    parcelas_con_habitantes: int
    # Ratios
    cobertura_footprints_pct: float
    cobertura_direccion_pct: float   # sobre edificios (BR) o parcelas (AR)
    cobertura_habitantes_pct: float
    # Validación
    suma_hab_vs_ibge_delta_pct: float
    errores: list[str]


def run(input: CoverageInput) -> CoverageReport:
    engine = get_engine()
    errores = []

    with engine.connect() as conn:
        # Resolver survey_id
        if input.survey_id:
            survey_id = input.survey_id
        else:
            row = conn.execute(text("""
                SELECT survey_id FROM surveys
                WHERE region_id = :rid
                ORDER BY started_at DESC LIMIT 1
            """), {"rid": input.region_id}).fetchone()

            if not row:
                # No hay survey — devolver reporte vacío
                return CoverageReport(
                    survey_id="none", region_id=input.region_id, step=0,
                    setores=0, pop_total_ibge=0, logradouros_count=0,
                    footprints=0, footprints_con_setor=0, edificios_con_direccion=0,
                    parcelas=0, parcelas_con_direccion=0, parcelas_con_habitantes=0,
                    cobertura_footprints_pct=0.0, cobertura_direccion_pct=0.0,
                    cobertura_habitantes_pct=0.0,
                    suma_hab_vs_ibge_delta_pct=0.0, errores=["no_survey_found"]
                )
            survey_id = str(row[0])

        # Setores censitários
        setores = conn.execute(text(
            "SELECT COUNT(*) FROM setores_censitarios WHERE region_id = :rid"
        ), {"rid": input.region_id}).scalar() or 0

        pop_total_ibge = conn.execute(text(
            "SELECT COALESCE(SUM(pop_total), 0) FROM setores_censitarios WHERE region_id = :rid"
        ), {"rid": input.region_id}).scalar() or 0

        # Logradouros (Brasil)
        logradouros_count = conn.execute(text(
            "SELECT COUNT(*) FROM logradouros WHERE region_id = :rid"
        ), {"rid": input.region_id}).scalar() or 0

        # Edificios / footprints (Brasil)
        footprints = conn.execute(text(
            "SELECT COUNT(*) FROM edificios WHERE survey_id = :sid"
        ), {"sid": survey_id}).scalar() or 0

        footprints_con_setor = conn.execute(text(
            "SELECT COUNT(*) FROM edificios WHERE survey_id = :sid AND setor_censitario_id IS NOT NULL"
        ), {"sid": survey_id}).scalar() or 0

        # TODO: cuando address_resolver soporte edificios, agregar columna endereco a la tabla
        edificios_con_direccion = 0

        # Parcelas (Argentina)
        parcelas = conn.execute(text(
            "SELECT COUNT(*) FROM parcelas WHERE survey_id = :sid"
        ), {"sid": survey_id}).scalar() or 0

        parcelas_con_direccion = conn.execute(text(
            "SELECT COUNT(*) FROM parcelas WHERE survey_id = :sid AND calle IS NOT NULL"
        ), {"sid": survey_id}).scalar() or 0

        parcelas_con_habitantes = conn.execute(text(
            "SELECT COUNT(*) FROM parcelas WHERE survey_id = :sid AND habitantes_estimados IS NOT NULL"
        ), {"sid": survey_id}).scalar() or 0

        # Validación: suma habitantes vs IBGE
        suma_hab = conn.execute(text(
            "SELECT COALESCE(SUM(habitantes_estimados), 0) FROM parcelas WHERE survey_id = :sid"
        ), {"sid": survey_id}).scalar() or 0

        # Step actual
        step = conn.execute(text(
            "SELECT COUNT(*) FROM orchestrator_log WHERE survey_id = :sid"
        ), {"sid": survey_id}).scalar() or 0

    # Calcular ratios
    cobertura_footprints_pct = round(footprints_con_setor / max(footprints, 1), 4) if footprints > 0 else 0.0
    # Dirección: usar edificios para Brasil (parcelas==0), parcelas para Argentina
    if parcelas > 0:
        cobertura_direccion_pct = round(parcelas_con_direccion / parcelas, 4)
    elif footprints > 0:
        cobertura_direccion_pct = round(edificios_con_direccion / footprints, 4)
    else:
        cobertura_direccion_pct = 0.0
    cobertura_habitantes_pct = round(parcelas_con_habitantes / max(parcelas, 1), 4) if parcelas > 0 else 0.0

    ibge_base = max(pop_total_ibge, 1)
    suma_delta = round(abs(suma_hab - pop_total_ibge) / ibge_base, 4) if pop_total_ibge > 0 else 0.0

    return CoverageReport(
        survey_id=survey_id,
        region_id=input.region_id,
        step=int(step),
        setores=int(setores),
        pop_total_ibge=int(pop_total_ibge),
        logradouros_count=int(logradouros_count),
        footprints=int(footprints),
        footprints_con_setor=int(footprints_con_setor),
        edificios_con_direccion=int(edificios_con_direccion),
        parcelas=int(parcelas),
        parcelas_con_direccion=int(parcelas_con_direccion),
        parcelas_con_habitantes=int(parcelas_con_habitantes),
        cobertura_footprints_pct=cobertura_footprints_pct,
        cobertura_direccion_pct=cobertura_direccion_pct,
        cobertura_habitantes_pct=cobertura_habitantes_pct,
        suma_hab_vs_ibge_delta_pct=suma_delta,
        errores=errores,
    )
