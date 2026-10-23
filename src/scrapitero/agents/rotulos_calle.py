"""Capa CALLES de los planos: el nombre de cada calle sobre su eje, sin pisarse con nada.

Pedido de Jaz (22-oct-2026): nombres bien ubicados, correctos y sin superponerse. Las reglas:

* **Sobre el eje real** de la calle, el de OpenStreetMap (`ejes_calle`, fuente
  `OSM_<region_id>`, que carga `scripts/cargar_calles_osm.py`). Sin ejes no se dibuja nada:
  deducir la calzada desde las manzanas ya puso nombres en la calle equivocada
  (ver `dxf_entrega`).
* **Paralelo a la calle** y siempre legible: el ángulo se normaliza para que el texto no
  quede cabeza abajo.
* **El rectángulo del texto, medido con la fuente real** (Arial; se mide con Liberation
  Sans, que tiene las mismas métricas), no puede tocar:
    - ningún lote (el nombre va en la calzada, no encima de las parcelas),
    - el eje de otra calle (no se escribe encima de un cruce),
    - otro nombre ya puesto.
  Si el texto no entra, se prueba más chico y después abreviado (AV., BV., DR.…). Si
  tampoco, esa cuadra queda sin rótulo: se prefiere una cuadra sin nombre a uno encimado.
* **Un nombre cada `_ESPACIADO_M`** a lo largo de la calle, empezando por las avenidas.
* **Una colectora pegada a la avenida no lleva nombre** en el tramo en que corre a su lado
  (paralela, a menos de `_COLECTORA_M` y sin lotes de por medio): se ve una sola calle y se
  rotula sólo la avenida. Donde se separa, vuelve a rotularse (Jaz, 22-oct-2026, por la
  colectora Conscripto Zurbriggen de la Av. Zobboli en Rafaela).
* Nombres de OSM con errores de tipeo comprobados contra el catastro: `_CORRECCIONES`.
"""

from __future__ import annotations

import math
import re

from ezdxf.enums import TextEntityAlignment
from ezdxf.fonts import fonts
from loguru import logger
from shapely import wkb
from shapely.affinity import rotate
from shapely.geometry import LineString, Point, box
from shapely.ops import linemerge, split, transform, unary_union
from shapely.strtree import STRtree
from sqlalchemy import text

CAPA = "CALLES"
COLOR = 4                       # cian
ESTILO = "CALLES"
_FUENTE_DXF = "arial.ttf"
_FUENTE_MEDIDA = "LiberationSans-Regular.ttf"   # métricas idénticas a Arial

_ALTURAS = (4.0, 3.5, 3.0, 2.5)  # m; la primera que entre
_MARGEN = 0.35                  # aire alrededor del texto, en alturas
_ESPACIADO_M = 160.0            # distancia mínima entre dos rótulos de la misma calle
_PASO_M = 3.0                   # paso al buscar posición a lo largo de la cuadra
_CORRIMIENTOS = (0.0, 1.5, -1.5, 3.0, -3.0)  # m perpendiculares al eje, para centrar en la calzada
_RECTITUD = 0.3                 # desvío máximo del eje bajo el texto, en alturas
_CUADRA_MIN_M = 12.0

# Errores de tipeo de OSM, verificados (Rafaela, 22-oct-2026).
_CORRECCIONES = {
    "Artuto Illia": "Arturo Illia",          # el catastro dice A ILLIA / ARTURO ILLIA
    "Sigmaringendor": "Sigmaringendorf",     # ciudad hermana de Rafaela
}

