"""SantaFeCatastroFetcher — parcelas catastrales de la provincia de Santa Fe, Argentina.

Fuente: GeoServer de la IDESF (sin autenticación), el mismo que usa el visualizador
https://www.santafe.gob.ar/idesf/visualizador/

    URL: https://apigwe.santafe.gob.ar/idesf/geoserver/Publico/wms

No hay WFS de parcelas: el WFS de la IDESF no publica ninguna capa, el MapServer de
`aswe.santafe.gov.ar` tiene el WFS apagado y el del SCIT (`scit.santafe.gov.ar`) tiene la
base caída. Las capas de parcelas de este GeoServer son WMS en cascada, así que lo único
que devuelve vectores es GetFeatureInfo, y de a un lote por punto (BUFFER no cambia nada):

  manzana           → GetMap en KML: todas las manzanas del recuadro, enteras.
  parcela_wms_scit  → GetFeatureInfo: el lote que contiene el punto, con nomenclatura,
                      ubicación, PH y superficies. OJO: coordenadas en [lat, lon].
  parcela_centro    → GetFeatureInfo: los puntos de parcela cercanos (radio fijo en
                      píxeles) con la partida inmobiliaria. Se unen al lote por gid.

Por eso se barre manzana por manzana: se consulta un punto de la parte todavía no
cubierta hasta que los lotes cubren la manzana. Medido en el centro de Rafaela (20-oct):
~0,6 consultas por lote, 98-99 % de cada manzana cubierta, ~0,5 s por lote.
Sobre ~1:40.000 el servicio no devuelve nada, por eso las ventanas son chicas.

Es lento (una ciudad son horas): corre por tandas de `max_runtime_s` y devuelve
`parcial=True` mientras falten manzanas; re-ejecutar saltea las ya cubiertas.

Atributos mapeados a la tabla parcelas:
  nomenclatura_catastral  ← nomenc (15 dígitos; los 10 primeros son la manzana)
  partida_inmobiliaria    ← partida (de parcela_centro)
  calle, numero           ← ubic ("PUEYRREDON 161", "ALVEAR S-N")
  area_m2_terreno         ← calculada en el huso UTM de la zona (geo.area_m2)
  estado_provincia        ← "Santa Fe"
  fuente_parcela          ← "santafe_idesf"
"""

from __future__ import annotations

import json
import re
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from shapely.geometry import MultiPolygon, Point, Polygon, box, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents import geo
from scrapitero.agents.smartgis_fetcher import _geom_to_polygon
from scrapitero.db.engine import get_engine

WMS_URL = "https://apigwe.santafe.gob.ar/idesf/geoserver/Publico/wms"
FUENTE = "santafe_idesf"
_KML_NS = "{http://www.opengis.net/kml/2.2}"
_UA = {"User-Agent": "scrapitero/1.0 (relevamiento catastral)"}

# Recuadro de cada pedido de manzanas en KML (~2 km de lado).
_KML_TILE_DEG = 0.02
# Ventana de GetFeatureInfo de lotes: ±50 m alrededor del punto, 101 px.
_LOTE_VENTANA_DEG = 0.0005
# Ventana para los puntos de parcela (la tolerancia es en píxeles: con ±800 m alcanza
# a toda la manzana en ~4 consultas; con más de ~±0,005° el servicio ya no contesta).
_CENTRO_VENTANA_DEG = 0.004
# Corte del barrido de una manzana.
_COBERTURA_OK = 0.99
_MAX_CONSULTAS_MANZANA = 400
_DEG2_A_M2 = 1.1e10  # aprox. a 31° S, sólo para umbrales

# Manzanas por zona entre tandas: pedirlas en KML lleva ~1 min por ciudad.
_MANZANAS_CACHE: dict[str, list] = {}


# ── Pydantic I/O ──────────────────────────────────────────────────────────────

class SantaFeCatastroInput(BaseModel):
    region_id: str
    survey_id: Optional[str] = None
    workers: int = 4               # manzanas en paralelo (no castigar al servidor)
    max_runtime_s: int = 600       # tanda; después devuelve parcial=True


class SantaFeCatastroOutput(BaseModel):
    ok: bool
    parcelas_insertadas: int = 0
    parcelas_actualizadas: int = 0
    manzanas_total: int = 0
    manzanas_hechas: int = 0       # cubiertas al terminar esta tanda (incluye las de antes)
    consultas: int = 0
    parcial: bool = False
    fuente_usada: str = "IDESF GeoServer (Santa Fe)"
    error: Optional[str] = None


# ── Zona ──────────────────────────────────────────────────────────────────────

