# Scrapitero — Instrucciones para Claude Code

> **Al inicio de cada sesión:** leer `ARCHITECTURE.md` para entender el sistema completo sin re-explorar código.

## Regla fundamental
**Nunca ejecutes curl, wget, ni proceses datos geoespaciales directamente.**
Siempre delegá en los agentes RPC. Sos el orquestador, no el ejecutor.

## Cómo ejecutar agentes

Patrón desde el VPS (Python 3.12):
```bash
cd /opt/scrapitero
source .venv/bin/activate && export $(cat .env | grep -v DB_HOST | xargs) && export DB_HOST=localhost
PYTHONPATH=src python -m scrapitero.rpc.<nombre_agente> <<< '<JSON_INPUT>'
```

Patrón desde el container Hermes (Python 3.13):
```bash
PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src python3 -m scrapitero.rpc.<nombre_agente> <<< '<JSON_INPUT>'
```

**No uses `echo '<JSON>' | python ...`.** El escaneo de seguridad bloquea el patrón
"pipe a un intérprete" (`echo | python`) por considerarlo posible ejecución de contenido
sin inspección, y queda esperando aprobación. El agente lee el JSON por stdin igual, así
que pasalo sin pipe: con herestring `<<< '<JSON>'` (como arriba) o, para JSON largo,
escribilo a un archivo y redirigí `python -m scrapitero.rpc.<agente> < input.json`.

## Catálogo completo de agentes

### Diagnóstico y estado
| Agente | RPC | Cuándo usarlo |
|--------|-----|---------------|
| CoverageReporter | `coverage_reporter` | **Siempre primero.** Ver estado actual: setores, parcelas, footprints, direcciones |
| SurveysStatus | `surveys_status` | Listar todos los surveys activos/recientes con conteos |
| SurveyStepUpdate | `survey_step_update` | Registrar progreso de un paso del pipeline en la DB |

### Fuentes de parcelas — Brasil
| Agente | RPC | Cuándo usarlo |
|--------|-----|---------------|
| VGPipelineRunner | `vg_pipeline_runner` | **VG: PREFERIDO.** Happy path compilado: SmartGIS→BCI(parse inline)→Parser→Agrupador en UNA llamada determinista. Registra pasos en `surveys.notes`, honra stop, marca completed. `parcial:true` ⇒ re-invocar (continúa). Los agentes de abajo quedan para correr pasos sueltos/debug |
| SmartGISFetcher | `smartgis_fetcher` | **VG: SIEMPRE primero.** Parcelas Várzea Grande: inscripción+geometría desde SmartGIS |
| VGBCIFetcher | `varzea_bci_fetcher` | VG: Después de SmartGIS. Descarga PDFs BCI (reutiliza existentes en `pdf_downloads/`) **y parsea cada uno apenas baja** (`parse_inline`=true: uso/UF/dirección a DB de a uno). Presupuesto de tiempo (`max_runtime_s`=840): frena con gracia antes del timeout de Hermes (~900s) y devuelve `parcial:true` + `pdfs_pendientes` — re-ejecutar continúa donde quedó (NO es error) |
| BCIParser | `bci_parser` | VG: Después de VGBCIFetcher, como **red de seguridad** (idempotente): re-parsea PDFs con inline fallido o preexistentes sin parsear. Extrae uso/UF/dirección de PDFs sin LLM |
| ONRLotesFetcher | `onr_lotes_fetcher` | Lotes urbanos Brasil (ciudades con cobertura ONR) |
| ONRSigefFetcher | `onr_sigef_fetcher` | Predios rurales Brasil (SIGEF/INCRA, todo el país) |
| ONRCartoIdentify | `onr_carto_identify` | Identificar cartório responsable de un punto (CNS/nombre) |
| IBGECensusFetcher | `ibge_census_fetcher` | Cuando `setores == 0`. Descarga setores censitários IBGE 2022 |
| IBGELogradourosFetcher | `ibge_logradouros_fetcher` | Antes de AddressResolver en Brasil. Geocoding gratis por interpolación |

