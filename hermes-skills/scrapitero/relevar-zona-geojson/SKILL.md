---
name: relevar-zona-geojson
description: "Crear una zona de relevamiento a partir de un archivo GeoJSON con uno o más polígonos. Crea la región en la DB, arranca un survey nuevo y descarga edificios OSM del área."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, geojson, zona, poligono, osm]
    category: scrapitero
---

# Relevar Zona desde GeoJSON

Crea un nuevo relevamiento a partir de un GeoJSON con el polígono del área.
Útil cuando ya se tiene el área delimitada (descargada de GeoJSON.io, QGIS, etc.).

## Cuándo usar
- "quiero relevar esta área" + adjunta un .geojson
- "creá un relevamiento para el barrio X con este polígono"
- "cargá este GeoJSON como nueva zona de relevamiento"

## Paso 1 — Crear zona y descargar edificios OSM

```bash
echo '{
  "region_nombre": "Barrio Centro",
  "geojson_str": "<CONTENIDO_DEL_ARCHIVO_GEOJSON>",
  "country_code": "BRA"
}' |

  python3 -m scrapitero.rpc.geojson_zone_fetcher
```

Para Argentina:
```bash
echo '{"region_nombre":"Villa Sur","geojson_str":"<GeoJSON>","country_code":"ARG"}' |

  python3 -m scrapitero.rpc.geojson_zone_fetcher
```

## Output esperado
```json
{
  "ok": true,
  "region_id": "zona-barrio-centro",
  "survey_id": "<UUID>",
  "bbox": {"south": -15.651, "west": -56.124, "north": -15.642, "east": -56.115},
  "edificios_insertados": 143,
  "proximos_pasos": [
    "address-resolver con region_id='...' survey_id='...'",
    "coverage-reporter con region_id='...' survey_id='...'"
  ],
  "error": null
}
```

## Parámetros
| Campo | Tipo | Requerido | Descripción |
|-------|------|-----------|-------------|
| `region_nombre` | string | ✅ | Nombre del relevamiento |
| `geojson_str` | string | ✅ | Contenido del archivo GeoJSON (FeatureCollection, Feature o Polygon) |
| `country_code` | string | ❌ | "BRA" (default) o "ARG" |
| `region_id` | string | ❌ | ID explícito (se genera como `zona-<slug>` si no se pasa) |

## Notas
- El `zone_geojson` se guarda en la tabla `regions` para mostrarse en el Web UI
- El Web UI en `http://localhost:8765` permite hacer esto mismo desde el browser
