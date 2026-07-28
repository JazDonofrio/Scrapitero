"""053 — parcela_ubicacion_manual: corrección humana de la COORDENADA de una ubicación

El panel de incidencias ya dejaba corregir todas las variables de texto de una dirección
(mig. 052), pero no la única que a veces está mal: **dónde está**. Casos donde hace falta:

  - `geocoding_dudoso` — la incidencia dice "esta dirección del relevamiento anterior está a
    2.616 m de su propia calle" y hasta ahora **no ofrecía ninguna acción**: el operador veía
    el problema y no podía arreglarlo. La corrección natural es arrastrar el punto al lugar
    correcto mirando el satélite.
  - una parcela cuyo centroide quedó mal (geometría del catastro desplazada, o el punto de un
    lote grande que no representa el frente construido).

Dos destinos distintos según qué se está moviendo:
  - **parcela** → `parcelas.centroid_lat/lng` (que es lo que dibuja el mapa y exporta el CSV:
    las parcelas se grafican por su punto, no por el polígono) + respaldo en esta tabla.
    **NO se toca `geometry`**: el polígono es el del catastro y sigue siendo el dato oficial;
    lo que el operador corrige es el punto representativo.
  - **dirección del baseline** → `baseline_direcciones.lat/lng` con `geocode_source='manual'`
    (no necesita tabla nueva: esa fila ES el registro durable del relevamiento anterior).

Clave `(region_id, cca_code)` igual que en la 052 — la inscrição sobrevive al re-scrape, el
`parcela_id` no. `parcelas.ubicacion_source='manual'` es el sello que hace respetar la
corrección: SmartGIS re-escribe el centroide en cada corrida y sin el sello la pisaría (ver
la regla de precedencia de fuentes en CLAUDE.md).

Revision ID: 053
Revises: 052
Create Date: 2026-07-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "053"
down_revision: Union[str, None] = "052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "parcela_ubicacion_manual",
        sa.Column("region_id", sa.String(50), nullable=False),
        sa.Column("cca_code", sa.String(50), nullable=False),
        sa.Column("lat", sa.Float, nullable=False),
        sa.Column("lng", sa.Float, nullable=False),
        # coordenada que tenía antes de la corrección, para poder auditar/deshacer
        sa.Column("lat_previa", sa.Float),
        sa.Column("lng_previa", sa.Float),
        sa.Column("nota", sa.Text),
        sa.Column("autor", sa.String(60)),
        sa.Column("parcela_id", sa.dialects.postgresql.UUID(as_uuid=False)),
        sa.Column("actualizado_at", sa.DateTime, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("region_id", "cca_code", name="pk_parcela_ubicacion_manual"),
    )
    op.add_column("parcelas", sa.Column("ubicacion_source", sa.String(20)))


def downgrade() -> None:
    op.drop_column("parcelas", "ubicacion_source")
    op.drop_table("parcela_ubicacion_manual")
