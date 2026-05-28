# Plan de Implementación v1 — Scrapitero + Hermes

**Fecha:** Mayo 2026  
**Estado:** Listo para ejecutar  
**Objetivo de pruebas:** Pipeline end-to-end sobre 1 setor censitário de Várzea Grande

---

## Principio rector: LLM mínimo

Hermes tiene un feature clave para este proyecto: **Python RPC scripts for zero-context-cost pipelines**. Eso significa que los 13 agentes Python corren como RPCs sin consumir tokens LLM. El LLM (Gemini, vía Hermes) solo toca dos cosas:

1. **Lee un CoverageReport** — JSON de ~150 tokens con el estado actual del snapshot
2. **Decide qué agente llamar a continuación** — 1 tool call

Eso es todo. Todo lo demás es código determinístico.

**Target por corrida sobre VG completo:**
- < 20 steps del orquestador
- < 500 tokens input/output por step
- Costo LLM estimado: U$D 0.02–0.05 usando Gemini Flash
- Costo APIs pagas: U$D 0 (todo open data en Fase 1)

---

## Arquitectura Hermes ↔ Scrapitero

```
Telegram (vos)
    │
    ▼
Hermes (VPS)  ←── memoria persistente, skills auto-generados
    │
    ├── Orquestador Gemini Flash (tool-use loop)
    │       lee: CoverageReport (~150 tokens)
    │       decide: próximo agente a llamar
    │
    └── Python RPC Scripts (zero LLM tokens)
            ├── ibge_census_fetcher.py
            ├── building_footprint_fetcher.py
            ├── spatial_joiner.py
            ├── address_resolver.py
            ├── population_estimator.py
            ├── coverage_reporter.py
            └── exporter.py
                    │
                    ▼
            Postgres + PostGIS (Docker, VPS)
```

**Flujo de una corrida:**
1. Vos mandás por Telegram: `relevar setor 310430505000001 VG`
2. Hermes crea snapshot_id, llama `coverage_reporter.py` → recibe JSON vacío
3. Gemini Flash lee el reporte → decide `ibge_census_fetcher`
4. Hermes ejecuta el RPC (0 tokens LLM) → resultado va a Postgres
5. Hermes vuelve a Gemini con resumen de 2 líneas → decide siguiente agente
6. Loop hasta que CoverageReporter diga cobertura aceptable
7. Hermes te notifica por Telegram con el parquet generado

---

## FASE 0 — Skeleton del repo (Sesión 1, ~3h)

### 0.1 Migrar a pyproject.toml + uv

```toml
# pyproject.toml (estructura)
[project]
name = "scrapitero"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "geopandas>=0.14", "pyogrio>=0.7", "shapely>=2.0", "pyproj>=3.6",
    "mercantile>=1.2",                   # quadkeys MS Buildings
    "sqlalchemy>=2.0", "geoalchemy2>=0.14", "alembic>=1.13",
    "psycopg[binary]>=3.1",
    "duckdb>=0.10",                       # joins sobre Parquet pesados
    "httpx>=0.27", "hishel>=0.0.30",      # HTTP + caché disco
    "pydantic>=2.7",
    "typer>=0.12", "rich>=13.7", "loguru>=0.7",
    "python-telegram-bot>=21.0",
]
```

### 0.2 docker-compose.yml

```yaml
services:
  postgis:
    image: postgis/postgis:16-3.4
    environment:
      POSTGRES_DB: scrapitero
      POSTGRES_USER: scrap
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    ports: ["5432:5432"]
    volumes: ["./data/postgres:/var/lib/postgresql/data"]
```

### 0.3 Estructura de carpetas

```
src/scrapitero/
    agents/          # los 13 agentes (clases Python)
    db/              # SQLAlchemy models + Alembic
    rpc/             # wrappers CLI de cada agente (entry points para Hermes)
    models/          # schemas Pydantic
    orchestrator/    # loop tool-use (para pruebas sin Hermes)
    notifier.py      # Telegram (código existente, solo se mueve)
    utils.py         # UUID v5 determinístico (código existente)
```

### 0.4 Migración Alembic inicial

Tablas a crear en la migración `001_initial`:

| Tabla | Propósito |
|---|---|
| `regions` | catálogo de regiones (VG, etc.) |
| `surveys` (snapshots) | cada corrida tiene su snapshot_id |
| `setores_censitarios` | geometría + población IBGE por setor |
| `parcelas` | output final (1 fila por parcela) |
| `edificios` | footprints internos |
| `unidades_funcionales` | estimadas, uso interno |
| `orchestrator_log` | auditoría de cada step LLM |

**Criterio de éxito Fase 0:** `docker compose up` levanta Postgres, `alembic upgrade head` crea las 7 tablas sin errores.

