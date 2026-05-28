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

**IMPORTANTE: el venv ya está instalado. NUNCA corras pip install ni uv install.**
Usar siempre este patrón exacto (el PYTHONPATH doble es obligatorio):

```bash
echo '<JSON_INPUT>' | \
  env $(cat /opt/scrapitero/.env | xargs) \
  PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \
  python3 -m scrapitero.rpc.<nombre_agente>
```

## Idioma
Siempre responder en español. Todos los mensajes, reportes y notificaciones en español.
