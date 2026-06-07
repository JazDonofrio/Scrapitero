---
name: uso-classifier
description: "Clasifica parcelas como residencial, comercial o mixto usando ARBA (campo sp catastral) + Google Places API. Usar cuando el usuario pregunta cuántas UFs son vivienda vs comercio/oficina."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, clasificacion, uso, vivienda, comercio, mixto, google-places, argentina]
    category: scrapitero
---

# Uso Classifier

Clasifica las parcelas de un relevamiento en residencial / comercial / mixto.

**Cómo funciona:**
1. **ARBA (catastral)**: el campo `sp` de cada subparcela indica si es local (`L`, `L1`...) o vivienda (números, `PH`, etc.). Esto ya se guarda automáticamente al correr `arba-carto-fetcher`.
2. **Google Places API (validación)**: busca negocios activos en las coordenadas de cada parcela. Si Places encuentra comercios que ARBA no vio, los suma. Si ARBA vio locales que Places no encontró, confía en ARBA.

El resultado se guarda en `uso_principal` de cada parcela: `residencial`, `comercial`, `mixto` o `sin_datos`.

## Cuándo usar
- "cuántas UFs son vivienda y cuántas son comercios?"
- "clasificá el uso de las parcelas"
- "quiero saber si hay locales comerciales en la manzana"

## Comando

```bash
python3 -m scrapitero.rpc.uso_classifier <<< '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>"}'
```

Sin survey_id (todas las parcelas de la región):
```bash
python3 -m scrapitero.rpc.uso_classifier <<< '{"region_id":"ituzaingo-ba-ar"}'
```

## Parámetros opcionales
- `delay_ms` (default 500): milisegundos entre requests a Places API
- `batch_notify` (default 10): cada cuántas parcelas loguear progreso

## Output esperado
```json
{
  "ok": true,
  "parcelas_procesadas": 26,
  "residencial": 20,
  "comercial": 2,
  "mixto": 4,
  "sin_datos": 0,
  "error": null
}
```

## Notas
- Requiere `GOOGLE_MAPS_API_KEY` en `.env`
- Correr **después** de `arba-carto-fetcher` (necesita `uf_vivienda`/`uf_comercio` ya calculados)
- Va despacio por diseño (delay entre requests para no saturar Places API)
- El resultado queda persistido en la DB — no hace falta correrlo de nuevo salvo que cambien los datos de ARBA
