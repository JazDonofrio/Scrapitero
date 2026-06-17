"""025 — tabla hoteles (agente especializado en hoteles)

Hoteles del relevamiento con lo que Google solo no da: cantidad de habitaciones
(UHs) y si cerró definitivamente. Fuente v1: Cadastur "Meios de Hospedagem"
(oficial Brasil: UHs/leitos/CNPJ/situação) + `business_status` de Google Places
(ya capturado en `comercios`). Cada hotel se vincula a su parcela (ST_Contains)
y sus habitaciones cuentan como `uf_comercio` de la parcela (hoteles abiertos).

Revision ID: 025
Revises: 024
Create Date: 2026-06-14
"""

from typing import Sequence, Union

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "025"
down_revision: Union[str, None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hoteles",
        sa.Column("hotel_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("survey_id", UUID(as_uuid=True),
                  sa.ForeignKey("surveys.survey_id", ondelete="CASCADE"), nullable=True),
        sa.Column("region_id", sa.String(50),
                  sa.ForeignKey("regions.region_id"), nullable=False),
        sa.Column("parcela_id", UUID(as_uuid=True),
                  sa.ForeignKey("parcelas.parcela_id", ondelete="SET NULL"), nullable=True),
        sa.Column("nombre", sa.String(250)),
        sa.Column("cnpj", sa.String(20)),
        sa.Column("tipo", sa.String(60)),                # hotel/pousada/flat/hostel…
        sa.Column("direccion", sa.Text),
        sa.Column("location", geoalchemy2.Geometry("POINT", srid=4326)),
        sa.Column("habitaciones", sa.Integer),           # UHs (unidades habitacionais)
        sa.Column("leitos", sa.Integer),
        sa.Column("estrellas", sa.Integer),
        sa.Column("fuente", sa.String(30), server_default="cadastur"),
        sa.Column("situacion_cadastur", sa.String(40)),  # Ativo/Inativo/Cancelado
        sa.Column("business_status", sa.String(40)),     # de Google Places
        sa.Column("cerrado_def", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("fetched_at", sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint("region_id", "cnpj", name="uq_hoteles_region_cnpj"),
    )
    op.create_index("ix_hoteles_parcela", "hoteles", ["parcela_id"])
    op.create_index("ix_hoteles_survey", "hoteles", ["survey_id"])
    op.create_index("ix_hoteles_location", "hoteles", ["location"],
                    postgresql_using="gist")


def downgrade() -> None:
    op.drop_index("ix_hoteles_location", table_name="hoteles")
    op.drop_index("ix_hoteles_survey", table_name="hoteles")
    op.drop_index("ix_hoteles_parcela", table_name="hoteles")
    op.drop_table("hoteles")
