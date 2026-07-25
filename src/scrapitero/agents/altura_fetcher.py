"""AlturaFetcher — altura satelital del edificio de cada parcela, para REVISIÓN visual.

Por qué existe: el **BCI no trae cantidad de pisos** (verificado sobre los PDFs de VG: `PISO
CERAMICA` es el material del piso, `PAVIMENTAÇÃO` el de la calle, y `NIVEL 1,00` un
coeficiente de valuación), y el footprint 2D de [[project_footprint_fetcher]] no distingue
una casa de un edificio de 8 pisos con la misma huella. Lo único que había era un proxy
circular: `ceil(area_construida / (FOS × area_terreno))`, que usa el área del **catastro** —
justo el dato que puede estar desactualizado si hubo obra no declarada.

Fuente (elegida tras comparar Mapbox/Overture/Open Buildings 2.5D — ver
[[project_fuentes_altura_edificios]]):
  - **Google Solar API** `buildingInsights:findClosest` → `planeHeightAtCenterMeters` por
    segmento de techo. **Es elevación sobre el nivel del mar, NO altura del edificio.**
  - **Google Elevation API** → elevación del terreno en el mismo punto. `altura = techo −
    terreno`. Se pide en **lote** (hasta 512 coords por request) ⇒ su costo es despreciable.
  - Pisos ≈ `altura_m / metros_por_piso` (default 3 m, la convención que usa OSM).

Costo: Solar tiene **10.000 buildingInsights/mes gratis** (VG entero son ~464) y Elevation
sale ~USD 5/1000 pero batcheada son 1-2 requests. Igual se respeta un tope `max_requests`
con aviso por Telegram, como `GooglePlacesFetcher`.

Limitaciones reales (medidas, no documentadas — se persisten para que el operador las vea):
  - `imagery_year`: la imagen de Solar en VG es **2014** en casi todos los puntos (uno dio
    2025). El dato NO es "estado actual" y la UI tiene que mostrar el año.
  - `findClosest` devuelve **UN** edificio (el más cercano al centroide): en una parcela
    grande/multi-edificio puede ser un anexo, no el cuerpo principal. Se guarda
    `ground_area_m2` para poder detectarlo.
  - El terreno de Elevation viene de SRTM (~30 m) ⇒ en pendiente mete ruido de varios metros.

Escribe `parcela_altura` (mig. 045). Idempotente y **resumible**: por default solo procesa
parcelas sin dato (`solo_faltantes`), así un corte por tope se reanuda sin re-pagar.
NO toca `parcelas` ni el relevamiento.
"""

from __future__ import annotations

import math
import os
import time
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.baseline_geocoder import _tg
from scrapitero.db.engine import get_engine

_SOLAR_URL = "https://solar.googleapis.com/v1/buildingInsights:findClosest"
_ELEVATION_URL = "https://maps.googleapis.com/maps/api/elevation/json"
_ELEVATION_BATCH = 250          # la API admite ~512 coords; 250 deja margen de URL
_FOS_DEFAULT = 0.6              # mismo factor de ocupación que el proxy de la web
_COST_SOLAR = 0.01              # USD aprox. por buildingInsights fuera del free tier


class AlturaInput(BaseModel):
    region_id: str
    survey_id: str
    metros_por_piso: float = 3.0
    max_requests: int = 600          # tope de llamadas a Solar (corta y devuelve parcial)
    solo_faltantes: bool = True      # resumible: saltea las parcelas que ya tienen altura
    # Umbral de discrepancia: diferencia de pisos entre satélite y proxy del catastro
    # a partir de la cual se marca para revisar.
    delta_pisos: int = 1
    # Piso de altura para considerar que hay algo construido (bajo esto = ruido de SRTM).
    altura_min_m: float = 2.0
    # Huella mínima (m²) para creerle a un edificio sobre una parcela que el catastro
    # declara sin construcción. Filtra tinglados/ruido del modelo de Solar.
    huella_min_m2: float = 20.0
    # Solar API limita ~100 queries/minuto: sin throttle la corrida choca con un 429 a mitad
    # de camino (medido). 0,7 s deja margen y hace ~85 req/min.
    throttle_s: float = 0.7


