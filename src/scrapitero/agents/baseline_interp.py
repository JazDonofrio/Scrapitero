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
    usar_gemini: bool = True          # canonizar nombres de calle no matcheados (cacheado)
    mapbox_max_requests: int = 800    # tope de llamadas Mapbox por corrida
    # Guarda de consistencia por calle: una dirección lejos del núcleo de su calle es un error
    # SOLO si su número cae DENTRO del rango ya poblado (debería estar entre sus vecinas) o está
    # absurdamente lejos (>`hard_calle_m`). Los números FUERA del rango (arranque/fin de una
    # avenida larga) NO se tocan. Las marcadas se reponen en su posición POR NÚMERO (interp).
    soft_calle_m: float = 500.0    # umbral de "lejos del núcleo" (gatilla la evaluación)
    hard_calle_m: float = 4000.0   # distancia absurda → error aunque el número esté fuera de rango
    # Encadenado del clustering: direcciones de la misma calle a ≤ esto = mismo bloque (mantiene
    # la calle/avenida continua como UN cluster aunque el muestreo sea ralo).
    link_calle_m: float = 1000.0
    # Cross-check contra geocodebr/CNEFE: si Mapbox ubicó una calle ENTERA a más de esto del
    # punto que le da CNEFE (fuente catastral oficial), y el punto de CNEFE es más coherente con
    # el grueso del relevamiento (más cerca del centroide), Mapbox la puso en el lugar equivocado
    # → toda la calle se reubica al punto de CNEFE (correcto, aunque apile los números).
    xcheck_geocodebr_m: float = 700.0


class BaselineInterpOutput(BaseModel):
    ok: bool = False
    error: Optional[str] = None
    baseline_id: str = ""
    por_mapbox: int = 0              # ubicadas por número con Mapbox (confiable)
    por_osm: int = 0                # distribuidas sobre la línea OSM (Mapbox apiló/falló)
    por_ciudad: int = 0            # no ubicables → centro de la ciudad, marcadas (aproximadas)
    sin_resolver: int = 0          # sin coordenada (no había centro de zona)
    reposicionadas_calle: int = 0  # outliers lejos del núcleo de su calle, reposicionados
    mapbox_consultas: int = 0
    gemini_consultas: int = 0


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


def _clusters(items: list, link_m: float) -> list:
    """Single-linkage clustering: items=[(rid,(lat,lng))]. Devuelve [[rid,…],…] — componentes
    donde cada punto está a ≤`link_m` de otro del mismo cluster. Así una calle continua
    (puntos encadenados) es UN cluster aunque sea larga, y un homónimo lejano es otro."""
    n = len(items)
    parent = list(range(n))

    def find(x):
        r = x
        while parent[r] != r:
            r = parent[r]
        while parent[x] != r:
            parent[x], x = r, parent[x]
        return r

    for i in range(n):
        for j in range(i + 1, n):
            if _hav_m(items[i][1][0], items[i][1][1], items[j][1][0], items[j][1][1]) <= link_m:
                parent[find(i)] = find(j)
    from collections import defaultdict
    comp: dict = defaultdict(list)
    for i in range(n):
        comp[find(i)].append(items[i][0])
    return list(comp.values())


