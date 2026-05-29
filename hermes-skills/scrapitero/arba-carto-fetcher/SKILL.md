---
name: arba-carto-fetcher
description: "Enriquece parcelas de Buenos Aires Province con subparcelas, UF, cocheras y dirección desde carto.arba.gov.ar. Requiere JSESSIONID activo. Si needs_cookies=true, pedir el valor al usuario por Telegram."
version: 2.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, arba, argentina, carto, subparcelas, uf, sesion]
    category: scrapitero
---

# ARBA Carto Fetcher

Para cada parcela ya cargada en la DB (por `arba-cadastral-fetcher`), consulta
`carto.arba.gov.ar/cartoArba/client/getInfo` y extrae subparcelas (UF, cocheras),
domicilio registrado en ARBA, y geocodifica la dirección via Google Maps → Nominatim.

## Cuándo usar
Después de `arba-cadastral-fetcher`, para enriquecer las parcelas con:
- Unidades funcionales y cocheras por parcela
- Dirección registrada en ARBA
- Geocodificación inversa

## FLUJO — leer completo

### Paso 1 — Intentar con sesión guardada

```bash
echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>","partido_id":"136","circunscripcion":"2","seccion":"C","manzana":"184"}' | \
  env $(cat /opt/scrapitero/.env | xargs) \
  PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \
  python3 -m scrapitero.rpc.arba_carto_fetcher
```

### Paso 2 — Si `needs_cookies: true` → pedir al usuario por Telegram

Enviar este mensaje:

> Necesito el JSESSIONID de carto.arba.gov.ar para buscar las subparcelas.
>
> **Opción A (más fácil):**
> 1. Abrí Chrome → https://carto.arba.gov.ar/cartoArba/
> 2. F12 → Application → Storage → Cookies → carto.arba.gov.ar
> 3. Copiame el valor de **JSESSIONID**
>
> **Opción B (desde Network):**
> 1. F12 → Network
> 2. Buscá la manzana en el formulario
> 3. Hacé click en cualquier request a `getInfo`
> 4. Headers → Request Headers → copiame el valor de `Cookie:`

### Paso 3 — Reintentar con el JSESSIONID recibido

```bash
echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>","partido_id":"136","circunscripcion":"2","seccion":"C","manzana":"184","jsessionid":"<VALOR_DEL_USUARIO>"}' | \
  env $(cat /opt/scrapitero/.env | xargs) \
  PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \
  python3 -m scrapitero.rpc.arba_carto_fetcher
```

Si el usuario mandó el header Cookie completo, usar `cookie_header` en lugar de `jsessionid`.

## Output esperado (éxito)
```json
{
  "ok": true,
  "parcelas_procesadas": 42,
  "parcelas_con_subparcelas": 39,
  "total_uf": 87,
  "total_cocheras": 12,
  "needs_cookies": false,
  "fuentes": ["arba_carto_getInfo", "google_maps"],
  "error": null
}
```

## Notas
- El JSESSIONID se guarda automáticamente en `/opt/scrapitero/.arba_session.json`
- Las sesiones de ARBA duran ~30 min de inactividad
- Si la sesión expiró, el agente borra el archivo y vuelve a pedir cookies
- `total_uf` = unidades funcionales (subparcelas ≥ 25 m²)
- `total_cocheras` = subparcelas < 25 m²
