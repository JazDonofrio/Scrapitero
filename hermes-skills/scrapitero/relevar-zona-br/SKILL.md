---
name: relevar-zona-br
description: "Relevar una zona de Brasil definida por coordenada central y radio en metros. No requiere conocer municipio ni nomenclatura. Descarga edificios OSM dentro del área y prepara el survey."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, brasil, zona, coordenada, radio, bbox, osm]
    category: scrapitero
---

# Relevar Zona Brasil — por coordenada + radio

Define una zona de relevamiento a partir de un punto central y un radio en metros.
No requiere conocer de antemano el municipio, la nomenclatura ni la región.

## Cuándo usar
- "relevá un radio de 300 metros alrededor de esta coordenada"
- "quiero relevar la zona del hospital de Várzea Grande"
- "relevá 500 metros alrededor de lat=-15.64, lng=-56.11"

## Paso 1 — Definir la zona y descargar edificios OSM

```bash
echo '{"lat":-15.6468,"lng":-56.1195,"radio_m":500}' |

  python3 -m scrapitero.rpc.zona_fetcher
```

Con nombre personalizado:
```bash
echo '{"lat":-15.6468,"lng":-56.1195,"radio_m":500,"region_nombre":"Centro Várzea Grande"}' |

  python3 -m scrapitero.rpc.zona_fetcher
```

Con region_id explícito (para reutilizar una zona ya definida):
```bash
echo '{"lat":-15.6468,"lng":-56.1195,"radio_m":500,"region_id":"centro-vg-br"}' |

  python3 -m scrapitero.rpc.zona_fetcher
```

## Output esperado
```json
{
  "ok": true,
  "region_id": "zona-varzea-grande-mato-grosso-br",
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

## Paso 2 — Continuar con el pipeline

Guardar el `region_id` y `survey_id` del output y continuar:

```bash
echo '{"region_id":"<REGION_ID>","survey_id":"<SURVEY_ID>"}' |

  python3 -m scrapitero.rpc.coverage_reporter
```

Luego resolver direcciones:
```bash
echo '{"region_id":"<REGION_ID>","survey_id":"<SURVEY_ID>","batch_size":100}' |

  python3 -m scrapitero.rpc.address_resolver
```

## Parámetros
| Campo | Tipo | Requerido | Descripción |
|-------|------|-----------|-------------|
| `lat` | float | ✅ | Latitud del centro (negativo para sur) |
| `lng` | float | ✅ | Longitud del centro (negativo para oeste) |
| `radio_m` | float | ✅ | Radio en metros |
| `region_id` | string | ❌ | ID de región (se genera automáticamente si no se pasa) |
| `region_nombre` | string | ❌ | Nombre legible (se detecta por reverse geocoding si no se pasa) |

## Notas
- El bbox se calcula geométricamente — para radios grandes (>5km) puede haber distorsión cerca de los polos
- La región creada automáticamente queda disponible para futuros surveys
- Funciona para cualquier ciudad de Brasil, no solo Várzea Grande
