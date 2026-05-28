"""002 — agregar región Ituzaingó, Buenos Aires, Argentina

Revision ID: 002
Revises: 001
Create Date: 2026-05-28
"""

from typing import Sequence, Union
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO regions (region_id, name, country_code, state_code, municipio_codigo)
        VALUES ('ituzaingo-ba-ar', 'Ituzaingó, Buenos Aires, Argentina', 'ARG', 'BA', '136')
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM regions WHERE region_id = 'ituzaingo-ba-ar'")
