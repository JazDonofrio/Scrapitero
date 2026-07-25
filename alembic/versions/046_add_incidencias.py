"""046 — reporte de incidencias por relevamiento (resolución humana) + override de UF

Centraliza los casos que **sólo un humano puede resolver** y que hasta ahora morían en un log,
un aviso de Telegram o una página ad-hoc de un solo tipo (la asistencia de hoteles, que queda
absorbida como un tipo más): hoteles sin habitaciones, parcelas donde la altura satelital
contradice al catastro, y UF declaradas físicamente imposibles.

Patrón (el mismo de `hotel_habitaciones_manual`/`hotel_descartado`): las incidencias se
**regeneran** desde el estado actual, pero el `estado`/`resolucion`/`nota` se preservan por
**clave natural** (`UniqueConstraint(survey_id, tipo, clave)`), así un re-run del botón 🏨 o de
📏 Altura no reabre lo ya resuelto. Para hoteles la clave NO puede ser `hotel_id` (HotelFetcher
borra+reinserta): se usa CNPJ o nombre normalizado.

`parcela_uf_manual` es el override durable de la acción "corregir UF", análogo a
`parcela_tipo_manual` (mig. 043).

Revision ID: 046
Revises: 045
Create Date: 2026-07-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "046"
down_revision: Union[str, None] = "045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "incidencias",
        sa.Column("incidencia_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("region_id", sa.String(50), nullable=False),
        sa.Column("survey_id", UUID(as_uuid=True), nullable=False),
        sa.Column("tipo", sa.String(40), nullable=False),
        # Clave natural del caso, estable entre corridas: '<tipo>:<parcela_id>' para las de
        # parcela, 'hotel:<cnpj|nombre_norm>' para hoteles. Es la que preserva el estado.
        sa.Column("clave", sa.String(200), nullable=False),
        # pendiente | resuelta | descartada | obsoleta
        sa.Column("estado", sa.String(20), nullable=False, server_default="pendiente"),
        sa.Column("prioridad", sa.Integer, nullable=False, server_default="2"),
        sa.Column("titulo", sa.String(250)),
        sa.Column("detalle", sa.Text),
        # Contexto del caso para la tarjeta (altura, pisos, m2/UF, imagery_year, tel/CNPJ…).
        sa.Column("datos", JSONB),
        sa.Column("lat", sa.Float),
        sa.Column("lng", sa.Float),
        sa.Column("parcela_id", UUID(as_uuid=True)),
        # SIN FK a hoteles: esa tabla se borra+reinserta en cada corrida de HotelFetcher.
        sa.Column("hotel_cnpj", sa.String(20)),
        # Resolución del operador
        sa.Column("resolucion", sa.String(40)),
        sa.Column("nota", sa.Text),
        sa.Column("autor", sa.String(60)),
        sa.Column("creada_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("actualizada_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("resuelta_at", sa.DateTime),
        sa.ForeignKeyConstraint(["survey_id"], ["surveys.survey_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parcela_id"], ["parcelas.parcela_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("survey_id", "tipo", "clave", name="uq_incidencias_survey_tipo_clave"),
    )
    op.create_index("idx_incidencias_survey_estado", "incidencias", ["survey_id", "estado"])
    op.create_index("idx_incidencias_tipo", "incidencias", ["tipo"])

    op.create_table(
        "parcela_uf_manual",
        sa.Column("parcela_id", UUID(as_uuid=True), nullable=False),
        sa.Column("uf_vivienda", sa.Integer),
        sa.Column("uf_comercio", sa.Integer),
        sa.Column("autor", sa.String(60)),
        sa.Column("actualizado_at", sa.DateTime, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("parcela_id"),
        sa.ForeignKeyConstraint(["parcela_id"], ["parcelas.parcela_id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("parcela_uf_manual")
    op.drop_index("idx_incidencias_tipo", table_name="incidencias")
    op.drop_index("idx_incidencias_survey_estado", table_name="incidencias")
    op.drop_table("incidencias")
