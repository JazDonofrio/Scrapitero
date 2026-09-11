"""BCIParser — extrae datos estructurados de los PDFs del BCI de Várzea Grande.

Fuente: pdf_downloads/reporte_{cca_code}.pdf
Formato: Boletim de Cadastramento Imobiliário — Prefeitura de Várzea Grande (GeneXus)

Extrae sin LLM usando regex sobre el texto del PDF:
  - Tipo do imóvel (Predial / Territorial)
  - Unidades funcionales: cantidad, uso (RESIDENCIAL / COMERCIAL), tipología
  - Área construída total y área do terreno (de fato)
  - Dirección (logradouro, número, complemento, CEP, bairro)
  - Código municipal del logradouro → codigo_logradouro (migración 016, CSV operadora)
  - Nomenclatura catastral completa (setor/quadra/lote/unidade)
  - Número de matrícula del registro de imóveis
  - Valor venal (terreno / construção / imóvel) y alíquota (IPTU) — migración 012
  - Año de construcción (el más antiguo entre las unidades) — migración 012
  - Proprietário: nombre + CPF/CNPJ y contribuyente secundario — migración 012 (PII)
"""

from __future__ import annotations

import os
import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Optional

import pdfplumber
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.db.engine import get_engine
from scrapitero.agents._run import agent_run

# Directorio de PDFs BCI — fuente única compartida con VGBCIFetcher. Debe ser ABSOLUTO
# y el MISMO para ambos agentes (el fetcher escribe, el parser lee). Configurable con
# SCRAPITERO_PDF_DIR para que un solo setting (p.ej. en el container Hermes) los alinee.
DEFAULT_PDF_DIR = os.environ.get("SCRAPITERO_PDF_DIR", "/opt/scrapitero/pdf_downloads")


class BCIParserInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None
    pdf_dir: str = DEFAULT_PDF_DIR
    batch_size: int = 0   # 0 = todas las parcelas con PDF disponible


class BCIParserOutput(BaseModel):
    ok: bool
    procesadas: int = 0
    actualizadas: int = 0
    sin_pdf: int = 0
    errores: int = 0
    error: Optional[str] = None


# ── Extracción de texto ────────────────────────────────────────────────────────

def _pdf_text(path: Path) -> str:
    pages = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                pages.append(t)
    return "\n".join(pages)


def _br_num(s: str) -> Optional[float]:
    """Número en formato brasileño → float: '43.857,04'→43857.04, '0,4000'→0.4."""
    try:
        return float(str(s).strip().replace(".", "").replace(",", "."))
    except (ValueError, AttributeError):
        return None


# ── Vocabulario de USO de la edificación (BCI de Várzea Grande) ─────────────────
# El "uso" de cada unidade aparece en el PDF como "... USO <VALOR>" (en la línea de
# acabamento). El BCI usa más valores que los 4 clásicos; los enumeramos explícitamente
# para NO confundirlos con otros campos que también dicen "USO" (USO INTERNO / USO
# TERRENO / USO PARTICULAR / USO EDIFICAÇÃO, que son headers de otras secciones).
# Mapeo a la categoría de UF del relevamiento. Decisión del cliente: todo lo construido
# que no es vivienda ni industria cuenta como COMÉRCIO EM GERAL (1 unidad de comercio):
# servicios, enseñanza, religioso y público (municipal/estadual/federal).
_BCI_USO_CAT = {
    "RESIDENCIAL": "vivienda",
    "COMERCIAL":   "comercio",
    "SERVICOS":    "comercio",
    "SERVIÇOS":    "comercio",
    "ENSINO":      "comercio",
    "RELIGIOSO":   "comercio",
    "MUNICIPAL":   "comercio",
    "ESTADUAL":    "comercio",
    "FEDERAL":     "comercio",
    "INDUSTRIAL":  "industrial",
    "MISTO":       "comercio",   # a nivel unidade, la porción no residencial → comercio
}
# Regex que captura sólo los valores conocidos (los más largos primero para no cortar).
_BCI_USO_RE = r'\bUSO\s+(' + "|".join(
    sorted(_BCI_USO_CAT, key=len, reverse=True)) + r')\b'
# Etiqueta canónica de uso por unidad (lo que se guarda en parcela_unidades.uso y usa el
# CSV expandido). Servicios/religioso/ensino/público quedan como 'comercial'.
_CAT_USO_LABEL = {"vivienda": "residencial", "comercio": "comercial",
                  "industrial": "industrial"}


# ── Parser de campos ───────────────────────────────────────────────────────────

