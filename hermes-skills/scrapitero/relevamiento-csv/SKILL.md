---
name: relevamiento-csv
description: "Genera y envía por Telegram el reporte de relevamiento catastral en formato CSV (compatible Google Sheets / Google Drive). Usar cuando el usuario pide planilla, Excel, CSV, Google Sheets o Google Drive."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, reporte, csv, planilla, google-sheets, google-drive, catastro, argentina]
    category: scrapitero
---

# Relevamiento CSV

Genera una planilla CSV con el reporte completo del relevamiento y la envía como archivo por Telegram.
El archivo se abre directamente en Google Sheets al subirlo a Google Drive.

## Cuándo usar
- "mandame la planilla del relevamiento"
- "quiero bajar el CSV"
- "exportá a Google Sheets"
- "mandame el Excel"
- "quiero subir los datos a Google Drive"

## Pasos

### Paso 1 — Generar el CSV
```bash
python3 -m scrapitero.rpc.relevamiento_csv <<< '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>"}'
```

Sin survey_id (usa el más reciente):
```bash
python3 -m scrapitero.rpc.relevamiento_csv <<< '{"region_id":"ituzaingo-ba-ar"}'
```

Output:
```json
{
  "ok": true,
  "csv_path": "/tmp/relevamiento_ituzaingo_ba_ar_20260529_041903.csv",
  "total_parcelas": 26,
  "error": null
}
```

### Paso 2 — Enviar el archivo por Telegram
Cuando `ok: true`, enviar el archivo `csv_path` al usuario por Telegram.
Mensaje de acompañamiento: "Planilla del relevamiento — {total_parcelas} parcelas. Podés abrirla directo en Google Sheets."
