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

from scrapitero.agents.logradouro_br import descomponer_logradouro
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
# Única escritura permitida al rol cliente: dejar comentarios (sugerencias/correcciones)
# en puntos del mapa de un relevamiento.
_CLIENTE_POST_RE = re.compile(r"^/api/surveys/[0-9a-fA-F-]+/comentarios$")


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
    if (request.method not in ("GET", "HEAD", "OPTIONS") and role != "operador"
            and not (request.method == "POST" and _CLIENTE_POST_RE.match(path))):
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

# Thread-local: cada thread de trabajo (pipeline, geocoding de un baseline, etc.)
# setea aquí el id del "job" — survey_id o baseline_id — para que sus logs se
# archiven en un buffer propio y la UI los muestre con fecha/hora.
_thread_job_id = threading.local()
# Historial por job (se llena mientras corre el pipeline / geocoding)
_activity_by_job: dict[str, deque] = {}
_activity_by_job_lock = threading.Lock()


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

    jid = getattr(_thread_job_id, "value", None)
    if jid:
        with _activity_by_job_lock:
            if jid not in _activity_by_job:
                _activity_by_job[jid] = deque(maxlen=600)
            _activity_by_job[jid].append(entry)


def _job_activity(job_id: str, since: int) -> dict:
    """Entradas de log del job (survey o baseline) posteriores a `since`."""
    with _activity_by_job_lock:
        entries = list(_activity_by_job.get(job_id, []))
    new = [e for e in entries if e["id"] > since]
    last_id = entries[-1]["id"] if entries else 0
    return {"entries": new, "last_id": last_id}


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
        if status == "running":
            # Al arrancar: marcar inicio y LIMPIAR finished_at. Antes sellaba finished_at
            # para CUALQUIER estado → un survey 'running' quedaba con finished_at (estado
            # contradictorio), y si Hermes no lo terminaba quedaba "running" para siempre.
            conn.execute(
                text("UPDATE surveys SET status=:s, started_at=NOW(), finished_at=NULL "
                     "WHERE survey_id=:sid"),
                {"s": status, "sid": survey_id},
            )
        else:
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
async def list_surveys(request: Request) -> list[dict]:
    # El rol 'cliente' solo ve los surveys con visible_cliente=true (el operador los
    # tilda en su lista). Con auth desactivada no hay rol: se devuelve todo y la SPA
    # filtra según la vista.
    solo_visibles = getattr(request.state, "role", None) == "cliente"
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
                COALESCE(es.n_est, 0)                                              AS total_establecimientos,
                s.visible_cliente,
                s.archivado,
                s.subzona_geojson,
                s.baseline_id::text
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
            WHERE (:solo_visibles = false OR (s.visible_cliente AND NOT s.archivado))
            ORDER BY s.started_at DESC
        """), {"solo_visibles": solo_visibles}).fetchall()

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
            "total_edificios": int(r[8] or 0),
            "total_parcelas": int(r[9] or 0),
            "total_uf_vivienda": int(r[10] or 0),
            "total_uf_comercio": int(r[11] or 0),
            "uf_estimado": bool(r[12]),
            "con_direccion": int(r[13] or 0),
            "area_total_m2": float(r[14]) if r[14] else None,
            "con_inscripcion": int(r[15] or 0),
            "total_establecimientos": int(r[16] or 0),
            "visible_cliente": bool(r[17]),
            "archivado": bool(r[18]),
            # Relevamiento parcial: el mapa muestra la SUB-zona, no la región entera
            "zone_geojson": r[19] or r[7],
            "es_parcial": bool(r[19]),
            # Actualización: relevamiento anterior a graficar (gris) bajo las parcelas
            "baseline_id": r[20],
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


# ── Tipo de edificación unificado (taxonomía del cliente) ─────────────────────
# Un solo label por parcela, de la lista fija del cliente. Prioridad:
#   1) hotel vinculado → HOTEL/MOTEL/FLAT/PENSÃO
#   2) establecimiento CNPJ (descripcion_uso) → esa descripción (BAR, ESCOLA, HOSPITAL…)
#   3) uso del catastro/BCI: vacante/sin construir→LOTE VAZIO, residencial→RESIDÊNCIA
#      (uf_vivienda=1) / APARTAMENTO (>1), industrial→INDÚSTRIA, comercial y mixto→
#      COMÉRCIO EM GERAL (la taxonomía del cliente no tiene 'MIXTO').

def _hotel_tipo_label(t: Optional[str]) -> str:
    t = (t or "").lower()
    if "motel" in t:
        return "MOTEL"
    if "apart" in t or "flat" in t:
        return "FLAT"
    if "pens" in t:
        return "PENSÃO"
    return "HOTEL"


def _tipo_edificacion(uso: Optional[str], uf_v, area, descripcion: Optional[str],
                      hotel_tipo: Optional[str]) -> str:
    if hotel_tipo:
        return _hotel_tipo_label(hotel_tipo)
    if descripcion:                                  # 1+ descripciones CNPJ → la primera
        return descripcion.split(",")[0].strip()
    u = (uso or "").lower()
    if u == "vacante" or not area:
        return "LOTE VAZIO"
    if u == "residencial":
        return "APARTAMENTO" if (uf_v or 0) > 1 else "RESIDÊNCIA"
    if u == "industrial":
        return "INDÚSTRIA"
    if u == "comercial":
        return "COMÉRCIO EM GERAL"
    if u == "mixto":
        # 'MIXTO' no está en la taxonomía del cliente (solo Brasil): la parcela mixta
        # (vivienda + comercio) se reporta como comercial. Ver docs/TIPOS_PROPIEDAD.md.
        return "COMÉRCIO EM GERAL"
    return ""


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
                   p.establecimiento_id::text, e.tipo, e.nombre, e.n_parcelas,
                   p.parcela_id::text, p.categoria_uso, p.descripcion_uso,
                   COALESCE((SELECT h.tipo FROM hoteles h
                       WHERE h.parcela_id = p.parcela_id AND NOT h.cerrado_def
                       ORDER BY h.habitaciones DESC NULLS LAST LIMIT 1), '') AS hotel_tipo
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
            "parcela_id": r[26],
            # Categoría/descripción de uso (taxonomía del cliente, de establecimientos CNPJ)
            "categoria_uso": r[27] or None,
            "descripcion_uso": r[28] or None,
            # Tipo de edificación unificado (1 label de la lista del cliente)
            "tipo_edificacion": _tipo_edificacion(r[2], uf_viv, r[11], r[28], r[29]) or None,
        })
    return out


@app.get("/api/surveys/{survey_id}/activity")
async def survey_activity(survey_id: str, since: int = Query(0)) -> dict:
    """Devuelve entradas de log del pipeline de este survey. 'since' es el último id visto."""
    return _job_activity(survey_id, since)


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
        _thread_job_id.value = survey_id
        return run_dasi(DasymetricInput(region_id=region_id, survey_id=survey_id)).model_dump()

    data = await asyncio.to_thread(_job)
    return JSONResponse(data, status_code=200 if data.get("ok") else 422)


@app.post("/api/surveys/{survey_id}/hoteles")
async def run_hoteles(survey_id: str, google: bool = True) -> JSONResponse:
    """Corre HotelFetcher para la región del survey: trae hoteles con habitaciones (UHs)
    y estado abierto/cerrado, los vincula a su parcela y suma las habitaciones como
    uf_comercio (hoteles abiertos). Solo Brasil.

    `google` (query, default True): si es False NO usa Google Places (la fuente PAGA) —
    corre solo las gratuitas (Cadastur + Receita + OSM). El tilde de la web lo controla."""
    row = _get_survey_row(survey_id)
    if not row:
        return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
    region_id = row[0]

    from scrapitero.agents.hotel_fetcher import HotelFetcherInput
    from scrapitero.agents.hotel_fetcher import run as run_hot

    fuentes = ["cadastur", "receita", "osm"] + (["google"] if google else [])

    def _job() -> dict:
        _thread_job_id.value = survey_id
        return run_hot(HotelFetcherInput(region_id=region_id, survey_id=survey_id,
                                         fuentes=fuentes)).model_dump()

    data = await asyncio.to_thread(_job)
    return JSONResponse(data, status_code=200 if data.get("ok") else 422)


@app.post("/api/surveys/{survey_id}/habitaciones-llm")
async def run_habitaciones_llm(survey_id: str) -> JSONResponse:
    """Completa con IA (Gemini + búsqueda web) las habitaciones de los hoteles abiertos sin
    dato. Pago por hotel; rellena solo donde no hay dato exacto. Solo Brasil/operador."""
    row = _get_survey_row(survey_id)
    if not row:
        return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
    region_id = row[0]

    from scrapitero.agents.hotel_habitaciones_llm import HotelHabLLMInput
    from scrapitero.agents.hotel_habitaciones_llm import run as run_llm

    def _job() -> dict:
        _thread_job_id.value = survey_id
        return run_llm(HotelHabLLMInput(region_id=region_id, survey_id=survey_id)).model_dump()

    data = await asyncio.to_thread(_job)
    return JSONResponse(data, status_code=200 if data.get("ok") else 422)


@app.post("/api/surveys/{survey_id}/clonar")
async def clonar_relevamiento(survey_id: str) -> JSONResponse:
    """Clona un relevamiento: copia la región (polígono + município + país) a una región +
    survey NUEVOS y VACÍOS en estado 'stopped', listos para ▶ Iniciar de cero. El original
    queda intacto. NO copia parcelas/hoteles/baseline/sub-zona — arranca limpio."""
    engine = get_engine()
    with engine.connect() as conn:
        src = conn.execute(text("""
            SELECT r.name, r.country_code, r.zone_geojson, r.bbox_wkt, r.municipio_codigo
            FROM surveys s JOIN regions r ON r.region_id = s.region_id
            WHERE s.survey_id = CAST(:sid AS uuid)
        """), {"sid": survey_id}).fetchone()
    if not src:
        return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
    if not src[2]:
        return JSONResponse({"ok": False, "error": "La región no tiene zona (zone_geojson) para clonar"},
                            status_code=422)
    name, cc, geojson, bbox, muni = src
    new_name = f"{name} (copia)"
    new_region = f"zona-{_slugify(name)}-copia-{uuid.uuid4().hex[:6]}"
    new_survey = str(uuid.uuid4())
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO regions (region_id, name, country_code, zone_geojson, bbox_wkt, municipio_codigo)
                VALUES (:rid, :name, :cc, :geojson, :bbox, :muni)
            """), {"rid": new_region, "name": new_name, "cc": cc,
                   "geojson": geojson, "bbox": bbox, "muni": muni})
            conn.execute(text(
                "INSERT INTO surveys (survey_id, region_id, status) VALUES (:sid, :rid, 'stopped')"),
                {"sid": new_survey, "rid": new_region})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": True, "survey_id": new_survey, "region_id": new_region,
                         "name": new_name})


