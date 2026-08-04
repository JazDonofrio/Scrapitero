"""Modelos SQLAlchemy para AI Mapping."""

import uuid
from datetime import datetime, date
from typing import Optional

from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ── Regiones ──────────────────────────────────────────────────────────────────

class Region(Base):
    """Catálogo de regiones geográficas soportadas."""
    __tablename__ = "regions"

    region_id: Mapped[str] = mapped_column(String(50), primary_key=True)  # "vg-mt-br"
    name: Mapped[str] = mapped_column(String(200))
    country_code: Mapped[str] = mapped_column(String(3))   # "BRA"
    state_code: Mapped[Optional[str]] = mapped_column(String(10))
    municipio_codigo: Mapped[Optional[str]] = mapped_column(String(20))  # IBGE/INDEC
    bbox_wkt: Mapped[Optional[str]] = mapped_column(Text)  # POLYGON WGS84
    zone_geojson: Mapped[Optional[str]] = mapped_column(Text)  # GeoJSON subido por el usuario
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ── Surveys (snapshots de relevamiento) ───────────────────────────────────────

class Survey(Base):
    """Cada corrida del orquestador genera un survey versionado."""
    __tablename__ = "surveys"

    survey_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                                  default=uuid.uuid4)
    region_id: Mapped[str] = mapped_column(String(50), ForeignKey("regions.region_id"))
    status: Mapped[str] = mapped_column(String(20), default="running")
    # "running" | "completed" | "partial" | "failed"
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    steps_count: Mapped[int] = mapped_column(Integer, default=0)
    llm_tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    llm_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    # Visible en la vista CLIENTE (raíz "/"); el operador lo controla con un tilde
    # en su lista (migración 014).
    visible_cliente: Mapped[bool] = mapped_column(Boolean, default=True,
                                                  server_default="true")
    # Los relevamientos NUNCA se borran: el anterior siempre queda disponible como
    # término de comparación. Archivado = oculto de la lista, comparable (migración 017).
    archivado: Mapped[bool] = mapped_column(Boolean, default=False,
                                            server_default="false")

    region: Mapped["Region"] = relationship("Region")
    parcelas: Mapped[list["Parcela"]] = relationship("Parcela", back_populates="survey")
    log_entries: Mapped[list["OrchestratorLog"]] = relationship(
        "OrchestratorLog", back_populates="survey"
    )


# ── Setores Censitários ────────────────────────────────────────────────────────

class SetorCensitario(Base):
    """Geometría y estadísticas censales IBGE por setor."""
    __tablename__ = "setores_censitarios"

    setor_id: Mapped[str] = mapped_column(String(20), primary_key=True)  # código IBGE
    region_id: Mapped[str] = mapped_column(String(50), ForeignKey("regions.region_id"))
    geometry: Mapped[object] = mapped_column(Geometry("MULTIPOLYGON", srid=4326))
    pop_total: Mapped[Optional[int]] = mapped_column(Integer)
    domicilios_total: Mapped[Optional[int]] = mapped_column(Integer)
    domicilios_casas: Mapped[Optional[int]] = mapped_column(Integer)
    domicilios_aptos: Mapped[Optional[int]] = mapped_column(Integer)
    area_km2: Mapped[Optional[float]] = mapped_column(Float)
    source_file: Mapped[Optional[str]] = mapped_column(String(200))
    loaded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ── Parcelas (output principal) ────────────────────────────────────────────────

