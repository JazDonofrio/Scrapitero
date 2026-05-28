---
name: arba-cadastral-fetcher
description: "Descarga parcelas catastrales de Buenos Aires Province (Argentina) desde el WFS público de ARBA. Filtrar por partido, circunscripción, sección y/o manzana. Usar cuando parcelas == 0 para una región argentina."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, arba, argentina, catastro, parcelas, buenos-aires]
    category: scrapitero
---

# ARBA Cadastral Fetcher

Descarga polígonos de parcelas catastrales desde el WFS público de ARBA
(Agencia de Recaudación de la Provincia de Buenos Aires).

## Cuándo usar
Cuando `coverage-reporter` devuelve `parcelas == 0` para una región argentina.

## Regiones soportadas
| region_id | partido_id | Nombre |
|-----------|-----------|--------|
| `ituzaingo-ba-ar` | `136` | Ituzaingó, Buenos Aires |

## Comando
**No instalar nada. El venv ya está listo.**

Para una manzana específica (recomendado para tests):
```bash
echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>","partido_id":"136","circunscripcion":"2","seccion":"C","manzana":"184"}' | \
  env $(cat /opt/scrapitero/.env | xargs) \
  PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \
  python3 -m scrapitero.rpc.arba_cadastral_fetcher
```

Para todo el partido:
```bash
echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>","partido_id":"136"}' | \
  env $(cat /opt/scrapitero/.env | xargs) \
  PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \
  python3 -m scrapitero.rpc.arba_cadastral_fetcher
```

## Output esperado
```json
{
  "ok": true,
  "parcelas_insertadas": 42,
  "parcelas_actualizadas": 0,
  "fuentes": ["arba_wfs_parcelas"],
  "error": null
}
```

## Si falla
- `ok: false` con mensaje en `error`
- Si el error menciona HTTP 500 o "featureType not found": el WFS de ARBA puede estar caído
  - URL pública: https://geo.arba.gov.ar/geoserver/irisas/ows
- Si devuelve 0 parcelas: verificar partido_id, circunscripcion, seccion, manzana con ceros a izquierda
  - partido_id: 4 dígitos (ej: "0136")
  - manzana: 4 dígitos (ej: "0184")
  - El agente rellena automáticamente con ceros
