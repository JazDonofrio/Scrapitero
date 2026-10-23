"""Capa CONTEO de los planos: las manzanas del relevamiento numeradas 1, 2, 3…

Es el mismo conteo que la constancia de cobro (`constancia_pdf`), con la misma función, para
que plano y constancia digan siempre el mismo número (Jaz, 21-oct-2026):

* la manzana de cada lote sale de su nomenclatura catastral (`manzana_catastral`);
* una manzana cuenta si más de la mitad de su superficie cae dentro del polígono encargado
  (sin polígono, cuentan todas).

Es la manzana **catastral**, no la cuadra física: el catastro a veces parte una cuadra en
dos o más códigos (en Rafaela, ~450 de 2.236 comparten cuadra con otra), y esas salen
pegadas, cada una con su número. Así el número se puede verificar código por código.

La numeración va por franjas de 100 m de norte a sur y, dentro de cada franja, de oeste a
este. Si la fuente no tiene parser de manzana no se dibuja nada (la constancia ya avisa).
"""

from __future__ import annotations

import json
from typing import Optional

from ezdxf.enums import TextEntityAlignment
from shapely.geometry import shape
from shapely.ops import transform, unary_union
from sqlalchemy import text

from scrapitero.agents.manzana_catastral import manzana_codigo

CAPA = "CONTEO"
COLOR = 1            # rojo
_FRANJA_M = 100      # alto de cada franja de numeración
_CIERRE_M = 0.5      # rendijas entre lotes vecinos que se cierran al unir la manzana


def contadas(por_mz: dict, zona) -> list:
    """{código: [geometrías de sus lotes]} → [(código, unión)] de las que cuentan.

    `zona` en el mismo sistema que las geometrías, o None. La usa también la constancia.
    """
    out = []
    for cod, trozos in por_mz.items():
        u = unary_union(trozos)
        if not u.area:
            continue
        if zona is None or u.intersection(zona).area / u.area >= 0.5:
            out.append((cod, u))
    return out


def _zona(zona_gj: Optional[str]):
    if not zona_gj:
        return None
    try:
        gj = json.loads(zona_gj)
        if gj.get("type") == "FeatureCollection":
            return unary_union([shape(f["geometry"]) for f in gj["features"] if f.get("geometry")])
        return shape(gj.get("geometry", gj))
    except Exception:
        return None


def manzanas(conn, survey_id: str) -> list:
    """[(código, geometría WGS 84)] de las manzanas que cuentan, sin ordenar."""
    zona_gj = conn.execute(text("""
        SELECT r.zone_geojson FROM surveys s JOIN regions r ON r.region_id = s.region_id
        WHERE s.survey_id = CAST(:s AS uuid)"""), {"s": survey_id}).scalar()
    # Misma consulta que la constancia (incluida la simplificación), para que el criterio
    # del 50 % caiga igual en las manzanas que el borde corta justo a la mitad.
    filas = conn.execute(text("""
        SELECT fuente_parcela, cca_code, nomenclatura_catastral,
               ST_AsGeoJSON(ST_SimplifyPreserveTopology(geometry, 0.000005))
        FROM parcelas WHERE survey_id = CAST(:s AS uuid) AND geometry IS NOT NULL"""),
        {"s": survey_id}).fetchall()
    por_mz: dict = {}
    for fuente, cca, nom, gj in filas:
        cod = manzana_codigo(fuente, cca, nom)
        if cod and gj:
            try:
                por_mz.setdefault(cod, []).append(shape(json.loads(gj)))
            except Exception:
                pass
    return contadas(por_mz, _zona(zona_gj))


def _anillos(g):
    for p in getattr(g, "geoms", [g]):
        if p.geom_type == "Polygon" and not p.is_empty:
            yield p.exterior.coords
            for h in p.interiors:
                yield h.coords


def dibujar(msp, conn, survey_id: str, tr) -> int:
    """Dibuja la capa CONTEO en `msp` (contorno + número + código de cada manzana) con el
    transformador `tr` (WGS 84 → metros del plano). Devuelve cuántas manzanas numeró."""
    lista = manzanas(conn, survey_id)
    if not lista:
        return 0
    doc = msp.doc
    if CAPA not in doc.layers:
        doc.layers.add(CAPA, color=COLOR)

    proy = []
    for cod, g in lista:
        gm = transform(tr.transform, g)
        # Los lotes vecinos no siempre comparten el vértice exacto: sin cerrar esas
        # rendijas la manzana sale como un rompecabezas de contornos.
        gm = gm.buffer(_CIERRE_M, join_style=2).buffer(-_CIERRE_M, join_style=2)
        if not gm.is_empty:
            proy.append((cod, gm, gm.representative_point()))
    proy.sort(key=lambda t: (-int(t[2].y // _FRANJA_M), t[2].x))

    for n, (cod, gm, p) in enumerate(proy, 1):
        for anillo in _anillos(gm):
            msp.add_lwpolyline(list(anillo), close=True, dxfattribs={"layer": CAPA})
        h = 8.0 if gm.area > 2000 else 4.0
        msp.add_text(str(n), dxfattribs={"layer": CAPA, "height": h}).set_placement(
            (p.x, p.y + h * 0.15), align=TextEntityAlignment.BOTTOM_CENTER)
        msp.add_text(cod, dxfattribs={"layer": CAPA, "height": h * 0.35}).set_placement(
            (p.x, p.y - h * 0.15), align=TextEntityAlignment.TOP_CENTER)

    x0 = min(t[1].bounds[0] for t in proy)
    y1 = max(t[1].bounds[3] for t in proy)
    msp.add_text(f"{len(proy)} MANZANAS (numeradas de norte a sur y de oeste a este; "
                 f"manzana segun la nomenclatura catastral, cuenta si mas de la mitad "
                 f"queda dentro de la zona)",
                 dxfattribs={"layer": CAPA, "height": 20}).set_placement((x0, y1 + 40))
    return len(proy)