class Parcela(Base):
    """Una fila por parcela catastral — output atómico del sistema."""
    __tablename__ = "parcelas"

    parcela_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    survey_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True),
                                                  ForeignKey("surveys.survey_id"))
    region_id: Mapped[str] = mapped_column(String(50), ForeignKey("regions.region_id"))
    setor_censitario_id: Mapped[Optional[str]] = mapped_column(
        String(20), ForeignKey("setores_censitarios.setor_id")
    )

    # Geometría
    geometry: Mapped[Optional[object]] = mapped_column(Geometry("POLYGON", srid=4326))
    centroid_lat: Mapped[Optional[float]] = mapped_column(Float)
    centroid_lng: Mapped[Optional[float]] = mapped_column(Float)
    area_m2_terreno: Mapped[Optional[float]] = mapped_column(Float)
    area_m2_construida: Mapped[Optional[float]] = mapped_column(Float)

    # Dirección
    calle: Mapped[Optional[str]] = mapped_column(String(200))
    numero: Mapped[Optional[str]] = mapped_column(String(20))
    complemento: Mapped[Optional[str]] = mapped_column(String(100))
    barrio: Mapped[Optional[str]] = mapped_column(String(100))
    localidad: Mapped[Optional[str]] = mapped_column(String(100))
    municipio: Mapped[Optional[str]] = mapped_column(String(100))
    estado_provincia: Mapped[Optional[str]] = mapped_column(String(100))
    pais: Mapped[Optional[str]] = mapped_column(String(50))
    codigo_postal: Mapped[Optional[str]] = mapped_column(String(20))
    direccion_source: Mapped[Optional[str]] = mapped_column(String(30))
    # "catastro" | "osm" | "nominatim" | "google_geocode"
    direccion_confidence: Mapped[Optional[float]] = mapped_column(Float)
    codigo_logradouro: Mapped[Optional[str]] = mapped_column(String(20))  # migración 016 (BCI)

    # Habitantes (siempre estimados)
    habitantes_estimados: Mapped[Optional[float]] = mapped_column(Float)
    habitantes_low: Mapped[Optional[float]] = mapped_column(Float)
    habitantes_high: Mapped[Optional[float]] = mapped_column(Float)
    habitantes_metodo: Mapped[Optional[str]] = mapped_column(String(50))
    habitantes_confidence: Mapped[Optional[float]] = mapped_column(Float)

    # Tipología y métricas auxiliares
    uso_principal: Mapped[Optional[str]] = mapped_column(String(30))
    # "residencial" | "comercial" | "mixto" | "industrial" | "vacante"
    footprints_count: Mapped[int] = mapped_column(Integer, default=0)
    pisos_estimados_max: Mapped[Optional[int]] = mapped_column(Integer)
    unidades_funcionales_estimadas: Mapped[Optional[int]] = mapped_column(Integer)
    uf_vivienda: Mapped[Optional[int]] = mapped_column(Integer)   # migración 004
    uf_comercio: Mapped[Optional[int]] = mapped_column(Integer)   # migración 004
    uf_fuente: Mapped[Optional[str]] = mapped_column(String(20))  # bci/osm/proxy/uso (migración 008)
    uso_fuente: Mapped[Optional[str]] = mapped_column(String(20))  # bci/cpua/sigsa/rentas/clasificador (migración 009)

    # Catastro (migración 003)
    cca_code: Mapped[Optional[str]] = mapped_column(String(100))
    nomenclatura_catastral: Mapped[Optional[str]] = mapped_column(String(100))
    partida_inmobiliaria: Mapped[Optional[str]] = mapped_column(String(100))

    # Valuación fiscal + propietario, del BCI (migración 012)
    valor_venal_terreno: Mapped[Optional[float]] = mapped_column(Float)
    valor_venal_construccion: Mapped[Optional[float]] = mapped_column(Float)
    valor_venal_total: Mapped[Optional[float]] = mapped_column(Float)
    aliquota: Mapped[Optional[float]] = mapped_column(Float)
    anio_construccion: Mapped[Optional[int]] = mapped_column(Integer)
    propietario_nombre: Mapped[Optional[str]] = mapped_column(String(200))      # PII
    propietario_documento: Mapped[Optional[str]] = mapped_column(String(30))    # PII (CPF/CNPJ)
    contribuyente_secundario: Mapped[Optional[str]] = mapped_column(String(200))  # PII

    # Agrupación en establecimiento (migración 013): parcelas con el mismo
    # establecimiento_id son partes de una única entidad (fábrica/colegio/iglesia…)
    establecimiento_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establecimientos.establecimiento_id"))

    # Auditoría
    fuente_parcela: Mapped[Optional[str]] = mapped_column(String(30))
    fecha_relevamiento: Mapped[date] = mapped_column(Date, server_default=func.current_date())
    validado_manual: Mapped[bool] = mapped_column(Boolean, default=False)

    survey: Mapped["Survey"] = relationship("Survey", back_populates="parcelas")
    edificios: Mapped[list["Edificio"]] = relationship("Edificio", back_populates="parcela")


