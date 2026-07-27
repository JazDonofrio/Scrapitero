"""NumeroEstimator — infiere el NÚMERO DE PUERTA de las parcelas que el catastro dejó sin altura,
interpolando sobre el eje de la calle a partir de sus linderos con número real.

**Por qué existe:** el BCI de Várzea Grande trae la dirección (código de logradouro, calle, CEP,
bairro) pero en una parte de las parcelas el campo NÚMERO viene literal `0` (típico de
`TIPO IMÓVEL: Territorial`, lote não construído) o directamente en blanco — verificado contra los
PDFs originales, no es un fallo del parser. En el survey de la actualización de VG son 31 con `0`
+ 41 vacías = **12,8%**. Sin altura, esas parcelas no aparean por dirección contra el relevamiento
anterior y salen con `NUMERO` vacío en el CSV de operadora.

**Método** (mismo principio que `baseline_interp`, pero al revés — ahí es número→posición, acá
posición→número): una calle es una línea y las alturas crecen de forma monótona sobre ella.

1. **Eje de la calle**: geometría real de OSM vía `baseline_interp._osm_geometrias` (una consulta
   Overpass para toda la zona, cacheada en `calle_geometria` por ciudad/calle) + `_stitch` (cose
   los tramos y deduplica los carriles de las avenidas de doble mano). Si OSM no tiene la vía, cae
   a un **eje sintético por PCA** sobre las parcelas de esa calle (los loteamentos son rectos, así
   que la dirección principal aproxima bien el eje).
2. **Anclas** = parcelas de la misma calle con número real, proyectadas sobre el eje. Se depura la
   secuencia quedándose con la **subsecuencia monótona más larga** (arclength vs número): una
   parcela mal geometrizada o con la calle mal rotulada rompería la interpolación.
3. **Interpolación** piecewise entre las dos anclas que rodean al target; fuera del rango se
   extrapola con la pendiente del tramo extremo (con confianza menor y marcada en el método).
4. **Paridad**: se calcula de qué lado del eje cae cada parcela (signo del producto cruzado). Si
   las anclas de ese lado son mayoritariamente pares o impares, el estimado se ajusta a esa
   paridad — así una parcela de la vereda impar nunca recibe un número par.
5. **Sin colisiones**: si el número cae en uno ya usado en la calle (real o estimado en esta misma
   corrida) se desplaza al siguiente libre de la misma paridad.

**Se niega a inventar:** si la numeración de la calle no sigue el orden espacial (coherencia
< `min_coherencia`) o no hay anclas suficientes, la calle se saltea y se reporta en
`calles_descartadas`. Esas parcelas van al panel de incidencias como `numero_faltante`, donde el
operador carga el número a mano mirando el frente.

**Los números cargados a mano mandan:** al arrancar lee `parcela_numero_manual` (mig. 051, clave
`(region_id, cca_code)` para sobrevivir al re-scrape) y los usa como **anclas** — mejoran la
estimación de las vecinas — además de re-escribirlos con `metodo='manual'` y confianza 1,0, para
que un re-run nunca pise el trabajo humano.

**Nunca pisa el catastro:** escribe en `parcelas.numero_estimado` / `numero_estimado_metodo` /
`numero_estimado_confianza` (mig. 050). `parcelas.numero` queda intacto, y la UI/CSV muestran el
valor marcado como estimado. Idempotente por survey/región (recalcula y reescribe; limpia las
parcelas que ya no aplican). `dry_run=True` devuelve el detalle sin tocar la DB.
"""

from __future__ import annotations

from typing import Optional

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.db.engine import get_engine

# Un target más lejos que esto del eje de su calle no se estima: o la calle está mal rotulada
# en el catastro, o el eje matcheado es un homónimo. Vale para lotes de fondo grande.
MAX_DIST_EJE_M = 120.0
# Distancia máxima al ancla más cercana para considerar la interpolación "apoyada".
DIST_ANCLA_BUENA_M = 80.0


