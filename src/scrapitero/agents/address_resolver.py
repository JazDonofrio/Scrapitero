"""AddressResolver — resuelve direcciones de parcelas sin dirección o incompletas.

Estrategia (en orden de preferencia, de menor a mayor costo):
  1. Interpolación por Faces de Logradouros IBGE (gratis, solo Brasil)
     Busca el segmento de calle más cercano en la tabla `logradouros` e
     interpola el número según la posición del centroide a lo largo del eje.
  2. Google Maps Geocoding API (USD 0.005/llamada, cualquier país)
     Fallback cuando no hay logradouros en DB o la interpolación falla.
     También rellena parcelas donde la calle existe pero falta el número.

El idioma de respuesta se detecta automáticamente desde region_id:
  - termina en -br  → pt-BR
  - termina en -ar  → es-AR
  - otro            → es

Requiere variable de entorno:
  GOOGLE_MAPS_API_KEY
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.db.engine import get_engine
from scrapitero.agents._run import agent_run


# ── Pydantic I/O ──────────────────────────────────────────────────────────────

class AddressResolverInput(BaseModel):
    region_id: str                          # "vg-mt-br", "ituzaingo-ba-ar", etc.
    survey_id: Optional[str] = None        # UUID — si se omite procesa toda la región
    batch_size: int = 100                  # máximo de parcelas por corrida
    delay_ms: int = 50                     # delay entre llamadas para no superar quota
    fill_partial: bool = True              # también rellenar parcelas con calle pero sin numero
    # Paso 0: reverse contra el catastro (gratis, autoritativo) antes de IBGE/Google.
    usar_catastro: bool = True
    # No pedirle a Google la dirección de una parcela que YA sale al entregable con número.
    #
    # ⚠ Esto es plata. El filtro de abajo busca `numero IS NULL`, y en la provincia de Buenos
    # Aires eso es CASI TODO el relevamiento: ARBA no publica el domicilio del lote, así que
    # `parcelas.numero` queda vacío incluso donde el número ya lo puso una fuente gratuita en
    # `numero_estimado` (el callejero oficial de georef, las alturas de ARBA). Medido en Merlo
    # el 17-sep-2026: 94.370 parcelas entraban al filtro y 74.060 de ellas **ya tenían número
    # utilizable**, así que un `batch_size` de 500 se habría gastado entero en preguntar lo que
    # ya sabíamos. Con el corte, la muestra cae sobre las 20.480 que de verdad faltan.
    #
    # El criterio es el MISMO del export (`_NUMERO_CONF_MIN` y `_NUMERO_METODOS_EXCLUIDOS` en
    # `web/app.py`): si el CSV va a sacar ese número, no hay nada que preguntar. En `False`
    # vuelve el barrido completo, que sirve para contrastar una estimación contra Google.
    saltear_con_numero_util: bool = True
    # Para MEDIR antes de gastar: sin esto el `LIMIT` sigue el orden físico de la tabla y las
    # 500 consultas caen casi todas en la misma calle (las parcelas se insertaron por cuadra),
    # así que la muestra mide una esquina del partido y no el partido.
    orden_aleatorio: bool = False
    # MODO ANCLA: en vez de preguntarle a Google el número de CADA lote que falta, se le
    # pregunta el de unos pocos repartidos a lo largo de cada calle y después
    # `NumeroEstimator` interpola el resto entre ellos — que es lo que ya hace con las
    # alturas de ARBA y con las cuadras de georef. La diferencia es de dos órdenes de
    # magnitud: medido en Merlo el 17-sep-2026, numerar las 18.546 parcelas sin altura
    # cuesta ~USD 93 preguntando una por una y **1.806 consultas** pidiendo una ancla cada
    # 15 lotes — o sea entra en el tramo gratuito mensual de Google (10.000 llamadas por
    # SKU) y sale sin gastar.
    #
    # Las anclas se reparten sobre el EJE de la calle (orden por componente principal de sus
    # parcelas), no al azar: lo que decide el error de la interpolación es la distancia al
    # ancla más cercana, así que tres anclas juntas en una punta no sirven de nada. Las
    # calles de 1 o 2 parcelas se preguntan enteras: interpolar entre dos puntos no ahorra.
    anclas_por_calle: bool = False
    anclas_cada: int = 15          # 1 ancla cada N parcelas sin número
    anclas_min: int = 3            # mínimo por calle (lo que `NumeroEstimator` exige)
    anclas_max: int = 60           # tope por calle, para que una avenida no se coma el lote
    numero_conf_min: float = 0.4
    numero_metodos_excluidos: list = ["georef_osm"]


class AddressResolverOutput(BaseModel):
    ok: bool
    parcelas_procesadas: int               # total encontradas sin dirección completa
    parcelas_resueltas: int                # con dirección encontrada (cualquier fuente)
    parcelas_resueltas_catastro: int = 0  # resueltas por reverse contra el catastro (gratis, oficial)
    parcelas_resueltas_logradouros: int = 0  # resueltas gratis por interpolación IBGE
    parcelas_resueltas_google: int = 0    # resueltas por Google Maps API
    parcelas_sin_resultado: int            # sin resultado en ninguna fuente
    costo_estimado_usd: float              # ~0.005 USD por llamada Google (SKU: Geocoding)
    error: Optional[str] = None


# ── Componentes de dirección Google Maps ─────────────────────────────────────

_COMPONENT_MAP = {
    "route":                        "calle",
    "street_number":                "numero",
    "sublocality_level_1":          "barrio",
    "sublocality":                  "barrio",
    "neighborhood":                 "barrio",
    "administrative_area_level_2":  "municipio",
    "administrative_area_level_1":  "estado_provincia",
    "country":                      "pais",
    "postal_code":                  "codigo_postal",
}


def _parse_components(components: list[dict]) -> dict:
    """Extrae un dict con calle, numero, barrio, etc. desde los components de Google."""
    result: dict[str, str] = {}
    for comp in components:
        for gtype in comp.get("types", []):
            field = _COMPONENT_MAP.get(gtype)
            if field and field not in result:
                result[field] = comp.get("long_name", "")
    return result


# ── Caché de reverse geocoding (coord→dirección) — migración 032 ──────────────
# El mismo punto físico se re-resuelve en re-runs, sub-zonas y regiones "copia"
# (mismo centroide en dos regiones). Cacheamos por coordenada redondeada + idioma
# para no re-pagarle a Google por el mismo lugar. Sólo se cachean resultados útiles
# (con calle o número); los vacíos quedan sin cachear (se reintentan, son baratos).

def _rev_key(lat: float, lng: float, language: str) -> str:
    return f"{lat:.6f}|{lng:.6f}|{language}"


def _reverse_geocode_cached(engine, lat: float, lng: float, api_key: str,
                            language: str) -> tuple[dict, bool]:
    """Devuelve (componentes_parseados, desde_cache). Reusa el caché por coordenada;
    en miss pega a Google, parsea y guarda el resultado si es útil."""
    clave = _rev_key(lat, lng, language)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT componentes FROM reverse_geocode_cache WHERE clave = :k"),
            {"k": clave},
        ).fetchone()
    if row is not None:
        return (row[0] or {}), True

    resultado = _reverse_geocode(lat, lng, api_key, language)
    addr_g = _parse_components(resultado.get("address_components", [])) if resultado else {}
    if addr_g.get("calle") or addr_g.get("numero"):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO reverse_geocode_cache (clave, componentes)
                VALUES (:k, CAST(:comp AS JSONB))
                ON CONFLICT (clave) DO NOTHING
            """), {"k": clave, "comp": json.dumps(addr_g, ensure_ascii=False)})
    return addr_g, False


