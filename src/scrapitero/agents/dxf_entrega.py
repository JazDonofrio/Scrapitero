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
import difflib
import math
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

import ezdxf
import shapely
from ezdxf import bbox as ezbbox
from loguru import logger
from pydantic import BaseModel
from pyproj import Transformer
from shapely import wkb
from shapely.geometry import LineString, MultiPoint, MultiPolygon, Point, Polygon, box
from shapely import voronoi_polygons
from shapely.ops import (linemerge, nearest_points, polygonize, polylabel,
                         unary_union)
from shapely.strtree import STRtree
from sqlalchemy import text

from scrapitero.agents import geo
from scrapitero.agents._run import agent_run
from scrapitero.agents.dxf_export import _consultar
from scrapitero.agents.logradouro_br import limpiar_nombre_via
from scrapitero.agents.pisos import texto as pisos_texto
from scrapitero.db.engine import get_engine

_PLANTILLA = Path(__file__).resolve().parents[1] / "assets" / "plantilla_entrega_bl.dxf"

# Alturas de texto del estándar, en metros de modelspace (medidas sobre el DWG del cliente).
_H_CELULA = 30.0     # título de la célula, capa SERVICO
_H_RESUMEN = 25.0    # "1133 HPS" / "6,219 KM", capa 0
_H_META = 2.0        # nuestra nota de auditoría


# Separación vertical, en metros de modelspace, entre el rótulo del SDU y los pisos.
# 2 m es el paso de línea del propio bloque (sus atributos ocultos van cada ~2,7 m con
# texto de 2,0; el rótulo visible mide 1,5).
_SALTO_PISOS_SDU = 2.0
# Cuánto se estira el recorte para traer la manzana de ENFRENTE: el ancho de una calle
# con sus dos veredas. Una unidad del borde necesita esa cara dibujada aunque adentro no
# haya nada relevado. Más que esto empieza a arrastrar manzanas de barrios vecinos.
_SALTO_VECINA_M = 45.0

# Una polilínea ABIERTA a la que sólo le falta un lado se cierra; una cadena de borde no.
# El discriminante es el hueco entre extremos contra el LADO MÁS LARGO que el cliente sí
# dibujó: si lo que falta no es más largo que un lado suyo, es un lado que no trazó. En
# Várzea separa sin ambigüedad —los lotes dan 0,15-0,74 y las cadenas 1,11-3,17—. Cerrar
# TODO lo abierto es el error que ya costó 190 polígonos cruzando lotes.
_RATIO_LADO_FALTANTE = 1.05

# Relleno de manzana en modo calco: una cara de otra fuente entra sólo donde el cliente no
# dibujó anillo. Un solape mayor a esto significa que él SÍ la tiene y sería doble línea.
# A partir de esta prioridad la fuente es un DIBUJO DEL CLIENTE (ACV_ y AC_). Lo suyo se
# copia ENTERO: ni recorte al relevamiento, ni corte contra la hoja, ni filtro de calle
# adentro. Pedido explícito de Jaz: "copiá todas sus quadra y pegalas arriba de las
# nuestras". Los filtros siguen valiendo para OSM y el MUB, que sí meten ruido.
_PRIO_CLIENTE = 3
_SOLAPE_MAX_RELLENO = 0.10
_AREA_MIN_MANZANA_RELLENO = 2_000.0   # m² — por debajo es un cantero, no una manzana


def _atributo_clonado(blk, modelo_tag: str, nuevo_tag: str, salto: float) -> bool:
    """Agrega un atributo visible clonando otro del mismo bloque, `salto` metros más abajo.

    Se clona en vez de crear uno nuevo para heredar exactamente el estilo, la altura, el
    color `ByBlock` y la alineación del bloque del cliente. `valign=MIDDLE` hace que la
    posición real la mande `align_point` y no `insert`: hay que bajar los dos.
    """
    if blk is None or any(a.dxf.tag == nuevo_tag for a in blk.query("ATTDEF")):
        return False
    modelo = next((a for a in blk.query("ATTDEF") if a.dxf.tag == modelo_tag), None)
    if modelo is None:
        logger.warning(f"El bloque no tiene {modelo_tag}: no se agrega {nuevo_tag}.")
        return False
    nuevo = modelo.copy()
    nuevo.dxf.tag = nuevo_tag
    nuevo.dxf.text = ""
    nuevo.dxf.insert = (modelo.dxf.insert.x, modelo.dxf.insert.y - salto)
    if modelo.dxf.hasattr("align_point"):
        nuevo.dxf.align_point = (modelo.dxf.align_point.x,
                                 modelo.dxf.align_point.y - salto)
    blk.add_entity(nuevo)
    return True


def _sumar_pisos_al_sdu(doc) -> None:
    """Agrega `QTD_ANDARES` al bloque SDU, que en el DWG del cliente no lo tiene.

    **Es la única desviación deliberada de su plantilla.** Su `SDU` trae 15 atributos y
    ninguno es de pisos: el dato sólo viaja en el `MDU`, al lado de `HP_TOTAL`. Pero el
    piso es dato de la parcela, no del edificio grande, y sin esto 612 de los 645 inmuebles
    entregan el relevamiento sin él.

    Se clona el atributo visible que ya existe (`N_C1_3_TP_QT`) en vez de crear uno nuevo,
    para heredar exactamente su estilo, altura, color `ByBlock` y alineación; sólo baja
    una línea. Si el cliente contesta que no lo quiere, se saca esta llamada y la plantilla
    vuelve a ser la suya, sin tocar el archivo de assets.
    """
    _atributo_clonado(doc.blocks.get("SDU"), "N_C1_3_TP_QT", "QTD_ANDARES",
                      _SALTO_PISOS_SDU)
    # La línea legible (`2V 1C PB`), lo que Jaz pidió ver en texto. Va en los dos bloques,
    # una línea más abajo que los pisos.
    # ⚠ Los renglones VISIBLES tienen que quedar a paso uniforme, porque así los modela
    # `_caja_grupo` para decidir si la ficha entra en el lote. Estaban a 0, −4 y −6 y el
    # modelo suponía 0, −2, −4: la pieza se medía 2 m más corta de lo que se dibujaba y el
    # renglón de pisos se salía. `QTD_ANDARES` comparte el −2 con `RESUMO`, pero es
    # invisible y no molesta.
    _atributo_clonado(doc.blocks.get("SDU"), "N_C1_3_TP_QT", "RESUMO",
                      _SALTO_PISOS_SDU)
    _atributo_clonado(doc.blocks.get("MDU"), "HP_TOTAL", "RESUMO", _SALTO_PISOS_SDU)
    # Y los pisos, un renglón más abajo que las unidades.
    # Segundo y último renglón visible del SDU: comparte posición con `RESUMO`, que
    # quedó de dato. Jaz pidió dos renglones, no tres.
    _atributo_clonado(doc.blocks.get("SDU"), "N_C1_3_TP_QT", "PISOS",
                      _SALTO_PISOS_SDU)
    _atributo_clonado(doc.blocks.get("MDU"), "HP_TOTAL", "PISOS", _SALTO_PISOS_SDU * 2)




# Color de los trazos de manzana y de lote. **La plantilla del cliente los trae en 8**
# (gris oscuro); Jaz pidió que se vean como el plano de Hurlingham que mandó Diego, que
# dibuja las parcelas en **7**, continuo, con grosor por defecto. Es una desviación
# deliberada de su plantilla y se revierte cambiando este número.
_COLOR_TRAZOS = 7


def _estilo_trazos(doc) -> None:
    """Iguala el trazo de manzana y lote al del plano de Hurlingham: color 7, continuo."""
    for capa in ("QUADRA", "DIV"):
        if doc.layers.has_entry(capa):
            l = doc.layers.get(capa)
            l.dxf.color = _COLOR_TRAZOS
            l.dxf.linetype = "Continuous"


