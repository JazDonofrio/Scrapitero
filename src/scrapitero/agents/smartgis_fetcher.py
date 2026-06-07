"""SmartGISFetcher — descarga parcelas de Várzea Grande desde la API SmartGIS.

Fuente: api.smartgis.net.br/varzeagrande/prefeitura/api
  POST /PublicLote/IdentifyOnExtent  → IDs de lotes en un extent
  GET  /PublicLote/Get/{id}          → detalle del lote (inscricao, geometría, atributos)

El campo clave es CODIGO_IMOVEL_AGRUPADO → se guarda en cca_code.
Ese código (15 dígitos con ceros) es la inscripción que usa vg.abaco.com.br
para descargar el BCI (Boletim de Cadastro Imobiliário).
"""

from __future__ import annotations

import json
import random
import re
import time
import uuid
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from shapely.geometry import Point, shape
from shapely.ops import unary_union
from sqlalchemy import text

from scrapitero.db.engine import get_engine
from scrapitero.agents._run import agent_run

import os

API_BASE = "https://api.smartgis.net.br/varzeagrande/prefeitura/api"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://vg.abaco.com.br",
    "Referer": "https://vg.abaco.com.br/",
}

# Stop flags por region_id — set desde el exterior para detener el agente
_stop_regions: set[str] = set()


def request_stop(region_id: str) -> None:
    _stop_regions.add(region_id)