def _detect_language(region_id: str) -> str:
    """Detecta el idioma de respuesta de Google Maps según el region_id."""
    rid = region_id.lower()
    if rid.endswith("-br") or "-br-" in rid:
        return "pt-BR"
    if rid.endswith("-ar") or "-ar-" in rid:
        return "es-AR"
    return "es"


def _reverse_geocode(lat: float, lng: float, api_key: str, language: str = "es") -> Optional[dict]:
    """
    Llama a Google Maps Geocoding API con lat/lng.
    Devuelve el primer resultado o None si no hay resultados.
    El idioma de respuesta es configurable (pt-BR, es-AR, es, etc.).
    """
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "latlng": f"{lat},{lng}",
        "key": api_key,
        "language": language,
        "result_type": "street_address|premise|subpremise",
    }
    try:
        with httpx.Client(timeout=15) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
        data = r.json()
        if data.get("status") == "OK" and data.get("results"):
            return data["results"][0]
        if data.get("status") not in ("OK", "ZERO_RESULTS"):
            logger.warning(f"Google Maps status inesperado: {data.get('status')} — {data.get('error_message','')}")
        return None
    except Exception as e:
        logger.warning(f"Error llamando Google Maps: {e}")
        return None


# ── Estrategia 1: interpolación por Logradouros IBGE ─────────────────────────

