#!/usr/bin/env python3
"""Genera la plantilla DXF con los bloques/capas del estándar de entrega del cliente.

Por qué una plantilla y no bloques definidos en código: los símbolos del cliente traen
geometría dibujada (la ficha del MDU es un marco con ARC + LINE + LWPOLYLINE + 3 TEXT,
el poste son dos CIRCLE, el FLY_TAP un HATCH). Redibujarlos a mano no daría el mismo
símbolo. Se importan del propio archivo del cliente y así el plano sale idéntico.

Entrada: un DXF convertido desde el DWG de referencia del cliente
         (`plano ejemplo de entrega.dwg`) con `dwg2dxf` de LibreDWG.

**OJO con la conversión**: `dwg2dxf` es fiable para geometría (verificado: 19.831 de
19.831 polilíneas del mapa base) pero **pierde INSERTs y descarta el flag de
invisibilidad de los atributos** (todos salen flags=0). Por eso la visibilidad NO se
lee del DXF: se aplica desde `_VISIBLES`, extraído del volcado JSON del DWG original
(`dwgread -O JSON`), que es la fuente de verdad.

Uso:
    python scripts/generar_plantilla_entrega.py <referencia.dxf> [salida.dxf]
"""

from __future__ import annotations

import sys
from pathlib import Path

import ezdxf
from ezdxf.addons.importer import Importer

# Bloques del estándar. Nombre tal cual lo tiene el cliente — no renombrar: el
# proyectista filtra y cuenta por nombre de bloque en su propio flujo.
BLOQUES = [
    "SDU",                          # unidad unifamiliar (Single Dwelling Unit)
    "MDU",                          # edificio multifamiliar (Multi Dwelling Unit)
    "LOGRADOURO",                   # rótulo de calle sobre el eje
    "HCSDU",                        # contador de home connects SDU
    "HCMDU",                        # contador de home connects MDU
    "POSTE_CONCRETO",
    "POSTE_DUPLO_T",
    "Poste_concreto_aterramento",
    "Poste_concreto_transf_aterr",
    "FLY_TAP",
]

# Atributos que SE DIBUJAN. Todos los demás del bloque son dato consultable y van con
# el bit de invisibilidad puesto. Sacado del DWG original, no del DXF convertido.
_VISIBLES = {
    "SDU": {"N_C1_3_TP_QT"},                                  # p.ej. "350R", "25R-2"
    "MDU": {"NUMERO", "HP_TOTAL", "QTD_ANDARES", "BLOCO"},    # la ficha con marco
    "LOGRADOURO": {"LOGRADOURO"},
    "HCSDU": {"QTD_SDU"},
    "HCMDU": {"QTD_MDU"},
    "POSTE_CONCRETO": set(),
    "POSTE_DUPLO_T": set(),
    "Poste_concreto_aterramento": set(),
    "Poste_concreto_transf_aterr": set(),
    "FLY_TAP": set(),
}

# Capas del estándar: nombre -> color ACI. Se usa índice ACI y no RGB porque es lo que
# respetan las tablas de estilos de impresión (CTB) con las que trabaja el proyectista.
CAPAS = {
    "QUADRA": (8, "Continuous"),           # manzana (viene del mapa base del cliente)
    "MEIOFIO": (254, "ACAD_ISO02W100"),    # cordón (idem)
    "LIMITE": (5, "PHANTOMX2"),            # límite de la célula relevada
    "LOGRADOURO": (40, "Continuous"),
    "SDU_RES": (6, "Continuous"),
    "SDU_COM": (4, "Continuous"),
    "SDU_ESP": (3, "Continuous"),
    "SDU_VAZ": (3, "Continuous"),
    "MDU_RES": (2, "Continuous"),
    "MDU_COM": (5, "Continuous"),
    "MDU_ESP": (1, "Continuous"),
    "CONT_SDU": (30, "Continuous"),
    "CONT_MDU": (200, "Continuous"),
    "POSTE": (7, "Continuous"),
    "DROP": (30, "Continuous"),
    "FLY_TAP": (7, "Continuous"),
    "DIV": (5, "Continuous"),
    "DIST": (7, "Continuous"),
    "DISTLIN": (7, "Continuous"),
    "SERVICO": (5, "Continuous"),
    "ANOTACAO": (1, "Continuous"),
}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    ref_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(
        "src/scrapitero/assets/plantilla_entrega_bl.dxf")

    ref = ezdxf.readfile(str(ref_path))

    # R2000: es la versión del propio ejemplo del cliente. No subir — un DXF más nuevo
    # se abre igual, pero el cliente valida contra su flujo y no hay nada que ganar.
    doc = ezdxf.new("R2000", setup=True)

    imp = Importer(ref, doc)
    imp.import_tables(["linetypes", "styles"])
    faltantes = [b for b in BLOQUES if b not in ref.blocks]
    if faltantes:
        print(f"AVISO: bloques ausentes en la referencia: {faltantes}")
    imp.import_blocks([b for b in BLOQUES if b in ref.blocks])
    imp.finalize()

    for nombre, (color, ltype) in CAPAS.items():
        if ltype not in doc.linetypes:
            ltype = "Continuous"
        doc.layers.add(nombre, color=color, linetype=ltype)

    # La invisibilidad se impone acá: el DXF de referencia la perdió en la conversión.
    for bloque, visibles in _VISIBLES.items():
        if bloque not in doc.blocks:
            continue
        for e in doc.blocks.get(bloque):
            if e.dxftype() != "ATTDEF":
                continue
            e.dxf.invisible = 0 if e.dxf.tag in visibles else 1
            # `flags` es el que lee AutoCAD (bit 0 = invisible); `invisible` solo no basta.
            e.dxf.flags = (e.dxf.flags & ~1) | (0 if e.dxf.tag in visibles else 1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(out_path)

    print(f"OK  {out_path}  ({out_path.stat().st_size / 1024:.0f} KB)")
    print(f"    bloques: {len([b for b in doc.blocks if not b.name.startswith('*')])}")
    print(f"    capas:   {len(doc.layers)}")
    for b in BLOQUES:
        if b not in doc.blocks:
            continue
        att = [(e.dxf.tag, "vis" if not (e.dxf.flags & 1) else "inv")
               for e in doc.blocks.get(b) if e.dxftype() == "ATTDEF"]
        if att:
            print(f"    {b:<28} {att}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
