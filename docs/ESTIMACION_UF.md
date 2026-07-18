# Estimación de Unidades Funcionales (UF) — vivienda y comercio

> Documento de referencia de toda la lógica con la que Scrapitero determina la
> **cantidad de unidades de vivienda (`uf_vivienda`) y de comercio (`uf_comercio`)**
> por parcela. Última actualización: 2026-06-03.

---

## 1. Objetivo y principio

El sistema **siempre** busca determinar la cantidad de UF de vivienda y de comercio de
cada parcela. Hay dos caminos:

1. **Exacto** — cuando existe una fuente registral por parcela (hoy: PDFs BCI de Brasil,
   vía `BCIParser`).
2. **Estimado** — cuando no hay fuente exacta gratuita (caso Argentina/Salta). Se estima
   con `UnidadesEstimator`, y **la estimación se muestra como tal en la web**.

> **Por qué se estima en Salta:** el conteo exacto de UF no existe en ninguna fuente
> gratuita. El catastro modela cada UF como clave independiente; el agrupamiento real
> solo está en la cédula paga de inmuebles.gov.ar. Ver memoria
> `project_salta_fuentes_uso_uf`.

---

## 2. De dónde sale cada dato

| Dato | Fuente | Agente |
|------|--------|--------|
| Geometría de la parcela | WFS catastro (IDEMSA/IDESA) | `SaltaCatastroFetcher` |
| `uso_principal` (resid/comercial/mixto/…) | Zonificación CPUA + registro SIGSA | `SaltaZonificacionFetcher`, `SaltaRegistroFetcher` |
| Footprints de edificios + tags | OpenStreetMap (Overpass) | `OSMBuildingFetcher` |
| **uf_vivienda / uf_comercio (estimado)** | Edificios OSM + uso + proxy | **`UnidadesEstimator`** |
| uf_vivienda / uf_comercio (exacto) | PDF BCI | `BCIParser` (solo Brasil) |

### Tags de OSM que captura `OSMBuildingFetcher`

| Tag OSM | Columna en `edificios` | Uso en la estimación |
|---------|------------------------|----------------------|
| `building=*` | `tipo_osm` | categoría vivienda/comercio (`yes`/`true` → null) |
| `building:levels` | `pisos_estimados` | pisos para el proxy geométrico |
| `building:flats` / `addr:units` | `unidades_osm` | **conteo real** de viviendas cuando existe |

Además, cada edificio se vincula a la parcela que contiene su centroide
(`ST_Contains(parcela.geometry, edificio.centroid)` → `edificios.parcela_id`).

---

## 3. Algoritmo por parcela (`UnidadesEstimator`)

Para cada parcela con `uso_principal` clasificado y `validado_manual = false`:

```
1. Reunir los edificios OSM vinculados a la parcela.
2. Según uso_principal:
   - vacante / industrial / equipamiento → uf_vivienda = 0, uf_comercio = 0
   - residencial / comercial / mixto     → estimar (pasos 3–6)
3. Si la parcela TIENE edificios vinculados:
     por cada edificio:
       a. categoría = vivienda | comercio | (ninguna) según tipo_osm  [ver §4]
          - si "ninguna" (galpón, iglesia, escuela…) → el edificio no aporta UF
       b. nº de UF del edificio:                                      [ver §5]
          - unidades_osm presente (building:flats/addr:units) → ese número (conteo real)
          - tipo unifamiliar (house/detached/…)               → 1
          - si no                                             → proxy geométrico
       c. sumar a uf_vivienda o uf_comercio según la categoría
4. Si la parcela NO tiene edificios OSM → fallback por uso:           [ver §6]
     residencial → 1 vivienda | comercial → 1 comercio | mixto → 1 vivienda
5. Aplicar mínimos por uso.                                           [ver §6]
6. Registrar uf_fuente (osm | proxy | uso) y persistir.               [ver §7]
```

Persiste en `parcelas`: `uf_vivienda`, `uf_comercio`,
`unidades_funcionales_estimadas` (= suma de ambas), `footprints_count`,
`pisos_estimados_max`, `uf_fuente`.

---

## 4. Categorización vivienda vs comercio (por `tipo_osm`)

| `building=*` | Categoría |
|--------------|-----------|
| apartments, residential, house, detached, semidetached_house, terrace, dormitory, bungalow, cabin, houseboat, static_caravan, farm, hut, ger, villa | **vivienda** |
| commercial, retail, supermarket, kiosk, shop, office, hotel, restaurant, warehouse_retail | **comercio** |
| industrial, warehouse, garage(s), shed, carport, church, school, hospital, public, civic, government, hangar, barn, construction, ruins, roof, … | **ninguna** (no aporta UF) |
| edificio sin tag útil (`building=yes` o sin tipo) | según `uso_principal` de la parcela → ver §4.1 |

