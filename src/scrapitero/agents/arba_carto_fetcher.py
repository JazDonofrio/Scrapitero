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
from scrapitero.agents.precedencia import (
    UF_FUENTES_PROTEGIDAS as _UF_FUENTES_PROTEGIDAS,
    USO_FUENTES_PROTEGIDAS as _USO_FUENTES_PROTEGIDAS,
    DIRECCION_FUENTE_PROTEGIDA as _DIR_PROTEGIDA,
)

# ── Config ────────────────────────────────────────────────────────────────────

CARTO_BASE       = "https://carto.arba.gov.ar/cartoArba"
# UA identificable con contacto: Nominatim lo exige y ARBA lo registra en sus logs.
_UA_HEADERS      = {"User-Agent": "ScraperGIS/1.0 (+https://github.com/Meter0r0/Scrapitero)"}
GMAPS_GEO        = "https://maps.googleapis.com/maps/api/geocode/json"
NOMINATIM        = "https://nominatim.openstreetmap.org/reverse"
COCHERA_M2       = 25   # subparcela < 25 m² → cochera; >= 25 m² → unidad funcional
ARBA_SESSION_FILE = Path(os.environ.get("ARBA_SESSION_FILE",
                                         "/opt/scrapitero/.arba_session.json"))
# Reintentos de `getInfo` ante un fallo que NO es de sesión. carto corre sobre Tomcat y
# devuelve 500 esporádicos; antes cualquier no-200 se leía como "JSESSIONID expirado",
# abortaba la corrida entera y borraba la cookie (medido: 372 de 619 parcelas en Malvinas
# el 10-ago-2026, con la sesión todavía válida).
GETINFO_REINTENTOS = 3
GETINFO_BACKOFF_S  = 1.5


class FalloTransitorio(Exception):
    """`getInfo` falló por algo que NO es la sesión: 500 de Tomcat, timeout, red.

    Se distingue del `None` —que sí significa sesión muerta (401/403)— para que un hipo
    del servidor saltee **esa parcela** en lugar de matar la corrida y tirar el JSESSIONID.
    """


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
    # Por defecto la corrida es REANUDABLE: saltea las parcelas que ya tienen
    # nomenclatura de carto. Así un corte a mitad de camino se retoma donde quedó, sin
    # re-pagarle a Google el reverse geocoding de lo ya resuelto. `rehacer=True` fuerza
    # el barrido completo (p. ej. para refrescar subparcelas contra un carto actualizado).
    rehacer: bool = False


class ARBACartoOutput(BaseModel):
    ok: bool
    parcelas_procesadas: int = 0
    parcelas_con_subparcelas: int = 0
    parcelas_saltadas_error: int = 0        # fallos transitorios de carto, no de sesión
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


# Campo de parcela del CCA, ya sin los ceros de relleno: una letra OPCIONAL seguida del
# número. Esa letra NO es de la parcela, es el sufijo de la manzana (ver `_split_cca`).
_CCA_PARCELA_RE = re.compile(r"^([A-Za-z])?(\d+)$")


def _split_cca(cca: str) -> tuple[str, str, str]:
    """Descompone el CCA de ARBA en (manzana, numero_parcela, sufijo_parcela).

    Layout fijo de 42 caracteres: `[28:32]` manzana, `[32:39]` parcela, `[39:42]` subparcela.

    El campo de parcela mezcla dos cosas distintas y hay que separarlas:

    - **Sufijo de la MANZANA**, al principio. Cuando la manzana lleva letra, el número va
      en su campo y la letra se guarda encabezando el de parcela:
      `…0074` + `00B0006` = **manzana 74B, parcela 6**.
    - **Sufijo de la PARCELA**, que va aparte en el campo de subparcela:
      `…0000016` + `00M` = **parcela 16M**.

    Antes se leía el campo entero como número de parcela (`'B0006'`), así que la respuesta
    correcta de carto (`Manzana: 74B Parcela: 6`) se descartaba como si fuera de otro lote:
    la parcela quedaba sin nomenclatura, sin UF y sólo con el geocoding.
    """
    if not cca or len(cca) < 39:
        return "", "", ""
    campo = cca[32:39].lstrip("0") or "0"
    sufijo = cca[39:].lstrip("0")
    num_mz = cca[28:32].lstrip("0")
    m = _CCA_PARCELA_RE.match(campo)
    if not m:                       # forma inesperada: devolverlo crudo, no inventar
        return num_mz, campo, sufijo
    letra_mz, numero = (m.group(1) or "").upper(), m.group(2)
    return f"{num_mz}{letra_mz}", (numero.lstrip("0") or "0"), sufijo


