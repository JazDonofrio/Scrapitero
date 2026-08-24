"""Las DOS convenciones de pisos que convive el proyecto, y cómo pasar de una a la otra.

Hay dos formas de contar y se mezclaban sin nombre, que es lo que hacía difícil saber qué
significaba un `2` en pantalla:

  · **NIVELES** — cuántas plantas construidas tiene el edificio. Planta baja sola = **1**.
    Es lo que guardan `parcela_altura.pisos_satelital`, `parcela_altura.pisos_bci_proxy` y
    `parcelas.pisos_estimados_max`, y es la convención correcta para ESE uso porque:
      1. el proxy del catastro es `ceil(construida / (FOS·terreno))`, que no puede dar 0
         si hay algo construido — un 0 significaría que no hay edificio;
      2. el número **multiplica** en dos cálculos: el proxy geométrico de UF de
         `unidades_estimator` (`área_footprint × pisos ÷ tamaño_típico`) y la incidencia de
         volumen de `incidencias_reporter` (`ground_area_m2 × pisos ÷ uf_vivienda`). Con
         planta baja = 0 las dos se multiplican por cero.

  · **PISOS SOBRE PLANTA BAJA** — cómo lo lee una persona: planta baja sola = **0**,
    planta baja + uno = **1**. Es la convención que se muestra y que se entrega.

Regla: la base guarda NIVELES; todo lo que se muestra o se exporta pasa por
`sobre_planta_baja()`. No convertir dos veces, y no guardar el resultado convertido.

⚠ Pendiente con el cliente (junto con CODLOG): en su DWG de ejemplo los 12 MDU tienen
`QTD_ANDARES` entre 2 y 9, nunca 0 ni 1, así que no está confirmado que su campo excluya
el térreo. Si contesta que lo incluye, revertir es dejar de llamar a esta función en
`dxf_entrega`.
"""

from __future__ import annotations

from typing import Optional


def sobre_planta_baja(niveles: Optional[int]) -> Optional[int]:
    """NIVELES → PISOS SOBRE PLANTA BAJA. 1 nivel (PB sola) → 0; 2 → 1; 3 → 2.

    `None` se propaga: sin dato no se inventa un 0, que se leería como "planta baja sola".
    """
    if niveles is None:
        return None
    return max(int(niveles) - 1, 0)


def texto(niveles: Optional[int], vacio: str = "") -> str:
    """Igual que `sobre_planta_baja` pero para escribir en un campo de texto (DXF, CSV).

    Devuelve `vacio` cuando no hay dato, para no confundir "no lo sabemos" con "planta baja".
    """
    n = sobre_planta_baja(niveles)
    return vacio if n is None else str(n)
