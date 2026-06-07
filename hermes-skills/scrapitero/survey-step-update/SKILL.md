---
name: survey-step-update
description: "Registra el progreso de un paso del relevamiento en la DB y devuelve si debe continuar o detenerse. Llamar SIEMPRE después de cada paso del pipeline."
version: 1.0.0
author: Scrapitero
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, pipeline, estado]
    category: scrapitero
---

# Survey Step Update

Registra en la DB el resultado de un paso del pipeline y retorna `should_stop: true`
si el survey fue detenido externamente (usuario hizo click en "Parar").

## Cuándo usar
- Justo después de completar cada paso (smartgis, bci, parser)
- Al finalizar el pipeline (paso="completado", status="completed")
- Si ocurre un error fatal (paso="error", status="failed")
- Si se detecta stop (paso="detenido", status="stopped")

## Comando

```bash
python3 -m scrapitero.rpc.survey_step_update <<< '<JSON>'
```

## Ejemplos de input

Registrar paso exitoso:
```json
{
  "survey_id": "6603f2ef-19f1-4241-bd0e-f224b2bf1fe5",
  "paso": "smartgis",
  "resultado": {"ok": true, "parcelas_insertadas": 88, "lotes_escaneados": 172}
}
```

Marcar completado:
```json
{
  "survey_id": "6603f2ef-19f1-4241-bd0e-f224b2bf1fe5",
  "paso": "completado",
  "status": "completed"
}
```

Marcar fallido con error:
```json
{
  "survey_id": "6603f2ef-19f1-4241-bd0e-f224b2bf1fe5",
  "paso": "error",
  "resultado": {"mensaje": "Timeout conectando a SmartGIS"},
  "status": "failed"
}
```

## Output

```json
{
  "ok": true,
  "survey_id": "...",
  "paso": "smartgis",
  "survey_status": "running",
  "should_stop": false
}
```

Si `should_stop: true` → el usuario detuvo el relevamiento desde la web UI. Detener inmediatamente y notificar por Telegram.
