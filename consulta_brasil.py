"""
Consulta de edificios por quadra — Brasil (Várzea Grande, MT)

Fuentes de datos:
  1. OpenStreetMap Overpass  — polígonos de edificios y direcciones georreferenciadas
     (vía scrapitero.agents.osm_buildings — agente reutilizable por el orquestador)
  2. Google Maps Geocoding   — dirección (calle y número) por coordenadas
     Fallback: Nominatim    — cuando GOOGLE_API_KEY no está configurada
  3. IBGE API                — referencia del setor censitário que contiene el área

ADVERTENCIA: Los datos catastrales de lotes (IPTU/cadastro imobiliário) de Várzea Grande
no son de acceso público. No existe API equivalente a ARBA para el catastro urbano de MT.
Este script usa OpenStreetMap como mejor alternativa disponible.

Variables de entorno (cargar vía .env — ver .env.example):
  GOOGLE_API_KEY   Clave de Google Maps Geocoding API (opcional, mejora direcciones)

Uso:
  python3 consulta_brasil.py

Nota arquitectónica: este script es el "wrapper CLI". La lógica de fetch
a OSM vive en scrapitero.agents.osm_buildings.fetch() y puede ser
invocada también desde notebooks o desde el orquestador-LLM.
"""

import csv
import json
import os
import time
import httpx

# Carga .env y agentes nuevos
from scrapitero import config
from scrapitero.agents.osm_buildings import OSMFetchInput, fetch as osm_fetch

# ── Endpoints ─────────────────────────────────────────────────────────────────

IBGE_MALHA   = "https://servicodados.ibge.gov.br/api/v3/malhas/municipios"
GMAPS_GEO    = "https://maps.googleapis.com/maps/api/geocode/json"
NOMINATIM_R  = "https://nominatim.openstreetmap.org/reverse"

# ── Configuración ─────────────────────────────────────────────────────────────

IBGE_COD_VG    = "5108402"  # Várzea Grande, Mato Grosso
GOOGLE_API_KEY = config.google_api_key()

# Bbox por defecto: área central de Várzea Grande (cuadras próximas a la prefeitura)
# Formato: (lat_min, lon_min, lat_max, lon_max)
DEFAULT_BBOX = (-15.6530, -56.1380, -15.6460, -56.1300)


# ── Input ─────────────────────────────────────────────────────────────────────

def pedir_bbox() -> tuple[float, float, float, float]:
    print(
        "Bounding box de la quadra/área a consultar "
        "(Enter = defaults: centro Várzea Grande):"
    )
    print(f"  [default: lat_min={DEFAULT_BBOX[0]}  lon_min={DEFAULT_BBOX[1]}"
          f"  lat_max={DEFAULT_BBOX[2]}  lon_max={DEFAULT_BBOX[3]}]")
    raw = input("  lat_min lon_min lat_max lon_max [Enter = defaults]: ").strip()
    if not raw:
        return DEFAULT_BBOX
    try:
        parts = [float(x) for x in raw.split()]
        if len(parts) != 4:
            raise ValueError
        return tuple(parts)  # type: ignore[return-value]
    except ValueError:
        print("  Formato inválido — usando defaults.")
        return DEFAULT_BBOX


# ── OpenStreetMap Overpass ─────────────────────────────────────────────────────
#
# La lógica de fetch a Overpass se movió a scrapitero.agents.osm_buildings.
# Este wrapper conserva la firma original (list[dict]) para no romper el resto
# del script y agrega la presentación (print del bbox y del error).

def fetch_buildings_osm(
    client: httpx.Client,
    bbox: tuple[float, float, float, float],
) -> list[dict]:
    """
    Wrapper de compatibilidad sobre scrapitero.agents.osm_buildings.fetch().
    Mantiene la salida como list[dict] que el resto del script ya consume.
    """
    lat_min, lon_min, lat_max, lon_max = bbox
    print(f"  Overpass bbox: ({lat_min:.4f},{lon_min:.4f}) → ({lat_max:.4f},{lon_max:.4f})")

    out = osm_fetch(
        OSMFetchInput(bbox=bbox, include_address_nodes=True),
        client=client,
    )

    if not out.ok:
        print(f"  ERROR: {out.error}")
        return []

    return [
        {
            "osm_id":  b.osm_id,
            "osm_type": b.osm_type,
            "lat": b.lat,
            "lon": b.lon,
            "tags": b.tags,
        }
        for b in out.buildings
    ]


# ── Dirección desde tags OSM ───────────────────────────────────────────────────

