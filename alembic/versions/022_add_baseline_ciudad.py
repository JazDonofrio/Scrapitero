"""022 — baselines.ciudad (contexto de ciudad para geocodificar bien)

Geocodificar sólo "calle número" caía en cualquier parte del país (puntos
dispersos por todo Brasil). El operador indica la ciudad/localidad del
relevamiento anterior al crear la actualización; se la agrega a cada consulta de
geocoding (y a la clave del caché) para desambiguar.

Revision ID: 022
Revises: 021
Create Date: 2026-06-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("baselines", sa.Column("ciudad", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("baselines", "ciudad")
