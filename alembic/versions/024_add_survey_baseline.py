"""024 — surveys.baseline_id (de qué relevamiento anterior es actualización este survey)

Para mostrar en el mapa del survey nuevo el relevamiento ANTERIOR (puntos grises
con la UF) por debajo de las parcelas nuevas — comparación directa viejo/nuevo
sobre el mismo mapa. Lo setea el flujo de actualización al crear el survey.

Revision ID: 024
Revises: 023
Create Date: 2026-06-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "024"
down_revision: Union[str, None] = "023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("surveys", sa.Column(
        "baseline_id", UUID(as_uuid=True),
        sa.ForeignKey("baselines.baseline_id", ondelete="SET NULL"), nullable=True))


def downgrade() -> None:
    op.drop_column("surveys", "baseline_id")
