---
name: osm-poi-fetcher
description: "SEGUNDA fuente de comercios, gratis: trae de OpenStreetMap los negocios que Overture NO publica (supermercados, carnicerías, restaurantes, ferreterías, talleres, estaciones de servicio…). Los escribe como comercios (source='osm') + establecimientos_poi, los vincula a su parcela, deduplica contra las otras fuentes y aporta uf_comercio. Completa a overture-places-fetcher, no lo reemplaza: correr las dos."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, osm, overpass, poi, estaciones, combustible, comercios, gratis, argentina]
    category: scrapitero
---

# OSM POI Fetcher

Rellena los comercios que **Overture no ve en Argentina**, leyéndolos de **OpenStreetMap**
vía Overpass. Gratis, sin token.

## Por qué existe

Overture es la fuente de comercios fuera de Brasil, pero **no trae todo**, y lo que se pierde
sale al CSV del cliente como una parcela `residencial` con `uf_comercio=0`. Es lo primero que
nota un operador al comparar contra Google Maps.

Medido en Malvinas Argentinas (12-ago-2026), una zona cruzada por la Ruta 8 y la 202:

- **32 negocios que OSM tiene y Overture no**, sobre 58 POIs de OSM que caen dentro de una
  parcela relevada — Supermercado Luna, Autoservicio Nelly, 3 carnicerías, 4 restaurantes,
  Maderera Burger, 2 ferreterías, Consultorio Dental, Bicicletería Ruben, 3 talleres…
- **0 estaciones de servicio** de Overture en 218 POIs, en una zona con 3 en el bbox según
  OSM. Ni un rubro `gas_station`, ni un YPF / Shell / Axion / Puma por nombre. Ese fue el
  agujero que originó el agente, antes de que se le sumara el resto del comercio.

Aplicado: **26 parcelas cambiaron de uso** (22 de `residencial` a `comercial`).

**Un cero así no es un dato, es un agujero**: la misma regla que aplica `_try_mirrors` cuando
distingue "no hay resultados" de "Overpass está caído".

## Qué hace

1. Consulta Overpass por el **bbox de la zona** (nodos **y ways** — el playón de una
   estación suele ser un `way`; `out center` devuelve su centroide) y **recorta al polígono
   exacto**: de las 3 estaciones del bbox de Malvinas, una cae afuera.
2. Guarda cada POI en **`comercios`** con `source='osm'` y `place_id='osm:way/123'` (upsert
   idempotente por `(region_id, place_id)`), con el **rubro nombrado igual que en Overture**
   (`gas_station`) para que cualquier filtro por rubro siga valiendo sin saber de la fuente.
3. Lo **vincula a su parcela** reusando las **guardas** de `overture-places-fetcher`
   (importadas, no copiadas): la **de huella** —si el lote no tiene ninguna construcción, el
   POI se reasigna al lote construido más cercano— y la **de número** —si el POI declara el
   número de otra parcela cercana y la calle coincide, se mueve ahí. En la práctica la de
   número casi no dispara acá: las estaciones de servicio de OSM rara vez traen dirección.
3b. **Descarta los que ya trajo otra fuente** (`_descartar_duplicados`), antes de insertar:
   si no, el mismo negocio cuenta dos veces en `uf_comercio`. Se deduplica **sólo por
   nombre**, nunca por cercanía sola — ver Notas.
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

- `tags` (default: **los ~88 de `_TAGS`**, o sea todo el comercio mapeado). **Sólo se aceptan
  tags con mapeo** (`_TAGS` en el agente); uno desconocido devuelve `ok:false` en vez de
  guardar un POI sin clasificar. Para volver al comportamiento viejo —sólo estaciones de
  servicio— pasar `tags=["amenity=fuel"]`.
- `dedupe` (default true): no guardar el POI si otra fuente ya trajo el mismo negocio.
  **Apagarlo duplica UF**; está para depurar, no para producción.
- `aportar_uf` (default true) y `set_uso` (default true): contar `uf_comercio` y marcar el
  uso al terminar, con la misma `_agregar_uf` que usa Overture.
- `exigir_huella` (default true) y `reasignar_max_m` (default 40): la guarda de huella.
- `exigir_numero` (default true), `numero_max_m` (default 60) y `salto_min` (default 300):
  la guarda de número. Mismos valores que Overture a propósito — dos fetchers de POI con
  criterios distintos sería una trampa.
- `timeout_s` (default 60).

## Output esperado

