"""Web app de AI Mapping — gestión de relevamientos."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import tempfile
import threading
import urllib.request
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, StreamingResponse)
from loguru import logger
from sqlalchemy import text
from starlette.background import BackgroundTask

from scrapitero.agents.logradouro_br import (clasificar_complemento, descomponer_logradouro,
                                             partes_unidad)
from scrapitero.db.engine import get_engine

app = FastAPI(title="AI Mapping")
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
# El logo se pide ANTES de autenticar (lo usa la pantalla de login), así que va público.
_AUTH_PUBLIC_PATHS = {"/login", "/logout", "/favicon.ico", "/logo.svg", "/logo.png",
                      "/logo-mark.svg", "/theme.css", "/i18n.js"}
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


@app.get("/theme.css")
async def theme_css() -> FileResponse:
    """Sistema de diseño de AI Mapping (tokens claro/oscuro + componentes).
    Lo piden las tres páginas y también el login, así que va en _AUTH_PUBLIC_PATHS."""
    return FileResponse(STATIC_DIR / "theme.css", media_type="text/css")


@app.get("/i18n.js")
async def i18n_js() -> FileResponse:
    """Idioma de la interfaz (motor + diccionario ES→PT). Lo piden el dashboard y el
    login, así que va en _AUTH_PUBLIC_PATHS igual que /theme.css: sin eso el middleware
    responde 302 y el navegador recibe HTML donde espera JavaScript.

    no-store como index.html: el diccionario viaja apareado con el markup, y una copia
    cacheada vieja contra un texto nuevo deja de traducir sin ningún error visible."""
    return FileResponse(
        STATIC_DIR / "i18n.js", media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/logo.svg")
async def logo_svg() -> FileResponse:
    """Wordmark horizontal (glifo + 'AiMapping')."""
    return FileResponse(STATIC_DIR / "logo.svg", media_type="image/svg+xml")


@app.get("/logo.png")
async def logo_png() -> FileResponse:
    """Wordmark original de la marca, en bitmap. El SVG dibuja el glifo pero no puede
    cargar Space Grotesk (un SVG servido por <img> no resuelve fuentes externas), así
    que donde hace falta el lettering exacto se usa este PNG."""
    return FileResponse(STATIC_DIR / "logo.png", media_type="image/png")


@app.get("/logo-mark.svg")
async def logo_mark_svg() -> FileResponse:
    """Glifo solo (la "A" con el pin) — favicon y header, donde el wordmark no entra."""
    return FileResponse(STATIC_DIR / "logo-mark.svg", media_type="image/svg+xml")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    """La ruta ya estaba permitida en el middleware de auth pero no la servía nadie (404)."""
    return FileResponse(STATIC_DIR / "logo-mark.svg", media_type="image/svg+xml")


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
    err_html = ('<p class="err" data-i18n>Contraseña incorrecta o sin permiso.</p>' if error else "")
    html = f"""<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title data-i18n>AI Mapping — Acceso</title>
<link rel="icon" type="image/svg+xml" href="/logo-mark.svg">
<link rel="stylesheet" href="/theme.css">
<script>
  // Mismo tema que el resto de la app, antes del primer pintado.
  (function () {{
    // `pref` y no `t`: `t` es la función global de traducción (i18n.js).
    var pref = null;
    try {{ pref = localStorage.getItem('aim-theme'); }} catch (e) {{}}
    if (pref !== 'light' && pref !== 'dark')
      pref = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    document.documentElement.dataset.theme = pref;
  }})();
</script>
<!-- Idioma: mismo mecanismo que el resto de la app (localStorage 'aim-lang'). El login
     es la primera página que se ve, cuando el servidor todavía no puede leer esa
     preferencia, así que la traducción es 100% del lado del cliente. -->
<script src="/i18n.js"></script>
<style>
  body {{ min-height: 100vh; display: flex; align-items: center; justify-content: center;
         padding: 1.5rem; position: relative; overflow: hidden; }}
  /* Resplandor teal del hero de aimapping.net, detrás de la tarjeta */
  .glow {{ position: fixed; width: 900px; height: 900px; top: -300px; left: 50%;
           transform: translateX(-50%); border-radius: 50%; pointer-events: none;
           background: radial-gradient(ellipse at center,
                       hsl(var(--primary) / .13) 0%, transparent 68%); filter: blur(2px); }}
  form.card {{ position: relative; padding: 2.25rem 2.25rem 2rem; width: 340px; max-width: 100%;
               box-shadow: var(--shadow-lg); }}
  .marca {{ display: block; margin: 0 auto .35rem; width: 62px; height: auto; }}
  h1 {{ font-family: var(--font-head); font-size: 1.5rem; text-align: center;
        letter-spacing: -.03em; margin-bottom: .3rem; }}
  h1 .ai {{ color: var(--brand-teal); }} h1 .mapping {{ color: var(--brand-indigo); }}
  :root[data-theme="dark"] h1 .mapping {{ color: #9ea4dd; }}
  p.sub {{ color: hsl(var(--muted-fg)); font-size: .84rem; text-align: center; margin-bottom: 1.5rem; }}
  p.tag {{ font-family: var(--font-head); font-size: .62rem; font-weight: 600;
           text-transform: uppercase; letter-spacing: .14em; text-align: center;
           color: hsl(var(--primary)); margin-bottom: 1.4rem; }}
  button[type=submit] {{ width: 100%; margin-top: 1.1rem; }}
  .err {{ color: var(--danger-fg); background: var(--danger-bg); font-size: .8rem;
          border-radius: var(--radius-sm); padding: .5rem .7rem; margin-bottom: .9rem; }}
  /* El toggle de idioma (.lang-toggle vive en theme.css) va sobre la tarjeta */
  .lang-toggle {{ position: absolute; top: 1rem; right: 1rem; }}
</style></head><body>
  <div class="glow"></div>
  <button class="lang-toggle" type="button" onclick="toggleLang()" data-i18n-attr="title,aria-label"
          title="Cambiar el idioma de la interfaz (español / portugués)"
          aria-label="Cambiar idioma"><span data-lang="es">ES</span><span data-lang="pt">PT</span></button>
  <form class="card" method="post" action="/login">
    <img class="marca" src="/logo-mark.svg" alt="">
    <h1><span class="ai">Ai</span><span class="mapping">Mapping</span></h1>
    <p class="tag" data-i18n>Relevamiento inteligente</p>
    {err_html}
    <input type="hidden" name="next" value="{next_url}">
    <label class="form-label" data-i18n>Contraseña</label>
    <input type="password" name="password" autofocus required>
    <button class="btn btn-primary" type="submit" data-i18n>Ingresar</button>
    <p class="sub" style="margin:1.1rem 0 0;font-size:.75rem" data-i18n>Plataforma de relevamiento geoespacial</p>
  </form>
<script>
  i18nApply(document);
  // A diferencia del dashboard, acá NO se recarga: _login_page también es la respuesta
  // de un POST (contraseña incorrecta), y recargar dispararía el reenvío del formulario.
  // Como el login es 100% markup estático, el walker lo traduce entero en vivo y no se
  // pierde lo ya tipeado en el campo de contraseña.
  function toggleLang() {{ i18nSetLang(LANG === 'pt' ? 'es' : 'pt'); }}
</script>
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
            # Formato de entrega propio del cliente, si su país tiene uno definido
            # (`_EXPORT_PERFILES_CLIENTE`). El front muestra el botón según esto, en vez
            # de preguntar `country_code === 'BRA'` en cada página.
            "export_cliente": _perfil_cliente_ui((r[6] or "").upper()),
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


# Las 4 etiquetas de hospedaje que devuelve `_hotel_tipo_label`. Un descarte rotulado con una
# de ellas es un hotel duplicado (ya contado en otro registro), no un falso hotel.
_TIPOS_HOSPEDAJE = ("HOTEL", "MOTEL", "FLAT", "PENSÃO")


# Taxonomía FIJA del cliente (solo Brasil) para el "Tipo de edificación". Fuente de verdad:
# docs/TIPOS_PROPIEDAD.md. Es la lista que ve el operador al etiquetar a mano una parcela.
TIPOS_EDIFICACION: dict[str, list[str]] = {
    "R": ["RESIDÊNCIA", "APARTAMENTO", "PENSÃO"],
    "C": ["AGÊNCIA DE AUTOMOVEIS", "BAR", "BUFFET", "CASA NOTURNA", "COMÉRCIO EM GERAL",
          "ESCRITÓRIO DE SERVICOS", "IMOBILIÁRIA", "INDÚSTRIA", "INSTITUIÇÃO FINANCEIRA",
          "LANCHONETE", "OFICINA", "PADARIA", "RESTAURANTE"],
    "E": ["ASSOCIAÇÃO / SINDICATO", "CLÍNICA PARTICULAR", "CLÍNICA PUBLICA",
          "CONSULTÓRIO PARTICULAR", "CONSULTÓRIO PÚBLICO", "CRECHE", "ESCOLA",
          "ESCOLA PARTICULAR", "ESCOLA PÚBLICA", "ESCOLA PÚBLICA ESTADUAL",
          "ESCOLA PÚBLICA MUNICIPAL", "ESTACIONAMENTO", "FLAT", "HOSPITAL PARTICULAR",
          "HOSPITAL PÚBLICO", "HOTEL", "INSTITUICAO ESPORTIVA", "MÉDICO / HOSPITALAR",
          "MOTEL", "ÓRGÃO PÚBLICO", "POSTO DE GASOLINA", "SERVICOS", "SHOPPING",
          "SUPERMERCADO", "UNIVERSIDADE/FACULDADE", "LOTE VAZIO"],
}
# categoría (R/C/E) de cada etiqueta, para guardar junto al override manual.
_TIPO_CATEGORIA: dict[str, str] = {
    t: cat for cat, ts in TIPOS_EDIFICACION.items() for t in ts}

# ── Localización de la taxonomía (relevamientos fuera de Brasil) ───────────────
# La lista de arriba es el contrato del cliente BRASILERO y por eso se guarda SIEMPRE
# en portugués (DB, `parcela_tipo_manual`, CSV Operadora, comparativas). Pero en un
# relevamiento argentino esas etiquetas se muestran en la leyenda del mapa, el popup y
# el CSV, y ahí el portugués no tiene sentido: se traducen al SALIR, nunca al guardar.
# Así el dato sigue siendo comparable entre países y sólo cambia la presentación.
_TIPO_ES: dict[str, str] = {
    "RESIDÊNCIA": "VIVIENDA",
    "APARTAMENTO": "DEPARTAMENTO",
    "PENSÃO": "PENSIÓN",
    "LOTE VAZIO": "LOTE BALDÍO",
    "COMÉRCIO EM GERAL": "COMERCIO EN GENERAL",
    "INDÚSTRIA": "INDUSTRIA",
    "AGÊNCIA DE AUTOMOVEIS": "AGENCIA DE AUTOMOTORES",
    "BAR": "BAR",
    "BUFFET": "SALÓN DE EVENTOS",
    "CASA NOTURNA": "BOLICHE",
    "ESCRITÓRIO DE SERVICOS": "OFICINA DE SERVICIOS",
    "IMOBILIÁRIA": "INMOBILIARIA",
    "INSTITUIÇÃO FINANCEIRA": "ENTIDAD FINANCIERA",
    "LANCHONETE": "ROTISERÍA",
    "OFICINA": "TALLER",
    "PADARIA": "PANADERÍA",
    "RESTAURANTE": "RESTAURANTE",
    "ASSOCIAÇÃO / SINDICATO": "ASOCIACIÓN / SINDICATO",
    "CLÍNICA PARTICULAR": "CLÍNICA PRIVADA",
    "CLÍNICA PUBLICA": "CLÍNICA PÚBLICA",
    "CONSULTÓRIO PARTICULAR": "CONSULTORIO PRIVADO",
    "CONSULTÓRIO PÚBLICO": "CONSULTORIO PÚBLICO",
    "CRECHE": "JARDÍN MATERNAL",
    "ESCOLA": "ESCUELA",
    "ESCOLA PARTICULAR": "ESCUELA PRIVADA",
    "ESCOLA PÚBLICA": "ESCUELA PÚBLICA",
    "ESCOLA PÚBLICA ESTADUAL": "ESCUELA PROVINCIAL",
    "ESCOLA PÚBLICA MUNICIPAL": "ESCUELA MUNICIPAL",
    "ESTACIONAMENTO": "ESTACIONAMIENTO",
    "FLAT": "APART HOTEL",
    "HOSPITAL PARTICULAR": "HOSPITAL PRIVADO",
    "HOSPITAL PÚBLICO": "HOSPITAL PÚBLICO",
    "HOTEL": "HOTEL",
    "INSTITUICAO ESPORTIVA": "CLUB DEPORTIVO",
    "MÉDICO / HOSPITALAR": "SALUD",
    "MOTEL": "ALBERGUE TRANSITORIO",
    "ÓRGÃO PÚBLICO": "ORGANISMO PÚBLICO",
    "POSTO DE GASOLINA": "ESTACIÓN DE SERVICIO",
    "SERVICOS": "SERVICIOS",
    "SHOPPING": "SHOPPING",
    "SUPERMERCADO": "SUPERMERCADO",
    "UNIVERSIDADE/FACULDADE": "UNIVERSIDAD / FACULTAD",
}
# ES → PT, para volver a la etiqueta canónica cuando el operador etiqueta a mano
# en un relevamiento argentino (lo que se guarda es siempre el portugués).
_TIPO_CANONICO: dict[str, str] = {es: pt for pt, es in _TIPO_ES.items()}


def _tipo_localizado(label: Optional[str], country_code: Optional[str]) -> Optional[str]:
    """Traduce la etiqueta de tipo de edificación al idioma del relevamiento.

    Brasil (o país desconocido) → se deja la taxonomía original del cliente.
    Cualquier otro país → español. Una etiqueta que no esté en el diccionario
    (p.ej. una descripción CNPJ suelta) se devuelve tal cual.
    """
    if not label or (country_code or "").upper() == "BRA":
        return label
    return _TIPO_ES.get(label, label)


def _tipo_canonico(label: Optional[str]) -> Optional[str]:
    """Inverso de `_tipo_localizado`: lo que llega de la UI vuelve a portugués."""
    if not label:
        return label
    return _TIPO_CANONICO.get(label.strip().upper(), label)


def _descripcion_localizada(desc: Optional[str], country_code: Optional[str]) -> Optional[str]:
    """Traduce `descripcion_uso`, que es una LISTA de etiquetas separadas por coma.

    `ParcelaCategoria` junta en un solo campo las etiquetas de TODOS los establecimientos
    que caen en la parcela, así que no alcanza con pasarla por `_tipo_localizado`: hay que
    traducir cada ítem. Sin esto la tarjeta del mapa mostraba el tipo ya traducido
    ("🏢 ROTISERÍA") y justo debajo el mismo concepto en portugués ("🏷️ Comercial
    LANCHONETE"), que en un relevamiento argentino no le dice nada al operador.
    """
    if not desc or (country_code or "").upper() == "BRA":
        return desc
    partes = [x.strip() for x in desc.split(",") if x.strip()]
    return ", ".join(_TIPO_ES.get(x, x) for x in partes) or None


def _country_de_survey(conn, survey_id: str) -> str:
    row = conn.execute(text(
        "SELECT COALESCE(r.country_code,'') FROM surveys s "
        "JOIN regions r ON r.region_id = s.region_id WHERE s.survey_id = :sid"
    ), {"sid": survey_id}).first()
    return (row[0] if row else "") or ""


def _tipo_edificacion(uso: Optional[str], uf_v, area, descripcion: Optional[str],
                      hotel_tipo: Optional[str], manual: Optional[str] = None) -> str:
    if manual:                                       # etiqueta forzada a mano → gana a todo
        return manual
    if hotel_tipo:
        return _hotel_tipo_label(hotel_tipo)
    if descripcion:                                  # 1+ descripciones CNPJ → la primera
        return descripcion.split(",")[0].strip()
    u = (uso or "").lower()
    if u == "vacante":
        return "LOTE VAZIO"
    # "Hay algo construido" se decide por área construida O por UF: el área es la señal
    # del BCI brasilero, pero NO todas las fuentes la publican —ARBA (PBA) no la trae—, y
    # con `not area → LOTE VAZIO` un relevamiento argentino entero salía como baldío
    # aunque tuviera cientos de UF declaradas (Hurlingham: 441 parcelas, 570 UF).
    if not area and not (uf_v or 0):
        return "LOTE VAZIO"
    if u == "residencial":
        return "APARTAMENTO" if (uf_v or 0) > 1 else "RESIDÊNCIA"
    # Uso todavía sin clasificar pero con UF de vivienda declarada (típico de PBA antes de
    # correr UsoClassifier): la UF manda, es dato del catastro.
    if u in ("", "sin_datos") and (uf_v or 0) > 0:
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
        # La taxonomía se guarda en portugués (contrato del cliente brasilero) pero se
        # muestra en el idioma del relevamiento: en Argentina la leyenda va en español.
        pais = _country_de_survey(conn, survey_id)
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
                       ORDER BY h.habitaciones DESC NULLS LAST LIMIT 1), '') AS hotel_tipo,
                   p.es_country,
                   (SELECT COUNT(*) FROM parcela_unidades pu
                    WHERE pu.parcela_id = p.parcela_id) AS n_unidades,
                   ptm.tipo_edificacion AS tipo_manual,
                   -- Número de puerta ESTIMADO (NumeroEstimator, mig. 050): sólo lo tienen las
                   -- parcelas que el catastro dejó sin altura. Se muestra marcado como estimado.
                   p.numero_estimado, p.numero_estimado_metodo, p.numero_estimado_confianza
            FROM parcelas p
            LEFT JOIN establecimientos e ON e.establecimiento_id = p.establecimiento_id
            LEFT JOIN parcela_tipo_manual ptm ON ptm.parcela_id = p.parcela_id
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
            # Categoría/descripción de uso (taxonomía del cliente, de establecimientos CNPJ).
            # La descripción se traduce al SALIR, igual que `tipo_edificacion`: en la DB la
            # etiqueta canónica sigue siendo la portuguesa.
            "categoria_uso": r[27] or None,
            "descripcion_uso": _descripcion_localizada(r[28], pais) or None,
            # Tipo de edificación unificado (1 label de la lista del cliente). r[32]=override manual.
            "tipo_edificacion": _tipo_localizado(
                _tipo_edificacion(r[2], uf_viv, r[11], r[28], r[29], r[32]), pais) or None,
            # Ítems críticos a los que pertenece la parcela (capas toggleables del mapa). Una
            # parcela puede estar en varias (un edificio de deptos es Edificio Y PH).
            "items": _items_criticos(hotel_tipo=r[29], uf_viv=uf_viv,
                                     n_unidades=int(r[31] or 0),
                                     descripcion=r[28], es_country=bool(r[30])),
            # Número inferido para las parcelas sin altura en el catastro. NUNCA reemplaza a
            # `numero`: el front lo dibuja aparte y marcado (≈ N est.).
            "numero_est": r[33] or "",
            "numero_est_metodo": r[34] or "",
            "numero_est_conf": round(float(r[35]), 2) if r[35] is not None else None,
        })
    return out


