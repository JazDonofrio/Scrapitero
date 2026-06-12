"""016 — codigo_logradouro en parcelas (código municipal del logradouro, del BCI)

El Boletim de Cadastramento Imobiliário imprime la línea de dirección como
"CÓDIGO LOGRADOURO NÚMERO CEP" → "1234 RUA FULANO 321 78110-328". Ese código
inicial es el identificador municipal del logradouro; hasta ahora BCIParser lo
descartaba. Se persiste para el CSV de operadora (columna CODIGO_LOGRADOURO).

Solo se llena en las próximas corridas de BCIParser / parseo inline de
VGBCIFetcher — sin backfill automático.

Revision ID: 016
Revises: 015
Create Date: 2026-06-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("parcelas", sa.Column("codigo_logradouro", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("parcelas", "codigo_logradouro")
