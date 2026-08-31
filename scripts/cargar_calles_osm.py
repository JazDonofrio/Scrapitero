"""Ejes de calle con nombre, desde OpenStreetMap.

El rótulo de calle del plano necesita dos cosas: **dónde está el eje de la calzada** y **cómo
se llama esa calle**. No teníamos las dos juntas — `logradouros` (IBGE) trae los ejes pero
sólo 23 nombres sobre 3.522 tramos, y en Argentina no hay ni ejes — así que el nombre se
ubicaba deduciendo la calzada desde el frente de la manzana, y terminaba cayendo adentro de
una manzana, encima de los lotes.

OSM tiene las dos, gratis (ver [[no-usar-apis-pagas]]).

    python scripts/cargar_calles_osm.py <survey_id> [--margen-m 400]

Queda en `ejes_calle` con `fuente = 'OSM_<region_id>'`, que es de donde lo lee `DXFEntrega`.
Se borra y se recarga por `fuente` sin tocar nada más.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request

from loguru import logger
from shapely.geometry import LineString
from sqlalchemy import text

from scrapitero.db.engine import get_engine

_OVERPASS = "https://overpass-api.de/api/interpreter"
# Overpass rechaza con 406 el User-Agent por defecto de urllib: hay que identificarse.
_UA = "scrapitero/1.0 (ejes de calle para rotulado de planos; contacto via repo)"

# Las mismas vías que delimitan manzana en `cargar_manzanas_osm.py`, para que el eje y la
# manzana hablen de la misma calle. Sin `service`/`footway`: los pasillos internos no llevan
# rótulo en el plano del cliente.
_VIAS = ("motorway|trunk|primary|secondary|tertiary|unclassified|residential|"
         "living_street|motorway_link|trunk_link|primary_link|secondary_link")

# Un tramo más corto que esto no alcanza ni para apoyar el texto y suele ser un empalme de
# esquina, que además rota mal.
_LARGO_MIN_M = 15.0


def _bbox_del_survey(conn, survey_id: str, margen_m: float):
    fila = conn.execute(text("""
        SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e)
        FROM (SELECT ST_Extent(geometry) e FROM parcelas
              WHERE survey_id = :s AND geometry IS NOT NULL) t
    """), {"s": survey_id}).fetchone()
    if not fila or fila[0] is None:
        raise SystemExit(f"El survey {survey_id} no tiene parcelas con geometría.")
    d = margen_m / 111_320.0
    return fila[0] - d, fila[1] - d, fila[2] + d, fila[3] + d


def _bajar(bbox) -> list[tuple[str, str, LineString]]:
    minx, miny, maxx, maxy = bbox
    consulta = (f'[out:json][timeout:120];'
                f'way({miny},{minx},{maxy},{maxx})'
                f'["highway"~"^({_VIAS})$"]["name"];'
                f'out geom;')
    datos = urllib.parse.urlencode({"data": consulta}).encode()
    pedido = urllib.request.Request(_OVERPASS, data=datos, headers={"User-Agent": _UA})
    with urllib.request.urlopen(pedido, timeout=180) as r:
        resp = json.loads(r.read().decode())
    salida = []
    for el in resp.get("elements", []):
        geo = el.get("geometry") or []
        nombre = (el.get("tags") or {}).get("name")
        if len(geo) >= 2 and nombre:
            salida.append((nombre, (el.get("tags") or {}).get("highway"),
                           LineString([(p["lon"], p["lat"]) for p in geo])))
    logger.info(f"OSM: {len(salida)} tramos de vía CON nombre en el entorno.")
    return salida


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("survey_id")
    ap.add_argument("--margen-m", type=float, default=400.0)
    args = ap.parse_args()

    engine = get_engine()
    with engine.begin() as conn:
        region = conn.execute(text("SELECT region_id FROM surveys WHERE survey_id = :s"),
                              {"s": args.survey_id}).scalar()
        if not region:
            raise SystemExit(f"Survey no encontrado: {args.survey_id}")
        fuente = f"OSM_{region}"

        tramos = _bajar(_bbox_del_survey(conn, args.survey_id, args.margen_m))
        if not tramos:
            logger.warning("OSM no devolvió ninguna calle con nombre.")
            return 1

        borradas = conn.execute(text("DELETE FROM ejes_calle WHERE fuente = :f"),
                                {"f": fuente}).rowcount
        puestos = 0
        for nombre, tipo, linea in tramos:
            # El largo se mide en metros, no en grados: lo hace PostGIS sobre el geography.
            largo = conn.execute(text("SELECT ST_Length(ST_GeogFromText(:w))"),
                                 {"w": linea.wkt}).scalar()
            if (largo or 0) < _LARGO_MIN_M:
                continue
            conn.execute(text("""
                INSERT INTO ejes_calle (fuente, region_id, nombre, tipo_via, geometry)
                VALUES (:f, :r, :n, :t, ST_SetSRID(ST_GeomFromText(:w), 4326))"""),
                {"f": fuente, "r": region, "n": nombre, "t": tipo, "w": linea.wkt})
            puestos += 1
    distintas = len({n for n, _t, _g in tramos})
    logger.info(f"{puestos} tramos cargados en '{fuente}' ({distintas} calles distintas; "
                f"borrados {borradas} de una corrida anterior).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
