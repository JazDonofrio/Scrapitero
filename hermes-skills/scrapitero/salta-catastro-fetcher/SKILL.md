---
name: salta-catastro-fetcher
description: "Descarga parcelas catastrales de la provincia de Salta (Argentina) desde WFS públicos. Capital: IDEMSA (125k parcelas, actualizado Ene 2025). Interior: IDESA provincial. Selección automática según la zona. Usar cuando parcelas == 0 para una región de Salta."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, salta, argentina, catastro, parcelas, wfs]
    category: scrapitero
---

# Salta Catastro Fetcher

Descarga polígonos de parcelas catastrales de la provincia de Salta desde dos
fuentes WFS públicas sin autenticación.

## Fuentes

| Fuente | URL | Cobertura | Actualización |
|--------|-----|-----------|---------------|
| IDEMSA Capital | geocloud.municipalidadsalta.gob.ar | Ciudad de Salta (~125k parcelas) | Ene 2025 |
| IDESA Provincial | geoportal.idesa.gob.ar | Interior de la provincia | Trimestral |

La selección es **automática** según el centroide de la zona cargada en la región.
Si está dentro del bbox de la ciudad capital → IDEMSA. Caso contrario → IDESA.

## Cuándo usar

Cuando `coverage-reporter` devuelve `parcelas == 0` para una región en **Salta, Argentina**.

## Comando

```bash
python3 -m scrapitero.rpc.salta_catastro_fetcher <<< '{"region_id":"{region_id}","survey_id":"{survey_id}","fuente":"auto"}'
```

Para forzar una fuente específica:
```bash
# Solo Capital (IDEMSA)
python3 -m scrapitero.rpc.salta_catastro_fetcher <<< '{"region_id":"...","fuente":"capital"}'

# Solo Interior (IDESA)
python3 -m scrapitero.rpc.salta_catastro_fetcher <<< '{"region_id":"...","fuente":"provincia"}'
```

## Parámetros

| Campo | Tipo | Default | Descripción |
|-------|------|---------|-------------|
| `region_id` | str | — | Obligatorio |
| `survey_id` | str | auto | Si no se pasa, usa el último survey de la región |
| `fuente` | str | `"auto"` | `"auto"` \| `"capital"` \| `"provincia"` |
| `batch_size` | int | 500 | Parcelas por página WFS |
| `delay_ms` | int | 300 | Pausa entre páginas (ms) |

## Output esperado

```json
{
  "ok": true,
  "parcelas_insertadas": 3420,
  "parcelas_actualizadas": 0,
  "fuera_zona": 128,
  "fuente_usada": "IDEMSA Capital",
  "bbox_usado": "-65.4120,-24.8790,-65.3890,-24.8560",
  "error": null
}
```

## Pipeline Salta (post-descarga)

Después de este agente, ejecutar en orden:

```bash
# 1. Footprints de edificios (independiente)
python3 -m scrapitero.rpc.osm_building_fetcher & <<< '{"region_id":"...","survey_id":"..."}'

# 2. Completar direcciones con Google Maps (es-AR)
python3 -m scrapitero.rpc.address_resolver <<< '{"region_id":"...","survey_id":"...","batch_size":200}'

# 3. Clasificar uso residencial/comercial/mixto
python3 -m scrapitero.rpc.uso_classifier <<< '{"region_id":"...","survey_id":"..."}'

# 4. Exportar
python3 -m scrapitero.rpc.relevamiento_csv <<< '{"region_id":"...","survey_id":"..."}'
```

## Si falla

| Error | Causa probable | Solución |
|-------|----------------|----------|
| `Timeout` | WFS IDESA lento | Reintentár en 5 min o usar `"fuente":"capital"` si aplica |
| `0 parcelas procesadas` | Bbox fuera de cobertura | Verificar que la zona sea de Salta; probar `fuente` explícita |
| `Región sin bbox` | region_id no existe | Crear la región con GeoJSONZoneFetcher primero |
| HTTP 4xx/5xx | Servicio caído | Esperar y reintentar |
