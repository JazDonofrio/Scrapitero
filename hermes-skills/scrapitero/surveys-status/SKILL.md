---
name: surveys-status
description: "Lista el estado de todos los relevamientos (surveys) activos o recientes. Usar cuando el usuario pregunta qué relevamientos están corriendo, cuánto llevan, cuántas parcelas hay, etc."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, relevamiento, estado, status]
    category: scrapitero
---

# Surveys Status

Devuelve un resumen de todos los surveys activos (o recientes) con métricas básicas.

## Cuándo usar
Cuando el usuario dice cosas como:
- "qué relevamientos están corriendo?"
- "estado de los relevamientos en curso"
- "cuánto lleva el relevamiento?"
- "hay algún relevamiento activo?"
- "mostrá todos los surveys"

## Comando

Solo los activos (status=running):
```bash
echo '{}' |

  python3 -m scrapitero.rpc.surveys_status
```

Todos (incluye completed/failed):
```bash
echo '{"solo_activos": false}' |

  python3 -m scrapitero.rpc.surveys_status
```

## Output esperado
```json
{
  "total": 1,
  "surveys": [
    {
      "survey_id": "uuid",
      "nombre": "varzea pequeno etapa 2",
      "region_id": "zona-varzea-pequeno-etapa-2",
      "status": "running",
      "paso_actual": "smartgis",
      "started_at": "2026-05-31 15:39 UTC",
      "duracion_minutos": 441,
      "parcelas": 88,
      "edificios": 0,
      "steps": 0
    }
  ]
}
```

## Cómo presentar el resultado

Usar el campo `nombre` como nombre del relevamiento (es el nombre visible que el usuario asignó).
Usar `paso_actual` para decir en qué etapa está: "smartgis" → descargando parcelas, "bci" → descargando PDFs, "parser" → procesando PDFs, "completado" → terminado.

**Relevamientos en curso (1)**

📍 **varzea pequeno etapa 2**
- Estado: running
- Paso actual: smartgis (descargando parcelas)
- Inicio: 31/05 15:39 UTC — lleva 441 min
- Parcelas: 88

Si no hay surveys activos: "No hay relevamientos en curso en este momento."
