---
name: procedencia-pdf
description: "Genera la ficha de procedencia del relevamiento en PDF: de dónde sale cada dato (catastro, padrones, satélite, verificación humana), con qué cobertura y de qué fecha es cada fuente. Usar cuando piden la trazabilidad, el respaldo de las fuentes o el documento que acompaña la entrega al cliente."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, procedencia, fuentes, trazabilidad, pdf, entrega, cliente]
    category: scrapitero
---

# Ficha de procedencia (PDF)

Una hoja que acompaña a la entrega y responde "¿de dónde sacaron este dato?".

El relevamiento guarda el origen campo por campo (`fuente_parcela`, `direccion_source`,
`uf_fuente`, `uso_fuente`, `categoria_uso_fuente`), pero nada de eso llega al cliente: el CSV
Operadora lleva las columnas de su contrato y el DXF los atributos de su estándar de planos.
Esta ficha es esa trazabilidad.

## Qué trae
1. **El relevamiento en números** — lotes, unidades de vivienda y de comercio, superficie.
   Con la MISMA consulta que el tablero de la web, para que no se contradigan.
2. **De dónde sale cada dato** — tabla campo → origen → cuántos lotes → porcentaje, con los
   sellos traducidos al idioma del cliente (`bci` → «Boletín de cadastro inmobiliario del
   municipio», `manual` → «Verificado por relevador»).
3. **Las fuentes utilizadas y su fecha** — el trimestre de Cadastur, el mes del dump de la
   Receita, el release de Overture, el año de las imágenes satelitales.
4. **Lo que conviene saber** — advertencias derivadas de los datos: los pisos que no son
   conteos (shopping, hotel sin habitaciones), las alturas estimadas, la antigüedad de la
   imagen, los casos abiertos para revisión.

## Cuándo usar
- "mandame la ficha de procedencia / las fuentes del relevamiento"
- "el cliente pregunta de dónde salen estos datos"
- Al preparar una entrega, junto al CSV Operadora y al DXF.

## Correr el agente
```bash
PYTHONPATH=src python -m scrapitero.rpc.procedencia_pdf <<< '{"region_id":"<region>"}'
```
Acepta `survey_id` (si no, toma el último no archivado de la región) y `output_path`.

También es el botón **"⬇ Procedencia (PDF)"** del detalle de cada relevamiento en la web
(`GET /api/surveys/{sid}/export/procedencia-pdf`), visible también en la vista cliente.

## Importante
**No se versiona ni se guarda**: se genera en el momento y refleja el estado del relevamiento
entonces. Si el relevamiento se modifica hay que exportarla de nuevo — el propio documento lo
dice en el encabezado, para que nadie archive una ficha que dejó de coincidir con los números
que tiene al lado.
