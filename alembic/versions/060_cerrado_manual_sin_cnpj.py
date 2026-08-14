"""060 — cerrar un hotel a mano: que también sobreviva SIN CNPJ (Argentina)

Misma historia que la 059, misma solución, otra tabla. `hotel_cerrado_manual` tenía PK
`(region_id, cnpj)` con `cnpj NOT NULL`, y `HotelFetcher` re-aplica el override con
`h.cnpj = m.cnpj`. En Argentina no hay identificador fiscal del hotel, así que los dos lados
son NULL, `NULL = NULL` no es TRUE y el apareo no matchea nunca — y encima el override ni se
podía guardar, porque la columna era NOT NULL.

Consecuencia: marcar cerrado un hotel argentino duraba hasta el próximo botón 🏨. El agente
borra y reinserta las filas de `hoteles`, la fuente lo vuelve a dar abierto, y el hotel
resucita. Peor que perder un dato: **un hotel cerrado que revive vuelve a aportar
`uf_comercio` a su parcela** y se mete en el entregable.

Clave alternativa: nombre normalizado + coordenada, la misma de `hotel_descartado` y la 059.

Revision ID: 060
Revises: 059
Create Date: 2026-08-14
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "060"
down_revision: Union[str, None] = "059"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("hotel_cerrado_manual", sa.Column("nombre_norm", sa.String(), nullable=True))
    op.add_column("hotel_cerrado_manual", sa.Column("lat", sa.Float(), nullable=True))
    op.add_column("hotel_cerrado_manual", sa.Column("lng", sa.Float(), nullable=True))

    op.drop_constraint("hotel_cerrado_manual_pkey", "hotel_cerrado_manual", type_="primary")
    op.alter_column("hotel_cerrado_manual", "cnpj",
                    existing_type=sa.String(), nullable=True)

    op.create_index("uq_cerrado_manual_cnpj", "hotel_cerrado_manual",
                    ["region_id", "cnpj"], unique=True,
                    postgresql_where=sa.text("cnpj IS NOT NULL"))
    op.create_index("uq_cerrado_manual_nombre", "hotel_cerrado_manual",
                    ["region_id", "nombre_norm"], unique=True,
                    postgresql_where=sa.text("cnpj IS NULL"))
    op.create_check_constraint(
        "ck_cerrado_manual_clave", "hotel_cerrado_manual",
        "cnpj IS NOT NULL OR nombre_norm IS NOT NULL")


def downgrade() -> None:
    op.execute("DELETE FROM hotel_cerrado_manual WHERE cnpj IS NULL")
    op.drop_constraint("ck_cerrado_manual_clave", "hotel_cerrado_manual", type_="check")
    op.drop_index("uq_cerrado_manual_nombre", table_name="hotel_cerrado_manual")
    op.drop_index("uq_cerrado_manual_cnpj", table_name="hotel_cerrado_manual")
    op.alter_column("hotel_cerrado_manual", "cnpj",
                    existing_type=sa.String(), nullable=False)
    op.create_primary_key("hotel_cerrado_manual_pkey", "hotel_cerrado_manual",
                          ["region_id", "cnpj"])
    op.drop_column("hotel_cerrado_manual", "lng")
    op.drop_column("hotel_cerrado_manual", "lat")
    op.drop_column("hotel_cerrado_manual", "nombre_norm")
