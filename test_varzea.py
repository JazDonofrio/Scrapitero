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

def parse_nomenclatura(inscricao):
    """
    Intenta extraer manzana y parcela de la inscripción.
    Ejemplo de Várzea Grande: 401-13-1#100994 -> Setor 401, Cuadra(Manzana) 13, Lote(Parcela) 1
    """
    manzana, parcela = "", ""
    try:
        base = inscricao.split("#")[0]  # Obtener la parte antes del #
        partes = base.split("-")
        if len(partes) >= 3:
            manzana = partes[1]
            parcela = partes[2]
    except Exception:
        pass
    return manzana, parcela

def main():
    parser = argparse.ArgumentParser(description="Generar CSV de Várzea Grande con formato Ituzaingó")
    parser.add_argument(
        "--bbox",
        type=float,
        nargs=4,
        metavar=("LAT_MIN", "LON_MIN", "LAT_MAX", "LON_MAX"),
        required=True,
        help="Bounding box: lat_min lon_min lat_max lon_max",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="resultado_varzea.csv",
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
    
    # Encabezados exactos del CSV de Ituzaingó
    headers = [
        "manzana",
        "parcela",
        "nombre",
        "direccion",
        "lat",
        "lon",
        "n_partidas",
        "n_cocheras",
        "n_uf",
        "dir_fuente",
        "nomencla"
    ]
    
    with open(args.output, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        
        for lote in resultado.lots:
            # Atributos de SmartGIS
            direccion_bruta = extract_field(lote.attributes, "LOTE_ENDERECO")
            bairro = extract_field(lote.attributes, "LOTE_BAIRRO")
            
            # Limpieza de dirección
            direccion_limpia = direccion_bruta.strip().strip('"')
            if bairro:
                direccion_limpia = f"{direccion_limpia}, {bairro}"
                
            unidades = lote.attributes.get("unidadesCount", 1)
            manzana, parcela = parse_nomenclatura(lote.inscricao)
            
            # Mapeo al formato Ituzaingó
            writer.writerow({
                "manzana": manzana,
                "parcela": parcela,
                "nombre": lote.lot_id,  # Usamos el ID interno como nombre
                "direccion": direccion_limpia,
                "lat": f"{lote.lat:.6f}",
                "lon": f"{lote.lon:.6f}",
                "n_partidas": unidades,
                "n_cocheras": 0,  # No tenemos este dato en Várzea Grande
                "n_uf": unidades,
                "dir_fuente": "smartgis_varzea_grande",
                "nomencla": lote.inscricao
            })
            
    print("¡CSV generado exitosamente con el formato compatible!")

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
    main()
