"""009 — agregar uso_fuente a parcelas

Registra qué agente determinó el uso_principal, para dejar el origen del dato en el
relevamiento final (data lineage):
  - 'bci'          → BCIParser (PDFs BCI, Brasil)
  - 'cpua'         → SaltaZonificacionFetcher (zonificación CPUA 2019, Salta Capital)
  - 'sigsa'        → SaltaRegistroFetcher (TIPO del registro SIGSA, provincia)
  - 'rentas'       → SaltaRentasFetcher (baldío por valorEdificado DGRM)
  - 'clasificador' → UsoClassifier (heurística)
  - NULL           → sin determinar

Revision ID: 009
Revises: 008
Create Date: 2026-06-05
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("parcelas", sa.Column("uso_fuente", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("parcelas", "uso_fuente")
