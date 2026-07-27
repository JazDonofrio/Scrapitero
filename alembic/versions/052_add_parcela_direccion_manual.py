"""052 — parcela_direccion_manual: corrección humana de la dirección completa de una parcela

Reemplaza a `parcela_numero_manual` (mig. 051, se dropea vacía): el operador tiene que poder
corregir **todas** las variables de la dirección desde cualquier tarjeta del panel de incidencias
—calle, número, complemento, bairro, CEP—, no sólo el número que faltaba. Un caso de
`altura_sin_declarar` o `uf_imposible` muchas veces destapa además que la dirección está mal
rotulada, y no había forma de arreglarla sin entrar a la base.

Cambio de criterio respecto de la 051: la corrección va a **`parcelas` directamente**, con
`direccion_source='manual'` como lineage. Antes el número cargado a mano vivía sólo en
`numero_estimado`, y eso dejaba el CSV, el CSV Operadora y el apareo contra el relevamiento
anterior con la dirección vieja — es decir, la corrección del operador no llegaba al entregable.
Queda entonces:

  - `parcelas.numero`  = dirección VIGENTE (del municipio o corregida por un humano).
  - `parcelas.numero_estimado` = inferencia de máquina (`NumeroEstimator`), nunca humana.

Esta tabla es el respaldo durable: clave `(region_id, cca_code)` —la inscrição del BCI es estable
entre relevamientos, el `parcela_id` no— para que un re-scrape del BCI no borre la corrección.
`NumeroEstimator` la re-aplica al arrancar.

Revision ID: 052
Revises: 051
Create Date: 2026-07-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "052"
down_revision: Union[str, None] = "051"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "parcela_direccion_manual",
        sa.Column("region_id", sa.String(50), nullable=False),
        sa.Column("cca_code", sa.String(50), nullable=False),
        # NULL = ese campo no se tocó (sólo se re-aplican los que el operador corrigió).
        sa.Column("calle", sa.String(200)),
        sa.Column("numero", sa.String(20)),
        sa.Column("complemento", sa.String(100)),
        sa.Column("barrio", sa.String(100)),
        sa.Column("codigo_postal", sa.String(20)),
        sa.Column("nota", sa.Text),
        sa.Column("autor", sa.String(60)),
        sa.Column("parcela_id", sa.dialects.postgresql.UUID(as_uuid=False)),
        sa.Column("actualizado_at", sa.DateTime, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("region_id", "cca_code", name="pk_parcela_direccion_manual"),
    )
    # Vacía en producción (verificado): la reemplaza la tabla de arriba, que cubre el número
    # además del resto de los campos.
    op.drop_table("parcela_numero_manual")


def downgrade() -> None:
    op.create_table(
        "parcela_numero_manual",
        sa.Column("region_id", sa.String(50), nullable=False),
        sa.Column("cca_code", sa.String(50), nullable=False),
        sa.Column("numero", sa.String(20), nullable=False),
        sa.Column("nota", sa.Text),
        sa.Column("autor", sa.String(60)),
        sa.Column("parcela_id", sa.dialects.postgresql.UUID(as_uuid=False)),
        sa.Column("actualizado_at", sa.DateTime, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("region_id", "cca_code", name="pk_parcela_numero_manual"),
    )
    op.drop_table("parcela_direccion_manual")
