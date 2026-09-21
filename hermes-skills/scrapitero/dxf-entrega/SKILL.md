---
name: dxf-entrega
description: "Genera el plano DXF con el estándar de la operadora brasilera (BL): un bloque SDU o MDU por inmueble sobre el mapa base municipal de manzanas, con el tipo del cliente (R residencial, C comercial, E especial, VAZ baldío) en la etiqueta y en la capa. Modo calco: si el cliente devolvió su propio DWG, se copian sus manzanas, sus lotes y el punto de cada ficha. Sale en UTM WGS84, 1 unidad = 1 metro."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, brasil, dxf, plano, entrega, autocad, bl]
    category: scrapitero
---

# DXF Entrega — el plano en el formato del cliente

## Qué dibuja
- **Un bloque por inmueble**, no por lote: la casa con el local adelante son dos fichas, una `R`
  y otra `C`, como en el plano del propio cliente.
- **`SDU`** para la unidad unifamiliar y **`MDU`** para el edificio multiunidad (corte por UF
  total, default 6). El MDU lleva `HP_TOTAL` y `QTD_ANDARES`.
- **Una sola etiqueta visible**: número + tipo + cantidad. `350R` · `25R-2` · `S/NC` · `VAZ`.
- **El tipo va también en la capa**: `SDU_RES`, `SDU_COM`, `SDU_ESP`, `SDU_VAZ` y sus `MDU_*`.
- **Mapa base del municipio** (`QUADRA`, `MEIOFIO`) desde `mapa_base_cliente`, y los nombres de
  calle apoyados sobre el eje real de OSM.

## Los tipos, y el especial
`R` residencial, `C` comercial, `E` especial, y `VAZ` para el baldío, que tiene capa propia.

**Un lote especial con unidades sale `E` con su cantidad.** Un hotel conserva sus habitaciones
como HP y un shopping sus locales (`uso_principal='equipamiento'`): la base del operador los
cuenta así — 123 HP en el Amazon Aeroporto, 379 en el shopping de Várzea Grande. Un especial sin
unidades (escuela, iglesia) sale sin cantidad ni pisos; el baldío sale `VAZ`. Un lote que alguien
etiquetó a mano como especial **no** sale `VAZ` aunque el catastro no declare construcción: la
ficha puede ser anterior al edificio (el Slaviero Slim, 8 pisos, figuraba baldío).

## Modo calco
Si el cliente devolvió su plano (capas `QUADRA` y `DIV` cargadas en `mapa_base_cliente`), se
copian sus manzanas, sus lotes **y el punto donde él puso cada ficha**; el código no dibuja una
polilínea propia. En Várzea: 232 fichas ancladas a su punto, y las que su capa no cubría
(cubría 562 de 567 parcelas) se dibujan con nuestra geometría.

## Correr el agente

```bash
python3 -m scrapitero.rpc.dxf_entrega <<< '{"survey_id":"<SURVEY_ID>","celula_id":"VAZ049","output_path":"/opt/scrapitero/exports/VAZ049.dxf"}'
```

## Parámetros
| Campo | Tipo | Default | Descripción |
|-------|------|---------|-------------|
| `survey_id` | string | ✅ | Survey a dibujar |
| `celula_id` | string | `""` | Célula del cliente; va en el atributo `ID` de cada bloque |
| `output_path` | string | — | Dónde escribir el DXF |
| `codlog_csv` | string | — | CSV `calle,codlog` del cliente, para el atributo CODLOG |
| `fuente_mapa_base` | string | `MUB_MT_VARZEA_GRANDE` | Capa del mapa base municipal |
| `mdu_min_uf` | int | 6 | UF total a partir de la cual el inmueble es MDU |
| `dxfversion` | string | R2000 | El estándar del cliente. Si AutoCAD web rebota, probar R2013 |
| `epsg` | int | — | None autodetecta el huso UTM (VG: 32721) |

## Notas
- ⚠️ **Un `inf` en una coordenada tumba el archivo entero**: AutoCAD lo descarta con «Invalid or
  incomplete DXF input» y ni el auditor de ezdxf ni el bbox lo ven. Pasó con la capa `LIMITE`.
- **El plano de una zona puede estar fijado a mano.** Cuando existe un DXF fijado, la web sirve
  ese archivo y no el generado: para pasarle los cambios de la base hay que sincronizarlo, no
  regenerarlo, o se pierden las fichas movidas por el cliente.
- Sin los ejes de calle de OSM (`ejes_calle`, clave `OSM_<region_id>`) los nombres se ubican por
  deducción y quedan en la calle equivocada, sin avisar.
