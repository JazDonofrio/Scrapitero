"""BaselineConsistenciaCheck — QA del geocoding de un relevamiento anterior por consistencia
de número.

Chequeo simple y de **bajo ruido** para revisar geocodes mal ubicados que la guarda de
consistencia (`baseline_interp`) no agarra (calles con pocas direcciones, etc.): en una misma
calle, dos direcciones con números **cercanos** (Δ ≤ `delta_num`) tienen que estar **cerca** en
el espacio (casas con números contiguos son adyacentes en cualquier calle, sin importar la
densidad). Si están **lejos** (> `dist_m`), una de las dos está mal geocodificada.

**Solo lectura / reporte** (no toca la DB): devuelve la lista de pares sospechosos con la fuente
de cada uno (g:numero/mapbox/osm_interp/…) y la distancia, para revisión humana. Marca como
`sospechoso` el de la fuente menos confiable del par (g:numero = geocodebr exacto manda). Excluye
las direcciones `ciudad` (ya marcadas como aproximadas, no son errores a revisar).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.baseline_interp import _hav_m, _num
from scrapitero.agents.direccion_norm import normalizar_calle
from scrapitero.db.engine import get_engine

# Confiabilidad de la fuente para decidir cuál del par es el sospechoso (mayor = más confiable).
_PRIO = {"g:numero": 5, "osm_interp": 3, "interp": 3, "mapbox": 2, "nominatim": 2,
         "g:cep": 1, "g:numero_aproximado": 1, "ciudad": 0}


class ConsistenciaInput(BaseModel):
    baseline_id: str
    delta_num: int = 30        # diferencia de número considerada "cercana"
    dist_m: float = 400.0      # distancia considerada "lejos" para números cercanos


class ConsistenciaOutput(BaseModel):
    ok: bool = True
    calles_con_casos: int = 0
    pares_sospechosos: int = 0
    direcciones_sospechosas: int = 0
    casos: list[dict] = []     # {calle, num_a, src_a, num_b, src_b, dist_m, sospechoso}
    error: Optional[str] = None


@agent_run
def run(input: ConsistenciaInput) -> ConsistenciaOutput:
    out = ConsistenciaOutput()
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT calle, numero, lat, lng, geocode_source
            FROM baseline_direcciones
            WHERE baseline_id = :b AND lat IS NOT NULL AND calle IS NOT NULL
        """), {"b": input.baseline_id}).fetchall()

    por: dict = defaultdict(list)
    for calle, num, la, ln, src in rows:
        n = _num(num)
        if n is None:
            continue
        por[normalizar_calle(calle)].append((n, float(la), float(ln), src or "", calle, num))

    sospechosas: set = set()
    calles: set = set()
    casos: list[dict] = []
    for cn, its in por.items():
        for i in range(len(its)):
            for j in range(i + 1, len(its)):
                a, b = its[i], its[j]
                if a[0] == b[0]:
                    continue
                if a[3] == "ciudad" or b[3] == "ciudad":   # aproximadas: no son errores a revisar
                    continue
                if abs(a[0] - b[0]) > input.delta_num:
                    continue
                d = _hav_m(a[1], a[2], b[1], b[2])
                if d <= input.dist_m:
                    continue
                # el sospechoso = el de fuente menos confiable (g:numero manda)
                susp = b if _PRIO.get(a[3], 1) >= _PRIO.get(b[3], 1) else a
                casos.append({
                    "calle": a[4], "num_a": a[5], "src_a": a[3], "num_b": b[5], "src_b": b[3],
                    "dist_m": round(d), "sospechoso": susp[5],
                })
                sospechosas.add((cn, susp[5]))
                calles.add(cn)

    casos.sort(key=lambda c: c["dist_m"], reverse=True)
    out.casos = casos
    out.pares_sospechosos = len(casos)
    out.direcciones_sospechosas = len(sospechosas)
    out.calles_con_casos = len(calles)
    logger.info(f"consistencia_check {input.baseline_id}: {len(casos)} pares sospechosos en "
                f"{len(calles)} calles ({len(sospechosas)} direcciones a revisar)")
    return out
