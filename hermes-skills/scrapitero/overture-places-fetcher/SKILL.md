---
name: overture-places-fetcher
description: "Baja comercios y equipamiento de Overture Maps Places (GRATIS, S3 público + DuckDB, sin token ni costo por request) y los vincula a su parcela para aportar el conteo real de uf_comercio (uf_fuente='overture') + la etiqueta del cliente (ESCUELA/HOSPITAL/SHOPPING/SUPERMERCADO) vía parcela-categoria. Es la fuente de comercios FUERA DE BRASIL, donde no hay padrón fiscal descargable. Reemplaza a google-places-fetcher, que se cobra por request."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, overture, comercios, poi, uf, comercio, gratis, argentina, enriquecimiento]
    category: scrapitero
---

# Overture Places Fetcher

Trae los **comercios y equipamientos reales** de una zona desde
[Overture Maps](https://docs.overturemaps.org/guides/places/), **gratis**.

## Por qué existe

Fuera de Brasil **no hay padrón fiscal descargable** (ARCA sólo permite consultar por CUIT
con clave fiscal), así que `receita-estab-fetcher` no tiene equivalente. La única fuente de
"qué hay en cada parcela" era **Google Places**, que se cobra **~USD 0,032 por request**: en
Hurlingham se gastaron ~USD 14 para responder únicamente *"¿hay comercio sí/no?"* por
parcela, sin guardar un solo nombre.

Overture publica sus POIs en **GeoParquet sobre S3 público**: se consulta con **DuckDB, sin
credenciales ni token y sin costo por request** — el mismo patrón que `footprint-fetcher`
usa para Open Buildings.

Licencias del dataset: **CDLA-Permissive-2.0** (Meta, Microsoft) y **Apache-2.0**
(Foursquare) ⇒ uso comercial permitido.

## Qué hace

1. Consulta Overture por el **bbox de la zona** (`zone_geojson`, o la subzona del survey) y
   **recorta al polígono exacto** con shapely.
2. Guarda cada POI en **`comercios`** (`source='overture'`) con nombre, categoría, dirección
   y punto.
3. Lo **vincula a su parcela** por `ST_Contains`, con **guarda de huella**: si el lote que
   contiene el punto **no tiene ninguna construcción** (footprints de `footprint-fetcher`,
   solape ≥ 25 m²), el POI se **reasigna al lote construido más cercano** dentro de
   `reasignar_max_m` (40 m) y, si no hay ninguno, queda **sin parcela**.
4. Escribe **`uf_comercio` = cantidad de comercios de la parcela** (`uf_fuente='overture'`) y
   marca el uso (`comercial`, o `mixto` si ya era residencial). Las parcelas que **dejaron**
   de tener comercios vuelven a `uf_comercio=0` y al uso que corresponde sin comercio.
5. Carga en **`establecimientos_poi`** los que mapean a la taxonomía del cliente (ESCOLA,
   HOSPITAL, SHOPPING, SUPERMERCADO, POSTO DE GASOLINA…) → después correr
   **`parcela-categoria`** para que sellen `descripcion_uso` y la parcela muestre su tipo
   específico en vez de "COMÉRCIO EM GERAL".

## Cuándo usar

- **Paso estándar fuera de Brasil** (Argentina), en lugar de `google-places-fetcher`.
- También en Brasil, como capa gratuita antes de decidir si se paga Google.
- Correr **después** de que las parcelas tengan geometría, y **`parcela-categoria` después**.

## Comando

```bash
python3 -m scrapitero.rpc.overture_places_fetcher <<< '{"region_id":"zona-hurlingham","survey_id":"<SURVEY_ID>"}'
```

Luego, para que la etiqueta llegue a la parcela:

```bash
python3 -m scrapitero.rpc.parcela_categoria <<< '{"region_id":"zona-hurlingham"}'
```

## Parámetros opcionales

- `release` (default `2026-07-22.0`): versión del dataset. El bucket **no permite listar
  releases**, así que si Overture publica uno nuevo hay que pasarlo a mano.
- `min_confidence` (default 0.0): piso de `confidence` (0-1).
- `set_uso` (default true): marcar `uso_principal` comercial/mixto.
- `aportar_uf` (default true): escribir `uf_comercio`.
- `exigir_huella` (default true): guarda de huella. **Requiere haber corrido
  `footprint-fetcher` antes**; si el relevamiento no tiene footprints la guarda se saltea
  sola con un WARNING, porque "no hay edificio" y "no se bajaron los edificios" no son lo
  mismo.
- `reasignar_max_m` (default 40): radio para buscarle al POI un lote construido vecino.

## Output esperado

```json
{
  "ok": true,
  "pois_encontrados": 137,
  "comercios_guardados": 137,
  "vinculados_a_parcela": 54,
  "pois_taxonomia": 78,
  "parcelas_con_comercio": 28,
  "total_uf_comercio": 53,
  "pois_reasignados_por_huella": 6,
  "pois_sin_edificio": 0,
  "parcelas_uf_limpiada": 5,
  "release": "2026-07-22.0"
}
```

## Notas

- **El punto de Overture viene corrido**, así que sin la guarda de huella el `ST_Contains`
  mete el comercio en el terreno vacío de al lado. Caso real (Malvinas, ago-2026): un
  «Burger King» cuya propia ficha dice *BK Terrazas de Mayo Shopping* quedó adentro de una
  **plaza de 8.022 m² sin un solo edificio**, a 27 m del lote del shopping, y le aportó una
  UF de comercio que salió al CSV del cliente. Con la guarda: 6 POIs reasignados al lote
  construido vecino, 5 parcelas vacías devueltas a `uf_comercio=0`.

- **Gratis y sin token.** No hay tope de costo que administrar ni avisos por Telegram.
- Lee **`basic_category`**, no `categories`: esta última está **deprecada y se elimina en el
  release de septiembre 2026**. Si el release no la tuviera, cae a `categories.primary`.
- ⚠ **`operating_status` viene NULL** (verificado en toda la zona de Hurlingham): NO sirve
  como señal de abierto/cerrado, y por eso no se filtra por él.
- Respeta la **precedencia de fuentes**: no pisa `uf_fuente` `manual`/`google`/`cadastur`/
  `shopping_min`. Sí actualiza su propio sello `overture` (una fuente no se protege de sí
  misma, o no podría refrescar su conteo con un release nuevo).
- Sólo mapea a la taxonomía del cliente las categorías con equivalente **claro**; el resto
  queda como comercio genérico, sin inventar etiqueta. Público vs. particular
  (HOSPITAL/ESCOLA) se deduce del nombre, porque la fuente no lo declara.
- Medido en Hurlingham: 299 POIs en el bbox de las parcelas, **100% con dirección** de calle
  y número.
