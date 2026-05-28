---
name: relevar-manzana-ar
description: "Relevamiento catastral de manzanas en Argentina (Buenos Aires Province). Activar cuando el usuario menciona Partido, Circunscripción, Sección, Manzana, Ituzaingó, ARBA, o cualquier localidad argentina. USA ARBA + OSM + Google Maps."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, argentina, manzana, catastro, arba, ituzaingo, relevamiento]
    category: scrapitero
---

# Relevar Manzana Argentina

Ejecuta el pipeline de relevamiento para una manzana del catastro de Buenos Aires Province.
El LLM **no procesa datos** — solo lee JSONs y decide qué agente correr.

## Cuándo usar
Cuando el usuario menciona:
- "relevá la manzana X"
- "partido 136, circunscripción 2, sección C, manzana 184"
- "relevá Ituzaingó"
- Cualquier combinación de Partido/Circunscripción/Sección/Manzana

## IMPORTANTE
- **Nunca instalar paquetes.** El venv ya está listo.
- Siempre cargar el .env: `env $(cat /opt/scrapitero/.env | xargs)`
- Siempre pasar PYTHONPATH: `PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src`
- Responder siempre en español.

## Pipeline completo

### Paso 1 — Crear survey
```bash
env $(cat /opt/scrapitero/.env | xargs) \
PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \
python3 -c "
import uuid
from sqlalchemy import text
from scrapitero.db.engine import get_engine
survey_id = uuid.uuid4()
with get_engine().begin() as conn:
    conn.execute(text(\"INSERT INTO surveys (survey_id, region_id) VALUES (:sid, :rid)\"),
                 {'sid': str(survey_id), 'rid': 'ituzaingo-ba-ar'})
print(survey_id)
"
```
Guardar el `survey_id` para los pasos siguientes.

### Paso 2 — Cargar parcelas ARBA

Intentar primero con el WFS público (no requiere autenticación):
```bash
echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>","partido_id":"136","circunscripcion":"2","seccion":"C","manzana":"184"}' | \
  env $(cat /opt/scrapitero/.env | xargs) \
  PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \
  python3 -m scrapitero.rpc.arba_cadastral_fetcher
```

**Si el WFS falla o devuelve 0 parcelas**, usar el portal Carto (requiere sesión):
```bash
echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>","partido_id":"136","circunscripcion":"2","seccion":"C","manzana":"184"}' | \
  env $(cat /opt/scrapitero/.env | xargs) \
  PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \
  python3 -m scrapitero.rpc.arba_carto_fetcher
```

**Si el output del Carto tiene `"needs_cookies": true`:**
Usar el skill `arba-carto-fetcher` — contiene el protocolo completo para pedir
el JSESSIONID al usuario por Telegram y reintentar.

Ajustar partido_id, circunscripcion, seccion y manzana según lo que pidió el usuario.

### Paso 3 — Ver estado
```bash
echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>"}' | \
  env $(cat /opt/scrapitero/.env | xargs) \
  PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \
  python3 -m scrapitero.rpc.coverage_reporter
```

### Paso 4 — Cargar edificios OSM
Si `parcelas > 0` y `footprints == 0`:
```bash
echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>"}' | \
  env $(cat /opt/scrapitero/.env | xargs) \
  PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \
  python3 -m scrapitero.rpc.osm_building_fetcher
```

### Paso 5 — Resolver direcciones
Si `parcelas_con_direccion / max(parcelas,1) < 0.90`:
```bash
echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>","batch_size":100}' | \
  env $(cat /opt/scrapitero/.env | xargs) \
  PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \
  python3 -m scrapitero.rpc.address_resolver
```

### Paso 6 — Verificar cobertura final
Repetir el `coverage_reporter` del Paso 3 y reportar al usuario:
- Parcelas relevadas
- Edificios encontrados
- Cobertura de dirección (%)
- Cualquier error

## Tabla de decisión
| Condición en CoverageReport | Acción |
|-----------------------------|--------|
| `parcelas == 0` | Correr `arba-cadastral-fetcher`; si falla → `arba-carto-fetcher` |
| `footprints == 0` | Correr `osm-building-fetcher` |
| `parcelas_con_direccion / max(parcelas,1) < 0.90` | Correr `address-resolver` |
| `parcelas_con_direccion / max(parcelas,1) >= 0.90` | Reportar éxito al usuario |