def _items_criticos(hotel_tipo, uf_viv, n_unidades, descripcion, es_country) -> list:
    """Capas críticas a las que pertenece una parcela (Hoteles/Edificios/PH/Shopping/Country).
    Una parcela puede caer en varias. Ver la tabla de definiciones en CLAUDE.md."""
    items = []
    if hotel_tipo:                                  # hotel abierto vinculado
        items.append("hotel")
    if (uf_viv or 0) > 1:                           # APARTAMENTO (residencial multi-unidad)
        items.append("edificio")
    if (n_unidades or 0) > 1:                       # propiedad horizontal (>1 unidad en el BCI)
        items.append("ph")
    if descripcion and "shopping" in descripcion.lower():
        items.append("shopping")
    if es_country:                                  # dentro de un condomínio/loteamento fechado (OSM)
        items.append("country")
    return items


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


@app.post("/api/surveys/{survey_id}/country")
async def run_country(survey_id: str) -> JSONResponse:
    """Corre CountryFetcher: detecta barrios cerrados / condomínios en OSM dentro de la zona y
    marca `parcelas.es_country` de las que caen adentro → alimenta la capa 🏘 Country del mapa."""
    row = _get_survey_row(survey_id)
    if not row:
        return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
    region_id = row[0]

    from scrapitero.agents.country_fetcher import CountryFetcherInput
    from scrapitero.agents.country_fetcher import run as run_country_agent

    def _job() -> dict:
        _thread_job_id.value = survey_id
        return run_country_agent(CountryFetcherInput(region_id=region_id,
                                                     survey_id=survey_id)).model_dump()

    data = await asyncio.to_thread(_job)
    return JSONResponse(data, status_code=200 if data.get("ok") else 422)


@app.post("/api/surveys/{survey_id}/numeros")
async def run_numeros(survey_id: str) -> JSONResponse:
    """Corre NumeroEstimator: infiere el número de puerta de las parcelas que el catastro dejó
    sin altura (`numero` en `0` o vacío), interpolando sobre el eje de la calle entre los
    linderos con número real. Escribe SOLO `parcelas.numero_estimado*` (mig. 050) — el `numero`
    del municipio nunca se toca, y la UI/CSV lo muestran marcado como estimado."""
    row = _get_survey_row(survey_id)
    if not row:
        return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)

    from scrapitero.agents.numero_estimator import NumeroEstimatorInput
    from scrapitero.agents.numero_estimator import run as run_numeros_agent

    def _job() -> dict:
        _thread_job_id.value = survey_id
        return run_numeros_agent(NumeroEstimatorInput(region_id=row[0], survey_id=survey_id,
                                                      max_detalle=0)).model_dump()

    data = await asyncio.to_thread(_job)
    return JSONResponse(data, status_code=200 if data.get("ok") else 422)


@app.post("/api/surveys/{survey_id}/footprints")
async def run_footprints(survey_id: str) -> JSONResponse:
    """Corre FootprintFetcher: trae footprints de edificios (Google Open Buildings, fallback
    OSM) para la capa de REVISIÓN visual 🏗️ — no toca `parcelas` ni el relevamiento, solo
    guarda en `footprints_revision` para comparar contra lo que dice el catastro."""
    row = _get_survey_row(survey_id)
    if not row:
        return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
    region_id = row[0]

    from scrapitero.agents.footprint_fetcher import FootprintInput
    from scrapitero.agents.footprint_fetcher import run as run_footprint_agent

    def _job() -> dict:
        _thread_job_id.value = survey_id
        return run_footprint_agent(FootprintInput(region_id=region_id,
                                                   survey_id=survey_id)).model_dump()

    data = await asyncio.to_thread(_job)
    return JSONResponse(data, status_code=200 if data.get("ok") else 422)


@app.get("/api/surveys/{survey_id}/parcelas.geojson")
async def get_parcelas_geojson(survey_id: str) -> dict:
    """Contorno catastral de cada parcela, para la capa 🧩 Límites de parcela del mapa.

    El marcador del mapa es un `ST_PointOnSurface`: un punto cualquiera **dentro** del
    polígono, o sea un derivado. Si la geometría del catastro vino mal, el punto también
    está mal y no hay forma de notarlo mirando puntos. Esta capa muestra el dato de origen.

    La geometría va **sin simplificar** a propósito: son 6-9 vértices por lote (110 kB las
    619 de Malvinas, 168 kB las 567 de Várzea Grande), así que simplificar ahorraría
    centenares de bytes a cambio de deformar un límite catastral — que es justo lo que la
    capa existe para poder verificar.
    """
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT ST_AsGeoJSON(geometry), parcela_id::text, cca_code, calle, numero,
                   area_m2_terreno, uso_principal, validado_manual
            FROM parcelas
            WHERE survey_id = :sid AND geometry IS NOT NULL
        """), {"sid": survey_id}).fetchall()

    features = []
    for geom_gj, parcela_id, cca, calle, numero, area, uso, validado in rows:
        if not geom_gj:
            continue
        features.append({
            "type": "Feature",
            "geometry": json.loads(geom_gj),
            "properties": {
                "parcela_id": parcela_id,
                "cca_code": cca,
                "direccion": " ".join(x for x in (calle, numero) if x and x != "0"),
                "area_m2_terreno": round(float(area), 1) if area is not None else None,
                "uso_principal": uso,
                "validado_manual": bool(validado),
            },
        })
    return {"type": "FeatureCollection", "features": features}


@app.get("/api/surveys/{survey_id}/footprints")
async def get_footprints(survey_id: str) -> dict:
    """GeoJSON FeatureCollection de los footprints ya traídos por 🏗️ Footprints (revisión).
    Solo los vinculados a una parcela relevada (el bbox de descarga es más ancho que la
    zona relevada — sin este filtro el payload trae también las cuadras vecinas, que no
    aportan nada a la revisión). Cada feature lleva lo que dice el catastro (uf_vivienda /
    tipo de edificación) para comparar visualmente contra el footprint en el popup."""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT ST_AsGeoJSON(f.footprint), f.source, f.confidence, f.area_m2,
                   p.parcela_id::text, p.uso_principal, p.uf_vivienda, p.area_m2_construida,
                   p.descripcion_uso,
                   COALESCE((SELECT h.tipo FROM hoteles h
                       WHERE h.parcela_id = p.parcela_id AND NOT h.cerrado_def
                       ORDER BY h.habitaciones DESC NULLS LAST LIMIT 1), '') AS hotel_tipo,
                   ptm.tipo_edificacion AS tipo_manual
            FROM footprints_revision f
            JOIN parcelas p ON p.parcela_id = f.parcela_id
            LEFT JOIN parcela_tipo_manual ptm ON ptm.parcela_id = p.parcela_id
            WHERE f.survey_id = :sid
        """), {"sid": survey_id}).fetchall()

    features = []
    for geom_gj, source, confidence, area_m2, parcela_id, uso, uf_viv, area_c, descripcion, hotel_tipo, tipo_manual in rows:
        if not geom_gj:
            continue
        props = {
            "source": source,
            "confidence": round(float(confidence), 3) if confidence is not None else None,
            "area_m2": round(float(area_m2), 1) if area_m2 is not None else None,
            "parcela_id": parcela_id,
        }
        if parcela_id:
            props["catastro_tipo_edificacion"] = _tipo_edificacion(
                uso, uf_viv, area_c, descripcion, hotel_tipo, tipo_manual)
            props["catastro_uf_vivienda"] = int(uf_viv or 0)
        features.append({
            "type": "Feature",
            "geometry": json.loads(geom_gj),
            "properties": props,
        })
    return {"type": "FeatureCollection", "features": features}


@app.post("/api/surveys/{survey_id}/altura")
async def run_altura(survey_id: str) -> JSONResponse:
    """Corre AlturaFetcher: altura satelital por parcela (Google Solar − Elevation) y la
    contrasta con el proxy de pisos del catastro → capa de revisión 📏. El BCI no trae
    pisos, así que esta es la única señal de "hay más construido de lo declarado"."""
    row = _get_survey_row(survey_id)
    if not row:
        return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
    region_id = row[0]

    from scrapitero.agents.altura_fetcher import AlturaInput
    from scrapitero.agents.altura_fetcher import run as run_altura_agent

    def _job() -> dict:
        _thread_job_id.value = survey_id
        return run_altura_agent(AlturaInput(region_id=region_id,
                                            survey_id=survey_id)).model_dump()

    data = await asyncio.to_thread(_job)
    return JSONResponse(data, status_code=200 if data.get("ok") else 422)