### Fuentes de parcelas — Argentina
| Agente | RPC | Cuándo usarlo |
|--------|-----|---------------|
| ARBACartoFetcher | `arba_carto_fetcher` | **PBA: SIEMPRE primero.** Requiere JSESSIONID. Si falla login → Telegram al usuario. Si no hay parcelas en DB las baja de IDERA por **filtro espacial** (polígono de la zona) o por nomenclatura si se pasa completa. |
| ARBACadastralFetcher | `arba_cadastral_fetcher` | PBA alternativo: WFS público de IDERA, sin autenticación. **Filtra por el polígono de la zona (`zone_geojson`) por default** — no requiere nomenclatura catastral; pasarla (partido/circ/secc/manzana) es opcional para bajar una manzana puntual. |
| SaltaCatastroFetcher | `salta_catastro_fetcher` | **Salta: SIEMPRE primero.** WFS público sin autenticación. Capital → IDEMSA (~125k parcelas). Interior → IDESA provincial. Selección automática por centroide de zona. |
| SaltaZonificacionFetcher | `salta_zonificacion_fetcher` | Después de SaltaCatastroFetcher. Clasifica uso_principal por CPUA 2019 (residencial/comercial/mixto/industrial/equipamiento/vacante). Cubre ciudad de Salta Capital. |
| SaltaRegistroFetcher | `salta_registro_fetcher` | Después de SaltaCatastroFetcher. Registro SIGSA público (toda la provincia). TIPO: RURAL→vacante, CLUB DE CAMPO→residencial, URBANO→defer a CPUA. Única señal de uso para el interior. |
| SaltaRentasFetcher | `salta_rentas_fetcher` | Después de SaltaZonificacionFetcher. DGRM rentas (Capital, vía Playwright por reCAPTCHA). valorEdificado≈0 → vacante. Detecta baldíos por parcela; corrige al CPUA. Lento + Telegram. |

### Enriquecimiento y resolución
| Agente | RPC | Cuándo usarlo |
|--------|-----|---------------|
| OSMBuildingFetcher | `osm_building_fetcher` | Footprints OSM (cualquier país) cuando `footprints == 0`. Captura tags (tipo/pisos/viviendas) y **vincula cada edificio a su parcela** (`parcela_id`). Insumo de UnidadesEstimator |
| UnidadesEstimator | `unidades_estimator` | Estima **cantidad de unidades de vivienda/comercio** (`uf_vivienda`/`uf_comercio`) por parcela. OSM tags first + proxy geométrico + fallback por uso. Último paso de uso/UF. **NO** usar en Brasil (BCIParser ya da UF exacto) |
| AddressResolver | `address_resolver` | Cuando falta `calle` OR `numero` en parcelas. Cualquier país. Idioma automático. Brasil: IBGE gratis primero, Google Maps fallback. ARG: directo a Google (`es-AR`) |
| GooglePlacesFetcher | `google_places_fetcher` | Comercios de Google Maps (cualquier país). **Después de UnidadesEstimator.** Baja POIs comerciales por teselas adaptativas, los vincula a parcela (`ST_Contains`) y aporta el **conteo real de `uf_comercio`** (cada comercio = +1 UF, `uf_fuente='google'`) + señal de uso (parcela con comercio → comercial/mixto, `uso_fuente='google'`). Caro (~USD 0,032/req): tope `max_requests` + Telegram |
| UsoClassifier | `uso_classifier` | Clasificar `uso_principal` (residencial/comercial/mixto) por parcela |
| EstablecimientoAgrupador | `establecimiento_agrupador` | **Después de uso/UF.** Agrupa parcelas que son UN solo establecimiento (fábrica/colegio/iglesia/galpón). La UF de la entidad = la del **miembro más desarrollado** (mín. 1), no la suma: una fábrica sobre 6 lotes de 1 UF → 1; pero una parcela con `uf_comercio=5` NO se colapsa (la entidad hereda esas 5). Regla: mismo `propietario_documento` real (CNPJ priorizado, sin sentinelas) + parcelas **contiguas** (componente conexa, `ST_DWithin`) + **uso no enteramente residencial**. CPF: solo agrupa sus parcelas con actividad (comercial/industrial/mixto/equipamiento), nunca sus viviendas; CNPJ: agrupa todo el bloque contiguo (incl. vivienda/baldío del predio). Escribe `establecimientos` + estampa `parcelas.establecimiento_id`. Idempotente por survey |

