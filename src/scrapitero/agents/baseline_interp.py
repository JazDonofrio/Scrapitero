"""BaselineInterp — reposiciona por INTERPOLACIÓN las direcciones del relevamiento anterior
que quedaron apiladas en un punto.

Problema: geocodebr (CNEFE) devuelve un único **punto aproximado** (`g:numero_aproximado`)
para los números de casa que no están en el padrón → muchos números distintos de la MISMA
calle caen en la misma coordenada y el mapa los muestra apilados en un solo marcador.

Solución definitiva y **gratis**: por cada calle usamos los números geocodificados **exacto**
(`g:numero`/`mapbox`/`google`) como **anclas** y calculamos la posición de los aproximados
por interpolación según el número de casa (técnica estándar de geocoding por interpolación):
- dentro del rango de anclas → interpolación lineal a tramos (sigue la curva de la calle),
- fuera del rango → se extiende con la pendiente global (ajuste por mínimos cuadrados),
  para no explotar con un tramo local diminuto.

Para las calles que **no** tienen ≥2 anclas exactas, hay un fallback **acotado** a Mapbox
(interpola sobre la geometría real), con tope para minimizar el costo.

Idempotente: solo toca filas aproximadas/apiladas; las marca `geocode_source='interp'`
(o `mapbox` si cayeron al fallback).
"""

from __future__ import annotations

import re
from typing import Optional

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.db.engine import get_engine

# Fuentes que apuntan al NÚMERO de casa puntual → sirven de ancla.
_EXACTAS = ("g:numero", "mapbox", "google")
# Fuentes "gruesas" (a nivel calle / aproximado) → candidatas a reposicionar.
_APROX = ("g:numero_aproximado", "g:logradouro", "g:cep", "nominatim")


class BaselineInterpInput(BaseModel):
    baseline_id: str
    usar_osm_fallback: bool = True   # calles sin ≥2 anclas → distribuir sobre geometría OSM (gratis)


class BaselineInterpOutput(BaseModel):
    ok: bool = False
    error: Optional[str] = None
    baseline_id: str = ""
    interpoladas: int = 0
    por_osm: int = 0
    calles_sin_ancla: int = 0
    sin_resolver: int = 0


def _num(s) -> Optional[int]:
    m = re.search(r"\d+", str(s or ""))
    return int(m.group(0)) if m else None


# Marcadores de "basura" tras el nombre real (loteamento/quadra/bloco/complemento) — al
# encontrarlos se corta el nombre. El nombre se compara por su NÚCLEO (sin tipo de vía).
_JUNK_MARK = {"lot", "loteamento", "cond", "condominio", "conj", "conjunto", "qd", "quadra",
              "bloco", "blc", "bsd", "kitnet", "apto", "casa", "sala", "lj", "fd", "andar"}


def _core_calle(name: str) -> str:
    """Núcleo del nombre de calle para matchear contra OSM: normaliza, saca el tipo de vía
    inicial y corta en la 1ª 'basura' o número (loteamento/complemento/altura leaked)."""
    from scrapitero.agents.direccion_norm import normalizar_calle, TIPOS_VIA
    via = set(TIPOS_VIA.values())
    toks = normalizar_calle(name or "").split()
    if toks and toks[0] in via:
        toks = toks[1:]
    out = []
    for t in toks:
        if t in _JUNK_MARK or t.isdigit():
            break
        out.append(t)
    return " ".join(out)


def _hav_m(a, b, c, d) -> float:
    import math
    R = 6371000.0
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = math.radians(c - a), math.radians(d - b)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))


def _puntos_sobre_linea(poly: list, k: int) -> list:
    """Devuelve k puntos repartidos uniformemente a lo largo de la polilínea `poly`
    [(lat,lng),…] (por longitud acumulada, en los centros de k tramos iguales)."""
    if not poly:
        return []
    if len(poly) == 1 or k <= 0:
        return [poly[0]] * max(k, 0)
    acc = [0.0]
    for i in range(1, len(poly)):
        acc.append(acc[-1] + _hav_m(poly[i - 1][0], poly[i - 1][1], poly[i][0], poly[i][1]))
    total = acc[-1] or 1.0
    out = []
    for j in range(k):
        target = total * (j + 0.5) / k
        # ubicar el tramo que contiene `target`
        seg = 1
        while seg < len(acc) and acc[seg] < target:
            seg += 1
        seg = min(seg, len(poly) - 1)
        d0, d1 = acc[seg - 1], acc[seg]
        f = (target - d0) / (d1 - d0) if d1 > d0 else 0.0
        a, b = poly[seg - 1], poly[seg]
        out.append((a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])))
    return out


