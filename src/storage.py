import json
import os
from typing import List, Dict, Any
from .models import Vivienda

STORAGE_FILE = "data_viviendas.json"

class StorageSimulado:
    def __init__(self):
        self.db: List[Dict[str, Any]] = []
        self._load()

    def _load(self):
        if os.path.exists(STORAGE_FILE):
            try:
                with open(STORAGE_FILE, "r") as f:
                    self.db = json.load(f)
            except:
                self.db = []

    def guardar_vivienda(self, vivienda: Vivienda):
        """
        Simula un PUT en DynamoDB.
        """
        # Convertimos Pydantic a dict (incluyendo UUID a str)
        vivienda_dict = json.loads(vivienda.json())
        
        # Evitamos duplicados por vivienda_id
        self.db = [v for v in self.db if v["vivienda_id"] != vivienda_dict["vivienda_id"]]
        self.db.append(vivienda_dict)
        self._save()

    def _save(self):
        with open(STORAGE_FILE, "w") as f:
            json.dump(self.db, f, indent=4)

    def obtener_todas(self) -> List[Dict[str, Any]]:
        return self.db