# Abreviaturas que se usan SÓLO si el nombre completo no entra.
_ABREV = [
    (r"\bAVENIDA\b", "AV."), (r"\bBULEVAR\b|\bBOULEVARD\b", "BV."), (r"\bPASAJE\b", "PJE."),
    (r"\bDOCTOR(A)?\b", "DR."), (r"\bINGENIERO\b", "ING."), (r"\bPRESB[IÍ]TERO\b", "PBRO."),
    (r"\bMONSE[NÑ]OR\b", "MONS."), (r"\bGENERAL\b", "GRAL."), (r"\bPRESIDENTE\b", "PTE."),
    (r"\bINTENDENTE\b", "INT."), (r"\bHERMANA\b", "HNA."), (r"\bSARGENTO\b", "SGTO."),
    (r"\bSOLDADO\b", "SDO."), (r"\bCONTADOR P[UÚ]BLICO NACIONAL\b", "C.P.N."),
    (r"\bCOMANDANTE\b", "CTE."), (r"\bTENIENTE\b", "TTE."), (r"\bCORONEL\b", "CNEL."),
]
_PRIORIDAD = re.compile(r"\b(AVENIDA|BULEVAR|BOULEVARD|RUTA)\b")
_COLECTORA_M = 50.0             # distancia máxima colectora–avenida (doble mano: 20-35 m)
_COLECTORA_ANG = 20.0           # grados de diferencia para considerarlas paralelas
_COLECTORA_FRAC = 0.7           # parte de la cuadra que tiene que ir pegada a la avenida


def _principal(nombre: str) -> bool:
    n = _CORRECCIONES.get(nombre, nombre).upper()
    return bool(_PRIORIDAD.search(n)) and "COLECTORA" not in n


def _rumbo(linea: LineString) -> float:
    (x0, y0), (x1, y1) = linea.coords[0], linea.coords[-1]
    return math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180


def _es_colectora(cuadra: LineString, principales: list, lotes, lotes_g) -> bool:
    """¿La cuadra corre pegada y paralela a una avenida, sin lotes entre las dos?"""
    for eje in principales:
        if eje.distance(cuadra) > _COLECTORA_M:
            continue
        pegado = cuadra.intersection(eje.buffer(_COLECTORA_M)).length
        if pegado < _COLECTORA_FRAC * cuadra.length:
            continue
        mid = cuadra.interpolate(0.5, normalized=True)
        d = eje.project(mid)
        tramo = LineString([eje.interpolate(max(d - 10, 0)),
                            eje.interpolate(min(d + 10, eje.length))])
        # Rumbo en el medio de la cuadra, no de punta a punta: una colectora que empalma
        # con una curva parecía torcida medida entre sus extremos.
        dm = cuadra.length / 2
        propio = LineString([cuadra.interpolate(max(dm - 10, 0)),
                             cuadra.interpolate(min(dm + 10, cuadra.length))])
        dif = abs(_rumbo(tramo) - _rumbo(propio)) % 180
        if min(dif, 180 - dif) > _COLECTORA_ANG:
            continue
        puente = LineString([mid, eje.interpolate(d)])
        if lotes is not None and any(lotes_g[i].intersects(puente)
                                     for i in lotes.query(puente)):
            continue
        return True
    return False


def _textos(nombre: str) -> list[str]:
    completo = _CORRECCIONES.get(nombre, nombre).upper()
    corto = completo
    for patron, abrev in _ABREV:
        corto = re.sub(patron, abrev, corto)
    return [completo] if corto == completo else [completo, corto]


def _ejes(conn, region_id: str, tr) -> dict[str, list[LineString]]:
    filas = conn.execute(text(
        "SELECT nombre, ST_AsBinary(geometry) FROM ejes_calle WHERE fuente = :f"),
        {"f": f"OSM_{region_id}"}).fetchall()
    por: dict[str, list] = {}
    for nombre, g in filas:
        por.setdefault(nombre, []).append(transform(tr.transform, wkb.loads(bytes(g))))
    return por


def _cuadras(lineas: list, otros: list) -> list[LineString]:
    """La calle cortada en cada cruce con otra calle: cada pieza es una cuadra."""
    union = unary_union(lineas)
    if union.geom_type == "MultiLineString":
        union = linemerge(union)
    piezas = [p for p in getattr(union, "geoms", [union]) if isinstance(p, LineString)]
    cortes = unary_union(otros) if otros else None
    out = []
    for p in piezas:
        if cortes is not None and p.intersects(cortes):
            try:
                partes = split(p, cortes).geoms
            except Exception:
                partes = [p]
        else:
            partes = [p]
        out += [q for q in partes if isinstance(q, LineString) and q.length >= _CUADRA_MIN_M]
    return out