def _parc_num_from_cca(cca: str) -> str:
    """Identificador de parcela tal como lo escribe carto: '21', '16M', '6'."""
    _, numero, sufijo = _split_cca(cca)
    if not numero:
        return ""
    return f"{numero}{sufijo}" if sufijo else numero


def _nomencla_matches_cca(nomencla: str, cca: str) -> bool:
    """Verifica que la nomenclatura de carto corresponda al CCA de IDERA."""
    if not nomencla or not cca:
        return True  # sin datos suficientes, aceptar
    manzana, numero, sufijo = _split_cca(cca)
    expected = f"{numero}{sufijo}" if sufijo else numero
    if not expected:
        return True
    # Buscar "Parcela: <N>" en la nomenclatura
    m = re.search(r"Parcela:\s*(\w+)", nomencla, re.IGNORECASE)
    if not m:
        return True
    if m.group(1).upper() != expected.upper():
        return False
    # Y la MANZANA completa, número y letra. Al separar los dos campos el número de parcela
    # se volvió menos específico ('6' en vez de 'B0006'), así que sin este chequeo el
    # validador se aflojaba: la parcela 6 de la manzana 75B pasaría por la de la 74B.
    if manzana:
        mz = re.search(r"Manzana:\s*(\w+)", nomencla, re.IGNORECASE)
        if mz and mz.group(1).upper().lstrip("0") != manzana.upper():
            return False
    return True


def _extract_jsessionid(cookie_header: str) -> Optional[str]:
    # Eliminar prefijo "Cookie:" si el usuario copió el header completo
    value = re.sub(r"(?i)^cookie\s*:\s*", "", cookie_header.strip())
    for part in value.split(";"):
        if part.strip().upper().startswith("JSESSIONID="):
            return part.split("=", 1)[1].strip()
    return None


# ── Caché de reverse geocoding (compartido con AddressResolver, mig. 032) ─────
# Misma clave y mismo dict de componentes que `address_resolver._rev_key` /
# `_reverse_geocode_cached`: los dos agentes reverse-geocodifican los mismos puntos
# (el interior de cada parcela), así que comparten caché en vez de pagar dos veces.
_REV_LANG = "es"     # PBA: las respuestas de Google se piden en español


def _rev_cache_get(lat: float, lng: float) -> Optional[dict]:
    """Componentes cacheados para ese punto, o None si es miss."""
    clave = f"{lat:.6f}|{lng:.6f}|{_REV_LANG}"
    try:
        with get_engine().connect() as conn:
            row = conn.execute(
                text("SELECT componentes FROM reverse_geocode_cache WHERE clave = :k"),
                {"k": clave},
            ).fetchone()
        return (row[0] or {}) if row is not None else None
    except Exception as e:      # el caché nunca debe romper la corrida
        logger.debug(f"caché reverse no disponible: {e}")
        return None


