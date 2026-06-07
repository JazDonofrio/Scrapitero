"""Decorador común para el `run()` de los agentes.

Objetivo: que CADA fallo de un agente quede registrado **desde el código del agente**
(no desde las skills ni desde memorias) con dos datos imprescindibles para el técnico
que asiste por Telegram / mira el cuadro de actividad de la web:

  1. **qué skill falló** (slug derivado del módulo: `osm_building_fetcher` → `osm-building-fetcher`)
  2. **el detalle concreto del fallo** (el campo `error` que devolvió el agente)

Así el `error` del output ya viene sellado con el nombre de la skill y el orquestador
sólo tiene que relayarlo tal cual. Además emite un `logger.error` con el mismo contenido,
que es lo que alimenta el log de actividad.
"""

from __future__ import annotations

import functools

from loguru import logger


def _skill_slug(module: str) -> str:
    """`scrapitero.agents.osm_building_fetcher` → `osm-building-fetcher`."""
    return module.rsplit(".", 1)[-1].replace("_", "-")


def agent_run(fn):
    """Envuelve `run()`: si el agente falla, sella el error con el nombre de la skill
    y lo loguea. No altera el camino feliz (output con ok=True)."""
    skill = _skill_slug(fn.__module__)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            out = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — re-lanzamos tras dejar rastro
            logger.error(f"[{skill}] excepción no controlada: {exc!r}")
            raise

        # Sólo tocamos el output cuando el agente reporta fallo (ok=False).
        if getattr(out, "ok", True) is False:
            detail = str(getattr(out, "error", None) or "falló sin detalle")
            if not detail.startswith(skill):
                detail = f"{skill}: {detail}"
            try:
                out.error = detail  # pydantic v2: asignación permitida
            except Exception:  # noqa: BLE001 — si el modelo es inmutable, igual logueamos
                pass
            logger.error(f"[{skill}] {detail}")
        return out

    return wrapper
