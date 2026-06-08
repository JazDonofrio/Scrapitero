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
| SmartGISFetcher | `smartgis_fetcher` | **VG: SIEMPRE primero.** Parcelas Várzea Grande: inscripción+geometría desde SmartGIS |
| VGBCIFetcher | `varzea_bci_fetcher` | VG: Después de SmartGIS. Descarga PDFs BCI (reutiliza existentes en `pdf_downloads/`) |
| BCIParser | `bci_parser` | VG: Después de VGBCIFetcher. Extrae uso/UF/dirección de PDFs sin LLM |
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
- Las utilidades geográficas comunes están en `src/scrapitero/agents/geo.py`:
  `area_m2`/`area_km2` (proyectan al **huso UTM correcto según la posición**, válido en
  todo el planeta — no usar husos fijos como 21S/20S), `utm_epsg`, `detect_country` (ISO-3)
  y `country_iso2`. Cualquier cálculo de área nuevo debe usar `geo`, no un EPSG fijo.
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
2. SmartGISFetcher     → inscripciones + geometría → parcelas en DB (cca_code = ID para BCI)
3. VGBCIFetcher        → descargar PDFs BCI (reutiliza reporte_*.pdf existentes en pdf_downloads/<ciudad>/)
4. BCIParser           → extraer uso/UF/dirección de los PDFs (sin LLM, regex)
5. CoverageReporter    → verificar estado
→ Desde Web UI: botón "▶ Iniciar" ejecuta pasos 2-4 automáticamente.
```

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
1. ARBACartoFetcher    → parcelas con geometría (requiere JSESSIONID vigente)
   └─ Si falla login  → notificar al usuario por Telegram y detener
2. OSMBuildingFetcher  → footprints de edificios
3. AddressResolver     → completar direcciones faltantes
4. UsoClassifier       → clasificar uso (opcional)
5. RelevamientoCSV     → exportar resultado
```

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

**Habitantes por manzana (opción adicional):** en el detalle de cada relevamiento hay una
sección aparte **"👥 Habitantes por manzana"** con un botón **"▶ Estimar habitantes"** que
corre `DasymetricPopulation` in-process y muestra una tabla por manzana (Habitantes ≈ ·
rango · UF Viv · UF Com · Parcelas) con total y **fecha de estimación**. Es **secundaria**
al relevamiento (menos exacta), claramente marcada como tal. Endpoints:
`POST /api/surveys/{id}/dasimetrico` (correr) y `GET /api/surveys/{id}/manzanas` (leer).

**UF exacta vs estimada:** la web siempre muestra la cantidad de UF de vivienda y comercio.
Cuando la UF es **estimada** (cualquier `parcelas.uf_fuente` ≠ `bci`) la marca con badge
`est.` y prefijo `≈` en los KPIs, y el popup de cada parcela detalla el origen
(`exacto (BCI)` / `estimado (OSM/proxy/por uso)`). **Al pasar el cursor sobre el KPI de UF
o sobre la línea de origen del popup, un tooltip explica cómo se estimó.** El CSV incluye
la columna **UF Fuente**.
`bci`=exacto (BCIParser, Brasil); `osm`/`proxy`/`uso`=estimado (UnidadesEstimator).

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
