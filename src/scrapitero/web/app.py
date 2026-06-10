"""Web app de Scrapitero — gestión de relevamientos."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import json
import os
import re
import threading
import urllib.request
import uuid
from collections import deque
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, StreamingResponse)
from loguru import logger
from sqlalchemy import text

from scrapitero.db.engine import get_engine

app = FastAPI(title="Scrapitero")
STATIC_DIR = Path(__file__).parent / "static"

# FOS (Factor de Ocupación del Suelo): fracción máxima del terreno que puede ocupar
# la huella del edificio. Se usa para estimar pisos: una huella real ≤ FOS·terreno,
# por lo que pisos ≥ construida/(FOS·terreno) → tomamos el ceil como mínimo de plantas.
FOS_DEFAULT = 0.6


def _pisos_estimados(area_terreno: Optional[float], area_construida: Optional[float],
                     fos: float = FOS_DEFAULT) -> Optional[int]:
    """Estima nº mínimo de pisos: ceil(construida / (FOS·terreno)). None si falta data."""
    if not area_terreno or not area_construida or area_terreno <= 0 or fos <= 0:
        return None
    import math
    pisos = math.ceil(area_construida / (fos * area_terreno))
    return max(1, pisos)

HERMES_WEBHOOK_URL = os.getenv("HERMES_WEBHOOK_URL", "http://localhost:8644/webhooks/relevar")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ── Autenticación (login con 2 roles) ─────────────────────────────────────────
# Dos contraseñas en el entorno: la de operador habilita la vista completa y las
# operaciones de escritura; la de cliente, solo lectura. Si NO se setea OPERADOR_PASSWORD,
# la auth queda DESACTIVADA (modo abierto, útil en local) — en el VPS público hay que
# setearla. El cookie de sesión se firma con HMAC (no se puede falsificar el rol).
OPERADOR_PASSWORD = os.getenv("OPERADOR_PASSWORD", "")
CLIENTE_PASSWORD = os.getenv("CLIENTE_PASSWORD", "")
_AUTH_SECRET = (os.getenv("WEB_AUTH_SECRET") or WEBHOOK_SECRET
                or "scrapitero-dev-secret-cambiar-en-prod")
_AUTH_COOKIE = "scrap_auth"
_AUTH_MAX_AGE = 7 * 24 * 3600          # 7 días
_AUTH_PUBLIC_PATHS = {"/login", "/logout", "/favicon.ico"}


def _auth_enabled() -> bool:
    return bool(OPERADOR_PASSWORD)


def _sign_role(role: str) -> str:
    sig = hmac.new(_AUTH_SECRET.encode(), role.encode(), hashlib.sha256).hexdigest()
    return f"{role}:{sig}"


def _verify_cookie(value: Optional[str]) -> Optional[str]:
    """Devuelve el rol ('operador'/'cliente') si el cookie es válido, si no None."""
    if not value or ":" not in value:
        return None
    role, sig = value.rsplit(":", 1)
    expected = hmac.new(_AUTH_SECRET.encode(), role.encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(sig, expected) and role in ("operador", "cliente"):
        return role
    return None


def _role_for_password(pw: str) -> Optional[str]:
    """Mapea una contraseña al rol. Comparación en tiempo constante."""
    if OPERADOR_PASSWORD and hmac.compare_digest(pw, OPERADOR_PASSWORD):
        return "operador"
    if CLIENTE_PASSWORD and hmac.compare_digest(pw, CLIENTE_PASSWORD):
        return "cliente"
    return None


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    if not _auth_enabled():
        return await call_next(request)            # auth desactivada (sin OPERADOR_PASSWORD)

    path = request.url.path
    if path in _AUTH_PUBLIC_PATHS:
        return await call_next(request)

    role = _verify_cookie(request.cookies.get(_AUTH_COOKIE))
    if not role:
        if path.startswith("/api/"):
            return JSONResponse({"error": "Autenticación requerida"}, status_code=401)
        return RedirectResponse(f"/login?next={path}", status_code=302)

    # Rol: la vista /operador y toda escritura (POST/DELETE/PUT/PATCH) requieren 'operador'.
    if path == "/operador" and role != "operador":
        return RedirectResponse("/login?err=1", status_code=302)
    if request.method not in ("GET", "HEAD", "OPTIONS") and role != "operador":
        return JSONResponse({"error": "Requiere rol operador"}, status_code=403)

    request.state.role = role
    return await call_next(request)

# ── Activity log (loguru sink → SSE + per-survey) ─────────────────────────────

# Hora de Buenos Aires (UTC-3, Argentina no usa horario de verano → offset fijo,
# sin depender de tzdata). Todos los timestamps que ve el operador van en esta zona.
from datetime import datetime, timezone, timedelta
BA_TZ = timezone(timedelta(hours=-3))


def _ahora_ba() -> str:
    """Fecha/hora actual de Buenos Aires en ISO (con offset), para guardar en notes."""
    return datetime.now(BA_TZ).isoformat(timespec="seconds")


_activity: deque = deque(maxlen=500)
_activity_seq: int = 0
_activity_lock = threading.Lock()

# Thread-local: cada thread de pipeline setea su survey_id aquí
_thread_survey_id = threading.local()
# Historial por survey (se llena mientras corre el pipeline)
_activity_by_survey: dict[str, deque] = {}
_activity_by_survey_lock = threading.Lock()


def _activity_sink(message) -> None:
    global _activity_seq
    record = message.record
    if not record["name"].startswith("scrapitero"):
        return
    with _activity_lock:
        _activity_seq += 1
        entry = {
            "id": _activity_seq,
            "ts": record["time"].astimezone(BA_TZ).strftime("%d/%m %H:%M:%S"),
            "lvl": record["level"].name,
            "msg": record["message"],
        }
        _activity.append(entry)

    sid = getattr(_thread_survey_id, "value", None)
    if sid:
        with _activity_by_survey_lock:
            if sid not in _activity_by_survey:
                _activity_by_survey[sid] = deque(maxlen=100)
            _activity_by_survey[sid].append(entry)


# Registrar el sink al importar
logger.add(_activity_sink, level="INFO", format="{message}")


# ── Helpers de geo / slug ──────────────────────────────────────────────────────

def _slugify(s: str) -> str:
    s = s.lower()
    for src, dst in [
        ("áàãâä", "a"), ("éèêë", "e"), ("íìîï", "i"),
        ("óòõôö", "o"), ("úùûü", "u"), ("ç", "c"), ("ñ", "n"),
    ]:
        for c in src:
            s = s.replace(c, dst)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def _bbox_from_geojson(data: dict) -> dict:
    coords: list[tuple[float, float]] = []

    def extract(obj: object) -> None:
        if isinstance(obj, dict):
            t = obj.get("type")
            if t == "FeatureCollection":
                for f in obj.get("features", []):
                    extract(f)
            elif t == "Feature":
                extract(obj.get("geometry") or {})
            elif t in ("Polygon", "MultiPolygon", "LineString", "MultiLineString", "Point"):
                extract(obj.get("coordinates", []))
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, list) and len(item) >= 2 and isinstance(item[0], (int, float)):
                    coords.append((item[0], item[1]))
                else:
                    extract(item)

    extract(data)
    if not coords:
        raise ValueError("Sin coordenadas en el GeoJSON")
    lngs = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return {"south": min(lats), "north": max(lats), "west": min(lngs), "east": max(lngs)}


def _bbox_to_wkt(b: dict) -> str:
    s, n, w, e = b["south"], b["north"], b["west"], b["east"]
    return f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"


# ── Pipeline helpers ───────────────────────────────────────────────────────────

def _set_pipeline_step(survey_id: str, step: str, result: Optional[dict] = None) -> None:
    """Actualiza notes con el paso actual del pipeline."""
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT notes FROM surveys WHERE survey_id = :sid"),
            {"sid": survey_id},
        ).fetchone()

    notas = {}
    if row and row[0]:
        try:
            notas = json.loads(row[0])
        except Exception:
            pass

    ahora = _ahora_ba()
    notas["paso_actual"] = step
    notas["paso_actual_ts"] = ahora
    notas.setdefault("pasos_ts", {})[step] = ahora
    if result is not None:
        notas.setdefault("pasos", {})[step] = result

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE surveys SET notes = :n WHERE survey_id = :sid"),
            {"n": json.dumps(notas), "sid": survey_id},
        )


def _set_survey_status(survey_id: str, status: str) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE surveys SET status=:s, finished_at=NOW() WHERE survey_id=:sid"),
            {"s": status, "sid": survey_id},
        )


# ── Background workers ─────────────────────────────────────────────────────────


def _dispatch_to_hermes(region_id: str, survey_id: str, country_code: str,
                        nombre: str, force_rescan: bool = False) -> None:
    """Llama al webhook de Hermes para orquestar el relevamiento vía LLM."""
    payload = json.dumps({
        "region_id": region_id,
        "survey_id": survey_id,
        "country_code": country_code,
        "nombre": nombre,
        "force_rescan": force_rescan,
    }).encode()

    sig = hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        HERMES_WEBHOOK_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={sig}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            logger.info(f"Hermes webhook OK para {survey_id}: {body.decode()[:200]}")
    except Exception as e:
        logger.error(f"Hermes webhook falló para {survey_id}: {e}")
        _set_survey_status(survey_id, "failed")


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def _cleanup_orphaned_surveys() -> None:
    """Al arrancar, todos los surveys 'running'/'stopping' son huérfanos — marcarlos 'stopped'."""
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE surveys SET status = 'stopped', finished_at = NOW()
            WHERE status IN ('running', 'stopping')
            RETURNING survey_id::text
        """))
        orphans = [r[0] for r in result]
    if orphans:
        logger.info(f"Startup: {len(orphans)} survey(s) huérfanos marcados como 'stopped'")