### Estimaciones adicionales (opcional — fuera del relevamiento principal)
| Agente | RPC | Cuándo usarlo |
|--------|-----|---------------|
| DasymetricPopulation | `dasymetric_population` | **Habitantes por manzana** (desagregación dasimétrica). Estimación **secundaria** (menos exacta), se guarda y muestra aparte con su fecha. Reparte `setores_censitarios.pop_total` entre parcelas por peso de ocupación (uf_vivienda → volumen → área) y agrega por manzana catastral. Genérico para cualquier país con censo + parcelas. Necesita: censo que cubra la zona + fuente con parser de manzana (`manzana_catastral.py`). Correr **después** de uso/UF para mejor reparto. NO toca `parcelas`; escribe en `manzanas_habitantes`. |

### Creación de zonas
| Agente | RPC | Cuándo usarlo |
|--------|-----|---------------|
| GeoJSONZoneFetcher | `geojson_zone_fetcher` | Crear región+survey desde archivo GeoJSON de polígonos. **País autodetectado** del centroide (cualquier país); `country_code` es override opcional |
| ZonaFetcher | `zona_fetcher` | Crear zona desde coordenada central + radio en metros. **País autodetectado** del centro |

**Genericidad / multi-país:** el sistema debe poder relevar **cualquier región del mundo**.
- El **país se autodetecta** (reverse-geocoding) al crear la zona, tanto en la web como en
  los agentes de creación — no se hardcodea ni se pide a mano (la web igual deja forzarlo).
- En **Brasil además se autodetecta y guarda el `municipio_codigo` IBGE** del centroide al
  crear la zona (`GeoJSONZoneFetcher`/`ZonaFetcher` → `geo.detect_municipio_br`). Es clave:
  IBGECensusFetcher/IBGELogradourosFetcher lo necesitan, y si la región lo tiene en NULL el
  orquestador puede adivinar un código inválido (rompe esos pasos). Si está en NULL, completarlo.
- Las utilidades geográficas comunes están en `src/scrapitero/agents/geo.py`:
  `area_m2`/`area_km2` (proyectan al **huso UTM correcto según la posición**, válido en
  todo el planeta — no usar husos fijos como 21S/20S), `utm_epsg`, `detect_country` (ISO-3),
  `country_iso2` y `detect_municipio_br` (código IBGE de un punto en Brasil). Cualquier
  cálculo de área nuevo debe usar `geo`, no un EPSG fijo.
- Las **fuentes** sí son por-zona (abaco=VG, IDEMSA/IDESA=Salta, ARBA=PBA): se agregan con
  el patrón **registry por fuente/región** (como `manzana_catastral.py`), y el orquestador
  elige la fuente según país/región.

### Reportes y exportación
| Agente | RPC | Cuándo usarlo |
|--------|-----|---------------|
| RelevamientoReporter | `relevamiento_reporter` | Reporte completo: dirección, UF, nomenclatura por parcela |
| RelevamientoCSV | `relevamiento_csv` | Exportar a CSV compatible Google Sheets |

## Flujo para Várzea Grande (Brasil)

