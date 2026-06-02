---
name: relevar-zona
description: "Orquesta el relevamiento completo de una zona de forma autónoma. El LLM evalúa el estado actual con coverage-reporter y elige qué skills ejecutar para lograr el mejor relevamiento posible en el menor tiempo."
version: 2.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, pipeline, orquestacion, autonomo]
    category: scrapitero
---

# Relevar Zona — Orquestador Autónomo

Tu objetivo es **lograr el relevamiento más completo posible de la zona en el menor tiempo**.
No seguís un guión fijo — evaluás el estado actual y elegís las skills más efectivas.

## Reglas fundamentales

1. **Siempre empezá con `coverage-reporter`** para saber qué hay y qué falta.
2. **Notificá por Telegram** al inicio, después de cada skill, y al finalizar.
3. **Registrá cada paso** con `survey-step-update` para que la web UI refleje el estado.
4. **Paralelizá cuando sea posible** — si una tarea no depende de otra, lanzala en background (`&`).
5. **Ante un error, reintentá** hasta 3 veces antes de reportar falla y continuar con lo que puedas.
6. **Ante una decisión que no podés tomar solo**, pedíla por Telegram y esperá respuesta.
7. **Verificá `should_stop`** en cada llamada a `survey-step-update` — si es `true`, detenete.

---

## Paso 1 — Diagnóstico inicial

```bash
python3 -m scrapitero.rpc.coverage_reporter <<< '{"region_id":"{region_id}"}'
```

Con el output decidís qué ejecutar:

| Condición | Skill a usar |
|---|---|
| `parcelas == 0` y país BRA | `smartgis-fetcher` |
| `parcelas == 0` y país AR y provincia Buenos Aires | `arba-carto-fetcher` (si JSESSIONID disponible) o `arba-cadastral-fetcher` |
| `parcelas == 0` y país AR y provincia Salta | `salta-catastro-fetcher` |
| `parcelas > 0` y Salta interior y `uso_principal` null | `salta-registro-fetcher` (marca rural/club de campo) |
| `parcelas > 0` y Salta Capital y `uso_principal` null | `salta-zonificacion-fetcher` (uso urbano por CPUA) |
| `parcelas > 0` y Salta Capital, detectar baldíos | `salta-rentas-fetcher` (valorEdificado≈0 → vacante) |
| `parcelas == 0` y otro país | `osm-building-fetcher` |
| `parcelas > 0` y hay cca_codes sin PDF | `varzea-bci-fetcher` |
| PDFs descargados pero parcelas sin `uso_principal` | `bci-parser` |
| `cobertura_direccion_pct < 0.90` | `address-resolver` |
| `logradouros_count == 0` y BRA | `ibge-logradouros-fetcher` antes de address-resolver |

Podés ejecutar varios en secuencia o en paralelo según las dependencias.

---

## Paralelismo

Cuando dos skills son independientes, lanzalas en background con `&` y esperá ambas con `wait`:

```bash
# Ejemplo: BCI y address-resolver son independientes si ya hay parcelas
echo '{"region_id":"...","survey_id":"..."}' |
  python3 -m scrapitero.rpc.varzea_bci_fetcher > /tmp/bci_out.json &
PID_BCI=$!

echo '{"region_id":"...","survey_id":"...","batch_size":200}' |
  python3 -m scrapitero.rpc.address_resolver > /tmp/addr_out.json &
PID_ADDR=$!

wait $PID_BCI $PID_ADDR
cat /tmp/bci_out.json
cat /tmp/addr_out.json
```

**Dependencias reales:**
- BCI depende de SmartGIS (necesita `cca_code` para descargar PDFs)
- Parser depende de BCI (necesita los PDFs descargados)
- Address resolver es independiente de BCI/Parser
- IBGELogradouros es independiente de SmartGIS/BCI

---

## Patrón de invocación de cada skill

```bash
python3 -m scrapitero.rpc.<nombre> <<< '<JSON>'
```

Skills disponibles:
  - `scrapitero.rpc.smartgis_fetcher` → `{"region_id":"...","survey_id":"..."}`
  - `scrapitero.rpc.varzea_bci_fetcher` → `{"region_id":"...","survey_id":"..."}`
