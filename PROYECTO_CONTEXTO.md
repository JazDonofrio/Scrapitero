# Plataforma de Relevamiento Catastral — Documento de Contexto del Proyecto

**Versión:** 1.0  
**Fecha:** Mayo 2026  
**Estado:** En definición / desarrollo inicial

---

## 1\. Objetivo del Producto

Construir una **plataforma de agentes** que permita relevar una región geográfica específica, a demanda del cliente, y producir como resultado un dataset estructurado con:

- **Dirección/domicilio** asociado a la parcela: calle, número, complemento, barrio, localidad, municipio, estado, país, código postal
- **Cantidad estimada de habitantes** por parcela (con intervalo de confianza y método)
- **Coordenadas geográficas** (lat/lng) del centroide de la parcela
- Tipología de la parcela: residencial, comercial, mixto, industrial, vacante
- Métricas auxiliares: área de terreno, área construida, # de footprints de edificios, unidades funcionales estimadas

El producto es **multi-región y multi-cliente**: cada cliente define qué región relevar y el sistema ejecuta el relevamiento de forma automatizada o semi-automatizada.

> **Nota crítica sobre habitantes:** "cantidad de habitantes por parcela" **no existe como dato público observado** en ninguna fuente del mundo (privacidad censal). Se produce por **desagregación dasymetrica**: la población oficial del *setor censitário* del IBGE se reparte entre las parcelas del setor usando pesos (área construida, # footprints, unidades estimadas). El output es siempre `(habitantes_estimados, low, high, método, confianza)`, no un número exacto. Esto está acordado con el cliente como parte del producto.

---

## 2\. Caso de Uso Inicial (MVP)

**Región:** Várzea Grande, Mato Grosso, Brasil  
**Tipo de dato buscado:** Una fila por parcela con dirección + habitantes estimados (+ intervalo de confianza) + tipología + geometría

### Datos ya conocidos de Várzea Grande

| Métrica | Valor | Fuente |
| :---- | :---- | :---- |
| Población (Censo 2022\) | 300.078 hab | IBGE |
| Estimación 2025 | 318.922 hab | IBGE |
| Domicilios totales (Censo 2022\) | 126.140 | IBGE |
| — Casas | 92.727 (73,5%) | IBGE |
| — Apartamentos | 5.955 (4,7%) | IBGE |
| — Casas de vila/condominio | 4.268 (3,4%) | IBGE |
| — Cortiços | 320 | IBGE |
| Inmuebles catastrales totales | \~165.000 | SIIG/VG Prefeitura |
| — Con construcción | \~104.000 | SIIG/VG Prefeitura |
| Área territorial | 939 km² | IBGE |
| Bairros | 37 | OpenStreetMap |
| Densidad demográfica | 414 hab/km² | IBGE |

---

## 3\. Fuentes de Datos Gratuitas Validadas

### 3.1 Footprints de Edificios (cobertura Brasil confirmada)

| Fuente | URL | Formato | Licencia | Prioridad |
| :---- | :---- | :---- | :---- | :---- |
| Microsoft Global Building Footprints | github.com/microsoft/GlobalMLBuildingFootprints | GeoJSON | ODbL | ★★★★★ |
| Google Open Buildings V3 | sites.research.google/gr/open-buildings | CSV/GeoParquet | CC-BY | ★★★★★ |
| OpenStreetMap / Overpass API | overpass-turbo.eu | GeoJSON | ODbL | ★★★☆☆ |
| Geofabrik OSM Extract Brasil | download.geofabrik.de/south-america/brazil.html | PBF/SHP | ODbL | ★★★☆☆ |

### 3.2 Estadísticas Oficiales

| Fuente | URL | Contenido | Prioridad |
| :---- | :---- | :---- | :---- |
| IBGE Malha Setores Censitários 2022 | geoftp.ibge.gov.br | SHP/GeoPackage domicilios por setor | ★★★★★ |
| IBGE SIDRA tablas Censo 2022 | sidra.ibge.gov.br/tabela/9596 | CSV/XLSX tipo domicilio por municipio | ★★★★★ |
| IBGE Censo Panorama | censo2022.ibge.gov.br/panorama | Visor interactivo setor censitário | ★★★★☆ |
| IBGE Cidades@ — VG | cidades.ibge.gov.br/brasil/mt/varzea-grande | KPIs del municipio | ★★★★☆ |

### 3.3 Catastro Municipal

| Fuente | URL | Contenido | Prioridad |
| :---- | :---- | :---- | :---- |
| Prefeitura VG — IPTU online | varzeagrande.mt.gov.br/iptu | Consulta por inscripción catastral | ★★★☆☆ |
| Shapefile bairros VG | gismaps.com.br | Límites bairros SHP (GEFAZ 2021\) | ★★★★☆ |
| SIGEF INCRA (rurales) | sigef.incra.gov.br | Parcelas georreferenciadas rurales | ★★★☆☆ |
| SNCR SERPRO | sncr.serpro.gov.br | Catastro rural nacional | ★★☆☆☆ |

### 3.4 Satelital y Población

| Fuente | URL | Contenido | Prioridad |
| :---- | :---- | :---- | :---- |
| Google Earth Engine (gratis investigación) | earthengine.google.com | Open Buildings \+ Sentinel-2 | ★★★★★ |
| Open Buildings Temporal V1 | sites.research.google/gr/open-buildings/temporal | Edificios 2016-2023 anual | ★★★★☆ |
| Overture Maps Foundation | overturemaps.org | Fusión MS+OSM+otros en Parquet | ★★★★☆ |
| WorldPop grilla población | hub.worldpop.org | Raster 100m personas/pixel | ★★★★☆ |

### 3.5 APIs de Google Maps (con free tier)

- **Geocoding API** — dirección → lat/lng \+ building outline ($5/1000 req, $200 crédito mensual gratis)  
- **Places API Nearby Search** — POIs y establecimientos por área  
- **Solar API** — footprint de edificio por coordenada (cobertura BASE disponible para Brasil)

---

## 4\. Estrategia de Relevamiento — Orden de Ataque

PASO 1 — Datos macro (sin costo, ya disponibles)

  └─ IBGE Setores Censitários SHP \+ SIDRA tablas

     → baseline estadístico por setor

PASO 2 — Footprints masivos (sin costo)

  └─ Microsoft Building Footprints \+ Google Open Buildings V3

     → polígono de cada edificio con área y coordenadas

PASO 3 — Enriquecimiento de direcciones

  └─ OpenStreetMap / Overpass API

     → calle, número, tipo de uso donde exista

PASO 4 — Geocodificación y validación

  └─ Geocoding API de Google (free tier)

     → lat/lng preciso \+ building outline para muestra

PASO 5 — Clasificación de tipo de unidad

  └─ Agente de clasificación (ML o reglas)

     → casa / depto / local comercial / etc.

PASO 6 — Validación manual de muestra

  └─ Street View \+ inspección visual

     → control de calidad por bairro

---

## 5\. Modelo de Datos — Output Esperado

**Unidad atómica del dataset:** la **parcela** (una fila por parcela catastral). Si una parcela tiene un edificio de 100 deptos, es **una sola fila** con la dirección base y los habitantes totales estimados del edificio. Las tablas `edificios` y `unidades_funcionales` existen internamente para mejorar la estimación, pero no son output.

Cada registro del dataset final tiene:

```jsonc
{
  // Identificación
  "parcela_id": "uuid",              // UUID v5 determinístico por (region, geometry_hash)
  "snapshot_id": "uuid",             // a qué corrida pertenece este registro
  "region_id": "string",             // "vg-mt-br"

  // Geometría
  "geometry_wkt": "POLYGON(...)",    // polígono de la parcela en WGS84
  "centroid_lat": 0.0,
  "centroid_lng": 0.0,
  "area_m2_terreno": 0.0,
  "area_m2_construida": 0.0,

  // Dirección
  "calle": "string",
  "numero": "string",
  "complemento": "string",           // si la fuente lo provee
  "barrio": "string",
  "localidad": "string",
  "municipio": "string",
  "estado_provincia": "string",
  "pais": "string",
  "codigo_postal": "string",
  "direccion_source": "string",      // "catastro" | "osm" | "nominatim" | "google_geocode"
  "direccion_confidence": 0.0,       // 0.0 a 1.0

  // Habitantes (ESTIMADO - ver nota crítica en sección 1)
  "habitantes_estimados": 0.0,
  "habitantes_low": 0.0,             // banda inferior intervalo de confianza
  "habitantes_high": 0.0,            // banda superior
  "habitantes_metodo": "string",     // "ibge_dasymetric" | "worldpop_zonal" | "count_x_avg_hogar"
  "habitantes_confidence": 0.0,

  // Tipología y métricas auxiliares
  "uso_principal": "string",         // "residencial" | "comercial" | "mixto" | "industrial" | "vacante"
  "footprints_count": 0,             // # de edificios sobre la parcela
  "pisos_estimados_max": 0,
  "unidades_funcionales_estimadas": 0,

  // Vínculo censal
  "setor_censitario_id": "string",   // ID del setor IBGE que contiene la parcela
  "fuente_parcela": "string",        // "catastro" | "footprints_proxy" | "osm_landuse"

  // Auditoría
  "fecha_relevamiento": "2026-05-17",
  "validado_manual": false
}
```

**Tablas internas** (no exportadas, asisten a la estimación):

- `edificios(edificio_id, parcela_id, snapshot_id, footprint, area, pisos_est, source)`
- `unidades_funcionales(unidad_id, edificio_id, piso, depto, tipo)` — solo se popula cuando hay fuente con ese nivel de detalle
- `setores_censitarios(setor_id, region_id, geometry, pop_total, domicilios_total, ...)` — referencia IBGE

---

## 6\. Arquitectura de Agentes (Definida)

**Patrón:** Orquestador LLM con tool-use sobre un toolkit de agentes-código.

- **Orquestador = LLM (Claude sonnet)**. Decide qué hacer, en qué orden, con qué fuente, cuándo parar. No procesa data. No toca filas. Solo dirige.
- **Agentes especializados = código Python determinístico**. Cada uno tiene UNA responsabilidad atómica e interfaz Pydantic tipada (input → output). El LLM los conoce como *tools*.
- **Persistencia transversal = Postgres+PostGIS**. Todos los agentes leen y escriben ahí. El orquestador-LLM nunca recibe datasets enteros, solo conteos, muestras y métricas (crítico para no quemar tokens).

```
                  ┌──────────────────────────────────┐
                  │   Orquestador LLM (Claude)       │
                  │   - loop tool-use                │
                  │   - lee CoverageReports          │
                  │   - decide próximo agente        │
                  └────┬───────────────────┬─────────┘
                       │ tool_call         │ tool_result (resumen)
                       ▼                   ▲
       ┌───────────────┴───────────────────┴────────────────┐
       │              Toolkit (todos código)                │
       │                                                    │
       │  Adquisición                  Transformación       │
       │  ─ ParcelFetcher              ─ SpatialJoiner      │
       │  ─ BuildingFootprintFetcher   ─ AddressResolver    │
       │  ─ OSMFetcher                 ─ UnitCounter        │
       │  ─ IBGECensusFetcher          ─ PopulationEstim.   │
       │  ─ WorldPopFetcher                                 │
       │  ─ GeocoderAgent              Calidad / Salida     │
       │                               ─ CoverageReporter   │
       │                               ─ Validator          │
       │                               ─ Exporter           │
       └────────────────────┬───────────────────────────────┘
                            │ lee/escribe
                            ▼
                  ┌──────────────────────┐
                  │  Postgres + PostGIS  │
                  │  (un snapshot)       │
                  └──────────────────────┘
```

El **catálogo completo de agentes** (responsabilidad atómica, input/output, fuente externa que usa) está en el **Anexo B**.

**Auditabilidad:** cada step del loop del orquestador se logea en la tabla `orchestrator_log` con `agent_called`, `input_resumido`, `output_resumido`, `razonamiento_llm`, `tokens`, `costo`. Cada corrida es reproducible y revisable. Se usa `temperature=0` para minimizar varianza.

**Límites duros por corrida** (configurables): max 50 steps, max U$D 5 en tokens LLM, max U$D 2 en APIs pagas (Google Geocoding). Cuando se rompe un límite, el orquestador exporta lo que tenga y declara cobertura parcial.

**Ejemplo de tour del orquestador** sobre una región (ver Anexo C para el log paso a paso de una corrida ficticia de VG).

---

## 7\. Decisiones Tomadas

- [x] **Stack tecnológico:** Python 3.11 + GeoPandas + Postgres+PostGIS + Anthropic SDK (detalle completo en Anexo A)
- [x] **Fuentes primarias para VG:** Microsoft Building Footprints + IBGE Setores Censitários (orden de ataque acordado)
- [x] **Agotar fuentes gratuitas antes de consumir APIs pagas**
- [x] **Google Maps API se evalúa después de agotar fuentes gratuitas**
- [x] **Arquitectura multi-región:** sistema parametrizado por `region_id + bbox`. Cada corrida = un `snapshot_id` versionado. Estructura preparada para refresh periódico sin reescribir.
- [x] **Persistencia del dataset:** PostgreSQL 16 + PostGIS 3.4 corriendo en Docker Compose local. SQLAlchemy 2.0 + GeoAlchemy2 como ORM. Alembic para migraciones.
- [x] **Patrón de orquestación:** Orquestador LLM (Claude sonnet) con tool-use sobre toolkit de agentes-código determinísticos. NO pipeline lineal en código puro, NO agentes LLM múltiples.
- [x] **Unidad atómica del output:** la **parcela** (1 fila por parcela). Edificios y unidades funcionales son tablas internas que asisten la estimación, no output.
- [x] **Habitantes:** valor **estimado** con intervalo de confianza y método declarado. No se promete número exacto auditable a nivel parcela (no existe esa fuente).
- [x] **Modo de entrega al cliente:** archivo descargable (CSV / GeoJSON / GeoParquet). Sin API ni dashboard en MVP. Estructura del repo deja la puerta abierta para FastAPI a futuro.
- [x] **Cadencia:** MVP one-shot. Arquitectura con `snapshot_id` ya prepara refresh periódico cuando se decida activarlo.

---

## 8\. Próximos Pasos Inmediatos

Hecho en sesión 2026-05-17 (ver Anexo D — Changelog):

- [x] Revisar código existente y catalogar qué se reutiliza
- [x] Definir modelo de datos del output
- [x] Definir stack tecnológico
- [x] Definir arquitectura (orquestador LLM + agentes-código)

Próxima sesión:

1. **Definir contratos Pydantic** detallados de los 13 agentes del Anexo B (input/output schema de cada uno).
2. **Escribir migración inicial Alembic** con las tablas: `regions`, `surveys`, `parcelas`, `edificios`, `unidades_funcionales`, `setores_censitarios`, `orchestrator_log`.
3. **Crear esqueleto del repo**: `pyproject.toml`, `docker-compose.yml`, estructura de carpetas (`src/scrapitero/...`), `.env.example`, mover `notifier.py` y `utils.py` a su nuevo lugar, borrar `models.py` y `storage.py` viejos.
4. **Implementar el primer agente end-to-end**: `IBGECensusFetcher` (es el más simple, no requiere parseo geo complejo, y desbloquea la estimación de habitantes que es la pieza más diferenciadora).
5. **Implementar el loop básico del orquestador** con 2-3 tools registradas y correr una prueba acotada sobre un setor censitário de VG.
6. **Bajar Microsoft Building Footprints** del quadkey de VG (Paso 2 del orden de ataque) e implementar `BuildingFootprintFetcher`.

---

## 9\. Preguntas Abiertas

Resueltas en sesión 2026-05-17:

- ✅ **¿El cliente recibe el dataset como archivo o a través de una API/dashboard?** → Archivo (CSV/GeoJSON/GeoParquet). Sin API en MVP.
- ✅ **¿Se necesita actualización periódica del relevamiento o es puntual?** → MVP one-shot. Arquitectura preparada para refresh periódico (snapshots versionados) cuando se decida activarlo.

Pendientes:

- ¿Qué nivel de precisión mínimo es aceptable para coordenadas? (rooftop / parcel / interpolated — afecta umbral de cobertura aceptable)
- ¿El sistema debe manejar también zonas rurales o solo urbanas? (define si sumamos INCRA/SIGEF como fuente)
- ¿Hay requerimientos de privacidad/datos sobre los titulares de inmuebles? (define qué se persiste de la consulta IPTU)
- ¿Cuál es el umbral mínimo de `direccion_confidence` y `habitantes_confidence` para considerar una parcela "válida" en el export?
- ¿Tope de presupuesto en U$D por relevamiento (LLM + APIs pagas)? Los límites por corrida hoy están seteados en U$D 5 LLM + U$D 2 APIs como placeholder.

---

## 10\. Instrucciones para Claude en este Proyecto

Sos el asistente técnico principal de este proyecto de plataforma de relevamiento catastral.

**Contexto que siempre tenés presente:**

- El objetivo es una plataforma multi-región, no un script one-shot para VG  
- Várzea Grande (MT, Brasil) es el caso de uso MVP  
- Hay código existente en la carpeta del proyecto — revisarlo antes de proponer algo nuevo  
- La prioridad es agotar fuentes gratuitas antes de APIs pagas  
- El output final es un dataset geoestructurado con unidades por dirección \+ coordenadas

**Cómo trabajamos:**

- Al final de cada sesión, actualizamos este documento con las decisiones tomadas  
- El código se versiona en la carpeta del proyecto  
- Antes de escribir código nuevo, revisamos si algo ya existe o puede reutilizarse  
- Decisiones de arquitectura se documentan aquí antes de implementar

**Tu stack de referencia (definido — ver Anexo A):**

- Python 3.11 + GeoPandas + pyogrio + Shapely + pyproj
- PostgreSQL 16 + PostGIS 3.4 (Docker) + SQLAlchemy 2.0 + GeoAlchemy2 + Alembic
- DuckDB con spatial extension para procesamiento intermedio de archivos pesados
- Anthropic SDK (Claude sonnet) con tool-use para el orquestador
- Typer + Rich + Loguru para CLI y observabilidad

---

## Anexo A — Stack Técnico Definido

| Capa | Herramienta | Justificación |
|---|---|---|
| Lenguaje | Python 3.11 | Alineado con el venv existente del proyecto. Ecosistema geoespacial maduro. |
| Deps & lockfile | `pyproject.toml` + `uv` | Reemplaza `requirements.txt`. Reproducible, rápido. |
| Geo vectorial | GeoPandas + Shapely | Estándar de facto. |
| Geo I/O | pyogrio | 10-50x más rápido que Fiona para shapefiles IBGE. |
| Reproyecciones | pyproj | WGS84 ↔ Web Mercator ↔ SIRGAS 2000 (Brasil). |
| Quadkeys | mercantile | Necesario para descargar MS Building Footprints. |
| Raster | rasterio | Solo si llegamos a WorldPop. |
| DB | PostgreSQL 16 + PostGIS 3.4 (Docker Compose) | Queries espaciales complejas, snapshots versionados, escalable. |
| ORM | SQLAlchemy 2.0 + GeoAlchemy2 | Tipado, migrable, geo-aware. |
| Migraciones | Alembic | Versionado de schema. |
| Driver DB | psycopg 3 | Driver moderno de Postgres. |
| Procesamiento intermedio | DuckDB + spatial ext. | Para joins espaciales sobre Parquet pesados de MS/Google Buildings antes de cargar a Postgres. |
| LLM (orquestador) | Anthropic SDK + Claude sonnet (default) / opus (fallback) | Tool-use loop manual, más control que Claude Agent SDK. |
| Structured output | `instructor` o `pydantic-ai` | Tipado de respuestas LLM. |
| HTTP + caché | httpx + hishel | Caché en disco para descargas pesadas. |
| CLI | Typer | Declarativo, tipado. |
| Logging | Loguru | Estructurado, a archivo y stdout. |
| HITL | python-telegram-bot | Mantener código existente de `notifier.py`. |
| Testing | pytest + pytest-asyncio | Estándar. |
| Lint + format | ruff | Reemplaza black + isort + flake8. |
| Type check | mypy strict en `models/` y `pipeline/` | Las zonas críticas de tipos. |

**Lo que dejamos fuera del MVP** (y por qué):

- **Prefect / Dagster / Airflow:** overkill para pipeline lineal. Si en 6 meses se necesita retries distribuidos y observabilidad fuerte, se migra.
- **FastAPI:** sin API en MVP (entrega por archivo). Estructura del repo lo deja preparado.
- **Frontend:** idem.
- **Claude Agent SDK:** uso loop tool-use manual para mayor auditabilidad y menor overhead.

---

## Anexo B — Catálogo de Agentes Especializados

Cada agente es una clase Python con interfaz Pydantic tipada (`run(input: I) -> O`). El orquestador-LLM los conoce como *tools* registradas en el loop de tool-use.

### Adquisición (hablan con fuentes externas)

| # | Agente | Responsabilidad atómica | Fuente externa |
|---|---|---|---|
| 1 | `ParcelFetcher` | Descarga geometrías de parcelas catastrales para un bbox | Catastro municipal (drivers por jurisdicción) |
| 2 | `BuildingFootprintFetcher` | Descarga footprints de edificios para un bbox | Microsoft Global Building Footprints / Google Open Buildings V3 |
| 3 | `OSMFetcher` | Query Overpass parametrizada (`building`, `addr:*`, `landuse`, etc.) | OpenStreetMap / Overpass API |
| 4 | `IBGECensusFetcher` | Descarga setores censitários + tablas SIDRA para un municipio | IBGE (Malha + SIDRA) |
| 5 | `WorldPopFetcher` | Descarga raster 100m de población para el bbox | WorldPop |
| 6 | `GeocoderAgent` | Reverse-geocoding desde centroide/footprint | Nominatim (default) / Google Geocoding (fallback pago) |

### Transformación (sin red, puros datos)

| # | Agente | Responsabilidad atómica |
|---|---|---|
| 7 | `SpatialJoiner` | Joins espaciales entre cualquier par de capas (intersects, within, contains) |
| 8 | `AddressResolver` | Para una parcela, resuelve dirección eligiendo la mejor fuente por jerarquía configurable. Reporta `(direccion, source, confidence)`. |
| 9 | `UnitCounter` | Estima cantidad de unidades funcionales por parcela según altura/área de footprints (regla calibrable: `unidades = round(area_construida / 80m²)` por default) |
| 10 | `PopulationEstimator` | **Agente clave.** Desagregación dasymetrica: reparte la población del setor IBGE entre sus parcelas usando pesos (área construida, # unidades estimadas, footprint count). Devuelve `(habitantes, low, high, método, confidence)`. |

### Calidad y salida

| # | Agente | Responsabilidad atómica |
|---|---|---|
| 11 | `CoverageReporter` | Lee snapshot actual y reporta `% parcelas con geometría`, `% con dirección`, `% con población estimada`, `desvío vs total IBGE`. El orquestador decide en base a esto si seguir o parar. |
| 12 | `Validator` | Chequeos duros: suma habitantes por setor ≈ total IBGE, geometrías válidas, sin duplicados, FKs íntegras. |
| 13 | `Exporter` | Emite CSV / GeoJSON / GeoParquet del snapshot pedido. |

---

## Anexo C — Ejemplo de tour del Orquestador (corrida ficticia sobre VG)

Esto ilustra el patrón de toma de decisiones del orquestador-LLM. **No es código**, es el log esperado de una corrida.

```
─── SNAPSHOT vg-mt-br / 2026-05-17 ──────────────────────────────────

[step 1]  LLM  → CoverageReporter()
          ←  { parcelas: 0, direcciones: 0, habitantes: 0 }
          razonamiento: "Snapshot vacío, arranco por geometría de parcelas."

[step 2]  LLM  → ParcelFetcher(region="VG", source="prefeitura_vg")
          ←  { ok: false, error: "endpoint requiere CAPTCHA" }
          razonamiento: "Catastro no accesible. Voy a footprints como proxy."

[step 3]  LLM  → BuildingFootprintFetcher(bbox=VG, source="ms_global")
          ←  { ok: true, rows: 87420, sample: [...3 filas...] }

[step 4]  LLM  → IBGECensusFetcher(municipio="VG")
          ←  { setores: 412, pop_total: 318922, sample: [...] }

[step 5]  LLM  → SpatialJoiner(left="footprints", right="setores", op="within")
          ←  { matched: 86891, unmatched: 529 }

[step 6]  LLM  → OSMFetcher(bbox=VG, filter="building+addr:*")
          ←  { rows: 12380 }   ← cobertura ~14% para VG

[step 7]  LLM  → AddressResolver(strategy="osm_first, then_reverse_geocode")
          ←  { resolved: 12380 (osm), remaining: 74511 }
          razonamiento: "74k parcelas sin dirección. Geocoding pago en
                         Google son ~U$D 372. Tengo presupuesto U$D 2.
                         Voy con Nominatim gratis aunque baje confianza."

[step 8]  LLM  → GeocoderAgent(driver="nominatim", batch=74511)
          ←  { resolved: 71203, conf_avg: 0.62 }

[step 9]  LLM  → UnitCounter()
          ←  { unidades_total_estimadas: 124880 }

[step 10] LLM  → PopulationEstimator(weights=["area_construida","unidades_est"])
          ←  { habitantes_asignados: 318922, parcelas_cubiertas: 86891 }

[step 11] LLM  → Validator()
          ←  { ok: true, suma_pop_vs_ibge: 0.00%, geom_invalidas: 0 }

[step 12] LLM  → CoverageReporter()
          ←  { parcelas: 86891, direcciones: 96%, habitantes: 100% }
          razonamiento: "Cobertura aceptable. Exporto."

[step 13] LLM  → Exporter(format="geoparquet")
          ←  { path: "data/processed/vg-mt-br/2026-05-17/parcelas.parquet" }

─── FIN ─ 13 steps, 8420 tokens, U$D 0.04 LLM, U$D 0 APIs pagas ────
```

---

## Anexo D — Changelog

### 2026-05-17 — Sesión de arquitectura

**Decisiones tomadas:**

- Pivote conceptual: la arquitectura es **agente LLM orquestador + toolkit de agentes-código determinísticos**, no pipeline lineal con LLM puntual.
- Unidad atómica del output: **parcela** (1 fila por parcela).
- Habitantes: **estimado por desagregación dasymetrica** con intervalo de confianza. No número exacto.
- Stack: Python 3.11 + GeoPandas + Postgres+PostGIS + Anthropic SDK + Typer.
- Persistencia: PostgreSQL 16 + PostGIS 3.4 en Docker Compose. SQLAlchemy + Alembic.
- Entrega al cliente: archivo descargable (CSV/GeoJSON/GeoParquet). Sin API ni dashboard en MVP.
- Cadencia: MVP one-shot, arquitectura preparada para refresh con `snapshot_id`.
- Catálogo de 13 agentes especializados definido (Anexo B).

**Código existente — destino:**

- `src/utils.py` (UUID v5 determinístico) → **se reutiliza** para `parcela_id`.
- `src/notifier.py` (Telegram bot) → **se mueve** a `src/scrapitero/notifier.py`, se mantiene.
- `src/storage.py` (JSON simulado) → **se borra**, reemplazado por SQLAlchemy + repo pattern.
- `src/models.py` (Parcela/Vivienda argentinos) → **se borra**, se rehace según sección 5.
- Archivos mencionados en README pero inexistentes (`cadastre.py`, `supplies.py`, `orchestrator.py`) → no se crean, la nueva arquitectura los reemplaza.

**Pendiente para próxima sesión:** ver sección 8.


