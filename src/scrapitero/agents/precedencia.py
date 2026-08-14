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
#   overture      → conteo de OverturePlacesFetcher. Sello HISTÓRICO: se sigue respetando
#                   por las filas ya escritas, pero hoy el agente sella 'poi'
#   poi           → conteo real de comercios de TODAS las fuentes de POI juntas (Overture +
#                   OSM). Existe desde ago-2026, cuando `_agregar_uf` pasó a contar las dos:
#                   seguir sellando 'overture' declaraba mal el origen en el CSV del cliente,
#                   el mismo modo de falla que las parcelas argentinas selladas 'cadastur'
#   cadastur      → habitaciones de hotel con conteo EXACTO (padrón oficial / OSM `rooms` /
#                   carga manual). Sólo cuando el número es real: ver `hotel_min`
#   hotel_min     → piso de UF por hotel CONFIRMADO pero SIN conteo de habitaciones. Es el
#                   `GREATEST(COALESCE(habitaciones,1),1)` del fetcher: alguien vio el hotel,
#                   nadie contó los cuartos. Existe desde ago-2026, cuando se midió que en
#                   Argentina **ningún** hotel tiene habitaciones —no hay Cadastur ni padrón
#                   hotelero descargable— y sin embargo 4 parcelas de Malvinas salían selladas
#                   'cadastur', declarando un padrón brasilero que el pipeline no consultó ni
#                   podía consultar. El número (1) estaba bien; la etiqueta mentía, y encima
#                   le daba a un piso la protección de un conteo oficial, con lo que ninguna
#                   fuente mejor podía corregirlo. Mismo modo de falla que 'overture' → 'poi'
#   shopping_min  → piso de UF=1 que pone ParcelaCategoria a un shopping
UF_FUENTES_PROTEGIDAS = ("('manual','google','overture','poi','cadastur','hotel_min',"
                         "'shopping_min')")

# Sellos que `_agregar_uf` SÍ puede reescribir aunque estén protegidos: los suyos. Sin esto
# el agente se auto-bloquea con el sello que él mismo dejó y no puede refrescar su conteo en
# la corrida siguiente. Incluye 'overture' para poder migrar las filas viejas a 'poi'.
UF_FUENTES_POI = "('overture','poi')"

# Sello de `direccion_source` que nunca se pisa: la dirección corregida a mano.
DIRECCION_FUENTE_PROTEGIDA = "manual"

# Sellos de `uso_fuente` que NO pisa un fetcher de catastro al deducir el uso desde sus UF.
# El catastro sabe CUÁNTAS unidades hay, pero no si alguna es comercio: eso lo saben las
# fuentes de negocios. Un 'residencial' inferido de las UF nunca debe degradar el
# 'mixto'/'comercial' que puso quien sí vio los comercios.
#   manual       → corrección del operador
#   overture     → OverturePlacesFetcher (sello histórico, ver arriba)
#   poi          → las fuentes de POI juntas (Overture + OSM)
#   google       → GooglePlacesFetcher
#   clasificador → UsoClassifier (catastro + Places)
#   bci          → el BCI declara el uso de cada unidad (Brasil)
#   cadastur     → hotel con habitaciones exactas
#   hotel_min    → hotel confirmado sin conteo de habitaciones (ver arriba). El USO igual es
#                  firme: que no sepamos cuántos cuartos tiene no cambia que ahí hay un hotel
USO_FUENTES_PROTEGIDAS = ("('manual','overture','poi','google','clasificador',"
                          "'bci','cadastur','hotel_min')")
