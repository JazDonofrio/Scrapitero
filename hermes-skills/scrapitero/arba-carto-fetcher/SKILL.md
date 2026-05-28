---
name: arba-carto-fetcher
description: "Descarga parcelas catastrales desde el portal Carto de ARBA (carto.arba.gov.ar). Requiere sesión activa del usuario. Si needs_cookies=true en el output, pedir el JSESSIONID al usuario por Telegram antes de reintentar."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, arba, argentina, catastro, carto, parcelas, sesion]
    category: scrapitero
---

# ARBA Carto Fetcher

Descarga parcelas desde https://carto.arba.gov.ar/cartoArba/ usando la sesión
del usuario. Complementa al `arba-cadastral-fetcher` (WFS público) con datos
adicionales del portal visual de ARBA.

## Cuándo usar
Cuando `arba-cadastral-fetcher` falla o devuelve datos incompletos,
o cuando el usuario pide usar el portal Carto de ARBA específicamente.

## FLUJO CRÍTICO — leer completo antes de ejecutar

### Intento 1 — con sesión guardada

```bash
echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>","partido_id":"136","circunscripcion":"2","seccion":"C","manzana":"184"}' | \
  env $(cat /opt/scrapitero/.env | xargs) \
  PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \
  python3 -m scrapitero.rpc.arba_carto_fetcher
```

### Si el output tiene `"needs_cookies": true` → PEDIR AL USUARIO POR TELEGRAM

Enviar EXACTAMENTE este mensaje al usuario:

> Necesito que me pases el JSESSIONID de carto.arba.gov.ar para poder buscar las parcelas.
>
> Pasos:
> 1. Abrí Chrome → https://carto.arba.gov.ar/cartoArba/
> 2. Abrí DevTools con F12 → pestaña **Network**
> 3. Hacé la búsqueda: Partido=136, Circunscripción=2, Sección=C, Manzana=184 → Aceptar
> 4. En la lista de requests, buscá uno de tipo Fetch/XHR con nombre como `getParcelasByNomenclatura` o similar
> 5. Hacé click → **Headers** → **Request Headers** → copiá el valor del header `Cookie:`
> 6. Enviame ese valor acá

### Intento 2 — con cookies del usuario

Cuando el usuario responda con el valor del Cookie (ej: `JSESSIONID=ABC123DEF`):

```bash
echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>","partido_id":"136","circunscripcion":"2","seccion":"C","manzana":"184","jsessionid":"<VALOR_QUE_MANDO_EL_USUARIO>"}' | \
  env $(cat /opt/scrapitero/.env | xargs) \
  PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \
  python3 -m scrapitero.rpc.arba_carto_fetcher
```

Si el usuario mandó el header Cookie completo (ej: `JSESSIONID=ABC123; otro=valor`),
usar el campo `cookie_header` en lugar de `jsessionid`:

```bash
echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>","partido_id":"136","circunscripcion":"2","seccion":"C","manzana":"184","cookie_header":"<COOKIE_HEADER_COMPLETO>"}' | \
  env $(cat /opt/scrapitero/.env | xargs) \
  PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \
  python3 -m scrapitero.rpc.arba_carto_fetcher
```

## Output esperado (éxito)
```json
{
  "ok": true,
  "parcelas_insertadas": 42,
  "parcelas_actualizadas": 0,
  "endpoint_usado": "https://carto.arba.gov.ar/cartoArba/getParcelasByNomenclatura",
  "fuentes": ["arba_carto"],
  "needs_cookies": false,
  "error": null
}
```

## Output cuando necesita sesión
```json
{
  "ok": false,
  "needs_cookies": true,
  "cookie_instructions": "Necesito que me pases el JSESSIONID...",
  "error": "Sin sesión activa de carto.arba.gov.ar"
}
```

## Notas
- La sesión se guarda automáticamente en `/opt/scrapitero/.arba_session.json`
- Las sesiones de ARBA duran típicamente 30 minutos de inactividad
- Si la sesión expiró, el agente vuelve a pedir cookies automáticamente
- El agente prueba múltiples endpoints automáticamente — no cambiar la URL manualmente