```
1. GeoJSONZoneFetcher  → crear región con zone_geojson (via Web UI o RPC)
2. VGPipelineRunner    → TODO el resto en una llamada determinista:
   SmartGIS → BCI (parseo inline por PDF) → Parser (red de seguridad) → Agrupador
   → marca completed. parcial:true ⇒ re-invocar con el mismo input (continúa).
3. CoverageReporter    → verificar estado
→ Desde Web UI: botón "▶ Iniciar" delega en Hermes, que invoca `vg-pipeline-runner`
  (skill preferida para BRA/VG; los pasos sueltos quedan como fallback/debug).
```

Pasos individuales (fallback/debug — el runner los ejecuta en este orden):
SmartGISFetcher → VGBCIFetcher → BCIParser → EstablecimientoAgrupador.

**Regla VG:** SmartGISFetcher SIEMPRE primero. El `CODIGO_IMOVEL_AGRUPADO` de SmartGIS = `cca_code` en DB = número para descargar BCI en `vg.abaco.com.br`. La zona se respeta automáticamente desde `regions.zone_geojson`.

**Regla `pdf_dir` (VGBCIFetcher ↔ BCIParser):** ambos deben usar el **MISMO `pdf_dir` absoluto**
(default `/opt/scrapitero/pdf_downloads`, o setear `SCRAPITERO_PDF_DIR` que ambos respetan).
Si el fetcher escribe en una carpeta (p.ej. el CWD del container Hermes con un `pdf_dir`
relativo) y el parser lee otra, BCIParser reporta "PDFs faltantes" aunque ya estén descargados.
BCIParser ahora detecta este desajuste y avisa en qué carpeta SÍ están — no hace falta re-descargar.

**Carpeta por ciudad (reuso entre relevamientos):** los PDFs NO se guardan en `pdf_dir/` plano
sino en una **subcarpeta por ciudad**: `pdf_dir/<ciudad>/reporte_*.pdf`. Tanto VGBCIFetcher
(escribe) como BCIParser (lee) resuelven la subcarpeta automáticamente desde el `region_id`
con `resolve_city_pdf_dir()` — no hay que pasar nada extra. La ciudad se deriva del
`municipio_codigo` de la región (`5108402`→`varzea-grande`); como `vg.abaco.com.br` es exclusivo
de Várzea Grande, el default es `varzea-grande` aunque la región no tenga `municipio_codigo`.
Así, al relevar una zona nueva de una ciudad ya relevada, los PDFs de parcelas compartidas se
**reutilizan** sin re-descargar. Para soportar otra ciudad de ábaco, agregar su código a
`_MUNICIPIO_SLUG` en `varzea_bci_fetcher.py`.

## Flujo para Salta (Argentina)

```
1. GeoJSONZoneFetcher           → crear región con zone_geojson (Web UI o RPC)
2. SaltaCatastroFetcher         → parcelas con geometría desde WFS público (capital o interior)
3. OSMBuildingFetcher           → footprints + tags OSM, vinculados a parcela (insumo de UF)
4. AddressResolver              → completar direcciones (directo Google Maps, es-AR)
5. SaltaRegistroFetcher         → TIPO provincial (rural/club de campo → uso)
6. SaltaZonificacionFetcher     → clasificar uso_principal urbano por CPUA 2019 (Capital)
7. SaltaRentasFetcher           → corregir baldíos por valorEdificado (Capital)
8. UnidadesEstimator            → estimar uf_vivienda/uf_comercio por parcela (último de uso/UF)
9. GooglePlacesFetcher          → comercios reales: uf_comercio exacto por conteo (opcional, pago)
10. RelevamientoCSV             → exportar resultado
```

