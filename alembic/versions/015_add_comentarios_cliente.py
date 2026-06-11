"""015 — tabla comentarios_cliente: sugerencias/correcciones del cliente sobre el mapa

El cliente (vista raíz "/") puede dejar comentarios en puntos del mapa del
relevamiento: sugerencias o correcciones ("acá hay un comercio, no una vivienda",
"falta este edificio", etc.). Cada comentario queda georreferenciado (POINT 4326)
y, si el punto cae dentro de una parcela del survey, vinculado a ella
(`parcela_id`, vía ST_Contains al crearlo) para poder aplicar la corrección.

Campos:
  - texto       → el comentario del cliente (obligatorio)
  - autor_rol   → 'cliente' / 'operador' (NULL = modo sin auth)
  - estado      → 'pendiente' (default) / 'resuelto'; el operador lo gestiona
  - resuelto_at → cuándo se marcó resuelto

Revision ID: 015
Revises: 014
Create Date: 2026-06-11
"""

from typing import Sequence, Union

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "comentarios_cliente",
        sa.Column("comentario_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("survey_id", UUID(as_uuid=True),
                  sa.ForeignKey("surveys.survey_id"), nullable=False),
        sa.Column("parcela_id", UUID(as_uuid=True),
                  sa.ForeignKey("parcelas.parcela_id"), nullable=True),
        sa.Column("geometry", geoalchemy2.Geometry("POINT", srid=4326),
                  nullable=False),
        sa.Column("texto", sa.Text, nullable=False),
        sa.Column("autor_rol", sa.String(20)),
        sa.Column("estado", sa.String(20), nullable=False,
                  server_default="pendiente"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("resuelto_at", sa.DateTime),
    )
    op.create_index("ix_comentarios_cliente_survey", "comentarios_cliente",
                    ["survey_id"])


def downgrade() -> None:
    op.drop_index("ix_comentarios_cliente_survey",
                  table_name="comentarios_cliente")
    op.drop_table("comentarios_cliente")