def _ajuste_lineal(anchors: list) -> tuple:
    """Pendiente global (Δlat/Δnum, Δlng/Δnum) por mínimos cuadrados sobre las anclas."""
    n = len(anchors)
    sx = sum(a[0] for a in anchors)
    mx = sx / n
    sxx = sum((a[0] - mx) ** 2 for a in anchors)
    if sxx == 0:
        return (0.0, 0.0)
    slat = sum((a[0] - mx) * a[1] for a in anchors) / sxx
    slng = sum((a[0] - mx) * a[2] for a in anchors) / sxx
    return (slat, slng)


def _interp_pos(N: int, anchors: list, slope: tuple) -> tuple:
    """anchors = lista [(num, lat, lng)] ordenada por num, única por num. Devuelve (lat,lng)."""
    if N <= anchors[0][0]:                     # antes del primer ancla → extiende con pendiente global
        a = anchors[0]
        return (a[1] + (N - a[0]) * slope[0], a[2] + (N - a[0]) * slope[1])
    if N >= anchors[-1][0]:                     # después del último → ídem
        a = anchors[-1]
        return (a[1] + (N - a[0]) * slope[0], a[2] + (N - a[0]) * slope[1])
    for i in range(len(anchors) - 1):          # dentro del rango → interpolación lineal a tramos
        lo, hi = anchors[i], anchors[i + 1]
        if lo[0] <= N <= hi[0]:
            f = (N - lo[0]) / (hi[0] - lo[0]) if hi[0] != lo[0] else 0.0
            return (lo[1] + f * (hi[1] - lo[1]), lo[2] + f * (hi[2] - lo[2]))
    return (anchors[0][1], anchors[0][2])      # no debería llegar


