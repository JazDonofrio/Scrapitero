---
name: vg-pipeline-runner
description: "Pipeline VG completo en UNA sola llamada determinista: SmartGIS → BCI (con parseo inline) → BCIParser → EstablecimientoAgrupador. Registra cada paso en surveys.notes, honra el stop del operador y marca completed al final. Preferir SIEMPRE sobre orquestar los pasos VG uno por uno."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, brasil, varzea-grande, pipeline, runner, orquestador, catastro]
    category: scrapitero
---

# VG Pipeline Runner — happy path compilado

Ejecuta el pipeline estándar de Várzea Grande de punta a punta **sin que el LLM
orqueste paso por paso**: una invocación = todos los pasos que entren en el
presupuesto de tiempo. Menos llamadas al modelo, menos contexto, menos cuota.

## Cuándo usar
- **SIEMPRE que haya que relevar una zona de Brasil/VG** (en lugar de llamar
  smartgis-fetcher, varzea-bci-fetcher, bci-parser y establecimiento-agrupador a mano).
- La región ya debe existir con `zone_geojson` y el survey creado.

## Correr el runner

```bash
python3 -m scrapitero.rpc.vg_pipeline_runner <<< '{"region_id":"<REGION_ID>","survey_id":"<SURVEY_ID>"}'
```

## Output

```json
{
  "ok": true,
  "pasos_ejecutados": ["smartgis_fetcher", "varzea_bci_fetcher", "bci_parser", "establecimiento_agrupador"],
  "resumen": {"varzea_bci_fetcher": {"pdfs_descargados": 47, "parcelas_parseadas": 47}},
  "parcial": false,
  "siguiente": null,
  "detenido": false,
  "error": null
}
```

## Protocolo según el resultado

| Resultado | Qué hacer |
|-----------|-----------|
| `ok:true, parcial:false` | Pipeline completo. El runner ya marcó el survey `completed` y registró todos los pasos. Notificar resumen final por Telegram. |
| `ok:true, parcial:true` | **NO es error.** Se agotó el presupuesto (840s default). **Re-invocar con el MISMO input** — continúa desde `siguiente`. Repetir hasta `parcial:false`. Avisar avance por Telegram ("sigo en otra pasada"). |
| `ok:true, detenido:true` | El operador detuvo el survey. Frenar y notificar. |
| `ok:false` | El paso que figura en `resumen` falló; `error` ya viene sellado (`skill: causa`) — relayarlo TAL CUAL por Telegram. |

## Qué hace por adentro
1. `smartgis-fetcher` (re-corre mientras devuelva parcial — acumula cobertura)
2. `varzea-bci-fetcher` (parseo inline: uso/UF/dirección a DB con cada PDF; re-corre mientras parcial)
3. `bci-parser` (red de seguridad idempotente)
4. `establecimiento-agrupador`
5. Marca `completado` + `surveys.status='completed'`

Cada paso queda registrado en `surveys.notes.pasos` (la web lo muestra en vivo).
Los sub-agentes ya notifican su propio avance por Telegram.

## Parámetros
| Campo | Tipo | Default | Descripción |
|-------|------|---------|-------------|
| `region_id` | string | ✅ | Región a relevar |
| `survey_id` | string | ✅ | Survey activo |
| `max_runtime_s` | int | 840 | Presupuesto total; frena con gracia antes del timeout del comando (~900s). 0 = sin límite (NO usar desde Hermes) |
| `max_pasadas` | int | 12 | Tope de re-runs internos de un paso parcial |
| `marcar_completado` | bool | true | Marcar `completed` al terminar todo |
