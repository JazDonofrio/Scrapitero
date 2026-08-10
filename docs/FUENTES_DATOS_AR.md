# Fuentes de datos para relevamientos en Argentina

Qué fuente aporta comercios, shoppings, hoteles y edificios en Argentina, qué tan
confiable es cada una y qué falta construir. Todo lo marcado **verificado** se midió
sobre la zona real de **Hurlingham** (bbox `-34.5968,-58.6418,-34.5886,-58.6312`,
~0,7 km², 441 parcelas) el **2026-08-10**.

> **El hallazgo de fondo:** la arquitectura que funciona en Brasil —un padrón fiscal
> nacional descargable (Receita CNPJ) que se aterriza sobre las parcelas— **no se puede
> replicar en Argentina**: ARCA/AFIP no publica un dump masivo (ver más abajo). El lugar
> que ocupa la Receita lo tiene que ocupar **Overture Places**, que es de otra naturaleza
> (POIs con dirección, no universo fiscal) pero cubre el mismo uso: nombre + categoría +
> dirección de cada comercio, gratis.

---

## Resumen: qué usar para cada cosa

| Necesidad | Fuente recomendada | Costo | Estado |
|---|---|---|---|
| **Edificios (footprints)** | Google Open Buildings (mirror VIDA) | gratis | ✅ **Ya implementado** (`FootprintFetcher`), falta sumarlo al flujo PBA |
| **Altura / pisos** | Google Solar API | 10.000 req/mes gratis | ⚠ Funciona, pero hay que ajustar el criterio de discrepancia (ver abajo) |
| **Comercios** | **Overture Places** | gratis | ❌ **A construir** — es lo de mayor valor por esfuerzo |
| **Geocoding (dirección→coord)** | **`georef-ar` (Datos Argentina)** → Google el resto | gratis + USD 5/1.000 | ❌ A enchufar. **Mapbox NO** (ver §4 bis) |
| **Dirección de la parcela** | hoy Google reverse (pago) | USD 5/1.000 | ⚠ ARBA **no** devolvió domicilio registral en Hurlingham |
| **Shoppings** | Overture Places + OSM | gratis | Parcial: `ShoppingFetcher` ya existe (OSM+Google), falta Overture |
| **Hoteles** | Overture/OSM + registro provincial | gratis | ❌ Sin equivalente a Cadastur; ver limitaciones |
| **Universo fiscal (tipo Receita)** | — | — | ❌ **No existe abierto en Argentina** |

---

## 1. Edificios — resuelto, y con mucho margen

**Google Open Buildings**, vía el mirror de VIDA en FlatGeobuf que ya usa
`FootprintFetcher`. El país se autodetecta del centroide, así que **funcionó en Argentina
sin tocar una línea de código**.

**Verificado en Hurlingham:**

| | OSM (`OSMBuildingFetcher`, paso actual del flujo) | Open Buildings (`FootprintFetcher`) |
|---|---|---|
| Footprints | **4** | **2.872** |
| Parcelas cubiertas | 2 | **416 de 441 (94%)** |
| Tiempo | ~45 s | ~5 s |
| Costo | gratis | gratis |

Área media 99,2 m², `confidence` media 0,78. **OSM es inservible para edificios en el
conurbano** (4 edificios mapeados en 0,7 km²): el flujo PBA hoy corre `OSMBuildingFetcher`
y se queda con eso.

> **Acción:** sumar `FootprintFetcher` al flujo PBA. Es gratis, tarda segundos y multiplica
> por ~700 la cobertura. Ojo que hoy escribe en `footprints_revision` (capa de revisión, no
> alimenta `UnidadesEstimator`) — para que aporte a la estimación de UF habría que decidir
> si también puebla `edificios`.

### Altura (Google Solar) — sirve para medir, no para detectar discrepancias

**Verificado:** 8/8 parcelas consultadas devolvieron altura, con valores plausibles
(3,0 a 10,5 m → 1 a 3 pisos). Ejemplo coherente: Pablo Pizzurno 1292, 12 UF declaradas,
3 pisos medidos.

Dos límites serios en Argentina:

1. **`imagery_year = 2013` en el 100% de las mediciones.** Trece años de desactualización
   (en Várzea Grande era 2014). El dato **no describe el estado actual**.
2. **La detección de `sin_declarar` no aplica en PBA.** El criterio compara contra el área
   construida del catastro, y **ARBA no publica área construida** (`area_m2_construida` es
   NULL en las 441 parcelas) ⇒ `pisos_bci_proxy = NULL` y **toda parcela con edificio se
   marca como "construcción no declarada"**: 6 de 8 en la prueba. Correrlo así llenaría el
   panel de incidencias de falsos positivos.

