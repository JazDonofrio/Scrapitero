"""Descomposición de calles argentinas para el CSV de operadora (perfil `ARG`).

Equivalente a `logradouro_br.py` pero para Argentina, donde la vía se rotula muy
distinto: no hay un prefijo `TIPO - NOMBRE` como el del BCI, y en la enorme mayoría
de los casos la calle viene **sin tipo** ("Arturo Jauretche", "Juan Díaz de Solís"
en Hurlingham). El tipo aparece sólo cuando lo hay de verdad (avenidas, pasajes,
diagonales, rutas) y casi siempre abreviado: "Av. Vergara", "Pje. San Martín".

El layout pide TIPO_CALLE y NOMBRE_CALLE por separado. La descomposición es
heurística por diccionario: si el primer token no es un tipo reconocido, `tipo`
queda vacío y el texto va **entero** a `nombre` — nunca se pierde información
(mismo criterio que la versión brasilera).
"""

from __future__ import annotations

import re

# Tipos de vía reconocidos → forma canónica. Las claves son variantes tal como las
# escriben las fuentes argentinas (catastro, OSM, Google), con y sin abreviar; el
# punto final y los acentos se normalizan antes de buscar.
TIPOS_VIA = {
    "CALLE": "CALLE",
    "AVENIDA": "AVENIDA", "AVDA": "AVENIDA", "AVD": "AVENIDA", "AV": "AVENIDA",
    "PASAJE": "PASAJE", "PJE": "PASAJE", "PSJE": "PASAJE", "PJ": "PASAJE",
    "DIAGONAL": "DIAGONAL", "DIAG": "DIAGONAL", "DG": "DIAGONAL",
    "BOULEVARD": "BOULEVARD", "BOULEVAR": "BOULEVARD", "BULEVAR": "BOULEVARD",
    "BLVD": "BOULEVARD", "BV": "BOULEVARD", "BVD": "BOULEVARD",
    "RUTA": "RUTA", "RUTA NACIONAL": "RUTA", "RUTA PROVINCIAL": "RUTA",
    "RN": "RUTA", "RP": "RUTA",
    "AUTOPISTA": "AUTOPISTA", "AU": "AUTOPISTA",
    "ACCESO": "ACCESO",
    "COLECTORA": "COLECTORA",
    "CAMINO": "CAMINO", "CNO": "CAMINO",
    "PASEO": "PASEO",
    "COSTANERA": "COSTANERA",
    "PEATONAL": "PEATONAL",
    "CALLEJON": "CALLEJON",
    "SENDERO": "SENDERO",
    "PLAZA": "PLAZA", "PLAZOLETA": "PLAZOLETA",
    "PARQUE": "PARQUE",
    "RONDA": "RONDA",
}

# El tipo puede venir en dos tokens ("RUTA NACIONAL 8", "RUTA PROVINCIAL 4").
_MAX_TOKENS_TIPO = 2


def _canon(tok: str) -> str:
    """Token en mayúsculas, sin acentos ni puntuación de abreviatura."""
    import unicodedata
    s = unicodedata.normalize("NFKD", tok or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[.\-,]+$", "", s).strip().upper()


def descomponer_calle(calle: str | None) -> dict:
    """Parte la calle en `{"tipo", "nombre"}`.

    "Av. Vergara"            → {"tipo": "AVENIDA", "nombre": "Vergara"}
    "RUTA NACIONAL 8"        → {"tipo": "RUTA",    "nombre": "8"}
    "Arturo Jauretche"       → {"tipo": "",        "nombre": "Arturo Jauretche"}
    "Avenida"                → {"tipo": "",        "nombre": "Avenida"}   (sin nombre → no se parte)
    """
    txt = " ".join((calle or "").split())
    if not txt:
        return {"tipo": "", "nombre": ""}

    # Algunas fuentes usan el mismo separador que el BCI ("AVENIDA - VERGARA").
    m = re.match(r"^([^-]{2,20})\s*-\s*(.+)$", txt)
    if m and _canon(m.group(1)) in TIPOS_VIA:
        return {"tipo": TIPOS_VIA[_canon(m.group(1))], "nombre": m.group(2).strip()}

    toks = txt.split()
    for n in range(min(_MAX_TOKENS_TIPO, len(toks) - 1), 0, -1):
        clave = " ".join(_canon(t) for t in toks[:n])
        if clave in TIPOS_VIA:
            return {"tipo": TIPOS_VIA[clave], "nombre": " ".join(toks[n:]).strip()}
    return {"tipo": "", "nombre": txt}
