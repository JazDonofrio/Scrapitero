"""ARBACartoFetcher — descarga y enriquece parcelas desde carto.arba.gov.ar.

Flujo autónomo (no requiere arba_cadastral_fetcher previo):
  1. Si no hay parcelas en DB para la manzana → las descarga de IDERA WFS
  2. Para cada parcela llama a carto.arba.gov.ar/client/getInfo y extrae:
     - subparcelas (partidas, s_terreno, sp)
     - domicilio registrado en carto
     - nomenclatura catastral

Si necesita JSESSIONID → devuelve needs_cookies=True con instrucciones.
El JSESSIONID se persiste en ARBA_SESSION_FILE para reutilización.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.db.engine import get_engine
from scrapitero.agents._run import agent_run
from scrapitero.agents.arba_cadastral_fetcher import (
    fetch_idera, fetch_idera_spatial, _load_zone_polygon, _upsert_parcelas,
)

# ── Config ────────────────────────────────────────────────────────────────────

CARTO_BASE       = "https://carto.arba.gov.ar/cartoArba"
GMAPS_GEO        = "https://maps.googleapis.com/maps/api/geocode/json"
NOMINATIM        = "https://nominatim.openstreetmap.org/reverse"
COCHERA_M2       = 25   # subparcela < 25 m² → cochera; >= 25 m² → unidad funcional
ARBA_SESSION_FILE = Path(os.environ.get("ARBA_SESSION_FILE",
                                         "/opt/scrapitero/.arba_session.json"))


# ── Pydantic I/O ──────────────────────────────────────────────────────────────

class ARBACartoInput(BaseModel):
    region_id: str
    survey_id: str
    partido_id: Optional[str] = None        # "136" — opcional (solo para vía nomenclatura)
    circunscripcion: Optional[str] = None
    seccion: Optional[str] = None
    manzana: Optional[str] = None
    jsessionid: Optional[str] = None
    cookie_header: Optional[str] = None
    delay_ms: int = 400                     # delay entre requests a carto


class ARBACartoOutput(BaseModel):
    ok: bool
    parcelas_procesadas: int = 0
    parcelas_con_subparcelas: int = 0
    total_uf: int = 0
    total_cocheras: int = 0
    needs_cookies: bool = False
    cookie_instructions: Optional[str] = None
    fuentes: list[str] = []
    error: Optional[str] = None


# ── Sesión ────────────────────────────────────────────────────────────────────

COOKIE_INSTRUCTIONS = (
    "Necesito el JSESSIONID de carto.arba.gov.ar.\n\n"
    "Pasos:\n"
    "1. Abrí Chrome → https://carto.arba.gov.ar/cartoArba/\n"
    "2. F12 → Network → buscá la manzana (Partido, Circunscripción, Sección, Manzana)\n"
    "3. Hacé click en cualquier request a 'getInfo'\n"
    "4. Headers → Request Headers → copiá el valor del header 'Cookie:'\n"
    "5. Enviame ese valor\n\n"
    "Alternativa: F12 → Application → Cookies → carto.arba.gov.ar → valor de JSESSIONID"
)


def _load_session() -> Optional[str]:
    if ARBA_SESSION_FILE.exists():
        try:
            return json.loads(ARBA_SESSION_FILE.read_text()).get("jsessionid")
        except Exception:
            pass
    return None


def _save_session(jsessionid: str) -> None:
    ARBA_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    ARBA_SESSION_FILE.write_text(json.dumps({"jsessionid": jsessionid}))


def _parc_num_from_cca(cca: str) -> str:
    """Extrae el número de parcela del CCA (posición 32-38 + sufijo)."""
    if not cca or len(cca) < 39:
        return ""
    num = cca[32:39].lstrip("0") or "0"
    suffix = cca[39:].lstrip("0")
    return f"{num}{suffix}" if suffix else num


def _nomencla_matches_cca(nomencla: str, cca: str) -> bool:
    """Verifica que la nomenclatura de carto corresponda al CCA de IDERA."""
    if not nomencla or not cca:
        return True  # sin datos suficientes, aceptar
    expected = _parc_num_from_cca(cca)
    if not expected:
        return True
    # Buscar "Parcela: <N>" en la nomenclatura
    import re as _re
    m = _re.search(r"Parcela:\s*(\w+)", nomencla, _re.IGNORECASE)
    if not m:
        return True
    return m.group(1).upper() == expected.upper()


def _extract_jsessionid(cookie_header: str) -> Optional[str]:
    # Eliminar prefijo "Cookie:" si el usuario copió el header completo
    value = re.sub(r"(?i)^cookie\s*:\s*", "", cookie_header.strip())
    for part in value.split(";"):
        if part.strip().upper().startswith("JSESSIONID="):
            return part.split("=", 1)[1].strip()
    return None


# ── Conversión de coordenadas ─────────────────────────────────────────────────

def _to_3857(lon: float, lat: float) -> tuple[float, float]:
    x = lon * 20037508.34 / 180
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * 20037508.34 / math.pi
    return x, y


# ── carto.arba.gov.ar/client/getInfo ─────────────────────────────────────────

def _get_info(client: httpx.Client, lon: float, lat: float,
              pad: float = 0.0004) -> Optional[dict]:
    cx, cy = _to_3857(lon, lat)
    dx = dy = pad * 20037508.34 / 180
    W = H = 800
    params = {
        "x": str(W // 2), "y": str(H // 2), "epsg": "EPSG:3857",
        "xmin": str(cx - dx), "ymin": str(cy - dy),
        "xmax": str(cx + dx), "ymax": str(cy + dy),
        "layerlistbaselayer": (
            "carto:Macizos,carto:Parcelas Rurales,carto:Parcelas,"
            "carto:Subparcelas,carto:Equipamiento_Comunitario,"
            "carto:Espacio Verde,carto:Cotas,carto:Cotas_sp,"
            "carto:Seccion,carto:Circunscripcion,carto:Partidos,"
            "carto:Calles,carto:Limites,carto:Cuerpos_de_agua,"
            "carto:Red_Ferroviaria,carto:ign_cursos_de_agua"
        ),
        "layerlistnotbaselayer": "",
        "querylayerlistbaselayer": (
            "carto:Macizos,carto:Parcelas Rurales,carto:Parcelas,"
            "carto:Subparcelas,carto:Cotas,carto:Cotas_sp,"
            "carto:Seccion,carto:Circunscripcion,carto:Partidos"
        ),
        "querylayerlistnotbaselayer": "",
        "stylelayerlistbaselayer": (
            "carto:Carto_Macizos,carto:Carto_Parcelas_Rurales,"
            "carto:Carto_Parcelas,carto:Carto_Subparcelas,"
            "carto:Equipamiento_Comunitario,carto:Carto_Espacio_Verde,"
            "carto:empty,carto:empty,carto:Carto_Seccion,"
            "carto:Carto_Circunscripcion,carto:Partidos,carto:empty,"
            "carto:Limite,carto:Cuerpos_de_agua,"
            "carto:Red_Ferrocarril,carto:Cursos_de_agua"
        ),
        "stylelayerlistnotbaselayer": "",
        "listidslayervisibles": "60", "listidslayersidevisibles": "",
        "listidsoperativosfisca": "", "listidsLayerdpout": "",
        "listLayersRRwms": "", "listLayersRRwfs": "",
        "scale": "846", "width": str(W), "height": str(H),
        "lon": str(cx), "lat": str(cy),
    }
    try:
        r = client.get(
            f"{CARTO_BASE}/client/getInfo", params=params,
            headers={"X-Requested-With": "XMLHttpRequest",
                     "Referer": f"{CARTO_BASE}/"},
            timeout=25,
        )
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            return r.json()
        if r.status_code in (401, 403):
            logger.warning(f"getInfo: HTTP {r.status_code} — sesión inválida")
            return None
        logger.warning(f"getInfo: HTTP {r.status_code} — {r.text[:120]!r}")
        return None
    except Exception as e:
        logger.warning(f"getInfo excepción: {e}")
        return None


def _parsear_subparcelas(data: dict) -> tuple[str, str, list[dict]]:
    """
    Extrae de la respuesta getInfo:
      - domicilio: dirección registrada en ARBA
      - nomencla:  nomenclatura catastral completa
      - rows:      [{partida, s_m2, sp}] — subparcelas
    """
    nomencla  = ""
    domicilio = ""
    rows_out: list[dict] = []

    for bloque in data.get("data", []):
        title = str(bloque.get("title", "")).lower()
        bd    = bloque.get("data", {})
        if not isinstance(bd, dict):
            continue

        if "nomenclatura" in title:
            for row in bd.get("table", {}).values():
                if row.get("clave") == "Abierta":
                    nomencla = row.get("valor", "").strip()
                    break

        elif "valores" in title or "básic" in title or "basic" in title:
            tabla = bd.get("table", {})
            if not tabla or "partida" not in next(iter(tabla.values()), {}):
                continue
            for _, row in sorted(tabla.items(), key=lambda x: int(x[0])):
                if "partida" not in row:
                    continue
                try:
                    s_m2 = int(float(row.get("s_terreno", 0) or 0))
                except (ValueError, TypeError):
                    s_m2 = 0
                rows_out.append({
                    "partida": str(row["partida"]),
                    "s_m2": s_m2,
                    "sp": str(row.get("sp", "")),
                })

        elif "direcci" in title:
            partes = [v.strip() for v in bd.values()
                      if isinstance(v, str) and v.strip()]
            domicilio = " ".join(partes)

    return domicilio, nomencla, rows_out


# ── Geocodificación ────────────────────────────────────────────────────────────

def _geocodificar(client: httpx.Client, lat: float, lon: float) -> tuple[str, str, str]:
    """Devuelve (calle, numero, fuente). Intenta Google Maps → Nominatim."""
    google_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")

    if google_key:
        for _ in range(3):
            try:
                r = client.get(GMAPS_GEO, params={
                    "latlng": f"{lat},{lon}",
                    "key": google_key,
                    "language": "es",
                }, timeout=10)
                d = r.json()
                if d.get("status") == "OK" and d.get("results"):
                    comps = {t: c["long_name"]
                             for c in d["results"][0].get("address_components", [])
                             for t in c["types"]}
                    road = comps.get("route", "")
                    num  = comps.get("street_number", "")
                    if road:
                        return road, num, "google"
                break
            except Exception:
                time.sleep(1.5)

    # Nominatim fallback
    try:
        r = client.get(NOMINATIM, params={
            "lat": lat, "lon": lon, "format": "json", "zoom": 18,
        }, headers={"User-Agent": "Scrapitero/1.0"}, timeout=10)
        if r.status_code == 200:
            addr = r.json().get("address", {})
            road = addr.get("road") or addr.get("pedestrian") or ""
            num  = addr.get("house_number", "")
            if road:
                return road, num, "nominatim"
    except Exception:
        pass

    return "", "", "sin_datos"


# ── Actualizar parcela en DB ──────────────────────────────────────────────────

def _update_parcela(conn, parcela_id: str, calle: str, numero: str,
                    fuente_dir: str, domicilio_carto: str,
                    n_uf: int, n_cocheras: int, nomencla: str,
                    partida: str = "") -> None:
    # Dirección: preferir carto (datos registrales) sobre geocoding
    calle_final  = domicilio_carto.split()[0] if domicilio_carto else calle
    numero_final = ""
    if domicilio_carto:
        partes = domicilio_carto.split()
        numero_final = partes[1] if len(partes) > 1 else ""
    else:
        numero_final = numero

    conn.execute(text("""
        UPDATE parcelas SET
            calle                          = :calle,
            numero                         = :numero,
            direccion_source               = :src,
            unidades_funcionales_estimadas = :n_uf,
            nomenclatura_catastral         = :nomencla,
            partida_inmobiliaria           = :partida,
            fuente_parcela                 = 'arba_carto'
        WHERE parcela_id = :pid
    """), {
        "calle": calle_final or None,
        "numero": numero_final or None,
        "src": "arba_carto" if domicilio_carto else fuente_dir,
        "n_uf": n_uf,
        "nomencla": nomencla or None,
        "partida": partida or None,
        "pid": parcela_id,
    })


def _insert_unidades(conn, parcela_id: str, rows: list[dict]) -> None:
    """Inserta subparcelas en unidades_funcionales."""
    # Limpiar UF previas de esta parcela
    conn.execute(text("DELETE FROM unidades_funcionales WHERE edificio_id IN "
                      "(SELECT edificio_id FROM edificios WHERE parcela_id = :pid)"),
                 {"pid": parcela_id})
    # NOTA: si no hay edificios asociados aún, las UF se guardan indirectamente
    # a través del campo unidades_funcionales_estimadas en parcelas.
    # Cuando BuildingFetcher cargue edificios, SpatialJoiner los asociará.


# ── Entry point ───────────────────────────────────────────────────────────────

@agent_run
def run(input: ARBACartoInput) -> ARBACartoOutput:
    # Resolver JSESSIONID
    jsessionid = input.jsessionid
    # Si jsessionid tiene formato cookie completo, extraer solo el valor
    if jsessionid and ("=" in jsessionid and ";" in jsessionid or jsessionid.upper().startswith("JSESSIONID=")):
        jsessionid = _extract_jsessionid(jsessionid) or jsessionid
    if not jsessionid and input.cookie_header:
        jsessionid = _extract_jsessionid(input.cookie_header)
    if not jsessionid:
        jsessionid = _load_session()
    logger.info(f"JSESSIONID resuelto: {'OK (' + jsessionid[:8] + '...)' if jsessionid else 'NONE'}")

    if not jsessionid:
        return ARBACartoOutput(
            ok=False,
            needs_cookies=True,
            cookie_instructions=COOKIE_INSTRUCTIONS,
            error="Sin sesión activa de carto.arba.gov.ar"
        )

    _save_session(jsessionid)
    engine = get_engine()

    # Cargar parcelas de la manzana desde DB
    # ST_PointOnSurface garantiza un punto interior al polígono (mejor que el centroide)
    with engine.connect() as conn:
        q = """
            SELECT parcela_id::text,
                   ST_Y(ST_PointOnSurface(geometry)) AS lat,
                   ST_X(ST_PointOnSurface(geometry)) AS lng,
                   cca_code
            FROM parcelas WHERE region_id = :region
        """
        params: dict = {"region": input.region_id}
        if input.survey_id:
            q += " AND survey_id = :sid"
            params["sid"] = input.survey_id
        if input.manzana:
            q += " AND fuente_parcela = 'arba_idera'"
        parcelas_db = conn.execute(text(q), params).fetchall()

    # Si no hay parcelas, descargarlas de IDERA WFS primero.
    # Por nomenclatura si viene completa; si no, por filtro espacial (polígono de la zona).
    if not parcelas_db:
        por_nomenclatura = all([
            input.partido_id, input.circunscripcion, input.seccion, input.manzana
        ])
        try:
            if por_nomenclatura:
                logger.info("Sin parcelas en DB — descargando de IDERA por nomenclatura...")
                features = fetch_idera(
                    input.partido_id, input.circunscripcion,
                    input.seccion, input.manzana
                )
                if not features:
                    return ARBACartoOutput(ok=False, error=(
                        f"IDERA WFS no devolvió parcelas para "
                        f"Partido={input.partido_id} Circ={input.circunscripcion} "
                        f"Secc={input.seccion} Mza={input.manzana}. "
                        "Verificar nomenclatura catastral."
                    ))
            else:
                logger.info("Sin parcelas en DB — descargando de IDERA por zona (GeoJSON)...")
                zone_poly = _load_zone_polygon(input.region_id, input.survey_id)
                if zone_poly is None:
                    return ARBACartoOutput(ok=False, error=(
                        f"La región '{input.region_id}' no tiene zone_geojson para filtrar "
                        "espacialmente. Creá la zona desde un GeoJSON o pasá la nomenclatura "
                        "completa (partido/circunscripcion/seccion/manzana)."
                    ))
                features = fetch_idera_spatial(zone_poly)
                if not features:
                    return ARBACartoOutput(ok=False, error=(
                        f"IDERA WFS no devolvió parcelas dentro del polígono de "
                        f"'{input.region_id}'. Verificar que la zona esté en PBA y que "
                        "geo.arba.gov.ar esté disponible."
                    ))
            _upsert_parcelas(features, input.region_id, input.survey_id)
            logger.info(f"IDERA: {len(features)} parcelas cargadas en DB")
        except Exception as e:
            return ARBACartoOutput(ok=False, error=f"IDERA WFS falló: {e}")

        # Recargar desde DB — MISMAS 4 columnas que la query principal
        # (parcela_id, lat, lng, cca_code); si no, el unpack de 4 más abajo
        # rompe con IndexError cuando las parcelas vienen por la vía IDERA.
        with engine.connect() as conn:
            parcelas_db = conn.execute(text(
                "SELECT parcela_id::text, "
                "ST_Y(ST_PointOnSurface(geometry)) AS lat, "
                "ST_X(ST_PointOnSurface(geometry)) AS lng, "
                "cca_code FROM parcelas "
                "WHERE region_id = :region AND survey_id = :sid"
            ), {"region": input.region_id, "sid": input.survey_id}).fetchall()

    if not parcelas_db:
        return ARBACartoOutput(
            ok=False,
            error=(
                "No hay parcelas para procesar. "
                "Indicá circunscripcion, seccion y manzana para descargarlas automáticamente."
            )
        )

    logger.info(f"Enriqueciendo {len(parcelas_db)} parcelas con carto.arba.gov.ar...")

    procesadas = 0
    con_subparcelas = 0
    total_uf = 0
    total_cocheras = 0
    session_invalida = False
    prev_partidas: Optional[list] = None

    with httpx.Client(follow_redirects=True) as client:
        # Inicializar sesión
        client.get(f"{CARTO_BASE}/", timeout=15)
        client.cookies.set("JSESSIONID", jsessionid, domain="carto.arba.gov.ar")

        with engine.begin() as conn:
            for row in parcelas_db:
                parcela_id, lat, lng, cca_code = row[0], row[1], row[2], row[3]
                if lat is None or lng is None:
                    continue

                # Geocodificación
                calle, numero, fuente_dir = _geocodificar(client, lat, lng)

                # getInfo desde carto
                data = _get_info(client, lng, lat)
                procesadas += 1

                if data is None:
                    session_invalida = True
                    break

                domicilio, nomencla, rows = _parsear_subparcelas(data)

                # Detectar duplicados (click cayó en parcela anterior)
                partidas_actuales = [r["partida"] for r in rows]
                if partidas_actuales and partidas_actuales == prev_partidas:
                    logger.debug(f"Parcela {parcela_id[:8]}… duplicada — skip")
                    time.sleep(input.delay_ms / 1000)
                    continue
                prev_partidas = partidas_actuales if partidas_actuales else prev_partidas

                # Validar que la nomenclatura de carto corresponde al CCA de IDERA
                if nomencla and not _nomencla_matches_cca(nomencla, cca_code):
                    logger.warning(
                        f"Parcela {parcela_id[:8]}… CCA={_parc_num_from_cca(cca_code)!r} "
                        f"pero carto devolvió {nomencla!r} — reintentando con offsets"
                    )
                    # Reintentar con puntos desplazados dentro del polígono
                    OFFSETS = [(0.00005,0), (-0.00005,0), (0,0.00005), (0,-0.00005),
                               (0.00003,0.00003), (-0.00003,-0.00003)]
                    matched = False
                    for dlat, dlng in OFFSETS:
                        data2 = _get_info(client, lng + dlng, lat + dlat)
                        if not data2:
                            continue
                        dom2, nom2, rows2 = _parsear_subparcelas(data2)
                        if nom2 and _nomencla_matches_cca(nom2, cca_code):
                            logger.info(f"Parcela {parcela_id[:8]}… encontrada con offset ({dlat},{dlng})")
                            domicilio, nomencla, rows = dom2, nom2, rows2
                            matched = True
                            break
                    if not matched:
                        logger.warning(
                            f"Parcela {parcela_id[:8]}… CCA={_parc_num_from_cca(cca_code)!r} "
                            "no encontrada en carto con ningún punto — guardando solo geocoding"
                        )
                        _update_parcela(conn, parcela_id, calle, numero,
                                        fuente_dir, "", 0, 0, None, None)
                        time.sleep(input.delay_ms / 1000)
                        continue

                cocheras = sum(1 for r in rows if 0 < r["s_m2"] < COCHERA_M2)
                uf       = sum(1 for r in rows if r["s_m2"] >= COCHERA_M2)

                if rows:
                    con_subparcelas += 1
                    total_uf += uf
                    total_cocheras += cocheras

                partida_principal = rows[0]["partida"] if rows else ""
                _update_parcela(conn, parcela_id, calle, numero,
                                fuente_dir, domicilio, uf, cocheras,
                                nomencla, partida_principal)

                logger.debug(
                    f"✓ {parcela_id[:8]}… → dir=[{fuente_dir}] {calle} {numero} "
                    f"| UF={uf} cocheras={cocheras} nomencla={nomencla!r}"
                )
                time.sleep(input.delay_ms / 1000)

    if session_invalida:
        # Borrar sesión guardada para forzar nuevo handshake
        if ARBA_SESSION_FILE.exists():
            ARBA_SESSION_FILE.unlink()
        return ARBACartoOutput(
            ok=False,
            parcelas_procesadas=procesadas,
            needs_cookies=True,
            cookie_instructions="Sesión expirada.\n\n" + COOKIE_INSTRUCTIONS,
            error="JSESSIONID expirado"
        )

    return ARBACartoOutput(
        ok=True,
        parcelas_procesadas=procesadas,
        parcelas_con_subparcelas=con_subparcelas,
        total_uf=total_uf,
        total_cocheras=total_cocheras,
        fuentes=["arba_carto_getInfo", "google_maps" if os.environ.get("GOOGLE_MAPS_API_KEY") else "nominatim"],
    )
