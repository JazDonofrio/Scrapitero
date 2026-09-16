"""DireccionAR — la dirección de una parcela argentina, sin pagar geocoding.

El problema: **ARBA/IDERA entrega la geometría catastral pero no la dirección**. En Merlo son
41.825 parcelas de 42.025 sin calle, y `AddressResolver` fuera de Brasil va derecho a Google
Maps, que se paga: ~USD 210 para ese relevamiento. Este agente hace lo mismo gratis, con dos
fuentes públicas que el proyecto ya usa para otras cosas.

* **La calle sale del eje de OSM.** Una sola consulta Overpass por zona trae todas las vías con
  nombre; a cada parcela se le asigna la más cercana. Medido en Merlo: 3.688 vías, 946 nombres,
  y el **98,5% de las parcelas tiene una calle a ≤40 m** (mediana 22 m).
* **El número sale del callejero oficial** (`georef-ar`, `apis.datos.gob.ar`, gratis y sin
  token): publica por partido cada calle con su **rango de alturas y por vereda**
  —`{inicio:{derecha,izquierda}, fin:{derecha,izquierda}}`—. Proyectando la parcela sobre el eje
  se interpola la altura contra ese rango. En Merlo, 1.443 calles con el 84% de los rangos
  utilizables.

⚠ **Lo que esto NO es.** La calle es una inferencia por cercanía, no un dato del catastro: en
una esquina puede tocarle la transversal. Y la altura es interpolada. Por eso la calle se sella
`direccion_source='osm_georef'` —queda visible en el CSV y en la ficha de procedencia, así el
cliente sabe qué está leyendo— y el número va a `numero_estimado`, **nunca** a `parcelas.numero`,
igual que hace `NumeroEstimator` en Brasil.

⚠ **La zona puede cruzar partidos y el callejero se pide por partido.** Los 63 km² de Merlo
tienen esquinas en Moreno y en Ituzaingó; pidiendo sólo el callejero de Merlo, un tercio de los
nombres de OSM quedaba sin rango de alturas. El agente resuelve los partidos por
reverse-geocoding de las esquinas y el centro, y pide el callejero de todos.

**Qué tan bien anda, medido contra las 800 direcciones que Google alcanzó a resolver en Merlo
antes de que se cortara** (es la única referencia disponible, y el callejero oficial la respalda:
geocodificadas con georef caen a mediana 37 m de su parcela, ninguna a más de 157 m):

    acierto de la calle, comparación estricta ......... 74,8%
    con el criterio de `_misma_calle` del proyecto .... 78,6%
    discrepancia real ................................ 21,4%

⚠ La comparación estricta **subestima**: `Sullivan` y `Alejandro Sullivan` son la misma calle, y
por eso se compara con `_misma_calle`, que ya resuelve el nombre de pila omitido. De lo que
queda, buena parte son **esquinas**: los nombres aparecen cruzados entre parcelas vecinas —a una
Google le pone `Sullivan` y este agente `Osorio`, a la de al lado al revés—, que es lo que pasa
cuando el lote da a dos calles y cada fuente elige una. Ninguna de las dos está equivocada.

Dos hipótesis que se probaron y **se cayeron**, anotadas para no repetirlas: (1) que Google
devolviera calles de Capital Federal —Achega, Pepirí, Altolaguirre existen también en Merlo e
Ituzaingó, verificado en el callejero oficial—; (2) que el error viniera de medir desde el
centroide en lotes profundos — medir desde el polígono completo da igual (73,9% contra 74,8%).

⚠ **La paridad no se puede atar al lado real.** georef dice qué alturas van por la vereda
«derecha» y cuáles por la «izquierda», pero esa mano es respecto del sentido en que crece la
numeración, que no tiene por qué coincidir con el sentido del `LineString` de OSM. Lo que sí se
garantiza es que **las dos veredas queden con paridades distintas y consistentes**, que es lo
que evita dos parcelas enfrentadas con el mismo número. Cuál de las dos es la par, en una calle
donde el sentido de OSM esté invertido, puede salir cambiado.
"""

from __future__ import annotations

