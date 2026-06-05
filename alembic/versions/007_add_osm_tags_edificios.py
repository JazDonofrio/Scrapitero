"""007 — agregar tipo_osm y unidades_osm a edificios

Captura tags de OSM que antes se descartaban, para estimar unidades funcionales:
  - tipo_osm:     valor del tag building=* (apartments/house/commercial/retail/…)
  - unidades_osm: building:flats / addr:units cuando OSM los trae (conteo real de UF)
  (pisos_estimados ya existía — ahí va building:levels)

Revision ID: 007
Revises: 006
Create Date: 2026-06-03
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("edificios", sa.Column("tipo_osm", sa.String(50), nullable=True))
    op.add_column("edificios", sa.Column("unidades_osm", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("edificios", "unidades_osm")
    op.drop_column("edificios", "tipo_osm")
