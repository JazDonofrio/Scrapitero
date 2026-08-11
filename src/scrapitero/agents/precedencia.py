"""Precedencia de fuentes al escribir `parcelas` (ver la regla en CLAUDE.md).

Varias etapas escriben las MISMAS columnas y corren más de una vez, así que sin
una guarda explícita gana la última corrida aunque su dato sea peor. Esta
constante vive en un solo lugar a propósito: espejarla en cada agente es el modo
de falla clásico —una copia se actualiza y la otra no, en silencio—.

Uso (va interpolada en el SQL; es un literal fijo, no entra input de usuario):

    f"... WHERE COALESCE(uf_fuente,'') NOT IN {UF_FUENTES_PROTEGIDAS}"
"""

from __future__ import annotations

# Sellos de `uf_fuente` que NO se pisan al reescribir uf_vivienda/uf_comercio:
#   manual        → corrección del operador (panel de incidencias), irreconstruible
#   google        → conteo real de comercios de GooglePlacesFetcher
#   overture      → conteo real de comercios de OverturePlacesFetcher (mismo rango que
#                   google, pero gratis; es la fuente de comercios fuera de Brasil)
#   cadastur      → habitaciones de hotel (fuente oficial)
#   shopping_min  → piso de UF=1 que pone ParcelaCategoria a un shopping
UF_FUENTES_PROTEGIDAS = "('manual','google','overture','cadastur','shopping_min')"

# Sello de `direccion_source` que nunca se pisa: la dirección corregida a mano.
DIRECCION_FUENTE_PROTEGIDA = "manual"

# Sellos de `uso_fuente` que NO pisa un fetcher de catastro al deducir el uso desde sus UF.
# El catastro sabe CUÁNTAS unidades hay, pero no si alguna es comercio: eso lo saben las
# fuentes de negocios. Un 'residencial' inferido de las UF nunca debe degradar el
# 'mixto'/'comercial' que puso quien sí vio los comercios.
#   manual       → corrección del operador
#   overture     → OverturePlacesFetcher (comercios reales; la fuente fuera de Brasil)
#   google       → GooglePlacesFetcher
#   clasificador → UsoClassifier (catastro + Places)
#   bci          → el BCI declara el uso de cada unidad (Brasil)
#   cadastur     → hotel oficial
USO_FUENTES_PROTEGIDAS = "('manual','overture','google','clasificador','bci','cadastur')"
