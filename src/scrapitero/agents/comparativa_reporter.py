"""ComparativaReporter — compara un survey actual contra un relevamiento anterior.

El término "anterior" puede ser:
  - otro **survey de la misma región** (incl. archivados): match primero por
    `cca_code` (identidad catastral exacta), dirección normalizada como fallback;
  - un **baseline importado** (CSV externo del cliente, tabla `baselines`):
    match por dirección normalizada (exacta y luego fuzzy con difflib).

Clasifica cada dirección/parcela en `nueva` / `desaparecida` / `cambio` / `igual`
y devuelve KPIs de delta (UF vivienda/comercio, direcciones) más una estimación
secundaria de Δhabitantes (ΔUF vivienda × habitantes/domicilio del censo de la
región, si hay setores censitarios cargados).

No persiste nada: es un join calculado on-the-fly.
"""

from __future__ import annotations

import difflib
from typing import Optional

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.direccion_norm import clave_direccion
from scrapitero.db.engine import get_engine

FUZZY_UMBRAL_DEFAULT = 0.78


class ComparativaInput(BaseModel):
    survey_id: str
    contra_survey_id: Optional[str] = None      # exactamente uno de los dos
    contra_baseline_id: Optional[str] = None
    fuzzy_umbral: float = FUZZY_UMBRAL_DEFAULT


class ComparativaOutput(BaseModel):
    ok: bool
    error: Optional[str] = None
    survey_id: str = ""
    contra_tipo: str = ""                       # "survey" | "baseline"
    contra_id: str = ""
    contra_nombre: str = ""
    contra_fecha: Optional[str] = None          # fecha del relevamiento anterior
    metodo_match: str = ""                      # "cca+direccion" | "direccion"
    kpis: dict = {}
    # nueva/desaparecida/cambio/igual — una fila por dirección (o por parcela
    # catastral cuando el match es por cca_code)
    filas: list[dict] = []
    # parcela_id (survey actual) → estado, para colorear el mapa
    parcelas_estado: dict = {}
    sin_direccion_ahora: int = 0                # parcelas actuales sin calle (no comparables)
    matches_fuzzy: int = 0


# ── Carga de cada lado como dict clave → agregado ─────────────────────────────

def _agg_nuevo() -> dict:
    return {"calle": "", "numero": "", "uso": None, "usos": {}, "uf_viv": 0,
            "uf_com": 0, "n": 0, "lat": None, "lng": None, "parcela_ids": [],
            "ccas": []}


def _acumular(agg: dict, calle, numero, uso, uf_viv, uf_com, lat=None, lng=None,
              parcela_id=None, cca=None) -> None:
    if not agg["calle"] and calle:
        agg["calle"], agg["numero"] = calle, numero or ""
    if uso:
        u = str(uso).strip().lower()
        agg["usos"][u] = agg["usos"].get(u, 0) + 1
        # uso del grupo = el más frecuente (desempate alfabético, determinista)
        agg["uso"] = min(agg["usos"], key=lambda k: (-agg["usos"][k], k))
    agg["uf_viv"] += int(uf_viv or 0)
    agg["uf_com"] += int(uf_com or 0)
    agg["n"] += 1
    if agg["lat"] is None and lat is not None:
        agg["lat"], agg["lng"] = float(lat), float(lng)
    if parcela_id:
        agg["parcela_ids"].append(parcela_id)
    if cca:
        agg["ccas"].append(cca)


def _cargar_survey(conn, survey_id: str) -> list[dict]:
    """Parcelas del survey con la UF efectiva (mismo criterio que la web: sin
    desglose viv/com, unidades_funcionales_estimadas cuenta como vivienda)."""
    rows = conn.execute(text("""
        SELECT parcela_id::text, cca_code, calle, numero, uso_principal,
               CASE WHEN uf_vivienda IS NULL AND uf_comercio IS NULL
                    THEN COALESCE(unidades_funcionales_estimadas, 0)
                    ELSE COALESCE(uf_vivienda, 0) END AS uf_viv,
               COALESCE(uf_comercio, 0) AS uf_com,
               centroid_lat, centroid_lng
        FROM parcelas WHERE survey_id = :sid
    """), {"sid": survey_id}).fetchall()
    return [{"parcela_id": r[0], "cca": r[1], "calle": r[2], "numero": r[3],
             "uso": r[4], "uf_viv": r[5], "uf_com": r[6], "lat": r[7], "lng": r[8]}
            for r in rows]