# Proporción bajo la cual una unidad del BCI se considera **anexo** (cochera, lavadero,
# edícula) y no vivienda, medida contra la unidad más grande del lote. Desde que la
# `TIPOLOGIA` y la `DISPOSICAO` deciden primero, este corte sólo reparte **dentro de una
# vila** (≥ `_VILA_MIN_UNIDADES` habitables): distingue la kitnet del lavadero de la vila.
_ANEXO_PCT = 0.30
# ...salvo que haya MUCHAS unidades chicas: eso ya no son anexos, es un edificio de kitnets.
# Da Liberdade 109 tiene una casa de 109 m² y 24 unidades de 18-23 m² que SÍ son viviendas.
_ANEXO_MIN_CHICAS = 3
# Lo que el catastro DICE que es cada construcción, en `TIPOLOGIA`. Nadie vive en una
# piscina: estas nunca son vivienda, midan lo que midan. Sale de la ficha, no de un
# supuesto nuestro, y por eso manda sobre cualquier umbral de área — que era el modo de
# falla anterior: sólo se descartaban por CHICAS, así que la piscina de 58 m² de
# `DA LIBERDADE 350` contaba como una casa. En VAZ049 hay 162 construcciones que el BCI
# marca `USO RESIDENCIAL` y rotula piscina, telheiro (cobertizo) o garagem.
# `SALA` y `APARTAMENTO` NO están acá a propósito: una sala con uso residencial es rara
# pero habitable, y descartarla perdería UF reales sin evidencia que lo respalde.
# ⚠ `PARQUE INDUSTRIAL` va con su nombre COMPLETO. Escrito `PARQUE` parecía decir «plaza» y
# en el catastro de VG no existe tal tipología: lo único que empieza así es el galpón
# industrial, que es lo que se quiere descartar. Una tipología nueva se agrega tal como la
# escribe el municipio, no por su primera palabra.
_TIPOLOGIA_NO_HABITABLE = {"PISCINA", "GARAGEM", "TELHEIRO", "GALPAO", "PARQUE INDUSTRIAL"}
# Lo mismo para el COMERCIO, y la lista NO es la misma: nadie atiende un negocio en una pileta
# ni en una cochera, pero **un galpón sí puede ser el comercio** —un depósito, una
# distribuidora, el taller— y no un agregado de otro local (criterio del operador, 9-sep-2026,
# a partir de `COUTO MAGALHAES 290`: galpón + cobertizo + oficina son UN taller). Por eso
# `GALPAO` y `PARQUE INDUSTRIAL` quedan afuera de esta lista y adentro de la de vivienda.
# Medido en VG: 259 unidades comerciales son de este tipo (237 TELHEIRO, 12 PISCINA,
# 8 GARAGEM), y en 8 parcelas de la región vigente son el ÚNICO «comercio» que declaran —
# `CLÓVIS HUGNEY 63` figuraba **mixta por tener pileta**.
_TIPOLOGIA_NO_LOCAL = {"PISCINA", "TELHEIRO", "GARAGEM"}
# A partir de cuántas construcciones habitables el lote es una VILA (kitnets en fila) y no
# una casa con su ampliación del fondo. Ver `_viviendas_reales`. **Es el número frágil de
# los tres**: entre 2 (Vuolo 151, São Bento 95: casa + ampliación) y 10 (Filinto Müller:
# vila) no hay ningún caso verificado en la calle. En la célula VAZ049 hay 103 fichas con
# exactamente 2 construcciones habitables, así que es ahí donde más pesa: si aparece un
# caso nuevo, mirar primero esas.
_VILA_MIN_UNIDADES = 3


def _es_seccion_muerta(blk: str) -> bool:
    """La sección describe algo sin techo Y sin aberturas: no aloja a nadie.

    Es el caso de GENEROSO TAVARES 174, que no declara ninguna `UNIDADE` y sin embargo traía
    dos `USO RESIDENCIAL`: el segundo es un `SITUACAO FUNDOS` con `COBERTURA SEM` y
    `ESQUADRIAS SEM`. `SEM` es un valor deliberado del catastro, no un campo vacío: el
    vocabulario de `ESQUADRIAS` es FERRO 61% · SEM 22% · VIDRO 6% · PVC 5% · ALUMINIO 4% ·
    MADEIRA 2%, y cuando de verdad no lo anotan el campo directamente falta (0,4%).
    """
    cob = re.search(r'COBERTURA\s+(\S+)', blk, re.I)
    esq = re.search(r'ESQUADRIAS\s+(\S+)', blk, re.I)
    return bool(cob and esq and cob.group(1).upper() == "SEM" and esq.group(1).upper() == "SEM")


def _viviendas_reales(unidades: list, unit_iter: list, text: str) -> int:
    """Cuántas de las construcciones residenciales del BCI son de verdad viviendas.

    Dos caminos, según lo que declare el PDF (ver el comentario largo en `_parse_bci`):

    * **Con `UNIDADE` enumeradas** — se lee lo que el catastro DECLARA de cada una antes de
      medirla: primero se descartan las `TIPOLOGIA` que no alojan a nadie
      (`_TIPOLOGIA_NO_HABITABLE`), y sobre las que quedan decide la cantidad: con
      `_VILA_MIN_UNIDADES` o más es una vila de kitnets y manda la proporción `_ANEXO_PCT`;
      con una o dos es una casa y su ampliación del fondo, donde la proporción NO sirve y
      manda `DISPOSICAO` (sólo la CONJUGADA es vivienda aparte).
    * **Sin `UNIDADE`** — se cuentan las secciones `USO` residenciales, salteando las que no
      tienen techo ni aberturas.

    Devuelve 0 cuando ninguna construcción residencial del lote es habitable — un lote cuyo
    "residencial" era sólo un cobertizo no tiene una vivienda que contar, y con construcción
    a la vista sale por `uf_sin_declarar` del panel de incidencias.
    """
    res = [u for u in unidades if (u.get("uso") or "residencial") == "residencial"]
    if unit_iter and res:
        # 1) Lo que el catastro dice que no es habitable, no lo es — sin mirar el área.
        habitables = []
        for u in res:
            if (u.get("tipologia") or "CASA") in _TIPOLOGIA_NO_HABITABLE:
                u["uso"] = "anexo"          # viaja a `parcela_unidades`; el CSV no la cuenta
            else:
                habitables.append(u)
        if not habitables:
            # Todo lo "residencial" del lote era cobertizo/piscina/garagem: no hay vivienda.
            # Devolver 0 y no 1 es deliberado — si además hay construcción, el lote sale por
            # `uf_sin_declarar` del panel de incidencias, que es donde un humano lo cierra.
            return 0
        areas = [u["area_m2"] for u in habitables if u.get("area_m2")]
        if not areas:
            return len(habitables)
        mayor = max(areas)

        # 2) Tres o más construcciones habitables: es una VILA de kitnets y manda la
        #    proporción, como venía. (Da Liberdade 109, Filinto Müller 326.)
        if len(habitables) >= _VILA_MIN_UNIDADES:
            chicas = [u for u in habitables
                      if u.get("area_m2") is not None and u["area_m2"] < mayor * _ANEXO_PCT]
            if len(chicas) < _ANEXO_MIN_CHICAS:
                for u in chicas:
                    u["uso"] = "anexo"
                return max(len(habitables) - len(chicas), 1)
            return len(habitables)

        # 3) Una o dos: es la casa y, a lo sumo, su ampliación del fondo. **La proporción
        #    no sirve acá** — SEN. VICENTE VUOLO 151 tiene atrás una CASA de 111 m² que es
        #    el 61% de la del frente y el operador verificó que es UNA sola vivienda
        #    (sep-2026); con el corte del 30% pasaba holgada. Lo que separa la ampliación
        #    de la vivienda independiente es `DISPOSICAO`: las verificadas como viviendas
        #    reales son CONJUGADA (una fila de unidades pegadas entre sí), y las
        #    ampliaciones son ISOLADA/GEMINADA al lado de la casa principal.
        #    Casos que sostienen la regla, los ocho verificados en la calle:
        #      SEN. VICENTE VUOLO 151  CASA 111 m² FUNDOS GEMINADA  → ampliación
        #      JOÃO LIBÂNIO 118 (634305) CASA 60 m² FUNDOS ISOLADA  → ampliación
        #      SÃO BENTO 95            CASA 46 m² FUNDOS GEMINADA   → ampliación
        #      SÃO BENTO 86            CASA 23 m² FUNDOS ISOLADA    → ampliación
        #      DA LIBERDADE 109 / FILINTO MÜLLER 326: CONJUGADA     → viviendas (24 y 10)
        resto = sorted(habitables, key=lambda u: u.get("area_m2") or 0, reverse=True)[1:]
        vivi = 1
        for u in resto:
            if (u.get("disposicion") or "") == "CONJUGADA":
                vivi += 1
            else:
                u["uso"] = "anexo"
        return vivi

    vivos = 0
    for m in re.finditer(_BCI_USO_RE, text, re.I):
        if _BCI_USO_CAT.get(m.group(1).upper()) != "vivienda":
            continue
        if not _es_seccion_muerta(text[max(0, m.start() - 300):m.end() + 220]):
            vivos += 1
    return vivos


