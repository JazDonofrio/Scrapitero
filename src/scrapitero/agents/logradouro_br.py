"""Descomposición de logradouros brasileños para el CSV de operadora.

`parcelas.calle` guarda el logradouro completo tal como viene de la fuente:
  - BCI:      "RUA - BRIG. EDUARDO GOMES", "AVENIDA - DO INDEPENDENTE"
  - SmartGIS: "MARIO MOTTA" (sin tipo)

El layout de operadora pide los componentes por separado:
  NOME_TIPO_LOGR (RUA/AVENIDA/…) · NOME_TITULO (DOUTOR/BRIGADEIRO/…) ·
  PREPOSICAO (DE/DO/DA/DOS/DAS) · NOME_OFICIAL_LOGR (el resto)

La descomposición es heurística por diccionario: si un componente no se
reconoce, queda vacío y el texto va entero a NOME_OFICIAL_LOGR — nunca se
pierde información.
"""

from __future__ import annotations

import re

# Tipos de logradouro reconocidos (prefijo "TIPO - NOMBRE" del BCI o primer token)
TIPOS_LOGR = {
    "RUA", "AVENIDA", "AV", "TRAVESSA", "ALAMEDA", "ESTRADA", "RODOVIA",
    "PRAÇA", "PRACA", "BECO", "VIELA", "VIA", "LARGO", "PASSAGEM",
    "SERVIDÃO", "SERVIDAO", "MARGINAL", "ANEL", "CONTORNO", "LADEIRA",
}

# Títulos honoríficos/profesionales (con y sin abreviar; los puntos se ignoran)
TITULOS = {
    "DR", "DRA", "DOUTOR", "DOUTORA",
    "PROF", "PROFA", "PROFESSOR", "PROFESSORA",
    "ENG", "ENGENHEIRO", "ENGENHEIRA",
    "CEL", "CORONEL", "BRIG", "BRIGADEIRO", "MAL", "MARECHAL",
    "GAL", "GEN", "GENERAL", "MAJ", "MAJOR", "CAP", "CAPITAO", "CAPITÃO",
    "TEN", "TENENTE", "SGT", "SARGENTO", "ALM", "ALMIRANTE", "CMTE", "COMANDANTE",
    "PRES", "PRESIDENTE", "SEN", "SENADOR", "DEP", "DEPUTADO",
    "GOV", "GOVERNADOR", "PREF", "PREFEITO", "VER", "VEREADOR",
    "MIN", "MINISTRO", "DES", "DESEMBARGADOR", "EMB", "EMBAIXADOR",
    "PE", "PADRE", "FREI", "DOM", "MONS", "MONSENHOR", "PASTOR", "BISPO",
}

PREPOSICOES = {"DE", "DO", "DA", "DOS", "DAS"}

# ── Complemento del BCI: un cajón de sastre ────────────────────────────────
# El municipio usa ese campo para cuatro cosas distintas, y sólo una es parte de
# la dirección. Medido en `zona-varzea-grande-actualizacion` (158 complementos):
#   · 74 nombre del inmueble/comercio  → "MECATRONICA AUTOMACAO INDUSTRIAL", "DROGASIL"
#   · 54 nota registral                → "AREA DESMEMBRADA \"A\"", "MAT.57499"
#   · 23 unidad (dirección de verdad)  → "QUADRA 04 LOTE 13", "QDA C LT 27", "sl 02"
#   ·  6 referencia de ubicación       → "ESQUINA COM A RUA JOÃO LIBANIO"
# O sea el 85% de lo que se pegaba a la dirección no era dirección.

# Unidad: prefijos de quadra/lote/sala/apto… El `(?=…)` en vez de `\b` es necesario
# porque el BCI los pega al número sin separador ("Q12 L13A17", "Q20L9,12,13").
# Tolera ".", ":" y "," ("Q.5 L.31", "N:84", "QD.C", "Q B, L 17").
_UNIDAD = re.compile(
    r"^(Q|QA|QD|QDA|QUADRA|L|LT|LOTE|SL|SALA|SALAS|APT|APTO|AP|BL|BLOCO|CASA|CS|"
    r"ANDAR|LOJA|LJ|BOX|CJ|CONJ|N|Nº|PT|BOSQUE|RESID|RESIDENCIA|RESIDÊNCIA)"
    r"(?=[\s.:,\-]|\d|$)", re.I)
# Nota registral: matrícula y las particiones catastrales del lote. Se listan las
# variantes tal como las escribe el municipio, erratas incluidas ("AEA", "DESMAMBRADA").
_NOTA = re.compile(
    r"^(MAT\.?\s*\d|MATR[IÍ]CULA|PARTE\s+DO\s+LOTE|DESMEMBRA|"
    r"([ÁA]|AE)[ÁA]?REAS?\b|[ÁA]REA\s*[\"']?[\w º°]{1,6}[\"']?$|REMANESCENTE)", re.I)
_NOTA_CONTIENE = re.compile(
    r"\b(DESMEMBRAD|DESMAMBRAD|REMEMBRAD|REMANESCENT|UNIFICAD)", re.I)
