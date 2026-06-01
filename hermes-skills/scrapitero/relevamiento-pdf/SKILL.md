---
name: relevamiento-pdf
description: "Genera y envía por Telegram el reporte de relevamiento catastral en formato PDF. Usar cuando el usuario pide descargar, exportar o recibir el reporte en PDF."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, reporte, pdf, catastro, descarga, argentina]
    category: scrapitero
---

# Relevamiento PDF

Genera un PDF con el reporte completo del relevamiento y lo envía como archivo por Telegram.

## Cuándo usar
- "mandame el PDF del relevamiento"
- "quiero descargar el reporte"
- "exportá el relevamiento en PDF"

## Pasos

### Paso 1 — Generar el PDF
```bash
echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>"}' |

  python3 -m scrapitero.rpc.relevamiento_pdf
```

Sin survey_id (usa el más reciente):
```bash
echo '{"region_id":"ituzaingo-ba-ar"}' |

  python3 -m scrapitero.rpc.relevamiento_pdf
```

Output:
```json
{
  "ok": true,
  "pdf_path": "/tmp/relevamiento_ituzaingo_ba_ar_20260529_041903.pdf",
  "total_parcelas": 26,
  "error": null
}
```

### Paso 2 — Enviar el archivo por Telegram
Cuando `ok: true`, usar la herramienta de mensajería para enviar el archivo `pdf_path` al usuario.
Mensaje de acompañamiento: "Reporte de relevamiento — {total_parcelas} parcelas relevadas."
