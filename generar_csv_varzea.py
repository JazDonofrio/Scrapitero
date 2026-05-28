import csv
import argparse
from scrapitero.agents.smartgis_varzea import fetch, SmartGISFetchInput

def extract_field(attributes, field_name):
    """Extrae un campo específico de la estructura dinámica fieldItemGroups."""
    groups = attributes.get("fieldItemGroups", [])
    for group in groups:
        for item in group.get("fieldItems", []):
            if item.get("name") == field_name or item.get("label") == field_name:
                return item.get("value", "")
    return ""

def main():
    parser = argparse.ArgumentParser(description="Exportar parcelas de Várzea Grande a CSV")
    parser.add_argument(
        "--bbox",
        type=float,
        nargs=4,
        metavar=("LAT_MIN", "LON_MIN", "LAT_MAX", "LON_MAX"),
        default=[-15.640, -56.133, -15.639, -56.132], # Por defecto una manzana
        help="Bounding box: lat_min lon_min lat_max lon_max",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="varzea_grande_parcelas.csv",
        help="Nombre del archivo CSV de salida",
    )
    args = parser.parse_args()
    
    print(f"Iniciando escaneo de SmartGIS Várzea Grande en BBOX: {args.bbox}...\n")
    
    input_data = SmartGISFetchInput(bbox=tuple(args.bbox))
    resultado = fetch(input_data)
    
    if not resultado.ok:
        print(f"Error en la consulta: {resultado.error}")
        return

    print(f"\nGenerando CSV con {resultado.count} lotes en {args.output}...")
    
    # Encabezados del CSV (adaptados a Várzea Grande)
    headers = [
        "id_lote",
        "inscricao",
        "codigo_imovel",
        "direccion",
        "bairro",
        "lat",
        "lon",
        "area_terreno_m2",
        "area_construida_m2",
        "muro",
        "dir_fuente"
    ]
    
    with open(args.output, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        
        for lote in resultado.lots:
            # Extraer campos de fieldItems
            direccion = extract_field(lote.attributes, "LOTE_ENDERECO")
            bairro = extract_field(lote.attributes, "LOTE_BAIRRO")
            area_terr = extract_field(lote.attributes, "LOTE_AREA_LOTE")
            area_const = extract_field(lote.attributes, "LOTE_AREA_CONSTRUIDA")
            codigo_imv = extract_field(lote.attributes, "CODIGO_IMOVEL_AGRUPADO")
            muro = extract_field(lote.attributes, "LOTE_MURO")
            
            writer.writerow({
                "id_lote": lote.lot_id,
                "inscricao": lote.inscricao,
                "codigo_imovel": codigo_imv,
                "direccion": direccion,
                "bairro": bairro,
                "lat": f"{lote.lat:.6f}",
                "lon": f"{lote.lon:.6f}",
                "area_terreno_m2": area_terr,
                "area_construida_m2": area_const,
                "muro": muro,
                "dir_fuente": "smartgis_varzea_grande"
            })
            
    print("¡CSV generado exitosamente!")

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
    main()
