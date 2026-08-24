"""DXFEntrega — exporta el relevamiento con el estándar de planos del cliente (BL).

**Qué lo diferencia de `DXFExport`.** `DXFExport` dibuja para nosotros: el polígono de
cada parcela y los datos en textos legibles al lado. Este agente dibuja para *ellos*,
replicando el `plano ejemplo de entrega.dwg` que mandaron el 18-ago-2026. Los dos quedan:
el viejo es la herramienta de trabajo interna, éste es el entregable.

El estándar se extrajo del DWG original (`dwgread -O JSON`), **no del DXF convertido**:
`dwg2dxf` de LibreDWG es fiel con la geometría (19.831 de 19.831 polilíneas del mapa base)
pero **pierde INSERTs** (24 SDU donde el DWG tiene 391) y **descarta el bit de
invisibilidad de los atributos** (todos salen `flags=0`). Un estándar leído del DXF habría
salido con los 15 atributos del SDU dibujados encima del plano.

**Cómo plasma la info el cliente** — y es lo opuesto a lo que hacíamos:

  · **Un INSERT de bloque por EDIFICACIÓN**, no un polígono con textos y **no uno por
    lote**: la casa con el local adelante son dos bloques, uno `R` y otro `C`, como en el
    plano del cliente (46 de sus 186 números aparecen repetidos, con tipos distintos).
    `SDU` para la unidad unifamiliar, `MDU` para el edificio multifamiliar.
  · **Una sola etiqueta visible** por inmueble: `N_C1_3_TP_QT` = número + tipo + cantidad.
    `350R` (nº 350, residencial, 1 HP) · `25R-2` (nº 25, residencial, 2 HP) · `S/NC`
    (sin número, comercial) · `VAZ` (lote vacío). Los otros 14 atributos viajan
    **invisibles**, consultables desde la ventana de propiedades de AutoCAD.
  · **El tipo va además en la capa**: `SDU_RES` / `SDU_COM` / `SDU_ESP` / `SDU_VAZ` y sus
    equivalentes `MDU_*`. El proyectista prende y apaga por capa, y cuenta por bloque.
  · **El mapa base es de ellos**: `QUADRA` (manzana) y `MEIOFIO` (cordón) salen del MUB
    municipal cargado en `mapa_base_cliente`, no de nuestro catastro. Si dibujáramos
    nuestros polígonos, las manzanas no calzarían contra el resto de sus planos.

**Escala y coordenadas:** modelspace 1:1, **1 unidad = 1 metro**, coordenadas UTM absolutas
en **WGS 84** — el cliente lo pide como «UTM84-21S», que para Várzea Grande es EPSG:32721.
No confundir con SIRGAS 2000 (EPSG:31981), que es lo que se emitía antes por suposición
nuestra: son numéricamente idénticos (0,0 cm de diferencia medidos sobre las esquinas de
VG), así que el cambio es de etiqueta y ninguna coordenada se mueve. No hay
paperspace ni cajetín — los dos `Layout` del ejemplo están vacíos, el plano se entrega en
modelspace. Alturas de texto en metros, tal cual el ejemplo: 1,5 la etiqueta del inmueble,
2,0 los datos, 1,0 los secundarios, 25–30 los títulos de célula. Formato **R2000**.

**Lo que NO llenamos, y por qué.** El estándar tiene campos que son del sistema del cliente
o del diseño de red, no del relevamiento:

  · `CODLOG` — código de logradouro del cliente. Se mapea desde un CSV que ellos den
    (`codlog_csv`); sin ese archivo sale vacío. En su ejemplo viene 24/24 lleno, así que
    es probable que lo exijan.
  · `ID` — identificador de célula (p.ej. `VAZ049`). Va por parámetro (`celula_id`).
  · `CODGED`, `NOME` (NAP de atendimiento), `CLASSE_SOCIAL`, `TIPOIMOVEL` — vacíos, que es
    como vienen en el propio ejemplo del cliente (0/24, 0/24, 0/24 y 0/24).
  · `QTD_ANDARES` del MDU — sale de `pisos_estimados_max` y, como respaldo, de los pisos
    satelitales de `parcela_altura` (`pisos_estimados_max` está **vacío en las 567 parcelas
    de VG**). Va en **pisos sobre planta baja**: PB sola = 0, PB + uno = 1. La base guarda
    la otra convención —niveles, PB = 1— porque ahí el número multiplica; la conversión es
    `pisos.sobre_planta_baja`, que documenta las dos. Sin ninguno de los dos datos sale
    vacío: no se inventa una altura, y un 0 se leería como "planta baja sola".
  · `POSTE`, `DROP`, `FLY_TAP`, `HCSDU`, `HCMDU` — son diseño de red (postes, acometidas,
    puntos de agregación). No los relevamos y no se inventan: las capas y los bloques
    quedan definidos en el archivo para que el proyectista los use, vacíos.

No toca la DB: es sólo lectura + archivo.
"""

from __future__ import annotations

import csv
import math
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

import ezdxf
import shapely
from loguru import logger
from pydantic import BaseModel
from pyproj import Transformer
from shapely import wkb
from shapely.geometry import LineString, MultiPoint, MultiPolygon, Point, Polygon, box
from sqlalchemy import text

from scrapitero.agents import geo
from scrapitero.agents._run import agent_run
from scrapitero.agents.dxf_export import _consultar
from scrapitero.agents.pisos import texto as pisos_texto
from scrapitero.db.engine import get_engine

