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
echo '<JSON_INPUT>' | PYTHONPATH=src python -m scrapitero.rpc.<nombre_agente>
```

Patrón desde el container Hermes (Python 3.13):
```bash
echo '<JSON_INPUT>' | PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src python3 -m scrapitero.rpc.<nombre_agente>
```

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
| ARBACartoFetcher | `arba_carto_fetcher` | **PBA: SIEMPRE primero.** Requiere JSESSIONID. Si falla login → Telegram al usuario |
| ARBACadastralFetcher | `arba_cadastral_fetcher` | PBA alternativo: WFS público de ARBA, sin autenticación |

### Enriquecimiento y resolución
| Agente | RPC | Cuándo usarlo |
|--------|-----|---------------|
| OSMBuildingFetcher | `osm_building_fetcher` | Footprints de edificios OSM (cualquier país) cuando `footprints == 0` |
| AddressResolver | `address_resolver` | Cuando falta `calle` OR `numero` en parcelas. Cualquier país. Idioma automático. Brasil: IBGE gratis primero, Google Maps fallback. ARG: directo a Google (`es-AR`) |
| UsoClassifier | `uso_classifier` | Clasificar `uso_principal` (residencial/comercial/mixto) por parcela |

### Creación de zonas
| Agente | RPC | Cuándo usarlo |
|--------|-----|---------------|
| GeoJSONZoneFetcher | `geojson_zone_fetcher` | Crear región+survey desde archivo GeoJSON de polígonos |
| ZonaFetcher | `zona_fetcher` | Crear zona desde coordenada central + radio en metros |

### Reportes y exportación
| Agente | RPC | Cuándo usarlo |
|--------|-----|---------------|
| RelevamientoReporter | `relevamiento_reporter` | Reporte completo: dirección, UF, nomenclatura por parcela |
| RelevamientoCSV | `relevamiento_csv` | Exportar a CSV compatible Google Sheets |

## Flujo para Várzea Grande (Brasil)

```
1. GeoJSONZoneFetcher  → crear región con zone_geojson (via Web UI o RPC)
2. SmartGISFetcher     → inscripciones + geometría → parcelas en DB (cca_code = ID para BCI)
3. VGBCIFetcher        → descargar PDFs BCI (reutiliza reporte_*.pdf existentes en pdf_downloads/)
4. BCIParser           → extraer uso/UF/dirección de los PDFs (sin LLM, regex)
5. CoverageReporter    → verificar estado
→ Desde Web UI: botón "▶ Iniciar" ejecuta pasos 2-4 automáticamente.
```

**Regla VG:** SmartGISFetcher SIEMPRE primero. El `CODIGO_IMOVEL_AGRUPADO` de SmartGIS = `cca_code` en DB = número para descargar BCI en `vg.abaco.com.br`. La zona se respeta automáticamente desde `regions.zone_geojson`.

## Flujo para Buenos Aires Province (Argentina)

```
1. ARBACartoFetcher    → parcelas con geometría (requiere JSESSIONID vigente)
   └─ Si falla login  → notificar al usuario por Telegram y detener
2. OSMBuildingFetcher  → footprints de edificios
3. AddressResolver     → completar direcciones faltantes
4. UsoClassifier       → clasificar uso (opcional)
5. RelevamientoCSV     → exportar resultado
```

## Lo que NO debés hacer
- ❌ `curl https://geoftp.ibge.gov.br/...`
- ❌ `wget ...`
- ❌ Procesar shapefiles directamente
- ❌ Insertar filas en la DB manualmente
- ❌ Instalar paquetes (`pip install`, `uv install`)

## Web UI

Dashboard para gestionar relevamientos. Corre en `http://localhost:8765`.

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
