"""Derivación de la MANZANA CATASTRAL a partir del código/nomenclatura de cada fuente.

El sistema no tiene una entidad "manzana"; la manzana catastral se deriva del código
catastral, cuyo formato depende de la fuente (`parcelas.fuente_parcela`). Este módulo
centraliza un registry de parsers, uno por fuente, para que el agente dasimétrico sea
**genérico**: corre en cualquier zona cuya fuente tenga parser.

El código devuelto se **namespacea** con el contexto disponible (circunscripción/sección,
o setor) para no fusionar manzanas distintas que comparten número entre secciones.
Devuelve `None` cuando no se puede derivar (fuente sin parser o dato insuficiente);
el agente lo reporta como parcelas "sin manzana".

Para soportar una fuente nueva: agregar su parser y registrarlo en `_PARSERS`.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

# (cca_code, nomenclatura_catastral) → código de manzana namespaceado, o None


def _arba(cca: Optional[str], nom: Optional[str]) -> Optional[str]:
    """ARBA (Provincia de Buenos Aires): carto e IDERA.

    Nomenclatura: 'Partido: 136 (...) Circunscripción: 2 Sección: C Manzana: 162 Parcela: 21'
    Código fijo:  '136 02 0C ...ceros... {manzana:4}{parcela:7}{subparcela:3}'
                  ej. 136020C0000000000000000000000162 0000021 000
    """
    if nom:
        circ = re.search(r"Circunscripci[oó]n:\s*([0-9A-Za-z]+)", nom)
        secc = re.search(r"Secci[oó]n:\s*([0-9A-Za-z]+)", nom)
        manz = re.search(r"Manzana:\s*([0-9A-Za-z]+)", nom)
        if manz:
            parts = [p.group(1) for p in (circ, secc, manz) if p]
            return "-".join(parts)
    if cca and len(cca) >= 14 and cca[:3].isdigit():
        manzana = cca[-14:-10].lstrip("0") or "0"
        circ = cca[3:5].lstrip("0") or "0"
        secc = cca[5:7].lstrip("0") or cca[5:7]
        return f"{circ}-{secc}-{manzana}"
    return None


def _salta(cca: Optional[str], nom: Optional[str]) -> Optional[str]:
    """Salta (IDEMSA capital). Nomenclatura fija de 14: '{seccion:5}{manzana:[A-Z]ddd}{parcela:5}'.

    Ej.: '01110M04600170' → sección '01110', manzana 'M046', parcela '00170'.
         '01110D00400090' → sección '01110', manzana 'D004', parcela '00090'.
    La manzana es el token letra+3 dígitos; la namespaceamos con la sección que la precede.
    """
    if nom:
        m = re.match(r"(\d+)([A-Z]\d{3})", nom.strip())
        if m:
            return f"{m.group(1)}-{m.group(2)}"
    return None


def _smartgis_vg(cca: Optional[str], nom: Optional[str]) -> Optional[str]:
    """Várzea Grande (SmartGIS). Nomenclatura 'setor-quadra-lote' ej. '103-318-103'.

    La manzana es la QUADRA (componente del medio); la namespaceamos con el setor:
    '103-318'.
    """
    if nom and "-" in nom:
        parts = nom.split("-")
        if len(parts) >= 2 and parts[1].strip():
            setor = parts[0].strip()
            quadra = parts[1].strip()
            return f"{setor}-{quadra}"
    return None


# Registry: fuente_parcela → parser
_PARSERS: dict[str, Callable[[Optional[str], Optional[str]], Optional[str]]] = {
    "arba_carto": _arba,
    "arba_idera": _arba,
    "salta_idemsa": _salta,
    "smartgis_vg": _smartgis_vg,
}


def fuentes_soportadas() -> set[str]:
    """Fuentes con parser de manzana disponible."""
    return set(_PARSERS)


def manzana_codigo(
    fuente: Optional[str], cca: Optional[str], nom: Optional[str]
) -> Optional[str]:
    """Deriva el código de manzana catastral para una parcela. None si no se puede."""
    if not fuente:
        return None
    parser = _PARSERS.get(fuente)
    if parser is None:
        return None
    try:
        return parser(cca, nom)
    except Exception:  # noqa: BLE001 — un código malformado no debe romper el batch
        return None