# Referencia para encontrar el domicilio, no la dirección en sí.
_REFERENCIA = re.compile(
    r"^(FACE\b|ESQ\b|ESQ\.|ESQUINA\b|PRA[ÇC]A\s+EM\s+FRENTE|EM\s+FRENTE|"
    r"\(?ANTIGA\b|CONTINUA[ÇC][ÃA]O|LUGAR\s+DENOMINADO)", re.I)
# Unidad pegada al final del nombre, sin separador ("… QD 4 LOTE 4").
_UNIDAD_SUFIJO = re.compile(
    r"\b(QD|QDA|QUADRA|Q)\s*\.?\s*\w{1,3}\b(\s+(L|LT|LOTE)\s*\.?\s*\w{1,3}\b)?\s*$", re.I)
# Bairro/loteamento: identifica la zona, no el inmueble ni su unidad.
_BAIRRO = re.compile(
    r"^(JD\b|JARDIM\b|VILA\b|LOT\.|LOTEAMENTO\b|PQ\b|PARQUE\b|BAIRRO\b|"
    r"CENTRO[\s\-]NORTE$)", re.I)


# Abreviatura con la que la operadora rotula cada tipo de unidad dentro del inmueble
# (`DSC_IMOVEL_TIPO_COMPLEMENTO1`). El vocabulario sale de su propio CSV: QD, LT, BL, CASA,
# SALA, LJ, FD, BSD. Lo que no está acá se manda tal cual, en mayúsculas.
_ABREV_UNIDAD = {
    "QUADRA": "QD", "QDA": "QD", "QD": "QD", "Q": "QD",
    "LOTE": "LT", "LT": "LT", "L": "LT",
    "BLOCO": "BL", "BL": "BL",
    "CASA": "CASA", "CS": "CASA",
    "SALA": "SALA", "SALAS": "SALA", "SL": "SALA",
    "LOJA": "LJ", "LJ": "LJ",
    "APTO": "APT", "APT": "APT", "AP": "APT",
    "ANDAR": "AND", "BOX": "BOX", "CONJ": "CJ", "CJ": "CJ",
}


def partes_unidad(unidad: str | None) -> list[tuple[str, str]]:
    """Parte la unidad en pares (tipo, texto) con el vocabulario de la operadora.

    "QUADRA 04 LOTE 13" → [("QD","04"), ("LT","13")] · "sl 02" → [("SALA","02")] ·
    "QD.C" → [("QD","C")]. El layout tiene cuatro pares de columnas
    (`DSC_IMOVEL_TIPO_COMPLEMENTO1..4` / `..._TEXTO_...`), así que se devuelven en orden.

    Se recorre por TOKENS y sólo se aceptan tipos del vocabulario conocido. Buscar el patrón
    con una regex suelta partía palabras por la mitad ("SALA SERVIÇOS" → tipo "ERVIÇO",
    "DA LIBERDADE" → "BERDAD"): lo que no se reconoce se deja en blanco, que es lo correcto —
    igual sigue entero en la dirección.
    """
    if not unidad or not unidad.strip():
        return []
    # "L.21" / "QD.C" / "N:84" vienen pegados: se separan en dos tokens antes de recorrer.
    # SÓLO si hay un separador explícito o un dígito — si no, una palabra entera como "QUADRA"
    # se partía en "QUADR"+"A" y el tipo salía irreconocible.
    tokens: list[str] = []
    for t in re.split(r"[\s,]+", unidad.strip()):
        m = (re.fullmatch(r"([A-Za-zÀ-ÿ]+)[.:]([0-9]+[A-Za-z]?|[A-Za-z])", t)
             or re.fullmatch(r"([A-Za-zÀ-ÿ]+)[.:]?([0-9]+[A-Za-z]?)", t))
        tokens.extend([m.group(1), m.group(2)] if m else [t])

    pares: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        clave = tokens[i].upper().strip(".:")
        abrev = _ABREV_UNIDAD.get(clave)
        if abrev and i + 1 < len(tokens):
            valor = tokens[i + 1].upper().strip(".:")
            # El valor de una unidad es un número o una letra suelta ("04", "13A", "C"),
            # no otra palabra ("SALA COMERCIAL" no lleva número).
            if re.fullmatch(r"[0-9]{1,4}[A-Z]?|[A-Z]", valor):
                pares.append((abrev, valor))
                i += 2
                continue
        i += 1
    return pares


def _segmentos_complemento(texto: str) -> list[str]:
    """Parte el complemento en sus piezas. El BCI las junta con ' - ' y entrecomilla
    el nombre: «"CHURRASCARIA AEROPORTO GRILL" - ÁREA 01 - MATRÍCULA 70.846»,
    «SORPAN - MAT. 24.917»."""
    partes: list[str] = []
    for bruto in re.split(r"\s+-\s+|\s+[–—]\s+", texto):
        s = bruto.strip().strip('"“”\'').strip()
        if s and s not in {",", "."}:
            partes.append(s)
    return partes


