# ibge-logradouros-fetcher

Carga la Base de Faces de Logradouros IBGE 2022 para un municipio de Brasil.
Esto permite que el AddressResolver resuelva direcciones por interpolación (gratis),
sin necesidad de llamar a Google Maps API.

**Cuándo usar:** Antes de correr address_resolver en una región brasileña por primera vez,
o cuando `logradouros_count == 0` en el CoverageReport.

**No usar para Argentina ni otros países fuera de Brasil.**

## Pipeline

1. Invocar `ibge_logradouros_fetcher` con region_id, municipio_codigo y estado_uf.
2. Verificar que `ok == true` y reportar `logradouros_insertados`.
3. Continuar con `address_resolver` para aprovechar el geocoding gratis.

## Invocar el agente

```bash
cd /opt/scrapitero
source .venv/bin/activate && export $(cat .env | xargs)
echo '{"region_id":"vg-mt-br","municipio_codigo":"5108402","estado_uf":"mt"}' | \
  python -m scrapitero.rpc.ibge_logradouros_fetcher
```

## Input

```json
{
  "region_id": "vg-mt-br",
  "municipio_codigo": "5108402",
  "estado_uf": "mt"
}
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

- El ZIP del estado completo (~16 MB para MT) se descarga una sola vez y queda en caché en
  `$SCRAPITERO_CACHE` (default: `/tmp/scrapitero_cache`).
- El filtro por municipio_codigo es un `startswith` sobre el campo cod_munic del shapefile.
- Para Várzea Grande, MT: `municipio_codigo = "5108402"`, `estado_uf = "mt"`.
