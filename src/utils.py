import uuid

# Namespace propio para el proyecto AI Mapping
NAMESPACE_SCRAPITERO = uuid.uuid5(uuid.NAMESPACE_DNS, "scrapitero.com.ar")

def generar_vivienda_uuid(provincia: str, partido: str, calle: str, altura: str, piso: str = "", depto: str = "") -> uuid.UUID:
    """
    Genera un UUID v5 determinístico basado en la ubicación.
    """
    # Normalización básica de la cadena
    componentes = [
        provincia.strip().lower(),
        partido.strip().lower(),
        calle.strip().lower(),
        altura.strip().lower(),
        piso.strip().lower() if piso else "",
        depto.strip().lower() if depto else ""
    ]
    seed_string = "-".join(componentes)
    return uuid.uuid5(NAMESPACE_SCRAPITERO, seed_string)