def _tg(msg: str) -> None:
    """Envía un mensaje a Telegram si hay token configurado. Falla silenciosamente."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_HOME_CHANNEL") or os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",")[0].strip()
    if not token or not chat_id:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass


class SmartGISInput(BaseModel):
    region_id: str
    survey_id: str
    bbox_south: Optional[float] = None  # Si None, se deriva de zone_geojson de la región
    bbox_west: Optional[float] = None
    bbox_north: Optional[float] = None
    bbox_east: Optional[float] = None
    step_grados: float = 0.00010        # ~11m por celda — cubre parcelas urbanas típicas (100-500 m²)
    n_passes: int = 2                   # 2 passes offset garantizan cobertura sin duplicados
    delay_ms: int = 80


class SmartGISOutput(BaseModel):
    ok: bool
    parcelas_insertadas: int = 0
    parcelas_actualizadas: int = 0
    lotes_escaneados: int = 0
    fuera_de_zona: int = 0
    error: Optional[str] = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_br_float(s: str) -> Optional[float]:
    try:
        return float(str(s).strip().replace(".", "").replace(",", "."))
    except (ValueError, AttributeError):
        return None


def _extract_field(lot_data: dict, name: str) -> str:
    for group in lot_data.get("fieldItemGroups", []):
        for item in group.get("fieldItems", []):
            if item.get("name") == name:
                return str(item.get("value") or "").strip()
    return ""


def _parse_codigo(lot_data: dict) -> Optional[int]:
    """Extrae CODIGO_IMOVEL_AGRUPADO del lot dict."""
    val = _extract_field(lot_data, "CODIGO_IMOVEL_AGRUPADO")
    if val:
        try:
            return int(val)
        except ValueError:
            pass
    # Fallback: parse inscricao "SETOR-QUAD-LOTE#CODIGO"
    insc = lot_data.get("inscricao", "")
    if "#" in insc:
        try:
            return int(insc.split("#")[1].strip())
        except ValueError:
            pass
    return None


def _geom_to_polygon(geom):
    """Convierte cualquier geometría a Polygon/MultiPolygon para intersección.
    Si la geometría es una LineString (error común al exportar de geojson.io),
    la cierra como Polygon usando sus vértices."""
    from shapely.geometry import Polygon as ShapelyPolygon
    t = geom.geom_type
    if t in ("Polygon", "MultiPolygon"):
        return geom.buffer(0)  # buffer(0) repara geometrías inválidas
    if t == "LineString":
        coords = list(geom.coords)
        if coords[0] != coords[-1]:
            coords.append(coords[0])  # cerrar el anillo
        poly = ShapelyPolygon(coords)
        logger.warning(
            f"zone_geojson contiene LineString — se interpreta como Polygon. "
            f"En geojson.io usar la herramienta de Polígono (no Línea)."
        )
        return poly.buffer(0)
    if t == "MultiLineString":
        from shapely.ops import polygonize
        polys = list(polygonize(geom))
        if polys:
            return unary_union(polys).buffer(0)
    if t in ("Point", "MultiPoint"):
        return geom.buffer(0.001)  # buffer mínimo para punto
    return geom


def _resolve_bbox_zone(input: SmartGISInput) -> tuple[Optional[tuple], Optional[object]]:
    """Devuelve ((s, w, n, e), zone_polygon_or_None)."""
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT bbox_wkt, zone_geojson FROM regions WHERE region_id = :rid"
        ), {"rid": input.region_id}).fetchone()

    zone_polygon = None
    if row and row[1]:
        try:
            gj = json.loads(row[1])
            raw_geoms = []
            if gj.get("type") == "FeatureCollection":
                raw_geoms = [shape(f["geometry"]) for f in gj.get("features", []) if f.get("geometry")]
            elif gj.get("type") == "Feature":
                raw_geoms = [shape(gj["geometry"])]
            else:
                raw_geoms = [shape(gj)]

            polys = [_geom_to_polygon(g) for g in raw_geoms if g is not None]
            zone_polygon = unary_union(polys) if polys else None

            if zone_polygon:
                b = zone_polygon.bounds
                logger.info(
                    f"Zona '{input.region_id}': tipo={zone_polygon.geom_type} "
                    f"bounds=lng[{b[0]:.4f},{b[2]:.4f}] lat[{b[1]:.4f},{b[3]:.4f}] "
                    f"area≈{zone_polygon.area * 111000**2 / 1e6:.3f}km²"
                )
        except Exception as e:
            logger.warning(f"No se pudo parsear zone_geojson: {e}")

    # Determinar bbox
    if all(v is not None for v in [input.bbox_south, input.bbox_west, input.bbox_north, input.bbox_east]):
        bbox = (input.bbox_south, input.bbox_west, input.bbox_north, input.bbox_east)
    elif zone_polygon:
        w, s, e, n = zone_polygon.bounds
        bbox = (s, w, n, e)
    elif row and row[0]:
        nums = [float(x) for x in re.findall(r"[-\d.]+", row[0])]
        if len(nums) >= 8:
            bbox = (nums[1], nums[0], nums[5], nums[2])  # s, w, n, e
        else:
            return None, zone_polygon
    else:
        return None, zone_polygon

    return bbox, zone_polygon


def _human_sleep(base_ms: int) -> None:
    """Delay que imita comportamiento humano: clicks rápidos con pausas ocasionales."""
    r = random.random()
    if r < 0.04:
        # Pausa larga: el usuario está leyendo el mapa (1.5–4s)
        time.sleep(random.uniform(1.5, 4.0))
    elif r < 0.15:
        # Pausa media: cambio de área o zoom (0.4–1.0s)
        time.sleep(random.uniform(0.4, 1.0))
    else:
        # Click normal con jitter asimétrico (más rápido que lento)
        time.sleep(base_ms / 1000 * random.triangular(0.6, 2.0, 0.9))


def _build_cells(s: float, w: float, n: float, e: float,
                 step: float, n_passes: int) -> list[tuple]:
    """
    Genera celdas de tamaño 'step' con n_passes grillas offset entre sí.
    Pass p desplaza el origen en step*p/n_passes (lat y lon).
    Cada punto del área queda cubierto por exactamente n_passes celdas distintas,
    lo que garantiza que un lote que queda 3ro (y es descartado por el límite de 2)
    en una celda aparezca como 1ro o 2do en la celda offset del otro pass.
    """
    cells = []
    for p in range(n_passes):
        off = step * p / n_passes
        lat = s - off
        while lat < n:
            lon = w - off
            while lon < e:
                c_s = max(s, lat)
                c_n = min(n, lat + step)
                c_w = max(w, lon)
                c_e = min(e, lon + step)
                if c_s < c_n and c_w < c_e:
                    cells.append((c_w, c_s, c_e, c_n))
                lon += step
            lat += step
    return cells


def _grid_scan(s: float, w: float, n: float, e: float,
               step: float, n_passes: int, delay_ms: int,
               region_id: str = "") -> set[int]:
    """Escanea el bbox con celdas offset entre passes. Devuelve IDs únicos de lotes."""
    cells = _build_cells(s, w, n, e, step, n_passes)
    # Orden aleatorio: evita el patrón sistemático de fila-por-fila
    random.shuffle(cells)

    total = len(cells)
    logger.info(
        f"SmartGIS grid scan: {total} celdas "
        f"({n_passes} passes × ~{total//n_passes}, ~{step*111000:.0f}m/celda)"
    )

    unique_ids: set[int] = set()
    scanned = 0

    with httpx.Client(timeout=20, headers=_HEADERS) as client:
        for c_w, c_s, c_e, c_n in cells:
            if region_id in _stop_regions:
                logger.info(f"SmartGIS: stop solicitado, deteniendo scan ({scanned} celdas)")
                _stop_regions.discard(region_id)
                return unique_ids

            try:
                r = client.post(
                    f"{API_BASE}/PublicLote/IdentifyOnExtent",
                    json=[c_w, c_s, c_e, c_n],
                )
                if r.status_code == 200:
                    ids = r.json()
                    if isinstance(ids, list):
                        unique_ids.update(int(x) for x in ids if x)
            except httpx.HTTPError:
                pass

            scanned += 1
            if scanned % 100 == 0:
                logger.info(f"  Scan: {scanned}/{total} celdas, {len(unique_ids)} lotes únicos")
            if scanned % 200 == 0:
                pct = int(scanned / total * 100)
                _tg(f"🔍 <b>Escaneando zona...</b> {pct}%\n"
                    f"Parcelas encontradas hasta ahora: {len(unique_ids)}")

            _human_sleep(delay_ms)

    logger.info(f"Grid scan completo: {len(unique_ids)} lotes en {total} celdas")
    return unique_ids


def _get_lot_details(ids: set[int], delay_ms: int) -> list[dict]:
    """Descarga atributos de cada lote por ID."""
    lots = []
    with httpx.Client(timeout=20, headers=_HEADERS) as client:
        for idx, lot_id in enumerate(ids, 1):
            try:
                r = client.get(f"{API_BASE}/PublicLote/Get/{lot_id}")
                if r.status_code == 200:
                    data = r.json()
                    # Parsear geometría y calcular centroide
                    geom_str = data.get("geoJson", "{}")
                    try:
                        geom_raw = json.loads(geom_str) if isinstance(geom_str, str) else geom_str
                        geom = shape(geom_raw)
                        centroid = geom.centroid
                        data["_geom"] = geom
                        data["_lat"] = centroid.y
                        data["_lon"] = centroid.x
                        data["_geom_wkt"] = geom.wkt
                    except Exception:
                        data["_geom"] = None
                        data["_lat"] = 0.0
                        data["_lon"] = 0.0
                        data["_geom_wkt"] = None
                    lots.append(data)
            except httpx.HTTPError as e:
                logger.warning(f"Error fetching lot {lot_id}: {e}")

            if idx % 50 == 0:
                logger.info(f"  Detalle: {idx}/{len(ids)} lotes descargados")

            time.sleep(delay_ms / 1000 * random.uniform(0.8, 1.2))

    return lots


def _upsert_lots(lots: list[dict], region_id: str, survey_id: str) -> tuple[int, int]:
    engine = get_engine()
    inserted = updated = 0

    with engine.begin() as conn:
        for lot in lots:
            codigo = _parse_codigo(lot)
            if codigo is None:
                continue

            calle = _extract_field(lot, "LOTE_ENDERECO") or None
            barrio = _extract_field(lot, "LOTE_BAIRRO") or None
            cep = (_extract_field(lot, "LOTE_CEP") or "").replace(".", "").replace("-", "").strip() or None
            area_terreno = _parse_br_float(_extract_field(lot, "LOTE_AREA_LOTE"))
            area_const = _parse_br_float(_extract_field(lot, "LOTE_AREA_CONSTRUIDA"))
            nomenclatura = _extract_field(lot, "INSCRICAO_LOTE_PARCIAL") or None
            unidades = lot.get("unidadesCount") or 0

            lat = lot["_lat"]
            lon = lot["_lon"]
            geom_wkt = lot["_geom_wkt"]

            existing = conn.execute(text("""
                SELECT parcela_id FROM parcelas
                WHERE region_id = :rid AND cca_code = :cca
                LIMIT 1
            """), {"rid": region_id, "cca": str(codigo)}).fetchone()

            if existing:
                conn.execute(text("""
                    UPDATE parcelas SET
                        survey_id = :sid,
                        geometry = COALESCE(ST_GeomFromText(:geom, 4326), geometry),
                        centroid_lat = :lat, centroid_lng = :lon,
                        calle = COALESCE(:calle, calle),
                        barrio = COALESCE(:barrio, barrio),
                        codigo_postal = COALESCE(:cep, codigo_postal),
                        area_m2_terreno = COALESCE(:aterr, area_m2_terreno),
                        area_m2_construida = COALESCE(:acons, area_m2_construida),
                        nomenclatura_catastral = COALESCE(:nomen, nomenclatura_catastral),
                        unidades_funcionales_estimadas = COALESCE(NULLIF(:unid, 0), unidades_funcionales_estimadas)
                    WHERE parcela_id = :pid
                """), {
                    "sid": survey_id, "pid": str(existing[0]),
                    "geom": geom_wkt, "lat": lat, "lon": lon,
                    "calle": calle, "barrio": barrio, "cep": cep,
                    "aterr": area_terreno, "acons": area_const,
                    "nomen": nomenclatura, "unid": unidades,
                })
                updated += 1
            else:
                conn.execute(text("""
                    INSERT INTO parcelas (
                        parcela_id, survey_id, region_id,
                        geometry, centroid_lat, centroid_lng,
                        cca_code, nomenclatura_catastral,
                        calle, barrio, codigo_postal,
                        area_m2_terreno, area_m2_construida,
                        unidades_funcionales_estimadas,
                        municipio, estado_provincia, pais,
                        fuente_parcela, direccion_source, direccion_confidence
                    ) VALUES (
                        :pid, :sid, :rid,
                        ST_GeomFromText(:geom, 4326),
                        :lat, :lon,
                        :cca, :nomen,
                        :calle, :barrio, :cep,
                        :aterr, :acons,
                        :unid,
                        'Várzea Grande', 'MT', 'BRA',
                        'smartgis_vg', 'smartgis_vg', 0.85
                    )
                """), {
                    "pid": str(uuid.uuid4()), "sid": survey_id, "rid": region_id,
                    "geom": geom_wkt, "lat": lat, "lon": lon,
                    "cca": str(codigo), "nomen": nomenclatura,
                    "calle": calle, "barrio": barrio, "cep": cep,
                    "aterr": area_terreno, "acons": area_const,
                    "unid": unidades,
                })
                inserted += 1

    return inserted, updated


# ── Entry point ────────────────────────────────────────────────────────────────

@agent_run
def run(input: SmartGISInput) -> SmartGISOutput:
    bbox, zone_polygon = _resolve_bbox_zone(input)
    if bbox is None:
        return SmartGISOutput(ok=False, error="No se pudo determinar bbox para la región")

    s, w, n, e = bbox
    logger.info(f"SmartGIS VG: bbox ({s:.5f},{w:.5f}) → ({n:.5f},{e:.5f})")

    _stop_regions.discard(input.region_id)  # limpiar flag anterior
    unique_ids = _grid_scan(s, w, n, e, input.step_grados, input.n_passes, input.delay_ms, input.region_id)
    if not unique_ids:
        return SmartGISOutput(ok=True, error="Sin lotes en el área")

    _tg(f"📋 <b>Zona escaneada</b> — {len(unique_ids)} parcelas encontradas\n"
        f"Descargando datos de cada parcela...")

    lots = _get_lot_details(unique_ids, input.delay_ms)
    logger.info(f"SmartGIS: {len(lots)} lotes con detalle")

    # Filtrar por zona poligonal exacta.
    # Usamos intersects(geom) en vez de contains(centroid): un lote grande puede
    # tener el centroide fuera de la zona aunque parte de su polígono caiga dentro.
    fuera = 0
    if zone_polygon:
        dentro = []
        for i, lot in enumerate(lots):
            geom = lot["_geom"]
            if geom is None:
                in_zone = zone_polygon.contains(Point(lot["_lon"], lot["_lat"]))
            else:
                in_zone = zone_polygon.intersects(geom)

            # Log detallado para los primeros 3 lotes (diagnóstico)
            if i < 3:
                codigo = _parse_codigo(lot)
                if geom is not None:
                    b = geom.bounds
                    logger.info(
                        f"  Lote {codigo}: geom.bounds=lng[{b[0]:.4f},{b[2]:.4f}] "
                        f"lat[{b[1]:.4f},{b[3]:.4f}] → {'DENTRO' if in_zone else 'FUERA'}"
                    )
                else:
                    logger.info(
                        f"  Lote {codigo}: centroide=({lot['_lon']:.4f},{lot['_lat']:.4f}) "
                        f"→ {'DENTRO' if in_zone else 'FUERA'}"
                    )

            if in_zone:
                dentro.append(lot)
            else:
                fuera += 1
        lots = dentro

    logger.info(f"SmartGIS: {len(lots)} dentro de zona, {fuera} fuera")
    inserted, updated = _upsert_lots(lots, input.region_id, input.survey_id)

    return SmartGISOutput(
        ok=True,
        parcelas_insertadas=inserted,
        parcelas_actualizadas=updated,
        lotes_escaneados=len(unique_ids),
        fuera_de_zona=fuera,
    )
