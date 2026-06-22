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
    usar_mapbox: bool = True          # fuente principal para los no-exactos (confiable, por número)
    usar_osm_fallback: bool = True    # calles donde Mapbox apila/falla → geometría OSM (gratis)
    mapbox_max_requests: int = 800    # tope de llamadas Mapbox por corrida


class BaselineInterpOutput(BaseModel):
    ok: bool = False
    error: Optional[str] = None
    baseline_id: str = ""
    por_mapbox: int = 0              # ubicadas por número con Mapbox (confiable)
    por_osm: int = 0                # distribuidas sobre la línea OSM (Mapbox apiló/falló)
    sin_resolver: int = 0          # quedan agrupadas (ninguna fuente las ubica)
    mapbox_consultas: int = 0


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
    """Ubica las direcciones NO-exactas del relevamiento anterior con la fuente más confiable:
    **Mapbox por número** (interpola sobre la calle real). Las que Mapbox **apila** (devuelve
    el centro de la calle para varios números → calles menores) o ubica fuera de la zona, caen
    a **distribución sobre la línea de OSM**; lo que ninguna ubica queda agrupado (no se inventa).
    Las exactas de geocodebr (`g:numero`) se respetan."""
    import httpx
    from collections import defaultdict, Counter
    from scrapitero.agents.baseline_geocoder import (
        _mapbox, _mapbox_token, _query, _HEADERS, _centroide_zona, _dist_km)
    out = BaselineInterpOutput(baseline_id=input.baseline_id)
    engine = get_engine()
    with engine.connect() as conn:
        meta = conn.execute(text(
            "SELECT b.ciudad, r.zone_geojson FROM baselines b "
            "JOIN regions r ON r.region_id = b.region_id WHERE b.baseline_id = :b"),
            {"b": input.baseline_id}).first()
        rows = conn.execute(text("""
            SELECT id::text, calle_norm, numero, lat, lng, geocode_source, calle, barrio,
                   ciudad, estado
            FROM baseline_direcciones
            WHERE baseline_id = :b AND lat IS NOT NULL AND calle_norm IS NOT NULL
        """), {"b": input.baseline_id}).fetchall()

    ciudad_g = meta[0] if meta else None
    centro = _centroide_zona(meta[1]) if meta else None
    _GUARDA_KM = 120.0   # backstop anti "otro estado" (homónimo en otra ciudad)

    def _en_zona(la, ln):
        return centro is None or _dist_km(centro[0], centro[1], la, ln) <= _GUARDA_KM

    por_calle: dict = defaultdict(list)
    info: dict = {}   # rid → (num_int, calle_norm, calle_raw) para los no-exactos
    for r in rows:
        por_calle[r[1]].append(r)
        if (r[5] or "") != "g:numero":   # exactas de geocodebr se respetan
            info[r[0]] = (_num(r[2]), r[1], r[6])

    # 1) Mapbox por número (memo por número dentro de cada calle).
    mapbox_on = input.usar_mapbox and bool(_mapbox_token())
    mb: dict = {}   # rid → (lat, lng) ubicada por Mapbox dentro de la zona
    if mapbox_on:
        with httpx.Client(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
            for cn, items in por_calle.items():
                memo: dict = {}
                for rid, _cn, num, la, ln, src, calle, barrio, ciu, est in items:
                    if (src or "") == "g:numero":
                        continue
                    if out.mapbox_consultas >= input.mapbox_max_requests:
                        break
                    key = (num or "").strip()
                    if key in memo:
                        res = memo[key]
                    else:
                        q = _query(calle, num, barrio, ciu or ciudad_g, est)
                        res = _mapbox(q, "br", client) if q else None
                        out.mapbox_consultas += 1
                        memo[key] = res
                    if res and _en_zona(res[0], res[1]):
                        mb[rid] = (res[0], res[1])

    # 2) Descartar los Mapbox APILADOS (mismo punto para ≥3 números de la calle = centroide).
    mb_por_calle: dict = defaultdict(list)
    for rid, (la, ln) in mb.items():
        mb_por_calle[info[rid][1]].append((rid, la, ln))
    aceptados: dict = {}
    for cn, lst in mb_por_calle.items():
        cc = Counter((round(la, 6), round(ln, 6)) for _, la, ln in lst)
        for rid, la, ln in lst:
            if cc[(round(la, 6), round(ln, 6))] < 3:
                aceptados[rid] = (la, ln)

    if aceptados:
        with engine.begin() as conn:
            for rid, (la, ln) in aceptados.items():
                conn.execute(text("UPDATE baseline_direcciones SET lat=:la, lng=:ln, "
                                  "geocode_source='mapbox' WHERE id=:id"),
                             {"la": la, "ln": ln, "id": rid})
    out.por_mapbox = len(aceptados)

    # 3) Lo que Mapbox no ubicó (apiló/fuera de zona/sin token) → línea OSM.
    leftover = [(rid, info[rid][0], info[rid][1], info[rid][2])
                for rid in info if rid not in aceptados]
    out.sin_resolver = len(leftover)
    if leftover and input.usar_osm_fallback:
        osm_upd = _distribuir_osm(engine, input.baseline_id, leftover, rows)
        out.por_osm = len(osm_upd)
        out.sin_resolver = len(leftover) - out.por_osm
        if osm_upd:
            with engine.begin() as conn:
                for rid, la, ln in osm_upd:
                    conn.execute(text("UPDATE baseline_direcciones SET lat=:la, lng=:ln, "
                                      "geocode_source='osm_interp' WHERE id=:id"),
                                 {"la": la, "ln": ln, "id": rid})

    out.ok = True
    logger.info(f"baseline_interp {input.baseline_id}: mapbox={out.por_mapbox} "
                f"osm={out.por_osm} sin_resolver={out.sin_resolver} "
                f"(consultas mapbox={out.mapbox_consultas})")
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
