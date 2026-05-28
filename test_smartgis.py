import json
import argparse
from scrapitero.agents.smartgis_varzea import fetch, SmartGISFetchInput

def main():
    parser = argparse.ArgumentParser(description="Prueba del agente SmartGIS Várzea Grande")
    parser.add_argument(
        "--bbox",
        type=float,
        nargs=4,
        metavar=("LAT_MIN", "LON_MIN", "LAT_MAX", "LON_MAX"),
        default=[-15.65, -56.14, -15.64, -56.13],
        help="Bounding box: lat_min lon_min lat_max lon_max",
    )
    args = parser.parse_args()
    
    print("Iniciando prueba con SmartGIS Várzea Grande...\n")
    
    # Bounding box provisto por la terminal (o el default)
    input_data = SmartGISFetchInput(bbox=tuple(args.bbox))
    
    print(f"Buscando parcelas en BBOX: {input_data.bbox}")
    resultado = fetch(input_data)
    
    print(f"\nEstado de la consulta: {'OK' if resultado.ok else 'Error'}")
    
    if not resultado.ok:
        print(f"Detalle del error: {resultado.error}")
        return

    print(f"Cantidad de lotes encontrados: {resultado.count}\n")
    
    # Mostramos los detalles de cada lote
    for lote in resultado.lots:
        print("-" * 40)
        print(f"ID del lote: {lote.lot_id}")
        print(f"Inscripción (Código Catastral): {lote.inscricao}")
        print(f"Centroide (Lat, Lon): {lote.lat:.6f}, {lote.lon:.6f}")
        
        # Opcional: Imprimir un resumen de la geometría
        tipo_geometria = lote.geojson.get("type", "Desconocido")
        print(f"Tipo de geometría: {tipo_geometria}")
        
        # Opcional: Mostrar algunos atributos adicionales que devuelve la API
        if lote.attributes:
            print("Atributos adicionales disponibles:", list(lote.attributes.keys())[:5], "...")
        print("-" * 40)
        
if __name__ == "__main__":
    # Configurar logging básico para ver los mensajes del agente
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
    main()
