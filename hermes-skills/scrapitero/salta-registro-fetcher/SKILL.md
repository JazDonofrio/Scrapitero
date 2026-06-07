---
name: salta-registro-fetcher
description: "Clasifica parcelas de Salta por TIPO del registro parcelario SIGSA (URBANO/RURAL/CLUB DE CAMPO). Cobertura provincial completa, sin autenticación. RURAL→vacante, CLUB DE CAMPO→residencial. Complementa a salta-zonificacion-fetcher cubriendo el interior provincial."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, salta, argentina, registro, sigsa, clasificacion, uso]
    category: scrapitero
---

# Salta Registro Fetcher

Consulta el registro parcelario del SIGSA (ArcGIS REST público, sin auth) y
clasifica parcelas según el campo `TIPO` ∈ {URBANO, RURAL, CLUB DE CAMPO}.

## Fuente

| Propiedad | Valor |
|-----------|-------|
| URL | sigsa.inmuebles.gov.ar/.../ConsultaParcelas/FeatureServer/4 |
| Tabla | DGI_GIS.CONSULTA_PARCELARIA (334.090 registros) |
| Cobertura | **Toda la provincia de Salta** |
| Autenticación | Ninguna (público) |
| Join | por `VINCULACION` = `nomenclatura_catastral` (única por parcela) |

## Mapeo TIPO → uso_principal

| TIPO | uso_principal |
|------|---------------|
| RURAL | `vacante` (terreno rural sin edificación urbana) → **0 UF** |
| CLUB DE CAMPO | `residencial` (loteo cerrado) → **siempre al menos 1 UF de vivienda** (`unidades_funcionales_estimadas` y `uf_vivienda` con `GREATEST(actual, 1)`) |
| URBANO | *(sin cambio — lo resuelve CPUA / salta-zonificacion-fetcher)* |

Cuando setea uso (RURAL/CLUB DE CAMPO) registra también `uso_fuente = 'sigsa'` — el origen del
uso queda en el relevamiento final (columna **Uso Fuente** del CSV).

## Cuándo usar

- **Interior provincial:** es la única señal de uso disponible (el CPUA solo cubre Capital).
- **Capital:** confirma que las parcelas son URBANO y las deja para `salta-zonificacion-fetcher`.

Usar después de `salta-catastro-fetcher` (necesita `nomenclatura_catastral`).

## Comando

```bash
python3 -m scrapitero.rpc.salta_registro_fetcher <<< '{"region_id":"{region_id}","survey_id":"{survey_id}"}'
```

`overwrite:true` para reclasificar; `batch_size` (default 100) nomenclaturas por request.

## Output

```json
{
  "ok": true,
  "parcelas_consultadas": 34,
  "parcelas_clasificadas": 0,
  "distribucion_tipo": {"URBANO": 34},
  "distribucion_uso": {},
  "sin_match": 0,
  "error": null
}
```

## Relación con las otras skills de Salta

```
salta-catastro-fetcher      → geometría + nomenclatura + cca_code
salta-registro-fetcher      → TIPO provincial (rural/club → uso; urbano → defer)
salta-zonificacion-fetcher  → uso urbano por zona CPUA (Capital)
```

Orden recomendado: registro primero (barato, marca rural/club), luego CPUA para
el resto urbano. Ambos respetan parcelas ya clasificadas salvo `overwrite:true`.

## Nota técnica

El SIGSA usa SSL con renegociación legacy → el agente configura un SSLContext
con `OP_LEGACY_SERVER_CONNECT`. No requiere cambios de entorno.