def _serve_spa() -> FileResponse:
    # no-cache: el dashboard es una SPA de un solo HTML; sin esto el navegador
    # (o un túnel intermedio) sirve una versión vieja del JS y rompe la UI.
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/")
async def root() -> FileResponse:
    """Raíz = vista CLIENTE (solo lectura): lista de relevamientos con progreso/estado y
    análisis (mapa/KPIs/CSV). Sin logs, sin cuadro de actividad, sin crear/parar/relanzar."""
    return _serve_spa()


@app.get("/operador")
async def operador() -> FileResponse:
    """Vista OPERADOR (completa, 'escondida' en esta URL): incluye crear/iniciar/parar/
    re-escanear, cuadro de actividad y log del pipeline. El mismo HTML decide el modo
    según el pathname."""
    return _serve_spa()


def _login_page(error: bool = False, next_url: str = "/") -> HTMLResponse:
    err_html = ('<p class="err">Contraseña incorrecta o sin permiso.</p>' if error else "")
    html = f"""<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Scrapitero — Acceso</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; }}
  body {{ background: #0f172a; color: #e2e8f0; min-height: 100vh; display: flex; align-items: center; justify-content: center; }}
  .card {{ background: #1e293b; padding: 2rem 2.25rem; border-radius: 12px; width: 320px; box-shadow: 0 10px 40px rgba(0,0,0,.4); }}
  h1 {{ font-size: 1.4rem; margin-bottom: .25rem; }} h1 span {{ color: #38bdf8; }}
  p.sub {{ color: #94a3b8; font-size: .85rem; margin-bottom: 1.25rem; }}
  label {{ display:block; font-size:.8rem; color:#94a3b8; margin-bottom:.35rem; }}
  input {{ width:100%; padding:.6rem .7rem; border-radius:8px; border:1px solid #334155; background:#0f172a; color:#e2e8f0; font-size:.95rem; }}
  button {{ width:100%; margin-top:1rem; padding:.65rem; border:0; border-radius:8px; background:#38bdf8; color:#0f172a; font-weight:700; font-size:.95rem; cursor:pointer; }}
  button:hover {{ background:#0ea5e9; }}
  .err {{ color:#fca5a5; font-size:.82rem; margin-bottom:.75rem; }}
  .tero {{ display:block; margin:0 auto .85rem; width:118px; height:auto; }}
</style></head><body>
  <form class="card" method="post" action="/login">
    <svg class="tero" viewBox="0 0 240 210" aria-label="Tero">
      <!-- patas -->
      <g stroke="#fb7185" stroke-width="5" stroke-linecap="round" fill="none">
        <path d="M120 140 L108 186"/>
        <path d="M142 142 L152 186"/>
        <path d="M96 186 h24 M140 186 h24"/>
      </g>
      <!-- cola -->
      <path d="M190 98 q34 -6 44 -22 q-6 26 -30 36 z" fill="#64748b"/>
      <!-- cuerpo -->
      <ellipse cx="140" cy="110" rx="62" ry="40" fill="#94a3b8"/>
      <!-- ala -->
      <path d="M98 98 q54 -12 94 6 q-20 32 -72 24 q-26 -6 -22 -30 z" fill="#64748b"/>
      <!-- pecho negro -->
      <path d="M90 94 q-6 28 12 46 q16 -6 20 -24 q-10 -18 -32 -22 z" fill="#0b1220"/>
      <!-- cuello + cabeza -->
      <path d="M98 98 q-22 -30 -8 -60 q20 6 24 42 z" fill="#cbd5e1"/>
      <circle cx="80" cy="46" r="21" fill="#e2e8f0"/>
      <!-- corona / antifaz negro -->
      <path d="M62 38 q18 -18 38 -8 q-2 16 -20 20 q-12 2 -18 -12 z" fill="#0b1220"/>
      <!-- copete (penacho hacia atrás) -->
      <path d="M94 26 q42 -16 66 -4 q-30 12 -66 12 z" fill="#0b1220"/>
      <!-- pico -->
      <path d="M60 48 l-32 4 l32 9 z" fill="#fb7185"/>
      <!-- ojo -->
      <circle cx="74" cy="42" r="4.6" fill="#0b1220"/>
      <circle cx="75.6" cy="40.4" r="1.5" fill="#fff"/>
    </svg>
    <h1>Scrapi<span>tero</span></h1>
    <p class="sub">Ingresá tu contraseña para continuar</p>
    {err_html}
    <input type="hidden" name="next" value="{next_url}">
    <label>Contraseña</label>
    <input type="password" name="password" autofocus required>
    <button type="submit">Ingresar</button>
  </form>
</body></html>"""
    return HTMLResponse(html)


