"""031 — tabla establecimientos_poi (POIs no-CNPJ: shoppings de OSM/Google)

Establecimientos que NO salen del CNPJ de Receita y que sí identifican bien ciertas fuentes:
hoy, **shoppings** (OSM `shop=mall` + Google Places `shopping_mall`). Se cargan por región
(`ShoppingFetcher`) con su categoría/descripción de la taxonomía del cliente (E / SHOPPING) y
los aterriza `ParcelaCategoria` junto a los establecimientos CNPJ (union por ST_Contains).

Receita NO sirve para shoppings: el CNAE 6822 ("administração de propriedade imobiliária")
captura todas las inmobiliarias, no los shopping centers — por eso se remapeó a IMOBILIÁRIA.

Revision ID: 031
Revises: 030
Create Date: 2026-06-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "031"
down_revision: Union[str, None] = "030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "establecimientos_poi",
        sa.Column("poi_id", sa.dialects.postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("region_id", sa.String, nullable=False),
        sa.Column("fuente", sa.String(12), nullable=False),    # osm / google
        sa.Column("categoria", sa.String(1)),                  # R / C / E
        sa.Column("descripcion", sa.String(40)),               # SHOPPING…
        sa.Column("nombre", sa.Text),
        sa.Column("lat", sa.Float, nullable=False),
        sa.Column("lng", sa.Float, nullable=False),
        sa.Column("fetched_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_estab_poi_region", "establecimientos_poi", ["region_id"])


def downgrade() -> None:
    op.drop_index("ix_estab_poi_region", table_name="establecimientos_poi")
    op.drop_table("establecimientos_poi")
