---
name: habitantes-dasimetrico
description: "Estimación ADICIONAL de habitantes por manzana catastral mediante desagregación dasimétrica: reparte la población de los polígonos censales (setores_censitarios) entre las parcelas usando un peso de ocupación (uf_vivienda → volumen edificado → área residencial) y agrega por manzana. Genérica para cualquier país que tenga censo + parcelas. Es secundaria al relevamiento principal (menos exacta) y se muestra aparte, con su fecha."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, habitantes, poblacion, dasimetrico, manzana, censo, estimacion]
    category: scrapitero
---

# Habitantes por manzana (dasimétrico)

Estimación **adicional y secundaria** del relevamiento principal. Reparte la población
de los polígonos censales entre las parcelas y la agrega por **manzana catastral**.
Es **menos exacta** que la UF del relevamiento → se guarda y se muestra aparte, con la
**fecha de estimación**. No toca la tabla `parcelas`; escribe en `manzanas_habitantes`.

## Cuándo usarlo
Cuando el usuario quiere saber **cuántos habitantes hay por manzana**. Es opcional:
no es parte del pipeline principal. Conviene correrlo **después** de tener uso/UF
(UnidadesEstimator) para que el peso sea mejor.

## Datos que necesita (si faltan, falla con mensaje claro diciendo qué falta)
- Polígonos censales con población en `setores_censitarios` que **cubran la zona**
  (el join es espacial por geometría, no por region_id; en Brasil los carga
  `ibge_census_fetcher`, en otros países el equivalente).
- Parcelas con geometría y, idealmente, `uf_vivienda`/uso.
- Una **fuente con parser de manzana catastral**: hoy `arba_carto`, `arba_idera`,
  `salta_idemsa`, `smartgis_vg`. Para una fuente nueva, agregar su parser en
  `src/scrapitero/agents/manzana_catastral.py`.

## Método (dasimétrico ponderado)
1. Peso de ocupación por parcela, en cascada: `uf_vivienda` (autoritativo) → volumen
   edificado `Σ(area×pisos)` → área de parcela residencial; comercial/industrial/vacante = 0.
2. Corrección por **cobertura areal**: a cada setor se le asigna sólo la fracción de su
   población proporcional al área de la zona que cae dentro (evita volcar toda la
   población de un setor a unas pocas parcelas si la zona lo cubre en parte).
3. `habitantes_parcela = pop_asignable_setor × peso / Σ pesos del setor`.
4. Agrega por manzana: Σ habitantes (banda ±30 %), Σ uf_vivienda, Σ uf_comercio, nº parcelas.

## Comando
**No instalar nada. El venv ya está listo.**
```bash
python3 -m scrapitero.rpc.dasymetric_population <<< '{"region_id":"zona-varzea-sector-sup"}'
```
Con survey explícito:
```bash
python3 -m scrapitero.rpc.dasymetric_population <<< '{"region_id":"...","survey_id":"<UUID>"}'
```

## Output esperado
```json
{
  "ok": true,
  "manzanas": 51,
  "parcelas_procesadas": 655,
  "parcelas_sin_setor": 0,
  "parcelas_sin_manzana": 0,
  "habitantes_total": 1611.2,
  "pop_total_referencia": 4870,
  "metodo_dominante": "area_residencial",
  "fecha_estimacion": "2026-06-07"
}
```

## Desde la Web
En el detalle de cada relevamiento hay una sección aparte **"👥 Habitantes por manzana"**
con el botón **"▶ Estimar habitantes"** (corre este agente) y una tabla por manzana
(Habitantes ≈ · rango · UF Viv · UF Com · Parcelas) con el total y la fecha. Está marcada
como estimación adicional, menos exacta.

## Importante
- Es una **segunda estimación**, no pisa la UF ni los habitantes del relevamiento principal.
- La calidad depende de la cobertura de `uf_vivienda`: con uso/UF estimado da mejor reparto
  que el fallback por área (que puede inflar lotes grandes sin clasificar).