@app.get("/login")
async def login_get(next: str = "/", err: str = "") -> HTMLResponse:
    if not _auth_enabled():
        return RedirectResponse("/", status_code=302)   # auth desactivada
    return _login_page(error=bool(err), next_url=next or "/")


@app.post("/login")
async def login_post(password: str = Form(...), next: str = Form("/")):
    role = _role_for_password(password)
    if not role:
        return _login_page(error=True, next_url=next or "/")
    # destino: operador → /operador por default; cliente siempre a la raíz
    dest = next if next and next.startswith("/") else "/"
    if role == "cliente":
        dest = "/"
    elif dest in ("/", "/login"):
        dest = "/operador"
    resp = RedirectResponse(dest, status_code=302)
    resp.set_cookie(_AUTH_COOKIE, _sign_role(role), max_age=_AUTH_MAX_AGE,
                    httponly=True, samesite="lax", path="/")
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(_AUTH_COOKIE, path="/")
    return resp


@app.get("/api/config")
async def get_config(request: Request) -> dict:
    """Config para el frontend: API key de Google Maps (key de cliente: restringirla por
    dominio/referrer en Google Cloud Console) + estado de auth y rol de la sesión actual."""
    return {
        "google_maps_key": os.environ.get("GOOGLE_MAPS_API_KEY", ""),
        "auth": _auth_enabled(),
        "role": getattr(request.state, "role", None),
    }


