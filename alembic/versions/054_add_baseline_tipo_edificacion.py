"""054 — baseline_direcciones.tipo_edificacion: etiqueta editable del relevamiento anterior

El editor del panel de incidencias deja corregir la etiqueta (taxonomía del cliente) y las UF
de cualquier ubicación. En las tarjetas **con parcela** eso ya vivía en `parcela_tipo_manual` /
`parcela_uf_manual`, pero los casos `geocoding_dudoso` apuntan a una dirección del relevamiento
ANTERIOR, que no tiene parcela: su etiqueta y sus UF son las de `baseline_direcciones`.

Por qué una columna nueva y no reusar `uso`: `uso` es la clasificación funcional
(`residencial`/`comercial`/`mixto`) que consumen `_agregar_por_direccion`, la comparativa y el
CSV; meterle una etiqueta de la taxonomía del cliente ("RESIDÊNCIA", "HOTEL", "LOTE VAZIO") la
rompería para todos esos consumidores. La etiqueta va aparte y `uso` se **deriva** de las UF
resultantes con la misma regla que ya usa la importación (mixto si hay de las dos, comercial si
sólo comercio, si no residencial), así los dos campos no se contradicen.

No hace falta tabla de override durable: `baseline_direcciones` **es** el registro persistente
del CSV del cliente (un re-import crea otro baseline, no pisa este).

Revision ID: 054
Revises: 053
Create Date: 2026-07-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "054"
down_revision: Union[str, None] = "053"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("baseline_direcciones", sa.Column("tipo_edificacion", sa.String(60)))


def downgrade() -> None:
    op.drop_column("baseline_direcciones", "tipo_edificacion")