_PLANTILLA = Path(__file__).resolve().parents[1] / "assets" / "plantilla_entrega_bl.dxf"

# Alturas de texto del estándar, en metros de modelspace (medidas sobre el DWG del cliente).
_H_CELULA = 30.0     # título de la célula, capa SERVICO
_H_RESUMEN = 25.0    # "1133 HPS" / "6,219 KM", capa 0
_H_META = 2.0        # nuestra nota de auditoría


class EntregaInput(BaseModel):
    survey_id: str
    celula_id: str = ""                 # atributo ID de cada bloque, p.ej. "VAZ049"
    codlog_csv: Optional[str] = None    # CSV calle,codlog del cliente
    fuente_mapa_base: str = "MUB_MT_VARZEA_GRANDE"
    output_path: Optional[str] = None
    epsg: Optional[int] = None          # None = autodetecta el huso UTM
    # Versión del DXF a escribir. El estándar del cliente es R2000 ("AC1015"), que es lo que
    # trae su propio DWG, y por eso es el default. Se expone porque algunos lectores nuevos
    # —AutoCAD web entre ellos— digieren mejor un formato más nuevo: si el R2000 rebota con
    # «Invalid or incomplete DXF input», probar "R2013" o "R2018" ANTES de suponer que el
    # dibujo está mal. Cambiar esto NO toca geometría ni atributos, sólo el formato de archivo.
    dxfversion: Optional[str] = None
    margen_base_m: float = 80.0         # mapa base a traer alrededor del relevamiento
    # Corte SDU/MDU, sobre la UF **total** (vivienda + comercio). El cliente reserva MDU
    # para el inmueble multiunidad: en su célula de ejemplo hay 391 SDU contra 12 MDU, y
    # una casa con 2 unidades sigue siendo SDU con el sufijo "-2".
    #
    # **Tiene que ser la UF total y no la de vivienda.** Con el corte sobre vivienda sola,
    # el Shopping Várzea Grande (385 UF de comercio en una parcela — el propio plano del
    # cliente lo rotula "SHOPPING VÁRZEA GRANDE") y un hotel de 72 unidades salían como
    # SDU, con etiquetas absurdas tipo `(388)C-72`. Un inmueble con 72 unidades es un
    # edificio multiunidad aunque ninguna sea vivienda.
    #
    # Con 6 sobre UF total el survey de VG da 33 MDU sobre 567 parcelas (~6%), sobre un
    # corredor comercial. Es el parámetro a mover si el cliente cuenta con otro criterio.
    mdu_min_uf: int = 6
    solo_con_uf: bool = False
    max_parcelas: Optional[int] = None


class EntregaOutput(BaseModel):
    ok: bool
    dxf_path: Optional[str] = None
    sdu: int = 0
    mdu: int = 0
    hp_total: int = 0
    logradouros: int = 0
    quadras: int = 0
    meiofio: int = 0
    epsg: Optional[int] = None
    sin_codlog: int = 0
    geometrias_rotas: int = 0
    error: Optional[str] = None


def _norm(s: str) -> str:
    """Clave de calle para cruzar contra el CSV de CODLOG: sin tildes, sin puntuación."""
    s = unicodedata.normalize("NFKD", (s or "").upper())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]+", " ", s).strip()


def _cargar_codlog(path: Optional[str]) -> dict[str, str]:
    """CSV del cliente con el código de logradouro. Dos columnas: calle, codlog."""
    if not path:
        return {}
    mapa: dict[str, str] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for fila in csv.reader(fh):
            if len(fila) >= 2 and fila[0].strip() and fila[1].strip():
                mapa[_norm(fila[0])] = fila[1].strip()
    logger.info(f"CODLOG: {len(mapa)} calles mapeadas desde {path}")
    return mapa


def _nombre_crs(epsg: int) -> str:
    """Nombre corto del CRS como lo escribe el cliente: 'UTM84-21S', no 'WGS 84 / UTM 21S'.

    Va en la nota de auditoría porque un DXF R2000 no tiene dónde declarar el sistema de
    coordenadas, y sin eso el plano es incalzable contra los suyos.
    """
    if 32701 <= epsg <= 32760:          # WGS 84 / UTM sur
        return f"UTM84-{epsg - 32700}S"
    if 32601 <= epsg <= 32660:          # WGS 84 / UTM norte
        return f"UTM84-{epsg - 32600}N"
    if 31977 <= epsg <= 31985:          # SIRGAS 2000 / UTM sur
        return f"SIRGAS2000-{epsg - 31960}S"
    return f"EPSG:{epsg}"


def _recortar(txt: str, n: int) -> str:
    """Recorta a `n` caracteres sin partir palabras."""
    txt = (txt or "").strip()
    if len(txt) <= n:
        return txt
    corte = txt[:n].rsplit(" ", 1)[0]
    return corte or txt[:n]


def _nombre_calle(calle: str) -> str:
    """Nombre de calle como lo escribe el cliente.

    Nuestro campo `calle` guarda el tipo separado con guion —"AVENIDA - COUTO
    MAGALHAES"— mientras que en sus planos el rótulo va corrido ("AV GOV JOAO P DE
    ARRUDA"). El guion es un artefacto nuestro de parseo, no parte del nombre.
    """
    s = re.sub(r"\s*-\s*", " ", (calle or "").strip().upper())
    return re.sub(r"\s+", " ", s).strip()


