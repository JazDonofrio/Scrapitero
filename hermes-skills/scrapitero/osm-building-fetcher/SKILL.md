---
name: osm-building-fetcher
description: "Descarga footprints de edificios desde OpenStreetMap (Overpass API), captura sus tags (tipo, pisos, viviendas) y los vincula a la parcela que los contiene. Genérico para Argentina y Brasil. Insumo de unidades-estimator. Usar cuando footprints == 0 y ya hay parcelas con coordenadas en la DB."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, osm, openstreetmap, edificios, footprints, argentina, brasil]
    category: scrapitero
---

# OSM Building Fetcher

Descarga polígonos de edificios desde OpenStreetMap via Overpass API.
El bbox se deriva automáticamente de las parcelas ya cargadas en la DB.
Funciona para cualquier país.

Además de la geometría, **captura los tags de OSM** que sirven para estimar unidades:

| Tag OSM | Columna en `edificios` | Para qué |
|---------|------------------------|----------|
| `building=*` | `tipo_osm` | apartments/house/commercial/retail/… (`yes` → null) |
| `building:levels` | `pisos_estimados` | pisos del edificio |
| `building:flats` / `addr:units` | `unidades_osm` | conteo real de viviendas cuando OSM lo trae |

Y **vincula cada edificio a su parcela** por join espacial PostGIS
(`ST_Contains(parcela.geometry, edificio.centroid)`) → setea `edificios.parcela_id`.
Esa vinculación es lo que después consume `unidades-estimator`.

## Cuándo usar
Cuando `coverage-reporter` devuelve `footprints == 0` y ya hay parcelas en la DB.
Es paso previo a `unidades-estimator` (estimación de uf_vivienda/uf_comercio).

## Comando
**No instalar nada. El venv ya está listo.**

Derivando bbox automáticamente de las parcelas (recomendado):
```bash
echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>"}' |

  python3 -m scrapitero.rpc.osm_building_fetcher
```

Con bbox explícita:
```bash
echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>","bbox_south":-34.67,"bbox_west":-58.68,"bbox_north":-34.66,"bbox_east":-58.67}' |

  python3 -m scrapitero.rpc.osm_building_fetcher
```

## Output esperado
```json
{
  "ok": true,
  "edificios_insertados": 38,
  "edificios_actualizados": 0,
  "bbox_usado": "-34.671,-58.681,-34.660,-58.670",
  "fuentes": ["osm_overpass"],
  "error": null
}
```

## Si falla
- Si `error` menciona "No se encontraron parcelas": cargar parcelas primero con `arba-cadastral-fetcher`
- Si Overpass devuelve timeout: reducir el área (usar bbox explícita más chica)
- Si `edificios_insertados == 0`: puede ser zona sin datos en OSM (raro en áreas urbanas)

## Importante
- Después de correr, verificar con `coverage-reporter` que `footprints > 0`
- OSM puede no tener 100% de cobertura en zonas periurbanas
- La cobertura de OSM en el GBA (Gran Buenos Aires) es generalmente buena
