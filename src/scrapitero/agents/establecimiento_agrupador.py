"""EstablecimientoAgrupador — agrupa parcelas que son un único establecimiento.

Problema: a veces UNA entidad real (una fábrica, un colegio, una iglesia, un
galpón comercial…) ocupa VARIAS parcelas catastrales. Sin agrupar, el sistema
cuenta esas N parcelas como N unidades funcionales (o N baldíos), cuando en
realidad son UNA sola UF. Este agente las detecta y las cuenta como 1.

Regla de detección (conservadora, multi-señal — ver memoria del proyecto):
  1. MISMO PROPIETARIO REAL: parcelas con el mismo `propietario_documento`,
     descartando documentos sentinela (todos los dígitos iguales: 999.999.999-99,
     000…) y NULL. Se prioriza CNPJ (persona jurídica, lleva '/').
  2. CONTIGUAS: forman un bloque adyacente (componente conexa del grafo donde hay
     arista si dos parcelas del mismo dueño están a ≤ `max_dist_m` metros). No se
     agrupan parcelas sueltas del mismo dueño dispersas por la zona.
  3. USO NO ENTERAMENTE RESIDENCIAL:
       - bloque todo residencial  → NO se agrupa (3 casas de un dueño = 3 viviendas);
       - tiene comercial/industrial/mixto/equipamiento → se agrupa;
       - sólo vacante (la fábrica registrada como lotes baldíos) → se agrupa **sólo
         si el dueño es CNPJ** (terreno de una empresa), no si es CPF (banco de tierra).

Cada componente que califica se guarda en `establecimientos` (1 fila), con la unión
geométrica de sus parcelas. La UF de la entidad = la del **miembro más desarrollado**
(mínimo 1), NO la suma: una fábrica sobre 6 lotes de 1 UF (o baldíos) cuenta 1, pero una
parcela que ya tiene varias UF reales (p.ej. `uf_comercio=5`) NO se colapsa — la entidad
hereda esas 5. Las parcelas miembro quedan vinculadas por `parcelas.establecimiento_id` y
conservan intactos sus datos; el conteo de la web/CSV/reporter cuenta el establecimiento
una sola vez (su UF), no la de cada parcela miembro.

Genérico: el grafo de adyacencia + componentes conexas sirve para cualquier región;
la señal de propietario viene del BCI (Brasil). Idempotente por survey: cada corrida
borra los establecimientos previos del survey y recalcula.
"""

from __future__ import annotations

import re
import uuid
from typing import Optional

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text, bindparam

from scrapitero.db.engine import get_engine
from scrapitero.agents._run import agent_run

# Usos que NO son vivienda (habilitan la agrupación de un bloque contiguo).
_USOS_NO_RESIDENCIALES = {"comercial", "industrial", "mixto", "equipamiento"}

# Tipificación del establecimiento por palabras clave en la razón social.
# (orden = prioridad; se evalúa sobre el nombre en mayúsculas, sin acentos suele bastar
#  porque el BCI ya viene en mayúsculas; igual contemplamos ambas grafías)
_TIPO_KEYWORDS = [
    ("fabrica",              ["INDUSTRIA", "INDÚSTRIA", "FABRICA", "FÁBRICA", "METALURG", "METALÚRG", "USINA"]),
    ("iglesia",              ["IGREJA", "PAROQUIA", "PARÓQUIA", "ASSEMBLEIA", "TEMPLO", "CATEDRAL", "DIOCESE"]),
    ("colegio",              ["COLEGIO", "COLÉGIO", "ESCOLA", "EDUCAC", "EDUCAÇ", "CRECHE", "UNIVERS", "FACULDADE", "INSTITUTO EDUC"]),
    ("salud",                ["HOSPITAL", "CLINICA", "CLÍNICA", "SAUDE", "SAÚDE", "UPA ", "PRONTO SOCORRO", "LABORATORIO"]),
    ("equipamiento_publico", ["PREFEITURA", "MUNICIPIO", "MUNICÍPIO", "ESTADO DE", "SECRETARIA", "GOVERNO", "UNIAO", "UNIÃO"]),
    ("comercio",             ["SUPERMERCADO", "ATACAD", "COMERCIO", "COMÉRCIO", "DISTRIBUIDORA", "POSTO ", "SHOPPING"]),
]

_USO_POR_TIPO = {
    "fabrica": "industrial",
    "iglesia": "equipamiento",
    "colegio": "equipamiento",
    "salud": "equipamiento",
    "equipamiento_publico": "equipamiento",
    "comercio": "comercial",
}


class AgrupadorInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None    # si falta, el survey más reciente de la región
    max_dist_m: float = 2.0            # distancia máx. entre parcelas para considerarlas contiguas
    min_parcelas: int = 2              # mínimo de parcelas para formar un establecimiento