import math
import time
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform
from shapely.strtree import STRtree
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents import georef_cuadras
from scrapitero.agents.direccion_norm import normalizar_calle, nucleo_calle
from scrapitero.agents.precedencia import (DIRECCION_FUENTE_PROTEGIDA,
                                           DIRECCION_FUENTES_PROTEGIDAS)
from scrapitero.db.engine import get_engine

_GEOREF = "https://apis.datos.gob.ar/georef/api"
# El sello: la calle salió de OSM y la altura del callejero oficial. No es el catastro, y el
# CSV y la ficha de procedencia tienen que poder decirlo.
_FUENTE = "osm_georef"
_METODO = "georef_osm"
# La altura salió del rango de LA CUADRA (georef bulk), no del de la calle entera. Se sella
# distinto para poder medir cuál de los dos puso cada número: son métodos con precisión muy
# distinta y el CSV y la ficha de procedencia tienen que poder distinguirlos.
_METODO_CUADRA = "georef_cuadra"


class DireccionARInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None
    # Hasta dónde se acepta que esté el eje de la calle. 40 m cubre el 98,5% de las parcelas de
    # Merlo; más allá la asignación empieza a ser un adivine (fondos de manzana, baldíos
    # grandes) y conviene dejar la parcela sin calle antes que ponerle una equivocada.
    max_dist_m: float = 40.0
    # Las calles se piden a Overpass en un bbox AMPLIADO: para interpolar la altura hace falta
    # el eje COMPLETO de la calle, no el pedacito que cae en la zona. Con el eje recortado, el
    # rango oficial (que es de la calle entera) se estira sobre el tramo y da números absurdos.
    margen_km: float = 3.0
    solo_faltantes: bool = True
    # Interpolar contra el rango de CADA CUADRA (georef bulk) en vez del de la calle entera.
    # Es lo que decide si el número sirve: con el rango global, de 76.629 estimaciones de
    # Merlo sólo 4.018 superaban el piso de confianza. Con `False` se vuelve al método viejo
    # (y no se toca el bulk de 1,12 GB, que la primera vez hay que bajar).
    usar_cuadras: bool = True
    # Medir la distancia a la calle desde el POLÍGONO del lote y no desde su centro. El
    # centro de un fondo de manzana o de un baldío grande queda a más de `max_dist_m` del
    # eje aunque su FRENTE dé a la calle, y esas parcelas quedaban sin calle: 2.615 en
    # Merlo. La proyección sobre el eje (para la altura y la vereda) se sigue haciendo con
    # el centroide, que es lo que representa al lote. Cuesta traer la geometría completa,
    # así que para barrer las 94.000 de una zona entera conviene apagarlo.
    usar_poligono: bool = True
    dry_run: bool = False
    batch: int = 5000


class DireccionAROutput(BaseModel):
    ok: bool
    parcelas: int = 0
    con_calle: int = 0
    con_numero: int = 0
    sin_calle_cerca: int = 0
    calles_osm: int = 0
    calles_oficiales: int = 0
    con_numero_cuadra: int = 0     # cuántos salieron por cuadra (el resto, por calle entera)
    partidos: list[str] = []
    escritas: int = 0
    error: Optional[str] = None


# ── fuentes ───────────────────────────────────────────────────────────────────
def _partidos(bbox) -> list[str]:
    """Qué partidos toca la zona. Se consultan las 4 esquinas y el centro: el callejero de
    georef se pide por partido, y una zona grande cruza varios (Merlo toca Moreno e Ituzaingó).
    """
    s, w, n, e = bbox
    pts = [((s + n) / 2, (w + e) / 2), (n, w), (n, e), (s, w), (s, e)]
    out: list[str] = []
    for lat, lon in pts:
        try:
            r = httpx.get(f"{_GEOREF}/ubicacion", params={"lat": lat, "lon": lon},
                          timeout=30).json()
            d = ((r.get("ubicacion") or {}).get("departamento") or {}).get("nombre")
            if d and d not in out:
                out.append(d)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"direccion_ar: georef/ubicacion falló en {lat},{lon}: {exc}")
    return out


