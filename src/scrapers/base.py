from abc import ABC, abstractmethod
from typing import List
from ..models import SuministroElectrico


class ScraperSuministros(ABC):
    nombre: str = "Base"

    @abstractmethod
    async def buscar_por_direccion(
        self, calle: str, altura: str, localidad: str
    ) -> List[SuministroElectrico]:
        """Devuelve los suministros encontrados en esa dirección."""
        ...