# ── Comentarios del cliente (sugerencias/correcciones sobre el mapa) ──────────

class ComentarioCliente(Base):
    """Comentario georreferenciado que el cliente deja en un punto del mapa de un
    relevamiento (sugerencia/corrección). Migración 015. Si el punto cae dentro de
    una parcela del survey queda vinculado (`parcela_id`); el operador lo gestiona
    con `estado` (pendiente → resuelto)."""
    __tablename__ = "comentarios_cliente"

    comentario_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                                      default=uuid.uuid4)
    survey_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True),
                                                  ForeignKey("surveys.survey_id"))
    parcela_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True),
                                                             ForeignKey("parcelas.parcela_id"))
    geometry: Mapped[object] = mapped_column(Geometry("POINT", srid=4326))
    texto: Mapped[str] = mapped_column(Text)
    autor_rol: Mapped[Optional[str]] = mapped_column(String(20))   # cliente/operador; NULL=sin auth
    estado: Mapped[str] = mapped_column(String(20), default="pendiente",
                                        server_default="pendiente")  # pendiente | resuelto
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    resuelto_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


# ── Edificios (tabla interna) ──────────────────────────────────────────────────

class Edificio(Base):
    """Footprints de edificios — tabla interna, no es output."""
    __tablename__ = "edificios"

    edificio_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                                    default=uuid.uuid4)
    survey_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True),
                                                  ForeignKey("surveys.survey_id"))
    parcela_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True),
                                                             ForeignKey("parcelas.parcela_id"))
    setor_censitario_id: Mapped[Optional[str]] = mapped_column(
        String(20), ForeignKey("setores_censitarios.setor_id")
    )
    footprint: Mapped[Optional[object]] = mapped_column(Geometry("POLYGON", srid=4326))
    centroid: Mapped[Optional[object]] = mapped_column(Geometry("POINT", srid=4326))
    area_m2: Mapped[Optional[float]] = mapped_column(Float)
    pisos_estimados: Mapped[Optional[int]] = mapped_column(Integer)  # building:levels (OSM)
    tipo_osm: Mapped[Optional[str]] = mapped_column(String(50))      # valor de building=* (migración 007)
    unidades_osm: Mapped[Optional[int]] = mapped_column(Integer)     # building:flats/addr:units (migración 007)
    source: Mapped[Optional[str]] = mapped_column(String(30))
    # "ms_global" | "google_open" | "osm"
    external_id: Mapped[Optional[str]] = mapped_column(String(100))

    parcela: Mapped[Optional["Parcela"]] = relationship("Parcela", back_populates="edificios")


# ── Comercios (POIs Google Places — tabla interna) ─────────────────────────────

