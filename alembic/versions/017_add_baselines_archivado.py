"""017 — baselines importados (relevamiento anterior del cliente) + archivado de surveys

Comparativa por dirección: el cliente sube su relevamiento anterior (CSV externo)
y queda como `baseline` de la región, con una fila normalizada por dirección en
`baseline_direcciones`. `ComparativaReporter` lo cruza contra un survey actual.

`surveys.archivado`: los relevamientos NUNCA se borran (requisito: el anterior
siempre debe quedar disponible como término de comparación). El botón de la web
archiva en vez de eliminar; el DELETE físico solo se permite sobre archivados.

Revision ID: 017
Revises: 016
Create Date: 2026-06-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "baselines",
        sa.Column("baseline_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("region_id", sa.String(50), sa.ForeignKey("regions.region_id"),
                  nullable=False),
        sa.Column("nombre", sa.String(200), nullable=False),
        # Fecha del relevamiento ORIGINAL (la carga el usuario al importar) — es la
        # que da sentido al "cómo creció desde entonces".
        sa.Column("fecha_relevamiento", sa.Date, nullable=True),
        sa.Column("archivo_nombre", sa.String(255), nullable=True),
        sa.Column("mapeo", sa.Text, nullable=True),          # JSON: columna CSV → campo
        sa.Column("n_registros", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_table(
        "baseline_direcciones",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("baseline_id", UUID(as_uuid=True),
                  sa.ForeignKey("baselines.baseline_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("direccion_raw", sa.Text, nullable=False),   # tal cual vino del CSV
        sa.Column("calle", sa.Text, nullable=True),
        sa.Column("numero", sa.String(30), nullable=True),
        sa.Column("calle_norm", sa.Text, nullable=True),       # clave de matching
        sa.Column("numero_norm", sa.String(30), nullable=True),
        sa.Column("uso", sa.String(30), nullable=True),
        sa.Column("uf_vivienda", sa.Integer, nullable=True),
        sa.Column("uf_comercio", sa.Integer, nullable=True),
        sa.Column("extras", sa.Text, nullable=True),           # JSON: columnas no mapeadas
    )
    op.create_index("ix_baseline_dir_clave", "baseline_direcciones",
                    ["baseline_id", "calle_norm", "numero_norm"])
    op.add_column("surveys", sa.Column("archivado", sa.Boolean, nullable=False,
                                       server_default="false"))


def downgrade() -> None:
    op.drop_column("surveys", "archivado")
    op.drop_index("ix_baseline_dir_clave", table_name="baseline_direcciones")
    op.drop_table("baseline_direcciones")
    op.drop_table("baselines")
