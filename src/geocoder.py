import httpx
import asyncio
from typing import Dict, Any

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

async def obtener_direccion_por_coordenadas(lat: float, lon: float) -> Dict[str, str]:
    """
    Hace Reverse Geocoding usando Nominatim (OpenStreetMap).
    Devuelve un diccionario con calle, altura y localidad.
    """
    if lat == 0 and lon == 0:
        return {"calle": "Desconocida", "altura": "0", "localidad": "Desconocida"}

    params = {
        "format": "json",
        "lat": lat,
        "lon": lon,
        "addressdetails": 1
    }
    
    # Nominatim exige un User-Agent identificatorio
    headers = {
        "User-Agent": "ScraperGIS/1.0 (+https://github.com/Meter0r0/Scrapitero)"
    }

    try:
        async with httpx.AsyncClient() as client:
            # Respetamos el límite de 1 request por segundo de Nominatim
            await asyncio.sleep(1.1) 
            response = await client.get(NOMINATIM_URL, params=params, headers=headers, timeout=10.0)
            
            if response.status_code == 200:
                data = response.json()
                address = data.get("address", {})
                
                # En CABA/AMBA, Nominatim suele usar 'suburb' o 'neighbourhood' en lugar de city/town
                localidad = address.get("city", address.get("town", address.get("village", address.get("suburb", address.get("neighbourhood", "Desconocida")))))
                
                return {
                    "calle": address.get("road", "Desconocida"),
                    "altura": address.get("house_number", "S/N"),
                    "localidad": localidad,
                    "partido": address.get("county", address.get("state_district", "Desconocido"))
                }

            return {"calle": "Error", "altura": "0", "localidad": "Error", "partido": "Error"}
    except Exception as e:
        print(f"Error en geocodificación: {e}")
        return {"calle": "Error", "altura": "0", "localidad": "Error", "partido": "Error"}
