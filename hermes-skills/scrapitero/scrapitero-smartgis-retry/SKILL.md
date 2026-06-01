---
name: scrapitero-smartgis-retry
description: "Documenta la estrategia de reintentos robusta para el `smartgis_fetcher` dentro del pipeline de relevamiento de `relevar-zona`, abordando fallos por timeout."
version: 1.0.0
author: Hermes Agent
platforms: [linux]
metadata:
  hermes:
    tags: [scrapitero, smartgis, retry, timeout, brasil, relevamiento]
    related_skills: [relevar-zona]
---

# Estrategia de reintentos para SmartGIS en `relevar-zona`

Cuando el paso SmartGIS (`smartgis_fetcher`) falla durante el relevamiento de una región brasileña, se debe seguir la siguiente estrategia de reintentos para asegurar la robustez del proceso:

1.  **Intento inicial**: Se realiza la primera llamada a `smartgis_fetcher`.

2.  **Reintentos rápidos**: Si el intento inicial falla, se realizan hasta 3 reintentos adicionales con un **intervalo de 30 segundos** entre cada uno. En cada reintento, se debe usar un `timeout` de 300 segundos para la ejecución del comando.

3.  **Reintento lento (si los rápidos fallan)**: Si los 3 reintentos rápidos también fallan, se debe notificar al usuario vía Telegram con el mensaje: "⚠️ SmartGIS no responde para {nombre}. ¿El sitio vg.abaco.com.br está accesible? Reintentando en 5 min...", y se debe esperar **5 minutos** antes de realizar un último reintento final. Este reintento también debe usar un `timeout` de 300 segundos.

4.  **Fallo definitivo**: Si después de todos los reintentos (el inicial, los 3 rápidos y el lento) el comando `smartgis_fetcher` sigue fallando (por ejemplo, por `Command timed out`), se debe registrar el error en la base de datos, marcar el estado del paso como "failed", notificar al usuario por Telegram con el error completo y detener el proceso de relevamiento.

Esta estrategia busca agotar las posibilidades de conexión temporal o rendimiento lento del servicio SmartGIS antes de declarar un fallo definitivo.
