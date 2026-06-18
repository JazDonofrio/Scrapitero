"""030 — asistencia humana para habitaciones de hoteles

Cuando ninguna fuente automática tiene las habitaciones (Cadastur caído, sin OSM rooms,
estimación BCI desactivada), un humano las consigue (llamando al hotel) y las carga a mano.

- `hoteles.telefono`: contacto del hotel (de Receita `telefone`), para que el asistente llame.
- `hotel_habitaciones_manual`: el valor cargado a mano, keyed por (region_id, cnpj), para que
  **sobreviva a un re-corte** del botón 🏨 (que borra+reinserta `hoteles`). HotelFetcher lo
  re-aplica tras insertar. El dato oficial de Cadastur (cuando vuelva) tiene prioridad.

Revision ID: 030
Revises: 029
Create Date: 2026-06-17
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "030"
down_revision: Union[str, None] = "029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("hoteles", sa.Column("telefono", sa.String(40)))
    op.create_table(
        "hotel_habitaciones_manual",
        sa.Column("region_id", sa.String, nullable=False),
        sa.Column("cnpj", sa.String(20), nullable=False),
        sa.Column("habitaciones", sa.Integer, nullable=False),
        sa.Column("autor", sa.String(60)),
        sa.Column("actualizado_at", sa.DateTime, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("region_id", "cnpj"),
    )


def downgrade() -> None:
    op.drop_table("hotel_habitaciones_manual")
    op.drop_column("hoteles", "telefono")
