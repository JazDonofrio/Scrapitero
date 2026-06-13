"""018 — subzona_geojson en surveys (relevamientos parciales)

Un relevamiento PARCIAL es un survey nuevo sobre una región ya relevada, con su
propio polígono (subconjunto de la zona). Los fetchers que filtran por
`regions.zone_geojson` prefieren `surveys.subzona_geojson` cuando existe
(COALESCE en la carga de zona). Los PDFs BCI se reutilizan por ciudad, así que
re-relevar una sub-zona no re-descarga lo compartido.

Revision ID: 018
Revises: 017
Create Date: 2026-06-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "018"
down_revision: Union[str, None] = "017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("surveys", sa.Column("subzona_geojson", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("surveys", "subzona_geojson")
