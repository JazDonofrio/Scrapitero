"""ScopeCallesFilter — acota un survey a las calles + rangos de altura de su `scope_calles`.

En el modo "actualización por calle + rango", el survey se baja con un polígono generoso
(cubre las calles) y este agente hace el **filtro estricto por dirección**: ya con las
direcciones resueltas (BCI), borra del survey las parcelas cuya calle no esté en el scope o
cuyo número quede fuera del rango `[num_min, num_max]`. Conserva las parcelas con `calle` NULL
(dirección no resuelta; están dentro del buffer de la calle → conservador). Idempotente.

Corre como paso del VGPipelineRunner tras BCIParser, y por RPC.
"""

from __future__ import annotations

import re
from typing import Optional

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.direccion_norm import normalizar_calle
from scrapitero.db.engine import get_engine


class ScopeCallesInput(BaseModel):
    survey_id: str


class ScopeCallesOutput(BaseModel):
    ok: bool = True
    aplicado: bool = False          # False si el survey no tiene scope_calles (no-op)
    parcelas_antes: int = 0
    removidas_fuera_calle: int = 0  # calle no está en el scope
    removidas_fuera_rango: int = 0  # calle en scope pero número fuera de [min,max]
    conservadas: int = 0
    sin_direccion: int = 0          # calle NULL → conservadas igual
    error: Optional[str] = None


def _num(s) -> Optional[int]:
    m = re.search(r"\d+", str(s or ""))
    return int(m.group(0)) if m else None


@agent_run
def run(input: ScopeCallesInput) -> ScopeCallesOutput:
    out = ScopeCallesOutput()
    engine = get_engine()
    with engine.connect() as conn:
        scope = conn.execute(text(
            "SELECT scope_calles FROM surveys WHERE survey_id = CAST(:sid AS uuid)"),
            {"sid": input.survey_id}).scalar()
    if not scope:
        out.aplicado = False
        logger.info("ScopeCallesFilter: el survey no tiene scope_calles → no-op")
        return out

    # rangos por calle normalizada: {calle_norm: (min, max)}
    rangos: dict[str, tuple[int, int]] = {}
    for c in scope:
        cn = (c.get("calle_norm") or normalizar_calle(c.get("calle") or "")).strip()
        if not cn:
            continue
        mn = c.get("num_min"); mx = c.get("num_max")
        rangos[cn] = (int(mn) if mn is not None else 0,
                      int(mx) if mx is not None else 10**9)
    if not rangos:
        out.ok = False
        out.error = "scope_calles vacío o sin calles válidas"
        return out

    with engine.connect() as conn:
        filas = conn.execute(text("""
            SELECT parcela_id::text, calle, numero, fuente_parcela
            FROM parcelas WHERE survey_id = CAST(:sid AS uuid)
        """), {"sid": input.survey_id}).fetchall()

    out.parcelas_antes = len(filas)
    a_borrar: list[str] = []
    for pid, calle, numero, fuente in filas:
        # Entradas agregadas a mano (POIs de shopping sin parcela catastral, `fuente='shopping_poi'`)
        # son inclusiones intencionales fuera del corredor → nunca se borran por scope.
        if (fuente or "") == "shopping_poi":
            out.conservadas += 1
            continue
        if not (calle or "").strip():
            out.sin_direccion += 1
            continue                                  # sin dirección → conservar (conservador)
        cn = normalizar_calle(calle)
        rango = rangos.get(cn)
        if rango is None:
            out.removidas_fuera_calle += 1
            a_borrar.append(pid)
            continue
        n = _num(numero)
        if n is not None and not (rango[0] <= n <= rango[1]):
            out.removidas_fuera_rango += 1
            a_borrar.append(pid)
            continue
        out.conservadas += 1

    if a_borrar:
        with engine.begin() as conn:
            # borrar en lotes para no pasar el límite de parámetros
            for i in range(0, len(a_borrar), 1000):
                lote = a_borrar[i:i + 1000]
                conn.execute(text(
                    "DELETE FROM parcelas WHERE parcela_id = ANY(CAST(:ids AS uuid[]))"),
                    {"ids": lote})

    out.aplicado = True
    out.conservadas += out.sin_direccion
    logger.info(
        f"ScopeCallesFilter: {out.parcelas_antes} parcelas → borradas "
        f"{out.removidas_fuera_calle} (calle fuera de scope) + {out.removidas_fuera_rango} "
        f"(número fuera de rango); conservadas {out.conservadas} "
        f"({out.sin_direccion} sin dirección)")
    return out