def _load_zone(region_id: str, survey_id: Optional[str]):
    """Polígono de la zona efectiva: la subzona del survey si existe, si no la región."""
    with get_engine().connect() as conn:
        row = conn.execute(text("""
            SELECT r.bbox_wkt, COALESCE(s.subzona_geojson, r.zone_geojson)
            FROM regions r
            LEFT JOIN surveys s ON s.survey_id::text = :sid
                 AND s.region_id = r.region_id AND s.subzona_geojson IS NOT NULL
            WHERE r.region_id = :rid
        """), {"rid": region_id, "sid": str(survey_id or "")}).fetchone()
    if not row:
        return None
    if row[1]:
        try:
            gj = json.loads(row[1])
            if gj.get("type") == "FeatureCollection":
                raw = [shape(f["geometry"]) for f in gj.get("features", []) if f.get("geometry")]
            elif gj.get("type") == "Feature":
                raw = [shape(gj["geometry"])]
            else:
                raw = [shape(gj)]
            polys = [_geom_to_polygon(g) for g in raw if g is not None]
            if polys:
                return unary_union(polys)
        except Exception as e:
            logger.warning(f"SantaFeCatastro: no se pudo parsear zone_geojson: {e}")
    if row[0]:
        from shapely import wkt as shp_wkt
        return shp_wkt.loads(row[0])
    return None


def _get_latest_survey(region_id: str) -> Optional[str]:
    with get_engine().connect() as conn:
        row = conn.execute(text("""
            SELECT survey_id::text FROM surveys
            WHERE region_id = :rid ORDER BY started_at DESC LIMIT 1
        """), {"rid": region_id}).fetchone()
    return row[0] if row else None


# ── Servicio ──────────────────────────────────────────────────────────────────

class _Cliente:
    """httpx con reintentos y contador de consultas (thread-safe para leer el total)."""

    def __init__(self) -> None:
        self.http = httpx.Client(timeout=90, headers=_UA)
        self.consultas = 0

    def get(self, params: dict) -> httpx.Response:
        for intento in range(4):
            self.consultas += 1
            try:
                r = self.http.get(WMS_URL, params=params)
                r.raise_for_status()
                return r
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as e:
                if intento == 3:
                    raise RuntimeError(f"IDESF no responde: {e}") from e
                time.sleep(2 * (intento + 1))
        raise AssertionError("inalcanzable")

    def gfi(self, capa: str, pt: Point, d: float, n: int = 101) -> list[dict]:
        r = self.get(dict(
            SERVICE="WMS", VERSION="1.1.1", REQUEST="GetFeatureInfo", LAYERS=capa,
            QUERY_LAYERS=capa, STYLES="", SRS="EPSG:4326",
            BBOX=f"{pt.x - d},{pt.y - d},{pt.x + d},{pt.y + d}",
            WIDTH=n, HEIGHT=n, X=n // 2, Y=n // 2, FEATURE_COUNT=500,
            INFO_FORMAT="application/json"))
        return r.json().get("features", [])


def _manzanas(cli: _Cliente, zona) -> list:
    """Manzanas (polígonos) que tocan la zona, pedidas en KML por recuadros."""
    x0, y0, x1, y1 = zona.bounds
    vistas: dict[bytes, object] = {}
    x = x0
    while x < x1:
        y = y0
        while y < y1:
            bb = (x, y, min(x + _KML_TILE_DEG, x1), min(y + _KML_TILE_DEG, y1))
            r = cli.get(dict(
                service="WMS", version="1.1.1", request="GetMap", layers="manzana", styles="",
                srs="EPSG:4326", bbox=",".join(map(str, bb)), width=1024, height=1024,
                format="application/vnd.google-earth.kml+xml", kmscore=100, mode="download"))
            for pm in ET.fromstring(r.content).iter(f"{_KML_NS}Placemark"):
                polys = []
                for co in pm.iter(f"{_KML_NS}outerBoundaryIs"):
                    txt = co.findtext(f".//{_KML_NS}coordinates") or ""
                    pts = [tuple(map(float, t.split(",")[:2])) for t in txt.split()]
                    if len(pts) >= 4:
                        polys.append(Polygon(pts))
                if polys:
                    g = unary_union(polys).buffer(0)
                    if g.intersects(zona):
                        vistas[g.wkb] = g
            y += _KML_TILE_DEG
        x += _KML_TILE_DEG
    return list(vistas.values())


def _vacio(v) -> Optional[str]:
    """El servicio manda `{}` (o el texto "{}") en lugar de null."""
    if v is None or isinstance(v, dict):
        return None
    s = str(v).strip()
    return None if s in ("", "{}") else s


def _lote(feat: dict):
    """Feature de parcela_wms_scit → (gid, geometría lon/lat, props)."""
    def sw(anillo):
        return [(p[1], p[0]) for p in anillo]  # viene [lat, lon]
    g = feat["geometry"]
    polys = [Polygon(sw(p[0]), [sw(h) for h in p[1:]]) for p in g["coordinates"]]
    return feat["id"].split(".")[-1], MultiPolygon(polys).buffer(0), feat.get("properties") or {}


