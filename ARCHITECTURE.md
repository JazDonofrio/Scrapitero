# Scrapitero — Estado de Implementación y Arquitectura

> **Leer este archivo al inicio de cada sesión** para entender el sistema sin re-explorar el código.
> Última actualización: 2026-06-01

---

## 1. Qué es el sistema

Plataforma multi-agente para relevar regiones geográficas y producir un dataset estructurado de parcelas con:
dirección, coordenadas, tipología (residencial/comercial/mixto), unidades funcionales estimadas (vivienda + comercio) y habitantes estimados (vía desagregación dasymetrica del censo IBGE).

**Casos de uso activos:** Várzea Grande (MT, Brasil), Ituzaingó (BA, Argentina).

**Stack:** Python 3.12 · PostgreSQL+PostGIS (Docker) · FastAPI · SQLAlchemy · Pydantic · httpx

---

## 2. Esquema de la DB (PostgreSQL + PostGIS)

### `regions` — catálogo de zonas
| Columna | Tipo | Descripción |
|---------|------|-------------|
| `region_id` | PK str(50) | Ej: `"vg-mt-br"`, `"zona-centro-vg"` |
| `name` | str(200) | Nombre legible |
| `country_code` | str(3) | `"BRA"` o `"ARG"` |
| `state_code` | str(10) | Opcional |
| `municipio_codigo` | str(20) | Código IBGE o INDEC |
| `bbox_wkt` | text | WKT del bbox (POLYGON WGS84) |
| `zone_geojson` | text | GeoJSON original subido por el usuario (migración 006) |

### `surveys` — cada corrida de relevamiento
| Columna | Tipo | Descripción |
|---------|------|-------------|
| `survey_id` | PK UUID | |
| `region_id` | FK regions | |
| `status` | str | `running` / `partial` / `completed` / `failed` |
| `started_at` | datetime | |
| `finished_at` | datetime | |
| `notes` | text | |

### `parcelas` — output principal (1 fila por parcela catastral)
| Columna | Tipo | Descripción |
|---------|------|-------------|
| `parcela_id` | PK UUID | |
| `survey_id`, `region_id` | FK | |
| `geometry` | POLYGON 4326 | |
| `centroid_lat/lng` | float | |
| `area_m2_terreno/construida` | float | |
| `calle`, `numero`, `complemento` | str | Dirección |
| `barrio`, `localidad`, `municipio` | str | |
| `estado_provincia`, `pais`, `codigo_postal` | str | |
| `direccion_source` | str | `catastro`/`osm`/`nominatim`/`google_geocode`/`ibge_logradouros` |
| `direccion_confidence` | float | 0.0–1.0 |
| `uso_principal` | str | `residencial`/`comercial`/`mixto`/`industrial`/`vacante` |
| `unidades_funcionales_estimadas` | int | Total UF |
| `uf_vivienda` | int | UF vivienda (migración 004) |
| `uf_comercio` | int | UF comercio (migración 004) |
| `cca_code` | str | Código catastral (migración 003) |
| `nomenclatura_catastral` | str | (migración 003) |
| `partida_inmobiliaria` | str | (migración 003) |
| `habitantes_estimados` | float | Estimación dasymetrica |
| `fuente_parcela` | str | `catastro`/`footprints_proxy`/`osm_landuse` |

### `edificios` — footprints (tabla interna)
| Columna | Descripción |
|---------|-------------|
| `edificio_id` | PK UUID |
| `survey_id`, `parcela_id` | FK |
| `footprint` | POLYGON 4326 |
| `area_m2`, `pisos_estimados` | |
| `source` | `osm`/`ms_global`/`google_open` |

### `setores_censitarios` — sectores IBGE
Contiene geometría MULTIPOLYGON y estadísticas: `pop_total`, `domicilios_total`, `domicilios_casas`, `domicilios_aptos`, `area_km2`.

### `logradouros` — segmentos de calle IBGE 2022 (migración 005)
Geometría LINESTRING con numeración de cada lado (izq/der) y CEP. Usada por AddressResolver para geocoding gratis.

### `orchestrator_log` — auditoría del LLM
Cada step del orquestador registra: `agent_called`, `input_resumen`, `output_resumen`, `razonamiento_llm`, `tokens`, `costo_usd`, `duration_ms`.

### Historial de migraciones
| # | Qué agrega |
|---|-----------|
| 001 | Schema inicial (7 tablas) |
| 002 | Región `ituzaingo-ba-ar` |
| 003 | `cca_code`, `nomenclatura_catastral`, `partida_inmobiliaria` en `parcelas` |
| 004 | `uf_vivienda`, `uf_comercio` en `parcelas` |
| 005 | Tabla `logradouros` |
| 006 | `zone_geojson` en `regions` |

---

