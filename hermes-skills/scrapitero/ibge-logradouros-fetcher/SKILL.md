---
name: ibge-logradouros-fetcher
description: "Carga la Base de Faces de Logradouros IBGE 2022 para un municipio brasileño. Permite geocoding gratuito por interpolación. Correr antes de address-resolver en regiones de Brasil."
version: 2.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, ibge, brasil, logradouros, geocoding, varzea-grande]
    category: scrapitero
---

# IBGE Logradouros Fetcher

Carga la Base de Faces de Logradouros IBGE 2022 para un municipio de Brasil.
Permite que `address-resolver` resuelva direcciones por interpolación (gratis),
sin necesidad de llamar a Google Maps API.

## Cuándo usar
Antes de correr `address-resolver` en una región brasileña por primera vez,
o cuando `logradouros_count == 0` en el CoverageReport.

**No usar para Argentina ni otros países fuera de Brasil.**

## Comando

```bash
echo '{"region_id":"vg-mt-br","municipio_codigo":"5108402","estado_uf":"mt"}' |

  python3 -m scrapitero.rpc.ibge_logradouros_fetcher
```

## Output esperado

```json
{
  "ok": true,
  "logradouros_insertados": 12543,
  "logradouros_actualizados": 0,
  "fuentes": ["ibge_faces_logradouros_2022_MT"]
}
```

## Notas
- El ZIP del estado completo (~16 MB para MT) se descarga una vez y queda en caché en `/tmp/scrapitero_cache/`
- El filtro por municipio es un `startswith` sobre el campo `cod_munic` del shapefile
- Para Várzea Grande, MT: `municipio_codigo = "5108402"`, `estado_uf = "mt"`
- Después de correr, verificar con `coverage-reporter` que `logradouros_count > 0`
