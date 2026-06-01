"""
OSMBuildingsFetcher — Agente de adquisición.

Responsabilidad atómica: descargar edificios y nodos con dirección
desde OpenStreetMap (vía Overpass API) para un bounding box.

Función pura: no usa input(), no usa print() (solo logging),
no toca el filesystem. Solo HTTP → datos tipados.

Uso directo (notebook):
    from scrapitero.agents.osm_buildings import fetch, OSMFetchInput
    out = fetch(OSMFetchInput(bbox=(-15.65, -56.14, -15.64, -56.13)))
    print(out.count, "edificios")

Uso desde el orquestador-LLM:
    Se registra como tool con `input_schema = OSMFetchInput.model_json_schema()`.
"""

from __future__ import annotations

import logging
from typing import Literal

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

OVERPASS_MIRRORS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
TOR_SOCKS5 = "socks5://127.0.0.1:9050"
OVERPASS_MIRRORS_TOR = [
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ScrapiteroResearch/1.0; +https://github.com/scrapitero)",
    "Accept": "*/*",
}


def _tor_available() -> bool:
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", 9050), timeout=2)
        s.close()
        return True
    except OSError:
        return False


# ── Schemas ──────────────────────────────────────────────────────────────

class OSMFetchInput(BaseModel):
    """Parámetros de entrada del agente OSMBuildingsFetcher."""

    bbox: tuple[float, float, float, float] = Field(
        ...,
        description=(
            "Bounding box (lat_min, lon_min, lat_max, lon_max) en WGS84. "
            "El sistema espera grados decimales."
        ),
    )
    include_address_nodes: bool = Field(
        default=True,
        description=(
            "Si True, incluye nodos sin tag `building` pero con `addr:street` "
            "(útiles para resolver direcciones independientes del edificio)."
        ),
    )
    timeout_seconds: int = Field(
        default=40,
        ge=5,
        le=180,
        description="Timeout para la request HTTP a Overpass.",
    )


class OSMBuilding(BaseModel):
    """Un elemento OSM (way o node) relevante para el relevamiento."""

    osm_id: int
    osm_type: Literal["way", "node"]
    lat: float
    lon: float
    tags: dict[str, str]


class OSMFetchOutput(BaseModel):
    """Resultado del agente OSMBuildingsFetcher."""

    bbox: tuple[float, float, float, float]
    buildings: list[OSMBuilding]
    count: int
    source: str = "overpass"
    ok: bool = True
    error: str | None = None

    @classmethod
    def empty(cls, bbox: tuple[float, float, float, float], error: str) -> "OSMFetchOutput":
        return cls(bbox=bbox, buildings=[], count=0, ok=False, error=error)


# ── Implementación ───────────────────────────────────────────────────────

def _build_query(bbox: tuple[float, float, float, float], include_addr_nodes: bool) -> str:
    lat1, lon1, lat2, lon2 = bbox
    lat_min, lat_max = sorted([lat1, lat2])
    lon_min, lon_max = sorted([lon1, lon2])
    box_str = f"{lat_min},{lon_min},{lat_max},{lon_max}"
    addr_node_line = f'  node["addr:street"]({box_str});' if include_addr_nodes else ""
    return f"""
[out:json][timeout:30];
(
  way["building"]({box_str});
  node["building"]({box_str});
{addr_node_line}
);
out body;
>;
out skel qt;
""".strip()


def _index_node_coords(elements: list[dict]) -> dict[int, tuple[float, float]]:
    return {
        el["id"]: (el["lat"], el["lon"])
        for el in elements
        if el.get("type") == "node" and "lat" in el and "lon" in el
    }


def _element_centroid(
    el: dict, nodes_xy: dict[int, tuple[float, float]]
) -> tuple[float, float] | None:
    if el["type"] == "way":
        coords = [nodes_xy[n] for n in el.get("nodes", []) if n in nodes_xy]
        if not coords:
            return None
        return (
            sum(c[0] for c in coords) / len(coords),
            sum(c[1] for c in coords) / len(coords),
        )
    if el["type"] == "node":
        return (el["lat"], el["lon"])
    return None


