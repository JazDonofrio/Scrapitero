"""
Carga de configuración desde .env.

Importar este módulo al arranque garantiza que las variables de
entorno estén disponibles en `os.environ`. Las llaves se leen
desde getters tipados para evitar typos.
"""

from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

# Carga .env desde la raíz del repo (un nivel arriba de este paquete).
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")


def telegram_token() -> str | None:
    return os.environ.get("TELEGRAM_TOKEN") or None


def telegram_chat_id() -> str | None:
    return os.environ.get("TELEGRAM_CHAT_ID") or None


def google_api_key() -> str | None:
    """Google Maps Geocoding API. Si es None, los agentes caen a Nominatim."""
    return os.environ.get("GOOGLE_API_KEY") or None


def carto_jsessionid() -> str | None:
    return os.environ.get("CARTO_JSESSIONID") or None


def carto_token() -> str | None:
    return os.environ.get("CARTO_TOKEN") or None


def anthropic_api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY") or None
