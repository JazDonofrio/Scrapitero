"""035 — baseline_direcciones.estado (UF/COD_UF para desambiguar el geocoding en Brasil)

El wizard de actualización ahora mapea también la columna **COD_UF** del CSV, donde está el
estado de Brasil. Se guarda como **sigla** (MT, SP…) — el `sigla_uf()` de `geocode_forward`
normaliza el código IBGE numérico (51→MT) o la sigla directa. BaselineGeocoder se lo pasa a
**geocodebr** por fila (para distinguir municípios homónimos entre estados) y lo agrega al
texto que va a Nominatim/Google.

Revision ID: 035
Revises: 034
Create Date: 2026-06-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "035"
down_revision: Union[str, None] = "034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("baseline_direcciones", sa.Column("estado", sa.String(2), nullable=True))


def downgrade() -> None:
    op.drop_column("baseline_direcciones", "estado")
