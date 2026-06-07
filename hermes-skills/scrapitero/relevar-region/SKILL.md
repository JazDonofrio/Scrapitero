---
name: relevar-region
description: "Relevamiento de regiones BRASILEÑAS. Acepta región predefinida (vg-mt-br) O coordenada + radio en metros. Activar cuando el usuario pide relevar una ciudad, zona o punto geográfico de Brasil."
version: 2.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, relevamiento, catastro, orquestador, parcelas, brasil, coordenada, radio]
    category: scrapitero
---

# Relevar Región — Orquestador Brasil

Pipeline completo de relevamiento catastral para Brasil.
Acepta **dos modos de entrada**:

---

## MODO A — Región predefinida (region_id conocido)

Usar cuando el usuario menciona una región ya configurada.

| Nombre | region_id | municipio_codigo | estado_uf |
|--------|-----------|-----------------|-----------|
| Várzea Grande, MT | `vg-mt-br` | `5108402` | `mt` |

---

## MODO B — Coordenada + radio (zona libre)

Usar cuando el usuario indica un punto geográfico y un radio.
Frases que activan este modo:
- "relevá 500 metros alrededor de lat=-15.64, lng=-56.11"
- "relevá esta coordenada con radio de 300m"
- "quiero relevar la zona del hospital"

En este modo, **Paso 1 es `zona_fetcher`** en lugar de crear el survey manualmente.

---

## PASO 1A — Crear survey (Modo A: región predefinida)

```bash
env $(cat /opt/scrapitero/.env | xargs) \
PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src
python3 -c "
import uuid
from sqlalchemy import text
from scrapitero.db.engine import get_engine
survey_id = uuid.uuid4()
with get_engine().begin() as conn:
    conn.execute(text(\"INSERT INTO surveys (survey_id, region_id) VALUES (:sid, :rid)\"),
                 {'sid': str(survey_id), 'rid': '<REGION_ID>'})
print(survey_id)
"
```

Guardar el `survey_id` para los pasos siguientes.

---

## PASO 1B — Definir zona y crear survey (Modo B: coordenada + radio)

```bash
python3 -m scrapitero.rpc.zona_fetcher <<< '{"lat":<LAT>,"lng":<LNG>,"radio_m":<RADIO>}'
```

El output incluye `region_id` y `survey_id` generados automáticamente.
Guardarlos para los pasos siguientes. El agente también descarga los edificios OSM
dentro del bbox — si `edificios_insertados > 0`, saltar directo al Paso 3.

---

## PASO 2 — Loop hasta cobertura aceptable

Repetir hasta máximo 20 iteraciones:

**a) Leer estado actual:**
```bash
python3 -m scrapitero.rpc.coverage_reporter <<< '{"region_id":"<REGION_ID>","survey_id":"<SURVEY_ID>"}'
```

**b) Decidir próximo agente según tabla:**

En Brasil los **edificios OSM** (`footprints`) son la unidad principal de relevamiento.
Adicionalmente, ONR provee **parcelas catastrales** según la zona geográfica:

| Condición en CoverageReport | Agente a correr | Zona |
|-----------------------------|----------------|------|
| `setores == 0` | `ibge-census-fetcher` | Solo Modo A |
| `logradouros_count == 0` | `ibge-logradouros-fetcher` | Solo Modo A |
| `footprints == 0` | `osm-building-fetcher` (con bbox si Modo B) | Todos |
| `parcelas == 0` y zona **rural** | `onr-sigef-fetcher` (predios SIGEF/INCRA) | Todos |
| `parcelas == 0` y ciudad **con cobertura ONR** | `onr-lotes-fetcher` (lotes urbanos) | Todos |
| `cobertura_direccion_pct < 0.90` | `address-resolver` | Todos |
| `cobertura_direccion_pct >= 0.90` | → Paso 3 (exportar) | |

**Routing ONR por zona geográfica:**
- **Zona rural / Várzea Grande / cualquier ciudad sin cobertura** → `onr-sigef-fetcher`
- **São Paulo, RJ, Fortaleza, Recife, BH, Curitiba, Manaus, João Pessoa, Natal, Florianópolis, Campo Grande, Niterói, Santa Maria, Rio Branco, São Bernardo, Maringá** → `onr-lotes-fetcher`

> ONR requiere token ArcGIS que se obtiene automáticamente — no requiere intervención humana.

**c) Correr el agente decidido** con su skill correspondiente.

---

## PASO 3 — Notificar al usuario

Reportar por Telegram:
- Región relevada y área cubierta
- Edificios/footprints encontrados
- Cobertura de dirección
- Ofrecer PDF y CSV con `relevamiento-pdf` y `relevamiento-csv`

---

## Notas
- **Nunca instalar paquetes.** Usar siempre `python3` con `.hermes-packages` en PYTHONPATH.
- Siempre enviar aviso parcial por Telegram cada vez que termina un agente.
- Si cualquier agente falla por login/sesión → consultar al usuario por Telegram antes de continuar.
- Máximo 20 steps por corrida. Si se alcanza el límite: exportar lo disponible y notificar.

### Mensajes de error: detalle concreto (audiencia técnica)
Quien lee Telegram es un **operador técnico** que puede destrabar el problema si sabe
qué falló. **Todo mensaje de error o problema DEBE incluir la causa concreta**, nunca un
genérico ("hubo un problema" / "reintentando…" a secas). Incluí, textual:
- el campo `error` del output del agente **copialo tal cual**: ya viene sellado por el
  código (decorador `agent_run`) con el formato `nombre-de-la-skill: detalle`, así que al
  relayarlo ya queda **qué skill falló + la causa**. No lo reescribas ni resumas.
- la causa técnica exacta ya está dentro de ese `error` (código HTTP + host/URL, credencial
  o sesión faltante como `JSESSIONID` vencido, reCAPTCHA que no cargó, `ModuleNotFoundError`,
  timeout del WFS…),
- **qué se necesita para resolverlo**, si se sabe.

Ejemplo: `❌ Falló descarga de edificios (OSM/Overpass). Causa: todos los mirrors
fallaron — último HTTP 504 (overpass-api.de). Reintento más tarde.`
