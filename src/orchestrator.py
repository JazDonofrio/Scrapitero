import asyncio
from src.cadastre import consultar_arba_por_coordenadas
from src.supplies import AgenteSuministros
from src.notifier import NotificadorTelegram
from src.storage import StorageSimulado
from src.models import Vivienda
from src.utils import generar_vivienda_uuid

async def procesar_domicilio(lat: float, lon: float, calle: str, altura: str):
    print(f"--- Iniciando procesamiento para {calle} {altura} ---")
    
    # 1. Módulo de Catastro
    parcela = await consultar_arba_por_coordenadas(lat, lon)
    
    if not parcela:
        print("No se encontró información catastral en ARBA.")
        return

    print(f"Catastro encontrado: Partida {parcela.partida}, Tipo: {parcela.tipo_propiedad}")

    # 2. Agente de Suministros
    agente = AgenteSuministros()
    unidades_descubiertas = await agente.descubrir_unidades(f"{calle} {altura}", parcela.partido)

    # 3. Lógica de Discrepancia y HITL
    # APRENDIZAJE: Aquí es el punto crítico para implementar lógica de RL (Reinforcement Learning) 
    # o un clasificador que decida si la discrepancia es un error de catastro o un 'outlier' 
    # legítimo (ej. una mansión con muchos medidores vs un edificio no declarado).
    # Se podría alimentar un modelo con el feedback del HITL (Human In The Loop) de Telegram.
    if parcela.tipo_propiedad == "UNIFAMILIAR" and len(unidades_descubiertas) > 1:
        notificador = NotificadorTelegram()
        await notificador.enviar_alerta_discrepancia(
            partida=parcela.partida,
            catastro_count=1,
            suministros_count=len(unidades_descubiertas),
            direccion=f"{calle} {altura}, {parcela.partido}"
        )

    # 4. Almacenamiento
    storage = StorageSimulado()
    
    for unidad in unidades_descubiertas:
        vivienda = Vivienda(
            vivienda_id=unidad.vivienda_id,
            direccion_completa=f"{calle} {altura} {unidad.piso} {unidad.depto}",
            provincia=parcela.provincia,
            partido=parcela.partido,
            calle=calle,
            altura=altura,
            piso=unidad.piso,
            depto=unidad.depto,
            coordenadas=parcela.coordenadas,
            fuentes=["ARBA WFS", "AySA Mock"],
            confiabilidad=0.8,
            metadata={"partida": parcela.partida}
        )
        storage.guardar_vivienda(vivienda)
        print(f"Guardada vivienda: {vivienda.vivienda_id} ({vivienda.direccion_completa})")

async def procesar_manzana(partido: str, circ: str, secc: str, manzana: str):
    from src.cadastre import consultar_arba_por_nomenclatura
    from src.geocoder import obtener_direccion_por_coordenadas
    
    print(f"\n--- Procesando Manzana: {partido}-{circ}-{secc}-{manzana} ---")
    parcelas = await consultar_arba_por_nomenclatura(partido, circ, secc, manzana)
    
    if not parcelas:
        print("\n❌ No se encontraron parcelas para esa nomenclatura.")
        print("💡 Posibles causas:")
        print("   - Los datos ingresados no existen en Catastro ARBA.")
        print("   - La Sección contiene una letra diferente o números (ej: 'I' vs '1').")
        print("   - El formato de Manzana tiene una letra adjunta (ej: 71A).")
        return

    print(f"Se encontraron {len(parcelas)} parcelas. Obteniendo direcciones físicas (Geocoding)...")
    
    total_unidades = 0
    storage = StorageSimulado()
    
    # Lista para guardar los datos de la tabla
    tabla_resultados = []
    
    # Caché de coordenadas para no consultar Nominatim de más
    geo_cache = {}
    
    for parcela in parcelas:
        lat = parcela.coordenadas.get("lat", 0)
        lon = parcela.coordenadas.get("lon", 0)
        coord_key = f"{lat},{lon}"
        
        # Reverse Geocoding
        if coord_key not in geo_cache:
            if lat != 0 and lon != 0:
                print(".", end="", flush=True) # Indicador de progreso
                dir_fisica = await obtener_direccion_por_coordenadas(lat, lon)
            else:
                dir_fisica = {"calle": "S/C", "altura": "S/N", "localidad": "S/D"}
            geo_cache[coord_key] = dir_fisica
        else:
            dir_fisica = geo_cache[coord_key]

        agente = AgenteSuministros()
        unidades = await agente.descubrir_unidades(parcela.nomenclatura, parcela.partido)
        total_unidades += len(unidades)
        
        for u in unidades:
            direccion_completa = f"{dir_fisica['calle']} {dir_fisica['altura']} {u.piso} {u.depto}".strip()
            v = Vivienda(
                vivienda_id=u.vivienda_id,
                direccion_completa=direccion_completa,
                provincia=parcela.provincia,
                partido=dir_fisica.get("partido", parcela.partido),
                calle=dir_fisica["calle"], 
                altura=dir_fisica["altura"], 
                piso=u.piso, 
                depto=u.depto,
                coordenadas=parcela.coordenadas,
                metadata={"partida": parcela.partida, "nomenclatura": parcela.nomenclatura, "localidad": dir_fisica["localidad"]}
            )
            storage.guardar_vivienda(v)
            
            # Guardamos para la tabla
            tabla_resultados.append({
                "Partida": parcela.partida,
                "Calle": dir_fisica["calle"],
                "Nro": dir_fisica["altura"],
                "Localidad": dir_fisica["localidad"],
                "Piso": u.piso or "-",
                "Depto": u.depto or "-"
            })

    print("\n\n--- Procesamiento de Manzana Finalizado ---")
    print(f"📊 Total de Parcelas analizadas: {len(parcelas)}")
    print(f"🏢 Total de Unidades funcionales descubiertas: {total_unidades}\n")
    
    # Imprimir Tabla
    print(f"{'PARTIDA':<10} | {'CALLE':<25} | {'NRO':<5} | {'PISO':<4} | {'DEPTO':<5} | {'LOCALIDAD'}")
    print("-" * 80)
    for fila in tabla_resultados:
        # Truncar calle si es muy larga
        calle_corta = (fila['Calle'][:22] + '...') if len(fila['Calle']) > 25 else fila['Calle']
        print(f"{fila['Partida']:<10} | {calle_corta:<25} | {fila['Nro']:<5} | {fila['Piso']:<4} | {fila['Depto']:<5} | {fila['Localidad']}")
    print("-" * 80)


if __name__ == "__main__":
    import sys

    print("\n--- Scrapitero: Sistema de Relevamiento ---")
    print("Búsqueda interactiva por Nomenclatura (Manzana)")
    
    try:
        partido = input("Partido (ej: 110): ") or "110"
        circ = input("Circunscripción (ej: 3): ") or "3"
        secc = input("Sección (ej: B): ") or "B"
        manzana = input("Manzana (ej: 20): ") or "20"
        
        asyncio.run(procesar_manzana(partido, circ, secc, manzana))
    except KeyboardInterrupt:
        print("\nProceso cancelado por el usuario.")
    except Exception as e:
        print(f"Error en la ejecución: {e}")