def _por_clave(parcelas: list[dict]) -> tuple[dict[str, dict], int]:
    """Agrupa parcelas por clave de dirección normalizada. Devuelve (grupos,
    cantidad sin dirección)."""
    grupos: dict[str, dict] = {}
    sin_dir = 0
    for p in parcelas:
        # tolerante=True: matchea por núcleo de calle (ignora Rua/Avenida/Travessa + títulos),
        # así el catastro y el relevamiento anterior no se separan por rotular distinto la vía.
        clave = clave_direccion(p["calle"], p["numero"], tolerante=True)
        if not clave:
            sin_dir += 1
            continue
        agg = grupos.setdefault(clave, _agg_nuevo())
        _acumular(agg, p["calle"], p["numero"], p["uso"], p["uf_viv"], p["uf_com"],
                  p["lat"], p["lng"], p["parcela_id"], p.get("cca"))
    return grupos, sin_dir


def _cargar_baseline(conn, baseline_id: str) -> dict[str, dict]:
    rows = conn.execute(text("""
        SELECT calle, numero, calle_norm, numero_norm, uso, uf_vivienda, uf_comercio,
               direccion_raw
        FROM baseline_direcciones WHERE baseline_id = :bid
    """), {"bid": baseline_id}).fetchall()
    grupos: dict[str, dict] = {}
    for calle, numero, calle_norm, numero_norm, uso, uf_v, uf_c, raw in rows:
        # misma clave tolerante que el lado parcelas (núcleo de calle, ignora tipo de vía +
        # título) — recomputada del `calle` crudo, no de la columna `calle_norm` (que conserva
        # el tipo). Sin esto, "Rua X" (baseline) nunca matchea "Avenida X" (catastro).
        clave = clave_direccion(calle, numero, tolerante=True)
        if not clave:
            continue
        agg = grupos.setdefault(clave, _agg_nuevo())
        _acumular(agg, calle or raw, numero, uso, uf_v, uf_c)
    return grupos


# ── Matching ───────────────────────────────────────────────────────────────────

def _match_claves(antes: dict[str, dict], ahora: dict[str, dict],
                  umbral: float) -> tuple[list[tuple[str, str, str]], int]:
    """Empareja claves de dirección: exacto primero, después fuzzy (misma altura,
    calle similar por difflib). Devuelve ([(clave_antes, clave_ahora, tipo)], fuzzy)."""
    pares: list[tuple[str, str, str]] = []
    usadas_ahora: set[str] = set()
    pendientes_antes = []
    for k in antes:
        if k in ahora:
            pares.append((k, k, "exacto"))
            usadas_ahora.add(k)
        else:
            pendientes_antes.append(k)

    fuzzy = 0
    # candidatos del lado "ahora" agrupados por número (la altura debe coincidir)
    por_numero: dict[str, list[str]] = {}
    for k in ahora:
        if k in usadas_ahora:
            continue
        por_numero.setdefault(k.rsplit("|", 1)[1], []).append(k)

    for k in pendientes_antes:
        calle_a, num_a = k.rsplit("|", 1)
        mejor, mejor_ratio = None, 0.0
        for kc in por_numero.get(num_a, []):
            if kc in usadas_ahora:
                continue
            ratio = difflib.SequenceMatcher(None, calle_a, kc.rsplit("|", 1)[0]).ratio()
            if ratio > mejor_ratio:
                mejor, mejor_ratio = kc, ratio
        if mejor and mejor_ratio >= umbral:
            pares.append((k, mejor, "fuzzy"))
            usadas_ahora.add(mejor)
            fuzzy += 1
    return pares, fuzzy


def _direccion_display(agg: dict) -> str:
    d = " ".join(str(x) for x in (agg["calle"], agg["numero"]) if x).strip()
    return d or "(sin dirección)"