@app.get("/api/surveys/{survey_id}/hoteles")
async def survey_hoteles(survey_id: str) -> JSONResponse:
    """Hoteles del relevamiento (para el mapa/popup): nombre, habitaciones, estado."""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT h.parcela_id::text, h.nombre, h.tipo, h.cnpj,
                   h.habitaciones, h.leitos, h.cerrado_def,
                   COALESCE(h.business_status, h.situacion_cadastur) AS estado,
                   ST_Y(h.location) AS lat, ST_X(h.location) AS lng, h.fuente,
                   h.habitaciones_fuente, h.direccion
            FROM hoteles h
            WHERE h.region_id = (SELECT region_id FROM surveys WHERE survey_id = CAST(:sid AS uuid))
              AND (h.survey_id = CAST(:sid AS uuid) OR h.survey_id IS NULL)
            ORDER BY h.cerrado_def, h.nombre
        """), {"sid": survey_id}).fetchall()
    hoteles = [{
        "parcela_id": r[0], "nombre": r[1], "tipo": r[2], "cnpj": r[3],
        "habitaciones": int(r[4]) if r[4] is not None else None,
        "leitos": int(r[5]) if r[5] is not None else None,
        "cerrado": bool(r[6]), "estado": r[7],
        "lat": float(r[8]) if r[8] is not None else None,
        "lng": float(r[9]) if r[9] is not None else None,
        "fuente": r[10],
        # 'cadastur'/'osm' = exacto · 'bci_proxy' = estimado por área
        "habitaciones_estimadas": (r[11] == "bci_proxy"),
        "habitaciones_fuente": r[11],
        "direccion": r[12],
    } for r in rows]
    abiertos = [h for h in hoteles if not h["cerrado"]]
    return JSONResponse({
        "ok": True, "hoteles": hoteles, "total": len(hoteles),
        "abiertos": len(abiertos), "cerrados": len(hoteles) - len(abiertos),
        "habitaciones_total": sum(h["habitaciones"] or 0 for h in abiertos),
    })


@app.get("/asistencia-hoteles/{survey_id}")
async def asistencia_hoteles_page(survey_id: str):
    """Página (operador) de carga manual de habitaciones: mapa con solo los hoteles +
    datos del hotel y del relevamiento anterior, para que un humano consiga el dato faltante."""
    return FileResponse(STATIC_DIR / "asistencia-hoteles.html")


@app.get("/api/surveys/{survey_id}/hoteles-asistencia")
async def hoteles_asistencia(survey_id: str) -> JSONResponse:
    """Hoteles ABIERTOS sin habitaciones (ninguna fuente las tiene) + datos de contacto y
    ubicación, para resolver a mano. Incluye `baseline_id` del survey (relevamiento anterior)."""
    engine = get_engine()
    with engine.connect() as conn:
        meta = conn.execute(text(
            "SELECT region_id, baseline_id::text FROM surveys WHERE survey_id = :sid"),
            {"sid": survey_id}).fetchone()
        if not meta:
            return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
        rows = conn.execute(text("""
            SELECT hotel_id::text, nombre, cnpj, tipo, telefono, direccion,
                   COALESCE(business_status, situacion_cadastur) AS estado, fuente,
                   ST_Y(location) AS lat, ST_X(location) AS lng, parcela_id::text
            FROM hoteles
            WHERE region_id = :rid
              AND (survey_id = CAST(:sid AS uuid) OR survey_id IS NULL)
              AND NOT cerrado_def AND habitaciones IS NULL AND location IS NOT NULL
            ORDER BY nombre
        """), {"rid": meta[0], "sid": survey_id}).fetchall()
    hoteles = [{
        "hotel_id": r[0], "nombre": r[1], "cnpj": r[2], "tipo": r[3], "telefono": r[4],
        "direccion": r[5], "estado": r[6], "fuente": r[7],
        "lat": float(r[8]) if r[8] is not None else None,
        "lng": float(r[9]) if r[9] is not None else None, "parcela_id": r[10],
    } for r in rows]
    return JSONResponse({"ok": True, "survey_id": survey_id, "baseline_id": meta[1],
                         "hoteles": hoteles, "total": len(hoteles)})


@app.post("/api/hoteles/{hotel_id}/habitaciones")
async def set_habitaciones_manual(hotel_id: str, request: Request) -> JSONResponse:
    """Carga manual de habitaciones (asistencia humana). Las marca `manual` y las persiste
    por CNPJ en `hotel_habitaciones_manual` para que sobrevivan a un re-corte del botón 🏨."""
    try:
        body = await request.json()
        n = int(body.get("habitaciones"))
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "habitaciones inválido"}, status_code=400)
    if n < 0:
        return JSONResponse({"ok": False, "error": "habitaciones debe ser ≥ 0"}, status_code=400)
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT region_id, cnpj FROM hoteles WHERE hotel_id::text = :h"),
            {"h": hotel_id}).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "Hotel no encontrado"}, status_code=404)
        region_id, cnpj = row
        conn.execute(text(
            "UPDATE hoteles SET habitaciones = :n, habitaciones_fuente = 'manual' "
            "WHERE hotel_id::text = :h"), {"n": n, "h": hotel_id})
        if cnpj:
            conn.execute(text("""
                INSERT INTO hotel_habitaciones_manual (region_id, cnpj, habitaciones, autor)
                VALUES (:r, :c, :n, 'operador')
                ON CONFLICT (region_id, cnpj)
                DO UPDATE SET habitaciones = :n, actualizado_at = now()
            """), {"r": region_id, "c": cnpj, "n": n})
    return JSONResponse({"ok": True, "habitaciones": n})


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


@app.post("/api/surveys/{survey_id}/visibilidad")
async def set_visibilidad(survey_id: str, visible: bool = Form(...)) -> JSONResponse:
    """Tilda/destilda el relevamiento en la vista CLIENTE (solo operador — POST gateado
    por el middleware de auth). El rol cliente solo ve surveys con visible_cliente=true."""
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(text(
            "UPDATE surveys SET visible_cliente=:v WHERE survey_id=:sid RETURNING 1"),
            {"v": visible, "sid": survey_id}).fetchone()
    if not row:
        return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
    logger.info(f"Survey {survey_id} {'visible' if visible else 'oculto'} para la vista cliente")
    return JSONResponse({"ok": True, "visible_cliente": visible})


# ── Comentarios del cliente (sugerencias/correcciones sobre el mapa) ──────────

def _telegram_notify(msg: str) -> None:
    """Aviso al operador por Telegram (best-effort, no bloquea si falla)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id": chat_id, "text": msg,
                             "parse_mode": "HTML"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.warning(f"No se pudo notificar por Telegram: {e}")


_COMENTARIO_MAX_LEN = 2000