```json
{
  "ok": true,
  "pois_encontrados": 58,
  "duplicados_descartados": 14,
  "comercios_guardados": 44,
  "vinculados_a_parcela": 44,
  "pois_taxonomia": 43,
  "pois_reasignados_por_huella": 0,
  "pois_sin_edificio": 0,
  "pois_reasignados_por_numero": 0,
  "parcelas_con_comercio": 96,
  "total_uf_comercio": 154,
  "por_tag": {"gas_station": 2, "restaurant": 5, "grocery_store": 6, "home_service": 5}
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

- ⚠ **El dedupe va SÓLO por nombre, nunca por cercanía sola.** Medido en Malvinas: hay **21
  pares de negocios DISTINTOS a menos de 20 m** —«Il Cappo» a 5 m de «Kiosko San Miguel»,
  «Ferretería Bulonera» a 10 m de «Pintureriapanacebo»—, que es lo normal en una tira de
  locales sobre una avenida. Deduplicar por proximidad habría tirado 21 comercios reales,
  justo lo contrario de para qué se sumó OSM.

- El radio del dedupe es **escalonado por lo distintivo del nombre**, y eso lo decidieron los
  datos. Los pares con nombre idéntico medidos:

  | Par | Distancia | ¿Mismo negocio? |
  |---|---|---|
  | Terrazas de Mayo Shopping | 175 m | **sí** — OSM apunta al centro del polígono, Overture a una tienda ancla |
  | Repair Car Shop | 131 m | sí |
  | Starbucks | 124 m | sí |
  | Elizabeth | 675 m | **no** — dos peluquerías distintas |

  De ahí: **600 m** con 2+ tokens significativos (mismo valor que
  `shopping_fetcher.merge_dist_fuerte_m`, porque un shopping ocupa una manzana) y **250 m**
  con un solo token, porque un nombre de pila no identifica un negocio.

- ⚠ **También matchea por CONTENCIÓN, no sólo por núcleo idéntico** (13-ago-2026). Pedir que
  los conjuntos de tokens distintivos sean *iguales* dejaba pasar el mismo negocio escrito con
  una palabra de más. Se arregló en dos capas, y **medir cambió el diseño dos veces**:

  1. Las palabras que no distinguen se sumaron a `_GENERICOS_NOMBRE`: `shopping`, `srl`,
     `sas`, `sac`, `scs`. Con eso *Terrazas de Mayo* ≡ *Terrazas de Mayo Shopping* y
     *Maderera Burger* ≡ *Maderera Burger SRL* caen por la vía de igualdad que ya existía.
     **`sa` NO se agregó**: se come el apellido brasilero «Sá» (los acentos ya se sacaron al
     normalizar). `S.A.` con puntos ya caía solo — la puntuación se vuelve espacios y quedan
     tokens de una letra, que `_tokens_sig` descarta por longitud.
  2. `_nombre_contenido` (señal **débil**) para el resto, con **dos condiciones extra**.

  Pedir sólo contención es peligroso, y esto es lo que pasó al medirlo: un POI de OSM llamado
  literalmente **«San Miguel»** —el partido vecino, no un negocio— se comía *Carrefour
  Hipermercado San Miguel*, *Kiosko San Miguel*, *Frávega san miguel 2*, *Diesel San Miguel*
  y 4 más. Por eso la contención exige además:

  | Condición | Por qué |
  |---|---|
  | **≤ 25 m** | Con el radio ancho, «Terrazas de Mayo» se comía a sus propios locales: *LOCAL 47* (130 m), *Destel* (109 m), el *Patio de comidas* (153 m). Son inquilinos, no el shopping. |
  | **match ÚNICO en todo el radio** | Si el nombre está contenido en VARIOS negocios distintos no identifica a ninguno. *La ambigüedad es la señal*, y evita necesitar un padrón de topónimos — que además no serviría: `localidad`/`municipio` vienen **vacíos** en PBA y el partido vecino tampoco sale del nombre de la región. |
  | **≥ 2 tokens del lado corto** | Que un nombre de una sola palabra no absorba a nadie (misma razón que el radio escalonado). |

  Resultado sobre los datos reales de Malvinas: **5 fusiones correctas, 0 falsas**, 0 hoteles
  afectados. Las 5: *Terrazas de Mayo Shopping* (157 m), *Maderera Burger SRL* (21 m),
  *Clínica Privada Neuropsiquiátrica Día San Miguel* (**0 m**), *Textil Obrero* (9 m),
  *Parrilla Sauce Ranch* (17 m).

- **Barre los POI de OSM que ya no corresponden**: los que desaparecieron del mapa y los que
  una corrida vieja guardó y el dedupe de hoy descarta. Sin eso un duplicado que se coló
  queda inflando `uf_comercio` para siempre.

- La consulta a Overpass **agrupa los tags por clave con regex**. Con un `node`+`way` por
  tag, los 88 del default son ~180 sentencias y Overpass devuelve **504** — medido, dos
  mirrors seguidos se cayeron con la forma larga. Agrupado son 10 sentencias y contesta en
  segundos.

- ⚠ **Overpass se cae seguido y el último recurso (Tor) estuvo roto** hasta el 13-ago-2026.
  `_try_mirrors` (en `osm_building_fetcher`, lo comparten TODOS los agentes de Overpass) le
  pasaba `proxies=` a `httpx.Client`, argumento que httpx 0.28 renombró a `proxy=`. Como el
  proxy sólo entra cuando **todos** los mirrors directos fallan, en vez de rescatar la corrida
  la reventaba con un `TypeError` justo cuando era la única salida — y no se notaba, porque el
  camino feliz nunca lo toca. Si hay que arreglar datos con Overpass caído: las filas
  `source='osm'` ya guardadas SON el resultado de la última bajada buena, así que se les puede
  volver a pasar `_descartar_duplicados` + `_agregar_uf` sin re-fetchear (es lo que se hizo el
  13-ago con los 4 mirrors abajo). Lo que **no** hay que hacer es reescribir el criterio a
  mano en un script suelto.
