"""UnidadesEstimator — estima unidades de vivienda y comercio (uf_vivienda/uf_comercio) por parcela.

Objetivo: NO contar edificios, sino determinar la **cantidad de unidades funcionales**
(viviendas y locales comerciales) de cada parcela. El conteo exacto de UF no existe en
ninguna fuente gratuita (ver memoria project_salta_fuentes_uso_uf); este agente lo
**estima** combinando, por parcela:

  1. OSM tags first  — si el edificio tiene `building:flats`/`addr:units` (→ unidades_osm),
                       se usa ese número real. `house`/`detached` → 1 UF.
  2. Proxy geométrico — solo para edificios con altura real (building:levels ≥ 2 o
                       tipo apartments): UF = (área_footprint × pisos) / tamaño_típico,
                       con tamaño 80 m² vivienda / 50 m² comercio (editables). Un edificio
                       de 1 sola planta sin tag multi-unidad cuenta como 1 UF (no se
                       subdivide la huella → evita sobrestimar casas/locales grandes).
  3. Categoría vivienda vs comercio por `tipo_osm` (building=*), con fallback al
     `uso_principal` ya clasificado (CPUA / registro SIGSA).
  4. Fallback de cobertura — parcelas sin edificios OSM (hueco frecuente en el interior
     provincial) caen al mínimo por uso: residencial→1 vivienda, comercial→1 comercio,
     mixto→1 vivienda (default).

Reglas por uso:
  - residencial → siempre al menos 1 UF de vivienda (GREATEST(…, 1))
  - comercial   → siempre al menos 1 UF de comercio
  - mixto       → cada UF es vivienda O comercio (excluyente, nunca ambas a la vez):
                  cada edificio se asigna a una sola categoría según su tag; los no tipados
                  y el fallback sin edificios → vivienda por defecto. Mínimo 1 UF total.
  - vacante     → 0 UF
  - industrial / equipamiento → 0 UF de vivienda y de comercio (no son ni una ni otro)

Escribe en `parcelas`: uf_vivienda, uf_comercio, unidades_funcionales_estimadas,
footprints_count, pisos_estimados_max.

Requiere que OSMBuildingFetcher haya corrido **con vinculación a parcela** (parcela_id),
y que uso_principal ya esté clasificado (salta_zonificacion_fetcher / salta_registro_fetcher).

Genérico (cualquier país). NO ejecutar sobre regiones de Brasil donde BCIParser ya
extrajo UF exactas de los PDFs BCI — este agente las pisaría con una estimación.
"""

from __future__ import annotations

from typing import Optional

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.db.engine import get_engine
from scrapitero.agents._run import agent_run
from scrapitero.agents.precedencia import UF_FUENTES_PROTEGIDAS


# ── Mapeo tipo_osm (building=*) → categoría de unidad ──────────────────────────

_VIVIENDA_TIPOS = {
    "apartments", "residential", "house", "detached", "semidetached_house",
    "terrace", "dormitory", "bungalow", "cabin", "houseboat", "static_caravan",
    "farm", "hut", "ger", "villa",
}
_COMERCIO_TIPOS = {
    "commercial", "retail", "supermarket", "kiosk", "shop", "office",
    "hotel", "restaurant", "warehouse_retail",
}
# tipos que NO son ni vivienda ni comercio (no aportan UF de ninguno)
_NO_UF_TIPOS = {
    "industrial", "warehouse", "garage", "garages", "shed", "carport",
    "church", "cathedral", "chapel", "mosque", "temple", "school", "university",
    "college", "kindergarten", "hospital", "public", "civic", "government",
    "fire_station", "hangar", "stable", "barn", "service", "transformer_tower",
    "water_tower", "construction", "ruins", "roof",
}
# tipos OSM de vivienda unifamiliar → 1 UF (no se aplica proxy de área)
_UNIFAMILIAR_TIPOS = {"house", "detached", "semidetached_house", "bungalow",
                      "cabin", "villa", "static_caravan", "hut"}


# ── Pydantic I/O ──────────────────────────────────────────────────────────────

class UnidadesInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None
    overwrite: bool = False           # True → recalcula parcelas ya estimadas (footprints_count>0)
    m2_vivienda: float = 80.0         # tamaño típico de una vivienda (proxy geométrico)
    m2_comercio: float = 50.0         # tamaño típico de un local comercial
    pisos_default: int = 1            # pisos asumidos si OSM no trae building:levels