def addr_from_tags(tags: dict) -> str:
    """Extrae dirección completa desde tags OSM addr:*."""
    street = tags.get("addr:street", "")
    number = tags.get("addr:housenumber", "")
    if street and number:
        return f"{street} {number}"
    if street:
        return street
    name = tags.get("name", "")
    return name


# ── Geocodificación inversa ────────────────────────────────────────────────────

def _extract_road_num(results: list[dict]) -> tuple[str, str]:
    """
    Busca en la lista de resultados de Google Maps el primero que tenga
    street_number. Si ninguno lo tiene, retorna el road del primer resultado.
    """
    road_fallback = ""
    for r0 in results:
        comps = {
            t: c["long_name"]
            for c in r0.get("address_components", [])
            for t in c["types"]
        }
        road = comps.get("route", "")
        num  = comps.get("street_number", "")
        if road and num:
            return road, num
        if road and not road_fallback:
            road_fallback = road
    return road_fallback, ""


def _addr_from_google(lat: float, lon: float) -> str:
    if not GOOGLE_API_KEY:
        print("        Google Maps: GOOGLE_API_KEY no configurada — usando Nominatim")
        return ""

    for intento in range(1, 4):
        try:
            # Primer intento: result_type=street_address fuerza snapping al
            # punto más cercano que tenga número asignado
            r = httpx.get(
                GMAPS_GEO,
                params={
                    "latlng": f"{lat},{lon}",
                    "result_type": "street_address",
                    "key": GOOGLE_API_KEY,
                },
                timeout=10,
            )
            d      = r.json()
            status = d.get("status")

            if status == "REQUEST_DENIED":
                print(
                    f"        Google Maps: REQUEST_DENIED (intento {intento}/3)"
                    f" — {d.get('error_message', '')!r}"
                )
                if intento < 3:
                    time.sleep(1.5)
                    continue
                return ""

            results = d.get("results", [])

            # Si result_type no devuelve nada, reintentar sin filtro
            if status == "ZERO_RESULTS" or not results:
                r2 = httpx.get(
                    GMAPS_GEO,
                    params={"latlng": f"{lat},{lon}", "key": GOOGLE_API_KEY},
                    timeout=10,
                )
                d2 = r2.json()
                if d2.get("status") == "OK":
                    results = d2.get("results", [])

            if not results:
                print(f"        Google Maps: sin resultados (status={status!r})")
                return ""

            road, num = _extract_road_num(results)
            addr = f"{road} {num}".strip() if road else ""
            tag  = "con nro" if num else "sin nro"
            print(f"        Google Maps [{tag}]: {results[0].get('formatted_address')!r}  →  {addr!r}")
            return addr

        except Exception as exc:
            print(f"        Google Maps: excepción (intento {intento}/3) — {exc}")
            if intento < 3:
                time.sleep(1.5)

    return ""


def _addr_from_nominatim(client: httpx.Client, lat: float, lon: float) -> str:
    try:
        r = client.get(
            NOMINATIM_R,
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 18},
            headers={"User-Agent": "Scrapitero/1.0"},
            timeout=10,
        )
        if r.status_code == 200:
            d    = r.json()
            addr = d.get("address", {})
            road = (
                addr.get("road")
                or addr.get("pedestrian")
                or addr.get("footway")
                or ""
            )
            num = addr.get("house_number", "")
            return f"{road} {num}".strip() if road else d.get("display_name", "")[:50]
    except Exception:
        pass
    return ""


def geocodificar(client: httpx.Client, lat: float, lon: float) -> tuple[str, str]:
    addr = _addr_from_google(lat, lon)
    if addr:
        return addr, "google"

    if GOOGLE_API_KEY:
        print(f"        Google Maps: sin resultado — intentando Nominatim")

    time.sleep(1.1)
    addr = _addr_from_nominatim(client, lat, lon)
    if addr:
        return addr, "nominatim"

    return "", "sin datos"


# ── IBGE: setor censitário de referencia ──────────────────────────────────────

