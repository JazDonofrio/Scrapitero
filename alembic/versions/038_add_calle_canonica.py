"""038 — calle_canonica (nombre de calle canonizado por Gemini, cacheado)

Para las calles del relevamiento anterior que no matchean en Mapbox/OSM (nombre abreviado o
incompleto, ej. "R ORIEL B CAMPOS"), se le pide a Gemini el nombre COMPLETO ("Rua Oriel
Bezerra de Campos") y se reintenta el geocoding. El resultado se cachea por (calle_norm,
ciudad) para no re-pagar Gemini en re-runs ni en otros baselines de la misma ciudad.

Revision ID: 038
Revises: 037
Create Date: 2026-06-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "038"
down_revision: Union[str, None] = "037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "calle_canonica",
        sa.Column("calle_norm", sa.Text(), primary_key=True),
        sa.Column("ciudad", sa.String(120), primary_key=True),
        sa.Column("nombre_canonico", sa.Text(), nullable=True),   # NULL = Gemini no la resolvió
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("calle_canonica")