class UnidadesOutput(BaseModel):
    ok: bool
    parcelas_procesadas: int = 0
    parcelas_con_edificios: int = 0      # estimadas a partir de footprints OSM
    parcelas_fallback_uso: int = 0       # sin edificios OSM → mínimo por uso
    total_uf_vivienda: int = 0
    total_uf_comercio: int = 0
    fuente_unidades_osm: int = 0         # edificios con conteo real (building:flats/addr:units)
    error: Optional[str] = None


# ── Categorización ──────────────────────────────────────────────────────────────

def _categoria(tipo_osm: Optional[str], uso: str) -> Optional[str]:
    """Devuelve 'vivienda' | 'comercio' | None (no aporta UF) para un edificio."""
    if tipo_osm:
        t = tipo_osm.lower()
        if t in _NO_UF_TIPOS:
            return None
        if t in _COMERCIO_TIPOS:
            return "comercio"
        if t in _VIVIENDA_TIPOS:
            return "vivienda"
    # Sin tag útil → usar el uso_principal de la parcela
    if uso == "comercial":
        return "comercio"
    # residencial y mixto (porción no tipada) → vivienda
    return "vivienda"


def _unidades_edificio(ed: dict, uso: str, cat: str,
                       m2_viv: float, m2_com: float, pisos_default: int) -> int:
    """Estima cuántas UF aporta un edificio según OSM tags + proxy geométrico."""
    tipo = (ed.get("tipo_osm") or "").lower()
    # 1. Conteo real de OSM (building:flats / addr:units)
    if ed.get("unidades_osm"):
        return int(ed["unidades_osm"])
    # 2. Vivienda unifamiliar → 1 UF
    if tipo in _UNIFAMILIAR_TIPOS:
        return 1
    area = ed.get("area_m2") or 0.0
    pisos = ed.get("pisos_estimados") or pisos_default
    if area <= 0:
        return 1
    # 3. Sin evidencia de altura → 1 UF.
    #    Subdividir la huella de un edificio de 1 sola planta sobrestima:
    #    una casa o un local grande de planta baja es UNA unidad, no varias.
    #    Solo se asume multi-unidad cuando hay altura real (building:levels ≥ 2)
    #    o el tag lo declara explícitamente (apartments). building:levels es
    #    escaso en OSM Salta → la mayoría cae acá y cuenta 1 UF (conservador).
    es_multi = tipo == "apartments" or pisos >= 2
    if not es_multi:
        return 1
    # 4. Proxy geométrico (solo edificios en altura): área construida / tamaño típico
    construida = area * max(pisos, 1)
    size = m2_com if cat == "comercio" else m2_viv
    return max(1, round(construida / size))


# ── Narrativa de la estimación (para el cuadro de actividad) ────────────────────

def _situacion_edificio(ed: dict, cat: str, n: int,
                        m2_viv: float, m2_com: float, pisos_default: int) -> Optional[str]:
    """Explica en una línea cómo se estimó la UF de un edificio, para el log de
    actividad. Devuelve None en casos triviales (1 vivienda unifamiliar) que no
    aportan a la narrativa del número final."""
    tipo = (ed.get("tipo_osm") or "").lower()
    area = ed.get("area_m2") or 0.0
    pisos = ed.get("pisos_estimados") or pisos_default
    unidad = "comercio" if cat == "comercio" else "vivienda"
    if ed.get("unidades_osm"):
        return f"{n} {unidad}(s) por conteo real OSM (building:flats/addr:units)"
    if tipo in _UNIFAMILIAR_TIPOS:
        return None  # trivial: 1 vivienda unifamiliar
    if tipo == "apartments" or pisos >= 2:
        return f"{n} {unidad}(s) por proxy: footprint {area:.0f} m² × {pisos} pisos"
    # Edificio de 1 planta sin tag multi-unidad: no se subdivide (regla anti-sobrestimación)
    size = m2_com if cat == "comercio" else m2_viv
    if area > size:
        return (f"edificio de {area:.0f} m² de 1 planta → 1 {unidad} "
                f"(no subdividido: sin altura mapeada)")
    return None


