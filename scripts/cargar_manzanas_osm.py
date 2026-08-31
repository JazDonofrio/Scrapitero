"""Manzanas desde OpenStreetMap, para las zonas sin mapa base municipal.

Para recortar los lotes contra su manzana hace falta el **polígono cerrado** de la manzana.
El MUB de Várzea sólo lo trae en parte del corredor, y en Argentina no tenemos nada: sin
ese polígono la parcela sale con su forma catastral completa, invadiendo la calle.

Las manzanas son, por definición, las **caras cerradas de la red de calles**. OSM publica
esa red gratis, así que se bajan las vías del entorno del relevamiento, se arma el grafo y
se poligoniza. Ver [[no-usar-apis-pagas]]: Overpass es libre y no cobra por consulta.

    python scripts/cargar_manzanas_osm.py <survey_id> [--margen-m 400]

Las manzanas quedan en `mapa_base_cliente` con `fuente = 'OSM_<region_id>'` y `capa =
'QUADRA'`, que es de donde ya las lee `DXFEntrega`. No se mezclan con el mapa del cliente:
son otra fuente, y se puede borrar y rehacer sin tocar la suya.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request

from loguru import logger
from shapely.geometry import LineString, Polygon
from shapely.ops import polygonize, unary_union
from sqlalchemy import text

from scrapitero.db.engine import get_engine

_OVERPASS = "https://overpass-api.de/api/interpreter"
_UA = "scrapitero/1.0 (relevamiento de manzanas; contacto via repo)"

# Vías que delimitan manzana. Se dejan afuera las de servicio, peatonales y caminos: parten
# la manzana por dentro (pasillos, estacionamientos) y darían "manzanas" que no existen.
_VIAS = ("motorway|trunk|primary|secondary|tertiary|unclassified|residential|"
         "living_street|motorway_link|trunk_link|primary_link|secondary_link")

# Una cara de la red que mida menos que esto es un cantero o una rotonda, no una manzana;
# más que esto es un área sin calles internas (un barrio entero, un campo).
_AREA_MIN_M2 = 400.0
_AREA_MAX_M2 = 400_000.0


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


def _bajar_vias(bbox) -> list[LineString]:
    minx, miny, maxx, maxy = bbox
    consulta = (f'[out:json][timeout:120];'
                f'way({miny},{minx},{maxy},{maxx})["highway"~"^({_VIAS})$"];'
                f'out geom;')
    datos = urllib.parse.urlencode({"data": consulta}).encode()
    # Overpass rechaza con 406 el User-Agent por defecto de urllib: hay que identificarse.
    pedido = urllib.request.Request(_OVERPASS, data=datos,
                                    headers={"User-Agent": _UA})
    with urllib.request.urlopen(pedido, timeout=180) as r:
        resp = json.loads(r.read().decode())
    lineas = []
    for el in resp.get("elements", []):
        geo = el.get("geometry") or []
        if len(geo) >= 2:
            lineas.append(LineString([(p["lon"], p["lat"]) for p in geo]))
    logger.info(f"OSM: {len(lineas)} tramos de vía en el entorno.")
    return lineas


def _manzanas(lineas: list[LineString], conn) -> list[Polygon]:
    if not lineas:
        return []
    caras = list(polygonize(unary_union(lineas)))
    logger.info(f"Caras cerradas por la red de calles: {len(caras)}")
    # El área se mide en metros, no en grados: se pide a PostGIS sobre el geography.
    salida = []
    for cara in caras:
        if cara.is_empty or not cara.is_valid:
            cara = cara.buffer(0)
        if not isinstance(cara, Polygon) or cara.is_empty:
            continue
        m2 = conn.execute(text("SELECT ST_Area(ST_GeogFromText(:w))"),
                          {"w": cara.wkt}).scalar()
        if _AREA_MIN_M2 <= (m2 or 0) <= _AREA_MAX_M2:
            salida.append(cara)
    return salida


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("survey_id")
    ap.add_argument("--margen-m", type=float, default=400.0)
    args = ap.parse_args()

    engine = get_engine()
    with engine.begin() as conn:
        region = conn.execute(text("""
            SELECT r.region_id FROM surveys s JOIN regions r ON s.region_id = r.region_id
            WHERE s.survey_id = :s"""), {"s": args.survey_id}).scalar()
        if not region:
            raise SystemExit(f"Survey no encontrado: {args.survey_id}")
        fuente = f"OSM_{region}"

        bbox = _bbox_del_survey(conn, args.survey_id, args.margen_m)
        manzanas = _manzanas(_bajar_vias(bbox), conn)
        if not manzanas:
            logger.warning("OSM no devolvió ninguna manzana utilizable.")
            return 1

        borradas = conn.execute(text("DELETE FROM mapa_base_cliente WHERE fuente = :f"),
                                {"f": fuente}).rowcount
        for m in manzanas:
            conn.execute(text("""
                INSERT INTO mapa_base_cliente (fuente, capa, cerrada, geometry)
                VALUES (:f, 'QUADRA', TRUE,
                        ST_SetSRID(ST_GeomFromText(:w), 4326))"""),
                {"f": fuente, "w": LineString(m.exterior.coords).wkt})
    logger.info(f"{len(manzanas)} manzanas cargadas en '{fuente}' "
                f"(borradas {borradas} de una corrida anterior).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
