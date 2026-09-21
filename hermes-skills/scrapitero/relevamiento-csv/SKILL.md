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

## ⚠️ No confundir con el CSV por lote de la web
Esta skill arma el **reporte resumen** (totales, cobertura, top calles). El **CSV por lote** —una
fila por unidad, con la dirección, la UF y el detalle de cada parcela— sale de la web
(`/api/surveys/{survey_id}/export/csv`) y es el que usa el operador. Ese archivo lleva además
tres columnas que conviene mirar antes de entregar:

| Columna | Qué trae |
|---------|----------|
| `UF Especial` | las unidades de un lote especial: habitaciones del hotel, locales del shopping, 1 para una escuela o una iglesia |
| `Revisión` | dato con respaldo que la referencia del cliente contradice, y las dudas que marca `hotel-reglas` (prefijo `[hoteles]`) |
| `Incidencias pendientes` | los tipos del panel que siguen **sin resolver** en ese lote — ninguna exportación los filtra, así que el lote sale igual |

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