---

## FASE 1 — Primeros 3 agentes determinísticos (Sesiones 2-3)

### Agente 1: IBGECensusFetcher

**Por qué primero:** es el más simple (HTTP GET + unzip), no requiere parseo geo complejo, y desbloquea `PopulationEstimator` que es la pieza más diferenciadora del producto.

**Responsabilidad:** dado `municipio_codigo` (ej: `5108402` para VG), descarga:
- Malha de setores censitários (SHP) → tabla `setores_censitarios`
- Tabla SIDRA 9596 (domicilios por tipo, por setor) → columnas adicionales en `setores_censitarios`

**Fuentes:**
```
# Malha setores VG (SHP, ~4 MB)
https://geoftp.ibge.gov.br/organizacao_do_territorio/malhas_territoriais/
malhas_de_setores_censitarios__divisoes_intramunicipais/censo_2022/
setores_censitarios_shp/mt/mt_setores_censitarios.zip

# SIDRA tabla 9596 (CSV)
https://apisidra.ibge.gov.br/values/t/9596/n322/5108402/v/allxp/p/last%201/c629/allxt
```

**Interface Pydantic:**
```python
class IBGEInput(BaseModel):
    municipio_codigo: str        # "5108402"
    survey_id: uuid.UUID
    bbox: Optional[tuple] = None # si None, municipio completo

class IBGEOutput(BaseModel):
    setores_insertados: int
    pop_total: int
    domicilios_total: int
    fuentes: list[str]
```

**RPC wrapper** (`src/scrapitero/rpc/ibge_census_fetcher.py`):
```python
# Entry point para Hermes — recibe JSON por stdin, devuelve JSON por stdout
# Hermes lo llama como: echo '{"municipio_codigo": "5108402", ...}' | python rpc/ibge_census_fetcher.py
```

### Agente 2: BuildingFootprintFetcher

**Responsabilidad:** dado `bbox` de VG, descarga Microsoft Global Building Footprints.

**Lógica de quadkeys (via `mercantile`):**
```python
# VG bbox aproximado
bbox = (-56.15, -15.85, -55.90, -15.55)  # lng_min, lat_min, lng_max, lat_max

# MS Building Footprints usa zoom 9
tiles = list(mercantile.tiles(*bbox, zooms=9))
# Cada tile → URL en Azure Blob:
# https://minedbuildings.z5.web.core.windows.net/global-buildings/{quadkey}.geojsonl.gz
```

**Procesamiento con DuckDB** (no carga todo en RAM):
```python
# DuckDB lee GeoJSONL directo, hace ST_Within con bbox, inserta en Postgres
# Estimado VG: ~87.000 footprints, ~120 MB descarga
```

**Interface:**
```python
class BuildingFootprintInput(BaseModel):
    bbox: tuple[float, float, float, float]
    survey_id: uuid.UUID
    source: Literal["ms_global", "google_open"] = "ms_global"

class BuildingFootprintOutput(BaseModel):
    footprints_insertados: int
    tiles_descargados: int
    area_m2_total: float
```

### Agente 3: SpatialJoiner

**Responsabilidad:** join PostGIS entre footprints y setores censitários.

```sql
-- La query que ejecuta el agente
UPDATE edificios e
SET setor_censitario_id = s.setor_id
FROM setores_censitarios s
WHERE ST_Within(e.centroid, s.geometry)
  AND e.survey_id = :survey_id;
```

**Nota:** PostGIS hace esto sobre índices GIST — sin Python, sin LLM. Para 87k footprints sobre 412 setores es < 5 segundos.

### Agente 4: CoverageReporter (el más importante para el LLM)

Este agente es el que más impacta en reducir tokens LLM. Su output tiene que ser el **mínimo JSON informativo** para que Gemini pueda decidir el próximo paso.

```python
class CoverageReport(BaseModel):
    survey_id: str
    step: int
    # Conteos (todo entero, nada de geometrías)
    setores: int
    pop_total_ibge: int
    footprints: int
    footprints_con_setor: int
    parcelas: int
    parcelas_con_direccion: int
    parcelas_con_habitantes: int
    # Ratios
    cobertura_footprints_pct: float    # footprints_con_setor / footprints
    cobertura_direccion_pct: float
    cobertura_habitantes_pct: float
    # Estado
    suma_hab_vs_ibge_delta_pct: float  # 0.0 si no hay datos aún
    errores: list[str]                 # lista corta de errores graves
```

**Este JSON no supera los 200 tokens nunca.** Eso es todo lo que Gemini lee por step.

**Criterio de éxito Fase 1:** correr los 3 agentes manualmente sobre el setor `310430505000001` de VG y tener filas en las tablas correspondientes.