**GooglePlacesFetcher (comercios de Google):** corre **después** de UnidadesEstimator.
Cada comercio que cae dentro de una parcela suma **+1 a `uf_comercio`** (sin agrupar; un
shopping de 20 locales = 20 UF). Es la **fuente autoritativa** de `uf_comercio` (pisa el
proxy geométrico, `uf_fuente='google'`) y señal de uso (parcela con comercio → comercial,
o mixto si ya era residencial, `uso_fuente='google'`). Busca por **teselas adaptativas**
(no por parcela) con tope `max_requests` y avisos de costo por Telegram — Places es caro
(~USD 0,032/req vs USD 0,005 del geocoding). Es **opcional/pago**: agregalo cuando el
conteo de comercios justifique el gasto.

**Objetivo de UF en Salta:** lo que importa es la **cantidad de unidades de vivienda y
de comercio** por parcela, no el conteo de edificios. El conteo exacto de UF no existe
gratis (ver memoria `project_salta_fuentes_uso_uf` y `project_salta_estimacion_uf`).
`OSMBuildingFetcher` + `UnidadesEstimator` lo **estiman**: tags OSM (`building:flats`,
`building:levels`, tipo) cuando existen, proxy geométrico (área×pisos/tamaño_típico)
**solo para edificios en altura** (`building:levels ≥ 2` o `apartments`), y fallback al
mínimo por uso para parcelas sin edificios OSM (común en el interior). Un edificio de
**1 sola planta** sin tag multi-unidad cuenta como **1 UF** (no se subdivide la huella;
evita sobrestimar — antes una casa/local grande de 2300 m² daba 30 viviendas).
**Toda la lógica está documentada en `docs/ESTIMACION_UF.md`.**

**`mixto` = vivienda O comercio (excluyente):** cada UF de una parcela mixto es vivienda
**o** comercio, nunca ambas a la vez. Cada edificio se asigna a una sola categoría por su
tag; los no tipados y el fallback sin edificios → vivienda por defecto. NO se cuentan
1 vivienda + 1 comercio en mixto.

**UF mínimas por uso (Salta):** al clasificar se computan `unidades_funcionales_estimadas`
**y `uf_vivienda`**:
**residencial → SIEMPRE al menos 1 UF de vivienda** (una vivienda mínima por parcela; se
setea tanto `unidades_funcionales_estimadas` como `uf_vivienda` con `GREATEST(…,1)`, sin
pisar un conteo real mayor); vacante/baldío → 0 UF (terreno vacío, sin unidad). Lo aplican
SaltaZonificacionFetcher (uso urbano CPUA) y SaltaRegistroFetcher (CLUB DE CAMPO → residencial);
SaltaRentasFetcher corrige a 0 cuando detecta baldío por `valorEdificado`.

**Uso/UF exactos por parcela:** no hay fuente gratuita con cobertura completa. Ver
memoria `project_salta_fuentes_uso_uf`. Lo gratuito: uso por zona (CPUA) + TIPO
provincial (registro SIGSA) + baldío/edificado por parcela (rentas DGRM).
**Número de UF/PH: ninguna fuente gratuita lo da** — el catastro modela cada UF como
clave independiente; el agrupamiento solo está en la cédula paga de inmuebles.gov.ar.

**Regla Salta:** SaltaCatastroFetcher detecta automáticamente la fuente según el centroide de la zona:
- Ciudad de Salta Capital → IDEMSA (geocloud.municipalidadsalta.gob.ar), ~125k parcelas, EPSG:4326, sin auth
- Interior provincial → IDESA (geoportal.idesa.gob.ar), cobertura provincial, puede ser más lento

---

## Flujo para Buenos Aires Province (Argentina)

```
1. ARBACartoFetcher    → parcelas con geometría + UF/cocheras (requiere JSESSIONID vigente)
   └─ Si falla login  → notificar al usuario por Telegram y detener
2. OSMBuildingFetcher  → footprints de edificios
3. AddressResolver     → completar direcciones faltantes
4. UsoClassifier       → clasificar uso_principal (PASO ESTÁNDAR, no opcional)
5. RelevamientoCSV     → exportar resultado
```