def _resolve_from_logradouros(engine, lat: float, lng: float, region_id: str) -> Optional[dict]:
    """
    Busca el segmento de calle más cercano en la tabla `logradouros` e interpola
    el número de puerta según la posición del centroide a lo largo del eje.

    Devuelve un dict con calle, numero, cep (si disponible) o None si no hay
    logradouros en DB o el segmento está a más de 100 m.
    """
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT
                logradouro_id,
                nome_logradouro,
                tipo_logradouro,
                titulo_logradouro,
                nro_inicial_esq, nro_final_esq,
                nro_inicial_dir, nro_final_dir,
                cep_esq, cep_dir,
                nom_municipio,
                ST_Distance(
                    geometry::geography,
                    ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography
                ) AS dist_m,
                ST_LineLocatePoint(
                    geometry,
                    ST_ClosestPoint(geometry, ST_SetSRID(ST_MakePoint(:lng, :lat), 4326))
                ) AS frac
            FROM logradouros
            WHERE region_id = :region
              AND nome_logradouro IS NOT NULL
            ORDER BY geometry <-> ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)
            LIMIT 1
        """), {"lat": lat, "lng": lng, "region": region_id}).fetchone()

    if row is None:
        return None

    dist_m = row[11]
    if dist_m > 100:  # más de 100 m → no es confiable
        return None

    frac = row[12]  # 0.0 = inicio, 1.0 = fin del segmento

    # Interpolar número según fracción a lo largo del segmento
    # Usamos lado izquierdo como primario; si no hay, lado derecho
    ini_e, fin_e = row[4], row[5]
    ini_d, fin_d = row[6], row[7]

    numero: Optional[int] = None
    if ini_e is not None and fin_e is not None and ini_e > 0 and fin_e > 0:
        numero = int(round(ini_e + frac * (fin_e - ini_e)))
    elif ini_d is not None and fin_d is not None and ini_d > 0 and fin_d > 0:
        numero = int(round(ini_d + frac * (fin_d - ini_d)))

    nome = row[1] or ""
    tipo = row[2] or ""
    titulo = row[3] or ""
    calle_parts = [p for p in [tipo, titulo, nome] if p]
    calle = " ".join(calle_parts).strip() or None

    cep = row[8] or row[9]  # cep_esq o cep_dir

    return {
        "calle":       calle,
        "numero":      str(numero) if numero else None,
        "municipio":   row[10],
        "codigo_postal": cep,
        "source":      "ibge_logradouros",
    }


# ── Consulta y actualización en DB ────────────────────────────────────────────

def _fetch_parcelas_sin_direccion(
    engine, region_id: str, survey_id: Optional[str], batch_size: int, fill_partial: bool,
    saltear_util: bool = True, conf_min: float = 0.4, excluidos: Optional[list] = None,
    aleatorio: bool = False
) -> list[dict]:
    """Devuelve parcelas con coordenadas pero sin calle o sin número.

    Cada fila incluye `calle_existente` para saber si la calle ya está y solo
    hay que completar el número (dirección parcial).
    """
    with engine.connect() as conn:
        base = """
            SELECT parcela_id::text, centroid_lat, centroid_lng, calle
            FROM parcelas
            WHERE region_id = :region
              AND centroid_lat IS NOT NULL
              AND centroid_lng IS NOT NULL
        """
        params: dict = {"region": region_id}

        if fill_partial:
            # Sin calle O con calle pero sin número
            base += " AND (calle IS NULL OR calle = '' OR numero IS NULL OR numero = '')"
        else:
            base += " AND (calle IS NULL OR calle = '')"

        if saltear_util:
            # La estimación gratuita que el export ya da por buena hace innecesaria la consulta.
            base += """ AND NOT (numero_estimado IS NOT NULL
                                 AND COALESCE(numero_estimado_confianza, 0) >= :conf
                                 AND COALESCE(numero_estimado_metodo, '') <> ALL(:excl))"""
            params["conf"] = conf_min
            params["excl"] = list(excluidos or ["georef_osm"])

        if survey_id:
            base += " AND survey_id = :survey_id"
            params["survey_id"] = survey_id
        if aleatorio:
            base += " ORDER BY random()"
        base += " LIMIT :batch"
        params["batch"] = batch_size

        rows = conn.execute(text(base), params).fetchall()
        return [
            {
                "parcela_id":      r[0],
                "lat":             r[1],
                "lng":             r[2],
                "calle_existente": r[3],   # None si no hay calle, str si hay calle pero falta numero
            }
            for r in rows
        ]


def _anclas_por_calle(
    engine, region_id: str, survey_id: Optional[str], tope: int,
    cada: int, minimo: int, maximo: int, conf_min: float, excluidos: Optional[list]
) -> list[dict]:
    """Elige, por calle, unas pocas parcelas repartidas a lo largo del eje para preguntarle
    el número a Google y usarlas después como anclas de la interpolación.

    El reparto es por **componente principal** de la nube de puntos de la calle: una calle es
    aproximadamente recta a escala de partido, así que proyectar sobre su eje mayor y tomar
    posiciones equiespaciadas deja las anclas separadas de punta a punta. Ordenar por lat o
    lng a secas fallaría en las calles diagonales, que en el conurbano son muchas.
    """
    import numpy as np

    cond_excl = ""
    params: dict = {"region": region_id}
    if excluidos is not None:
        cond_excl = """ AND NOT (numero_estimado IS NOT NULL
                                 AND COALESCE(numero_estimado_confianza, 0) >= :conf
                                 AND COALESCE(numero_estimado_metodo, '') <> ALL(:excl))"""
        params["conf"] = conf_min
        params["excl"] = list(excluidos or ["georef_osm"])
    cond_survey = ""
    if survey_id:
        cond_survey = " AND survey_id = :survey_id"
        params["survey_id"] = survey_id

    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT parcela_id::text, centroid_lat, centroid_lng, calle
            FROM parcelas
            WHERE region_id = :region
              AND centroid_lat IS NOT NULL AND centroid_lng IS NOT NULL
              AND calle IS NOT NULL AND calle <> ''
              AND (numero IS NULL OR numero = '' OR numero = '0')
              {cond_excl}{cond_survey}
        """), params).fetchall()

    por_calle: dict = {}
    for pid, la, lo, calle in rows:
        por_calle.setdefault(calle, []).append((pid, float(la), float(lo)))

    elegidas: list[dict] = []
    for calle, lst in sorted(por_calle.items(), key=lambda kv: -len(kv[1])):
        n = len(lst)
        if n <= 2:                       # preguntar las dos sale igual que anclarlas
            k = n
            orden = lst
        else:
            k = max(minimo, min(maximo, round(n / max(cada, 1))))
            k = min(k, n)
            pts = np.array([[p[1], p[2]] for p in lst], dtype=float)
            centrado = pts - pts.mean(axis=0)
            # eje mayor de la nube = 1er autovector de la covarianza
            try:
                _, _, vt = np.linalg.svd(centrado, full_matrices=False)
                t = centrado @ vt[0]
            except Exception:  # noqa: BLE001
                t = centrado[:, 0]
            orden = [lst[i] for i in np.argsort(t)]
        # posiciones equiespaciadas sobre el eje (incluye las dos puntas)
        if k >= len(orden):
            sel = orden
        else:
            paso = (len(orden) - 1) / (k - 1) if k > 1 else 0
            sel = [orden[int(round(i * paso))] for i in range(k)]
        for pid, la, lo in sel:
            elegidas.append({"parcela_id": pid, "lat": la, "lng": lo,
                             "calle_existente": calle})

    logger.info(f"AddressResolver: modo ancla — {len(elegidas)} anclas en {len(por_calle)} "
                f"calles (cubren {len(rows)} parcelas sin número)")
    return elegidas[:tope]


