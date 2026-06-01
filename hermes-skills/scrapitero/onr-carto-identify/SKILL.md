---
name: onr-carto-identify
description: "Identifica el cartório (Registro de Imóveis) responsable de un punto geográfico usando la API ArcGIS de mapa.onr.org.br. Devuelve CNS (Código Nacional de Serventia), nombre del cartório y comarca. Fuente alternativa cuando SmartGIS no cubre el área."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, brasil, onr, cartorio, cns, registro-imoveis]
    category: scrapitero
---

# ONR Cartório Identify

Dado un punto (lat, lng), determina qué cartório de Registro de Imóveis es
responsable de esa ubicación según el mapa de competências registrais del ONR.

## Cuándo usar
- Necesitás saber a qué cartório pertenece una parcela
- SmartGIS no cubre el área (ciudades fuera de VG)
- Querés el CNS para consultar matrículas en el Registro de Imóveis

Para Várzea Grande siempre devuelve: CNS=063446, "1º Registro de Imóveis de Várzea Grande"

## Correr el agente

```bash
echo '{"lat":-15.65,"lng":-56.10}' |

  python3 -m scrapitero.rpc.onr_carto_identify
```

## Output esperado
```json
{
  "ok": true,
  "cns": "063446",
  "cartorio": "1º Registro de Imóveis de Várzea Grande",
  "comarca": "Várzea Grande",
  "uf": "MT",
  "abrangencia": "Várzea Grande",
  "error": null
}
```

## Parámetros
| Campo | Tipo | Descripción |
|-------|------|-------------|
| `lat` | float | Latitud (negativo para sur) |
| `lng` | float | Longitud (negativo para oeste) |