def _tipo_cliente(uso: str, uf_v: int, uf_c: int) -> str:
    """TP del estándar: R residencial · C comercial · E especial/vacío.

    El cliente no tiene código para "mixto": la parcela mixta se resuelve por el uso que
    manda en unidades, que es el criterio con el que ellos cuentan HPs.
    """
    if uso in ("vacante", "baldio"):
        return "E"
    if uso == "comercial":
        return "C"
    if uso == "residencial":
        return "R"
    if (uf_c or 0) > (uf_v or 0):
        return "C"
    if (uf_v or 0) > 0:
        return "R"
    return "E"


def _etiqueta(num: str, tp: str, qt: int) -> str:
    """La única etiqueta que se dibuja. `VAZ` para el lote vacío, como en su ejemplo."""
    if tp == "E" and not num:
        return "VAZ"
    base = f"{num or 'S/N'}{tp}"
    return f"{base}-{qt}" if qt > 1 else base


def _puntos_para_dos(geom, tr, calles: list[LineString]) -> list:
    """Dos puntos de inserción dentro del lote, el primero el más cercano a la calle.

    Cuando una parcela lleva dos bloques (el local y la vivienda) no pueden ir los dos en
    el mismo punto: se pisarían el símbolo y la etiqueta. Se parte el lote en dos por su
    eje corto y se toma un punto representativo de cada mitad —representativo, no
    centroide, porque en un lote en "L" el centroide cae afuera—. El primero que se
    devuelve es el más cercano al eje de la calle, que es donde de verdad está el local:
    en esta zona de Várzea el comercio da a la vereda y la vivienda queda al fondo.

    Si el lote no se puede partir en dos partes útiles (una mitad vacía, una geometría
    rota), devuelve lista vacía y el llamador vuelve a un solo bloque: es preferible
    perder el desglose que dibujar un bloque fuera de su parcela.
    """
    minx, miny, maxx, maxy = geom.bounds
    if maxx - minx >= maxy - miny:
        corte = (minx + maxx) / 2
        cajas = (box(minx, miny, corte, maxy), box(corte, miny, maxx, maxy))
    else:
        corte = (miny + maxy) / 2
        cajas = (box(minx, miny, maxx, corte), box(minx, corte, maxx, maxy))

    puntos = []
    for caja in cajas:
        parte = geom.intersection(caja)
        if parte.is_empty or parte.area <= 0:
            return []
        rp = parte.representative_point()
        puntos.append(tr.transform(rp.x, rp.y))
    if not calles:
        return puntos
    return sorted(puntos, key=lambda xy: min(c.distance(Point(*xy)) for c in calles))


def _rotacion_a_calle(punto, calles: list[LineString]) -> float:
    """Ángulo del eje de calle más cercano, para que la etiqueta salga paralela a la vía.

    En el plano del cliente los SDU están rotados siguiendo la calle (347°, 54°, …), no
    horizontales. Sin esto el bloque de texto queda cruzado contra la manzana y el plano
    no se lee como el de ellos.
    """
    if not calles:
        return 0.0
    mejor, mejor_d = None, float("inf")
    for c in calles:
        d = c.distance(punto)
        if d < mejor_d:
            mejor, mejor_d = c, d
    if mejor is None:
        return 0.0
    s = mejor.project(punto)
    delta = max(mejor.length * 0.02, 1.0)
    p1 = mejor.interpolate(max(s - delta, 0.0))
    p2 = mejor.interpolate(min(s + delta, mejor.length))
    ang = math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x))
    # Se normaliza a [-90, 90) para que el texto nunca salga cabeza abajo.
    if ang > 90:
        ang -= 180
    elif ang <= -90:
        ang += 180
    return ang


# Palabras del `complemento` del BCI que NO identifican a nadie: unidad, posición o
# jerga catastral. Sin sacarlas, dos lotes vecinos que dicen "SALA COMERCIAL" o
# "AREA REMANESCENTE" (27 parcelas en la base) parecerían el mismo establecimiento.
_GENERICOS_COMPLEMENTO = {
    "QUADRA", "QD", "QDA", "LOTE", "LT", "SALA", "SALAS", "SL", "COMERCIAL", "SERVICOS",
    "AREA", "MATRICULA", "MAT", "ESQUINA", "CENTRO", "NORTE", "SUL", "EDIFICIO", "CASA",
    "APTO", "BLOCO", "DESMEMBRADA", "DESMEMBRAMENTO", "REMANESCENTE", "FECHADA", "LOJA",
    "PAVIMENTO", "TERREO", "FUNDOS", "FRENTE", "PARTE", "COM", "DE", "DA", "DO", "DOS",
}


def _nombre_significativo(texto: str) -> str:
    """El complemento reducido a lo que de verdad nombra un establecimiento."""
    t = unicodedata.normalize("NFKD", (texto or "").upper())
    t = "".join(c for c in t if not unicodedata.combining(c))
    palabras = [w for w in re.split(r"[^A-Z0-9]+", t)
                if w and w not in _GENERICOS_COMPLEMENTO and not w.isdigit() and len(w) > 2]
    return " ".join(palabras)


