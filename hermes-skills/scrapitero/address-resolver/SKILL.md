---
name: address-resolver
description: "Resuelve direcciones faltantes o incompletas en parcelas de cualquier país usando Google Maps Geocoding API. Rellena calle y/o número a partir de las coordenadas. Funciona para Argentina, Brasil y cualquier otra zona. El idioma de respuesta se detecta automáticamente del region_id."
version: 2.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, direcciones, geocoding, google-maps, catastro, argentina, brasil]
    category: scrapitero
---

# Address Resolver — Geocoding inverso para cualquier país

Toma parcelas que tienen coordenadas pero sin dirección completa y las resuelve
usando Google Maps Geocoding API (reverse geocoding).

Cubre dos casos:
- **Sin calle:** resuelve dirección completa (calle + número + barrio + municipio + CP)
- **Con calle pero sin número:** solo completa el número y barrio (no sobreescribe la calle)

El idioma de respuesta es automático según el `region_id`:
- Termina en `-br` → `pt-BR`
- Termina en `-ar` → `es-AR`
- Otro → `es`

Para Brasil, intenta primero interpolación IBGE (gratis) antes de llamar a Google.

## Cuándo usar
- Cuando `coverage-reporter` devuelve `parcelas_con_direccion / parcelas < 0.90`
- Cuando hay parcelas con calle pero sin número (dirección parcial)
- Para cualquier país — no requiere datasets locales previos

## Requisito
La variable `GOOGLE_MAPS_API_KEY` debe estar en `/opt/scrapitero/.env`.
Costo: USD 0.005 por llamada (Google SKU: Geocoding).

## Comando
```bash
python3 -m scrapitero.rpc.address_resolver <<< '{"region_id":"<REGION_ID>"}'
```

Con parámetros completos:
```bash
python3 -m scrapitero.rpc.address_resolver <<< '{
  "region_id": "ituzaingo-ba-ar",
  "batch_size": 200,
  "delay_ms": 50,
  "fill_partial": true
}'
```

## Output esperado
```json
{
  "ok": true,
  "parcelas_procesadas": 150,
  "parcelas_resueltas": 138,
  "parcelas_resueltas_logradouros": 40,
  "parcelas_resueltas_google": 98,
  "parcelas_sin_resultado": 12,
  "costo_estimado_usd": 0.49,
  "error": null
}
```

## Parámetros
| Campo | Default | Descripción |
|-------|---------|-------------|
| `region_id` | ✅ | ID de la región |
| `survey_id` | null | Filtrar por survey específico |
| `batch_size` | 100 | Parcelas a procesar por corrida |
| `delay_ms` | 50 | Milisegundos entre llamadas (evita quota) |
| `fill_partial` | true | También rellenar parcelas con calle pero sin número |

## Si falla
- `GOOGLE_MAPS_API_KEY` no definida → agregar a `/opt/scrapitero/.env`
- Quota excedida → reducir `batch_size` o aumentar `delay_ms`
- `parcelas_procesadas == 0` → no hay parcelas con coordenadas y dirección incompleta