class _Rotulador:
    def __init__(self, lotes: list, ejes: dict[str, list], zona):
        self.lotes = STRtree(lotes) if lotes else None
        self._lotes = lotes
        self.ejes = ejes
        todos = [(n, l) for n, ls in ejes.items() for l in ls]
        self._ejes_n = [n for n, _ in todos]
        self._ejes_g = [l for _, l in todos]
        self.arbol_ejes = STRtree(self._ejes_g)
        self.zona = zona
        self.puestos: list = []          # polígonos ya ocupados por rótulos
        self._grilla: dict = {}          # índice simple: celda de 50 m → índices en puestos
        self._fuentes: dict = {}

    def _ancho(self, txt: str, h: float) -> float:
        if h not in self._fuentes:
            self._fuentes[h] = fonts.make_font(_FUENTE_MEDIDA, h)
        return self._fuentes[h].text_width(txt) * 1.03

    def _celdas(self, g):
        x0, y0, x1, y1 = g.bounds
        for i in range(int(x0 // 50), int(x1 // 50) + 1):
            for j in range(int(y0 // 50), int(y1 // 50) + 1):
                yield (i, j)

    def _libre(self, caja, nombre: str, sobre_lotes: bool) -> bool:
        for c in self._celdas(caja):
            for k in self._grilla.get(c, ()):
                if self.puestos[k].intersects(caja):
                    return False
        if not sobre_lotes and self.lotes is not None:
            for i in self.lotes.query(caja):
                if self._lotes[i].intersects(caja):
                    return False
        for i in self.arbol_ejes.query(caja):
            if self._ejes_n[i] != nombre and self._ejes_g[i].intersects(caja):
                return False
        return True

    def _ocupar(self, caja):
        k = len(self.puestos)
        self.puestos.append(caja)
        for c in self._celdas(caja):
            self._grilla.setdefault(c, []).append(k)

    def _en(self, cuadra: LineString, d: float, txt: str, h: float, nombre: str,
            sobre_lotes: bool):
        """Rótulo centrado a `d` m del inicio de la cuadra, o None si no entra limpio."""
        largo = self._ancho(txt, h)
        if largo > cuadra.length:
            return None
        a, b = d - largo / 2, d + largo / 2
        if a < 0 or b > cuadra.length:
            return None
        p0, p1 = cuadra.interpolate(a), cuadra.interpolate(b)
        cuerda = LineString([p0, p1])
        # El eje bajo el texto tiene que ser casi recto: en una curva el texto se sale.
        for t in range(1, 6):
            if cuadra.interpolate(a + (b - a) * t / 6).distance(cuerda) > _RECTITUD * h:
                return None
        ang = math.degrees(math.atan2(p1.y - p0.y, p1.x - p0.x))
        if ang > 90:
            ang -= 180
        elif ang <= -90:
            ang += 180
        c = cuadra.interpolate(d)
        nx, ny = -math.sin(math.radians(ang)), math.cos(math.radians(ang))
        m = _MARGEN * h
        for off in _CORRIMIENTOS:
            cx, cy = c.x + nx * off, c.y + ny * off
            if self.zona is not None and not self.zona.contains(Point(cx, cy)):
                return None
            caja = rotate(box(cx - largo / 2 - m, cy - h / 2 - m, cx + largo / 2 + m,
                              cy + h / 2 + m), ang, origin=(cx, cy))
            if self._libre(caja, nombre, sobre_lotes):
                return (txt, h, cx, cy, ang, caja)
        return None

    def _mejor_en_cuadra(self, cuadra, nombre, textos, sobre_lotes):
        mitad = cuadra.length / 2
        pasos = [0.0]
        k = 1
        while k * _PASO_M < mitad:
            pasos += [k * _PASO_M, -k * _PASO_M]
            k += 1
        for h in _ALTURAS:
            for txt in textos:
                for dp in pasos:
                    r = self._en(cuadra, mitad + dp, txt, h, nombre, sobre_lotes)
                    if r:
                        return r
        return None

    def calle(self, nombre: str, cuadras: list, sobre_lotes: bool = False) -> list:
        textos = _textos(nombre)
        puestos, centros = [], []
        # De la cuadra más larga a la más corta: las largas dan el rótulo más cómodo.
        for cuadra in sorted(cuadras, key=lambda q: -q.length):
            mid = cuadra.interpolate(0.5, normalized=True)
            if any(mid.distance(c) < _ESPACIADO_M for c in centros):
                continue
            r = self._mejor_en_cuadra(cuadra, nombre, textos, sobre_lotes)
            if r:
                self._ocupar(r[5])
                puestos.append(r)
                centros.append(Point(r[2], r[3]))
        return puestos


def dibujar(msp, conn, survey_id: str, region_id: str, tr, zona_4326=None) -> dict:
    """Dibuja la capa CALLES. Devuelve un resumen para el log y la nota del plano."""
    ejes = _ejes(conn, region_id, tr)
    if not ejes:
        logger.warning(f"Rótulos de calle {survey_id}: no hay ejes OSM_{region_id} en "
                       f"ejes_calle; correr scripts/cargar_calles_osm.py. Capa CALLES vacía.")
        return {"rotulos": 0, "calles": 0, "sin_rotulo": [], "sin_ejes": True}
    lotes = [transform(tr.transform, wkb.loads(bytes(g))) for (g,) in conn.execute(text(
        "SELECT ST_AsBinary(geometry) FROM parcelas WHERE survey_id = CAST(:s AS uuid) "
        "AND geometry IS NOT NULL"), {"s": survey_id})]
    if zona_4326 is not None:
        zona = transform(tr.transform, zona_4326).buffer(30)
    elif lotes:
        zona = unary_union(lotes).convex_hull.buffer(30)
    else:
        zona = None

    rot = _Rotulador(lotes, ejes, zona)
    nombres = sorted(ejes, key=lambda n: (not _principal(n), -sum(l.length for l in ejes[n])))
    principales = {n: [l for l in ejes[n]] for n in nombres if _principal(n)}
    cuadras = {}
    colectoras: dict[str, int] = {}
    for n in nombres:
        otros = [l for i in rot.arbol_ejes.query(unary_union(ejes[n]).buffer(1))
                 for l in [rot._ejes_g[i]] if rot._ejes_n[i] != n]
        cuadras[n] = _cuadras(ejes[n], otros)
        if not _principal(n):
            cerca = [l for m, ls in principales.items() for l in ls
                     if l.distance(unary_union(ejes[n])) <= _COLECTORA_M]
            if cerca:
                antes = len(cuadras[n])
                cuadras[n] = [q for q in cuadras[n]
                              if not _es_colectora(q, cerca, rot.lotes, rot._lotes)]
                if len(cuadras[n]) < antes:
                    colectoras[n] = antes - len(cuadras[n])
    if colectoras:
        logger.info(f"Rótulos de calle {survey_id}: cuadras de colectora pegadas a una "
                    f"avenida, sin rótulo propio: {colectoras}")

    puestos: dict[str, list] = {}
    for n in nombres:
        puestos[n] = rot.calle(n, cuadras[n])
    # Segunda pasada sólo para las calles que quedaron sin ningún nombre (p.ej. calles que
    # el catastro carga como lote): se permite apoyar sobre lotes, nunca sobre otro rótulo.
    for n in nombres:
        if not puestos[n] and any(zona is None or zona.intersects(q) for q in cuadras[n]):
            puestos[n] = rot.calle(n, cuadras[n], sobre_lotes=True)

    doc = msp.doc
    if CAPA not in doc.layers:
        doc.layers.add(CAPA, color=COLOR)
    if ESTILO not in doc.styles:
        doc.styles.add(ESTILO, font=_FUENTE_DXF)
    total = 0
    for n, lista in puestos.items():
        for txt, h, cx, cy, ang, _ in lista:
            msp.add_text(txt, dxfattribs={"layer": CAPA, "height": h, "rotation": ang,
                                          "style": ESTILO}).set_placement(
                (cx, cy), align=TextEntityAlignment.MIDDLE_CENTER)
            total += 1
    en_zona = [n for n in nombres
               if any(zona is None or zona.intersects(q) for q in cuadras[n])]
    sin = sorted(n for n in en_zona if not puestos[n])
    logger.info(f"Rótulos de calle {survey_id}: {total} rótulos en {len(en_zona) - len(sin)} "
                f"de {len(en_zona)} calles; sin rótulo: {sin[:20]}")
    return {"rotulos": total, "calles": len(en_zona), "sin_rotulo": sin, "sin_ejes": False,
            "cajas": [r[5] for lista in puestos.values() for r in lista]}
