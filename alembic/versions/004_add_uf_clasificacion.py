"""004 — agregar uf_vivienda y uf_comercio a parcelas

Revision ID: 004
Revises: 003
Create Date: 2026-05-29
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("parcelas", sa.Column("uf_vivienda", sa.Integer(), nullable=True))
    op.add_column("parcelas", sa.Column("uf_comercio", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("parcelas", "uf_vivienda")
    op.drop_column("parcelas", "uf_comercio")