@app.get("/api/surveys")
async def list_surveys() -> list[dict]:
    engine = get_engine()
    with engine.connect() as conn:
        # NOTA: edificios y parcelas se agregan por SEPARADO (subconsultas) para evitar
        # el producto cartesiano que tenía un solo JOIN — antes inflaba SUM(uf_*) y área
        # multiplicándolos por la cantidad de edificios (p.ej. 51 UF × 12 edif = 612).
        # total_uf_vivienda: si la parcela no tiene desglose viv/com, se usa
        # unidades_funcionales_estimadas como vivienda (igual que el "UF total" del mapa).
        rows = conn.execute(text("""
            SELECT
                s.survey_id::text,
                s.region_id,
                s.status,
                s.started_at,
                s.notes,
                r.name                                                             AS region_nombre,
                r.country_code,
                r.zone_geojson,
                COALESCE(ed.total_edificios, 0)                                    AS total_edificios,
                COALESCE(pa.total_parcelas, 0)                                     AS total_parcelas,
                -- UF: la parcela suelta cuenta su UF; cada establecimiento (varias
                -- parcelas = 1 entidad) cuenta como 1, no la suma de sus miembros.
                COALESCE(pa.total_uf_vivienda, 0) + COALESCE(es.est_uf_v, 0)        AS total_uf_vivienda,
                COALESCE(pa.total_uf_comercio, 0) + COALESCE(es.est_uf_c, 0)        AS total_uf_comercio,
                COALESCE(pa.uf_estimado, false)                                    AS uf_estimado,
                COALESCE(pa.con_direccion, 0)                                      AS con_direccion,
                pa.area_total_m2                                                   AS area_total_m2,
                COALESCE(pa.con_inscripcion, 0)                                    AS con_inscripcion,
                COALESCE(es.n_est, 0)                                              AS total_establecimientos
            FROM surveys s
            JOIN regions r ON s.region_id = r.region_id
            LEFT JOIN (
                SELECT survey_id,
                       COUNT(*)                                                    AS total_parcelas,
                       -- sólo parcelas NO agrupadas en un establecimiento
                       COALESCE(SUM(CASE
                           WHEN uf_vivienda IS NULL AND uf_comercio IS NULL
                           THEN COALESCE(unidades_funcionales_estimadas, 0)
                           ELSE COALESCE(uf_vivienda, 0) END)
                           FILTER (WHERE establecimiento_id IS NULL), 0)           AS total_uf_vivienda,
                       COALESCE(SUM(uf_comercio)
                           FILTER (WHERE establecimiento_id IS NULL), 0)           AS total_uf_comercio,
                       BOOL_OR(
                           (uf_fuente IS NOT NULL AND uf_fuente <> 'bci')
                           OR (uf_vivienda IS NULL AND uf_comercio IS NULL
                               AND unidades_funcionales_estimadas IS NOT NULL)
                       )                                                           AS uf_estimado,
                       COUNT(*) FILTER (WHERE calle IS NOT NULL)                   AS con_direccion,
                       SUM(area_m2_terreno)                                        AS area_total_m2,
                       COUNT(*) FILTER (WHERE cca_code IS NOT NULL)                AS con_inscripcion
                FROM parcelas
                GROUP BY survey_id
            ) pa ON pa.survey_id = s.survey_id
            LEFT JOIN (
                SELECT survey_id,
                       COALESCE(SUM(uf_vivienda), 0) AS est_uf_v,
                       COALESCE(SUM(uf_comercio), 0) AS est_uf_c,
                       COUNT(*) AS n_est
                FROM establecimientos GROUP BY survey_id
            ) es ON es.survey_id = s.survey_id
            LEFT JOIN (
                SELECT survey_id, COUNT(*) AS total_edificios
                FROM edificios GROUP BY survey_id
            ) ed ON ed.survey_id = s.survey_id
            ORDER BY s.started_at DESC
        """)).fetchall()

    result = []
    for r in rows:
        notas = {}
        if r[4]:
            try:
                notas = json.loads(r[4])
            except Exception:
                pass
        result.append({
            "survey_id": r[0],
            "region_id": r[1],
            "status": r[2],
            "started_at": r[3].isoformat() if r[3] else None,
            "pipeline": notas,
            "region_nombre": r[5],
            "country_code": r[6],
            "zone_geojson": r[7],
            "total_edificios": int(r[8] or 0),
            "total_parcelas": int(r[9] or 0),
            "total_uf_vivienda": int(r[10] or 0),
            "total_uf_comercio": int(r[11] or 0),
            "uf_estimado": bool(r[12]),
            "con_direccion": int(r[13] or 0),
            "area_total_m2": float(r[14]) if r[14] else None,
            "con_inscripcion": int(r[15] or 0),
            "total_establecimientos": int(r[16] or 0),
        })
    return result