## 3. Agentes implementados

Todos en `src/scrapitero/agents/`. Interfaz: `run(input: XInput) -> XOutput`. RPC en `src/scrapitero/rpc/`.

### CoverageReporter
**Input:** `region_id`, `survey_id?`
**Output:** conteos de setores, footprints, parcelas, direcciones, habitantes + porcentajes de cobertura + errores
**Cuándo:** siempre primero para ver el estado actual

### IBGECensusFetcher
**Input:** `region_id`, `municipio_codigo`, `estado_uf`
**Output:** `setores_insertados`, `pop_total`
**Cuándo:** cuando `setores == 0` en el CoverageReport
**Cómo:** descarga SHP de geoftp.ibge.gov.br, carga en `setores_censitarios`

### IBGELogradourosFetcher
**Input:** `region_id`, `municipio_codigo`, `estado_uf`
**Output:** `logradouros_insertados`
**Cuándo:** antes de AddressResolver para tener geocoding gratis
**Cómo:** descarga shapefile de Faces de Logradouros 2022 de IBGE

### OSMBuildingFetcher
**Input:** `region_id`, `survey_id`, `bbox_south/west/north/east?`
**Output:** `edificios_insertados`, `edificios_actualizados`, `bbox_usado`
**Cuándo:** para descargar footprints de edificios
**Cómo:** Overpass API, con fallback a mirrors; proyecta a UTM para área_m2

### AddressResolver
**Input:** `region_id`, `survey_id?`, `batch_size`, `delay_ms`
**Output:** `parcelas_procesadas`, `parcelas_resueltas`, breakdown por fuente, `costo_estimado_usd`
**Estrategia:** 1) interpolación sobre `logradouros` IBGE (gratis) → 2) Google Maps Reverse Geocoding ($0.005/req)

### ARBACadastralFetcher
**Input:** `region_id`, `survey_id`, `partido_id`
**Output:** `parcelas_insertadas`
**Cuándo:** parcelas de Buenos Aires Province via WFS público (sin autenticación)

### ARBACartoFetcher
**Input:** `region_id`, `survey_id`, `jsessionid`, `bbox?`
**Output:** `parcelas_insertadas`, `parcelas_actualizadas`
**Cuándo:** parcelas de ARBA Carto — **requiere JSESSIONID** vigente
**Importante:** si falla login, enviar alerta Telegram antes de continuar

### ONRLotesFetcher
**Input:** `region_id`, `survey_id`, + coordenadas
**Output:** lotes ONR insertados
**Cuándo:** parcelas rurales Brasil (ONR — Ofício de Registro de Imóveis)

### ONRSigefFetcher
**Input:** similar a ONRLotes
**Cuándo:** parcelas georreferenciadas INCRA/SIGEF (rurales Brasil)

### UsoClassifier
**Input:** `region_id`, `survey_id?`, `batch_size`
**Output:** `clasificadas`
**Cuándo:** clasificar `uso_principal` en `parcelas`

### RelevamientoReporter
**Input:** `region_id`, `survey_id?`
**Output:** `RelevamientoReport` con lista de `ParcelaResumen` (dirección, área, UF, nomenclatura)
**Cuándo:** generar reporte del relevamiento

### RelevamientoCSV
**Input:** `region_id`, `survey_id?`, `output_path`
**Output:** archivo CSV con headers Google Sheets (UTF-8-sig, separador `;`)

### ZonaFetcher
**Input:** `lat`, `lng`, `radio_m`, `region_id?`, `region_nombre?`
**Output:** `region_id`, `survey_id`, `bbox`, `edificios_insertados`
**Cuándo:** crear zona desde coordenada central + radio (sin GeoJSON)

### SmartGISFetcher
**Input:** `region_id`, `survey_id`, `bbox?` (se deriva de `zone_geojson` si no se pasa)
**Output:** `parcelas_insertadas`, `parcelas_actualizadas`, `lotes_escaneados`, `fuera_de_zona`
**Cuándo:** Primer agente del flujo VG. Escanea en grid de ~30m, llama `IdentifyOnExtent` + `Get/{id}`
**Clave:** `CODIGO_IMOVEL_AGRUPADO` → `cca_code` en DB. Es el número para descargar BCI.
**Respeta** `zone_geojson` automáticamente. Si no hay zone_geojson, usa `bbox_wkt`.

### VGBCIFetcher
**Input:** `region_id`, `survey_id?`, `pdf_dir`, `batch_size`, delays, pausa params
**Output:** `pdfs_descargados`, `pdfs_ya_existentes`, `pdfs_fallidos`, `parcelas_procesadas`
**Cuándo:** Después de SmartGISFetcher. Requiere `cca_code` en parcelas.
**Clave:** Lee inscripciones del DB → filtra por zone_geojson → verifica si `reporte_{cca}.pdf` existe → Playwright si falta
**Reutiliza** PDFs ya descargados en `pdf_downloads/`. Compatible con scraper legacy.
**Notifica** vía Telegram al inicio y al final.

