---
name: hotel-reglas
description: "Ubica cada hotel en su lote por consenso de pines (censo 2022, Overture, OSM, Google) y lo clasifica según la tabla del cliente brasilero: hotel/motel/flat = especial con sus habitaciones, pousada con kitnets = PENSÃO residencial. Deshace el vínculo que el pin fiscal dejó en el lote equivocado y marca en el CSV de relevamiento los casos que no resuelve. Sólo Brasil. Lo corre HotelFetcher al final; se invoca suelto para medir en seco."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, brasil, hoteles, cadastur, censo, reglas, especial]
    category: scrapitero
---

# Hotel Reglas — en qué lote está el hotel y qué es

`HotelFetcher` ubica cada hotel con la **dirección fiscal geocodificada** del Cadastur o de
Receita, y ese punto cae en la calle o a cientos de metros. Este agente lo corrige con lo que
ven las fuentes de mapa, y aplica el criterio de tipos del cliente.

## Las reglas

**H1 — ubicar.** Junta los pines del MISMO hotel en censo 2022, Overture, OSM y las filas
Google de `hoteles`. Cada pin vota el lote que lo contiene o el más cercano a ≤15 m. Asigna
cuando votan **≥2 tipos de fuente** y **todos coinciden**. El nombre se compara sin lo que va
entre paréntesis ni las palabras de razón social (HOTELARIA, EMPREENDIMENTOS…), que son las que
cruzaban hoteles distintos.

**H1b — contrastar el vínculo que ya existe.** Si ninguna fuente ve el hotel a ≤15 m del lote
que le puso el pipeline, lo desvincula; si otro lote tiene más fuentes a favor, **marca y no
toca**. Un vínculo puesto a mano (`hotel_ubicacion_manual`) no se discute.

**H2 — clasificar.** Hotel, motel o flat → **especial** (`uso_principal='equipamiento'`) **con
sus habitaciones como unidades**; pousada con kitnets, dormitório o pensão → **PENSÃO**, que es
residencial. Marca en vez de cambiar si en el lote hay otro negocio visto por 2 fuentes (el
restaurante o el bar del propio hotel no cuentan) o si hay viviendas del censo o de la ficha.

Sólo cuenta un hotel que existe físicamente: ≥2 fuentes de mapa, o 1 + Cadastur vigente. Un
CNPJ de Receita sin ningún pin es una oficina o una filial administrativa, no un hotel.

## Cuándo usar
- **No hace falta llamarlo en un relevamiento normal:** `hotel-fetcher` lo corre solo al final
  (por eso el botón 🏨 y el pipeline dejan el mismo resultado).
- **Sí conviene llamarlo suelto con `dry_run`** para ver qué haría en una zona sin tocar nada,
  o después de cargar hoteles a mano.
- Fuera de Brasil devuelve `omitido` y no hace nada: la tabla R/C/E es el pliego del cliente
  brasilero.

## Correr el agente

```bash
python3 -m scrapitero.rpc.hotel_reglas <<< '{"region_id":"<REGION_ID>","survey_id":"<SURVEY_ID>"}'
```

En seco (no escribe):
```bash
python3 -m scrapitero.rpc.hotel_reglas <<< '{"region_id":"<REGION_ID>","survey_id":"<SURVEY_ID>","dry_run":true}'
```

## Output esperado (corrida real en VAZ049)
```json
{
  "ok": true,
  "vinculados": ["CERRADOS PARK HOTEL → AVENIDA - COUTO MAGALHAES 755"],
  "desvinculados": [],
  "especiales": ["AVENIDA - FILINTO MULLER 62: HOTEL (AMAZON AEROPORTO HOTEL)"],
  "pensoes": ["RUA - CEL GONCALO DE FIGUEIREDO 167: 2v (Pousada Dona Nhazinha)"],
  "marcas": ["AVENIDA - GOV. JOAO PONCE DE ARRUDA 936 — HOTEL TAINA: las fuentes lo ubican en lotes distintos"],
  "respetados": ["AVENIDA - COUTO MAGALHAES 0 (HOTEL PORTAL DA AMAZÔNIA)"],
  "ignorados": 29,
  "fuentes": {"censo": 57, "overture": 25, "osm": 11, "google": 1}
}
```

## Qué escribe
| Dónde | Qué |
|-------|-----|
| `hoteles.parcela_id` | el lote que decidió H1, o NULL si H1b deshizo el vínculo |
| `parcelas` | sello `uf_fuente='regla_hoteles'`: especial con sus habitaciones, o pensão residencial |
| `parcela_revision_nota` | las marcas, con el prefijo `[hoteles]` — salen en la columna **Revisión** del CSV de relevamiento |

**Nunca pisa** una UF, un uso o un tipo corregidos a mano: esos lotes salen en `respetados`.
Es idempotente: cada corrida deshace lo que dejó la anterior y lo vuelve a calcular.

## Parámetros
| Campo | Tipo | Default | Descripción |
|-------|------|---------|-------------|
| `region_id` | string | ✅ | Región |
| `survey_id` | string | ✅ | Survey activo |
| `dry_run` | bool | false | Decide y devuelve, sin escribir |
| `dist_lote_m` | float | 15.0 | Distancia máxima pin → lote para que el pin vote |
| `hueco_m` | float | 25.0 | Más lejos que esto de todo lote: el hotel está fuera de la zona |
| `cadastur_vigente_desde` | int | 2023 | Trimestre desde el cual el Cadastur cuenta como vigente |
| `censo`/`overture`/`osm` | — | — | No son parámetros: las fuentes se leen solas (`establecimientos_observados`, el zip del CNEFE y Overpass) |

## Notas
- Medido a ciegas en VAZ049 (sin las correcciones a mano): 7 casos resueltos igual que la
  decisión humana, 0 roturas, 8 marcados. Sin las reglas, el pipeline dejaba las habitaciones
  como comercio, un hotel de 8 pisos como baldío y otro en el lote del negocio de al lado.
- Si Overpass está caído el agente sigue: pierde los votos de OSM y marca más casos.
- ⚠️ El criterio de tipos (qué es R, C o E) vive en `docs/TIPOS_PROPIEDAD.md`, que transcribe la
  tabla del pliego del cliente. Si el cliente la cambia, se cambia ahí, no acá.