def _label_parcela(p: dict) -> str:
    """Etiqueta legible de una parcela para el log (dirección o id corto)."""
    dirr = " ".join(x for x in (p.get("calle"), p.get("numero")) if x).strip()
    return dirr or f"parcela {p['pid'][:8]}"


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_parcelas(region_id: str, survey_id: Optional[str], overwrite: bool) -> list[dict]:
    engine = get_engine()
    base = """
        SELECT parcela_id::text AS pid, uso_principal AS uso,
               calle, numero
        FROM parcelas
        WHERE region_id = :rid
          AND uso_principal IS NOT NULL
          AND validado_manual = false
          -- UF cargadas a mano desde el panel de incidencias: se respetan SIEMPRE, también
          -- con overwrite=true. `overwrite` significa "recalculá las estimaciones", no "borrá
          -- lo que contó un humano" — y nadie relee `parcela_uf_manual` para reponerlas, así
          -- que pisarlas acá las perdía sin vuelta atrás.
          AND COALESCE(uf_fuente, '') <> 'manual'
    """
    params: dict = {"rid": region_id}
    if not overwrite:
        base += " AND COALESCE(footprints_count, 0) = 0"
        # No pisar un CONTEO REAL con una estimación. La lista vive en `precedencia.py`;
        # acá estaba copiada a mano y sólo nombraba a 'google', así que 'overture',
        # 'cadastur' y 'shopping_min' quedaban desprotegidas — exactamente el modo de falla
        # que ese módulo documenta. Medido en Malvinas (11-ago-2026): esta query se llevó
        # puestas 81 viviendas y los 88 comercios que Overture acababa de contar.
        base += f" AND COALESCE(uf_fuente, '') NOT IN {UF_FUENTES_PROTEGIDAS}"
    if survey_id:
        base += " AND survey_id = :sid"
        params["sid"] = survey_id
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text(base), params)]


def _get_edificios(region_id: str, survey_id: Optional[str]) -> dict[str, list[dict]]:
    """Devuelve {parcela_id: [edificio, …]} para los edificios vinculados de la región."""
    engine = get_engine()
    base = """
        SELECT e.parcela_id::text AS pid, e.area_m2, e.pisos_estimados,
               e.tipo_osm, e.unidades_osm
        FROM edificios e
        JOIN parcelas p ON p.parcela_id = e.parcela_id
        WHERE p.region_id = :rid AND e.parcela_id IS NOT NULL
    """
    params: dict = {"rid": region_id}
    if survey_id:
        base += " AND p.survey_id = :sid"
        params["sid"] = survey_id
    out: dict[str, list[dict]] = {}
    with engine.connect() as conn:
        for r in conn.execute(text(base), params):
            m = dict(r._mapping)
            out.setdefault(m["pid"], []).append(m)
    return out


