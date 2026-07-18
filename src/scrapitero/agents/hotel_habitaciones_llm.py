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
import unicodedata
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
    cache_ttl_dias: int = 90         # reusar respuesta cacheada si es más nueva que esto


class HotelHabLLMOutput(BaseModel):
    ok: bool = True
    error: Optional[str] = None
    region_id: str = ""
    consultados: int = 0
    resueltos: int = 0               # con número escrito
    desconocidos: int = 0
    desde_cache: int = 0             # servidos del caché LLM (sin pegarle a Gemini)


def _norm(s: Optional[str]) -> str:
    """Normaliza para la clave de caché sin CNPJ: sin acentos (á→a), minúsculas, sin
    puntuación, 1 espacio. Transliterar evita que 'Águia'/'Aguia' den claves distintas."""
    base = unicodedata.normalize("NFKD", s or "")
    base = "".join(ch for ch in base if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", base.lower())).strip()


def _cache_key(cnpj: Optional[str], nombre: Optional[str], ciudad: Optional[str]) -> Optional[str]:
    """Clave global del hotel: por CNPJ si existe; si no, nombre normalizado + ciudad
    (los hoteles de Google/OSM no traen CNPJ). None si no hay con qué identificarlo."""
    dig = re.sub(r"\D", "", cnpj or "")
    if dig:
        return f"cnpj:{dig}"
    nom = _norm(nombre)
    return f"nom:{nom}|{_norm(ciudad)}" if nom else None


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
            SELECT hotel_id::text, nombre, tipo, direccion, telefono, cnpj
            FROM hoteles
            WHERE region_id=:r AND NOT cerrado_def AND habitaciones IS NULL
              AND nombre IS NOT NULL
            ORDER BY nombre
            LIMIT :lim
        """), {"r": input.region_id, "lim": input.max_hoteles}).fetchall()

    if not hoteles:
        _tg("🛏️ LLM habitaciones: no hay hoteles sin habitaciones para consultar.")
        return out

    # Caché LLM (migración 033): respuestas ya conseguidas — número o "no encontrado" —
    # por CNPJ / nombre+ciudad, dentro del TTL. Evita re-pagarle a Gemini por el mismo
    # hotel tras un re-corte de 🏨 (que reconstruye `hoteles` y borra el dato del LLM).
    claves = {k for h in hoteles if (k := _cache_key(h[5], h[1], mun))}
    cache: dict[str, Optional[int]] = {}
    if claves:
        with engine.connect() as conn:
            for row in conn.execute(text("""
                SELECT clave, habitaciones FROM hotel_habitaciones_llm_cache
                WHERE clave = ANY(:cl)
                  AND created_at > now() - make_interval(days => :ttl)
            """), {"cl": list(claves), "ttl": input.cache_ttl_dias}):
                cache[row[0]] = row[1]   # None = consultado pero no encontrado

    def _escribir_hoteles(hid: str, n: int) -> None:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE hoteles SET habitaciones=:n, habitaciones_fuente='llm' "
                "WHERE hotel_id::text=:h AND habitaciones IS NULL"), {"n": n, "h": hid})

    _tg(f"🛏️ <b>LLM habitaciones</b>: {len(hoteles)} hoteles de "
        f"{mun or input.region_id} ({len(cache)} desde caché, "
        f"{len(hoteles) - len(cache)} a Gemini)…")

    for hid, nombre, tipo, direccion, _tel, cnpj in hoteles:
        out.consultados += 1
        clave = _cache_key(cnpj, nombre, mun)

        # 1) Caché: si ya consultamos este hotel (dentro del TTL), no le pagamos a Gemini.
        if clave is not None and clave in cache:
            out.desde_cache += 1
            n_cache = cache[clave]
            if n_cache is not None:
                _escribir_hoteles(hid, n_cache)
                out.resueltos += 1
            else:
                out.desconocidos += 1
            continue

        # 2) Miss: preguntarle a Gemini (pago).
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

        # Guardar en caché el resultado (número o "no encontrado") para no re-pagarlo.
        if clave is not None:
            with engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO hotel_habitaciones_llm_cache (clave, habitaciones, encontrado, nombre, created_at)
                    VALUES (:k, :n, :enc, :nom, now())
                    ON CONFLICT (clave) DO UPDATE SET
                        habitaciones=EXCLUDED.habitaciones, encontrado=EXCLUDED.encontrado,
                        nombre=EXCLUDED.nombre, created_at=now()
                """), {"k": clave, "n": n, "enc": n is not None, "nom": nombre})
            cache[clave] = n

        if n is None:
            out.desconocidos += 1
        else:
            _escribir_hoteles(hid, n)
            out.resueltos += 1
            logger.info(f"LLM habitaciones: '{nombre}' → {n}")
        time.sleep(max(input.delay_ms, 0) / 1000.0)

    _tg(f"🛏️ <b>LLM habitaciones</b> ({mun or input.region_id}): "
        f"{out.resueltos} resueltos, {out.desconocidos} sin dato, de {out.consultados} "
        f"({out.desde_cache} desde caché, sin costo).")
    logger.info(f"LLM habitaciones {input.region_id}: resueltos={out.resueltos} "
                f"desconocidos={out.desconocidos} consultados={out.consultados} "
                f"desde_cache={out.desde_cache}")
    return out
