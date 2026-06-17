"""029 — parcelas.categoria_uso / descripcion_uso (taxonomía del cliente)

Sello sobre cada parcela del relevamiento de la categoría (R/C/E) y la descripción
(BAR, ESCOLA, HOSPITAL PÚBLICO, SHOPPING…) derivada de los establecimientos del CNPJ que
caen dentro de la parcela (ST_Contains), vía `receita_estabelecimentos` (migración 028).
Una parcela puede tener varios establecimientos → `descripcion_uso` puede listar más de uno.

Revision ID: 029
Revises: 028
Create Date: 2026-06-17
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "029"
down_revision: Union[str, None] = "028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("parcelas", sa.Column("categoria_uso", sa.String(1)))          # R / C / E
    op.add_column("parcelas", sa.Column("descripcion_uso", sa.Text))            # 1+ descripciones
    op.add_column("parcelas", sa.Column("categoria_uso_fuente", sa.String(20)))  # 'receita_cnae'


def downgrade() -> None:
    op.drop_column("parcelas", "categoria_uso_fuente")
    op.drop_column("parcelas", "descripcion_uso")
    op.drop_column("parcelas", "categoria_uso")
