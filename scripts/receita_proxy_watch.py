#!/usr/bin/env python3
"""Watchdog del acceso a Receita vía proxy Bright Data (cron de Hermes, patrón --no-agent).

Bright Data bloquea los sitios `.gov.br` detrás de un KYC + habilitación de dominio
(error `policy_20051`). Este watcher prueba el acceso a `dadosabertos.rfb.gov.br` a través
de `RECEITA_PROXY` y avisa por Telegram SOLO en la transición bloqueado→habilitado, para
saber el momento exacto en que se puede lanzar la carga del universo de hospedagem (CNPJ).

Patrón --no-agent: imprime a stdout solo en la transición (Hermes lo entrega a Telegram);
stdout vacío = silencio. Historial en `/opt/data/receita_proxy_watch.log`.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

_STATE = Path("/opt/data/.receita_proxy_state")     # "blocked" | "open"
_LOG = Path("/opt/data/receita_proxy_watch.log")
_HERMES_ENV = Path("/opt/data/.env")
_TARGET = "http://dadosabertos.rfb.gov.br/CNPJ/"     # HTTP: el 403 de BrightData trae x-brd-err-code


def _proxy() -> str:
    """RECEITA_PROXY del entorno, o lo parsea de /opt/data/.env (el gateway puede no
    haberlo recargado tras agregarlo)."""
    p = os.environ.get("RECEITA_PROXY")
    if p:
        return p
    if _HERMES_ENV.exists():
        for line in _HERMES_ENV.read_text().splitlines():
            if line.startswith("RECEITA_PROXY="):
                return line.split("=", 1)[1].strip()
    return ""


def _check(proxy: str) -> tuple[bool, str]:
    """(habilitado, detalle). habilitado = el proxy NO devuelve un error de policy de
    BrightData (llegamos a Receita). detalle lleva el código brd o el HTTP de Receita."""
    try:
        with httpx.Client(proxy=proxy, timeout=40, follow_redirects=False, verify=False,
                          headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = c.get(_TARGET)
        brd = r.headers.get("x-brd-err-code")
        if brd:
            return False, f"BrightData {brd}"
        return True, f"Receita HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001  (ProxyError 403 en CONNECT = bloqueado)
        return False, f"{type(e).__name__}: {e}"[:160]


def main() -> int:
    proxy = _proxy()
    if not proxy:
        return 0  # sin proxy configurado: silencio (no es el caso a vigilar)

    habilitado, detalle = _check(proxy)
    prev = _STATE.read_text().strip() if _STATE.exists() else "unknown"
    estado = "open" if habilitado else "blocked"

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        _STATE.write_text(estado)
        with _LOG.open("a") as f:
            f.write(f"{ts}\t{estado}\t{detalle}\n")
    except OSError:
        pass

    if habilitado and prev != "open":
        print(
            "✅ Bright Data habilitó el acceso a Receita por el proxy BR "
            f"({detalle}).\n"
            "Ya se puede lanzar la carga del universo de hospedagem (CNPJ / "
            "receita_cnpj_fetcher). Avisá para arrancar la primera descarga."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
