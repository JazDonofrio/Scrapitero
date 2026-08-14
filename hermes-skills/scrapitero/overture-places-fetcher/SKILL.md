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
4. Aplica la **guarda de número**: si el POI declara en su ficha un número de puerta que es
   de **otra** parcela construida a ≤ `numero_max_m` (60 m), y la calle coincide, y el salto
   contra el número de su parcela es de `salto_min` (300) o más, lo **mueve ahí**. Los casos
   ambiguos **no se tocan**: los levanta `incidencias-reporter` como `poi_numero_ajeno`.
5. Escribe **`uf_comercio` = cantidad de comercios de la parcela**, contando **todas** las
   fuentes de POI (Overture **y** OSM), con `uf_fuente='poi'`, y marca el uso (`comercial`, o
   `mixto` si además hay vivienda). **El comercio se DESCUENTA del conteo del catastro, no se
   suma encima** — ver Notas. Las parcelas que **dejaron** de tener comercios vuelven a
   `uf_comercio=0`, la vivienda vuelve al crudo de `uf_catastro` y el uso al que corresponde.
6. Carga en **`establecimientos_poi`** los que mapean a la taxonomía del cliente (ESCOLA,
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
- `exigir_numero` (default true): guarda de número. **También requiere `footprint-fetcher`**
  (la parcela destino tiene que estar construida).
- `numero_max_m` (default 60): hasta dónde buscar la parcela que lleva el número declarado.
- `salto_min` (default 300): cuántos números tiene que saltar el declarado contra el de la
  parcela para que se mueva solo. Una cuadra argentina son 100 números, así que 300 es un
  salto que un punto corrido unos metros no puede explicar. **Bajarlo es peligroso**: entre
  7 y 57 están los casos de vecino inmediato, que son moneda al aire.

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
  "pois_reasignados_por_numero": 1,
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

- **La guarda de huella no alcanza cuando el lote equivocado también tiene edificios.** Ahí
  el único testigo es la dirección que el POI declara. *Questa Pizza* decía «Av. Pres.
  Arturo Umberto Illia **3770**» y estaba en la parcela «Illia **30**» —la del McDonald's—,
  a 7 m del lote 3770 de 131.620 m² que es el shopping. Eso lo arregla la guarda de número.

- ⚠ **La guarda de número es deliberadamente tímida, y hay que dejarla así.** Medido en
  Malvinas: de 137 POIs con dirección, **65** declaran un número distinto al de su parcela,
  **14** tienen una parcela cercana con ese número, y **sólo 1** se mueve. Lo que se deja a
  ojo humano:
  - **esquina** (calle declarada ≠ calle del catastro). Parece la señal más fuerte y es la
    más traicionera: **3 de 4** tenían una parcela de la calle declarada pegada (≤5 m). Es
    un lote de dos frentes, que el catastro rotula por una calle y el comercio publicita por
    la otra — moverlo rompería una asignación correcta. Mismo modo de falla que las
    etiquetas de calle del BCI.
  - **vecino** (misma calle, número contiguo). *Colegio Don Bosco* está en Artigas 171 y
    declara Artigas 161, con la 161 a 4 m: no hay forma de saber si el punto está corrido o
    si el número del catastro está mal.

- ⚠ **Corrige de qué lote cuelga el comercio, no la dirección del lote.** Illia 30 sigue
  saliendo al CSV con ese número, que en una calle numerada 3770-4651 es casi seguro un
  rótulo malo del catastro. Y **no cubre al POI sin número parseable**: *Starbucks* declara
  «Cruce Ruta 8 Y 202 San Miguel» y se queda donde está.

- ⚠ **El comercio se DESCUENTA del total de ARBA, no se suma encima.** ARBA da **cuántas**
  subparcelas tiene el lote pero **no el destino** de cada una, así que un comercio
  confirmado no es una unidad nueva: es una de esas mismas, mal rotulada como vivienda.
  `uf_vivienda = max(uf_catastro − comercios, 0)` y `UF total = max(uf_catastro, comercios)`.
  Sumando —como hacía esta ruta hasta ago-2026, al revés de lo que `UsoClassifier` documenta
  desde el principio— el **shopping Terrazas de Mayo** salía con «**1 vivienda** + 32
  comercios» y el lote del McDonald's con «1 vivienda + 2 comercios». Al aplicarlo en
  Malvinas: mixtas **77 → 11**, comerciales **2 → 68**, y el edificio real de José León
  Suárez 1800 conserva sus 82 UF repartidas (77 viv + 5 com) en vez de inflarse a 87.

- La resta **siempre se calcula desde `uf_catastro`** (mig. 056, el conteo crudo que escribe
  `arba-carto-fetcher` y no toca nadie más), nunca desde el `uf_vivienda` ya restado: si no,
  cada corrida restaría de nuevo. Verificado idempotente en 4 pasadas. En **Brasil**
  `uf_catastro` queda **NULL** —el BCI sí declara el destino de cada unidad— y ahí el
  comportamiento no cambia.

- ⚠ **Lo que el descuento NO arregla:** donde no hay ninguna señal de comercio, la unidad sin
  destino se sigue contando como vivienda. En Malvinas son **1.875 de 1.974 parcelas con
  exactamente "1 vivienda"** puesta por default. Es una decisión pendiente con el cliente,
  no un bug.

- **Correr también `osm-poi-fetcher`**: Overture sola dejó 32 negocios afuera en Malvinas.
  `_agregar_uf` cuenta las dos fuentes, y el dedupe entre ellas lo hace OSM antes de insertar.

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