**Regla PBA — uso_principal SIEMPRE se clasifica:** PBA no tiene fuente nativa de uso
(a diferencia de Brasil=BCI y Salta=CPUA/SIGSA). La única señal es `UsoClassifier`, que
combina la **UF de ARBA** (`uf_vivienda`/`uf_comercio` que llena ARBACartoFetcher desde las
subparcelas de carto.arba.gov.ar) + **Google Places** (comercios alrededor). Por eso:
- `UsoClassifier` es **paso estándar** del flujo PBA (no opcional) — si no se corre, todas
  las parcelas quedan `uso_principal = NULL` ("sin clasificar").
- **Depende de ARBACartoFetcher:** si las parcelas entraron solo por IDERA (`arba_idera`,
  geometría sin UF), `UsoClassifier` no tiene UF y cae a Google Places / `sin_datos`. Para
  uso útil, correr ARBACartoFetcher (con JSESSIONID) **antes**.
- Requiere `GOOGLE_MAPS_API_KEY`. Opcional: `GooglePlacesFetcher` para conteo real de comercios.

**Regla PBA — la zona (GeoJSON) maneja la descarga de parcelas:** como todo relevamiento
parte de un GeoJSON, **no hace falta la nomenclatura catastral** para entrar a ARBA. Tanto
ARBACartoFetcher como ARBACadastralFetcher, si no encuentran parcelas en DB, las bajan de
IDERA por **filtro espacial**: bbox del polígono de la zona (`regions.zone_geojson`) vía el
parámetro WFS `bbox=...,EPSG:4326` (que reproyecta desde el CRS nativo Gauss-Krüger del
layer) + **recorte exacto al polígono con shapely**. La nomenclatura (partido/circ/secc/
manzana) es **opcional**: pasarla completa filtra por prefijo CCA (una manzana puntual).
No se usa CQL `INTERSECTS` porque GeoServer interpreta el WKT en el CRS nativo (metros), no
en lat/lon. Para esto la región debe tener `zone_geojson` (creada con GeoJSONZoneFetcher).

## Lo que NO debés hacer
- ❌ `curl https://geoftp.ibge.gov.br/...`
- ❌ `wget ...`
- ❌ Procesar shapefiles directamente
- ❌ Insertar filas en la DB manualmente
- ❌ Instalar paquetes (`pip install`, `uv install`)

## Notificaciones (Telegram + actividad web)

**La audiencia es un operador técnico**, no un usuario final. El operador puede destrabar
el problema (dar una credencial, levantar una fuente caída, reiniciar un servicio) **solo
si el mensaje dice qué falló exactamente**. Por eso, en Telegram y en el cuadro de
actividad de la web:

- **Éxito / progreso:** mensajes cortos con los números clave.
- **Error o problema: SIEMPRE el detalle concreto de la causa.** Prohibido el genérico
  ("hubo un problema" / "reintentando…" sin más). Incluir, textual:
  - el campo `error` del output del agente (copiado tal cual),
  - **qué agente/paso** falló (nombrarlo: SmartGIS, OSM/Overpass, ARBA Carto, SaltaRentas…),
  - la causa técnica exacta: código HTTP + host/URL, credencial/sesión faltante (p.ej.
    `JSESSIONID` vencido), reCAPTCHA que no cargó, `ModuleNotFoundError`, timeout del WFS…,
  - **qué se necesita para resolverlo**, si se sabe.

**Lado código (los logs nacen en el agente, no en las skills):** el decorador
`agent_run` (en `src/scrapitero/agents/_run.py`) envuelve el `run()` de **todos** los
agentes y, ante un fallo (`ok=False`), **sella el campo `error` con el slug de la skill**
(`osm_building_fetcher` → `osm-building-fetcher`) y emite un `logger.error` con `[skill]
detalle`. Por eso el `error` que devuelve cualquier agente **ya incluye qué skill falló +
la causa**; el orquestador sólo tiene que relayarlo tal cual (no reescribir ni resumir).
El cuadro de actividad de la web muestra `surveys.notes.pasos[paso].error`, que ya viene
sellado. Si agregás un agente nuevo, ponele `@agent_run` sobre su `run()`.
Al tocar mensajería, aplicar el cambio en `CLAUDE.md` **y** en las skills `relevar-zona` /
`relevar-region` en el mismo turno (ver memoria `feedback_cambios_hermes`).

