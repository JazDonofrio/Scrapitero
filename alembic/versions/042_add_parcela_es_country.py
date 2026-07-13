"""042 — parcelas.es_country (parcela dentro de un condomínio/loteamento fechado)

Para la capa "Country" del mapa: `CountryFetcher` detecta barrios cerrados/condomínios en OSM y
marca las parcelas cuyo centroide cae dentro de alguno. Los otros ítems críticos del mapa
(Hoteles/Edificios/PH/Shopping) se derivan de datos ya existentes; Country es el único que necesita
un flag persistido porque OSM es su única fuente.

Revision ID: 042
Revises: 041
Create Date: 2026-07-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "042"
down_revision: Union[str, None] = "041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("parcelas", sa.Column("es_country", sa.Boolean(), nullable=False,
                                        server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("parcelas", "es_country")