def _inconsistencias_por_calle(por_calle: dict, pos: dict, info: dict,
                               soft_m: float, hard_m: float, link_m: float) -> dict:
    """rid → (lat, lng) PREDICHA por número, para las direcciones de cada calle que están lejos
    del núcleo Y cuyo número cae dentro del rango ya poblado (deberían estar entre sus vecinas)
    o a distancia absurda (>hard_m). Reusa el modelo número→posición del núcleo (`_ajuste_lineal`
    + `_interp_pos`) → las repone en su posición POR NÚMERO. Los números FUERA del rango (arranque
    /fin de avenida) NO se marcan: son extensiones legítimas, no errores."""
    out: dict = {}
    for items in por_calle.values():
        ps = [(it[0], pos[it[0]]) for it in items if it[0] in pos]
        if len(ps) < 4:
            continue
        cls = _clusters(ps, link_m)
        cls.sort(key=len, reverse=True)
        main = set(cls[0])                          # núcleo = cluster más grande
        mainpos = [pos[r] for r in cls[0]]
        anc: dict = {}                              # número → (num, lat, lng) del núcleo
        for it in items:
            if it[0] in main:
                nn = _num(it[2])
                if nn is not None:
                    anc[nn] = (nn, pos[it[0]][0], pos[it[0]][1])
        anchors = sorted(anc.values())
        if len(anchors) < 2:
            continue
        nmin, nmax = anchors[0][0], anchors[-1][0]
        slope = _ajuste_lineal(anchors)
        for it in items:
            rid = it[0]
            if rid in main or rid not in pos or rid not in info:
                continue
            la, ln = pos[rid]
            nd = min(_hav_m(la, ln, p[0], p[1]) for p in mainpos)
            if nd <= soft_m:
                continue
            nn = _num(it[2])
            if (nn is not None and nmin <= nn <= nmax) or nd > hard_m:
                out[rid] = _interp_pos(nn, anchors, slope) if nn is not None \
                    else (anchors[0][1], anchors[0][2])
    return out


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
    """Ubica las direcciones NO-exactas del relevamiento anterior, en orden de confiabilidad:
    1) **PRIMARIO — eje de calle de OSM**: interpola el número sobre la geometría real de la
       calle (cacheada por ciudad/calle), anclando en las exactas `g:numero` y desambiguando el
       homónimo correcto por consenso. Secuencial por construcción → resuelve de raíz el apilado
       y el desorden del geocoder por-dirección;
    2) **Mapbox por número** solo para las calles que OSM no tiene/no ubica;
    3) **Gemini** canoniza el nombre de calle no matcheado y se reintenta OSM/Mapbox;
    4) lo que ninguna ubica → punto aproximado de **geocodebr/CNEFE** (apilado, ubicación oficial)
       o, si no, **centro de la ciudad, marcado** `ciudad`. Las exactas `g:numero` se respetan."""
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
                   ciudad, estado, cep
            FROM baseline_direcciones
            WHERE baseline_id = :b AND lat IS NOT NULL AND calle_norm IS NOT NULL
        """), {"b": input.baseline_id}).fetchall()

    ciudad_g = meta[0] if meta else None
    centro = _centroide_zona(meta[1]) if meta else None
    _GUARDA_KM = 120.0   # backstop anti "otro estado" (homónimo en otra ciudad)

    def _en_zona(la, ln):
        return centro is None or _dist_km(centro[0], centro[1], la, ln) <= _GUARDA_KM

    por_calle: dict = defaultdict(list)
    item_by_rid: dict = {}
    info: dict = {}        # rid → (num_int, calle_norm) para los no-exactos
    calle_raw: dict = {}   # calle_norm → calle cruda (para Gemini/OSM)
    for r in rows:
        por_calle[r[1]].append(r)
        item_by_rid[r[0]] = r
        if (r[5] or "") != "g:numero":   # exactas de geocodebr se respetan
            info[r[0]] = (_num(r[2]), r[1])
            calle_raw.setdefault(r[1], r[6])

    mapbox_on = input.usar_mapbox and bool(_mapbox_token())
    client = httpx.Client(timeout=20, headers=_HEADERS, follow_redirects=True)
    aceptados: dict = {}   # rid → (lat, lng, fuente)

    def _mapbox_calle(items: list, calle_override: Optional[str] = None) -> dict:
        """Ubica por número con Mapbox los targets de UNA calle. Descarta apilados (≥3 al
        mismo punto = centroide) y fuera de zona. Devuelve rid→(lat,lng)."""
        memo: dict = {}
        mb: dict = {}
        for rid, _cn, num, la, ln, src, calle, barrio, ciu, est, cep in items:
            if out.mapbox_consultas >= input.mapbox_max_requests:
                break
            name = calle_override or calle
            key = (name, (num or "").strip())
            if key in memo:
                res = memo[key]
            else:
                q = _query(name, num, barrio, ciu or ciudad_g, est, cep)
                res = _mapbox(q, "br", client) if q else None
                out.mapbox_consultas += 1
                memo[key] = res
            if res and _en_zona(res[0], res[1]):
                mb[rid] = (res[0], res[1])
        cc = Counter((round(a, 6), round(b, 6)) for a, b in mb.values())
        return {rid: (a, b) for rid, (a, b) in mb.items()
                if cc[(round(a, 6), round(b, 6))] < 3}

    # Anclas exactas (g:numero) por calle y punto geocodebr/CNEFE por calle → desambiguan el
    # homónimo correcto de OSM y orientan el eje. Una sola llamada batch a geocodebr.
    anclas_cn: dict = defaultdict(list)
    for r in rows:
        if (r[5] or "") == "g:numero" and _num(r[2]) is not None:
            anclas_cn[r[1]].append((_num(r[2]), float(r[3]), float(r[4])))
    gb_cn: dict = {}
    try:
        from scrapitero.agents.geocode_forward import geocodebr_lote
        reps = []
        for cn, items in por_calle.items():
            tg = [it for it in items if (it[5] or "") != "g:numero" and _num(it[2]) is not None]
            if not tg:
                continue
            md = sorted(tg, key=lambda r: _num(r[2]))[len(tg) // 2]
            reps.append({"id": cn, "logradouro": md[6] or "", "numero": md[2] or "",
                         "municipio": (md[8] or ciudad_g or ""), "estado": md[9] or "",
                         "bairro": md[7] or "", "cep": md[10] or ""})
        for cn, t in (geocodebr_lote(reps, max_desvio_m=1500.0) if reps else {}).items():
            gb_cn[cn] = (t[0], t[1])
    except Exception as e:  # noqa: BLE001
        logger.warning(f"baseline_interp: geocodebr (anclas de consenso) falló: {str(e)[:120]}")

    # bbox para OSM con margen amplio (una calle puede estar mal ubicada por Mapbox).
    _lats = [float(r[3]) for r in rows]; _lngs = [float(r[4]) for r in rows]
    bbox = (min(_lats) - 0.01, min(_lngs) - 0.01, max(_lats) + 0.01, max(_lngs) + 0.01)

    def _osm_calle(lineas, cn, targets_rid_num):
        """Coloca por eje OSM los targets [(rid,num)] de UNA calle. {} si OSM no la tiene/ubica."""
        if not lineas:
            return {}
        pistas = []
        for rid, _n in targets_rid_num:
            it = item_by_rid[rid]
            if it[3] is not None:
                pistas.append((_num(it[2]), float(it[3]), float(it[4])))
        if cn in gb_cn:
            pistas.append((None, gb_cn[cn][0], gb_cn[cn][1]))
        return _colocar_en_eje(lineas, targets_rid_num, anclas_cn.get(cn, []), pistas)

    try:
        # 1) PRIMARIO: eje de calle de OSM. Una calle es una línea y las alturas crecen monótonas
        # sobre ella → interpolar el número da posiciones SECUENCIALES por construcción, respetando
        # las anclas exactas. Es mucho más confiable que el geocoder por-dirección (que apila/
        # desordena) y subsume el viejo cross-check (una calle mal ubicada por Mapbox se reubica
        # sola al eje real). UNA consulta Overpass para toda la zona, cacheada por ciudad/calle.
        if input.usar_osm_fallback:
            con_target = [(cn, calle_raw.get(cn)) for cn, items in por_calle.items()
                          if any((it[5] or "") != "g:numero" for it in items)]
            geoms = _osm_geometrias(engine, con_target, ciudad_g, bbox)
            for cn, items in por_calle.items():
                tg = [(it[0], _num(it[2])) for it in items if (it[5] or "") != "g:numero"]
                if not tg:
                    continue
                for rid, (la, ln) in _osm_calle(geoms.get(cn, []), cn, tg).items():
                    aceptados[rid] = (la, ln, "osm_interp")

        # 2) Respaldo Mapbox por número SOLO para las calles que OSM no tiene/no ubicó.
        if mapbox_on:
            for cn, items in por_calle.items():
                tg = [it for it in items
                      if (it[5] or "") != "g:numero" and it[0] not in aceptados]
                if tg:
                    for rid, p in _mapbox_calle(tg).items():
                        aceptados[rid] = (p[0], p[1], "mapbox")

        # 3) Gemini: canonizar el nombre de las calles aún sin ubicar (ej. "R ORIEL B CAMPOS" →
        # "Rua Oriel Bezerra de Campos") y reintentar — eje OSM primero (con el nombre canónico,
        # vía _distribuir_osm que no usa la caché negativa de cn), Mapbox después.
        rem_calles: dict = defaultdict(list)
        for rid in info:
            if rid not in aceptados:
                rem_calles[info[rid][1]].append(item_by_rid[rid])
        if rem_calles and input.usar_gemini:
            for cn, items in rem_calles.items():
                canon = _gemini_canonico(engine, cn, calle_raw.get(cn), ciudad_g, out)
                if not canon:
                    continue
                pend = [(it[0], _num(it[2]), cn, canon) for it in items]
                if input.usar_osm_fallback:
                    for rid, la, ln in _distribuir_osm(engine, input.baseline_id, pend, rows):
                        aceptados[rid] = (la, ln, "osm_interp")
                if mapbox_on:
                    falta = [it for it in items if it[0] not in aceptados]
                    for rid, p in _mapbox_calle(falta, calle_override=canon).items():
                        aceptados[rid] = (p[0], p[1], "mapbox")

        # 3.5) Consistencia por calle: una dirección lejos del núcleo de SU calle cuyo NÚMERO
        # cae dentro del rango ya poblado (debería estar entre sus vecinas) — o a distancia
        # absurda — es un geocode equivocado (homónimo / desplazado). Se la repone en su
        # posición POR NÚMERO (modelo número→posición del núcleo). Los números FUERA del rango
        # (arranque/fin de avenida) NO se tocan: son extensiones legítimas.
        def _pos(rid):
            if rid in aceptados:
                return (aceptados[rid][0], aceptados[rid][1])
            it = item_by_rid[rid]
            return (float(it[3]), float(it[4])) if it[3] is not None else None
        pos = {rid: p for rid in item_by_rid if (p := _pos(rid))}
        # Reagrupar por `normalizar_calle` RECALCULADO del nombre crudo (la columna calle_norm
        # guardada puede ser stale: se computó al importar, antes de mejoras como cortar el LOT).
        from scrapitero.agents.direccion_norm import normalizar_calle as _ncalle
        por_calle_g: dict = defaultdict(list)
        for it in item_by_rid.values():
            por_calle_g[_ncalle(it[6])].append(it)
        repos = _inconsistencias_por_calle(por_calle_g, pos, info, input.soft_calle_m,
                                           input.hard_calle_m, input.link_calle_m)
        for rid, (la, ln) in repos.items():
            aceptados[rid] = (la, ln, "interp")
        out.reposicionadas_calle = len(repos)
        if repos:
            logger.info(f"baseline_interp: {len(repos)} direcciones desplazadas (número dentro "
                        f"del rango de su calle) → repuestas en su posición por número")

        # 4) Sin ubicar: punto aproximado de geocodebr/CNEFE (apilado, pero en la ubicación
        # oficial de la calle) si lo hay; si no, centro de la ciudad MARCADO (aproximado, sin
        # inventar la cuadra). No pisar un placement previo bueno (osm_interp/interp) si este run
        # no lo re-ubicó (p.ej. Overpass flakeó).
        for rid in info:
            if rid in aceptados:
                continue
            if (item_by_rid[rid][5] or "") in ("osm_interp", "interp"):
                continue   # conservar placement previo bueno
            cn = info[rid][1]
            if cn in gb_cn:
                aceptados[rid] = (gb_cn[cn][0], gb_cn[cn][1], "g:numero_aproximado")
            elif centro:
                aceptados[rid] = (centro[0], centro[1], "ciudad")
    finally:
        client.close()

    with engine.begin() as conn:
        for rid, (la, ln, fuente) in aceptados.items():
            conn.execute(text("UPDATE baseline_direcciones SET lat=:la, lng=:ln, "
                              "geocode_source=:f WHERE id=:id"),
                         {"la": la, "ln": ln, "f": fuente, "id": rid})

    out.por_mapbox = sum(1 for v in aceptados.values() if v[2] == "mapbox")
    out.por_osm = sum(1 for v in aceptados.values() if v[2] == "osm_interp")
    out.por_ciudad = sum(1 for v in aceptados.values() if v[2] == "ciudad")
    out.sin_resolver = len(info) - len(aceptados)
    out.ok = True
    logger.info(f"baseline_interp {input.baseline_id}: mapbox={out.por_mapbox} "
                f"osm={out.por_osm} ciudad={out.por_ciudad} sin_resolver={out.sin_resolver} "
                f"reposicionadas_calle={out.reposicionadas_calle} "
                f"(mapbox={out.mapbox_consultas} gemini={out.gemini_consultas})")
    return out


def _extraer_nombre_calle(texto: str) -> Optional[str]:
    """Extrae el nombre de calle de la respuesta (a veces verbosa) de Gemini. Busca la frase
    con tipo de vía COMPLETO (Rua/Avenida/Travessa…) — evita el input abreviado (AV/R/TV) que
    el modelo repite — y toma la más larga. None si no hay o dice DESCONOCIDO."""
    if not texto or "DESCONOCIDO" in texto.upper():
        return None
    # tipo de vía (cualquier caso) seguido de un nombre propio (mayúscula) → evita capturar
    # "avenida es una de las principales…" y el input abreviado (AV/R/TV). Tomar el PRIMERO.
    m = re.search(
        r"(?:[Rr]ua|[Aa]venida|[Aa]v\.|[Tt]ravessa|[Aa]lameda|[Pp]ra[çc]a|[Rr]odovia|"
        r"[Ee]strada|[Ll]argo|[Mm]arginal|[Bb]eco|[Vv]iela)\s+[A-ZÀ-Ý][^.,;\n]{0,55}", texto)
    if not m:
        return None
    return m.group(0).strip(" .,;")[:200] or None


def _gemini_canonico(engine, calle_norm: str, calle_raw: Optional[str],
                     ciudad: Optional[str], out) -> Optional[str]:
    """Nombre COMPLETO de la calle según Gemini (con búsqueda), cacheado por (calle_norm,
    ciudad) en `calle_canonica`. Devuelve el nombre o None (no resuelto/sin key/rate-limit)."""
    ciu = (ciudad or "").strip()[:120]
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT nombre_canonico FROM calle_canonica WHERE calle_norm=:c AND ciudad=:u"),
            {"c": calle_norm, "u": ciu}).first()
    if row is not None:                      # cacheado (incluye None = ya se sabe que no resuelve)
        return row[0]
    try:
        from scrapitero.agents.hotel_habitaciones_llm import _ask_gemini, _api_key
        key = _api_key()
        if not key:
            return None
        prompt = (f"¿Cuál es el nombre COMPLETO y correcto de la calle abreviada "
                  f"'{calle_raw or calle_norm}' en {ciudad or 'Brasil'}, Brasil? "
                  f"Abreviaturas: R=Rua, AV=Avenida, TV=Travessa; una inicial suelta es un "
                  f"nombre. Respondé SOLO el nombre completo de la calle (con su tipo de vía), "
                  f"o 'DESCONOCIDO' si no estás seguro.")
        out.gemini_consultas += 1
        raw = _ask_gemini(key, "gemini-2.5-flash", prompt, 60) or ""
        nombre = _extraer_nombre_calle(raw)
    except Exception as e:  # noqa: BLE001 — 429/red: NO cachear, reintentar en otra corrida
        logger.warning(f"baseline_interp: Gemini falló para «{calle_norm}»: {str(e)[:120]}")
        return None
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO calle_canonica (calle_norm, ciudad, nombre_canonico) "
            "VALUES (:c, :u, :n) ON CONFLICT (calle_norm, ciudad) DO UPDATE "
            "SET nombre_canonico = EXCLUDED.nombre_canonico"),
            {"c": calle_norm, "u": ciu, "n": nombre})
    return nombre


def _distribuir_osm(engine, baseline_id: str, sin_ancla_targets: list, rows: list) -> list:
    """Para las calles sin anclas exactas, reparte los números sobre la geometría de la calle
    de OSM. Hace **una consulta Overpass POR CALLE** filtrando por un token distintivo del
    nombre (respuesta chica y confiable; el bbox grande con todas las calles daba timeout/vacío).
    Matchea localmente por núcleo de nombre (fuzzy ≥0.82). Devuelve [(id, lat, lng)]."""
    import difflib
    import re as _re
    import time as _time
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
        # Overpass es intermitente: a veces devuelve {} sin error (mirror flakeó). Reintentar
        # SOLO ante respuesta vacía (no cuando trae elementos pero ninguno matchea: ahí la calle
        # realmente no está y reintentar es inútil). Hasta 3 intentos con pausa corta.
        data = None
        for _intento in range(3):
            try:
                data = _fetch_overpass(query)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"baseline_interp: Overpass falló para «{core}»: {exc}")
                data = None
            if data and data.get("elements"):
                break
            _time.sleep(2)
        if not (data and data.get("elements")):
            continue
        best_poly, best_r = None, 0.0
        for el in data.get("elements", []):
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


# ─────────────────────────────────────────────────────────────────────────────
# Colocación PRIMARIA por eje de calle (OSM): una calle es una línea y las alturas crecen de
# forma monótona sobre ella. Interpolar el número sobre el eje real da posiciones secuenciales
# por construcción — mucho más confiable que el geocoder por-dirección, que apila/desordena.
# ─────────────────────────────────────────────────────────────────────────────

def _merge_lines(geom):
    """(Multi)LineString → lista de LineStrings continuas (linemerge)."""
    from shapely.ops import linemerge
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    merged = linemerge(geom)
    if merged.geom_type == "LineString":
        return [merged]
    return [g for g in merged.geoms if g.geom_type == "LineString" and not g.is_empty]


def _stitch(lines, max_gap=0.0045, overlap=0.0003):
    """Cose varios tramos de la MISMA calle en UN eje continuo (cadena greedy por extremo más
    cercano, volteando los tramos según haga falta). OSM suele partir una calle en varias ways con
    huecos (cruces, topología) que `linemerge` no une; sin coserlos, interpolar sobre un solo tramo
    desordena los números. No une tramos separados por más de `max_gap` (~500 m → es otro pedazo /
    homónimo). Devuelve un LineString.

    Antes de coser DEDUPLICA carriles paralelos (avenidas de doble mano: OSM trae un way por
    sentido, ~ida y vuelta): un tramo cuyo punto medio cae sobre otro ya aceptado se descarta —
    si no, la cadena vuelve sobre sí misma (lazo) y las alturas bajas y altas caen en el mismo
    extremo (caso AV Arthur Bernardes)."""
    from shapely.geometry import LineString, Point
    raw = sorted((list(l.coords) for l in lines if len(l.coords) >= 2),
                 key=lambda s: -LineString(s).length)
    segs: list = []
    for seg in raw:                                       # quedarse con un carril por tramo
        mid = Point(seg[len(seg) // 2])
        if any(LineString(k).distance(mid) < overlap for k in segs):
            continue
        segs.append(seg)
    if not segs:
        return None
    if len(segs) == 1:
        return LineString(segs[0])
    start = max(range(len(segs)), key=lambda i: LineString(segs[i]).length)
    chain = segs.pop(start)
    while segs:
        head, tail = chain[0], chain[-1]
        best = None  # (dist, idx, donde, flip)
        for i, seg in enumerate(segs):
            for pt, flip in ((seg[0], False), (seg[-1], True)):
                dt = (tail[0] - pt[0]) ** 2 + (tail[1] - pt[1]) ** 2
                dh = (head[0] - pt[0]) ** 2 + (head[1] - pt[1]) ** 2
                if best is None or dt < best[0]:
                    best = (dt, i, "tail", flip)
                if dh < best[0]:
                    best = (dh, i, "head", flip)
        d, i, donde, flip = best
        if d ** 0.5 > max_gap:
            break                                        # el tramo más cercano está lejos → no es la misma calle
        seg = segs.pop(i)
        # al pegar por 'tail' queremos que el extremo MÁS CERCANO al tail quede primero
        if donde == "tail":
            d0 = (tail[0] - seg[0][0]) ** 2 + (tail[1] - seg[0][1]) ** 2
            d1 = (tail[0] - seg[-1][0]) ** 2 + (tail[1] - seg[-1][1]) ** 2
            chain = chain + (seg if d0 <= d1 else seg[::-1])
        else:
            d0 = (head[0] - seg[0][0]) ** 2 + (head[1] - seg[0][1]) ** 2
            d1 = (head[0] - seg[-1][0]) ** 2 + (head[1] - seg[-1][1]) ** 2
            chain = (seg[::-1] if d0 <= d1 else seg) + chain
    return LineString(chain)


def _osm_geometrias(engine, calles, ciudad, bbox):
    """Geometría OSM de MUCHAS calles a la vez → {calle_norm: [LineStrings]}.

    `calles`=[(calle_norm, calle_raw)]. Lee primero la caché `calle_geometria` (por ciudad/calle,
    incl. resultado NEGATIVO); para las que faltan hace **UNA sola** consulta Overpass de todas las
    vías con nombre del bbox (sin regex → barata) y matchea localmente por núcleo de nombre
    (fuzzy ≥0.82). Cachea cada calle (incl. las no encontradas). Así el primer relevamiento de una
    ciudad paga una consulta y los re-runs / otras zonas de esa ciudad la reusan.

    La **clave de caché es el `calle_norm` RECOMPUTADO del nombre** (`normalizar_calle(raw)`), no el
    `calle_norm` que pasa el llamador (que puede venir de la columna stale del baseline o del nombre
    lindo del scope) → así el geocoding y el corredor de scope-calles comparten el caché aunque
    partan de strings distintos del mismo nombre. El dict de salida se devuelve bajo el `calle_norm`
    que pasó el llamador."""
    import json
    import difflib
    import time as _time
    from collections import defaultdict
    from shapely.geometry import LineString, shape, mapping
    from shapely.ops import unary_union
    from scrapitero.agents.osm_building_fetcher import _fetch_overpass
    from scrapitero.agents.direccion_norm import normalizar_calle
    ciu = (ciudad or "").strip()[:120]
    out: dict = {}
    faltan: list = []   # (cn_llamador, raw, nkey)
    with engine.connect() as conn:
        for cn, raw in calles:
            nkey = normalizar_calle(raw) or cn      # clave canónica del caché
            row = conn.execute(text("SELECT geojson FROM calle_geometria WHERE ciudad=:c AND "
                                    "calle_norm=:n"), {"c": ciu, "n": nkey}).first()
            if row is not None:
                out[cn] = _merge_lines(shape(json.loads(row[0]))) if row[0] else []
            else:
                faltan.append((cn, raw, nkey))
    if not faltan:
        return out

    # UNA consulta: todas las vías CON nombre del bbox (sin regex de nombre → Overpass la resuelve
    # rápido; el matcheo por calle se hace local).
    s, w, n, e = bbox
    query = f'[out:json][timeout:90];way[highway][name]({s},{w},{n},{e});out geom;'
    data = None
    for _ in range(5):                                   # Overpass flakea: reintentar (timeout corto
        try:                                             # → fail-fast en mirrors colgados)
            data = _fetch_overpass(query, timeout=55)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"baseline_interp: Overpass (red de calles) falló: {exc}")
            data = None
        if data and data.get("elements"):
            break
        _time.sleep(3)
    if data is None:                                     # sin respuesta → no cachear, devolver lo que haya
        for cn, _raw, _nk in faltan:
            out[cn] = []
        return out

    # agrupar las ways devueltas por núcleo de nombre
    ways_por_core: dict = defaultdict(list)
    for el in data.get("elements", []):
        if el.get("type") != "way" or not el.get("geometry"):
            continue
        c2 = _core_calle((el.get("tags") or {}).get("name", ""))
        if not c2:
            continue
        coords = [(g["lon"], g["lat"]) for g in el["geometry"] if "lon" in g and "lat" in g]
        if len(coords) >= 2:
            ways_por_core[c2].append(LineString(coords))

    cores_osm = list(ways_por_core.keys())
    with engine.begin() as conn:
        for cn, raw, nkey in faltan:
            core = _core_calle(raw or cn)
            lines: list = []
            if core:
                for c2 in cores_osm:
                    if difflib.SequenceMatcher(None, core, c2).ratio() >= 0.82:
                        lines.extend(ways_por_core[c2])
            gj = json.dumps(mapping(unary_union(lines))) if lines else None
            conn.execute(text(
                "INSERT INTO calle_geometria (ciudad, calle_norm, geojson) VALUES (:c,:n,:g) "
                "ON CONFLICT (ciudad, calle_norm) DO UPDATE SET geojson=EXCLUDED.geojson, "
                "fetched_at=now()"), {"c": ciu, "n": nkey, "g": gj})
            out[cn] = _merge_lines(unary_union(lines)) if lines else []
    return out


def _colocar_en_eje(lineas, targets, anclas, pistas):
    """Coloca `targets`=[(rid,num)] sobre el eje OSM de su calle por interpolación de número.

    - `anclas`=[(num,lat,lng)] exactas (g:numero) → modelo número→arclength piecewise (exacto
      donde lo hay, interpolado/extrapolado el resto).
    - `pistas`=[(num,lat,lng)] posiciones ruidosas de los targets (mapbox) + punto geocodebr →
      desambiguan el homónimo (qué way) y orientan el eje cuando faltan anclas exactas.
    Devuelve {rid:(lat,lng)} (fuente osm_interp) o {} si no se puede ubicar/orientar con
    confianza (→ el llamador cae al camino por-dirección)."""
    from shapely.geometry import Point
    if not lineas or not targets:
        return {}
    UMB = 0.0020          # ~200 m: "cerca del eje" para soporte/orientación
    UMB_ANCLA = 0.0014    # ~150 m: un ancla más lejos del eje está mal geocodificada → se descarta

    def proj(ln, la, lo):
        return ln.project(Point(lo, la))

    # 1) elegir la línea (homónimos) por mayor soporte: anclas exactas pesan 3, pistas 1.
    sop_pts = [(la, lo, 3.0) for _, la, lo in anclas] + [(la, lo, 1.0) for _, la, lo in pistas]

    def soporte(ln):
        return sum(p[2] for p in sop_pts if ln.distance(Point(p[1], p[0])) <= UMB)
    # Quedarse con los tramos CERCA de las anclas/pistas (descarta homónimos lejanos) y COSERLOS
    # en un eje continuo (una calle suele venir partida en varias ways con huecos).
    if sop_pts:
        soportadas = [ln for ln in lineas if soporte(ln) > 0]
        if not soportadas:
            return {}     # ninguna línea cerca de las anclas/pistas → es otro homónimo
    else:
        soportadas = list(lineas)
    linea = _stitch(soportadas)
    if linea is None or linea.length <= 0:
        return {}
    L = linea.length

    # 2) modelo número→arclength
    ax = {}
    for num, la, lo in anclas:
        if num is None:
            continue
        p = Point(lo, la)
        if linea.distance(p) <= UMB_ANCLA:
            ax.setdefault(num, []).append(proj(linea, la, lo))
    ax = sorted((n, sum(v) / len(v)) for n, v in ax.items())

    num2arc = None
    if len(ax) >= 2 and ax[0][0] != ax[-1][0]:
        def num2arc(N):                                  # piecewise por anclas exactas
            if N <= ax[0][0]:
                (n0, a0), (n1, a1) = ax[0], ax[1]
            elif N >= ax[-1][0]:
                (n0, a0), (n1, a1) = ax[-2], ax[-1]
            else:
                for i in range(len(ax) - 1):
                    if ax[i][0] <= N <= ax[i + 1][0]:
                        (n0, a0), (n1, a1) = ax[i], ax[i + 1]
                        break
            return a0 + (a1 - a0) * (N - n0) / (n1 - n0) if n1 != n0 else a0
    else:
        # sin ≥2 anclas exactas: proporción por número sobre el largo, orientada por las pistas.
        nums = [n for _, n in targets if n is not None]
        if not nums:
            return {}
        nmin, nmax = min(nums), max(nums)
        if nmin == nmax:
            mid = linea.interpolate(0.5 * L)
            return {rid: (mid.y, mid.x) for rid, _ in targets}
        # orientación: regresión número→arclength sobre las pistas proyectadas (aunque ruidosas,
        # el SIGNO suele ser correcto). Si no hay señal, asumir inicio del eje = altura menor.
        pp = [(num, proj(linea, la, lo)) for num, la, lo in pistas
              if num is not None and linea.distance(Point(lo, la)) <= UMB]
        signo = 1
        if len({n for n, _ in pp}) >= 2:
            mn = sum(n for n, _ in pp) / len(pp)
            ma = sum(a for _, a in pp) / len(pp)
            cov = sum((n - mn) * (a - ma) for n, a in pp)
            if cov < 0:
                signo = -1

        def num2arc(N):
            f = (N - nmin) / (nmax - nmin)
            return f * L if signo > 0 else (1 - f) * L

    out = {}
    for rid, num in targets:
        if num is None:
            p = linea.interpolate(0.5 * L)
        else:
            arc = min(max(num2arc(num), 0.0), L)
            p = linea.interpolate(arc)
        out[rid] = (p.y, p.x)
    return out
