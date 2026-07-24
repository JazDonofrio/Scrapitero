"""043 — etiquetado manual del tipo de edificación + descarte de falsos hoteles

El operador puede corregir a mano el "Tipo de edificación" de una parcela (taxonomía del
cliente) desde la asistencia de hoteles: cuando un supuesto hotel NO es hotel (p.ej. Google
devuelve "Casa Cortina" — una tienda de cortinas — con `primaryType=lodging`), lo saca de
`hoteles` y le pone la etiqueta correcta a la parcela.

- `parcela_tipo_manual`: etiqueta forzada por parcela (gana a todo el cálculo automático de
  `_tipo_edificacion`). Persiste, keyed por parcela_id.
- `hotel_descartado`: memoria de "esto NO es hotel" **y** el punto de comercio a dibujar en su
  coordenada real. Sirve para dos cosas: (a) que un re-corte del botón 🏨 (HotelFetcher
  borra+reinserta `hoteles`) no lo vuelva a crear como hotel —match por CNPJ si lo hay; si no
  (Google no trae), por nombre normalizado + proximidad—; y (b) alimentar la capa de comercios
  marcados del mapa (nombre + etiqueta + coordenada), sin depender de que caiga en una parcela.

Revision ID: 043
Revises: 042
Create Date: 2026-07-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "043"
down_revision: Union[str, None] = "042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "parcela_tipo_manual",
        sa.Column("parcela_id", UUID(as_uuid=True), nullable=False),
        sa.Column("tipo_edificacion", sa.String(80), nullable=False),
        sa.Column("categoria", sa.String(1)),          # R / C / E (informativo)
        sa.Column("autor", sa.String(60)),
        sa.Column("actualizado_at", sa.DateTime, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("parcela_id"),
        sa.ForeignKeyConstraint(["parcela_id"], ["parcelas.parcela_id"], ondelete="CASCADE"),
    )
    op.create_table(
        "hotel_descartado",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("region_id", sa.String(50), nullable=False),
        sa.Column("survey_id", UUID(as_uuid=True)),    # survey donde se marcó (para el mapa)
        sa.Column("cnpj", sa.String(20)),              # nullable: Google no trae CNPJ
        sa.Column("nombre", sa.String(250)),           # nombre para el popup del comercio
        sa.Column("nombre_norm", sa.String(250)),      # normalizado, para el match del filtro
        sa.Column("lat", sa.Float),
        sa.Column("lng", sa.Float),
        sa.Column("tipo_edificacion", sa.String(80)),  # etiqueta elegida (ej. COMÉRCIO EM GERAL)
        sa.Column("categoria", sa.String(1)),          # R / C / E
        sa.Column("autor", sa.String(60)),
        sa.Column("creado_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_hotel_descartado_region", "hotel_descartado", ["region_id"])
    op.create_index("ix_hotel_descartado_survey", "hotel_descartado", ["survey_id"])


def downgrade() -> None:
    op.drop_index("ix_hotel_descartado_survey", table_name="hotel_descartado")
    op.drop_index("ix_hotel_descartado_region", table_name="hotel_descartado")
    op.drop_table("hotel_descartado")
    op.drop_table("parcela_tipo_manual")