@app.post("/api/surveys")
async def create_survey(
    nombre: str = Form(...),
    country_code: str = Form("AUTO"),   # "AUTO" → autodetectar del centroide del GeoJSON
    geojson_file: UploadFile = File(...),
) -> JSONResponse:
    geojson_bytes = await geojson_file.read()
    try:
        geojson_str = geojson_bytes.decode("utf-8")
        geojson_data = json.loads(geojson_str)
        bbox = _bbox_from_geojson(geojson_data)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    # País: autodetectado del centroide del GeoJSON (cualquier país) salvo override.
    if not country_code or country_code.upper() == "AUTO":
        from scrapitero.agents import geo
        lat = (bbox["south"] + bbox["north"]) / 2.0
        lng = (bbox["west"] + bbox["east"]) / 2.0
        country_code = await asyncio.to_thread(geo.detect_country, lat, lng)
        if not country_code:
            return JSONResponse({"ok": False, "error": (
                "No se pudo autodetectar el país del GeoJSON (reverse-geocoding falló). "
                "Reintentá o elegí el país manualmente."
            )}, status_code=422)

    region_id = f"zona-{_slugify(nombre)}"
    survey_id = str(uuid.uuid4())

    engine = get_engine()
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO regions (region_id, name, country_code, zone_geojson, bbox_wkt)
                VALUES (:rid, :name, :cc, :geojson, :bbox)
                ON CONFLICT (region_id) DO UPDATE
                    SET name = EXCLUDED.name,
                        zone_geojson = EXCLUDED.zone_geojson,
                        bbox_wkt = EXCLUDED.bbox_wkt
            """), {
                "rid": region_id, "name": nombre, "cc": country_code,
                "geojson": geojson_str, "bbox": _bbox_to_wkt(bbox),
            })
            conn.execute(text("""
                INSERT INTO surveys (survey_id, region_id, status)
                VALUES (:sid, :rid, 'running')
            """), {"sid": survey_id, "rid": region_id})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    _set_survey_status(survey_id, "stopped")  # listo para que el usuario haga click en Iniciar
    return JSONResponse({"ok": True, "survey_id": survey_id, "region_id": region_id})


@app.get("/api/activity/stream")
async def activity_stream() -> StreamingResponse:
    """SSE: polling 300ms — envía nuevos log entries en tiempo real."""
    async def generate() -> AsyncGenerator[str, None]:
        last_id = 0
        heartbeat = 0
        yield "event: ping\ndata: {}\n\n"
        while True:
            with _activity_lock:
                new = [e for e in _activity if e["id"] > last_id]
            if new:
                last_id = new[-1]["id"]
                for entry in new:
                    yield f"data: {json.dumps(entry)}\n\n"
                heartbeat = 0
            else:
                heartbeat += 1
                if heartbeat >= 100:   # cada ~30s sin actividad
                    yield "event: ping\ndata: {}\n\n"
                    heartbeat = 0
            await asyncio.sleep(0.3)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/surveys/{survey_id}/parcelas")
async def survey_parcelas(survey_id: str) -> list[dict]:
    """Devuelve centroide + metadata de cada parcela para renderizar en el mapa."""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT p.centroid_lat, p.centroid_lng,
                   p.uso_principal, p.uf_vivienda, p.uf_comercio,
                   p.unidades_funcionales_estimadas,
                   p.calle, p.numero, p.barrio, p.cca_code,
                   p.area_m2_terreno, p.area_m2_construida,
                   p.uf_fuente,
                   COALESCE((
                       SELECT array_agg(c.nombre ORDER BY c.nombre)
                       FROM comercios c
                       WHERE c.parcela_id = p.parcela_id AND c.nombre IS NOT NULL
                   ), '{}') AS comercios,
                   p.valor_venal_terreno, p.valor_venal_construccion,
                   p.valor_venal_total, p.aliquota, p.anio_construccion,
                   p.propietario_nombre, p.propietario_documento,
                   p.contribuyente_secundario,
                   p.establecimiento_id::text, e.tipo, e.nombre, e.n_parcelas
            FROM parcelas p
            LEFT JOIN establecimientos e ON e.establecimiento_id = p.establecimiento_id
            WHERE p.survey_id = :sid
              AND p.centroid_lat IS NOT NULL AND p.centroid_lng IS NOT NULL
            ORDER BY p.calle NULLS LAST
        """), {"sid": survey_id}).fetchall()
    out = []
    for r in rows:
        # UF consistente con el resumen: si no hay desglose viv/com, se usa
        # unidades_funcionales_estimadas como vivienda (vivienda por defecto).
        # uf_total = uf_viv + uf_com (antes mostraba la columna estimada por separado,
        # lo que difería del resumen).
        uf_viv_raw, uf_com_raw, ufe = r[3], r[4], r[5]
        if uf_viv_raw is None and uf_com_raw is None:
            uf_viv = int(ufe or 0)
            estimado_fallback = ufe is not None
        else:
            uf_viv = int(uf_viv_raw or 0)
            estimado_fallback = False
        uf_com = int(uf_com_raw or 0)
        out.append({
            "lat": float(r[0]), "lng": float(r[1]),
            "uso": r[2] or "",
            "uf_viv": uf_viv, "uf_com": uf_com,
            "uf_total": uf_viv + uf_com,
            "calle": r[6] or "", "numero": r[7] or "", "barrio": r[8] or "",
            "cca": r[9] or "",
            "area_t": round(float(r[10]), 1) if r[10] else None,
            "area_c": round(float(r[11]), 1) if r[11] else None,
            "uf_fuente": r[12] or "",
            "uf_estimado": (bool(r[12]) and r[12] != "bci") or estimado_fallback,
            "comercios": list(r[13] or []),
            "pisos": _pisos_estimados(
                float(r[10]) if r[10] else None,
                float(r[11]) if r[11] else None,
            ),
            "vv_terreno": round(float(r[14]), 2) if r[14] else None,
            "vv_construccion": round(float(r[15]), 2) if r[15] else None,
            "vv_total": round(float(r[16]), 2) if r[16] else None,
            "aliquota": round(float(r[17]), 4) if r[17] else None,
            "anio": int(r[18]) if r[18] else None,
            "propietario": r[19] or "",
            "propietario_doc": r[20] or "",
            "contrib_sec": r[21] or "",
            "est_id": r[22] or None,
            "est_tipo": r[23] or None,
            "est_nombre": r[24] or None,
            "est_n_parcelas": int(r[25]) if r[25] else None,
        })
    return out


