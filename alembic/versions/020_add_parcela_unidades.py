"""020 — parcela_unidades (unidades del BCI por parcela, para expandir el CSV)

El BCI lista cada unidade del imóvel (UNIDADE 1..N con su código, área, año y uso)
en la sección "DADOS E CARACTERÍSTICAS DA CONSTRUÇÃO". Hoy el parser sólo las
**cuenta** (uf_vivienda/uf_comercio). Para edificios/lotes con varias unidades el
CSV del relevamiento debe emitir **una fila por unidad** (repitiendo la dirección +
complemento, con su identificador) en vez de un conteo. Esta tabla guarda esas
unidades; sólo se persisten parcelas con MÁS de una unidad. No cambia `parcelas`
(la web/KPIs siguen mostrando el conteo).

Revision ID: 020
Revises: 019
Create Date: 2026-06-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "020"
down_revision: Union[str, None] = "019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "parcela_unidades",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("parcela_id", UUID(as_uuid=True),
                  sa.ForeignKey("parcelas.parcela_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("n_unidade", sa.Integer, nullable=True),       # 1..N en el BCI
        sa.Column("codigo_unidade", sa.String(30), nullable=True),  # código único de la unidade
        sa.Column("area_m2", sa.Float, nullable=True),
        sa.Column("anio_construccion", sa.Integer, nullable=True),
        sa.Column("uso", sa.String(20), nullable=True),          # residencial/comercial/…
    )
    op.create_index("ix_parcela_unidades_parcela", "parcela_unidades", ["parcela_id"])


def downgrade() -> None:
    op.drop_index("ix_parcela_unidades_parcela", table_name="parcela_unidades")
    op.drop_table("parcela_unidades")