class AlturaOutput(BaseModel):
    ok: bool = True
    error: Optional[str] = None
    parcelas_consultadas: int = 0
    con_altura: int = 0
    discrepancias: int = 0
    disc_sin_declarar: int = 0       # catastro sin construcción, satélite ve edificio
    disc_mas_alto: int = 0           # satélite ve más pisos que el proxy del catastro
    sin_edificio: int = 0            # Solar no encontró edificio cerca del centroide
    requests_solar: int = 0
    cap_alcanzado: bool = False
    parcial: bool = False            # quedaron parcelas sin procesar (re-invocar continúa)


# ── Fuentes ───────────────────────────────────────────────────────────────────────────

def _solar_edificio(client: httpx.Client, lat: float, lng: float, key: str) -> Optional[dict]:
    """Datos del edificio más cercano al punto, o None si no hay cobertura/edificio.

    Devuelve techo_msnm (el plano MÁS ALTO del techo), áreas y metadata de la imagen.
    Ante un 429 (cuota por minuto) espera y reintenta: el límite es por minuto, así que
    frenar un rato destraba la corrida en vez de abortarla."""
    r = None
    for espera in (20, 40, 60):
        r = client.get(_SOLAR_URL, params={
            "location.latitude": lat, "location.longitude": lng,
            "requiredQuality": "BASE", "key": key,
        }, timeout=30)
        if r.status_code != 429:
            break
        logger.warning(f"Solar API 429 (cuota por minuto): esperando {espera}s y reintentando…")
        time.sleep(espera)

    if r.status_code == 404:
        return None                      # sin edificio/cobertura en ese punto (caso normal)
    if r.status_code != 200:
        detalle = ""
        try:
            detalle = (r.json().get("error") or {}).get("message", "")
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"Solar API HTTP {r.status_code}: {detalle[:200]}")

    d = r.json()
    sp = d.get("solarPotential") or {}
    alturas = [s["planeHeightAtCenterMeters"] for s in sp.get("roofSegmentStats", [])
               if s.get("planeHeightAtCenterMeters") is not None]
    if not alturas:
        return None
    stats = sp.get("buildingStats") or {}
    centro = d.get("center") or {}
    return {
        "techo_msnm": max(alturas),
        "ground_area_m2": stats.get("groundAreaMeters2"),
        "roof_area_m2": stats.get("areaMeters2"),
        "imagery_year": (d.get("imageryDate") or {}).get("year"),
        "imagery_quality": d.get("imageryQuality"),
        # Dónde está el edificio que se midió. `findClosest` devuelve el MÁS CERCANO al
        # punto, no necesariamente el de la parcela: en un lote vacío es siempre el del
        # vecino. Sin esta coordenada no se puede distinguir un caso real de ese artefacto.
        "edificio_lat": centro.get("latitude"),
        "edificio_lng": centro.get("longitude"),
    }


def _elevacion_lote(client: httpx.Client, puntos: list[tuple], key: str) -> dict:
    """{(lat,lng): elevacion_m} pidiendo el terreno en LOTE (no una request por punto)."""
    out: dict[tuple, float] = {}
    for i in range(0, len(puntos), _ELEVATION_BATCH):
        chunk = puntos[i:i + _ELEVATION_BATCH]
        locs = "|".join(f"{lat},{lng}" for lat, lng in chunk)
        try:
            r = client.get(_ELEVATION_URL, params={"locations": locs, "key": key}, timeout=60)
            j = r.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Elevation API falló en el lote {i}: {exc!r}")
            continue
        if j.get("status") != "OK":
            logger.warning(f"Elevation API status={j.get('status')} en el lote {i}")
            continue
        for punto, res in zip(chunk, j.get("results", [])):
            if res.get("elevation") is not None:
                out[punto] = float(res["elevation"])
    return out


# ── Proxy del catastro (para contrastar) ──────────────────────────────────────────────

