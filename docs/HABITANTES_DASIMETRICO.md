# Estimación de habitantes por manzana — desagregación dasimétrica

Estimación **secundaria** del relevamiento principal: dice cuántos habitantes hay por
**manzana catastral**. Es **menos exacta** que la UF (que sale de catastro/PDF/footprints),
por eso se guarda y se muestra **aparte**, con su **fecha de estimación**. No modifica
`parcelas`; escribe en la tabla `manzanas_habitantes` (migración 011).

Agente: `DasymetricPopulation` (`src/scrapitero/agents/dasymetric_population.py`,
RPC `dasymetric_population`). Skill: `habitantes-dasimetrico`.

## Idea
La desagregación dasimétrica toma un total agregado (la población de un polígono censal)
y lo **reparte** a unidades más finas usando datos auxiliares que indican *dónde* y *cuánta*
gente vive. Acá el agregado es `setores_censitarios.pop_total` y el dato auxiliar es la
señal de ocupación de cada parcela.

## Datos necesarios (genérico, cualquier país)
1. **Censo**: polígonos en `setores_censitarios` con `pop_total` que **cubran la zona**.
   El cruce parcela↔setor es **espacial por geometría** (`ST_Contains`), no por `region_id`
   — los setores pueden estar cargados bajo otra región que englobe la zona (p.ej. el
   municipio entero). En Brasil los baja `ibge_census_fetcher`.
2. **Parcelas** con geometría y, deseablemente, `uf_vivienda` / uso.
3. **Manzana catastral** derivable de la fuente, vía `manzana_catastral.py`
   (hoy: `arba_carto`, `arba_idera`, `salta_idemsa`, `smartgis_vg`).

Si falta alguno, el agente devuelve `ok=false` con el detalle de qué falta.

## Método
1. **Peso de ocupación por parcela** (cascada):
   - `uf_vivienda` si está (autoritativo; `0` ⇒ sin viviendas ⇒ sin residentes),
   - si no, **volumen edificado** `Σ(area_m2 × pisos)` de `edificios`,
   - si no, **área de la parcela** sólo si el uso es residencial/mixto/desconocido,
   - comercial / industrial / vacante ⇒ peso 0.
2. **Corrección por cobertura areal**: a cada setor se le asigna sólo la fracción de su
   población proporcional al área de la zona que cae dentro:
   `pop_asignable = pop_total × min(1, área_parcelas_en_setor / área_setor)`.
   Evita volcar toda la población de un setor a unas pocas parcelas cuando la zona lo
   cubre parcialmente.
3. **Reparto**: `habitantes_parcela = pop_asignable_setor × peso / Σ pesos del setor`.
4. **Agregación por manzana**: Σ habitantes, banda `±30 %` (low/high), Σ `uf_vivienda`,
   Σ `uf_comercio`, nº de parcelas, geometría = unión de las parcelas.

## Limitaciones
- Es una estimación gruesa: la banda `±30 %` lo refleja.
- La calidad depende de la cobertura de `uf_vivienda`. Correr `UnidadesEstimator` antes
  mejora el reparto; sin uso/UF, el fallback por área puede **inflar lotes grandes sin
  clasificar**.
- El total estimado en la zona ≈ Σ(`pop_total × cobertura`) de los setores que la tocan;
  no es Σ`pop_total` completo salvo que la zona cubra los setores enteros.

## Uso
```bash
python3 -m scrapitero.rpc.dasymetric_population <<< '{"region_id":"<region>"}'
```
O desde la Web: detalle del relevamiento → sección **"👥 Habitantes por manzana"** →
**"▶ Estimar habitantes"**.

## Soportar una fuente nueva
Agregar el parser de manzana en `src/scrapitero/agents/manzana_catastral.py` y registrarlo
en `_PARSERS` por `fuente_parcela`. El resto del agente es genérico.
