"""036 — baseline_direcciones.cep (CEP para geocoding de máxima precisión en Brasil)

El wizard de import/actualización ahora mapea también la columna **CEP** del CSV del
relevamiento anterior. En Brasil el CEP es la señal de **máxima precisión**: BaselineGeocoder
se lo pasa a **geocodebr** (campo `cep`) por fila y lo agrega al texto que va a
Nominatim/Mapbox/Google. Reduce la cola de geocodes lejanos (calles homónimas, etc.).

Revision ID: 036
Revises: 035
Create Date: 2026-06-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "036"
down_revision: Union[str, None] = "035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("baseline_direcciones", sa.Column("cep", sa.String(9), nullable=True))


def downgrade() -> None:
    op.drop_column("baseline_direcciones", "cep")
