import asyncio
import random
from typing import List
from .models import UnidadFuncional
from .utils import generar_vivienda_uuid

class AgenteSuministros:
    """
    Agente encargado de interactuar con portales de servicios (AySA, Edenor, etc.)
    para descubrir unidades funcionales reales.
    """
    
    def __init__(self, portal_nombre: str = "AySA Mock"):
        self.portal_nombre = portal_nombre

    async def descubrir_unidades(self, direccion: str, partido: str) -> List[UnidadFuncional]:
        """
        Simula una consulta a un portal de servicios que devuelve medidores/unidades.
        """
        # Simulamos latencia de red casi nula para pruebas masivas
        await asyncio.sleep(0)

        # Como el usuario pidió armar la tabla SOLO con lo que informa Catastro
        # y la capa WFS idera:Parcela no detalla las unidades funcionales internas,
        # devolvemos estrictamente 1 unidad base por cada parcela encontrada.
        unidades = []
        
        # Casa / Parcela base única
        v_id = generar_vivienda_uuid("Buenos Aires", partido, direccion, "", "", "")
        unidades.append(UnidadFuncional(piso="-", depto="-", vivienda_id=v_id))
        
        return unidades

