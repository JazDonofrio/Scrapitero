---
name: vg-pipeline-runner
description: "Pipeline VG completo en UNA sola llamada determinista: SmartGIS → BCI (con parseo inline) → BCIParser → EstablecimientoAgrupador → shoppings y categorías → establecimientos observados (censo 2022 + Overture, incluidos los domicilios) → hoteles (que aplican solas las reglas de ubicación y de especial) → footprints y altura. Registra cada paso en surveys.notes, honra el stop del operador y marca completed al final. Preferir SIEMPRE sobre orquestar los pasos VG uno por uno."
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
- **Relevamientos parciales (sub-zona):** si el survey tiene `surveys.subzona_geojson`
  (creado desde la web con "🔁 Sub-zona"), todos los pasos filtran automáticamente por
  ese polígono en vez de la zona completa — no hay que pasar nada extra, solo el
  `survey_id` correcto. Los PDFs BCI compartidos se reutilizan. El survey grande
  anterior queda intacto (sirve después para la comparativa de la web).

## Correr el runner

```bash
python3 -m scrapitero.rpc.vg_pipeline_runner <<< '{"region_id":"<REGION_ID>","survey_id":"<SURVEY_ID>"}'
```

## Output

```json
{
  "ok": true,
  "pasos_ejecutados": ["smartgis_fetcher", "varzea_bci_fetcher", "bci_parser", "establecimiento_agrupador", "establecimientos_observados", "hotel_fetcher", "footprint_fetcher"],
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
4. `scope-calles-filter` (sólo en modo actualización por calle+rango) y `establecimiento-agrupador`
5. `shopping-fetcher` (OSM; Google es pago y se corre aparte) y `parcela-categoria`
6. `establecimientos-observados`: qué negocio se ve HOY en cada lote (censo 2022 + Overture) y
   los **domicilios del censo**, que son el control independiente del conteo de viviendas
7. `country-fetcher` (barrios cerrados) y `hotel-fetcher`, que al final aplica solo
   **`hotel-reglas`**: ubica el hotel por consenso de pines y deja el lote como especial con sus
   habitaciones (o PENSÃO si es una pousada con kitnets)
8. `hotel-habitaciones-llm` (opcional, pago por hotel) y las capas de revisión:
   `footprint-fetcher` y `altura-fetcher`
9. Marca `completado` + `surveys.status='completed'`

**No incluye `incidencias-reporter`**: correlo después de terminar, para dejarle al operador la
lista de lo que hay que mirar (incluida la diferencia entre la ficha y el censo).

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