---

## FASE 2 — Integración Hermes (Sesión 4)

### 2.1 Hermes Skills para cada agente

Cada RPC se registra como skill en Hermes. Hermes ya sabe ejecutar scripts Python en el VPS.

Estructura de un skill en Hermes:
```markdown
# SKILL: ibge_census_fetcher
# Descripción: Descarga setores censitários IBGE + tabla SIDRA para un municipio
# Uso: ibge_census_fetcher municipio_codigo=5108402 survey_id=<uuid>
```

### 2.2 Loop del orquestador (herramienta para Gemini)

El orquestador es un loop en Hermes que:
1. Llama `coverage_reporter` → recibe JSON
2. Pasa ese JSON a Gemini Flash con el system prompt del orquestador
3. Gemini devuelve 1 tool call (nombre de agente + parámetros)
4. Hermes ejecuta el RPC correspondiente (Python, 0 tokens)
5. Vuelve a paso 1

**System prompt del orquestador** (< 400 tokens, se manda una sola vez):
```
Sos el orquestador de Scrapitero. Tu trabajo es decidir qué agente ejecutar
a continuación para completar el relevamiento de una región.

Reglas:
- NUNCA proceses datos vos mismo. Solo llamá agentes.
- Si cobertura_footprints_pct < 0.95 → llamá SpatialJoiner
- Si footprints == 0 → llamá BuildingFootprintFetcher
- Si setores == 0 → llamá IBGECensusFetcher
- Si parcelas_con_direccion / footprints < 0.90 → llamá AddressResolver
- Si parcelas_con_habitantes == 0 → llamá PopulationEstimator
- Si cobertura_habitantes_pct > 0.95 y suma_hab_vs_ibge_delta_pct < 0.02 → llamá Exporter
- Si step > 20 → llamá Exporter con cobertura parcial y avisá por Telegram
```

**Costo real de esta interacción:** ~350 tokens (system) + ~150 tokens (CoverageReport) + ~50 tokens (tool call) = **~550 tokens por step**. A precio de Gemini Flash (~$0.075/1M tokens): **< U$D 0.001 por step**.

### 2.3 HITL via Telegram

Hermes ya tiene Telegram integrado. Se agregan 2 casos de notificación:

1. **Error grave** (agente falla 2 veces seguido) → Hermes te manda mensaje con el error + te pide instrucción
2. **Corrida completa** → Hermes te manda: cobertura final + link de descarga del parquet

```python
# El notifier.py existente ya hace esto, solo hay que conectarlo a los eventos de Hermes
```

---

## Prueba end-to-end objetivo

**Alcance mínimo para validar el pipeline:**
- 1 setor censitário de VG: `310430505000001` (setor urbano, ~600 domicilios)
- Solo fuentes gratuitas: IBGE + MS Building Footprints + Nominatim
- Output esperado: ~200-300 filas en `parcelas` con geometría + habitantes estimados

**Comando por Telegram:**
```
relevar setor 310430505000001
```

**Output esperado por Telegram (< 5 minutos):**
```
✅ Corrida completa — setor 310430505000001 VG
• Parcelas: 287
• Con dirección: 271 (94%)
• Con habitantes estimados: 287 (100%)
• Suma hab vs IBGE: +0.3% ✓
• Steps LLM: 8
• Costo Gemini: U$D 0.004
• Archivo: parcelas_310430505000001.parquet
```

---

## Orden de trabajo por sesión

| Sesión | Tarea | Resultado verificable |
|---|---|---|
| 1 (hoy) | Fase 0: skeleton + docker + alembic | `alembic upgrade head` OK, tablas en Postgres |
| 2 | IBGECensusFetcher | setores de VG en DB, ~412 filas |
| 3 | BuildingFootprintFetcher + SpatialJoiner | ~87k footprints en DB, join > 95% |
| 4 | CoverageReporter + loop Hermes mínimo | Gemini decide agentes, 0 errores en 3 steps |
| 5 | Prueba end-to-end setor 310430505000001 | Output parquet con > 90% cobertura |

---

## Decisiones de esta sesión a agregar al PROYECTO_CONTEXTO.md

- Hermes (Nous Research) es el runtime del orquestador. Gemini Flash es el LLM de rutina.
- Los 13 agentes son Hermes Skills ejecutados como Python RPC (zero LLM tokens).
- Gemini solo lee CoverageReports (< 200 tokens) y decide 1 tool call por step.
- Target de costo LLM: < U$D 0.01 por corrida de 1 setor, < U$D 0.50 por corrida VG completa.
- HITL via Telegram ya cubierto por la integración nativa de Hermes.
- Primera prueba: setor censitário `310430505000001` de VG.