def _comercios_reales(unidades: list) -> int:
    """Cuántas de las construcciones comerciales del BCI son de verdad un local aparte.

    Mismo criterio que `_viviendas_reales`, que es el punto: el catastro enumera
    CONSTRUCCIONES y un negocio ocupa varias. Dos pasos:

    * **Tipología** — lo que el catastro dice que no es un local no lo es, mida lo que mida
      (`_TIPOLOGIA_NO_LOCAL`). Ya estaba.
    * **Proporción** — la construcción chica al lado de la grande es el depósito, la
      oficina o el techo del mismo negocio, no otro comercio. Mismo corte que vivienda
      (`_ANEXO_PCT`), medido contra la mayor del lote.

    El segundo paso es lo que faltaba, y es el criterio que el operador ya venía aplicando
    a mano: `COUTO MAGALHAES 290` —galpón de 319 m² + oficina de 31— lo cerró como UN taller
    (9-sep-2026), y `PONCE DE ARRUDA 1096` —galpón de 740 m² con una sala de 130, otra de 68,
    otra de 14 y un galpón de 57— como UN comercio (10-sep-2026). Las dos son el mismo caso.

    Verificado contra las 279 direcciones que cruzan con la revisión cuadra por cuadra del
    cliente: con el corte al 30 % arregla 8 direcciones y no rompe ninguna. Probado también
    a 15/20/25/40/50 %; el 30 % es el óptimo y es el número que ya usa vivienda.

    **No** colapsa la fila de locales iguales: cuatro `SALA` de ~80 m² —`COUTO MAGALHAES
    300`— son cuatro construcciones del mismo porte y quedan cuatro. Ahí el catastro no
    alcanza y hace falta el ojo; ver el grupo «predio con varias construcciones del mismo
    uso» del cotejo.

    Devuelve como mínimo 1 cuando el lote declara algún comercio: si todo lo comercial era
    cobertizo y pileta, algo hay, pero es uno solo.
    """
    com = [u for u in unidades if u.get("uso") == "comercial"]
    if not com:
        return 0
    # 1) Lo que el catastro dice que no es un local. Queda marcado `anexo` —igual que en
    #    vivienda— así el CSV por unidad tampoco lo emite como comercio.
    locales = []
    for u in com:
        if (u.get("tipologia") or "SALA") in _TIPOLOGIA_NO_LOCAL:
            u["uso"] = "anexo"
        else:
            locales.append(u)
    if not locales:
        return 1
    areas = [u["area_m2"] for u in locales if u.get("area_m2")]
    if not areas:
        return len(locales)
    mayor = max(areas)

    # 2) Lo chico al lado de lo grande es parte del mismo negocio.
    chicas = [u for u in locales
              if u.get("area_m2") is not None and u["area_m2"] < mayor * _ANEXO_PCT]
    for u in chicas:
        u["uso"] = "anexo"
    return max(len(locales) - len(chicas), 1)


