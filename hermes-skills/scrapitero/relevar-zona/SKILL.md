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
| `parcelas > 0` y país AR y provincia Buenos Aires y `uso_principal` null | `uso-classifier` (uso por UF de ARBA + Google Places) — **paso estándar de PBA, no opcional**. Antes correr `arba-carto-fetcher` para que las parcelas tengan UF |
| `parcelas == 0` y otro país | `osm-building-fetcher` |
| `parcelas > 0` y hay cca_codes sin PDF | `varzea-bci-fetcher` |
| PDFs descargados pero parcelas sin `uso_principal` | `bci-parser` |
| BRA: ya corrió `bci-parser` (uso/UF cargados) | `establecimiento-agrupador` — **paso estándar de VG/Brasil, no opcional**. Agrupa parcelas de un mismo establecimiento (fábrica/colegio/iglesia/galpón) para no contar N UF donde hay 1. Correr una vez después de bci-parser, antes de exportar |
| `cobertura_direccion_pct < 0.90` | `address-resolver` |
| `logradouros_count == 0` y BRA | `ibge-logradouros-fetcher` antes de address-resolver |

Podés ejecutar varios en secuencia o en paralelo según las dependencias.

**Salidas PARCIALES (`"parcial": true`) — NO son errores ni timeouts.**
`smartgis-fetcher` y `varzea-bci-fetcher` tienen presupuesto de tiempo interno
(`max_runtime_s`, default 840s): frenan con gracia antes de que el timeout del comando
(~900s) los mate, devolviendo `ok: true` con lo avanzado persistido (`parcial: true`,
y en BCI además `pdfs_pendientes`). Ante un parcial:
1. Registrar el paso con `survey-step-update` (resultado tal cual).
2. **Re-ejecutar el mismo agente con el mismo input** — acumula/continúa donde quedó.
3. Repetir hasta `parcial: false`; recién entonces avanzar al paso siguiente.
No avisar "Reintentando por timeout" por Telegram: es el funcionamiento esperado
(avisar avance parcial está bien: "BCI: X descargados, faltan Y, sigo en otra pasada").

---

## Paralelismo

Cuando dos skills son independientes, lanzalas en background con `&` y esperá ambas con `wait`:

```bash
# Ejemplo: BCI y address-resolver son independientes si ya hay parcelas
python3 -m scrapitero.rpc.varzea_bci_fetcher > /tmp/bci_out.json & <<< '{"region_id":"...","survey_id":"..."}'
PID_BCI=$!

python3 -m scrapitero.rpc.address_resolver > /tmp/addr_out.json & <<< '{"region_id":"...","survey_id":"...","batch_size":200}'
PID_ADDR=$!

wait $PID_BCI $PID_ADDR
cat /tmp/bci_out.json
cat /tmp/addr_out.json
```

