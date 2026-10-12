"""ConstanciaPDF — la constancia de trabajo realizado, para facturar un relevamiento.

Una hoja que dice qué se relevó, dónde y cuándo: el nombre del relevamiento, la zona, la
cantidad de manzanas y de lotes, y la fecha. La descarga el operador (no el cliente) y la
manda junto con la factura.

* **Las manzanas salen de la nomenclatura catastral oficial**, no de un dibujo nuestro: cada
  lote trae su código de manzana del catastro (`manzana_catastral.manzana_codigo`), y se
  cuentan las manzanas distintas. Es el número que el cliente puede verificar contra el
  catastro. Los lotes sin manzana en la nomenclatura (rurales en Salta, parcelas cargadas a
  mano) se informan aparte y no suman manzanas.
* **Si la fuente no tiene parser de manzana, el documento lo dice** en vez de inventar un
  número con otro método: una constancia de cobro con un conteo discutible es peor que una
  que avisa que falta.
* Como la ficha de procedencia, se genera al pedirla y refleja el estado de ese momento.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections import Counter
from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from shapely.geometry import shape
from shapely.ops import unary_union
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.manzana_catastral import manzana_codigo
from scrapitero.agents.procedencia_pdf import (_FONDO, _GRIS, _INDIGO, _TEAL, _TINTA,
                                               _Ficha, _etiqueta, _txt)
from scrapitero.db.engine import get_engine


class ConstanciaInput(BaseModel):
    survey_id: str
    output_path: Optional[str] = None


class ConstanciaOutput(BaseModel):
    ok: bool
    pdf_path: Optional[str] = None
    manzanas: int = 0
    lotes: int = 0
    error: Optional[str] = None


# Lotes que no vienen de un catastro (cargados a mano, o del relevamiento anterior del
# cliente): no son "catastro de origen" y no se espera leerles la manzana.
_NO_CATASTRO = {"manual", "shopping_poi", "baseline", None}

# Valor por manzana relevada, en pesos argentinos (Jaz, 8-oct-2026).
_PRECIO_MANZANA_ARS = 150


def _miles(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _fecha(d) -> str:
    return d.strftime("%d/%m/%Y") if d else "-"


def _anillos(geojson: Optional[str]) -> list[list[tuple[float, float]]]:
    """Los anillos exteriores del polígono de la zona (Feature, FeatureCollection o geometría)."""
    if not geojson:
        return []
    try:
        g = json.loads(geojson)
    except Exception:
        return []
    geoms = []
    if g.get("type") == "FeatureCollection":
        geoms = [f.get("geometry") for f in g.get("features", [])]
    elif g.get("type") == "Feature":
        geoms = [g.get("geometry")]
    else:
        geoms = [g]
    out = []
    for geo in geoms:
        if not geo:
            continue
        if geo.get("type") == "Polygon":
            out.append(geo["coordinates"][0])
        elif geo.get("type") == "MultiPolygon":
            out += [p[0] for p in geo["coordinates"]]
    return [[(float(x), float(y)) for x, y, *_ in a] for a in out]


def _zona_shape(geojson: Optional[str]):
    """El polígono encargado como geometría shapely (unión de sus features), o None."""
    try:
        g = json.loads(geojson) if geojson else None
    except Exception:
        return None
    if not g:
        return None
    if g.get("type") == "FeatureCollection":
        geoms = [f.get("geometry") for f in g.get("features", [])]
    elif g.get("type") == "Feature":
        geoms = [g.get("geometry")]
    else:
        geoms = [g]
    partes = [shape(x).buffer(0) for x in geoms if x]
    return unary_union(partes) if partes else None


def _area_ha(anillos) -> float:
    """Superficie aproximada (proyección local equirectangular): basta para un documento."""
    tot = 0.0
    for a in anillos:
        if len(a) < 3:
            continue
        lat0 = math.radians(sum(y for _, y in a) / len(a))
        kx, ky = 111_320 * math.cos(lat0), 110_540
        pts = [(x * kx, y * ky) for x, y in a]
        tot += abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))) / 2
    return tot / 10_000


def _mapa(pdf: _Ficha, anillos, lotes, x0: float, y0: float, w: float, h: float):
    """Croquis: los lotes en gris y el contorno de la zona en teal, escalados a la caja."""
    # El encuadre lo da la zona encargada (con un margen), no los lotes: un lote enorme que
    # la toca —el aeropuerto en Várzea Grande— achicaba la zona a una esquina del croquis.
    base = anillos or lotes
    xs = [x for a in base for x, _ in a]
    ys = [y for a in base for _, y in a]
    if not xs:
        return
    mx, my = (max(xs) - min(xs)) * 0.08, (max(ys) - min(ys)) * 0.08
    xs, ys = [min(xs) - mx, max(xs) + mx], [min(ys) - my, max(ys) + my]
    lat0 = math.radians((min(ys) + max(ys)) / 2)
    kx = math.cos(lat0)
    ancho, alto = (max(xs) - min(xs)) * kx or 1e-9, (max(ys) - min(ys)) or 1e-9
    esc = min(w / ancho, h / alto)
    ox = x0 + (w - ancho * esc) / 2
    oy = y0 + (h - alto * esc) / 2

    def p(x, y):
        return ox + (x - min(xs)) * kx * esc, oy + (max(ys) - y) * esc

    pdf.set_fill_color(*_FONDO)
    pdf.rect(x0, y0, w, h, style="F")
    with pdf.rect_clip(x0, y0, w, h):
        pdf.set_draw_color(170, 176, 192)
        pdf.set_line_width(0.05)
        for a in lotes:
            pdf.polygon([p(x, y) for x, y in a], style="D")
        pdf.set_draw_color(*_TEAL)
        pdf.set_line_width(0.6)
        for a in anillos:
            pdf.polygon([p(x, y) for x, y in a], style="D")


def _construir(conn, sid: str) -> tuple[_Ficha, dict]:
    meta = conn.execute(text("""
        SELECT COALESCE(r.name, r.region_id), r.zone_geojson, s.status, s.started_at,
               s.finished_at, r.country_code
        FROM surveys s JOIN regions r ON r.region_id = s.region_id
        WHERE s.survey_id = CAST(:s AS uuid)"""), {"s": sid}).first()
    if not meta:
        raise ValueError("relevamiento no encontrado")
    nombre, zona_gj, estado, inicio, fin, _pais = meta

    filas = conn.execute(text("""
        SELECT fuente_parcela, cca_code, nomenclatura_catastral, localidad, municipio,
               estado_provincia, pais, ST_AsGeoJSON(ST_SimplifyPreserveTopology(geometry, 0.000005))
        FROM parcelas WHERE survey_id = CAST(:s AS uuid)"""), {"s": sid}).fetchall()

    codigos = [manzana_codigo(f[0], f[1], f[2]) for f in filas]
    # Sólo cuentan las manzanas DENTRO del polígono encargado (Jaz, 8-oct): la que el borde
    # corta entra si tiene al menos la mitad de su superficie adentro. Sin polígono, todas.
    zona = _zona_shape(zona_gj)
    por_mz: dict = {}
    for f, c in zip(filas, codigos):
        if c and f[7]:
            try:
                por_mz.setdefault(c, []).append(shape(json.loads(f[7])))
            except Exception:
                pass
    if zona is not None:
        manzanas = sum(1 for trozos in por_mz.values()
                       if (u := unary_union(trozos)).area
                       and u.intersection(zona).area / u.area >= 0.5)
    else:
        manzanas = len({c for c in codigos if c})
    fuentes = Counter(f[0] for f in filas)
    es_salta = any((k or "").startswith("salta_") for k in fuentes)
    fuentes_sin_parser = sorted({f[0] or "sin fuente" for f, c in zip(filas, codigos)
                                 if not c} - _NO_CATASTRO - {"salta_idesa"})

    def moda(i):
        c = Counter(f[i] for f in filas if f[i])
        return c.most_common(1)[0][0] if c else ""
    # El catastro de Salta no trae localidad ni municipio por lote: sin ellos, «Salta,
    # Argentina» no dice dónde; se antepone el nombre de la zona.
    # El de Salta interior además pone la provincia como municipio en casi todos los lotes
    # (5.949 de 5.989 «Salta» en Rosario de Lerma): un municipio igual a la provincia no se usa.
    muni = moda(4) if moda(4) != moda(5) else ""
    local = moda(3) or muni or nombre
    lugar = ", ".join(x for x in dict.fromkeys([local, muni, moda(5), moda(6)]) if x)

    anillos = _anillos(zona_gj)
    lotes = []
    for f in filas:
        try:
            g = json.loads(f[7]) if f[7] else None
        except Exception:
            g = None
        if g and g.get("type") == "Polygon":
            lotes.append([(float(x), float(y)) for x, y, *_ in g["coordinates"][0]])
        elif g and g.get("type") == "MultiPolygon":
            lotes += [[(float(x), float(y)) for x, y, *_ in pg[0]] for pg in g["coordinates"]]

    pdf = _Ficha(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.titulo("Constancia de relevamiento")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*_GRIS)
    pdf.cell(0, 5, _txt(f"Emitida el {datetime.now().strftime('%d/%m/%Y')}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    if estado != "completed":
        pdf.nota(f"Atención: el relevamiento figura como «{estado}», no como terminado. Los "
                 "números de esta constancia pueden cambiar si se vuelve a correr.")
        pdf.ln(1)

    # 1 ── datos
    pdf.seccion(1, "Datos del relevamiento")
    if inicio and fin and inicio.date() != fin.date():
        fechas = f"del {_fecha(inicio)} al {_fecha(fin)}"
    else:
        fechas = _fecha(fin or inicio)
    datos = [("Relevamiento", nombre),
             ("Zona", lugar or "-"),
             ("Superficie de la zona", f"{_area_ha(anillos):,.1f} ha".replace(",", "X")
              .replace(".", ",").replace("X", ".") if anillos else "-"),
             ("Fecha de realización", fechas),
             ("Catastro de origen", ", ".join(_etiqueta(k) for k, _ in fuentes.most_common()
                                              if k not in _NO_CATASTRO) or "-"),
             ("Identificador", sid)]
    if es_salta:   # Jaz, 8-oct: en la constancia de Salta no van ni el catastro ni el ID
        datos = [d for d in datos if d[0] not in ("Catastro de origen", "Identificador")]
    for k, v in datos:
        pdf.fila([k, v], [52, 134], bold=False, fondo=False, size=9.5, alto=6.2)

    # 2 ── trabajo realizado
    pdf.seccion(2, "Trabajo realizado")
    # Los lotes sin manzana (rurales, cargados a mano) no se muestran aparte: Jaz, 8-oct.
    # El valor por manzana es en pesos argentinos: sólo se muestra en relevamientos de AR.
    precio = _PRECIO_MANZANA_ARS if (_pais or "").upper() in ("ARG", "AR") else None
    cab, vals = ["Manzanas relevadas"], [_miles(manzanas)]
    # En Salta se cobra por manzana y los lotes no se informan (Jaz, 8-oct).
    if not es_salta:
        cab.append("Lotes relevados")
        vals.append(_miles(len(filas)))
    if precio is not None:
        cab += ["Valor por manzana", "Total"]
        vals += [f"$ {_miles(precio)}", f"$ {_miles(precio * manzanas)}"]
    w = [186 / len(cab) if len(cab) > 2 else 62] * len(cab)
    pdf.fila(cab, w, bold=True, fondo=True)
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(*_INDIGO)
    for val, ww in zip(vals, w):
        pdf.cell(ww, 11, _txt(val))
    pdf.ln(12)
    pdf.set_text_color(*_TINTA)
    nota = ("Las manzanas se cuentan según la nomenclatura catastral oficial de cada lote: "
            "se suman las manzanas distintas a las que pertenecen los lotes relevados que "
            "quedan dentro de la zona encargada (una manzana cortada por el borde cuenta si "
            "tiene al menos la mitad de su superficie adentro).")
    if fuentes_sin_parser:
        nota += (" Atención: para el catastro " + ", ".join(_etiqueta(f) for f in fuentes_sin_parser)
                 + " todavía no se lee la manzana de la nomenclatura; esos lotes no suman.")
    pdf.nota(nota)

    # 3 ── croquis
    if anillos or lotes:
        pdf.seccion(3, "Zona relevada")
        y = pdf.get_y() + 1
        alto = min(110.0, pdf.h - pdf.b_margin - y - 34)
        if alto > 40:
            _mapa(pdf, anillos, lotes, pdf.l_margin, y, pdf.w - pdf.l_margin - pdf.r_margin, alto)
            pdf.set_y(y + alto + 2)
            pdf.nota("En verde, el contorno de la zona encargada; en gris, los lotes relevados.")
    return pdf, {"manzanas": manzanas, "lotes": len(filas), "nombre": nombre,
                 "fecha": fin or inicio}


@agent_run
def run(input: ConstanciaInput) -> ConstanciaOutput:
    with get_engine().connect() as conn:
        try:
            pdf, info = _construir(conn, input.survey_id)
        except ValueError as e:
            return ConstanciaOutput(ok=False, error=str(e))
    out = input.output_path or os.path.join(
        tempfile.gettempdir(), f"constancia_{input.survey_id}_{datetime.now():%Y%m%d_%H%M}.pdf")
    pdf.output(out)
    return ConstanciaOutput(ok=True, pdf_path=out, manzanas=info["manzanas"], lotes=info["lotes"])
