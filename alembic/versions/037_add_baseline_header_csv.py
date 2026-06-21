"""037 — baselines.header_csv (encabezado crudo del CSV importado)

Guarda el header original (lista de columnas en orden) del CSV del relevamiento anterior,
para poder **re-exportar el CSV de operadora con el MISMO formato** (mismas columnas, mismo
orden) pero con los datos del relevamiento nuevo. Los baselines importados antes de esta
migración no lo tienen → la exportación reconstruye un orden best-effort hasta reimportar.

Revision ID: 037
Revises: 036
Create Date: 2026-06-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "037"
down_revision: Union[str, None] = "036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("baselines", sa.Column("header_csv", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("baselines", "header_csv")