### 4.1 Fallback de categoría por `uso_principal`

Cuando el edificio no tiene un `tipo_osm` clasificable:

| `uso_principal` | Categoría asumida |
|-----------------|-------------------|
| comercial | comercio |
| residencial | vivienda |
| **mixto** | **vivienda** (ver §4.2) |

### 4.2 Regla de `mixto`: vivienda **O** comercio

En una parcela **mixto**, cada UF es **vivienda O comercio** — nunca ambas a la vez, y no
existe una "UF mixta". Implicancias:

- Cada edificio se asigna a **una sola** categoría según su `tipo_osm`.
- Edificios sin tag clasificable en una parcela mixto → **vivienda** por defecto.
- Una parcela mixto con edificios **solo comerciales** queda con `uf_comercio` > 0 y
  `uf_vivienda = 0` (no se fuerza una vivienda).
- Sin edificios OSM (fallback) → **1 vivienda** (no se suman 1 vivienda + 1 comercio).
- Mínimo garantizado: al menos **1 UF total** en la parcela.

---

## 5. Proxy geométrico (cuando no hay conteo real)

Para un edificio sin `building:flats` y que no es unifamiliar:

```
SI hay altura real (building:levels ≥ 2  O  tipo apartments):
    área_construida = area_m2 (footprint) × pisos      (pisos = building:levels o pisos_default)
    UF del edificio = max(1, round(área_construida / tamaño_típico))
SI NO (edificio de 1 sola planta, sin tag multi-unidad):
    UF del edificio = 1
```

> **Regla anti-sobrestimación (2026-06-04):** la huella de un edificio **solo se subdivide
> en varias UF cuando hay evidencia de altura** (`building:levels ≥ 2`) o el tag lo declara
> (`apartments`). Un edificio de planta baja —una casa o un local grande— es **1 unidad**, no
> varias. Antes se dividía siempre `area×pisos / 80`, lo que con `pisos_default = 1` contaba
> una casa de 180 m² como 2–3 viviendas. Como `building:levels` es escaso en OSM Salta, la
> mayoría de los edificios cae a **1 UF** (conservador), corrigiendo la sobrestimación.

| Parámetro | Default | Significado |
|-----------|---------|-------------|
| `m2_vivienda` | 80 m² | tamaño típico de una vivienda |
| `m2_comercio` | 50 m² | tamaño típico de un local comercial |
| `pisos_default` | 1 | pisos asumidos si OSM no trae `building:levels` |

El `tamaño_típico` usado es `m2_comercio` si la categoría del edificio es comercio, si no
`m2_vivienda`. Todos son ajustables por parámetro de entrada del agente.

> **Footprints: qué dato de altura/superficie hay.** `OSMBuildingFetcher` ya captura del
> footprint la **superficie** (`area_m2`, calculada proyectando a UTM) y la **altura en
> pisos** (`pisos_estimados` = `building:levels`). Ambos alimentan el proxy. OSM **no** trae
> altura en metros ni superficie por planta; `building:levels` es el único proxy de altura y
> está poco mapeado en Salta, por eso domina el caso "1 planta → 1 UF".

---

## 6. Fallback por uso y mínimos

**Fallback (parcela sin ningún edificio OSM — común en el interior provincial):**

| `uso_principal` | UF asignadas |
|-----------------|--------------|
| residencial | 1 vivienda |
| comercial | 1 comercio |
| mixto | 1 vivienda (ver §4.2) |
| vacante / industrial / equipamiento | 0 |

**Mínimos por uso (se aplican siempre, tengan o no edificios):**

| `uso_principal` | Mínimo |
|-----------------|--------|
| residencial | `uf_vivienda = max(uf_vivienda, 1)` |
| comercial | `uf_comercio = max(uf_comercio, 1)` |
| mixto | si `uf_vivienda + uf_comercio == 0` → `uf_vivienda = 1` |
| vacante / industrial / equipamiento | 0 (no se tocan) |

---

## 7. Exacto vs estimado: `parcelas.uf_fuente`

Cada parcela registra cómo se determinó su UF:

| `uf_fuente` | Tipo | Quién lo setea | Significado |
|-------------|------|----------------|-------------|
| `bci` | **exacto** | `BCIParser` | conteo extraído del PDF oficial (Brasil) |
| `osm` | estimado | `UnidadesEstimator` | algún edificio aportó conteo real (`building:flats`) |
| `proxy` | estimado | `UnidadesEstimator` | proxy geométrico (área×pisos/tamaño) |
| `uso` | estimado | `UnidadesEstimator` | mínimo por uso (parcela sin edificios OSM) |
| `NULL` | — | — | UF aún no determinada |

Cualquier valor distinto de `bci` se considera **estimación**.

---

## 8. Cómo se muestra en la web

- **KPIs del survey** ("UF Vivienda" / "UF Comercio"): si el survey tiene UF estimada
  (`uf_estimado = true`, es decir alguna parcela con `uf_fuente ≠ bci`), el valor se
  muestra con prefijo `≈` y un badge naranja `est.`.
  Al pasar el cursor sobre el KPI aparece un tooltip que explica cómo se estimó.
- **Popup de cada parcela**: muestra el origen — `exacto (BCI)` / `estimado (OSM)` /
  `estimado (proxy)` / `estimado (por uso)`. Al pasar el cursor sobre esa línea, un
  tooltip describe el método concreto de esa parcela (ver `UF_FUENTE_DESC` en index.html).
- **Mapa del survey**: los marcadores se colorean por **tipología** (no por `uso` crudo),
  combinando uso + UF estimadas: `vivienda` (resid. 1 UF), `edificio de viviendas`
  (resid. ≥2 UF), `comercio` (com. 1 UF), `edificio de comercios` (com. ≥2 UF), `mixto`,
  `vacante`. La **leyenda (referencias)** lista los **tipos de edificación de la taxonomía
  del cliente** (`tipo_edificacion`: RESIDÊNCIA, APARTAMENTO, BAR, ESCOLA, HOTEL, LOTE VAZIO…
  — ver `docs/TIPOS_PROPIEDAD.md`) con su conteo; el punto de cada línea usa el color del
  marcador (la tipología amplia). Con una comparativa activa la leyenda pasa a listar por
  estado (nueva/cambió/igual). Cada marcador lleva un **label permanente con la cantidad de
  UF totales** de la parcela (ver `categoriaMapa`, `CAT_COLOR`/`CAT_LABEL` en index.html).
- **Cuadro de actividad** ("Actividad" del survey): al correr `UnidadesEstimator` se
  registran las **situaciones particulares** que explican el número final — p.ej.
  "Caseros 549 (mixto): edificio de 2373 m² de 1 planta → 1 vivienda (no subdividido)",
  conteos reales OSM y edificios en altura estimados por proxy. Cierra con una línea
  "Cómo se llegó al número: N conteo real OSM, N proxy multi, N footprint grande acotado,
  N sin edificios OSM (mínimo por uso)".
- **CSV exportado**: incluye la columna **UF Fuente**.
- Endpoints: `/api/surveys` devuelve `uf_estimado` (bool);
  `/api/surveys/{id}/parcelas` devuelve `uf_fuente` y `uf_estimado` por parcela.

---

## 9. Orden de ejecución (flujo Salta)

```
SaltaCatastroFetcher     → parcelas con geometría
OSMBuildingFetcher       → footprints + tags OSM, vinculados a parcela
SaltaRegistroFetcher     → TIPO provincial (rural/club de campo → uso)
SaltaZonificacionFetcher → uso_principal urbano (CPUA, Capital)
SaltaRentasFetcher       → corrige baldíos (Capital)
UnidadesEstimator        → uf_vivienda / uf_comercio   ← último paso de UF
RelevamientoCSV          → exportar
```

**No ejecutar `UnidadesEstimator` en regiones de Brasil**: `BCIParser` ya extrae UF exactas
de los PDFs BCI y el estimador las pisaría.

---

## 10. Limitaciones

- Es una **estimación**. La señal más fuerte (`building:flats`) cubre muy pocos edificios
  en Salta; la mayoría cae al proxy geométrico o al fallback por uso.
- El proxy depende de `building:levels` de OSM (escaso) → por defecto asume 1 piso, lo que
  subestima edificios en altura. Ajustar `pisos_default` por zona si hace falta.
- En `mixto` sin tags no se conoce la proporción real vivienda/comercio: se asume vivienda.
- OSM tiene huecos grandes en el interior provincial → muchas parcelas terminan en `uso`
  (mínimo por uso).
- El conteo **exacto** de UF por parcela sigue sin existir gratis (ver
  `project_salta_fuentes_uso_uf`).