### BCIParser
**Input:** `region_id`, `survey_id?`, `pdf_dir`, `batch_size`
**Output:** `procesadas`, `actualizadas`, `sin_pdf`, `errores`
**Cuándo:** Después de VGBCIFetcher. Lee los PDFs en `/opt/scrapitero/pdf_downloads/reporte_{cca_code}.pdf`
**Extrae (regex, sin LLM):** `uso_principal`, `uf_vivienda`, `uf_comercio`, `area_m2_construida`, dirección completa, `partida_inmobiliaria`
**Dependencia:** `pdfplumber` (declarado en pyproject.toml); `.hermes-packages/` contiene `_cffi_backend.cpython-313-x86_64-linux-gnu.so` para Python 3.13.

### ONRCartoIdentify
**Input:** `lat`, `lng`
**Output:** `cns`, `cartorio`, `comarca`, `uf`
**Cuándo:** Alternativo a SmartGIS. Útil para saber qué cartório registra el área.
**Para VG** siempre devuelve CNS=063446, "1º Registro de Imóveis de Várzea Grande".

### GeoJSONZoneFetcher
**Input:** `region_nombre`, `geojson_str`, `country_code` (BRA/ARG), `region_id?`
**Output:** `region_id`, `survey_id`, `bbox`, `edificios_insertados`
**Cuándo:** crear zona desde un archivo GeoJSON (también usado internamente por el Web UI)
**Guarda:** el GeoJSON en `regions.zone_geojson` para mostrarlo en el mapa

---

## 4. Web UI *(nuevo, 2026-05-30)*

FastAPI app en `src/scrapitero/web/app.py`. Frontend vanilla JS + Leaflet en `src/scrapitero/web/static/index.html`.

### Cómo levantar
```bash
cd /opt/scrapitero
source .venv/bin/activate
export $(cat .env | grep -v DB_HOST | xargs) && export DB_HOST=localhost
uvicorn scrapitero.web.app:app --host 0.0.0.0 --port 8765
```
Abre en `http://localhost:8765`

### Endpoints
| Método | Ruta | Qué hace |
|--------|------|----------|
| GET | `/` | Sirve `index.html` |
| GET | `/api/surveys` | Lista todos los surveys con stats agregadas |
| POST | `/api/surveys` | Crea nuevo survey (form: `nombre`, `country_code`, `geojson_file`) |

**Stats que devuelve `/api/surveys`:** `total_edificios`, `total_parcelas`, `total_uf_vivienda`, `total_uf_comercio`, `con_direccion`, `area_total_m2`, `zone_geojson`

**Comportamiento del POST:** crea región+survey síncronamente, lanza descarga OSM en background thread, devuelve `{ok, survey_id, region_id}` de inmediato.

### Por qué DB_HOST=localhost
`.env` tiene `DB_HOST=scrapitero_db` (hostname Docker). Desde el host, el puerto 5432 está expuesto en `localhost`. Hermes y los agentes dentro de Docker usan `scrapitero_db`.

---

## 5. Flujo de trabajo estándar

```
1. CoverageReporter → ver estado
2. Si setores == 0 → IBGECensusFetcher
3. Si edificios == 0 → OSMBuildingFetcher (o GeoJSONZoneFetcher para zona nueva)
4. Si logradouros == 0 → IBGELogradourosFetcher (Brasil, antes de AddressResolver)
5. Si parcelas_con_direccion/parcelas < 0.90 → AddressResolver
6. Si país == ARG → ARBACartoFetcher (primero) o ARBACadastralFetcher
7. RelevamientoReporter → ver resultado
8. RelevamientoCSV → exportar
```

### Cómo ejecutar cualquier agente
```bash
cd /opt/scrapitero
source .venv/bin/activate && export $(cat .env | grep -v DB_HOST | xargs) && export DB_HOST=localhost
echo '{ ...JSON... }' | python -m scrapitero.rpc.<nombre_agente>
```

> **Nota:** En producción (dentro de Docker/Hermes) usar `export $(cat .env | xargs)` sin override de DB_HOST.

---

## 6. Estructura de carpetas