def _update_parcela_direccion(conn, parcela_id: str, addr: dict, preserve_calle: bool = False) -> None:
    """Actualiza los campos de dirección de una parcela.

    Si `preserve_calle=True` (calle ya estaba en DB), no sobreescribe `calle` ni campos
    de localidad — solo actualiza `numero`, `barrio` y `codigo_postal`.
    """
    source = addr.get("source", "google_maps")
    # `catastro` es la dirección oficial del inmueble (no una inferencia) → confianza máxima.
    confidence = {"catastro": 0.99, "ibge_logradouros": 0.80}.get(source, 0.85)

    if preserve_calle:
        conn.execute(text("""
            UPDATE parcelas SET
                numero             = COALESCE(:numero, numero),
                barrio             = COALESCE(:barrio, barrio),
                codigo_postal      = COALESCE(:cp, codigo_postal),
                direccion_source   = :source,
                direccion_confidence = :conf
            WHERE parcela_id = :pid
        """), {
            "numero": addr.get("numero"),
            "barrio": addr.get("barrio"),
            "cp":     addr.get("codigo_postal"),
            "source": source,
            "conf":   confidence,
            "pid":    parcela_id,
        })
    else:
        conn.execute(text("""
            UPDATE parcelas SET
                calle              = :calle,
                numero             = :numero,
                barrio             = :barrio,
                municipio          = :municipio,
                estado_provincia   = :estado,
                pais               = :pais,
                codigo_postal      = :cp,
                direccion_source   = :source,
                direccion_confidence = :conf
            WHERE parcela_id = :pid
        """), {
            "calle":     addr.get("calle"),
            "numero":    addr.get("numero"),
            "barrio":    addr.get("barrio"),
            "municipio": addr.get("municipio"),
            "estado":    addr.get("estado_provincia"),
            "pais":      addr.get("pais"),
            "cp":        addr.get("codigo_postal"),
            "source":    source,
            "conf":      confidence,
            "pid":       parcela_id,
        })


