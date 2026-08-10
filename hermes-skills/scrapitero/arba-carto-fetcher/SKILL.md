---
name: arba-carto-fetcher
description: "Enriquece parcelas de Buenos Aires Province con subparcelas, UF, cocheras y dirección desde carto.arba.gov.ar. Requiere JSESSIONID activo. Si no hay parcelas en DB las baja de IDERA por el polígono de la zona (zone_geojson, sin nomenclatura) o por nomenclatura si se pasa. Si needs_cookies=true, pedir el valor al usuario por Telegram."
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

### Paso 0 — PEDIR JSESSIONID AL USUARIO SIEMPRE ANTES DE CONTINUAR

**Antes de correr el comando, enviar al usuario:**

> Necesito el cookie de sesión de carto.arba.gov.ar.
> 1. Abrí Chrome → https://carto.arba.gov.ar/cartoArba/
> 2. F12 → Application → Storage → Cookies → carto.arba.gov.ar
> 3. Copiame el valor de **JSESSIONID** (o el header Cookie: completo desde Network)

**Esperar respuesta antes de continuar.**

### Paso 1 — Correr con el JSESSIONID recibido

**Por zona (recomendado):** si no hay parcelas en DB, las baja de IDERA por el polígono de
la zona (`zone_geojson`). No requiere nomenclatura.
```bash
python3 -m scrapitero.rpc.arba_carto_fetcher <<< '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>"}'
```

**Por nomenclatura (opcional):** para acotar a una manzana puntual.
```bash
python3 -m scrapitero.rpc.arba_carto_fetcher <<< '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>","partido_id":"136","circunscripcion":"2","seccion":"C","manzana":"184"}'
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

**Si el usuario mandó solo el valor del JSESSIONID** (ej: `ABC123XYZ`):
```bash
python3 -m scrapitero.rpc.arba_carto_fetcher <<< '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>","jsessionid":"<VALOR>"}'
```

**Si el usuario mandó el header Cookie completo** (ej: `JSESSIONID=ABC123; TS01x=yyy` o `Cookie: JSESSIONID=ABC123`):
```bash
python3 -m scrapitero.rpc.arba_carto_fetcher <<< '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>","cookie_header":"<STRING_COMPLETO>"}'
```

(Agregar `partido_id`/`circunscripcion`/`seccion`/`manzana` solo si querés acotar a una manzana.)

**IMPORTANTE:** no inventar ni modificar el valor del cookie. Usarlo exactamente como lo mandó el usuario.

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
- La UF de cada parcela se guarda en `uf_vivienda` (total del lote, `uf_fuente='arba_carto'`)
  y en `unidades_funcionales_estimadas`. **ARBA no dice el destino** de cada subparcela (el
  campo `sp` es su número, no el uso), así que el reparto vivienda/comercio lo hace después
  `uso-classifier` con Google Places — correrlo siempre, es el último paso de UF en PBA
- No pisa lo corregido a mano: respeta `uf_fuente='manual'` y `direccion_source='manual'`
