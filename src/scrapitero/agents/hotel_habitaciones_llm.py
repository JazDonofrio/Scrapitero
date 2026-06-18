"""HotelHabitacionesLLM — completa las habitaciones faltantes preguntándole a Gemini.

Para los hoteles **abiertos sin cantidad de habitaciones** (ninguna fuente la tiene:
Cadastur caído, sin OSM rooms, estimación BCI desactivada), le pregunta a **Gemini** (Google,
con **grounding de Google Search**) cuántas habitaciones tiene cada hotel — ej.: "¿Cuántas
habitaciones tiene el HOTEL X de Várzea Grande?". Si el modelo devuelve un número (lo busca en
la web, no de memoria), se registra en `hoteles.habitaciones` con `habitaciones_fuente='llm'`.
Si no encuentra info confiable, se deja en NULL.

Requiere una API key de Gemini (`GEMINI_API_KEY` / `GOOGLE_API_KEY`). Llamada por HTTP directo
(httpx) a la Generative Language API (`generateContent`). Es **pago** (1 llamada con búsqueda por
hotel) → tope `max_hoteles` + throttle. El valor del LLM rellena solo donde no hay dato exacto
(Cadastur/OSM/manual tienen prioridad); un re-corte del botón 🏨 lo borra (re-ejecutar el agente
lo vuelve a completar).
"""

from __future__ import annotations

import os
import re
import time
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.baseline_geocoder import _tg
from scrapitero.db.engine import get_engine

_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_HABS_RE = re.compile(r"HABITACIONES:\s*([0-9]{1,5}|DESCONOCIDO)", re.IGNORECASE)


def _api_key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GOOGLE_MAPS_API_KEY") or "")


class HotelHabLLMInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None
    model: str = "gemini-2.5-flash"
    max_hoteles: int = 40            # tope de hoteles a consultar (control de costo)
    delay_ms: int = 800              # throttle entre llamadas
    timeout_s: int = 120
    hab_min: int = 1                 # rango de cordura del número devuelto
    hab_max: int = 2000


class HotelHabLLMOutput(BaseModel):
    ok: bool = True
    error: Optional[str] = None
    region_id: str = ""
    consultados: int = 0
    resueltos: int = 0               # con número escrito
    desconocidos: int = 0


def _ask_gemini(api_key: str, model: str, prompt: str, timeout: int) -> str:
    """Texto de respuesta de Gemini con grounding de Google Search."""
    url = _API_URL.format(model=model)
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 1024},
    }
    with httpx.Client(timeout=timeout) as c:
        r = c.post(url, params={"key": api_key},
                   headers={"content-type": "application/json"}, json=body)
        r.raise_for_status()
        d = r.json()
    cands = d.get("candidates") or []
    if not cands:
        return ""
    parts = ((cands[0].get("content") or {}).get("parts") or [])
    return "".join(p.get("text", "") for p in parts)


def _parse_habitaciones(txt: str, lo: int, hi: int) -> Optional[int]:
    hits = _HABS_RE.findall(txt or "")
    if not hits:
        return None
    ultimo = hits[-1].upper()
    if ultimo == "DESCONOCIDO":
        return None
    try:
        n = int(ultimo)
    except ValueError:
        return None
    return n if lo <= n <= hi else None


@agent_run
def run(input: HotelHabLLMInput) -> HotelHabLLMOutput:
    out = HotelHabLLMOutput(region_id=input.region_id)
    api_key = _api_key()
    if not api_key:
        return HotelHabLLMOutput(ok=False, region_id=input.region_id,
                                 error="falta API key de Gemini (GEMINI_API_KEY / GOOGLE_API_KEY)")

    engine = get_engine()
    with engine.connect() as conn:
        mun = conn.execute(text(
            "SELECT r.name FROM regions r WHERE r.region_id=:r"), {"r": input.region_id}).scalar()
        hoteles = conn.execute(text("""
            SELECT hotel_id::text, nombre, tipo, direccion, telefono
            FROM hoteles
            WHERE region_id=:r AND NOT cerrado_def AND habitaciones IS NULL
              AND nombre IS NOT NULL
            ORDER BY nombre
            LIMIT :lim
        """), {"r": input.region_id, "lim": input.max_hoteles}).fetchall()

    if not hoteles:
        _tg("🛏️ LLM habitaciones: no hay hoteles sin habitaciones para consultar.")
        return out

    _tg(f"🛏️ <b>LLM habitaciones</b>: consultando {len(hoteles)} hoteles de "
        f"{mun or input.region_id} a Gemini (Google Search)…")

    for hid, nombre, tipo, direccion, _tel in hoteles:
        out.consultados += 1
        ubic = ", ".join(x for x in (direccion, mun, "Brasil") if x)
        prompt = (
            f"¿Cuántas habitaciones (quartos / unidades habitacionais) tiene o tenía "
            f"\"{nombre}\"{(' (' + tipo + ')') if tipo else ''}, ubicado en {ubic}? "
            "Buscá en la web para confirmarlo. Dame tu mejor número SOLO si encontrás "
            "información confiable de ESE hotel puntual; si no, no inventes.\n"
            "Terminá tu respuesta con una línea EXACTA así, sin texto extra después:\n"
            "HABITACIONES: <entero>   (o)   HABITACIONES: DESCONOCIDO"
        )
        try:
            txt = _ask_gemini(api_key, input.model, prompt, input.timeout_s)
        except httpx.HTTPError as e:
            logger.warning(f"LLM habitaciones: fallo consultando '{nombre}': {e}")
            continue
        n = _parse_habitaciones(txt, input.hab_min, input.hab_max)
        if n is None:
            out.desconocidos += 1
        else:
            with engine.begin() as conn:
                conn.execute(text(
                    "UPDATE hoteles SET habitaciones=:n, habitaciones_fuente='llm' "
                    "WHERE hotel_id::text=:h AND habitaciones IS NULL"),
                    {"n": n, "h": hid})
            out.resueltos += 1
            logger.info(f"LLM habitaciones: '{nombre}' → {n}")
        time.sleep(max(input.delay_ms, 0) / 1000.0)

    _tg(f"🛏️ <b>LLM habitaciones</b> ({mun or input.region_id}): "
        f"{out.resueltos} resueltos, {out.desconocidos} sin dato, de {out.consultados} consultados.")
    logger.info(f"LLM habitaciones {input.region_id}: resueltos={out.resueltos} "
                f"desconocidos={out.desconocidos} consultados={out.consultados}")
    return out