```
src/scrapitero/
├── agents/          # Lógica de cada agente (run(input) → output)
│   ├── coverage_reporter.py
│   ├── ibge_census_fetcher.py
│   ├── ibge_logradouros_fetcher.py
│   ├── osm_building_fetcher.py
│   ├── address_resolver.py
│   ├── arba_cadastral_fetcher.py
│   ├── arba_carto_fetcher.py
│   ├── onr_lotes_fetcher.py
│   ├── onr_sigef_fetcher.py
│   ├── onr_token.py
│   ├── uso_classifier.py
│   ├── zona_fetcher.py
│   ├── geojson_zone_fetcher.py   ← nuevo
│   ├── relevamiento_reporter.py
│   ├── relevamiento_csv.py
│   └── relevamiento_pdf.py
├── rpc/             # Wrappers stdin/stdout JSON para cada agente
├── db/
│   ├── engine.py    # get_engine(), get_session(), check_connection()
│   └── models.py    # ORM SQLAlchemy (Region, Survey, Parcela, Edificio, etc.)
├── web/             # ← nuevo
│   ├── app.py       # FastAPI
│   └── static/index.html
└── models/          # (revisar si tiene contenido relevante)

alembic/versions/    # Migraciones 001–006
hermes-skills/scrapitero/  # Skills para el orquestador Hermes
```

---

## 7. Dependencias clave instaladas en .venv

- **Geo:** geopandas, shapely, pyproj, pyogrio, geoalchemy2, mercantile
- **DB:** sqlalchemy≥2.0, alembic, psycopg[binary]≥3.1, duckdb
- **HTTP:** httpx, hishel (caché en disco)
- **Web:** fastapi≥0.111, uvicorn[standard], python-multipart
- **I/O:** pydantic≥2.7, typer, rich, loguru
- **Notif:** python-telegram-bot≥21.0
- **PDF:** pdfplumber≥0.11 (extracción texto PDFs BCI)

### Nota sobre .hermes-packages/
El container Hermes usa Python 3.13. `.hermes-packages/` contiene los paquetes con extensiones compiladas para esa versión. El archivo clave es `_cffi_backend.cpython-313-x86_64-linux-gnu.so` — si se actualiza el container a otra versión de Python, regenerarlo con:
```bash
docker exec <container> sh -c "uv pip install --target /tmp/cffi313 cffi --python python3"
docker cp <container>:/tmp/cffi313/_cffi_backend.cpython-313-x86_64-linux-gnu.so /opt/scrapitero/.hermes-packages/
```

---

## 8. Variables de entorno (.env)

| Variable | Descripción |
|----------|-------------|
| `DB_USER`, `DB_PASSWORD` | Credenciales PostgreSQL |
| `DB_HOST` | `scrapitero_db` (Docker) / `localhost` (host) |
| `DB_PORT` | `5432` |
| `DB_NAME` | `scrapitero` |
| `GOOGLE_MAPS_API_KEY` | Para AddressResolver fallback (pago) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Alertas HITL |
| (posiblemente) `ARBA_JSESSIONID` | Para ARBACartoFetcher |

---

## 9. Estado actual y próximos pasos

### Implementado y funcional (v1.0 — 2026-06-01)
- [x] Schema DB completo (migrations 001–006)
- [x] CoverageReporter, IBGECensusFetcher, IBGELogradourosFetcher
- [x] OSMBuildingFetcher (con Overpass + mirrors)
- [x] AddressResolver (IBGE logradouros + Google fallback)
- [x] ARBACadastralFetcher, ARBACartoFetcher (Argentina)
- [x] ONRLotesFetcher, ONRSigefFetcher (rurales Brasil)
- [x] UsoClassifier, ZonaFetcher, GeoJSONZoneFetcher
- [x] RelevamientoReporter, RelevamientoCSV, RelevamientoPDF
- [x] Web UI (FastAPI + Leaflet) en puerto 8765
- [x] **SmartGISFetcher** — parcelas VG desde SmartGIS (inscripción + geometría)
- [x] **VGBCIFetcher** — descarga PDFs BCI via Playwright, reutiliza existentes
- [x] **BCIParser** — extrae uso/UF/dirección de PDFs (sin LLM, regex). Funcional en Hermes (Python 3.13).

### Pendiente / parcial
- [ ] PopulationEstimator (desagregación dasymetrica IBGE)
- [ ] SpatialJoiner (joins entre capas)
- [ ] BuildingFootprintFetcher (Microsoft Global / Google Open Buildings)
- [ ] Exporter (GeoJSON/GeoParquet)
- [ ] Validator (chequeos duros post-relevamiento)
- [ ] Web UI: detalle de parcelas, filtros, exportación desde UI
- [ ] Web UI: ejecutar agentes desde la interfaz

### Datos relevados en DB (a la fecha)
- `ituzaingo-ba-ar`: ~220 parcelas con dirección (ARG)
- `zona-varzea-pequeno-etapa-2`: 121 parcelas, 56 BCIs parseados
- `zona-prueba-varzea`, zonas menores VG: ~175 parcelas adicionales con cca_code
- `vg-mt-br`, `zona-vg-*`: surveys con setores/logradouros, parcelas pendientes de SmartGIS