> **Acción:** antes de usar la capa 📏 en Argentina, hacer que `AlturaFetcher` sólo mida
> (pisos/altura) y **no** emita `sin_declarar` cuando la fuente del catastro no publica área
> construida. Es el mismo modo de falla que el `LOTE VAZIO` corregido en `_tipo_edificacion`:
> lógica que asume el BCI brasilero.

---

## 2. Comercios — Overture Places es la fuente

[Overture Maps](https://docs.overturemaps.org/guides/places/) publica un dataset global de
POIs en GeoParquet sobre S3 público, **consultable con DuckDB sin credenciales ni token** —
exactamente el patrón que `FootprintFetcher` ya usa para Open Buildings.

**Verificado en Hurlingham** (release `2026-07-22.0`):

- **299 POIs** en 0,7 km².
- **100% con dirección** (`addresses[1].freeform`, con calle y número → apareable contra el
  catastro por dirección, no sólo por `ST_Contains`).
- 140 con sitio web; todos con `confidence`.
- Categorías directamente mapeables a la taxonomía del cliente: `gas_station`, `bakery`,
  `supermarket`, `hospital`, `restaurant`, `bar`, `pub`, `gym`, `clothing_store`,
  `shopping_center`, `beauty_salon`…
- Nombres reales verificables: *Hospital Municipal de Hurlingham*, *Carrefour Market*,
  *Paseo Florido*, *Shell*, *Teatro Brote*.

**Contra lo que tenemos hoy:** el `UsoClassifier` gastó **441 requests de Google Places
(~USD 14)** en Hurlingham para responder únicamente *"¿hay comercio sí/no?"* por parcela, sin
guardar un solo nombre (la tabla `comercios` quedó vacía). Overture da 299 comercios **con
nombre, categoría y dirección, gratis, en una consulta**.

```sql
-- consulta verificada (DuckDB + httpfs, sin credenciales)
SET s3_region='us-west-2'; SET s3_url_style='path';
SELECT names.primary, categories.primary, confidence, addresses[1].freeform
FROM read_parquet('s3://overturemaps-us-west-2/release/2026-07-22.0/theme=places/type=place/*')
WHERE bbox.xmin BETWEEN :lng0 AND :lng1 AND bbox.ymin BETWEEN :lat0 AND :lat1;
```

⚠ **Fecha de vencimiento del esquema:** la propiedad `categories` está **deprecada y se
elimina en el release de septiembre 2026**, reemplazada por `basic_category` + `taxonomy`.
Un agente nuevo debe leer las tres mientras convivan y migrar a `basic_category`.

**Alternativa:** [Foursquare OS Places](https://opensource.foursquare.com/os-places/)
(~104M POIs, Apache 2.0). Igual de válida, pero el acceso pasó a un portal con cuenta y
token sobre catálogo Iceberg, contra el S3 anónimo de Overture. Overture es más simple.

> **Acción:** construir `OverturePlacesFetcher` — mismo patrón que `FootprintFetcher`
> (DuckDB + bbox + recorte a la zona + `ST_Contains` a parcela). Reemplaza a Google Places
> como fuente primaria de comercios en Argentina y deja lo pago como complemento.

---

## 3. Shoppings

Overture trae **7** `shopping_center`/`mall` en el bbox, incluido *Paseo Florido* (shopping
real de Hurlingham). `ShoppingFetcher` ya existe y hace OSM + Google con dedupe por nombre y
proximidad: **sumarle Overture como tercera fuente** es barato y lo vuelve gratis.

Sigue sin resolverse —igual que en Brasil— **la cantidad de locales de un shopping**:
ninguna fuente gratuita la publica. En Argentina existe además el listado de la Cámara
Argentina de Shopping Centers y la encuesta de centros de compras del INDEC, pero son
agregados sectoriales, no un padrón de locales por dirección.

---

## 4. Hoteles — es el punto flojo, no hay equivalente a Cadastur

| Fuente | Qué da | Estado |
|---|---|---|
| **PUNA** (Padrón Único Nacional de Alojamiento, `datos.yvera.gob.ar`) | establecimientos, unidades, habitaciones y plazas | ⚠ **No verificado**: el portal devolvió **502** en todos los intentos del 2026-08-10. La documentación disponible lo describe agregado **por provincia / departamento / localidad**, no como padrón nominal con domicilio ⇒ probablemente **no** sirve para ubicar hotel por hotel |
| **Registro de Prestadores Turísticos de PBA** (Res. 23/14, Subsecretaría de Turismo) | nombre, domicilio, teléfono, mail y referencia geográfica | ⚠ Existe por normativa y aparece referenciado con esos campos, pero **no encontré un recurso de descarga abierto**. Habría que pedirlo formalmente |
| **CABA — Alojamientos Turísticos** (`data.buenosaires.gob.ar`) | CSV y SHP con nombre, dirección, contacto y coordenadas | ✅ Existe y está georreferenciado, pero es **sólo Ciudad de Buenos Aires** (no cubre Hurlingham ni el resto de PBA) |
| **Overture / OSM** | nombre, dirección, categoría | ✅ Gratis. **0 hoteles en Hurlingham** — coherente: es un partido residencial |
| **Google Places `lodging`** | ubicación + `businessStatus` | pago, ya integrado en `HotelFetcher` |

**Ninguna fuente gratuita argentina da habitaciones por establecimiento con domicilio**, que
es justo lo que Cadastur aporta en Brasil. Para hoteles en Argentina hay que contar con
`HotelHabitacionesLLM` + la asistencia humana del panel de incidencias, que ya existen.

---

## 4 bis. Geocoding — `georef-ar` es el "geocodebr argentino" · **Mapbox NO sirve en Argentina**

En Brasil la cadena es `geocodebr → Nominatim → Mapbox → Google`. **En Argentina hay que
sacar a Mapbox del medio** y poner `georef-ar` adelante.

**Verificado** sobre 30 direcciones reales de Hurlingham, contra el centroide catastral
(IDERA) como verdad de referencia:

| Fuente | Mediana | p90 | Peor caso | Resueltas | Costo |
|---|---|---|---|---|---|
| **`georef-ar` (Datos Argentina)** | **60 m** | **130 m** | 320 m | 27/30 | **gratis** |
| Nominatim | 414 m | 883 m | — | 29/30 | gratis |
| **Mapbox Geocoding v6** | **810 m** | **24.596 m** | **398 km** | 30/30 | pago |
| Google Geocoding | 10 m ⚠ | 18 m ⚠ | — | 30/30 | USD 5 / 1.000 |

⚠ El número de Google es **circular y no debe tomarse como su precisión real**: las
direcciones de Hurlingham las produjo el propio Google por reverse-geocoding
(`direccion_source='google'` en 438 de 441), así que se lo está midiendo contra sí mismo.

### `georef-ar-api` — https://apis.datos.gob.ar/georef/api/direcciones

Servicio **oficial y gratuito** de normalización de datos geográficos de Argentina. Mismo
rol que geocodebr en Brasil: interpola la altura sobre el nomenclador oficial de calles.
Sin token, sin cuota publicada. Acepta `direccion`, `provincia`, `departamento`.

> ✅ **IMPLEMENTADO** — `georef_ar_lote()` en `agents/geocode_forward.py`, enchufado como
> **paso 0 de `BaselineGeocoder`** en Argentina (`usar_georef_ar`), igual que geocodebr en
> Brasil. **Batch por HTTP: 1.000 direcciones en ~3 s.** Sobre 200 direcciones de Hurlingham:
> **88% resuelto, mediana 57 m, p90 144 m, máx 372 m, ninguna por encima de 500 m.**
>
> ⚠ **Nunca consultar sin ámbito administrativo.** Filtrando sólo por provincia, «Arturo
> Jauretche 1401» resuelve en **Olavarría, a 350 km** de Hurlingham — el mismo tipo de error
> que descartó a Mapbox. Una fila sin partido/localidad se saltea y cae a Nominatim/Google.
> El helper reintenta por **localidad** cuando el nombre no es el del partido (en el conurbano
> el CSV trae «Villa Tesei», que es localidad del partido Hurlingham).

### Mapbox en Argentina: no aporta

- **POIs: cobertura CERO.** Verificado con `Search Box /category`: Times Square 5 resultados,
  Av. Paulista (Brasil) 5 resultados, **Obelisco 0, Hurlingham 0**. El token funciona — es
  falta de datos, no de credencial. Mapbox no puede aportar comercios ni hoteles en Argentina.
- **Geocoding: el peor de los cuatro**, con outliers catastróficos (una dirección de
  Hurlingham resuelta a 398 km). Es **pago** y rinde peor que Nominatim, que es gratis.
- **Edificios**: Mapbox Streets deriva su capa `building` de OSM ⇒ en el conurbano hereda los
  mismos 4 edificios de OSM. Nada nuevo sobre Open Buildings.
- Precio de referencia: Search Box 500 sesiones/mes gratis, después USD 11,50/1.000
  (el endpoint `/category` se factura **por request**, no por sesión).

**Conclusión:** Mapbox se queda **sólo para Brasil**, donde sí midió bien (mediana 44 m, ver
`project_mapbox_benchmark`). En Argentina no debe usarse.

---

## 5. Lo que NO existe: el equivalente a Receita CNPJ

**ARCA (ex AFIP) no publica un dump masivo de contribuyentes.** El padrón se consulta:

- por **CUIT individual**, vía el web service `ws_sr_padron_a10` (requiere certificado y
  clave fiscal), o
- por la constancia de inscripción web, también de a uno.

No hay un archivo descargable con domicilio fiscal + actividad (CLAE) de todos los
establecimientos, como sí lo hay en Brasil. Consecuencia directa: **`ReceitaEstabFetcher` y
`ParcelaCategoria` no tienen insumo equivalente en Argentina**, y la categoría/descripción de
uso de cada parcela tiene que salir de Overture (POIs) en vez del universo fiscal.

Complementos sectoriales útiles, todos por zona y no por parcela:
- **IGN** ([geoservicios](https://www.ign.gob.ar/geoservicios), WMS/WFS + 255 capas SIG en
  SHP/KML/GeoJSON): equipamiento institucional (salud, educación) → alimenta la categoría
  **E** de la taxonomía.
- **Datos Abiertos PBA** (`catalogo.datos.gba.gob.ar`) y portales municipales: las
  **habilitaciones comerciales** son municipales; CABA las publica georreferenciadas, en PBA
  depende de cada municipio. Vale preguntar directo al municipio de Hurlingham.
- **INDEC** — Censo Nacional Económico: agregados por radio censal, no por dirección.

---

## Orden sugerido de trabajo

1. **`FootprintFetcher` al flujo PBA** — gratis, ya está hecho, 94% de cobertura. Sin código nuevo.
2. ✅ **`georef-ar` como paso 0 del geocoding argentino** — hecho. Mapbox además quedó
   **desactivado fuera de Brasil**, donde era pago y peor que Nominatim.
3. ✅ **`OverturePlacesFetcher`** — hecho. En Hurlingham: 137 POIs en la zona, 53 UF de
   comercio en 28 parcelas, 78 con etiqueta de la taxonomía. Costo cero.
4. **Overture como tercera fuente de `ShoppingFetcher`.**
5. **Guarda de `AlturaFetcher`** para no marcar `sin_declarar` donde el catastro no publica
   área construida.
6. **Hoteles**: pedir formalmente el Registro de Prestadores Turísticos de PBA; mientras
   tanto, Overture/OSM + IA + asistencia humana.

**Qué queda pago, y por qué es poco:** sólo el **geocoding de lo que `georef-ar` no resuelve**
(Google Geocoding, USD 5/1.000 ≈ **USD 0,22 por cada 441 parcelas** si georef resuelve el 90%).
Google **Places** —lo caro, USD 32/1.000— **se puede eliminar** de Argentina: lo reemplaza
Overture. En Hurlingham se gastaron ~USD 14 de Places para responder "¿hay comercio sí/no?"
sin guardar un solo nombre.

## Fuentes consultadas

- Overture Places — https://docs.overturemaps.org/guides/places/ · release notes
  https://docs.overturemaps.org/blog/2026/07/22/release-notes/ · DuckDB
  https://docs.overturemaps.org/getting-data/duckdb/
- Foursquare OS Places — https://opensource.foursquare.com/os-places/ ·
  https://docs.foursquare.com/data-products/docs/access-fsq-os-places
- PUNA — https://datos.yvera.gob.ar/dataset/padron-unico-nacional-alojamiento (502 al consultar)
- Alojamientos turísticos CABA — https://data.buenosaires.gob.ar/dataset/alojamientos-turisticos
- Datos Abiertos PBA — https://catalogo.datos.gba.gob.ar/
- IGN — https://www.ign.gob.ar/geoservicios · https://www.ign.gob.ar/NuestrasActividades/InformacionGeoespacial/CapasSIG
- ARCA/AFIP padrón — https://www.arca.gob.ar/ws/ws_sr_padron_a10/manual_ws_sr_padron_a10_v1.2.pdf
- georef-ar API (Datos Argentina) — https://apis.datos.gob.ar/georef/api/direcciones
- Mapbox Search Box — https://docs.mapbox.com/api/search/search-box/ · precios
  https://docs.mapbox.com/mapbox-search-js/guides/pricing/
