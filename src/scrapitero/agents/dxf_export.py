"""DXFExport — exporta el relevamiento a DXF (AutoCAD) con las unidades de vivienda.

Por qué DXF y no DWG: el `.dwg` es formato cerrado de Autodesk y **ninguna librería
libre lo escribe** de forma confiable. El DXF es el formato de intercambio del propio
AutoCAD: lo abre nativo y lo guarda como `.dwg` con "Guardar como". Si alguna vez el
cliente exige el `.dwg` literal, el paso que falta es una conversión con ODA File
Converter sobre este mismo archivo — no cambia nada de lo de acá.

Por qué `ezdxf` y no el driver DXF de GDAL (que ya está instalado): el driver de OGR
**no escribe atributos** (falla con `FieldError` al agregar un campo) ni textos. Sólo
sabe volcar geometría y respetar un campo `Layer`. Es decir, daría polígonos mudos: sin
la UF, que es justamente el dato que se quiere entregar.

Estructura del dibujo — todo en **capas separadas** para que el proyectista prenda y
apague lo que necesite:

    PARCELAS          polilínea cerrada del lote (el polígono del catastro)
    UF_VIVIENDA       texto con las unidades de vivienda        ← el dato pedido
    UF_COMERCIO       texto con las unidades de comercio
    DIRECCION         calle + número (con el estimado, regla del cliente)
    TIPO_EDIFICACION  etiqueta de la taxonomía del cliente
    DATOS             bloque con TODOS los atributos, invisible en pantalla pero
                      consultable desde la ventana de propiedades de AutoCAD
    _METADATOS        EPSG, survey y fecha, para que el archivo se pueda auditar

**Unidades y coordenadas:** la DB guarda en EPSG:4326 (grados). Un CAD trabaja en
metros, así que se proyecta a UTM — el huso se autodetecta con `geo.utm_epsg`, que es
válido en todo el planeta (no hay husos fijos). En Brasil se rotula como SIRGAS 2000
(EPSG:319xx), que numéricamente es idéntico a WGS 84 UTM pero es la etiqueta que
espera un proyectista brasilero. `epsg` en el input permite forzar otro.

No toca la DB: es sólo lectura + archivo.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import ezdxf
from ezdxf.enums import TextEntityAlignment
from loguru import logger
from pydantic import BaseModel
from pyproj import Transformer
from shapely import wkb
from shapely.geometry import MultiPolygon, Polygon
from sqlalchemy import text

from scrapitero.agents import geo
from scrapitero.agents._run import agent_run
from scrapitero.db.engine import get_engine

# Colores ACI (índice de color de AutoCAD). Se usa el índice y no RGB porque es lo que
# respetan las tablas de estilos de impresión (CTB) con las que trabaja el proyectista.
_LAYERS = {
    "PARCELAS": 7,          # blanco/negro según fondo
    "UF_VIVIENDA": 5,       # azul
    "UF_COMERCIO": 1,       # rojo
    "DIRECCION": 8,         # gris
    "TIPO_EDIFICACION": 3,  # verde
    "DATOS": 8,
    "_METADATOS": 8,
}

# Atributos que viajan en el bloque invisible de cada parcela.
_ATRIBUTOS = [
    "DIRECCION", "CALLE", "NUMERO", "COMPLEMENTO", "BAIRRO", "CEP",
    "UF_VIVIENDA", "UF_COMERCIO", "UF_TOTAL", "UF_FUENTE",
    "USO", "TIPO_EDIFICACION", "INSCRIPCION",
    "AREA_TERRENO_M2", "AREA_CONSTRUIDA_M2", "PISOS",
    "ESTABLECIMIENTO",  # tipo + nombre si la parcela es miembro de uno
]

_BLOQUE = "PARCELA_INFO"


class DXFInput(BaseModel):
    survey_id: str
    output_path: Optional[str] = None
    epsg: Optional[int] = None          # None = autodetecta el huso UTM del centroide
    text_height: float = 2.0            # metros (unidades de dibujo)
    solo_con_uf: bool = False           # exportar sólo parcelas con al menos 1 UF
    max_parcelas: Optional[int] = None  # para generar muestras


class DXFOutput(BaseModel):
    ok: bool
    dxf_path: Optional[str] = None
    parcelas: int = 0
    uf_vivienda_total: int = 0
    uf_comercio_total: int = 0
    epsg: Optional[int] = None
    error: Optional[str] = None


def _consultar(survey_id: str, solo_con_uf: bool,
               limite: Optional[int]) -> tuple[list, Optional[str], tuple[int, int]]:
    """Parcelas del survey con geometría. Mismo criterio de UF efectiva que el CSV y la web."""
    engine = get_engine()
    with engine.connect() as conn:
        meta = conn.execute(text("""
            SELECT r.name, s.started_at, r.region_id
            FROM surveys s JOIN regions r ON s.region_id = r.region_id
            WHERE s.survey_id = :sid
        """), {"sid": survey_id}).fetchone()
        if not meta:
            return [], None, (0, 0)

        filtro_uf = ""
        if solo_con_uf:
            filtro_uf = """AND COALESCE(uf_vivienda, 0) + COALESCE(uf_comercio, 0)
                           + COALESCE(unidades_funcionales_estimadas, 0) > 0"""
        limite_sql = f"LIMIT {int(limite)}" if limite else ""

        rows = conn.execute(text(f"""
            SELECT
                ST_AsBinary(geometry) AS geom,
                calle, numero, complemento, barrio, codigo_postal,
                -- UF vivienda EFECTIVA: sin desglose viv/com, las estimadas cuentan
                -- como vivienda (mismo criterio que el CSV y los KPIs de la web).
                CASE WHEN uf_vivienda IS NULL AND uf_comercio IS NULL
                     THEN COALESCE(unidades_funcionales_estimadas, 0)
                     ELSE COALESCE(uf_vivienda, 0) END AS uf_viv,
                COALESCE(uf_comercio, 0) AS uf_com,
                uf_fuente, uso_principal, cca_code,
                area_m2_terreno, area_m2_construida, pisos_estimados_max,
                numero_estimado, numero_estimado_confianza,
                categoria_uso, descripcion_uso,
                establecimiento_id,
                (SELECT e.tipo FROM establecimientos e
                   WHERE e.establecimiento_id = parcelas.establecimiento_id) AS est_tipo,
                (SELECT e.nombre FROM establecimientos e
                   WHERE e.establecimiento_id = parcelas.establecimiento_id) AS est_nombre,
                (SELECT ptm.tipo_edificacion FROM parcela_tipo_manual ptm
                   WHERE ptm.parcela_id = parcelas.parcela_id) AS tipo_manual,
                COALESCE((SELECT h.tipo FROM hoteles h
                   WHERE h.parcela_id = parcelas.parcela_id AND NOT h.cerrado_def
                   ORDER BY h.habitaciones DESC NULLS LAST LIMIT 1), '') AS hotel_tipo
            FROM parcelas
            WHERE survey_id = :sid AND geometry IS NOT NULL {filtro_uf}
            ORDER BY calle NULLS LAST, numero NULLS LAST
            {limite_sql}
        """), {"sid": survey_id}).fetchall()

        # UF de los establecimientos: cada uno (varias parcelas = 1 entidad) cuenta por
        # la UF de su miembro más desarrollado, NO por la suma. Es el mismo criterio de
        # los KPIs de la web y del CSV; sin esto el total del DXF los contradice.
        est = conn.execute(text("""
            SELECT COALESCE(SUM(uf_vivienda), 0), COALESCE(SUM(uf_comercio), 0)
            FROM establecimientos WHERE survey_id = :sid
        """), {"sid": survey_id}).fetchone()
    return list(rows), meta[0], (int(est[0] or 0), int(est[1] or 0))


def _tipo_label(uso, uf_v, area, descripcion, hotel_tipo, manual) -> str:
    """Etiqueta de la taxonomía del cliente.

    Se reusa el `_tipo_edificacion` de la web para no mantener dos taxonomías que se
    van a separar sola; si el import falla (la web arrastra FastAPI), cae a una
    derivación mínima equivalente para los casos del catastro.
    """
    try:
        from scrapitero.web.app import _tipo_edificacion
        return _tipo_edificacion(uso, uf_v, area, descripcion, hotel_tipo, manual)
    except Exception:
        if manual:
            return manual
        if descripcion:
            return descripcion.split("|")[0].strip()
        if uso in ("vacante", "baldio"):
            return "LOTE VAZIO"
        if uso == "residencial":
            return "APARTAMENTO" if (uf_v or 0) > 1 else "RESIDÊNCIA"
        if uso == "industrial":
            return "INDÚSTRIA"
        if uso in ("comercial", "mixto"):
            return "COMÉRCIO EM GERAL"
        return ""


def _definir_bloque(doc) -> None:
    """Bloque con los atributos invisibles: el dibujo queda limpio y el dato consultable."""
    blk = doc.blocks.new(name=_BLOQUE)
    for i, tag in enumerate(_ATRIBUTOS):
        blk.add_attdef(
            tag=tag,
            insert=(0, -i * 0.5),
            dxfattribs={"invisible": 1, "height": 0.5, "layer": "DATOS"},
        )


def _anillos(geom) -> list[list[tuple[float, float]]]:
    """Contornos del polígono (exterior + huecos), cada uno como lista de vértices."""
    partes = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
    out = []
    for p in partes:
        if not isinstance(p, Polygon) or p.is_empty:
            continue
        out.append(list(p.exterior.coords))
        out.extend(list(h.coords) for h in p.interiors)
    return out


@agent_run
def run(input: DXFInput) -> DXFOutput:
    rows, region_nombre, (est_uf_v, est_uf_c) = _consultar(
        input.survey_id, input.solo_con_uf, input.max_parcelas)
    if region_nombre is None:
        return DXFOutput(ok=False, error=f"Survey no encontrado: {input.survey_id}")
    if not rows:
        return DXFOutput(ok=False, error="El survey no tiene parcelas con geometría.")

    geoms = [wkb.loads(bytes(r[0])) for r in rows]

    # EPSG de salida: el huso UTM del centroide del conjunto. En Brasil se rotula como
    # SIRGAS 2000 (319xx), idéntico numéricamente a WGS 84 UTM pero es la etiqueta local.
    if input.epsg:
        epsg = input.epsg
    else:
        c = MultiPolygon([g for g in geoms if isinstance(g, Polygon)]).centroid \
            if len(geoms) > 1 else geoms[0].centroid
        epsg = geo.utm_epsg(c.x, c.y)
        if -34 <= c.y <= 6 and -74 <= c.x <= -34 and 32717 <= epsg <= 32725:  # Brasil
            epsg = 31977 + (epsg - 32717)  # 32717/zona17S → 31977, y así

    tr = Transformer.from_crs(4326, epsg, always_xy=True)

    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 6  # metros
    for nombre, color in _LAYERS.items():
        doc.layers.add(nombre, color=color)
    _definir_bloque(doc)
    msp = doc.modelspace()

    h = input.text_height
    total_v = total_c = 0
    dibujadas = 0

    for row, geom in zip(rows, geoms):
        (_, calle, numero, complemento, barrio, cep, uf_v, uf_c, uf_fuente, uso,
         cca, area_t, area_c, pisos, num_est, num_est_conf, _categoria, descripcion,
         establecimiento_id, est_tipo, est_nombre, tipo_manual, hotel_tipo) = row

        anillos = _anillos(geom)
        if not anillos:
            continue
        for anillo in anillos:
            msp.add_lwpolyline(
                [tr.transform(x, y) for x, y in anillo],
                close=True, dxfattribs={"layer": "PARCELAS"},
            )

        # Punto de rotulado: representative_point cae SIEMPRE dentro del polígono
        # (el centroide de una parcela en "L" puede caer afuera).
        rp = geom.representative_point()
        cx, cy = tr.transform(rp.x, rp.y)

        # Número: el del municipio, y si no lo declaró el estimado (regla del cliente:
        # toda dirección sale con número). El estimado va entre paréntesis — no con "≈",
        # porque las fuentes SHX de AutoCAD no traen ese glifo y saldría un signo roto.
        num = str(numero) if numero not in (None, "", 0, "0") else ""
        if not num and num_est and (num_est_conf or 0) >= 0.4:
            num = f"({num_est})"
        direccion = " ".join(x for x in [calle or "", num] if x).strip()
        tipo = _tipo_label(uso, uf_v, area_c, descripcion, hotel_tipo or None, tipo_manual)

        # Rótulos apilados sobre el centro de la parcela, cada uno en su capa.
        # **Sólo el número**, no la calle: el nombre se repite en toda la cuadra (es
        # ruido) y no entra en un lote de 10 m de frente. La dirección completa viaja
        # igual en el bloque de atributos, que es donde se consulta.
        lineas = []
        if num:
            lineas.append((num, "DIRECCION", h * 0.8))
        if uf_v:
            lineas.append((f"{uf_v} VIV", "UF_VIVIENDA", h))
        if uf_c:
            lineas.append((f"{uf_c} COM", "UF_COMERCIO", h))
        if tipo:
            lineas.append((tipo, "TIPO_EDIFICACION", h * 0.7))

        # Los lotes de VG son angostos y muy desiguales: con una altura fija los rótulos
        # de los chicos se pisan entre sí y con los del vecino. Se escala al ancho real
        # del lote, con un piso para que no se vuelva ilegible al hacer zoom.
        pminx, pminy, pmaxx, pmaxy = geom.bounds
        ancho_m = max(tr.transform(pmaxx, pminy)[0] - tr.transform(pminx, pminy)[0], 1.0)
        largo_txt = max(len(t) for t, _, _ in lineas) if lineas else 1
        escala = min(1.0, ancho_m * 0.85 / max(largo_txt * h * 0.6, 0.1))
        escala = max(escala, 0.35)

        # El paso entre renglones se calcula sobre la altura REAL de cada uno: las líneas
        # no miden todas igual (la UF es la más grande) y con un paso fijo el número se
        # montaba encima del rótulo de UF.
        alturas = [alto * escala for _, _, alto in lineas]
        paso = max(alturas) * 1.45
        offset = (len(lineas) - 1) * paso / 2
        for i, ((txt, capa, _), alto) in enumerate(zip(lineas, alturas)):
            t = msp.add_text(txt, dxfattribs={"layer": capa, "height": alto})
            t.set_placement((cx, cy + offset - i * paso), align=TextEntityAlignment.MIDDLE_CENTER)

        # Bloque invisible con todo el dato consultable en propiedades de AutoCAD.
        ref = msp.add_blockref(_BLOQUE, (cx, cy), dxfattribs={"layer": "DATOS"})
        ref.add_auto_attribs({
            "DIRECCION": direccion,
            "CALLE": calle or "",
            "NUMERO": str(numero or ""),
            "COMPLEMENTO": complemento or "",
            "BAIRRO": barrio or "",
            "CEP": cep or "",
            "UF_VIVIENDA": str(uf_v or 0),
            "UF_COMERCIO": str(uf_c or 0),
            "UF_TOTAL": str((uf_v or 0) + (uf_c or 0)),
            "UF_FUENTE": uf_fuente or "",
            "USO": uso or "",
            "TIPO_EDIFICACION": tipo,
            "INSCRIPCION": cca or "",
            "AREA_TERRENO_M2": f"{area_t:.1f}" if area_t else "",
            "AREA_CONSTRUIDA_M2": f"{area_c:.1f}" if area_c else "",
            "PISOS": str(pisos or ""),
            "ESTABLECIMIENTO": " - ".join(x for x in [est_tipo or "", est_nombre or ""] if x),
        })

        # El total NO suma las parcelas agrupadas en un establecimiento: la entidad
        # cuenta una vez (por su miembro más desarrollado) y se agrega aparte, abajo.
        # El rótulo de la parcela sí muestra su propia UF — es el dato de esa parcela.
        if establecimiento_id is None:
            total_v += uf_v or 0
            total_c += uf_c or 0
        dibujadas += 1

    # Cada establecimiento aporta la UF de su miembro más desarrollado (ya calculada en
    # la tabla `establecimientos`), en lugar de la suma de las parcelas que lo componen.
    # Sólo aplica al export completo: con `max_parcelas` el recorte no tiene por qué
    # contener todos los miembros, así que el total sería peor que la suma cruda.
    if not input.max_parcelas:
        total_v += est_uf_v
        total_c += est_uf_c

    # Metadatos en el propio dibujo: sin esto, un DXF sin CRS declarado es incalzable
    # contra los planos del cliente y nadie puede auditar con qué salió.
    minx, miny, maxx, maxy = MultiPolygon(
        [g for g in geoms if isinstance(g, Polygon)]).bounds if len(geoms) > 1 else geoms[0].bounds
    mx, my = tr.transform(minx, miny)
    nota = (f"AI Mapping - {region_nombre} | survey {input.survey_id} | "
            f"{datetime.now():%Y-%m-%d %H:%M} | CRS EPSG:{epsg} | unidades: metros | "
            f"{dibujadas} parcelas | {total_v} UF vivienda | {total_c} UF comercio | "
            f"numero entre parentesis = estimado (el municipio no lo declaro)")
    msp.add_text(nota, dxfattribs={"layer": "_METADATOS", "height": h}) \
       .set_placement((mx, my - h * 4))

    out = Path(input.output_path) if input.output_path else Path(
        f"/tmp/relevamiento_{input.survey_id[:8]}.dxf")
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(out)

    logger.info(f"DXFExport {input.survey_id}: {dibujadas} parcelas, "
                f"{total_v} UF viv / {total_c} UF com, EPSG:{epsg} → {out}")

    return DXFOutput(ok=True, dxf_path=str(out), parcelas=dibujadas,
                     uf_vivienda_total=total_v, uf_comercio_total=total_c, epsg=epsg)
