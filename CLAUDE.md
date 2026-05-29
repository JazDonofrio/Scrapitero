# Scrapitero — Instrucciones para Claude Code

## Regla fundamental
**Nunca ejecutes curl, wget, ni proceses datos geoespaciales directamente.**
Siempre delegá en los agentes RPC. Sos el orquestador, no el ejecutor.

## Cómo ejecutar agentes

Patrón único para todos los agentes:
```bash
cd /opt/scrapitero
source .venv/bin/activate && export $(cat .env | xargs)
echo '<JSON_INPUT>' | python -m scrapitero.rpc.<nombre_agente>
```

## Agentes disponibles

| Agente | RPC | Cuándo usarlo |
|--------|-----|---------------|
| CoverageReporter | `scrapitero.rpc.coverage_reporter` | Siempre primero, para ver el estado actual |
| IBGECensusFetcher | `scrapitero.rpc.ibge_census_fetcher` | Cuando `setores == 0` en el CoverageReport |
| AddressResolver | `scrapitero.rpc.address_resolver` | Cuando `parcelas_con_direccion / footprints < 0.90` |
| ARBACadastralFetcher | `scrapitero.rpc.arba_cadastral_fetcher` | Parcelas de Buenos Aires Province via WFS público |
| ARBACartoFetcher | `scrapitero.rpc.arba_carto_fetcher` | Parcelas de ARBA Carto (requiere JSESSIONID) |
| IBGELogradourosFetcher | `scrapitero.rpc.ibge_logradouros_fetcher` | Cargar Faces de Logradouros IBGE 2022 (Brasil) — correr antes de AddressResolver para geocoding gratis |
| OSMBuildingFetcher | `scrapitero.rpc.osm_building_fetcher` | Footprints de edificios OSM (cualquier país) |

## Flujo correcto

1. Correr `coverage_reporter` → leer el JSON
2. Decidir qué agente invocar según el JSON
3. Correr ese agente → leer el JSON de output
4. Volver a paso 1

## Ejemplos

**Ver estado actual:**
```bash
echo '{"region_id":"vg-mt-br"}' | \
  source .venv/bin/activate && export $(cat .env | xargs) && \
  python -m scrapitero.rpc.coverage_reporter
```

**Cargar setores IBGE:**
```bash
echo '{"region_id":"vg-mt-br","municipio_codigo":"5108402","estado_uf":"mt"}' | \
  source .venv/bin/activate && export $(cat .env | xargs) && \
  python -m scrapitero.rpc.ibge_census_fetcher
```

**Resolver direcciones faltantes:**
```bash
echo '{"region_id":"vg-mt-br","survey_id":"<UUID>","batch_size":100}' | \
  source .venv/bin/activate && export $(cat .env | xargs) && \
  python -m scrapitero.rpc.address_resolver
```

## Lo que NO debés hacer
- ❌ `curl https://geoftp.ibge.gov.br/...`
- ❌ `wget ...`
- ❌ Procesar shapefiles directamente
- ❌ Insertar filas en la DB manualmente
- ❌ Instalar paquetes (`pip install`, `uv install`)

## Stack
- Python 3.12, venv en `/opt/scrapitero/.venv`
- PostgreSQL+PostGIS en Docker (`scrapitero_db`)
- Hermes Agent en Docker (orquestador de producción)
- Repo: github.com/Meter0r0/Scrapitero
