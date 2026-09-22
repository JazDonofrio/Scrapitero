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
    # Construcciones que el PDF declara y el parser no pudo leer: esas fichas cayeron al
    # conteo por texto y su comercio puede estar inflado. Debería ser 0.
    construcciones_sin_leer: int = 0
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
# Techo del número de puerta que se copia de la ficha. Ver la guarda en el parseo del CEP.
_NUMERO_MAX_PLAUSIBLE = 20000

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
# Tamaño de kitnet: la unidad de la fila alquilada. Las verificadas miden 16-23 m²
# (Da Liberdade 109) y 18 m² (Filinto Müller 326). Con 30 m² el cotejo da lo mismo.
_KITNET_MAX_M2 = 40.0
# Años entre la construcción más vieja y la más nueva del lote a partir de los cuales las
# construcciones son ampliaciones de una casa y no viviendas hechas juntas. Ver
# `_viviendas_reales`, paso 2.a: entre 2 y 3 años hay casos que se contradicen.
_AMPLIACION_MIN_ANIOS = 5
# Dos casas del mismo año son GEMELAS —viviendas hechas juntas— si la chica mide al menos
# este tanto de la grande. Ver `_viviendas_reales`, paso 2.a.
_GEMELAS_PCT = 0.80


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
      con una o dos es una casa y su ampliación del fondo, y es **una** vivienda: ni la
      proporción ni `DISPOSICAO` sirven para separarlas (ver el paso 3).
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

        # 2) Tres o más construcciones habitables.
        if len(habitables) >= _VILA_MIN_UNIDADES:
            # 2.a) ¿Es una CASA QUE CRECIÓ? Sin fila de kitnets y con construcciones de
            #    años separados, es una sola vivienda con sus ampliaciones — la misma
            #    conclusión que el paso 3 para los pares, con una construcción más.
            #    Las unidades independientes se construyen juntas; las ampliaciones se
            #    agregan con los años.
            #
            #    Medido contra la revisión en Google Maps del cliente (DETALLE
            #    COMPARACION.xlsx, columna GOOGLE, VAZ049, 14-sep-2026) sobre los 19 lotes
            #    cotejados con 3+ construcciones habitables. Hasta acá contaban todas:
            #      PEDRO PEDROSSIAN 220  116+63+38 m²   1950/1980/1990 → 1, contábamos 3
            #      CEL GONÇALO 177       101+83+59 m²   1969/1984/1990 → 1, contábamos 3
            #      MAL RONDON 75         177+94+72 m²   1975/1980      → 1, contábamos 3
            #    Sin mirar los años arreglaba 7 pero reducía a 1 casa el condominio de
            #    PEDRO PEDROSSIAN 139 — cinco bloques de 257 m², todos de 2024, donde el
            #    cliente ve 15 —, que es justo lo contrario: unidades hechas juntas.
            #    El umbral de años es `_AMPLIACION_MIN_ANIOS`: con 2 o 3 años hay casos
            #    que se contradicen (PEDRO PEDROSSIAN 175, 1988/1990, es 1 casa; DA LIBERDADE
            #    71, 1986/1989, son 3 viviendas: dos gemelas de 84 m² agregadas juntas).
            #    Con 5: arregla 5, no rompe ninguna, −16 viviendas en el cotejo.
            #    No toca las dos vilas verificadas en la calle (tienen 23 y 10 kitnets).
            #    Guarda de GEMELAS: dos casas del mismo año y de tamaño parecido se hicieron
            #    juntas como viviendas, aunque el lote tenga además una ampliación posterior
            #    que estire el rango de años (GENEROSO TAVARES 262: 126 + 126 m² de 1987 y
            #    una de 32 m² de 1997). Mismo año pero tamaños distintos NO es gemela:
            #    MAL RONDON 75 tiene 177 y 72 m² de 1980 y el cliente ve una sola casa.
            kitnets = sum(1 for u in habitables
                          if u.get("area_m2") is not None and u["area_m2"] <= _KITNET_MAX_M2)
            anios = [u["anio"] for u in habitables if u.get("anio")]
            casas = [u for u in habitables
                     if u.get("anio") and (u.get("area_m2") or 0) > _KITNET_MAX_M2]
            gemelas = any(
                a["anio"] == b["anio"]
                and min(a["area_m2"], b["area_m2"]) >= _GEMELAS_PCT * max(a["area_m2"],
                                                                          b["area_m2"])
                for i, a in enumerate(casas) for b in casas[i + 1:])
            if (kitnets < _ANEXO_MIN_CHICAS and not gemelas and len(anios) >= 2
                    and max(anios) - min(anios) >= _AMPLIACION_MIN_ANIOS):
                resto = sorted(habitables, key=lambda u: u.get("area_m2") or 0,
                               reverse=True)[1:]
                for u in resto:
                    u["uso"] = "anexo"
                return 1

            # 2.b) Es una VILA de kitnets y manda la proporción, como venía.
            #    (Da Liberdade 109, Filinto Müller 326.)
            chicas = [u for u in habitables
                      if u.get("area_m2") is not None and u["area_m2"] < mayor * _ANEXO_PCT]
            if len(chicas) < _ANEXO_MIN_CHICAS:
                for u in chicas:
                    u["uso"] = "anexo"
                return max(len(habitables) - len(chicas), 1)
            return len(habitables)

        # 3) Una o dos: es UNA casa y, a lo sumo, su ampliación del fondo. **Siempre 1.**
        #    Ni la proporción ni `DISPOSICAO` separan acá una vivienda independiente:
        #
        #    · La proporción no sirve — SEN. VICENTE VUOLO 151 tiene atrás una CASA de
        #      111 m² que es el 61% de la del frente y el operador verificó que es UNA
        #      sola vivienda (sep-2026); con el corte del 30% pasaba holgada.
        #    · `DISPOSICAO` tampoco. Hasta el 14-sep-2026 la segunda contaba como vivienda
        #      aparte si era CONJUGADA, con esta evidencia:
        #        SEN. VICENTE VUOLO 151  CASA 111 m² FUNDOS GEMINADA  → ampliación
        #        JOÃO LIBÂNIO 118 (634305) CASA 60 m² FUNDOS ISOLADA  → ampliación
        #        SÃO BENTO 95            CASA 46 m² FUNDOS GEMINADA   → ampliación
        #        SÃO BENTO 86            CASA 23 m² FUNDOS ISOLADA    → ampliación
        #        DA LIBERDADE 109 / FILINTO MÜLLER 326: CONJUGADA     → viviendas (24 y 10)
        #      Las cuatro verificadas de a DOS construcciones eran todas ampliaciones; la
        #      prueba de que CONJUGADA es vivienda salía de las dos VILAS, de 24 y 10
        #      unidades, y se extendió a los pares sin ningún par verificado. Cotejado
        #      contra la revisión en Google Maps del cliente (DETALLE COMPARACION.xlsx,
        #      columna GOOGLE, VAZ049): de los 13 lotes de dos construcciones con la
        #      segunda CONJUGADA, **los 13 estaban mal** — el cliente ve 1 vivienda (9) o
        #      ninguna (4) donde contábamos 2. Y el tamaño no los salvaba: con la
        #      CONJUGADA exigida al 60% de la principal seguían 7 mal, porque las hay de
        #      11% (JAIME BENEVIDES 189: 434 + 46 m²) y de 96% (MAL RONDON 19: 76 + 73).
        #      Con «siempre 1»: arregla 9 de 243 direcciones cotejadas y no rompe ninguna;
        #      en todo VAZ049 mueve 18 parcelas (−18 viviendas).
        #    La vila de verdad sigue saliendo por el paso 2, que empieza en
        #    `_VILA_MIN_UNIDADES` construcciones habitables.
        resto = sorted(habitables, key=lambda u: u.get("area_m2") or 0, reverse=True)[1:]
        for u in resto:
            u["uso"] = "anexo"
        return 1

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
    quedan = [u for u in locales if u not in chicas]

    # 3) Lo que usa EL DUEÑO es un solo negocio; cada local ALQUILADO es otro.
    #    El BCI dice quién ocupa cada construcción (`REGIME DE UTILIZACAO`). Varios
    #    ambientes comerciales `PROPRIA` son el mismo negocio del dueño repartido en salas;
    #    varios `ALUGADA` son inquilinos distintos. `CEDIDA` y sin dato van con el dueño.
    #    `FECHADA` (cerrada) sigue contando aparte: es un local, aunque hoy esté vacío.
    #    Medido contra la revisión en Google Maps del cliente (DETALLE COMPARACION.xlsx,
    #    columna GOOGLE, VAZ049, 14-sep-2026) sobre 243 direcciones: arregla 5 y no rompe
    #    ninguna — FILINTO MÜLLER 464 (4 salas `PROPRIA` de 71-166 m²: el cliente ve 1
    #    comercio, contábamos 4), JOÃO LIBÂNIO 55 (3→1), COUTO MAGALHAES 450, FILINTO
    #    MÜLLER 218 y PONCE DE ARRUDA 618 (2→1). El área sola no los separaba: son salas
    #    de tamaño parecido y el corte del 30 % no dispara.
    #    La misma idea NO sirve para vivienda: arregla 3, rompe 2 y bajaría la vila
    #    verificada de DA LIBERDADE 109 de 24 a 17 (tiene 9 kitnets `PROPRIA`).
    duenio = [u for u in quedan if (u.get("regime") or "") in ("PROPRIA", "CEDIDA", "")]
    if len(duenio) > 1:
        principal = max(duenio, key=lambda u: u.get("area_m2") or 0)
        for u in duenio:
            if u is not principal:
                u["uso"] = "anexo"
    return max(len(quedan) - max(len(duenio) - 1, 0), 1)


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
        # Filas de la tabla de construcciones que el PDF abre y el regex no pudo leer.
        # > 0 significa que esta ficha cayó al conteo por texto SIN ser un terreno: el
        # comercio queda inflado. Ver la guarda de paridad más abajo.
        "unidades_no_leidas": 0,
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
        # ⚠ El número de unidad se escribe de TRES formas y hay que aceptar las tres:
        # `2` a secas, `2/3` («la 2 de 3») y `1-1` / `1-2` (unidad por construcción). Basta
        # que UNA fila no matchee para que `unit_iter` quede vacío y **la ficha entera** se
        # caiga al conteo por texto del `else` de abajo, que no sabe de tipología ni de área
        # —o sea, sin ninguno de los dos filtros— y cuenta la pileta, el cobertizo y el
        # depósito como locales aparte. Silencioso: `uf_fuente` sigue diciendo `bci` y
        # `parcela_unidades` queda vacía, que es lo mismo que hace una ficha de una sola
        # construcción. Sólo se ve cotejando contra un dato externo.
        #   · `2/3`  → 19 fichas de VAZ049; `PONCE DE ARRUDA 802` declaraba SALA + SALA +
        #     TELHEIRO y salía con 3 comercios en vez de 2 (10-sep-2026).
        #   · `1-1`  → 3 fichas de VAZ049, 2 de ellas en Couto Magalhães y las dos con el
        #     comercio inflado (12-sep-2026, cotejo contra la columna GOOGLE del cliente):
        #     el `260` contaba 2 locales y es 1 (CASA de 860 m² + SALA de 94 = 11 %, anexo)
        #     y el `520` contaba 3 y son 2 (SALA 208 + SALA 113 + TELHEIRO, que no es local).
        # El separador se lee como clase (`[/-]`) y no como alternativa suelta, así que si
        # el municipio agrega otro basta sumarlo acá; y la GUARDA de más abajo avisa cuando
        # aparece un formato nuevo, en vez de degradar sin decir nada.
        unit_iter = list(re.finditer(
            r'UNIDADE\s+C[OÓ]DIGO\s+DA\s+UNIDADE.*?TOTAL\s*\n\s*'
            r'(\d+(?:[/-]\d+)?)\s+(\d+)\s+([\d,]+)\s+([\d,]+)',
            text, re.I | re.DOTALL,
        ))
        # GUARDA DE PARIDAD: cuántas veces el PDF abre la tabla de construcciones contra
        # cuántas filas se pudieron leer. Si el header está y la fila no matcheó, es un
        # formato de numeración que no conocemos —no una ficha sin construcciones— y el
        # conteo por texto va a inflar el comercio. Se devuelve para que el loop del agente
        # lo loguee con el cca_code, que es el único lugar donde se sabe de qué ficha se
        # trata. Esto es lo que faltó las dos veces: el modo de falla no era el regex, era
        # que fallara **en silencio**.
        headers = len(re.findall(r'UNIDADE\s+C[OÓ]DIGO\s+DA\s+UNIDADE', text, re.I))
        r["unidades_no_leidas"] = max(headers - len(unit_iter), 0)
        unidades = []
        for i, mu in enumerate(unit_iter):
            n_u, cod_u, area_u, _area_tot = mu.groups()
            # `1-1` y `1-2` son dos construcciones del mismo bloque: el número que queda es
            # el mismo para las dos. Para contar UF da igual (lo que cuenta es la cantidad
            # de bloques leídos), pero `parcela_unidades.n_unidade` es un entero y las
            # guarda a las dos como 1. El código de la unidad (`codigo_unidade`) sí es
            # único, así que es el que identifica la fila.
            n_u = re.split(r"[/-]", n_u)[0]
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
            # Quién ocupa la construcción: PROPRIA (el dueño), ALUGADA, CEDIDA, FECHADA.
            # Lo usa `_comercios_reales` para no contar como negocios distintos los
            # ambientes que usa un mismo dueño.
            reg_m = re.search(r'REGIME DE UTILIZACAO\s+([A-ZÀ-Ú]+)', blk, re.I)
            # Dónde está la construcción en el lote. Vocabulario de VAZ049: FRENTE 642 ·
            # FUNDOS 359 · `SUP. DE FRENTE` 14 · `SUPL. DE FUNDOS` 14 (superpuesta: arriba de
            # otra) · sin dato 85. Lo usa la regla de avenida del final de `_parse_bci`.
            sit_m = re.search(r'SITUACAO\s+(FRENTE|FUNDOS|SUPL?\.?\s*DE\s+(?:FRENTE|FUNDOS))',
                              blk, re.I)
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
                "regime": reg_m.group(1).upper() if reg_m else None,
                "situacao": (("SUPERPOSTA" if sit_m.group(1).upper().startswith("SUP")
                              else sit_m.group(1).upper()) if sit_m else None),
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
                # **Guarda de número imposible.** La ficha puede imprimir un centinela en el
                # lugar del número: Rua São Bernardo `51000`, en una calle cuyos 13 lotes van
                # del 12 al 400 (cca 101203, VAZ049). Copiado tal cual salía al entregable como
                # `51000C`, y la dirección no existe. Por encima de `_NUMERO_MAX_PLAUSIBLE` no
                # se copia: la parcela queda sin número —que es lo que el municipio realmente
                # declara— y `IncidenciasReporter` la levanta como `numero_faltante`.
                # El tope es holgado a propósito: el número más alto de Várzea Grande es 1.096
                # y en el conurbano bonaerense hay alturas de 5 dígitos, así que sólo corta lo
                # que no puede ser una puerta.
                if int(num_m.group(1)) <= _NUMERO_MAX_PLAUSIBLE:
                    r["numero"] = num_m.group(1)
                else:
                    logger.warning(
                        f"BCIParser: número {num_m.group(1)} descartado por imposible "
                        f"(> {_NUMERO_MAX_PLAUSIBLE}) en «{before}»; la parcela queda S/N")
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

    # ── Lote mixto sobre AVENIDA: la casa del FRENTE es el local ──────────────────
    # La ficha declara una casa y una construcción comercial en el mismo lote, y la calle es
    # una avenida. **El criterio de conteo es 1 vivienda + 1 comercio** (Jaz, 15-sep-2026:
    # «casa más comercio se cuenta 1 para residencia y 1 para comercio»), así que la casa
    # sólo se descarta cuando no es un hogar aparte: cuando es la construcción del FRENTE,
    # la que da a la avenida y en la calle se ve como el negocio. La del FUNDOS o la que
    # está encima (`SUP. DE FRENTE`, `SUPL. DE FUNDOS`) es otra casa y cuenta. Va acá al
    # final porque la calle de la ficha se lee después de las UF.
    #
    # Medido contra la revisión en Google Maps del cliente (DETALLE COMPARACION.xlsx,
    # columna GOOGLE, VAZ049). De 38 lotes mixtos cotejados el cliente anota UNA sola cosa
    # en 34; sobre AVENIDA (20): sólo comercio 16 · sólo casa 3 · las dos 1. El 14-sep eso
    # se leyó como convención («el cliente cuenta UN comercio») y la regla descartaba la
    # casa siempre. **No era convención:** de los 27 lotes que tocaba, en 16 la casa está al
    # FUNDOS o arriba del local, adonde la revisión desde la calle no llega — el mismo punto
    # ciego de las vilas (COUTO MAGALHAES 520, casa de 91 m² al fondo, el cliente sí la ve).
    # Con la casa del FRENTE el cliente acierta: de 10 cotejados, en 8 no ve casa aparte
    # (caso tipo PEDRO PEDROSSIAN 447, CASA 236 m² al frente + SALA 29 m²); en PEDRO
    # PEDROSSIAN 78 y 385 sí la ve.
    #
    # ⚠ Las VILAS quedan afuera por `uf_vivienda == 1`: se mira cuántas viviendas QUEDARON
    # después de `_viviendas_reales`, no cuántas construcciones declara la ficha. Las 10
    # kitnets de FILINTO MÜLLER 326 — una de las dos vilas verificadas en la calle, donde
    # el cliente anota 0 viviendas — terminan en 10 y no se tocan: esas dos fuentes se
    # contradicen y no lo decide una regla.
    # Hasta el 14-sep la guarda era `construcciones habitables declaradas ≤ 2`, y eso
    # dejaba afuera a la casa que creció (paso 2.a de `_viviendas_reales`): declara 3 o más
    # construcciones pero es UNA vivienda. PEDRO PEDROSSIAN 424 —lote de 8.095 m² sobre la
    # avenida, 7 construcciones, Receita `COMÉRCIO EM GERAL, IMOBILIÁRIA`— seguía saliendo
    # mixto. Mirando el resultado final arreglaba FILINTO MÜLLER 488 y COUTO MAGALHAES 535
    # (el cliente ve sólo comercio); desde el 15-sep 488 vuelve a 1 + 1, porque sus cinco
    # casas están al FUNDOS.
    casas = [u for u in r["unidades"]
             if (u.get("uso") or "residencial") == "residencial"
             and (u.get("tipologia") or "") not in _TIPOLOGIA_NO_HABITABLE]
    casa = max(casas, key=lambda u: u.get("area_m2") or 0) if casas else None
    if (casa is not None and casa.get("situacao") == "FRENTE"
            and r["uf_vivienda"] == 1 and r["uf_comercio"] >= 1
            and re.match(r"AVENIDA\b", (r["calle"] or "").upper())):
        for u in r["unidades"]:
            # Mismo default que `_viviendas_reales` y que el CSV por unidad: la
            # construcción sin línea `USO` cuenta como vivienda (FILINTO MÜLLER 310).
            if (u.get("uso") or "residencial") == "residencial":
                u["uso"] = "anexo"   # el CSV por unidad no la emite como vivienda
        r["uf_vivienda"] = 0
        r["uso_principal"] = "comercial"

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


