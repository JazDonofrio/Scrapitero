"""041 — calle_geometria (geometría de calle de OSM, cacheada por ciudad/calle)

El geocoding del relevamiento anterior coloca las direcciones no-exactas **interpolando sobre
el eje real de la calle** (geometría de OSM). Traerla de Overpass es lento e intermitente, y la
misma calle se necesita en re-runs y en otras zonas de la misma ciudad. Se cachea la geometría
cruda (todas las ways que matchean el nombre, como MultiLineString GeoJSON) por (ciudad,
calle_norm), incluyendo el **resultado negativo** (geojson NULL = OSM no tiene la calle) para no
re-consultar calles ausentes. Modelada como `calle_canonica` (mig. 038).

Revision ID: 041
Revises: 040
Create Date: 2026-06-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "041"
down_revision: Union[str, None] = "040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "calle_geometria",
        sa.Column("ciudad", sa.String(120), primary_key=True),
        sa.Column("calle_norm", sa.Text(), primary_key=True),
        sa.Column("geojson", sa.Text(), nullable=True),   # MultiLineString GeoJSON; NULL = OSM no la tiene
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("calle_geometria")
