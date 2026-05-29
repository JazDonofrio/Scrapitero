---
name: relevar-region
description: "Relevamiento de regiones BRASILEÑAS (IBGE/SIDRA). SOLO para Brasil. Activar cuando el usuario pide relevar una región brasileña como Várzea Grande, vg-mt-br u otras ciudades de Brasil. NO usar para Argentina."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, relevamiento, catastro, orquestador, parcelas, brasil]
    category: scrapitero
---

# Relevar Región — Orquestador Scrapitero

Ejecuta el pipeline completo de relevamiento catastral para una región.
El LLM **no procesa datos** — solo lee CoverageReports y decide qué agente correr.

## Cuándo usar
Cuando el usuario dice cosas como:
- "relevá Várzea Grande"
- "actualizá los datos de VG"
- "corré el relevamiento de vg-mt-br"
- "necesito los datos catastrales de Várzea Grande"

## Regiones disponibles
| Nombre | region_id |
|--------|-----------|
| Várzea Grande, MT, Brasil | `vg-mt-br` |

## Protocolo del loop (seguir en orden)

## IMPORTANTE
- **Nunca instalar paquetes.** El venv `/opt/scrapitero/.venv` ya tiene todo instalado.
- Siempre usar el Python del venv directamente: `/opt/scrapitero/.venv/bin/python`
- Siempre cargar el .env: `env $(cat /opt/scrapitero/.env | xargs)`
- Siempre pasar PYTHONPATH: `PYTHONPATH=/opt/scrapitero/src`
- Responder siempre en español.

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
                 {'sid': str(survey_id), 'rid': 'vg-mt-br'})
print(survey_id)
"
```
Guardar el `survey_id` para usarlo en los siguientes pasos.

### Paso 2 — Loop hasta cobertura aceptable

Repetir hasta máximo 20 iteraciones:

**a) Leer estado actual:**
```bash
echo '{"region_id":"vg-mt-br","survey_id":"<SURVEY_ID>"}' | python -m scrapitero.rpc.coverage_reporter
```

**b) Decidir próximo agente según tabla:**

| Condición en CoverageReport | Agente a correr |
|-----------------------------|----------------|
| `setores == 0` | `ibge-census-fetcher` |
| `logradouros_count == 0` (o ausente) | `ibge-logradouros-fetcher` (correr antes de address-resolver) |
| `footprints == 0` | `building-footprint-fetcher` *(próximamente)* |
| `footprints_con_setor / footprints < 0.95` | `spatial-joiner` *(próximamente)* |
| `parcelas_con_direccion / max(footprints,1) < 0.90` | `address-resolver` |
| `parcelas_con_habitantes == 0 y footprints > 0` | `population-estimator` *(próximamente)* |
| `cobertura_habitantes_pct >= 0.95` | → ir a Paso 3 (exportar) |

**c) Correr el agente decidido** usando su skill correspondiente.

**d) Registrar el step en DB:**
```bash
python3 -c "
import uuid, os
from sqlalchemy import text
from scrapitero.db.engine import get_engine
with get_engine().begin() as conn:
    conn.execute(text('''
        INSERT INTO orchestrator_log (log_id, survey_id, step, agent_called, output_resumen)
        VALUES (:lid, :sid, :step, :agent, :output)
    '''), {'lid': uuid.uuid4(), 'sid': '<SURVEY_ID>', 'step': <N>,
           'agent': '<AGENTE>', 'output': '<RESUMEN_JSON>'})
"
```

### Paso 3 — Notificar al usuario
Al terminar (cobertura alcanzada o límite de steps), reportar:
- Parcelas relevadas
- Cobertura de dirección y habitantes
- Steps usados
- Cualquier error encontrado

## Límites
- Máximo 20 steps por corrida
- Si se alcanza el límite: exportar lo disponible y notificar cobertura parcial

## Notas importantes
- Los agentes marcados como "próximamente" aún no están implementados
- Agentes disponibles: `ibge-census-fetcher`, `ibge-logradouros-fetcher`, `address-resolver`
- Cada vez que se corre un agente, **siempre** verificar con `coverage-reporter` después
