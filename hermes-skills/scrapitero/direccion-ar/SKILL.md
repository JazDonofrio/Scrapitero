---
name: direccion-ar
description: "Completa calle y altura de las parcelas argentinas SIN pagar geocoding: la calle sale del eje de OpenStreetMap y el número del callejero oficial de georef-ar. Usar en relevamientos de PBA donde ARBA/IDERA entrega la geometría pero no la dirección, en lugar de AddressResolver (que fuera de Brasil va directo a Google, que se paga)."
version: 1.1.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, direcciones, argentina, pba, gratis, osm, georef]
    category: scrapitero
---

# Dirección AR — calle y altura sin costo

**El problema que resuelve:** ARBA/IDERA entrega la geometría catastral pero **no la
dirección**. En Merlo eran 41.825 parcelas de 42.025 sin calle, y `AddressResolver` fuera de
Brasil va derecho a Google Maps: ~USD 210 para ese relevamiento.

## Cómo lo hace
- **La calle** — una sola consulta a Overpass trae todas las vías con nombre de la zona y a
  cada parcela se le asigna la más cercana (tope `max_dist_m`, 40 m por default).
- **El número** — `georef-ar` (oficial, gratis, sin token) publica el rango de alturas **de
  cada CUADRA y por vereda**, con su geometría (dataset bulk `cuadras.ndjson`, cacheado por
  partido). Se interpola contra la cuadra donde cae la parcela, no contra la calle entera: una
  calle de Merlo tiene 9 cuadras de mediana, así que el rango global daba cualquier número.
- **El nombre de la calle se aparea tolerando la grafía del callejero** — el oficial escribe la
  MISMA vía de otra forma y el match exacto perdía calles enteras: `alm brown` vs `almte brown`,
  `leandro alem` vs `leandro n alem`, `jose terry` vs `terry`. Se aparea por conjunto de tokens
  distintivos, y por contención cuando un lado trae el nombre de pila y el otro no, exigiendo
  candidato **único**. En Merlo destrabó **134 calles / 12.146 parcelas** que salían S/N
  teniendo su cuadra publicada.

## Cuándo usar
- Relevamientos de Provincia de Buenos Aires, después de `ARBACartoFetcher`/`ARBACadastralFetcher`.
- **En lugar de `AddressResolver`**, que en Argentina no tiene capa gratuita.

## Correr el agente
```bash
PYTHONPATH=src python -m scrapitero.rpc.direccion_ar <<< '{"region_id":"<region>","dry_run":true}'
```
`dry_run` mide sin escribir. Acepta `survey_id`, `max_dist_m`, `margen_km` y `solo_faltantes`.

## Qué esperar (medido en Merlo)
- **98,5% con calle**, **78,9% con número** sobre las 94.540 parcelas del partido entero
  (17-sep-2026). Antes del apareo tolerante era 63,5%.
- Lo que queda sin número son calles que el callejero oficial **no publica** (ni georef ni
  ARBA): ahí la única fuente es Google, que se paga, y va al panel de incidencias.
- Acierto de la calle contra las direcciones de Google: **78,6%**. El resto son mayormente
  **esquinas** — el lote da a dos calles y cada fuente elige una.

## Importante
- La calle queda sellada `direccion_source='osm_georef'`: es una inferencia por cercanía, no un
  dato del catastro, y tiene que poder distinguirse en el CSV y en la ficha de procedencia.
- El número va a `numero_estimado` con su método y confianza, **nunca** a `parcelas.numero` —
  el mismo criterio que `NumeroEstimator` en Brasil.
- **No pisa la dirección `manual`**: la corrección del operador manda siempre.
