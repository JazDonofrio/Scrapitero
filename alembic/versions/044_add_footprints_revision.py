"""044 — footprints_revision: capa de revisión de edificios (Google Open Buildings / OSM)

Tabla de solo revisión visual (mapa), NO downstream: guarda los footprints de
edificios traídos por `FootprintFetcher` (prioridad Google Open Buildings vía
el mirror de VIDA en FlatGeobuf, fallback OSM Overpass) para comparar contra
lo que dice el catastro (BCI en Brasil) sin tocar `edificios` — esa tabla ya
la consume `UnidadesEstimator` (Salta/PBA) para estimar UF y no debe mezclarse
con una fuente experimental de revisión.

Revision ID: 044
Revises: 043
Create Date: 2026-07-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
import geoalchemy2
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "044"
down_revision: Union[str, None] = "043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "footprints_revision",
        sa.Column("footprint_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("region_id", sa.String(50), sa.ForeignKey("regions.region_id"), nullable=False),
        sa.Column("survey_id", UUID(as_uuid=True), sa.ForeignKey("surveys.survey_id"), nullable=False),
        sa.Column("source", sa.String(30), nullable=False),   # google_open_buildings | osm
        sa.Column("external_id", sa.String(100)),
        sa.Column("footprint", geoalchemy2.Geometry("POLYGON", srid=4326)),
        sa.Column("centroid", geoalchemy2.Geometry("POINT", srid=4326)),
        sa.Column("area_m2", sa.Float),
        sa.Column("confidence", sa.Float),   # solo google_open_buildings (0.65-1.0)
        sa.Column("parcela_id", UUID(as_uuid=True), sa.ForeignKey("parcelas.parcela_id")),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("idx_footprints_revision_survey", "footprints_revision", ["survey_id"])
    op.create_index("idx_footprints_revision_parcela", "footprints_revision", ["parcela_id"])


def downgrade() -> None:
    op.drop_index("idx_footprints_revision_parcela", table_name="footprints_revision")
    op.drop_index("idx_footprints_revision_survey", table_name="footprints_revision")
    op.drop_table("footprints_revision")
