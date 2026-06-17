#!/usr/bin/env python3
"""Watchdog de Cadastur para el cron de Hermes (patrón --no-agent).

Chequea el portal de dados abertos de Cadastur y avisa SOLO en las transiciones
de estado (una vez cada una, sin spam):
  • disponible→caído  → "⚠️ Cadastur caído (HTTP 502 / timeout / …)"
  • caído→disponible  → "✅ Cadastur volvió"

A diferencia de un watcher que llama a ``_tg`` directo, este imprime el aviso a
stdout y Hermes lo entrega al canal de Telegram (``--deliver telegram``). stdout
vacío ⇒ silencio. Cada corrida deja una línea con timestamp + detalle en
``/opt/data/cadastur_watch.log`` para poder distinguir a mano 502 (backend caído)
de 403/451 (bloqueo por IP) o timeout, aunque no haya habido transición.

Pensado para correr cada 4 h vía ``hermes cron``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

# Auto-inyectar los paths del proyecto (el cron de Hermes corre con sys.executable
# del venv del gateway; no dependemos de PYTHONPATH).
for _p in ("/opt/scrapitero/.hermes-packages", "/opt/scrapitero/src"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import httpx  # noqa: E402

from scrapitero.agents.hotel_fetcher import _CKAN_PACKAGE, _HEADERS  # noqa: E402

_STATE = Path("/opt/data/.cadastur_state")          # "up" | "down" (lógica de transición)
_LOG = Path("/opt/data/cadastur_watch.log")          # historial: timestamp + detalle


def _check() -> tuple[bool, str]:
    """Devuelve (disponible, detalle). ``detalle`` distingue la causa del fallo:
    "HTTP 502", "HTTP 200 sin recurso CSV", "Timeout: …", etc."""
    try:
        with httpx.Client(timeout=25, headers=_HEADERS, follow_redirects=True) as c:
            r = c.get(_CKAN_PACKAGE)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        resources = ((r.json().get("result") or {}).get("resources") or [])
        if any("csv" in (x.get("format") or "").lower() for x in resources) or resources:
            return True, "HTTP 200 (recurso CSV disponible)"
        return False, "HTTP 200 sin recurso CSV"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"[:160]


def main() -> int:
    disponible, detalle = _check()
    prev = _STATE.read_text().strip() if _STATE.exists() else "unknown"
    estado = "up" if disponible else "down"

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        _STATE.write_text(estado)
        with _LOG.open("a") as f:
            f.write(f"{ts}\t{estado}\t{detalle}\n")
    except OSError:
        pass

    # Avisar solo en las transiciones (stdout vacío = silencio).
    if disponible and prev != "up":
        print(
            "✅ Cadastur volvió a estar disponible.\n"
            "Ya podés correr el botón 🏨 Hoteles en los relevamientos de Brasil "
            "(trae habitaciones/UHs + situação)."
        )
    elif not disponible and prev == "up":
        print(
            f"⚠️ Cadastur se cayó ({detalle}).\n"
            "El botón 🏨 Hoteles seguirá trayendo OSM/Google, pero no las "
            "habitaciones/UHs ni la situação oficial hasta que vuelva."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