def _callejero(partidos: list[str]) -> dict[str, tuple]:
    """El callejero oficial de esos partidos: {calle_norm: (nombre, alt_ini, alt_fin, pares)}.

    `pares` es la paridad que georef asigna a la vereda «izquierda» — se deduce de su propio
    rango en vez de suponerla.
    """
    out: dict[str, tuple] = {}
    for dep in partidos:
        off, total = 0, None
        while total is None or off < total:
            try:
                r = httpx.get(f"{_GEOREF}/calles", timeout=60, params={
                    "provincia": "buenos aires", "departamento": dep,
                    "max": 1000, "inicio": off, "campos": "completo"}).json()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"direccion_ar: georef/calles falló en {dep}: {exc}")
                break
            total = r.get("total", 0)
            for c in r.get("calles", []):
                a = c.get("altura") or {}
                ini = (a.get("inicio") or {}).get("derecha")
                fin = (a.get("fin") or {}).get("derecha")
                fin_izq = (a.get("fin") or {}).get("izquierda")
                if ini is None or fin is None or fin <= 0:
                    continue
                izq_par = (fin_izq % 2 == 0) if isinstance(fin_izq, int) else True
                k = normalizar_calle(c["nombre"])
                # Si la calle está en dos partidos, gana el rango más amplio: el eje de OSM
                # tampoco se corta en el límite administrativo.
                if k not in out or (fin - ini) > (out[k][2] - out[k][1]):
                    out[k] = (c["nombre"], ini, fin, izq_par)
            off += 1000
            if not r.get("calles"):
                break
    return out


def _preparar_cuadras(cuadras: dict[str, list[dict]], proj) -> dict[str, list[tuple]]:
    """Las cuadras de georef proyectadas a metros: {calle_norm: [(linea, rangos...), ...]}."""
    out: dict[str, list[tuple]] = {}
    for k, lista in cuadras.items():
        for c in lista:
            try:
                linea = transform(proj, LineString(c["coords"]))
            except Exception:  # noqa: BLE001
                continue
            if linea.length <= 0:
                continue
            out.setdefault(k, []).append(
                (linea, c["ini_der"], c["fin_der"], c["ini_izq"], c["fin_izq"]))
    return out


def _numero_por_cuadra(cuadras: list[tuple], pt, max_dist_m: float):
    """Altura interpolada dentro de la CUADRA donde cae la parcela, y su confianza.

    Es lo que reemplaza a estirar el rango de la calle entera sobre todo su eje: una calle de
    Merlo tiene 9 cuadras de mediana, así que el rango global daba un número cualquiera y la
    confianza salía en 0,2. Acá el tramo mide ~100 m y su rango son ~100 números.

    El lado decide la paridad: se usa el rango de la vereda donde cae la parcela y, si ese
    lado no lo publica (`0`, el sentinela de georef), se cae al otro corrigiendo la paridad.
    Vale la misma advertencia que el resto del módulo: cuál vereda es «derecha» depende del
    sentido del tramo, así que lo garantizado es que los dos lados queden con paridades
    distintas y consistentes, no que «derecha» sea un lado físico fijo.
    """
    mejor, dmin = None, None
    for c in cuadras:
        d = c[0].distance(pt)
        if dmin is None or d < dmin:
            mejor, dmin = c, d
    if mejor is None or dmin > max_dist_m:
        return None, None
    linea, ini_der, fin_der, ini_izq, fin_izq = mejor
    izquierda = _lado(linea, pt) > 0
    ini, fin = (ini_izq, fin_izq) if izquierda else (ini_der, fin_der)
    presta = False
    if not (ini and fin):                      # ese lado no publica rango: se usa el otro
        ini, fin = (ini_der, fin_der) if izquierda else (ini_izq, fin_izq)
        presta = True
    if not (ini and fin):
        return None, None
    frac = min(max(linea.project(pt) / linea.length, 0.0), 1.0)
    n = int(round(ini + frac * (fin - ini)))
    if presta:                                 # el rango prestado es de la otra vereda
        n += 1
    n = max(n, 1)
    # Confianza: alta por construcción (el tramo es una cuadra), penalizada por la distancia
    # al eje y por un rango de cuadra desmesurado (una «cuadra» de 1.000 números no es tal).
    amplitud = abs(fin - ini)
    conf = 0.8 * (1 - min(dmin / max(max_dist_m, 1.0), 1.0) * 0.5)
    if amplitud > 400:
        conf *= 0.7
    if presta:
        conf *= 0.9
    return n, round(max(0.2, min(0.85, conf)), 2)