def fetch_ibge_setor(client: httpx.Client, lat: float, lon: float) -> str:
    """
    Consulta la API de malhas del IBGE para identificar el setor censitário
    que contiene el punto central del área consultada.
    Retorna el código del setor o cadena vacía si no se puede determinar.
    """
    try:
        r = client.get(
            f"{IBGE_MALHA}/{IBGE_COD_VG}",
            params={"formato": "application/vnd.geo+json", "resolucao": "8"},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            props = data.get("features", [{}])[0].get("properties", {})
            return props.get("codarea", props.get("CD_SETOR", ""))
    except Exception:
        pass
    return ""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    bbox = pedir_bbox()
    lat_centro = (bbox[0] + bbox[2]) / 2
    lon_centro = (bbox[1] + bbox[3]) / 2

    print()
    print("Configuración:")
    print(f"  GOOGLE_API_KEY : {'configurada (' + GOOGLE_API_KEY[:8] + '...)' if GOOGLE_API_KEY else 'NO configurada — se usará Nominatim'}")
    print(f"  IBGE municipio : {IBGE_COD_VG} (Várzea Grande, MT)")
    print()
    print("AVISO: Cadastro imobiliário (IPTU/lotes) de Várzea Grande no disponible vía API pública.")
    print("       Datos de edificios provistos por OpenStreetMap.")
    print()

    resultados: list[dict] = []

    with httpx.Client(follow_redirects=True) as client:

        # ── 1. Edificios OSM ──────────────────────────────────────────────────
        print("══ Edificios OSM (Overpass) ══")
        edificios = fetch_buildings_osm(client, bbox)
        print(f"  {len(edificios)} elementos encontrados en el área")

        if not edificios:
            print(
                "\n  Sin datos de Overpass. Posibles causas:\n"
                "    - Área sin edificios mapeados en OSM\n"
                "    - Overpass API temporalmente no disponible\n"
                "    - Timeout (área demasiado grande)\n"
                "  Ajustá el bbox y reintentá."
            )
            return

        # ── 2. Geocodificación ────────────────────────────────────────────────
        print()
        print("══ Geocodificación inversa ══")
        for i, ed in enumerate(edificios, 1):
            tags_addr = addr_from_tags(ed["tags"])
            if tags_addr:
                ed["direccion"] = tags_addr
                ed["dir_fuente"] = "osm"
                print(f"  [{i:3d}] OSM tags    → {tags_addr!r}")
                continue

            print(f"  [{i:3d}] ({ed['lat']:.5f}, {ed['lon']:.5f}) — sin addr en OSM, consultando...")
            addr, fuente = geocodificar(client, ed["lat"], ed["lon"])
            ed["direccion"] = addr
            ed["dir_fuente"] = fuente
            print(f"         [{fuente}] → {addr!r}")

        # ── 3. Atributos de edificio ──────────────────────────────────────────
        for ed in edificios:
            tags = ed["tags"]
            ed["tipo"]  = tags.get("building", "")
            ed["pisos"] = tags.get("building:levels", tags.get("levels", ""))
            ed["nombre"]= tags.get("name", "")
            resultados.append(ed)

    # ── Tabla resumen ──────────────────────────────────────────────────────────
    W = 108
    print()
    print("=" * W)
    print(
        f"  {'#':>4}  {'DIRECCIÓN':<36}  {'COORD':<23}  "
        f"{'TIPO':<10}  {'PISOS':>5}  {'NOMBRE':<16}  {'FUENTE':<10}"
    )
    print("=" * W)

    for i, r in enumerate(resultados, 1):
        coord = f"{r['lat']:.5f},{r['lon']:.5f}"
        print(
            f"  {i:>4}  {r['direccion']:<36}  {coord:<23}  "
            f"{r['tipo']:<10}  {r['pisos']:>5}  {r['nombre']:<16}  {r['dir_fuente']:<10}"
        )

    print("=" * W)
    print(f"  Total: {len(resultados)} edificios/elementos en el área")
    print()

    # ── Guardar archivos ───────────────────────────────────────────────────────
    tag = f"{bbox[0]:.3f}_{bbox[1]:.3f}"
    tag = tag.replace("-", "m").replace(".", "p")

    out_json = f"vg_quadra_{tag}.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(
            {
                "municipio": "Várzea Grande",
                "estado": "Mato Grosso",
                "ibge_cod": IBGE_COD_VG,
                "bbox": {"lat_min": bbox[0], "lon_min": bbox[1],
                         "lat_max": bbox[2], "lon_max": bbox[3]},
                "fuentes": ["OpenStreetMap", "Google Maps", "Nominatim"],
                "advertencia": (
                    "Cadastro imobiliário (lotes/IPTU) não disponível via API pública. "
                    "Dados de edificações provêm do OpenStreetMap."
                ),
                "edificios": [
                    {k: v for k, v in r.items() if k != "tags"}
                    for r in resultados
                ],
            },
            f,
            indent=2,
            ensure_ascii=False,
        )

    out_csv = f"vg_quadra_{tag}.csv"
    campos = ["osm_id", "osm_type", "lat", "lon", "direccion", "tipo", "pisos", "nombre", "dir_fuente"]
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(resultados)

    print("Resultados guardados en:")
    print(f"  {out_json}")
    print(f"  {out_csv}")


if __name__ == "__main__":
    main()
