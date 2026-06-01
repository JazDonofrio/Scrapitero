---
name: onr-lotes-fetcher
description: "Descarga lotes urbanos del ONR para ciudades brasileñas con cobertura. Auto-detecta la ciudad por coordenada. Usar cuando la zona es urbana en una ciudad soportada."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, brasil, onr, lotes, urbano, parcelas, sao-paulo, rio-de-janeiro, curitiba]
    category: scrapitero
---

# ONR Lotes Fetcher — Lotes Urbanos Brasil

Descarga lotes catastrales urbanos del ONR (parcelamento do solo) para las ciudades
con cobertura disponible. Auto-detecta la ciudad a partir de una coordenada.

## Cuándo usar

Zona **urbana** en alguna de estas ciudades:

| Slug | Ciudad | UF |
|---|---|---|
| `sp_capital` | São Paulo (Capital) | SP |
| `rj_capital` | Rio de Janeiro (Capital) | RJ |
| `fortaleza` | Fortaleza | CE |
| `recife` | Recife | PE |
| `belo_horizonte` | Belo Horizonte | MG |
| `curitiba` | Curitiba | PR |
| `manaus` | Manaus | AM |
| `joao_pessoa` | João Pessoa | PB |
| `natal` | Natal | RN |
| `florianopolis` | Florianópolis | SC |
| `campo_grande` | Campo Grande | MS |
| `niteroi` | Niterói | RJ |
| `santa_maria` | Santa Maria | RS |
| `rio_branco` | Rio Branco | AC |
| `sao_bernardo` | São Bernardo do Campo | SP |
| `maringa` | Maringá | PR |

Si la ciudad **no está en la lista** → usar `onr-sigef-fetcher` (predios rurales).
**Várzea Grande, MT** → NO está → usar `onr-sigef-fetcher`.

## Modo A — Auto-detección por coordenada

```bash
echo '{
  "region_id": "sp-capital-br",
  "survey_id": "<SURVEY_ID>",
  "lat": -23.55,
  "lng": -46.63
}' |

  python3 -m scrapitero.rpc.onr_lotes_fetcher
```

## Modo B — Ciudad explícita

```bash
echo '{
  "region_id": "sp-capital-br",
  "survey_id": "<SURVEY_ID>",
  "cidade_slug": "sp_capital"
}' |

  python3 -m scrapitero.rpc.onr_lotes_fetcher
```

## Modo C — Con bbox explícita (zona específica dentro de la ciudad)

```bash
echo '{
  "region_id": "sp-capital-br",
  "survey_id": "<SURVEY_ID>",
  "cidade_slug": "sp_capital",
  "bbox_south": -23.57,
  "bbox_west": -46.65,
  "bbox_north": -23.53,
  "bbox_east": -46.61
}' |

  python3 -m scrapitero.rpc.onr_lotes_fetcher
```

## Output esperado

```json
{
  "ok": true,
  "cidade": "São Paulo (Capital)",
  "parcelas_inseridas": 312,
  "parcelas_atualizadas": 0,
  "total_features": 312,
  "error": null
}
```

## Datos disponibles por lote (SP Capital)

| Campo DB | Fuente ONR | Descripción |
|---|---|---|
| `cca_code` | `st_qd_lo` | Código setor.quadra.lote |
| `area_m2_terreno` | `area_m2` | Área do lote |
| `municipio` | detectado | Nome da cidade |
| `estado_provincia` | detectado | UF |
| `geometry` | polígono | Limite do lote WGS84 |
| `fuente_parcela` | — | `onr_lotes_sp` / `onr_lotes_rj` etc. |

## Notas
- El token ArcGIS se obtiene automáticamente de mapa.onr.org.br y se cachea 7h.
- Delay entre queries: 500ms (configurable con `delay_ms`).
- Máx 500 lotes por query; pagina automáticamente si hay más.
