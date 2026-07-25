"""045 — parcela_altura: altura satelital del edificio (capa de revisión)

El BCI **no trae cantidad de pisos** (verificado sobre los PDFs: `PISO CERAMICA` es el
material, `PAVIMENTAÇÃO` es el de la calle) y el footprint 2D no distingue una casa de un
edificio con la misma huella. Esta tabla guarda la altura derivada de **Google Solar API**
(`planeHeightAtCenterMeters`, elevación msnm) menos el terreno (**Elevation API**), con los
pisos estimados, para contrastarla contra el proxy del catastro y marcar discrepancias.

Igual que `footprints_revision` (mig. 044): es de **revisión visual**, no alimenta
`parcelas` ni el relevamiento. Se guarda `imagery_year` porque la imagen de Solar puede
tener más de una década (en VG casi todo 2014) — el dato NO es "estado actual".

Revision ID: 045
Revises: 044
Create Date: 2026-07-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "045"
down_revision: Union[str, None] = "044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "parcela_altura",
        sa.Column("parcela_id", UUID(as_uuid=True), nullable=False),
        sa.Column("region_id", sa.String(50), nullable=False),
        sa.Column("survey_id", UUID(as_uuid=True), nullable=False),
        # Altura
        sa.Column("techo_msnm", sa.Float),          # max planeHeightAtCenterMeters (Solar)
        sa.Column("terreno_msnm", sa.Float),        # Elevation API
        sa.Column("altura_m", sa.Float),            # techo - terreno
        sa.Column("pisos_satelital", sa.Integer),   # round(altura_m / metros_por_piso)
        sa.Column("pisos_bci_proxy", sa.Integer),   # ceil(area_constr / (FOS*terreno)) del catastro
        sa.Column("discrepancia", sa.Boolean, nullable=False, server_default=sa.false()),
        # Por qué se marcó: 'sin_declarar' (catastro sin construcción pero el satélite ve
        # un edificio — el caso más valioso) | 'mas_alto' (satélite ve más pisos que el proxy).
        sa.Column("motivo", sa.String(30)),
        # Metadata de la fuente (imprescindible: la imagen puede ser vieja)
        sa.Column("imagery_year", sa.Integer),
        sa.Column("imagery_quality", sa.String(10)),   # BASE / MEDIUM / HIGH
        sa.Column("ground_area_m2", sa.Float),         # huella del edificio que devolvió Solar
        sa.Column("roof_area_m2", sa.Float),
        sa.Column("source", sa.String(30), nullable=False, server_default="google_solar"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("parcela_id"),
        sa.ForeignKeyConstraint(["parcela_id"], ["parcelas.parcela_id"], ondelete="CASCADE"),
    )
    op.create_index("idx_parcela_altura_survey", "parcela_altura", ["survey_id"])
    op.create_index("idx_parcela_altura_discrepancia", "parcela_altura", ["discrepancia"])


def downgrade() -> None:
    op.drop_index("idx_parcela_altura_discrepancia", table_name="parcela_altura")
    op.drop_index("idx_parcela_altura_survey", table_name="parcela_altura")
    op.drop_table("parcela_altura")
