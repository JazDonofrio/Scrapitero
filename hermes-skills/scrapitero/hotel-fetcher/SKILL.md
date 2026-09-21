---
name: hotel-fetcher
description: "Trae los hoteles de la zona desde Cadastur (padrón oficial de hospedaje, con habitaciones), Receita (CNPJ con CNAE de hospedaje), OSM y Google, los deduplica, marca los cerrados y los vincula a su parcela. En Brasil termina llamando a hotel-reglas, que corrige la ubicación por consenso de pines y deja el lote como especial con sus habitaciones. Escribe la tabla hoteles y el uso/UF de esas parcelas."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, hoteles, cadastur, receita, osm, brasil, argentina]
    category: scrapitero
---

# Hotel Fetcher — los hospedajes de la zona

Junta hoteles de varias fuentes, los unifica en la tabla `hoteles` y le pone al lote lo que
corresponde. Es el paso que convierte "hay un hotel acá" en unidades del relevamiento.

## Fuentes y qué aporta cada una
| Fuente | Qué trae | Ojo |
|--------|----------|-----|
| `cadastur` | padrón oficial de turismo: **habitaciones (UH) y camas**, situación y trimestre | la dirección es FISCAL: su coordenada cae lejos (medido en VG: hasta 2.135 m) |
| `receita` | CNPJ con CNAE de hospedaje | un CNPJ activo puede ser la oficina o una filial, no el hotel |
| `osm` | `tourism=hotel` y a veces `rooms` | cobertura despareja |
| `google` | pin y `business_status` | **es paga**: no va en el pipeline, se corre aparte |

## Cuándo usar
- Dentro del pipeline (ya está en `vg-pipeline-runner`) o cuando el operador aprieta 🏨.
- Después de tener las parcelas cargadas: sin geometría no puede vincular nada.

## Correr el agente

```bash
python3 -m scrapitero.rpc.hotel_fetcher <<< '{"region_id":"<REGION_ID>","survey_id":"<SURVEY_ID>","fuentes":["cadastur","receita","osm"]}'
```

## Output esperado
```json
{
  "ok": true,
  "municipio": "Várzea Grande",
  "por_fuente": {"cadastur": 41, "receita": 27, "osm": 3},
  "hoteles_en_zona": 31,
  "vinculados_parcela": 12,
  "cerrados": 14,
  "sin_habitaciones": 4,
  "total_uf_comercio": 709,
  "reglas": {"vinculados": 3, "desvinculados": 0, "especiales": 9, "pensoes": 1, "lotes_marcados": 7}
}
```

## Lo que pasa al final: `hotel-reglas`
En regiones de Brasil, cuando corre con `set_uf` y `survey_id`, llama a **`hotel-reglas`**, que
ubica por consenso de pines lo que el pin fiscal no vinculó o vinculó mal, y deja el lote como
**especial con sus habitaciones** (o PENSÃO si es una pousada con kitnets). Va adentro de este
agente a propósito: si no, cada corrida suelta del botón 🏨 volvía a cargar las habitaciones
como comercio. El resumen viaja en `reglas`. Fuera de Brasil no se aplica.

## Parámetros
| Campo | Tipo | Default | Descripción |
|-------|------|---------|-------------|
| `region_id` | string | ✅ | Región |
| `survey_id` | string | — | Survey; **hace falta** para que corran las reglas y para la asistencia |
| `fuentes` | list | `["cadastur","receita","osm","google"]` | En el pipeline va **sin `google`** (es paga) |
| `set_uf` | bool | true | Escribir uso/UF en las parcelas con hotel |
| `estimar_habitaciones` | bool | false | Proxy por área del BCI. Desactivado: miente más de lo que ayuda |
| `max_hoteles` | int | — | Tope para pruebas |

## Notas
- **Lo que se carga a mano gana:** habitaciones (`hotel_habitaciones_manual`), abierto/cerrado
  (`hotel_cerrado_manual`) y ubicación (`hotel_ubicacion_manual`) sobreviven a cada corrida,
  incluso sin CNPJ (se aparean por nombre normalizado + proximidad).
- Un hotel que cierra o se muda deja de aportar UF: el barrido limpia el sello de su parcela
  vieja. Sin eso quedaba contado en dos lotes a la vez.
- Si quedan hoteles abiertos sin habitaciones, avisa por Telegram con el link al panel de
  incidencias (`tipo=hotel_sin_habitaciones`).
