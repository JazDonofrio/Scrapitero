"""012 — campos extra del BCI: valor venal, alíquota, año y propietario (PII)

BCIParser extrae del Boletim de Cadastramento Imobiliário (Várzea Grande) más
señales que ya estaban en el PDF pero no se persistían:

  - valor_venal_terreno / construccion / total → valuación fiscal (IPTU)
  - aliquota                                   → alícuota de IPTU aplicada
  - anio_construccion                          → año más antiguo entre las unidades
  - propietario_nombre / propietario_documento → contribuyente principal (PII: CPF/CNPJ)
  - contribuyente_secundario                   → co-responsable (nombre)

PII: propietario_nombre/documento y contribuyente_secundario son datos personales
del PDF público de la prefeitura; se guardan para trazabilidad pero NO se exportan
por default en el CSV/web (manejar aparte si se necesita).

Revision ID: 012
Revises: 011
Create Date: 2026-06-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("parcelas", sa.Column("valor_venal_terreno", sa.Float, nullable=True))
    op.add_column("parcelas", sa.Column("valor_venal_construccion", sa.Float, nullable=True))
    op.add_column("parcelas", sa.Column("valor_venal_total", sa.Float, nullable=True))
    op.add_column("parcelas", sa.Column("aliquota", sa.Float, nullable=True))
    op.add_column("parcelas", sa.Column("anio_construccion", sa.Integer, nullable=True))
    op.add_column("parcelas", sa.Column("propietario_nombre", sa.String(200), nullable=True))
    op.add_column("parcelas", sa.Column("propietario_documento", sa.String(30), nullable=True))
    op.add_column("parcelas", sa.Column("contribuyente_secundario", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("parcelas", "contribuyente_secundario")
    op.drop_column("parcelas", "propietario_documento")
    op.drop_column("parcelas", "propietario_nombre")
    op.drop_column("parcelas", "anio_construccion")
    op.drop_column("parcelas", "aliquota")
    op.drop_column("parcelas", "valor_venal_total")
    op.drop_column("parcelas", "valor_venal_construccion")
    op.drop_column("parcelas", "valor_venal_terreno")
