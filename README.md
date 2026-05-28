# Scrapitero - Sistema Multi-Agente de Relevamiento Domiciliario

Este es un prototipo inicial para la identificación de unidades funcionales geolocalizadas en Argentina.

## Estructura del Proyecto

- `src/cadastre.py`: Cliente WFS para ARBA.
- `src/supplies.py`: Agente que simula la consulta a portales de servicios (AySA).
- `src/notifier.py`: Notificador de Telegram para discrepancias (HITL).
- `src/storage.py`: Simulación de persistencia (DynamoDB Local).
- `src/utils.py`: Generador de UUIDs determinísticos.
- `src/models.py`: Esquemas de datos con Pydantic.

## Configuración

1. Crear un entorno virtual:
   ```bash
   python -m venv venv
   source venv/bin/activate
   ```

2. Instalar dependencias:
   ```bash
   pip install -r requirements.txt
   ```

3. Configurar `.env`:
   Copia `.env.example` a `.env` y completa tus credenciales de Telegram.

## Ejecución

Para correr el orquestador con un ejemplo:
```bash
python -m src.orchestrator
```

## Lógica de Aprendizaje
Se han incluido comentarios marcados con `APRENDIZAJE` o `LOGICA DE APRENDIZAJE` en los archivos `cadastre.py`, `supplies.py` y `orchestrator.py` indicando dónde integrar los modelos de entrenamiento para normalización de direcciones y clasificación de discrepancias.