def fetch(
    input: OSMFetchInput,
    client: httpx.Client | None = None,
) -> OSMFetchOutput:
    """
    Descarga edificios y nodos con dirección desde Overpass para el bbox.

    Parámetros:
        input: parámetros tipados (ver OSMFetchInput)
        client: cliente httpx opcional. Si es None, crea uno efímero.

    Devuelve:
        OSMFetchOutput. Si Overpass falla, devuelve `ok=False` con `error`
        descriptivo y `buildings=[]` (no lanza excepción).
    """
    query = _build_query(input.bbox, input.include_address_nodes)
    log.info(
        "OSMBuildingsFetcher: bbox=%s timeout=%ss",
        input.bbox,
        input.timeout_seconds,
    )

    owns_client = client is None
    if owns_client:
        client = httpx.Client(follow_redirects=True)

    try:
        r = None
        last_error = ""

        # Intento 1: mirrors directos
        for mirror in OVERPASS_MIRRORS:
            try:
                r = client.post(mirror, data={"data": query},
                                timeout=input.timeout_seconds, headers=_HEADERS)
                if r.status_code == 200:
                    log.info("OSMBuildingsFetcher: mirror OK — %s", mirror)
                    break
                last_error = f"overpass_http_{r.status_code} ({mirror})"
                log.warning("OSMBuildingsFetcher: %s", last_error)
                r = None
            except httpx.HTTPError as exc:
                last_error = f"http_error ({mirror}): {exc}"
                log.warning("OSMBuildingsFetcher: %s", last_error)
                r = None

        # Intento 2: via Tor si directos fallaron
        if r is None and _tor_available():
            log.info("OSMBuildingsFetcher: reintentando via Tor...")
            for mirror in OVERPASS_MIRRORS_TOR:
                try:
                    tor_client = httpx.Client(proxies=TOR_SOCKS5, follow_redirects=True)
                    r = tor_client.post(mirror, data={"data": query},
                                        timeout=input.timeout_seconds, headers=_HEADERS)
                    tor_client.close()
                    if r.status_code == 200:
                        log.info("OSMBuildingsFetcher: Tor OK — %s", mirror)
                        break
                    last_error = f"tor_http_{r.status_code} ({mirror})"
                    log.warning("OSMBuildingsFetcher: %s", last_error)
                    r = None
                except httpx.HTTPError as exc:
                    last_error = f"tor_error ({mirror}): {exc}"
                    log.warning("OSMBuildingsFetcher: %s", last_error)
                    r = None

        if r is None:
            return OSMFetchOutput.empty(input.bbox, last_error)

        try:
            data = r.json()
        except ValueError as exc:
            return OSMFetchOutput.empty(input.bbox, f"invalid_json: {exc}")

        elements = data.get("elements", [])
        nodes_xy = _index_node_coords(elements)

        buildings: list[OSMBuilding] = []
        seen: set[str] = set()

        for el in elements:
            key = f"{el['type']}:{el['id']}"
            if key in seen:
                continue
            tags = el.get("tags") or {}

            is_building = "building" in tags
            has_addr = "addr:street" in tags
            if not (is_building or has_addr):
                continue

            centroid = _element_centroid(el, nodes_xy)
            if centroid is None:
                continue

            seen.add(key)
            buildings.append(
                OSMBuilding(
                    osm_id=el["id"],
                    osm_type=el["type"],
                    lat=centroid[0],
                    lon=centroid[1],
                    tags={k: str(v) for k, v in tags.items()},
                )
            )

        log.info("OSMBuildingsFetcher: %d elementos encontrados", len(buildings))
        return OSMFetchOutput(
            bbox=input.bbox,
            buildings=buildings,
            count=len(buildings),
        )
    finally:
        if owns_client:
            client.close()
