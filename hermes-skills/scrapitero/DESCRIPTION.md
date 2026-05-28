---
name: scrapitero
description: "Plataforma de relevamiento catastral geoestructurado. Relevá parcelas, edificios, habitantes estimados y direcciones de cualquier región geográfica de forma autónoma."
---

# Scrapitero — Relevamiento Catastral

Skills para operar la plataforma de relevamiento catastral Scrapitero.
El sistema usa agentes Python determinísticos orquestados por el LLM.

**Regla fundamental:** nunca proceses datos crudos vos mismo.
Siempre usá los agentes RPC y leé solo sus outputs resumidos (JSON).

## Proyecto
- Código: `/opt/scrapitero/`
- Venv: `/opt/scrapitero/.venv`
- DB: PostgreSQL+PostGIS en Docker (`scrapitero_db`)

## Patrón de invocación de todos los agentes
```bash
cd /opt/scrapitero
source .venv/bin/activate && export $(cat .env | xargs)
echo '<JSON_INPUT>' | python -m scrapitero.rpc.<nombre_agente>
```
