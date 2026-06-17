#!/usr/bin/env python3
"""Watcher de Cadastur: chequea si el portal de dados abertos volvió y avisa por
Telegram en la transición caído→disponible (no spamea mientras sigue arriba/abajo).

Pensado para correr por cron cada 4 h. Cuando avisa, el operador puede correr el
botón "🏨 Hoteles" del relevamiento (que usa Cadastur como fuente de habitaciones).
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from scrapitero.agents.hotel_fetcher import _HEADERS, _resolver_csv_url, _tg  # noqa: E402

_STATE = Path(__file__).resolve().parent.parent / ".cadastur_state"


def _cadastur_disponible() -> tuple[bool, str]:
    try:
        with httpx.Client(timeout=25, headers=_HEADERS, follow_redirects=True) as c:
            url = _resolver_csv_url(c)
        return (bool(url), url or "")
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def main() -> int:
    disponible, detalle = _cadastur_disponible()
    prev = _STATE.read_text().strip() if _STATE.exists() else "unknown"
    estado = "up" if disponible else "down"
    _STATE.write_text(estado)

    if disponible and prev != "up":
        _tg("✅ <b>Cadastur volvió a estar disponible</b>\n"
            "Ya podés correr el botón 🏨 Hoteles en los relevamientos de Brasil "
            "(trae habitaciones/UHs + situação).")
        print(f"Cadastur UP (transición {prev}→up). Recurso: {detalle[:120]}")
    elif disponible:
        print("Cadastur UP (sin cambio).")
    else:
        print(f"Cadastur DOWN: {detalle[:160]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
