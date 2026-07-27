"""050 — parcelas: número de puerta ESTIMADO (nunca pisa el del catastro)

El BCI de Várzea Grande trae la dirección completa (código de logradouro, calle, CEP, bairro,
loteamento) pero en una parte de las parcelas **no trae el número de puerta**: viene literal
`0` en el PDF (típico de `TIPO IMÓVEL: Territorial` / lote não construído) o el campo sale en
blanco. Medido en el survey `zona-varzea-grande-actualizacion`: 31 parcelas con `numero='0'`
(5,5%) + 41 con el campo vacío (7,3%) = **12,8%** sin altura utilizable. No es un fallo del
parser — se verificó contra los PDFs originales.

Esas parcelas quedan fuera del apareo por dirección contra el relevamiento anterior y salen con
`NUMERO` vacío en el CSV de operadora. `NumeroEstimator` las completa **interpolando la altura
sobre el eje de la calle** a partir de los números reales de sus linderos.

El valor estimado va en columnas PROPIAS: `parcelas.numero` es dato del municipio y no se toca
nunca. Así el consumidor decide si lo usa, y la UI/CSV pueden marcarlo como estimado (requisito
explícito: que se note que el número no es oficial).

Revision ID: 050
Revises: 049
Create Date: 2026-07-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "050"
down_revision: Union[str, None] = "049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Número de puerta inferido. NULL = no estimado (o el catastro ya trae el suyo).
    op.add_column("parcelas", sa.Column("numero_estimado", sa.String(20)))
    # Cómo se estimó: 'eje_osm' (interpolado sobre la geometría real de la calle) o
    # 'eje_pca' (eje sintético por regresión sobre las parcelas de la calle, cuando OSM
    # no tiene esa vía). Sufijo '_extrap' si el target cae fuera del rango de anclas.
    op.add_column("parcelas", sa.Column("numero_estimado_metodo", sa.String(30)))
    # 0..1. Baja con la distancia a los linderos con número real y con la extrapolación.
    op.add_column("parcelas", sa.Column("numero_estimado_confianza", sa.Float))


def downgrade() -> None:
    op.drop_column("parcelas", "numero_estimado_confianza")
    op.drop_column("parcelas", "numero_estimado_metodo")
    op.drop_column("parcelas", "numero_estimado")
