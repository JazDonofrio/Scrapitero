---
name: osm-poi-fetcher
description: "Trae de OpenStreetMap los POIs que Overture NO publica en Argentina, empezando por las ESTACIONES DE SERVICIO (amenity=fuel). Gratis. Los escribe como comercios (source='osm') + establecimientos_poi (POSTO DE GASOLINA) y los vincula a su parcela con la guarda de huella; parcela-categoria les pone el piso de UF=1. Completa a overture-places-fetcher, no lo reemplaza."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, osm, overpass, poi, estaciones, combustible, comercios, gratis, argentina]
    category: scrapitero
---

# OSM POI Fetcher

Rellena los rubros que **Overture tiene vacíos en Argentina**, leyéndolos de
**OpenStreetMap** vía Overpass. Gratis, sin token.

## Por qué existe

Overture es la fuente de comercios fuera de Brasil, pero **no trae todo**. Medido en
Malvinas Argentinas (12-ago-2026), una zona cruzada por la **Ruta 8 y la Ruta 202**:

- Overture: **0 estaciones de servicio** en 218 POIs. Ni un rubro `gas_station`, ni un
  YPF / Shell / Axion / Puma por nombre.
- OSM: **3 en el bbox**, 2 dentro del polígono de la zona.

Una de esas dos es la que el operador ve desde la vereda, al lado del McDonald's de
Illia — y el relevamiento no la tenía. **Un cero así no es un dato, es un agujero**: la
misma regla que aplica `_try_mirrors` cuando distingue "no hay resultados" de "Overpass
está caído".

## Qué hace

1. Consulta Overpass por el **bbox de la zona** (nodos **y ways** — el playón de una
   estación suele ser un `way`; `out center` devuelve su centroide) y **recorta al polígono
   exacto**: de las 3 estaciones del bbox de Malvinas, una cae afuera.
2. Guarda cada POI en **`comercios`** con `source='osm'` y `place_id='osm:way/123'` (upsert
   idempotente por `(region_id, place_id)`), con el **rubro nombrado igual que en Overture**
   (`gas_station`) para que cualquier filtro por rubro siga valiendo sin saber de la fuente.
3. Lo **vincula a su parcela** reusando la **guarda de huella** de
   `overture-places-fetcher` (importada, no copiada): si el lote no tiene ninguna
   construcción, el POI se reasigna al lote construido más cercano.
4. Carga **`establecimientos_poi`** con `fuente='osm_poi'` y la etiqueta del cliente
   (`POSTO DE GASOLINA` → "ESTACIÓN DE SERVICIO" en la web en español). ⚠ La fuente es
   `osm_poi` y **no** `osm` a propósito: `shopping-fetcher` borra sus POIs por fuente y se
   los llevaría puestos.

Después hay que correr **`parcela-categoria`**, que sella `descripcion_uso` sobre la parcela
y le pone el **piso de UF=1** (`uf_fuente='shopping_min'`) si el lote no tiene ningún
conteo, más el uso que corresponda.

## Cuándo usar

- **Argentina, después de `overture-places-fetcher` y antes de `parcela-categoria`.**
- Requiere `footprint-fetcher` corrido antes para que la guarda de huella tenga evidencia
  (sin footprints se saltea sola, con WARNING).

## Comando

```bash
cd /opt/scrapitero && source .venv/bin/activate && \
  set -a && . ./.env && set +a && export DB_HOST=localhost && \
  PYTHONPATH=src python -m scrapitero.rpc.osm_poi_fetcher <<'JSON'
{"region_id": "zona-malvinas-argentinas",
 "survey_id": "217c2319-a966-46ec-8c68-74e280266118"}
JSON
```

## Parámetros opcionales

- `tags` (default `["amenity=fuel"]`): qué pedirle a OSM. **Sólo se aceptan tags con
  mapeo a la taxonomía del cliente** (`_TAGS` en el agente); uno desconocido devuelve
  `ok:false` en vez de guardar un POI sin etiqueta.
- `exigir_huella` (default true) y `reasignar_max_m` (default 40): la guarda de huella.
- `timeout_s` (default 60).

## Output esperado

```json
{
  "ok": true,
  "pois_encontrados": 2,
  "comercios_guardados": 2,
  "vinculados_a_parcela": 2,
  "pois_reasignados_por_huella": 0,
  "pois_sin_edificio": 0,
  "por_tag": {"gas_station": 2}
}
```

## Notas

- **Si Overpass no contesta confiable, devuelve `ok:false`** en vez de 0 POIs. En la corrida
  real de Malvinas hicieron falta 4 mirrors: 406, 504, un **200 vacío** (rechazado por la
  guarda) y recién el cuarto trajo los datos. Sin esa guarda, la zona habría quedado
  registrada como "sin estaciones de servicio" por segunda vez.
- Una estación **sin `name`** se guarda igual, nombrada por `brand`/`operator` o con el
  genérico: descartarla por no tener nombre sería perder justo el dato que falta.
- El piso de UF **no suma sobre un conteo existente**: la Puma de Malvinas cayó dentro del
  lote del shopping (13 ha, 31 comercios de Overture) y no aporta una UF más. Es una
  limitación conocida del piso, no un error de vínculo.
