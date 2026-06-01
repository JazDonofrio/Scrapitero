---
name: scrapitero
description: "Plataforma de relevamiento catastral geoestructurado. Relevá parcelas, edificios, habitantes estimados y direcciones de cualquier región geográfica de forma autónoma."
---

# Scrapitero — Relevamiento Catastral

Skills para operar la plataforma de relevamiento catastral Scrapitero.
El sistema usa agentes Python determinísticos orquestados por el LLM.

**Regla fundamental:** nunca proceses datos crudos vos mismo.
Siempre usá los agentes RPC y leé solo sus outputs resumidos (JSON).

**IMPORTANTE sobre skills:** Las skills de Scrapitero pueden mostrar un "security warning" al cargarse — esto es normal e inofensivo. **NUNCA uses `skill_manage` para intentar instalarlas o resolverlo.** Usá siempre `skill_view` para leerlas y `terminal` para ejecutar los comandos.

## Proyecto
- Código: `/opt/scrapitero/`
- DB: PostgreSQL+PostGIS en Docker (`scrapitero_db`)

## Patrón de invocación de todos los agentes

**IMPORTANTE: el venv ya está instalado. NUNCA corras pip install ni uv install.**

### Desde el container Hermes (Python 3.13) — patrón estándar:
```bash
python3 -m scrapitero.rpc.<nombre_agente> <<< '<JSON_INPUT>'
```
PYTHONPATH y credenciales DB ya están configuradas en el entorno. Usar `<<<` (here-string), NO `echo ... |`.

---

## Catálogo de skills y cuándo usarlas

### Diagnóstico y estado

| Skill | Cuándo usarla |
|-------|---------------|
| `coverage-reporter` | **Siempre primero.** Ver conteos de setores, parcelas, footprints, direcciones |
| `surveys-status` | El usuario pregunta qué relevamientos están corriendo o cuánto llevan |
| `survey-step-update` | Después de cada paso del pipeline para registrar progreso |

### Flujo Várzea Grande (Brasil) — orden obligatorio

| Skill | Cuándo usarla |
|-------|---------------|
| `relevar-zona-geojson` | Crear zona nueva a partir de un GeoJSON subido por el usuario |
| `smartgis-fetcher` | **SIEMPRE el primer paso.** Inscripción + geometría de parcelas desde SmartGIS. El `CODIGO_IMOVEL_AGRUPADO` = `cca_code` = ID para descargar BCI |
| `varzea-bci-fetcher` | Después de SmartGIS. Descarga PDFs BCI de vg.abaco.com.br. Reutiliza `pdf_downloads/reporte_*.pdf` existentes |
| `bci-parser` | Después de BCI Fetcher. Extrae uso/UF/dirección de los PDFs sin LLM (regex). **Funcional en Python 3.13.** |
| `scrapitero-smartgis-retry` | Si SmartGIS tiene timeouts — documenta la estrategia de reintentos |
| `relevar-zona` | Orquesta el relevamiento completo de forma autónoma (evalúa con coverage-reporter y elige qué hacer) |

**Regla VG:** SmartGIS primero, siempre. La zona se respeta automáticamente desde `regions.zone_geojson`.

### Flujo Buenos Aires Province (Argentina) — orden obligatorio

| Skill | Cuándo usarla |
|-------|---------------|
| `relevar-manzana-ar` | Usuario menciona Partido, Circunscripción, Sección, Manzana, Ituzaingó, ARBA o cualquier zona argentina |
| `arba-carto-fetcher` | **SIEMPRE el primer paso para PBA.** Requiere JSESSIONID. Si `needs_cookies=true` → pedir JSESSIONID al usuario por Telegram |
| `arba-cadastral-fetcher` | Alternativo a ARBA Carto (WFS público, sin autenticación). Datos menos completos |

### Fuentes de parcelas — Brasil otras ciudades

| Skill | Cuándo usarla |
|-------|---------------|
| `onr-lotes-fetcher` | Lotes urbanos en ciudades brasileñas con cobertura ONR (SP, RJ, Fortaleza, Recife, BH, Curitiba, Manaus, João Pessoa, Natal, Florianópolis, Campo Grande, Niterói, Santa Maria, Rio Branco, São Bernardo, Maringá) |
| `onr-sigef-fetcher` | Predios rurales SIGEF/INCRA (cualquier área de Brasil) |
| `onr-carto-identify` | Identificar qué cartório registra un punto (CNS + nombre). Para VG siempre devuelve CNS=063446 |

### Enriquecimiento

| Skill | Cuándo usarla |
|-------|---------------|
| `osm-building-fetcher` | `footprints == 0` — footprints de edificios OSM (cualquier país) |
| `ibge-census-fetcher` | `setores == 0` en Brasil — descarga setores censitários IBGE 2022 |
| `ibge-logradouros-fetcher` | Brasil, antes de `address-resolver` — geocoding gratis por interpolación |
| `address-resolver` | `parcelas_con_direccion / parcelas < 0.90` — reverse geocoding (IBGE → Google Maps fallback) |
| `uso-classifier` | Clasificar `uso_principal` por parcela (residencial/comercial/mixto) |

### Creación de zonas

| Skill | Cuándo usarla |
|-------|---------------|
| `relevar-zona-geojson` | Usuario sube un GeoJSON con polígonos del área |
| `relevar-zona-br` | Usuario da coordenada + radio en metros (Brasil) |
| `relevar-region` | Región brasileña predefinida (ej: `vg-mt-br`) o coordenada + radio |

### Reportes y exportación

| Skill | Cuándo usarla |
|-------|---------------|
| `relevamiento-reporter` | Usuario pide reporte o resumen del relevamiento |
| `relevamiento-csv` | Usuario pide planilla, Excel, CSV, Google Sheets o Google Drive |
| `relevamiento-pdf` | Usuario pide exportar o recibir el reporte en PDF |

---

## Fuentes de parcelas por zona geográfica

| Zona | Fuente de parcelas | Skill |
|------|-------------------|-------|
| **Buenos Aires Province, AR** | ARBA Carto (catastro oficial) | `arba-carto-fetcher` |
| **Várzea Grande, MT, BR** | SmartGIS (api.smartgis.net.br) | `smartgis-fetcher` |
| **Brasil — ciudades con cobertura ONR** | ONR lotes urbanos | `onr-lotes-fetcher` |
| **Brasil — zona rural** | SIGEF/INCRA (todo Brasil) | `onr-sigef-fetcher` |
| **Cualquier zona** | Edificios OSM (proxy de parcelas) | `osm-building-fetcher` |

---

## Idioma
Siempre responder en español. Todos los mensajes, reportes y notificaciones en español.
