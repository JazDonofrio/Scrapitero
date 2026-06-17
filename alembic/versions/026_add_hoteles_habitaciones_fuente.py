"""026 — hoteles.habitaciones_fuente (origen del conteo de habitaciones)

El número de habitaciones puede venir EXACTO (Cadastur UHs / OSM rooms) o ser una
ESTIMACIÓN a partir del área construida del BCI de la parcela del hotel
(`area_m2_construida / m2_por_habitación`). Esta columna deja claro cuál es, para
mostrar el badge "estimado" y darle prioridad al dato exacto de Cadastur cuando esté.

Valores: 'cadastur' (exacto, oficial), 'osm' (tag rooms), 'bci_proxy' (estimado por área).

Revision ID: 026
Revises: 025
Create Date: 2026-06-14
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "026"
down_revision: Union[str, None] = "025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("hoteles", sa.Column("habitaciones_fuente", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("hoteles", "habitaciones_fuente")