def _hay_footprints(engine, survey_id: str) -> bool:
    """True si este survey tiene la capa de footprints corrida. Si no corrió, `n_fp` vale 0
    para todas las parcelas y no se puede usar como guarda (anularía la capa completa)."""
    with engine.connect() as conn:
        return bool(conn.execute(text(
            "SELECT 1 FROM footprints_revision WHERE survey_id = :sid LIMIT 1"),
            {"sid": survey_id}).fetchone())


def _pisos_proxy_bci(area_terreno, area_construida) -> Optional[int]:
    """Pisos mínimos según el catastro: ceil(construida / (FOS·terreno)).

    Mismo cálculo que `_pisos_estimados` de la web (web/app.py) — se replica acá para no
    importar el módulo web desde un agente."""
    if not area_terreno or not area_construida or area_terreno <= 0:
        return None
    return max(1, math.ceil(area_construida / (_FOS_DEFAULT * area_terreno)))


# ── Entry point ───────────────────────────────────────────────────────────────────────

@agent_run
def run(input: AlturaInput) -> AlturaOutput:
    out = AlturaOutput()
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        return AlturaOutput(ok=False, error="falta GOOGLE_MAPS_API_KEY en el entorno")

    engine = get_engine()
    filtro_faltantes = ("AND NOT EXISTS (SELECT 1 FROM parcela_altura pa "
                        "WHERE pa.parcela_id = p.parcela_id)") if input.solo_faltantes else ""
    with engine.connect() as conn:
        parcelas = conn.execute(text(f"""
            SELECT p.parcela_id::text, p.centroid_lat, p.centroid_lng,
                   p.area_m2_terreno, p.area_m2_construida
            FROM parcelas p
            WHERE p.survey_id = :sid
              AND p.centroid_lat IS NOT NULL AND p.centroid_lng IS NOT NULL
              {filtro_faltantes}
            ORDER BY p.parcela_id
        """), {"sid": input.survey_id}).fetchall()

    if not parcelas:
        logger.info(f"AlturaFetcher {input.region_id}: nada por procesar")
        return out

    pendientes = len(parcelas)
    a_procesar = parcelas[:input.max_requests]
    out.cap_alcanzado = pendientes > input.max_requests
    out.parcial = out.cap_alcanzado

    _tg(f"📏 <b>Altura</b> ({input.region_id}): consultando {len(a_procesar)} parcela(s) "
        f"a Google Solar (free tier 10k/mes; ≈ USD {len(a_procesar) * _COST_SOLAR:.1f} si se pasa).")

    resultados = []
    with httpx.Client() as client:
        # 1) Solar (1 request por parcela, es el recurso acotado)
        for pid, lat, lng, area_t, area_c in a_procesar:
            lat, lng = float(lat), float(lng)
            try:
                info = _solar_edificio(client, lat, lng, key)
            except RuntimeError as exc:
                # Un error de API (key sin Solar habilitado, cuota) no debe seguir quemando
                # requests: se corta acá y lo que ya se resolvió queda persistido.
                out.error = str(exc)
                out.parcial = True
                logger.error(f"AlturaFetcher cortado: {exc}")
                break
            out.requests_solar += 1
            if input.throttle_s:
                time.sleep(input.throttle_s)
            if info is None:
                out.sin_edificio += 1
                continue
            resultados.append((pid, lat, lng, area_t, area_c, info))

        # 2) Elevation en LOTE para los que sí tienen techo
        terrenos = _elevacion_lote(client, [(lat, lng) for _, lat, lng, _, _, _ in resultados], key)

    # 2b) ¿El edificio medido PERTENECE a la parcela? `findClosest` devuelve el más cercano,
    # así que en un lote vacío mide al vecino y generaba un `sin_declarar` falso (caso
    # DA LIBERDADE 144: edificio a 20,3 m, fuera de la parcela, era la casa de al lado).
    # Se resuelve en una query: ST_Contains del punto del edificio + distancia al centroide,
    # y de paso el conteo de footprints de Open Buildings dentro (2ª fuente satelital).
    pertenencia: dict = {}
    con_centro = [(pid, info["edificio_lat"], info["edificio_lng"])
                  for pid, _la, _ln, _at, _ac, info in resultados
                  if info.get("edificio_lat") is not None]
    if con_centro:
        with engine.connect() as conn:
            for pid, dentro, dist, nfp in conn.execute(text("""
                WITH e AS (
                    SELECT * FROM unnest(CAST(:pids AS uuid[]), CAST(:lats AS float8[]),
                                         CAST(:lngs AS float8[])) AS t(pid, lat, lng)
                )
                SELECT e.pid::text,
                       ST_Contains(p.geometry, ST_SetSRID(ST_MakePoint(e.lng, e.lat), 4326)),
                       ST_Distance(
                           ST_SetSRID(ST_MakePoint(p.centroid_lng, p.centroid_lat), 4326)::geography,
                           ST_SetSRID(ST_MakePoint(e.lng, e.lat), 4326)::geography),
                       (SELECT count(*) FROM footprints_revision f WHERE f.parcela_id = p.parcela_id)
                FROM e JOIN parcelas p ON p.parcela_id = e.pid
            """), {"pids": [c[0] for c in con_centro],
                   "lats": [c[1] for c in con_centro],
                   "lngs": [c[2] for c in con_centro]}).fetchall():
                pertenencia[pid] = (dentro, dist, nfp)

    # 3) Derivar altura/pisos, comparar con el catastro y persistir
    filas = []
    for pid, lat, lng, area_t, area_c, info in resultados:
        terreno = terrenos.get((lat, lng))
        techo = info["techo_msnm"]
        altura = (techo - terreno) if terreno is not None else None
        pisos_sat = None
        if altura is not None and altura >= input.altura_min_m:
            # FLOOR, no round: `techo_msnm` es el plano MÁS ALTO del techo (la cumbrera), así
            # que una casa de una planta con techo a dos aguas mide 4-5 m. Con `round` toda
            # casa de 4,5 m pasaba a "2 pisos" y generaba discrepancias falsas (medido: 4 de
            # 5 marcas en la primera corrida eran esto).
            pisos_sat = max(1, math.floor(altura / input.metros_por_piso))
        pisos_bci = _pisos_proxy_bci(
            float(area_t) if area_t else None, float(area_c) if area_c else None)

        # Dos tipos de discrepancia, ambos en el sentido "hay MÁS construido de lo declarado".
        # No se marca el sentido inverso: que el satélite vea menos suele ser el anexo que
        # devolvió `findClosest` o ruido de SRTM, no información útil.
        dentro, dist_m, n_fp = pertenencia.get(pid, (None, None, None))

        motivo = None
        if pisos_sat and pisos_bci is None and (info["ground_area_m2"] or 0) >= input.huella_min_m2:
            # El catastro no declara construcción (LOTE VAZIO / sin área) pero el satélite ve
            # un edificio con huella real. Es el caso MÁS valioso del análisis... siempre que
            # el edificio SEA de esta parcela. Dos guardas, porque acá está el 56-69% de los
            # falsos positivos (`findClosest` mide al vecino en los lotes vacíos):
            #   1. el edificio tiene que caer DENTRO de la parcela;
            #   2. Google Open Buildings (2ª fuente satelital, independiente de Solar) tiene
            #      que ver al menos una huella dentro. Si el footprint no corrió, `n_fp` es 0
            #      para todos y esta guarda se omite para no anular la capa entera.
            if dentro is False:
                motivo = None
            elif n_fp == 0 and _hay_footprints(engine, input.survey_id):
                motivo = None
            else:
                motivo = "sin_declarar"
        elif pisos_sat and pisos_bci and (pisos_sat - pisos_bci) >= input.delta_pisos:
            # `mas_alto` es mucho más robusto (solo 6-9% sin footprint dentro): la parcela SÍ
            # tiene construcción declarada, así que el edificio medido casi siempre es el suyo.
            # Igual se descarta si se comprobó que el medido está fuera.
            motivo = None if dentro is False else "mas_alto"
        discrepancia = motivo is not None
        filas.append({
            "pid": pid, "rid": input.region_id, "sid": input.survey_id,
            "techo": techo, "terreno": terreno, "altura": altura,
            "pisos_sat": pisos_sat, "pisos_bci": pisos_bci, "disc": discrepancia,
            "motivo": motivo,
            "img_year": info["imagery_year"], "img_q": info["imagery_quality"],
            "ground": info["ground_area_m2"], "roof": info["roof_area_m2"],
            "e_lat": info.get("edificio_lat"), "e_lng": info.get("edificio_lng"),
            "dentro": dentro, "dist_m": dist_m, "n_fp": n_fp,
        })
        if altura is not None:
            out.con_altura += 1
        if motivo == "sin_declarar":
            out.disc_sin_declarar += 1
        elif motivo == "mas_alto":
            out.disc_mas_alto += 1
    out.discrepancias = out.disc_sin_declarar + out.disc_mas_alto

    if filas:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO parcela_altura
                    (parcela_id, region_id, survey_id, techo_msnm, terreno_msnm, altura_m,
                     pisos_satelital, pisos_bci_proxy, discrepancia, motivo, imagery_year,
                     imagery_quality, ground_area_m2, roof_area_m2, source,
                     edificio_lat, edificio_lng, dentro_parcela, edificio_dist_m,
                     footprints_dentro)
                VALUES
                    (:pid, :rid, :sid, :techo, :terreno, :altura,
                     :pisos_sat, :pisos_bci, :disc, :motivo, :img_year,
                     :img_q, :ground, :roof, 'google_solar',
                     :e_lat, :e_lng, :dentro, :dist_m, :n_fp)
                ON CONFLICT (parcela_id) DO UPDATE SET
                    techo_msnm = EXCLUDED.techo_msnm,
                    terreno_msnm = EXCLUDED.terreno_msnm,
                    altura_m = EXCLUDED.altura_m,
                    pisos_satelital = EXCLUDED.pisos_satelital,
                    pisos_bci_proxy = EXCLUDED.pisos_bci_proxy,
                    discrepancia = EXCLUDED.discrepancia,
                    motivo = EXCLUDED.motivo,
                    imagery_year = EXCLUDED.imagery_year,
                    imagery_quality = EXCLUDED.imagery_quality,
                    ground_area_m2 = EXCLUDED.ground_area_m2,
                    roof_area_m2 = EXCLUDED.roof_area_m2,
                    edificio_lat = EXCLUDED.edificio_lat,
                    edificio_lng = EXCLUDED.edificio_lng,
                    dentro_parcela = EXCLUDED.dentro_parcela,
                    edificio_dist_m = EXCLUDED.edificio_dist_m,
                    footprints_dentro = EXCLUDED.footprints_dentro
            """), filas)

    # Lo efectivamente consultado, no lo planificado: si se cortó por cuota/error, `a_procesar`
    # sobreestima y el reporte mentía sobre cuánto se hizo.
    out.parcelas_consultadas = out.requests_solar
    if out.requests_solar < len(a_procesar):
        out.parcial = True
    # Si no se pudo procesar NADA y hubo un error de API, es un fallo, no un parcial: así el
    # endpoint devuelve 422 y `agent_run` sella el error con el slug de la skill para que el
    # operador vea la causa (cuota agotada, key sin Solar habilitado) en vez de un "0 listo".
    if out.requests_solar == 0 and out.error:
        out.ok = False
    logger.info(f"AlturaFetcher {input.region_id}: consultadas={out.parcelas_consultadas} "
                f"con_altura={out.con_altura} discrepancias={out.discrepancias} "
                f"sin_edificio={out.sin_edificio} parcial={out.parcial}")
    _tg(f"📏 <b>Altura</b> ({input.region_id}): {out.con_altura} con altura, "
        f"<b>{out.discrepancias} discrepancia(s)</b> vs catastro "
        f"({out.disc_sin_declarar} sin declarar, {out.disc_mas_alto} más alto)"
        + (f", faltan {pendientes - len(a_procesar)} (re-ejecutar)" if out.cap_alcanzado else "")
        + (f" ⚠ {out.error}" if out.error else ""))
    return out