@app.get("/api/surveys/{survey_id}/altura")
async def get_altura(survey_id: str) -> dict:
    """Altura satelital por parcela para la capa 📏 (revisión).

    Devuelve el año de la imagen junto a cada dato **a propósito**: la imagery de Solar en
    VG es en su mayoría de 2014, así que el número no es "estado actual" y el operador tiene
    que poder verlo antes de sacar conclusiones."""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT p.centroid_lat, p.centroid_lng, p.cca_code, p.calle, p.numero,
                   p.uso_principal, p.area_m2_construida,
                   a.altura_m, a.pisos_satelital, a.pisos_bci_proxy,
                   a.discrepancia, a.motivo, a.imagery_year, a.imagery_quality,
                   a.ground_area_m2
            FROM parcela_altura a
            JOIN parcelas p ON p.parcela_id = a.parcela_id
            WHERE a.survey_id = :sid
              AND p.centroid_lat IS NOT NULL AND p.centroid_lng IS NOT NULL
            ORDER BY a.discrepancia DESC, a.altura_m DESC NULLS LAST
        """), {"sid": survey_id}).fetchall()

    items = [{
        "lat": float(r[0]), "lng": float(r[1]),
        "cca": r[2] or "", "calle": r[3] or "", "numero": r[4] or "",
        "uso": r[5] or "", "area_c": round(float(r[6]), 1) if r[6] else None,
        "altura_m": round(float(r[7]), 1) if r[7] is not None else None,
        "pisos_sat": int(r[8]) if r[8] is not None else None,
        "pisos_bci": int(r[9]) if r[9] is not None else None,
        "discrepancia": bool(r[10]),
        "motivo": r[11] or None,
        "imagery_year": int(r[12]) if r[12] else None,
        "imagery_quality": r[13] or None,
        "ground_area_m2": round(float(r[14]), 0) if r[14] else None,
    } for r in rows]
    return {
        "total": len(items),
        "discrepancias": sum(1 for i in items if i["discrepancia"]),
        "items": items,
    }


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
                   h.habitaciones_fuente, h.direccion, h.nota
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
        "nota": r[13],          # comentario del operador (mig. 048), se ve en el popup
    } for r in rows]
    abiertos = [h for h in hoteles if not h["cerrado"]]
    return JSONResponse({
        "ok": True, "hoteles": hoteles, "total": len(hoteles),
        "abiertos": len(abiertos), "cerrados": len(hoteles) - len(abiertos),
        "habitaciones_total": sum(h["habitaciones"] or 0 for h in abiertos),
    })


@app.get("/asistencia-hoteles/{survey_id}")
async def asistencia_hoteles_page(survey_id: str):
    """Compatibilidad: la asistencia de hoteles quedó absorbida por el reporte de incidencias.
    Se mantiene la ruta para que los links ya enviados por Telegram sigan funcionando."""
    return RedirectResponse(f"/incidencias/{survey_id}?tipo=hotel_sin_habitaciones",
                            status_code=302)


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


def _marcar_cerrado(conn, hotel_id: str, cerrado: bool, nota: str | None) -> tuple[bool, object]:
    """Fuerza a mano el abierto/cerrado de un hotel y le deja una nota (mig. 048).

    Extraído para que lo compartan el endpoint del mapa y el del reporte de incidencias
    (misma transacción del caller). El override se guarda por `(region_id, cnpj)` en
    `hotel_cerrado_manual`, que `HotelFetcher` re-aplica al final de cada corrida — si no,
    el próximo botón 🏨 lo reabriría porque Receita lo sigue dando ATIVA.

    Devuelve (ok, region_id | mensaje_error)."""
    row = conn.execute(text(
        "SELECT region_id, cnpj FROM hoteles WHERE hotel_id::text = :h"),
        {"h": hotel_id}).fetchone()
    if not row:
        return False, "Hotel no encontrado"
    region_id, cnpj = row
    conn.execute(text(
        "UPDATE hoteles SET cerrado_def = :cerr, nota = :nota WHERE hotel_id::text = :h"),
        {"cerr": cerrado, "nota": nota, "h": hotel_id})
    # Sin CNPJ (típico de Google) el override no tiene clave natural durable: el cambio vale
    # para el estado actual pero un re-corte del botón 🏨 lo pierde. Mismo límite que las
    # habitaciones manuales.
    if cnpj:
        conn.execute(text("""
            INSERT INTO hotel_cerrado_manual (region_id, cnpj, cerrado, nota, autor)
            VALUES (:r, :c, :cerr, :nota, 'operador')
            ON CONFLICT (region_id, cnpj)
            DO UPDATE SET cerrado = :cerr, nota = :nota, actualizado_at = now()
        """), {"r": region_id, "c": cnpj, "cerr": cerrado, "nota": nota})
    return True, region_id


def _mover_hotel(conn, hotel_id: str, lat: Optional[float], lng: Optional[float],
                 direccion: Optional[str], nota: Optional[str]) -> tuple[bool, str]:
    """Aplica la corrección de ubicación/dirección de un hotel (mig. 049).

    Extraído del endpoint `/api/hoteles/{id}/ubicacion` para que el editor único del panel de
    incidencias mueva el hotel con exactamente las mismas escrituras: `hoteles` + re-vínculo de
    parcela por `ST_Contains` + override durable por `(region_id, cnpj)`. Devuelve
    `(ok, parcela_id_o_error)`."""
    row = conn.execute(text(
        "SELECT region_id, cnpj FROM hoteles WHERE hotel_id::text = :h"),
        {"h": hotel_id}).fetchone()
    if not row:
        return False, "Hotel no encontrado"
    region_id, cnpj = row
    conn.execute(text("""
        UPDATE hoteles
           SET location = COALESCE(ST_SetSRID(ST_MakePoint(:lng, :lat), 4326), location),
               direccion = COALESCE(:dir, direccion)
         WHERE hotel_id::text = :h
    """), {"lat": lat, "lng": lng, "dir": direccion, "h": hotel_id})
    # Re-vincular la parcela con el punto ya corregido (el motivo de fondo de la corrección).
    conn.execute(text("""
        UPDATE hoteles h SET parcela_id = p.parcela_id
        FROM parcelas p
        WHERE h.hotel_id::text = :h AND p.region_id = h.region_id
          AND p.geometry IS NOT NULL AND h.location IS NOT NULL
          AND ST_Contains(p.geometry, h.location)
    """), {"h": hotel_id})
    if cnpj:
        conn.execute(text("""
            INSERT INTO hotel_ubicacion_manual (region_id, cnpj, lat, lng, direccion, nota, autor)
            VALUES (:r, :c, :lat, :lng, :dir, :nota, 'operador')
            ON CONFLICT (region_id, cnpj) DO UPDATE SET
                lat = COALESCE(:lat, hotel_ubicacion_manual.lat),
                lng = COALESCE(:lng, hotel_ubicacion_manual.lng),
                direccion = COALESCE(:dir, hotel_ubicacion_manual.direccion),
                nota = COALESCE(:nota, hotel_ubicacion_manual.nota),
                actualizado_at = now()
        """), {"r": region_id, "c": cnpj, "lat": lat, "lng": lng,
               "dir": direccion, "nota": nota})
    pid = conn.execute(text(
        "SELECT parcela_id::text FROM hoteles WHERE hotel_id::text = :h"),
        {"h": hotel_id}).scalar()
    return True, pid


@app.post("/api/hoteles/{hotel_id}/ubicacion")
async def set_ubicacion_manual(hotel_id: str, request: Request) -> JSONResponse:
    """El operador corrige la coordenada y/o la dirección de un hotel (mig. 049).

    Hace falta cuando la fuente no trae coordenada y el geocoder externo erra sobre la dirección
    fiscal: REAL VILLES quedaba a 419 m del hotel real, en una cuadra sin ningún alojamiento.
    Re-vincula la parcela en el acto por `ST_Contains` con el punto corregido, y persiste el
    override para que el próximo botón 🏨 no lo pierda."""
    try:
        body = await request.json()
        lat = body.get("lat")
        lng = body.get("lng")
        lat = float(lat) if lat is not None else None
        lng = float(lng) if lng is not None else None
        direccion = (body.get("direccion") or "").strip() or None
        nota = (body.get("nota") or "").strip() or None
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "lat/lng inválidos"}, status_code=400)
    if lat is None and lng is None and direccion is None:
        return JSONResponse({"ok": False, "error": "nada para corregir"}, status_code=400)
    if (lat is None) != (lng is None):
        return JSONResponse({"ok": False, "error": "lat y lng van juntos"}, status_code=400)
    engine = get_engine()
    with engine.begin() as conn:
        ok, detalle = _mover_hotel(conn, hotel_id, lat, lng, direccion, nota)
    if not ok:
        return JSONResponse({"ok": False, "error": detalle}, status_code=404)
    return JSONResponse({"ok": True, "lat": lat, "lng": lng, "direccion": direccion,
                         "parcela_id": detalle})


@app.post("/api/hoteles/{hotel_id}/cerrado")
async def set_cerrado_manual(hotel_id: str, request: Request) -> JSONResponse:
    """El operador marca un hotel como cerrado (o lo reabre) y le deja un comentario.

    Necesario porque ninguna fuente lo resuelve: un CNPJ puede seguir ATIVA en Receita años
    después de que el hotel dejó de operar (caso MONTANA PALACE en Filinto Müller 1059, donde
    hoy opera el Zazori). A diferencia de "no es hotel", la fila se conserva: sigue en el mapa
    con el pin rojo y la nota explica qué hay hoy en su lugar."""
    try:
        body = await request.json()
        cerrado = bool(body.get("cerrado", True))
        nota = (body.get("nota") or "").strip() or None
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "body inválido"}, status_code=400)
    engine = get_engine()
    with engine.begin() as conn:
        ok, detalle = _marcar_cerrado(conn, hotel_id, cerrado, nota)
    if not ok:
        return JSONResponse({"ok": False, "error": detalle}, status_code=404)
    return JSONResponse({"ok": True, "cerrado": cerrado, "nota": nota})


@app.get("/api/tipos-edificacion")
async def tipos_edificacion(survey_id: Optional[str] = None) -> JSONResponse:
    """Taxonomía fija del cliente (R/C/E) para que el operador elija una etiqueta a mano.
    Fuente de verdad: docs/TIPOS_PROPIEDAD.md / TIPOS_EDIFICACION.

    Con `survey_id` de un relevamiento fuera de Brasil las etiquetas salen en español
    (`_tipo_localizado`); lo que el operador elija vuelve a la etiqueta canónica en
    portugués al guardarse (`_tipo_canonico`), así la DB queda igual en todos los países.
    """
    pais = ""
    if survey_id:
        with get_engine().connect() as conn:
            pais = _country_de_survey(conn, survey_id)
    etiquetas = {"R": "Residencial", "C": "Comercial", "E": "Especial"}
    return JSONResponse({"ok": True, "grupos": [
        {"categoria": cat, "titulo": etiquetas[cat],
         "tipos": [_tipo_localizado(t, pais) for t in TIPOS_EDIFICACION[cat]]}
        for cat in ("C", "R", "E")]})     # Comercial primero (lo más común al corregir un falso hotel)


@app.post("/api/hoteles/{hotel_id}/no-es-hotel")
async def hotel_no_es_hotel(hotel_id: str, request: Request) -> JSONResponse:
    """El operador marca que un supuesto hotel NO es hotel (p.ej. Google mal-tagueó una
    tienda como `lodging`). Lo saca de `hoteles`, recuerda el descarte en `hotel_descartado`
    (para que un re-corte del botón 🏨 no lo re-cree) y le pone la etiqueta elegida a la
    parcela en `parcela_tipo_manual` (gana al cálculo automático de _tipo_edificacion)."""
    try:
        body = await request.json()
        # En un relevamiento no-brasilero la UI ofrece la etiqueta en español: se guarda
        # siempre la canónica en portugués, para que la DB no dependa del país.
        tipo = _tipo_canonico((body.get("tipo_edificacion") or "").strip())
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "body inválido"}, status_code=400)
    if tipo not in _TIPO_CATEGORIA:
        return JSONResponse({"ok": False, "error": f"etiqueta desconocida: {tipo!r}"},
                            status_code=400)
    engine = get_engine()
    with engine.begin() as conn:
        ok, detalle = _descartar_hotel(conn, hotel_id, tipo)
    if not ok:
        return JSONResponse({"ok": False, "error": detalle}, status_code=404)
    return JSONResponse({"ok": True, "tipo_edificacion": tipo,
                         "parcela_etiquetada": bool(detalle)})


def _descartar_hotel(conn, hotel_id: str, tipo: str) -> tuple[bool, object]:
    """Aplica "esto NO es hotel": lo borra de `hoteles`, lo recuerda en `hotel_descartado`,
    etiqueta la parcela y corrige el rubro del comercio homónimo.

    Extraído para que lo compartan el endpoint de la asistencia y el de resolución de
    incidencias (misma transacción del caller). Devuelve (ok, parcela_id | mensaje_error)."""
    cat = _TIPO_CATEGORIA[tipo]
    row = conn.execute(text("""
        SELECT region_id, survey_id::text, cnpj, nombre, parcela_id::text,
               ST_Y(location), ST_X(location)
        FROM hoteles WHERE hotel_id::text = :h"""), {"h": hotel_id}).fetchone()
    if not row:
        return False, "Hotel no encontrado"
    region_id, survey_id, cnpj, nombre, parcela_id, lat, lng = row
    import unicodedata
    # misma normalización que hotel_fetcher._norm (sin acentos, lower) para que el
    # filtro de descarte matchee en la próxima corrida.
    nombre_norm = " ".join("".join(
        c for c in unicodedata.normalize("NFKD", str(nombre or ""))
        if not unicodedata.combining(c)).lower().split())
    # Descarte = memoria de "no es hotel" + punto de comercio a dibujar en su coordenada.
    # HotelFetcher lo lee y filtra en la próxima corrida (por CNPJ si lo hay; si no
    # —Google—, por nombre normalizado + proximidad). El mapa lo dibuja como comercio.
    conn.execute(text("""
        INSERT INTO hotel_descartado (region_id, survey_id, cnpj, nombre, nombre_norm,
                                      lat, lng, tipo_edificacion, categoria, autor)
        VALUES (:r, CAST(:s AS uuid), :c, :nom, :n, :lat, :lng, :t, :cat, 'operador')
    """), {"r": region_id, "s": survey_id, "c": cnpj, "nom": nombre,
           "n": nombre_norm or None, "lat": lat, "lng": lng, "t": tipo, "cat": cat})
    # Etiqueta manual a la parcela (si el hotel estaba vinculado a una).
    if parcela_id:
        conn.execute(text("""
            INSERT INTO parcela_tipo_manual (parcela_id, tipo_edificacion, categoria, autor)
            VALUES (CAST(:p AS uuid), :t, :cat, 'operador')
            ON CONFLICT (parcela_id) DO UPDATE SET
                tipo_edificacion = :t, categoria = :cat, actualizado_at = now()
        """), {"p": parcela_id, "t": tipo, "cat": cat})
    # Si el mismo punto está en `comercios` con rubro 'lodging' (así lo trajo Google),
    # corregirlo para que no cuente como hospedaje en ningún lado.
    if lat is not None and lng is not None:
        conn.execute(text("""
            UPDATE comercios SET rubro = 'comercio'
            WHERE region_id = :r AND rubro ILIKE 'lodging'
              AND location IS NOT NULL
              AND ST_DWithin(location::geography,
                             ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography, 30)
        """), {"r": region_id, "lat": lat, "lng": lng})
    conn.execute(text("DELETE FROM hoteles WHERE hotel_id::text = :h"), {"h": hotel_id})
    return True, parcela_id


# ── Reporte de incidencias (resolución humana) ────────────────────────────────────────
# Centraliza los casos que sólo un humano puede resolver. La asistencia de hoteles quedó
# absorbida como el tipo `hotel_sin_habitaciones`. Ver agents/incidencias_reporter.py.

_INCIDENCIA_ESTADOS = ("pendiente", "resuelta", "descartada", "obsoleta")


@app.get("/incidencias/{survey_id}")
async def incidencias_page(survey_id: str) -> FileResponse:
    """Página del reporte de incidencias (el JS lee el survey_id del path)."""
    return FileResponse(STATIC_DIR / "incidencias.html")


@app.get("/api/surveys/{survey_id}/incidencias")
async def listar_incidencias(survey_id: str, estado: str = Query("pendiente"),
                             tipo: str = Query("")) -> JSONResponse:
    """Incidencias del relevamiento + resumen por tipo/estado (alimenta la página y el banner).

    `estado` acepta un estado concreto o `todas`; `tipo` filtra por tipo (vacío = todos)."""
    engine = get_engine()
    with engine.connect() as conn:
        meta = conn.execute(text(
            "SELECT region_id, baseline_id::text FROM surveys WHERE survey_id = :sid"),
            {"sid": survey_id}).fetchone()
        if not meta:
            return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)

        cond, params = ["survey_id = CAST(:sid AS uuid)"], {"sid": survey_id}
        if estado and estado != "todas":
            cond.append("estado = :estado")
            params["estado"] = estado
        if tipo:
            cond.append("tipo = :tipo")
            params["tipo"] = tipo
        rows = conn.execute(text(f"""
            SELECT incidencia_id::text, tipo, estado, prioridad, titulo, detalle, datos,
                   lat, lng, parcela_id::text, hotel_cnpj, resolucion, nota, autor,
                   creada_at, resuelta_at
            FROM incidencias
            WHERE {' AND '.join(cond)}
            ORDER BY prioridad, tipo, titulo
        """), params).fetchall()

        # Resumen SIEMPRE sobre todo el survey (no sobre el filtro): lo usan los chips.
        por_tipo = conn.execute(text("""
            SELECT tipo, estado, count(*) FROM incidencias
            WHERE survey_id = CAST(:sid AS uuid) GROUP BY tipo, estado
        """), {"sid": survey_id}).fetchall()

    resumen: dict = {"por_tipo": {}, "por_estado": {}}
    for t, e, n in por_tipo:
        resumen["por_tipo"].setdefault(t, {})[e] = n
        resumen["por_estado"][e] = resumen["por_estado"].get(e, 0) + n

    items = [{
        "incidencia_id": r[0], "tipo": r[1], "estado": r[2], "prioridad": r[3],
        "titulo": r[4], "detalle": r[5], "datos": r[6] or {},
        "lat": float(r[7]) if r[7] is not None else None,
        "lng": float(r[8]) if r[8] is not None else None,
        "parcela_id": r[9], "hotel_cnpj": r[10],
        "resolucion": r[11], "nota": r[12], "autor": r[13],
        "creada_at": r[14].isoformat() if r[14] else None,
        "resuelta_at": r[15].isoformat() if r[15] else None,
    } for r in rows]
    return JSONResponse({"ok": True, "survey_id": survey_id, "baseline_id": meta[1],
                         "incidencias": items, "total": len(items),
                         "pendientes": resumen["por_estado"].get("pendiente", 0),
                         "resumen": resumen})


@app.post("/api/surveys/{survey_id}/incidencias/generar")
async def generar_incidencias(survey_id: str) -> JSONResponse:
    """Re-escanea el relevamiento y actualiza las incidencias. Idempotente: preserva el estado
    y la nota de las ya resueltas/descartadas (clave natural)."""
    row = _get_survey_row(survey_id)
    if not row:
        return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
    region_id = row[0]

    from scrapitero.agents.incidencias_reporter import IncidenciasInput
    from scrapitero.agents.incidencias_reporter import run as run_incidencias

    def _job() -> dict:
        _thread_job_id.value = survey_id
        return run_incidencias(IncidenciasInput(region_id=region_id,
                                                survey_id=survey_id)).model_dump()

    data = await asyncio.to_thread(_job)
    return JSONResponse(data, status_code=200 if data.get("ok") else 422)


# Tipo de las incidencias que abre el operador a mano desde el mapa, para lo que ve mal y
# ningún agente detecta. Se importa de `IncidenciasReporter` porque es ese agente el que tiene
# que excluirlo del barrido a obsoleta: si los dos lados no dicen el mismo string, la primera
# corrida del agente marca obsoletas todas las marcas hechas a mano.
from scrapitero.agents.incidencias_reporter import TIPO_MANUAL as TIPO_INCIDENCIA_MANUAL


@app.post("/api/surveys/{survey_id}/incidencias/manual")
async def crear_incidencia_manual(survey_id: str, request: Request) -> JSONResponse:
    """Abre una incidencia a mano sobre una parcela o una coordenada del mapa.

    El operador ve por satélite algo que el relevamiento no refleja (una parcela mal
    clasificada, un edificio que el catastro no trajo) y lo marca para revisar; la corrección
    se hace después desde `/incidencias/{survey_id}` con el mismo editor que el resto.

    Se puede anclar a una `parcela_id` o sólo a una coordenada — este último es el caso del
    marcador del relevamiento ANTERIOR, que es dato histórico y **no se toca**: la incidencia
    guarda la coordenada, no escribe nada en `baseline_direcciones`.
    """
    try:
        body = await request.json()
        parcela_id = (body.get("parcela_id") or "").strip() or None
        nota = (body.get("nota") or "").strip() or None
        lat = body.get("lat")
        lng = body.get("lng")
        lat = float(lat) if lat is not None else None
        lng = float(lng) if lng is not None else None
    except (ValueError, TypeError, AttributeError):
        return JSONResponse({"ok": False, "error": "body inválido"}, status_code=400)
    if (lat is None) != (lng is None):
        return JSONResponse({"ok": False, "error": "lat y lng van juntos"}, status_code=400)
    if not parcela_id and lat is None:
        return JSONResponse({"ok": False, "error": "hace falta una parcela o una coordenada"},
                            status_code=400)

    row = _get_survey_row(survey_id)
    if not row:
        return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
    region_id = row[0]

    engine = get_engine()
    with engine.begin() as conn:
        titulo = "📌 Marcada para revisar"
        if parcela_id:
            p = conn.execute(text("""
                SELECT calle, numero, centroid_lat, centroid_lng FROM parcelas
                WHERE parcela_id = CAST(:p AS uuid) AND survey_id = CAST(:sid AS uuid)
            """), {"p": parcela_id, "sid": survey_id}).fetchone()
            if not p:
                return JSONResponse({"ok": False, "error": "La parcela no es de este survey"},
                                    status_code=404)
            direccion = " ".join(x for x in (p[0], p[1]) if x) or "(sin dirección)"
            titulo = f"📌 {direccion} — marcada para revisar"
            # La coordenada del click gana; si no vino, la del centroide de la parcela.
            if lat is None:
                lat, lng = p[2], p[3]
        # Clave natural: una marca por parcela (o por punto), para que marcar dos veces lo
        # mismo actualice la nota en vez de acumular tarjetas duplicadas.
        clave = f"manual:{parcela_id}" if parcela_id else f"manual:{lat:.6f},{lng:.6f}"
        r = conn.execute(text("""
            INSERT INTO incidencias (incidencia_id, region_id, survey_id, tipo, clave, estado,
                                     prioridad, titulo, detalle, lat, lng, parcela_id, autor)
            VALUES (gen_random_uuid(), :rid, CAST(:sid AS uuid), :tipo, :clave, 'pendiente',
                    1, :titulo, :nota, :lat, :lng, CAST(:pid AS uuid), 'operador')
            ON CONFLICT (survey_id, tipo, clave) DO UPDATE SET
                estado = 'pendiente', titulo = EXCLUDED.titulo, detalle = EXCLUDED.detalle,
                lat = EXCLUDED.lat, lng = EXCLUDED.lng,
                resolucion = NULL, resuelta_at = NULL, actualizada_at = now()
            RETURNING incidencia_id::text
        """), {"rid": region_id, "sid": survey_id, "tipo": TIPO_INCIDENCIA_MANUAL,
               "clave": clave, "titulo": titulo, "nota": nota,
               "lat": lat, "lng": lng, "pid": parcela_id}).fetchone()
    return JSONResponse({"ok": True, "incidencia_id": r[0], "tipo": TIPO_INCIDENCIA_MANUAL,
                         "url": f"/incidencias/{survey_id}?inc={r[0]}"})


@app.post("/api/incidencias/{incidencia_id}/reabrir")
async def reabrir_incidencia(incidencia_id: str) -> JSONResponse:
    """Vuelve una incidencia resuelta/descartada a `pendiente` para poder rehacerla.

    Una resolución puede estar mal (se cargó un número equivocado, se descartó algo que sí
    era un problema), así que el estado no puede ser un camino de una sola dirección. Se
    conserva la `nota` como historial y se limpia la resolución.

    OJO: **no deshace el efecto** de la resolución sobre los datos — los overrides
    (`hotel_habitaciones_manual`, `parcela_tipo_manual`, `parcela_uf_manual`) siguen
    aplicados, y `no_es_hotel` ya borró el hotel. Reabrir habilita volver a resolver con el
    valor correcto (los upserts pisan el anterior); para el falso descarte de un hotel hay
    que re-correr 🏨 tras limpiar `hotel_descartado`."""
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(text("""
            UPDATE incidencias
               SET estado = 'pendiente', resolucion = NULL, resuelta_at = NULL,
                   actualizada_at = now()
             WHERE incidencia_id = CAST(:i AS uuid)
            RETURNING tipo, nota
        """), {"i": incidencia_id}).fetchone()
    if not row:
        return JSONResponse({"ok": False, "error": "Incidencia no encontrada"}, status_code=404)
    return JSONResponse({"ok": True, "estado": "pendiente", "tipo": row[0], "nota": row[1]})


# Campos de la dirección que el operador puede corregir desde una incidencia. El valor va a
# `parcelas` (dato vigente del relevamiento) + `parcela_direccion_manual` (respaldo durable).
_CAMPOS_DIRECCION = ("calle", "numero", "complemento", "barrio", "codigo_postal")

# Confianza mínima para que un número ESTIMADO salga como número de la dirección en los
# entregables. Por debajo, la parcela va al panel de incidencias (`numero_faltante`) para que
# un humano cargue la altura. El corte en 0,4 deja afuera justo las extrapolaciones más allá
# del último ancla de la calle, que es donde `NumeroEstimator` mide peor.
# MISMO valor que `IncidenciasInput.numero_conf_min` — si cambia uno, cambiar el otro, o
# quedan parcelas sin número en el CSV y sin incidencia que las reclame.
_NUMERO_CONF_MIN = 0.4


@app.get("/api/parcelas/{parcela_id}")
async def get_parcela_editable(parcela_id: str) -> JSONResponse:
    """Valores actuales de una parcela para precargar el formulario de corrección.

    Devuelve la dirección + las variables derivadas que también se editan desde la tarjeta
    (tipo de edificación y UF), para que el operador vea qué está cambiando."""
    engine = get_engine()
    with engine.connect() as conn:
        r = conn.execute(text("""
            SELECT p.parcela_id::text, p.cca_code, p.calle, p.numero, p.complemento, p.barrio,
                   p.codigo_postal, p.uso_principal, p.uf_vivienda, p.uf_comercio, p.uf_fuente,
                   p.direccion_source, p.numero_estimado, p.numero_estimado_metodo,
                   p.area_m2_terreno, p.area_m2_construida,
                   ptm.tipo_edificacion,
                   COALESCE((SELECT h.tipo FROM hoteles h
                       WHERE h.parcela_id = p.parcela_id AND NOT h.cerrado_def
                       ORDER BY h.habitaciones DESC NULLS LAST LIMIT 1), '') AS hotel_tipo,
                   p.descripcion_uso,
                   -- al final a propósito: el resto se lee por índice posicional
                   p.centroid_lat, p.centroid_lng, p.ubicacion_source, p.uso_fuente,
                   COALESCE(reg.country_code, '') AS country_code
            FROM parcelas p
            LEFT JOIN parcela_tipo_manual ptm ON ptm.parcela_id = p.parcela_id
            LEFT JOIN regions reg ON reg.region_id = p.region_id
            WHERE p.parcela_id = CAST(:p AS uuid)
        """), {"p": parcela_id}).fetchone()
    if not r:
        return JSONResponse({"ok": False, "error": "Parcela no encontrada"}, status_code=404)
    uf_v, uf_c = r[8], r[9]
    return JSONResponse({
        "ok": True, "parcela_id": r[0], "cca_code": r[1] or "",
        "calle": r[2] or "", "numero": r[3] or "", "complemento": r[4] or "",
        "barrio": r[5] or "", "codigo_postal": r[6] or "",
        "uso_principal": r[7] or "", "uf_vivienda": uf_v, "uf_comercio": uf_c,
        "uf_fuente": r[10] or "", "direccion_source": r[11] or "",
        "numero_estimado": r[12] or "", "numero_estimado_metodo": r[13] or "",
        # el tipo que efectivamente muestra la web (override manual > hotel > CNPJ > catastro),
        # traducido al idioma del relevamiento (r[23]=país) igual que en el mapa y el CSV
        "tipo_edificacion": _tipo_localizado(
            _tipo_edificacion(r[7], uf_v, r[15], r[18], r[17], r[16]), r[23]) or "",
        "tipo_manual": _tipo_localizado(r[16], r[23]) or "",
        "lat": float(r[19]) if r[19] is not None else None,
        "lng": float(r[20]) if r[20] is not None else None,
        "ubicacion_source": r[21] or "", "uso_fuente": r[22] or "",
    })


def _guardar_direccion_manual(conn, parcela_id: str, campos: dict, nota: Optional[str]) -> int:
    """Aplica la corrección de dirección a `parcelas` y la respalda en `parcela_direccion_manual`.

    Sólo toca los campos presentes en `campos` (los vacíos = "no lo toqué"). Marca
    `direccion_source='manual'` para no perder el lineage: la dirección dejó de ser la que
    publicó el municipio. Devuelve la cantidad de campos aplicados."""
    campos = {k: v for k, v in campos.items() if k in _CAMPOS_DIRECCION and v is not None}
    if not campos:
        return 0
    sets = ", ".join(f"{k} = :{k}" for k in campos)
    conn.execute(text(f"UPDATE parcelas SET {sets}, direccion_source = 'manual' "
                      f"WHERE parcela_id = CAST(:p AS uuid)"),
                 {**campos, "p": parcela_id})
    row = conn.execute(text(
        "SELECT region_id, cca_code FROM parcelas WHERE parcela_id = CAST(:p AS uuid)"),
        {"p": parcela_id}).fetchone()
    # Respaldo durable por inscrição: sobrevive al re-scrape del BCI, donde el parcela_id
    # cambia. Sin cca_code la corrección igual queda aplicada en la parcela.
    if row and row[1]:
        cols = ", ".join(campos)
        vals = ", ".join(f":{k}" for k in campos)
        upd = ", ".join(f"{k} = :{k}" for k in campos)
        conn.execute(text(f"""
            INSERT INTO parcela_direccion_manual
                (region_id, cca_code, {cols}, nota, autor, parcela_id)
            VALUES (:r, :c, {vals}, :nota, 'operador', CAST(:p AS uuid))
            ON CONFLICT (region_id, cca_code) DO UPDATE SET
                {upd}, nota = :nota, parcela_id = CAST(:p AS uuid), actualizado_at = now()
        """), {**campos, "r": row[0], "c": row[1], "nota": nota, "p": parcela_id})
    return len(campos)


# Usos que el operador puede fijar a mano. Misma taxonomía que escriben los agentes
# (`uso_classifier`, `bci_parser`) y que colorea el mapa en `categoriaMapa`.
_USOS_VALIDOS = ("residencial", "comercial", "mixto", "industrial", "equipamiento", "vacante")


def _guardar_uso_manual(conn, parcela_id: str, uso: str, nota: Optional[str]) -> int:
    """Fija el uso de una parcela a mano y lo respalda en `parcela_uso_manual`.

    `uso_principal` gobierna casi todo lo derivado —el color del círculo en el mapa, el CSV, el
    reparto de habitantes de `dasymetric_population` y la estimación de UF—, así que corregir
    sólo la etiqueta 🏢 (`parcela_tipo_manual`) dejaba el resto mal. `uso_fuente='manual'` es el
    sello que impide que la próxima corrida lo pise. Devuelve 1 si cambió algo, 0 si no."""
    prev = conn.execute(text(
        "SELECT region_id, cca_code, uso_principal, uso_fuente FROM parcelas "
        "WHERE parcela_id = CAST(:p AS uuid)"), {"p": parcela_id}).fetchone()
    if not prev:
        return 0
    if (prev[2] or "") == uso and (prev[3] or "") == "manual":
        return 0                                     # ya estaba así a mano → nada que hacer
    conn.execute(text("UPDATE parcelas SET uso_principal = :u, uso_fuente = 'manual' "
                      "WHERE parcela_id = CAST(:p AS uuid)"), {"u": uso, "p": parcela_id})
    # Respaldo durable por inscrição: sobrevive al re-scrape, donde el parcela_id cambia.
    if prev[1]:
        conn.execute(text("""
            INSERT INTO parcela_uso_manual
                (region_id, cca_code, uso_principal, uso_previo, uso_fuente_previa,
                 nota, autor, parcela_id)
            VALUES (:r, :c, :u, :prev_u, :prev_f, :nota, 'operador', CAST(:p AS uuid))
            ON CONFLICT (region_id, cca_code) DO UPDATE SET
                uso_principal = :u, nota = :nota, parcela_id = CAST(:p AS uuid),
                actualizado_at = now()
        """), {"r": prev[0], "c": prev[1], "u": uso, "prev_u": prev[2], "prev_f": prev[3],
               "nota": nota, "p": parcela_id})
    return 1


def _guardar_ubicacion_manual(conn, parcela_id: str, lat: float, lng: float,
                              nota: Optional[str]) -> int:
    """Mueve el punto de una parcela y lo respalda en `parcela_ubicacion_manual`.

    Toca `centroid_lat/lng` —que es lo que dibuja el mapa y sale al CSV— y **no** `geometry`:
    el polígono es del catastro y sigue siendo el dato oficial; lo que el operador corrige es
    el punto representativo. `ubicacion_source='manual'` es el sello que impide que la próxima
    corrida de SmartGIS lo pise. Devuelve 1 si movió algo, 0 si la coordenada no cambió."""
    prev = conn.execute(text(
        "SELECT region_id, cca_code, centroid_lat, centroid_lng FROM parcelas "
        "WHERE parcela_id = CAST(:p AS uuid)"), {"p": parcela_id}).fetchone()
    if not prev:
        return 0
    lat_prev = float(prev[2]) if prev[2] is not None else None
    lng_prev = float(prev[3]) if prev[3] is not None else None
    # ~0,1 m: por debajo de eso es el redondeo del arrastre, no una corrección.
    if (lat_prev is not None and lng_prev is not None
            and abs(lat - lat_prev) < 1e-6 and abs(lng - lng_prev) < 1e-6):
        return 0
    conn.execute(text("""
        UPDATE parcelas SET centroid_lat = :lat, centroid_lng = :lng,
               ubicacion_source = 'manual'
        WHERE parcela_id = CAST(:p AS uuid)
    """), {"lat": lat, "lng": lng, "p": parcela_id})
    if prev[1]:
        conn.execute(text("""
            INSERT INTO parcela_ubicacion_manual
                (region_id, cca_code, lat, lng, lat_previa, lng_previa, nota, autor, parcela_id)
            VALUES (:r, :c, :lat, :lng, :latp, :lngp, :nota, 'operador', CAST(:p AS uuid))
            ON CONFLICT (region_id, cca_code) DO UPDATE SET
                lat = :lat, lng = :lng, nota = :nota,
                parcela_id = CAST(:p AS uuid), actualizado_at = now()
        """), {"r": prev[0], "c": prev[1], "lat": lat, "lng": lng,
               "latp": lat_prev, "lngp": lng_prev, "nota": nota, "p": parcela_id})
    return 1


@app.get("/api/baseline-direcciones/{direccion_id}")
async def get_baseline_direccion(direccion_id: str) -> JSONResponse:
    """Valores actuales de una dirección del relevamiento anterior, para precargar el editor.

    Es el equivalente de `/api/parcelas/{id}` para los casos que no tienen parcela del
    relevamiento nuevo (`geocoding_dudoso`)."""
    engine = get_engine()
    with engine.connect() as conn:
        r = conn.execute(text("""
            SELECT direccion_raw, calle, numero, uso, uf_vivienda, uf_comercio,
                   tipo_edificacion, lat, lng, geocode_source
            FROM baseline_direcciones WHERE id = :i
        """), {"i": direccion_id}).fetchone()
    if not r:
        return JSONResponse({"ok": False, "error": "Dirección no encontrada"}, status_code=404)
    return JSONResponse({
        "ok": True, "direccion_raw": r[0] or "", "calle": r[1] or "", "numero": r[2] or "",
        "uso": r[3] or "", "uf_vivienda": r[4], "uf_comercio": r[5],
        # sin etiqueta cargada a mano se ofrece la que se deduce del uso, para no arrancar vacío
        "tipo_edificacion": r[6] or _tipo_edificacion(r[3], r[4], 1, None, None),
        "tipo_manual": r[6] or "",
        "lat": float(r[7]) if r[7] is not None else None,
        "lng": float(r[8]) if r[8] is not None else None,
        "geocode_source": r[9] or "",
    })


def _guardar_datos_baseline(conn, baseline_direccion_id, tipo: Optional[str],
                            uf_v: Optional[int], uf_c: Optional[int]) -> int:
    """Etiqueta y UF de una dirección del relevamiento anterior.

    `uso` NO se escribe a mano: se **deriva** de las UF resultantes con la misma regla que la
    importación (`_agregar_por_direccion`), porque es lo que consumen la comparativa y el CSV.
    Si se tocan sólo las UF, la etiqueta queda como estaba, y viceversa."""
    sets, params = [], {"i": baseline_direccion_id}
    if tipo:
        sets.append("tipo_edificacion = :t")
        params["t"] = tipo
    if uf_v is not None or uf_c is not None:
        sets += ["uf_vivienda = :uv", "uf_comercio = :uc",
                 "uso = CASE WHEN :uv > 0 AND :uc > 0 THEN 'mixto' "
                 "WHEN :uc > 0 THEN 'comercial' ELSE 'residencial' END"]
        params["uv"], params["uc"] = uf_v or 0, uf_c or 0
    if not sets:
        return 0
    r = conn.execute(text(f"UPDATE baseline_direcciones SET {', '.join(sets)} WHERE id = :i"),
                     params)
    return r.rowcount or 0


def _guardar_ubicacion_baseline(conn, baseline_direccion_id, lat: float, lng: float) -> int:
    """Mueve una dirección del relevamiento ANTERIOR (caso `geocoding_dudoso`).

    No necesita tabla de respaldo: `baseline_direcciones` ES el registro durable del CSV del
    cliente. `geocode_source='manual'` con confianza 1 lo blinda del re-geocoding — el
    geocoder sólo procesa filas sin coordenada, y `baseline_interp` respeta las fuentes
    exactas."""
    r = conn.execute(text("""
        UPDATE baseline_direcciones
        SET lat = :lat, lng = :lng, geocode_source = 'manual', geocode_confidence = 1.0
        WHERE id = :i
    """), {"lat": lat, "lng": lng, "i": baseline_direccion_id})
    return r.rowcount or 0


def _coords_validas(v: dict) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """Lee lat/lng del payload. Devuelve (lat, lng, error)."""
    if v.get("lat") in (None, "") or v.get("lng") in (None, ""):
        return None, None, None
    try:
        lat, lng = float(v["lat"]), float(v["lng"])
    except (ValueError, TypeError):
        return None, None, "coordenadas inválidas"
    if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
        return None, None, "coordenadas fuera de rango"
    return lat, lng, None


@app.post("/api/incidencias/{incidencia_id}/resolver")
async def resolver_incidencia(incidencia_id: str, request: Request) -> JSONResponse:
    """Aplica la resolución del operador y cierra la incidencia.

    `accion` despacha a la escritura correspondiente, reusando las tablas de override que ya
    existen para que un re-scrape no pierda la corrección:
      - `habitaciones`      → `hoteles` + `hotel_habitaciones_manual` (durable por CNPJ)
      - `cerrado`           → helper `_marcar_cerrado` (+ `hotel_cerrado_manual`, `valor`=nota)
      - `no_es_hotel`       → helper `_descartar_hotel` (4 tablas)
      - `tipo_edificacion`  → `parcela_tipo_manual`
      - `uf`                → `parcelas` (uf_fuente='manual') + `parcela_uf_manual`
      - `descartar`         → no cambia datos, sólo cierra el caso con nota

    `direccion`/`ubicacion` es el formulario completo y también aplica `uso_principal`
    (`parcelas` con uso_fuente='manual' + `parcela_uso_manual`), que es de donde derivan la
    etiqueta de edificación y la estimación de UF.
    """
    try:
        body = await request.json()
        accion = (body.get("accion") or "").strip()
        valor = body.get("valor")
        nota = (body.get("nota") or "").strip() or None
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "body inválido"}, status_code=400)

    engine = get_engine()
    with engine.begin() as conn:
        inc = conn.execute(text("""
            SELECT tipo, parcela_id::text, datos FROM incidencias
            WHERE incidencia_id = CAST(:i AS uuid)"""), {"i": incidencia_id}).fetchone()
        if not inc:
            return JSONResponse({"ok": False, "error": "Incidencia no encontrada"},
                                status_code=404)
        _tipo, parcela_id, datos = inc[0], inc[1], (inc[2] or {})
        estado_final = "resuelta"

        if accion == "habitaciones":
            hotel_id = datos.get("hotel_id")
            if not hotel_id:
                return JSONResponse({"ok": False, "error": "la incidencia no tiene hotel_id"},
                                    status_code=400)
            try:
                n = int(valor)
            except (ValueError, TypeError):
                return JSONResponse({"ok": False, "error": "habitaciones inválido"},
                                    status_code=400)
            if n < 0:
                return JSONResponse({"ok": False, "error": "habitaciones debe ser ≥ 0"},
                                    status_code=400)
            h = conn.execute(text(
                "SELECT region_id, cnpj FROM hoteles WHERE hotel_id::text = :h"),
                {"h": hotel_id}).fetchone()
            if not h:
                return JSONResponse({"ok": False, "error": "El hotel ya no existe (re-corré 🏨)"},
                                    status_code=409)
            conn.execute(text("UPDATE hoteles SET habitaciones = :n, "
                              "habitaciones_fuente = 'manual' WHERE hotel_id::text = :h"),
                         {"n": n, "h": hotel_id})
            if h[1]:
                conn.execute(text("""
                    INSERT INTO hotel_habitaciones_manual (region_id, cnpj, habitaciones, autor)
                    VALUES (:r, :c, :n, 'operador')
                    ON CONFLICT (region_id, cnpj)
                    DO UPDATE SET habitaciones = :n, actualizado_at = now()
                """), {"r": h[0], "c": h[1], "n": n})

        elif accion == "cerrado":
            # El hotel SÍ es un hotel, pero ya no opera. A diferencia de `no_es_hotel` la fila
            # se conserva (pin rojo en el mapa) y `valor` lleva el comentario que explica qué
            # hay hoy en su lugar.
            hotel_id = datos.get("hotel_id")
            if not hotel_id:
                return JSONResponse({"ok": False, "error": "la incidencia no tiene hotel_id"},
                                    status_code=400)
            comentario = (str(valor or "")).strip() or nota
            ok, detalle = _marcar_cerrado(conn, hotel_id, True, comentario)
            if not ok:
                return JSONResponse({"ok": False, "error": detalle}, status_code=409)

        elif accion == "no_es_hotel":
            tipo_ed = _tipo_canonico((str(valor or "")).strip())
            if tipo_ed not in _TIPO_CATEGORIA:
                return JSONResponse({"ok": False, "error": f"etiqueta desconocida: {tipo_ed!r}"},
                                    status_code=400)
            hotel_id = datos.get("hotel_id")
            if not hotel_id:
                return JSONResponse({"ok": False, "error": "la incidencia no tiene hotel_id"},
                                    status_code=400)
            ok, detalle = _descartar_hotel(conn, hotel_id, tipo_ed)
            if not ok:
                return JSONResponse({"ok": False, "error": detalle}, status_code=409)

        elif accion == "tipo_edificacion":
            tipo_ed = _tipo_canonico((str(valor or "")).strip())
            if tipo_ed not in _TIPO_CATEGORIA:
                return JSONResponse({"ok": False, "error": f"etiqueta desconocida: {tipo_ed!r}"},
                                    status_code=400)
            if not parcela_id:
                return JSONResponse({"ok": False, "error": "la incidencia no tiene parcela"},
                                    status_code=400)
            conn.execute(text("""
                INSERT INTO parcela_tipo_manual (parcela_id, tipo_edificacion, categoria, autor)
                VALUES (CAST(:p AS uuid), :t, :cat, 'operador')
                ON CONFLICT (parcela_id) DO UPDATE SET
                    tipo_edificacion = :t, categoria = :cat, actualizado_at = now()
            """), {"p": parcela_id, "t": tipo_ed, "cat": _TIPO_CATEGORIA[tipo_ed]})

        elif accion == "uf":
            if not parcela_id:
                return JSONResponse({"ok": False, "error": "la incidencia no tiene parcela"},
                                    status_code=400)
            v = valor if isinstance(valor, dict) else {}
            try:
                uf_v = int(v.get("uf_vivienda"))
                uf_c = int(v.get("uf_comercio"))
            except (ValueError, TypeError):
                return JSONResponse({"ok": False, "error": "uf_vivienda/uf_comercio inválidos"},
                                    status_code=400)
            if uf_v < 0 or uf_c < 0:
                return JSONResponse({"ok": False, "error": "las UF deben ser ≥ 0"},
                                    status_code=400)
            conn.execute(text("""
                UPDATE parcelas SET uf_vivienda = :uv, uf_comercio = :uc,
                       unidades_funcionales_estimadas = :ut, uf_fuente = 'manual'
                WHERE parcela_id = CAST(:p AS uuid)
            """), {"uv": uf_v, "uc": uf_c, "ut": uf_v + uf_c, "p": parcela_id})
            conn.execute(text("""
                INSERT INTO parcela_uf_manual (parcela_id, uf_vivienda, uf_comercio, autor)
                VALUES (CAST(:p AS uuid), :uv, :uc, 'operador')
                ON CONFLICT (parcela_id) DO UPDATE SET
                    uf_vivienda = :uv, uf_comercio = :uc, actualizado_at = now()
            """), {"p": parcela_id, "uv": uf_v, "uc": uf_c})

        elif accion == "numero":
            # Atajo de las tarjetas `numero_faltante`: cargar sólo el número. Misma escritura
            # que `direccion` — el número corregido a mano ES la dirección vigente y tiene que
            # llegar al CSV, al CSV Operadora y al apareo contra el relevamiento anterior.
            num = str(valor or "").strip()[:20]
            if not num or not any(ch.isdigit() for ch in num):
                return JSONResponse({"ok": False, "error": "número inválido"}, status_code=400)
            if not parcela_id:
                return JSONResponse({"ok": False, "error": "la incidencia no tiene parcela"},
                                    status_code=400)
            _guardar_direccion_manual(conn, parcela_id, {"numero": num}, nota)

        elif accion in ("direccion", "ubicacion"):
            # UNA sola escritura con TODO lo editable de la ubicación: dirección, tipo, UF,
            # coordenada y —si el caso es de hotel— habitaciones. La página manda el formulario
            # entero con un único botón de guardar; acá se aplica campo por campo y cada
            # concepto va a SU tabla de override (dirección → parcela_direccion_manual, tipo →
            # parcela_tipo_manual, UF → parcela_uf_manual, coordenada → parcela_ubicacion_manual,
            # habitaciones → hotel_habitaciones_manual). Así no hay dos lugares donde se guarde
            # lo mismo y un re-scrape puede re-aplicar todo.
            # (`direccion` se acepta como alias histórico: es la resolución que quedó guardada
            # en las incidencias cerradas antes de que el formulario incluyera la coordenada.)
            v = valor if isinstance(valor, dict) else {}
            lat, lng, err_coord = _coords_validas(v)
            if err_coord:
                return JSONResponse({"ok": False, "error": err_coord}, status_code=400)

            aplicados = 0
            hotel_id = datos.get("hotel_id")

            # ── Habitaciones (sólo casos con hotel) ──────────────────────────────────────
            if v.get("habitaciones") not in (None, ""):
                if not hotel_id:
                    return JSONResponse({"ok": False, "error": "la incidencia no tiene hotel"},
                                        status_code=400)
                try:
                    n_hab = int(v["habitaciones"])
                except (ValueError, TypeError):
                    return JSONResponse({"ok": False, "error": "habitaciones inválido"},
                                        status_code=400)
                if n_hab < 0:
                    return JSONResponse({"ok": False, "error": "habitaciones debe ser ≥ 0"},
                                        status_code=400)
                h = conn.execute(text(
                    "SELECT region_id, cnpj FROM hoteles WHERE hotel_id::text = :h"),
                    {"h": hotel_id}).fetchone()
                if not h:
                    return JSONResponse({"ok": False, "error": "El hotel ya no existe (re-corré 🏨)"},
                                        status_code=409)
                conn.execute(text("UPDATE hoteles SET habitaciones = :n, "
                                  "habitaciones_fuente = 'manual' WHERE hotel_id::text = :h"),
                             {"n": n_hab, "h": hotel_id})
                if h[1]:
                    conn.execute(text("""
                        INSERT INTO hotel_habitaciones_manual (region_id, cnpj, habitaciones, autor)
                        VALUES (:r, :c, :n, 'operador')
                        ON CONFLICT (region_id, cnpj)
                        DO UPDATE SET habitaciones = :n, actualizado_at = now()
                    """), {"r": h[0], "c": h[1], "n": n_hab})
                aplicados += 1

            # ── La coordenada, al objeto que corresponda ─────────────────────────────────
            # Tres destinos según qué es la ubicación del caso, en orden de especificidad:
            # el hotel (mueve el pin y re-vincula parcela), la parcela (mueve su punto), o la
            # dirección del relevamiento anterior (`geocoding_dudoso`, que hasta ahora no
            # ofrecía NINGUNA acción: se veía el problema y no se podía arreglar).
            if lat is not None:
                if hotel_id:
                    ok_h, det = _mover_hotel(conn, hotel_id, lat, lng, None, nota)
                    if not ok_h:
                        return JSONResponse({"ok": False, "error": det}, status_code=409)
                    aplicados += 1
                elif parcela_id:
                    aplicados += _guardar_ubicacion_manual(conn, parcela_id, lat, lng, nota)
                elif datos.get("baseline_direccion_id"):
                    if not _guardar_ubicacion_baseline(conn, datos["baseline_direccion_id"],
                                                       lat, lng):
                        return JSONResponse({"ok": False, "error": "la dirección del relevamiento "
                                                                  "anterior ya no existe"},
                                            status_code=409)
                    aplicados += 1
                else:
                    return JSONResponse({"ok": False, "error": "el caso no tiene a qué objeto "
                                                              "aplicarle la coordenada"},
                                        status_code=400)
                conn.execute(text("""
                    UPDATE incidencias SET lat = :lat, lng = :lng
                    WHERE incidencia_id = CAST(:i AS uuid)
                """), {"lat": lat, "lng": lng, "i": incidencia_id})

            # ── Sin parcela: etiqueta y UF van a la dirección del relevamiento anterior ──
            if not parcela_id:
                bid = datos.get("baseline_direccion_id")
                tipo_ed = _tipo_canonico((str(v.get("tipo_edificacion") or "")).strip())
                hay_uf = (v.get("uf_vivienda") not in (None, "")
                          or v.get("uf_comercio") not in (None, ""))
                if bid and (tipo_ed or hay_uf):
                    if tipo_ed and tipo_ed not in _TIPO_CATEGORIA:
                        return JSONResponse({"ok": False, "error": f"etiqueta desconocida: {tipo_ed!r}"},
                                            status_code=400)
                    uf_v = uf_c = None
                    if hay_uf:
                        try:
                            uf_v = int(v.get("uf_vivienda") or 0)
                            uf_c = int(v.get("uf_comercio") or 0)
                        except (ValueError, TypeError):
                            return JSONResponse({"ok": False, "error": "uf_vivienda/uf_comercio inválidos"},
                                                status_code=400)
                        if uf_v < 0 or uf_c < 0:
                            return JSONResponse({"ok": False, "error": "las UF deben ser ≥ 0"},
                                                status_code=400)
                    aplicados += _guardar_datos_baseline(conn, bid, tipo_ed or None, uf_v, uf_c)
                if not aplicados:
                    return JSONResponse(
                        {"ok": False, "error": "esta incidencia no tiene parcela: se pueden "
                                               "corregir su ubicación, su etiqueta y sus UF (y, "
                                               "si es un hotel, sus habitaciones)"},
                        status_code=400)
                conn.execute(text("""
                    UPDATE incidencias SET estado = 'resuelta', resolucion = :res, nota = :nota,
                           autor = 'operador', resuelta_at = now(), actualizada_at = now()
                    WHERE incidencia_id = CAST(:i AS uuid)
                """), {"res": accion, "nota": nota, "i": incidencia_id})
                return JSONResponse({"ok": True, "estado": "resuelta", "accion": accion,
                                     "aplicados": aplicados})

            campos = {k: str(v[k]).strip()[:200] for k in _CAMPOS_DIRECCION
                      if v.get(k) is not None and str(v[k]).strip() != ""}
            if campos.get("numero") and not any(c.isdigit() for c in campos["numero"]):
                return JSONResponse({"ok": False, "error": "número inválido"}, status_code=400)
            aplicados += _guardar_direccion_manual(conn, parcela_id, campos, nota)

            # El uso va ANTES de la etiqueta y las UF a propósito: es el campo del que dependen
            # los otros dos (`_tipo_edificacion` deriva de él, y `UnidadesEstimator` lo toma
            # como input), así que si el operador manda los tres, el uso es el que manda.
            uso = (str(v.get("uso_principal") or "")).strip().lower()
            if uso:
                if uso not in _USOS_VALIDOS:
                    return JSONResponse({"ok": False, "error": f"uso desconocido: {uso!r}"},
                                        status_code=400)
                aplicados += _guardar_uso_manual(conn, parcela_id, uso, nota)

            tipo_ed = _tipo_canonico((str(v.get("tipo_edificacion") or "")).strip())
            if tipo_ed:
                if tipo_ed not in _TIPO_CATEGORIA:
                    return JSONResponse({"ok": False, "error": f"etiqueta desconocida: {tipo_ed!r}"},
                                        status_code=400)
                conn.execute(text("""
                    INSERT INTO parcela_tipo_manual (parcela_id, tipo_edificacion, categoria, autor)
                    VALUES (CAST(:p AS uuid), :t, :cat, 'operador')
                    ON CONFLICT (parcela_id) DO UPDATE SET
                        tipo_edificacion = :t, categoria = :cat, actualizado_at = now()
                """), {"p": parcela_id, "t": tipo_ed, "cat": _TIPO_CATEGORIA[tipo_ed]})
                aplicados += 1

            if v.get("uf_vivienda") not in (None, "") or v.get("uf_comercio") not in (None, ""):
                try:
                    uf_v = int(v.get("uf_vivienda") or 0)
                    uf_c = int(v.get("uf_comercio") or 0)
                except (ValueError, TypeError):
                    return JSONResponse({"ok": False, "error": "uf_vivienda/uf_comercio inválidos"},
                                        status_code=400)
                if uf_v < 0 or uf_c < 0:
                    return JSONResponse({"ok": False, "error": "las UF deben ser ≥ 0"},
                                        status_code=400)
                conn.execute(text("""
                    UPDATE parcelas SET uf_vivienda = :uv, uf_comercio = :uc,
                           unidades_funcionales_estimadas = :ut, uf_fuente = 'manual'
                    WHERE parcela_id = CAST(:p AS uuid)
                """), {"uv": uf_v, "uc": uf_c, "ut": uf_v + uf_c, "p": parcela_id})
                conn.execute(text("""
                    INSERT INTO parcela_uf_manual (parcela_id, uf_vivienda, uf_comercio, autor)
                    VALUES (CAST(:p AS uuid), :uv, :uc, 'operador')
                    ON CONFLICT (parcela_id) DO UPDATE SET
                        uf_vivienda = :uv, uf_comercio = :uc, actualizado_at = now()
                """), {"p": parcela_id, "uv": uf_v, "uc": uf_c})
                aplicados += 1

            if not aplicados:
                return JSONResponse({"ok": False, "error": "no enviaste ningún cambio"},
                                    status_code=400)

        elif accion == "descartar":
            estado_final = "descartada"

        else:
            return JSONResponse({"ok": False, "error": f"acción desconocida: {accion!r}"},
                                status_code=400)

        conn.execute(text("""
            UPDATE incidencias SET estado = :est, resolucion = :res, nota = :nota,
                   autor = 'operador', resuelta_at = now(), actualizada_at = now()
            WHERE incidencia_id = CAST(:i AS uuid)
        """), {"est": estado_final, "res": accion, "nota": nota, "i": incidencia_id})

    return JSONResponse({"ok": True, "estado": estado_final, "accion": accion})


@app.get("/api/surveys/{survey_id}/comercios-marcados")
async def comercios_marcados(survey_id: str) -> JSONResponse:
    """Puntos que el operador reclasificó de falso-hotel a comercio (`hotel_descartado`), para
    dibujarlos en el mapa con color/etiqueta de comercio en su coordenada real. Sin parcela:
    viven en su propia coordenada. Scope: el survey + los de la región sin survey (compartidos).

    Los descartes rotulados con una etiqueta de HOSPEDAJE se excluyen: no significan "esto no
    es un hotel" sino "es un hotel ya contado en OTRO registro" (duplicado). El caso que lo
    obliga es el duplicado SIN CNPJ: `_esta_descartado` sólo lo reconoce por nombre normalizado
    + proximidad, así que hay que guardarle la coordenada sí o sí — y sin este filtro esa
    coordenada volvía a dibujar, como comercio, el mismo punto que el descarte vino a sacar."""
    engine = get_engine()
    with engine.connect() as conn:
        region = conn.execute(text(
            "SELECT region_id FROM surveys WHERE survey_id = :s"), {"s": survey_id}).scalar()
        if region is None:
            return JSONResponse({"ok": False, "error": "Survey no encontrado"}, status_code=404)
        rows = conn.execute(text("""
            SELECT nombre, tipo_edificacion, categoria, lat, lng, cnpj
            FROM hotel_descartado
            WHERE region_id = :r AND lat IS NOT NULL AND lng IS NOT NULL
              AND (survey_id = CAST(:s AS uuid) OR survey_id IS NULL)
              AND COALESCE(tipo_edificacion, '') <> ALL(:hospedaje)
            ORDER BY nombre
        """), {"r": region, "s": survey_id,
               "hospedaje": list(_TIPOS_HOSPEDAJE)}).fetchall()
    puntos = [{"nombre": r[0], "tipo_edificacion": r[1], "categoria": r[2],
               "lat": float(r[3]), "lng": float(r[4]), "cnpj": r[5]} for r in rows]
    return JSONResponse({"ok": True, "puntos": puntos, "total": len(puntos)})


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


def _letra_secuencial(i: int) -> str:
    """0→A, 1→B, …, 25→Z, 26→AA, 27→AB… (estilo columnas de planilla)."""
    s = ""
    i += 1
    while i > 0:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _letras_direccion_repetida(items) -> dict:
    """Para direcciones (calle+número) REPETIDAS, asigna una letra secuencial (A, B, C…) a cada
    parcela SIN complemento propio, para identificar la vivienda. Las que ya traen complemento
    (p.ej. el BCI dio ED/BLOCO/APTO) NO reciben letra: se conserva su complemento.

    `items`: iterable de (id, calle, numero, complemento, lat, lng). Devuelve {id: letra}. El
    orden es determinista (por posición) para que la letra de cada vivienda sea estable."""
    import re
    from collections import defaultdict
    from scrapitero.agents.direccion_norm import normalizar_calle
    grupos: dict = defaultdict(list)
    for it in items:
        cn = normalizar_calle(it[1] or "")
        if not cn:
            continue                      # sin calle → no se agrupa
        # clave = calle + dígitos del número (conserva "0" y el "sin número"; dos parcelas que
        # muestran la misma calle+número —incluido 0 o vacío— cuentan como dirección repetida).
        nn = re.sub(r"\D", "", str(it[2] or ""))
        grupos[(cn, nn)].append(it)
    out: dict = {}
    for its in grupos.values():
        if len(its) <= 1:
            continue                      # dirección única → no hay repetición
        its.sort(key=lambda t: (t[4] if t[4] is not None else 0,
                                t[5] if t[5] is not None else 0, str(t[0])))
        i = 0
        for it in its:
            if (it[3] or "").strip():
                continue                  # ya tiene complemento → se mantiene
            out[it[0]] = _letra_secuencial(i)
            i += 1
    return out


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
        pais = _country_de_survey(conn, survey_id)   # taxonomía en el idioma del relevamiento

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
                    ORDER BY h.habitaciones DESC NULLS LAST LIMIT 1), '') AS hotel_tipo,
                (SELECT ptm.tipo_edificacion FROM parcela_tipo_manual ptm
                    WHERE ptm.parcela_id = parcelas.parcela_id) AS tipo_manual,
                numero_estimado, numero_estimado_metodo, numero_estimado_confianza
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
            # Número inferido para las parcelas que el catastro dejó sin altura. Va en columnas
            # PROPIAS y no se mezcla con la dirección: es una inferencia, no dato del municipio.
            "Número estimado", "Número est. método", "Número est. confianza",
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
                # área=r[13], descripción CNPJ=r[36], hotel_tipo=r[38], override manual=r[39]
                _tipo_localizado(
                    _tipo_edificacion(r[8], r[9], r[13], r[36], r[38], r[39]), pais),
                r[40] or "", r[41] or "",
                f"{r[42]:.2f}" if r[42] is not None else "",
            ]

        # Letra secuencial (A, B, C…) para direcciones repetidas sin complemento propio.
        letras_rep = _letras_direccion_repetida(
            (r[34], r[2], r[3], r[4], r[18], r[19]) for r in rows)

        for r in rows:
            comp_ef = (r[4] or "").strip() or letras_rep.get(r[34], "")
            # Mismo criterio que el CSV de operadora: toda dirección sale con número, usando el
            # estimado cuando el municipio no lo declaró y la interpolación es confiable. Acá el
            # número crudo del catastro sigue visible en su columna (`DSC_LOGRADOURO_NO`) y el
            # inferido en las suyas (método y confianza), así se puede auditar cuál se usó.
            num_ef = (r[3] or "").strip()
            if num_ef in ("", "0") and (r[42] or 0) >= _NUMERO_CONF_MIN:
                num_ef = str(r[40] or "")
            direccion = " ".join(s for s in (r[2], num_ef, comp_ef) if s).strip()
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


# Columnas del layout de operadora que afirman ESTADO de una dirección concreta —de red, técnico
# o comercial— o la identifican en los sistemas de la operadora. Aunque sean constantes en el CSV
# importado, no se reproducen: para una dirección nueva no sabemos si su nodo está activo ni si
# tiene venta liberada, y su COD_HP/COD_IMOVEL lo asigna la operadora, no el relevamiento.
_OPERADORA_NO_INFERIBLE = (
    "DSC_STATUS_", "DSC_SITUACAO_", "DSC_MOTIVO_", "COD_SITUACAO_", "IND_BLOQUEIO_",
    "QTD_CAPACIDADE_", "DAT_", "NUM_CONTRATO", "COD_HP", "COD_IMOVEL", "NUM_UTM",
    "COD_CELULA", "COD_NODE", "COD_BAIRRO", "COD_LOGRADOURO", "COD_TIPO_IMOVEL",
    "COD_CID_CONTRATO", "NUM_IMPAR_", "NUM_PAR_", "COD_CONDOMINIO", "DSC_CONDOMINIO",
)


def _constantes_baseline(filas_extras: list, header: list, ya_mapeadas: set) -> dict:
    """Columnas que valen SIEMPRE lo mismo en el CSV que importó el cliente.

    Si en las 673 filas de su base `COD_OPERADORA` es 858, `COD_IBGE` 5108402 y `DSC_REGIONAL`
    "Regional Leste", eso no es dato de una dirección: es identidad de la base. Se reproduce tal
    cual en las filas nuevas. Se deriva del archivo en vez de hardcodearlo para que sirva con
    cualquier operadora y ciudad — otro cliente traerá otras constantes y salen solas.
    """
    vistos: dict[str, set] = {}
    for ex in filas_extras:
        d = json.loads(ex) if isinstance(ex, str) else (ex or {})
        for k, v in d.items():
            if v in (None, "", "NULL"):
                continue
            vistos.setdefault(k, set()).add(v)
            if len(vistos[k]) > 1:                      # ya no es constante, no hace falta más
                vistos[k] = {"__multi__", "__multi__2"}
    return {k: next(iter(v)) for k, v in vistos.items()
            if len(v) == 1 and k in header and k not in ya_mapeadas
            and not k.startswith(_OPERADORA_NO_INFERIBLE)}


def _vocabulario_tipo_logradouro(filas_extras: list) -> dict:
    """Abreviatura que usa la operadora para cada tipo de vía, sacada de su propio CSV:
    {"RUA": "R", "AVENIDA": "AV", "TRAVESSA": "TV", "ROTULA": "ROT", "BECO": "BC"}."""
    voc: dict[str, str] = {}
    for ex in filas_extras:
        d = json.loads(ex) if isinstance(ex, str) else (ex or {})
        dsc, cod = d.get("DSC_TIPO_LOGRADOURO"), d.get("COD_TIPO_LOGRADOURO")
        if dsc and cod and dsc != "NULL" and cod != "NULL":
            voc.setdefault(dsc.strip().upper(), cod.strip().upper())
    return voc


# ── Perfiles de export "formato del cliente" ──────────────────────────────────
# El CSV genérico (`/export/csv`) sirve en cualquier país; ADEMÁS cada cliente puede
# tener su propio layout de entrega. Para sumar el de otro cliente: agregar la entrada
# acá y su generador (`layout`), sin tocar el resto del endpoint.
# Ver docs/FUENTES_DATOS_AR.md § entregable.
#
# Claves de UI (`etiqueta`/`descripcion`, las únicas que viajan al front) e internas:
#   layout        · qué generador usa el survey SIN baseline (con baseline manda el
#                   header del CSV que el cliente importó, en cualquier país).
#   tipos_uso     · cómo se rotula una unidad de vivienda / de comercio en ESE idioma.
#   cod_operadora · el código de la base del cliente, primera columna del layout.
_EXPORT_PERFILES_CLIENTE: dict[str, dict] = {
    "BRA": {
        "etiqueta": "CSV Operadora",
        "descripcion": "Layout de base de logradouros de operadora (Brasil)",
        "layout": "operadora_br",
        "tipos_uso": ("RESIDENCIAL", "COMERCIO EM GERAL"),
        "cod_operadora": CSV_OPERADORA_COD,
    },
    # Argentina: mismas columnas que el contrato de import `ARG` de `_BASELINE_PERFILES`
    # (DIRECCION/LOCALIDAD/PROVINCIA/CODIGO_POSTAL/TIPO_INMUEBLE), así el entregable se
    # puede volver a importar como baseline del próximo relevamiento sin traducir nada.
    "ARG": {
        "etiqueta": "CSV Operadora",
        "descripcion": "Layout de base de calles de operadora (Argentina)",
        "layout": "operadora_ar",
        "tipos_uso": ("RESIDENCIAL", "COMERCIAL"),
        "cod_operadora": CSV_OPERADORA_COD,
    },
}
_EXPORT_PERFIL_UI = ("etiqueta", "descripcion")


_LOCALIDAD_REGION_CACHE: dict[str, tuple[str, str]] = {}


def _localidad_provincia_region(conn, region_id: str,
                                region_nombre: str = "") -> tuple[str, str]:
    """(localidad, provincia) de la región, para rellenar las parcelas que no las traen.

    Fuera de Brasil el catastro no siempre publica municipio/provincia por parcela (ARBA
    e IDERA no los traen: en Hurlingham las 441 parcelas vienen en NULL), y el entregable
    no puede salir con esas columnas vacías. Como son las MISMAS para toda la región, se
    resuelven una sola vez por reverse-geocoding del centroide (Nominatim, gratis) y se
    cachean en memoria — no una llamada por fila ni una por descarga.

    Respaldo si el reverse falla: el nombre de la región como localidad. Nunca lanza.
    """
    if region_id in _LOCALIDAD_REGION_CACHE:
        return _LOCALIDAD_REGION_CACHE[region_id]

    localidad, provincia = "", ""
    try:
        pt = conn.execute(text("""
            SELECT AVG(centroid_lat), AVG(centroid_lng) FROM parcelas
            WHERE region_id = :rid AND centroid_lat IS NOT NULL AND centroid_lng IS NOT NULL
        """), {"rid": region_id}).fetchone()
        if pt and pt[0] is None:
            pt = None
        if pt:
            from scrapitero.agents.geo import detect_localidad_provincia
            loc, prov = detect_localidad_provincia(float(pt[0]), float(pt[1]))
            localidad, provincia = (loc or ""), (prov or "")
    except Exception as e:      # el export no se cae por un reverse que no anduvo
        logger.warning(f"No se pudo resolver localidad/provincia de {region_id}: {e}")

    if not localidad:
        localidad = (region_nombre or "").strip()
    _LOCALIDAD_REGION_CACHE[region_id] = (localidad, provincia)
    logger.info(f"Entregable {region_id}: localidad={localidad!r} provincia={provincia!r}")
    return localidad, provincia


def _perfil_cliente_ui(pais: str) -> dict | None:
    """El perfil de entrega del país, recortado a lo que necesita el front. Las claves
    internas (`layout`, `tipos_uso`, `cod_operadora`) no viajan en la API."""
    perfil = _EXPORT_PERFILES_CLIENTE.get((pais or "").upper())
    return {k: perfil[k] for k in _EXPORT_PERFIL_UI} if perfil else None


@app.get("/api/surveys/{survey_id}/export/perfiles")
async def export_perfiles(survey_id: str) -> JSONResponse:
    """Qué formatos de entrega aplican a este relevamiento, para que la UI muestre los
    botones que corresponden en vez de decidirlo con un `country_code === 'BRA'` en el
    front (que había que tocar en cada página al sumar un cliente)."""
    with get_engine().connect() as conn:
        pais = _country_de_survey(conn, survey_id).upper()
    perfil = _EXPORT_PERFILES_CLIENTE.get(pais)
    return JSONResponse({
        "ok": True, "pais": pais,
        # siempre disponibles: no dependen del país
        "genericos": [{"clave": "csv", "etiqueta": "CSV"},
                      {"clave": "dxf", "etiqueta": "DXF (AutoCAD)"}],
        "cliente": ({"clave": "csv-operadora",
                     **{k: perfil[k] for k in _EXPORT_PERFIL_UI}} if perfil else None),
    })


@app.get("/api/surveys/{survey_id}/export/csv-operadora")
async def export_csv_operadora(survey_id: str) -> StreamingResponse:
    """CSV con el layout de entrega del cliente (`_EXPORT_PERFILES_CLIENTE`).

    Con baseline vinculado se usa el header del CSV que el cliente importó (cualquier
    país). Sin baseline, el layout del perfil de su país:
      · `operadora_br` — base de logradouros. Descompone `calle` en tipo/título/
        preposição/nome oficial (heurística por diccionario — logradouro_br.py).
        CODIGO_LOGRADOURO sale de parcelas.codigo_logradouro (BCI, migración 016);
        vacío para parcelas parseadas antes de esa migración.
      · `operadora_ar` — base de calles en español, con las columnas del contrato de
        import `ARG` para que el entregable se pueda re-importar como baseline.
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
        pais_survey = (meta[3] or "").upper()
        perfil = _EXPORT_PERFILES_CLIENTE.get(pais_survey)
        if not perfil:
            definidos = ", ".join(sorted(_EXPORT_PERFILES_CLIENTE)) or "(ninguno)"
            return JSONResponse(
                {"error": f"No hay formato de entrega de cliente definido para {pais_survey or '?'}. "
                          f"Definidos: {definidos}. Usá el CSV genérico o el DXF."},
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
            extras_base = [r[0] for r in conn.execute(text(
                "SELECT extras FROM baseline_direcciones WHERE baseline_id = CAST(:bid AS uuid) "
                "AND extras IS NOT NULL"), {"bid": base[2]}).fetchall()]
            parc = conn.execute(text("""
                SELECT calle,
                       -- Toda dirección tiene que salir con número. Cuando el municipio no lo
                       -- declaró (campo en '0' o vacío, el 12,8% de las parcelas del BCI), se
                       -- usa el que interpoló `NumeroEstimator` sobre el eje de la calle,
                       -- pero SÓLO si su confianza llega al piso: las extrapolaciones más allá
                       -- del último ancla miden mal y esas parcelas van al panel de incidencias
                       -- para que un humano cargue la altura mirando el frente.
                       -- `parcelas.numero` NO se toca: sigue siendo el dato del municipio, y el
                       -- inferido vive en `numero_estimado*` (mig. 050). Acá sólo se elige cuál
                       -- de los dos sale al entregable.
                       CASE WHEN COALESCE(NULLIF(numero, '0'), '') <> '' THEN numero
                            WHEN COALESCE(numero_estimado_confianza, 0) >= :conf
                                 THEN numero_estimado
                            ELSE NULL END AS numero,
                       complemento, barrio, municipio, estado_provincia,
                       codigo_postal, COALESCE(uf_vivienda, 0), COALESCE(uf_comercio, 0),
                       -- Nombre del comercio de la parcela (para DSC_NOME_DO_IMOVEL): hoteles +
                       -- comercios (Google) + establecimiento agrupado + shoppings (POI espacial).
                       NULLIF(TRIM(BOTH ' |' FROM CONCAT_WS(' | ',
                         (SELECT string_agg(h.nombre, ' | ' ORDER BY h.nombre) FROM hoteles h
                            WHERE h.parcela_id = parcelas.parcela_id
                              AND h.nombre IS NOT NULL AND NOT h.cerrado_def),
                         (SELECT string_agg(co.nombre, ' | ' ORDER BY co.nombre) FROM comercios co
                            WHERE co.parcela_id = parcelas.parcela_id AND co.nombre IS NOT NULL),
                         (SELECT est.nombre FROM establecimientos est
                            WHERE est.establecimiento_id = parcelas.establecimiento_id),
                         (SELECT string_agg(poi.nombre, ' | ' ORDER BY poi.nombre)
                            FROM establecimientos_poi poi
                            WHERE poi.region_id = parcelas.region_id AND poi.nombre IS NOT NULL
                              AND parcelas.geometry IS NOT NULL
                              AND ST_Contains(parcelas.geometry,
                                    ST_SetSRID(ST_MakePoint(poi.lng, poi.lat), 4326)))
                       )), '') AS nome_imovel,
                       -- UNICO / MULTIPLO: lo sabe el BCI (unidades de la inscrição). Es el
                       -- `COD_TIPO_EDIFICACAO` del layout.
                       (SELECT count(*) FROM parcela_unidades u
                          WHERE u.parcela_id = parcelas.parcela_id) AS n_unidades
                FROM parcelas
                WHERE survey_id = CAST(:sid AS uuid) AND calle IS NOT NULL
                ORDER BY calle,
                         NULLIF(regexp_replace(COALESCE(numero, ''), '\\D', '', 'g'), '')::bigint
                           NULLS LAST
            """), {"sid": survey_id, "conf": _NUMERO_CONF_MIN}).fetchall()
            plantilla = (header, mapeo_d, parc,
                         _constantes_baseline(extras_base, header,
                                              {v for v in mapeo_d.values() if v}),
                         _vocabulario_tipo_logradouro(extras_base))

        rows = None
        if not plantilla and perfil["layout"] == "operadora_ar":
            # Fallback ARG: layout de base de calles, columnas del contrato de import `ARG`.
            # Una fila por DIRECCIÓN única, con la UF SUMADA de las parcelas que comparten
            # esa dirección (a diferencia del layout brasilero, que no lleva UF: acá es el
            # dato del relevamiento y el que permite re-importar el archivo como baseline).
            rows = conn.execute(text("""
                SELECT municipio, estado_provincia, barrio, calle, codigo_postal,
                       CASE WHEN COALESCE(NULLIF(numero, '0'), '') <> '' THEN numero
                            WHEN COALESCE(numero_estimado_confianza, 0) >= :conf
                                 THEN numero_estimado
                            ELSE NULL END AS numero,
                       SUM(COALESCE(uf_vivienda, 0))::int AS uf_v,
                       SUM(COALESCE(uf_comercio, 0))::int AS uf_c,
                       MIN(uso_principal) AS uso
                FROM parcelas
                WHERE survey_id = CAST(:sid AS uuid) AND calle IS NOT NULL
                GROUP BY municipio, estado_provincia, barrio, calle, codigo_postal, 6
                ORDER BY calle,
                         NULLIF(regexp_replace(COALESCE(
                             CASE WHEN COALESCE(NULLIF(numero, '0'), '') <> '' THEN numero
                                  WHEN COALESCE(numero_estimado_confianza, 0) >= :conf
                                       THEN numero_estimado
                                  ELSE NULL END, ''), '\\D', '', 'g'), '')::bigint
                           NULLS LAST
            """), {"sid": survey_id, "conf": _NUMERO_CONF_MIN}).fetchall()
        elif not plantilla:
            # Fallback BRA (survey sin baseline): layout de base de logradouros de operadora.
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

        # Localidad/provincia de respaldo: ARBA/IDERA no las publican por parcela (en
        # Hurlingham las 441 vienen en NULL) y el entregable no puede salir sin ellas.
        # Se resuelven UNA vez por región desde su centroide, no por fila — y sólo si
        # falta alguna: donde el catastro sí las trae (el BCI de Brasil) no se paga el
        # reverse-geocoding, que además es lo único de este export que sale a la red.
        loc_region, prov_region = "", ""
        if conn.execute(text("""
            SELECT EXISTS (SELECT 1 FROM parcelas
                           WHERE survey_id = CAST(:sid AS uuid) AND calle IS NOT NULL
                             AND (municipio IS NULL OR estado_provincia IS NULL))
        """), {"sid": survey_id}).scalar():
            loc_region, prov_region = _localidad_provincia_region(conn, meta[0], meta[1])

    fecha = meta[2].strftime("%Y-%m-%d") if meta[2] else ""
    filename = f"operadora_{meta[0]}_{fecha}.csv".replace(" ", "_")

    if plantilla:
        header, mapeo_d, parc, constantes, voc_tipo = plantilla
        inv = {h: f for f, h in mapeo_d.items() if h}   # header → campo
        # Cómo rotula el cliente una unidad de vivienda / de comercio, en SU idioma.
        TIPO_VIV, TIPO_COM = perfil["tipos_uso"]
        idx = {h: i for i, h in enumerate(header)}
        # El respaldo de provincia sale del reverse con el nombre completo, pero el
        # `COD_UF` brasilero es la SIGLA. `sigla_uf` sólo conoce estados de Brasil y
        # devuelve '' para el resto, así que "Mato Grosso"→MT y "Buenos Aires" queda igual.
        from scrapitero.agents.geocode_forward import sigla_uf
        prov_plantilla = sigla_uf(prov_region) or prov_region

        def _set(fila, col, valor):
            """Escribe una columna del layout sólo si existe y hay algo que poner."""
            i = idx.get(col)
            if i is not None and valor not in (None, ""):
                fila[i] = valor

        def _gen_plantilla():
            buf = io.StringIO()
            buf.write("﻿")   # BOM para Excel
            w = csv.writer(buf, delimiter=";")
            w.writerow(header)
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)
            for calle, numero, compl, barrio, muni, est, cep, uv, uc, nome, n_unid in parc:
                # El `complemento` del BCI mezcla cuatro cosas y sólo una es dirección:
                # unidad ("QUADRA 04 LOTE 13"), nombre del inmueble ("DROGASIL"), nota
                # registral ("MAT.57499") y referencia ("ESQUINA COM A RUA X"). Sin separar,
                # el 85% de lo que se pegaba a DSC_ENDERECO_COMPLETO no era una dirección.
                # El nombre pasa a su columna propia; el resto no va al layout de operadora
                # (el valor crudo queda intacto en `parcelas.complemento` y en el CSV completo).
                unidad, nombre_compl = clasificar_complemento(compl)
                full = " ".join(x for x in (calle, numero, unidad) if x)
                if nombre_compl:
                    # Dedupe: el nombre suele venir por partida doble (el hotel ya está en
                    # `hoteles` y además rotulado en el complemento del BCI).
                    ya = {p.strip().upper() for p in (nome or "").split("|")}
                    if nombre_compl.upper() not in ya:
                        nome = " | ".join(x for x in (nome, nombre_compl) if x)
                # Una fila por UNIDAD: uf_vivienda → RESIDENCIAL, uf_comercio → COMERCIO.
                # Sin UF (vacante) → 1 fila para no perder la dirección.
                unidades = [TIPO_VIV] * int(uv) + [TIPO_COM] * int(uc)
                if not unidades:
                    unidades = [""]
                # Descomposición de la vía, para las columnas que el layout pide por separado.
                d_logr = descomponer_logradouro(calle)
                cod_tipo = voc_tipo.get(d_logr["tipo"], "")
                # "R ORIEL B CAMPOS", "AV PRES ARTHUR BERNARDES": tipo abreviado + resto.
                logr_completo = " ".join(x for x in (cod_tipo, d_logr["titulo"],
                                                     d_logr["preposicao"], d_logr["nome"]) if x)
                pares_unidad = partes_unidad(unidad)

                for tipo in unidades:
                    # Ciudad/provincia: la de la parcela y, si el catastro no las publica
                    # (ARBA/IDERA), la de la región — la columna no puede salir vacía.
                    val = {"direccion": full, "calle": calle or "", "numero": numero or "",
                           "barrio": barrio or "", "ciudad": muni or loc_region,
                           "estado": (est or prov_plantilla).upper(),
                           "cep": cep or "", "uso": tipo}
                    fila = [val.get(inv.get(col), "") for col in header]
                    # 1) Constantes de la base del cliente (COD_OPERADORA, COD_IBGE, …).
                    for col, v in constantes.items():
                        _set(fila, col, v)
                    # 2) Lo que sale del relevamiento (BCI + catastro).
                    _set(fila, "DSC_NOME_DO_IMOVEL", nome)
                    _set(fila, "DSC_LOGRADOURO_NO", numero)
                    _set(fila, "COD_TIPO_LOGRADOURO", cod_tipo)
                    _set(fila, "DSC_TIPO_LOGRADOURO", d_logr["tipo"])
                    _set(fila, "DSC_LOGR_COMPLETO", logr_completo)
                    # Una inscrição con más de una unidad en el BCI es edificación MÚLTIPLE.
                    _set(fila, "COD_TIPO_EDIFICACAO", "MULTIPLO" if (n_unid or 0) > 1 else "UNICO")
                    # El complemento de unidad, en los pares tipo/texto del layout (hasta 4).
                    for i, (t_u, x_u) in enumerate(pares_unidad[:4], start=1):
                        _set(fila, f"DSC_IMOVEL_TIPO_COMPLEMENTO{i}", t_u)
                        _set(fila, f"DSC_IMOVEL_TEXTO_COMPLEMENTO{i}", x_u)
                    w.writerow(fila)
                yield buf.getvalue(); buf.seek(0); buf.truncate(0)

        return StreamingResponse(
            _gen_plantilla(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    if perfil["layout"] == "operadora_ar":
        from scrapitero.agents.calle_ar import descomponer_calle

        TIPO_VIV, TIPO_COM = perfil["tipos_uso"]

        def _tipo_inmueble(uf_v: int, uf_c: int, uso: str) -> str:
            """Cómo se rotula la dirección en la columna que el import lee como uso.
            Misma regla que `_agregar_por_direccion` al importar un baseline, para que el
            archivo entre y salga diciendo lo mismo: residencial → vivienda, resto →
            comercio. Sin UF no hay unidad que clasificar: sale el uso del catastro."""
            if uf_v > 0 and uf_c > 0:
                return "MIXTO"
            if uf_c > 0:
                return TIPO_COM
            if uf_v > 0:
                return TIPO_VIV
            return (uso or "").upper() if uso in _USOS_VALIDOS else ""

        def _gen_ar():
            buf = io.StringIO()
            buf.write("﻿")   # BOM para Excel
            w = csv.writer(buf, delimiter=";")
            w.writerow([
                "COD_OPERADORA", "LOCALIDAD", "PROVINCIA", "BARRIO", "TIPO_CALLE",
                "NOMBRE_CALLE", "CODIGO_POSTAL", "NUMERO", "DIRECCION",
                "TIPO_INMUEBLE", "UF_VIVIENDA", "UF_COMERCIO",
            ])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)
            for municipio, prov, barrio, calle, cp, numero, uf_v, uf_c, uso in rows:
                d = descomponer_calle(calle)
                w.writerow([
                    perfil["cod_operadora"],
                    (municipio or loc_region or "").upper(),
                    (prov or prov_region or "").upper(),
                    (barrio or "").upper(),      # ARBA/IDERA no publican barrio
                    d["tipo"],
                    d["nombre"],
                    cp or "",                    # ARBA/IDERA no publican código postal
                    numero or "",
                    " ".join(x for x in (calle, numero) if x),
                    _tipo_inmueble(int(uf_v or 0), int(uf_c or 0), uso),
                    int(uf_v or 0),
                    int(uf_c or 0),
                ])
                yield buf.getvalue(); buf.seek(0); buf.truncate(0)

        return StreamingResponse(
            _gen_ar(),
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


@app.get("/api/surveys/{survey_id}/export/dxf")
async def export_dxf(survey_id: str):
    """DXF (AutoCAD) del relevamiento, con las UF de vivienda rotuladas por parcela.

    DXF y no DWG: el `.dwg` es formato cerrado de Autodesk y ninguna librería libre lo
    escribe; el DXF es el formato de intercambio del propio AutoCAD, que lo abre nativo.

    Se genera on-the-fly (no se cachea: el relevamiento cambia con cada paso del
    pipeline y una corrida sobre 567 parcelas tarda ~2 s) en un temporal que se borra
    cuando termina de servirse.
    """
    engine = get_engine()
    with engine.connect() as conn:
        meta = conn.execute(text("""
            SELECT r.name, s.started_at
            FROM surveys s JOIN regions r ON s.region_id = r.region_id
            WHERE s.survey_id = :sid
        """), {"sid": survey_id}).fetchone()
    if not meta:
        return JSONResponse({"error": "Survey no encontrado"}, status_code=404)

    from scrapitero.agents.dxf_export import DXFInput
    from scrapitero.agents.dxf_export import run as run_dxf_agent

    tmpdir = tempfile.mkdtemp(prefix="dxf_")
    fecha = meta[1].strftime("%Y-%m-%d") if meta[1] else ""
    nombre = f"relevamiento_{meta[0]}_{fecha}.dxf".replace(" ", "_").replace("/", "-")
    destino = os.path.join(tmpdir, nombre)

    def _job() -> dict:
        _thread_job_id.value = survey_id
        return run_dxf_agent(DXFInput(survey_id=survey_id, output_path=destino)).model_dump()

    data = await asyncio.to_thread(_job)
    if not data.get("ok"):
        shutil.rmtree(tmpdir, ignore_errors=True)
        return JSONResponse({"error": data.get("error") or "No se pudo generar el DXF"},
                            status_code=422)

    return FileResponse(
        destino, media_type="image/vnd.dxf", filename=nombre,
        background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True),
    )


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
# ── Contrato de columnas del relevamiento anterior, POR PERFIL ────────────────
# Cada cliente/país entrega el CSV con SUS nombres de columna. El perfil no se pide al
# operador ni se deduce del país (el país todavía no se conoce al importar: se detecta
# geocodificando la ciudad, que sale de este mismo CSV) — **se detecta de los headers**,
# eligiendo el perfil que más columnas obligatorias matchea. Para sumar un cliente nuevo
# alcanza con agregar una entrada acá: no hay que tocar la lógica de import.
_BASELINE_PERFILES: dict[str, dict] = {
    # Operadora brasilera (Várzea Grande) — el contrato original.
    "BRA": {
        "etiqueta": "Operadora Brasil",
        "campos": {
            "direccion":       "DSC_ENDERECO_COMPLETO",   # obligatoria
            "ciudad":          "DSC_CIDADE",              # obligatoria
            "estado":          "COD_UF",                  # obligatoria (sigla/código/nombre de UF)
            "cep":             "NUM_CEP",                 # obligatoria
            "uso":             "DSC_TIPO_IMOVEL",         # obligatoria (tipo de inmueble → UF)
            "barrio":          "DSC_BAIRRO",              # opcional
            "status_contrato": "DSC_STATUS_CONTRATO",     # opcional
            "status_node":     "COD_NODE",                # opcional
        },
        "obligatorias": ["DSC_ENDERECO_COMPLETO", "DSC_CIDADE", "COD_UF",
                         "NUM_CEP", "DSC_TIPO_IMOVEL"],
        "opcionales": ["DSC_BAIRRO", "DSC_STATUS_CONTRATO", "COD_NODE"],
    },
    # Argentina — nombres genéricos en español. En AR no hay "UF" ni "CEP": son PROVINCIA
    # y CÓDIGO POSTAL. Cuando haya un cliente concreto con su propio layout, se agrega su
    # perfil acá (o se ajusta éste) sin tocar nada más.
    "ARG": {
        "etiqueta": "Genérico Argentina",
        "campos": {
            "direccion":       "DIRECCION",       # obligatoria
            "ciudad":          "LOCALIDAD",       # obligatoria
            "estado":          "PROVINCIA",       # obligatoria
            "cep":             "CODIGO_POSTAL",   # obligatoria
            "uso":             "TIPO_INMUEBLE",   # obligatoria
            "barrio":          "BARRIO",          # opcional
            "status_contrato": "ESTADO_CONTRATO", # opcional
            "status_node":     "NODO",            # opcional
        },
        "obligatorias": ["DIRECCION", "LOCALIDAD", "PROVINCIA",
                         "CODIGO_POSTAL", "TIPO_INMUEBLE"],
        "opcionales": ["BARRIO", "ESTADO_CONTRATO", "NODO"],
    },
}
_BASELINE_PERFIL_DEFECTO = "BRA"

# Compatibilidad: el resto del módulo sigue leyendo estos nombres para el perfil brasilero.
_BASELINE_COLUMNAS = _BASELINE_PERFILES["BRA"]["campos"]
_BASELINE_OBLIGATORIAS = _BASELINE_PERFILES["BRA"]["obligatorias"]
_BASELINE_OPCIONALES = _BASELINE_PERFILES["BRA"]["opcionales"]


def _detectar_perfil_baseline(headers: list[str]) -> tuple[str, dict]:
    """Elige el perfil de columnas que mejor matchea los headers del CSV subido.

    Devuelve (clave_perfil, perfil). Si ninguno matchea nada se devuelve el de defecto,
    para que el error que ve el operador nombre las columnas del contrato conocido.
    """
    norm = {_norm_header(h) for h in headers if str(h).strip()}
    mejor, mejor_n = _BASELINE_PERFIL_DEFECTO, -1
    for clave, perfil in _BASELINE_PERFILES.items():
        n = sum(1 for c in perfil["obligatorias"] if _norm_header(c) in norm)
        if n > mejor_n:
            mejor, mejor_n = clave, n
    return mejor, _BASELINE_PERFILES[mejor]


def _norm_header(h: str) -> str:
    """Normaliza un header para comparar: sin acentos, minúsculas y con `_`/`-` tratados
    como espacio — el layout de una operadora usa `CODIGO_POSTAL` y una planilla hecha a
    mano escribe `Código Postal`, y son la misma columna. Se aplica a los DOS lados de la
    comparación, así que no cambia lo que ya matcheaba."""
    import unicodedata
    s = "".join(c for c in unicodedata.normalize("NFKD", str(h or ""))
                if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[_\-]+", " ", s).lower().strip())


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
    """Resuelve el `mapeo_d` interno {campo: header_real} con el perfil de columnas que
    matchea el CSV (`_detectar_perfil_baseline`). Match case/acento-insensible.
    Lanza ValueError si falta alguna columna OBLIGATORIA (con el nombre exacto esperado)."""
    norm = {_norm_header(h): h for h in headers if str(h).strip()}
    _, perfil = _detectar_perfil_baseline(headers)
    mapeo: dict[str, str] = {}
    for campo, columna in perfil["campos"].items():
        real = norm.get(_norm_header(columna))
        if real:
            mapeo[campo] = real
    faltan = [c for c in perfil["obligatorias"] if _norm_header(c) not in norm]
    if faltan:
        raise ValueError(
            f"Al CSV le faltan columnas obligatorias del formato «{perfil['etiqueta']}»: "
            + ", ".join(faltan) +
            ". El relevamiento anterior debe traer estas columnas (nombre exacto): " +
            ", ".join(perfil["obligatorias"]) + ".")
    return mapeo


def _validar_columnas_csv(headers: list[str], datos: list, sep: str) -> dict:
    """Payload de preview: valida que estén las columnas obligatorias del perfil detectado.
    Devuelve detectadas/faltantes para mostrar en la UI. Ya no hay mapeo manual."""
    norm = {_norm_header(h) for h in headers if str(h).strip()}
    clave, perfil = _detectar_perfil_baseline(headers)
    obligatorias, opcionales = perfil["obligatorias"], perfil["opcionales"]
    presentes = lambda cols: [c for c in cols if _norm_header(c) in norm]
    faltantes = [c for c in obligatorias if _norm_header(c) not in norm]
    return {
        "ok": not faltantes,
        "perfil": clave,
        "perfil_etiqueta": perfil["etiqueta"],
        "perfiles": [{"clave": k, "etiqueta": p["etiqueta"], "obligatorias": p["obligatorias"]}
                     for k, p in _BASELINE_PERFILES.items()],
        "obligatorias": obligatorias,
        "opcionales": opcionales,
        "detectadas": presentes(obligatorias + opcionales),
        "faltantes": faltantes,
        "separador": sep,
        "total_filas": len(datos),
        "error": (f"Faltan columnas obligatorias del formato «{perfil['etiqueta']}»: "
                  + ", ".join(faltantes)) if faltantes else None,
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
            # COD_UF/nombre → sigla (51/"Mato Grosso"→MT). `sigla_uf` sólo conoce estados
            # BRASILEROS y devuelve '' para el resto: fuera de Brasil se conserva el valor
            # tal cual vino (una provincia argentina —"Buenos Aires"— se perdería, y el
            # geocoding la necesita para desambiguar calles homónimas entre provincias).
            "estado": (sigla_uf(celda(fila, "estado"))
                       or celda(fila, "estado").strip()[:100] or None),
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


def _direccion_norm(calle, numero) -> str:
    """Dirección normalizada (calle canónica + número) con la normalización ACTUAL — sirve
    para mostrarla junto a la original en el popup del punto."""
    from scrapitero.agents.direccion_norm import normalizar_calle, normalizar_numero
    return " ".join(x for x in (normalizar_calle(calle), normalizar_numero(numero)) if x).strip()


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
            "direccion_norm": _direccion_norm(r[2], r[3]),
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
        "direccion_norm": _direccion_norm(r[2], r[3]),
        "uso": r[4], "uf_v": int(r[5] or 0), "uf_c": int(r[6] or 0),
        "uf_total": int((r[5] or 0) + (r[6] or 0)),
        # 'ciudad' = no se pudo ubicar en la calle → centro de la ciudad (aproximado).
        "aprox_ciudad": (r[9] == "ciudad"),
        # el mapa de incidencias pinta distinto el punto que un humano ya reubicó
        "fuente": r[9],
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

# Expansión de abreviaturas para MOSTRAR el nombre lindo (no para matchear: eso lo hace
# normalizar_calle). Tipo de vía + títulos honoríficos, ES/PT.
_VIA_DISPLAY = {
    "av": "Avenida", "avda": "Avenida", "aven": "Avenida", "r": "Rua", "rua": "Rua",
    "tv": "Travessa", "trav": "Travessa", "al": "Alameda", "est": "Estrada",
    "rod": "Rodovia", "pc": "Praça", "pca": "Praça", "pje": "Passagem", "diag": "Diagonal",
    "bv": "Bulevar", "cno": "Caminho", "via": "Via", "largo": "Largo", "beco": "Beco",
    "viela": "Viela", "ladeira": "Ladeira", "marginal": "Marginal", "c": "Calle",
}
_TIT_DISPLAY = {
    "gov": "Governador", "gob": "Governador", "mal": "Marechal", "cel": "Coronel",
    "cnel": "Coronel", "dr": "Doutor", "dra": "Doutora", "prof": "Professor",
    "profa": "Professora", "eng": "Engenheiro", "ing": "Engenheiro", "gral": "General",
    "gen": "General", "brig": "Brigadeiro", "cap": "Capitão", "tte": "Tenente",
    "ten": "Tenente", "sgt": "Sargento", "alm": "Almirante", "cmte": "Comandante",
    "pte": "Presidente", "pres": "Presidente", "sen": "Senador", "dep": "Deputado",
    "dip": "Deputado", "pref": "Prefeito", "ver": "Vereador", "min": "Ministro",
    "mons": "Monsenhor", "pe": "Padre", "frei": "Frei",
    "s": "São", "sao": "São", "san": "São", "sta": "Santa", "sto": "Santo",
}
_PREP_DISPLAY = {"de", "do", "da", "dos", "das", "e", "del", "la", "las", "los", "y"}


def _calle_display(calle: Optional[str]) -> str:
    """Nombre de calle lindo para mostrar: saca la basura '(LOT …)', expande el tipo de vía
    y los títulos (AV→Avenida, GOV→Governador, MAL→Marechal…) y Title-Case."""
    s = re.sub(r"\(.*?\)", " ", calle or "")                 # quitar (...)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return (calle or "").strip()
    out = []
    for i, tok in enumerate(s.split()):
        low = re.sub(r"[^\wçãõáéíóúâêôàü]", "", tok.lower())
        if i == 0 and low in _VIA_DISPLAY:
            out.append(_VIA_DISPLAY[low])
        elif low in _TIT_DISPLAY:
            out.append(_TIT_DISPLAY[low])
        elif low in _PREP_DISPLAY:
            out.append(low)
        else:
            out.append(tok.capitalize())
    return " ".join(out)


def _cuadra(num_min: int, num_max: int) -> tuple[int, int]:
    """Redondea el rango a CUADRAS COMPLETAS de 100 alturas (inicio de cuadra del mínimo →
    fin de cuadra del máximo). Cuadras: 1–100, 101–200, 201–300, …"""
    lo = ((max(num_min, 1) - 1) // 100) * 100 + 1
    hi = ((max(num_max, num_min, 1) + 99) // 100) * 100
    return lo, hi


def _detectar_calles_baseline(baseline_id: str, cuadras: bool = True) -> list[dict]:
    """Autodetecta las calles + rango de numeración del relevamiento anterior.
    Agrupa `baseline_direcciones` por calle normalizada y saca min/max del número.
    `cuadras`=True redondea el rango a cuadras completas de 100 alturas."""
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
        g = grupos.setdefault(cn, {"calle": _calle_display(calle), "calle_norm": cn, "n": 0,
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
        elif cuadras:
            g["num_min"], g["num_max"] = _cuadra(g["num_min"], g["num_max"])
        out.append(g)
    out.sort(key=lambda g: g["n"], reverse=True)
    return out


def _buffer_grados(geoms: list, metros: float, lat: float, cap_style: int = 1):
    """Une y buffea geometrías (lat/lng) por ~`metros`, en grados (corrige por latitud).
    `cap_style`=2 (flat) para corredores de calle (paralelas al eje, sin puntas redondas).
    Devuelve una geometría shapely. El polígono es de DESCARGA (generoso) → el filtro
    estricto por dirección hace la precisión, así que la aproximación en grados alcanza."""
    import math
    from shapely.ops import unary_union
    deg = metros / 111000.0
    u = unary_union(geoms)
    # buffer isotrópico en grados; la distorsión lng a lat ~-15° es chica (cos≈0.96)
    return u.buffer(deg / max(math.cos(math.radians(lat)), 0.3), cap_style=cap_style).buffer(0)


def _poligono_de_calles(calles: list[dict], pts_por_calle: dict, ancho_m: float = 40.0,
                        ciudad: Optional[str] = None, largo_cuadra_m: float = 100.0) -> Optional[dict]:
    """Construye el polígono de DESCARGA garantizando, **por CADA dirección**, su CUADRA: un
    bloque de `largo_cuadra_m`×`ancho_m` (default 100 m de largo × 40 m de ancho = ±50 m sobre el
    eje de la calle y ±20 m a cada lado del eje) centrado en la dirección, orientado según el eje
    real de la calle (OSM). La unión de los bloques da el corredor: en calles densas se funden en
    un segmento continuo; una dirección aislada igual recibe su cuadra completa.

    Reusa el **mismo eje OSM cacheado** que el geocoding (`_osm_geometrias` + `_stitch`: cosido y
    sin carriles duplicados), así el corredor es consistente con dónde se ubicaron las direcciones
    y no re-consulta Overpass si la ciudad ya está cacheada. Respaldo si OSM no tiene la calle:
    un blob de radio media-cuadra por punto (garantiza ≥`largo_cuadra_m` de cobertura). SmartGIS
    baja por **intersección** → el corredor fino agarra las parcelas de las dos veredas.
    `pts_por_calle`: {calle_norm: [(lat,lng,num)]}. Devuelve GeoJSON (Polygon/MultiPolygon) o None."""
    import math
    from shapely.geometry import Point, mapping
    from shapely.ops import unary_union, substring
    from scrapitero.agents.baseline_interp import _osm_geometrias, _stitch
    from scrapitero.db.engine import get_engine

    allpts = [(la, ln) for v in pts_por_calle.values() for (la, ln, _n) in v]
    if not allpts:
        return None
    half = ancho_m / 2.0                       # ancho a cada lado del eje (m)
    medio_largo_deg = (largo_cuadra_m / 2.0) / 111000.0   # ±media cuadra en grados de arclength
    lats = [p[0] for p in allpts]; lngs = [p[1] for p in allpts]
    mlat = sum(lats) / len(lats)
    mrg = 0.012
    bbox = (min(lats) - mrg, min(lngs) - mrg, max(lats) + mrg, max(lngs) + mrg)

    # Geometría OSM de las calles del scope, CACHEADA por ciudad/calle (la misma que el geocoding).
    req = [((c.get("calle_norm") or ""), (c.get("calle") or c.get("calle_norm") or "")) for c in calles]
    try:
        geoms = _osm_geometrias(get_engine(), req, ciudad, bbox)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(f"scope-calles: OSM falló ({exc}); polígono solo con blobs de puntos")
        geoms = {}

    partes = []
    for c in calles:
        cn = c.get("calle_norm") or ""
        pts = pts_por_calle.get(cn, [])
        if not pts:
            continue
        lineas = geoms.get(cn) or []
        eje = _stitch(lineas) if lineas else None
        if eje is not None and eje.length > 0:
            # Por cada dirección: ±media cuadra sobre el eje → bloque de 100 m, buffeado ±20 m.
            L = eje.length
            for la, ln, _n in pts:
                s = eje.project(Point(ln, la))
                seg = substring(eje, max(0.0, s - medio_largo_deg), min(L, s + medio_largo_deg))
                if not seg.is_empty:
                    partes.append(_buffer_grados([seg], half, mlat, cap_style=2))
            # GARANTÍA: un blob de radio ±ancho/2 en cada dirección, por si su punto geocodificado
            # (g:numero/mapbox) cae a más de `half` del eje (retiro/calle ancha) → no queda afuera.
            partes.append(_buffer_grados([Point(ln, la) for la, ln, _ in pts], half, mlat))
        else:
            # Sin eje OSM: blob de radio media-cuadra por punto (garantiza la cobertura mínima).
            partes.append(_buffer_grados([Point(ln, la) for la, ln, _ in pts],
                                         largo_cuadra_m / 2.0, mlat))

    partes = [p for p in partes if p and not p.is_empty]
    if not partes:
        return None
    poly = unary_union(partes).buffer(0)
    if poly.is_empty:
        return None
    return json.loads(json.dumps(mapping(poly)))


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


def _scope_y_puntos_de_calles(baseline_id: str, lista: list) -> tuple:
    """Normaliza la lista de calles editada → `scope` (cuadras completas) y `pts_por_calle`
    (direcciones geocodificadas del baseline por calle del scope). Devuelve (scope, pts, ciudad,
    region_id, nombre) o (None, error, ...) si falla."""
    from scrapitero.agents.direccion_norm import normalizar_calle
    from scrapitero.agents.baseline_interp import _num as _numero
    scope = []
    for c in lista:
        cl = (c.get("calle") or "").strip()
        cn = normalizar_calle(cl)
        if not cn:
            continue
        try:
            mn = int(c.get("num_min") or 0); mx = int(c.get("num_max") or 0)
        except (TypeError, ValueError):
            mn, mx = 0, 0
        if mx and mx < mn:
            mn, mx = mx, mn
        mn, mx = _cuadra(mn or 1, mx or mn or 1)
        scope.append({"calle": cl or cn, "calle_norm": cn, "num_min": mn, "num_max": mx})
    if not scope:
        return None, "Ninguna calle válida", None, None, None
    engine = get_engine()
    with engine.connect() as conn:
        base = conn.execute(text(
            "SELECT region_id, nombre, ciudad FROM baselines WHERE baseline_id = :bid"),
            {"bid": baseline_id}).fetchone()
        if not base:
            return None, "Baseline no encontrado", None, None, None
        region_id, nombre, ciudad = base[0], base[1], base[2]
        filas_pt = conn.execute(text("""
            SELECT lat, lng, calle, numero FROM baseline_direcciones
            WHERE baseline_id = :bid AND lat IS NOT NULL
        """), {"bid": baseline_id}).fetchall()
    scope_norms = {c["calle_norm"] for c in scope}
    pts_por_calle: dict = defaultdict(list)
    for r in filas_pt:
        cn = normalizar_calle(r[2] or "")
        if cn in scope_norms:
            pts_por_calle[cn].append((float(r[0]), float(r[1]), _numero(r[3])))
    if not pts_por_calle:
        return None, "El relevamiento anterior no tiene direcciones geocodificadas", None, None, None
    return scope, pts_por_calle, ciudad, region_id, nombre


@app.post("/api/baselines/{baseline_id}/calles/preview")
async def actualizacion_calles_preview(
    baseline_id: str,
    calles: str = Form(...),
    ancho_calle_m: float = Form(40.0),
) -> JSONResponse:
    """Genera el polígono de la zona (cuadras de 100×40 m por dirección) SIN crear el survey, para
    que el operador lo PREVISUALICE y lo ajuste en el mapa antes de lanzar."""
    try:
        lista = json.loads(calles)
        assert isinstance(lista, list) and lista
    except Exception:
        return JSONResponse({"ok": False, "error": "Lista de calles inválida"}, status_code=400)
    scope, pts, ciudad, *_ = _scope_y_puntos_de_calles(baseline_id, lista)
    if scope is None:
        return JSONResponse({"ok": False, "error": pts}, status_code=400)
    geojson = await asyncio.to_thread(_poligono_de_calles, scope, pts, ancho_calle_m, ciudad)
    if not geojson:
        return JSONResponse({"ok": False, "error":
                             "No se pudo construir la zona (OSM no respondió)"}, status_code=422)
    return JSONResponse({"ok": True, "geojson": geojson, "calles": len(scope)})


@app.post("/api/baselines/{baseline_id}/crear-survey-calles")
async def actualizacion_crear_survey_calles(
    baseline_id: str,
    calles: str = Form(...),     # JSON: lista editada [{calle, calle_norm, num_min, num_max}]
    ancho_calle_m: float = Form(40.0),   # ancho del corredor sobre el eje de calle (m)
    zone_geojson: str = Form(None),      # polígono ya editado por el operador (override de la generación)
) -> JSONResponse:
    """Crea el survey en modo CALLE+RANGO: construye el polígono de descarga (cuadras de 100×40 m
    por dirección sobre la geometría OSM) → zone_geojson, y guarda `scope_calles` (filtro estricto
    que aplica ScopeCallesFilter tras el BCI). Si el operador editó la zona en el mapa
    (`zone_geojson`), se usa ese polígono en vez de regenerarlo."""
    try:
        lista = json.loads(calles)
        assert isinstance(lista, list) and lista
    except Exception:
        return JSONResponse({"ok": False, "error": "Lista de calles inválida"}, status_code=400)
    scope, pts_por_calle, ciudad, region_id, nombre = _scope_y_puntos_de_calles(baseline_id, lista)
    if scope is None:
        err = pts_por_calle
        code = 404 if err == "Baseline no encontrado" else 400
        return JSONResponse({"ok": False, "error": err}, status_code=code)
    engine = get_engine()

    # Polígono: el editado por el operador (override) o el autogenerado (cuadras 100×40 por dir.).
    geojson = None
    if zone_geojson:
        try:
            gj = json.loads(zone_geojson)
            geojson = gj.get("geometry", gj) if isinstance(gj, dict) else None
            assert geojson and geojson.get("type") in ("Polygon", "MultiPolygon")
        except Exception:
            return JSONResponse({"ok": False, "error": "zona editada inválida"}, status_code=400)
    if geojson is None:
        geojson = await asyncio.to_thread(_poligono_de_calles, scope, pts_por_calle,
                                          ancho_calle_m, ciudad)
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
