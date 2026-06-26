"""039 — tabla cadastur_hospedagem (cache local de Cadastur, con UH/leitos)

Copia local consolidada del registro oficial de Meios de Hospedagem de Cadastur
(Ministério do Turismo). El portal CKAN (`dados.turismo.gov.br`) es intermitente
(se cayó con 502), así que se descargan todos los trimestres parseables, se
filtran por município y se consolidan por CNPJ (registro más reciente por hotel,
UH/leitos coalescidos del más reciente que los traiga). Un relevamiento futuro usa
esta tabla en vez de pegarle al portal (HotelFetcher lee local primero).

A diferencia de `receita_estabelecimentos_hospedagem` (027, universo CNPJ SIN
habitaciones), esta tabla SÍ trae **UH (unidades habitacionais)** y **leitos** —
el valor exacto de Cadastur. La carga la hace `CadasturLocalFetcher`.

Revision ID: 039
Revises: 038
Create Date: 2026-06-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "039"
down_revision: Union[str, None] = "038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cadastur_hospedagem",
        sa.Column("cnpj", sa.String(20), primary_key=True),       # solo dígitos
        sa.Column("razao_social", sa.Text),
        sa.Column("nome_fantasia", sa.Text),
        sa.Column("uf", sa.String(2)),
        sa.Column("municipio", sa.String(160)),
        sa.Column("tipo_hospedagem", sa.String(80)),              # Tipo de Hospedagem / Atividade
        sa.Column("uh", sa.Integer),                              # unidades habitacionais
        sa.Column("leitos", sa.Integer),
        sa.Column("situacao", sa.String(60)),                     # situação da atividade / cadastral
        sa.Column("logradouro", sa.Text),
        sa.Column("numero", sa.String(30)),
        sa.Column("bairro", sa.String(160)),
        sa.Column("complemento", sa.Text),
        sa.Column("cep", sa.String(12)),
        sa.Column("telefone", sa.String(40)),
        sa.Column("fonte_trimestre", sa.String(8)),               # del registro de identidad (ej. 2025T3)
        sa.Column("fonte_fecha", sa.Date),                        # last_modified del recurso de origen
        sa.Column("uh_fonte_trimestre", sa.String(8)),            # trimestre del que salió la UH
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_cadastur_uf_mun", "cadastur_hospedagem", ["uf", "municipio"])


def downgrade() -> None:
    op.drop_index("ix_cadastur_uf_mun", table_name="cadastur_hospedagem")
    op.drop_table("cadastur_hospedagem")
