---
name: smartgis-fetcher
description: "Descarga parcelas de Várzea Grande desde la API SmartGIS (api.smartgis.net.br/varzeagrande). Obtiene inscripción catastral (CODIGO_IMOVEL_AGRUPADO), geometría, dirección, bairro, área y lo guarda en la tabla parcelas. El CODIGO es el número que usa vg.abaco.com.br para las BCIs. Siempre respetar el polígono de zona cargado en la región."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, brasil, varzea-grande, smartgis, cadastro, iptu]
    category: scrapitero
---

# SmartGIS Fetcher — Parcelas de Várzea Grande

Escanea el área de la región usando la API SmartGIS de VG, que expone el catastro
de la Prefeitura de Várzea Grande. Guarda geometría + inscripción en `parcelas`.

## Cuándo usar
- Primer paso obligatorio para relevamientos en Várzea Grande (Brasil)
- Antes de VGBCIFetcher (necesita `cca_code` en parcelas)
- Cuando `total_parcelas == 0` para una región de VG

## Paso 1 — Correr el agente (bbox derivado automáticamente del zone_geojson)

```bash
echo '{"region_id":"<REGION_ID>","survey_id":"<SURVEY_ID>"}' |

  python3 -m scrapitero.rpc.smartgis_fetcher
```

Con bbox explícito (si la región no tiene zone_geojson):
```bash
echo '{
  "region_id":"<REGION_ID>",
  "survey_id":"<SURVEY_ID>",
  "bbox_south":-15.660,
  "bbox_west":-56.130,
  "bbox_north":-15.640,
  "bbox_east":-56.110
}' | env $(cat /opt/scrapitero/.env | xargs)
   
     python3 -m scrapitero.rpc.smartgis_fetcher
```

## Output esperado
```json
{
  "ok": true,
  "parcelas_insertadas": 284,
  "parcelas_actualizadas": 12,
  "lotes_escaneados": 298,
  "fuera_de_zona": 2,
  "error": null
}
```

## Paso 2 — Continuar con BCI Fetcher

Si `parcelas_insertadas + parcelas_actualizadas > 0`, llamar a `varzea-bci-fetcher`.

## Parámetros
| Campo | Tipo | Default | Descripción |
|-------|------|---------|-------------|
| `region_id` | string | ✅ | ID de la región |
| `survey_id` | string | ✅ | ID del survey |
| `bbox_*` | float | ❌ | Bbox explícita (si no hay zone_geojson) |
| `step_grados` | float | 0.000018 | ~2m por celda. Con paso de 2m cada parcela recibe decenas de hits — el límite de 2 IDs/request de la API no puede causar omisiones |
| `n_passes` | int | 1 | Pases de grillas offset. Con paso de 2m es redundante; aumentar solo si se usa un paso mayor |
| `delay_ms` | int | 120 | Delay base entre requests (distribución humana) |

## Tiempo estimado
- ~100 celdas/min con delay_ms=120
- 1 km² ≈ 2.200 celdas × 2 passes ≈ 22 minutos de escaneo
- Luego ~50 req/min para detalles de lotes
