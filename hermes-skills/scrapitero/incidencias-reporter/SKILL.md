---
name: incidencias-reporter
description: "Escanea el relevamiento y carga en la tabla incidencias todos los casos que sólo un humano puede resolver: construcción no declarada, UF imposible o sin declarar, diferencia entre la ficha del catastro y el censo 2022, falta de número, ubicación dudosa, hoteles sin habitaciones o duplicados y POIs en el lote equivocado. Alimenta la página /incidencias/{survey_id}. No cambia ningún dato: sólo señala dónde mirar."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, incidencias, control-calidad, censo, revision, operador]
    category: scrapitero
---

# Incidencias Reporter — qué tiene que mirar un humano

Junta en un solo lugar los casos que el dato no puede resolver solo y los deja en la tabla
`incidencias`, que es lo que ve el operador en `/incidencias/{survey_id}`.

## Tipos que genera
| Tipo | Qué detecta | Prioridad |
|------|-------------|-----------|
| `uf_sin_declarar` | hay obra declarada (o huella de edificio) y **cero unidades**: el lote aporta 0 al entregable | 1 |
| `uf_imposible` | la UF declarada no cabe en el volumen visible (m² por unidad absurdo) | 1 |
| `altura_sin_declarar` | el catastro no declara construcción y el satélite ve un edificio | 1 |
| `geocoding_dudoso` | una dirección del relevamiento anterior quedó lejos de su calle: **se revisa antes de dibujar la zona** | 1 |
| `censo_viviendas` | **la ficha del catastro y el censo 2022 no cuentan lo mismo** en el lote | 2 |
| `hotel_sin_habitaciones` / `hotel_duplicado` | hotel abierto sin UH, o el mismo hotel dos veces | 2 |
| `numero_faltante` | ni el catastro ni la interpolación dan el número de puerta | 2 |
| `altura_mas_alta` | el satélite ve más pisos que los que sugiere el catastro | 3 |
| `poi_numero_ajeno` | el comercio está en un lote cuyo número no es el que él declara | 3 |
| `revision_manual` | la abrió el operador desde el mapa (no la produce este agente) | — |

**`censo_viviendas` es el control del dato más grande del relevamiento.** La cantidad de
unidades de cada lote sale de la ficha del catastro, que cuenta inscrições y puede tener
décadas; el censo 2022 es la única fuente independiente que contó viviendas una por una, con la
coordenada tomada en la puerta y el complemento que anotó el censista («QUITINETE CASA 7»,
«BLOCO A APARTAMENTO 3»). Necesita que `establecimientos-observados` haya corrido antes: es el
que carga `censo_domicilios`. Ignora los lotes con UF corregida a mano y los especiales.

## Cuándo usar
- Al terminar de relevar una zona, y de nuevo después de cada corrida que cambie datos
  (hoteles, altura, footprints): el escaneo es idempotente.
- Antes de entregar: lo que quede `pendiente` viaja en la columna **Incidencias pendientes** del
  CSV de relevamiento.

## Correr el agente

```bash
python3 -m scrapitero.rpc.incidencias_reporter <<< '{"region_id":"<REGION_ID>","survey_id":"<SURVEY_ID>"}'
```

## Output esperado
```json
{
  "ok": true,
  "creadas": 33,
  "actualizadas": 183,
  "obsoletas": 22,
  "pendientes": 203,
  "por_tipo": {"altura_mas_alta": 50, "numero_faltante": 41, "geocoding_dudoso": 45,
               "censo_viviendas": 31, "uf_sin_declarar": 29, "hotel_sin_habitaciones": 4}
}
```

## Parámetros
| Campo | Tipo | Default | Descripción |
|-------|------|---------|-------------|
| `region_id` | string | ✅ | Región |
| `survey_id` | string | ✅ | Survey a escanear |
| `censo_dif_min` | int | 2 | Diferencia mínima entre la ficha y el censo para levantar tarjeta (en 1 manda el ruido) |
| `m2_por_uf_min` | float | 25.0 | Piso de m² por unidad para `uf_imposible` |
| `area_construida_min` | float | 30.0 | Superficie mínima para reclamar UF a un lote que declara 0 |
| `lejos_calle_m` | float | 150.0 | Distancia a su calle para `geocoding_dudoso` |
| `numero_conf_min` | float | 0.4 | Confianza mínima del número estimado (mismo valor que el export) |

## Notas
- **No pisa el trabajo del operador:** el upsert refresca el contenido del caso pero nunca
  `estado`, `resolucion` ni `nota`. Re-correr no reabre lo resuelto.
- Las pendientes cuya condición ya no se cumple pasan a `obsoleta` solas.
- Ninguna exportación filtra por incidencias: un lote con tarjeta pendiente **sale igual** al
  CSV y al plano. Por eso el CSV de relevamiento lleva la columna con los tipos pendientes.
