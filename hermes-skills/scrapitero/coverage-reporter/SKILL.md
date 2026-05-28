---
name: coverage-reporter
description: "Reporta el estado actual de cobertura de un survey: cuántas parcelas, edificios, direcciones y habitantes estimados hay en la DB. Úsalo al inicio de cada loop para decidir qué agente correr a continuación."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, catastro, relevamiento, cobertura]
    category: scrapitero
---

# Coverage Reporter

Devuelve un JSON compacto con el estado del relevamiento activo.
**Siempre corré esto primero** antes de decidir qué agente ejecutar.

## Cuándo usar
- Al inicio de cualquier loop de relevamiento
- Para decidir qué agente ejecutar a continuación
- Para saber si el relevamiento está completo

## Comando
**No instalar nada. El venv ya está listo.**
```bash
echo '{"region_id":"vg-mt-br","survey_id":"<SURVEY_ID>"}' | \
  env $(cat /opt/scrapitero/.env | xargs) \
  PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \
  python3 -m scrapitero.rpc.coverage_reporter
```

Si no hay survey activo todavía, omitir `survey_id`:
```bash
echo '{"region_id":"vg-mt-br"}' | \
  env $(cat /opt/scrapitero/.env | xargs) \
  PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \
  python3 -m scrapitero.rpc.coverage_reporter
```

## Output esperado
```json
{
  "survey_id": "uuid",
  "step": 3,
  "setores": 412,
  "pop_total_ibge": 318922,
  "footprints": 87420,
  "footprints_con_setor": 86891,
  "parcelas": 86891,
  "parcelas_con_direccion": 71203,
  "parcelas_con_habitantes": 86891,
  "cobertura_footprints_pct": 0.994,
  "cobertura_direccion_pct": 0.815,
  "cobertura_habitantes_pct": 1.0,
  "suma_hab_vs_ibge_delta_pct": 0.003,
  "errores": []
}
```

## Lógica de decisión basada en el output

| Condición | Siguiente agente |
|-----------|-----------------|
| `setores == 0` | `ibge-census-fetcher` |
| `footprints == 0` | `building-footprint-fetcher` (aún no implementado) |
| `footprints_con_setor / footprints < 0.95` | `spatial-joiner` (aún no implementado) |
| `parcelas_con_direccion / footprints < 0.90` | `address-resolver` (aún no implementado) |
| `parcelas_con_habitantes == 0` | `population-estimator` (aún no implementado) |
| `cobertura_habitantes_pct > 0.95 y delta < 0.02` | `exporter` (aún no implementado) |
| `step > 20` | exportar con cobertura parcial y notificar |
