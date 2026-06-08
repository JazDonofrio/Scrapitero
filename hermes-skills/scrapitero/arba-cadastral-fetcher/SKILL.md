---
name: arba-cadastral-fetcher
description: "Descarga parcelas catastrales de Buenos Aires Province (Argentina) desde el WFS público de IDERA. Por default filtra por el polígono de la zona (zone_geojson) — no requiere nomenclatura. Opcionalmente filtra por partido/circunscripción/sección/manzana. Usar cuando parcelas == 0 para una región argentina."
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

**Por zona (recomendado — todo relevamiento parte del GeoJSON):** sin nomenclatura. Baja
las parcelas que caen dentro del polígono de la zona (`regions.zone_geojson`). La región
debe haberse creado desde un GeoJSON (GeoJSONZoneFetcher).
```bash
python3 -m scrapitero.rpc.arba_cadastral_fetcher <<< '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>"}'
```

**Por nomenclatura (opcional):** para bajar una manzana puntual, pasar las 4 partes.
```bash
python3 -m scrapitero.rpc.arba_cadastral_fetcher <<< '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>","partido_id":"136","circunscripcion":"2","seccion":"C","manzana":"184"}'
```

## Cómo filtra (importante)
- **Espacial (default):** bbox del polígono de la zona vía el parámetro WFS
  `bbox=...,EPSG:4326` (reproyecta desde el CRS nativo Gauss-Krüger del layer) + recorte
  exacto al polígono con shapely. **No** se usa CQL `INTERSECTS` (GeoServer interpreta el
  WKT en el CRS nativo en metros, no en lat/lon → da 0 features).
- **Por nomenclatura:** CQL `cca LIKE '{prefix}%'` con el prefijo armado de partido/circ/secc/manzana.

## Output esperado
```json
{
  "ok": true,
  "parcelas_insertadas": 629,
  "parcelas_actualizadas": 0,
  "fuentes": ["idera_wfs_espacial"],
  "error": null
}
```
(`fuentes`: `idera_wfs_espacial` por zona, `idera_wfs` por nomenclatura)

## Si falla
- `ok: false` con mensaje en `error`
- Si el error menciona HTTP 500 o "featureType not found": el WFS de IDERA puede estar caído
  - URL pública: https://geo.arba.gov.ar/geoserver/idera/wfs (layer `idera:Parcela`)
- Si dice "no tiene zone_geojson": la región se creó sin polígono → crearla con
  GeoJSONZoneFetcher, o pasar la nomenclatura completa como fallback.
- Si por zona devuelve 0 parcelas: verificar que la zona esté en Provincia de Buenos Aires.
- Si por nomenclatura devuelve 0: revisar partido/circunscripcion/seccion/manzana (el
  agente rellena ceros a izquierda automáticamente).
