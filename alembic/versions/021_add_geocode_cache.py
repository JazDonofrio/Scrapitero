"""021 — geocode_cache (reusar geocoding dirección→coordenada, ahorrar Google)

Al geocodificar el relevamiento anterior (BaselineGeocoder) las direcciones
repetidas (p.ej. varias unidades del mismo edificio), los re-runs y futuras
actualizaciones de la misma zona volvían a pegarle a Nominatim/Google por cada
dirección. Este caché guarda el resultado por **dirección normalizada + país**
(`clave`) y se reusa antes de llamar a la API → menos costo (sobre todo Google).

Revision ID: 021
Revises: 020
Create Date: 2026-06-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "021"
down_revision: Union[str, None] = "020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "geocode_cache",
        # clave = "<iso2>|<calle_norm>|<numero_norm>" (dirección normalizada + país)
        sa.Column("clave", sa.Text, primary_key=True),
        sa.Column("query", sa.Text, nullable=True),          # texto crudo que se geocodificó
        sa.Column("lat", sa.Float, nullable=False),
        sa.Column("lng", sa.Float, nullable=False),
        sa.Column("geocode_source", sa.String(20), nullable=True),
        sa.Column("geocode_confidence", sa.Float, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("geocode_cache")
