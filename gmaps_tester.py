import requests
import json
import os
import argparse

# Configura tu API Key de Google Maps aquí. 
# Es una buena práctica usar variables de entorno en producción.
API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "AIzaSyBcBveB2soQ221-i4iEFgb1-l9hh1GPkuQ")

def test_reverse_geocoding(lat, lng):
    """
    Convierte una coordenada en una dirección estructurada.
    Útil para verificar la dirección exacta de un polígono de parcela.
    """
    print(f"\n--- 📍 Probando Reverse Geocoding para {lat}, {lng} ---")
    url = f"https://maps.googleapis.com/maps/api/geocode/json?latlng={lat},{lng}&key={API_KEY}"
    
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        if data['status'] == 'OK':
            print(f"Resultados encontrados: {len(data['results'])}")
            
            # Analizamos el primer resultado (el más preciso)
            best_result = data['results'][0]
            print("Mejor coincidencia:")
            print(f"  Dirección formateada: {best_result.get('formatted_address')}")
            print(f"  Place ID: {best_result.get('place_id')}")
            print(f"  Nivel de precisión (Location Type): {best_result.get('geometry', {}).get('location_type')}")
            print("  (Nota: 'ROOFTOP' significa que apuntó exactamente al edificio/parcela)")
            return best_result
        else:
            print(f"Sin resultados o error: {data['status']}")
            if 'error_message' in data: print(data['error_message'])
    else:
        print(f"Error HTTP: {response.status_code}")
    return None


def test_nearby_places(lat, lng, radius=30):
    """
    Busca lugares/comercios alrededor de una coordenada dentro de un radio en metros.
    30 a 50 metros suele ser ideal para identificar qué hay DENTRO o PEGADO a una parcela.
    """
    print(f"\n--- 🏪 Probando Places API (Nearby Search) en radio de {radius}m ---")
    url = f"https://maps.googleapis.com/maps/api/place/nearbysearch/json?location={lat},{lng}&radius={radius}&key={API_KEY}"
    
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        if data['status'] in ['OK', 'ZERO_RESULTS']:
            resultados = data.get('results', [])
            print(f"Comercios/Lugares encontrados en {radius}m: {len(resultados)}")
            
            for i, place in enumerate(resultados[:5]): # Limitamos a mostrar 5 por consola
                print(f"\n  Lugar {i+1}:")
                print(f"    Nombre: {place.get('name')}")
                print(f"    Tipos/Categorías: {', '.join(place.get('types', []))}")
                print(f"    Place ID: {place.get('place_id')}")
                print(f"    Vicinity (Dirección corta): {place.get('vicinity')}")
                print(f"    Estado: {place.get('business_status', 'No especificado')}")
                
            if len(resultados) > 5:
                print(f"\n  ... y {len(resultados) - 5} lugares más ocultos en consola.")
                
            return resultados
        else:
            print(f"Error en la respuesta: {data['status']}")
            if 'error_message' in data: print(data['error_message'])
    else:
        print(f"Error HTTP: {response.status_code}")
    return []


def test_place_details(place_id):
    """
    Obtiene información enriquecida de un comercio en particular usando su Place ID.
    Para ahorrar costos, SIEMPRE pide solo los campos (fields) que necesites.
    """
    print(f"\n--- 🔍 Probando Place Details para Place ID: {place_id} ---")
    
    # Especificar fields reduce drásticamente el costo de la API
    fields = "name,formatted_address,formatted_phone_number,website,business_status,types"
    url = f"https://maps.googleapis.com/maps/api/place/details/json?place_id={place_id}&fields={fields}&key={API_KEY}"
    
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        if data['status'] == 'OK':
            result = data['result']
            print(f"  Nombre: {result.get('name')}")
            print(f"  Dirección Completa: {result.get('formatted_address')}")
            print(f"  Teléfono: {result.get('formatted_phone_number', 'No disponible')}")
            print(f"  Sitio Web: {result.get('website', 'No disponible')}")
            print(f"  Estado Comercial: {result.get('business_status')}")
            print(f"  Categorías: {', '.join(result.get('types', []))}")
        else:
            print(f"Error o sin resultados: {data['status']}")
    else:
        print(f"Error HTTP: {response.status_code}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test de Google Maps APIs para una coordenada.")
    parser.add_argument("lat", type=float, help="Latitud de la coordenada (ej: -34.469032)")
    parser.add_argument("lng", type=float, help="Longitud de la coordenada (ej: -58.508544)")
    args = parser.parse_args()
    
    LAT = args.lat
    LNG = args.lng
    
    print(f"🚀 INICIANDO TEST DE GOOGLE MAPS API PARA {LAT}, {LNG}")
    print("====================================")
    
    # 1. ¿Qué dirección exacta es esta coordenada?
    geo_result = test_reverse_geocoding(LAT, LNG)
    
    # 2. ¿Qué comercios hay en un radio pequeño (30 metros simula el tamaño de un lote)?
    lugares_encontrados = test_nearby_places(LAT, LNG, radius=30)
    
    # 3. Si encontramos comercios, consultamos el detalle enriquecido del primero
    if lugares_encontrados:
        primer_lugar_id = lugares_encontrados[0].get('place_id')
        test_place_details(primer_lugar_id)
    else:
        print("\nNo se encontraron lugares para probar los detalles.")
        
    print("\n====================================")
    print("📊 ANÁLISIS FINAL DE LA COORDENADA")
    print("====================================")
    
    # A) Determinar si hay edificio
    es_edificio = False
    if geo_result:
        location_type = geo_result.get('geometry', {}).get('location_type')
        if location_type == 'ROOFTOP':
            es_edificio = True
            
    print(f"🏢 ¿Hay edificio/parcela exacta mapeada?: {'SÍ (Ubicación ROOFTOP confirmada)' if es_edificio else 'NO SEGURO (Ubicación aproximada)'}")
    
    # B) Determinar si es un local comercial
    # Tipos que consideramos genéricos y NO comerciales per se
    tipos_ignorados = ['locality', 'political', 'route', 'administrative_area_level_1', 
                       'administrative_area_level_2', 'country', 'sublocality', 
                       'sublocality_level_1', 'neighborhood', 'postal_code', 'street_address', 'premise']
                       
    comercios_reales = []
    for lugar in lugares_encontrados:
        tipos = lugar.get('types', [])
        # Si tiene al menos un tipo que NO está en los ignorados, asumimos que es un POI/Local
        if any(t not in tipos_ignorados for t in tipos):
            comercios_reales.append(lugar)
            
    es_comercial = len(comercios_reales) > 0
    
    if es_comercial:
        print(f"🏪 ¿Es un local comercial?: SÍ ({len(comercios_reales)} comercios detectados)")
        print("   Comercios:")
        for c in comercios_reales[:3]:
            print(f"   - {c.get('name')} ({', '.join(c.get('types', []))})")
    else:
        print("🏪 ¿Es un local comercial?: NO (Es residencial o no hay comercios registrados en Google Maps)")
