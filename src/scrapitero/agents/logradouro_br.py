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


def descomponer_logradouro(calle: str | None) -> dict:
    """Separa `calle` en tipo / título / preposição / nome oficial.

    Devuelve {"tipo": str, "titulo": str, "preposicao": str, "nome": str},
    todos en mayúsculas y con "" cuando el componente no está presente.
    """
    out = {"tipo": "", "titulo": "", "preposicao": "", "nome": ""}
    if not calle or not calle.strip():
        return out

    resto = calle.strip().upper()

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
