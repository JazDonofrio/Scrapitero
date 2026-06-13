"""019 — coordenadas en baseline_direcciones (geocoding del relevamiento anterior)

El relevamiento anterior se importa como CSV (baselines, mig. 017) pero NO trae
coordenadas. Para poder graficarlo en el mapa al crear una *actualización* (y
dibujar encima el polígono de la nueva zona) geocodificamos cada dirección y
guardamos su punto. `BaselineGeocoder` escribe estas columnas; el flujo
"Actualización" de la Web UI las lee para pintar los puntos.

Revision ID: 019
Revises: 018
Create Date: 2026-06-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "019"
down_revision: Union[str, None] = "018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("baseline_direcciones", sa.Column("lat", sa.Float, nullable=True))
    op.add_column("baseline_direcciones", sa.Column("lng", sa.Float, nullable=True))
    op.add_column("baseline_direcciones",
                  sa.Column("geocode_source", sa.String(20), nullable=True))
    op.add_column("baseline_direcciones",
                  sa.Column("geocode_confidence", sa.Float, nullable=True))
    op.add_column("baselines", sa.Column("geocoded_at", sa.DateTime, nullable=True))
    op.add_column("baselines", sa.Column("n_geocodificadas", sa.Integer,
                                         nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("baselines", "n_geocodificadas")
    op.drop_column("baselines", "geocoded_at")
    op.drop_column("baseline_direcciones", "geocode_confidence")
    op.drop_column("baseline_direcciones", "geocode_source")
    op.drop_column("baseline_direcciones", "lng")
    op.drop_column("baseline_direcciones", "lat")
