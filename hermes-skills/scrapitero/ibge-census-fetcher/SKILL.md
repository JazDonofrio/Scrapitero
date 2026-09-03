---
name: ibge-census-fetcher
description: "Descarga setores censitários IBGE 2022 y tabla SIDRA de domicilios para un municipio brasileño. Inserta los datos en la tabla setores_censitarios de la DB. Usar cuando setores == 0 en el CoverageReport."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, ibge, brasil, catastro, censitario, relevamiento]
    category: scrapitero
---

# IBGE Census Fetcher

Descarga y persiste los setores censitários IBGE 2022 de un municipio.
Primera descarga tarda ~2 minutos (SHP ~30 MB). Las siguientes usan caché.

## Cuándo usar
Cuando `coverage-reporter` devuelve `setores == 0`.

## Regiones soportadas actualmente
| region_id | municipio_codigo | estado_uf | Nombre |
|-----------|-----------------|-----------|--------|
| `vg-mt-br` | `5108402` | `mt` | Várzea Grande, Mato Grosso |

## Comando
**No instalar nada. El venv ya está listo.**
```bash
python3 -m scrapitero.rpc.ibge_census_fetcher <<< '{"region_id":"vg-mt-br","municipio_codigo":"5108402","estado_uf":"mt"}'
```

## Output esperado
```json
{
  "ok": true,
  "setores_insertados": 412,
  "setores_atualizados": 0,
  "pop_total": 318922,
  "domicilios_total": 126140,
  "fuentes": ["ibge_malha_setores_2022_mt", "ibge_sidra_t9596"],
  "error": null
}
```

## Si falla
- `ok: false` con mensaje en `error`
- Verificar conectividad: `curl -I https://geoftp.ibge.gov.br`
- Revisar logs: `docker logs scrapitero_db` para problemas de DB
- La caché está en `/tmp/scrapitero_cache/` — borrarla si el archivo está corrupto

## Importante
- Después de correr, verificar con `coverage-reporter` que `setores > 0`
- Los setores son la base para todos los agentes siguientes


## Si la región no tiene `municipio_codigo`

No lo adivines: un código inválido no falla acá, hace que este paso baje el municipio equivocado. Completalo con `scrapitero.rpc.municipio_codigo_fetcher` (`{"region_id":"..."}`), que lo resuelve por región vecina, por reverse-geocoding contra la lista oficial del IBGE o por el nombre de la región, y **nunca escribe un código que no exista**. Si devuelve `sin_resolver`, el `motivo` dice qué le falta a esa región (típicamente: no tiene ni polígono ni parcelas).