@agent_run
def run(input: BaselineInterpInput) -> BaselineInterpOutput:
    out = BaselineInterpOutput(baseline_id=input.baseline_id)
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id::text, calle_norm, numero, lat, lng, geocode_source, calle
            FROM baseline_direcciones
            WHERE baseline_id = :b AND lat IS NOT NULL AND calle_norm IS NOT NULL
        """), {"b": input.baseline_id}).fetchall()

    # Agrupar por calle. Detectar coordenadas compartidas (apiladas).
    from collections import defaultdict, Counter
    por_calle: dict = defaultdict(list)
    coord_count: dict = defaultdict(Counter)
    calle_raw: dict = {}
    for rid, cn, num, lat, lng, src, calle in rows:
        por_calle[cn].append((rid, num, lat, lng, src))
        coord_count[cn][(round(lat, 6), round(lng, 6))] += 1
        calle_raw.setdefault(cn, calle)

    updates: list = []           # (id, lat, lng)
    sin_ancla_targets: list = []  # (id, num, calle_norm, calle) para fallback OSM

    for cn, items in por_calle.items():
        anchors_raw = {}
        for rid, num, lat, lng, src in items:
            n = _num(num)
            if n is not None and (src or "").startswith(_EXACTAS):
                # ancla = exacta y NO apilada (coordenada única en la calle)
                if coord_count[cn][(round(lat, 6), round(lng, 6))] == 1:
                    anchors_raw.setdefault(n, (lat, lng))   # 1ª por número
        anchors = sorted((n, p[0], p[1]) for n, p in anchors_raw.items())

        # targets: aproximadas O apiladas (coordenada compartida)
        targets = []
        for rid, num, lat, lng, src in items:
            n = _num(num)
            apilada = coord_count[cn][(round(lat, 6), round(lng, 6))] > 1
            if n is not None and ((src or "").startswith(_APROX) or apilada) \
                    and not (src or "").startswith(_EXACTAS):
                targets.append((rid, n))
            elif n is not None and (src or "").startswith(_EXACTAS) and apilada:
                # exacta pero apilada con otras → también reposicionar
                targets.append((rid, n))

        if not targets:
            continue
        if len(anchors) < 2:
            out.calles_sin_ancla += 1
            sin_ancla_targets += [(rid, n, cn, calle_raw.get(cn)) for rid, n in targets]
            continue
        slope = _ajuste_lineal(anchors)
        for rid, n in targets:
            lat, lng = _interp_pos(n, anchors, slope)
            updates.append((rid, lat, lng))

    if updates:
        with engine.begin() as conn:
            for rid, lat, lng in updates:
                conn.execute(text("UPDATE baseline_direcciones SET lat=:lat, lng=:lng, "
                                  "geocode_source='interp' WHERE id=:id"),
                             {"lat": lat, "lng": lng, "id": rid})
    out.interpoladas = len(updates)

    # Fallback GRATIS para calles sin anclas: distribuir los números sobre la geometría
    # de la calle en OSM (una sola consulta Overpass para todo el bbox del baseline).
    out.sin_resolver = len(sin_ancla_targets)
    if sin_ancla_targets and input.usar_osm_fallback:
        osm_upd = _distribuir_osm(engine, input.baseline_id, sin_ancla_targets, rows)
        out.por_osm = len(osm_upd)
        out.sin_resolver = len(sin_ancla_targets) - out.por_osm
        if osm_upd:
            with engine.begin() as conn:
                for rid, lat, lng in osm_upd:
                    conn.execute(text("UPDATE baseline_direcciones SET lat=:lat, lng=:lng, "
                                      "geocode_source='osm_interp' WHERE id=:id"),
                                 {"lat": lat, "lng": lng, "id": rid})

    out.ok = True
    logger.info(f"baseline_interp {input.baseline_id}: interpoladas={out.interpoladas} "
                f"osm={out.por_osm} calles_sin_ancla={out.calles_sin_ancla} "
                f"sin_resolver={out.sin_resolver}")
    return out


def _distribuir_osm(engine, baseline_id: str, sin_ancla_targets: list, rows: list) -> list:
    """Para las calles sin anclas exactas, reparte los números sobre la geometría de la calle
    de OSM. Hace **una consulta Overpass POR CALLE** filtrando por un token distintivo del
    nombre (respuesta chica y confiable; el bbox grande con todas las calles daba timeout/vacío).
    Matchea localmente por núcleo de nombre (fuzzy ≥0.82). Devuelve [(id, lat, lng)]."""
    import difflib
    import re as _re
    from collections import defaultdict
    from scrapitero.agents.osm_building_fetcher import _fetch_overpass
    lats = [r[3] for r in rows]
    lngs = [r[4] for r in rows]
    if not lats:
        return []
    s, n = min(lats) - 0.003, max(lats) + 0.003
    w, e = min(lngs) - 0.003, max(lngs) + 0.003

    por_calle: dict = defaultdict(list)
    raw_de_calle: dict = {}
    for rid, num, cn, calle in sin_ancla_targets:
        por_calle[cn].append((num, rid))
        raw_de_calle.setdefault(cn, calle)

    updates = []
    consultas = 0
    for cn, items in por_calle.items():
        if consultas >= 80:   # tope de consultas OSM por baseline (acota el tiempo)
            break
        core = _core_calle(raw_de_calle.get(cn) or cn)
        if not core:
            continue
        consultas += 1
        # token distintivo (el más largo) para el filtro de nombre; los núcleos van sin
        # acentos, así que filtramos por un token y desambiguamos localmente por fuzzy.
        token = max(core.split(), key=len)
        if len(token) < 3:
            continue
        rx = _re.sub(r"[^a-z0-9]", ".", token)
        query = (f'[out:json][timeout:60];way[highway][name~"{rx}",i]'
                 f"({s},{w},{n},{e});out geom;")
        try:
            data = _fetch_overpass(query)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"baseline_interp: Overpass falló para «{core}»: {exc}")
            continue
        best_poly, best_r = None, 0.0
        for el in (data.get("elements", []) if data else []):
            if el.get("type") != "way" or not el.get("geometry"):
                continue
            c2 = _core_calle((el.get("tags") or {}).get("name", ""))
            if not c2:
                continue
            poly = [(g["lat"], g["lon"]) for g in el["geometry"] if "lat" in g and "lon" in g]
            if len(poly) < 2:
                continue
            r = difflib.SequenceMatcher(None, core, c2).ratio()
            if r >= 0.82 and r > best_r:
                best_poly, best_r = poly, r
        if not best_poly:
            continue
        items.sort(key=lambda t: t[0])
        pts = _puntos_sobre_linea(best_poly, len(items))
        for (num, rid), p in zip(items, pts):
            updates.append((rid, p[0], p[1]))
    return updates