def _numeros_de_lindero(conn, survey_id: str) -> dict:
    """{cca_code: numero} para el lote SIN numerar cuyo vecino comparte el nombre.

    Un establecimiento sobre dos lotes catastrales suele tener el número en uno solo: el
    Hotel Portal da Amazônia de Várzea tiene sus 72 habitaciones en el lote que el
    municipio numeró `0` y el 400 en el lindero, que declara una única unidad. Sin esto la
    ficha del hotel —la mayor concentración de HPs de la célula— salía rotulada `(388)`,
    el número que interpola `NumeroEstimator`. Un número real del catastro es mejor que
    una interpolación, así que este vínculo gana al estimado (y va sin paréntesis).

    Guardas, porque un número equivocado es peor que ninguno: los dos lotes tienen que
    tocarse (5 m), los dos tienen que traer `complemento`, y el nombre significativo de
    uno tiene que contener al del otro con al menos 10 caracteres. Si dos linderos
    distintos proponen números distintos, no se elige: se deja sin número. Medido en VG
    el 20-ago-2026: dispara en 1 de 567 parcelas, y es exactamente el hotel.
    """
    filas = conn.execute(text("""
        SELECT a.cca_code AS insc, a.complemento AS comp,
               b.numero AS b_num, b.complemento AS b_comp
        FROM parcelas a JOIN parcelas b
          ON b.survey_id = a.survey_id AND b.parcela_id <> a.parcela_id
         AND ST_DWithin(a.geometry::geography, b.geometry::geography, 5)
        WHERE a.survey_id = :sid AND a.cca_code IS NOT NULL
          AND a.complemento IS NOT NULL AND b.complemento IS NOT NULL
          AND COALESCE(NULLIF(regexp_replace(
                COALESCE(a.numero, ''), '\\D', '', 'g'), '')::int, 0) = 0
          AND COALESCE(NULLIF(regexp_replace(
                COALESCE(b.numero, ''), '\\D', '', 'g'), '')::int, 0) > 0
    """), {"sid": survey_id}).fetchall()

    propuestas: dict = {}
    for f in filas:
        na, nb = _nombre_significativo(f.comp), _nombre_significativo(f.b_comp)
        if len(na) >= 10 and len(nb) >= 10 and (na in nb or nb in na):
            propuestas.setdefault(f.insc, set()).add(str(f.b_num))
    resueltos = {insc: nums.pop() for insc, nums in propuestas.items() if len(nums) == 1}
    if resueltos:
        logger.info(f"DXFEntrega: {len(resueltos)} lote(s) sin numerar toman el número de "
                    f"su lindero por compartir establecimiento: {sorted(resueltos.items())}")
    return resueltos


