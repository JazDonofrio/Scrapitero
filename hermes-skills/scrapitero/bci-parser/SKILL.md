---
name: bci-parser
description: "Extrae datos estructurados de los PDFs BCI descargados: uso (residencial/comercial/vacante), unidades funcionales de vivienda y comercio, área construida, dirección completa. Sin LLM — usa regex sobre el texto del PDF. Actualiza las columnas uso_principal, uf_vivienda, uf_comercio, area_m2_construida, calle, numero, barrio, cep, partida_inmobiliaria en la tabla parcelas."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, brasil, varzea-grande, bci, pdf, parser, cadastro]
    category: scrapitero
---

# BCI Parser — Extrae datos de BCIs de Várzea Grande

Lee cada PDF `pdf_downloads/reporte_{cca_code}.pdf` y actualiza la parcela en DB con:
- `uso_principal`: residencial / comercial / mixto / industrial / vacante
- `uf_vivienda`: cantidad de unidades residenciales
- `uf_comercio`: cantidad de unidades comerciales
- `uf_fuente = 'bci'`: marca la UF como **exacta** (extraída del PDF oficial). La web la
  muestra sin el badge `est.`/`≈` que llevan las UF estimadas por UnidadesEstimator
- `area_m2_construida`: área construída total
- `calle`, `numero`, `barrio`, `codigo_postal`: dirección completa
- `partida_inmobiliaria`: número de matrícula del Registro de Imóveis
- `direccion_source = 'bci_pdf'` con confidence 0.95

## Cuándo usar
- Después de VGBCIFetcher (necesita los PDFs descargados)
- Para llenar uso, UF y dirección a partir del catastro oficial
- Cuando `uso_principal IS NULL` o `uf_vivienda IS NULL` en parcelas

## Correr el agente

```bash
echo '{"region_id":"<REGION_ID>"}' |

  python3 -m scrapitero.rpc.bci_parser
```

Con batch (procesar de a 100):
```bash
echo '{"region_id":"<REGION_ID>","batch_size":100}' | ...
```

## Output esperado
```json
{
  "ok": true,
  "procesadas": 284,
  "actualizadas": 271,
  "sin_pdf": 13,
  "errores": 0
}
```

## Parámetros
| Campo | Default | Descripción |
|-------|---------|-------------|
| `region_id` | ✅ | ID de la región |
| `pdf_dir` | `/opt/scrapitero/pdf_downloads` (o `$SCRAPITERO_PDF_DIR`) | Carpeta **base** de PDFs — **la MISMA que usó VGBCIFetcher**. Lee de `pdf_dir/<ciudad>/` (subcarpeta por ciudad, resuelta sola desde `region_id`) |
| `batch_size` | 0 (todas) | Limitar cantidad |

## Notas
- ⚠️ **`pdf_dir` debe coincidir con el de VGBCIFetcher.** Si el parser reporta "PDFs
  faltantes" pero el fetcher dijo que ya existían, es casi siempre un desajuste de
  `pdf_dir` (el fetcher descargó en otra carpeta — p.ej. el CWD del container Hermes). El
  parser ahora lo detecta y avisa en qué carpeta SÍ están los PDFs faltantes. Solución:
  pasar el MISMO `pdf_dir` absoluto a ambos (o setear `SCRAPITERO_PDF_DIR`). No hace falta
  re-descargar.
- 📁 **Carpeta por ciudad:** lee de `pdf_dir/<ciudad>/reporte_{codigo}.pdf`, la misma
  subcarpeta donde VGBCIFetcher guarda (la ciudad se resuelve sola desde `region_id`). Pasá
  el mismo `pdf_dir` **base** que al fetcher; ambos le anexan la ciudad automáticamente.
- No usa LLM — extrae datos con regex sobre el texto del PDF
- Compatible con PDFs `Predial` (con construcción) y `Territorial` (terreno vacante)
- Detecta TIPOLOGIA: CASA, APARTAMENTO, SALA, LOJA, etc.
- La dirección queda con `direccion_source='bci_pdf'` (mayor confianza que Nominatim/IBGE)
