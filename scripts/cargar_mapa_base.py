#!/usr/bin/env python3
"""Carga el mapa base del cliente (capas QUADRA y MEIOFIO) a `mapa_base_cliente`.

El cliente entrega el MUB en DWG. Como ninguna librería libre lee DWG, primero hay que
convertirlo a DXF con LibreDWG:

    dwg2dxf -o mapa.dxf "plano MUB_MT_VARZEA_GRANDE (con coordenadas).dwg"

**La conversión es fiable para esto**: verificado sobre el MUB de Várzea Grande, las
19.831 polilíneas del DWG salen 19.831 en el DXF, y el reparto por capa calza exacto
(9.355 QUADRA + 10.476 MEIOFIO). Lo que `dwg2dxf` sí rompe son los INSERT y los flags de
los atributos — irrelevante acá, el mapa base es sólo geometría.

El DXF grande puede venir con tags corruptos sueltos (el del MUB los tiene); por eso se
lee con `ezdxf.recover`, que los repara en vez de abortar.

Uso:
    PYTHONPATH=src python scripts/cargar_mapa_base.py mapa.dxf MUB_MT_VARZEA_GRANDE [--epsg 31981]
"""

from __future__ import annotations

import argparse

import ezdxf
import ezdxf.recover
from pyproj import Transformer
from shapely.geometry import LineString
from sqlalchemy import text

from scrapitero.db.engine import get_engine

CAPAS = ("QUADRA", "MEIOFIO")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dxf")
    ap.add_argument("fuente", help="etiqueta de origen, p.ej. MUB_MT_VARZEA_GRANDE")
    ap.add_argument("--epsg", type=int, default=31981,
                    help="CRS del DXF. 31981 = SIRGAS 2000 / UTM 21S (Várzea Grande)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        doc = ezdxf.readfile(args.dxf)
    except Exception as e:
        print(f"lectura estricta falló ({e}); reintento con recover")
        doc, auditor = ezdxf.recover.readfile(args.dxf)
        print(f"  recuperado: {len(auditor.errors)} errores, {len(auditor.fixes)} arreglos")

    tr = Transformer.from_crs(args.epsg, 4326, always_xy=True)
    msp = doc.modelspace()

    filas, descartadas = [], 0
    for p in msp.query("LWPOLYLINE"):
        if p.dxf.layer not in CAPAS:
            continue
        pts = [(x, y) for x, y, *_ in p.get_points()]
        # Una polilínea de 1 vértice no es una línea; PostGIS la rechaza y aborta el lote.
        if len(pts) < 2:
            descartadas += 1
            continue
        geom = LineString([tr.transform(x, y) for x, y in pts])
        filas.append({
            "fuente": args.fuente,
            "capa": p.dxf.layer,
            "cerrada": bool(p.closed),
            "wkb": geom.wkb_hex,
        })

    print(f"leídas {len(filas)} polilíneas ({descartadas} descartadas por < 2 vértices)")
    por_capa: dict[str, int] = {}
    for f in filas:
        por_capa[f["capa"]] = por_capa.get(f["capa"], 0) + 1
    for capa, n in sorted(por_capa.items()):
        print(f"   {capa:<10} {n}")

    if args.dry_run:
        print("dry-run: no se escribió nada")
        return 0
    if not filas:
        print("nada para cargar")
        return 1

    engine = get_engine()
    with engine.begin() as conn:
        # Reemplazo por fuente: recargar un MUB actualizado no debe duplicar el anterior.
        borradas = conn.execute(
            text("DELETE FROM mapa_base_cliente WHERE fuente = :f"),
            {"f": args.fuente}).rowcount
        if borradas:
            print(f"reemplazo: borradas {borradas} filas previas de {args.fuente}")
        for i in range(0, len(filas), 2000):
            conn.execute(text("""
                INSERT INTO mapa_base_cliente (fuente, capa, cerrada, geometry)
                VALUES (:fuente, :capa, :cerrada, ST_GeomFromWKB(decode(:wkb, 'hex'), 4326))
            """), filas[i:i + 2000])
            print(f"   insertadas {min(i + 2000, len(filas))}/{len(filas)}", end="\r")

    with engine.connect() as conn:
        n, minx, miny, maxx, maxy = conn.execute(text("""
            SELECT COUNT(*),
                   ST_XMin(ST_Extent(geometry)), ST_YMin(ST_Extent(geometry)),
                   ST_XMax(ST_Extent(geometry)), ST_YMax(ST_Extent(geometry))
            FROM mapa_base_cliente WHERE fuente = :f
        """), {"f": args.fuente}).fetchone()
    print(f"\nOK  {n} geometrías cargadas como '{args.fuente}'")
    print(f"    extensión: lat {miny:.4f}..{maxy:.4f}  lon {minx:.4f}..{maxx:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
