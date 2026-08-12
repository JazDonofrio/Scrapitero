"""OsmPoiFetcher — POIs de OpenStreetMap que Overture NO publica (GRATIS).

Qué resuelve: Overture es la fuente de comercios fuera de Brasil, pero **tiene rubros que
en Argentina vienen vacíos**. El caso medido: **estaciones de servicio**. En Malvinas
Argentinas —una zona cruzada por la Ruta 8 y la Ruta 202— Overture devolvió **cero**:
ni un `gas_station`, ni un YPF/Shell/Axion/Puma por nombre, en 218 POIs. OSM tiene **tres**
en el mismo bbox, dos adentro de la zona, y una es la que el operador ve desde la vereda
al lado del McDonald's. Un cero así no es un dato, es un agujero
(ver `_try_mirrors`: "el cero hay que ganárselo").

No reemplaza a `OverturePlacesFetcher`: lo **completa**. Por eso los tags son un parámetro
y no una lista fija — cuando aparezca otro rubro vacío se suma acá, no se escribe otro
agente.

Escribe en los mismos dos lugares que Overture, con los mismos roles:
  - **`comercios`** (`source='osm'`) → nombre y punto para el mapa y el CSV.
  - **`establecimientos_poi`** (`fuente='osm_poi'`) → la etiqueta de la taxonomía del
    cliente (POSTO DE GASOLINA → "ESTACIÓN DE SERVICIO" en la web en español), para que
    **`ParcelaCategoria`** la aterrice sobre la parcela y le ponga el **piso de UF=1** si el
    lote no tiene ningún conteo. `fuente='osm_poi'` NO es `'osm'` a propósito:
    `ShoppingFetcher` borra sus POIs por fuente y se los llevaría puestos.

Reusa la **guarda de huella** de `OverturePlacesFetcher._link_to_parcelas` (importada, no
copiada): un POI que cae en un lote sin ninguna construcción se reasigna al lote construido
más cercano y, si no hay, queda sin parcela.

Idempotente: upsert por `(region_id, place_id)` en `comercios` con `place_id='osm:way/123'`,
y borrado+reinserto de los `establecimientos_poi` de fuente `osm_poi` de la región.
"""

from __future__ import annotations

import uuid
from typing import Optional

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.google_places_fetcher import _load_zone
from scrapitero.agents.osm_building_fetcher import _bbox_from_db, _fetch_overpass
from scrapitero.agents.overture_places_fetcher import _link_to_parcelas
from scrapitero.db.engine import get_engine

# tag de OSM → (rubro compatible con Overture, categoría R/C/E, etiqueta del cliente).
# El rubro se nombra igual que en Overture para que cualquier lógica que ya filtre por
# rubro (p.ej. `_NO_COMERCIO`) siga valiendo sin enterarse de la fuente.
_TAGS: dict[str, tuple[str, str, str]] = {
    "amenity=fuel": ("gas_station", "E", "POSTO DE GASOLINA"),
}

_NOMBRE_GENERICO = {"gas_station": "Estación de servicio"}


class OsmPoiInput(BaseModel):
    region_id: str
    survey_id: str
    tags: list[str] = ["amenity=fuel"]
    timeout_s: float = 60.0
    exigir_huella: bool = True
    reasignar_max_m: float = 40.0


class OsmPoiOutput(BaseModel):
    ok: bool = True
    error: Optional[str] = None
    region_id: str = ""
    pois_encontrados: int = 0        # dentro de la zona, ya recortados
    comercios_guardados: int = 0
    vinculados_a_parcela: int = 0
    pois_reasignados_por_huella: int = 0
    pois_sin_edificio: int = 0
    por_tag: dict = {}


def _query(tags: list[str], s: float, w: float, n: float, e: float) -> str:
    """Nodos Y ways con cada tag. Las estaciones de servicio suelen ser un `way` (el
    polígono del playón), así que pedir sólo nodos deja la mitad afuera: `out center`
    devuelve el centroide del way y lo trata igual que un punto."""
    bbox = f"({s},{w},{n},{e})"
    partes = []
    for t in tags:
        k, _, v = t.partition("=")
        sel = f'["{k}"="{v}"]' if v else f'["{k}"]'
        partes.append(f"node{sel}{bbox};")
        partes.append(f"way{sel}{bbox};")
    return f"[out:json][timeout:50];({''.join(partes)});out center tags;"


def _parse(data: dict, tags: list[str]) -> list[dict]:
    pois = []
    for el in (data or {}).get("elements", []):
        t = el.get("tags") or {}
        lat = el.get("lat") if el.get("lat") is not None else (el.get("center") or {}).get("lat")
        lng = el.get("lon") if el.get("lon") is not None else (el.get("center") or {}).get("lon")
        if lat is None or lng is None:
            continue
        # ¿Qué tag de los pedidos matcheó? (un elemento puede traer varios)
        cfg = None
        for tag in tags:
            k, _, v = tag.partition("=")
            if t.get(k) and (not v or t.get(k) == v):
                cfg = _TAGS.get(tag)
                break
        if not cfg:
            continue
        rubro, categoria, etiqueta = cfg
        # Una estación sin `name` igual existe: se la nombra por la marca y, si tampoco,
        # con el genérico. Descartarla por no tener nombre sería perder el dato que falta.
        nombre = (t.get("name") or t.get("brand") or t.get("operator")
                  or _NOMBRE_GENERICO.get(rubro) or rubro)
        direccion = " ".join(x for x in (t.get("addr:street"), t.get("addr:housenumber")) if x)
        pois.append({
            "place_id": f"osm:{el.get('type')}/{el.get('id')}",
            "nombre": nombre, "rubro": rubro, "categoria": categoria,
            "etiqueta": etiqueta, "direccion": direccion or None,
            "lat": float(lat), "lng": float(lng),
        })
    return pois