def _mapa_base(conn, fuente: str, bbox_4326, tr) -> tuple[list, list]:
    """QUADRA y MEIOFIO del MUB del cliente, recortados al entorno del relevamiento."""
    minx, miny, maxx, maxy = bbox_4326
    # `ST_Intersection` y no sólo `&&`: el operador de bbox devuelve la geometría **entera**
    # de todo lo que toque el recuadro, y una avenida de 2,7 km que apenas roza una esquina
    # se dibujaba completa, saliéndose kilómetros de la hoja. Acá se corta en el borde.
    filas = conn.execute(text("""
        SELECT capa, cerrada,
               ST_AsBinary(ST_Intersection(
                   geometry, ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, 4326)))
        FROM mapa_base_cliente
        WHERE fuente = :f
          AND geometry && ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, 4326)
    """), {"f": fuente, "minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy}).fetchall()
    quadras, meiofio = [], []
    for capa, cerrada, geom_wkb in filas:
        recorte = wkb.loads(bytes(geom_wkb))
        if recorte.is_empty:
            continue
        # El recorte puede partir una polilínea en varios tramos; cada uno va por su lado.
        partes = list(getattr(recorte, "geoms", [recorte]))
        for parte in partes:
            coords = list(getattr(parte, "coords", []))
            if len(coords) < 2:
                continue
            pts = [tr.transform(x, y) for x, y in coords]
            # Una manzana cortada por el borde deja de ser un anillo: se dibuja abierta,
            # porque cerrarla inventaría un lado que no está en el mapa del cliente.
            cerrada_ok = bool(cerrada) and len(partes) == 1 and \
                coords[0] == coords[-1]
            (quadras if capa == "QUADRA" else meiofio).append((pts, cerrada_ok))
    return quadras, meiofio


def _calles(conn, bbox_4326, tr) -> tuple[list, list]:
    """Ejes de calle del relevamiento: sirven para rotar las etiquetas y para el rótulo
    de logradouro. Devuelve (geometrías en UTM, filas con nombre/CEP).

    **Se filtra por geometría y NO por `region_id`, a propósito.** Cada relevamiento crea
    una `region_id` nueva (acá `zona-varzea-grande-actualizacion`) mientras que los
    logradouros del IBGE se cargan una vez por municipio (`vg-mt-br`). Filtrar por región
    devolvía 0 calles y el plano salía sin un solo rótulo — el mismo modo de falla que
    hace que los overrides manuales no se hereden entre relevamientos. El bbox ya acota
    el alcance, que es lo que realmente importa acá.
    """
    minx, miny, maxx, maxy = bbox_4326
    filas = conn.execute(text("""
        SELECT ST_AsBinary(geometry), tipo_logradouro, titulo_logradouro,
               nome_logradouro, COALESCE(cep_esq, cep_dir)
        FROM logradouros
        WHERE geometry && ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, 4326)
    """), {"minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy}).fetchall()
    geoms, datos = [], []
    for geom_wkb, tipo, titulo, nome, cep in filas:
        linea = wkb.loads(bytes(geom_wkb))
        if linea.is_empty or linea.length == 0:
            continue
        utm = LineString([tr.transform(x, y) for x, y in linea.coords])
        geoms.append(utm)
        # El nombre completo lleva el título (SANTO, DOUTOR…): sin él se fusionan calles
        # distintas que comparten el núcleo del nombre.
        nombre = " ".join(x for x in [tipo or "", titulo or "", nome or ""] if x).strip()
        if nombre:
            datos.append((utm, nombre.upper(), re.sub(r"\D", "", cep or "")))
    return geoms, datos


def _ejes_por_calle(por_calle: dict[str, list]) -> list[tuple[float, float, float, str, str]]:
    """Eje de cada calle deducido de las parcelas que dan a ella.

    **Por qué de las parcelas y no de `logradouros`.** Esa tabla tiene la geometría pero
    está vacía de atributos: 49 de 3.522 filas traen nombre y **ninguna** trae CEP. Usarla
    para rotular dejaba el plano sin un solo nombre de calle. Las parcelas, en cambio,
    traen `calle` y `codigo_postal` en las 567 del relevamiento.

    El eje se saca por componente principal de los puntos de las parcelas de esa calle: la
    dirección de máxima dispersión de las parcelas que dan a una calle **es** la calle. El
    rótulo va al centro de esa nube, que cae sobre la calzada porque hay parcelas de los
    dos lados — que es donde lo pone el cliente.
    """
    import numpy as np
    salida = []
    for nombre, items in por_calle.items():
        pts = np.array([(x, y) for x, y, _ in items], dtype=float)
        if len(pts) == 0:
            continue
        cx, cy = pts.mean(axis=0)
        if len(pts) >= 2:
            centrados = pts - (cx, cy)
            # autovector dominante de la covarianza = dirección de la calle
            _, _, vt = np.linalg.svd(centrados, full_matrices=False)
            ang = math.degrees(math.atan2(vt[0][1], vt[0][0]))
        else:
            ang = 0.0
        if ang > 90:
            ang -= 180
        elif ang <= -90:
            ang += 180
        ceps = [c for _, _, c in items if c]
        cep = max(set(ceps), key=ceps.count) if ceps else ""
        salida.append((float(cx), float(cy), float(ang), nombre, cep))
    return salida


@agent_run
def run(input: EntregaInput) -> EntregaOutput:
    if not _PLANTILLA.exists():
        return EntregaOutput(ok=False, error=(
            f"Falta la plantilla de bloques: {_PLANTILLA}. "
            "Generala con scripts/generar_plantilla_entrega.py"))

    rows, region_nombre, _ = _consultar(input.survey_id, input.solo_con_uf, input.max_parcelas)
    if region_nombre is None:
        return EntregaOutput(ok=False, error=f"Survey no encontrado: {input.survey_id}")
    if not rows:
        return EntregaOutput(ok=False, error="El survey no tiene parcelas con geometría.")

    geoms = [wkb.loads(bytes(r[0])) for r in rows]
    poligonos = [g for g in geoms if isinstance(g, (Polygon, MultiPolygon))]
    conjunto = MultiPolygon([p for p in poligonos if isinstance(p, Polygon)]) \
        if len(poligonos) > 1 else poligonos[0]

    # EPSG: huso UTM del centroide sobre **WGS 84** (327xx), que es lo que el cliente pidió
    # explícitamente — "UTM84-21S", o sea EPSG:32721 para Várzea Grande.
    #
    # Antes se re-rotulaba a SIRGAS 2000 (319xx) suponiendo que era la etiqueta que espera
    # un proyectista brasilero. La suposición era nuestra y resultó equivocada para BL. El
    # cambio es SÓLO de etiqueta: medido sobre las esquinas del relevamiento de VG, la
    # diferencia entre EPSG:31981 y EPSG:32721 es de **0,0 cm** — pyproj los trata como
    # alineados y no aplica desplazamiento de datum. Ninguna coordenada del plano se mueve.
    #
    # Ojo: un DXF R2000 no tiene dónde declarar el CRS (el objeto GeoData recién aparece en
    # AC1024), así que el único lugar donde el dato viaja es la nota de auditoría de abajo.
    # El propio DWG del cliente tampoco lo declara — hubo que deducirlo transformando sus
    # esquinas. Por eso la nota no es decorativa.
    if input.epsg:
        epsg = input.epsg
    else:
        epsg = geo.utm_epsg(conjunto.centroid.x, conjunto.centroid.y)
    tr = Transformer.from_crs(4326, epsg, always_xy=True)

    codlog_map = _cargar_codlog(input.codlog_csv)

    # **La hoja se dimensiona por los puntos de rotulado, NO por los polígonos.**
    # Una sola geometría catastral rota arruina la hoja entera: en VG hay una parcela de
    # `AVENIDA - GOV. JOAO PONCE DE ARRUDA` sin número de **4.606.312 m²** con 311
    # vértices —190 veces la siguiente más grande, que tiene 24.079— y con ella el dibujo
    # salía de 3.845 × 5.244 m para un relevamiento que ocupa 1.708 × 1.619 m: un triángulo
    # enorme cruzando el plano, kilómetros de mapa base traídos de más, y un `LIMITE` de
    # 5,31 km² afirmando cobertura donde no la hay.
    #
    # El punto de rotulado de cada parcela sí es sano (cae dentro del polígono, y el de la
    # parcela rota cae dentro del área real), así que la nube de puntos describe el
    # relevamiento sin que un outlier pueda estirarla.
    puntos = [g.representative_point() for g in geoms
              if isinstance(g, (Polygon, MultiPolygon)) and not g.is_empty]
    nube = MultiPoint(puntos)
    minx, miny, maxx, maxy = nube.bounds
    dg = input.margen_base_m / 111_320.0
    bbox = (minx - dg, miny - dg, maxx + dg, maxy + dg)

    # Se avisa de las geometrías desproporcionadas: no se descartan (el dato de la parcela
    # es real, lo roto es el polígono) pero quien revisa el plano tiene que saber que están.
    areas = sorted(g.area for g in geoms if not g.is_empty)
    mediana = areas[len(areas) // 2] if areas else 0
    rotas = [g for g in geoms if mediana and g.area > mediana * 200]
    if rotas:
        logger.warning(f"{len(rotas)} parcela(s) con geometría desproporcionada "
                       f"(> 200x la mediana). No definen la hoja, pero sí se dibujan.")

    engine = get_engine()
    with engine.connect() as conn:
        quadras, meiofio = _mapa_base(conn, input.fuente_mapa_base, bbox, tr)
        calles_geom, calles_datos = _calles(conn, bbox, tr)
        num_lindero = _numeros_de_lindero(conn, input.survey_id)

    if not quadras and not meiofio:
        logger.warning(
            f"El mapa base '{input.fuente_mapa_base}' no cubre este relevamiento: el plano "
            f"sale sin QUADRA ni MEIOFIO. Cargalo con scripts/cargar_mapa_base.py")

    doc = ezdxf.readfile(str(_PLANTILLA))   # trae bloques, capas y estilos del cliente
    if input.dxfversion:
        doc.dxfversion = ezdxf.const.acad_release_to_dxf_version.get(
            input.dxfversion.upper(), input.dxfversion.upper())
    msp = doc.modelspace()

    # 1. Mapa base del cliente, tal cual él lo dibuja.
    for pts, cerrada in quadras:
        msp.add_lwpolyline(pts, close=cerrada, dxfattribs={"layer": "QUADRA"})
    for pts, cerrada in meiofio:
        msp.add_lwpolyline(pts, close=cerrada, dxfattribs={"layer": "MEIOFIO"})

    # 2. Límite de la célula relevada.
    #
    # **Envolvente cóncava, no convexa.** La `LIMITE` del cliente es el contorno real de
    # la célula que entrega (44 vértices en su ejemplo). Un relevamiento con forma de arco
    # —como el de VG, que sigue dos avenidas— tiene una envolvente convexa que encierra
    # kilómetros de terreno no relevado: el plano salía con un triángulo enorme cruzando
    # el medio, afirmando cobertura donde no la hay. `ratio=0.15` sigue el contorno sin
    # deshilacharse en las parcelas sueltas; si falla, se cae a la convexa antes que
    # quedarse sin límite.
    nube_utm = MultiPoint([Point(*tr.transform(p.x, p.y)) for p in puntos])
    try:
        limite = shapely.concave_hull(nube_utm, ratio=0.12)
    except Exception as e:
        logger.warning(f"concave_hull falló ({e}); se usa la envolvente convexa")
        limite = nube_utm.convex_hull
    if not isinstance(limite, Polygon) or limite.is_empty:
        limite = nube_utm.convex_hull
    # El límite se sale 30 m de los puntos de rotulado para envolver el lote entero, no
    # su centro: si no, la línea corta las parcelas del borde por la mitad.
    if isinstance(limite, Polygon):
        limite = limite.buffer(30.0)
    if isinstance(limite, Polygon):
        msp.add_lwpolyline([tr.transform(x, y) for x, y in limite.exterior.coords],
                           close=True, dxfattribs={"layer": "LIMITE"})

    # 3. Un bloque por inmueble.
    n_sdu = n_mdu = hp_total = sin_codlog = 0
    por_calle: dict[str, list] = {}
    for row, geom in zip(rows, geoms):
        (_, calle, numero, complemento, barrio, cep, uf_v, uf_c, _uf_fuente, uso,
         cca, _area_t, area_c, pisos, num_est, num_est_conf, _categoria, descripcion,
         _est_id, _est_tipo, _est_nombre, tipo_manual, hotel_tipo, huella,
         pisos_sat) = row

        if not isinstance(geom, (Polygon, MultiPolygon)) or geom.is_empty:
            continue

        # representative_point cae SIEMPRE dentro del polígono; el centroide de una
        # parcela en "L" puede caer afuera y el bloque quedaría sobre el vecino.
        rp = geom.representative_point()
        cx, cy = tr.transform(rp.x, rp.y)

        uf_v, uf_c = int(uf_v or 0), int(uf_c or 0)
        qt = max(uf_v + uf_c, 1)
        tp = _tipo_cliente(uso or "", uf_v, uf_c)

        # Número: el del municipio y, si no lo declaró, el estimado **entre paréntesis**.
        # Los paréntesis no son decorativos: es la marca con la que el proyecto distingue
        # el dato del catastro del que dedujo NumeroEstimator, y sale también en el CSV.
        # Sin número de ninguna fuente va `S/N`, que es la convención del propio cliente.
        num = str(numero) if numero not in (None, "", 0, "0") else ""
        # El número real del lindero que comparte establecimiento gana al estimado: es
        # dato del catastro, no una interpolación, y por eso va sin paréntesis.
        if not num:
            num = num_lindero.get(cca or "", "")
        if not num and num_est and (num_est_conf or 0) >= 0.4:
            num = f"({num_est})"

        es_mdu = (uf_v + uf_c) >= input.mdu_min_uf

        # UN BLOQUE POR EDIFICACIÓN, NO POR LOTE — la convención del propio cliente.
        # Leído del DWG de ejemplo con `dwgread -O JSON`: 46 de sus 186 números de puerta
        # aparecen más de una vez, y con tipos distintos en el mismo número (el 78 sale
        # `C, R, R, R`; el 114 `R, C, R-2`; el 330 `R, C, C`). O sea: la casa con el local
        # adelante son DOS bloques, uno `R` y otro `C`, no uno con el tipo de la mayoría.
        #
        # Con un bloque por lote, las 83 parcelas mixtas de VG entregaban 78 comercios
        # rotulados como unidades residenciales y 30 viviendas como comerciales, y en las
        # 30 empatadas el tipo lo decidía el orden de los `if` de `_tipo_cliente`.
        #
        # **El desglose sale de `uf_vivienda`/`uf_comercio` de la parcela, NO de
        # `parcela_unidades`.** Es la guarda que impide perder HPs: el Hotel Portal da
        # Amazônia tiene 72 UF de comercio traídas de Cadastur y sólo 2 unidades declaradas
        # en el BCI — repartir por unidades le borraría 70 habitaciones. Así, la suma de
        # los QT de los bloques es siempre la UF de la parcela.
        #
        # Sólo se parte a nivel SDU. La ficha MDU describe UN edificio (trae `HP_TOTAL` y
        # `QTD_ANDARES`, no `TP`), y partir en dos fichas un edificio de 72 habitaciones
        # sería peor que dejarlo entero: el tipo del MDU sigue viajando en la capa.
        bloques = [(tp, qt, (cx, cy))]
        if not es_mdu and tp != "E" and uf_v > 0 and uf_c > 0:
            puntos = _puntos_para_dos(geom, tr, calles_geom)
            if puntos:      # sin dos puntos utilizables se deja el bloque único
                bloques = [("C", uf_c, puntos[0]), ("R", uf_v, puntos[1])]

        # Lo que es de la PARCELA y no del bloque se cuenta una sola vez: el CEP para el
        # rótulo de calle y la guarda de CODLOG, que mide parcelas sin código, no bloques.
        cep_num = re.sub(r"\D", "", cep or "")
        if calle:
            por_calle.setdefault(_nombre_calle(calle), []).append((cx, cy, cep_num))
        codlog = codlog_map.get(_norm(calle or ""), "")
        if not codlog:
            sin_codlog += 1

        for tp_b, qt_b, (bx_bloque, by_bloque) in bloques:
            sufijo = {"R": "RES", "C": "COM", "E": "ESP"}[tp_b]
            if not es_mdu and tp_b == "E":
                sufijo = "VAZ"     # el lote vacío tiene capa propia en el estándar
            capa = f"{'MDU' if es_mdu else 'SDU'}_{sufijo}"
            rot = _rotacion_a_calle(Point(bx_bloque, by_bloque), calles_geom)
            ref = msp.add_blockref("MDU" if es_mdu else "SDU", (bx_bloque, by_bloque),
                                   dxfattribs={"layer": capa, "rotation": rot})
            # Cada bloque lleva SU coordenada: son puntos distintos del mismo lote.
            utm_txt = f"{int(round(bx_bloque))},{int(round(by_bloque))}"

            if es_mdu:
                ref.add_auto_attribs({
                    "NUMERO": num or "S/N",
                    "HP_TOTAL": str(uf_v + uf_c),
                    # Manda el catastro si lo declara y, si no, los pisos que ve el satélite
                    # (`parcela_altura`, AlturaFetcher). `pisos_estimados_max` está vacío en las
                    # 567 parcelas de VG, así que sin este respaldo el campo salía en blanco en
                    # las 33 fichas — y es VISIBLE en la ficha del cliente, que lo llena en sus
                    # 12 de ejemplo. Con el respaldo se cubren 26 de las 33. Si no hay ninguno
                    # de los dos sigue saliendo vacío: no se inventa una altura.
                    #
                    # La base cuenta NIVELES (planta baja sola = 1) porque ahí el número
                    # multiplica; el entregable va en PISOS SOBRE PLANTA BAJA (PB = 0).
                    # La conversión vive en `pisos.py`, que documenta las dos convenciones.
                    "QTD_ANDARES": pisos_texto(pisos or pisos_sat),
                    "BLOCO": "UNICO",
                    "ESTABELECIMENTO": "",
                    "CODLOG": codlog,
                    "CEP": cep_num,
                    "UTM": utm_txt,
                    "NOME": "",
                    "CLASSE_SOCIAL": "",
                    "CODGED": "",
                    "ID": input.celula_id,
                })
                n_mdu += 1
            else:
                ref.add_auto_attribs({
                    "N_C1_3_TP_QT": _etiqueta(num, tp_b, qt_b),
                    "N": num or "S/N",
                    # Corte en límite de palabra: `[:32]` partía "…QD 4 LO" a mitad de palabra.
                    "C1": _recortar(complemento or "", 48),
                    "C2": "",
                    "C3": "",
                    "TP": tp_b,
                    "QT": str(qt_b),
                    "CODLOG": codlog,
                    "CEP": cep_num,
                    "UTM": utm_txt,
                    "NOME": "",
                    "CLASSE_SOCIAL": "",
                    # VACÍO A PROPÓSITO, como en el plano del cliente: leído del DWG original
                    # con `dwgread -O JSON` (el DXF convertido sólo deja ver 24 de los 391
                    # bloques), `TIPOIMOVEL` viene presente pero vacío en **0/388** SDU, igual
                    # que NOME, CLASSE_SOCIAL y CODGED. Llenarlo con nuestra taxonomía además
                    # contradecía la etiqueta visible en 92 bloques de los 534 de VG: el
                    # rótulo sale del uso y las UF, y `_tipo_edificacion` manda todo lo mixto a
                    # comercio y le da prioridad al rubro del CNPJ. Medido el 20-ago-2026.
                    "TIPOIMOVEL": "",
                    "ID": input.celula_id,
                    "CODGED": "",
                })
                n_sdu += 1
        hp_total += uf_v + uf_c

    # `add_auto_attribs` copia el ATTDEF pero no siempre arrastra el bit de invisibilidad;
    # sin esta pasada los 14 atributos de dato se dibujarían encima del plano.
    _VIS = {"SDU": {"N_C1_3_TP_QT"},
            "MDU": {"NUMERO", "HP_TOTAL", "QTD_ANDARES", "BLOCO"},
            "LOGRADOURO": {"LOGRADOURO"}}
    for ins in msp.query("INSERT"):
        visibles = _VIS.get(ins.dxf.name)
        if visibles is None:
            continue
        for a in ins.attribs:
            oculto = a.dxf.tag not in visibles
            a.dxf.invisible = 1 if oculto else 0
            a.dxf.flags = (a.dxf.flags & ~1) | (1 if oculto else 0)

    # 4. Rótulo de calle sobre el eje, rotado como la vía (bloque LOGRADOURO).
    n_log = 0
    ejes = _ejes_por_calle(por_calle)
    for cx_c, cy_c, ang, nombre, cep_calle in ejes:
        ref = msp.add_blockref("LOGRADOURO", (cx_c, cy_c), dxfattribs={
            "layer": "LOGRADOURO",
            "rotation": ang,
        })
        ref.add_auto_attribs({
            "LOGRADOURO": nombre,
            "CODLOG": codlog_map.get(_norm(nombre), ""),
            "CEP": cep_calle,
        })
        for a in ref.attribs:
            oculto = a.dxf.tag != "LOGRADOURO"
            a.dxf.invisible = 1 if oculto else 0
            a.dxf.flags = (a.dxf.flags & ~1) | (1 if oculto else 0)
        n_log += 1

    # 5. Títulos de célula, con las alturas del ejemplo (30 y 25 m).
    # Se cuelgan **debajo** de todo lo dibujado: puestos en la esquina del bbox caían
    # encima de las manzanas y se leían pisados contra el plano.
    lx0, ly0, _lx1, _ly1 = limite.bounds if isinstance(limite, Polygon) else nube_utm.bounds
    bx, by = lx0, ly0 - _H_CELULA * 1.5
    if input.celula_id:
        msp.add_text(input.celula_id, dxfattribs={
            "layer": "SERVICO", "height": _H_CELULA}).set_placement((bx, by - _H_CELULA * 2))
    msp.add_text(f"{hp_total} HPS", dxfattribs={
        "layer": "0", "height": _H_RESUMEN}).set_placement((bx, by - _H_CELULA * 4))

    # Nota de auditoría: un DXF sin CRS declarado es incalzable contra los planos del
    # cliente y nadie puede saber con qué corrida salió.
    nota = (f"AI Mapping - {region_nombre} | survey {input.survey_id} | "
            f"{datetime.now():%Y-%m-%d %H:%M} | CRS {_nombre_crs(epsg)} (EPSG:{epsg}) | "
            f"unidades: metros | "
            f"{n_sdu} SDU + {n_mdu} MDU | {hp_total} HPs | "
            f"base {input.fuente_mapa_base} | "
            f"numero entre parentesis = estimado (el municipio no lo declaro)")
    msp.add_text(nota, dxfattribs={"layer": "ANOTACAO", "height": _H_META}) \
       .set_placement((bx, by - _H_CELULA * 5))

    out = Path(input.output_path) if input.output_path else Path(
        f"/tmp/entrega_{input.survey_id[:8]}.dxf")
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(out)

    logger.info(f"DXFEntrega {input.survey_id}: {n_sdu} SDU + {n_mdu} MDU, {hp_total} HPs, "
                f"{n_log} logradouros, base {len(quadras)}q/{len(meiofio)}m, "
                f"EPSG:{epsg} -> {out}")

    return EntregaOutput(
        ok=True, dxf_path=str(out), sdu=n_sdu, mdu=n_mdu, hp_total=hp_total,
        logradouros=n_log, quadras=len(quadras), meiofio=len(meiofio),
        epsg=epsg, sin_codlog=sin_codlog, geometrias_rotas=len(rotas))
