"""008 — agregar uf_fuente a parcelas

Registra cómo se determinó el conteo de UF, para distinguir exacto vs estimado en la web:
  - 'bci'   → exacto (extraído de PDFs BCI en Brasil)
  - 'osm'   → estimado, con conteo real de OSM (building:flats/addr:units)
  - 'proxy' → estimado, proxy geométrico (área×pisos/tamaño_típico)
  - 'uso'   → estimado, mínimo por uso (parcela sin edificios OSM)
  - NULL    → sin determinar

Revision ID: 008
Revises: 007
Create Date: 2026-06-03
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("parcelas", sa.Column("uf_fuente", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("parcelas", "uf_fuente")
