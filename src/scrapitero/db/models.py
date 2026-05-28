"""Modelos SQLAlchemy para Scrapitero."""

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

    # Auditoría
    fuente_parcela: Mapped[Optional[str]] = mapped_column(String(30))
    fecha_relevamiento: Mapped[date] = mapped_column(Date, server_default=func.current_date())
    validado_manual: Mapped[bool] = mapped_column(Boolean, default=False)

    survey: Mapped["Survey"] = relationship("Survey", back_populates="parcelas")
    edificios: Mapped[list["Edificio"]] = relationship("Edificio", back_populates="parcela")


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
    pisos_estimados: Mapped[Optional[int]] = mapped_column(Integer)
    source: Mapped[Optional[str]] = mapped_column(String(30))
    # "ms_global" | "google_open" | "osm"
    external_id: Mapped[Optional[str]] = mapped_column(String(100))

    parcela: Mapped[Optional["Parcela"]] = relationship("Parcela", back_populates="edificios")


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
