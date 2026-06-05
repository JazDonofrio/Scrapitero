---
name: unidades-estimator
description: "Estima la cantidad de unidades de vivienda (uf_vivienda) y de comercio (uf_comercio) por parcela. NO cuenta edificios: combina tags OSM (building:flats/levels/tipo) con proxy geométrico (área×pisos/tamaño_típico) y fallback por uso. Usar como último paso del flujo Salta, después de osm-building-fetcher y de la clasificación de uso. NO usar en regiones de Brasil con UF exacta de BCIParser."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, salta, argentina, unidades, vivienda, comercio, uf, estimacion, osm]
    category: scrapitero
---

# Unidades Estimator

Determina la **cantidad de unidades funcionales** (viviendas y locales comerciales)
de cada parcela. El objetivo NO es contar edificios, sino estimar `uf_vivienda` y
`uf_comercio`.

> El conteo **exacto** de UF no existe en ninguna fuente gratuita de Salta/Argentina
> (el catastro modela cada UF como clave independiente; el agrupamiento real solo
> está en la cédula paga de inmuebles.gov.ar). Este agente **estima**.

## Metodología — OSM tags first, proxy geométrico fallback

Por cada parcela, agrega sus edificios (vinculados por `osm-building-fetcher`) y:

1. **OSM tags first**: si el edificio tiene `building:flats` / `addr:units`
   (`unidades_osm`) → usa ese número real de viviendas.
2. **Vivienda unifamiliar**: `building=house|detached|bungalow|villa…` → 1 UF.
3. **Proxy geométrico (solo edificios en altura)**: si el edificio tiene altura real
   (`building:levels ≥ 2` o tipo `apartments`) → `UF = (área_footprint × pisos) / tamaño_típico`
   (vivienda 80 m², comercio 50 m²). Un edificio de **1 sola planta** sin tag multi-unidad
   cuenta como **1 UF**: no se subdivide la huella (evita sobrestimar casas/locales grandes).
4. **Categoría vivienda/comercio**: por `tipo_osm` (`building=*`); si el edificio no
   está tipado, cae al `uso_principal` de la parcela (CPUA / registro SIGSA).
5. **Fallback de cobertura**: parcela sin edificios OSM (hueco frecuente en el interior
   provincial) → mínimo por uso: residencial→1 vivienda, comercial→1 comercio, mixto→1 vivienda.

> **Regla `mixto`: vivienda O comercio (excluyente).** En una parcela mixto cada UF es
> vivienda **o** comercio, nunca ambas a la vez. Cada edificio se asigna a una sola
> categoría por su tag; los no tipados y el fallback sin edificios → vivienda por defecto.
> Una parcela mixto con edificios solo comerciales queda con uf_comercio>0 y uf_vivienda=0.

## Reglas mínimas por uso

| uso_principal | UF |
|---------------|----|
| `residencial` | al menos **1** vivienda (`GREATEST(uf_vivienda,1)`) |
| `comercial` | al menos **1** comercio |
| `mixto` | al menos **1 UF total** (vivienda O comercio; default vivienda si no hay dato) |
| `vacante` | **0** (baldío) |
| `industrial` / `equipamiento` | **0** vivienda y **0** comercio |

> Lógica completa documentada en `docs/ESTIMACION_UF.md`.

## Marcado exacto vs estimado (`uf_fuente`)

Escribe `parcelas.uf_fuente` para que la web distinga el origen de la UF:

| `uf_fuente` | Significado |
|-------------|-------------|
| `osm` | estimado — algún edificio aportó conteo real (`building:flats`/`addr:units`) |
| `proxy` | estimado — proxy geométrico (área×pisos/tamaño) |
| `uso` | estimado — mínimo por uso (parcela sin edificios OSM) |
| `bci` | **exacto** — lo setea `bci-parser` (Brasil), no este agente |

La web muestra las UF con `uf_fuente ≠ bci` como estimación (badge `est.`, prefijo `≈`).

## Narrativa en el cuadro de actividad

El agente loguea (vía `scrapitero.*`, visible en el cuadro **Actividad** del survey) las
**situaciones particulares** que explican el número final, una línea por parcela no trivial:
conteo real OSM, edificio en altura estimado por proxy, o footprint grande de 1 planta
acotado a 1 UF. Cierra con un resumen "Cómo se llegó al número: N conteo real OSM, N proxy
multi, N footprint grande acotado, N sin edificios OSM (mínimo por uso)". Las parcelas
triviales (1 vivienda unifamiliar / mínimo por uso) no se loguean individualmente.

## Requisitos previos

1. `salta-catastro-fetcher` → parcelas con geometría
2. `osm-building-fetcher` → footprints **con tags y vinculados a parcela** (parcela_id)
3. `salta-zonificacion-fetcher` y/o `salta-registro-fetcher` → `uso_principal` clasificado

## Cuándo NO usar

En regiones de **Brasil** donde `bci-parser` ya extrajo UF exactas de los PDFs BCI:
este agente las pisaría con una estimación. Es solo para fuentes sin UF exacta (Salta).

## Comando

```bash
echo '{"region_id":"{region_id}","survey_id":"{survey_id}"}' |
  python3 -m scrapitero.rpc.unidades_estimator
```

Recalcular todo (incluso parcelas ya estimadas):
```bash
echo '{"region_id":"...","overwrite":true}' |
  python3 -m scrapitero.rpc.unidades_estimator
```

Ajustar supuestos del proxy geométrico:
```bash
echo '{"region_id":"...","m2_vivienda":90,"m2_comercio":60,"pisos_default":2}' |
  python3 -m scrapitero.rpc.unidades_estimator
```

## Parámetros

| Campo | Tipo | Default | Descripción |
|-------|------|---------|-------------|
| `region_id` | str | — | Obligatorio |
| `survey_id` | str | auto | Filtra por survey |
| `overwrite` | bool | false | true = recalcula todas; false = solo parcelas sin estimar (`footprints_count=0`) |
| `m2_vivienda` | float | 80.0 | Tamaño típico de vivienda (proxy) |
| `m2_comercio` | float | 50.0 | Tamaño típico de local comercial (proxy) |
| `pisos_default` | int | 1 | Pisos asumidos si OSM no trae `building:levels` |

## Output esperado

```json
{
  "ok": true,
  "parcelas_procesadas": 52,
  "parcelas_con_edificios": 18,
  "parcelas_fallback_uso": 34,
  "total_uf_vivienda": 71,
  "total_uf_comercio": 23,
  "fuente_unidades_osm": 4,
  "error": null
}
```

## Limitaciones

- Es una **estimación**, no un conteo registral. La señal más fuerte (`building:flats`)
  cubre pocos edificios en Salta; la mayoría son edificios de planta baja → 1 UF c/u.
- El proxy depende de `building:levels` de OSM (escaso): sin altura mapeada un edificio
  cuenta como 1 UF (conservador, evita sobrestimar). Si una zona tiene edificios en altura
  no mapeados, subir `pisos_default` ≥ 2 para que el proxy los subdivida.
- `mixto` sin tags reparte 1 vivienda + 1 comercio (no conoce la proporción real).