class AgrupadorOutput(BaseModel):
    ok: bool
    region_id: Optional[str] = None
    survey_id: Optional[str] = None
    establecimientos: int = 0
    parcelas_agrupadas: int = 0
    uf_ahorradas: int = 0              # UF que se dejan de contar por la agrupación
    por_tipo: dict = {}
    error: Optional[str] = None


# ── Helpers ─────────────────────────────────────────────────────────────────

def _es_sentinela(doc: Optional[str]) -> bool:
    """True si el documento no es un propietario real (placeholder)."""
    if not doc:
        return True
    digs = re.sub(r"\D", "", doc)
    if len(digs) < 8:
        return True
    return len(set(digs)) == 1            # todos los dígitos iguales (000…, 999…)


def _es_cnpj(doc: str) -> bool:
    """CNPJ (persona jurídica) lleva '/'; CPF (persona física) no."""
    return "/" in (doc or "")


def _tipificar(nombre: Optional[str]) -> str:
    up = (nombre or "").upper()
    for tipo, kws in _TIPO_KEYWORDS:
        if any(kw in up for kw in kws):
            return tipo
    return "establecimiento"


def _resolve_survey(conn, region_id: str, survey_id: Optional[str]) -> Optional[str]:
    if survey_id:
        return survey_id
    row = conn.execute(text(
        "SELECT survey_id::text FROM surveys WHERE region_id=:r ORDER BY started_at DESC LIMIT 1"
    ), {"r": region_id}).fetchone()
    return row[0] if row else None


class _UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


# ── Entry point ─────────────────────────────────────────────────────────────

