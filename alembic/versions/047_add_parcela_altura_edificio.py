"""047 — parcela_altura: ubicación del edificio medido + validación de pertenencia

Falso positivo encontrado en «DA LIBERDADE 144» (cca 102846): el BCI la declara **vacante**
(0 m² construidos) y Street View confirma el terreno vacío, pero la capa de altura marcaba
`sin_declarar` con un edificio de 4,5 m y 75 m² de huella.

Causa: `buildingInsights:findClosest` de Solar API hace exactamente lo que su nombre dice —
devuelve el edificio **más cercano al punto**, sin importar si está dentro de la parcela. En
un lote vacío eso es **siempre** la construcción del vecino. En este caso el edificio medido
estaba a 20,3 m del centroide, fuera de la parcela, y caía en `DA LIBERDADE 350` (residencial,
385 m² construidos).

Alcance medido antes del fix: **56%** de los `sin_declarar` de la región nueva (20/36) y
**69%** de la anterior (38/55) no tenían NINGÚN footprint de Google Open Buildings dentro de
la parcela — es decir, una segunda fuente satelital independiente tampoco veía construcción.

Estas columnas permiten (a) saber DÓNDE está el edificio que se midió y (b) descartar la
discrepancia cuando no pertenece a la parcela.

Revision ID: 047
Revises: 046
Create Date: 2026-07-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "047"
down_revision: Union[str, None] = "046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Centro del edificio que devolvió Solar (para auditar y para el ST_Contains).
    op.add_column("parcela_altura", sa.Column("edificio_lat", sa.Float))
    op.add_column("parcela_altura", sa.Column("edificio_lng", sa.Float))
    # ¿ese edificio cae dentro de la parcela? NULL = no evaluado (datos previos al fix).
    op.add_column("parcela_altura", sa.Column("dentro_parcela", sa.Boolean))
    # Distancia del edificio medido al centroide de la parcela (m) — útil para revisar.
    op.add_column("parcela_altura", sa.Column("edificio_dist_m", sa.Float))
    # Footprints de Google Open Buildings dentro de la parcela: segunda fuente satelital
    # independiente. 0 + `sin_declarar` ⇒ casi seguro se midió al vecino.
    op.add_column("parcela_altura", sa.Column("footprints_dentro", sa.Integer))


def downgrade() -> None:
    op.drop_column("parcela_altura", "footprints_dentro")
    op.drop_column("parcela_altura", "edificio_dist_m")
    op.drop_column("parcela_altura", "dentro_parcela")
    op.drop_column("parcela_altura", "edificio_lng")
    op.drop_column("parcela_altura", "edificio_lat")