def clasificar_complemento(complemento: str | None) -> tuple[str, str]:
    """Separa el complemento del BCI en (parte_de_la_direccion, nombre_del_inmueble).

    Sólo la unidad es dirección. El nombre sale por su columna propia
    (`DSC_NOME_DO_IMOVEL`) y las notas registrales y referencias no van a ninguna:
    una columna de dirección tiene que contener una dirección. Nada se pierde — el
    valor crudo sigue en `parcelas.complemento` y en el CSV completo del relevamiento.
    """
    if not complemento or not complemento.strip():
        return "", ""
    unidad, nombre = [], []
    for s in _segmentos_complemento(complemento):
        if (_NOTA.match(s) or _NOTA_CONTIENE.search(s) or _REFERENCIA.match(s)
                or _BAIRRO.match(s)):
            continue
        if re.fullmatch(r"\d{1,5}", s):
            unidad.append(s)                     # número suelto: "237"
        # "QUADRA 04 LOTE 13", "Q12 L13": prefijo de unidad Y algo que lo numere.
        elif _UNIDAD.match(s) and re.search(r"\d", s):
            unidad.append(s)
        elif _UNIDAD.match(s) and re.fullmatch(r"[\w.:]+\W*[A-Z]", s.strip(), re.I):
            unidad.append(s)                     # "QD.C", "Q A"
        elif re.fullmatch(r"SALA(S)?\s+COMERCIA\w*|SALA\s+\w+", s, re.I):
            unidad.append(s)                     # "SALA COMERCIAL", "SALA SERVIÇOS"
        else:
            # La unidad puede venir pegada al nombre sin separador:
            # "HOTEL PORTAL DA AMAZÔNIA QD 4 LOTE 4" → nombre + "QD 4 LOTE 4".
            m = _UNIDAD_SUFIJO.search(s)
            if m and m.start() > 0:
                unidad.append(s[m.start():].strip())
                s = s[:m.start()].strip()
            if s:
                nombre.append(s)
    return " ".join(unidad).strip(), " ".join(nombre).strip()


# El BCI mete el "S/N" del NÚMERO dentro del nombre de la vía y deja puntos sueltos al
# final (ver [[bci-etiquetas-calle-no-confiables]]). Sin sacarlo, el entregable sale con el
# S/N duplicado —`R ZEQUINHA DE ABREU S/N   S/N QD 43 LT 11`, una vez en el nombre y otra en
# la columna del número— y además parte la calle en dos: "SANTA LAURA" y "SANTA LAURA S/N"
# salían como dos logradouros distintos con el mismo CEP y el mismo código.
_S_N_AL_FINAL = re.compile(r"[\s.,;]*\bS/?\s?N\.?$", re.IGNORECASE)


def limpiar_nombre_via(calle: str | None) -> str:
    """El nombre de la vía sin la basura de la etiqueta de origen, en mayúsculas.

    Saca sólo un `S/N` **final**: en el medio puede ser parte del nombre. Y si al sacarlo no
    queda nada más que el tipo de vía ("RUA S/N" → "RUA"), el S/N era el nombre y se deja
    como estaba: perder el nombre es peor que dejar el ruido.
    """
    s = re.sub(r"\s+", " ", (calle or "").strip().upper())
    limpio = _S_N_AL_FINAL.sub("", s).strip(" .,;-")
    sin_guion = re.sub(r"\s*-\s*", " ", limpio).split()
    return limpio if len(sin_guion) >= 2 else s.strip(" .,;")


def descomponer_logradouro(calle: str | None) -> dict:
    """Separa `calle` en tipo / título / preposição / nome oficial.

    Devuelve {"tipo": str, "titulo": str, "preposicao": str, "nome": str},
    todos en mayúsculas y con "" cuando el componente no está presente.
    """
    out = {"tipo": "", "titulo": "", "preposicao": "", "nome": ""}
    if not calle or not calle.strip():
        return out

    resto = limpiar_nombre_via(calle)

    # Tipo: formato BCI "TIPO - NOMBRE" o primer token reconocido
    m = re.match(r"^([^-]+?)\s*-\s*(.+)$", resto)
    if m and m.group(1).strip() in TIPOS_LOGR:
        out["tipo"] = m.group(1).strip()
        resto = m.group(2).strip()
    else:
        tokens = resto.split(None, 1)
        if len(tokens) == 2 and tokens[0] in TIPOS_LOGR:
            out["tipo"] = tokens[0]
            resto = tokens[1]

    # Título: primer token contra diccionario (ignorando puntos: "BRIG." → BRIG)
    tokens = resto.split(None, 1)
    if len(tokens) == 2 and tokens[0].rstrip(".") in TITULOS:
        out["titulo"] = tokens[0].rstrip(".")
        resto = tokens[1]

    # Preposição: DE/DO/DA/DOS/DAS antes del nombre
    tokens = resto.split(None, 1)
    if len(tokens) == 2 and tokens[0] in PREPOSICOES:
        out["preposicao"] = tokens[0]
        resto = tokens[1]

    out["nome"] = resto.strip()
    return out
