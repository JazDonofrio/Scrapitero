"""034 — baseline_direcciones.barrio (bairro como parte de la dirección a geocodificar)

El wizard de actualización pide tres columnas que juntas definen la dirección completa
con la que se recuperan las coordenadas: endereço completo + **bairro** + cidade. Faltaba
dónde guardar el bairro; BaselineGeocoder ahora lo agrega al texto a geocodificar
('endereço, bairro, cidade') para desambiguar mejor.

Revision ID: 034
Revises: 033
Create Date: 2026-06-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "034"
down_revision: Union[str, None] = "033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("baseline_direcciones", sa.Column("barrio", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("baseline_direcciones", "barrio")