# El número es el que cierra la ubicación (puede seguir "- 43", el otro frente):
# así "9 DE JULIO 500" no toma el 9.
_RE_UBIC = re.compile(r"^(.*?)\s+(\d+)\s*(?:[-/].*)?$")


def _calle_numero(ubic: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """'PUEYRREDON 141- 43' → ('PUEYRREDON', '141'); 'ALVEAR S-N' → ('ALVEAR', None)."""
    if not ubic:
        return None, None
    s = ubic.strip()
    m = _RE_UBIC.match(s)
    if m and m.group(1):
        return m.group(1).strip(), m.group(2)
    s = re.sub(r"\s+S[-/]?N$", "", s).strip()
    return (s or None), None


def _barrer_manzana(cli: _Cliente, manz) -> dict:
    """Lotes de una manzana: {gid: (geom, props)}, con 'partida' agregada a props."""
    lotes: dict = {}
    cubierto = Polygon()
    consultas = 0
    while consultas < _MAX_CONSULTAS_MANZANA:
        resto = manz.difference(cubierto).buffer(-0.000005)  # ~0,5 m hacia adentro
        if resto.is_empty or resto.area < manz.area * (1 - _COBERTURA_OK):
            break
        pieza = max(getattr(resto, "geoms", [resto]), key=lambda g: g.area)
        if pieza.area * _DEG2_A_M2 < 20:
            break
        pt = pieza.representative_point()
        consultas += 1
        feats = cli.gfi("parcela_wms_scit", pt, _LOTE_VENTANA_DEG)
        if not feats:
            cubierto = cubierto.union(pt.buffer(0.00003))  # ~3 m sin lote: no volver
            continue
        for f in feats:
            gid, geom, props = _lote(f)
            if not geom.is_empty:
                lotes[gid] = (geom, props)
                cubierto = cubierto.union(geom)
        cubierto = cubierto.union(pt.buffer(0.00001))

    # Partidas: puntos de parcela alrededor de la manzana hasta tener todas.
    partidas: dict[str, str] = {}
    pts = [manz.centroid] + [Point(c) for g in getattr(manz, "geoms", [manz])
                             for c in list(g.exterior.coords)[:-1]]
    for pt in pts:
        if set(lotes) <= set(partidas):
            break
        for f in cli.gfi("parcela_centro", pt, _CENTRO_VENTANA_DEG):
            p = _vacio((f.get("properties") or {}).get("partida"))
            if p:
                partidas[f["id"].split(".")[-1]] = p
    for gid, (geom, props) in lotes.items():
        props["partida"] = partidas.get(gid)
    return lotes


# ── DB ────────────────────────────────────────────────────────────────────────

def _existentes(survey_id: str) -> list:
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT ST_AsText(geometry) FROM parcelas
            WHERE survey_id = :sid AND fuente_parcela = :f AND geometry IS NOT NULL
        """), {"sid": survey_id, "f": FUENTE}).fetchall()
    from shapely import wkt as shp_wkt
    return [shp_wkt.loads(r[0]) for r in rows]


def _guardar(conn, region_id: str, survey_id: str, gid: str, geom, props: dict) -> str:
    """Upsert por (survey, nomenclatura). Devuelve 'inserted' | 'updated'."""
    nom = _vacio(props.get("nomenc")) or f"gid:{gid}"
    calle, numero = _calle_numero(_vacio(props.get("ubic")))
    lat, lng = geom.centroid.y, geom.centroid.x
    vals = {
        "geom": geom.wkt, "lat": lat, "lng": lng, "area": geo.area_m2(geom),
        "calle": calle, "numero": numero, "partida": props.get("partida"),
        "src": FUENTE if calle else None, "conf": 0.9 if calle else None,
        "fuente": FUENTE, "nom": nom, "sid": survey_id,
    }
    pid = conn.execute(text("""
        SELECT parcela_id::text FROM parcelas
        WHERE survey_id = :sid AND nomenclatura_catastral = :nom
    """), vals).scalar()
    if pid:
        # La dirección no se pisa si alguien la corrigió a mano.
        conn.execute(text("""
            UPDATE parcelas SET
                geometry = ST_GeomFromText(:geom, 4326), centroid_lat = :lat, centroid_lng = :lng,
                area_m2_terreno = COALESCE(:area, area_m2_terreno),
                partida_inmobiliaria = COALESCE(:partida, partida_inmobiliaria),
                calle  = CASE WHEN direccion_source = 'manual' THEN calle  ELSE COALESCE(:calle, calle) END,
                numero = CASE WHEN direccion_source = 'manual' THEN numero ELSE COALESCE(:numero, numero) END,
                direccion_source = CASE WHEN direccion_source = 'manual' THEN direccion_source
                                        ELSE COALESCE(:src, direccion_source) END,
                estado_provincia = 'Santa Fe', pais = 'Argentina', fuente_parcela = :fuente
            WHERE parcela_id = :pid
        """), {**vals, "pid": pid})
        return "updated"
    conn.execute(text("""
        INSERT INTO parcelas (
            parcela_id, survey_id, region_id, geometry, centroid_lat, centroid_lng,
            area_m2_terreno, calle, numero, direccion_source, direccion_confidence,
            estado_provincia, pais, fuente_parcela, nomenclatura_catastral, partida_inmobiliaria
        ) VALUES (
            :pid, :sid, :region, ST_GeomFromText(:geom, 4326), :lat, :lng,
            :area, :calle, :numero, :src, :conf,
            'Santa Fe', 'Argentina', :fuente, :nom, :partida
        )
    """), {**vals, "pid": str(uuid.uuid4()), "region": region_id})
    return "inserted"


# ── Entry point ───────────────────────────────────────────────────────────────

@agent_run
def run(inp: SantaFeCatastroInput) -> SantaFeCatastroOutput:
    inicio = time.monotonic()
    zona = _load_zone(inp.region_id, inp.survey_id)
    if zona is None or zona.is_empty:
        return SantaFeCatastroOutput(ok=False, error=f"Región '{inp.region_id}' sin polígono")
    survey_id = inp.survey_id or _get_latest_survey(inp.region_id)
    if not survey_id:
        return SantaFeCatastroOutput(ok=False, error=f"No hay survey para '{inp.region_id}'")

    cli = _Cliente()
    clave = f"{survey_id}:{zona.wkb_hex[:64]}:{zona.area}"
    manzanas = _MANZANAS_CACHE.get(clave)
    if manzanas is None:
        try:
            manzanas = _manzanas(cli, zona)
        except Exception as e:
            return SantaFeCatastroOutput(ok=False, error=f"No se pudieron bajar las manzanas: {e}")
        _MANZANAS_CACHE[clave] = manzanas
    if not manzanas:
        return SantaFeCatastroOutput(ok=False, error="La IDESF no tiene manzanas en la zona")

    # Reanudar: saltear las manzanas que los lotes ya guardados cubren.
    previas = _existentes(survey_id)
    arbol = STRtree(previas) if previas else None

    def cubierta(m) -> bool:
        if arbol is None:
            return False
        cerca = [previas[i] for i in arbol.query(m)]
        return bool(cerca) and unary_union(cerca).intersection(m).area >= m.area * 0.95

    pendientes = [m for m in manzanas if not cubierta(m)]
    hechas = len(manzanas) - len(pendientes)
    logger.info(f"SantaFeCatastro '{inp.region_id}': {len(manzanas)} manzanas, "
                f"{hechas} ya cubiertas, {len(pendientes)} por barrer")

    ins = act = 0
    error: Optional[str] = None
    engine = get_engine()
    with ThreadPoolExecutor(max_workers=max(1, inp.workers)) as pool:
        i = 0
        while i < len(pendientes):
            if time.monotonic() - inicio > inp.max_runtime_s:
                break
            tanda = pendientes[i:i + inp.workers]
            i += len(tanda)
            try:
                resultados = list(pool.map(lambda m: _barrer_manzana(cli, m), tanda))
            except Exception as e:  # el servidor se cayó a mitad: lo guardado sirve
                error = str(e)
                break
            with engine.begin() as conn:
                for lotes in resultados:
                    for gid, (geom, props) in lotes.items():
                        if not zona.intersects(geom):
                            continue
                        if _guardar(conn, inp.region_id, survey_id, gid, geom, props) == "inserted":
                            ins += 1
                        else:
                            act += 1
            hechas += len(tanda)
            if hechas % 50 < inp.workers:
                logger.info(f"  SantaFeCatastro: {hechas}/{len(manzanas)} manzanas, "
                            f"{ins} lotes nuevos, {cli.consultas} consultas")

    parcial = hechas < len(manzanas)
    if error and ins + act == 0:
        return SantaFeCatastroOutput(ok=False, error=error, manzanas_total=len(manzanas),
                                     manzanas_hechas=hechas, consultas=cli.consultas)
    logger.info(f"SantaFeCatastro tanda: {ins} nuevos, {act} actualizados, "
                f"{hechas}/{len(manzanas)} manzanas, parcial={parcial}")
    return SantaFeCatastroOutput(
        ok=True, parcelas_insertadas=ins, parcelas_actualizadas=act,
        manzanas_total=len(manzanas), manzanas_hechas=hechas,
        consultas=cli.consultas, parcial=parcial)