@app.get("/api/surveys/{survey_id}/comentarios")
async def list_comentarios(survey_id: str) -> list[dict]:
    """Comentarios del relevamiento (ambos roles los ven en el mapa)."""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT c.comentario_id::text,
                   ST_Y(c.geometry) AS lat, ST_X(c.geometry) AS lng,
                   c.texto, c.autor_rol, c.estado, c.created_at, c.resuelto_at,
                   p.calle, p.numero, c.parcela_id::text
            FROM comentarios_cliente c
            LEFT JOIN parcelas p ON p.parcela_id = c.parcela_id
            WHERE c.survey_id = :sid
            ORDER BY c.created_at
        """), {"sid": survey_id}).fetchall()
    return [{
        "comentario_id": r[0],
        "lat": float(r[1]), "lng": float(r[2]),
        "texto": r[3],
        "autor_rol": r[4],
        "estado": r[5],
        "created_at": r[6].isoformat() if r[6] else None,
        "resuelto_at": r[7].isoformat() if r[7] else None,
        "parcela_direccion": " ".join(str(x) for x in (r[8], r[9]) if x) or None,
        "parcela_id": r[10],
    } for r in rows]


@app.post("/api/surveys/{survey_id}/comentarios")
async def crear_comentario(survey_id: str, request: Request,
                           parcela_id: str = Form(...),
                           texto: str = Form(...)) -> JSONResponse:
    """Crea un comentario (sugerencia/corrección) sobre UNA parcela relevada del survey
    (uno de los puntos del mapa). Se abre desde el popup de detalle del círculo. Es la
    única escritura permitida al rol cliente (excepción en el middleware de auth)."""
    texto = texto.strip()
    if not texto:
        return JSONResponse({"ok": False, "error": "El comentario está vacío"},
                            status_code=400)
    if len(texto) > _COMENTARIO_MAX_LEN:
        return JSONResponse(
            {"ok": False, "error": f"Comentario demasiado largo (máx. {_COMENTARIO_MAX_LEN} caracteres)"},
            status_code=400)

    rol = getattr(request.state, "role", None)
    engine = get_engine()
    with engine.begin() as conn:
        survey = conn.execute(text("""
            SELECT r.name, s.visible_cliente FROM surveys s
            JOIN regions r ON r.region_id = s.region_id
            WHERE s.survey_id = :sid
        """), {"sid": survey_id}).fetchone()
        if not survey or (rol == "cliente" and not survey[1]):
            # un survey oculto no existe para el cliente
            return JSONResponse({"ok": False, "error": "Survey no encontrado"},
                                status_code=404)
        # la parcela debe ser una de las relevadas en ESTE survey
        parcela = conn.execute(text("""
            SELECT TRIM(CONCAT(calle, ' ', numero)), centroid_lat, centroid_lng
            FROM parcelas WHERE parcela_id = :pid AND survey_id = :sid
        """), {"pid": parcela_id, "sid": survey_id}).fetchone()
        if not parcela:
            return JSONResponse(
                {"ok": False, "error": "La parcela no pertenece a este relevamiento"},
                status_code=404)
        row = conn.execute(text("""
            INSERT INTO comentarios_cliente
                   (comentario_id, survey_id, parcela_id, geometry, texto, autor_rol)
            SELECT :cid, :sid, :pid,
                   COALESCE(ST_SetSRID(ST_MakePoint(centroid_lng, centroid_lat), 4326),
                            ST_PointOnSurface(geometry)),
                   :texto, :rol
            FROM parcelas WHERE parcela_id = :pid
            RETURNING comentario_id::text, created_at
        """), {"cid": str(uuid.uuid4()), "sid": survey_id, "pid": parcela_id,
               "texto": texto, "rol": rol}).fetchone()

    region_nombre = survey[0]
    direccion = parcela[0] or "(sin dirección)"
    logger.info(f"Nuevo comentario ({rol or 'sin auth'}) en survey {survey_id}"
                f" — parcela {direccion}: {texto[:120]}")
    # Aviso al operador: el comentario del cliente es accionable (sugerencia/corrección).
    coords = (f"{parcela[1]:.6f}, {parcela[2]:.6f}"
              if parcela[1] is not None and parcela[2] is not None else "—")
    asyncio.get_running_loop().run_in_executor(None, _telegram_notify, (
        f"💬 <b>Nuevo comentario del cliente</b>\n"
        f"Relevamiento: {region_nombre}\n"
        f"Parcela: {direccion}\n"
        f"Punto: {coords}\n"
        f"«{texto}»"
    ))
    return JSONResponse({"ok": True, "comentario_id": row[0],
                         "parcela_direccion": direccion,
                         "created_at": row[1].isoformat() if row[1] else None})


@app.post("/api/comentarios/{comentario_id}/estado")
async def set_comentario_estado(comentario_id: str, estado: str = Form(...)) -> JSONResponse:
    """Marca un comentario como resuelto (o lo reabre). Solo operador (middleware)."""
    if estado not in ("pendiente", "resuelto"):
        return JSONResponse({"ok": False, "error": f"Estado inválido: {estado!r}. "
                             "Válidos: pendiente, resuelto"}, status_code=400)
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(text("""
            UPDATE comentarios_cliente
            SET estado = :e,
                resuelto_at = CASE WHEN CAST(:e AS varchar) = 'resuelto' THEN NOW() ELSE NULL END
            WHERE comentario_id = :cid RETURNING 1
        """), {"e": estado, "cid": comentario_id}).fetchone()
    if not row:
        return JSONResponse({"ok": False, "error": "Comentario no encontrado"},
                            status_code=404)
    return JSONResponse({"ok": True, "estado": estado})


@app.delete("/api/comentarios/{comentario_id}")
async def eliminar_comentario(comentario_id: str) -> JSONResponse:
    """Elimina un comentario. Solo operador (middleware)."""
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(text(
            "DELETE FROM comentarios_cliente WHERE comentario_id = :cid RETURNING 1"),
            {"cid": comentario_id}).fetchone()
    if not row:
        return JSONResponse({"ok": False, "error": "Comentario no encontrado"},
                            status_code=404)
    return JSONResponse({"ok": True})


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
                -- UF vivienda EFECTIVA (mismo criterio que la web y la comparativa):
                -- sin desglose viv/com, las unidades estimadas cuentan como vivienda.
                CASE WHEN uf_vivienda IS NULL AND uf_comercio IS NULL
                     THEN COALESCE(unidades_funcionales_estimadas, 0)
                     ELSE COALESCE(uf_vivienda, 0) END AS uf_vivienda,
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
                establecimiento_id::text,
                parcelas.parcela_id::text,
                categoria_uso,
                descripcion_uso,
                COALESCE((
                    SELECT string_agg(h.nombre, ' | ' ORDER BY h.nombre)
                    FROM hoteles h
                    WHERE h.parcela_id = parcelas.parcela_id
                      AND h.nombre IS NOT NULL AND NOT h.cerrado_def
                ), '') AS hoteles_nombres,
                COALESCE((SELECT h.tipo FROM hoteles h
                    WHERE h.parcela_id = parcelas.parcela_id AND NOT h.cerrado_def
                    ORDER BY h.habitaciones DESC NULLS LAST LIMIT 1), '') AS hotel_tipo
            FROM parcelas
            WHERE survey_id = :sid
            ORDER BY calle NULLS LAST, numero NULLS LAST
        """), {"sid": survey_id}).fetchall()

        # Unidades del BCI por parcela (sólo parcelas con >1 unidad las tienen).
        # Para esas, el CSV emite una fila por unidad repitiendo la dirección +
        # complemento, identificada por "Unidade N" + código de unidad.
        unidades_por_parcela: dict[str, list[dict]] = {}
        for ur in conn.execute(text("""
            SELECT p.parcela_id::text, u.n_unidade, u.codigo_unidade, u.area_m2, u.uso
            FROM parcela_unidades u JOIN parcelas p ON p.parcela_id = u.parcela_id
            WHERE p.survey_id = :sid
            ORDER BY u.n_unidade
        """), {"sid": survey_id}):
            unidades_por_parcela.setdefault(ur[0], []).append({
                "n": ur[1], "codigo": ur[2], "area_m2": ur[3], "uso": ur[4],
            })

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
            "Dirección", "Unidad", "Código Unidad", "Uso", "UF Vivienda", "UF Comercio",
            "Total UF", "UF Fuente", "Uso Fuente",
            "Bairro", "Municipio", "CEP",
            "Inscripción", "Setor-Quadra-Lote",
            "Área Terreno m²", "Área Construida m²", "Pisos",
            "Matrícula", "Fuente", "Fuente dirección",
            "Lat", "Lng", "Comercios (Google)",
            "Valor Venal Terreno", "Valor Venal Construcción", "Valor Venal Total",
            "Alícuota", "Año Construcción",
            "Propietario", "Documento (CPF/CNPJ)", "Contribuyente Secundario",
            "Establecimiento (tipo)", "Establecimiento (nombre)",
            "Categoría (R/C/E)", "Descripción (CNPJ)",
            "DSC_NOME_DO_IMOVEL", "DSC_LOGRADOURO_NO",
            "Tipo de edificación",
        ])

        def _fila(r, direccion, unidad, codigo, uso, uf_v, uf_c, total, area_con):
            """Arma una fila del CSV. `unidad`/`codigo`/`uso`/UF/`area_con` pueden venir
            de una unidad del BCI (fila expandida) o de la parcela (fila normal)."""
            return [
                direccion or "(sin dirección)",
                unidad, codigo,
                uso or "", uf_v or 0, uf_c or 0,
                total or 0, r[20] or "", r[21] or "",
                r[5] or "", r[6] or "", r[7] or "",
                r[0] or "", r[1] or "",
                f"{r[12]:.2f}" if r[12] else "",
                f"{area_con:.2f}" if area_con else "",
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
                r[35] or "", r[36] or "",
                # DSC_NOME_DO_IMOVEL = nombre(s) del comercio/hotel de la parcela (hoteles + comercios)
                " | ".join(x for x in (r[37], r[22]) if x),
                # DSC_LOGRADOURO_NO = número de la dirección
                r[3] or "",
                # Tipo de edificación unificado (parcela-level): uso=r[8], uf_viv=r[9],
                # área=r[13], descripción CNPJ=r[36], hotel_tipo=r[38]
                _tipo_edificacion(r[8], r[9], r[13], r[36], r[38]),
            ]

        for r in rows:
            direccion = " ".join(s for s in (r[2], r[3], r[4]) if s).strip()
            unidades = unidades_por_parcela.get(r[34])
            if unidades:
                # Edificio/lote con varias unidades: una fila por unidad (sin conteo).
                for u in unidades:
                    es_com = (u["uso"] or "").lower().startswith("comerc")
                    w.writerow(_fila(
                        r, direccion, f"Unidade {u['n']}", u["codigo"] or "",
                        (u["uso"] or "residencial"),
                        0 if es_com else 1, 1 if es_com else 0, 1,
                        u["area_m2"] or r[13]))
            else:
                w.writerow(_fila(r, direccion, "", "", r[8], r[9], r[10], r[11], r[13]))
        yield buf.getvalue()

    return StreamingResponse(
        _gen(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# Constantes del CSV de operadora (layout de base de logradouros, solo Brasil).
# COD_OPERADORA fijo según definición del cliente; BASE/COD_LOG_PARA/abreviados
# quedan en blanco por ahora; CEP_UNICO siempre 'N'.
CSV_OPERADORA_COD = "858"


@app.get("/api/surveys/{survey_id}/export/csv-operadora")
async def export_csv_operadora(survey_id: str) -> StreamingResponse:
    """CSV con el layout de base de logradouros de operadora (solo Brasil).

    Una fila por parcela con dirección. Descompone `calle` en tipo/título/
    preposição/nome oficial (heurística por diccionario — logradouro_br.py).
    CODIGO_LOGRADOURO sale de parcelas.codigo_logradouro (BCI, migración 016);
    vacío para parcelas parseadas antes de esa migración.
    """
    engine = get_engine()
    with engine.connect() as conn:
        meta = conn.execute(text("""
            SELECT s.region_id, r.name, s.started_at, r.country_code
            FROM surveys s JOIN regions r ON s.region_id = r.region_id
            WHERE s.survey_id = :sid
        """), {"sid": survey_id}).fetchone()
        if not meta:
            return JSONResponse({"error": "Survey no encontrado"}, status_code=404)
        if (meta[3] or "").upper() != "BRA":
            return JSONResponse(
                {"error": "El CSV de operadora es solo para relevamientos en Brasil"},
                status_code=400)

        # Si el survey tiene un baseline vinculado, exportamos con el MISMO formato del CSV
        # que se importó (mismas columnas/orden) pero con los datos del relevamiento nuevo.
        base = conn.execute(text("""
            SELECT b.mapeo, b.header_csv, b.baseline_id::text
            FROM baselines b WHERE b.baseline_id = (
                SELECT baseline_id FROM surveys WHERE survey_id = CAST(:sid AS uuid))
        """), {"sid": survey_id}).fetchone()

        plantilla = None
        if base:
            mapeo_d = json.loads(base[0]) if base[0] else {}
            header = json.loads(base[1]) if base[1] else None
            if not header:   # baseline viejo sin header guardado → reconstruir (orden best-effort)
                ex = conn.execute(text(
                    "SELECT extras FROM baseline_direcciones WHERE baseline_id = CAST(:bid AS uuid) "
                    "AND extras IS NOT NULL LIMIT 1"), {"bid": base[2]}).scalar()
                extras_keys = list(json.loads(ex).keys()) if ex else []
                mapped = [h for h in mapeo_d.values() if h]
                header = mapped + [k for k in extras_keys if k not in mapped]
            parc = conn.execute(text("""
                SELECT calle, numero, complemento, barrio, municipio, estado_provincia,
                       codigo_postal, COALESCE(uf_vivienda, 0), COALESCE(uf_comercio, 0)
                FROM parcelas
                WHERE survey_id = CAST(:sid AS uuid) AND calle IS NOT NULL
                ORDER BY calle,
                         NULLIF(regexp_replace(COALESCE(numero, ''), '\\D', '', 'g'), '')::bigint
                           NULLS LAST
            """), {"sid": survey_id}).fetchall()
            plantilla = (header, mapeo_d, parc)

        rows = None
        if not plantilla:
            # Fallback (survey sin baseline): layout de base de logradouros de operadora.
            # Una fila por DIRECCIÓN COMPLETA única (varias parcelas con la misma
            # calle+número+CEP+bairro colapsan en un solo registro).
            rows = conn.execute(text("""
                SELECT municipio, estado_provincia, barrio, calle,
                       codigo_postal, numero, MAX(codigo_logradouro) AS codigo_logradouro
                FROM parcelas
                WHERE survey_id = :sid AND calle IS NOT NULL
                GROUP BY municipio, estado_provincia, barrio, calle, codigo_postal, numero
                ORDER BY calle,
                         NULLIF(regexp_replace(COALESCE(numero, ''), '\\D', '', 'g'), '')::bigint
                           NULLS LAST
            """), {"sid": survey_id}).fetchall()

    fecha = meta[2].strftime("%Y-%m-%d") if meta[2] else ""
    filename = f"operadora_{meta[0]}_{fecha}.csv".replace(" ", "_")

    if plantilla:
        header, mapeo_d, parc = plantilla
        inv = {h: f for f, h in mapeo_d.items() if h}   # header → campo
        TIPO_VIV, TIPO_COM = "RESIDENCIAL", "COMERCIO EM GERAL"

        def _gen_plantilla():
            buf = io.StringIO()
            buf.write("﻿")   # BOM para Excel
            w = csv.writer(buf, delimiter=";")
            w.writerow(header)
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)
            for calle, numero, compl, barrio, muni, est, cep, uv, uc in parc:
                full = " ".join(x for x in (calle, numero) if x)
                if compl:
                    full = f"{full} {compl}".strip()
                # Una fila por UNIDAD: uf_vivienda → RESIDENCIAL, uf_comercio → COMERCIO.
                # Sin UF (vacante) → 1 fila para no perder la dirección.
                unidades = [TIPO_VIV] * int(uv) + [TIPO_COM] * int(uc)
                if not unidades:
                    unidades = [""]
                for tipo in unidades:
                    val = {"direccion": full, "calle": calle or "", "numero": numero or "",
                           "barrio": barrio or "", "ciudad": muni or "",
                           "estado": (est or "").upper(), "cep": cep or "", "uso": tipo}
                    w.writerow([val.get(inv.get(col), "") for col in header])
                yield buf.getvalue(); buf.seek(0); buf.truncate(0)

        return StreamingResponse(
            _gen_plantilla(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def _gen():
        buf = io.StringIO()
        buf.write("﻿")   # BOM para Excel
        w = csv.writer(buf, delimiter=";")
        w.writerow([
            "COD_OPERADORA", "NOME_LOCALIDADE", "UF", "BAIRRO", "BAIRRO_ABREVIADO",
            "NOME_TIPO_LOGR", "NOME_TITULO", "PREPOSICAO", "NOME_OFICIAL_LOGR",
            "NOME_LOGR_ABREV", "CEP", "NUMERO", "CEP_UNICO", "CODIGO_LOGRADOURO",
            "COD_LOG_PARA", "BASE",
        ])
        for municipio, uf, barrio, calle, cep, numero, cod_logr in rows:
            d = descomponer_logradouro(calle)
            w.writerow([
                CSV_OPERADORA_COD,
                (municipio or "").upper(),
                (uf or "").upper(),
                (barrio or "").upper(),
                "",                       # BAIRRO_ABREVIADO — en blanco por ahora
                d["tipo"],
                d["titulo"],
                d["preposicao"],
                d["nome"],
                "",                       # NOME_LOGR_ABREV — en blanco por ahora
                cep or "",
                numero or "",
                "N",                      # CEP_UNICO — siempre N
                cod_logr or "",
                "",                       # COD_LOG_PARA — en blanco
                "",                       # BASE — en blanco (sin valor definido)
            ])
        yield buf.getvalue()

    return StreamingResponse(
        _gen(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Relevamientos parciales (sub-zona de una región ya relevada) ───────────────

@app.post("/api/surveys/{survey_id}/parcial")
async def crear_parcial(survey_id: str,
                        geojson_file: UploadFile = File(...)) -> JSONResponse:
    """Crea un survey NUEVO sobre la misma región, acotado a una sub-zona
    (polígono propio en `surveys.subzona_geojson`, migración 018). Los fetchers
    prefieren la subzona al filtrar; los PDFs BCI compartidos se reutilizan.
    El survey grande queda intacto (los relevamientos no se pisan)."""
    row = _get_survey_row(survey_id)
    if not row:
        return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
    region_id = row[0]

    geojson_bytes = await geojson_file.read()
    try:
        geojson_str = geojson_bytes.decode("utf-8")
        geojson_data = json.loads(geojson_str)
        _bbox_from_geojson(geojson_data)        # valida que tenga coordenadas
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"GeoJSON inválido: {e}"}, status_code=400)

    # La sub-zona debe tocar la zona de la región (si la región tiene polígono).
    engine = get_engine()
    with engine.connect() as conn:
        zona_region = conn.execute(text(
            "SELECT zone_geojson FROM regions WHERE region_id = :rid"),
            {"rid": region_id}).scalar()
    if zona_region:
        try:
            from shapely.geometry import shape
            from shapely.ops import unary_union

            def _poly(gj: dict):
                if gj.get("type") == "FeatureCollection":
                    geoms = [shape(f["geometry"]) for f in gj.get("features", [])
                             if f.get("geometry")]
                elif gj.get("type") == "Feature":
                    geoms = [shape(gj["geometry"])]
                else:
                    geoms = [shape(gj)]
                return unary_union(geoms).buffer(0)

            if not _poly(geojson_data).intersects(_poly(json.loads(zona_region))):
                return JSONResponse({"ok": False, "error":
                                     "La sub-zona no se superpone con la zona de la región — "
                                     "revisá el polígono (¿es de otro lugar?)"}, status_code=400)
        except Exception as e:
            logger.warning(f"No se pudo validar la sub-zona contra la región: {e}")

    nuevo_sid = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO surveys (survey_id, region_id, status, subzona_geojson)
            VALUES (:sid, :rid, 'stopped', :gj)
        """), {"sid": nuevo_sid, "rid": region_id, "gj": geojson_str})
    logger.info(f"Relevamiento PARCIAL creado para {region_id}: survey {nuevo_sid} "
                f"(sub-zona de {len(geojson_str)} bytes)")
    return JSONResponse({"ok": True, "survey_id": nuevo_sid, "region_id": region_id})


@app.get("/api/regions/{region_id}/export/csv-consolidado")
async def export_csv_consolidado(region_id: str):
    """CSV CONSOLIDADO de la región: el dato más reciente de cada parcela entre
    TODOS los surveys (completos y parciales, incl. archivados). Los relevamientos
    nunca se pisan — esta vista los combina en lectura: identidad de parcela =
    cca_code (o clave de dirección, o el id) y gana la fila del survey más nuevo."""
    engine = get_engine()
    with engine.connect() as conn:
        meta = conn.execute(text(
            "SELECT name FROM regions WHERE region_id = :rid"), {"rid": region_id}).fetchone()
        if not meta:
            return JSONResponse({"error": "Región no encontrada"}, status_code=404)
        rows = conn.execute(text("""
            WITH filas AS (
                SELECT p.calle, p.numero, p.complemento, p.barrio, p.municipio,
                       p.codigo_postal, p.uso_principal,
                       CASE WHEN p.uf_vivienda IS NULL AND p.uf_comercio IS NULL
                            THEN COALESCE(p.unidades_funcionales_estimadas, 0)
                            ELSE COALESCE(p.uf_vivienda, 0) END AS uf_viv,
                       COALESCE(p.uf_comercio, 0) AS uf_com,
                       p.cca_code, p.nomenclatura_catastral,
                       p.area_m2_terreno, p.area_m2_construida,
                       p.fuente_parcela, p.centroid_lat, p.centroid_lng,
                       COALESCE(s.finished_at, s.started_at) AS s_fecha,
                       (s.subzona_geojson IS NOT NULL) AS es_parcial,
                       COALESCE(NULLIF(p.cca_code, ''),
                                NULLIF(LOWER(TRIM(COALESCE(p.calle, '') || '|' ||
                                             COALESCE(p.numero, ''))), '|'),
                                p.parcela_id::text) AS ident
                FROM parcelas p JOIN surveys s ON s.survey_id = p.survey_id
                WHERE p.region_id = :rid
            )
            SELECT DISTINCT ON (ident) *
            FROM filas ORDER BY ident, s_fecha DESC
        """), {"rid": region_id}).fetchall()

    if not rows:
        return JSONResponse({"error": "La región no tiene parcelas relevadas"},
                            status_code=404)

    def _gen():
        buf = io.StringIO()
        buf.write("﻿")   # BOM para Excel
        w = csv.writer(buf, delimiter=";")
        w.writerow([f"Consolidado de la región: {meta[0]}",
                    f"({len(rows)} parcelas — dato más reciente de cada una entre todos los relevamientos)"])
        w.writerow([])
        w.writerow(["Dirección", "Uso", "UF Vivienda", "UF Comercio",
                    "Bairro", "Municipio", "CEP", "Inscripción", "Nomenclatura",
                    "Área Terreno m²", "Área Construida m²", "Fuente",
                    "Lat", "Lng", "Relevado el", "De relevamiento parcial"])
        for r in sorted(rows, key=lambda x: ((x[0] or "~"), (x[1] or ""))):
            direccion = " ".join(s for s in (r[0], r[1], r[2]) if s).strip()
            w.writerow([
                direccion or "(sin dirección)",
                r[6] or "", int(r[7] or 0), int(r[8] or 0),
                r[3] or "", r[4] or "", r[5] or "",
                r[9] or "", r[10] or "",
                f"{r[11]:.2f}" if r[11] else "",
                f"{r[12]:.2f}" if r[12] else "",
                r[13] or "",
                f"{r[14]:.6f}" if r[14] else "",
                f"{r[15]:.6f}" if r[15] else "",
                r[16].strftime("%Y-%m-%d") if r[16] else "",
                "sí" if r[17] else "",
            ])
        yield buf.getvalue()

    filename = f"consolidado_{region_id}.csv"
    return StreamingResponse(
        _gen(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Baselines (relevamiento anterior del cliente) + Comparativa ────────────────
# El cliente sube su relevamiento anterior como CSV externo → queda como baseline
# de la región (tablas `baselines`/`baseline_direcciones`, migración 017) y se puede
# comparar por dirección contra cualquier survey actual. Entre dos surveys propios
# la comparación va por cca_code (exacta). Ver agents/comparativa_reporter.py.

_BASELINE_MAX_BYTES = 10 * 1024 * 1024     # 10 MB de CSV es muchísimo más que un relevamiento

# Contrato de columnas fijas del CSV del relevamiento anterior (operadora). Ya NO hay
# mapeo manual: el CSV debe traer estas columnas con su nombre exacto. campo_interno →
# nombre EXACTO de columna. El match es case/acento-insensible (_norm_header), pero el
# nombre tiene que estar. Ver _resolver_columnas / docs.
_BASELINE_COLUMNAS = {
    "direccion":       "DSC_ENDERECO_COMPLETO",   # obligatoria
    "ciudad":          "DSC_CIDADE",              # obligatoria
    "estado":          "COD_UF",                  # obligatoria (sigla/código/nombre de UF)
    "cep":             "NUM_CEP",                 # obligatoria
    "uso":             "DSC_TIPO_IMOVEL",         # obligatoria (tipo de inmueble → UF)
    "barrio":          "DSC_BAIRRO",              # opcional
    "status_contrato": "DSC_STATUS_CONTRATO",     # opcional
    "status_node":     "COD_NODE",                # opcional
}
_BASELINE_OBLIGATORIAS = ["DSC_ENDERECO_COMPLETO", "DSC_CIDADE", "COD_UF",
                          "NUM_CEP", "DSC_TIPO_IMOVEL"]
_BASELINE_OPCIONALES = ["DSC_BAIRRO", "DSC_STATUS_CONTRATO", "COD_NODE"]


def _norm_header(h: str) -> str:
    import unicodedata
    s = "".join(c for c in unicodedata.normalize("NFKD", str(h or ""))
                if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower().strip())


def _leer_csv_baseline(data: bytes, filename: str) -> tuple[list[str], list[list[str]], str]:
    """Parsea el CSV externo: devuelve (headers, filas, separador). Tolera BOM,
    latin-1, separador ';' o ',' y filas de título antes del header (como las que
    genera nuestro propio export)."""
    if re.search(r"\.xlsx?$", filename or "", re.IGNORECASE):
        raise ValueError(
            "Excel no soportado en el servidor: abrí el archivo y guardalo como CSV "
            "(Archivo → Guardar como → CSV) y volvé a subirlo.")
    if len(data) > _BASELINE_MAX_BYTES:
        raise ValueError(f"Archivo demasiado grande (máx. {_BASELINE_MAX_BYTES // 1024 // 1024} MB)")
    try:
        texto = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = data.decode("latin-1")

    primera = texto.splitlines()[0] if texto.splitlines() else ""
    sep = ";" if primera.count(";") >= primera.count(",") else ","
    filas = [f for f in csv.reader(io.StringIO(texto), delimiter=sep)]

    # Header = primera de las filas iniciales con el ANCHO MÁXIMO de celdas no
    # vacías (las filas de título/meta que algunos exports traen antes — incluido
    # el nuestro — son más angostas que la de encabezados).
    no_vacias = lambda f: [c for c in f if str(c).strip()]
    anchos = [len(no_vacias(f)) for f in filas[:20]]
    max_ancho = max(anchos, default=0)
    if max_ancho < 2:
        raise ValueError("No se encontró una fila de encabezados en el CSV "
                         "(se esperan al menos 2 columnas)")
    header_idx = anchos.index(max_ancho)
    headers = [str(c).strip() for c in filas[header_idx]]
    datos = [f for f in filas[header_idx + 1:] if no_vacias(f)]
    if not datos:
        raise ValueError("El CSV no tiene filas de datos después del encabezado")
    return headers, datos, sep


def _resolver_columnas(headers: list[str]) -> dict:
    """Resuelve el `mapeo_d` interno {campo: header_real} buscando las columnas de nombre
    fijo (`_BASELINE_COLUMNAS`) en los headers del CSV. Match case/acento-insensible.
    Lanza ValueError si falta alguna columna OBLIGATORIA (con el nombre exacto esperado)."""
    norm = {_norm_header(h): h for h in headers if str(h).strip()}
    mapeo: dict[str, str] = {}
    for campo, columna in _BASELINE_COLUMNAS.items():
        real = norm.get(_norm_header(columna))
        if real:
            mapeo[campo] = real
    faltan = [c for c in _BASELINE_OBLIGATORIAS if _norm_header(c) not in norm]
    if faltan:
        raise ValueError(
            "Al CSV le faltan columnas obligatorias: " + ", ".join(faltan) +
            ". El relevamiento anterior debe traer estas columnas (nombre exacto): " +
            ", ".join(_BASELINE_OBLIGATORIAS) + ".")
    return mapeo


def _validar_columnas_csv(headers: list[str], datos: list, sep: str) -> dict:
    """Payload de preview: valida que estén las columnas obligatorias (por nombre fijo).
    Devuelve detectadas/faltantes para mostrar en la UI. Ya no hay mapeo manual."""
    norm = {_norm_header(h) for h in headers if str(h).strip()}
    presentes = lambda cols: [c for c in cols if _norm_header(c) in norm]
    faltantes = [c for c in _BASELINE_OBLIGATORIAS if _norm_header(c) not in norm]
    return {
        "ok": not faltantes,
        "obligatorias": _BASELINE_OBLIGATORIAS,
        "opcionales": _BASELINE_OPCIONALES,
        "detectadas": presentes(_BASELINE_OBLIGATORIAS + _BASELINE_OPCIONALES),
        "faltantes": faltantes,
        "separador": sep,
        "total_filas": len(datos),
        "error": ("Faltan columnas obligatorias: " + ", ".join(faltantes)) if faltantes else None,
    }


@app.post("/api/surveys/{survey_id}/baselines/preview")
async def baseline_preview(survey_id: str, archivo: UploadFile = File(...)) -> JSONResponse:
    """Paso 1 del import: lee el CSV y valida que traiga las columnas obligatorias por
    nombre fijo (sin mapeo). No persiste nada (el archivo se re-sube en el paso 2)."""
    row = _get_survey_row(survey_id)
    if not row:
        return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
    data = await archivo.read()
    try:
        headers, datos, sep = _leer_csv_baseline(data, archivo.filename or "")
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse(_validar_columnas_csv(headers, datos, sep))


def _es_residencial(uso: str) -> bool:
    """El 'tipo de inmueble' del CSV anterior: residencial → vivienda; el resto
    (comercial, y cualquier otro caso) → comercio."""
    return "resid" in (uso or "").lower()


def _agregar_por_direccion(registros: list[dict], baseline_id: str) -> list[dict]:
    """Colapsa las filas (una por unidad/entrada) en UNA por dirección, derivando la
    UF del 'tipo de inmueble': cada entrada residencial cuenta 1 vivienda; cada
    entrada del resto, 1 comercio. Si la fila trae UF explícita, se suma esa en su lugar."""
    grupos: dict = {}
    orden: list = []
    for r in registros:
        key = (r["calle_norm"], r["numero_norm"] or "",
               (r.get("barrio") or "").lower().strip(), (r["ciudad"] or "").lower())
        g = grupos.get(key)
        if not g:
            g = {**r, "id": str(uuid.uuid4()), "bid": baseline_id, "uf_v": 0, "uf_c": 0, "_n": 0}
            grupos[key] = g
            orden.append(key)
        g["_n"] += 1
        if r["uf_v"] is not None or r["uf_c"] is not None:   # UF explícita en la fila
            g["uf_v"] += r["uf_v"] or 0
            g["uf_c"] += r["uf_c"] or 0
        elif _es_residencial(r["uso"]):                      # entrada residencial → 1 vivienda
            g["uf_v"] += 1
        else:                                                # resto → 1 comercio
            g["uf_c"] += 1
    out = []
    for key in orden:
        g = grupos[key]
        g["uso"] = ("mixto" if g["uf_v"] > 0 and g["uf_c"] > 0
                    else "comercial" if g["uf_c"] > 0 else "residencial")
        g.pop("_n", None)
        out.append(g)
    return out


def _construir_registros_baseline(headers: list, datos: list, mapeo_d: dict,
                                  baseline_id: str, agregar: bool = False) -> tuple[list[dict], int]:
    """Normaliza cada fila del CSV a una fila de `baseline_direcciones`.
    Devuelve (registros, filas_sin_direccion). Lanza ValueError si faltan columnas.
    Con `agregar=True` colapsa por dirección y deriva la UF del tipo de inmueble."""
    from scrapitero.agents.direccion_norm import (normalizar_calle, normalizar_numero,
                                                  separar_numero)
    from scrapitero.agents.geocode_forward import sigla_uf
    idx = {h: i for i, h in enumerate(headers)}
    faltantes = [h for h in mapeo_d.values() if h and h not in idx]
    if faltantes:
        raise ValueError(f"Columnas del mapeo que no están en el CSV: {faltantes}")

    def celda(fila: list, campo: str) -> str:
        h = mapeo_d.get(campo)
        if not h:
            return ""
        i = idx[h]
        return str(fila[i]).strip() if i < len(fila) else ""

    def entero(s: str):
        m = re.search(r"-?\d+", s.replace(".", "").replace(",", "."))
        return int(m.group(0)) if m else None

    def cep_norm(s: str):
        d = re.sub(r"\D", "", s or "")
        return f"{d[:5]}-{d[5:8]}" if len(d) == 8 else (d or None)

    mapeadas = {h for h in mapeo_d.values() if h}
    registros = []
    sin_direccion = 0
    for fila in datos:
        if mapeo_d.get("calle"):
            calle, numero = celda(fila, "calle"), celda(fila, "numero")
            raw = " ".join(x for x in (calle, numero) if x)
        else:
            raw = celda(fila, "direccion")
            calle, numero = separar_numero(raw)
            if mapeo_d.get("numero") and celda(fila, "numero"):
                numero = celda(fila, "numero")
        calle_norm = normalizar_calle(calle)
        if not calle_norm:
            sin_direccion += 1
            continue
        extras = {h: str(fila[i]).strip() for h, i in idx.items()
                  if h and h not in mapeadas and i < len(fila) and str(fila[i]).strip()}
        # Status del relevamiento anterior (mapeados, no son dirección): se guardan en
        # extras con clave canónica para mostrarlos en el popup del punto viejo.
        for _stf in ("status_contrato", "status_node"):
            _v = celda(fila, _stf)
            if _v:
                extras[_stf] = _v
        registros.append({
            "id": str(uuid.uuid4()), "bid": baseline_id,
            "raw": raw, "calle": calle, "numero": numero[:30] or None,
            "calle_norm": calle_norm,
            "numero_norm": normalizar_numero(numero),
            "uso": (celda(fila, "uso").lower()[:30] or None),
            "barrio": (celda(fila, "barrio")[:200] or None),
            "ciudad": (celda(fila, "ciudad")[:200] or None),
            "estado": (sigla_uf(celda(fila, "estado")) or None),  # COD_UF/nombre → sigla (51/"Mato Grosso"→MT)
            "cep": cep_norm(celda(fila, "cep")),
            "uf_v": entero(celda(fila, "uf_vivienda")),
            "uf_c": entero(celda(fila, "uf_comercio")),
            "extras": json.dumps(extras, ensure_ascii=False)[:4000] if extras else None,
        })
    if agregar and registros:
        registros = _agregar_por_direccion(registros, baseline_id)
    return registros, sin_direccion


def _aplicar_ciudad_baseline(registros: list[dict], ciudad_form: str = "") -> str:
    """Garantiza que CADA fila del baseline tenga ciudad antes de geocodificar.

    Sin ciudad, el geocoder recibe solo la calle ("R MAL RONDON") y puede ubicarla en
    cualquier estado del país (geocodes en otra ciudad/UF). La ciudad efectiva es la
    **ingresada a mano** (form) o, si no, la **predominante** de la columna mapeada; con
    ella se rellenan las filas que vinieron sin ciudad. Si no hay ninguna, lanza ValueError
    para que el wizard la exija (no se importa un baseline sin ciudad).
    """
    from collections import Counter
    pred = Counter((r.get("ciudad") or "").strip() for r in registros
                   if (r.get("ciudad") or "").strip())
    ciudad = (ciudad_form or "").strip() or (pred.most_common(1)[0][0] if pred else "")
    if not ciudad:
        raise ValueError(
            "El relevamiento anterior no trae ciudad: mapeá la columna Ciudad del CSV o "
            "escribí la ciudad del relevamiento. Sin ciudad el geocoding ubica las "
            "direcciones en cualquier parte del país.")
    ciudad = ciudad[:200]
    for r in registros:
        if not (r.get("ciudad") or "").strip():
            r["ciudad"] = ciudad
    return ciudad


def _persistir_baseline(conn, region_id: str, nombre: str, fecha_val, archivo_nombre: str,
                        mapeo_d: dict, registros: list[dict], ciudad: str = "",
                        header_csv: Optional[list] = None) -> str:
    """Inserta `baselines` + `baseline_direcciones` en la conexión dada. Devuelve baseline_id.
    `header_csv` = encabezado crudo del CSV (para re-exportar con el mismo formato)."""
    baseline_id = registros[0]["bid"]
    conn.execute(text("""
        INSERT INTO baselines (baseline_id, region_id, nombre, fecha_relevamiento,
                               archivo_nombre, mapeo, n_registros, ciudad, header_csv)
        VALUES (:bid, :rid, :nombre, :fecha, :archivo, :mapeo, :n, :ciudad, :header)
    """), {"bid": baseline_id, "rid": region_id, "nombre": nombre.strip(),
           "fecha": fecha_val, "archivo": (archivo_nombre or "")[:255],
           "mapeo": json.dumps(mapeo_d, ensure_ascii=False), "n": len(registros),
           "ciudad": (ciudad or "").strip()[:200] or None,
           "header": json.dumps(header_csv, ensure_ascii=False) if header_csv else None})
    conn.execute(text("""
        INSERT INTO baseline_direcciones
               (id, baseline_id, direccion_raw, calle, numero, calle_norm,
                numero_norm, uso, barrio, ciudad, estado, cep, uf_vivienda, uf_comercio, extras)
        VALUES (:id, :bid, :raw, :calle, :numero, :calle_norm, :numero_norm,
                :uso, :barrio, :ciudad, :estado, :cep, :uf_v, :uf_c, :extras)
    """), registros)
    return baseline_id


def _status_de_extras(extras_json) -> dict:
    """Extrae los status del relevamiento anterior guardados en `extras` (JSON)."""
    try:
        e = json.loads(extras_json) if extras_json else {}
    except (ValueError, TypeError):
        e = {}
    return {"status_contrato": e.get("status_contrato") or None,
            "status_node": e.get("status_node") or None}


def _parse_fecha(fecha: str):
    """'YYYY-MM-DD' → date, o None si vacío. Lanza ValueError si tiene formato inválido."""
    if not fecha.strip():
        return None
    try:
        return datetime.strptime(fecha.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Fecha inválida: {fecha!r} (formato YYYY-MM-DD)")


@app.post("/api/surveys/{survey_id}/baselines")
async def baseline_import(
    survey_id: str,
    archivo: UploadFile = File(...),
    nombre: str = Form(...),
    fecha: str = Form(""),               # fecha del relevamiento ORIGINAL (YYYY-MM-DD)
) -> JSONResponse:
    """Paso 2 del import: persiste el baseline con una fila normalizada por dirección.
    Las columnas se resuelven por nombre fijo (sin mapeo manual)."""
    row = _get_survey_row(survey_id)
    if not row:
        return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
    region_id = row[0]

    fecha_val = _parse_fecha(fecha)
    data = await archivo.read()
    try:
        headers, datos, _sep = _leer_csv_baseline(data, archivo.filename or "")
        mapeo_d = _resolver_columnas(headers)   # ValueError si falta alguna obligatoria
        registros, sin_direccion = _construir_registros_baseline(
            headers, datos, mapeo_d, str(uuid.uuid4()))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    if not registros:
        return JSONResponse({"ok": False, "error":
                             "Ninguna fila tiene una dirección legible en DSC_ENDERECO_COMPLETO."},
                            status_code=400)

    try:
        ciudad_efectiva = _aplicar_ciudad_baseline(registros, "")
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    engine = get_engine()
    with engine.begin() as conn:
        baseline_id = _persistir_baseline(conn, region_id, nombre, fecha_val,
                                          archivo.filename or "", mapeo_d, registros,
                                          ciudad_efectiva, header_csv=headers)

    logger.info(f"Baseline «{nombre}» importado para {region_id}: "
                f"{len(registros)} direcciones ({sin_direccion} filas sin dirección descartadas)")
    return JSONResponse({"ok": True, "baseline_id": baseline_id,
                         "importadas": len(registros), "sin_direccion": sin_direccion})


@app.delete("/api/baselines/{baseline_id}")
async def eliminar_baseline(baseline_id: str) -> JSONResponse:
    """Elimina un baseline importado (re-importable desde el CSV; no es un survey).
    Solo operador (middleware)."""
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(text(
            "DELETE FROM baselines WHERE baseline_id = :bid RETURNING nombre"),
            {"bid": baseline_id}).fetchone()
    if not row:
        return JSONResponse({"ok": False, "error": "Baseline no encontrado"}, status_code=404)
    logger.info(f"Baseline «{row[0]}» eliminado")
    return JSONResponse({"ok": True})


# ── Actualización: crear un relevamiento NUEVO sobre el CSV anterior geocodificado ──
# El operador marca "actualización", sube el CSV del relevamiento anterior (sin
# coordenadas), lo geocodificamos para graficarlo en el mapa, y dibuja encima el
# polígono de la nueva zona. El CSV queda como baseline auto-vinculado a la región.

# Estado en memoria de los jobs de geocoding (el web corre en un único proceso).
_geocoding_jobs: dict[str, dict] = {}


@app.post("/api/actualizaciones/preview")
async def actualizacion_preview(archivo: UploadFile = File(...)) -> JSONResponse:
    """Paso 1 (sin survey): lee el CSV anterior y valida las columnas obligatorias por
    nombre fijo (sin mapeo manual)."""
    data = await archivo.read()
    try:
        headers, datos, sep = _leer_csv_baseline(data, archivo.filename or "")
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse(_validar_columnas_csv(headers, datos, sep))


def _detectar_pais_baseline(registros: list[dict], ciudad: str = "",
                            muestra: int = 5) -> Optional[str]:
    """ISO-3 del país de las direcciones del baseline: geocodifica unas pocas SIN
    sesgo de país y deduce el país del primer punto. None si ninguna ubica.
    Se usa cuando el operador deja el país en AUTO (el geocoder necesita el país
    correcto para sesgar la búsqueda de todo el lote). La ciudad ayuda a ubicar bien."""
    import httpx
    from scrapitero.agents import geo
    from scrapitero.agents.baseline_geocoder import _HEADERS, _google, _nominatim, _query
    with httpx.Client(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
        for reg in registros[:muestra]:
            q = _query(reg.get("calle") or "", reg.get("numero") or "",
                       reg.get("barrio") or None, (reg.get("ciudad") or ciudad) or None)
            if not q:
                continue
            hit = _nominatim(q, None, client) or _google(q, None, client)
            if hit:
                iso3 = geo.detect_country(hit[0], hit[1])
                if iso3:
                    return iso3
    return None


def _lanzar_geocoding(baseline_id: str) -> None:
    """Corre BaselineGeocoder en un thread; deja el estado en `_geocoding_jobs`."""
    from scrapitero.agents.baseline_geocoder import BaselineGeocoderInput
    from scrapitero.agents.baseline_geocoder import run as run_geo

    _geocoding_jobs[baseline_id] = {"estado": "running", "error": None}

    def _job() -> None:
        # Taggear los logs del geocoder con el baseline_id → el wizard los muestra
        # con fecha/hora (paso 0 geocodebr, progreso cada 50, interpolación, final).
        _thread_job_id.value = baseline_id
        try:
            out = run_geo(BaselineGeocoderInput(baseline_id=baseline_id))
            if out.ok:
                _geocoding_jobs[baseline_id] = {"estado": "done", "error": None,
                                                "reusadas": out.reusadas}
            else:
                _geocoding_jobs[baseline_id] = {"estado": "error", "error": out.error}
        except Exception as e:  # noqa: BLE001
            logger.error(f"Geocoding baseline {baseline_id} falló: {e!r}")
            _geocoding_jobs[baseline_id] = {"estado": "error", "error": str(e)}

    threading.Thread(target=_job, daemon=True).start()


@app.post("/api/actualizaciones/preparar")
async def actualizacion_preparar(
    nombre: str = Form(...),
    country_code: str = Form("AUTO"),
    archivo: UploadFile = File(...),
) -> JSONResponse:
    """Paso 2: crea la región + persiste el baseline y lanza el geocoding en background.
    El polígono de la nueva zona se dibuja después (crear-survey). Las columnas se
    resuelven por nombre fijo (sin mapeo); la ciudad sale de la columna DSC_CIDADE."""
    fecha_val = None      # la fecha del relevamiento anterior no se usa

    data = await archivo.read()
    try:
        headers, datos, _sep = _leer_csv_baseline(data, archivo.filename or "")
        mapeo_d = _resolver_columnas(headers)   # ValueError si falta alguna obligatoria
        # Agregamos por dirección: la UF se deriva del tipo de inmueble (residencial
        # → vivienda; resto → comercio), contando las entradas de cada dirección.
        registros, sin_direccion = _construir_registros_baseline(
            headers, datos, mapeo_d, str(uuid.uuid4()), agregar=True)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    if not registros:
        return JSONResponse({"ok": False, "error":
                             "Ninguna fila tiene una dirección legible en DSC_ENDERECO_COMPLETO."},
                            status_code=400)

    # Ciudad efectiva (de DSC_CIDADE): rellena las filas sin ciudad con la predominante y
    # ancla la detección de país y el geocoding. Si no hay ninguna, se rechaza el import.
    try:
        ciudad = _aplicar_ciudad_baseline(registros, "")
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    cc = (country_code or "AUTO").upper()
    if cc in ("BRA", "ARG"):
        region_cc = cc
    else:   # AUTO: detectar el país de las direcciones (el geocoder lo necesita para sesgar)
        region_cc = await asyncio.to_thread(_detectar_pais_baseline, registros, ciudad)
        if not region_cc:
            return JSONResponse({"ok": False, "error": (
                "No se pudo autodetectar el país de las direcciones (el geocoding de "
                "muestra falló). Reintentá o elegí el país manualmente.")}, status_code=422)
    region_id = f"zona-{_slugify(nombre)}"

    engine = get_engine()
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO regions (region_id, name, country_code)
                VALUES (:rid, :name, :cc)
                ON CONFLICT (region_id) DO UPDATE SET name = EXCLUDED.name
            """), {"rid": region_id, "name": nombre, "cc": region_cc})
            baseline_id = _persistir_baseline(conn, region_id, nombre, fecha_val,
                                              archivo.filename or "", mapeo_d, registros,
                                              ciudad, header_csv=headers)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    _lanzar_geocoding(baseline_id)
    logger.info(f"Actualización «{nombre}»: región {region_id} + baseline {baseline_id} "
                f"({len(registros)} direcciones, geocoding lanzado)")
    return JSONResponse({"ok": True, "region_id": region_id, "baseline_id": baseline_id,
                         "total": len(registros), "sin_direccion": sin_direccion})


@app.get("/api/baselines/{baseline_id}/activity")
async def baseline_activity(baseline_id: str, since: int = Query(0)) -> dict:
    """Log en vivo del geocoding de este baseline (con fecha/hora). 'since' = último id visto."""
    return _job_activity(baseline_id, since)


@app.get("/api/baselines/{baseline_id}/geocoding")
async def actualizacion_geocoding_status(baseline_id: str) -> JSONResponse:
    """Progreso del geocoding. Devuelve los puntos cuando terminó (`listo`)."""
    engine = get_engine()
    with engine.connect() as conn:
        meta = conn.execute(text("""
            SELECT nombre, n_registros, geocoded_at FROM baselines WHERE baseline_id = :bid
        """), {"bid": baseline_id}).fetchone()
        if not meta:
            return JSONResponse({"ok": False, "error": "Baseline no encontrado"}, status_code=404)
        geocodificadas = conn.execute(text("""
            SELECT COUNT(*) FROM baseline_direcciones
            WHERE baseline_id = :bid AND lat IS NOT NULL
        """), {"bid": baseline_id}).scalar() or 0

    job = _geocoding_jobs.get(baseline_id, {})
    estado = job.get("estado")
    db_listo = meta[2] is not None
    listo = db_listo or estado == "done"
    total = int(meta[1] or 0)

    puntos = []
    if listo:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT lat, lng, calle, numero, uso, uf_vivienda, uf_comercio, direccion_raw, extras
                FROM baseline_direcciones
                WHERE baseline_id = :bid AND lat IS NOT NULL
            """), {"bid": baseline_id}).fetchall()
        puntos = [{
            "lat": float(r[0]), "lng": float(r[1]),
            "direccion": (r[7] or " ".join(x for x in (r[2] or "", r[3] or "") if x)).strip(),
            "uso": r[4], "uf_v": int(r[5] or 0), "uf_c": int(r[6] or 0),
            **_status_de_extras(r[8]),
        } for r in rows]

    return JSONResponse({
        "ok": True, "nombre": meta[0], "total": total,
        "geocodificadas": int(geocodificadas),
        "fallidas": (total - int(geocodificadas)) if listo else 0,
        "reusadas": int(job.get("reusadas") or 0),
        "listo": listo,
        "error": job.get("error") if estado == "error" else None,
    } | ({"puntos": puntos} if listo else {}))


@app.get("/api/baselines/{baseline_id}/puntos")
async def baseline_puntos(baseline_id: str) -> JSONResponse:
    """Puntos geocodificados del relevamiento anterior (para graficarlo en gris en
    el mapa, con su UF). Reusado por el wizard de actualización y el mapa del survey."""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT lat, lng, calle, numero, uso, uf_vivienda, uf_comercio, direccion_raw, extras,
                   geocode_source
            FROM baseline_direcciones
            WHERE baseline_id = :bid AND lat IS NOT NULL
        """), {"bid": baseline_id}).fetchall()
    puntos = [{
        "lat": float(r[0]), "lng": float(r[1]),
        # Dirección COMPLETA tal cual vino del CSV (cae a calle+numero si no hay raw).
        "direccion": (r[7] or " ".join(x for x in (r[2] or "", r[3] or "") if x)).strip(),
        "uso": r[4], "uf_v": int(r[5] or 0), "uf_c": int(r[6] or 0),
        "uf_total": int((r[5] or 0) + (r[6] or 0)),
        # 'ciudad' = no se pudo ubicar en la calle → centro de la ciudad (aproximado).
        "aprox_ciudad": (r[9] == "ciudad"),
        **_status_de_extras(r[8]),
    } for r in rows]
    return JSONResponse({"ok": True, "puntos": puntos, "total": len(puntos)})