def _rev_cache_put(lat: float, lng: float, calle: str, numero: str) -> None:
    """Guarda sólo resultados útiles (con calle o número); los vacíos se reintentan."""
    if not (calle or numero):
        return
    clave = f"{lat:.6f}|{lng:.6f}|{_REV_LANG}"
    try:
        with get_engine().begin() as conn:
            conn.execute(text("""
                INSERT INTO reverse_geocode_cache (clave, componentes)
                VALUES (:k, CAST(:comp AS JSONB))
                ON CONFLICT (clave) DO NOTHING
            """), {"k": clave,
                   "comp": json.dumps({"calle": calle, "numero": numero},
                                      ensure_ascii=False)})
    except Exception as e:
        logger.debug(f"no se pudo cachear el reverse: {e}")


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
    for intento in range(GETINFO_REINTENTOS):
        try:
            r = client.get(
                f"{CARTO_BASE}/client/getInfo", params=params,
                headers={"X-Requested-With": "XMLHttpRequest",
                         "Referer": f"{CARTO_BASE}/"},
                timeout=25,
            )
            if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                return r.json()
            # SÓLO el 401/403 es sesión muerta. Cualquier otro código es del servidor y
            # merece reintento, no dar por vencida la cookie.
            if r.status_code in (401, 403):
                logger.warning(f"getInfo: HTTP {r.status_code} — sesión inválida")
                return None
            motivo = f"HTTP {r.status_code} — {r.text[:120]!r}"
        except Exception as e:
            motivo = f"excepción: {e}"
        if intento < GETINFO_REINTENTOS - 1:
            espera = GETINFO_BACKOFF_S * (2 ** intento)
            logger.warning(f"getInfo: {motivo} — reintento "
                           f"{intento + 1}/{GETINFO_REINTENTOS - 1} en {espera:.1f}s")
            time.sleep(espera)
    raise FalloTransitorio(motivo)


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
    """Devuelve (calle, numero, fuente). Caché → Google Maps → Nominatim.

    El caché es `reverse_geocode_cache` (mig. 032), el MISMO que usa `AddressResolver`:
    mismo formato de clave e idéntico dict de componentes, así que los dos agentes se
    aprovechan mutuamente. Importa porque acá el reverse es por parcela y se paga: sin
    caché, retomar una corrida cortada volvía a comprarle a Google direcciones que ya
    teníamos.
    """
    google_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")

    cacheado = _rev_cache_get(lat, lon)
    if cacheado is not None:
        return cacheado.get("calle", ""), cacheado.get("numero", ""), "google"

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
                        _rev_cache_put(lat, lon, road, num)
                        return road, num, "google"
                break
            except Exception:
                time.sleep(1.5)

    # Nominatim fallback
    try:
        r = client.get(NOMINATIM, params={
            "lat": lat, "lon": lon, "format": "json", "zoom": 18,
        }, headers=_UA_HEADERS, timeout=10)
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

    # ARBA no dice el DESTINO de cada subparcela (el campo `sp` es el número de
    # subparcela, no el uso), así que lo que devuelve es la UF TOTAL del lote.
    # Se carga entera como vivienda —el caso dominante— y `UsoClassifier` corre
    # después para pasar a comercio la parte que Google Places confirme.
    # Sin esto la UF quedaba sólo en `unidades_funcionales_estimadas` y la web,
    # el CSV y el propio UsoClassifier (que lee uf_vivienda/uf_comercio) veían 0.
    conn.execute(text(f"""
        UPDATE parcelas SET
            calle                          = CASE WHEN COALESCE(direccion_source,'') = '{_DIR_PROTEGIDA}'
                                                  THEN calle ELSE :calle END,
            numero                         = CASE WHEN COALESCE(direccion_source,'') = '{_DIR_PROTEGIDA}'
                                                  THEN numero ELSE :numero END,
            direccion_source               = CASE WHEN COALESCE(direccion_source,'') = '{_DIR_PROTEGIDA}'
                                                  THEN direccion_source ELSE :src END,
            unidades_funcionales_estimadas = :n_uf,
            -- `uf_vivienda` es EL campo de ARBA: se actualiza siempre, salvo corrección
            -- manual. Ojo que el sello `uf_fuente` es uno solo para dos campos de fuentes
            -- distintas (la vivienda la da ARBA, el comercio lo cuenta Overture/Google):
            -- si acá se respetara la lista entera de protegidas, una parcela con comercio
            -- sellado quedaba congelada y nunca más se le actualizaba la vivienda.
            uf_vivienda                    = CASE WHEN COALESCE(uf_fuente,'') = 'manual'
                                                  THEN uf_vivienda ELSE :n_uf END,
            -- el comercio sí lo conservan las fuentes que lo cuentan de verdad
            uf_comercio                    = CASE WHEN COALESCE(uf_fuente,'') IN {_UF_FUENTES_PROTEGIDAS}
                                                  THEN uf_comercio ELSE 0 END,
            uf_fuente                      = CASE WHEN COALESCE(uf_fuente,'') IN {_UF_FUENTES_PROTEGIDAS}
                                                  THEN uf_fuente ELSE 'arba_carto' END,
            -- Uso deducido de las propias UF: con unidades y sin comercio conocido, la
            -- parcela es RESIDENCIAL. Es la misma regla que aplica el BCI en Brasil
            -- (`bci_parser._parse_bci`), sólo que acá la única señal es el conteo.
            -- Sin esto el uso quedaba NULL en todo lo que no tocara una fuente de
            -- comercios: en Malvinas, 592 de 619 parcelas (96%) salían al mapa sin color
            -- y al CSV sin uso, con 628 viviendas ya contadas.
            -- Con 0 UF no se infiere nada: puede ser un baldío o un lote que carto no
            -- devolvió, y no hay cómo distinguirlos desde acá.
            uso_principal                  = CASE
                                                WHEN COALESCE(uso_fuente,'') IN {_USO_FUENTES_PROTEGIDAS}
                                                     THEN uso_principal
                                                WHEN :n_uf > 0 THEN 'residencial'
                                                ELSE uso_principal END,
            uso_fuente                     = CASE
                                                WHEN COALESCE(uso_fuente,'') IN {_USO_FUENTES_PROTEGIDAS}
                                                     THEN uso_fuente
                                                WHEN :n_uf > 0 THEN 'arba_carto'
                                                ELSE uso_fuente END,
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
        if not input.rehacer:
            # Reanudar: las que ya trajeron nomenclatura de carto están hechas.
            q += " AND nomenclatura_catastral IS NULL"
        parcelas_db = conn.execute(text(q), params).fetchall()
        # Cuántas hay en total, sin el filtro de reanudación: distingue "la región está
        # vacía" (hay que bajar de IDERA) de "ya están todas enriquecidas" (no hay nada
        # que hacer). Sin esto, una corrida completa volvería a descargar de IDERA.
        q_total = "SELECT count(*) FROM parcelas WHERE region_id = :region"
        if input.survey_id:
            q_total += " AND survey_id = :sid"
        parcelas_totales = conn.execute(text(q_total), params).scalar() or 0

    if not parcelas_db and parcelas_totales:
        logger.info(f"Nada que hacer: las {parcelas_totales} parcelas ya tienen "
                    "nomenclatura de carto. Usar rehacer=true para forzar el barrido.")
        return ARBACartoOutput(ok=True, parcelas_procesadas=0,
                               fuentes=["carto.arba.gov.ar"])

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
    saltadas_error = 0
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
                try:
                    data = _get_info(client, lng, lat)
                except FalloTransitorio as e:
                    saltadas_error += 1
                    logger.warning(f"Parcela {parcela_id[:8]}… se saltea ({e}) — sigue la corrida")
                    time.sleep(input.delay_ms / 1000)
                    continue
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
                        try:
                            data2 = _get_info(client, lng + dlng, lat + dlat)
                        except FalloTransitorio:
                            continue     # el offset falló por el servidor: probar el siguiente
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
            parcelas_saltadas_error=saltadas_error,
            needs_cookies=True,
            cookie_instructions="Sesión expirada.\n\n" + COOKIE_INSTRUCTIONS,
            error="JSESSIONID expirado"
        )

    if saltadas_error:
        logger.warning(f"{saltadas_error} parcelas salteadas por fallos de carto "
                       "(no de sesión). Volver a correr el agente las retoma.")

    return ARBACartoOutput(
        ok=True,
        parcelas_procesadas=procesadas,
        parcelas_con_subparcelas=con_subparcelas,
        parcelas_saltadas_error=saltadas_error,
        total_uf=total_uf,
        total_cocheras=total_cocheras,
        fuentes=["arba_carto_getInfo", "google_maps" if os.environ.get("GOOGLE_MAPS_API_KEY") else "nominatim"],
    )
