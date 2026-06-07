"""010 — tabla comercios (Google Places) + uf_fuente/uso_fuente 'google'

GooglePlacesFetcher baja comercios (POIs) de Google Places, los vincula a su
parcela con ST_Contains y de ahí sale el conteo real de uf_comercio:
cada comercio dentro de una parcela = +1 UF de comercio (sin agrupar).

Tabla `comercios` (insumo, no es output atómico):
  - place_id        → id de Google, dedup entre celdas/corridas
  - parcela_id      → link espacial (centroide del comercio dentro de la parcela)
  - rubro/tipos     → categoría primaria + tipos de Google
  - location POINT  → punto del comercio (índice GIST para ST_Contains)

Marca de data lineage en parcelas: uf_fuente='google' / uso_fuente='google'
(no agrega columnas — reutiliza las de migraciones 008/009).

Revision ID: 010
Revises: 009
Create Date: 2026-06-07
"""

from typing import Sequence, Union

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "comercios",
        sa.Column("comercio_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("survey_id", UUID(as_uuid=True),
                  sa.ForeignKey("surveys.survey_id"), nullable=False),
        sa.Column("region_id", sa.String(50),
                  sa.ForeignKey("regions.region_id"), nullable=False),
        sa.Column("parcela_id", UUID(as_uuid=True),
                  sa.ForeignKey("parcelas.parcela_id"), nullable=True),
        sa.Column("place_id", sa.String(120), nullable=False),
        sa.Column("nombre", sa.String(250)),
        sa.Column("rubro", sa.String(80)),            # primaryType de Google
        sa.Column("tipos", sa.Text),                  # csv de types
        sa.Column("business_status", sa.String(40)),
        sa.Column("location", geoalchemy2.Geometry("POINT", srid=4326)),
        sa.Column("source", sa.String(30), server_default="google_places"),
        sa.Column("fetched_at", sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint("region_id", "place_id", name="uq_comercios_region_place"),
    )
    op.create_index("ix_comercios_parcela", "comercios", ["parcela_id"])
    op.create_index("ix_comercios_survey", "comercios", ["survey_id"])
    op.create_index(
        "ix_comercios_location", "comercios", ["location"],
        postgresql_using="gist",
    )


def downgrade() -> None:
    op.drop_index("ix_comercios_location", table_name="comercios")
    op.drop_index("ix_comercios_survey", table_name="comercios")
    op.drop_index("ix_comercios_parcela", table_name="comercios")
    op.drop_table("comercios")
