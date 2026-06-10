"""013 — tabla establecimientos: una entidad (fábrica/colegio/iglesia…) sobre N parcelas

Resolución de entidad: a veces un único establecimiento ocupa VARIAS parcelas
catastrales (una fábrica sobre 6 lotes, un colegio sobre 3, etc.). Sin agrupar,
el sistema cuenta esas N parcelas como N UF (o N baldíos), cuando en realidad
son UNA sola unidad funcional.

`EstablecimientoAgrupador` detecta esos clústeres (mismo propietario real +
parcelas contiguas + uso no enteramente residencial) y los guarda acá. Cada
establecimiento aporta **1 UF** en lugar de la suma de sus parcelas; el conteo de
la web/CSV/reporter cuenta el establecimiento una sola vez (las parcelas miembro
conservan su geometría e identidad catastral — no se pisan sus datos).

Tabla `establecimientos` (una fila por entidad agrupada de un survey):
  - tipo                 → fabrica / colegio / iglesia / comercio / equipamiento / …
  - nombre               → razón social (propietario) o nombre del POI de Google
  - uso_principal        → uso de la entidad (comercial/industrial/equipamiento/…)
  - uf_vivienda/comercio → UF que aporta la entidad (típicamente 1)
  - geometry             → unión (MULTIPOLYGON) de las parcelas miembro
  - n_parcelas/area_m2   → tamaño del clúster
  - propietario_documento→ CPF/CNPJ que disparó la agrupación (trazabilidad)
  - fuente               → cómo se detectó (agrupador_propietario)

Link en parcelas: `establecimiento_id` (FK nullable). Las parcelas con el mismo
establecimiento_id son partes de la misma entidad.

Revision ID: 013
Revises: 012
Create Date: 2026-06-10
"""

from typing import Sequence, Union

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "establecimientos",
        sa.Column("establecimiento_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("survey_id", UUID(as_uuid=True),
                  sa.ForeignKey("surveys.survey_id"), nullable=False),
        sa.Column("region_id", sa.String(50),
                  sa.ForeignKey("regions.region_id"), nullable=False),
        sa.Column("tipo", sa.String(40)),
        sa.Column("nombre", sa.String(250)),
        sa.Column("uso_principal", sa.String(30)),
        sa.Column("uf_vivienda", sa.Integer, server_default="0"),
        sa.Column("uf_comercio", sa.Integer, server_default="0"),
        sa.Column("n_parcelas", sa.Integer, server_default="0"),
        sa.Column("area_m2", sa.Float),
        sa.Column("propietario_documento", sa.String(30)),
        sa.Column("fuente", sa.String(30), server_default="agrupador_propietario"),
        sa.Column("geometry", geoalchemy2.Geometry("MULTIPOLYGON", srid=4326)),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_establecimientos_survey", "establecimientos", ["survey_id"])
    op.create_index("ix_establecimientos_region", "establecimientos", ["region_id"])
    op.create_index(
        "ix_establecimientos_geom", "establecimientos", ["geometry"],
        postgresql_using="gist",
    )

    op.add_column("parcelas",
                  sa.Column("establecimiento_id", UUID(as_uuid=True),
                            sa.ForeignKey("establecimientos.establecimiento_id"),
                            nullable=True))
    op.create_index("ix_parcelas_establecimiento", "parcelas", ["establecimiento_id"])


def downgrade() -> None:
    op.drop_index("ix_parcelas_establecimiento", table_name="parcelas")
    op.drop_column("parcelas", "establecimiento_id")
    op.drop_index("ix_establecimientos_geom", table_name="establecimientos")
    op.drop_index("ix_establecimientos_region", table_name="establecimientos")
    op.drop_index("ix_establecimientos_survey", table_name="establecimientos")
    op.drop_table("establecimientos")