class NumeroEstimatorInput(BaseModel):
    survey_id: Optional[str] = None
    region_id: Optional[str] = None
    # anclas (parcelas con número real) mínimas por calle para intentar la interpolación
    min_anclas: int = 3
    # coherencia mínima de la numeración de la calle (fracción de anclas que respetan el orden
    # espacial). Por debajo NO se estima: ver `_monotona_mas_larga` y el comentario en `run`.
    min_coherencia: float = 0.6
    max_dist_eje_m: float = MAX_DIST_EJE_M
    # extrapolar más allá del rango de anclas (con confianza menor). Si False, esas quedan sin estimar.
    extrapolar: bool = True
    usar_osm: bool = True          # False → sólo eje PCA (sin tocar Overpass)
    dry_run: bool = False          # calcula y devuelve, no escribe
    max_detalle: int = 400         # filas de detalle en la salida


class NumeroEstimatorOutput(BaseModel):
    ok: bool = True
    error: Optional[str] = None
    survey_id: Optional[str] = None
    region_id: Optional[str] = None
    candidatas: int = 0            # parcelas con calle pero sin número utilizable
    estimadas: int = 0
    sin_estimar: int = 0
    calles: int = 0                # calles con al menos una estimación
    por_metodo: dict = {}
    # calles salteadas y por qué: {calle: 'pocas_anclas' | 'numeracion_incoherente:0.42' | 'sin_eje'}
    calles_descartadas: dict = {}
    detalle: list = []             # [{parcela_id, cca_code, calle, numero_estimado, metodo, conf}]


# ─────────────────────────────────────────────────────────────────────────────
# Geometría auxiliar
# ─────────────────────────────────────────────────────────────────────────────

def _proyector(lat0: float, lng0: float):
    """(lng,lat) → (x,y) en metros, plano local equirectangular centrado en (lng0,lat0).

    Alcanza y sobra para una calle (pocos km) y evita depender del huso UTM por punto."""
    import math
    kx = 111320.0 * math.cos(math.radians(lat0))
    ky = 110540.0
    return lambda lng, lat: ((lng - lng0) * kx, (lat - lat0) * ky)


def _eje_pca(pts: list):
    """Eje sintético (LineString en metros) por dirección principal de una nube de puntos.

    Respaldo para cuando OSM no tiene la vía. Los loteamentos son rectos, así que la primera
    componente principal aproxima el eje de la calle; basta para ordenar y para la paridad."""
    from shapely.geometry import LineString
    n = len(pts)
    if n < 2:
        return None
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    syy = sum((p[1] - my) ** 2 for p in pts)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
    # autovector dominante de [[sxx,sxy],[sxy,syy]]
    tr, det = sxx + syy, sxx * syy - sxy * sxy
    disc = max(tr * tr / 4.0 - det, 0.0) ** 0.5
    lam = tr / 2.0 + disc
    if abs(sxy) > 1e-9:
        vx, vy = lam - syy, sxy
    else:
        vx, vy = (1.0, 0.0) if sxx >= syy else (0.0, 1.0)
    norm = (vx * vx + vy * vy) ** 0.5
    if norm < 1e-9:
        return None
    vx, vy = vx / norm, vy / norm
    ts = [(p[0] - mx) * vx + (p[1] - my) * vy for p in pts]
    t0, t1 = min(ts) - 30.0, max(ts) + 30.0
    return LineString([(mx + t0 * vx, my + t0 * vy), (mx + t1 * vx, my + t1 * vy)])


def _lado(linea, x: float, y: float) -> int:
    """+1/-1 según de qué lado del eje cae el punto (0 si está sobre el eje).

    Producto cruzado entre la dirección local del eje (tomada en el punto proyectado) y el
    vector eje→punto. Sirve para separar vereda par de vereda impar."""
    from shapely.geometry import Point
    L = linea.length
    if L <= 0:
        return 0
    s = linea.project(Point(x, y))
    a = linea.interpolate(max(s - 5.0, 0.0))
    b = linea.interpolate(min(s + 5.0, L))
    dx, dy = b.x - a.x, b.y - a.y
    p = linea.interpolate(s)
    cross = dx * (y - p.y) - dy * (x - p.x)
    if abs(cross) < 1e-6:
        return 0
    return 1 if cross > 0 else -1