# ── Un solo negocio en la puerta (Receita) ─────────────────────────────────────
# Este parser es de Várzea Grande: contra ese padrón se cruzan las fichas.
_RECEITA_MUNICIPIO = "VARZEA GRANDE"
_PALABRAS_VIA = re.compile(
    r"\b(AVENIDA|AV|RUA|R|TRAVESSA|TV|BECO|ALAMEDA|PRACA|SEN|SENADOR|GOV|GOVERNADOR|CEL|"
    r"CORONEL|DR|DOUTOR|PRES|PRESIDENTE|DE|DA|DO|DOS|DAS)\b")
_SUFIJOS_EMPRESA = re.compile(r"\b(LTDA|ME|EPP|EIRELI|SA|S A|MEI)\b")


def _sin_acentos(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().upper()


def _clave_via(calle: str) -> str:
    """El nombre de la vía sin tipo, títulos ni preposiciones: `RUA - GOV PEDRO PEDROSSIAN`,
    `RUA GOVERNADOR PEDRO PEDROSSIAN (LOT CENTRO)` y `AV GOV PEDRO PEDROSSIAN` dan lo mismo."""
    s = re.sub(r"\([^)]*\)", " ", _sin_acentos(calle))
    s = _PALABRAS_VIA.sub(" ", re.sub(r"[^A-Z0-9]+", " ", s))
    return " ".join(s.split())


def _clave_negocio(nombre: str) -> str:
    """Dos CNPJ con el mismo nombre (matriz y filial, o MEI y LTDA) son el mismo negocio."""
    s = re.sub(r"^[\d.\s]+", "", _sin_acentos(nombre))
    s = _SUFIJOS_EMPRESA.sub(" ", re.sub(r"[^A-Z0-9]+", " ", s))
    return " ".join(s.split())


def _negocios_por_puerta() -> dict[tuple[str, str], set[str]]:
    """Negocios distintos con CNPJ ACTIVO en cada (vía, número) del municipio.

    Se cruza por calle y número porque el padrón cargado no trae coordenadas. Sólo `ATIVA`:
    una empresa dada de baja no es un negocio que esté hoy en el lote.
    """
    engine = get_engine()
    idx: dict[tuple[str, str], set[str]] = {}
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT coalesce(tipo_logradouro, '') || ' ' || coalesce(logradouro, ''),
                   regexp_replace(coalesce(numero, ''), '\\D', '', 'g'),
                   coalesce(nullif(nome_fantasia, ''), razao_social)
            FROM receita_estabelecimentos
            WHERE municipio_nome = :mun AND situacao = 'ATIVA'
        """), {"mun": _RECEITA_MUNICIPIO}).fetchall()
    for via, num, nombre in rows:
        if num:
            idx.setdefault((_clave_via(via), num), set()).add(_clave_negocio(nombre))
    return idx


def _un_solo_negocio(data: dict, negocios: dict[tuple[str, str], set[str]]) -> bool:
    """Varias construcciones comerciales y UN solo negocio en la puerta → 1 comercio.

    Criterio de conteo del cliente (Jaz, 14-sep-2026): se cuentan NEGOCIOS, no salas. Ocho
    negocios en un edificio son 8; varias salas de un mismo negocio son 1. Del BCI se sabe
    cuántas construcciones comerciales hay, pero no si son del mismo negocio: eso lo dice
    Receita cuando en la puerta hay un único CNPJ activo.

    Medido contra la revisión en Google Maps del cliente (DETALLE COMPARACION.xlsx, VAZ049):
    arregla 4, no rompe ninguna y no aleja ninguna. En las 4 una segunda fuente coincide —
    COUTO MAGALHAES 520 (Auto Visão, también en Google), 475 (Sanches Automóveis; el plano
    del cliente de 2014 decía 1), PONCE DE ARRUDA 6 (Aeroporto Grill, también en Google),
    CEL GONÇALO 187 (Marmitt; plano 2014: 1).
    ⚠ Sólo BAJA. Subir con "negocios que aparecen" rompía entre 3 y 14 lotes: Receita tiene
    empresas registradas en domicilios sin local, y Google repite un negocio con variantes
    y le atribuye negocios del vecino. Por eso PEDRO PEDROSSIAN 100 (8 negocios en un
    edificio de salas) quedó con un sello manual y no con una regla.
    """
    num = re.sub(r"\D", "", data.get("numero") or "")
    if data.get("uf_comercio", 0) <= 1 or not num or not data.get("calle"):
        return False
    if len(negocios.get((_clave_via(data["calle"]), num), set())) != 1:
        return False
    com = [u for u in data.get("unidades") or [] if u.get("uso") == "comercial"]
    if len(com) > 1:
        principal = max(com, key=lambda u: u.get("area_m2") or 0)
        for u in com:
            if u is not principal:
                u["uso"] = "anexo"   # el CSV por unidad tampoco la emite como comercio
    data["uf_comercio"] = 1
    return True


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
        #   - `regla_hoteles` → el lote del hotel es especial (0 UF) o pensão según la tabla
        #     del cliente (HotelReglas, sep-2026). Sin esto el re-parseo le devolvía al Slaviero
        #     Slim el "baldío" de su ficha vieja y al Amazon su única unidad del BCI.
        # ⚠ Esta lista está COPIADA a mano y ya quedó corta: `precedencia.py` protege además
        # `overture`/`poi` (conteo real de comercios) y este UPDATE los pisa. No se unificó
        # acá porque la constante compartida incluye `'bci'` y el parser se auto-bloquearía
        # al re-parsear. Pendiente: el mismo tratamiento que `UF_FUENTES_POI`.
        # El uso va con la misma lista: si el hotel abierto hace comercial a la
        # parcela, el BCI no debe devolverla a vacante/residencial.
        conn.execute(text("""
            UPDATE parcelas SET
                uso_principal               = CASE WHEN uso_fuente IN ('manual','cadastur','hotel_min','google','regla_hoteles')
                                              THEN uso_principal
                                              -- UF sellada a mano y uso no: el uso sale de la UF
                                              -- sellada, no del parser. Si no, el BCI deja
                                              -- 'comercial' con 1 vivienda sellada (PEDROSSIAN
                                              -- 262) o 'vacante' con 1 comercio (DA LIBERDADE 0).
                                              -- Las columnas de la derecha son las VIEJAS: la UF
                                              -- manual que este mismo UPDATE no toca.
                                              WHEN uf_fuente = 'manual' THEN
                                                  CASE WHEN uf_comercio > 0
                                                        AND uf_vivienda > 0 THEN 'mixto'
                                                       WHEN uf_comercio > 0 THEN 'comercial'
                                                       WHEN uf_vivienda > 0 THEN 'residencial'
                                                       ELSE COALESCE(:uso, uso_principal) END
                                              ELSE COALESCE(:uso, uso_principal) END,
                uso_fuente                  = CASE WHEN uso_fuente IN ('manual','cadastur','hotel_min','google','regla_hoteles')
                                              THEN uso_fuente
                                                   WHEN :uso IS NOT NULL THEN 'bci'
                                                   ELSE uso_fuente END,
                uf_vivienda                 = CASE WHEN uf_fuente IN ('manual','cadastur','hotel_min','google','shopping_min','regla_hoteles')
                                              THEN uf_vivienda ELSE :uf_viv END,
                uf_comercio                 = CASE WHEN uf_fuente IN ('manual','cadastur','hotel_min','google','shopping_min','regla_hoteles')
                                              THEN uf_comercio ELSE :uf_com END,
                uf_fuente                   = CASE WHEN uf_fuente IN ('manual','cadastur','hotel_min','google','shopping_min','regla_hoteles')
                                              THEN uf_fuente ELSE 'bci' END,
                unidades_funcionales_estimadas = CASE WHEN uf_fuente IN ('manual','cadastur','hotel_min','google','shopping_min','regla_hoteles')
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
    # Construcciones declaradas que ningún regex pudo leer (guarda de paridad).
    sin_leer = 0
    # Negocios con CNPJ activo por puerta, para `_un_solo_negocio`.
    negocios = _negocios_por_puerta()
    un_negocio = 0

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
            if _un_solo_negocio(data, negocios):
                un_negocio += 1
            # La ficha abre la tabla de construcciones y no se pudo leer alguna fila: es un
            # formato de numeración nuevo, y esta parcela se contó por texto (sin filtro de
            # tipología ni de área). No se interrumpe el parseo —el dato por texto es mejor
            # que nada— pero tiene que quedar dicho con el código de la ficha para poder ir
            # a buscarla. Ver la guarda de paridad en `_parse_bci`.
            if data.get("unidades_no_leidas"):
                sin_leer += data["unidades_no_leidas"]
                logger.warning(
                    f"BCIParser: reporte_{codigo}.pdf declara "
                    f"{data['unidades_no_leidas']} construcción/es que no se pudieron leer "
                    f"(numeración de UNIDADE con un formato nuevo) — la ficha se contó por "
                    f"texto y el comercio puede estar inflado: "
                    f"uf_viv={data['uf_vivienda']} uf_com={data['uf_comercio']}"
                )
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
    if un_negocio:
        logger.info(f"BCIParser: {un_negocio} fichas con varias salas comerciales y un solo "
                    f"negocio en Receita quedaron en 1 comercio")
    if sin_leer > 0:
        logger.warning(
            f"BCIParser: {sin_leer} construcciones declaradas que no se pudieron leer — "
            f"esas fichas se contaron por texto y el comercio puede estar inflado. "
            f"Buscar 'formato nuevo' en el log para los cca_code."
        )
    return BCIParserOutput(
        ok=True,
        procesadas=procesadas,
        actualizadas=actualizadas,
        sin_pdf=sin_pdf,
        errores=errores,
        construcciones_sin_leer=sin_leer,
    )
