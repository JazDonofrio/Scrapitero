"""ProcedenciaPDF — la ficha de procedencia que acompaña a cada entrega.

El relevamiento registra de dónde salió **cada** dato (`fuente_parcela`, `direccion_source`,
`uf_fuente`, `uso_fuente`, `categoria_uso_fuente`…), pero nada de eso llegaba al cliente: el
CSV Operadora lleva las columnas de su contrato y el DXF de entrega los atributos de su
estándar de planos. Este PDF es esa trazabilidad, en una hoja que se lee.

Tres decisiones que valen más que el código:

* **Se genera desde la base, nunca se escribe a mano.** Una ficha redactada se desactualiza en
  la primera corrida y entonces miente con más autoridad que no decir nada.
* **Los sellos técnicos se traducen** (`_ETIQUETAS`). `bci` no le dice nada a nadie, y `manual`
  leído por el cliente puede sonar a «inventado» cuando significa lo contrario: que una persona
  fue a mirarlo. Un sello mal rotulado hace más daño que la falta de trazabilidad.
* **Las advertencias salen de los datos, no de una plantilla.** Si la altura se midió sobre
  imágenes de 2014, lo dice; si no se midió altura, no aparece la sección. Una ficha que
  siempre dice lo mismo deja de leerse.

No versiona ni guarda nada: se genera al exportar y refleja el estado de ese momento. Si el
relevamiento cambia, hay que exportarlo de nuevo — el propio documento lo avisa.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from fpdf import FPDF
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.db.engine import get_engine

# ── Marca (espejo de static/theme.css) ────────────────────────────────────────
_TEAL = (55, 190, 167)
_INDIGO = (125, 131, 191)
_TINTA = (24, 28, 45)
_GRIS = (110, 116, 135)
_FONDO = (243, 246, 252)

# Cómo se llama cada sello en el idioma del cliente. Lo que no esté acá sale tal cual, que es
# preferible a inventarle un nombre: un sello sin traducir se nota y se agrega; uno traducido
# de más miente sin que nadie lo vea.
_ETIQUETAS = {
    # de dónde salió la parcela
    "smartgis_vg": "Catastro municipal (SmartGIS)",
    "arba_carto": "Catastro de ARBA",
    "arba_idera": "IDERA - geometría catastral",
    "salta_idemsa": "Catastro municipal de Salta (IDEMSA)",
    "salta_idesa": "Catastro provincial de Salta (IDESA)",
    "sigef_onr": "SIGEF/INCRA - predios rurales",
    "onr_lotes": "ONR - lotes urbanos",
    "catastro": "Catastro",
    "shopping_poi": "Relevamiento anterior del cliente",
    "baseline": "Relevamiento anterior del cliente",
    # de dónde salió la dirección
    "bci_pdf": "Boletín de cadastro inmobiliario (BCI) del municipio",
    "bci_pdf_calle": "BCI del municipio (calle; altura inferida)",
    "catastro_interp": "Catastro, altura interpolada entre lotes vecinos",
    "google_maps": "Google Maps",
    "google_geocode": "Google Maps",
    "nominatim": "OpenStreetMap (Nominatim)",
    "mapbox": "Mapbox",
    "ibge": "IBGE - base de logradouros",
    "osm": "OpenStreetMap",
    # de dónde salió el conteo de unidades
    "bci": "Boletín de cadastro inmobiliario (BCI) del municipio",
    "cadastur": "Padrón federal de turismo (Cadastur)",
    "hotel_min": "Hotel confirmado, sin cantidad de habitaciones",
    "shopping_min": "Shopping identificado, sin cantidad de locales",
    "poi": "Comercios relevados (Overture Maps y OpenStreetMap)",
    "overture": "Comercios relevados (Overture Maps)",
    "google": "Comercios de Google Places",
    "arba_carto_uf": "Subparcelas declaradas en ARBA",
    "proxy": "Estimación por volumen construido",
    "uso": "Estimación por tipo de uso",
    "receita_cnae": "Padrón de empresas CNPJ (Receita Federal)",
    # clasificación de uso
    "cpua": "Zonificación municipal CPUA 2019",
    "sigsa": "Registro provincial SIGSA",
    "rentas": "Rentas provinciales (valuación edificada)",
    "clasificador": "Clasificación por comercios detectados",
    # trabajo humano
    "manual": "Verificado por relevador",
}

_CAMPOS = [
    ("fuente_parcela", "El lote y su geometría"),
    ("direccion_source", "La dirección"),
    ("uf_fuente", "La cantidad de unidades (UF)"),
    ("uso_fuente", "La clasificación de uso"),
    ("categoria_uso_fuente", "La etiqueta de tipo de edificación"),
]


class ProcedenciaInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None
    output_path: Optional[str] = None


class ProcedenciaOutput(BaseModel):
    ok: bool
    pdf_path: Optional[str] = None
    total_parcelas: int = 0
    error: Optional[str] = None


def _plural(n: int, singular: str, plural: str) -> str:
    """«1 shopping figura» y no «1 shopping(s) figuran»: el documento lo lee un cliente."""
    return f"{n} {singular}" if n == 1 else f"{n:,}".replace(",", ".") + f" {plural}"


def _etiqueta(sello: Optional[str]) -> str:
    if not sello:
        return "Sin registrar"
    return _ETIQUETAS.get(sello, sello)


def _txt(s) -> str:
    """Lo que fpdf puede escribir con las fuentes del core (latin-1).

    El acento va bien —es lo que hay en portugués y español—; lo que no entra son los signos
    que usamos en pantalla (flechas, ≈). Se reemplazan en vez de romper la exportación.
    """
    s = "" if s is None else str(s)
    for malo, bueno in (("→", "->"), ("≈", "~"), ("—", "-"), ("–", "-"),
                        ("“", '"'), ("”", '"'), ("’", "'"), ("⚠", "!")):
        s = s.replace(malo, bueno)
    return s.encode("latin-1", "replace").decode("latin-1")


class _Ficha(FPDF):
    def header(self):
        self.set_fill_color(*_TEAL)
        self.rect(0, 0, 210, 3, style="F")
        self.set_fill_color(*_INDIGO)
        self.rect(105, 0, 105, 3, style="F")
        self.set_y(9)
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(*_GRIS)
        self.cell(0, 5, _txt("AI MAPPING  |  Relevamiento geoespacial"),
                  new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", "", 7.5)
        self.set_text_color(*_GRIS)
        self.cell(0, 4, _txt("Documento generado automáticamente a partir del relevamiento. "
                             "Refleja su estado al momento de exportarlo."),
                  align="C", new_x="LMARGIN", new_y="NEXT")
        self.cell(0, 4, _txt(f"Página {self.page_no()}"), align="C")

    # ── piezas ────────────────────────────────────────────────────────────────
    def titulo(self, t: str):
        self.set_font("Helvetica", "B", 19)
        self.set_text_color(*_TINTA)
        self.cell(0, 10, _txt(t), new_x="LMARGIN", new_y="NEXT")

    def seccion(self, n: int, t: str):
        self.ln(3)
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(*_TEAL)
        self.cell(6, 7, _txt(f"{n}."), new_x="RIGHT", new_y="TOP")
        self.set_text_color(*_TINTA)
        self.cell(0, 7, _txt(t), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*_TEAL)
        self.set_line_width(0.4)
        y = self.get_y()
        self.line(self.l_margin, y, self.l_margin + 28, y)
        self.ln(2.5)

    def parrafo(self, t: str, size: float = 9):
        self.set_font("Helvetica", "", size)
        self.set_text_color(*_TINTA)
        self.multi_cell(0, 4.4, _txt(t), align="L", new_x="LMARGIN", new_y="NEXT")

    def nota(self, t: str):
        self.set_font("Helvetica", "I", 8.5)
        self.set_text_color(*_GRIS)
        self.multi_cell(0, 4.2, _txt(t), align="L", new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(*_TINTA)

    def fila(self, cols, widths, *, bold=False, fondo=False, size=8.5, alto=5.6):
        self.set_font("Helvetica", "B" if bold else "", size)
        self.set_text_color(*_TINTA)
        if fondo:
            self.set_fill_color(*_FONDO)
        for txt, w in zip(cols, widths):
            self.cell(w, alto, _txt(txt), border=0, fill=fondo,
                      align="R" if (w < 24 and txt and str(txt)[0].isdigit()) else "L")
        self.ln(alto)


# ── datos ─────────────────────────────────────────────────────────────────────
def _resumen(conn, survey_id: str) -> dict:
    """Los KPI, con la MISMA consulta que la web (`/api/surveys`), no con una equivalente.

    Un establecimiento que ocupa varios lotes cuenta una vez, y su UF sale de la tabla
    `establecimientos` —no del máximo de sus miembros, que da otro número—; la vivienda cae a
    `unidades_funcionales_estimadas` cuando no hay desglose. Escrito «a ojo» daba 579/1.111
    contra 578/921 del tablero: una ficha que contradice la pantalla hace dudar de las dos.
    """
    r = conn.execute(text("""
        WITH sueltas AS (
            SELECT count(*) n,
                   COALESCE(SUM(CASE
                       WHEN uf_vivienda IS NULL AND uf_comercio IS NULL
                       THEN COALESCE(unidades_funcionales_estimadas, 0)
                       ELSE COALESCE(uf_vivienda, 0) END)
                       FILTER (WHERE establecimiento_id IS NULL), 0) v,
                   COALESCE(SUM(uf_comercio) FILTER (WHERE establecimiento_id IS NULL), 0) c,
                   COALESCE(SUM(area_m2_terreno), 0) sup
            FROM parcelas WHERE survey_id = CAST(:sid AS uuid)
        ), ents AS (
            SELECT COALESCE(SUM(uf_vivienda), 0) v, COALESCE(SUM(uf_comercio), 0) c
            FROM establecimientos WHERE survey_id = CAST(:sid AS uuid)
        )
        SELECT sueltas.n, sueltas.v + ents.v, sueltas.c + ents.c, sueltas.sup
        FROM sueltas, ents
    """), {"sid": survey_id}).first()
    return {"parcelas": r[0] or 0, "viv": int(r[1] or 0), "com": int(r[2] or 0),
            "sup_m2": float(r[3] or 0)}


def _distribucion(conn, survey_id: str, col: str) -> list[tuple[str, int, float]]:
    filas = conn.execute(text(f"""
        SELECT {col} AS f, count(*) n FROM parcelas WHERE survey_id = :sid
        GROUP BY 1 ORDER BY 2 DESC
    """), {"sid": survey_id}).fetchall()
    tot = sum(f[1] for f in filas) or 1
    return [(_etiqueta(f[0]), f[1], 100.0 * f[1] / tot) for f in filas]


def _fuentes_externas(conn, region_id: str, survey_id: str) -> list[tuple[str, str, str]]:
    """Qué versión de cada fuente externa alimentó este relevamiento.

    Es la mitad que más se pregunta —«¿de cuándo es este dato?»— y la que no está en ninguna
    columna: vive en el nombre del dump, en el trimestre del padrón o en el pin del release.
    """
    out: list[tuple[str, str, str]] = []

    q = lambda s, **kw: conn.execute(text(s), kw).first()

    # Cadastur: el trimestre del que salió cada hotel con habitaciones
    # El padrón se consolida por CNPJ tomando el trimestre más reciente en que cada hotel
    # aparece, así que lo informativo es ése y no el rango: los hoteles que ya no figuran
    # conservan el dato de cuando figuraban, que es justamente lo que aporta esta fuente.
    tri = q("""SELECT max(fonte_trimestre), count(*) FROM cadastur_hospedagem""")
    if tri and tri[1]:
        out.append(("Padrón federal de turismo (Cadastur)",
                    "Hoteles y su cantidad de habitaciones",
                    f"Hasta el trimestre {tri[0]}"))

    # Receita: el mes del dump en disco
    dumps = sorted(p.name for p in Path("/opt/scrapitero/receita_dump").glob("*")
                   if p.is_dir()) if Path("/opt/scrapitero/receita_dump").exists() else []
    if dumps:
        out.append(("Padrón de empresas CNPJ (Receita Federal)",
                    "Rubro y situación de cada empresa", f"Dump {dumps[-1]}"))

    # Overture: el release pineado
    try:
        from scrapitero.agents.overture_places_fetcher import _RELEASE_DEFAULT
        n = q("""SELECT count(*) FROM comercios c JOIN surveys s ON s.survey_id = c.survey_id
                 WHERE c.survey_id = :sid AND c.source = 'overture'""", sid=survey_id)
        if n and n[0]:
            out.append(("Overture Maps", "Comercios y equipamiento", f"Release {_RELEASE_DEFAULT}"))
    except Exception:  # noqa: BLE001
        pass

    n = q("""SELECT count(*) FROM comercios WHERE survey_id = :sid AND source LIKE 'osm%'""",
          sid=survey_id)
    if n and n[0]:
        out.append(("OpenStreetMap", "Comercios y equipamiento", "Consultado en cada corrida"))

    n = q("""SELECT count(*) FROM footprints_revision WHERE survey_id = :sid""", sid=survey_id)
    if n and n[0]:
        out.append(("Google Open Buildings", "Huella de los edificios (satelital)",
                    f"{n[0]:,}".replace(",", ".") + " edificios detectados"))

    # Altura satelital: el año de la imagen sobre la que se midió
    a = q("""SELECT count(*), min(imagery_year), max(imagery_year), mode() WITHIN GROUP
             (ORDER BY imagery_year) FROM parcela_altura WHERE survey_id = :sid
             AND imagery_year IS NOT NULL""", sid=survey_id)
    if a and a[0]:
        rango = str(a[3]) if a[1] == a[2] else f"{a[1]} a {a[2]}, mayoría {a[3]}"
        out.append(("Google Solar API", "Altura de los edificios (satelital)",
                    f"Imágenes de {rango}"))
    return out


def _advertencias(conn, region_id: str, survey_id: str) -> list[str]:
    """Lo que el cliente necesita saber para leer bien los números. Sale de los datos: si la
    condición no se da, la línea no aparece."""
    av: list[str] = []
    q = lambda s, **kw: conn.execute(text(s), kw).first()

    r = q("""SELECT count(*) FROM parcelas WHERE survey_id = :sid AND uf_fuente = 'shopping_min'""",
          sid=survey_id)
    if r and r[0]:
        av.append(_plural(r[0], "shopping figura", "shoppings figuran") +
                  " con 1 unidad de comercio. Es un piso, no un conteo: ninguna fuente pública "
                  "publica la cantidad de locales de un centro comercial, y contarlos requiere "
                  "relevamiento en el lugar.")

    r = q("""SELECT count(*) FROM parcelas WHERE survey_id = :sid AND uf_fuente = 'hotel_min'""",
          sid=survey_id)
    if r and r[0]:
        av.append(_plural(r[0], "lote con hotel se cuenta", "lotes con hotel se cuentan") +
                  " como 1 unidad porque ninguna fuente declara sus habitaciones. Donde sí se "
                  "conocen, el conteo es el del padrón oficial.")

    r = q("""SELECT count(*) FROM parcelas WHERE survey_id = :sid
             AND numero_estimado IS NOT NULL AND (numero IS NULL OR numero IN ('', '0'))""",
          sid=survey_id)
    if r and r[0]:
        av.append(_plural(r[0], "dirección sale", "direcciones salen") +
                  " con la altura estimada por interpolación sobre el eje de la calle, porque el "
                  "municipio no la declara. Van identificadas en el CSV con su método y su nivel "
                  "de confianza.")

    a = q("""SELECT count(*), mode() WITHIN GROUP (ORDER BY imagery_year)
             FROM parcela_altura WHERE survey_id = :sid AND imagery_year IS NOT NULL""",
          sid=survey_id)
    if a and a[0] and a[1] and a[1] < datetime.now().year - 2:
        av.append(f"La altura satelital se midió sobre imágenes de {a[1]}. No refleja lo "
                  "construido después de esa fecha.")

    r = q("""SELECT count(*) FROM incidencias i JOIN surveys s ON s.survey_id = i.survey_id
             WHERE i.survey_id = :sid AND i.estado = 'pendiente'""", sid=survey_id)
    if r and r[0]:
        av.append(_plural(r[0], "caso queda marcado", "casos quedan marcados") +
                  " para revisión de un relevador: son los que ninguna fuente puede cerrar sola "
                  "(contradicciones entre el catastro y lo que se ve, direcciones sin altura, "
                  "hoteles sin habitaciones).")

    r = q("""SELECT count(*) FROM parcelas WHERE survey_id = :sid AND uf_fuente = 'manual'""",
          sid=survey_id)
    if r and r[0]:
        av.append(_plural(r[0], "lote tiene su conteo", "lotes tienen su conteo") +
                  " corregido a mano después de verificarlo. Esa corrección tiene prioridad "
                  "sobre cualquier fuente automática y se conserva en las actualizaciones "
                  "siguientes.")
    return av


# ── documento ─────────────────────────────────────────────────────────────────
def _construir(conn, region_id: str, survey_id: str, meta: dict) -> FPDF:
    pdf = _Ficha(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    pdf.titulo("Procedencia de los datos")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*_GRIS)
    pdf.cell(0, 5, _txt(f"Relevamiento: {meta['nombre']}"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, _txt(f"Generado el {datetime.now().strftime('%d/%m/%Y a las %H:%M')}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.nota("Este documento acompaña al relevamiento entregado y describe de dónde sale cada "
             "dato. Se genera en el momento de exportar: si el relevamiento se modifica, hay "
             "que exportarlo de nuevo para que la ficha vuelva a coincidir.")

    # 1 ── el relevamiento en números
    res = _resumen(conn, survey_id)
    pdf.seccion(1, "El relevamiento en números")
    w = [47, 47, 47, 45]
    pdf.fila(["Lotes relevados", "Unidades de vivienda", "Unidades de comercio",
              "Superficie de terreno"], w, bold=True, fondo=True)
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(*_INDIGO)
    for val, ww in zip([f"{res['parcelas']:,}".replace(",", "."),
                        f"{res['viv']:,}".replace(",", "."),
                        f"{res['com']:,}".replace(",", "."),
                        f"{res['sup_m2']/10000:,.1f} ha".replace(",", "X").replace(".", ",").replace("X", ".")],
                       w):
        pdf.cell(ww, 8, _txt(val))
    pdf.ln(8)
    pdf.set_text_color(*_TINTA)

    # 2 ── de dónde sale cada dato
    pdf.seccion(2, "De dónde sale cada dato")
    pdf.parrafo("Cada lote del relevamiento guarda el origen de su información, campo por "
                "campo. Así se reparte en esta entrega:")
    pdf.ln(1.5)
    w2 = [62, 84, 20, 20]
    pdf.fila(["Dato", "Origen", "Lotes", "%"], w2, bold=True, fondo=True)
    for col, titulo in _CAMPOS:
        dist = _distribucion(conn, survey_id, col)
        primero = True
        for etiqueta, n, pct in dist:
            pdf.fila([titulo if primero else "", etiqueta, str(n),
                      f"{pct:.1f}%".replace(".", ",")], w2)
            primero = False
        pdf.ln(0.8)

    # 3 ── las fuentes
    ext = _fuentes_externas(conn, region_id, survey_id)
    if ext:
        pdf.seccion(3, "Las fuentes utilizadas y su fecha")
        pdf.parrafo("Además del catastro municipal, el relevamiento se completa con fuentes "
                    "públicas. Ésta es la versión de cada una:")
        pdf.ln(1.5)
        w3 = [66, 66, 54]
        pdf.fila(["Fuente", "Qué aporta", "Versión"], w3, bold=True, fondo=True)
        for nombre, aporta, version in ext:
            pdf.fila([nombre, aporta, version], w3)

    # 4 ── advertencias
    av = _advertencias(conn, region_id, survey_id)
    if av:
        pdf.seccion(4 if ext else 3, "Lo que conviene saber para leer estos números")
        for a in av:
            pdf.set_font("Helvetica", "", 9)
            # Medir el bloque ANTES de dibujar el cuadrito: con el salto automático, la viñeta
            # se pintaba al pie de la página y el texto arrancaba en la siguiente, dejando un
            # cuadradito solo. El adorno se dibuja recién cuando se sabe que el texto lo sigue.
            alto = pdf.multi_cell(0, 4.4, _txt(a), dry_run=True, output="HEIGHT",
                                  new_x="LMARGIN", new_y="NEXT")
            if pdf.get_y() + alto > pdf.h - pdf.b_margin:
                pdf.add_page()
            y0 = pdf.get_y()
            pdf.set_fill_color(*_TEAL)
            pdf.rect(pdf.l_margin, y0 + 1.4, 1.6, 1.6, style="F")
            pdf.set_x(pdf.l_margin + 5)
            pdf.multi_cell(0, 4.4, _txt(a), align="L", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1.2)
    return pdf


@agent_run
def run(input: ProcedenciaInput) -> ProcedenciaOutput:
    engine = get_engine()
    with engine.connect() as conn:
        if input.survey_id:
            sid = input.survey_id
            row = conn.execute(text("""
                SELECT s.survey_id::text, r.region_id, COALESCE(r.name, r.region_id)
                FROM surveys s JOIN regions r ON r.region_id = s.region_id
                WHERE s.survey_id = CAST(:sid AS uuid)"""), {"sid": sid}).first()
        else:
            row = conn.execute(text("""
                SELECT s.survey_id::text, r.region_id, COALESCE(r.name, r.region_id)
                FROM surveys s JOIN regions r ON r.region_id = s.region_id
                WHERE s.region_id = :rid AND NOT s.archivado
                ORDER BY s.started_at DESC NULLS LAST LIMIT 1"""), {"rid": input.region_id}).first()
        if not row:
            return ProcedenciaOutput(ok=False, error=f"sin relevamiento para '{input.region_id}'")
        sid, region_id, nombre = row
        pdf = _construir(conn, region_id, sid, {"nombre": nombre})
        total = conn.execute(text("SELECT count(*) FROM parcelas WHERE survey_id = CAST(:s AS uuid)"),
                             {"s": sid}).scalar() or 0

    out = input.output_path or os.path.join(
        tempfile.gettempdir(), f"procedencia_{region_id}_{datetime.now():%Y%m%d_%H%M}.pdf")
    pdf.output(out)
    return ProcedenciaOutput(ok=True, pdf_path=out, total_parcelas=total)