def _coordenadas_no_finitas(path: Path) -> list[str]:
    """Valores `inf`/`nan` en el DXF emitido, ubicados por entidad y capa.

    Un real no finito es válido para Python y para ezdxf, pero no para AutoCAD: alcanza
    uno para que descarte el dibujo entero. La fuente típica es una reproyección fuera de
    dominio —pasar por el `Transformer` algo que ya estaba proyectado— que pyproj resuelve
    devolviendo `inf` en vez de fallar.
    """
    # El DXF ASCII alterna código y valor, una línea cada uno: el par (i, i+1).
    lineas = path.read_text(encoding="latin-1").splitlines()
    malas: list[str] = []
    tipo = capa = "?"
    for i in range(0, len(lineas) - 1, 2):
        codigo, valor = lineas[i].strip(), lineas[i + 1].strip()
        if codigo == "0":
            tipo, capa = valor, "?"
        elif codigo == "8":
            capa = valor
        if valor.lower().lstrip("+-") in ("inf", "nan", "infinity"):
            malas.append(f"{tipo} capa={capa} linea={i + 2}")
    return malas


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
    # Contadores `HCSDU`/`HCMDU` — el circulito con la cantidad de unidades de la parcela.
    # Son del estándar del cliente (145 + 12 en su plano de ejemplo) pero Jaz los sacó del
    # dibujo: su número repite lo que ya dice el rótulo cuando hay más de una unidad, y el
    # círculo ensucia el lote. Se apagan acá, no se borra el código: vuelven con un flag.
    contadores: bool = False
    # Capa `MEIOFIO` — el cordón punteado del MUB municipal. Apagada a pedido de Jaz: el
    # plano de Hurlingham no tiene nada equivalente y en la hoja compite con el trazo de
    # manzana. Se dibuja igual con `meiofio=True`; los tramos se siguen leyendo de la base
    # porque sirven para ubicar la manzana aunque no se rotulen.
    meiofio: bool = False
    # Capa `LIMITE` — el contorno de la célula relevada. Es una polilínea única que cruza
    # por el medio de las manzanas, así que Jaz la sacó del dibujo. Se enciende con
    # `limite_celula=True`; el CRS y el alcance siguen en la nota de auditoría.
    limite_celula: bool = False


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
    divisiones: int = 0
    contadores: int = 0
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

    **Y sin la basura de la etiqueta de origen.** El BCI mete el "S/N" del número dentro
    del propio nombre y deja puntos sueltos al final (ver [[bci-etiquetas-calle-no-
    confiables]]): así "RUA DA LIBERDADE" y "RUA DA LIBERDADE ." salían como dos calles
    distintas y el tope de dos rótulos por calle daba cuatro, encimados a 20 m.
    """
    # La limpieza vive en `logradouro_br.limpiar_nombre_via`, que es la que usa también el
    # CSV Operadora: una sola definición de "nombre de vía limpio" para plano y entregable.
    s = limpiar_nombre_via(calle)
    return re.sub(r"\s+", " ", re.sub(r"\s*-\s*", " ", s)).strip(" .,;-")


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


# Retiro del bloque respecto del borde de manzana, en metros. **Medido sobre el DWG del
# cliente, no elegido**: con `dwgread -O JSON` sobre sus 403 bloques, 353 caen a 3,0 ± 0,5 m
# de la línea `QUADRA` (y 350 a 6,0 m del `MEIOFIO`, que corre 3 m más afuera).
_RETIRO_QUADRA = 3.0

# Más lejos que esto, el borde de manzana más cercano no es el frente del lote: o el mapa
# base no cubre la zona, o la parcela es interior. Ahí se deja el punto de la parcela.
_MAX_BUSQUEDA_FRENTE = 60.0

# Separación a lo largo del frente entre los dos bloques de una parcela mixta (el local y
# la vivienda). Sin esto los dos caen en el mismo punto del cordón y se tapan.
_SEPARACION_BLOQUES = 4.0

# Cuánto entra el nombre de calle en la manzana. Mediana medida sobre los 39 rótulos del
# cliente: 14,4 m del borde, todos adentro del polígono de manzana.
_RETIRO_LOGRADOURO = 14.0

# Hueco sobre la misma vereda que separa una cara de manzana de la siguiente. Con lotes de
# ~10 m de frente, 60 m sólo se abre en una bocacalle.
_CORTE_CARA = 60.0

# Lotes por tramo de fila. Con frentes de ~10 m, 14 lotes son ~140 m: el largo de una
# manzana. Más que eso y la curva de la avenida tuerce la alineación.
_LOTES_POR_CARA = 14

# Cuánto se toleran dos filas pisándose antes de achicarles el fondo, cuántas vueltas de
# ajuste se dan, y hasta dónde se puede achicar.
_SOLAPE_TOLERADO = 0.05
_VUELTAS_AJUSTE = 6
_FACTOR_FONDO_MIN = 0.35

# Lado de manzana demasiado corto para ser una cara: los chaflanes de esquina del MUB
# miden 2-3 m y apoyar una fila entera sobre uno de ellos la deja cruzada.
_LADO_MIN_MANZANA = 12.0

# Radio de búsqueda de la línea de manzana para una cara sin anillo cerrado.
_BUSQUEDA_BORDE = 90.0

# Área mínima para considerar manzana a un anillo del MUB, y cuánto de la parcela tiene
# que caer adentro para darla por perteneciente a esa manzana.
_AREA_MIN_MANZANA = 300.0

# Cuánto puede recorrer un eje de calle por dentro de una cara antes de que deje de ser
# una manzana. 20 m tolera que la calle apenas roce una esquina.
_CALLE_ADENTRO = 20.0

# Área mínima para que un recorte cuente como lote y no como astilla de digitalización.
_AREA_MIN_LOTE = 12.0

# Cuánto puede crecer un lote al absorber el sobrante de su manzana, sobre su superficie
# catastral real. Más que esto y deja de ser el lote para pasar a ser media manzana.
_CRECIMIENTO_MAX = 2.5

# Cuánto de nuestra parcela tiene que caer dentro de un lote del DWG del cliente para
# darla por suya. Su dibujo y el catastro no coinciden al centímetro; con un tercio alcanza
# para no confundirse de lote, y de hecho el 96% de las parcelas supera el 80%.
_SOLAPE_MIN_LOTE = 0.33

# Cuánto puede moverse una ficha para caer en el punto que le dio el cliente. Con su plano
# el salto típico es de centímetros; 120 m tolera la avenida de lote profundo sin aceptar un
# cruce de rótulo repetido que en realidad es otra parcela a tres cuadras.
_SALTO_MAX_A_FICHA = 120.0

# Cierre morfológico del contorno de manzana: rellena las muescas entre fondos de lote que
# no están alineados. 4 m tapa el diente de sierra típico sin deformar la manzana.
_SUAVIZADO_BORDE = 4.0
_SIMPLIFICA_BORDE = 0.4

# Pasos de bisección para encontrar el corte que deja la rebanada con el área pedida. Con
# 18 el error queda por debajo del centímetro sobre una manzana de 200 m.
_PASOS_BISECCION = 18
_MIN_EN_MANZANA = 0.30
# Distancia máxima a la que se adopta la manzana MÁS CERCANA cuando ninguna solapa lo
# suficiente. La manzana es el contenedor: el lote se dibuja adentro de ella y es una guía,
# así que una parcela sin manzana no puede quedar suelta en la calle. Medido en la zona
# piloto del 31-ago-2026 con el MUB municipal: 77 de 409 parcelas no solapaban NINGUNA
# manzana —el catastro y el MUB son dos relevamientos distintos y no encajan—, y las 77
# estaban a menos de 43,7 m (media 21,8). Con 50 m se asignan las 409 sin forzar nada:
# el salto siguiente sería a otra manzana, mucho más lejos.
_DIST_MAX_MANZANA = 50.0

# Hueco por debajo del cual una cadena que da la vuelta se considera cerrada. Medido: en
# este relevamiento sólo 7 cadenas caen ahí; la mediana de las abiertas de verdad es 76 m.
_HUECO_REDONDEO = 1.0


def _geom_utm(geom, tr):
    """La parcela proyectada a UTM, como un polígono válido. `None` si no sirve."""
    partes = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
    mejor = None
    for parte in partes:
        if not isinstance(parte, Polygon) or parte.is_empty:
            continue
        try:
            poli = Polygon([tr.transform(x, y) for x, y in parte.exterior.coords])
        except Exception:
            continue
        if not poli.is_valid:
            poli = poli.buffer(0)
        if isinstance(poli, MultiPolygon):
            poli = max(poli.geoms, key=lambda g: g.area)
        if poli.is_empty or poli.area <= 0:
            continue
        # De un multipolígono se dibuja y se rotula la parte grande: las astillas de
        # digitalización no son lotes.
        if mejor is None or poli.area > mejor.area:
            mejor = poli
    return mejor


def _frente_de_parcela(poli: Polygon, calles: list[LineString]):
    """El frente del lote: dónde da a la calle, y con qué orientación.

    **Se ancla a la parcela, no al mapa municipal.** El `MUB` del cliente no dibuja el
    frente de los lotes: sus líneas `QUADRA` **atraviesan** 437 de nuestras 567 parcelas,
    y son cadenas abiertas —702 líneas dan sólo 51 caras cerradas en este corredor—, así
    que "3 m adentro del borde de manzana" mandaba fichas a la calzada o encima de otra.
    La parcela, en cambio, es geometría sana: mediana de 452 m² y sólo 2 pares solapados
    en 567.

    Devuelve `(punto del frente, dirección de la calle, normal hacia adentro del lote)`.
    """
    centro = poli.representative_point()
    if not calles:
        return None
    calle = min(calles, key=lambda c: c.distance(centro))
    # El punto del borde del lote más cercano al eje de la calle: ahí da el frente.
    s = calle.project(centro)
    sobre_eje = calle.interpolate(s)
    frente = nearest_points(poli.exterior, sobre_eje)[0]

    # La orientación la manda la calle, no el zigzag del borde catastral: es lo que hace
    # que los rótulos de una cuadra salgan alineados entre sí y no cada uno a su ángulo.
    delta = max(calle.length * 0.01, 1.0)
    a = calle.interpolate(max(s - delta, 0.0))
    b = calle.interpolate(min(s + delta, calle.length))
    ux, uy = b.x - a.x, b.y - a.y
    largo = math.hypot(ux, uy)
    if largo < 1e-9:
        return None
    ux, uy = ux / largo, uy / largo
    nx, ny = -uy, ux
    if (centro.x - frente.x) * nx + (centro.y - frente.y) * ny < 0:
        nx, ny = -nx, -ny
    return frente, (ux, uy), (nx, ny)


def _punto_adentro(poli: Polygon, frente, direccion, normal,
                   retiro: float, corrimiento: float = 0.0):
    """Un punto a `retiro` del frente que cae **dentro** del lote, pase lo que pase.

    Entrar una distancia fija no alcanza: en un lote de 4 m de fondo, 3 m adentro puede
    caer fuera por la forma del polígono. Se prueba el retiro pedido y se va acortando; si
    ninguno entra, se usa el punto representativo, que por definición está adentro. Así
    **ninguna ficha puede terminar en mitad de la calle**, que es de donde venía el
    desorden.
    """
    ux, uy = direccion
    nx, ny = normal
    for factor in (1.0, 0.6, 0.35, 0.15):
        x = frente.x + nx * retiro * factor + ux * corrimiento
        y = frente.y + ny * retiro * factor + uy * corrimiento
        if poli.contains(Point(x, y)):
            return x, y
    rp = poli.representative_point()
    return rp.x, rp.y


def _falta_un_lado(pts) -> bool:
    """¿Es un anillo al que le falta un lado, o una cadena de borde abierta de verdad?"""
    if len(pts) < 3:
        return False
    hueco = math.dist(pts[0], pts[-1])
    lado_max = max(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    return bool(lado_max) and hueco / lado_max <= _RATIO_LADO_FALTANTE


def _cadenas_de_mapa_base(tramos, recorte=None) -> list[tuple[list, bool]]:
    """Une los tramos sueltos del MUB en cadenas continuas y cierra las que son anillos.

    El mapa base del municipio guarda cada manzana como **decenas de líneas sueltas** —en
    el entorno de este relevamiento, 702 tramos de `QUADRA`— y el recorte al bbox las
    partía todavía más: el plano salía con 112 pedazos de contorno, ninguno cerrado, que
    es lo que se ve como manzanas abiertas. `linemerge` las vuelve a coser: 588 cadenas,
    y las que dan la vuelta entera se dibujan cerradas.
    """
    # Las que el DWG ya declara cerradas se respetan tal cual: son las manzanas del
    # municipio y no hay nada que coser. Sólo se cosen los tramos sueltos.
    salida = []
    sueltos = []
    for pts, cerrada, *_r in tramos:
        if cerrada and len(pts) >= 3:
            anillo = Polygon(pts)
            if recorte is None or anillo.intersects(recorte):
                salida.append((pts, True))
        elif len(pts) >= 2:
            sueltos.append(LineString(pts))
    if not sueltos:
        return salida
    unidas = linemerge(sueltos)
    for parte in getattr(unidas, "geoms", [unidas]):
        # Recorte tardío: una cadena que se va kilómetros de la hoja se corta ahora, ya
        # cosida, en vez de cortar los tramos antes de coserlos. Un anillo nunca se toca:
        # cerrarlo es justo lo que queremos conservar.
        if recorte is not None and not parte.intersects(recorte):
            continue          # manzana entera fuera de la hoja: no se dibuja
        if recorte is not None and not parte.is_ring and not recorte.contains(parte):
            corte = parte.intersection(recorte)
            for trozo in getattr(corte, "geoms", [corte]):
                if isinstance(trozo, LineString) and len(trozo.coords) >= 2:
                    salida.append((list(trozo.coords), False))
            continue
        pts = list(parte.coords)
        if len(pts) < 2:
            continue
        # `is_ring` ya trae el primer punto repetido al final: se quita y se marca cerrada,
        # que es como AutoCAD espera una polilínea cerrada.
        if parte.is_ring and len(pts) > 3:
            salida.append((pts[:-1], True))
        elif len(pts) > 3 and math.dist(pts[0], pts[-1]) < _HUECO_REDONDEO:
            # Da la vuelta entera y no cierra por centímetros: es redondeo del MUB, no un
            # lado que falta. Sólo se cierra ese caso — el hueco mediano de las cadenas
            # realmente abiertas es de 76 m, y cerrar eso sería inventar una manzana.
            salida.append((pts, True))
        else:
            salida.append((pts, False))
    return salida


# Ancho mínimo de un lote en la fila, en metros. Dos frentes catastrales pueden proyectarse
# casi en el mismo punto; sin un mínimo, el rectángulo sale de ancho cero y no se ve.
_ANCHO_MIN_LOTE = 3.0

# Ancho máximo de un lote en la fila. Sin tope, una geometría rota se lleva puesta la
# manzana entera: en VG hay una parcela de 4,6 km².
_ANCHO_MAX_LOTE = 40.0

# Fondo de lote por defecto cuando la fila no permite deducirlo, y sus topes. La mediana
# real de una cara de manzana de VG ronda los 25 m.
_FONDO_POR_DEFECTO = 25.0
_FONDO_MIN, _FONDO_MAX = 12.0, 30.0


def _resumen_uf(uf_v: int, uf_c: int, pisos_txt: str) -> str:
    """El texto legible que pidió Jaz: cuántas viviendas, cuántos comercios y cuántos pisos.

    El código del cliente (`198R-2`) es exacto pero cifrado, y **oculta la cantidad cuando
    es 1**: un lote de una vivienda no muestra ningún número. Esta línea la dice siempre y
    en castellano llano. `PB` en vez de `0P` porque un "0" al lado de un piso se lee como
    dato faltante, no como planta baja.
    """
    partes = []
    if uf_v:
        partes.append(f"{uf_v}V")
    if uf_c:
        partes.append(f"{uf_c}C")
    return " ".join(partes) if partes else "VAZIO"


def _texto_pisos(pisos_txt: str) -> str:
    """Los pisos, en su propio renglón y abreviados: `0P`, `1P`, `3P`.

    **La convención es pisos SOBRE la planta baja**, la misma que ya viajaba en
    `QTD_ANDARES`: una casa de una sola planta es `0P`, planta baja más uno es `1P`.

    Vacío cuando no hay dato: no se inventa una altura.
    """
    if pisos_txt == "":
        return ""
    return f"{pisos_txt}P"


# Largo de la línea divisoria entre lotes. Medido sobre las 388 `DIV` del cliente: 10,00 m
# exactos, mediana, mínimo y máximo.
_LARGO_DIV = 10.0

# Cuánto se sale el nombre de calle hacia la calzada cuando no hay eje de calle cargado.
_ANCHO_CALZADA = 8.0

# Hasta dónde se busca la manzana de enfrente para ubicar el eje de la calzada. Más allá
# de 45 m ya no es una calle, es un baldío o el borde del relevamiento.
_BUSQUEDA_EJE = 45.0

# Distancia mínima entre dos rótulos de la misma calle. Menos que esto es el mismo tramo
# rotulado dos veces, una por cada vereda.
_SEPARACION_ROTULOS = 70.0

# Cuántas veces como mucho se rotula una misma calle en la hoja.
_ROTULOS_POR_CALLE = 2

# A qué distancia de la manzana más cercana un rótulo de calle deja de estar en una calle.
# El plano del cliente los pone a 4,5 m de mediana del borde y ninguno pasa de 15,4 m.
_LEJOS_DE_MANZANA = 40.0

# Cuánto puede estar el eje de OSM del punto que dedujimos desde la manzana para darlo por
# la misma calle. 45 m es media cuadra: más lejos ya es otra vía.
_CERCA_DEL_EJE = 45.0

# Cuánto puede apartarse el renglón del borde de la manzana que tiene enfrente. El nombre
# tiene que leerse PARALELO a la manzana: cruzado se lee como si nombrara a la transversal.
# Medido en la zona piloto antes de esto: 5 rótulos a más de 30° —Aracy de Almeida a 44,7° y
# 75,0°, o sea sus DOS apariciones cruzadas sobre Santa Laura—. 20° tolera la curva de una
# avenida y el error de la tangente.
_DESVIO_MAX_MANZANA = 20.0
# Cuánto pesa el cruce en la penalización, comparado con pisar la manzana (que vale 1,0 al
# 100% del renglón tapado). Un nombre cruzado se lee mal aunque esté en calzada limpia.
_PESO_CRUCE = 0.6

# Cuánto se mira hacia cada lado sobre el eje para sacar la tangente. Con menos, un vértice
# de la polilínea de OSM tuerce el ángulo del texto; con más, se pierde la curva.
_TANGENTE_EJE = 8.0

# Hasta dónde se camina la calle buscando un lugar limpio para el nombre, y con qué paso.
# 180 m es una cuadra larga para cada lado: más que eso y el rótulo se va del tramo relevado.
_BUSQUEDA_ROTULO = 180.0
_PASO_BUSQUEDA_ROTULO = 6.0

# Cuánto del texto puede quedar sobre la manzana y seguir siendo aceptable. En el plano del
# propio cliente 7 de sus 77 rótulos entran en una manzana, así que un roce es normal.
_ROCE_MANZANA_OK = 0.08

# Cuánto se retira la cara de OSM para pasar del eje de calzada a la línea municipal. La
# manzana clásica del conurbano son ~86,6 m de manzana y 17,32 m de calle, o sea 8,66 m por
# lado; 8 m deja una calle de 16 m, que es lo que se mide entre ejes en Malvinas (100 m).
_RETIRO_LINEA_MUNICIPAL = 8.0
# Si el retiro se come más que esto de la cara, no era una manzana rodeada de calles.
_MIN_TRAS_RETIRO = 0.40


# Altura del rótulo visible del SDU y del MDU (la de la plantilla del cliente).
# Ancho medio de un carácter como fracción de la altura del texto, para Arial. Sirve para
# saber cuánto mide un renglón en metros de modelspace y que no se salga del lote.
_ANCHO_CARACTER = 0.62

# Altura del rótulo visible del SDU y del MDU (la de la plantilla del cliente).
_H_ROTULO_SDU = 1.5
_H_ROTULO_MDU = 1.4

# Altura del nombre de calle en la plantilla del cliente.
_H_ROTULO_LOG = 2.0


def _largo_texto(lineas: list[str], altura: float) -> float:
    """Cuánto ocupa, en metros, el renglón más largo de una ficha."""
    return max((len(t) for t in lineas if t), default=0) * altura * _ANCHO_CARACTER


def _eje_de_calzada(frente, normal, manzanas: list, arbol, ancho_por_defecto: float):
    """El centro de la calzada frente a esta cara: la mitad del hueco entre manzanas.

    El nombre de calle va donde iría la línea divisoria de carriles, así que hay que
    encontrar el vacío entre esta manzana y la de enfrente. Se camina hacia afuera del
    lote y se mide dónde termina la manzana propia y dónde empieza la de enfrente; el
    medio de ese tramo es el eje.

    Sin manzana enfrente —el borde del relevamiento— se usa media calzada por defecto.
    """
    nx, ny = -normal[0], -normal[1]        # hacia la calle
    fuera = None
    for paso in range(0, int(_BUSQUEDA_EJE) + 1):
        q = Point(frente.x + nx * paso, frente.y + ny * paso)
        dentro = arbol is not None and any(manzanas[k].contains(q)
                                           for k in arbol.query(q))
        if fuera is None:
            if not dentro:
                fuera = paso           # salió de la manzana propia
        elif dentro:
            medio = (fuera + paso) / 2.0
            return (frente.x + nx * medio, frente.y + ny * medio), (paso - fuera)
    d = (fuera if fuera is not None else 0) + ancho_por_defecto
    return (frente.x + nx * d, frente.y + ny * d), None


def _toca_manzana(caja: Polygon, manzanas: list, arbol) -> bool:
    """¿La caja de texto pisa alguna manzana? Para dejar el nombre de calle en la calzada."""
    if arbol is None:
        return False
    return any(manzanas[k].intersects(caja) for k in arbol.query(caja))


def _pisa_manzana(caja: Polygon, manzanas: list, arbol) -> float:
    """Qué fracción de la caja de texto cae sobre una manzana. 0 = toda en la calzada."""
    if arbol is None or caja.area <= 0:
        return 0.0
    sobre = 0.0
    for k in arbol.query(caja):
        sobre += manzanas[k].intersection(caja).area
        if sobre >= caja.area:
            break
    return min(sobre / caja.area, 1.0)


class _Ocupadas:
    """Las cajas de texto ya dibujadas, con índice espacial.

    Recorrerlas en lista alcanza en Várzea (567 parcelas) pero no en Malvinas (2.059): son
    ~8 millones de intersecciones y el export no termina. El índice se rearma cada tanto y
    la cola sin indexar se recorre a mano, que es corta por construcción.
    """

    def __init__(self, cada: int = 250):
        self._indexadas: list = []
        self._cola: list = []
        self._arbol = None
        self._cada = cada

    def add(self, caja) -> None:
        self._cola.append(caja)
        if len(self._cola) >= self._cada:
            self._indexadas += self._cola
            self._cola = []
            self._arbol = STRtree(self._indexadas)

    def choca(self, caja) -> bool:
        if self._arbol is not None:
            for k in self._arbol.query(caja):
                if self._indexadas[k].intersects(caja):
                    return True
        return any(c.intersects(caja) for c in self._cola)


def _caja_texto(ancla, rot_grados: float, largo: float, altura: float) -> Polygon:
    """La huella real del renglón: arranca en el ancla y corre en la dirección de la
    rotación, con la altura repartida a los dos lados (los atributos son `valign=MIDDLE`).
    """
    r = math.radians(rot_grados)
    dx, dy = math.cos(r), math.sin(r)
    px, py = -dy * altura / 2, dx * altura / 2
    ax, ay = ancla
    return Polygon([
        (ax + px, ay + py), (ax + dx * largo + px, ay + dy * largo + py),
        (ax + dx * largo - px, ay + dy * largo - py), (ax - px, ay - py),
    ])


def _ubicar_renglon(lote: Polygon, punto, rot_grados: float, normal, direccion,
                    largo: float, altura: float, fondo: float, manzana=None,
                    ocupadas=None):
    """El ancla donde el renglón ENTERO entra en el lote, no sólo su punto de inicio.

    Verificar el punto de inserción no alcanza: el texto arranca ahí y corre varios metros.
    Se prueban, en orden: tal cual, retrocedido un largo (el renglón termina en el frente),
    y algo más adentro. Si ninguna entra, se deja la que menos se sale — perder la ficha
    sería peor que rozar el borde.
    """
    r = math.radians(rot_grados)
    dx, dy = math.cos(r), math.sin(r)
    nx, ny = normal
    candidatos = [punto]
    # Si el renglón apunta a la calzada, se retrocede: termina donde iba a empezar.
    if dx * nx + dy * ny < 0:
        candidatos.insert(0, (punto[0] - dx * largo, punto[1] - dy * largo))
    else:
        candidatos.append((punto[0] - dx * largo, punto[1] - dy * largo))
    # Y un par de posiciones más adentro, por si el frente está justo.
    for extra in (largo * 0.5, largo):
        candidatos.append((punto[0] + nx * extra, punto[1] + ny * extra))
        if dx * nx + dy * ny < 0:
            candidatos.append((punto[0] + nx * extra - dx * largo,
                               punto[1] + ny * extra - dy * largo))

    # Primero se busca que entre en su propio lote. Si ninguno entra, alcanza con que
    # entre en la MANZANA: que el renglón invada un poco al vecino se ve bien; que asome
    # sobre la calle, no.
    mejor, peor_fuera = None, float("inf")
    en_manzana = None
    entra_pero_choca = None
    for cand in candidatos:
        caja = _caja_texto(cand, rot_grados, largo, altura)
        fuera = caja.difference(lote).area
        choca = bool(ocupadas) and ocupadas.choca(caja)
        if fuera <= 0.01:
            if not choca:
                return cand
            if entra_pero_choca is None:
                entra_pero_choca = cand
            continue
        if (en_manzana is None and not choca and manzana is not None
                and manzana.contains(caja)):
            en_manzana = cand
        if fuera < peor_fuera:
            peor_fuera, mejor = fuera, cand
    # Orden de preferencia: entra y no choca · no choca y está en la manzana · entra
    # aunque choque · la que menos se sale.
    return entra_pero_choca or en_manzana or mejor or punto


def _anclar_texto(punto, rot_grados: float, normal, largo: float):
    """Corre el punto de inserción para que el renglón entre hacia adentro del lote.

    El texto **arranca** en el punto de inserción y corre en la dirección de la rotación.
    Como la rotación se normaliza a [0, 180) para que no salga cabeza abajo, la mitad de
    las veces apunta a la calzada y el renglón se dibujaba sobre la calle. Cuando pasa
    eso, se retrocede el ancla un largo de texto: el renglón termina donde iba a empezar y
    queda igual de pegado al frente, pero adentro.
    """
    dx, dy = math.cos(math.radians(rot_grados)), math.sin(math.radians(rot_grados))
    if dx * normal[0] + dy * normal[1] < 0:
        return punto[0] - dx * largo, punto[1] - dy * largo
    return punto


def _adentro_de(lote: Polygon, punto, respaldo):
    """El punto, si cae dentro del lote; si no, el respaldo; si no, el interior del lote.

    Recortar el rectángulo contra la manzana puede dejar afuera el lugar donde iba la
    ficha: **nada tiene que quedar fuera de su lote**, que es lo que se veía como fichas
    en la calzada.
    """
    if lote.contains(Point(*punto)):
        return punto
    if lote.contains(Point(*respaldo)):
        return respaldo
    rp = lote.representative_point()
    return rp.x, rp.y


def _borde_suelto(segmentos: list, arbol, puntos: list):
    """El tramo de `QUADRA` más cercano a una cara que no tiene manzana cerrada.

    Donde el MUB no cierra el anillo no hay polígono al que agarrarse, y la fila se apoyaba
    en el promedio de los frentes catastrales: como el catastro y el MUB no coinciden, eso
    dejaba fichas **en el medio de la manzana**, hasta a 387 m de un borde. La línea de
    manzana existe igual aunque el anillo no cierre; alcanza con usarla.

    La normal se orienta hacia donde están los lotes, que es el lado que se releva.
    """
    if not segmentos or arbol is None or not puntos:
        return None
    cx = sum(q.x for q in puntos) / len(puntos)
    cy = sum(q.y for q in puntos) / len(puntos)
    centro = Point(cx, cy)
    cand = [segmentos[k] for k in arbol.query(centro.buffer(_BUSQUEDA_BORDE))]
    if not cand:
        return None
    mejor = min(cand, key=lambda seg: sum(seg.distance(q) for q in puntos))
    (ax, ay), (bx, by) = mejor.coords[0], mejor.coords[-1]
    largo = math.hypot(bx - ax, by - ay)
    if largo < 1e-9:
        return None
    ux, uy = (bx - ax) / largo, (by - ay) / largo
    nx, ny = -uy, ux
    q = mejor.interpolate(mejor.project(centro))
    if (cx - q.x) * nx + (cy - q.y) * ny < 0:
        nx, ny = -nx, -ny
    return (ax, ay), (ux, uy), (nx, ny)


def _borde_mas_cercano(manzana: Polygon, puntos: list):
    """El lado de la manzana al que da esta cara, con su dirección y su normal interior.

    Apoyar la fila en el borde real de la manzana —y no en el promedio de los frentes
    catastrales— es lo que hace que los lotes queden **sobre** la manzana y alineados con
    ella, que es como los dibuja el cliente.
    """
    anillo = list(manzana.exterior.coords)
    mejor, dmin = None, float("inf")
    for a, b in zip(anillo, anillo[1:]):
        if a == b:
            continue
        seg = LineString([a, b])
        if seg.length < _LADO_MIN_MANZANA:
            continue
        d = sum(seg.distance(q) for q in puntos) / len(puntos)
        if d < dmin:
            dmin, mejor = d, seg
    if mejor is None:
        return None
    (ax, ay), (bx, by) = mejor.coords[0], mejor.coords[-1]
    largo = math.hypot(bx - ax, by - ay)
    ux, uy = (bx - ax) / largo, (by - ay) / largo
    nx, ny = -uy, ux
    # La normal apunta al interior de la manzana.
    mx, my = (ax + bx) / 2, (ay + by) / 2
    if not manzana.contains(Point(mx + nx * 0.5, my + ny * 0.5)):
        nx, ny = -nx, -ny
    return (ax, ay), (ux, uy), (nx, ny)


def _fila_de_lotes(items, borde=None) -> list:
    """Alinea los lotes de una cara de manzana en una fila de rectángulos pegados.

    Dibujar el polígono catastral tal cual sale en escalera: cada lote entra y sale unos
    metros respecto del vecino, y el conjunto no se lee como una manzana. Acá se toma **una
    sola línea de frente para toda la cara** —la mediana de los frentes reales— y **un solo
    fondo**, y cada lote se queda con el tramo de frente que de verdad ocupa. El resultado
    es la manzana subdividida, alineada, sin dientes.

    `items` son `(clave, polígono, punto de frente, dirección, normal)` con la dirección ya
    orientada igual para toda la cara. Devuelve `(clave, rectángulo, origen, dirección,
    normal, s0, s1, fondo)`.
    """
    if not items:
        return []
    # Si la cara tiene manzana, la fila se apoya en el lado real de la manzana. Si no
    # —el MUB no la trae cerrada—, en la nube de frentes, que ya viene orientada.
    _k0, _p0, pf0, (ux, uy), (nx, ny) = items[0]
    ox, oy = pf0.x, pf0.y
    if borde is not None:
        (ox, oy), (ux, uy), (nx, ny) = borde

    tramos = []
    for clave, poli, _pf, _u, _n in items:
        ss, dd = [], []
        for x, y in poli.exterior.coords:
            vx, vy = x - ox, y - oy
            ss.append(vx * ux + vy * uy)
            dd.append(vx * nx + vy * ny)
        # Ancho medido sobre la fila y acotado: la parcela rota de 4,6 km² proyectada
        # daría un rectángulo de cientos de metros y se comería la manzana entera.
        s_ini = min(ss)
        ancho = min(max(max(ss) - s_ini, _ANCHO_MIN_LOTE), _ANCHO_MAX_LOTE)
        # **El fondo sale del área, no de la proyección.** Proyectar el polígono sobre la
        # normal infla el fondo de todo lote que no esté alineado con la fila: daba
        # rectángulos de 57 m de profundidad para lotes de 450 m². Con `área ÷ ancho` el
        # rectángulo conserva la superficie real de la parcela.
        tramos.append([clave, s_ini, s_ini + ancho, min(dd), poli.area / ancho])

    if borde is not None:
        # **La fila arranca EN el borde de la manzana.** Tomar la mediana de los frentes
        # catastrales dejaba la fila donde el catastro cree que están los lotes, que en
        # este MUB no coincide con la manzana: por eso había fichas en el medio de la
        # manzana, hasta a 387 m de un borde. Con el borde como frente, todas quedan
        # donde las pone el cliente: pegadas a la línea de manzana.
        base = 0.0
    else:
        frentes = sorted(t[3] for t in tramos)
        base = frentes[len(frentes) // 2]
    fondos = sorted(t[4] for t in tramos if t[4] > 0)
    fondo = fondos[len(fondos) // 2] if fondos else _FONDO_POR_DEFECTO
    fondo = min(max(fondo, _FONDO_MIN), _FONDO_MAX)

    # Los lotes se pegan uno al lado del otro: el que empieza antes de que termine el
    # anterior se corre. Así comparten el límite en vez de solaparse.
    tramos.sort(key=lambda t: t[1])
    ultimo = None
    for t in tramos:
        if ultimo is not None and t[1] < ultimo:
            t[1] = ultimo
        if t[2] - t[1] < _ANCHO_MIN_LOTE:
            t[2] = t[1] + _ANCHO_MIN_LOTE
        ultimo = t[2]

    salida = []
    for clave, s0, s1, _f, _d in tramos:
        esquinas = [
            (ox + ux * s0 + nx * base, oy + uy * s0 + ny * base),
            (ox + ux * s1 + nx * base, oy + uy * s1 + ny * base),
            (ox + ux * s1 + nx * (base + fondo), oy + uy * s1 + ny * (base + fondo)),
            (ox + ux * s0 + nx * (base + fondo), oy + uy * s0 + ny * (base + fondo)),
        ]
        salida.append((clave, esquinas, (ox, oy), (ux, uy), (nx, ny), s0, s1, base, fondo))
    return salida


def _caras_de_manzana(marcas: list) -> list:
    """Parte los lotes de una calle en caras de manzana: por vereda y por bocacalle.

    Las dos veredas de una calle están más cerca entre sí que dos lotes separados por una
    bocacalle, así que primero se separa por lado —el signo de la normal contra la
    dirección de la calle— y recién después por cercanía entre frentes.
    """
    if not marcas:
        return []
    u0 = marcas[0][3]
    lados: dict[int, list] = {}
    for marca in marcas:
        _clave, _poli, _pf, (ux, uy), (nx, ny) = marca
        if ux * u0[0] + uy * u0[1] < 0:
            ux, uy = -ux, -uy
        lado = 1 if (u0[0] * ny - u0[1] * nx) > 0 else -1
        lados.setdefault(lado, []).append(
            (marca[0], marca[1], marca[2], (ux, uy), (nx, ny)))

    caras = []
    for _lado, items in lados.items():
        grupos: list[list] = []
        for item in sorted(items, key=lambda m: (m[2].x, m[2].y)):
            for g in grupos:
                if any(item[2].distance(otro[2]) <= _CORTE_CARA for otro in g):
                    g.append(item)
                    break
            else:
                grupos.append([item])

        # Una cara muy larga deja de ser recta: sobre una avenida curva, alinear 300 m de
        # lotes contra una sola dirección los tuerce. Se parte en tramos del largo de una
        # manzana — pero **la dirección se fija antes de partir, para toda la cara**. Si
        # cada tramo la recalcula, dos tramos vecinos salen con ángulos distintos y sus
        # rectángulos se cruzan justo en la costura: así se solapaban 95 lotes.
        for g in grupos:
            g = sorted(g, key=lambda m: (m[2].x, m[2].y))
            # Cada tramo se orienta con SU nube de frentes. Probé fijar una sola dirección
            # para la cara entera y es peor —105 lotes solapados contra 85—: sobre una
            # avenida curva, forzar 300 m de lotes a un mismo ángulo hace que los tramos
            # lejanos se despeguen del frente y se crucen con la fila de al lado.
            for k in range(0, len(g), _LOTES_POR_CARA):
                caras.append(_orientar_cara(g[k:k + _LOTES_POR_CARA]))
    return caras


def _orientar_cara(items: list) -> list:
    """Le pone a todos los lotes de una cara la misma dirección: la de la nube de frentes.

    Con la dirección del primer lote, una avenida curva mandaba los lotes lejanos a
    proyectarse a cientos de metros y salían rectángulos gigantes cruzando el plano.
    """
    if len(items) < 3:
        return items
    import numpy as np
    _k0, poli0, pf0, (ux, uy), _n0 = items[0]
    pfs = np.array([[m[2].x, m[2].y] for m in items], dtype=float)
    _uu, _ss, vt = np.linalg.svd(pfs - pfs.mean(axis=0), full_matrices=False)
    vx, vy = float(vt[0][0]), float(vt[0][1])
    if vx * ux + vy * uy < 0:       # se conserva el sentido del primer frente
        vx, vy = -vx, -vy
    nx, ny = -vy, vx
    rp0 = poli0.representative_point()
    if (rp0.x - pf0.x) * nx + (rp0.y - pf0.y) * ny < 0:
        nx, ny = -nx, -ny
    return [(k, poli, pf, (vx, vy), (nx, ny)) for k, poli, pf, _u, _n in items]


def _banda(fila: list, fondo_f: float = 1.0) -> Polygon:
    """El rectángulo que ocupa una fila entera, para ver si choca con la de al lado."""
    _i, _e, (ox, oy), (ux, uy), (nx, ny), _s0, _s1, base, fondo = fila[0]
    s_ini = min(f[5] for f in fila)
    s_fin = max(f[6] for f in fila)
    f = fondo * fondo_f
    return Polygon([
        (ox + ux * s_ini + nx * base, oy + uy * s_ini + ny * base),
        (ox + ux * s_fin + nx * base, oy + uy * s_fin + ny * base),
        (ox + ux * s_fin + nx * (base + f), oy + uy * s_fin + ny * (base + f)),
        (ox + ux * s_ini + nx * (base + f), oy + uy * s_ini + ny * (base + f)),
    ])


def _achicar_filas_que_chocan(filas: list) -> dict:
    """Achica el fondo de las filas que se pisan entre sí.

    Las filas se arman una por cara de manzana, cada una sin saber de las otras: en una
    esquina, la fila de una calle entra en la de la calle que cruza y los lotes se cruzan.
    Acá se mira el conjunto y se le baja el fondo a las que chocan, hasta un piso —por
    debajo de eso el lote deja de leerse como lote y es preferible el cruce.

    Devuelve `{índice de fila: factor de fondo}`.
    """
    factores = {i: 1.0 for i in range(len(filas))}
    for _vuelta in range(_VUELTAS_AJUSTE):
        bandas = [_banda(f, factores[i]) for i, f in enumerate(filas)]
        arbol = STRtree(bandas)
        chocan = set()
        for i, b in enumerate(bandas):
            for j in arbol.query(b):
                if j <= i:
                    continue
                comun = b.intersection(bandas[j]).area
                if comun > min(b.area, bandas[j].area) * _SOLAPE_TOLERADO:
                    chocan.add(i)
                    chocan.add(j)
        if not chocan:
            break
        for i in chocan:
            factores[i] = max(factores[i] * 0.7, _FACTOR_FONDO_MIN)
    return factores


def _celdas_de_manzana(manzana: Polygon, semillas: list) -> list:
    """Parte la manzana en una celda por parcela, sin huecos y sin superposición.

    Colocar cada ficha sobre el borde, en su tramo de frente, dejaba los renglones
    peleándose entre lotes angostos: el texto es más largo que el frente y no hay forma de
    que entren todos pegados a la línea. Partiendo la manzana **por dentro** —una celda por
    parcela, con el texto centrado— el solape se vuelve imposible por construcción: cada
    renglón vive en su propia celda y las celdas no se pisan.

    La partición es el diagrama de Voronoi de los puntos de las parcelas recortado contra
    la manzana: es la forma más simple de repartir una manzana entre N lotes respetando
    dónde está cada uno, y da celdas convexas, que es lo que hace fácil centrar un texto.

    Devuelve una celda por semilla, en el mismo orden. `None` donde no salió celda.
    """
    if not semillas:
        return []
    if len(semillas) == 1:
        return [manzana]
    puntos = MultiPoint([Point(x, y) for x, y in semillas])
    marco = box(*manzana.buffer(50.0).bounds)
    try:
        crudas = list(voronoi_polygons(puntos, extend_to=marco).geoms)
    except Exception as e:
        logger.warning(f"Voronoi falló en una manzana ({e}); se deja sin subdividir.")
        return [None] * len(semillas)

    # `voronoi_polygons` no garantiza el orden de las celdas: se reasigna cada una a la
    # semilla que contiene.
    salida = [None] * len(semillas)
    for celda in crudas:
        recorte = celda.intersection(manzana)
        if isinstance(recorte, MultiPolygon) and not recorte.is_empty:
            recorte = max(recorte.geoms, key=lambda g: g.area)
        if not isinstance(recorte, Polygon) or recorte.is_empty:
            continue
        for k, (x, y) in enumerate(semillas):
            if salida[k] is None and celda.contains(Point(x, y)):
                salida[k] = recorte
                break
    return salida


def _partir_celda(celda: Polygon, n, partes: int) -> list:
    """Parte la celda en franjas paralelas al frente, una por inmueble del terreno.

    Un terreno con vivienda y comercio son dos fichas en la MISMA parcela: se le da a cada
    una su franja —el comercio adelante, contra la calle; la vivienda al fondo— para que
    los dos textos entren centrados y sin tocarse.
    """
    if partes <= 1:
        return [celda]
    nx, ny = n
    ds = [(x * nx + y * ny) for x, y in celda.exterior.coords]
    d0, d1 = min(ds), max(ds)
    radio = max(celda.bounds[2] - celda.bounds[0],
                celda.bounds[3] - celda.bounds[1]) * 2 + 50
    # ⚠ La franja hay que centrarla EN LA CELDA. Armada alrededor del origen de
    # coordenadas quedaba a 8.000 km de acá —el plano está en UTM—, la intersección salía
    # vacía y los dos inmuebles del terreno mixto terminaban centrados en la misma celda,
    # uno encima del otro.
    c = celda.centroid
    t0 = -c.x * ny + c.y * nx
    salida = []
    for k in range(partes):
        a = d0 + (d1 - d0) * k / partes
        b = d0 + (d1 - d0) * (k + 1) / partes
        franja = Polygon([
            (nx * a - ny * (t0 - radio), ny * a + nx * (t0 - radio)),
            (nx * a - ny * (t0 + radio), ny * a + nx * (t0 + radio)),
            (nx * b - ny * (t0 + radio), ny * b + nx * (t0 + radio)),
            (nx * b - ny * (t0 - radio), ny * b + nx * (t0 - radio)),
        ])
        trozo = celda.intersection(franja)
        if isinstance(trozo, MultiPolygon) and not trozo.is_empty:
            trozo = max(trozo.geoms, key=lambda g: g.area)
        salida.append(trozo if isinstance(trozo, Polygon) and not trozo.is_empty else celda)
    return salida


def _semiplano(orig, u, s: float, radio: float, antes: bool) -> Polygon:
    """El medio plano perpendicular a `u` que corta en la posición `s` sobre el frente."""
    ox, oy = orig
    ux, uy = u
    px, py = -uy, ux
    a = 0.0 if antes else s
    b = s if antes else radio * 2
    if antes:
        a = -radio * 2
    return Polygon([
        (ox + ux * a + px * radio, oy + uy * a + py * radio),
        (ox + ux * b + px * radio, oy + uy * b + py * radio),
        (ox + ux * b - px * radio, oy + uy * b - py * radio),
        (ox + ux * a - px * radio, oy + uy * a - py * radio),
    ])


def _rebanar_por_area(banda: Polygon, orig, u, areas: list, s_ini: float,
                      s_fin: float) -> list:
    """Corta la banda en rebanadas perpendiculares al frente, una por lote.

    **Figurativo pero dimensionado.** Dibujar el polígono catastral tal cual deja lotes que
    se salen de la manzana o que no la llenan, y el conjunto se lee como "una manzana con
    un lote encima". Acá la manzana se reparte entera en franjas geométricas y a cada una
    se le da **el área real de su parcela**: la forma es esquemática, la superficie no.

    Las áreas se escalan para que sumen la de la banda, así no quedan huecos ni sobrantes;
    lo que se conserva es la proporción entre lotes, que es lo que se lee en el plano.

    Cada corte se busca por bisección sobre la posición a lo largo del frente: es la forma
    simple de partir un polígono irregular en trozos de área dada.
    """
    total = sum(a for a in areas if a > 0)
    if total <= 0 or banda.is_empty:
        return [None] * len(areas)
    objetivo = [banda.area * (a / total) if a > 0 else 0.0 for a in areas]
    radio = max(banda.bounds[2] - banda.bounds[0],
                banda.bounds[3] - banda.bounds[1]) * 2 + 100

    def _mayor(g):
        '''La parte más grande, para dibujar; el corte se resta ENTERO.'''
        if isinstance(g, MultiPolygon) and not g.is_empty:
            return max(g.geoms, key=lambda x: x.area)
        return g if isinstance(g, Polygon) else None

    salida = []
    s_actual = s_ini
    restante = banda
    for k, meta in enumerate(objetivo):
        if k == len(objetivo) - 1:
            salida.append(_mayor(restante))
            break
        lo, hi = s_actual, s_fin
        corte = None
        for _ in range(_PASOS_BISECCION):
            medio = (lo + hi) / 2
            corte = restante.intersection(_semiplano(orig, u, medio, radio, True))
            if corte.area < meta:
                lo = medio
            else:
                hi = medio
        # ⚠ Partición EXACTA: se dibuja la parte mayor pero se descuenta el corte entero.
        # Quedándome con la parte mayor también para descontar, lo que sobraba seguía
        # dentro de `restante` y volvía a repartirse: 194 pares de lotes solapados y
        # manzanas enteras adjudicadas a un solo lote.
        salida.append(_mayor(corte) if corte is not None and corte.area > _AREA_MIN_LOTE
                      else None)
        s_actual = (lo + hi) / 2
        restante = restante.difference(_semiplano(orig, u, s_actual, radio, True))
        if restante.is_empty:
            salida += [None] * (len(objetivo) - len(salida))
            break
    while len(salida) < len(areas):
        salida.append(None)
    return salida


def _lotes_de_manzana(manzana: Polygon, parcelas: list) -> list:
    """Subdivide la manzana con las parcelas REALES, sin huecos y sin pisarse.

    Es el estilo del plano viejo de Hurlingham, que es el que Jaz quiere: la manzana con su
    forma y los lotes de verdad adentro, no rectángulos apoyados encima. Tres pasos:

    1. **Recortar cada parcela contra la manzana.** El catastro no coincide con el mapa
       municipal y muchas parcelas se salen a la calle; recortadas, la manzana marca el
       límite y desaparece el efecto de escalera sobre la calzada.
    2. **Resolver los pisados.** Dos parcelas del catastro pueden solaparse: la que se
       procesa después cede la parte compartida.
    3. **Repartir lo que sobra.** Lo que queda de la manzana sin parcela se le suma al lote
       con el que comparte más borde, así la manzana queda cubierta entera y no aparecen
       huecos entre lotes.

    Devuelve un polígono por parcela, en el mismo orden. `None` donde no quedó nada.
    """
    lotes: list = []
    areas_reales: list = []
    tomado = None
    for poli in parcelas:
        if poli is None or poli.is_empty:
            lotes.append(None)
            areas_reales.append(0.0)
            continue
        trozo = poli.intersection(manzana)
        if trozo.is_empty:
            # La parcela NO toca su manzana. Pasa porque el catastro y el mapa municipal
            # son dos relevamientos distintos que no encajan: en la zona piloto, 77 de 409
            # parcelas caían en lo que el MUB llama calle, a 21,8 m de media de su manzana.
            # Antes se devolvía None y la parcela se quedaba sin lote — pero la ficha salía
            # igual y aterrizaba en la calzada. Como la manzana es el contenedor y el lote
            # es una guía adentro de ella, se la ancla en el punto de la manzana más cercano
            # con una semilla del tamaño de la parcela, y el reparto del sobrante (paso 3)
            # le termina de dar su superficie contra los lotes vecinos.
            ancla, _ = nearest_points(manzana, poli)
            radio = max(math.sqrt(max(poli.area, _AREA_MIN_LOTE) / math.pi), 1.0)
            trozo = ancla.buffer(radio).intersection(manzana)
        if tomado is not None and not trozo.is_empty:
            trozo = trozo.difference(tomado)
        if isinstance(trozo, MultiPolygon) and not trozo.is_empty:
            trozo = max(trozo.geoms, key=lambda g: g.area)
        if not isinstance(trozo, Polygon) or trozo.area < _AREA_MIN_LOTE:
            lotes.append(None)
            areas_reales.append(0.0)
            continue
        lotes.append(trozo)
        areas_reales.append(poli.area)
        tomado = trozo if tomado is None else tomado.union(trozo)

    if tomado is None:
        return lotes

    # Lo que sobra de la manzana se reparte entre los lotes que lo rodean.
    resto = manzana.difference(tomado)
    for pieza in getattr(resto, "geoms", [resto]):
        if not isinstance(pieza, Polygon) or pieza.area < _AREA_MIN_LOTE:
            continue
        mejor, mayor = None, 0.0
        for k, lote in enumerate(lotes):
            if lote is None:
                continue
            comun = lote.buffer(0.05).intersection(pieza.buffer(0.05)).area
            if comun > mayor:
                mayor, mejor = comun, k
        if mejor is None:
            continue
        # ⚠ Con tope. Sin él, el lote que más borde comparte se queda con TODO el sobrante
        # de la manzana: una parcela de 465 m² terminó con una celda de 18.877 m² y su
        # rótulo centrado a 131 m de la parcela real, en otra calle. Lo que no entra en el
        # tope queda como interior de manzana, sin dibujar.
        tope = max(areas_reales[mejor], _AREA_MIN_LOTE) * _CRECIMIENTO_MAX
        if lotes[mejor].area + pieza.area > tope:
            continue
        unido = lotes[mejor].union(pieza)
        if isinstance(unido, MultiPolygon):
            unido = max(unido.geoms, key=lambda g: g.area)
        if isinstance(unido, Polygon):
            lotes[mejor] = unido
    return lotes


def _caja_grupo(ancla, ang: float, largo: float, altura: float,
                salto: float, lineas: int) -> Polygon:
    """La huella del conjunto de renglones de una ficha, como una sola pieza.

    Los renglones **cuelgan** del punto de inserción: el primero a la altura del ancla y
    cada siguiente `salto` metros más abajo, en coordenadas del bloque. La pieza va desde
    media altura por encima del ancla hasta media altura por debajo del último renglón.
    """
    rr = math.radians(ang)
    ex, ey = math.cos(rr), math.sin(rr)
    bx, by = math.sin(rr), -math.cos(rr)        # "abajo" del bloque
    alto = altura + salto * (lineas - 1)
    ax = ancla[0] - bx * altura / 2
    ay = ancla[1] - by * altura / 2
    return Polygon([
        (ax, ay), (ax + ex * largo, ay + ey * largo),
        (ax + ex * largo + bx * alto, ay + ey * largo + by * alto),
        (ax + bx * alto, ay + by * alto),
    ])


def _ubicar_grupo(celda: Polygon, u, n, largo: float, altura: float,
                  salto: float, lineas: int):
    """El ancla y el ángulo donde el conjunto ENTERO entra en el lote.

    Se coloca la ficha completa —rótulo, resumen y contador— como **una sola pieza**: así
    no puede pisarse a sí misma, y como los lotes no se solapan, tampoco puede pisar al
    vecino. Antes cada renglón se ubicaba por su cuenta y el contador, colocado aparte,
    provocaba 47 de los 87 solapes.

    Se prueba primero perpendicular a la calle, que es la convención del cliente, después
    paralelo, y si no entra se barren ángulos cada 15°. Medido sobre Várzea: la pieza
    completa entra en 539 de 566 lotes y la pieza sin contador en 561, así que buscar el
    ángulo alcanza — no hace falta achicar el texto.

    Devuelve `(ancla, ángulo, entró)`.
    """
    try:
        c = polylabel(celda, tolerance=max(0.3, math.sqrt(celda.area) / 50))
    except Exception:
        c = celda.representative_point()

    alto = altura + salto * (lineas - 1)
    angulos = [math.degrees(math.atan2(n[1], n[0])) % 180.0,
               math.degrees(math.atan2(u[1], u[0])) % 180.0]
    angulos += [a for a in range(0, 180, 15)]
    for ang in angulos:
        rr = math.radians(ang)
        ex, ey = math.cos(rr), math.sin(rr)
        bx, by = math.sin(rr), -math.cos(rr)
        # Centro de la pieza en el punto más interior del lote.
        ancla = (c.x - ex * largo / 2 - bx * (alto - altura) / 2,
                 c.y - ey * largo / 2 - by * (alto - altura) / 2)
        if celda.contains(_caja_grupo(ancla, ang, largo, altura, salto, lineas)):
            return ancla, ang, True

    # No entra en ningún ángulo: se deja centrado y perpendicular, que es lo que menos se
    # sale, y el llamador sabe que este lote quedó apretado.
    ang = angulos[0]
    rr = math.radians(ang)
    return ((c.x - math.cos(rr) * largo / 2 - math.sin(rr) * (alto - altura) / 2,
             c.y - math.sin(rr) * largo / 2 + math.cos(rr) * (alto - altura) / 2),
            ang, False)


def _emitir_ficha(msp, rec: dict, tp_b: str, qt_b: int, punto, rot: float,
                  celula_id: str, escala: float = 1.0) -> str:
    """Dibuja un bloque `SDU`/`MDU` con todos sus atributos. Devuelve cuál dibujó."""
    es_mdu = rec["es_mdu"]
    sufijo = {"R": "RES", "C": "COM", "E": "ESP"}[tp_b]
    if not es_mdu and tp_b == "E":
        sufijo = "VAZ"     # el lote vacío tiene capa propia en el estándar
    capa = f"{'MDU' if es_mdu else 'SDU'}_{sufijo}"
    ref = msp.add_blockref("MDU" if es_mdu else "SDU", punto,
                           dxfattribs={"layer": capa, "rotation": rot})
    # Cada bloque lleva SU coordenada: son puntos distintos del mismo lote.
    utm_txt = f"{int(round(punto[0]))},{int(round(punto[1]))}"
    pisos = rec["pisos"]
    num, cep, codlog = rec["num"], rec["cep"], rec["codlog"]

    def _escalar():
        if escala >= 0.999:
            return
        for a in ref.attribs:
            a.dxf.height = a.dxf.height * escala

    if es_mdu:
        ref.add_auto_attribs({
            "NUMERO": num or "S/N",
            "HP_TOTAL": str(rec["uf_v"] + rec["uf_c"]),
            # Manda el catastro si lo declara y, si no, los pisos que ve el satélite
            # (`parcela_altura`, AlturaFetcher). La base cuenta NIVELES (planta baja sola
            # = 1) porque ahí el número multiplica; el entregable va en PISOS SOBRE PLANTA
            # BAJA (PB = 0). La conversión vive en `pisos.py`.
            "QTD_ANDARES": pisos,
            # Sin los pisos: en el MDU ya los muestra `QTD_ANDARES`, que es campo visible
            # del bloque del cliente. `RESUMO` sólo agrega lo que a él le falta, el
            # reparto entre viviendas y comercios.
            "RESUMO": _resumen_uf(rec["uf_v"], rec["uf_c"], ""),
            "PISOS": _texto_pisos(pisos),
            "BLOCO": "UNICO",
            "ESTABELECIMENTO": "",
            "CODLOG": codlog,
            "CEP": cep,
            "UTM": utm_txt,
            "NOME": "",
            "CLASSE_SOCIAL": "",
            "CODGED": "",
            "ID": celula_id,
        })
        _escalar()
        return "MDU"

    ref.add_auto_attribs({
        "N_C1_3_TP_QT": _etiqueta(num, tp_b, qt_b),
        # El lote vacío no lleva pisos: no hay nada construido que contar.
        "QTD_ANDARES": "" if tp_b == "E" else pisos,
        # La línea legible. Se arma con las UF **de este bloque**, no las de la parcela:
        # en una parcela mixta el bloque `R` dice sus viviendas y el `C` sus comercios.
        "RESUMO": _resumen_uf(qt_b if tp_b == "R" else 0,
                              qt_b if tp_b == "C" else 0, ""),
        "PISOS": "" if tp_b == "E" else _texto_pisos(pisos),
        "N": num or "S/N",
        # Corte en límite de palabra: `[:32]` partía "…QD 4 LO" a mitad de palabra.
        "C1": _recortar(rec["complemento"] or "", 48),
        "C2": "",
        "C3": "",
        "TP": tp_b,
        "QT": str(qt_b),
        "CODLOG": codlog,
        "CEP": cep,
        "UTM": utm_txt,
        "NOME": "",
        "CLASSE_SOCIAL": "",
        # VACÍO A PROPÓSITO, como en el plano del cliente: `TIPOIMOVEL` viene presente
        # pero vacío en 0/388 de sus SDU, igual que NOME, CLASSE_SOCIAL y CODGED.
        "TIPOIMOVEL": "",
        "ID": celula_id,
        "CODGED": "",
    })
    _escalar()
    return "SDU"


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


def _ejes_desde_parcelas(rows, geoms, tr) -> list:
    """Ejes de calle deducidos de las propias parcelas, para los países sin `logradouros`.

    Esa tabla es de Brasil: en Malvinas y Hurlingham devuelve 0 filas y el plano se quedaba
    sin ejes, sin los cuales no hay frente de lote, ni fila, ni celda — todas las fichas
    caían al punto de la parcela. Las parcelas argentinas, en cambio, traen `calle` en el
    100% (2.059 de 2.059 en Malvinas, 441 de 441 en Hurlingham).

    Para cada nombre de calle, la dirección de máxima dispersión de sus parcelas **es** la
    calle, y el eje se traza sobre esa dirección cubriendo toda la nube. Es la misma
    cuenta que `_ejes_por_calle`, pero devolviendo la recta en vez del punto.
    """
    import numpy as np
    por_nombre: dict[str, list] = {}
    for row, geom in zip(rows, geoms):
        calle = row[1]
        if not calle or not isinstance(geom, (Polygon, MultiPolygon)) or geom.is_empty:
            continue
        rp = geom.representative_point()
        por_nombre.setdefault(_nombre_calle(calle), []).append(tr.transform(rp.x, rp.y))

    ejes = []
    for nombre, pts in por_nombre.items():
        if len(pts) < 2:
            continue
        arr = np.array(pts, dtype=float)
        centro = arr.mean(axis=0)
        _u, _s, vt = np.linalg.svd(arr - centro, full_matrices=False)
        d = vt[0]
        t = (arr - centro) @ d
        a = centro + d * t.min()
        b = centro + d * t.max()
        if math.dist(a, b) < 1.0:
            continue
        ejes.append(LineString([tuple(a), tuple(b)]))
    logger.info(f"Ejes de calle deducidos de las parcelas: {len(ejes)} "
                f"sobre {len(por_nombre)} nombres.")
    return ejes


def _mapa_base(conn, fuentes: dict[str, int], bbox_4326, tr) -> tuple[list, list, list]:
    """QUADRA, DIV y MEIOFIO del entorno del relevamiento, de varias fuentes a la vez.

    Se leen juntas las tres que tenemos, y `fuentes` le pone a cada una su **prioridad**:

      2. `AC_<region>` — el DWG de entrega que el cliente nos devolvió terminado. Trae sus
         manzanas cerradas y, en `DIV`, los **lotes** ya subdivididos. Es la mejor: son la
         forma que él mismo dibuja y cubren 562 de nuestras 567 parcelas.
      1. `OSM_<region>` — caras de la red de calles, para las zonas sin nada más.
      0. El MUB municipal, que sólo cierra manzanas en parte del corredor.

    Donde una de más prioridad ya cubre el terreno, la de menos se descarta: dos juegos de
    manzanas superpuestos hacían que un lote recortado contra una cruzara el borde de la
    otra. Sin manzana no hay contra qué recortar el lote y la parcela sale invadiendo la
    calle.

    **Se pide más ancho de lo que se dibuja.** El contorno de una manzana del borde se
    completa con tramos que caen fuera del bbox del relevamiento: pidiendo justo lo que
    entra en la hoja, esas manzanas nunca cierran. Se traen con 800 m extra y el recorte a
    la hoja se hace después de coser las cadenas.
    """
    minx, miny, maxx, maxy = bbox_4326
    holgura = 800.0 / 111_320.0
    minx, miny, maxx, maxy = minx - holgura, miny - holgura, maxx + holgura, maxy + holgura
    # `ST_Intersection` y no sólo `&&`: el operador de bbox devuelve la geometría **entera**
    # de todo lo que toque el recuadro, y una avenida de 2,7 km que apenas roza una esquina
    # se dibujaba completa, saliéndose kilómetros de la hoja. Acá se corta en el borde.
    # **Sin `ST_Intersection`.** Cortar en el borde del bbox parte los contornos y deja
    # todo abierto: era la razón principal de que las manzanas salieran sin cerrar. Se
    # traen enteros y el recorte se hace después, sólo sobre los que se van de la hoja
    # (una avenida de 2,7 km que apenas roza una esquina), ya con las cadenas cosidas.
    filas = conn.execute(text("""
        SELECT capa, cerrada, ST_AsBinary(geometry), fuente
        FROM mapa_base_cliente
        WHERE fuente = ANY(:f)
          AND geometry && ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, 4326)
    """), {"f": list(fuentes), "minx": minx, "miny": miny,
              "maxx": maxx, "maxy": maxy}).fetchall()
    quadras, meiofio, lotes = [], [], []
    for capa, cerrada, geom_wkb, fuente in filas:
        prioridad = fuentes.get(fuente, 0)
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
            # **Manda el flag del DWG, no que el primer punto se repita.** Una polilínea
            # cerrada de AutoCAD NO repite el vértice inicial, y el cargador la guardó tal
            # cual: exigir `coords[0] == coords[-1]` daba `False` en las **3.944** manzanas
            # que el MUB sí trae cerradas, y por eso el plano salía con todas abiertas.
            cerrada_ok = bool(cerrada) and len(partes) == 1 and len(pts) >= 3
            if capa == "DIV":
                # Los lotes ya vienen dibujados del cliente: no hay que subdividir nada.
                # Las cadenas ABIERTAS también viajan: están en su plano y hay que
                # copiarlas, aunque no sirvan como polígono para asignar la parcela.
                #
                # El anillo se guardó en la DB con el primer vértice repetido —un LINESTRING
                # no lleva flag de cerrado— y una LWPOLYLINE cerrada NO lo repite: si se
                # dibuja tal cual, sale con un vértice de más y deja de ser un calco.
                if cerrada_ok and len(pts) > 3 and \
                        math.dist(pts[0], pts[-1]) < 1e-6:
                    pts = pts[:-1]
                lotes.append((pts, cerrada_ok))
                continue
            (quadras if capa == "QUADRA" else meiofio).append(
                (pts, cerrada_ok, prioridad))
    return quadras, meiofio, lotes


def _fichas_del_cliente(conn, fuente: str, bbox_4326, tr) -> dict[str, list]:
    """Dónde puso el cliente cada ficha, por rótulo (`scripts/cargar_base_dwg.py`).

    Colocar la ficha nosotros —centrada en el lote, como pidió Jaz— coincide con la suya en
    la calle común, pero en la avenida con lote profundo se va lejos: medido contra su
    plano, en la Av. Gov. João Ponce de Arruda nuestras fichas quedaban **23,6 m** corridas
    hacia la calle, en Filinto Müller 13,8 m y en Ulisses Pompeu 10,2 m. Con su punto, la
    ficha queda donde él la puso y el plano deja de verse desplazado.

    Devuelve `{etiqueta: [(x, y, rotación_grados), …]}` en UTM. El rótulo puede repetirse
    (varios "VAZ", varios "S/NC"), así que el cruce se desempata por cercanía.
    """
    minx, miny, maxx, maxy = bbox_4326
    holgura = 400.0 / 111_320.0
    filas = conn.execute(text("""
        SELECT etiqueta, ST_X(geometry), ST_Y(geometry), rotacion
        FROM fichas_cliente
        WHERE fuente = :f
          AND geometry && ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, 4326)
    """), {"f": fuente, "minx": minx - holgura, "miny": miny - holgura,
           "maxx": maxx + holgura, "maxy": maxy + holgura}).fetchall()
    salida: dict[str, list] = {}
    for etiqueta, lon, lat, rot in filas:
        x, y = tr.transform(lon, lat)
        salida.setdefault((etiqueta or "").strip(), []).append((x, y, float(rot or 0.0)))
    return salida


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


# Palabras que NO distinguen una calle de otra: el tipo de vía y los títulos. "Av. Gov. João
# Ponce de Arruda" en OSM y "AVENIDA GOV. JOAO PONCE DE ARRUDA" en el BCI son la misma calle;
# lo único que hay que comparar es "JOAO PONCE ARRUDA".
_TIPOS_VIA = {"RUA", "R", "AVENIDA", "AV", "AVDA", "TRAVESSA", "TV", "PRACA", "PC", "PRAÇA",
              "ALAMEDA", "AL", "RODOVIA", "ROD", "ESTRADA", "ESTR", "VIELA", "BECO", "LARGO",
              "CALLE", "AVENIDA.", "PASAJE", "PJE", "DIAGONAL", "BOULEVARD", "BLVD", "VIA"}
_TITULOS_VIA = {"GOV", "GOVERNADOR", "CEL", "CORONEL", "DR", "DOUTOR", "PROF", "PROFESSOR",
                "SEN", "SENADOR", "PRESIDENTE", "PRES", "GEN", "GENERAL", "MAL", "MARECHAL",
                "SAO", "SANTA", "SANTO", "DOM", "PADRE", "PE", "ENG", "ENGENHEIRO",
                "DE", "DA", "DO", "DOS", "DAS", "E", "DEL", "LA", "LOS"}


def _clave_calle(nombre: str) -> str:
    """Núcleo comparable del nombre: sin tipo de vía, sin títulos, sin tildes.

    Es lo que permite cruzar nuestro nombre del BCI contra el `name` de OpenStreetMap, que
    viene abreviado y con tildes. Ver [[vg-calles-homonimas-cep]]: acá se borran los títulos
    a propósito, porque del otro lado tampoco están, y el cruce se valida además por
    geometría — no alcanza con que el nombre se parezca, el eje tiene que caer cerca.
    """
    palabras = [w for w in _norm(nombre).split()
                if w not in _TIPOS_VIA and w not in _TITULOS_VIA]
    return " ".join(palabras) or _norm(nombre)


def _ejes_de_calle(conn, fuente: str, bbox_4326, tr) -> dict[str, list]:
    """Ejes de calzada CON nombre, de OpenStreetMap (`scripts/cargar_calles_osm.py`).

    Es la pieza que faltaba para rotular bien: `logradouros` del IBGE trae los ejes pero
    casi sin nombre (23 sobre 3.522 tramos) y en Argentina no hay ni ejes. Sin saber qué
    calle es cada eje, el rótulo se ubicaba deduciendo la calzada desde el frente de la
    manzana, y terminaba adentro de la manzana, encima de los lotes.

    Devuelve `{clave: [LineString UTM, …]}` cosidas por calle.
    """
    minx, miny, maxx, maxy = bbox_4326
    holgura = 600.0 / 111_320.0
    filas = conn.execute(text("""
        SELECT nombre, ST_AsBinary(geometry) FROM ejes_calle
        WHERE fuente = :f
          AND geometry && ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, 4326)
    """), {"f": fuente, "minx": minx - holgura, "miny": miny - holgura,
           "maxx": maxx + holgura, "maxy": maxy + holgura}).fetchall()
    crudo: dict[str, list] = {}
    for nombre, geom_wkb in filas:
        linea = wkb.loads(bytes(geom_wkb))
        if linea.is_empty or linea.length == 0:
            continue
        crudo.setdefault(_clave_calle(nombre), []).append(
            LineString([tr.transform(x, y) for x, y in linea.coords]))
    salida: dict[str, list] = {}
    for clave, tramos in crudo.items():
        # Cosidos: Overpass devuelve la vía partida y el rótulo se corre a lo largo del eje.
        cosido = linemerge(tramos) if len(tramos) > 1 else tramos[0]
        salida[clave] = [g for g in getattr(cosido, "geoms", [cosido]) if g.length > 0]
    return salida


def _desvio_de_la_manzana(centro, ang: float, manzanas, arbol) -> float:
    """Cuántos grados se aparta el renglón del borde de manzana que tiene enfrente.

    Es el criterio del cliente leído literalmente: el nombre de la calle corre a lo largo de
    la cuadra. Se mide contra la manzana más cercana al centro del renglón —no contra el eje
    de la calle— porque es lo que se ve en el plano: dos manzanas y el nombre en el medio.
    """
    if not manzanas:
        return 0.0
    vecinas = (arbol.query(centro.buffer(_LEJOS_DE_MANZANA))
               if arbol is not None else range(len(manzanas)))
    mejor, mejor_d = None, None
    for j in vecinas:
        borde = getattr(manzanas[j], "exterior", None)
        if borde is None:
            continue
        d = borde.project(centro)
        dist = centro.distance(borde.interpolate(d))
        if mejor_d is None or dist < mejor_d:
            a = borde.interpolate(max(0.0, d - _TANGENTE_EJE))
            b = borde.interpolate(min(borde.length, d + _TANGENTE_EJE))
            mejor_d, mejor = dist, math.degrees(math.atan2(b.y - a.y, b.x - a.x))
    if mejor is None:
        return 0.0
    return abs(((ang - mejor + 90) % 180) - 90)


def _ejes_de_esta_calle(nombre: str, ejes: dict[str, list],
                        punto: Point) -> tuple[str, list]:
    """Los tramos de eje de esta calle, ordenados por cercanía a este punto.

    Dos guardas, porque poner el nombre en la calle equivocada es peor que no ponerlo:

    1. **La clave tiene que coincidir**, exacta o muy parecida (`difflib` ≥ 0,82). Es lo que
       salva "JOAO LIBANIO" contra "JOAO LIBANO" o "CLOVIS HUGNEY" contra "CLOVIS HUGUENEY",
       que son la misma calle escrita distinto en el BCI y en OSM.
    2. **El eje tiene que caer cerca del punto** que ya calculamos desde la manzana
       (`_CERCA_DEL_EJE`). Un nombre parecido en la otra punta de la ciudad no se toma.
    """
    clave = _clave_calle(nombre)
    # **La cota de 45 m es para el match DUDOSO, no para el exacto.** Existe porque un
    # nombre parecido en la otra punta de la ciudad no puede ganar; pero cuando la clave
    # coincide letra por letra no hay a quién confundir, y el punto deducido puede estar
    # legítimamente lejos —viene de la cara de la manzana y en una esquina o una avenida se
    # va—. Con la cota aplicada al match exacto, 22 de 114 rótulos se quedaban sin eje y
    # caían por deducción: Ataulfo Alves terminaba a 109 m de su propia calle y Escolástico
    # Pinto a 349 m, los dos apoyados sobre la calzada de Santa Laura, que es lo que se
    # leía como "los nombres de las transversales cruzados sobre la avenida".
    if clave in ejes:
        return clave, sorted(ejes[clave], key=punto.distance)
    candidatas = difflib.get_close_matches(clave, list(ejes), n=3, cutoff=0.82)
    mejor, mejor_d = None, _CERCA_DEL_EJE
    for c in candidatas:
        for tramo in ejes.get(c, ()):
            d = punto.distance(tramo)
            if d < mejor_d:
                mejor, mejor_d = c, d
    if mejor is None:
        return "", []
    # **Se devuelven TODOS los tramos de esa calle, no sólo el más cercano.** Una avenida
    # llega cosida en varios pedazos y el pedazo de al lado puede ser justo el que entra en
    # una manzana; con la lista entera el rótulo se va a buscar lugar a la cuadra siguiente.
    return mejor, sorted(ejes[mejor], key=punto.distance)


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
    # La misma mediana, pero en m²: `geoms` está en grados y el contorno se dibuja en UTM.
    _areas_utm = sorted(p_.area for p_ in
                        (_geom_utm(g, tr) for g in geoms if not g.is_empty) if p_)
    mediana_utm = _areas_utm[len(_areas_utm) // 2] if _areas_utm else 0
    rotas = [g for g in geoms if mediana and g.area > mediana * 200]
    if rotas:
        logger.warning(f"{len(rotas)} parcela(s) con geometría desproporcionada "
                       f"(> 200x la mediana). No definen la hoja, pero sí se dibujan.")

    engine = get_engine()
    with engine.connect() as conn:
        # El MUB del cliente y, además, las manzanas que sacamos de OSM para esta región
        # (`scripts/cargar_manzanas_osm.py`). Las segundas rellenan donde la suya no cierra.
        region_id = conn.execute(text(
            "SELECT region_id FROM surveys WHERE survey_id = :s"),
            {"s": input.survey_id}).scalar()
        # Prioridad creciente: el MUB municipal, después las caras de OSM y arriba de
        # todo el DWG de entrega que el cliente nos devolvió (`cargar_base_dwg.py`), que
        # trae sus manzanas **y sus lotes**.
        fuentes = {input.fuente_mapa_base: 0}
        if region_id:
            fuentes[f"OSM_{region_id}"] = 1
            # El MUB municipal RECORTADO a la zona (`cargar_base_dwg.py --prefijo MUB`).
            # Va por encima de OSM porque es el catastro del municipio y no una cara de la
            # red de calles: en la zona piloto trae 111 manzanas contra las 39 de OSM y las
            # 42 del MUB general. Y por debajo del `AC_`, que es el dibujo del propio cliente.
            fuentes[f"MUB_{region_id}"] = 2
            # El plano VIEJO del mismo cliente (`cargar_base_dwg.py --prefijo ACV`). Va
            # arriba del MUB y de OSM porque sigue siendo SU línea, y abajo del vigente.
            # En Várzea cierra la manzana de Ponce de Arruda que el plano de 2026 deja
            # como una cadena abierta de 1,3 km.
            fuentes[f"ACV_{region_id}"] = 3
            fuentes[f"AC_{region_id}"] = 4
        quadras, meiofio, lotes_cliente = _mapa_base(conn, fuentes, bbox, tr)
        # **Recorte al relevamiento.** El bbox es un rectángulo y la zona suele ser un
        # corredor, así que su bbox ya vale varias veces la zona; encima, una manzana que
        # apenas TOCA ese rectángulo se dibuja ENTERA y se va mucho más lejos. Medido en la
        # zona piloto: 21,8 ha de zona y una hoja de 1.503 × 1.230 m (181 ha), con 71 de 100
        # manzanas caídas fuera del relevamiento. El plano se abría con los datos reducidos
        # a una mancha en el medio.
        # Se descarta la manzana que no toca la huella real del relevamiento (la unión de
        # las parcelas) más el mismo margen que ya se usa para traer la base. No se recorta
        # la geometría de la manzana —eso la deformaría—: se la incluye entera o no se la
        # incluye. `prioridad_de` indexa por id(), así que filtrar la lista no lo rompe.
        huella_utm = unary_union([g for g in (_geom_utm(x, tr) for x in geoms) if g is not None])
        if not huella_utm.is_empty:
            # `geoms` viene en 4326 y las manzanas ya están proyectadas: se compara en UTM,
            # que además permite expresar el margen en metros y no en grados.
            recorte = huella_utm.buffer(input.margen_base_m)
            def _toca(t):
                pts = t[0]
                if len(pts) < 2:
                    return False
                g = Polygon(pts) if (t[1] and len(pts) >= 3) else LineString(pts)
                # ⚠ `buffer(0)` va SÓLO sobre el polígono, donde repara auto-intersecciones.
                # Sobre una línea devuelve POLYGON EMPTY, y un vacío no interseca nada: el
                # recorte se comía el 100 % de las cadenas ABIERTAS —las 88 de manzana que
                # el cliente dibuja sueltas incluidas— y las manzanas del borde salían sin
                # cerrar. El log lo cantaba como "0 cadenas de borde".
                if isinstance(g, Polygon):
                    g = g.buffer(0)
                return g.intersects(recorte)
            def _geom(t):
                pts = t[0]
                if len(pts) < 2:
                    return None
                g = Polygon(pts) if (t[1] and len(pts) >= 3) else LineString(pts)
                if isinstance(g, Polygon):
                    g = g.buffer(0)
                return None if g.is_empty else g

            n_q, n_m = len(quadras), len(meiofio)
            geoms_q = [_geom(q) for q in quadras]
            dentro = {i for i, (g, q) in enumerate(zip(geoms_q, quadras))
                      if q[2] >= _PRIO_CLIENTE or (g is not None and g.intersects(recorte))}
            # **Un salto de vecindad.** Una manzana sin nada relevado adentro igual hace
            # falta: la unidad del borde da a la calle, y la vereda de enfrente es esa
            # manzana. Sin ella el lote del borde queda con la calle abierta al vacío.
            # Se suma UNA vuelta de vecinas a menos del ancho de una calle, que trae la
            # cara de enfrente sin arrastrar el barrio entero —que es lo que el recorte
            # vino a evitar—.
            if dentro:
                cerca = unary_union([geoms_q[i] for i in dentro]).buffer(_SALTO_VECINA_M)
                vecinas = {i for i, g in enumerate(geoms_q)
                           if i not in dentro and g is not None and g.intersects(cerca)}
            else:
                vecinas = set()
            quadras = [q for i, q in enumerate(quadras) if i in dentro or i in vecinas]
            meiofio = [m for m in meiofio if _toca(m)]
            logger.info(f"Recorte al relevamiento (+{input.margen_base_m:.0f} m): "
                        f"manzanas {n_q}→{len(quadras)} ({len(vecinas)} por vecindad), "
                        f"meiofio {n_m}→{len(meiofio)}")
        calles_geom, calles_datos = _calles(conn, bbox, tr)
        # Los ejes de calzada CON nombre, de OSM: es lo que decide dónde va cada rótulo.
        ejes_osm = _ejes_de_calle(conn, f"OSM_{region_id}", bbox, tr) if region_id else {}
        fichas_ac = (_fichas_del_cliente(conn, f"AC_{region_id}", bbox, tr)
                     if region_id else {})
        num_lindero = _numeros_de_lindero(conn, input.survey_id)

    # **Modo calco.** Cuando la región tiene la base del DWG que devolvió el cliente, el
    # dibujo de manzanas y lotes es *el suyo*, copiado tal cual: nada de mezclarlo con OSM
    # ni de completar con lotes armados acá. Es lo que pidió Jaz —"hacé un copy y paste de
    # las manzanas y lotes del DWG"— después de ver que lo que agregábamos nosotros salía
    # como polilíneas raras (38 lotes, uno de 23.003 m², y 9 líneas sueltas).
    calco = bool(lotes_cliente)
    if calco:
        logger.info(f"Modo calco: manzanas y lotes salen del DWG del cliente "
                    f"({len(lotes_cliente)} lotes en la base).")

    # Sin `logradouros` (todo lo que no sea Brasil) los ejes salen de las parcelas.
    ejes_reales = bool(calles_geom)
    if not calles_geom:
        calles_geom = _ejes_desde_parcelas(rows, geoms, tr)

    if not quadras and not meiofio:
        logger.warning(
            f"El mapa base '{input.fuente_mapa_base}' no cubre este relevamiento: el plano "
            f"sale sin QUADRA ni MEIOFIO. Cargalo con scripts/cargar_mapa_base.py")

    doc = ezdxf.readfile(str(_PLANTILLA))   # trae bloques, capas y estilos del cliente
    _sumar_pisos_al_sdu(doc)
    _estilo_trazos(doc)
    if input.dxfversion:
        doc.dxfversion = ezdxf.const.acad_release_to_dxf_version.get(
            input.dxfversion.upper(), input.dxfversion.upper())
    msp = doc.modelspace()

    # 1. Mapa base del cliente, **cosido**. El MUB guarda cada manzana como decenas de
    # tramos sueltos (702 de `QUADRA` en el entorno de este relevamiento) y el recorte al
    # bbox los parte todavía más: así salían 112 pedazos de contorno, ninguno cerrado, que
    # es lo que se ve como manzanas abiertas. `linemerge` los vuelve a coser y las cadenas
    # que dan la vuelta entera se dibujan cerradas.
    hoja = box(*MultiPoint([Point(*tr.transform(p.x, p.y)) for p in puntos]).buffer(
        input.margen_base_m).bounds)
    # **Un solo juego de manzanas y sólo las cerradas.**
    #
    # Teníamos dos fuentes superpuestas —el MUB del municipio y las caras de OSM— que no
    # coinciden: un lote recortado contra una manzana del MUB cruzaba el borde de la de
    # OSM que la pisa, y así 301 de 566 lotes "sobresalían". Se prefiere OSM, que cubre
    # todo el relevamiento, y del MUB se rescata sólo lo que OSM no cubre.
    #
    # Y sólo se dibujan las CERRADAS: las 40 cadenas abiertas del MUB son las líneas
    # sueltas que cruzaban por el medio de las manzanas y de la calzada.
    # Se recorren de mayor a menor prioridad y cada una descarta lo que ya está cubierto:
    # el DWG del cliente manda sobre OSM y OSM sobre el MUB.
    por_prioridad: dict[int, list] = {}
    for pts, cerrada, prioridad in quadras:
        # La abierta a la que sólo le falta un lado cuenta como anillo: es como el cliente
        # dibuja la manzana de Ponce de Arruda en su plano viejo —14 m de hueco contra un
        # lado de 114— y sin esto esa cuadra se queda sin manzana. La cadena de borde de
        # verdad sigue afuera: cerrarla es lo que metía polígonos cruzando lotes.
        if (not cerrada and not _falta_un_lado(pts)) or len(pts) < 3:
            continue
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if not isinstance(poly, Polygon) or poly.area < _AREA_MIN_MANZANA:
            continue
        if prioridad < _PRIO_CLIENTE and not poly.intersects(hoja):
            continue
        if prioridad < 2:
            # **La cara de OSM llega al EJE de la calle, no a la línea municipal.**
            # `polygonize` cierra las caras contra los ejes de calzada, así que cada cara se
            # come media calle por lado: las manzanas quedan pegadas entre sí, sin calle en
            # el medio, y el nombre de calle no tiene dónde ir — en Malvinas 50 de 91
            # rótulos caían encima de una manzana. Se retira media calzada.
            # No se toca la del cliente (prioridad 2): la suya ya es la línea municipal.
            achicada = poly.buffer(-_RETIRO_LINEA_MUNICIPAL, join_style=2)
            if isinstance(achicada, MultiPolygon) and not achicada.is_empty:
                achicada = max(achicada.geoms, key=lambda g: g.area)
            # Una cara chica no es una manzana con calles alrededor: se deja como está.
            if isinstance(achicada, Polygon) and \
                    achicada.area > poly.area * _MIN_TRAS_RETIRO:
                poly = achicada.simplify(0.2)
        por_prioridad.setdefault(prioridad, []).append(poly)

    anillos_dib, cubierto = [], None
    prioridad_de: dict[int, int] = {}
    if calco:
        # Ni una manzana de OSM ni del MUB: no coinciden con las suyas y se ven como una
        # segunda línea corrida al lado de la buena.
        # **La prioridad del cliente no se escribe a mano.** Estaba fijada en `2`, que era
        # la del `AC_` cuando se escribió esto; al insertarse `MUB_<region>` en el 2 el
        # `AC_` pasó al 3 y el calco se quedaba filtrando por una fuente que en Várzea no
        # existe: 0 manzanas, con los 545 lotes del cliente dibujados sueltos. Se toma el
        # nivel más alto presente, que es de donde salen los lotes que encendieron `calco`.
        top = max(por_prioridad, default=None)
        # **Relleno donde él no dibujó manzana.** Sobre Ponce de Arruda el cliente sólo
        # traza una cadena de borde de 1,3 km, sin anillo cerrado, y las fichas de esa
        # cuadra quedaban en el aire. Se admite UNA manzana de otra fuente sólo si (a) no
        # pisa ninguna suya —el solape mata la "segunda línea corrida al lado de la buena"
        # que motivó el calco— y (b) hay una parcela relevada adentro que hoy no tiene
        # manzana. Sin las dos condiciones no entra: no es volver al híbrido.
        suyas = [p for p in por_prioridad.get(top, [])] if top is not None else []
        relleno = []
        if suyas:
            union_suyas = unary_union(suyas)
            # **Huérfana = su punto NO cae dentro de ninguna manzana.** Con `intersects`
            # bastaba que un anillo le rozara una esquina para darla por cubierta, y el
            # relleno siguiente ya no la rescataba: quedaba la ficha en el aire igual.
            huerfanas = [g.representative_point() for g in (_geom_utm(x, tr) for x in geoms)
                         if g is not None and not union_suyas.contains(g.representative_point())]
            # De mayor a menor prioridad: primero el plano viejo del propio cliente, que es
            # su línea, y sólo después OSM o el MUB para lo que aquél no llegue a tapar.
            # El criterio de admisión NO es el solape entre rellenos —eso dejaba huecos
            # sin cubrir— sino si la candidata rescata alguna parcela que siga huérfana.
            sin_cubrir = list(huerfanas)
            for prio in sorted(por_prioridad, reverse=True):
                if prio == top or not sin_cubrir:
                    continue
                for poly in por_prioridad[prio]:
                    if poly.area < _AREA_MIN_MANZANA_RELLENO:
                        continue
                    if poly.intersection(union_suyas).area > _SOLAPE_MAX_RELLENO * poly.area:
                        continue
                    rescata = [h for h in sin_cubrir if poly.contains(h)]
                    if not rescata:
                        continue
                    relleno.append(poly)
                    sin_cubrir = [h for h in sin_cubrir if h not in rescata]
        # Se quedan TODOS los dibujos del cliente (AC_ y ACV_), no sólo el vigente: es el
        # copy-paste que pidió Jaz. OSM y el MUB siguen fuera salvo como relleno.
        por_prioridad = {p_: v for p_, v in por_prioridad.items() if p_ >= _PRIO_CLIENTE}
        if relleno:
            por_prioridad[top] = por_prioridad[top] + relleno
            logger.info(f"Relleno de manzana donde el cliente no dibujó ninguna: "
                        f"{len(relleno)} agregada(s).")
    for prioridad in sorted(por_prioridad, reverse=True):
        nivel = []
        for poly in por_prioridad[prioridad]:
            # Lo del cliente NO se descarta por estar cubierto: sus dos planos se pegan
            # los dos, encimados si hace falta. El descarte es para OSM/MUB, donde dos
            # juegos superpuestos hacían que un lote cruzara el borde del otro.
            if prioridad < _PRIO_CLIENTE and cubierto is not None and \
                    poly.intersection(cubierto).area > poly.area * 0.02:
                continue        # ya la cubre una manzana de una fuente mejor
            prioridad_de[id(poly)] = prioridad
            nivel.append(poly)
        anillos_dib.extend(nivel)
        if nivel:
            cubierto = unary_union(nivel) if cubierto is None else \
                unary_union([cubierto, unary_union(nivel)])

    # `polygonize` devuelve tanto la cara grande como las manzanas que quedan adentro de
    # ella: sin filtrarlas quedan 51 pares de manzanas encimadas, y un lote recortado
    # contra la chica cruza el borde de la grande. Se descarta la que contiene a otra.
    # **Una manzana no tiene una calle adentro.** Cuando a la red de OSM le falta un tramo,
    # `polygonize` fusiona dos o tres manzanas en una sola cara que se traga la calzada:
    # en esta hoja eran 18 caras, dos de ellas con tres avenidas adentro, y los lotes que
    # caían ahí quedaban dibujados en el medio de la calle. Si un eje de calle recorre más
    # de `_CALLE_ADENTRO` metros por dentro de la cara, no es una manzana.
    # ⚠ Sólo con ejes REALES. Los deducidos de las parcelas son rectas por mínimos
    # cuadrados que atraviesan manzanas enteras: con ellos el filtro descartaba 101 de las
    # 247 manzanas de Malvinas y el plano se quedaba casi sin lotes.
    if calles_geom and ejes_reales:
        limpias = []
        for poly in anillos_dib:
            # Las del DWG del cliente son manzanas de verdad y no se cuestionan: el eje
            # del IBGE viene con su propio error de trazado y descartaría manzanas buenas.
            if prioridad_de.get(id(poly), 0) >= 2:
                limpias.append(poly)
                continue
            adentro = 0.0
            for eje in calles_geom:
                if not poly.intersects(eje):
                    continue
                adentro = max(adentro, poly.intersection(eje).length)
                if adentro > _CALLE_ADENTRO:
                    break
            if adentro <= _CALLE_ADENTRO:
                limpias.append(poly)
        if len(limpias) < len(anillos_dib):
            logger.info(f"Manzanas descartadas por tener una calle adentro: "
                        f"{len(anillos_dib) - len(limpias)}")
        anillos_dib = limpias

    anillos_dib.sort(key=lambda g: g.area)
    arbol_ani = STRtree(anillos_dib)
    quedan = []
    for i, poly in enumerate(anillos_dib):
        # Las del cliente se dibujan como vengan. El filtro está para las caras espurias de
        # `polygonize`; en su DWG hay manzanas anidadas de verdad —una de 62.500 m² con un
        # cantero de 560 m² adentro— y descartarlas dejaba 14 manzanas sin contorno, con
        # sus lotes flotando sueltos.
        if prioridad_de.get(id(poly), 0) >= 2:
            quedan.append(poly)
            continue
        contiene_otra = any(
            k != i and poly.contains(anillos_dib[k].representative_point())
            and anillos_dib[k].area < poly.area * 0.9
            for k in arbol_ani.query(poly))
        if not contiene_otra:
            quedan.append(poly)
    if len(quedan) < len(anillos_dib):
        perdidas = {}
        for poly in anillos_dib:
            if poly not in quedan:
                pr = prioridad_de.get(id(poly), 0)
                perdidas[pr] = perdidas.get(pr, 0) + 1
        logger.info(f"Manzanas descartadas por contener a otra: {perdidas}")
    anillos_dib = quedan

    for poly in anillos_dib:
        msp.add_lwpolyline(list(poly.exterior.coords)[:-1], close=True,
                           dxfattribs={"layer": "QUADRA"})
    quadras_dib = [(list(p.exterior.coords)[:-1], True) for p in anillos_dib]

    # **Los lotes del cliente, calcados.** Se copian TODOS los que entran en la hoja, tenga
    # o no ficha ese lote: en su plano están todos y el dibujo se lee como el suyo. Después
    # nadie vuelve a tocar la capa `DIV`.
    # Se guardan los vértices ORIGINALES además del polígono. El `buffer(0)` que repara un
    # lote auto-intersectado mueve vértices, y dibujar el reparado deja de ser un calco: eran
    # 10 lotes que no coincidían con el DWG. El reparado se usa para asignar la parcela, los
    # originales para dibujar.
    lotes_ac, lotes_ac_pts, div_abiertos = [], [], []
    for pts, cerrada in lotes_cliente:
        if not cerrada:
            if len(pts) >= 2 and LineString(pts).intersects(hoja):
                div_abiertos.append(pts)
            continue
        g = Polygon(pts)
        if not g.is_valid:
            g = g.buffer(0)
        if isinstance(g, Polygon) and g.area >= _AREA_MIN_LOTE and g.intersects(hoja):
            lotes_ac.append(g)
            lotes_ac_pts.append(pts)
    if calco:
        for pts in lotes_ac_pts:
            msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "DIV"})
        div_cerradas = 0
        for pts in div_abiertos:
            cerrar = _falta_un_lado(pts)
            div_cerradas += cerrar
            msp.add_lwpolyline(pts, close=cerrar, dxfattribs={"layer": "DIV"})
        # Y las cadenas de borde de manzana que él dibuja abiertas: sin ellas 37 manzanas
        # se quedan sin contorno. Las que sólo tienen un lado sin trazar se cierran; las
        # cadenas de verdad NO, que es lo que metía polígonos cruzando lotes.
        # **Sólo las del plano VIGENTE.** Las cadenas del plano viejo del cliente dicen casi
        # lo mismo unos metros corridas: dibujarlas encima es la doble línea que el calco
        # vino a sacar. Del viejo se aprovecha sólo lo que cierra manzana, vía el relleno.
        quadra_abiertas = quadra_cerradas = 0
        for pts, cerrada, prioridad in quadras:
            if cerrada or prioridad < _PRIO_CLIENTE or len(pts) < 2:
                continue
            if _falta_un_lado(pts):
                continue   # ya salió como anillo cerrado por el camino de los polígonos
            cerrar = _falta_un_lado(pts)
            quadra_cerradas += cerrar
            msp.add_lwpolyline(pts, close=cerrar, dxfattribs={"layer": "QUADRA"})
            quadra_abiertas += 1
        logger.info(f"Calcado del DWG del cliente: {len(anillos_dib)} manzanas cerradas + "
                    f"{quadra_abiertas} cadenas de borde ({quadra_cerradas} cerradas por lado faltante) · "
                    f"{len(lotes_ac)} lotes + "
                    f"{len(div_abiertos)} cadenas de lote ({div_cerradas} cerradas por lado faltante)")
    meiofio_dib = _cadenas_de_mapa_base(meiofio, hoja)
    if input.meiofio:
        for pts, cerrada in meiofio_dib:
            msp.add_lwpolyline(pts, close=cerrada, dxfattribs={"layer": "MEIOFIO"})
    # Las manzanas cerradas del MUB son la referencia para apoyar los lotes: se quedan
    # sólo las que tienen tamaño de manzana, no los islotes de dos metros.
    manzanas = list(anillos_dib)
    arbol_manzanas = STRtree(manzanas) if manzanas else None

    # Todos los tramos de `QUADRA`, cierren o no: son la línea de manzana a la que se
    # apoyan las filas cuando el anillo no está cerrado.
    segmentos_quadra = []
    for pts, cerrada in quadras_dib:
        anillo = list(pts) + ([pts[0]] if cerrada and len(pts) > 2 else [])
        for a, b in zip(anillo, anillo[1:]):
            if a != b and math.dist(a, b) >= _LADO_MIN_MANZANA:
                segmentos_quadra.append(LineString([a, b]))
    arbol_quadra = STRtree(segmentos_quadra) if segmentos_quadra else None
    conteo: dict[int, int] = {}
    for mm in manzanas:
        conteo[prioridad_de.get(id(mm), 0)] = conteo.get(prioridad_de.get(id(mm), 0), 0) + 1
    logger.info(f"Manzanas cerradas utilizables: {len(manzanas)} "
                f"(cliente {conteo.get(2, 0)}, OSM {conteo.get(1, 0)}, "
                f"MUB {conteo.get(0, 0)})")

    n_cerradas = sum(1 for _pts, c in quadras_dib + meiofio_dib if c)
    logger.info(f"Mapa base cosido: {len(quadras)}+{len(meiofio)} tramos -> "
                f"{len(quadras_dib)}+{len(meiofio_dib)} cadenas, {n_cerradas} cerradas.")

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
    if isinstance(limite, Polygon) and input.limite_celula:
        # `limite` YA está en UTM: se armó sobre `nube_utm`, que son los puntos ya
        # transformados. Pasarlo otra vez por `tr` lo trata como lon/lat, se sale del
        # dominio de la proyección y pyproj devuelve `inf` en los 158 vértices. Un `inf`
        # en un real hace que AutoCAD descarte el dibujo entero («Invalid or incomplete
        # DXF input»), y el auditor de ezdxf no lo ve porque valida estructura, no números.
        msp.add_lwpolyline(list(limite.exterior.coords),
                           close=True, dxfattribs={"layer": "LIMITE"})

    # 3. Un bloque por inmueble.
    n_sdu = n_mdu = hp_total = sin_codlog = n_al_frente = n_div = 0
    fichas: list[dict] = []
    rotulos_calle: dict[tuple, list] = {}
    contadores: list[tuple] = []
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

        # La parcela en UTM es el ancla de todo lo que sigue: el contorno que se dibuja,
        # dónde va la ficha y dónde va el contador.
        poli = _geom_utm(geom, tr)
        frente = _frente_de_parcela(poli, calles_geom) if poli is not None else None


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
        elif not es_mdu and tp != "E" and uf_v > 0 and uf_c > 0:
            # Sin dos puntos utilizables igual se separan: van en el MISMO lote, el
            # comercio adelante y la vivienda al fondo, que es como están en la calle.
            bloques = [("C", uf_c, (cx, cy)), ("R", uf_v, (cx, cy))]

        # Lo que es de la PARCELA y no del bloque se cuenta una sola vez: el CEP para el
        # rótulo de calle y la guarda de CODLOG, que mide parcelas sin código, no bloques.
        cep_num = re.sub(r"\D", "", cep or "")
        if calle:
            por_calle.setdefault(_nombre_calle(calle), []).append((cx, cy, cep_num))
        codlog = codlog_map.get(_norm(calle or ""), "")
        if not codlog:
            sin_codlog += 1

        # Nada se dibuja todavía: la posición de cada ficha depende de cómo quede
        # alineada la cara de manzana entera, y eso recién se sabe con todos los lotes.
        # A qué manzana pertenece el lote: la que lo contiene y, si ninguna, la que más
        # superficie comparte. Sin manzana, la fila se arma sola como antes.
        i_mz = None
        if poli is not None and arbol_manzanas is not None:
            cand = [k for k in arbol_manzanas.query(poli)
                    if manzanas[k].intersects(poli)]
            if cand:
                i_mz = max(cand, key=lambda k: manzanas[k].intersection(poli).area)
                if manzanas[i_mz].intersection(poli).area < poli.area * _MIN_EN_MANZANA:
                    i_mz = None
            # Sin solape suficiente NO se abandona la parcela: se adopta la manzana más
            # cercana dentro de `_DIST_MAX_MANZANA`. Antes quedaba en None y entonces el
            # lote no se dibujaba (correcto) pero **la ficha salía igual**, y sin manzana
            # que la apoye caía en la calle: 106 de 464 fichas fuera de toda manzana en la
            # zona piloto. La manzana manda y el lote es una guía adentro de ella, así que
            # lo que corresponde es meter la parcela en la manzana que le toca, no soltarla.
            if i_mz is None:
                cerca = [(manzanas[k].distance(poli), k)
                         for k in arbol_manzanas.query(poli.buffer(_DIST_MAX_MANZANA))]
                cerca = [(d, k) for d, k in cerca if d <= _DIST_MAX_MANZANA]
                if cerca:
                    i_mz = min(cerca)[1]

        fichas.append({
            "poli": poli, "frente": frente, "bloques": bloques, "es_mdu": es_mdu,
            "manzana": i_mz,
            "num": num, "complemento": complemento, "cep": cep_num, "codlog": codlog,
            "pisos": pisos_texto(pisos or pisos_sat), "uf_v": uf_v, "uf_c": uf_c,
            "calle": _nombre_calle(calle) if calle else "", "punto": (cx, cy),
        })
        hp_total += uf_v + uf_c

    # 3.b. Contador por parcela — los bloques `HCSDU` / `HCMDU` del cliente.
    #
    # Es donde su plano publica **a la vista** cuántas unidades hay en el lote: la ficha
    # sólo muestra el sufijo `-N` cuando N > 1, así que un lote de una unidad no dice
    # ninguna cantidad. Él pone 145 `HCSDU` y 12 `HCMDU` sobre 403 fichas.
    #
    # Semántica leída de su DWG: `QTD_SDU` es la **suma de los `QT`** de la parcela —
    # coincide en 135 de sus 145 contadores, contra 125 si fuera la cantidad de bloques—,
    # mientras que `QTD_MDU` cuenta edificios (12 contadores suman 13), porque las unidades
    # del MDU ya viajan en `HP_TOTAL`.
    #
    # Va en la misma banda de 3 m que las fichas —mediana medida: 3,11 m del borde— y
    # corrido hacia atrás sobre el frente para no taparlas.
    # ------------------------------------------------------------------ PASE 2
    # Ahora sí se dibuja. Los lotes de una misma cara de manzana se alinean juntos, y de
    # esa fila salen las tres cosas: el rectángulo del lote, la ficha sobre el frente y el
    # contador. Antes cada parcela se resolvía sola contra el mapa municipal, y por eso el
    # conjunto quedaba en escalera.
    por_calle_fichas: dict[str, list] = {}
    sueltas = []
    for i, rec in enumerate(fichas):
        if rec["frente"] is None or rec["poli"] is None:
            sueltas.append(rec)
            continue
        pf, u, n = rec["frente"]
        # La cara es de UNA manzana y UNA calle: agrupando sólo por calle, una fila podía
        # arrancar en una manzana y terminar en la de al lado, cruzando la bocacalle.
        por_calle_fichas.setdefault((rec["manzana"], rec["calle"]), []).append(
            (i, rec["poli"], pf, u, n))

    n_cont = n_log = n_apretados = 0
    # Las cajas de todos los renglones de ficha: el nombre de calle se coloca al final,
    # cuando ya están todas, porque el rótulo cae en la calzada y ahí lo pueden alcanzar
    # los renglones de las DOS veredas, no sólo los de su propia fila.
    cajas_todas = _Ocupadas()
    pendientes_log: list[tuple] = []
    # Todas las filas primero, para poder mirarlas juntas: una fila sola no sabe que está
    # entrando en la de la calle que cruza.
    filas = []
    for (i_mz, nombre), marcas in por_calle_fichas.items():
        mz = manzanas[i_mz] if i_mz is not None else None
        for cara in _caras_de_manzana(marcas):
            puntos_cara = [m[2] for m in cara]
            borde = (_borde_mas_cercano(mz, puntos_cara) if mz is not None
                     else _borde_suelto(segmentos_quadra, arbol_quadra, puntos_cara))
            fila = _fila_de_lotes(cara, borde)
            if fila:
                filas.append((nombre, fila, mz))
    sin_borde = sum(1 for _n, _f, mz in filas if mz is None)
    logger.info(f"Filas: {len(filas)} ({sin_borde} sin manzana cerrada) · "
                f"fichas sueltas sin frente: {len(sueltas)}")
    factores = _achicar_filas_que_chocan([f for _n, f, _mz in filas])

    # Se parte cada manzana por dentro, una celda por parcela, y el texto va centrado en
    # su celda. Es lo que hace imposible el solape: dos renglones no pueden compartir
    # celda, y las celdas no se pisan.
    idx_mz = {id(mm): k for k, mm in enumerate(manzanas)}
    semillas: dict = {}
    contenedor: dict = {}
    for i_f, (_n, fila, mz) in enumerate(filas):
        f_f = factores.get(i_f, 1.0)
        # Con manzana cerrada, la clave es la manzana: sus lotes se reparten el polígono
        # entero. Sin manzana, la clave es la fila y el contenedor es su propia franja:
        # así **todas** las parcelas quedan con celda, no sólo las 23 manzanas cerradas.
        clave = ("mz", idx_mz.get(id(mz))) if mz is not None else ("fila", i_f)
        if clave[1] is None:
            clave = ("fila", i_f)
        rects = []
        for i_l, (_i, _e, orig, (ux, uy), (nx, ny), s0, s1, base, fondo_b) in enumerate(fila):
            fondo = fondo_b * f_f
            d = min(_RETIRO_QUADRA, fondo * 0.35)
            semillas.setdefault(clave, []).append(
                (i_f, i_l, (orig[0] + ux * (s0 + s1) / 2 + nx * d,
                            orig[1] + uy * (s0 + s1) / 2 + ny * d)))
            rects.append(Polygon([
                (orig[0] + ux * s0 + nx * base, orig[1] + uy * s0 + ny * base),
                (orig[0] + ux * s1 + nx * base, orig[1] + uy * s1 + ny * base),
                (orig[0] + ux * s1 + nx * (base + fondo),
                 orig[1] + uy * s1 + ny * (base + fondo)),
                (orig[0] + ux * s0 + nx * (base + fondo),
                 orig[1] + uy * s0 + ny * (base + fondo)),
            ]))
        if clave[0] == "mz":
            contenedor[clave] = manzanas[clave[1]]
        elif rects:
            union = unary_union(rects)
            if isinstance(union, MultiPolygon):
                union = max(union.geoms, key=lambda g: g.area)
            contenedor.setdefault(clave, union)

    # **La manzana se subdivide en franjas geométricas, no con el contorno catastral.**
    # El polígono real deja lotes que se salen de la manzana o que no la llenan, y el
    # conjunto se lee como "una manzana con un lote encima". Cada cara se reparte en
    # rebanadas perpendiculares al frente y a cada una se le da el área real de su
    # parcela: la forma es esquemática, la dimensión no.
    # **La manzana se subdivide con las parcelas REALES**, recortadas contra ella y con los
    # huecos repartidos entre los lotes vecinos. Es el estilo del plano de Hurlingham.
    #
    # Probé dos alternativas y Jaz las descartó: rebanar la cara en franjas de área real
    # dejaba el interior de la manzana vacío —huecos que parecen plazas donde no las hay—
    # y rebanar la manzana entera entre las pocas parcelas relevadas inflaba los lotes y
    # metía los rótulos en el medio de la manzana. El código de ambas quedó en
    # `_rebanar_por_area`, por si hay que volver.
    por_mz: dict = {}
    for i_f, (_n, fila, mz) in enumerate(filas):
        k = idx_mz.get(id(mz)) if mz is not None else None
        if k is None:
            continue
        for i_l, (i, *_r) in enumerate(fila):
            por_mz.setdefault(k, []).append((i_f, i_l, i))

    # **Primero los lotes que ya dibujó el cliente.** El DWG de entrega trae la capa `DIV`
    # con 545 lotes cerrados, uno por lote real, y 542 de nuestras 567 parcelas caen
    # adentro de uno. Donde hay lote del cliente no se inventa nada: se usa el suyo, que es
    # exactamente lo que pidió Jaz al pasarnos el archivo ("que queden así las manzanas y
    # los lotes"). Lo que quede sin lote suyo cae al reparto de siempre.
    celda_de: dict[tuple, Polygon] = {}
    del_cliente: dict[int, list] = {}
    if lotes_ac:
        arbol_lotes = STRtree(lotes_ac)
        for i_f, (_n, fila, _mz) in enumerate(filas):
            for i_l, (i, *_r) in enumerate(fila):
                parcela = fichas[i]["poli"]
                if parcela is None or parcela.is_empty:
                    continue
                # El lote del cliente que más superficie comparte con nuestra parcela. Por
                # solape y no por "contiene el punto": el catastro y su dibujo no coinciden
                # al centímetro y el punto representativo cae afuera con demasiada
                # facilidad.
                mejor, mejor_area = None, 0.0
                for j in arbol_lotes.query(parcela):
                    a = lotes_ac[j].intersection(parcela).area
                    if a > mejor_area:
                        mejor, mejor_area = j, a
                if mejor is None or mejor_area < parcela.area * _SOLAPE_MIN_LOTE:
                    continue
                del_cliente.setdefault(mejor, []).append((i_f, i_l, i))

    for j, items in del_cliente.items():
        if len(items) == 1:
            i_f, i_l, _i = items[0]
            celda_de[(i_f, i_l)] = lotes_ac[j]
            continue
        # Dos relevamientos en el mismo lote del cliente —él tiene 77 lotes así— se lo
        # reparten por Voronoi: cada rótulo necesita su propia celda para no pisarse.
        semillas = [fichas[i]["poli"].representative_point().coords[0]
                    for _f, _l, i in items]
        for (i_f, i_l, _i), celda in zip(items, _celdas_de_manzana(lotes_ac[j], semillas)):
            if celda is not None and celda.area > _AREA_MIN_LOTE:
                celda_de[(i_f, i_l)] = celda
    n_ac = len(celda_de)

    if calco:
        # Sin lote del cliente no se inventa uno: la ficha se centra en su propia parcela
        # recortada contra la manzana. La celda es sólo para ubicar el texto; no se dibuja.
        for k, items in por_mz.items():
            for i_f, i_l, i in items:
                if (i_f, i_l) in celda_de:
                    continue
                poli = fichas[i]["poli"]
                if poli is None or poli.is_empty:
                    continue
                corte = poli.intersection(manzanas[k])
                if isinstance(corte, MultiPolygon) and not corte.is_empty:
                    corte = max(corte.geoms, key=lambda g: g.area)
                if isinstance(corte, Polygon) and corte.area > _AREA_MIN_LOTE:
                    celda_de[(i_f, i_l)] = corte
                elif poli.area > _AREA_MIN_LOTE:
                    celda_de[(i_f, i_l)] = poli
        logger.info(f"Fichas ubicadas: {n_ac} en un lote suyo, "
                    f"{len(celda_de) - n_ac} en su propia parcela (él no tiene lote ahí).")

    for k, items in ({} if calco else por_mz).items():
        # Lo que ya tiene lote del cliente se saca del reparto, y su superficie se le resta
        # a la manzana: si no, las parcelas sin lote crecerían por encima de las del
        # cliente y quedarían dos lotes encimados.
        faltan = [(i_f, i_l, i) for i_f, i_l, i in items if (i_f, i_l) not in celda_de]
        if not faltan:
            continue
        libre = manzanas[k]
        ya = [celda_de[(i_f, i_l)] for i_f, i_l, _i in items if (i_f, i_l) in celda_de]
        if ya:
            libre = libre.difference(unary_union(ya))
            if isinstance(libre, MultiPolygon) and not libre.is_empty:
                libre = max(libre.geoms, key=lambda g: g.area)
            if not isinstance(libre, Polygon) or libre.area < _AREA_MIN_LOTE:
                continue
        lotes = _lotes_de_manzana(libre, [fichas[i]["poli"] for _f, _l, i in faltan])
        for (i_f, i_l, _i), lote in zip(faltan, lotes):
            if lote is not None and lote.area > _AREA_MIN_LOTE:
                celda_de[(i_f, i_l)] = lote
    if lotes_ac and not calco:
        logger.info(f"Lotes del DWG del cliente usados tal cual: {n_ac} · "
                    f"repartidos por nosotros: {len(celda_de) - n_ac}")

    # **Sin manzana no se dibuja el lote.** Antes se caía a la parcela cruda, y ésa cruza
    # el borde de las manzanas vecinas: eran 177 de 566 lotes atravesando una manzana que
    # no es la suya. Decisión de Jaz: es preferible que algún lote no salga a que el plano
    # tenga lotes montados sobre manzanas ajenas. La ficha con su rótulo sale igual.
    # **El contorno de la manzana se traza sobre el borde que forman sus lotes.** El
    # polígono de OSM es más grande que lo relevado —cubre la manzana entera, y nosotros
    # tenemos sólo una parte de sus lotes—, así que el contorno se rehace con la unión de
    # los lotes dibujados: queda una polilínea cerrada que envuelve exactamente lo que se
    # ve. En las manzanas sin lotes se deja el contorno de OSM, que da el trazado de calles.
    # ⚠ **La manzana del cliente se deja como está.** Rehacer el contorno tiene sentido
    # sobre la cara de OSM, que es más grande que lo relevado; la manzana que dibujó él ya
    # es la buena, y envolverla con nuestros lotes la achicaría hasta la parte relevada.
    lotes_por_mz: dict = {}
    for i_f, (_n, fila, mz) in enumerate(filas):
        k = idx_mz.get(id(mz)) if mz is not None else None
        if k is None or prioridad_de.get(id(manzanas[k]), 0) >= 2:
            continue
        for i_l in range(len(fila)):
            celda = celda_de.get((i_f, i_l))
            if celda is not None:
                lotes_por_mz.setdefault(k, []).append(celda)

    n_contornos = 0
    for k, trozos in lotes_por_mz.items():
        union = unary_union(trozos)
        # **Borde parejo.** Los fondos de lote no están alineados, así que la unión cruda
        # sale en diente de sierra. Un cierre morfológico —dilatar y volver a contraer—
        # rellena esas muescas sin mover el contorno general, y el `simplify` saca los
        # vértices que quedan pegados. Después se recorta contra la manzana para que el
        # engorde no se meta en la calle.
        try:
            suave = union.buffer(_SUAVIZADO_BORDE, join_style=2)
            suave = suave.buffer(-_SUAVIZADO_BORDE, join_style=2)
            suave = suave.intersection(manzanas[k]).simplify(_SIMPLIFICA_BORDE)
            if not suave.is_empty:
                union = suave
        except Exception as e:
            logger.warning(f"No se pudo emparejar el contorno de una manzana ({e}).")
        for parte in getattr(union, "geoms", [union]):
            if not isinstance(parte, Polygon) or parte.area < _AREA_MIN_LOTE:
                continue
            msp.add_lwpolyline(list(parte.exterior.coords)[:-1], close=True,
                               dxfattribs={"layer": "QUADRA"})
            n_contornos += 1
        # La manzana de OSM que ya se dibujó se borra: su contorno no coincide con el de
        # los lotes y quedarían dos líneas encimadas.
        objetivo = manzanas[k]
        for e in list(msp.query("LWPOLYLINE[layer=='QUADRA']")):
            if not e.closed:
                continue
            pts = [(x, y) for x, y, *_ in e.get_points()]
            if len(pts) == len(objetivo.exterior.coords) - 1 and \
                    math.dist(pts[0], objetivo.exterior.coords[0]) < 0.01:
                msp.delete_entity(e)
                break
    logger.info(f"Contornos de manzana trazados sobre los lotes: {n_contornos}")

    logger.info(f"Parcelas con manzana asignada: "
                f"{sum(1 for f in fichas if f.get('manzana') is not None)} de {len(fichas)}")
    dist_a_parcela: list = []
    anclados = 0
    logger.info(f"Manzanas subdivididas con parcelas reales: {len(por_mz)} · "
                f"lotes dibujados: {len(celda_de)} sobre "
                f"{sum(len(f) for _n, f, _m in filas)}")

    for i_fila, (nombre, fila, mz) in enumerate(filas):
            f_fondo = factores.get(i_fila, 1.0)
            fondo_ocupado = 0.0

            for i_lote, (i, _esq, orig, (ux, uy), (nx, ny), s0, s1, base,
                         fondo_bruto) in enumerate(fila):
                es_ultimo = i_lote == len(fila) - 1
                fondo = fondo_bruto * f_fondo
                esquinas = [
                    (orig[0] + ux * s0 + nx * base, orig[1] + uy * s0 + ny * base),
                    (orig[0] + ux * s1 + nx * base, orig[1] + uy * s1 + ny * base),
                    (orig[0] + ux * s1 + nx * (base + fondo),
                     orig[1] + uy * s1 + ny * (base + fondo)),
                    (orig[0] + ux * s0 + nx * (base + fondo),
                     orig[1] + uy * s0 + ny * (base + fondo)),
                ]
                rec = fichas[i]
                ox, oy = orig

                def _en(s_rel: float, d_rel: float, _o=orig, _u=(ux, uy), _n=(nx, ny)):
                    return (_o[0] + _u[0] * s_rel + _n[0] * d_rel,
                            _o[1] + _u[1] * s_rel + _n[1] * d_rel)

                # 3.a. El lote, como rectángulo pegado a sus vecinos sobre la manzana.
                # Recortado contra la manzana: así el lote no puede sobresalir a la
                # calle ni pisar la manzana de enfrente, que era lo que se veía como
                # rectángulos cruzados.
                celda = celda_de.get((i_fila, i_lote))
                lote = celda if celda is not None else Polygon(esquinas)
                if celda is None and mz is not None:
                    corte = lote.intersection(mz)
                    if isinstance(corte, MultiPolygon) and not corte.is_empty:
                        corte = max(corte.geoms, key=lambda g: g.area)
                    if isinstance(corte, Polygon) and corte.area > 5.0:
                        lote = corte

                if calco:
                    # **No se dibuja nada.** Los lotes son los del DWG del cliente y ya se
                    # calcaron enteros más arriba; la celda de acá existe sólo para ubicar
                    # el texto. Dibujarla además metía 38 polilíneas que no son suyas
                    # —trozos de Voronoi y sobrantes de manzana, uno de 23.003 m²— que es
                    # lo que Jaz vio como "polilíneas raras".
                    pass
                elif celda is not None:
                    # La parcela dibujada es la celda: subdivide la manzana por dentro,
                    # de borde a borde, sin dejar huecos entre lotes.
                    msp.add_lwpolyline(list(celda.exterior.coords)[:-1], close=True,
                                       dxfattribs={"layer": "DIV"})
                    n_div += 1
                else:
                    # Sin manzana cerrada no hay nada que subdividir: se marca el límite
                    # con la línea de 10 m del cliente, apoyada en la línea de manzana.
                    largo_div = min(_LARGO_DIV, fondo)
                    msp.add_line(_en(s0, base), _en(s0, base + largo_div),
                                 dxfattribs={"layer": "DIV"})
                    n_div += 1
                    if es_ultimo:
                        msp.add_line(_en(s1, base), _en(s1, base + largo_div),
                                     dxfattribs={"layer": "DIV"})
                        n_div += 1

                rot = (math.degrees(math.atan2(uy, ux)) - 90.0) % 180.0
                ancho = s1 - s0
                # La ficha va sobre el frente, como en el plano del cliente: 3 m adentro,
                # o menos si el lote es más chato que eso.
                d_ficha = min(_RETIRO_QUADRA, fondo * 0.35)
                bloques = rec["bloques"]
                altura = _H_ROTULO_MDU if rec["es_mdu"] else _H_ROTULO_SDU
                largo_max = 0.0
                punto_ficha, rot_ficha = (_en(s0 + (s1 - s0) * 0.5, base + d_ficha)
                                          if celda is None else (0.0, 0.0)), rot
                franjas = (_partir_celda(celda, (nx, ny), len(bloques))
                           if celda is not None else [])
                for i_b, (tp_b, qt_b, _pt) in enumerate(bloques):
                    # Los bloques de una parcela mixta se reparten el frente en partes
                    # iguales, cada uno centrado en la suya.
                    # Los dos inmuebles del mismo terreno, acomodados dentro de su
                    # parcela: repartidos a lo ancho **y** escalonados en profundidad, el
                    # comercio adelante y la vivienda al fondo. Probé las dos cosas por
                    # separado y las dos son peores: sólo a lo ancho, 85 renglones
                    # pisados; sólo en profundidad, 111. Las dos juntas, 70.
                    frac = (i_b + 0.5) / len(bloques)
                    # El renglón más largo de esta ficha, para que entre en el lote.
                    if rec["es_mdu"]:
                        lineas = [str(rec["uf_v"] + rec["uf_c"]), rec["pisos"],
                                  _resumen_uf(rec["uf_v"], rec["uf_c"], ""),
                                  _texto_pisos(rec["pisos"])]
                    else:
                        # Los TRES renglones que se van a dibujar: el que mida más manda
                        # el ancho de la pieza. Sin el de pisos acá, la pieza se medía
                        # corta y el renglón más largo terminaba fuera del lote.
                        # Los DOS renglones que se dibujan: rótulo y pisos.
                        lineas = [_etiqueta(rec["num"], tp_b, qt_b),
                                  "" if tp_b == "E" else _texto_pisos(rec["pisos"])]
                    # **Todas las letras del mismo tamaño**, el de la plantilla del
                    # cliente. Llegué a achicar el texto en los lotes angostos para que
                    # entrara —bajaba los renglones pisados de 128 a 76— pero un plano con
                    # tipografías de distinto cuerpo se lee peor que uno con algún rótulo
                    # apretado, y es lo que pidió Jaz. Si alguna vez hay que volver, el
                    # criterio era `ancho / (bloques × 10 m)` con piso en la mitad.
                    escala = 1.0
                    alt = altura
                    largo = _largo_texto(lineas, alt)
                    largo_max = max(largo_max, largo)
                    # Los dos bloques de una parcela mixta se escalonan en profundidad:
                    # a la misma altura, en un lote angosto, se pisan entre sí.
                    d_este = base + d_ficha + i_b * (largo + 1.0)
                    if celda is not None:
                        # La ficha entera —rótulo, resumen y contador— se coloca como una
                        # sola pieza centrada en su franja del lote.
                        franja = franjas[i_b] if i_b < len(franjas) else celda
                        n_lineas = 2   # rótulo y pisos, nada más
                        punto, rot_texto, entro = _ubicar_grupo(
                            franja, (ux, uy), (nx, ny), largo, alt,
                            _SALTO_PISOS_SDU, n_lineas)
                        # Sin plan B con menos renglones: ajustar la pieza a dos y
                        # dibujar tres deja el renglón de pisos afuera, que es justo lo
                        # que se quería adentro. Si no entra en ningún ángulo, se deja
                        # centrada y se cuenta como apretada.
                        if not entro:
                            n_apretados += 1
                    else:
                        rot_texto = rot
                        punto = _adentro_de(lote, _en(s0 + ancho * frac, d_este),
                                            _en(s0 + ancho * frac, base + fondo * 0.5))
                        punto = _ubicar_renglon(lote, punto, rot, (nx, ny), (ux, uy),
                                                largo, alt, fondo, mz, cajas_todas)
                    # Hasta dónde llega DE VERDAD el renglón: `_ubicar_renglon` puede
                    # haberlo corrido, y el nombre de calle tiene que ir más atrás que
                    # esto o se le cruza encima.
                    rr = math.radians(rot_texto)
                    fin = (punto[0] + math.cos(rr) * largo,
                           punto[1] + math.sin(rr) * largo)
                    # Con el ángulo REALMENTE dibujado: en las celdas el texto puede
                    # salir paralelo a la calle en vez de perpendicular, y registrar la
                    # caja con el otro ángulo dejaba de detectar la mitad de los choques.
                    cajas_todas.add(_caja_texto(punto, rot_texto, largo, alt))
                    for q in (punto, fin):
                        fondo_ocupado = max(
                            fondo_ocupado,
                            (q[0] - orig[0]) * nx + (q[1] - orig[1]) * ny)
                    # **Si el cliente ya puso esta ficha, va donde él la puso.** Su
                    # rótulo y el nuestro cruzan 1 a 1 (645 contra 645 en Várzea); el
                    # desempate entre rótulos repetidos —varios "VAZ", varios "S/NC"— es
                    # por cercanía a donde la habíamos calculado nosotros.
                    if calco and fichas_ac:
                        etiqueta = (str(rec["num"] or "S/N") if rec["es_mdu"]
                                    else _etiqueta(rec["num"], tp_b, qt_b))
                        suyas = fichas_ac.get(etiqueta)
                        if suyas:
                            sx, sy, srot = min(
                                suyas, key=lambda q: math.dist(punto, (q[0], q[1])))
                            if math.dist(punto, (sx, sy)) <= _SALTO_MAX_A_FICHA:
                                punto, rot_texto = (sx, sy), srot
                                anclados += 1
                    punto_ficha, rot_ficha = punto, rot_texto
                    if rec.get("poli") is not None and not rec["poli"].is_empty:
                        dist_a_parcela.append((Point(punto).distance(rec["poli"]),
                                               rec.get("calle") or "?",
                                               rec.get("num") or "?"))
                    tipo = _emitir_ficha(msp, rec, tp_b, qt_b, punto, rot_texto,
                                         input.celula_id, escala)
                    if tipo == "MDU":
                        n_mdu += 1
                    else:
                        n_sdu += 1
                n_al_frente += 1

                # 3.b. El contador de unidades — el circulito con la cantidad.
                #
                # **Apagado por defecto** (`EntregaInput.contadores`). Es del estándar del
                # cliente, pero su número repite lo que ya dice el rótulo cuando hay más
                # de una unidad, y el círculo ensucia el lote. El dato de unidades sigue
                # viajando en los atributos `QT` y `RESUMO` del propio bloque.
                if not input.contadores:
                    continue
                total_uf = max(rec["uf_v"] + rec["uf_c"], 1)
                es_mdu_c = rec["es_mdu"]
                if celda is not None:
                    # Cuelga de la ficha, un renglón más abajo y con su mismo ángulo, así
                    # se mueve con ella y no puede pisarla.
                    rr_f = math.radians(rot_ficha)
                    salto = _SALTO_PISOS_SDU * 2
                    cxx = punto_ficha[0] + math.sin(rr_f) * salto
                    cyy = punto_ficha[1] - math.cos(rr_f) * salto
                else:
                    d_cont = min(max(d_ficha + largo_max + 2.5, fondo_ocupado + 2.0),
                                 base + fondo)
                    cxx, cyy = _adentro_de(
                        lote,
                        _en(s0 + ancho * 0.5, base + d_cont),
                        _en(s0 + ancho * 0.5, base + fondo * 0.5))
                    fondo_ocupado = max(fondo_ocupado, d_cont)
                ref = msp.add_blockref(
                    "HCMDU" if es_mdu_c else "HCSDU", (cxx, cyy),
                    dxfattribs={"layer": "CONT_MDU" if es_mdu_c else "CONT_SDU",
                                "rotation": rot_ficha if celda is not None else rot})
                ref.add_auto_attribs({
                    ("QTD_MDU" if es_mdu_c else "QTD_SDU"):
                        "1" if es_mdu_c else str(total_uf),
                    "UTM": f"{int(round(cxx))},{int(round(cyy))}",
                })
                n_cont += 1

            # 4. El nombre de la calle, uno por cara, al fondo de la fila y paralelo a
            # ella: adentro de la manzana, como lo pone el cliente.
            medio = fila[len(fila) // 2]
            _i, _e, (ox, oy), (ux, uy), (nx, ny), s0, s1, base, fondo_bruto = medio
            fondo = fondo_bruto * f_fondo
            # **El nombre de calle va EN LA CALZADA**, entre manzana y manzana. Es lo
            # que pidió Jaz y además resuelve de raíz el choque con el relevamiento: en el
            # medio de la calle no hay ni un lote ni una ficha. (El cliente lo pone adentro
            # de la manzana, a 14 m del borde; esto se aparta de su plano a propósito.)
            # Se anota y se dibuja al final, con todas las fichas ya colocadas.
            # **El rótulo se ubica desde la propia cara**, no desde el eje de calle más
            # cercano. Con el eje más cercano, una cara de esquina se proyectaba sobre la
            # calle transversal: el rótulo terminaba a 55 m de mediana de sus propios
            # lotes —hasta 456 m— y varias caras caían sobre el mismo eje, apiladas (133
            # de 146 tenían otro a menos de 25 m, y en un punto había 18 juntos).
            #
            # Desde el frente de la cara y hacia afuera media calzada, el rótulo cae en la
            # calle que le corresponde, enfrente de sus lotes, sí o sí.
            s_ini = min(f[5] for f in fila)
            s_fin = max(f[6] for f in fila)
            centro_s = (s_ini + s_fin) / 2
            frente_pt = Point(ox + ux * centro_s + nx * base,
                              oy + uy * centro_s + ny * base)
            (px, py), ancho_calle = _eje_de_calzada(
                frente_pt, (nx, ny), manzanas, arbol_manzanas, _ANCHO_CALZADA)
            vx, vy = ux, uy
            pendientes_log.append((nombre, px, py, vx, vy, -nx, -ny,
                                   next((fichas[j]["cep"] for j, *_r in fila
                                         if fichas[j]["cep"]), ""), None))
            n_log += 1

    # Las que no tienen frente utilizable (sin eje de calle cerca) salen igual, en el
    # punto de la parcela: perder el inmueble sería peor que colocarlo sin fila.
    for rec in sueltas:
        rot = _rotacion_a_calle(Point(*rec["punto"]), calles_geom)
        for tp_b, qt_b, punto in rec["bloques"]:
            tipo = _emitir_ficha(msp, rec, tp_b, qt_b, punto, rot, input.celula_id)
            if tipo == "MDU":
                n_mdu += 1
            else:
                n_sdu += 1

    # Y recién ahora los nombres de calle, con todos los renglones ya colocados: el
    # rótulo cae en la calzada y lo pueden alcanzar las fichas de las DOS veredas.
    # **Como mucho dos rótulos por calle**, y los dos más separados entre sí.
    #
    # Cada cara de manzana pedía su nombre, así que una avenida que cruza el relevamiento
    # aparecía diez veces y el plano se llenaba de nombres. Con dos alcanza para orientarse
    # —uno cerca de cada punta del tramo relevado— y el dibujo respira.
    # **El rótulo se apoya sobre el eje real de la calle.** Hasta acá su posición salía de
    # caminar desde el frente de la manzana hasta la manzana de enfrente y tomar el punto
    # medio: funciona en la cuadra regular y falla en esquinas, avenidas curvas y manzanas
    # de borde, y el nombre termina adentro de la manzana, encima de los lotes. Con el eje
    # de OSM el punto ya está en la calzada por construcción, y el ángulo sale de la
    # tangente de la propia calle en vez de la dirección de la fila de lotes.
    claves_osm: dict[str, str] = {}
    if ejes_osm:
        ajustados, apoyados = [], 0
        for (nombre, px, py, vx, vy, mx, my, cep_calle, _e) in pendientes_log:
            clave_osm, tramos = _ejes_de_esta_calle(nombre, ejes_osm, Point(px, py))
            if not tramos:
                ajustados.append((nombre, px, py, vx, vy, mx, my, cep_calle, None))
                continue
            claves_osm[nombre] = clave_osm
            eje = tramos[0]
            d = eje.project(Point(px, py))
            q = eje.interpolate(d)
            a = eje.interpolate(max(0.0, d - _TANGENTE_EJE))
            b = eje.interpolate(min(eje.length, d + _TANGENTE_EJE))
            tx, ty = b.x - a.x, b.y - a.y
            largo = math.hypot(tx, ty)
            if largo < 1e-6:
                ajustados.append((nombre, px, py, vx, vy, mx, my, cep_calle, None))
                continue
            ajustados.append((nombre, q.x, q.y, tx / largo, ty / largo, mx, my,
                              cep_calle, tramos))
            apoyados += 1
        logger.info(f"Rótulos apoyados sobre el eje real de la calle: "
                    f"{apoyados} de {len(pendientes_log)}")
        pendientes_log = ajustados

    # **Un rótulo lejos de toda manzana no está en ninguna calle.** Cuando la cara que lo
    # pidió está en el borde del relevamiento, `_eje_de_calzada` no encuentra la manzana de
    # enfrente y se va caminando: había uno de la Av. Gov. João Ponce de Arruda a 408 m del
    # dibujo, solo en el medio del campo. Se descartan, salvo que la calle se quede sin
    # ninguno: en ese caso se conserva el menos malo.
    def _lejania(p_log) -> float:
        if not manzanas:
            return 0.0
        q = Point(p_log[1], p_log[2])
        vecinas = (arbol_manzanas.query(q.buffer(_LEJOS_DE_MANZANA))
                   if arbol_manzanas is not None else [])
        if len(vecinas) == 0:
            return min(q.distance(m) for m in manzanas)
        return min(q.distance(manzanas[j]) for j in vecinas)

    # **Se agrupa por la calle de OSM, no por el texto.** El BCI escribe la misma vía de
    # varias formas —"FILINTO MULLER" y "AVENIDA FILINTO MULLER"— y el tope de dos rótulos
    # por calle daba cuatro. Cuando las dos grafías apoyan sobre el mismo eje son la misma
    # calle; sin eje que lo confirme se agrupa por el nombre literal, que es lo prudente:
    # dos calles homónimas de verdad no se pueden distinguir por el nombre solo
    # (ver [[vg-calles-homonimas-cep]]).
    def _grupo(p_log) -> str:
        return claves_osm.get(p_log[0]) or p_log[0]

    por_nombre: dict[str, list] = {}
    for p_log in pendientes_log:
        por_nombre.setdefault(_grupo(p_log), []).append(p_log)
    # El nombre que se dibuja es el más completo de los que se fusionaron.
    rotulo_del_grupo = {g: max({c[0] for c in cands}, key=len)
                        for g, cands in por_nombre.items()}
    for nombre, cands in list(por_nombre.items()):
        cerca = [c for c in cands if _lejania(c) <= _LEJOS_DE_MANZANA]
        por_nombre[nombre] = cerca or [min(cands, key=_lejania)]
    descartados = len(pendientes_log) - sum(len(v) for v in por_nombre.values())
    if descartados:
        logger.info(f"Rótulos de calle descartados por caer lejos de toda manzana: "
                    f"{descartados}")

    elegidos = []
    for nombre, cands in por_nombre.items():
        if len(cands) <= _ROTULOS_POR_CALLE:
            elegidos += cands
            continue
        # El par más separado: se ordena a lo largo de la propia calle y se toman las
        # puntas. Es más barato y más estable que comparar todos contra todos.
        vx, vy = cands[0][3], cands[0][4]
        orden = sorted(cands, key=lambda c: c[1] * vx + c[2] * vy)
        elegidos += [orden[0], orden[-1]]

    def _angulo(dx: float, dy: float) -> float:
        """Ángulo del texto, normalizado a [-90, 90) para que no salga cabeza abajo."""
        a = math.degrees(math.atan2(dy, dx))
        if a > 90:
            a -= 180
        elif a <= -90:
            a += 180
        return a

    puestos: dict[str, list] = {}
    n_sobre_manzana = 0
    n_cruzados = 0
    for nombre, px, py, vx, vy, mx, my, cep_calle, eje in elegidos:
        grupo = claves_osm.get(nombre) or nombre
        nombre = rotulo_del_grupo.get(grupo, nombre)
        if any(math.dist((px, py), q) < _SEPARACION_ROTULOS
               for q in puestos.get(grupo, ())):
            continue
        puestos.setdefault(grupo, []).append((px, py))
        largo_log = _largo_texto([nombre], _H_ROTULO_LOG)

        # **Se busca lugar caminando la calle entera, no ±40 m.** El nombre se desliza a lo
        # largo del eje —nunca de costado, que lo sacaría de la calzada— y en cada parada se
        # mide cuánto del texto queda sobre una manzana. Antes se probaban nueve posiciones
        # fijas y, si ninguna servía, se dibujaba igual en la primera: así quedaban ocho
        # rótulos encima de los lotes, dos de ellos enteros.
        origen = Point(px, py)
        candidatos = []
        if eje:
            for tramo in eje:
                d0 = tramo.project(origen)
                d = 0.0
                while d <= tramo.length:
                    for dd in ({d0} if d == 0.0 else {d0 + d, d0 - d}):
                        if not (0.0 <= dd <= tramo.length):
                            continue
                        q = tramo.interpolate(dd)
                        lejos = origen.distance(q)
                        if lejos > _BUSQUEDA_ROTULO:
                            continue
                        a = tramo.interpolate(max(0.0, dd - _TANGENTE_EJE))
                        b = tramo.interpolate(min(tramo.length, dd + _TANGENTE_EJE))
                        ang_d = _angulo(b.x - a.x, b.y - a.y)
                        rr_d = math.radians(ang_d)
                        candidatos.append((lejos, ang_d,
                                           (q.x - math.cos(rr_d) * largo_log / 2,
                                            q.y - math.sin(rr_d) * largo_log / 2)))
                    d += _PASO_BUSQUEDA_ROTULO
            candidatos.sort(key=lambda c: c[0])
        if not candidatos:
            ang_d = _angulo(vx, vy)
            rr_d = math.radians(ang_d)
            ex, ey = math.cos(rr_d), math.sin(rr_d)
            delta = 0.0
            while delta <= _BUSQUEDA_ROTULO:
                for signo in ((1,) if delta == 0.0 else (1, -1)):
                    d = delta * signo
                    candidatos.append((delta, ang_d,
                                       (px + ex * d - ex * largo_log / 2,
                                        py + ey * d - ey * largo_log / 2)))
                delta += _PASO_BUSQUEDA_ROTULO

        ang, (cx_l, cy_l) = candidatos[0][1], candidatos[0][2]
        mejor_pena = None
        mejor_par = None
        for _orden, ang_d, cand in candidatos:
            caja_log = _caja_texto(cand, ang_d, largo_log, _H_ROTULO_LOG)
            sobre = _pisa_manzana(caja_log, manzanas, arbol_manzanas)
            choca = cajas_todas.choca(caja_log)
            # **Cruzado no sirve aunque la calzada esté limpia.** En la esquina el eje de la
            # transversal pasa por una calle despejada, así que sin esto la posición cruzada
            # puntuaba igual que la buena y a veces ganaba por estar más cerca del origen:
            # Aracy de Almeida salía con sus DOS rótulos atravesados sobre Santa Laura.
            rr_c = math.radians(ang_d)
            centro_c = Point(cand[0] + math.cos(rr_c) * largo_log / 2,
                             cand[1] + math.sin(rr_c) * largo_log / 2)
            cruce = _desvio_de_la_manzana(centro_c, ang_d, manzanas, arbol_manzanas)
            if sobre <= _ROCE_MANZANA_OK and not choca and cruce <= _DESVIO_MAX_MANZANA:
                ang, (cx_l, cy_l) = ang_d, cand
                mejor_pena = 0.0
                break
            # Si no hay lugar limpio, se guarda el menos malo en vez de dibujar en el
            # primero: penaliza mucho pisar la manzana y poco rozar otro texto.
            pena = sobre + (0.15 if choca else 0.0) + (cruce / 90.0) * _PESO_CRUCE
            # **Paralelo le gana a despejado.** Se lleva aparte el mejor de los candidatos
            # que SÍ están alineados con la manzana: si existe alguno, se usa ése aunque
            # roce un texto o entre un poco en el lote. Un nombre torcido se lee como si
            # nombrara a otra calle; un roce se lee igual. Sin esto, Elvira Monteiro y Jacob
            # do Bandolin —que tienen un solo rótulo cada una— se quedaban a 20,6° y 24,9°
            # porque la posición torcida puntuaba mejor por estar limpia.
            if cruce <= _DESVIO_MAX_MANZANA and (mejor_par is None or pena < mejor_par[0]):
                mejor_par = (pena, ang_d, cand)
            if mejor_pena is None or pena < mejor_pena:
                mejor_pena, ang, (cx_l, cy_l) = pena, ang_d, cand
        if mejor_par is not None and mejor_pena:
            mejor_pena, ang, (cx_l, cy_l) = mejor_par
        if mejor_pena and mejor_pena > _ROCE_MANZANA_OK:
            n_sobre_manzana += 1
        rr_f = math.radians(ang)
        if _desvio_de_la_manzana(
                Point(cx_l + math.cos(rr_f) * largo_log / 2,
                      cy_l + math.sin(rr_f) * largo_log / 2),
                ang, manzanas, arbol_manzanas) > _DESVIO_MAX_MANZANA:
            n_cruzados += 1
        ref = msp.add_blockref("LOGRADOURO", (cx_l, cy_l),
                               dxfattribs={"layer": "LOGRADOURO", "rotation": ang})
        ref.add_auto_attribs({
            "LOGRADOURO": nombre,
            "CODLOG": codlog_map.get(_norm(nombre), ""),
            "CEP": cep_calle,
        })
        cajas_todas.add(_caja_texto((cx_l, cy_l), ang, largo_log, _H_ROTULO_LOG))
    if n_cruzados:
        logger.info(f"Rótulos de calle que quedaron cruzados respecto de la manzana: "
                    f"{n_cruzados} (no había posición paralela libre en toda la cuadra).")
    if n_sobre_manzana:
        logger.info(f"Rótulos de calle sin lugar limpio en toda la cuadra: "
                    f"{n_sobre_manzana} (se dibujan en la posición menos mala).")

    if anclados:
        logger.info(f"Fichas ancladas al punto que les dio el cliente: {anclados}")
    if dist_a_parcela:
        dd = sorted(d for d, _c, _n in dist_a_parcela)
        logger.info(f"Ficha a su propia parcela: mediana {dd[len(dd) // 2]:.1f} m · "
                    f"p90 {dd[int(0.9 * len(dd))]:.1f} m · "
                    f"a más de 60 m: {sum(1 for v in dd if v > 60)}")
        # Por calle: una avenida entera corrida se ve acá y no en la mediana global.
        por_calle: dict[str, list] = {}
        for d, c, _n in dist_a_parcela:
            por_calle.setdefault(c, []).append(d)
        peores = sorted(((sorted(v)[len(v) // 2], max(v), len(v), c)
                         for c, v in por_calle.items() if len(v) >= 3), reverse=True)[:6]
        for med, mx, n, c in peores:
            if med > 1.0:
                logger.info(f"  ⚠ {c}: {n} fichas, mediana {med:.1f} m de su parcela "
                            f"(máx {mx:.0f} m)")

    # `add_auto_attribs` copia el ATTDEF pero no siempre arrastra el bit de invisibilidad;
    # sin esta pasada los atributos de dato se dibujarían encima del plano.
    #
    # ⚠ Acá vivía un bug: `QTD_ANDARES` se agregaba al bloque SDU pero no figuraba en esta
    # lista, así que la pasada lo volvía invisible y los pisos no se veían en ninguna de
    # las 612 fichas. Todo atributo que tenga que verse va SÍ o SÍ en este diccionario.
    # `QTD_ANDARES` del SDU queda de dato y no se dibuja: los pisos ya los dice `RESUMO`
    # con letra ("1P", "PB"), y un número suelto al lado repetía la misma cifra.
    _VIS = {"SDU": {"N_C1_3_TP_QT", "PISOS"},
            # El MDU se deja con los cuatro campos que dibuja el cliente: `HP_TOTAL` ya
            # da el total de unidades y `QTD_ANDARES` los pisos, así que `RESUMO` y
            # `PISOS` sólo repetirían.
            "MDU": {"NUMERO", "HP_TOTAL", "QTD_ANDARES", "BLOCO"},
            "LOGRADOURO": {"LOGRADOURO"},
            # El contador queda de DATO, sin dibujar: su número repite lo que ya dice el
            # renglón de unidades, y como cuarto renglón hacía que la ficha no entrara en
            # 20 lotes más (530 contra 550 de 566). El bloque y el atributo siguen ahí
            # para el sistema del cliente; sólo no se rotula.
            "HCSDU": set(),
            "HCMDU": set()}
    for ins in msp.query("INSERT"):
        visibles = _VIS.get(ins.dxf.name)
        if visibles is None:
            continue
        for a in ins.attribs:
            oculto = a.dxf.tag not in visibles
            a.dxf.invisible = 1 if oculto else 0
            a.dxf.flags = (a.dxf.flags & ~1) | (1 if oculto else 0)

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

    ext = ezbbox.extents(msp, fast=True)

    # El dibujo vive en UTM, a ~8.270.000 m del origen, y la plantilla trae la vista
    # guardada en (0,0) con 1.000 m de alto: sin esto el archivo abre bien pero en una
    # pantalla vacía, y hay que hacer ZOOM EXTENTS a mano para ver algo.
    # Los extents van en el LAYOUT, no en la cabecera: ezdxf sobrescribe `$EXTMIN`/
    # `$EXTMAX` al guardar con lo que diga `msp.dxf` (`Drawing.update_extents`).
    msp.dxf.extmin = ext.extmin
    msp.dxf.extmax = ext.extmax
    doc.set_modelspace_vport(height=max(ext.size.y, ext.size.x) * 1.05,
                             center=(ext.center.x, ext.center.y))

    out = Path(input.output_path) if input.output_path else Path(
        f"/tmp/entrega_{input.survey_id[:8]}.dxf")
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(out)

    # GUARDA DE COORDENADAS, sobre el archivo ya escrito. Ni el auditor de ezdxf ni
    # `ezdxf.bbox` sirven acá: el auditor valida estructura (handles, capas, referencias)
    # y `bbox` **descarta en silencio** los vértices no finitos, así que una polilínea
    # entera en `inf` da `has_data=False` y no aparece por ningún lado. Recién lo ve
    # AutoCAD, que descarta el dibujo completo con «Invalid or incomplete DXF input» sin
    # decir dónde. Por eso se relee el texto emitido, que es lo que el cliente va a abrir.
    malas = _coordenadas_no_finitas(out)
    if malas:
        out.unlink(missing_ok=True)   # más vale sin entregable que con uno que no abre
        return EntregaOutput(ok=False, error=(
            f"{len(malas)} coordenada(s) no finita(s) en el plano; AutoCAD lo rechazaría "
            f"entero. Primeras: " + "; ".join(malas[:5])))

    if n_apretados:
        logger.info(f"{n_apretados} ficha(s) en lotes donde el texto no entra a tamaño "
                    f"completo: quedan centradas y pueden rozar al vecino.")
    logger.info(f"DXFEntrega {input.survey_id}: {n_sdu} SDU + {n_mdu} MDU, {hp_total} HPs, "
                f"{n_log} logradouros, base {len(quadras)}q/{len(meiofio)}m, "
                f"EPSG:{epsg} -> {out}")

    return EntregaOutput(
        ok=True, dxf_path=str(out), sdu=n_sdu, mdu=n_mdu, hp_total=hp_total,
        logradouros=n_log, quadras=len(quadras), meiofio=len(meiofio),
        epsg=epsg, sin_codlog=sin_codlog, geometrias_rotas=len(rotas), divisiones=n_div,
        contadores=n_cont)