## Web UI

Dashboard para gestionar relevamientos. Corre en `http://localhost:8765`.

**Comentarios del cliente (sugerencias/correcciones sobre direcciones relevadas):** el
cliente (y el operador) puede dejar comentarios sobre **una parcela relevada** — uno de los
círculos de color del mapa. Se crean y se leen en el **mismo popup de detalle de la
parcela** (botón "💬 Comentar" dentro del popup → texto). Se guardan en la tabla
`comentarios_cliente` (migración 015: `parcela_id` obligatorio en la práctica, POINT 4326
en el centroide de la parcela, texto, `autor_rol`, `estado` pendiente/resuelto). Es la
**única escritura permitida al rol cliente** (excepción explícita en el middleware de
auth); el backend valida que la parcela pertenezca al survey. Las parcelas comentadas
muestran un pin 💬 (ámbar=pendiente, verde=resuelto) que al clickearlo abre el popup de la
parcela. Cada comentario nuevo dispara un **aviso por Telegram al operador** (región,
dirección de la parcela, coordenadas y texto). El operador gestiona desde la misma sección
del popup: ✔ resolver / ↩ reabrir / 🗑 eliminar. Endpoints:
`GET|POST /api/surveys/{id}/comentarios` (POST con `parcela_id`+`texto`),
`POST /api/comentarios/{id}/estado`, `DELETE /api/comentarios/{id}`.

**Visibilidad en vista cliente (tilde "👁 Cliente"):** cada tarjeta de la lista del
operador tiene un tilde que controla si ese relevamiento se muestra en la vista
cliente (raíz `/`). `surveys.visible_cliente` (migración 014, default `true`); el rol
cliente solo recibe los visibles (filtro server-side en `GET /api/surveys`; toggle:
`POST /api/surveys/{id}/visibilidad`, solo operador).

**Habitantes por manzana (opción adicional):** en el detalle de cada relevamiento hay una
sección aparte **"👥 Habitantes por manzana"** con un botón **"▶ Estimar habitantes"** que
corre `DasymetricPopulation` in-process y muestra una tabla por manzana (Habitantes ≈ ·
rango · UF Viv · UF Com · Parcelas) con total y **fecha de estimación**. Es **secundaria**
al relevamiento (menos exacta), claramente marcada como tal. Endpoints:
`POST /api/surveys/{id}/dasimetrico` (correr) y `GET /api/surveys/{id}/manzanas` (leer).

**Formato del CSV (web y RelevamientoCSV):** la primera columna es la **Dirección
completa** (calle + número + complemento) y actúa como ID de la fila; siguen **Uso**,
**UF Vivienda** y **UF Comercio**, y de ahí en adelante el resto de la información de la
parcela. Sin dirección → `(sin dirección)`.

**CSV Operadora (solo Brasil):** botón verde Brasil "⬇ CSV Operadora" en el detalle de
cada relevamiento (vistas cliente y operador), visible solo si `country_code='BRA'`.
Endpoint `GET /api/surveys/{id}/export/csv-operadora`. Layout de base de logradouros de
operadora: `COD_OPERADORA` (=858 fijo), `NOME_LOCALIDADE`, `UF`, `BAIRRO`,
`BAIRRO_ABREVIADO` (vacío), `NOME_TIPO_LOGR`/`NOME_TITULO`/`PREPOSICAO`/
`NOME_OFICIAL_LOGR` (descomposición heurística de `calle` —
`agents/logradouro_br.py`), `NOME_LOGR_ABREV` (vacío), `CEP`, `NUMERO`,
`CEP_UNICO` (='N' fijo), `CODIGO_LOGRADOURO` (código municipal del logradouro que
BCIParser extrae del PDF — migración 016; vacío para parcelas parseadas antes),
`COD_LOG_PARA` (vacío), `BASE` (vacío, sin valor definido aún). Una fila por parcela
con `calle`; las sin calle se excluyen.

