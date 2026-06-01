---
name: onr-sigef-fetcher
description: "Descarga predios rurales georreferenciados de SIGEF/ONR para cualquier área de Brasil. Usar cuando la zona es rural o no está en la lista de ciudades con lotes urbanos de ONR. Requiere bbox en WGS84."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, brasil, onr, sigef, rural, parcelas, matricula, georreferenciado]
    category: scrapitero
---

# ONR SIGEF Fetcher — Predios Rurales Brasil

Descarga polígonos de predios rurales certificados/registrados en SIGEF (INCRA) vía ONR.
Cubre **todo Brasil**. El token de ArcGIS se obtiene automáticamente de mapa.onr.org.br.

## Cuándo usar

**Zona rural** o ciudad **sin cobertura de lotes urbanos en ONR**.

Ciudades CON lotes urbanos → usar `onr-lotes-fetcher` en su lugar:
São Paulo, Rio de Janeiro, Fortaleza, Recife, Belo Horizonte, Curitiba, Manaus,
João Pessoa, Natal, Florianópolis, Campo Grande, Niterói, Santa Maria (RS),
Rio Branco, São Bernardo do Campo, Maringá.

**Várzea Grande, MT** → NO está en la lista → usar este fetcher (SIGEF).

## Datos disponibles por predio

| Campo DB | Fuente SIGEF | Descripción |
|---|---|---|
| `nomenclatura_catastral` | `cnm` | Código Nacional de Matrícula |
| `partida_inmobiliaria` | `matricula` | Número de matrícula no RI |
| `complemento` | `nome_area` | Nome do predio (Fazenda XYZ) |
| `municipio` | `municipio_` | Código IBGE do municipio |
| `area_m2_terreno` | geometria | Área calculada do polígono |
| `geometry` | polígono | Limite georreferenciado WGS84 |
| `fuente_parcela` | — | `sigef_onr` |

Status possíveis: `CERTIFICADA`, `REGISTRADA`

## Comando

```bash
echo '{
  "region_id": "vg-mt-br",
  "survey_id": "<SURVEY_ID>",
  "bbox_south": -15.72,
  "bbox_west": -56.25,
  "bbox_north": -15.56,
  "bbox_east": -56.00
}' |

  python3 -m scrapitero.rpc.onr_sigef_fetcher
```

## Output esperado

```json
{
  "ok": true,
  "parcelas_inseridas": 47,
  "parcelas_atualizadas": 0,
  "total_features": 47,
  "error": null
}
```

## Si falla con error de token (498)
El agente renueva el token automáticamente. Si sigue fallando, esperar 1 hora
y reintentar — el token se renueva desde mapa.onr.org.br sin intervención humana.

## Notas
- Solo cubre **predios rurales** (SIGEF/INCRA). No incluye imóveis urbanos.
- Para Várzea Grande: bbox sugerida `south=-15.72 west=-56.25 north=-15.56 east=-56.00`
- Máx 2000 features por query; el agente pagina automáticamente si hay más.
- Delay entre queries: 500ms por defecto (configurable con `delay_ms`).