@app.get("/api/surveys/{survey_id}/activity")
async def survey_activity(survey_id: str, since: int = Query(0)) -> dict:
    """Devuelve entradas de log del pipeline de este survey. 'since' es el último id visto."""
    with _activity_by_survey_lock:
        entries = list(_activity_by_survey.get(survey_id, []))
    new = [e for e in entries if e["id"] > since]
    last_id = entries[-1]["id"] if entries else 0
    return {"entries": new, "last_id": last_id}


@app.post("/api/surveys/{survey_id}/dasimetrico")
async def run_dasimetrico(survey_id: str) -> JSONResponse:
    """Estimación ADICIONAL de habitantes por manzana (desagregación dasimétrica).

    Es secundaria al relevamiento principal (menos exacta). Corre in-process — es sólo
    cómputo en DB sobre los setores censales y las parcelas ya cargadas.
    """
    row = _get_survey_row(survey_id)
    if not row:
        return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
    region_id = row[0]

    from scrapitero.agents.dasymetric_population import DasymetricInput
    from scrapitero.agents.dasymetric_population import run as run_dasi

    def _job() -> dict:
        _thread_survey_id.value = survey_id
        return run_dasi(DasymetricInput(region_id=region_id, survey_id=survey_id)).model_dump()

    data = await asyncio.to_thread(_job)
    return JSONResponse(data, status_code=200 if data.get("ok") else 422)


