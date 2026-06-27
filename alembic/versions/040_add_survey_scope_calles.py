"""040 — surveys.scope_calles (alcance por calle + rango de alturas)

Modo alternativo de definir el alcance de una *actualización*: en vez de dibujar un
polígono, se autodetectan las calles y el rango de numeración del relevamiento anterior
y el relevamiento nuevo cubre exactamente esas calles dentro de esos rangos.

`scope_calles` (JSONB): lista de {calle, calle_norm, num_min, num_max}. El `zone_geojson`
de la región sigue usándose como **polígono de descarga** (cubre las calles); `scope_calles`
es el **filtro estricto por dirección** que aplica `ScopeCallesFilter` tras tener las
direcciones (BCI). NULL ⇒ survey normal (solo polígono).

Revision ID: 040
Revises: 039
Create Date: 2026-06-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "040"
down_revision: Union[str, None] = "039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("surveys", sa.Column("scope_calles", postgresql.JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("surveys", "scope_calles")