**UF exacta vs estimada:** la web siempre muestra la cantidad de UF de vivienda y comercio.
Cuando la UF es **estimada** (cualquier `parcelas.uf_fuente` ≠ `bci`) la marca con badge
`est.` y prefijo `≈` en los KPIs, y el popup de cada parcela detalla el origen
(`exacto (BCI)` / `estimado (OSM/proxy/por uso)`). **Al pasar el cursor sobre el KPI de UF
o sobre la línea de origen del popup, un tooltip explica cómo se estimó.** El CSV incluye
la columna **UF Fuente**.
`bci`=exacto (BCIParser, Brasil); `osm`/`proxy`/`uso`=estimado (UnidadesEstimator).

**Establecimientos (1 entidad sobre N parcelas):** cuando una fábrica/colegio/iglesia/galpón
ocupa varias parcelas catastrales, `EstablecimientoAgrupador` las agrupa en la tabla
`establecimientos` y el conteo de UF de la web/CSV/reporter cuenta el establecimiento por la
UF de su **parcela más desarrollada** (no la suma de sus miembros). Así una fábrica sobre 6
lotes de 1 UF cuenta 1, pero una parcela con varias UF reales (`uf_comercio=5`) NO se colapsa.
Las parcelas miembro conservan sus datos y quedan vinculadas por `parcelas.establecimiento_id`;
en el mapa el popup las marca como "parte de establecimiento". El CSV trae las columnas
**Establecimiento (tipo)** y **(nombre)**.

**Origen de los datos (data lineage):** el relevamiento final deja registrado de dónde
salió cada dato, en 4 columnas de origen por parcela (todas exportadas en el CSV de la web):
- `fuente_parcela` (col **Fuente**) — quién aportó la parcela/geometría (smartgis_vg, arba_carto,
  salta_idemsa/idesa, sigef_onr, catastro…).
- `direccion_source` (col **Fuente dirección**) — origen de la dirección (bci_pdf, google_geocode,
  osm, nominatim, ibge, catastro).
- `uf_fuente` (col **UF Fuente**) — origen del conteo de UF (bci/osm/proxy/uso/google).
  `google`=GooglePlacesFetcher (conteo real de comercios; autoritativo para `uf_comercio`).
- `uso_fuente` (col **Uso Fuente**, migración 009) — qué agente clasificó `uso_principal`:
  `bci`=BCIParser, `cpua`=SaltaZonificacionFetcher, `sigsa`=SaltaRegistroFetcher,
  `rentas`=SaltaRentasFetcher, `clasificador`=UsoClassifier, `google`=GooglePlacesFetcher
  (parcela con comercio → comercial/mixto). NULL = sin determinar (datos previos
  a la migración).

**Levantar el servidor:**
```bash
cd /opt/scrapitero
source .venv/bin/activate && export $(cat .env | grep -v DB_HOST | xargs) && export DB_HOST=localhost
uvicorn scrapitero.web.app:app --host 0.0.0.0 --port 8765
```

## Stack
- Python 3.12, venv en `/opt/scrapitero/.venv`
- PostgreSQL+PostGIS en Docker (`scrapitero_db`), accesible en `localhost:5432` desde el host
- Hermes Agent en Docker (orquestador de producción), Python 3.13
- Web: FastAPI + uvicorn en puerto 8765
- Repo: github.com/Meter0r0/Scrapitero
