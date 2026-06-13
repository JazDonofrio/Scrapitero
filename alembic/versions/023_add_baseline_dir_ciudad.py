"""023 — baseline_direcciones.ciudad (ciudad por fila del relevamiento anterior)

En el mapeo de columnas de la actualización el operador puede señalar la columna
de ciudad del CSV. Se guarda por fila y el geocoder la usa (con fallback a la
ciudad global del baseline) para geocodificar bien aunque el CSV abarque varias
ciudades.

Revision ID: 023
Revises: 022
Create Date: 2026-06-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "023"
down_revision: Union[str, None] = "022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("baseline_direcciones", sa.Column("ciudad", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("baseline_direcciones", "ciudad")
