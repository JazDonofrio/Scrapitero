---
name: direccion-ar
description: "Completa calle y altura de las parcelas argentinas SIN pagar geocoding: la calle sale del eje de OpenStreetMap y el número del callejero oficial de georef-ar. Usar en relevamientos de PBA donde ARBA/IDERA entrega la geometría pero no la dirección, en lugar de AddressResolver (que fuera de Brasil va directo a Google, que se paga)."
version: 1.0.0
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
- **El número** — `georef-ar` (`apis.datos.gob.ar`, oficial, gratis, sin token) publica por
  partido cada calle con su rango de alturas y por vereda. Se proyecta la parcela sobre el eje
  y se interpola contra ese rango, respetando la paridad.

## Cuándo usar
- Relevamientos de Provincia de Buenos Aires, después de `ARBACartoFetcher`/`ARBACadastralFetcher`.
- **En lugar de `AddressResolver`**, que en Argentina no tiene capa gratuita.

## Correr el agente
```bash
PYTHONPATH=src python -m scrapitero.rpc.direccion_ar <<< '{"region_id":"<region>","dry_run":true}'
```
`dry_run` mide sin escribir. Acepta `survey_id`, `max_dist_m`, `margen_km` y `solo_faltantes`.

## Qué esperar (medido en Merlo, 41.225 parcelas)
- **98,3% con calle**, **84,4% con número**.
- Acierto de la calle contra las direcciones de Google: **78,6%**. El resto son mayormente
  **esquinas** — el lote da a dos calles y cada fuente elige una.

## Importante
- La calle queda sellada `direccion_source='osm_georef'`: es una inferencia por cercanía, no un
  dato del catastro, y tiene que poder distinguirse en el CSV y en la ficha de procedencia.
- El número va a `numero_estimado` con su método y confianza, **nunca** a `parcelas.numero` —
  el mismo criterio que `NumeroEstimator` en Brasil.
- **No pisa la dirección `manual`**: la corrección del operador manda siempre.