def _parse_bci(text: str) -> dict:
    """Extrae campos estructurados del texto del BCI."""
    r: dict = {
        "tipo_imovel": None,
        "uso_principal": None,
        "uf_vivienda": 0,
        "uf_comercio": 0,
        "uf_total": 0,
        "area_m2_construida": None,
        "area_m2_terreno": None,
        "calle": None,
        "numero": None,
        "complemento": None,
        "barrio": None,
        "codigo_postal": None,
        "codigo_logradouro": None,   # código municipal del logradouro (migración 016)
        "nomenclatura_catastral": None,
        "partida_inmobiliaria": None,
        "tipologia": None,
        # migración 012
        "valor_venal_terreno": None,
        "valor_venal_construccion": None,
        "valor_venal_total": None,
        "aliquota": None,
        "anio_construccion": None,
        "propietario_nombre": None,
        "propietario_documento": None,
        "contribuyente_secundario": None,
        # Lista de unidades del imóvel (UNIDADE 1..N del BCI) — para expandir el CSV
        # una fila por unidad. Sólo tiene sentido cuando hay más de una.
        "unidades": [],
    }

    # ── Tipo do imóvel ─────────────────────────────────────────────────────────
    m = re.search(r'TIPO\s+IM[OÓ]VEL\s+CATEGORIA\s+TIPO\s+PROPRIEDADE\s*\n(\S+)', text, re.I)
    if m:
        r["tipo_imovel"] = m.group(1).strip().lower()

    # ── Unidades funcionales (sólo para Predial) ──────────────────────────────
    if r["tipo_imovel"] == "territorial":
        r["uso_principal"] = "vacante"
    else:
        # Cada unidade del imóvel arranca con el header "UNIDADE CÓDIGO DA UNIDADE …
        # TOTAL" seguido de su línea de datos (n, código, área_unidade, área_total).
        # Iteramos por bloque para sacar también uso y año de cada una.
        # ⚠ El número de unidad se escribe de DOS formas: `2` a secas o `2/3` («la 2 de 3»).
        # Con `(\d+)` sola la segunda no matcheaba y la ficha entera se caía al conteo por
        # texto, que no sabe de tipología ni de área — o sea, sin ninguno de los filtros.
        # Son 19 de las 511 fichas de VAZ049, y `PONCE DE ARRUDA 802` es una: declara
        # SALA + SALA + TELHEIRO y salía con 3 comercios en vez de 2.
        unit_iter = list(re.finditer(
            r'UNIDADE\s+C[OÓ]DIGO\s+DA\s+UNIDADE.*?TOTAL\s*\n\s*'
            r'(\d+(?:/\d+)?)\s+(\d+)\s+([\d,]+)\s+([\d,]+)',
            text, re.I | re.DOTALL,
        ))
        unidades = []
        for i, mu in enumerate(unit_iter):
            n_u, cod_u, area_u, _area_tot = mu.groups()
            n_u = n_u.split("/")[0]
            fin = unit_iter[i + 1].start() if i + 1 < len(unit_iter) else len(text)
            blk = text[mu.end():fin]
            uso_m = re.search(_BCI_USO_RE, blk, re.I)
            uso_cat = _BCI_USO_CAT.get(uso_m.group(1).upper()) if uso_m else None
            ano_m = re.search(r'ANO\s+CONSTRU[CÇ][AÃ]O:?\s*(\d{4})', blk, re.I)
            # `TIPOLOGIA` y `DISPOSICAO` son lo que el catastro DICE que es cada
            # construcción; con el área sólo se puede adivinar. Ver `_viviendas_reales`.
            # ⚠ La tipología puede ser de MÁS DE UNA PALABRA y hay que leerla entera.
            # Con `([A-ZÀ-Ú]+)` se leía sólo la primera, y `PARQUE INDUSTRIAL` —un galpón
            # industrial— entraba como `PARQUE`, que en `_TIPOLOGIA_NO_HABITABLE` significaba
            # una plaza: dos cosas opuestas bajo la misma palabra (caso JOÃO LIBÂNIO 220,
            # 9-sep-2026). Medido sobre las dos regiones de VG, el vocabulario completo es
            # CASA 702 · SALA 250 · TELHEIRO 212 · PISCINA 73 · GALPAO 57 · GARAGEM 22 ·
            # APARTAMENTO 18 · CONJUNTO 12 · PARQUE INDUSTRIAL 2, y esa última es la única
            # compuesta — pero la lista es del municipio y puede crecer, así que se lee entera.
            tip_m = re.search(
                r'TIPOLOGIA\s+([A-ZÀ-Ú][A-ZÀ-Ú /]*?)\s*(?:\n|RESPONABILIDADE|REGIME|\d\d\s)',
                blk, re.I)
            disp_m = re.search(r'DISPOSICAO\s+([A-ZÀ-Ú]+)', blk, re.I)
            try:
                area_v = float(area_u.replace(",", "."))
            except ValueError:
                area_v = None
            unidades.append({
                "n": int(n_u),
                "codigo": cod_u,
                "area_m2": area_v,
                "anio": int(ano_m.group(1)) if (ano_m and ano_m.group(1) != "0") else None,
                "tipologia": " ".join(tip_m.group(1).split()).upper() if tip_m else None,
                "disposicion": disp_m.group(1).upper() if disp_m else None,
                # Etiqueta canónica (servicios/religioso/etc. → comercial) para que la
                # expansión del CSV cuente la unidad como comercio, no como vivienda.
                "uso": _CAT_USO_LABEL.get(uso_cat) if uso_cat else None,
            })
        r["unidades"] = unidades

        # Servicios/religioso/ensino/público cuentan como comercio (COMÉRCIO EM GERAL) por
        # decisión del cliente; antes caían fuera del whitelist → 0 UF + uso 'residencial'.
        usos = re.findall(_BCI_USO_RE, text, re.I)
        cats = [_BCI_USO_CAT[u.upper()] for u in usos]

        # ⚠ **Una UF por CONSTRUCCIÓN declarada, no por aparición de la palabra en el PDF.**
        # Se contaba `USO` sobre el texto entero, y el formulario del BCI **repite la sección
        # de características**: cada repetición sumaba un comercio que no existe. Medido en la
        # región vigente el 9-sep-2026: 404 UF en la base contra 393 construcciones reales,
        # con la escuela `EMEB MARILCE` declarando **11 y teniendo 7**, la `SECRETARIA DE
        # ESPORTES` **10 y teniendo 7**, y `PONCE DE ARRUDA 1016` **3 con una sola
        # construcción** (su ficha dice `USO COMERCIAL` tres veces, todas de la misma unidad).
        # Cuando el PDF **no** enumera `UNIDADE` no hay construcciones que contar y se cae al
        # conteo por secciones, que es el mismo camino que usa `_viviendas_reales`.
        if unit_iter:
            r["uf_comercio"] = _comercios_reales(unidades)
        else:
            r["uf_comercio"] = sum(1 for c in cats if c == "comercio")

        # ⚠ **La vivienda NO se cuenta así.** El BCI enumera CONSTRUCCIONES, no hogares: la
        # cochera, el lavadero y el galpón del fondo son "unidades" con su código, su área y
        # su año, y contarlas una por una infla el relevamiento. Verificado en la calle por
        # el operador (sep-2026), seis casos, seis errores:
        #
        #   GENEROSO TAVARES 174   decía 2 · es 1   (sección de fondo sin techo ni aberturas)
        #   GENEROSO TAVARES 134   decía 3 · es 1   (anexos de 10 y 47 m²)
        #   SÃO BENTO 86           decía 3 · es 1   (anexos de 23 y 14 m²)
        #   SÃO BENTO 95           decía 2 · es 1   (anexo de 46 m², mismo año que la casa)
        #   JOÃO LIBÂNIO 118       decía 3 · es 1   (anexos de 33 y 36 m²)
        #   PEDRO PEDROSSIAN 234   decía 3 · es 1   (anexos de 5 y 22 m²)
        #
        # Lo que separa el anexo de la casa **no** es el área absoluta —São Bento 95 tiene un
        # anexo de 46 m² y Filinto Müller 326 tiene kitnets de 18 m² que SÍ son viviendas—
        # sino la **proporción** contra la mayor del lote, más una excepción para los
        # edificios de kitnets: una o dos unidades chicas al lado de una grande son anexos;
        # veinte son un negocio de alquiler.
        #
        # Descartados por medición, no por opinión: `TIPO DE OCUPACAO` dice `UNICA` en 510 de
        # 511 parcelas (constante del formulario, no informa); y el conteo de huellas
        # satelitales acuerda sólo el 47% del tiempo porque Open Buildings **fusiona**
        # construcciones pegadas — justo el caso a separar (huellas únicas de 457 y 559 m²).
        vivi = _viviendas_reales(unidades, unit_iter, text)
        r["uf_vivienda"] = vivi
        r["uf_total"] = len(unit_iter) or len(usos)

        if r["uf_comercio"] > 0 and r["uf_vivienda"] > 0:
            r["uso_principal"] = "mixto"
        elif r["uf_comercio"] > 0:
            r["uso_principal"] = "comercial"
        elif "industrial" in cats:
            r["uso_principal"] = "industrial"
        else:
            r["uso_principal"] = "residencial"

        if unit_iter:
            try:
                # group(4) = ÁREA CONSTRUÇÃO TOTAL de la primera unidade
                r["area_m2_construida"] = float(unit_iter[0].group(4).replace(",", "."))
            except ValueError:
                pass

        tipologias = re.findall(r'\bTIPOLOGIA\s+(\S+)', text, re.I)
        if tipologias:
            r["tipologia"] = Counter(t.upper() for t in tipologias).most_common(1)[0][0]

    # ── Dirección ──────────────────────────────────────────────────────────────
    # Línea: "CODE  LOGRADOURO  [NUMBER]  CEP"
    m = re.search(r'C[OÓ]DIGO\s+LOGRADOURO\s+N[UÚ]MERO\s+CEP\s*\n(.+)', text, re.I)
    if m:
        line = m.group(1).strip()
        # Buscar CEP al final. El CEP brasileño son 8 dígitos (NNNNN-NNN) pero el BCI
        # lo imprime con separadores inconsistentes: "78115-060", "78.115-060",
        # "78.115.660" (puntos) o "78110841" (sin separador). Toleramos cualquier
        # combinación de '.'/'-' entre los grupos; si exigimos solo "NN.NNN-NNN" se
        # descarta toda la línea (y se pierde el número de puerta que sí está presente).
        cep_m = re.search(r'(\d{2}[.\-]?\d{3}[.\-]?\d{3})\s*$', line)
        if cep_m:
            r["codigo_postal"] = re.sub(r'[.\-]', '', cep_m.group(1))
            before = line[:cep_m.start()].strip()
            # Número de puerta = último token numérico antes del CEP
            num_m = re.search(r'\s(\d{1,5})\s*$', before)
            if num_m:
                r["numero"] = num_m.group(1)
                logradouro_raw = before[:num_m.start()].strip()
            else:
                logradouro_raw = before
            # Separar el código de logradouro al inicio (identificador municipal,
            # se exporta en el CSV de operadora) del nombre de la calle
            cod_m = re.match(r'^(\d+)\s+(.*)$', logradouro_raw)
            if cod_m:
                r["codigo_logradouro"] = cod_m.group(1)
                r["calle"] = cod_m.group(2).strip() or None
            else:
                r["calle"] = logradouro_raw.strip() or None

    # ── Bairro ─────────────────────────────────────────────────────────────────
    m = re.search(r'\n\s*\d+\s*-\s*(?:BAIRRO\s+)?(.+?)\s*\nQUADRA', text, re.I)
    if m:
        r["barrio"] = re.sub(r'^BAIRRO\s+', '', m.group(1).strip(), flags=re.I)

    # ── Número de matrícula ────────────────────────────────────────────────────
    # Header: "N° MATRÍCULA FRENTE (M)"  →  data row: "0,00  1,00  119.441  0,00"
    m = re.search(r'N[°O]\s+MATR[IÍ]CULA\s+FRENTE\s*\(M\)\s*\n(.*)', text, re.I)
    if m:
        # Matrículas reales tienen formato "119.441" (dígitos con punto)
        mats = re.findall(r'\b(\d{1,6}\.\d{3})\b', m.group(1))
        if mats:
            r["partida_inmobiliaria"] = mats[0]

    # ── Nomenclatura catastral completa ─────────────────────────────────────────
    # "INSCRIÇÃO SETOR QUADRA LOTE UNIDADE ZONA FISCAL\n000000000030894 202 0145 0203 2 4"
    m = re.search(
        r'INSCRI[CÇ][AÃ]O\s+SETOR\s+QUADRA\s+LOTE\s+UNIDADE\s+ZONA\s+FISCAL\s*\n'
        r'\s*\d+\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)', text, re.I)
    if m:
        setor, quadra, lote, unidade, zona = m.groups()
        r["nomenclatura_catastral"] = f"{setor}-{quadra}-{lote}-{unidade} ZF{zona}"

    # ── Complemento (BLOCO / APTO) ──────────────────────────────────────────────
    m = re.search(r'COMPLEMENTO\s+BLOCO\s+APTO\s*\n(.+)', text, re.I)
    if m:
        comp = m.group(1).strip()
        # Si la parcela no tiene complemento, la línea siguiente es el header "BAIRRO"
        if comp and not re.match(r'^BAIRRO\b', comp, re.I):
            r["complemento"] = comp[:100]

    # ── Área do terreno (de fato) ───────────────────────────────────────────────
    # "MÉTRICA TESTADA (M) ÁREA DO TERRENO DE FATO ÁREA ... DIREITO\n675 12,50 284,62 0,00"
    m = re.search(
        r'M[ÉE]TRICA\s+TESTADA\s*\(M\)\s+[ÁA]REA\s+DO\s+TERRENO\s+DE\s+FATO.*?\n'
        r'\s*[\d.,]+\s+[\d.,]+\s+([\d.,]+)\s+[\d.,]+', text, re.I)
    if m:
        area_t = _br_num(m.group(1))
        if area_t and area_t > 0:
            r["area_m2_terreno"] = area_t

    # ── Valor venal + alíquota ──────────────────────────────────────────────────
    # "VALOR VENAL DO TERRENO ... ALÍQUOTA\n43.857,04 98.489,31 142.346,35 0,4000"
    m = re.search(
        r'VALOR\s+VENAL\s+DO\s+TERRENO.*?AL[IÍ]QUOTA\s*\n'
        r'\s*([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)', text, re.I)
    if m:
        r["valor_venal_terreno"] = _br_num(m.group(1))
        r["valor_venal_construccion"] = _br_num(m.group(2))
        r["valor_venal_total"] = _br_num(m.group(3))
        r["aliquota"] = _br_num(m.group(4))

    # ── Año de construcción (el más antiguo entre las unidades) ─────────────────
    anos = [int(a) for a in re.findall(r'ANO\s+CONSTRU[CÇ][AÃ]O:?\s*(\d{4})', text, re.I)
            if a != "0000" and int(a) > 1800]
    if anos:
        r["anio_construccion"] = min(anos)

    # ── Proprietário principal: nombre + CPF/CNPJ ───────────────────────────────
    # "CÓD. CONTRIBUINTE CPF / CNPJ CONTRIBUINTE PRINCIPAL / Proprietário\n
    #  9188910 111.309.971-20 TEREZINHA ALVES BARRETO DOS SANTOS"
    m = re.search(r'C[OÓ]D\.?\s+CONTRIBUINTE\s+CPF\s*/\s*CNPJ.*?Propriet[aá]rio\s*\n(.+)',
                  text, re.I)
    if m:
        line = m.group(1).strip()
        doc_m = re.search(r'(\d{3}\.\d{3}\.\d{3}-\d{2}|\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})', line)
        if doc_m:
            r["propietario_documento"] = doc_m.group(1)
            nombre = line[doc_m.end():].strip()
        else:
            # Sin documento: el nombre es lo que sigue al cód. contribuinte (1er token num.)
            nombre = re.sub(r'^\d+\s+', '', line).strip()
        r["propietario_nombre"] = (nombre[:200] or None)

    # ── Contribuyente secundario (nombre) ───────────────────────────────────────
    # "CONTRIBUINTE SECUNDÁRIO CPF/CNPJ TIPO CONTRIBUINTE\n
    #  EDENILCE FATIMA DA COSTA 48672912187 Co-Responsável"
    m = re.search(r'CONTRIBUINTE\s+SECUND[AÁ]RIO\s+CPF\s*/?\s*CNPJ\s+TIPO\s+CONTRIBUINTE\s*\n(.+)',
                  text, re.I)
    if m:
        line = m.group(1).strip()
        # Nombre = texto antes del documento (8+ dígitos, con o sin separadores)
        sec_m = re.match(r'(.+?)\s+\d[\d.\-/]{7,}\b', line)
        if sec_m and not re.match(r'^IDENTIFICA', line, re.I):
            r["contribuyente_secundario"] = sec_m.group(1).strip()[:200]

    return r


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _buscar_en_dirs_alternativos(codigos: list[int], pdf_dir: Path) -> dict[str, int]:
    """Busca los PDFs faltantes en ubicaciones alternativas conocidas para detectar
    un desajuste de `pdf_dir` entre VGBCIFetcher (que descarga) y BCIParser (que lee).

    Devuelve {directorio: cuántos de los faltantes están ahí}. Esto convierte el caso
    '10 PDFs faltantes' (silencioso) en un aviso accionable que nombra dónde SÍ están.
    """
    if not codigos:
        return {}
    candidatos = [
        Path.cwd() / "pdf_downloads",                          # default relativo del fetcher
        Path("/opt/scrapitero/pdf_downloads"),                 # default histórico del host
        Path("/docker/hermes-agent-wgnq/data/pdf_downloads"),  # data dir del container Hermes
    ]
    env = os.environ.get("SCRAPITERO_PDF_DIR")
    if env:
        candidatos.insert(0, Path(env))
    encontrados: dict[str, int] = {}
    vistos = {pdf_dir.resolve()}
    for d in candidatos:
        try:
            rd = d.resolve()
        except OSError:
            continue
        if rd in vistos:
            continue
        vistos.add(rd)
        n = sum(1 for c in codigos if (d / f"reporte_{c}.pdf").exists())
        if n:
            encontrados[str(d)] = n
    return encontrados


