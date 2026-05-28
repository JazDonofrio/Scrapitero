from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from uuid import UUID

class Parcela(BaseModel):
    partida: str
    tipo_propiedad: str
    provincia: str = "Buenos Aires"
    partido: str
    nomenclatura: Optional[str] = None
    coordenadas: Dict[str, float]  # {"lat": ..., "lon": ...}

class UnidadFuncional(BaseModel):
    piso: Optional[str] = None
    depto: Optional[str] = None
    vivienda_id: UUID

class Vivienda(BaseModel):
    vivienda_id: UUID
    direccion_completa: str
    provincia: str
    partido: str
    calle: str
    altura: str
    piso: Optional[str] = None
    depto: Optional[str] = None
    coordenadas: Dict[str, float]
    fuentes: List[str] = []
    confiabilidad: float = 1.0  # 0.0 a 1.0
    metadata: Dict[str, Any] = {}