@app.get("/api/surveys/{survey_id}/manzanas")
async def survey_manzanas(survey_id: str) -> dict:
    """Resultado de la estimación dasimétrica: habitantes por manzana (+ UF de la manzana)."""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT manzana_codigo, habitantes_est, habitantes_low, habitantes_high,
                   uf_vivienda, uf_comercio, n_parcelas, metodo, fecha_estimacion,
                   ST_Y(ST_Centroid(geometry)) AS lat, ST_X(ST_Centroid(geometry)) AS lng
            FROM manzanas_habitantes
            WHERE survey_id = :sid
            ORDER BY habitantes_est DESC NULLS LAST
        """), {"sid": survey_id}).mappings().all()

    manzanas = [{
        "codigo": r["manzana_codigo"],
        "habitantes": round(r["habitantes_est"]) if r["habitantes_est"] is not None else None,
        "habitantes_low": round(r["habitantes_low"]) if r["habitantes_low"] is not None else None,
        "habitantes_high": round(r["habitantes_high"]) if r["habitantes_high"] is not None else None,
        "uf_vivienda": int(r["uf_vivienda"] or 0),
        "uf_comercio": int(r["uf_comercio"] or 0),
        "n_parcelas": int(r["n_parcelas"] or 0),
        "metodo": r["metodo"] or "",
        "lat": float(r["lat"]) if r["lat"] is not None else None,
        "lng": float(r["lng"]) if r["lng"] is not None else None,
    } for r in rows]

    total_hab = sum(m["habitantes"] or 0 for m in manzanas)
    fecha = rows[0]["fecha_estimacion"].isoformat() if rows else None
    return {
        "manzanas": manzanas,
        "total_manzanas": len(manzanas),
        "total_habitantes": total_hab,
        "fecha_estimacion": fecha,
    }


def _get_survey_row(survey_id: str) -> Optional[tuple]:
    engine = get_engine()
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT s.region_id, r.country_code, s.status
            FROM surveys s JOIN regions r ON s.region_id = r.region_id
            WHERE s.survey_id = :sid
        """), {"sid": survey_id}).fetchone()