def _vias_osm(bbox) -> list[tuple[str, LineString]]:
    """Todas las vías con nombre del bbox, en UNA consulta (sin regex → Overpass la resuelve
    rápido). Es la misma forma que usa `baseline_interp` para la red de calles."""
    from scrapitero.agents.osm_building_fetcher import _fetch_overpass
    s, w, n, e = bbox
    q = f'[out:json][timeout:180];way[highway][name]({s},{w},{n},{e});out geom;'
    data = None
    for intento in range(4):
        try:
            data = _fetch_overpass(q, timeout=170)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"direccion_ar: Overpass falló (intento {intento + 1}): {exc}")
            data = None
        if data and data.get("elements"):
            break
        time.sleep(3)
    if not data or not data.get("elements"):
        # Un 0 acá no es «no hay calles», es «Overpass no contestó». Que el agente falle es
        # preferible a dejar 42.000 parcelas sin dirección como si fuera un resultado.
        raise RuntimeError("Overpass no devolvió la red de calles (probá de nuevo)")
    out = []
    for x in data["elements"]:
        nm = (x.get("tags") or {}).get("name")
        g = x.get("geometry") or []
        if nm and len(g) > 1:
            out.append((nm, LineString([(p["lon"], p["lat"]) for p in g])))
    return out


# ── el trabajo ────────────────────────────────────────────────────────────────
def _buscar_rango(calle_norm: str, oficial: dict) -> Optional[tuple]:
    """El rango de alturas de esa calle, con el mismo escalón de matcheo que el resto del
    proyecto: exacto, después el núcleo del nombre, después difuso."""
    if calle_norm in oficial:
        return oficial[calle_norm]
    nuc = nucleo_calle(calle_norm)
    for k, v in oficial.items():
        if nucleo_calle(k) == nuc:
            return v
    import difflib
    m = difflib.get_close_matches(calle_norm, list(oficial), n=1, cutoff=0.86)
    return oficial[m[0]] if m else None


def _lado(eje: LineString, pt: Point) -> int:
    """De qué lado del eje cae el punto (+1 / -1), por producto cruzado en el tramo más cercano.
    Es lo que separa una vereda de la otra; cuál es «derecha» para georef no se sabe (ver el
    docstring del módulo)."""
    s = eje.project(pt)
    a = eje.interpolate(max(s - 5, 0))
    b = eje.interpolate(min(s + 5, eje.length))
    cruz = (b.x - a.x) * (pt.y - a.y) - (b.y - a.y) * (pt.x - a.x)
    return 1 if cruz >= 0 else -1


