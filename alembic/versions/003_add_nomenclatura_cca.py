"""003 — agregar nomenclatura_catastral y cca_code a parcelas

Revision ID: 003
Revises: 002
Create Date: 2026-05-29
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("parcelas", sa.Column("cca_code", sa.String(50), nullable=True))
    op.add_column("parcelas", sa.Column("nomenclatura_catastral", sa.String(200), nullable=True))
    op.add_column("parcelas", sa.Column("partida_inmobiliaria", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column("parcelas", "cca_code")
    op.drop_column("parcelas", "nomenclatura_catastral")
    op.drop_column("parcelas", "partida_inmobiliaria")