@app.post("/api/surveys/{survey_id}/iniciar")
async def iniciar_relevamiento(
    survey_id: str,
    rescan: bool = Query(False, description="Re-corre SmartGIS aunque ya haya parcelas"),
) -> JSONResponse:
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT s.region_id, r.country_code, s.status, r.name
            FROM surveys s JOIN regions r ON s.region_id = r.region_id
            WHERE s.survey_id = :sid
        """), {"sid": survey_id}).fetchone()

    if not row:
        return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
    region_id, country_code, status, nombre = row

    if status == "running":
        return JSONResponse({"ok": False, "error": "Relevamiento ya en ejecución"}, status_code=409)

    _set_survey_status(survey_id, "running")

    threading.Thread(
        target=_dispatch_to_hermes,
        args=(region_id, survey_id, country_code, nombre, rescan),
        daemon=True,
    ).start()
    return JSONResponse({"ok": True, "message": "Relevamiento delegado a Hermes"})


@app.post("/api/surveys/{survey_id}/parar")
async def parar_relevamiento(survey_id: str) -> JSONResponse:
    row = _get_survey_row(survey_id)
    if not row:
        return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
    _, _, status = row
    if status not in ("running",):
        return JSONResponse({"ok": False, "error": "El relevamiento no está corriendo"}, status_code=409)

    # Marcar como stopped en la DB — Hermes detecta should_stop en el próximo survey-step-update
    _set_survey_status(survey_id, "stopped")
    logger.info(f"Stop solicitado para survey {survey_id} — Hermes lo detectará en el próximo paso")
    return JSONResponse({"ok": True, "message": "Stop solicitado — Hermes detendrá el relevamiento"})


_ESTADOS_VALIDOS = {"running", "stopping", "stopped", "partial", "completed", "failed"}


@app.post("/api/surveys/{survey_id}/estado")
async def set_estado(survey_id: str, status: str = Form(...)) -> JSONResponse:
    """Fija manualmente el estado del relevamiento (solo operador — POST gateado por el
    middleware de auth). No corre ni detiene el pipeline; solo cambia la etiqueta de estado.
    Para estados finales sella `finished_at`; al pasar a 'running' lo limpia."""
    if status not in _ESTADOS_VALIDOS:
        return JSONResponse(
            {"ok": False, "error": f"Estado inválido: {status!r}. "
             f"Válidos: {', '.join(sorted(_ESTADOS_VALIDOS))}"}, status_code=400)
    engine = get_engine()
    with engine.begin() as conn:
        exists = conn.execute(text("SELECT 1 FROM surveys WHERE survey_id=:sid"),
                              {"sid": survey_id}).fetchone()
        if not exists:
            return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
        if status == "running":
            conn.execute(text("UPDATE surveys SET status=:s, finished_at=NULL WHERE survey_id=:sid"),
                         {"s": status, "sid": survey_id})
        else:
            conn.execute(text("UPDATE surveys SET status=:s, finished_at=NOW() WHERE survey_id=:sid"),
                         {"s": status, "sid": survey_id})
    logger.info(f"Estado de survey {survey_id} cambiado manualmente a '{status}'")
    return JSONResponse({"ok": True, "status": status})


@app.get("/api/surveys/{survey_id}/export/csv")
async def export_csv(survey_id: str) -> StreamingResponse:
    """Descarga un CSV con todas las parcelas del relevamiento."""
    engine = get_engine()
    with engine.connect() as conn:
        # Info del survey
        meta = conn.execute(text("""
            SELECT s.region_id, r.name, s.started_at
            FROM surveys s JOIN regions r ON s.region_id = r.region_id
            WHERE s.survey_id = :sid
        """), {"sid": survey_id}).fetchone()
        if not meta:
            return JSONResponse({"error": "Survey no encontrado"}, status_code=404)

        rows = conn.execute(text("""
            SELECT
                cca_code,
                nomenclatura_catastral,
                calle,
                numero,
                complemento,
                barrio,
                municipio,
                codigo_postal,
                uso_principal,
                uf_vivienda,
                uf_comercio,
                unidades_funcionales_estimadas,
                area_m2_terreno,
                area_m2_construida,
                pisos_estimados_max,
                partida_inmobiliaria,
                fuente_parcela,
                direccion_source,
                centroid_lat,
                centroid_lng,
                uf_fuente,
                uso_fuente,
                COALESCE((
                    SELECT string_agg(c.nombre, ' | ' ORDER BY c.nombre)
                    FROM comercios c
                    WHERE c.parcela_id = parcelas.parcela_id AND c.nombre IS NOT NULL
                ), '') AS comercios,
                valor_venal_terreno,
                valor_venal_construccion,
                valor_venal_total,
                aliquota,
                anio_construccion,
                propietario_nombre,
                propietario_documento,
                contribuyente_secundario,
                (SELECT e.tipo FROM establecimientos e
                   WHERE e.establecimiento_id = parcelas.establecimiento_id),
                (SELECT e.nombre FROM establecimientos e
                   WHERE e.establecimiento_id = parcelas.establecimiento_id),
                establecimiento_id::text
            FROM parcelas
            WHERE survey_id = :sid
            ORDER BY calle NULLS LAST, numero NULLS LAST
        """), {"sid": survey_id}).fetchall()

    region_nombre = meta[1]
    fecha = meta[2].strftime("%Y-%m-%d") if meta[2] else ""
    filename = f"relevamiento_{meta[0]}_{fecha}.csv".replace(" ", "_")

    def _gen():
        buf = io.StringIO()
        # BOM para Excel
        buf.write("﻿")
        w = csv.writer(buf, delimiter=";")
        w.writerow([f"Relevamiento: {region_nombre}", f"Survey: {survey_id}", f"Fecha: {fecha}"])
        w.writerow([])
        w.writerow([
            "Inscripción", "Setor-Quadra-Lote", "Calle", "Número",
            "Complemento", "Bairro", "Municipio", "CEP",
            "Uso", "Uso Fuente", "UF Vivienda", "UF Comercio", "Total UF", "UF Fuente",
            "Área Terreno m²", "Área Construida m²", "Pisos",
            "Matrícula", "Fuente", "Fuente dirección",
            "Lat", "Lng", "Comercios (Google)",
            "Valor Venal Terreno", "Valor Venal Construcción", "Valor Venal Total",
            "Alícuota", "Año Construcción",
            "Propietario", "Documento (CPF/CNPJ)", "Contribuyente Secundario",
            "Establecimiento (tipo)", "Establecimiento (nombre)",
        ])
        for r in rows:
            w.writerow([
                r[0] or "", r[1] or "",
                r[2] or "", r[3] or "", r[4] or "",
                r[5] or "", r[6] or "", r[7] or "",
                r[8] or "", r[21] or "", r[9] or 0, r[10] or 0,
                r[11] or 0, r[20] or "",
                f"{r[12]:.2f}" if r[12] else "",
                f"{r[13]:.2f}" if r[13] else "",
                r[14] or "",
                r[15] or "", r[16] or "", r[17] or "",
                f"{r[18]:.6f}" if r[18] else "",
                f"{r[19]:.6f}" if r[19] else "",
                r[22] or "",
                f"{r[23]:.2f}" if r[23] else "",
                f"{r[24]:.2f}" if r[24] else "",
                f"{r[25]:.2f}" if r[25] else "",
                f"{r[26]:.4f}" if r[26] else "",
                r[27] or "",
                r[28] or "", r[29] or "", r[30] or "",
                r[31] or "", r[32] or "",
            ])
        yield buf.getvalue()

    return StreamingResponse(
        _gen(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/surveys/{survey_id}")
async def eliminar_survey(survey_id: str) -> JSONResponse:
    row = _get_survey_row(survey_id)
    if not row:
        return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
    _, _, status = row
    if status in ("running", "stopping"):
        return JSONResponse(
            {"ok": False, "error": "Detené el pipeline antes de eliminar"},
            status_code=409,
        )

    engine = get_engine()
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                DELETE FROM unidades_funcionales
                WHERE edificio_id IN (
                    SELECT edificio_id FROM edificios WHERE survey_id = :sid
                )
            """), {"sid": survey_id})
            conn.execute(text("DELETE FROM edificios WHERE survey_id = :sid"), {"sid": survey_id})
            conn.execute(text("DELETE FROM parcelas  WHERE survey_id = :sid"), {"sid": survey_id})
            conn.execute(text("DELETE FROM orchestrator_log WHERE survey_id = :sid"), {"sid": survey_id})
            conn.execute(text("DELETE FROM surveys WHERE survey_id = :sid"), {"sid": survey_id})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    logger.info(f"Survey {survey_id} eliminado")
    return JSONResponse({"ok": True})