def _persist(updates: list[dict]) -> None:
    if not updates:
        return
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE parcelas SET
                uf_vivienda = :uf_v,
                uf_comercio = :uf_c,
                unidades_funcionales_estimadas = :uf_total,
                footprints_count = :fcount,
                pisos_estimados_max = :pisos_max,
                uf_fuente = :uf_fuente
            WHERE parcela_id = :pid
        """), updates)


# ── Entry point ───────────────────────────────────────────────────────────────

@agent_run
def run(inp: UnidadesInput) -> UnidadesOutput:
    try:
        parcelas = _get_parcelas(inp.region_id, inp.survey_id, inp.overwrite)
    except Exception as e:
        logger.exception("UnidadesEstimator: error leyendo parcelas")
        return UnidadesOutput(ok=False, error=str(e))

    if not parcelas:
        msg = (
            f"Sin parcelas a estimar en '{inp.region_id}'. "
            "Verificá que uso_principal esté clasificado (salta_zonificacion_fetcher / "
            "salta_registro_fetcher) y, si no es la primera corrida, usá overwrite=true."
        )
        logger.warning(f"UnidadesEstimator: {msg}")
        return UnidadesOutput(ok=True, error=msg)

    edificios_por_parcela = _get_edificios(inp.region_id, inp.survey_id)
    logger.info(
        f"UnidadesEstimator '{inp.region_id}': {len(parcelas)} parcelas, "
        f"{sum(len(v) for v in edificios_por_parcela.values())} edificios vinculados"
    )

    updates: list[dict] = []
    con_edificios = 0
    fallback_uso = 0
    tot_v = tot_c = 0
    fuente_osm = 0
    # Contadores de situaciones particulares (para explicar el número final)
    n_conteo_real = 0      # parcelas con building:flats/addr:units
    n_proxy_multi = 0      # parcelas con un edificio en altura → varias UF por proxy
    n_cap_grande = 0       # parcelas con footprint grande de 1 planta → 1 UF (no subdividido)

    for p in parcelas:
        pid, uso = p["pid"], p["uso"]
        eds = edificios_por_parcela.get(pid, [])

        # Usos sin unidades de vivienda ni comercio (derivado de la clasificación de uso)
        if uso in ("vacante", "industrial", "equipamiento"):
            updates.append({"pid": pid, "uf_v": 0, "uf_c": 0, "uf_total": 0,
                            "fcount": len(eds), "uf_fuente": "uso",
                            "pisos_max": max((e.get("pisos_estimados") or 0) for e in eds) if eds else None})
            continue

        uf_v = uf_c = 0
        uso_conteo_real = False   # algún edificio aportó building:flats/addr:units
        situaciones: list[str] = []   # narrativa de los casos no triviales de esta parcela
        if eds:
            con_edificios += 1
            for e in eds:
                cat = _categoria(e.get("tipo_osm"), uso)
                if cat is None:           # edificio que no aporta UF (galpón, iglesia, etc.)
                    continue
                n = _unidades_edificio(e, uso, cat, inp.m2_vivienda, inp.m2_comercio, inp.pisos_default)
                if e.get("unidades_osm"):
                    fuente_osm += 1
                    uso_conteo_real = True
                if cat == "comercio":
                    uf_c += n
                else:
                    uf_v += n
                sit = _situacion_edificio(e, cat, n, inp.m2_vivienda, inp.m2_comercio, inp.pisos_default)
                if sit:
                    situaciones.append(sit)
            fuente = "osm" if uso_conteo_real else "proxy"
            # Registrar la situación particular de esta parcela en el cuadro de actividad
            if situaciones:
                if uso_conteo_real:
                    n_conteo_real += 1
                elif any("por proxy" in s for s in situaciones):
                    n_proxy_multi += 1
                if any("no subdividido" in s for s in situaciones):
                    n_cap_grande += 1
                logger.info(f"  · {_label_parcela(p)} ({uso}): " + "; ".join(situaciones))
        else:
            # Sin edificios OSM → mínimo por uso (cobertura del interior)
            fallback_uso += 1
            fuente = "uso"
            if uso == "residencial":
                uf_v = 1
            elif uso == "comercial":
                uf_c = 1
            elif uso == "mixto":
                # mixto = vivienda O comercio (excluyente): sin edificios se asume
                # 1 sola UF, por defecto vivienda. NO se suman ambas.
                uf_v = 1

        # Reglas mínimas por uso
        if uso == "residencial":
            uf_v = max(uf_v, 1)
        elif uso == "comercial":
            uf_c = max(uf_c, 1)
        elif uso == "mixto":
            # al menos 1 UF total; cada UF ya es vivienda O comercio según su edificio.
            # No se fuerza una vivienda si los edificios son todos comercio.
            if uf_v + uf_c == 0:
                uf_v = 1

        pisos_max = max((e.get("pisos_estimados") or 0) for e in eds) if eds else None
        if pisos_max == 0:
            pisos_max = None

        updates.append({
            "pid": pid, "uf_v": uf_v, "uf_c": uf_c, "uf_total": uf_v + uf_c,
            "fcount": len(eds), "pisos_max": pisos_max, "uf_fuente": fuente,
        })
        tot_v += uf_v
        tot_c += uf_c

    _persist(updates)

    logger.info(
        f"UnidadesEstimator completo '{inp.region_id}': {len(updates)} parcelas "
        f"({con_edificios} con edificios, {fallback_uso} fallback por uso). "
        f"Total UF vivienda={tot_v}, comercio={tot_c}."
    )
    # Desglose de las situaciones particulares que explican el número final
    logger.info(
        f"  Cómo se llegó al número: {n_conteo_real} parcela(s) con conteo real OSM, "
        f"{n_proxy_multi} con varias UF por proxy (edificio en altura), "
        f"{n_cap_grande} con footprint grande de 1 planta acotado a 1 UF, "
        f"{fallback_uso} sin edificios OSM (mínimo por uso)."
    )

    return UnidadesOutput(
        ok=True,
        parcelas_procesadas=len(updates),
        parcelas_con_edificios=con_edificios,
        parcelas_fallback_uso=fallback_uso,
        total_uf_vivienda=tot_v,
        total_uf_comercio=tot_c,
        fuente_unidades_osm=fuente_osm,
    )
