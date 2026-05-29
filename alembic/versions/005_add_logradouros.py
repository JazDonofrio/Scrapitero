"""005 — tabla logradouros (segmentos de calle IBGE Faces de Logradouros 2022)

Revision ID: 005
Revises: 004
Create Date: 2026-05-29
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
import geoalchemy2

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "logradouros",
        sa.Column("logradouro_id", sa.String(50), primary_key=True),  # cod_face del SHP
        sa.Column("region_id", sa.String(50), sa.ForeignKey("regions.region_id"),
                  nullable=False),
        sa.Column("setor_censitario_id", sa.String(20),
                  sa.ForeignKey("setores_censitarios.setor_id"), nullable=True),
        sa.Column("geometry", geoalchemy2.Geometry("LINESTRING", srid=4326)),
        # Nombre del logradouro
        sa.Column("tipo_logradouro", sa.String(50)),   # RUA, AV, TRAVESSA…
        sa.Column("titulo_logradouro", sa.String(50)), # DR, PROF, …
        sa.Column("nome_logradouro", sa.String(200)),
        # Numeração — lado esquerdo e direito do segmento
        sa.Column("nro_inicial_esq", sa.Integer),
        sa.Column("nro_final_esq",   sa.Integer),
        sa.Column("nro_inicial_dir", sa.Integer),
        sa.Column("nro_final_dir",   sa.Integer),
        # CEP
        sa.Column("cep_esq", sa.String(9)),
        sa.Column("cep_dir", sa.String(9)),
        # Município
        sa.Column("cod_municipio", sa.String(7)),
        sa.Column("nom_municipio", sa.String(100)),
        sa.Column("loaded_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("idx_logradouros_region", "logradouros", ["region_id"],
                    if_not_exists=True)
    op.create_index("idx_logradouros_setor", "logradouros", ["setor_censitario_id"],
                    if_not_exists=True)
    op.create_index("idx_logradouros_nome", "logradouros", ["nome_logradouro"],
                    if_not_exists=True)


def downgrade() -> None:
    op.drop_table("logradouros")