@agent_run
def run(input: AgrupadorInput) -> AgrupadorOutput:
    engine = get_engine()
    with engine.begin() as conn:
        survey_id = _resolve_survey(conn, input.region_id, input.survey_id)
        if not survey_id:
            return AgrupadorOutput(ok=False, region_id=input.region_id,
                                   error="No se encontró survey para la región.")

        # Parcelas con propietario + uso (las que tienen geometría sirven para adyacencia)
        rows = conn.execute(text("""
            SELECT parcela_id::text, propietario_documento, propietario_nombre,
                   uso_principal, COALESCE(uf_vivienda,0), COALESCE(uf_comercio,0),
                   COALESCE(unidades_funcionales_estimadas,0)
            FROM parcelas
            WHERE survey_id = :sid AND geometry IS NOT NULL
        """), {"sid": survey_id}).fetchall()

        info = {}                      # parcela_id -> dict
        por_doc: dict[str, list[str]] = {}
        for pid, doc, nombre, uso, ufv, ufc, ufe in rows:
            info[pid] = {"doc": doc, "nombre": nombre, "uso": uso,
                         "ufv": ufv, "ufc": ufc, "ufe": ufe}
            if _es_sentinela(doc):
                continue
            # Una parcela entra al clúster sólo si es "agrupable":
            #   - tiene actividad real (comercial/industrial/mixto/equipamiento), o
            #   - el dueño es CNPJ (empresa/institución): entonces hasta su vivienda o
            #     baldío forman parte del predio (la fábrica registrada como lotes baldíos).
            # Las viviendas/baldíos de un CPF NO se absorben (son unidades separadas).
            if (uso in _USOS_NO_RESIDENCIALES) or _es_cnpj(doc):
                por_doc.setdefault(doc, []).append(pid)

        # Documentos candidatos: dueños reales con ≥2 parcelas en la zona
        candidatos = {doc: pids for doc, pids in por_doc.items() if len(pids) >= input.min_parcelas}
        logger.info(f"Agrupador: {len(rows)} parcelas, {len(candidatos)} propietarios con ≥{input.min_parcelas} parcelas")

        # Limpieza idempotente del survey
        conn.execute(text("UPDATE parcelas SET establecimiento_id=NULL "
                          "WHERE survey_id=:sid AND establecimiento_id IS NOT NULL"), {"sid": survey_id})
        conn.execute(text("DELETE FROM establecimientos WHERE survey_id=:sid"), {"sid": survey_id})

        if not candidatos:
            return AgrupadorOutput(ok=True, region_id=input.region_id, survey_id=survey_id,
                                   establecimientos=0, parcelas_agrupadas=0)

        # Aristas de adyacencia DENTRO de cada grupo de mismo dueño
        all_pids = [pid for pids in candidatos.values() for pid in pids]
        edge_q = text("""
            SELECT a.parcela_id::text, b.parcela_id::text
            FROM parcelas a JOIN parcelas b
              ON a.parcela_id < b.parcela_id
             AND a.propietario_documento = b.propietario_documento
             AND ST_DWithin(a.geometry::geography, b.geometry::geography, :dist)
            WHERE a.survey_id = :sid AND b.survey_id = :sid
              AND a.parcela_id::text IN :ids AND b.parcela_id::text IN :ids
        """).bindparams(bindparam("ids", expanding=True))
        edges = conn.execute(edge_q, {"sid": survey_id, "dist": input.max_dist_m, "ids": all_pids}).fetchall()

        # Componentes conexas SOLO entre parcelas del mismo dueño (la arista ya garantiza
        # mismo dueño + contigüidad)
        uf = _UnionFind()
        for pid in all_pids:
            uf.find(pid)
        for a, b in edges:
            uf.union(a, b)

        comps: dict[str, list[str]] = {}
        for pid in all_pids:
            comps.setdefault(uf.find(pid), []).append(pid)

        creados = 0
        parcelas_agrupadas = 0
        uf_ahorradas = 0
        por_tipo: dict[str, int] = {}

        for members in comps.values():
            if len(members) < input.min_parcelas:
                continue
            usos = {info[m]["uso"] for m in members}
            doc = info[members[0]]["doc"]
            nombre = next((info[m]["nombre"] for m in members if info[m]["nombre"]), None)

            # Bloque enteramente residencial (sólo posible con dueño CNPJ, p.ej. casas
            # reposeídas de un banco) → no es un establecimiento.
            if all(info[m]["uso"] == "residencial" for m in members):
                continue

            tipo = _tipificar(nombre)
            uso_est = _USO_POR_TIPO.get(tipo)
            if not uso_est:
                uso_est = "industrial" if "industrial" in usos else (
                    "comercial" if "comercial" in usos else (
                        "equipamiento" if "equipamiento" in usos else "comercial"))

            # UF del establecimiento = la del miembro MÁS DESARROLLADO (mínimo 1). Así una
            # fábrica sobre 6 lotes de 1 UF (o baldíos) cuenta 1, pero una parcela que ya
            # tiene varias UF reales (p.ej. uf_comercio=5, una tira de locales) NO se
            # colapsa a 1: la entidad hereda esas 5. No se SUMAN las UF de las demás
            # parcelas (esa suma era el sobreconteo que estamos corrigiendo).
            def _mtot(m: str) -> int:
                t = info[m]["ufv"] + info[m]["ufc"]
                return t if t > 0 else (info[m]["ufe"] or 0)

            richest = max(members, key=_mtot)
            r_v, r_c = info[richest]["ufv"], info[richest]["ufc"]
            if (r_v + r_c) <= 1:
                est_v, est_c = 0, 1                  # entidad = 1 unidad no residencial
            else:
                est_v, est_c = r_v, r_c

            uf_actual = sum(_mtot(m) for m in members)
            uf_ahorradas += max(uf_actual - (est_v + est_c), 0)

            est_id = str(uuid.uuid4())
            ins = text("""
                INSERT INTO establecimientos
                    (establecimiento_id, survey_id, region_id, tipo, nombre, uso_principal,
                     uf_vivienda, uf_comercio, n_parcelas, area_m2,
                     propietario_documento, fuente, geometry)
                SELECT :eid, :sid, :rid, :tipo, :nombre, :uso,
                       :uf_v, :uf_c, :n, ST_Area(ST_Union(geometry)::geography),
                       :doc, 'agrupador_propietario', ST_Multi(ST_Union(geometry))
                FROM parcelas WHERE parcela_id::text IN :ids
            """).bindparams(bindparam("ids", expanding=True))
            conn.execute(ins, {
                "eid": est_id, "sid": survey_id, "rid": input.region_id,
                "tipo": tipo, "nombre": (nombre or "")[:250] or None, "uso": uso_est,
                "uf_v": est_v, "uf_c": est_c,
                "n": len(members), "doc": doc, "ids": members,
            })
            upd = text("UPDATE parcelas SET establecimiento_id=:eid "
                       "WHERE parcela_id::text IN :ids").bindparams(bindparam("ids", expanding=True))
            conn.execute(upd, {"eid": est_id, "ids": members})

            creados += 1
            parcelas_agrupadas += len(members)
            por_tipo[tipo] = por_tipo.get(tipo, 0) + 1
            logger.info(f"Agrupador: establecimiento {tipo} '{(nombre or '')[:40]}' "
                        f"({len(members)} parcelas, doc {doc})")

    logger.info(f"Agrupador completo '{input.region_id}': {creados} establecimientos, "
                f"{parcelas_agrupadas} parcelas agrupadas, {uf_ahorradas} UF deduplicadas")
    return AgrupadorOutput(
        ok=True, region_id=input.region_id, survey_id=survey_id,
        establecimientos=creados, parcelas_agrupadas=parcelas_agrupadas,
        uf_ahorradas=uf_ahorradas, por_tipo=por_tipo,
    )
