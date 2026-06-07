---
name: google-places-fetcher
description: "Baja comercios de Google Places y los vincula a su parcela (ST_Contains) para aportar el conteo real de uf_comercio: cada comercio dentro de una parcela = +1 UF de comercio. Fuente autoritativa de uf_comercio (pisa el proxy de unidades-estimator, uf_fuente='google') y señal de uso (parcela con comercio → comercial/mixto, uso_fuente='google'). Genérico cualquier país. Busca por teselas adaptativas con tope de costo. Correr DESPUÉS de unidades-estimator."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, google, places, comercios, uf, comercio, uso, salta, argentina, enriquecimiento]
    category: scrapitero
---

# Google Places Fetcher

Aporta el dato de **comercios reales** de Google Maps al relevamiento. Cada comercio
cuyo punto cae dentro de una parcela suma **+1 a `uf_comercio`** (sin agrupar: un shopping
de 20 locales = 20 UF de comercio). Es la **fuente autoritativa** de `uf_comercio` —
reemplaza el proxy geométrico de `unidades-estimator` (`uf_fuente='google'`).

Genérico (cualquier país). Idioma de respuesta automático por `region_id`
(`-br`→pt-BR, `-ar`→es-AR, otro→es), igual que `address-resolver`.

## Qué hace

1. **Tesselado adaptativo (quadtree)** del polígono de la zona (`zone_geojson`/`bbox_wkt`):
   celdas de `cell_size_m`; las que caen fuera del polígono se descartan (no gasta request).
   Si una celda devuelve el máximo (20 = posible truncado) se **subdivide en 4** y recurre
   hasta `min_cell_m`. Zonas densas se afinan, zonas ralas no se sobrepagan.
2. **`places:searchNearby`** (Places API New, field mask acotado → SKU Pro). Filtra a tipos
   comerciales (store, restaurant, bank, pharmacy, gym, lodging…), excluyendo equipamiento
   que no es comercio (escuelas, hospitales, culto, transporte, gobierno, parques).
3. **Upsert** en tabla `comercios` (dedup por `(region_id, place_id)`).
4. **Vinculación espacial** a parcela (`ST_Contains`, igual que `osm-building-fetcher`).
   Comercios sobre la calle/vereda (fuera de toda parcela) quedan sin vincular, reportados.
5. **Agregación por parcela**: `uf_comercio = #comercios`, `uf_fuente='google'`, recalcula
   `unidades_funcionales_estimadas = uf_vivienda + uf_comercio`. Si `set_uso=true`, sube
   `uso_principal` a **comercial** (o **mixto** si ya era residencial), `uso_fuente='google'`.
   Los comercios `CLOSED_PERMANENTLY` no se cuentan.

## Costo (Google Places es caro)

`searchNearby` ≈ **USD 0,032/llamada** (SKU Pro) vs USD 0,005 del geocoding. El gasto
escala con el **área** (no con la cantidad de parcelas). Controles:

- `max_requests` corta la corrida (resultado parcial, `cap_alcanzado=true`).
- Avisos por **Telegram**: costo estimado al inicio, parcial al cortar, y final.
- El output trae `requests_usados` y `costo_estimado_usd`.

> Estimación rápida: `costo ≈ max_requests × 0,032`. Default 400 req ≈ USD 12,8 tope.

## Orden en el pipeline

Correr **después** de `unidades-estimator` (que ya puso `uf_vivienda`): este agente solo
sobrescribe `uf_comercio` con el conteo real. Requiere parcelas con geometría
(`salta-catastro-fetcher`).

## Requisitos

- `GOOGLE_MAPS_API_KEY` con **Places API (New)** habilitada.
- Parcelas con `geometry` cargadas en la región (para `ST_Contains`).

## Comando

```bash
python3 -m scrapitero.rpc.google_places_fetcher <<< '{"region_id":"{region_id}","survey_id":"{survey_id}"}'
```

Acotar gasto / afinar densidad:
```bash
python3 -m scrapitero.rpc.google_places_fetcher <<< '{"region_id":"...","cell_size_m":120,"min_cell_m":40,"max_requests":200}'
```

Re-bajar todo (ignora comercios ya cargados):
```bash
python3 -m scrapitero.rpc.google_places_fetcher <<< '{"region_id":"...","overwrite":true}'
```

Sin tocar el uso (solo conteo de uf_comercio):
```bash
python3 -m scrapitero.rpc.google_places_fetcher <<< '{"region_id":"...","set_uso":false}'
```

## Parámetros

| Campo | Tipo | Default | Descripción |
|-------|------|---------|-------------|
| `region_id` | str | — | Obligatorio |
| `survey_id` | str | auto | Si se omite usa el último survey de la región |
| `cell_size_m` | float | 150.0 | Lado de la celda inicial de búsqueda |
| `min_cell_m` | float | 40.0 | No subdividir por debajo de esto (zonas densas) |
| `max_requests` | int | 400 | Tope de llamadas (corta resultado parcial) |
| `included_types` | list | comercial | Override de tipos de Google (Table A) |
| `set_uso` | bool | true | Sube `uso_principal` a comercial/mixto en parcelas con comercio |
| `overwrite` | bool | false | Re-tesselar aunque ya haya comercios cargados |
| `bbox_south/west/north/east` | float | — | Override de bbox (zonas con features dispersos) |

## Output esperado

```json
{
  "ok": true,
  "requests_usados": 138,
  "requests_truncados": 6,
  "comercios_encontrados": 412,
  "comercios_vinculados": 357,
  "comercios_sin_parcela": 55,
  "parcelas_con_comercio": 210,
  "total_uf_comercio": 357,
  "parcelas_uso_actualizado": 198,
  "costo_estimado_usd": 4.42,
  "cap_alcanzado": false,
  "error": null
}
```

## Limitaciones

- Cuenta cada `place_id` como 1 UF: no agrupa cadenas ni distingue locales que comparten
  unidad física. Es exactamente la regla pedida (1 comercio = 1 UF de comercio).
- `searchNearby` (New) tope a 20 resultados/celda; el quadtree lo resuelve subdividiendo,
  pero un microcentro muy denso con `min_cell_m` grande puede subcontar → bajar `min_cell_m`.
- Comercios fuera de toda parcela (mal geolocalizados o sobre la calle) no se cuentan.
- Cobertura y rubros dependen de la calidad de Google Maps en la zona.