**Dependencias reales:**
- BCI depende de SmartGIS (necesita `cca_code` para descargar PDFs)
- Parser depende de BCI (necesita los PDFs descargados)
- EstablecimientoAgrupador depende del Parser (usa uso/UF + propietario que llena el BCI)
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
- `scrapitero.rpc.establecimiento_agrupador` → `{"region_id":"...","survey_id":"..."}` (agrupa parcelas de un mismo establecimiento → 1 UF; correr después de bci-parser)
- `scrapitero.rpc.osm_building_fetcher` → `{"region_id":"...","survey_id":"...","bbox_south":...,"bbox_west":...,"bbox_north":...,"bbox_east":...}`
- `scrapitero.rpc.arba_carto_fetcher` → ver skill arba-carto-fetcher
- `scrapitero.rpc.arba_cadastral_fetcher` → `{"region_id":"...","survey_id":"..."}`
- `scrapitero.rpc.salta_catastro_fetcher` → `{"region_id":"...","survey_id":"...","fuente":"auto"}` (fuente: "auto"|"capital"|"provincia")
- `scrapitero.rpc.salta_zonificacion_fetcher` → `{"region_id":"...","survey_id":"..."}` (classifica uso por CPUA; `overwrite:true` para reclasificar)
- `scrapitero.rpc.salta_registro_fetcher` → `{"region_id":"...","survey_id":"..."}` (TIPO registro SIGSA; rural→vacante, club de campo→residencial)
- `scrapitero.rpc.salta_rentas_fetcher` → `{"region_id":"...","survey_id":"..."}` (baldíos por valorEdificado DGRM; lento, vía Playwright)
- `scrapitero.rpc.ibge_census_fetcher` → `{"region_id":"...","municipio_codigo":"...","survey_id":"..."}` (`estado_uf` opcional: si falta se deriva de los 2 primeros dígitos de `municipio_codigo`, p.ej. 51→MT)
- `scrapitero.rpc.ibge_logradouros_fetcher` → `{"region_id":"...","municipio_codigo":"..."}` (`estado_uf` opcional: se deriva de `municipio_codigo` si falta)
- `scrapitero.rpc.address_resolver` → `{"region_id":"...","survey_id":"...","batch_size":200}`
- `scrapitero.rpc.survey_step_update` → `{"survey_id":"...","paso":"...","resultado":{...}}`
- `scrapitero.rpc.coverage_reporter` → `{"region_id":"..."}` 

---

## Mensajes de Telegram

Enviar a la **chat configurada del operador**: usar `target="telegram:$TELEGRAM_CHAT_ID"`
(la variable de entorno `TELEGRAM_CHAT_ID` del entorno de Hermes). No hardcodear un id de
chat: así el mismo flujo sirve para distintos operadores/zonas. Si por algún motivo la
variable no está disponible, pedírsela al usuario antes de seguir.
Escribir siempre en español.

**Audiencia técnica.** Quien lee Telegram es un operador técnico capaz de destrabar el
problema (dar una credencial, reiniciar un servicio, levantar una fuente caída, etc.)
**siempre que sepa qué falló exactamente**. Por eso:
- **Éxito / progreso:** mensajes cortos y claros con los números clave.
- **Error o problema: SIEMPRE incluir el detalle concreto de la causa.** Nunca un
  genérico tipo "hubo un problema" o "reintentando…" sin decir qué. Incluí, textual, lo
  que devolvió el agente:
  - el campo `error` del output **copialo tal cual**: ya viene sellado por el código del
    agente con el formato `nombre-de-la-skill: detalle` (lo hace el decorador `agent_run`),
    así que con relayarlo ya queda **qué skill falló + la causa**. No lo reescribas ni resumas.
  - si querés, reforzá **qué paso** del pipeline era (descarga de parcelas, edificios, etc.),
  - la causa técnica exacta: código HTTP + host/URL que falló, credencial o sesión
    faltante (p.ej. `JSESSIONID` vencido), reCAPTCHA que no cargó, `ModuleNotFoundError`,
    timeout del WFS, etc.,
  - **qué se necesita para resolverlo**, si se sabe (p.ej. "mandá un JSESSIONID nuevo").

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

**Si hay error y se reintenta** (incluí la causa concreta, no un genérico):
```
⚠️ Problema en {paso legible} ({agente}). Causa: {detalle textual del error del agente}.
Reintentando ({intento}/{máx})…
```
Ejemplo real:
```
⚠️ Problema al descargar edificios de OpenStreetMap (Overpass). Causa: todos los
mirrors fallaron — último: HTTP 504 (overpass-api.de). Reintentando (2/3)…
```

**Si un paso falla definitivamente:**
```
❌ Falló {paso legible} ({agente}). Causa: {detalle textual del error del agente}.
{Qué se necesita para resolverlo, si aplica.} Continúo con los pasos siguientes.
```
Ejemplo real:
```
❌ Falló la detección de baldíos (Salta Rentas DGRM). Causa: el script de reCAPTCHA
no cargó en el portal; no se pudieron generar tokens. Se puede reintentar más tarde.
Continúo con los pasos siguientes.
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
