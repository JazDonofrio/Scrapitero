"""014 — agregar visible_cliente a surveys

El operador controla qué relevamientos se muestran en la vista CLIENTE (raíz "/")
con un tilde en su lista. Default TRUE: todos los relevamientos existentes y los
nuevos siguen visibles para el cliente hasta que el operador los destilde.

Revision ID: 014
Revises: 013
Create Date: 2026-06-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "surveys",
        sa.Column("visible_cliente", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("surveys", "visible_cliente")
