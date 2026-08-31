"""Manzanas y lotes desde el DWG de entrega del propio cliente.

El cliente nos devolvió su plano terminado de Várzea Grande
(`entrega_Varzea_Grande_Ac_2026-07-25.dwg`): 399 manzanas cerradas en la capa `QUADRA` y
552 **lotes** cerrados en `DIV`. Es la mejor base posible, mejor que el MUB municipal y
que las caras de OpenStreetMap:

  · Son las manzanas y los lotes que él mismo dibuja, así que el plano sale con su forma.
  · Cubren lo relevado: 562 de nuestras 567 parcelas caen dentro de una `QUADRA` y 542
    dentro de un lote `DIV`.
  · `DIV` no son líneas divisorias sueltas —como en el plano de ejemplo viejo— sino
    polígonos de lote, uno por lote, mediana 422 m². Eso ahorra tener que inventar la
    subdivisión de la manzana: ya viene hecha y es la real.

    python scripts/cargar_base_dwg.py <survey_id> <archivo.dwg> [--epsg 32721]

Queda en `mapa_base_cliente` con `fuente = 'AC_<region_id>'` (AC = "as-built" del
cliente), que es de donde lo lee `DXFEntrega` con prioridad sobre `OSM_*` y sobre el MUB.
Es otra fuente más: no pisa la del cliente ni la de OSM, y se puede borrar y rehacer.

LibreDWG está compilado a mano en esta máquina (ver [[libredwg-compilado-a-mano]]); su
`dwg2dxf` produce un DXF que ezdxf no lee, así que se va por `dwgread -O JSON`, que sí es
fiel con la geometría.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

from loguru import logger
from pyproj import Transformer
from shapely.geometry import LineString, Point as ShapelyPoint, Polygon
from shapely.ops import unary_union
from sqlalchemy import text

from scrapitero.db.engine import get_engine

_CAPAS = ("QUADRA", "DIV")

# Capas donde el cliente pone la ficha del inmueble. El rótulo visible es
# `N_C1_3_TP_QT` en el SDU y `NUMERO` en el MDU.
_CAPAS_FICHA = ("SDU_RES", "SDU_COM", "SDU_ESP", "SDU_VAZ",
                "MDU_RES", "MDU_COM", "MDU_ESP")
_TAGS_ROTULO = ("N_C1_3_TP_QT", "NUMERO")

# Un polígono más chico que esto es un resto de digitalización, no una manzana ni un lote.
_AREA_MIN_M2 = 20.0
# Y más grande que esto es el marco del plano o un área sin calles internas.
_AREA_MAX_M2 = 500_000.0
# Una cadena abierta más corta que esto es un resto de digitalización, no un tramo de borde.
_LARGO_MIN_M = 2.0
# Lejos del relevamiento no interesa: el DWG trae manzanas de todo el municipio y algunos
# polígonos degenerados pegados al origen de coordenadas.
_MARGEN_M = 1_500.0


def _a_json(dwg: Path) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as t:
        salida = Path(t.name)
    r = subprocess.run(["dwgread", "-O", "JSON", "-o", str(salida), str(dwg)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not salida.exists():
        raise SystemExit(f"dwgread falló sobre {dwg}: {r.stderr[-400:]}")
    # El DWG trae textos en latin-1 (nombres de calle con acento) dentro de un JSON que
    # dwgread declara UTF-8: sin `errors='replace'` no se puede ni parsear.
    datos = json.loads(salida.read_text(encoding="utf-8", errors="replace"))
    salida.unlink(missing_ok=True)
    return datos


def _capas(datos: dict) -> dict[int, str]:
    """Handle de cada capa -> nombre. En el JSON la entidad referencia la capa por handle."""
    tabla = {}
    for o in datos.get("OBJECTS", []):
        if o.get("object") == "LAYER" and isinstance(o.get("handle"), list):
            tabla[o["handle"][-1]] = o.get("name")
    return tabla


def _trazos(datos: dict) -> dict[str, list[tuple[list, bool]]]:
    """Cada polilínea de `QUADRA`/`DIV` con su vértices y **su flag de cerrada**.

    ⚠ **Manda el flag del DWG, no que el primer punto se repita.** Una polilínea cerrada de
    AutoCAD NO repite el vértice inicial, así que mirando los puntos no se sabe. Y al revés:
    cerrar a la fuerza una polilínea ABIERTA inventa un polígono que no existe. En este DWG
    **174 `QUADRA` y 29 `DIV` vienen abiertas** —son cadenas de borde, tramos de manzana que
    él dibuja sueltos— y cerrarlas metía 190 polígonos que cruzaban el dibujo bueno y
    cortaban lotes.

    Las dos clases se guardan: las cerradas son manzanas y lotes de verdad y sirven para
    recortar y asignar; las abiertas **se copian igual**, porque están en su plano y sin
    ellas 37 manzanas se quedan sin contorno. Simplemente no se usan como polígono.
    """
    tabla = _capas(datos)
    salida: dict[str, list[tuple[list, bool]]] = {c: [] for c in _CAPAS}
    descartadas = 0
    for o in datos.get("OBJECTS", []):
        if o.get("entity") != "LWPOLYLINE":
            continue
        capa = tabla.get(o.get("layer", [None])[-1])
        if capa not in _CAPAS:
            continue
        pts = [(p[0], p[1]) for p in (o.get("points") or [])]
        if len(pts) < 2:
            descartadas += 1
            continue
        cerrada = bool(o.get("flag", 0) & 512)
        if cerrada:
            if len(pts) < 3:
                descartadas += 1
                continue
            g = Polygon(pts)
            if not g.is_valid:
                g = g.buffer(0)
            if not isinstance(g, Polygon) or not (_AREA_MIN_M2 <= g.area <= _AREA_MAX_M2):
                descartadas += 1
                continue
        elif LineString(pts).length < _LARGO_MIN_M:
            descartadas += 1
            continue
        salida[capa].append((pts, cerrada))
    if descartadas:
        logger.info(f"{descartadas} polilínea(s) descartadas por tamaño o por degeneradas.")
    return salida


def _fichas(datos: dict) -> list[tuple[str, str, float, float, float]]:
    """Dónde y con qué rotación puso el cliente cada ficha.

    Devuelve `(capa, etiqueta, x, y, rotacion_grados)`. El `INSERT` referencia sus `ATTRIB`
    por handle, y el rótulo visible es el único atributo que no viene con `invisible=1`.
    """
    tabla = _capas(datos)
    ins: dict = {}
    for o in datos.get("OBJECTS", []):
        if o.get("entity") != "INSERT":
            continue
        capa = tabla.get(o.get("layer", [None])[-1])
        if capa in _CAPAS_FICHA and isinstance(o.get("handle"), list):
            ins[o["handle"][-1]] = {
                "capa": capa, "pt": o.get("ins_pt") or [0, 0],
                # dwgread devuelve la rotación en radianes; el DXF la escribe en grados.
                "rot": math.degrees(o.get("rotation") or 0.0), "rotulo": "",
            }
    for o in datos.get("OBJECTS", []):
        if o.get("entity") != "ATTRIB":
            continue
        dueno = ins.get((o.get("ownerhandle") or [None])[-1])
        if dueno is None or o.get("invisible"):
            continue
        if o.get("tag") in _TAGS_ROTULO and not dueno["rotulo"]:
            dueno["rotulo"] = (o.get("text_value") or "").strip()
    return [(v["capa"], v["rotulo"], v["pt"][0], v["pt"][1], v["rot"])
            for v in ins.values() if v["rotulo"]]


def _bbox_utm(conn, survey_id: str, tr: Transformer):
    fila = conn.execute(text("""
        SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e)
        FROM (SELECT ST_Extent(geometry) e FROM parcelas
              WHERE survey_id = :s AND geometry IS NOT NULL) t
    """), {"s": survey_id}).fetchone()
    if not fila or fila[0] is None:
        raise SystemExit(f"El survey {survey_id} no tiene parcelas con geometría.")
    x0, y0 = tr.transform(fila[0], fila[1])
    x1, y1 = tr.transform(fila[2], fila[3])
    m = _MARGEN_M
    return min(x0, x1) - m, min(y0, y1) - m, max(x0, x1) + m, max(y0, y1) + m


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("survey_id")
    ap.add_argument("dwg", type=Path)
    ap.add_argument("--epsg", type=int, default=32721,
                    help="CRS del DWG. Várzea Grande está en UTM 21S (32721).")
    args = ap.parse_args()

    if not args.dwg.exists():
        raise SystemExit(f"No existe: {args.dwg}")
    logger.info(f"Leyendo {args.dwg.name} con dwgread…")
    datos = _a_json(args.dwg)
    pol = _trazos(datos)
    fichas = _fichas(datos)
    logger.info(f"  fichas del cliente con rótulo visible: {len(fichas)}")
    for c in _CAPAS:
        cerradas = sum(1 for _pts, cc in pol[c] if cc)
        logger.info(f"  {c}: {cerradas} cerradas + {len(pol[c]) - cerradas} abiertas")
    if not any(cc for _pts, cc in pol["QUADRA"]):
        raise SystemExit("El DWG no trae ninguna QUADRA cerrada: no sirve como base.")

    hacia_4326 = Transformer.from_crs(args.epsg, 4326, always_xy=True)
    hacia_utm = Transformer.from_crs(4326, args.epsg, always_xy=True)

    engine = get_engine()
    with engine.begin() as conn:
        region = conn.execute(text("SELECT region_id FROM surveys WHERE survey_id = :s"),
                              {"s": args.survey_id}).scalar()
        if not region:
            raise SystemExit(f"Survey no encontrado: {args.survey_id}")
        fuente = f"AC_{region}"

        minx, miny, maxx, maxy = _bbox_utm(conn, args.survey_id, hacia_utm)
        recuadro = Polygon([(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)])

        borradas = conn.execute(text("DELETE FROM mapa_base_cliente WHERE fuente = :f"),
                                {"f": fuente}).rowcount
        puestas = {c: [0, 0] for c in _CAPAS}
        for capa in _CAPAS:
            # Los lotes se solapan entre sí en el DWG sólo por errores de digitalización;
            # no se tocan acá. Lo que sí se descarta es lo que cae lejos del relevamiento.
            for pts, cerrada in pol[capa]:
                trazo = LineString(pts)
                if not trazo.intersects(recuadro) and \
                        not (cerrada and Polygon(pts).intersects(recuadro)):
                    continue
                # La cerrada se guarda como el anillo COMPLETO —repitiendo el primer
                # vértice— porque un LINESTRING no lleva flag; el flag va en `cerrada` y el
                # export vuelve a escribir la LWPOLYLINE con `close=True`.
                coords = list(pts) + ([pts[0]] if cerrada else [])
                anillo = [hacia_4326.transform(x, y) for x, y in coords]
                conn.execute(text("""
                    INSERT INTO mapa_base_cliente (fuente, capa, cerrada, geometry)
                    VALUES (:f, :c, :k, ST_SetSRID(ST_GeomFromText(:w), 4326))"""),
                    {"f": fuente, "c": capa, "k": cerrada,
                     "w": LineString(anillo).wkt})
                puestas[capa][0 if cerrada else 1] += 1
        # `buffer(0)` antes de unir: algunos lotes suyos se auto-intersectan y `unary_union`
        # revienta con "side location conflict". Es sólo para el resumen que se loguea.
        sanos = []
        for pts, cc in pol["DIV"]:
            if not cc:
                continue
            g = Polygon(pts)
            if not g.is_valid:
                g = g.buffer(0)
            if not g.is_empty and g.intersects(recuadro):
                sanos.append(g)
        conn.execute(text("DELETE FROM fichas_cliente WHERE fuente = :f"), {"f": fuente})
        puestas_f = 0
        for capa, rotulo, x, y, rot in fichas:
            if not recuadro.contains(ShapelyPoint(x, y)):
                continue
            lon, lat = hacia_4326.transform(x, y)
            conn.execute(text("""
                INSERT INTO fichas_cliente (fuente, capa_dwg, etiqueta, rotacion, geometry)
                VALUES (:f, :c, :e, :r,
                        ST_SetSRID(ST_MakePoint(:lon, :lat), 4326))"""),
                {"f": fuente, "c": capa, "e": rotulo, "r": rot, "lon": lon, "lat": lat})
            puestas_f += 1

        cubre = unary_union(sanos) if sanos else Polygon()
    logger.info(f"Cargado en '{fuente}': " +
                ", ".join(f"{n[0]} {c} cerradas + {n[1]} abiertas"
                          for c, n in puestas.items()) +
                f", {puestas_f} fichas"
                f" (borradas {borradas} de una corrida anterior). "
                f"Los lotes cubren {cubre.area / 10_000:.1f} ha.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
