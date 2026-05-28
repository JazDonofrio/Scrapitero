import httpx
from typing import Optional, Dict, Any, List, Tuple
from .models import Parcela

ARBA_WFS_URL = "https://geo.arba.gov.ar/geoserver/idera/wfs"

async def consultar_arba_por_coordenadas(lat: float, lon: float) -> Optional[Parcela]:
    """
    Consulta el servicio WFS de ARBA para obtener información catastral de un punto.
    """
    # Intentamos con 'geom' que es el estándar de IDERA
    # Usamos un BBOX pequeño alrededor del punto para ser más tolerantes
    delta = 0.0001 
    bbox = f"{lon-delta},{lat-delta},{lon+delta},{lat+delta}"
    
    params = {
        "service": "WFS",
        "version": "1.1.0",
        "request": "GetFeature",
        "typeName": "idera:Parcela",
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "bbox": bbox
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(ARBA_WFS_URL, params=params, timeout=15.0)
            
            if response.status_code != 200:
                print(f"Error ARBA (Status {response.status_code}): {response.text[:500]}")
                return None

            try:
                data = response.json()
            except Exception:
                print(f"ARBA no devolvió JSON válido. Respuesta completa:\n{response.text[:500]}")
                return None

            if not data.get("features") or len(data["features"]) == 0:
                print(f"No se encontraron parcelas en el BBOX {bbox}. ¿Seguro que es Provincia de BSAS?")
                return None

            # Extraemos la primera feature (la parcela que contiene el punto)
            feature = data["features"][0]
            props = feature.get("properties", {})

            # Mapeo actualizado de campos IDERA de ARBA
            return Parcela(
                partida=props.get("pda", "N/A"),
                tipo_propiedad=props.get("tpa", "N/A"),
                partido=props.get("cca", "N/A")[:3] if props.get("cca") else "N/A",
                coordenadas={"lat": lat, "lon": lon},
                nomenclatura=props.get("cca")
            )

    except httpx.HTTPStatusError as e:
        print(f"Error HTTP consultando ARBA: {e}")
        return None
    except Exception as e:
        print(f"Error inesperado consultando ARBA: {e}")
        return None

async def consultar_arba_por_nomenclatura(partido: str, circ: str, secc: str, manzana: str) -> List[Parcela]:
    """
    Busca todas las parcelas dentro de una manzana específica.
    """
    # El campo 'cca' tiene 42 caracteres. Formato aproximado:
    # 0-2: Partido (ej: 110 o 047)
    # 3-4: Circunscripción (ej: 03)
    # 5:   0 (o letra de circ)
    # 6:   Sección (ej: B)
    # 7-27: Chacra/Quinta/Fracción (21 caracteres)
    # 28-31: Manzana (4 dígitos, ej: 0020)
    #
    # Para no traer parcelas de la manzana 520, usamos '_' para la posición exacta
    partido_fmt = partido.zfill(3)
    circ_fmt = circ.zfill(2)
    manzana_fmt = manzana.zfill(4)
    # 21 guiones bajos para saltar Chacra/Quinta/Fracción
    cql = f"cca LIKE '{partido_fmt}{circ_fmt}_{secc}_____________________{manzana_fmt}%'"
    
    params = {
        "service": "WFS",
        "version": "1.1.0",
        "request": "GetFeature",
        "typeName": "idera:Parcela",
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "cql_filter": cql
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(ARBA_WFS_URL, params=params, timeout=20.0)
            if response.status_code != 200:
                print(f"Error ARBA Nomenclatura HTTP {response.status_code}: {response.text[:500]}")
                return []

            try:
                data = response.json()
            except Exception:
                print(f"ARBA no devolvió JSON válido. Respuesta completa:\n{response.text[:1000]}")
                return []

            features = data.get("features", [])
            
            parcelas = []
            for f in features:
                props = f.get("properties", {})
                geom = f.get("geometry", {})
                coords = {"lat": 0, "lon": 0}
                if geom and geom.get("type") in ["Polygon", "MultiPolygon"]:
                    try:
                        if geom["type"] == "MultiPolygon":
                            coords = {"lon": geom["coordinates"][0][0][0][0], "lat": geom["coordinates"][0][0][0][1]}
                        else:
                            coords = {"lon": geom["coordinates"][0][0][0], "lat": geom["coordinates"][0][0][1]}
                    except:
                        pass

                parcelas.append(Parcela(
                    partida=props.get("pda", "N/A"),
                    tipo_propiedad=props.get("tpa", "N/A"),
                    partido=props.get("cca", "N/A")[:3] if props.get("cca") else "N/A",
                    coordenadas=coords,
                    nomenclatura=props.get("cca")
                ))
            return parcelas
    except Exception as e:
        print(f"Error consultando por nomenclatura: {e}")
        return []


def _cql_manzana(partido: str, circ: str, secc: str, manzana: str) -> str:
    p = partido.zfill(3)
    c = circ.zfill(2)
    m = manzana.zfill(4)
    return f"cca LIKE '{p}{c}_{secc}_____________________{m}%'"


def _centroid(geom: dict) -> Tuple[float, float]:
    """Returns (lat, lon) centroid of a Polygon/MultiPolygon geometry."""
    try:
        if geom["type"] == "MultiPolygon":
            ring = geom["coordinates"][0][0]
        else:
            ring = geom["coordinates"][0]
        lons = [pt[0] for pt in ring]
        lats = [pt[1] for pt in ring]
        return sum(lats) / len(lats), sum(lons) / len(lons)
    except Exception:
        return 0.0, 0.0


def _line_midpoint(geom: dict) -> Tuple[float, float]:
    """Returns (lat, lon) midpoint of a LineString geometry."""
    try:
        coords = geom["coordinates"]
        mid = coords[len(coords) // 2]
        return mid[1], mid[0]
    except Exception:
        return 0.0, 0.0


async def consultar_lados_por_manzana(
    partido: str, circ: str, secc: str, manzana: str
) -> Dict[str, Dict]:
    """
    Returns {parcela_cca: {"altura": int, "lat": float, "lon": float}}
    using only FRENTE (front-facing) sides from Lado_Catastral.
    The coordinates are the midpoint of the front line — useful for geocoding.
    """
    params = {
        "service": "WFS",
        "version": "1.1.0",
        "request": "GetFeature",
        "typeName": "idera:Lado_Catastral",
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "cql_filter": _cql_manzana(partido, circ, secc, manzana),
    }
    result: Dict[str, Dict] = {}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(ARBA_WFS_URL, params=params, timeout=20.0)
            if r.status_code != 200:
                return result
            data = r.json()
            for f in data.get("features", []):
                props = f.get("properties", {})
                if props.get("tdl") != "FRENTE":
                    continue
                cca = props.get("cca", "")
                feo = props.get("feo")
                if not cca or feo is None:
                    continue
                lat, lon = _line_midpoint(f.get("geometry", {}))
                # Keep the first FRENTE for each parcela
                # feo is measured from the street origin; sign indicates direction.
                # Postal number = absolute value.
                altura = abs(int(feo))
                if cca not in result and altura > 0:
                    result[cca] = {"altura": altura, "lat": lat, "lon": lon}
    except Exception as e:
        print(f"Error consultando Lado_Catastral: {e}")
    return result


async def consultar_subparcelas_por_manzana(
    partido: str, circ: str, secc: str, manzana: str
) -> Dict[str, int]:
    """
    Returns {parcela_cca: unit_count} for parcelas with sub-units (PHs/apartments).
    Parcelas not present in the result have 1 unit.
    Subparcela CCA = parent parcela CCA (42 chars) + unit suffix.
    """
    params = {
        "service": "WFS",
        "version": "1.1.0",
        "request": "GetFeature",
        "typeName": "idera:Subparcela",
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "cql_filter": _cql_manzana(partido, circ, secc, manzana),
    }
    counts: Dict[str, int] = {}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(ARBA_WFS_URL, params=params, timeout=20.0)
            if r.status_code != 200:
                return counts
            data = r.json()
            for f in data.get("features", []):
                cca = f.get("properties", {}).get("cca", "")
                # Parent parcela CCA is the first 42 chars of subparcela CCA
                parent_cca = cca[:42]
                counts[parent_cca] = counts.get(parent_cca, 0) + 1
    except Exception as e:
        print(f"Error consultando Subparcela: {e}")
    return counts