def _get_parcelas(region_id: str, batch_size: int) -> list[tuple[str, str]]:
    """Devuelve (parcela_id, cca_code) con PDF pendiente de parseo."""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT parcela_id::text, cca_code
            FROM parcelas
            WHERE region_id = :rid
              AND cca_code IS NOT NULL
              AND fuente_parcela IN ('smartgis_vg', 'catastro')
            ORDER BY cca_code
        """), {"rid": region_id}).fetchall()
    result = [(r[0], r[1]) for r in rows]
    if batch_size > 0:
        result = result[:batch_size]
    return result


def _update_parcela(parcela_id: str, d: dict) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        # BCIParser es la RED DE SEGURIDAD: se re-corre sobre regiones ya terminadas
        # para recuperar PDFs mal parseados. Por eso no puede pisar los datos que
        # aportaron los pasos POSTERIORES del pipeline, que para su campo son mejores
        # que el BCI:
        #   - `manual`   → corrección del operador en el panel de incidencias. Es la
        #     única que el re-parseo no puede reconstruir: trabajo humano.
        #   - `cadastur` → habitaciones reales del hotel como `uf_comercio`. Medido:
        #     un re-parseo sin guarda tiraba Filinto Müller 62 de 146 UF a 1 (el BCI
        #     cuenta unidades del inmueble, no habitaciones).
        #   - `google`   → conteo real de comercios de Places (fuente autoritativa de
        #     `uf_comercio` según el flujo).
        #   - `hotel_min`    → piso de UF por hotel confirmado SIN conteo de habitaciones
        #     (ago-2026). Va con `cadastur` porque protege lo mismo: que el BCI no borre
        #     "acá hay un hotel" al re-parsear.
        #   - `shopping_min` → piso de UF=1 de ParcelaCategoria para un SHOPPING.
        # ⚠ Esta lista está COPIADA a mano y ya quedó corta: `precedencia.py` protege además
        # `overture`/`poi` (conteo real de comercios) y este UPDATE los pisa. No se unificó
        # acá porque la constante compartida incluye `'bci'` y el parser se auto-bloquearía
        # al re-parsear. Pendiente: el mismo tratamiento que `UF_FUENTES_POI`.
        # El uso va con la misma lista: si el hotel abierto hace comercial a la
        # parcela, el BCI no debe devolverla a vacante/residencial.
        conn.execute(text("""
            UPDATE parcelas SET
                uso_principal               = CASE WHEN uso_fuente IN ('manual','cadastur','hotel_min','google')
                                              THEN uso_principal ELSE COALESCE(:uso, uso_principal) END,
                uso_fuente                  = CASE WHEN uso_fuente IN ('manual','cadastur','hotel_min','google')
                                              THEN uso_fuente
                                                   WHEN :uso IS NOT NULL THEN 'bci'
                                                   ELSE uso_fuente END,
                uf_vivienda                 = CASE WHEN uf_fuente IN ('manual','cadastur','hotel_min','google','shopping_min')
                                              THEN uf_vivienda ELSE :uf_viv END,
                uf_comercio                 = CASE WHEN uf_fuente IN ('manual','cadastur','hotel_min','google','shopping_min')
                                              THEN uf_comercio ELSE :uf_com END,
                uf_fuente                   = CASE WHEN uf_fuente IN ('manual','cadastur','hotel_min','google','shopping_min')
                                              THEN uf_fuente ELSE 'bci' END,
                unidades_funcionales_estimadas = CASE WHEN uf_fuente IN ('manual','cadastur','hotel_min','google','shopping_min')
                                              THEN unidades_funcionales_estimadas
                                              ELSE COALESCE(:uf_tot, unidades_funcionales_estimadas) END,
                area_m2_construida          = COALESCE(:area, area_m2_construida),
                area_m2_terreno             = COALESCE(:area_terr, area_m2_terreno),
                calle                       = CASE WHEN direccion_source = 'manual'
                                              THEN calle ELSE COALESCE(:calle, calle) END,
                numero                      = CASE WHEN direccion_source = 'manual'
                                              THEN numero ELSE COALESCE(:nro, numero) END,
                complemento                 = COALESCE(:comp, complemento),
                barrio                      = CASE WHEN direccion_source = 'manual'
                                              THEN barrio ELSE COALESCE(:barrio, barrio) END,
                codigo_postal               = CASE WHEN direccion_source = 'manual'
                                              THEN codigo_postal ELSE COALESCE(:cep, codigo_postal) END,
                codigo_logradouro           = COALESCE(:cod_logr, codigo_logradouro),
                nomenclatura_catastral      = COALESCE(:nomen, nomenclatura_catastral),
                partida_inmobiliaria        = COALESCE(:mat, partida_inmobiliaria),
                valor_venal_terreno         = COALESCE(:vv_terr, valor_venal_terreno),
                valor_venal_construccion    = COALESCE(:vv_con, valor_venal_construccion),
                valor_venal_total           = COALESCE(:vv_tot, valor_venal_total),
                aliquota                    = COALESCE(:aliq, aliquota),
                anio_construccion           = COALESCE(:anio, anio_construccion),
                propietario_nombre          = COALESCE(:prop, propietario_nombre),
                propietario_documento       = COALESCE(:prop_doc, propietario_documento),
                contribuyente_secundario    = COALESCE(:prop_sec, contribuyente_secundario),
                -- El sello vale por la dirección COMPLETA, no sólo por la calle. Antes se
                -- promovía a 'bci_pdf' con sólo encontrar el logradouro, y como el número se
                -- conserva con COALESCE(:nro, numero), un número que este PDF nunca dijo
                -- —típicamente el que interpola `address_resolver` entre los extremos de
                -- cuadra del IBGE, sellado honestamente como 'ibge_logradouros' con 0.80—
                -- terminaba con el linaje del catastro y confianza 0.95: la inferencia salía
                -- con MÁS confianza de la que entró. Medido en Várzea Grande: en
                -- `zona-varzea-grande-zonalimitada` el parser reproduce 3 de 25 números
                -- guardados, y los otros 22 no aparecen en su propio PDF ni en texto ni en
                -- tablas. Mismo modo de falla que 'overture' pisando 'poi'.
                -- Ahora son tres casos:
                --   · el PDF trae número, o la fila no tenía ninguno → 'bci_pdf'
                --   · el PDF trae sólo la calle y ya había un número → 'bci_pdf_calle'
                --   · el PDF no trae calle                          → se conserva el sello
                -- `bci_pdf_calle` NO va en `catastro_geocoder.FUENTES_AUTORITATIVAS`, a
                -- propósito: la calle es del catastro pero la altura es inferida, así que la
                -- parcela no sirve de ancla para geocodificar. Sí va en la guarda de
                -- `smartgis_fetcher`, que protege el logradouro completo del BCI de que lo
                -- pise el de SmartGIS, que viene sin el tipo de vía.
                direccion_source            = CASE WHEN direccion_source = 'manual' THEN 'manual'
                                                   WHEN :calle IS NOT NULL
                                                    AND (:nro IS NOT NULL OR numero IS NULL)
                                                        THEN 'bci_pdf'
                                                   WHEN :calle IS NOT NULL THEN 'bci_pdf_calle'
                                                   ELSE direccion_source END,
                direccion_confidence        = CASE WHEN direccion_source = 'manual'
                                                   THEN direccion_confidence
                                                   WHEN :calle IS NOT NULL
                                                    AND (:nro IS NOT NULL OR numero IS NULL)
                                                        THEN 0.95
                                                   WHEN :calle IS NOT NULL THEN 0.80
                                                   ELSE direccion_confidence END
            WHERE parcela_id = :pid
        """), {
            "pid": parcela_id,
            "uso": d["uso_principal"],
            "uf_viv": d["uf_vivienda"],
            "uf_com": d["uf_comercio"],
            "uf_tot": d["uf_total"] or None,
            "area": d["area_m2_construida"],
            "area_terr": d["area_m2_terreno"],
            "calle": d["calle"],
            "nro": d["numero"],
            "comp": d["complemento"],
            "barrio": d["barrio"],
            "cep": d["codigo_postal"],
            "cod_logr": d["codigo_logradouro"],
            "nomen": d["nomenclatura_catastral"],
            "mat": d["partida_inmobiliaria"],
            "vv_terr": d["valor_venal_terreno"],
            "vv_con": d["valor_venal_construccion"],
            "vv_tot": d["valor_venal_total"],
            "aliq": d["aliquota"],
            "anio": d["anio_construccion"],
            "prop": d["propietario_nombre"],
            "prop_doc": d["propietario_documento"],
            "prop_sec": d["contribuyente_secundario"],
        })

        # Unidades del imóvel (para expandir el CSV una fila por unidad). Sólo se
        # guardan parcelas con MÁS de una unidad — el resto es una sola fila normal.
        # Idempotente: se reescriben en cada parseo.
        conn.execute(text("DELETE FROM parcela_unidades WHERE parcela_id = :pid"),
                     {"pid": parcela_id})
        unidades = d.get("unidades") or []
        if len(unidades) > 1:
            conn.execute(text("""
                INSERT INTO parcela_unidades
                    (id, parcela_id, n_unidade, codigo_unidade, area_m2, anio_construccion, uso)
                VALUES (:id, :pid, :n, :cod, :area, :anio, :uso)
            """), [{
                "id": str(uuid.uuid4()), "pid": parcela_id,
                "n": u["n"], "cod": (u["codigo"] or "")[:30],
                "area": u["area_m2"], "anio": u["anio"], "uso": u["uso"],
            } for u in unidades])


# ── Entry point ────────────────────────────────────────────────────────────────

@agent_run
def run(input: BCIParserInput) -> BCIParserOutput:
    parcelas = _get_parcelas(input.region_id, input.batch_size)
    if not parcelas:
        logger.warning(
            f"BCIParser: región '{input.region_id}' no tiene parcelas con cca_code "
            f"y fuente_parcela=smartgis_vg/catastro — ¿SmartGISFetcher fue ejecutado?"
        )
        return BCIParserOutput(ok=True, error="Sin parcelas para parsear en esta región")

    logger.info(f"BCIParser: {len(parcelas)} parcelas con cca_code en '{input.region_id}'")
    # pdf_dir efectivo = base/<ciudad>/ — la MISMA carpeta por ciudad que escribe VGBCIFetcher.
    from scrapitero.agents.varzea_bci_fetcher import resolve_city_pdf_dir
    pdf_dir = resolve_city_pdf_dir(input.pdf_dir, input.region_id)

    # Diagnóstico previo: cuántos PDFs existen antes de empezar
    codigos_ok = [int(cca.strip()) for _, cca in parcelas
                  if cca and cca.strip().isdigit()
                  and (pdf_dir / f"reporte_{int(cca.strip())}.pdf").exists()]
    codigos_faltantes = [int(cca.strip()) for _, cca in parcelas
                         if cca and cca.strip().isdigit()
                         and not (pdf_dir / f"reporte_{int(cca.strip())}.pdf").exists()]
    logger.info(
        f"BCIParser pre-check: {len(codigos_ok)} PDFs disponibles, "
        f"{len(codigos_faltantes)} sin PDF en '{pdf_dir}'"
    )
    if codigos_faltantes:
        sample = codigos_faltantes[:10]
        logger.warning(
            f"BCIParser: PDFs faltantes (muestra {len(sample)}/{len(codigos_faltantes)}): "
            f"{sample} — ¿VGBCIFetcher fue ejecutado para '{input.region_id}'?"
        )
        # ¿Están en otra carpeta? Detecta desajuste de pdf_dir fetcher↔parser.
        alt = _buscar_en_dirs_alternativos(codigos_faltantes, pdf_dir)
        if alt:
            detalle = ", ".join(f"{n} en {d}" for d, n in alt.items())
            logger.warning(
                f"BCIParser: ⚠ {len(codigos_faltantes)} PDFs 'faltantes' SÍ existen en otra "
                f"ubicación ({detalle}) pero NO en pdf_dir='{pdf_dir}'. Es un DESAJUSTE de "
                f"pdf_dir entre VGBCIFetcher (descarga) y BCIParser (lee): pasá el MISMO "
                f"pdf_dir absoluto a ambos (o seteá SCRAPITERO_PDF_DIR). No hace falta "
                f"re-descargar."
            )
    if not codigos_ok:
        msg = (
            f"BCIParser: NINGÚN PDF disponible para los {len(parcelas)} cca_codes de "
            f"'{input.region_id}'. Ejecutar VGBCIFetcher primero."
        )
        logger.error(msg)
        return BCIParserOutput(
            ok=False,
            procesadas=0,
            sin_pdf=len(parcelas),
            error=msg,
        )

    procesadas = actualizadas = sin_pdf = errores = 0

    for parcela_id, cca_code in parcelas:
        try:
            codigo = int(cca_code.strip())
        except (ValueError, AttributeError):
            errores += 1
            logger.warning(f"BCIParser: cca_code inválido '{cca_code}' para parcela {parcela_id}")
            continue

        pdf_path = pdf_dir / f"reporte_{codigo}.pdf"
        if not pdf_path.exists():
            sin_pdf += 1
            logger.debug(f"BCIParser: sin PDF para cca_code={codigo} (reporte_{codigo}.pdf)")
            continue

        procesadas += 1
        try:
            text = _pdf_text(pdf_path)
            data = _parse_bci(text)
            _update_parcela(parcela_id, data)
            actualizadas += 1
            logger.debug(
                f"BCIParser: {codigo} → uso={data['uso_principal']} "
                f"uf_viv={data['uf_vivienda']} uf_com={data['uf_comercio']}"
            )

            if procesadas % 50 == 0:
                logger.info(
                    f"BCIParser: {procesadas}/{len(parcelas)} — "
                    f"{actualizadas} actualizadas, {sin_pdf} sin PDF, {errores} errores"
                )
        except Exception as e:
            errores += 1
            logger.error(f"BCIParser: error parseando reporte_{codigo}.pdf: {e}", exc_info=True)

    logger.info(
        f"BCIParser completo '{input.region_id}': "
        f"{procesadas} procesadas, {actualizadas} actualizadas, "
        f"{sin_pdf} sin PDF, {errores} errores"
    )
    if errores > 0:
        logger.warning(
            f"BCIParser: {errores} errores de parseo — revisar PDFs individuales con logs DEBUG"
        )
    return BCIParserOutput(
        ok=True,
        procesadas=procesadas,
        actualizadas=actualizadas,
        sin_pdf=sin_pdf,
        errores=errores,
    )
