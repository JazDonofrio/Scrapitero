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
    1) **Mapbox por número** (solo `address`/`street`; descarta resultados a nivel ciudad);
    2) **línea de OSM** para las que Mapbox apila/no ubica;
    3) **Gemini** canoniza el nombre de calle no matcheado (ej. "R ORIEL B CAMPOS" →
       "Rua Oriel Bezerra de Campos", cacheado por calle) y se reintenta Mapbox/OSM;
    4) lo que ninguna ubica → **centro de la ciudad, marcado** `ciudad` (aproximado, no inventa
       una posición de calle). Las exactas de geocodebr (`g:numero`) se respetan."""
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

    try:
        # 1) Mapbox por número con el nombre original.
        if mapbox_on:
            for cn, items in por_calle.items():
                tg = [it for it in items if (it[5] or "") != "g:numero"]
                if tg:
                    for rid, p in _mapbox_calle(tg).items():
                        aceptados[rid] = (p[0], p[1], "mapbox")

        # 1.5) Cross-check contra geocodebr/CNEFE: Mapbox a veces ubica una calle ENTERA en el
        # lugar equivocado (homónimo / barrio errado) — coherente consigo misma, así que la guarda
        # por-calle (3.5) no la agarra. CNEFE (catastral oficial) es la verdad de terreno para la
        # UBICACIÓN de la calle (aunque solo dé un punto aproximado, sin secuencia por número). Si
        # el núcleo Mapbox de una calle quedó lejos del punto CNEFE y CNEFE es más coherente con el
        # grueso del relevamiento (más cerca del centroide), reubicamos TODA la calle al punto CNEFE.
        if aceptados:
            try:
                from scrapitero.agents.geocode_forward import geocodebr_lote
                lats0 = [float(r[3]) for r in rows]; lngs0 = [float(r[4]) for r in rows]
                cen = (sum(lats0) / len(lats0), sum(lngs0) / len(lngs0))
                reps, por_cn_rids = [], {}
                for cn, items in por_calle.items():
                    acc = [item_by_rid[i[0]] for i in items
                           if i[0] in aceptados and aceptados[i[0]][2] == "mapbox"]
                    if not acc:
                        continue
                    por_cn_rids[cn] = [i[0] for i in items if (i[5] or "") != "g:numero"]
                    md = sorted(acc, key=lambda r: _num(r[2]) or 0)[len(acc) // 2]  # fila mediana
                    reps.append({"id": cn, "logradouro": md[6] or "", "numero": md[2] or "",
                                 "municipio": (md[8] or ciudad_g or ""), "estado": md[9] or "",
                                 "bairro": md[7] or "", "cep": md[10] or ""})
                gb = geocodebr_lote(reps, max_desvio_m=1500.0) if reps else {}
                sospechosas = []
                for cn, t in gb.items():
                    gla, gln = t[0], t[1]
                    pts = [aceptados[r] for r in por_cn_rids[cn] if r in aceptados]
                    nla = sum(a[0] for a in pts) / len(pts); nln = sum(a[1] for a in pts) / len(pts)
                    # Distancia de CNEFE al punto MÁS CERCANO de la calle (no al centroide): una
                    # avenida larga bien repartida tiene su centroide lejos de cualquier punto único,
                    # pero CNEFE cae SOBRE la avenida (min chico) → no se toca. Solo cuando hasta el
                    # punto más cercano está lejos, toda la calle es sospechosa de estar mal ubicada.
                    if min(_hav_m(a[0], a[1], gla, gln) for a in pts) <= input.xcheck_geocodebr_m:
                        continue                                     # CNEFE cae sobre la calle → ok
                    if _hav_m(gla, gln, *cen) >= _hav_m(nla, nln, *cen):
                        continue                                     # CNEFE no es más central → no tocar
                    sospechosas.append((cn, gla, gln))
                # Para cada calle sospechosa, OSM es el ÁRBITRO: su geometría (nombre exacto) dice
                # dónde está REALMENTE la calle y permite interpolar los números secuencialmente.
                # Mapbox y OSM suelen coincidir (y CNEFE numero_aproximado ser el outlier) → OSM
                # gana. Solo si OSM no tiene la calle se la reubica al punto (apilado) de CNEFE.
                relocadas = osm_fix = 0
                for cn, gla, gln in sospechosas:
                    osm_pos = []
                    if input.usar_osm_fallback:
                        tg = [(rid, _num(item_by_rid[rid][2]), cn, item_by_rid[rid][6])
                              for rid in por_cn_rids[cn]]
                        osm_pos = _distribuir_osm(engine, input.baseline_id, tg, rows)
                    if osm_pos:
                        for rid, la, ln in osm_pos:
                            aceptados[rid] = (la, ln, "osm_interp")
                        osm_fix += 1
                    else:
                        for rid in por_cn_rids[cn]:
                            aceptados[rid] = (gla, gln, "g:numero_aproximado")
                        relocadas += 1
                if osm_fix or relocadas:
                    logger.info(f"baseline_interp: {len(sospechosas)} calle(s) mal ubicadas por "
                                f"Mapbox → {osm_fix} reinterpoladas sobre OSM, {relocadas} reubicadas "
                                f"a CNEFE (sin geometría OSM)")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"baseline_interp: cross-check geocodebr falló: {str(e)[:120]}")

        # 2) Línea de OSM para lo que Mapbox no ubicó.
        leftover = [(rid, info[rid][0], info[rid][1], calle_raw.get(info[rid][1]))
                    for rid in info if rid not in aceptados]
        if leftover and input.usar_osm_fallback:
            for rid, la, ln in _distribuir_osm(engine, input.baseline_id, leftover, rows):
                aceptados[rid] = (la, ln, "osm_interp")

        # 3) Gemini: canonizar el nombre de las calles aún sin ubicar y reintentar.
        rem_calles: dict = defaultdict(list)
        for rid in info:
            if rid not in aceptados:
                rem_calles[info[rid][1]].append(item_by_rid[rid])
        if rem_calles and input.usar_gemini:
            for cn, items in rem_calles.items():
                canon = _gemini_canonico(engine, cn, calle_raw.get(cn), ciudad_g, out)
                if not canon:
                    continue
                if mapbox_on:
                    for rid, p in _mapbox_calle(items, calle_override=canon).items():
                        aceptados[rid] = (p[0], p[1], "mapbox")
                pend = [(it[0], _num(it[2]), cn, canon) for it in items if it[0] not in aceptados]
                if pend and input.usar_osm_fallback:
                    for rid, la, ln in _distribuir_osm(engine, input.baseline_id, pend, rows):
                        aceptados[rid] = (la, ln, "osm_interp")

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

        # 4) Sin ubicar → centro de la ciudad, MARCADO (aproximado), sin inventar la cuadra.
        # No pisar un placement previo bueno (osm_interp/interp) si este run no lo re-ubicó
        # (p.ej. Overpass flakeó): solo mandamos al centro lo que está claramente mal/sin ubicar.
        if centro:
            for rid in info:
                if rid in aceptados:
                    continue
                if (item_by_rid[rid][5] or "") in ("osm_interp", "interp"):
                    continue   # conservar placement previo bueno
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
