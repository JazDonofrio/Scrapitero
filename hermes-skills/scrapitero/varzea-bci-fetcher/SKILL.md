---
name: varzea-bci-fetcher
description: "Descarga BCIs (Boletim de Cadastro Imobiliário) de Várzea Grande desde vg.abaco.com.br usando Playwright. Lee inscripciones (cca_code) de la DB, filtra por zone_geojson, reutiliza PDFs ya descargados en pdf_downloads/, descarga solo los faltantes. PDF nombrado reporte_{codigo}.pdf. Notifica vía Telegram al inicio y al final."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, brasil, varzea-grande, bci, iptu, catastro, pdf, playwright]
    category: scrapitero
---

# VG BCI Fetcher — Boletins de Cadastro Imobiliário

Descarga BCIs en PDF desde el portal de la Prefeitura de Várzea Grande.
Prerequisito: `smartgis-fetcher` debe haberse ejecutado (necesita `cca_code` en parcelas).

## Cuándo usar
- Después de `smartgis-fetcher` cuando `parcelas_insertadas > 0`
- Para completar el relevamiento con datos del catastro oficial de VG
- Los PDFs se usan luego para extraer dirección completa, propietario, UF, etc.

## Correr el agente

```bash
echo '{"region_id":"<REGION_ID>","survey_id":"<SURVEY_ID>"}' |

  python3 -m scrapitero.rpc.varzea_bci_fetcher
```

Con parámetros explícitos:
```bash
echo '{
  "region_id":"<REGION_ID>",
  "survey_id":"<SURVEY_ID>",
  "pdf_dir":"pdf_downloads",
  "batch_size":50,
  "min_delay_secs":2.0,
  "max_delay_secs":8.0,
  "pausa_cada_n":20,
  "pausa_minutos":3
}' | env $(cat /opt/scrapitero/.env | xargs)
   
     python3 -m scrapitero.rpc.varzea_bci_fetcher
```

## Output esperado
```json
{
  "ok": true,
  "pdfs_descargados": 47,
  "pdfs_ya_existentes": 210,
  "pdfs_fallidos": 3,
  "parcelas_procesadas": 260,
  "error": null
}
```

## Parámetros
| Campo | Tipo | Default | Descripción |
|-------|------|---------|-------------|
| `region_id` | string | ✅ | ID de la región |
| `survey_id` | string | ❌ | No se usa para filtrar (usa region_id) |
| `pdf_dir` | string | `pdf_downloads` | Carpeta de PDFs |
| `batch_size` | int | 0 | 0=todos; >0=limita cantidad |
| `min_delay_secs` | float | 1.5 | Delay mínimo entre descargas |
| `max_delay_secs` | float | 6.0 | Delay máximo entre descargas |
| `pausa_cada_n` | int | 20 | Pausa larga cada N descargas |
| `pausa_minutos` | int | 2 | Minutos de pausa larga |

## Notas
- Reutiliza PDFs existentes en `pdf_dir/reporte_{codigo}.pdf` (compatibles con scraper legacy)
- El scraper legacy (`scrape_catastro_varzea.py`) puede correr en paralelo sin conflictos
- Tiempo promedio: ~23s/PDF (delay + carga GeneXus)
- PDF válido = archivo >1KB
