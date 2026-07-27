"""051 — parcela_numero_manual: número de puerta cargado a mano por el operador

`NumeroEstimator` (mig. 050) completa el número de las parcelas que el catastro dejó sin altura
interpolando entre los linderos, pero **se niega a estimar** cuando la numeración de la calle no
sigue el orden espacial (coherencia < 0,6) o cuando no hay anclas suficientes. En la actualización
de VG son 26 parcelas: `CLOVIS HUGNEY` (coherencia 0,42), `JOAO LIBANIO` (0,41), `MAL RONDON`
(0,55), `SÃO BERNARDO` (0,56)… Ahí ninguna interpolación acierta (p90 de error medido: ~220
números), así que el único camino es la asistencia humana — el tipo de incidencia
`numero_faltante`.

La clave es **`(region_id, cca_code)`**, no `parcela_id`, a diferencia de `parcela_uf_manual` /
`parcela_tipo_manual`: la inscrição del BCI es estable entre relevamientos y el `parcela_id` no
(cada survey inserta filas nuevas). Así el número que cargó el operador sobrevive al próximo
re-scrape de la zona, que es justamente para lo que existe esta tabla.

El valor se refleja en `parcelas.numero_estimado` con `numero_estimado_metodo='manual'` y
confianza 1,0. `parcelas.numero` sigue siendo lo que publicó el municipio — un número cargado a
mano no es dato del catastro y la UI lo muestra como «(manual)», no como oficial.

Revision ID: 051
Revises: 050
Create Date: 2026-07-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "051"
down_revision: Union[str, None] = "050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "parcela_numero_manual",
        sa.Column("region_id", sa.String(50), nullable=False),
        # Inscrição/CCA del catastro: identidad estable de la parcela entre relevamientos.
        sa.Column("cca_code", sa.String(50), nullable=False),
        sa.Column("numero", sa.String(20), nullable=False),
        sa.Column("nota", sa.Text),
        sa.Column("autor", sa.String(60)),
        sa.Column("parcela_id", sa.dialects.postgresql.UUID(as_uuid=False)),  # trazabilidad
        sa.Column("actualizado_at", sa.DateTime, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("region_id", "cca_code", name="pk_parcela_numero_manual"),
    )


def downgrade() -> None:
    op.drop_table("parcela_numero_manual")