@app.post("/api/baselines/{baseline_id}/crear-survey")
async def actualizacion_crear_survey(
    baseline_id: str,
    geojson_file: UploadFile = File(...),
) -> JSONResponse:
    """Paso final: el polígono dibujado sobre el relevamiento anterior define la
    nueva zona. Setea `regions.zone_geojson` + bbox y crea el survey (stopped)."""
    engine = get_engine()
    with engine.connect() as conn:
        base = conn.execute(text(
            "SELECT region_id, nombre FROM baselines WHERE baseline_id = :bid"),
            {"bid": baseline_id}).fetchone()
    if not base:
        return JSONResponse({"ok": False, "error": "Baseline no encontrado"}, status_code=404)
    region_id, nombre = base[0], base[1]

    geojson_bytes = await geojson_file.read()
    try:
        geojson_str = geojson_bytes.decode("utf-8")
        geojson_data = json.loads(geojson_str)
        bbox = _bbox_from_geojson(geojson_data)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"GeoJSON inválido: {e}"}, status_code=400)

    # Sanity: el polígono debe cubrir al menos un punto del relevamiento anterior.
    with engine.connect() as conn:
        dentro = conn.execute(text("""
            SELECT COUNT(*) FROM baseline_direcciones
            WHERE baseline_id = :bid AND lat IS NOT NULL
              AND lat BETWEEN :s AND :n AND lng BETWEEN :w AND :e
        """), {"bid": baseline_id, "s": bbox["south"], "n": bbox["north"],
               "w": bbox["west"], "e": bbox["east"]}).scalar() or 0
    if dentro == 0:
        return JSONResponse({"ok": False, "error":
                             "El polígono no cubre ninguna dirección del relevamiento anterior — "
                             "dibujalo sobre los puntos del mapa."}, status_code=400)

    # País/municipio del centroide del polígono (si no se fijaron al preparar).
    cc_lat = (bbox["south"] + bbox["north"]) / 2.0
    cc_lng = (bbox["west"] + bbox["east"]) / 2.0
    from scrapitero.agents import geo
    municipio = None
    with engine.connect() as conn:
        cc_actual = conn.execute(text(
            "SELECT country_code FROM regions WHERE region_id = :rid"),
            {"rid": region_id}).scalar()
    country_code = cc_actual or await asyncio.to_thread(geo.detect_country, cc_lat, cc_lng)
    if country_code == "BRA":
        municipio = await asyncio.to_thread(geo.detect_municipio_br, cc_lat, cc_lng)

    survey_id = str(uuid.uuid4())
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE regions
                SET zone_geojson = :gj, bbox_wkt = :bbox,
                    country_code = COALESCE(country_code, :cc),
                    municipio_codigo = COALESCE(municipio_codigo, :mun)
                WHERE region_id = :rid
            """), {"gj": geojson_str, "bbox": _bbox_to_wkt(bbox), "cc": country_code,
                   "mun": municipio, "rid": region_id})
            conn.execute(text("""
                INSERT INTO surveys (survey_id, region_id, status, baseline_id)
                VALUES (:sid, :rid, 'stopped', :bid)
            """), {"sid": survey_id, "rid": region_id, "bid": baseline_id})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    logger.info(f"Actualización «{nombre}»: survey {survey_id} creado sobre {region_id} "
                f"(zona dibujada cubre {dentro} direcciones del anterior)")
    return JSONResponse({"ok": True, "survey_id": survey_id, "region_id": region_id})


# ── Actualización por CALLE + RANGO DE ALTURAS (autodetectado) ──────────────────

def _detectar_calles_baseline(baseline_id: str, extender: bool = True) -> list[dict]:
    """Autodetecta las calles + rango de numeración del relevamiento anterior.
    Agrupa `baseline_direcciones` por calle normalizada y saca min/max del número.
    `extender`=True amplía el tope para captar obra nueva en la misma cuadra."""
    from scrapitero.agents.direccion_norm import normalizar_calle, normalizar_numero
    engine = get_engine()
    with engine.connect() as conn:
        filas = conn.execute(text("""
            SELECT calle, numero FROM baseline_direcciones
            WHERE baseline_id = :bid AND calle IS NOT NULL AND calle <> ''
        """), {"bid": baseline_id}).fetchall()
    grupos: dict[str, dict] = {}
    for calle, numero in filas:
        cn = normalizar_calle(calle)
        if not cn:
            continue
        g = grupos.setdefault(cn, {"calle": calle, "calle_norm": cn, "n": 0,
                                   "num_min": None, "num_max": None})
        g["n"] += 1
        m = re.search(r"\d+", normalizar_numero(numero) or "")
        if m:
            v = int(m.group(0))
            g["num_min"] = v if g["num_min"] is None else min(g["num_min"], v)
            g["num_max"] = v if g["num_max"] is None else max(g["num_max"], v)
    out = []
    for g in grupos.values():
        if g["num_min"] is None:
            g["num_min"], g["num_max"] = 0, 0
        if extender and g["num_max"]:
            # tope ampliado: +20% (mín. +50), redondeado a 10
            top = max(int(g["num_max"] * 1.2), g["num_max"] + 50)
            g["num_max"] = int(round(top / 10.0) * 10)
        out.append(g)
    out.sort(key=lambda g: g["n"], reverse=True)
    return out


def _buffer_grados(geoms: list, metros: float, lat: float):
    """Une y buffea geometrías (lat/lng) por ~`metros`, en grados (corrige por latitud).
    Devuelve una geometría shapely. El polígono es de DESCARGA (generoso) → el filtro
    estricto por dirección hace la precisión, así que la aproximación en grados alcanza."""
    import math
    from shapely.ops import unary_union
    deg = metros / 111000.0
    u = unary_union(geoms)
    # buffer isotrópico en grados; la distorsión lng a lat ~-15° es chica (cos≈0.96)
    return u.buffer(deg / max(math.cos(math.radians(lat)), 0.3)).buffer(0)


def _poligono_de_calles(calles: list[dict], puntos: list[tuple], buffer_m: float = 55.0) -> Optional[dict]:
    """Construye el polígono de DESCARGA: geometría OSM de cada calle (recortada al bbox de
    los puntos del baseline + margen) buffereada, + buffer de los puntos como respaldo.
    Devuelve un GeoJSON (Polygon/MultiPolygon) o None si no hay nada."""
    from shapely.geometry import LineString, Point, mapping
    from scrapitero.agents.baseline_interp import _core_calle
    from scrapitero.agents.osm_building_fetcher import _fetch_overpass

    if not puntos:
        return None
    lats = [p[0] for p in puntos]; lngs = [p[1] for p in puntos]
    s, n, w, e = min(lats), max(lats), min(lngs), max(lngs)
    mlat = (s + n) / 2.0
    mrg = 0.012  # ~1.3 km de margen para captar el extremo de las calles (obra nueva)
    bbox = (s - mrg, w - mrg, n + mrg, e + mrg)   # Overpass: (south,west,north,east)

    import difflib
    geoms = [Point(ln, la) for la, ln in puntos]   # respaldo: los puntos del anterior
    cores = [cc for cc in (_core_calle(c.get("calle") or c.get("calle_norm") or "")
                           for c in calles) if cc]

    def _matchea(nm: str) -> bool:
        return any(difflib.SequenceMatcher(None, nm, core).ratio() >= 0.82
                   or core in nm or nm in core for core in cores)

    # UNA sola consulta: todas las vías con nombre del bbox; matcheo local por núcleo.
    q = (f'[out:json][timeout:90];way[highway][name]'
         f'({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});out geom;')
    try:
        data = _fetch_overpass(q)
    except Exception as exc:  # noqa: BLE001 — best-effort: si OSM falla, polígono = buffer de puntos
        logger.warning(f"scope-calles: Overpass falló ({exc}); polígono solo con puntos del baseline")
        data = {"elements": []}
    for el in (data.get("elements") or []):
        if el.get("type") != "way" or not el.get("geometry"):
            continue
        nm = _core_calle(el.get("tags", {}).get("name", ""))
        if not nm or not _matchea(nm):
            continue
        pts = [(g["lon"], g["lat"]) for g in el["geometry"] if "lat" in g and "lon" in g]
        if len(pts) >= 2:
            geoms.append(LineString(pts))
    if not geoms:
        return None
    poly = _buffer_grados(geoms, buffer_m, mlat)
    return mapping(poly)


@app.get("/api/baselines/{baseline_id}/calles")
async def baseline_calles(baseline_id: str) -> JSONResponse:
    """Calles + rango de alturas autodetectados del relevamiento anterior (tabla editable)."""
    engine = get_engine()
    with engine.connect() as conn:
        ok = conn.execute(text("SELECT 1 FROM baselines WHERE baseline_id = :b"),
                          {"b": baseline_id}).scalar()
    if not ok:
        return JSONResponse({"ok": False, "error": "Baseline no encontrado"}, status_code=404)
    calles = await asyncio.to_thread(_detectar_calles_baseline, baseline_id, True)
    return JSONResponse({"ok": True, "calles": calles, "total": len(calles)})


@app.post("/api/baselines/{baseline_id}/crear-survey-calles")
async def actualizacion_crear_survey_calles(
    baseline_id: str,
    calles: str = Form(...),     # JSON: lista editada [{calle, calle_norm, num_min, num_max}]
) -> JSONResponse:
    """Crea el survey en modo CALLE+RANGO: construye el polígono de descarga (geometría OSM
    de las calles + puntos del baseline) → zone_geojson, y guarda `scope_calles` (filtro
    estricto que aplica ScopeCallesFilter tras el BCI)."""
    from scrapitero.agents.direccion_norm import normalizar_calle
    try:
        lista = json.loads(calles)
        assert isinstance(lista, list) and lista
    except Exception:
        return JSONResponse({"ok": False, "error": "Lista de calles inválida"}, status_code=400)
    # normalizar/validar cada entrada
    scope = []
    for c in lista:
        cl = (c.get("calle") or "").strip()
        cn = (c.get("calle_norm") or normalizar_calle(cl)).strip()
        if not cn:
            continue
        try:
            mn = int(c.get("num_min") or 0); mx = int(c.get("num_max") or 0)
        except (TypeError, ValueError):
            mn, mx = 0, 0
        if mx and mx < mn:
            mn, mx = mx, mn
        scope.append({"calle": cl or cn, "calle_norm": cn, "num_min": mn, "num_max": mx})
    if not scope:
        return JSONResponse({"ok": False, "error": "Ninguna calle válida"}, status_code=400)

    engine = get_engine()
    with engine.connect() as conn:
        base = conn.execute(text(
            "SELECT region_id, nombre FROM baselines WHERE baseline_id = :bid"),
            {"bid": baseline_id}).fetchone()
        if not base:
            return JSONResponse({"ok": False, "error": "Baseline no encontrado"}, status_code=404)
        region_id, nombre = base[0], base[1]
        puntos = [(float(r[0]), float(r[1])) for r in conn.execute(text("""
            SELECT lat, lng FROM baseline_direcciones
            WHERE baseline_id = :bid AND lat IS NOT NULL
        """), {"bid": baseline_id}).fetchall()]
    if not puntos:
        return JSONResponse({"ok": False, "error":
                             "El relevamiento anterior no tiene direcciones geocodificadas"},
                            status_code=400)

    geojson = await asyncio.to_thread(_poligono_de_calles, scope, puntos)
    if not geojson:
        return JSONResponse({"ok": False, "error":
                             "No se pudo construir la zona de las calles (OSM no respondió y "
                             "no hay puntos suficientes)"}, status_code=422)
    geojson_str = json.dumps({"type": "Feature", "geometry": geojson, "properties": {}})
    try:
        bbox = _bbox_from_geojson(geojson)
    except Exception as ex:
        return JSONResponse({"ok": False, "error": f"polígono inválido: {ex}"}, status_code=500)

    cc_lat = (bbox["south"] + bbox["north"]) / 2.0
    cc_lng = (bbox["west"] + bbox["east"]) / 2.0
    from scrapitero.agents import geo
    with engine.connect() as conn:
        cc_actual = conn.execute(text(
            "SELECT country_code FROM regions WHERE region_id = :rid"), {"rid": region_id}).scalar()
    country_code = cc_actual or await asyncio.to_thread(geo.detect_country, cc_lat, cc_lng)
    municipio = await asyncio.to_thread(geo.detect_municipio_br, cc_lat, cc_lng) \
        if country_code == "BRA" else None

    survey_id = str(uuid.uuid4())
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE regions SET zone_geojson = :gj, bbox_wkt = :bbox,
                    country_code = COALESCE(country_code, :cc),
                    municipio_codigo = COALESCE(municipio_codigo, :mun)
                WHERE region_id = :rid
            """), {"gj": geojson_str, "bbox": _bbox_to_wkt(bbox), "cc": country_code,
                   "mun": municipio, "rid": region_id})
            conn.execute(text("""
                INSERT INTO surveys (survey_id, region_id, status, baseline_id, scope_calles)
                VALUES (:sid, :rid, 'stopped', :bid, CAST(:scope AS jsonb))
            """), {"sid": survey_id, "rid": region_id, "bid": baseline_id,
                   "scope": json.dumps(scope)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    logger.info(f"Actualización «{nombre}» (modo calle+rango): survey {survey_id} creado sobre "
                f"{region_id} con {len(scope)} calles")
    return JSONResponse({"ok": True, "survey_id": survey_id, "region_id": region_id,
                         "calles": len(scope)})


@app.get("/api/surveys/{survey_id}/comparativa/opciones")
async def comparativa_opciones(survey_id: str) -> JSONResponse:
    """Términos disponibles para 'Comparar con…': surveys anteriores de la misma
    región (incl. archivados, con parcelas) + baselines importados."""
    row = _get_survey_row(survey_id)
    if not row:
        return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
    region_id = row[0]
    engine = get_engine()
    with engine.connect() as conn:
        surveys = conn.execute(text("""
            SELECT s.survey_id::text, COALESCE(s.finished_at, s.started_at) AS fecha,
                   s.status, s.archivado, COUNT(p.parcela_id) AS n
            FROM surveys s LEFT JOIN parcelas p ON p.survey_id = s.survey_id
            WHERE s.region_id = :rid AND s.survey_id::text <> :sid
            GROUP BY s.survey_id HAVING COUNT(p.parcela_id) > 0
            ORDER BY COALESCE(s.finished_at, s.started_at) DESC
        """), {"rid": region_id, "sid": survey_id}).fetchall()
        baselines = conn.execute(text("""
            SELECT baseline_id::text, nombre, fecha_relevamiento, n_registros
            FROM baselines WHERE region_id = :rid ORDER BY created_at DESC
        """), {"rid": region_id}).fetchall()
    return JSONResponse({"ok": True, "surveys": [{
        "survey_id": r[0],
        "fecha": r[1].date().isoformat() if r[1] else None,
        "status": r[2], "archivado": bool(r[3]), "n_parcelas": int(r[4]),
    } for r in surveys], "baselines": [{
        "baseline_id": r[0], "nombre": r[1],
        "fecha": r[2].isoformat() if r[2] else None,
        "n_registros": int(r[3]),
    } for r in baselines]})


def _run_comparativa(survey_id: str, contra_tipo: str, contra_id: str) -> dict:
    from scrapitero.agents.comparativa_reporter import ComparativaInput
    from scrapitero.agents.comparativa_reporter import run as run_comp
    kwargs = ({"contra_survey_id": contra_id} if contra_tipo == "survey"
              else {"contra_baseline_id": contra_id})
    return run_comp(ComparativaInput(survey_id=survey_id, **kwargs)).model_dump()


@app.get("/api/surveys/{survey_id}/comparativa")
async def comparativa(survey_id: str,
                      contra_tipo: str = Query(...),
                      contra_id: str = Query(...)) -> JSONResponse:
    """Compara el survey contra un término anterior (survey o baseline).
    Calculado on-the-fly; no persiste nada."""
    if contra_tipo not in ("survey", "baseline"):
        return JSONResponse({"ok": False, "error": f"contra_tipo inválido: {contra_tipo!r}"},
                            status_code=400)
    data = await asyncio.to_thread(_run_comparativa, survey_id, contra_tipo, contra_id)
    return JSONResponse(data, status_code=200 if data.get("ok") else 422)


@app.get("/api/surveys/{survey_id}/export/csv-comparativa")
async def export_csv_comparativa(survey_id: str,
                                 contra_tipo: str = Query(...),
                                 contra_id: str = Query(...)):
    """CSV de la comparativa: una fila por dirección con estado y antes/ahora/Δ."""
    if contra_tipo not in ("survey", "baseline"):
        return JSONResponse({"ok": False, "error": f"contra_tipo inválido: {contra_tipo!r}"},
                            status_code=400)
    data = await asyncio.to_thread(_run_comparativa, survey_id, contra_tipo, contra_id)
    if not data.get("ok"):
        return JSONResponse(data, status_code=422)

    k = data["kpis"]
    estados = {"nueva": "Nueva", "cambio": "Cambió", "igual": "Sin cambio",
               "desaparecida": "Desaparecida"}

    def _gen():
        buf = io.StringIO()
        buf.write("﻿")   # BOM para Excel
        w = csv.writer(buf, delimiter=";")
        w.writerow([f"Comparativa del relevamiento {survey_id}",
                    f"Contra: {data['contra_nombre']} ({data['contra_tipo']})",
                    f"Fecha del anterior: {data['contra_fecha'] or '—'}"])
        w.writerow([f"ΔUF Vivienda: {k['uf_vivienda']['delta']:+}",
                    f"ΔUF Comercio: {k['uf_comercio']['delta']:+}",
                    f"Nuevas: {k['por_estado']['nueva']}",
                    f"Cambiaron: {k['por_estado']['cambio']}",
                    f"Desaparecidas: {k['por_estado']['desaparecida']}",
                    f"Sin cambio: {k['por_estado']['igual']}"])
        w.writerow([])
        w.writerow(["Estado", "Dirección", "Dirección anterior", "Match",
                    "Uso antes", "Uso ahora",
                    "UF Viv antes", "UF Viv ahora", "Δ UF Viv",
                    "UF Com antes", "UF Com ahora", "Δ UF Com"])
        for f in data["filas"]:
            w.writerow([
                estados.get(f["estado"], f["estado"]),
                f["direccion"], f["direccion_antes"] or "",
                f["match"] or "",
                f["uso_antes"] or "", f["uso_ahora"] or "",
                f["uf_viv_antes"], f["uf_viv_ahora"],
                f["uf_viv_ahora"] - f["uf_viv_antes"],
                f["uf_com_antes"], f["uf_com_ahora"],
                f["uf_com_ahora"] - f["uf_com_antes"],
            ])
        yield buf.getvalue()

    filename = f"comparativa_{survey_id[:8]}_{data['contra_fecha'] or 'anterior'}.csv"
    return StreamingResponse(
        _gen(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Archivado de surveys ───────────────────────────────────────────────────────
# Los relevamientos NUNCA se borran por defecto: el anterior siempre queda
# disponible como término de comparación (requisito del cliente). El botón de la
# web archiva; el DELETE físico queda solo para limpieza expresa de surveys YA
# archivados (doble paso).

@app.post("/api/surveys/{survey_id}/archivar")
async def archivar_survey(survey_id: str, archivado: bool = Form(...)) -> JSONResponse:
    """Archiva (o restaura) un relevamiento. Archivado = oculto de la lista pero
    intacto en la DB y disponible en el selector 'Comparar con…'. Solo operador."""
    row = _get_survey_row(survey_id)
    if not row:
        return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
    if archivado and row[2] in ("running", "stopping"):
        return JSONResponse({"ok": False, "error": "Detené el pipeline antes de archivar"},
                            status_code=409)
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("UPDATE surveys SET archivado=:a WHERE survey_id=:sid"),
                     {"a": archivado, "sid": survey_id})
    logger.info(f"Survey {survey_id} {'archivado' if archivado else 'restaurado'}")
    return JSONResponse({"ok": True, "archivado": archivado})


@app.delete("/api/surveys/{survey_id}")
async def eliminar_survey(survey_id: str) -> JSONResponse:
    """Borrado FÍSICO — solo permitido sobre surveys ya archivados (el camino normal
    es archivar; esto queda para limpiar pruebas/basura de forma expresa)."""
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
    with engine.connect() as conn:
        archivado = conn.execute(text(
            "SELECT archivado FROM surveys WHERE survey_id=:sid"),
            {"sid": survey_id}).scalar()
    if not archivado:
        return JSONResponse(
            {"ok": False, "error": "Los relevamientos no se eliminan: archivalo. "
             "(El borrado definitivo solo se permite sobre archivados.)"},
            status_code=409,
        )
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                DELETE FROM unidades_funcionales
                WHERE edificio_id IN (
                    SELECT edificio_id FROM edificios WHERE survey_id = :sid
                )
            """), {"sid": survey_id})
            conn.execute(text("DELETE FROM edificios WHERE survey_id = :sid"), {"sid": survey_id})
            conn.execute(text("DELETE FROM comentarios_cliente WHERE survey_id = :sid"), {"sid": survey_id})
            conn.execute(text("DELETE FROM comercios WHERE survey_id = :sid"), {"sid": survey_id})
            conn.execute(text("DELETE FROM parcelas  WHERE survey_id = :sid"), {"sid": survey_id})
            conn.execute(text("DELETE FROM manzanas_habitantes WHERE survey_id = :sid"), {"sid": survey_id})
            conn.execute(text("DELETE FROM establecimientos WHERE survey_id = :sid"), {"sid": survey_id})
            conn.execute(text("DELETE FROM orchestrator_log WHERE survey_id = :sid"), {"sid": survey_id})
            conn.execute(text("DELETE FROM surveys WHERE survey_id = :sid"), {"sid": survey_id})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    logger.info(f"Survey {survey_id} eliminado")
    return JSONResponse({"ok": True})
