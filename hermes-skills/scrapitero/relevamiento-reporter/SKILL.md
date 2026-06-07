---
name: relevamiento-reporter
description: "Genera un reporte completo de un relevamiento catastral: dirección, unidades funcionales, nomenclatura catastral y área por parcela. Usar cuando el usuario pide un reporte, resumen o resultado del relevamiento."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, reporte, catastro, nomenclatura, argentina, brasil]
    category: scrapitero
---

# Relevamiento Reporter

Genera un reporte completo del último survey de una región, con:
- Resumen general (total parcelas, UF, área)
- Por parcela: dirección completa, UF, nomenclatura catastral, partida inmobiliaria

## Cuándo usar
Cuando el usuario dice cosas como:
- "mostrame el reporte del relevamiento"
- "qué se relevó en Ituzaingó?"
- "dame el resumen de la manzana"
- "cuántas unidades funcionales hay?"

## Comando

Sin survey_id (usa el más reciente de la región):
```bash
python3 -m scrapitero.rpc.relevamiento_reporter <<< '{"region_id":"ituzaingo-ba-ar"}'
```

Con survey_id específico:
```bash
python3 -m scrapitero.rpc.relevamiento_reporter <<< '{"region_id":"ituzaingo-ba-ar","survey_id":"<SURVEY_ID>"}'
```

## Output esperado
```json
{
  "survey_id": "uuid",
  "region_id": "ituzaingo-ba-ar",
  "region_nombre": "Ituzaingó, Buenos Aires, Argentina",
  "total_parcelas": 26,
  "parcelas_con_direccion": 26,
  "parcelas_con_uf": 26,
  "parcelas_con_nomenclatura": 26,
  "total_uf": 54,
  "area_total_m2": 10209.49,
  "parcelas": [
    {
      "direccion": "Avenida Rivadavia 21800",
      "unidades_funcionales": 1,
      "nomenclatura_catastral": "Partido: 136 (Ituzaingó) Circunscripción: 2 Sección: C Manzana: 184 Parcela: 1A",
      "partida_inmobiliaria": "658",
      "area_m2": 172.5
    }
  ]
}
```

## Cómo presentar el resultado al usuario

Mostrar primero el resumen general y luego la tabla de parcelas. Formato sugerido:

**Relevamiento — Ituzaingó, Manzana 184**
- Total parcelas: 26
- Total UF: 54
- Área total: 10.209 m²

| Dirección | UF | Nomenclatura | Partida |
|---|---|---|---|
| Avenida Rivadavia 21800 | 1 | Circ 2 / Secc C / Mzna 184 / Parc 1A | 658 |
| ... | | | |
