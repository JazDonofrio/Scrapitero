---
name: salta-zonificacion-fetcher
description: "Clasifica uso_principal de parcelas de Salta Capital por zonificación CPUA 2019 (residencial/comercial/mixto/industrial/equipamiento/vacante). Fuente: IDEMSA WFS público, sin costo. Usar después de salta-catastro-fetcher cuando uso_principal == null."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, salta, argentina, clasificacion, uso, zonificacion, cpua]
    category: scrapitero
---

# Salta Zonificacion Fetcher

Clasifica el campo `uso_principal` de las parcelas de Salta Capital usando la
capa de zonificación CPUA 2019 (Código de Planeamiento Urbano Ambiental),
disponible como WFS público en IDEMSA.

## Fuente de datos

| Propiedad | Valor |
|-----------|-------|
| URL | geocloud.municipalidadsalta.gob.ar/geoserver/wfs |
| Layer | public:zonificaicon_usos_del_suelo102019 |
| Cobertura | Ciudad de Salta Capital |
| Autenticación | Ninguna (público) |
| Polígonos | 178 distritos CPUA |

## Cuándo usar

Después de `salta-catastro-fetcher` cuando las parcelas tienen `uso_principal = null`.

**No requiere** datos adicionales: descarga la zonificación sola y hace la
clasificación por intersección espacial (centroide de parcela vs polígono de zona).

## Mapeo de distritos

| Distrito | uso_principal |
|---------|---------------|
| R1, R2, R3, R4, R5, R6, Apto R6 | `residencial` |
| R3/R5_Corredor Comercial, M1-M6, MA, AC*, Corredor Belgrano | `mixto` |
| NC1, NC2, NC3, NC4 | `comercial` |
| PI | `industrial` |
| AGR, Area Rural | `vacante` |
| AE-*, EP, PSM, Espacios Verdes, Red Vial | `equipamiento` |

Parcelas fuera de cobertura CPUA (interior provincial) → quedan `sin_datos`.

## UF mínimas por uso

Al clasificar, además del `uso_principal` se computan `unidades_funcionales_estimadas`
**y `uf_vivienda`**:

| uso_principal | UF computadas |
|---------------|---------------|
| `residencial` | **siempre al menos 1** UF de vivienda — `unidades_funcionales_estimadas` y `uf_vivienda` con `GREATEST(actual, 1)` (no pisa un conteo real mayor; una vivienda mínima por parcela) |
| `vacante` | **0** (terreno baldío, sin unidad) |
| resto (comercial/mixto/industrial/equipamiento) | sin tocar |

`salta-rentas-fetcher` puede corregir después un residencial a `vacante` (UF → 0) si
el `valorEdificado` es ≈ 0.

**Origen del uso:** al clasificar se setea también `uso_fuente = 'cpua'` (queda registrado en
el relevamiento final, columna **Uso Fuente** del CSV). Si después `salta-rentas-fetcher` lo
corrige a vacante, pasa a `uso_fuente = 'rentas'`.

## Comando

```bash
python3 -m scrapitero.rpc.salta_zonificacion_fetcher <<< '{"region_id":"{region_id}","survey_id":"{survey_id}"}'
```

Para reclasificar parcelas que ya tienen uso:
```bash
python3 -m scrapitero.rpc.salta_zonificacion_fetcher <<< '{"region_id":"...","overwrite":true}'
```

## Parámetros

| Campo | Tipo | Default | Descripción |
|-------|------|---------|-------------|
| `region_id` | str | — | Obligatorio |
| `survey_id` | str | auto | Filtra por survey |
| `overwrite` | bool | false | true = reclasifica todas; false = solo las sin uso |

## Output esperado

```json
{
  "ok": true,
  "parcelas_procesadas": 18,
  "parcelas_clasificadas": 18,
  "parcelas_sin_cobertura": 0,
  "distribucion": {
    "residencial": 14,
    "comercial": 2,
    "mixto": 2
  },
  "error": null
}
```

## Limitaciones

- Cobertura **solo ciudad de Salta Capital** — interior provincial sin datos
- Clasifica por **zona** (no por parcela individual) → todas las parcelas de
  un mismo distrito CPUA reciben la misma clasificación
- Para clasificación exacta por parcela (con UF) → se requiere acceso a
  inmuebles.gov.ar o SIGSA Extranet (pendiente)