def _monotona_mas_larga(anclas: list) -> list:
    """Subsecuencia monótona (creciente o decreciente) más larga de [(arc, num)] ordenada por arc.

    Descarta anclas que rompen la secuencia: una parcela con la calle mal rotulada, o partida en
    dos por el catastro, desordenaría la interpolación de todo el tramo. Se prueban las dos
    direcciones y gana la más larga (la numeración puede crecer en cualquier sentido del eje)."""
    if len(anclas) <= 2:
        return anclas

    def _lis(seq, creciente: bool) -> list:
        n = len(seq)
        best = [1] * n
        prev = [-1] * n
        for i in range(n):
            for j in range(i):
                ok = seq[j][1] <= seq[i][1] if creciente else seq[j][1] >= seq[i][1]
                if ok and best[j] + 1 > best[i]:
                    best[i], prev[i] = best[j] + 1, j
        if not n:
            return []
        i = max(range(n), key=lambda k: best[k])
        out = []
        while i >= 0:
            out.append(seq[i])
            i = prev[i]
        return out[::-1]

    a = _lis(anclas, True)
    b = _lis(anclas, False)
    return a if len(a) >= len(b) else b


def _sin_absurdos(planas: list) -> list:
    """Descarta anclas con un número disparatado para su calle (error de carga del catastro).

    Medido en VG: «SÃO BERNARDO» tiene una parcela cargada con el número **51000** entre vecinas
    de 2 y 3 cifras. Como ancla arrastra toda la interpolación del tramo (una vecina real de 58
    salía estimada en 2271). Filtro robusto por MAD (no por media, que el propio outlier corre)
    más un techo absoluto: ninguna calle tiene alturas de 6 cifras."""
    if len(planas) < 4:
        return [(a, n) for a, n in planas if n < 100000]
    nums = sorted(n for _a, n in planas)
    med = nums[len(nums) // 2]
    desv = sorted(abs(n - med) for n in nums)
    mad = desv[len(desv) // 2] or 1
    lim = 20 * mad + 200
    return [(a, n) for a, n in planas if abs(n - med) <= lim and n < 100000]


def _pendiente_local(ax: list, arc: float, ventana: float = 250.0) -> Optional[float]:
    """Números por metro alrededor de `arc` (Theil-Sen: mediana de las pendientes de a pares).

    La densidad de numeración NO es constante a lo largo de una avenida (cambia con el tamaño de
    los lotes, y muchas vías reinician la cuenta al cruzar un límite de bairro). Una pendiente
    global mete errores de cientos de números; una local, del entorno del target, no."""
    sub = [(a, n) for a, n in ax if abs(a - arc) <= ventana]
    if len(sub) < 3:
        sub = sorted(ax, key=lambda t: abs(t[0] - arc))[:6]
    pend = [(n2 - n1) / (a2 - a1) for i, (a1, n1) in enumerate(sub)
            for (a2, n2) in sub[i + 1:] if abs(a2 - a1) > 5.0]
    if not pend:
        return None
    pend.sort()
    return pend[len(pend) // 2]


def _interpolar(arc: float, ax: list) -> tuple:
    """Número en `arc` según las anclas `ax`=[(arc,num)] (ordenadas). → (num, extrapolado)."""
    if arc <= ax[0][0]:
        (a0, n0), (a1, n1) = ax[0], ax[1]
        extrap = arc < ax[0][0]
    elif arc >= ax[-1][0]:
        (a0, n0), (a1, n1) = ax[-2], ax[-1]
        extrap = arc > ax[-1][0]
    else:
        extrap = False
        (a0, n0), (a1, n1) = ax[0], ax[1]
        for i in range(len(ax) - 1):
            if ax[i][0] <= arc <= ax[i + 1][0]:
                (a0, n0), (a1, n1) = ax[i], ax[i + 1]
                break
    if a1 == a0:
        return float(n0), extrap
    return n0 + (n1 - n0) * (arc - a0) / (a1 - a0), extrap


def _estimar(arc: float, ax: list) -> tuple:
    """Número estimado en `arc`. → (valor, extrapolado, dist_ancla_m, discrepancia).

    Combina dos modelos y los contrasta:
      - **interpolación** entre las dos anclas que rodean al target (exacta cuando el tramo es
        homogéneo);
      - **local**: número del ancla MÁS CERCANA + pendiente local × distancia. El error queda
        acotado por la distancia a esa ancla, así que aguanta discontinuidades de numeración.
    Si coinciden se usa la interpolación; si no (tramo con salto: cambio de bairro, homónimo
    cosido, avenida que reinicia), manda el modelo local y baja la confianza."""
    a_near, n_near = min(ax, key=lambda t: abs(t[0] - arc))
    d_near = abs(arc - a_near)
    est_int, extrap = _interpolar(arc, ax)
    m = _pendiente_local(ax, arc)
    if m is None:
        return est_int, extrap, d_near, False
    est_loc = n_near + m * (arc - a_near)
    if extrap:
        return est_loc, True, d_near, False
    tol = max(15.0, 0.35 * abs(est_loc - n_near) + 15.0)
    if abs(est_int - est_loc) <= tol:
        return est_int, False, d_near, False
    return est_loc, False, d_near, True


def _ajustar(valor: float, paridad: Optional[int], usados: set) -> Optional[int]:
    """Redondea respetando paridad y evitando números ya usados en la calle."""
    n = int(round(valor))
    if n < 1:
        n = 1 if paridad is None else (1 if paridad == 1 else 2)
    if paridad is not None and n % 2 != paridad:
        n += 1                                   # el vecino inmediato de la paridad correcta
    paso = 2 if paridad is not None else 1
    for k in range(0, 12):                       # ±11 posiciones: suficiente para una cuadra
        for cand in ((n + k * paso), (n - k * paso)):
            if cand >= 1 and cand not in usados:
                return cand
    return None


# ─────────────────────────────────────────────────────────────────────────────

@agent_run
def run(input: NumeroEstimatorInput) -> NumeroEstimatorOutput:
    from collections import Counter, defaultdict
    from shapely.geometry import Point
    from shapely.ops import transform as shp_transform

    from scrapitero.agents.baseline_interp import _osm_geometrias, _stitch
    from scrapitero.agents.direccion_norm import normalizar_numero, nucleo_calle

    out = NumeroEstimatorOutput(survey_id=input.survey_id, region_id=input.region_id)
    if not input.survey_id and not input.region_id:
        return NumeroEstimatorOutput(ok=False, error="falta survey_id o region_id")

    engine = get_engine()
    scope = "p.survey_id::text = :sid" if input.survey_id else "p.region_id = :rid"
    params = {"sid": input.survey_id} if input.survey_id else {"rid": input.region_id}

    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT p.parcela_id::text, p.calle, p.numero, p.centroid_lat, p.centroid_lng,
                   p.cca_code, p.municipio, p.region_id
            FROM parcelas p
            WHERE {scope} AND p.calle IS NOT NULL AND p.calle <> ''
              AND p.centroid_lat IS NOT NULL AND p.centroid_lng IS NOT NULL
        """), params).fetchall()
        # Números cargados a mano por el operador desde el panel de incidencias (mig. 051).
        # Son trabajo humano: valen MÁS que cualquier interpolación. Entran como anclas (mejoran
        # la estimación de sus vecinas) y se re-escriben al final para que un re-run no los pise.
        manual = {c: n for c, n in conn.execute(text(
            "SELECT cca_code, numero FROM parcela_numero_manual WHERE region_id = :r"),
            {"r": input.region_id or (rows[0][7] if rows else None)}).fetchall()}

    if not rows:
        return NumeroEstimatorOutput(ok=False, survey_id=input.survey_id,
                                     region_id=input.region_id,
                                     error="no hay parcelas con calle y centroide en el alcance")
    out.region_id = out.region_id or rows[0][7]
    ciudad = Counter(r[6] for r in rows if r[6]).most_common(1)
    ciudad = ciudad[0][0] if ciudad else None

    # Agrupar por núcleo de calle (tolerante a "RUA - X" vs "X", títulos, acentos).
    por_calle: dict = defaultdict(lambda: {"anclas": [], "targets": [], "raw": None})
    updates: list = []
    for pid, calle, numero, lat, lng, cca, _mun, _reg in rows:
        nuc = nucleo_calle(calle)
        if not nuc:
            continue
        g = por_calle[nuc]
        g["raw"] = g["raw"] or calle
        num = normalizar_numero(numero)          # '' cubre NULL, '', '0', 'S/N'
        if not num and cca and cca in manual:
            # Cargado a mano: ancla (no target) y se re-aplica tal cual.
            num = normalizar_numero(manual[cca])
            if num:
                updates.append((pid, str(manual[cca]).strip()[:20], "manual", 1.0))
        if num:
            g["anclas"].append((int(num), float(lat), float(lng)))
        else:
            g["targets"].append((pid, float(lat), float(lng), cca, calle))

    # `candidatas` = las que siguen sin número (las cargadas a mano ya están resueltas y
    # entraron como anclas, pero cuentan como estimadas por método 'manual').
    out.candidatas = sum(len(g["targets"]) for g in por_calle.values()) + len(updates)
    if not out.candidatas:
        logger.info("NumeroEstimator: no hay parcelas sin número en el alcance")
        return out

    # Ejes OSM: UNA consulta para toda la zona (cacheada por ciudad/calle en calle_geometria).
    lats = [r[3] for r in rows]
    lngs = [r[4] for r in rows]
    bbox = (min(lats) - 0.01, min(lngs) - 0.01, max(lats) + 0.01, max(lngs) + 0.01)
    geoms: dict = {}
    if input.usar_osm:
        con_target = [(cn, g["raw"]) for cn, g in por_calle.items() if g["targets"]]
        try:
            geoms = _osm_geometrias(engine, con_target, ciudad, bbox)
        except Exception as exc:                 # noqa: BLE001 — best-effort: sigue con PCA
            logger.warning(f"NumeroEstimator: ejes OSM no disponibles ({exc}); se usa PCA")
            geoms = {}

    por_metodo: Counter = Counter({"manual": len(updates)}) if updates else Counter()
    calles_ok = 0

    for cn, g in por_calle.items():
        targets, anclas = g["targets"], g["anclas"]
        if not targets:
            continue
        if len(anclas) < input.min_anclas:
            out.calles_descartadas[g["raw"]] = f"pocas_anclas:{len(anclas)}"
            continue

        # Plano local en metros centrado en la calle.
        lat0 = sum(a[1] for a in anclas) / len(anclas)
        lng0 = sum(a[2] for a in anclas) / len(anclas)
        fwd = _proyector(lat0, lng0)
        pxy = lambda la, lo: fwd(lo, la)                                  # noqa: E731

        # 1) eje OSM (cosido y sin carriles duplicados) → metros; 2) respaldo PCA.
        linea = None
        metodo = "eje_pca"
        lineas_osm = geoms.get(cn) or []
        if lineas_osm:
            # quedarse con los tramos cerca de las parcelas de la calle (descarta homónimos)
            pts_sop = [Point(a[2], a[1]) for a in anclas]
            cerca = [ln for ln in lineas_osm
                     if any(ln.distance(p) <= 0.0020 for p in pts_sop)]   # ~200 m
            cosida = _stitch(cerca or lineas_osm)
            if cosida is not None and cosida.length > 0:
                linea = shp_transform(lambda xs, ys, z=None: fwd(xs, ys), cosida)
                metodo = "eje_osm"
        if linea is None:
            nube = [pxy(a[1], a[2]) for a in anclas] + [pxy(t[1], t[2]) for t in targets]
            linea = _eje_pca(nube)
        if linea is None or linea.length <= 0:
            out.calles_descartadas[g["raw"]] = "sin_eje"
            continue

        # 2) anclas sobre el eje, depuradas a la subsecuencia monótona más larga
        proy = []
        for num, la, lo in anclas:
            x, y = pxy(la, lo)
            p = Point(x, y)
            if linea.distance(p) > input.max_dist_eje_m:
                continue                          # ancla lejos del eje: geometría o rótulo dudoso
            proy.append((linea.project(p), num, _lado(linea, x, y)))
        proy.sort(key=lambda t: t[0])
        # promediar anclas del mismo número (edificios con varias inscrições en la misma altura)
        agg: dict = defaultdict(list)
        for arc, num, lado in proy:
            agg[num].append((arc, lado))
        planas = _sin_absurdos(sorted((sum(a for a, _ in v) / len(v), num)
                                      for num, v in agg.items()))
        limpias = _monotona_mas_larga(planas)
        if len(limpias) < 2:
            out.calles_descartadas[g["raw"]] = "sin_secuencia"
            continue

        # COHERENCIA de la numeración: qué fracción de las anclas respeta el orden espacial.
        # No es un detalle: medido por leave-one-out sobre las 469 parcelas CON número de la
        # actualización de VG, en calles coherentes el error mediano es 6 y el p90 21; en las
        # incoherentes (JOAO LIBANIO 0,41 · CLOVIS HUGNEY 0,42 · SÃO BERNARDO 0,56) el p90 se
        # dispara a 227. Ahí la numeración del catastro sencillamente no sigue la calle —
        # ninguna interpolación puede acertar, así que es preferible no estimar.
        coher = len(limpias) / max(len(planas), 1)
        if coher < input.min_coherencia:
            out.calles_descartadas[g["raw"]] = f"numeracion_incoherente:{coher:.2f}"
            continue

        # 3) paridad por vereda (sólo si la evidencia es clara)
        lados: dict = defaultdict(Counter)
        for _arc, num, lado in proy:
            if lado:
                lados[lado][num % 2] += 1
        paridad_lado: dict = {}
        for lado, c in lados.items():
            tot = sum(c.values())
            if tot >= 3:
                par, n = c.most_common(1)[0]
                if n / tot >= 0.7:
                    paridad_lado[lado] = par

        usados = {num for _arc, num in planas}
        estimadas_calle = 0
        for pid, la, lo, cca, calle_raw in targets:
            x, y = pxy(la, lo)
            p = Point(x, y)
            dist_eje = linea.distance(p)
            if dist_eje > input.max_dist_eje_m:
                continue
            arc = linea.project(p)
            valor, extrap, d_ancla, discrepa = _estimar(arc, limpias)
            if extrap and not input.extrapolar:
                continue
            paridad = paridad_lado.get(_lado(linea, x, y))
            num = _ajustar(valor, paridad, usados)
            if num is None:
                continue
            usados.add(num)

            # confianza: apoyo de las anclas que rodean + penalización por extrapolar/PCA/salto
            conf = 0.85
            conf -= 0.35 if extrap else 0.0
            conf -= 0.10 if metodo == "eje_pca" else 0.0
            conf -= 0.15 if discrepa else 0.0
            conf -= (1.0 - coher) * 0.30
            conf -= min(d_ancla / DIST_ANCLA_BUENA_M, 1.0) * 0.25
            conf -= 0.05 if paridad is None else 0.0
            conf = round(max(min(conf, 0.9), 0.1), 2)
            met = metodo + ("_extrap" if extrap else "")

            updates.append((pid, str(num), met, conf))
            por_metodo[met] += 1
            estimadas_calle += 1
            if len(out.detalle) < input.max_detalle:
                out.detalle.append({
                    "parcela_id": pid, "cca_code": cca, "calle": calle_raw,
                    "numero_estimado": num, "metodo": met, "confianza": conf,
                    "dist_eje_m": round(dist_eje, 1),
                })
        if estimadas_calle:
            calles_ok += 1

    out.estimadas = len(updates)
    out.sin_estimar = out.candidatas - out.estimadas
    out.calles = calles_ok
    out.por_metodo = dict(por_metodo)

    if input.dry_run:
        logger.info(f"NumeroEstimator (dry-run): {out.estimadas}/{out.candidatas} estimadas "
                    f"en {out.calles} calles — {out.por_metodo}")
        return out

    # Escritura: SOLO las columnas *_estimado (parcelas.numero es del municipio, no se toca).
    # Idempotente: limpia el alcance y reescribe (una parcela que recuperó su número real en un
    # re-scrape deja de tener estimación).
    with engine.begin() as conn:
        conn.execute(text(f"""
            UPDATE parcelas p SET numero_estimado = NULL, numero_estimado_metodo = NULL,
                                  numero_estimado_confianza = NULL
            WHERE {scope} AND p.numero_estimado IS NOT NULL
        """), params)
        for i in range(0, len(updates), 500):
            conn.execute(text("""
                UPDATE parcelas SET numero_estimado = d.num, numero_estimado_metodo = d.met,
                                    numero_estimado_confianza = d.conf
                FROM (SELECT UNNEST(CAST(:ids AS uuid[])) AS pid,
                             UNNEST(CAST(:nums AS text[])) AS num,
                             UNNEST(CAST(:mets AS text[])) AS met,
                             UNNEST(CAST(:confs AS float8[])) AS conf) d
                WHERE parcelas.parcela_id = d.pid
            """), {
                "ids": [u[0] for u in updates[i:i + 500]],
                "nums": [u[1] for u in updates[i:i + 500]],
                "mets": [u[2] for u in updates[i:i + 500]],
                "confs": [u[3] for u in updates[i:i + 500]],
            })

    logger.info(f"NumeroEstimator: {out.estimadas}/{out.candidatas} números estimados en "
                f"{out.calles} calles — {out.por_metodo}")
    return out
