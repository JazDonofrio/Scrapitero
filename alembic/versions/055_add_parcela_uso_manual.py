"""055 — parcela_uso_manual: corrección humana del USO de una parcela

El panel de incidencias ya deja corregir la dirección (052), la coordenada (053), las UF (046)
y la etiqueta de edificación (043). Faltaba la variable que gobierna a casi todas las demás:
**para qué se usa la parcela**.

El caso que lo motivó: un condominio residencial que el pipeline marcó `comercial`. Corregir
sólo `parcela_tipo_manual` (la etiqueta 🏢 de la taxonomía del cliente) arregla lo que se lee
en el popup y en el CSV Operadora, pero deja el resto mal — `uso_principal` es lo que colorea
el círculo del mapa (`categoriaMapa`), lo que sale en el CSV del relevamiento, lo que decide
el reparto de habitantes en `dasymetric_population` y lo que `UnidadesEstimator` toma como
input para estimar UF. Era una corrección de fachada.

Clave `(region_id, cca_code)` igual que 052/053: la inscrição sobrevive al re-scrape, el
`parcela_id` no (cada relevamiento crea parcelas nuevas).

`parcelas.uso_fuente='manual'` es el sello que hace respetar la corrección. Notar que la guarda
ya existía a medias: `bci_parser.py` respeta `uso_fuente IN ('manual','cadastur','google')`
desde hace tiempo, pero **nadie escribía nunca `'manual'`** — faltaba el escritor, no la
protección. Los que sí había que blindar en esta migración son `uso_classifier` (que hacía un
UPDATE incondicional), `google_places_fetcher` y `hotel_fetcher`.

Revision ID: 055
Revises: 054
Create Date: 2026-07-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "055"
down_revision: Union[str, None] = "054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "parcela_uso_manual",
        sa.Column("region_id", sa.String(50), nullable=False),
        sa.Column("cca_code", sa.String(50), nullable=False),
        sa.Column("uso_principal", sa.String(30), nullable=False),
        # uso que tenía antes de la corrección, para poder auditar/deshacer
        sa.Column("uso_previo", sa.String(30)),
        sa.Column("uso_fuente_previa", sa.String(30)),
        sa.Column("nota", sa.Text),
        sa.Column("autor", sa.String(60)),
        sa.Column("parcela_id", sa.dialects.postgresql.UUID(as_uuid=False)),
        sa.Column("actualizado_at", sa.DateTime, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("region_id", "cca_code", name="pk_parcela_uso_manual"),
    )


def downgrade() -> None:
    op.drop_table("parcela_uso_manual")