@agent_run
def run(input: DireccionARInput) -> DireccionAROutput:
    from scrapitero.agents.baseline_interp import _stitch
    import pyproj

    engine = get_engine()
    with engine.connect() as conn:
        sid = input.survey_id or conn.execute(text("""
            SELECT survey_id::text FROM surveys WHERE region_id = :r AND NOT archivado
            ORDER BY started_at DESC NULLS LAST LIMIT 1"""), {"r": input.region_id}).scalar()
        if not sid:
            return DireccionAROutput(ok=False, error=f"sin survey para '{input.region_id}'")
        b = conn.execute(text("""
            SELECT ST_YMin(g), ST_XMin(g), ST_YMax(g), ST_XMax(g)
            FROM (SELECT ST_Extent(geometry)::geometry g FROM parcelas
                  WHERE survey_id = CAST(:s AS uuid)) x"""), {"s": sid}).first()
        if not b or b[0] is None:
            return DireccionAROutput(ok=False, error="el survey no tiene parcelas con geometría")
        bbox = tuple(float(v) for v in b)

        cond = ("AND (calle IS NULL OR calle = '')" if input.solo_faltantes else "")
        filas = conn.execute(text(f"""
            SELECT parcela_id::text, ST_AsGeoJSON(ST_Centroid(geometry)) g,
                   {"ST_AsGeoJSON(geometry)" if input.usar_poligono else "NULL"} AS gp
            FROM parcelas
            WHERE survey_id = CAST(:s AS uuid) AND geometry IS NOT NULL
              AND COALESCE(direccion_source, '')
                  NOT IN {DIRECCION_FUENTES_PROTEGIDAS} {cond}"""),
            {"s": sid}).fetchall()

    if not filas:
        return DireccionAROutput(ok=True, parcelas=0)
    logger.info(f"direccion_ar: {len(filas)} parcelas a resolver en '{input.region_id}'")

    # El bbox de las calles va ampliado: hace falta el eje completo para interpolar (ver input).
    dlat = input.margen_km / 111.0
    dlon = input.margen_km / (111.0 * max(math.cos(math.radians((bbox[0] + bbox[2]) / 2)), 0.3))
    ancho = (bbox[0] - dlat, bbox[1] - dlon, bbox[2] + dlat, bbox[3] + dlon)

    partidos = _partidos(bbox)
    logger.info(f"direccion_ar: la zona toca {partidos}")
    oficial = _callejero(partidos)
    vias = _vias_osm(ancho)
    logger.info(f"direccion_ar: {len(vias)} vías de OSM, {len(oficial)} calles oficiales")

    # Cuadras oficiales: el rango de altura de cada cuadra con su geometría. Es lo que hace
    # que la interpolación valga algo — ver `georef_cuadras`. Best-effort: si el bulk no está
    # y no se puede bajar, se sigue con el rango de la calle entera como hasta ahora.
    cuadras_raw: dict = {}
    if input.usar_cuadras:
        try:
            dep_ids = [d for d in (georef_cuadras.departamento_id(p) for p in partidos) if d]
            cuadras_raw = georef_cuadras.cargar(dep_ids)
            logger.info(f"direccion_ar: {sum(len(v) for v in cuadras_raw.values())} cuadras "
                        f"oficiales en {len(cuadras_raw)} calles ({dep_ids})")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"direccion_ar: sin cuadras oficiales ({exc}); se usa el rango "
                           "de la calle entera")

    proj = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:32721", always_xy=True).transform
    cuadras_m = _preparar_cuadras(cuadras_raw, proj) if cuadras_raw else {}
    # Un eje cosido por nombre (para interpolar la altura) y el índice de todos los tramos
    # (para saber qué calle le toca a cada parcela).
    tramos, nombres = [], []
    por_nombre: dict[str, list] = {}
    for nm, ln in vias:
        m = transform(proj, ln)
        tramos.append(m)
        nombres.append(nm)
        por_nombre.setdefault(nm, []).append(m)
    ejes = {}
    for nm, ls in por_nombre.items():
        try:
            ejes[nm] = _stitch(ls, max_gap=500.0, overlap=30.0) if len(ls) > 1 else ls[0]
        except Exception:  # noqa: BLE001
            ejes[nm] = max(ls, key=lambda x: x.length)
    arbol = STRtree(tramos)

    res, con_calle, con_num, lejos, n_cuadra = [], 0, 0, 0, 0
    for pid, gj, gpj in filas:
        pt = transform(proj, shape(__import__("json").loads(gj)))
        # El lote se BUSCA por su polígono (su frente toca la calle aunque su centro esté
        # lejos) pero se PROYECTA por su centroide, que es lo que lo representa sobre el eje.
        forma = pt
        if gpj:
            try:
                forma = transform(proj, shape(__import__("json").loads(gpj)))
            except Exception:  # noqa: BLE001
                forma = pt
        i = arbol.nearest(forma)
        d = tramos[i].distance(forma)
        if d > input.max_dist_m:
            lejos += 1
            continue
        nm = nombres[i]
        con_calle += 1
        numero, conf, metodo = None, None, _METODO
        nm_norm = normalizar_calle(nm)
        # 1º la CUADRA (rango de ~100 números sobre ~100 m). 2º, si esa calle no tiene
        # cuadras publicadas, el rango de la calle entera, que es lo que había antes.
        cu = cuadras_m.get(nm_norm)
        if cu:
            numero, conf = _numero_por_cuadra(cu, pt, input.max_dist_m)
            if numero is not None:
                metodo = _METODO_CUADRA
                con_num += 1
                n_cuadra += 1
        rango = _buscar_rango(nm_norm, oficial)
        eje = ejes.get(nm)
        if numero is None and rango and eje is not None and eje.length > 0:
            _, ini, fin, izq_par = rango
            frac = min(max(eje.project(pt) / eje.length, 0.0), 1.0)
            n = ini + frac * (fin - ini)
            # La vereda decide la paridad; cuál es cuál puede salir invertido (ver módulo).
            quiere_par = (izq_par if _lado(eje, pt) > 0 else not izq_par)
            n = int(round(n))
            if (n % 2 == 0) != quiere_par:
                n += 1
            numero = max(n, 1)
            con_num += 1
            # Confianza: la distancia a la calle es lo que más pesa (una parcela pegada al eje
            # está bien asignada), y el rango largo diluye la interpolación.
            conf = round(max(0.15, min(0.6, (1 - d / input.max_dist_m) * 0.6
                                       * (1 if (fin - ini) < 3000 else 0.7))), 2)
        res.append((pid, nm, numero, conf, metodo))

    out = DireccionAROutput(
        ok=True, parcelas=len(filas), con_calle=con_calle, con_numero=con_num,
        sin_calle_cerca=lejos, calles_osm=len(por_nombre), calles_oficiales=len(oficial),
        con_numero_cuadra=n_cuadra, partidos=partidos)
    if input.dry_run:
        logger.info(f"direccion_ar (dry-run): {out.model_dump()}")
        return out

    escritas = 0
    with engine.begin() as conn:
        for j in range(0, len(res), input.batch):
            lote = res[j:j + input.batch]
            # ⚠ `numero_estimado` es **varchar** (lo comparte con el número del catastro, que
            # trae cosas como "1503 BIS"), así que el número va como texto: mandando un int el
            # COALESCE se queda sin tipo común y la escritura entera falla.
            conn.execute(text(f"""
                UPDATE parcelas SET
                    calle = :calle,
                    direccion_source = :fuente,
                    direccion_confidence = 0.5,
                    -- ⚠ `numero_estimado` NO es de este agente: `NumeroEstimator` estima en
                    -- la misma columna con los métodos `eje_*` (y en Brasil es el bueno, que
                    -- interpola entre los números REALES del catastro), y `ARBACallesFetcher`
                    -- escribe ahí las alturas OFICIALES del callejero de ARBA (`arba_calles`),
                    -- que valen más que cualquier interpolación nuestra. Sin esta guarda gana
                    -- el último que corre y cada agente deshace al otro en silencio, que es
                    -- justo el modo de falla que costó 75.656 estimaciones en Merlo. Acá se
                    -- respeta lo suyo; el reset de aquel agente ya respeta lo de éste.
                    numero_estimado = CASE
                        WHEN COALESCE(numero_estimado_metodo, '') LIKE 'eje\\_%' ESCAPE '\\'
                          OR COALESCE(numero_estimado_metodo, '') = 'arba_calles'
                            THEN numero_estimado
                        ELSE COALESCE(:num, numero_estimado) END,
                    numero_estimado_metodo = CASE
                        WHEN COALESCE(numero_estimado_metodo, '') LIKE 'eje\\_%' ESCAPE '\\'
                          OR COALESCE(numero_estimado_metodo, '') = 'arba_calles'
                            THEN numero_estimado_metodo
                        WHEN :num IS NULL THEN numero_estimado_metodo
                        ELSE :metodo END,
                    numero_estimado_confianza = CASE
                        WHEN COALESCE(numero_estimado_metodo, '') LIKE 'eje\\_%' ESCAPE '\\'
                          OR COALESCE(numero_estimado_metodo, '') = 'arba_calles'
                            THEN numero_estimado_confianza
                        ELSE COALESCE(:conf, numero_estimado_confianza) END
                WHERE parcela_id = CAST(:pid AS uuid)
                  AND COALESCE(direccion_source, '')
                      NOT IN {DIRECCION_FUENTES_PROTEGIDAS}
            """), [{"pid": p, "calle": c, "num": (str(n) if n is not None else None), "conf": cf,
                    "fuente": _FUENTE, "metodo": mt} for p, c, n, cf, mt in lote])
            escritas += len(lote)
            logger.info(f"direccion_ar: {escritas}/{len(res)} escritas")
    out.escritas = escritas
    return out
