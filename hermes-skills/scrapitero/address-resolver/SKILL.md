---
name: address-resolver
description: "Resuelve direcciones faltantes o incompletas en parcelas de cualquier país usando Google Maps Geocoding API. Rellena calle y/o número a partir de las coordenadas. Funciona para Argentina, Brasil y cualquier otra zona. El idioma de respuesta se detecta automáticamente del region_id."
version: 2.2.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, direcciones, geocoding, google-maps, catastro, argentina, brasil]
    category: scrapitero
---

# Address Resolver — Geocoding inverso para cualquier país

Toma parcelas que tienen coordenadas pero sin dirección completa y las resuelve
usando Google Maps Geocoding API (reverse geocoding).

Cubre dos casos:
- **Sin calle:** resuelve dirección completa (calle + número + barrio + municipio + CP)
- **Con calle pero sin número:** solo completa el número y barrio (no sobreescribe la calle)

El idioma de respuesta es automático según el `region_id`:
- Termina en `-br` → `pt-BR`
- Termina en `-ar` → `es-AR`
- Otro → `es`

Para Brasil, intenta primero interpolación IBGE (gratis) antes de llamar a Google.

## Cuándo usar
- Cuando `coverage-reporter` devuelve `parcelas_con_direccion / parcelas < 0.90`
- Cuando hay parcelas con calle pero sin número (dirección parcial)
- Para cualquier país — no requiere datasets locales previos

## Requisito
La variable `GOOGLE_MAPS_API_KEY` debe estar en `/opt/scrapitero/.env`.
Costo: USD 0.005 por llamada (Google SKU: Geocoding).

## ⚠ Modo ancla: comprar anclas, no lotes
Preguntarle el número a Google lote por lote es pagar por algo que la calle ya contiene: las
alturas crecen ordenadas sobre el eje. Con `anclas_por_calle` se pide **una parcela cada 15**
(mín. 3, tope 60), repartidas sobre el eje de la calle, y `numero-estimator` interpola el resto.

Medido en Merlo (17-sep-2026): **1.806 consultas en vez de 18.546** para las mismas parcelas —
entra en el tramo gratuito mensual de Google (10.000 por SKU) en vez de costar ~USD 93.
Validado contra un test independiente de parcelas interpoladas que no fueron ancla: **mediana 10
· p90 89 · 85% ≤50**. Se itera: cada ronda numera, y las que quedan piden anclas nuevas
(1.806 → 862 → 664). Las parcelas **sin calle** van aparte con `fill_partial:false`, porque sin
calle no hay eje donde interpolar.

```bash
python3 -m scrapitero.rpc.address_resolver <<< '{"region_id":"<R>","survey_id":"<S>","anclas_por_calle":true,"batch_size":2000}'
python3 -m scrapitero.rpc.numero_estimator  <<< '{"region_id":"<R>","survey_id":"<S>"}'
```

## ⚠ En Argentina, correr `direccion-ar` ANTES
Fuera de Brasil este agente no tiene capa gratuita: va derecho a Google. En PBA la calle y el
número los da `direccion-ar` sin costo, y este agente queda para lo que esa fuente no cubre.

**No pregunta lo que una fuente gratuita ya contestó** (`saltear_con_numero_util`, default on):
el selector busca `numero IS NULL`, y en PBA eso es casi todo el relevamiento porque ARBA no
publica el domicilio del lote — el número gratuito vive en `numero_estimado`. Medido en Merlo:
94.370 parcelas entraban al filtro y **74.060 ya tenían número utilizable**, así que una muestra
de 500 consultas se gastaba entera preguntando lo ya sabido. Para medir antes de gastar, usar
`batch_size` chico + `orden_aleatorio` (sin eso el `LIMIT` sigue el orden físico de la tabla y
la muestra sale toda de una cuadra). Medido así en Merlo con 500 consultas (USD 2,50): 497
resueltas, números coherentes en el espacio (vecinos a <60 m difieren 18 de mediana, 3% pasa
de 100).

## Comando
```bash
python3 -m scrapitero.rpc.address_resolver <<< '{"region_id":"<REGION_ID>"}'
```

Con parámetros completos:
```bash
python3 -m scrapitero.rpc.address_resolver <<< '{
  "region_id": "ituzaingo-ba-ar",
  "batch_size": 200,
  "delay_ms": 50,
  "fill_partial": true
}'
```

## Output esperado
```json
{
  "ok": true,
  "parcelas_procesadas": 150,
  "parcelas_resueltas": 138,
  "parcelas_resueltas_logradouros": 40,
  "parcelas_resueltas_google": 98,
  "parcelas_sin_resultado": 12,
  "costo_estimado_usd": 0.49,
  "error": null
}
```

## Parámetros
| Campo | Default | Descripción |
|-------|---------|-------------|
| `region_id` | ✅ | ID de la región |
| `survey_id` | null | Filtrar por survey específico |
| `batch_size` | 100 | Parcelas a procesar por corrida |
| `delay_ms` | 50 | Milisegundos entre llamadas (evita quota) |
| `fill_partial` | true | También rellenar parcelas con calle pero sin número |

## Si falla
- `GOOGLE_MAPS_API_KEY` no definida → agregar a `/opt/scrapitero/.env`
- Quota excedida → reducir `batch_size` o aumentar `delay_ms`
- `parcelas_procesadas == 0` → no hay parcelas con coordenadas y dirección incompleta
