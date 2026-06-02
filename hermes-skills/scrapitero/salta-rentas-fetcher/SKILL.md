---
name: salta-rentas-fetcher
description: "Detecta parcelas baldías (terreno sin construcción) de Salta Capital consultando el valor edificado en Rentas Municipal (DGRM). valorEdificado≈0 → uso_principal=vacante. Gratis, sin login. Corrige al CPUA que clasifica por zona y no detecta lotes vacíos."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, salta, argentina, rentas, baldio, vacante, dgrm]
    category: scrapitero
---

# Salta Rentas Fetcher

Consulta la Dirección General de Rentas Municipal de Salta (DGRM) y detecta
**parcelas baldías** (sin construcción) usando el `valorEdificado` fiscal.

## Fuente

| Propiedad | Valor |
|-----------|-------|
| Portal | rentas.dgrmsalta.gov.ar |
| Endpoint | POST /api/inmobiliario/login-inmobiliario |
| Cobertura | Salta Capital |
| Autenticación | Ninguna, pero **requiere token reCAPTCHA v3** → corre vía Playwright |
| Join | por `cca_code` (= número de catastro) |

## Lógica

| valorEdificado | Acción |
|----------------|--------|
| ≈ 0 (< 1) | `uso_principal = 'vacante'` + `unidades_funcionales_estimadas = 0` (terreno baldío, sin unidad) — **autoritativo** |
| > 0 | edificado; se deja el uso a `salta-zonificacion-fetcher` (no dice res/com) |

Es la única fuente que detecta **lotes vacíos por parcela**: el CPUA asigna uso por
zona y marcaría un baldío en zona R3 como "residencial". Rentas lo corrige a vacante.

## Cuándo usar

Después de `salta-catastro-fetcher` (necesita `cca_code`). Idealmente **después** de
`salta-zonificacion-fetcher`, para corregir a vacante los lotes que CPUA marcó por zona.

## Comando

```bash
echo '{"region_id":"{region_id}","survey_id":"{survey_id}"}' |
  python3 -m scrapitero.rpc.salta_rentas_fetcher
```

| Campo | Default | Descripción |
|-------|---------|-------------|
| `overwrite` | false | reconsulta parcelas ya marcadas vacante |
| `delay_ms` | 1500 | throttle entre consultas (reCAPTCHA + cortesía) |
| `batch_size` | 0 | 0 = todas las pendientes |
| `headless` | true | |

## Output

```json
{
  "ok": true,
  "parcelas_consultadas": 34,
  "baldios_detectados": 13,
  "edificados": 21,
  "sin_match": 0,
  "errores": 0
}
```

## Notas

- **Lento por diseño:** cada consulta genera un token reCAPTCHA + espera `delay_ms`.
  Notifica por Telegram al inicio y al final (patrón de throttling del proyecto).
- **No da número de UF/PH:** el catastro de Salta modela cada unidad funcional como
  una clave independiente; el agrupamiento solo está en la cédula parcelaria paga.
  Esta skill solo aporta la señal vacante/edificado + (en el futuro) valuación fiscal.

## Pipeline de clasificación Salta (completo)

```
salta-registro-fetcher      → rural/club de campo (provincia)
salta-zonificacion-fetcher  → uso urbano por zona CPUA (Capital)
salta-rentas-fetcher        → corrige baldíos por valorEdificado (Capital)
```