class Comercio(Base):
    """Comercios de Google Places — POIs vinculados a parcela. Tabla interna.

    Cada comercio cuyo punto cae dentro de una parcela suma +1 a `uf_comercio`
    (sin agrupar). Insumo de GooglePlacesFetcher. Migración 010.
    """
    __tablename__ = "comercios"
    __table_args__ = (
        UniqueConstraint("region_id", "place_id", name="uq_comercios_region_place"),
    )

    comercio_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                                    default=uuid.uuid4)
    survey_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True),
                                                  ForeignKey("surveys.survey_id"))
    region_id: Mapped[str] = mapped_column(String(50), ForeignKey("regions.region_id"))
    parcela_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parcelas.parcela_id")
    )
    place_id: Mapped[str] = mapped_column(String(120))   # id de Google (dedup)
    nombre: Mapped[Optional[str]] = mapped_column(String(250))
    rubro: Mapped[Optional[str]] = mapped_column(String(80))   # primaryType
    tipos: Mapped[Optional[str]] = mapped_column(Text)         # csv de types
    business_status: Mapped[Optional[str]] = mapped_column(String(40))
    location: Mapped[Optional[object]] = mapped_column(Geometry("POINT", srid=4326))
    source: Mapped[Optional[str]] = mapped_column(String(30), default="google_places")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ── Unidades Funcionales (tabla interna) ───────────────────────────────────────

class UnidadFuncional(Base):
    """Estimaciones de unidades por edificio — tabla interna."""
    __tablename__ = "unidades_funcionales"

    unidad_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                                  default=uuid.uuid4)
    edificio_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True),
                                                    ForeignKey("edificios.edificio_id"))
    piso: Mapped[Optional[int]] = mapped_column(Integer)
    depto: Mapped[Optional[str]] = mapped_column(String(10))
    tipo: Mapped[Optional[str]] = mapped_column(String(30))
    # "casa" | "depto" | "local_comercial" | "garage"


# ── Manzanas: estimación dasimétrica de habitantes (tabla aparte) ──────────────

class ManzanaHabitantes(Base):
    """Estimación SECUNDARIA de habitantes por manzana catastral (dasimétrica).

    Reparte `setores_censitarios.pop_total` entre las parcelas por un peso de
    ocupación y agrega por manzana catastral. Es menos exacta que la UF del
    relevamiento principal → se guarda y muestra aparte, con su fecha. Migración 011.
    """
    __tablename__ = "manzanas_habitantes"
    __table_args__ = (
        UniqueConstraint("survey_id", "manzana_codigo",
                         name="uq_manzana_hab_survey_codigo"),
    )

    manzana_hab_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                                       default=uuid.uuid4)
    survey_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True),
                                                  ForeignKey("surveys.survey_id"))
    region_id: Mapped[str] = mapped_column(String(50), ForeignKey("regions.region_id"))
    manzana_codigo: Mapped[str] = mapped_column(String(120))
    geometry: Mapped[Optional[object]] = mapped_column(Geometry("MULTIPOLYGON", srid=4326))
    habitantes_est: Mapped[Optional[float]] = mapped_column(Float)
    habitantes_low: Mapped[Optional[float]] = mapped_column(Float)
    habitantes_high: Mapped[Optional[float]] = mapped_column(Float)
    uf_vivienda: Mapped[Optional[int]] = mapped_column(Integer)
    uf_comercio: Mapped[Optional[int]] = mapped_column(Integer)
    n_parcelas: Mapped[int] = mapped_column(Integer, default=0)
    metodo: Mapped[Optional[str]] = mapped_column(String(40))
    fecha_estimacion: Mapped[date] = mapped_column(Date, server_default=func.current_date())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Establecimiento(Base):
    """Una entidad real (fábrica/colegio/iglesia/comercio grande…) que ocupa VARIAS
    parcelas catastrales. Cada establecimiento cuenta como 1 UF en vez de la suma de
    sus parcelas miembro (que se vinculan por `parcelas.establecimiento_id`).
    Lo detecta EstablecimientoAgrupador por propietario + adyacencia + uso. Migración 013.
    """
    __tablename__ = "establecimientos"

    establecimiento_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                                          default=uuid.uuid4)
    survey_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True),
                                                  ForeignKey("surveys.survey_id"))
    region_id: Mapped[str] = mapped_column(String(50), ForeignKey("regions.region_id"))
    tipo: Mapped[Optional[str]] = mapped_column(String(40))
    nombre: Mapped[Optional[str]] = mapped_column(String(250))
    uso_principal: Mapped[Optional[str]] = mapped_column(String(30))
    uf_vivienda: Mapped[int] = mapped_column(Integer, default=0)
    uf_comercio: Mapped[int] = mapped_column(Integer, default=0)
    n_parcelas: Mapped[int] = mapped_column(Integer, default=0)
    area_m2: Mapped[Optional[float]] = mapped_column(Float)
    propietario_documento: Mapped[Optional[str]] = mapped_column(String(30))
    fuente: Mapped[Optional[str]] = mapped_column(String(30), default="agrupador_propietario")
    geometry: Mapped[Optional[object]] = mapped_column(Geometry("MULTIPOLYGON", srid=4326))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ── Baselines (relevamiento anterior importado — comparativa por dirección) ───

