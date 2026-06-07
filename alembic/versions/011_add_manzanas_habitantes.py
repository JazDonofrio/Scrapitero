"""011 — tabla manzanas_habitantes (estimación dasimétrica de población)

Estimación SECUNDARIA e independiente del relevamiento principal: reparte la
población de los polígonos censales (`setores_censitarios.pop_total`) entre las
parcelas usando un peso de ocupación (uf_vivienda → volumen edificado → área
residencial) y agrega el resultado por MANZANA CATASTRAL.

Es menos exacta que la UF del relevamiento, así que se guarda y se muestra aparte,
con su fecha de estimación. No toca la tabla `parcelas`.

Tabla `manzanas_habitantes` (una fila por manzana catastral de un survey):
  - manzana_codigo   → manzana catastral derivada de la fuente (ARBA / Salta / VG…)
  - geometry         → unión de las parcelas de la manzana (para mapa/área)
  - habitantes_est   → habitantes estimados (dasimétrico) + banda low/high
  - uf_vivienda/uf_comercio → suma de UF del relevamiento principal en la manzana
  - metodo           → cómo se ponderó el reparto (uf/volumen/area)
  - fecha_estimacion → a qué fecha corresponde la estimación

Revision ID: 011
Revises: 010
Create Date: 2026-06-07
"""

from typing import Sequence, Union

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "manzanas_habitantes",
        sa.Column("manzana_hab_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("survey_id", UUID(as_uuid=True),
                  sa.ForeignKey("surveys.survey_id"), nullable=False),
        sa.Column("region_id", sa.String(50),
                  sa.ForeignKey("regions.region_id"), nullable=False),
        sa.Column("manzana_codigo", sa.String(120), nullable=False),
        sa.Column("geometry", geoalchemy2.Geometry("MULTIPOLYGON", srid=4326)),
        sa.Column("habitantes_est", sa.Float),
        sa.Column("habitantes_low", sa.Float),
        sa.Column("habitantes_high", sa.Float),
        sa.Column("uf_vivienda", sa.Integer),
        sa.Column("uf_comercio", sa.Integer),
        sa.Column("n_parcelas", sa.Integer, server_default="0"),
        sa.Column("metodo", sa.String(40)),            # uf_vivienda / volumen / area_residencial
        sa.Column("fecha_estimacion", sa.Date, server_default=sa.func.current_date()),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint("survey_id", "manzana_codigo",
                            name="uq_manzana_hab_survey_codigo"),
    )
    op.create_index("ix_manzana_hab_survey", "manzanas_habitantes", ["survey_id"])
    op.create_index("ix_manzana_hab_region", "manzanas_habitantes", ["region_id"])
    op.create_index(
        "ix_manzana_hab_geom", "manzanas_habitantes", ["geometry"],
        postgresql_using="gist",
    )


def downgrade() -> None:
    op.drop_index("ix_manzana_hab_geom", table_name="manzanas_habitantes")
    op.drop_index("ix_manzana_hab_region", table_name="manzanas_habitantes")
    op.drop_index("ix_manzana_hab_survey", table_name="manzanas_habitantes")
    op.drop_table("manzanas_habitantes")
