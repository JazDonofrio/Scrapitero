---
name: establecimiento-agrupador
description: "Agrupa parcelas catastrales que en realidad son UN solo establecimiento (una fábrica, colegio, iglesia o galpón comercial sobre varios lotes) y lo cuenta como 1 unidad funcional en vez de N. Detecta el clúster por propietario real (CPF/CNPJ del BCI) + contigüidad geométrica + uso no enteramente residencial. Escribe la tabla establecimientos y vincula las parcelas miembro con establecimiento_id."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, brasil, varzea-grande, uf, establecimiento, agrupacion, entity-resolution]
    category: scrapitero
---

# EstablecimientoAgrupador — N parcelas de una misma entidad = 1 UF

Una fábrica/colegio/iglesia/galpón puede ocupar **varias parcelas catastrales**. Sin
agrupar, el sistema las cuenta como N UF (o N baldíos) cuando son **1 sola unidad
funcional**. Este agente las detecta y las cuenta como 1.

## Cuándo usar
- **Después de `bci-parser`** (necesita `uso_principal`, `uf_*` y `propietario_documento`
  ya cargados en las parcelas).
- En el flujo de Várzea Grande / Brasil, antes de exportar.

## Regla de detección (conservadora)
Se agrupan parcelas que cumplen **las tres**:
1. **Mismo `propietario_documento` real** (se descartan sentinelas como `999.999.999-99` y NULL).
2. **Contiguas** — componente conexa por `ST_DWithin` (default ≤ 2 m).
3. **Uso no enteramente residencial**, distinguiendo:
   - **CPF (persona física):** sólo se agrupan parcelas con actividad real
     (comercial/industrial/mixto/equipamiento); sus **viviendas nunca se absorben**.
   - **CNPJ (persona jurídica):** se agrupa todo el bloque contiguo (incl. vivienda mal
     tageada o baldío del predio); excepción: bloque enteramente residencial no se agrupa.

## Correr el agente
```bash
python3 -m scrapitero.rpc.establecimiento_agrupador <<< '{"region_id":"<REGION_ID>","survey_id":"<SURVEY_ID>"}'
```

## Output esperado
```json
{
  "ok": true,
  "establecimientos": 6,
  "parcelas_agrupadas": 12,
  "uf_ahorradas": 11,
  "por_tipo": {"establecimiento": 5, "fabrica": 1}
}
```

## Parámetros
| Campo | Default | Descripción |
|-------|---------|-------------|
| `region_id` | ✅ | ID de la región |
| `survey_id` | último de la región | Survey a procesar |
| `max_dist_m` | 2.0 | Distancia máx. (m) entre parcelas para considerarlas contiguas |
| `min_parcelas` | 2 | Mínimo de parcelas para formar un establecimiento |

## Notas
- **Idempotente por survey:** cada corrida borra los establecimientos previos del survey y recalcula.
- La UF del establecimiento = la de su **parcela más desarrollada** (mín. 1), no la suma: una
  fábrica sobre 6 lotes de 1 UF cuenta 1, pero una parcela con `uf_comercio=5` no se colapsa
  (hereda las 5). El conteo de la web/CSV/reporter excluye las parcelas miembro y suma el
  establecimiento una sola vez.
- Las parcelas miembro **conservan** sus datos (geometría, dirección, etc.) y quedan
  vinculadas por `parcelas.establecimiento_id`.
- Tipifica por razón social: `INDÚSTRIA`→fabrica, `IGREJA`→iglesia, `COLÉGIO/ESCOLA`→colegio,
  `PREFEITURA`→equipamiento_publico, etc.