def _comparar(antes: dict[str, dict], ahora: dict[str, dict],
              umbral: float) -> tuple[list[dict], dict[str, str], int]:
    """Compara los dos lados ya agrupados por clave. Devuelve (filas,
    parcelas_estado, matches_fuzzy)."""
    pares, fuzzy = _match_claves(antes, ahora, umbral)
    filas: list[dict] = []
    parcelas_estado: dict[str, str] = {}
    matcheadas_antes = {p[0] for p in pares}
    matcheadas_ahora = {p[1] for p in pares}

    for k_antes, k_ahora, tipo in pares:
        a, b = antes[k_antes], ahora[k_ahora]
        cambio_uso = bool(a["uso"] and b["uso"] and a["uso"] != b["uso"])
        cambio_uf = (a["uf_viv"] != b["uf_viv"]) or (a["uf_com"] != b["uf_com"])
        estado = "cambio" if (cambio_uso or cambio_uf) else "igual"
        filas.append({
            "estado": estado, "match": tipo,
            "direccion": _direccion_display(b),
            "direccion_antes": _direccion_display(a),
            "uso_antes": a["uso"], "uso_ahora": b["uso"],
            "uf_viv_antes": a["uf_viv"], "uf_viv_ahora": b["uf_viv"],
            "uf_com_antes": a["uf_com"], "uf_com_ahora": b["uf_com"],
            "lat": b["lat"], "lng": b["lng"],
        })
        for pid in b["parcela_ids"]:
            parcelas_estado[pid] = estado

    for k, b in ahora.items():
        if k in matcheadas_ahora:
            continue
        filas.append({
            "estado": "nueva", "match": None,
            "direccion": _direccion_display(b), "direccion_antes": None,
            "uso_antes": None, "uso_ahora": b["uso"],
            "uf_viv_antes": 0, "uf_viv_ahora": b["uf_viv"],
            "uf_com_antes": 0, "uf_com_ahora": b["uf_com"],
            "lat": b["lat"], "lng": b["lng"],
        })
        for pid in b["parcela_ids"]:
            parcelas_estado[pid] = "nueva"

    for k, a in antes.items():
        if k in matcheadas_antes:
            continue
        filas.append({
            "estado": "desaparecida", "match": None,
            "direccion": _direccion_display(a), "direccion_antes": _direccion_display(a),
            "uso_antes": a["uso"], "uso_ahora": None,
            "uf_viv_antes": a["uf_viv"], "uf_viv_ahora": 0,
            "uf_com_antes": a["uf_com"], "uf_com_ahora": 0,
            "lat": a["lat"], "lng": a["lng"],
        })

    orden = {"nueva": 0, "cambio": 1, "desaparecida": 2, "igual": 3}
    filas.sort(key=lambda f: (orden.get(f["estado"], 9), f["direccion"]))
    return filas, parcelas_estado, fuzzy


def _comparar_por_cca(antes_p: list[dict], ahora_p: list[dict],
                      umbral: float) -> tuple[list[dict], dict[str, str], int]:
    """Survey vs survey: match exacto por cca_code; las parcelas sin cca de ambos
    lados caen al matching por dirección."""
    antes_cca = {}
    ahora_cca = {}
    resto_antes, resto_ahora = [], []
    for p in antes_p:
        if p["cca"]:
            agg = antes_cca.setdefault(p["cca"], _agg_nuevo())
            _acumular(agg, p["calle"], p["numero"], p["uso"],
                      p["uf_viv"], p["uf_com"], p["lat"], p["lng"], p["parcela_id"])
        else:
            resto_antes.append(p)
    for p in ahora_p:
        if p["cca"]:
            agg = ahora_cca.setdefault(p["cca"], _agg_nuevo())
            _acumular(agg, p["calle"], p["numero"], p["uso"],
                      p["uf_viv"], p["uf_com"], p["lat"], p["lng"], p["parcela_id"])
        else:
            resto_ahora.append(p)

    # Las claves cca son directamente comparables (sin fuzzy)
    filas, parcelas_estado, _ = _comparar(
        {f"cca:{k}|": v for k, v in antes_cca.items()},
        {f"cca:{k}|": v for k, v in ahora_cca.items()},
        umbral=2.0,   # >1 ⇒ nunca fuzzy entre ccas distintos
    )
    for f in filas:
        if f["match"] == "exacto":
            f["match"] = "cca"

    # Resto (sin cca): por dirección
    g_antes, _ = _por_clave(resto_antes)
    g_ahora, _ = _por_clave(resto_ahora)
    filas2, estado2, fuzzy = _comparar(g_antes, g_ahora, umbral)
    filas.extend(filas2)
    parcelas_estado.update(estado2)
    orden = {"nueva": 0, "cambio": 1, "desaparecida": 2, "igual": 3}
    filas.sort(key=lambda f: (orden.get(f["estado"], 9), f["direccion"]))
    return filas, parcelas_estado, fuzzy


# ── KPIs ───────────────────────────────────────────────────────────────────────

def _kpis(filas: list[dict], hab_por_dom: Optional[float]) -> dict:
    uf_viv_antes = sum(f["uf_viv_antes"] for f in filas)
    uf_viv_ahora = sum(f["uf_viv_ahora"] for f in filas)
    uf_com_antes = sum(f["uf_com_antes"] for f in filas)
    uf_com_ahora = sum(f["uf_com_ahora"] for f in filas)
    por_estado = {e: sum(1 for f in filas if f["estado"] == e)
                  for e in ("nueva", "cambio", "igual", "desaparecida")}
    kpis = {
        "uf_vivienda": {"antes": uf_viv_antes, "ahora": uf_viv_ahora,
                        "delta": uf_viv_ahora - uf_viv_antes},
        "uf_comercio": {"antes": uf_com_antes, "ahora": uf_com_ahora,
                        "delta": uf_com_ahora - uf_com_antes},
        "direcciones": {"antes": sum(1 for f in filas if f["estado"] != "nueva"),
                        "ahora": sum(1 for f in filas if f["estado"] != "desaparecida")},
        "por_estado": por_estado,
    }
    if hab_por_dom:
        delta_viv = kpis["uf_vivienda"]["delta"]
        kpis["habitantes"] = {
            "hab_por_domicilio": round(hab_por_dom, 2),
            "delta_estimado": round(delta_viv * hab_por_dom),
            "nota": "estimación secundaria: ΔUF vivienda × habitantes/domicilio (censo)",
        }
    return kpis


