---
name: establecimientos-observados
description: "Carga, gratis y sólo para Brasil, qué se ve HOY en cada lote: los establecimientos del censo 2022 (CNEFE) y de Overture Places con su nombre, y los domicilios del censo, que son el único control independiente de cuántas viviendas hay. Los ata al lote por número de puerta, por caer adentro o por cercanía. Escribe establecimientos_observados y censo_domicilios; no toca ninguna cantidad del relevamiento."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, brasil, censo, cnefe, overture, nombres, control-calidad]
    category: scrapitero
---

# Establecimientos Observados — qué hay hoy en cada lote

Dos insumos, las dos fuentes gratis:

1. **Los negocios con nombre** (censo 2022 + Overture). Sirven para el `DSC_NOME_DO_IMOVEL` del
   entregable: el complemento de la ficha del catastro trae el negocio **de cuando se registró
   el lote**. Medido en VAZ049: 10 de 36 nombres comparables ya no estaban (Ponce de Arruda 962
   decía «ESTACIONAMENTO AEROPARKING» y ahí está la locadora Loca10).
2. **Los domicilios del censo** (`COD_ESPECIE=1`), que no tienen nombre pero son **el único
   control independiente de cuántas viviendas hay en el lote**: el censista pasó por la puerta
   y anotó una por una, con el complemento que vio («QUITINETE CASA 7», «FUNDOS»). Van a
   `censo_domicilios` y los usa `incidencias-reporter` para contrastar la ficha del catastro.

## Cómo ata cada punto al lote
1. **Por número de puerta** (sólo censo): el lote de esa calle con ese número, a ≤60 m. El punto
   del censo es la coordenada de la dirección y suele caer en el lote de al lado.
2. **Adentro** del polígono.
3. **Por cercanía**: el lote construido más cercano a ≤10 m (el punto cae en la vereda).

A Overture **no** se le aplica la regla del número: declara mal la altura (58 % contra la
revisión del cliente).

## Cuándo usar
- Ya está en el pipeline de Brasil (`vg-pipeline-runner`), después del BCI.
- Suelto: cuando se quiera refrescar nombres o cargar los domicilios en una zona vieja, antes de
  correr `incidencias-reporter`.
- Fuera de Brasil, el censo se saltea (necesita `country_code='BRA'` y `municipio_codigo` de 7
  dígitos); Overture sí corre.

## Correr el agente

```bash
python3 -m scrapitero.rpc.establecimientos_observados <<< '{"region_id":"<REGION_ID>","survey_id":"<SURVEY_ID>"}'
```

## Output esperado
```json
{
  "ok": true,
  "por_fuente": {
    "censo_2022": {"cargados": 1415, "descartados_genericos": 532, "numero": 24, "adentro": 95, "cercania": 112, "sin_parcela": 1184},
    "overture": {"cargados": 1222, "adentro": 194, "cercania": 101, "sin_parcela": 927},
    "censo_domicilios": {"cargados": 8576, "en_parcela": 520}
  }
}
```
`sin_parcela` alto es normal: el bbox del censo cubre más que el polígono del relevamiento.

## Parámetros
| Campo | Tipo | Default | Descripción |
|-------|------|---------|-------------|
| `region_id` | string | ✅ | Región |
| `survey_id` | string | ✅ | Survey activo |
| `fuentes` | list | `["censo","overture"]` | Con `["censo"]` sólo el CNEFE |
| `numero_max_m` | float | 60.0 | Radio para atar por número de puerta |
| `cercania_max_m` | float | 10.0 | Radio para atar por cercanía |
| `umbral_rubro` | int | 10 | Palabra que aparece en ≥N negocios del município = rubro, no nombre |

## Notas
- **No toca `parcelas`.** Los nombres alimentan el entregable y los domicilios alimentan una
  incidencia; ninguna cantidad se mueve acá.
- El zip del CNEFE se cachea por município en `SCRAPITERO_CACHE` (Várzea Grande: 2,7 MB).
- El censo más nuevo es el de **2022**: el IBGE sólo publica 2010 y 2022, y lo que aparece con
  fecha 2024 son republicaciones del mismo relevamiento. El próximo censo es 2030.
- Descarta lo que el censista anota cuando no hay nombre («VAGO», «SEM NOME») y las palabras de
  rubro sueltas, con el vocabulario del propio município.
