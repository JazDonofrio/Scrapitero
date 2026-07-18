"""032 — reverse_geocode_cache (reusar coordenada→dirección, ahorrar Google)

AddressResolver resuelve la dirección de cada parcela por **reverse geocoding**
(centroide lat/lng → calle/número) con Google Maps (~USD 0,005/llamada). El mismo
punto físico se re-consulta en re-runs, sub-zonas y sobre todo en **regiones "copia"**
(el mismo `cca_code`/centroide existe en dos regiones → 2 llamadas al mismo lugar).

Este caché guarda el resultado **por coordenada redondeada + idioma** (`clave`) y se
reusa antes de pegarle a Google. Es el análogo "reverse" del `geocode_cache` (mig. 021),
que es "forward" (dirección→coordenada). Por eso va en tabla aparte: distinta clave.

Revision ID: 032
Revises: 031
Create Date: 2026-06-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "032"
down_revision: Union[str, None] = "031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reverse_geocode_cache",
        # clave = "<lat:.6f>|<lng:.6f>|<idioma>" (coordenada redondeada ~0,1 m + idioma)
        sa.Column("clave", sa.Text, primary_key=True),
        # componentes parseados de Google (calle, numero, barrio, codigo_postal, …)
        sa.Column("componentes", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("reverse_geocode_cache")
