"""028 — tabla receita_estabelecimentos (universo CNPJ clasificado por categoría)

Establecimientos del CNPJ (dados abertos da Receita Federal) filtrados a los CNAE que
mapean a la taxonomía del cliente (C-Comercial / E-Especial: BAR, RESTAURANTE, ESCOLA,
HOSPITAL, SHOPPING, etc.) y clasificados con `agents/receita_categorias.py`. Complementa a
`receita_estabelecimentos_hospedagem` (que es solo hospedagem); ésta es el universo amplio.

Se geocodifica aparte (GeocodebrFetcher) y se aterriza sobre las parcelas del relevamiento
(link ST_Contains + sello de categoría/descripción en `parcelas`, migración 029).

Revision ID: 028
Revises: 027
Create Date: 2026-06-17
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "028"
down_revision: Union[str, None] = "027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "receita_estabelecimentos",
        sa.Column("cnpj", sa.String(14), primary_key=True),
        sa.Column("cnpj_basico", sa.String(8), nullable=False),
        sa.Column("razao_social", sa.Text),
        sa.Column("nome_fantasia", sa.Text),
        sa.Column("cnae_principal", sa.String(7), nullable=False),
        sa.Column("categoria", sa.String(1)),          # R / C / E
        sa.Column("descripcion", sa.String(40)),       # BAR, ESCOLA, HOSPITAL PÚBLICO…
        sa.Column("natureza_juridica", sa.String(4)),  # de Empresas (público si empieza con 1)
        sa.Column("situacao_cadastral", sa.String(2)),
        sa.Column("situacao", sa.String(20)),
        sa.Column("tipo_logradouro", sa.String(40)),
        sa.Column("logradouro", sa.Text),
        sa.Column("numero", sa.String(20)),
        sa.Column("complemento", sa.Text),
        sa.Column("bairro", sa.String(120)),
        sa.Column("cep", sa.String(8)),
        sa.Column("uf", sa.String(2)),
        sa.Column("municipio_rf", sa.String(4)),
        sa.Column("municipio_nome", sa.String(120)),
        sa.Column("lat", sa.Float),
        sa.Column("lng", sa.Float),
        sa.Column("geocode_source", sa.String(20)),
        sa.Column("periodo", sa.String(7)),
        sa.Column("fetched_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_receita_estab_uf_mun", "receita_estabelecimentos",
                    ["uf", "municipio_nome"])
    op.create_index("ix_receita_estab_categoria", "receita_estabelecimentos",
                    ["categoria", "descripcion"])


def downgrade() -> None:
    op.drop_index("ix_receita_estab_categoria", table_name="receita_estabelecimentos")
    op.drop_index("ix_receita_estab_uf_mun", table_name="receita_estabelecimentos")
    op.drop_table("receita_estabelecimentos")
