---
name: uso-classifier
description: "Clasifica parcelas como residencial, comercial o mixto y reparte la UF de ARBA entre vivienda y comercio con Google Places. Último paso de UF del flujo PBA. Usar cuando el usuario pregunta cuántas UFs son vivienda vs comercio/oficina."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, clasificacion, uso, vivienda, comercio, mixto, google-places, argentina]
    category: scrapitero
---

# Uso Classifier

Clasifica las parcelas de un relevamiento en residencial / comercial / mixto.

**Cómo funciona:**
1. **ARBA (catastral)**: `arba-carto-fetcher` cuenta las subparcelas (≥25 m² → UF, menores →
   cochera) y deja la **UF total** del lote en `uf_vivienda`. ⚠ ARBA **no dice el destino**:
   el campo `sp` es el **número** de subparcela, no el uso — de ARBA sale el *cuántas*, nunca
   el vivienda-vs-comercio.
2. **Google Places API**: única señal de comercio (radio 15 m ≈ la propia parcela). Lo que
   confirma se **descuenta** del total de ARBA, no se suma encima: una casa con local al
   frente no gana una UF.

El resultado se guarda en `uso_principal` (`residencial`/`comercial`/`mixto`/`sin_datos`) **y
en el desglose `uf_vivienda`/`uf_comercio`** (`uf_fuente='clasificador'`). Es el **último paso
de UF del flujo PBA**: sin él la web y el CSV muestran 0 UF.

## Cuándo usar
- **Paso ESTÁNDAR del flujo PBA (Buenos Aires):** correr siempre que haya `parcelas > 0`
  y `uso_principal` null en una región de Buenos Aires. PBA no tiene fuente nativa de uso
  (no hay CPUA como Salta ni BCI como Brasil); sin este paso las parcelas quedan "sin
  clasificar". **Correr `arba-carto-fetcher` antes** para que las parcelas tengan UF
  (`uf_vivienda`/`uf_comercio`); si entraron solo por IDERA (geometría sin UF), la
  clasificación cae a Google Places / `sin_datos`.
- También bajo pedido: "cuántas UFs son vivienda y cuántas son comercios?", "clasificá el
  uso de las parcelas", "hay locales comerciales en la manzana?".

Requiere `GOOGLE_MAPS_API_KEY`.

## Comando

```bash
python3 -m scrapitero.rpc.uso_classifier <<< '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>"}'
```

Sin survey_id (todas las parcelas de la región):
```bash
python3 -m scrapitero.rpc.uso_classifier <<< '{"region_id":"ituzaingo-ba-ar"}'
```

## Parámetros opcionales
- `delay_ms` (default 500): milisegundos entre requests a Places API
- `batch_notify` (default 10): cada cuántas parcelas loguear progreso

## Output esperado
```json
{
  "ok": true,
  "parcelas_procesadas": 26,
  "residencial": 20,
  "comercial": 2,
  "mixto": 4,
  "sin_datos": 0,
  "error": null
}
```

## Notas
- Requiere `GOOGLE_MAPS_API_KEY` en `.env`
- Correr **después** de `arba-carto-fetcher` (necesita la UF de ARBA ya cargada)
- ⚠ **Si el output da `residencial: 0` y todo cae en `sin_datos`/`comercial`**, las parcelas
  no tienen la UF de ARBA cargada: correr `arba-carto-fetcher` antes y volver a intentar.
  Fue el modo de falla del relevamiento de Hurlingham (2026-08-10)
- Va despacio por diseño (delay entre requests para no saturar Places API)
- El resultado queda persistido en la DB — no hace falta correrlo de nuevo salvo que cambien los datos de ARBA
