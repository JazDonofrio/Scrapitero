---
name: relevar-manzana-ar
description: "Relevamiento catastral de manzanas en Argentina (Buenos Aires Province). Activar cuando el usuario menciona Partido, Circunscripción, Sección, Manzana, Ituzaingó, ARBA, o cualquier localidad argentina."
version: 2.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, argentina, manzana, catastro, arba, ituzaingo, relevamiento]
    category: scrapitero
---

# Relevar Manzana Argentina

Pipeline de relevamiento catastral para manzanas de Buenos Aires Province.

## REGLAS OBLIGATORIAS
- **Nunca instalar paquetes.** El venv ya está listo.
- Siempre usar: `env $(cat /opt/scrapitero/.env | xargs) PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src python3`
- **SIEMPRE pedir el JSESSIONID antes de correr `arba_carto_fetcher`.** Ver Paso 2.

---

## Paso 1 — Crear survey

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
                 {'sid': str(survey_id), 'rid': 'ituzaingo-ba-ar'})
print(survey_id)
"
```
Guardar el `survey_id`.

---

## Paso 2 — PEDIR JSESSIONID AL USUARIO (OBLIGATORIO antes de continuar)

**SIEMPRE enviar este mensaje al usuario antes de correr arba_carto_fetcher:**

> Necesito el cookie de sesión de carto.arba.gov.ar para obtener los datos catastrales.
>
> **Pasos:**
> 1. Abrí Chrome → https://carto.arba.gov.ar/cartoArba/
> 2. Presioná F12 → pestaña **Application** → Storage → Cookies → carto.arba.gov.ar
> 3. Copiame el valor completo de la fila **JSESSIONID**
>
> O si preferís: F12 → Network → hacé una búsqueda en el mapa → click en cualquier request a `getInfo` → Headers → Request Headers → copiame el header **Cookie:**

**Esperar la respuesta del usuario.** No continuar hasta recibir el JSESSIONID.

---

## Paso 3 — Cargar parcelas + datos catastrales

Con el JSESSIONID recibido, correr `arba_carto_fetcher` (descarga IDERA + enriquece carto en un solo paso):

```bash
echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>","partido_id":"136","circunscripcion":"<CIRC>","seccion":"<SECC>","manzana":"<MZA>","cookie_header":"<COOKIE_DEL_USUARIO>"}' |

  python3 -m scrapitero.rpc.arba_carto_fetcher
```

### Si el output tiene `"needs_cookies": true`:
La sesión expiró. Enviar al usuario:
> La sesión de ARBA expiró. Necesito un JSESSIONID nuevo — seguí los mismos pasos de antes en Chrome.

Esperar nuevo JSESSIONID y reintentar.

### Si el output tiene `"ok": false` con otro error:
Reportar el error exacto al usuario y detener.

---

## Paso 4 — Verificar estado

```bash
echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>"}' |

  python3 -m scrapitero.rpc.coverage_reporter
```

---

## Paso 5 — Reportar al usuario

Informar:
- Parcelas relevadas y con nomenclatura
- Total de UF
- Cualquier parcela sin datos de carto (si `parcelas_con_subparcelas < total_parcelas`)

Ofrecer generar el PDF con el skill `relevamiento-pdf`.