# ── Agente ─────────────────────────────────────────────────────────────────────

@agent_run
def run(input: ComparativaInput) -> ComparativaOutput:
    if bool(input.contra_survey_id) == bool(input.contra_baseline_id):
        return ComparativaOutput(
            ok=False, survey_id=input.survey_id,
            error="indicar exactamente uno: contra_survey_id o contra_baseline_id")

    engine = get_engine()
    with engine.connect() as conn:
        meta = conn.execute(text("""
            SELECT s.region_id, r.name FROM surveys s
            JOIN regions r ON r.region_id = s.region_id WHERE s.survey_id = :sid
        """), {"sid": input.survey_id}).fetchone()
        if not meta:
            return ComparativaOutput(ok=False, survey_id=input.survey_id,
                                     error=f"survey {input.survey_id} no existe")
        region_id = meta[0]

        ahora_p = _cargar_survey(conn, input.survey_id)
        if not ahora_p:
            return ComparativaOutput(ok=False, survey_id=input.survey_id,
                                     error="el survey actual no tiene parcelas")

        if input.contra_survey_id:
            contra = conn.execute(text("""
                SELECT s.region_id, r.name, COALESCE(s.finished_at, s.started_at)
                FROM surveys s JOIN regions r ON r.region_id = s.region_id
                WHERE s.survey_id = :sid
            """), {"sid": input.contra_survey_id}).fetchone()
            if not contra:
                return ComparativaOutput(ok=False, survey_id=input.survey_id,
                                         error=f"survey anterior {input.contra_survey_id} no existe")
            antes_p = _cargar_survey(conn, input.contra_survey_id)
            if not antes_p:
                return ComparativaOutput(ok=False, survey_id=input.survey_id,
                                         error="el survey anterior no tiene parcelas")
            contra_tipo, contra_id = "survey", input.contra_survey_id
            contra_nombre = contra[1]
            contra_fecha = contra[2].date().isoformat() if contra[2] else None
            filas, parcelas_estado, fuzzy = _comparar_por_cca(
                antes_p, ahora_p, input.fuzzy_umbral)
            metodo = "cca+direccion"
            _, sin_dir = _por_clave([p for p in ahora_p if not p["cca"]])
        else:
            base = conn.execute(text("""
                SELECT region_id, nombre, fecha_relevamiento
                FROM baselines WHERE baseline_id = :bid
            """), {"bid": input.contra_baseline_id}).fetchone()
            if not base:
                return ComparativaOutput(ok=False, survey_id=input.survey_id,
                                         error=f"baseline {input.contra_baseline_id} no existe")
            antes = _cargar_baseline(conn, input.contra_baseline_id)
            if not antes:
                return ComparativaOutput(ok=False, survey_id=input.survey_id,
                                         error="el baseline no tiene direcciones importadas")
            contra_tipo, contra_id = "baseline", input.contra_baseline_id
            contra_nombre = base[1]
            contra_fecha = base[2].isoformat() if base[2] else None
            ahora, sin_dir = _por_clave(ahora_p)
            filas, parcelas_estado, fuzzy = _comparar(antes, ahora, input.fuzzy_umbral)
            metodo = "direccion"

        # habitantes/domicilio del censo de la región (para Δhabitantes estimado)
        hab_por_dom = conn.execute(text("""
            SELECT SUM(pop_total)::float / NULLIF(SUM(domicilios_total), 0)
            FROM setores_censitarios WHERE region_id = :rid
        """), {"rid": region_id}).scalar()

    kpis = _kpis(filas, hab_por_dom)
    logger.info(
        f"Comparativa {input.survey_id} vs {contra_tipo} «{contra_nombre}»: "
        f"{kpis['por_estado']['nueva']} nuevas, {kpis['por_estado']['cambio']} cambios, "
        f"{kpis['por_estado']['desaparecida']} desaparecidas, "
        f"{kpis['por_estado']['igual']} sin cambio "
        f"(ΔUF viv {kpis['uf_vivienda']['delta']:+}, com {kpis['uf_comercio']['delta']:+})")

    return ComparativaOutput(
        ok=True, survey_id=input.survey_id,
        contra_tipo=contra_tipo, contra_id=contra_id,
        contra_nombre=contra_nombre, contra_fecha=contra_fecha,
        metodo_match=metodo, kpis=kpis, filas=filas,
        parcelas_estado=parcelas_estado,
        sin_direccion_ahora=sin_dir, matches_fuzzy=fuzzy,
    )