# ── Entry point ───────────────────────────────────────────────────────────────

@agent_run
def run(input: AddressResolverInput) -> AddressResolverOutput:
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        logger.warning("GOOGLE_MAPS_API_KEY no configurada — usando solo interpolación IBGE")

    language = _detect_language(input.region_id)
    engine = get_engine()

    try:
        parcelas = (_anclas_por_calle(
            engine, input.region_id, input.survey_id, input.batch_size,
            input.anclas_cada, input.anclas_min, input.anclas_max,
            input.numero_conf_min,
            input.numero_metodos_excluidos if input.saltear_con_numero_util else None)
            if input.anclas_por_calle else _fetch_parcelas_sin_direccion(
            engine, input.region_id, input.survey_id, input.batch_size, input.fill_partial,
            input.saltear_con_numero_util, input.numero_conf_min,
            input.numero_metodos_excluidos, input.orden_aleatorio
        ))
        logger.info(
            f"AddressResolver [{input.region_id}] lang={language}: "
            f"{len(parcelas)} parcelas a resolver"
        )

        resueltas_logr = 0
        resueltas_google = 0
        resueltas_catastro = 0
        sin_resultado = 0
        google_calls = 0
        cache_hits = 0

        # ── Estrategia 0: REVERSE contra el CATASTRO (gratis y autoritativo) ──
        # Si el centroide de la parcela cae dentro de OTRA parcela que sí tiene dirección
        # oficial (típico: subdivisiones/PH sobre un lote catalogado, o parcelas de una capa
        # sin dirección superpuestas a una con BCI), esa dirección manda. Es preferible a
        # preguntarle a Google la dirección de un punto cuya dirección el municipio ya
        # publicó — además de gratis, no tiene la ambigüedad del reverse comercial.
        rev: dict = {}
        if input.usar_catastro and parcelas:
            try:
                from scrapitero.agents.catastro_geocoder import reverse_lote
                rev = reverse_lote([(p["lat"], p["lng"]) for p in parcelas],
                                   region_id=input.region_id)
            except Exception as exc:  # noqa: BLE001 — best-effort, sigue con IBGE/Google
                logger.warning(f"AddressResolver: reverse por catastro no disponible: {exc!r}")

        with engine.begin() as conn:
            for p in parcelas:
                preserve_calle = bool(p["calle_existente"])  # True = solo completar numero

                cat = rev.get((float(p["lat"]), float(p["lng"])))
                # Solo sirve si aporta lo que falta (número para las parciales, calle para
                # las vacías) y no es la propia parcela.
                if cat and cat.get("parcela_id") != p["parcela_id"]:
                    aporta = cat.get("numero") if preserve_calle else cat.get("calle")
                    if aporta:
                        _update_parcela_direccion(conn, p["parcela_id"], {
                            "calle": cat.get("calle"), "numero": cat.get("numero"),
                            "codigo_postal": cat.get("codigo_postal"),
                            "barrio": cat.get("barrio"), "source": "catastro",
                        }, preserve_calle)
                        resueltas_catastro += 1
                        continue

                # ── Estrategia 1: interpolación IBGE (gratis, solo si no hay calle) ──
                if not preserve_calle:
                    addr = _resolve_from_logradouros(engine, p["lat"], p["lng"], input.region_id)
                    if addr and addr.get("calle"):
                        _update_parcela_direccion(conn, p["parcela_id"], addr, preserve_calle=False)
                        resueltas_logr += 1
                        logger.debug(
                            f"✓ IBGE {p['parcela_id'][:8]}… → "
                            f"{addr.get('calle', '')} {addr.get('numero', '')}"
                        )
                        continue

                # ── Estrategia 2: Google Maps API (con caché coord→dirección) ──
                if not api_key:
                    sin_resultado += 1
                    continue

                addr_g, desde_cache = _reverse_geocode_cached(
                    engine, p["lat"], p["lng"], api_key, language)
                if desde_cache:
                    cache_hits += 1
                else:
                    google_calls += 1

                # Para parciales: basta con encontrar numero; para completas: necesitamos calle
                useful = addr_g.get("numero") if preserve_calle else addr_g.get("calle")
                if useful:
                    _update_parcela_direccion(conn, p["parcela_id"], addr_g, preserve_calle)
                    resueltas_google += 1
                    logger.debug(
                        f"✓ Google{' [cache]' if desde_cache else ''}"
                        f"{' [parcial]' if preserve_calle else ''} "
                        f"{p['parcela_id'][:8]}… → "
                        f"{p.get('calle_existente') or addr_g.get('calle', '')} "
                        f"{addr_g.get('numero', '')}"
                    )
                else:
                    sin_resultado += 1

                # Throttle sólo cuando realmente pegamos a Google (no en cache hit)
                if not desde_cache and input.delay_ms > 0:
                    time.sleep(input.delay_ms / 1000)

        resueltas = resueltas_catastro + resueltas_logr + resueltas_google
        costo = google_calls * 0.005  # USD por llamada Geocoding API

        logger.info(
            f"AddressResolver: {resueltas} resueltas "
            f"({resueltas_catastro} catastro gratis/oficial, {resueltas_logr} IBGE gratis, "
            f"{resueltas_google} Google), "
            f"{sin_resultado} sin resultado, {cache_hits} de caché (sin costo). "
            f"Costo estimado: USD {costo:.2f}"
        )

        return AddressResolverOutput(
            ok=True,
            parcelas_procesadas=len(parcelas),
            parcelas_resueltas=resueltas,
            parcelas_resueltas_catastro=resueltas_catastro,
            parcelas_resueltas_logradouros=resueltas_logr,
            parcelas_resueltas_google=resueltas_google,
            parcelas_sin_resultado=sin_resultado,
            costo_estimado_usd=round(costo, 4),
        )

    except Exception as e:
        logger.exception("AddressResolver falló")
        return AddressResolverOutput(
            ok=False, parcelas_procesadas=0, parcelas_resueltas=0,
            parcelas_resueltas_logradouros=0, parcelas_resueltas_google=0,
            parcelas_sin_resultado=0, costo_estimado_usd=0.0,
            error=str(e)
        )