- `scrapitero.rpc.bci_parser` → `{"region_id":"...","survey_id":"..."}`
- `scrapitero.rpc.osm_building_fetcher` → `{"region_id":"...","survey_id":"...","bbox_south":...,"bbox_west":...,"bbox_north":...,"bbox_east":...}`
- `scrapitero.rpc.arba_carto_fetcher` → ver skill arba-carto-fetcher
- `scrapitero.rpc.arba_cadastral_fetcher` → `{"region_id":"...","survey_id":"..."}`
- `scrapitero.rpc.salta_catastro_fetcher` → `{"region_id":"...","survey_id":"...","fuente":"auto"}` (fuente: "auto"|"capital"|"provincia")
- `scrapitero.rpc.salta_zonificacion_fetcher` → `{"region_id":"...","survey_id":"..."}` (classifica uso por CPUA; `overwrite:true` para reclasificar)
- `scrapitero.rpc.salta_registro_fetcher` → `{"region_id":"...","survey_id":"..."}` (TIPO registro SIGSA; rural→vacante, club de campo→residencial)
- `scrapitero.rpc.salta_rentas_fetcher` → `{"region_id":"...","survey_id":"..."}` (baldíos por valorEdificado DGRM; lento, vía Playwright)
- `scrapitero.rpc.ibge_logradouros_fetcher` → `{"region_id":"...","municipio_codigo":"...","estado_uf":"..."}`
- `scrapitero.rpc.address_resolver` → `{"region_id":"...","survey_id":"...","batch_size":200}`
- `scrapitero.rpc.survey_step_update` → `{"survey_id":"...","paso":"...","resultado":{...}}`
- `scrapitero.rpc.coverage_reporter` → `{"region_id":"..."}` 

---

## Mensajes de Telegram

Usar siempre `target="telegram:979088442"` en cada llamada a `send_message`.
Escribir siempre en español. Mensajes cortos, claros, sin jerga técnica.
NO mencionar nombres internos de módulos, region_id ni survey_id.

**Al inicio:**
```
🚀 Iniciando relevamiento de {nombre}...
```

**SmartGIS completado:**
```
✅ Parcelas descargadas: {N} parcelas catastrales encontradas en la zona.
```

**BCI completado:**
```
📄 BCIs descargados: {N} nuevos, {Y} ya existían{, Z fallidos si Z > 0}.
```

**Parser completado:**
```
🔍 Datos extraídos: {N} parcelas con uso y dirección.
```

**Si hay error y se reintenta:**
```
⚠️ Problema con {paso legible}: {descripción simple}. Reintentando...
```

**Si un paso falla definitivamente:**
```
❌ No se pudo completar {paso legible}: {descripción simple del problema}.
Continuando con los pasos siguientes.
```

**Al finalizar:**
```
🎉 Relevamiento completado: {nombre}
📦 Parcelas relevadas: X
🏠 Con dirección: Y  
🏷️ Con uso clasificado: Z
⏱️ Duración: N minutos
```

**Si necesitás algo del usuario:**
```
⏸️ Pausado — necesito que confirmes: {pregunta clara en español}
Respondé acá para continuar.
```

---

## Observaciones y Pitfalls

- **`coverage-reporter` no refleja actualizaciones de IBGE inmediatamente**: Se ha observado que después de ejecutar `ibge-census-fetcher` o `ibge-logradouros-fetcher`, el `coverage-reporter` puede no mostrar los nuevos `setores` o `logradouros_count` actualizados en el mismo ciclo. Esto podría deberse a un caché o a un retraso en la actualización de la vista de la base de datos que utiliza `coverage-reporter`. Confía en el output directo de `ibge_census_fetcher` y `ibge_logradouros_fetcher` para verificar su éxito.
- **`bci-parser` requiere `pdfplumber`**: La skill `bci-parser` tiene una dependencia de la librería `pdfplumber`. Si esta no está instalada en el entorno, el parser fallará con un `ModuleNotFoundError`. Asegurarse de que el entorno esté configurado con esta dependencia.

## Registro de progreso

Después de cada skill, llamar a `survey-step-update`:
```json
{"survey_id": "{survey_id}", "paso": "{nombre_paso}", "resultado": {output_de_la_skill}}
```

Al finalizar todo:
```json
{"survey_id": "{survey_id}", "paso": "completado", "status": "completed"}
```

Si `should_stop: true` en la respuesta → detener inmediatamente y notificar.
