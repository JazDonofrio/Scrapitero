"""001 initial schema — 7 tablas base

Revision ID: 001
Revises:
Create Date: 2026-05-28
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
import geoalchemy2
from sqlalchemy.dialects.postgresql import UUID

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Extensión PostGIS (idempotente)
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    # ── regions ───────────────────────────────────────────────────────────────
    op.create_table(
        "regions",
        sa.Column("region_id", sa.String(50), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("country_code", sa.String(3), nullable=False),
        sa.Column("state_code", sa.String(10)),
        sa.Column("municipio_codigo", sa.String(20)),
        sa.Column("bbox_wkt", sa.Text),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    # Insertar región inicial: Várzea Grande
    op.execute("""
        INSERT INTO regions (region_id, name, country_code, state_code, municipio_codigo)
        VALUES ('vg-mt-br', 'Várzea Grande, Mato Grosso, Brasil', 'BRA', 'MT', '5108402')
        ON CONFLICT DO NOTHING
    """)

    # ── surveys ───────────────────────────────────────────────────────────────
    op.create_table(
        "surveys",
        sa.Column("survey_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("region_id", sa.String(50), sa.ForeignKey("regions.region_id"),
                  nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime),
        sa.Column("steps_count", sa.Integer, server_default="0"),
        sa.Column("llm_tokens_used", sa.Integer, server_default="0"),
        sa.Column("llm_cost_usd", sa.Float, server_default="0.0"),
        sa.Column("notes", sa.Text),
    )

    # ── setores_censitarios ───────────────────────────────────────────────────
    op.create_table(
        "setores_censitarios",
        sa.Column("setor_id", sa.String(20), primary_key=True),
        sa.Column("region_id", sa.String(50), sa.ForeignKey("regions.region_id"),
                  nullable=False),
        sa.Column("geometry", geoalchemy2.Geometry("MULTIPOLYGON", srid=4326)),
        sa.Column("pop_total", sa.Integer),
        sa.Column("domicilios_total", sa.Integer),
        sa.Column("domicilios_casas", sa.Integer),
        sa.Column("domicilios_aptos", sa.Integer),
        sa.Column("area_km2", sa.Float),
        sa.Column("source_file", sa.String(200)),
        sa.Column("loaded_at", sa.DateTime, server_default=sa.func.now()),
    )
    # GeoAlchemy2 crea el índice GIST de geometry automáticamente al crear la tabla
    op.create_index("idx_setores_region", "setores_censitarios", ["region_id"],
                    if_not_exists=True)

    # ── parcelas ──────────────────────────────────────────────────────────────
    op.create_table(
        "parcelas",
        sa.Column("parcela_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("survey_id", UUID(as_uuid=True), sa.ForeignKey("surveys.survey_id"),
                  nullable=False),
        sa.Column("region_id", sa.String(50), sa.ForeignKey("regions.region_id"),
                  nullable=False),
        sa.Column("setor_censitario_id", sa.String(20),
                  sa.ForeignKey("setores_censitarios.setor_id")),
        # Geometría
        sa.Column("geometry", geoalchemy2.Geometry("POLYGON", srid=4326)),
        sa.Column("centroid_lat", sa.Float),
        sa.Column("centroid_lng", sa.Float),
        sa.Column("area_m2_terreno", sa.Float),
        sa.Column("area_m2_construida", sa.Float),
        # Dirección
        sa.Column("calle", sa.String(200)),
        sa.Column("numero", sa.String(20)),
        sa.Column("complemento", sa.String(100)),
        sa.Column("barrio", sa.String(100)),
        sa.Column("localidad", sa.String(100)),
        sa.Column("municipio", sa.String(100)),
        sa.Column("estado_provincia", sa.String(100)),
        sa.Column("pais", sa.String(50)),
        sa.Column("codigo_postal", sa.String(20)),
        sa.Column("direccion_source", sa.String(30)),
        sa.Column("direccion_confidence", sa.Float),
        # Habitantes
        sa.Column("habitantes_estimados", sa.Float),
        sa.Column("habitantes_low", sa.Float),
        sa.Column("habitantes_high", sa.Float),
        sa.Column("habitantes_metodo", sa.String(50)),
        sa.Column("habitantes_confidence", sa.Float),
        # Tipología
        sa.Column("uso_principal", sa.String(30)),
        sa.Column("footprints_count", sa.Integer, server_default="0"),
        sa.Column("pisos_estimados_max", sa.Integer),
        sa.Column("unidades_funcionales_estimadas", sa.Integer),
        # Auditoría
        sa.Column("fuente_parcela", sa.String(30)),
        sa.Column("fecha_relevamiento", sa.Date, server_default=sa.func.current_date()),
        sa.Column("validado_manual", sa.Boolean, server_default="false"),
    )
    # GeoAlchemy2 crea el índice GIST de geometry automáticamente
    op.create_index("idx_parcelas_survey", "parcelas", ["survey_id"], if_not_exists=True)
    op.create_index("idx_parcelas_setor", "parcelas", ["setor_censitario_id"],
                    if_not_exists=True)

    # ── edificios ─────────────────────────────────────────────────────────────
    op.create_table(
        "edificios",
        sa.Column("edificio_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("survey_id", UUID(as_uuid=True), sa.ForeignKey("surveys.survey_id"),
                  nullable=False),
        sa.Column("parcela_id", UUID(as_uuid=True), sa.ForeignKey("parcelas.parcela_id")),
        sa.Column("setor_censitario_id", sa.String(20),
                  sa.ForeignKey("setores_censitarios.setor_id")),
        sa.Column("footprint", geoalchemy2.Geometry("POLYGON", srid=4326)),
        sa.Column("centroid", geoalchemy2.Geometry("POINT", srid=4326)),
        sa.Column("area_m2", sa.Float),
        sa.Column("pisos_estimados", sa.Integer),
        sa.Column("source", sa.String(30)),
        sa.Column("external_id", sa.String(100)),
    )
    # GeoAlchemy2 crea los índices GIST de footprint y centroid automáticamente
    op.create_index("idx_edificios_survey", "edificios", ["survey_id"], if_not_exists=True)
    op.create_index("idx_edificios_setor", "edificios", ["setor_censitario_id"],
                    if_not_exists=True)

    # ── unidades_funcionales ──────────────────────────────────────────────────
    op.create_table(
        "unidades_funcionales",
        sa.Column("unidad_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("edificio_id", UUID(as_uuid=True), sa.ForeignKey("edificios.edificio_id"),
                  nullable=False),
        sa.Column("piso", sa.Integer),
        sa.Column("depto", sa.String(10)),
        sa.Column("tipo", sa.String(30)),
    )

    # ── orchestrator_log ──────────────────────────────────────────────────────
    op.create_table(
        "orchestrator_log",
        sa.Column("log_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("survey_id", UUID(as_uuid=True), sa.ForeignKey("surveys.survey_id"),
                  nullable=False),
        sa.Column("step", sa.Integer, nullable=False),
        sa.Column("agent_called", sa.String(50)),
        sa.Column("input_resumen", sa.Text),
        sa.Column("output_resumen", sa.Text),
        sa.Column("razonamiento_llm", sa.Text),
        sa.Column("tokens_input", sa.Integer, server_default="0"),
        sa.Column("tokens_output", sa.Integer, server_default="0"),
        sa.Column("costo_usd", sa.Float, server_default="0.0"),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("idx_log_survey", "orchestrator_log", ["survey_id"], if_not_exists=True)


def downgrade() -> None:
    op.drop_table("orchestrator_log")
    op.drop_table("unidades_funcionales")
    op.drop_table("edificios")
    op.drop_table("parcelas")
    op.drop_table("setores_censitarios")
    op.drop_table("surveys")
    op.drop_table("regions")
