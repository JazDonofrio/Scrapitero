"""027 — tabla receita_estabelecimentos_hospedagem (universo de hospedagem por CNPJ)

Universo completo de establecimientos de hospedagem de Brasil, del Cadastro
Nacional da Pessoa Jurídica (CNPJ, dados abertos da Receita Federal), filtrado a
los CNAE de hospedagem (5510-8 hotéis/apart-hotéis/motéis + 5590-6
albergues/campings/pensões/outros). Aporta identidad oficial — razão social,
nome fantasia, endereço, **situação cadastral** (Ativa/Baixada/Suspensa/Inapta/
Nula) — pero **no** trae cantidad de habitaciones (eso sigue saliendo de Cadastur).

Complementa a Cadastur (cobertura mayor + señal gratuita de abierto/cerrado por
situação) y mergea por CNPJ. La carga la hace `ReceitaCNPJFetcher` (mensual, vía
proxy con salida en Brasil porque el host de Receita geo-bloquea IPs extranjeras).

Revision ID: 027
Revises: 026
Create Date: 2026-06-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "027"
down_revision: Union[str, None] = "026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "receita_estabelecimentos_hospedagem",
        sa.Column("cnpj", sa.String(14), primary_key=True),       # básico+ordem+dv (14 díg.)
        sa.Column("cnpj_basico", sa.String(8), nullable=False),   # 8 díg. (join con Empresas)
        sa.Column("matriz_filial", sa.String(1)),                 # 1=matriz, 2=filial
        sa.Column("razao_social", sa.Text),                       # de Empresas (por cnpj_basico)
        sa.Column("nome_fantasia", sa.Text),
        sa.Column("cnae_principal", sa.String(7), nullable=False),
        sa.Column("situacao_cadastral", sa.String(2)),            # 01/02/03/04/08
        sa.Column("situacao", sa.String(20)),                     # decodificada (ATIVA/BAIXADA…)
        sa.Column("data_situacao", sa.String(8)),                 # AAAAMMDD
        sa.Column("data_inicio_atividade", sa.String(8)),
        sa.Column("tipo_logradouro", sa.String(40)),
        sa.Column("logradouro", sa.Text),
        sa.Column("numero", sa.String(20)),
        sa.Column("complemento", sa.Text),
        sa.Column("bairro", sa.String(120)),
        sa.Column("cep", sa.String(8)),
        sa.Column("uf", sa.String(2)),
        sa.Column("municipio_rf", sa.String(4)),                  # código RF/SERPRO (≠ IBGE)
        sa.Column("municipio_nome", sa.String(120)),
        sa.Column("telefone", sa.String(30)),                     # ddd1+telefone1
        sa.Column("lat", sa.Float),                               # nullable: se geocodifica aparte
        sa.Column("lng", sa.Float),
        sa.Column("geocode_source", sa.String(20)),
        sa.Column("periodo", sa.String(7)),                       # dump mensual "AAAA-MM"
        sa.Column("fetched_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_receita_hosp_basico", "receita_estabelecimentos_hospedagem",
                    ["cnpj_basico"])
    op.create_index("ix_receita_hosp_cnae", "receita_estabelecimentos_hospedagem",
                    ["cnae_principal"])
    op.create_index("ix_receita_hosp_uf_mun", "receita_estabelecimentos_hospedagem",
                    ["uf", "municipio_nome"])


def downgrade() -> None:
    op.drop_index("ix_receita_hosp_uf_mun", table_name="receita_estabelecimentos_hospedagem")
    op.drop_index("ix_receita_hosp_cnae", table_name="receita_estabelecimentos_hospedagem")
    op.drop_index("ix_receita_hosp_basico", table_name="receita_estabelecimentos_hospedagem")
    op.drop_table("receita_estabelecimentos_hospedagem")
