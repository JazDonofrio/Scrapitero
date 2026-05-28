---
name: address-resolver
description: "Resuelve direcciones faltantes en parcelas usando reverse geocoding de Google Maps API. Usar cuando parcelas_con_direccion / max(footprints,1) < 0.90 en el CoverageReport."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, direcciones, geocoding, google-maps, catastro]
    category: scrapitero
---

# Address Resolver

Toma parcelas que tienen coordenadas pero no tienen dirección y las resuelve
usando la API de Geocoding de Google Maps (reverse geocoding).

## Cuándo usar
Cuando `coverage-reporter` devuelve `parcelas_con_direccion / max(footprints,1) < 0.90`.

## Requisito
La variable `GOOGLE_MAPS_API_KEY` debe estar en `/opt/scrapitero/.env`.

## Comando
**No instalar nada. El venv ya está listo.**
```bash
echo '{"region_id":"vg-mt-br","survey_id":"<SURVEY_ID>","batch_size":100}' | \
  env $(cat /opt/scrapitero/.env | xargs) \
  PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \
  python3 -m scrapitero.rpc.address_resolver
```

Para procesar toda la región sin filtrar por survey:
```bash
echo '{"region_id":"vg-mt-br"}' | \
  env $(cat /opt/scrapitero/.env | xargs) \
  PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \
  python3 -m scrapitero.rpc.address_resolver
```

## Output esperado
```json
{
  "ok": true,
  "parcelas_procesadas": 100,
  "parcelas_resueltas": 93,
  "parcelas_sin_resultado": 7,
  "costo_estimado_usd": 0.5,
  "error": null
}
```

## Parámetros opcionales
- `batch_size` (default: 100): cantidad de parcelas a procesar por corrida
- `delay_ms` (default: 50): milisegundos entre llamadas (evita superar quota)

## Si falla
- `ok: false` con mensaje en `error`
- Si el error menciona `GOOGLE_MAPS_API_KEY`: agregar la key a `/opt/scrapitero/.env`
- Si el error es de quota: reducir `batch_size` o aumentar `delay_ms`
- Si `parcelas_procesadas == 0`: verificar que existan parcelas con coordenadas pero sin dirección

## Importante
- Después de correr, verificar con `coverage-reporter` que `parcelas_con_direccion` aumentó
- Costo aproximado: USD 0.005 por llamada (Google SKU: Geocoding)
- Las direcciones se guardan en portugués (language=pt-BR) por estar en Brasil