class Baseline(Base):
    """Relevamiento ANTERIOR del cliente, importado de un CSV externo (migración 017).

    Sirve como término de comparación contra un survey actual de la misma región
    (ComparativaReporter). `fecha_relevamiento` es la del relevamiento original,
    cargada por el usuario al importar."""
    __tablename__ = "baselines"

    baseline_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                                   default=uuid.uuid4)
    region_id: Mapped[str] = mapped_column(String(50), ForeignKey("regions.region_id"))
    nombre: Mapped[str] = mapped_column(String(200))
    fecha_relevamiento: Mapped[Optional[date]] = mapped_column(Date)
    archivo_nombre: Mapped[Optional[str]] = mapped_column(String(255))
    mapeo: Mapped[Optional[str]] = mapped_column(Text)        # JSON: columna CSV → campo
    n_registros: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    direcciones: Mapped[list["BaselineDireccion"]] = relationship(
        "BaselineDireccion", back_populates="baseline", cascade="all, delete-orphan")


class BaselineDireccion(Base):
    """Una fila por dirección del relevamiento anterior, con la clave normalizada
    (`calle_norm` + `numero_norm`, agents/direccion_norm.py) usada para el matching."""
    __tablename__ = "baseline_direcciones"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    baseline_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("baselines.baseline_id", ondelete="CASCADE"))
    direccion_raw: Mapped[str] = mapped_column(Text)
    calle: Mapped[Optional[str]] = mapped_column(Text)
    numero: Mapped[Optional[str]] = mapped_column(String(30))
    calle_norm: Mapped[Optional[str]] = mapped_column(Text)
    numero_norm: Mapped[Optional[str]] = mapped_column(String(30))
    uso: Mapped[Optional[str]] = mapped_column(String(30))
    uf_vivienda: Mapped[Optional[int]] = mapped_column(Integer)
    uf_comercio: Mapped[Optional[int]] = mapped_column(Integer)
    extras: Mapped[Optional[str]] = mapped_column(Text)       # JSON: columnas no mapeadas

    baseline: Mapped["Baseline"] = relationship("Baseline", back_populates="direcciones")


# ── Orchestrator Log ──────────────────────────────────────────────────────────

class OrchestratorLog(Base):
    """Auditoría de cada step del loop del orquestador LLM."""
    __tablename__ = "orchestrator_log"

    log_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                               default=uuid.uuid4)
    survey_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True),
                                                  ForeignKey("surveys.survey_id"))
    step: Mapped[int] = mapped_column(Integer)
    agent_called: Mapped[Optional[str]] = mapped_column(String(50))
    input_resumen: Mapped[Optional[str]] = mapped_column(Text)
    output_resumen: Mapped[Optional[str]] = mapped_column(Text)
    razonamiento_llm: Mapped[Optional[str]] = mapped_column(Text)
    tokens_input: Mapped[int] = mapped_column(Integer, default=0)
    tokens_output: Mapped[int] = mapped_column(Integer, default=0)
    costo_usd: Mapped[float] = mapped_column(Float, default=0.0)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    survey: Mapped["Survey"] = relationship("Survey", back_populates="log_entries")