def _upsert_comercios(region_id: str, survey_id: str, pois: list[dict]) -> int:
    engine = get_engine()
    with engine.begin() as conn:
        for p in pois:
            conn.execute(text("""
                INSERT INTO comercios (comercio_id, survey_id, region_id, place_id, nombre,
                                       rubro, tipos, location, source, fetched_at)
                VALUES (:cid, CAST(:sid AS uuid), :rid, :pid, :nombre, :rubro, :dir,
                        ST_SetSRID(ST_MakePoint(:lng, :lat), 4326), 'osm', now())
                ON CONFLICT (region_id, place_id) DO UPDATE SET
                    nombre = EXCLUDED.nombre, rubro = EXCLUDED.rubro,
                    tipos = EXCLUDED.tipos, location = EXCLUDED.location,
                    survey_id = EXCLUDED.survey_id, fetched_at = now()
            """), {"cid": str(uuid.uuid4()), "sid": survey_id, "rid": region_id,
                   "pid": p["place_id"], "nombre": p["nombre"], "rubro": p["rubro"],
                   "dir": p["direccion"], "lat": p["lat"], "lng": p["lng"]})
    return len(pois)


def _sellar_pois(region_id: str, pois: list[dict]) -> int:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM establecimientos_poi "
                          "WHERE region_id = :rid AND fuente = 'osm_poi'"), {"rid": region_id})
        for p in pois:
            conn.execute(text("""
                INSERT INTO establecimientos_poi
                    (poi_id, region_id, fuente, categoria, descripcion, nombre, lat, lng)
                VALUES (:id, :rid, 'osm_poi', :cat, :desc, :nombre, :lat, :lng)
            """), {"id": str(uuid.uuid4()), "rid": region_id, "cat": p["categoria"],
                   "desc": p["etiqueta"], "nombre": p["nombre"],
                   "lat": p["lat"], "lng": p["lng"]})
    return len(pois)


@agent_run
def run(input: OsmPoiInput) -> OsmPoiOutput:
    out = OsmPoiOutput(region_id=input.region_id)

    desconocidos = [t for t in input.tags if t not in _TAGS]
    if desconocidos:
        return OsmPoiOutput(ok=False, region_id=input.region_id,
                            error=f"tags sin mapeo a la taxonomía: {desconocidos} "
                                  f"(agregarlos a _TAGS con su etiqueta del cliente)")

    bbox, zone = _load_zone(input.region_id, input.survey_id)
    if bbox is None:
        bbox = _bbox_from_db(input.region_id, 0.001)
    if bbox is None:
        return OsmPoiOutput(ok=False, region_id=input.region_id,
                            error="sin zona ni parcelas para derivar el bbox")

    south, west, north, east = bbox
    try:
        data = _fetch_overpass(_query(input.tags, south, west, north, east),
                               timeout=input.timeout_s)
    except Exception as e:  # noqa: BLE001
        return OsmPoiOutput(ok=False, region_id=input.region_id,
                            error=f"Overpass falló: {e}")
    if not data:
        # `_fetch_overpass` ya distingue "sin resultados" de "todos los mirrors caídos".
        return OsmPoiOutput(ok=False, region_id=input.region_id,
                            error="Overpass no devolvió una respuesta confiable "
                                  "(mirrors caídos): el cero NO es dato")

    pois = _parse(data, input.tags)

    # Recorte exacto a la zona: el bbox es un rectángulo y la zona puede ser un polígono
    # rotado, como el de Malvinas — una de las tres estaciones del bbox cae afuera.
    if zone is not None and not zone.is_empty:
        from shapely.geometry import Point
        pois = [p for p in pois if zone.covers(Point(p["lng"], p["lat"]))]

    out.pois_encontrados = len(pois)
    for p in pois:
        out.por_tag[p["rubro"]] = out.por_tag.get(p["rubro"], 0) + 1
    if not pois:
        logger.info(f"OsmPoi {input.region_id}: sin POIs {input.tags} dentro de la zona")
        return out

    out.comercios_guardados = _upsert_comercios(input.region_id, input.survey_id, pois)
    out.vinculados_a_parcela, out.pois_reasignados_por_huella, out.pois_sin_edificio = (
        _link_to_parcelas(input.region_id, input.survey_id, input.exigir_huella,
                          input.reasignar_max_m, source="osm"))
    _sellar_pois(input.region_id, pois)

    logger.info(f"OsmPoi {input.region_id}: {out.pois_encontrados} POIs {out.por_tag} · "
                f"{out.vinculados_a_parcela} vinculados · "
                f"{out.pois_reasignados_por_huella} reasignados por huella")
    return out
