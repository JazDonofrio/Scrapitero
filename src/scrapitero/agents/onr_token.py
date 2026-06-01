"""ONRToken — obtiene y cachea el token ArcGIS de mapa.onr.org.br.

El token se inyecta en la página principal como window.sTokenArcGis y dura ~8h.
Se cachea en disco para no refetchear en cada corrida.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx
from loguru import logger

ONR_URL = "https://mapa.onr.org.br/"
TOKEN_FILE = Path("/opt/scrapitero/.onr_token.json")
TOKEN_TTL = 7 * 3600  # 7h (el token dura 8h, renovamos con margen)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0",
    "Accept-Language": "pt-BR,pt;q=0.9",
}


def _load_cached() -> str | None:
    try:
        if TOKEN_FILE.exists():
            data = json.loads(TOKEN_FILE.read_text())
            if time.time() - data.get("ts", 0) < TOKEN_TTL:
                return data.get("token")
    except Exception:
        pass
    return None


def _save(token: str) -> None:
    try:
        TOKEN_FILE.write_text(json.dumps({"token": token, "ts": time.time()}))
        TOKEN_FILE.chmod(0o600)
    except Exception:
        pass


def get_token(force_refresh: bool = False) -> str:
    if not force_refresh:
        cached = _load_cached()
        if cached:
            logger.debug("ONR: usando token cacheado")
            return cached

    logger.info("ONR: obteniendo token de mapa.onr.org.br...")
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        r = client.get(ONR_URL, headers=_HEADERS)
        r.raise_for_status()

    tokens = re.findall(r"sTokenArcGis\s*=\s*'([^']{20,})'", r.text)
    if not tokens:
        raise RuntimeError("No se pudo extraer sTokenArcGis de mapa.onr.org.br")

    token = tokens[0]
    _save(token)
    logger.info(f"ONR: token obtenido ({token[:12]}...)")
    return token
