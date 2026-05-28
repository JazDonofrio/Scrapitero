"""
SmartGIS Várzea Grande Fetcher — Agente de adquisición.

Responsabilidad atómica: descargar parcelas (lotes) y su geometría 
desde el geoportal SmartGIS de Várzea Grande para un bounding box dado.

Flujo:
1. Llama a IdentifyOnExtent para obtener IDs de lotes en el BBOX.
2. Itera sobre los IDs y llama a Get/{id} para obtener el GeoJSON y atributos.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

API_BASE_URL = "https://api.smartgis.net.br/varzeagrande/prefeitura/api"

# ── Schemas ──────────────────────────────────────────────────────────────

class SmartGISFetchInput(BaseModel):
    """Parámetros de entrada del agente SmartGIS Várzea Grande."""

    bbox: tuple[float, float, float, float] = Field(
        ...,
        description=(
            "Bounding box (lat_min, lon_min, lat_max, lon_max) en WGS84. "
            "El sistema espera grados decimales."
        ),
    )
    timeout_seconds: int = Field(
        default=60,
        ge=5,
        le=180,
        description="Timeout para las request HTTP.",
    )


class SmartGISLot(BaseModel):
    """Un lote (parcela) obtenido de SmartGIS."""

    lot_id: int
    inscricao: str
    lat: float
    lon: float
    geojson: dict[str, Any]
    attributes: dict[str, Any]


class SmartGISFetchOutput(BaseModel):
    """Resultado del agente SmartGIS."""

    bbox: tuple[float, float, float, float]
    lots: list[SmartGISLot]
    count: int
    source: str = "smartgis_varzea_grande"
    ok: bool = True
    error: str | None = None

    @classmethod
    def empty(cls, bbox: tuple[float, float, float, float], error: str) -> "SmartGISFetchOutput":
        return cls(bbox=bbox, lots=[], count=0, ok=False, error=error)


# ── Implementación ───────────────────────────────────────────────────────

def _element_centroid(geojson: dict) -> tuple[float, float] | None:
    """Calcula un centroide aproximado a partir de las coordenadas del polígono."""
    try:
        if geojson.get("type") == "Polygon":
            coords = geojson["coordinates"][0]
            if not coords:
                return None
            lats = [c[1] for c in coords]
            lons = [c[0] for c in coords]
            return (sum(lats) / len(lats), sum(lons) / len(lons))
        elif geojson.get("type") == "MultiPolygon":
            coords = geojson["coordinates"][0][0]
            if not coords:
                return None
            lats = [c[1] for c in coords]
            lons = [c[0] for c in coords]
            return (sum(lats) / len(lats), sum(lons) / len(lons))
    except Exception:
        return None
    return None


def fetch(
    input: SmartGISFetchInput,
    client: httpx.Client | None = None,
) -> SmartGISFetchOutput:
    """
    Descarga lotes desde SmartGIS Várzea Grande para el bbox.

    Devuelve:
        SmartGISFetchOutput. Si falla, devuelve `ok=False` con `error`
        descriptivo y `lots=[]`.
    """
    import math
    import time
    import random
    
    lat1, lon1, lat2, lon2 = input.bbox
    lat_min, lat_max = sorted([lat1, lat2])
    lon_min, lon_max = sorted([lon1, lon2])
    
    # Cálculo aproximado de la superficie (en hectáreas y km2)
    # 1 grado de latitud ~ 111.32 km
    lat_mid = (lat_min + lat_max) / 2
    height_km = (lat_max - lat_min) * 111.32
    width_km = (lon_max - lon_min) * 111.32 * math.cos(math.radians(lat_mid))
    area_km2 = width_km * height_km
    area_hectareas = area_km2 * 100
    
    log.info(
        "SmartGIS Várzea Grande Fetcher: bbox=%s (Área aprox: %.2f km² / %.1f hectáreas) timeout=%ss",
        input.bbox,
        area_km2,
        area_hectareas,
        input.timeout_seconds,
    )

    owns_client = client is None
    if owns_client:
        client = httpx.Client(follow_redirects=True, headers={"User-Agent": "ScrapiteroBot/1.0"})

    try:
        # --- OPCIÓN A: Escaneo por cuadrícula (Grid Scanner) ---
        # Como el servidor limita IdentifyOnExtent a 2 lotes por request,
        # fraccionamos el BBOX grande en cajas muy pequeñas (ej. ~30x30 metros)
        # para simular "clicks" por toda el área y no perder ningún lote.
        
        STEP = 0.0003  # Aprox 30 metros
        unique_lot_ids = set()
        
        # Calcular el número de pasos
        lat_steps = max(1, int((lat_max - lat_min) / STEP) + 1)
        lon_steps = max(1, int((lon_max - lon_min) / STEP) + 1)
        total_cells = lat_steps * lon_steps
        
        log.info("SmartGIS: Iniciando escaneo por cuadrícula. Dividiendo el área en %d celdas (%.5f grados/celda) para evadir el límite del servidor...", total_cells, STEP)
        
        # 1. Identificar lotes escaneando la grilla
        cells_scanned = 0
        for i in range(lat_steps):
            for j in range(lon_steps):
                c_lat_min = lat_min + (i * STEP)
                c_lat_max = min(lat_max, c_lat_min + STEP)
                c_lon_min = lon_min + (j * STEP)
                c_lon_max = min(lon_max, c_lon_min + STEP)
                
                extent = [c_lon_min, c_lat_min, c_lon_max, c_lat_max]
                
                try:
                    r = client.post(
                        f"{API_BASE_URL}/PublicLote/IdentifyOnExtent",
                        json=extent,
                        timeout=input.timeout_seconds,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        if isinstance(data, list):
                            unique_lot_ids.update(data)
                except httpx.HTTPError:
                    pass  # Ignoramos fallos en celdas individuales para continuar el escaneo
                
                # Pausa para evitar baneo
                time.sleep(random.uniform(0.05, 0.15))
                
                cells_scanned += 1
                if cells_scanned % 50 == 0:
                    log.info("SmartGIS: Progreso escaneo... %d/%d celdas. Lotes únicos encontrados hasta ahora: %d", cells_scanned, total_cells, len(unique_lot_ids))

        log.info("SmartGIS: Escaneo completado. Se encontraron %d lotes únicos en total.", len(unique_lot_ids))

        lots: list[SmartGISLot] = []
        
        # 2. Descargar detalle de cada lote
        log.info("SmartGIS: Descargando geometrías y atributos de los %d lotes...", len(unique_lot_ids))
        for idx, lot_id in enumerate(unique_lot_ids, start=1):
            try:
                lot_resp = client.get(
                    f"{API_BASE_URL}/PublicLote/Get/{lot_id}",
                    timeout=input.timeout_seconds,
                )
                if lot_resp.status_code == 200:
                    data = lot_resp.json()
                    geom_str = data.get("geoJson", "{}")
                    try:
                        geom = json.loads(geom_str)
                    except Exception:
                        geom = {}
                        
                    centroid = _element_centroid(geom) or (0.0, 0.0)
                    
                    lots.append(SmartGISLot(
                        lot_id=data.get("id", lot_id),
                        inscricao=data.get("inscricao", ""),
                        lat=centroid[0],
                        lon=centroid[1],
                        geojson=geom,
                        attributes=data
                    ))
            except httpx.HTTPError as exc:
                log.warning("SmartGIS: HTTP error fetching lot %s — %s", lot_id, exc)
                continue
            
            if idx % 50 == 0:
                log.info("SmartGIS: Descarga de detalles en progreso... %d/%d", idx, len(unique_lot_ids))

        log.info("SmartGIS Várzea Grande Fetcher: %d elementos procesados exitosamente", len(lots))
        return SmartGISFetchOutput(
            bbox=input.bbox,
            lots=lots,
            count=len(lots),
        )
    finally:
        if owns_client:
            client.close()
