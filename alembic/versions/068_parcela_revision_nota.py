"""068 — nota de revisión por parcela: nuestro dato tiene respaldo y la referencia del cliente dice otra cosa

Cotejando VAZ049 contra el `DETALLE COMPARACION.xlsx` del cliente (17-sep-2026) quedaron lotes
donde lo que publicamos coincide con fuentes independientes —la ficha del BCI y el censo 2022,
CNPJ activos en Receita, Overture, los números de los lotes vecinos— y el excel dice otra cosa.
No hay regla que los resuelva: ni corregirlos para que coincidan con el excel ni dejarlos sin
marca sirve. El operador los mira a ojo, y para eso la nota tiene que viajar con la parcela
hasta el CSV de relevamiento.

Se guarda por (region_id, cca_code), igual que `parcela_direccion_manual`, para que sobreviva
al re-scrape del BCI, donde el parcela_id cambia. `motivo` separa `cantidad` (viviendas /
comercios) de `numero` (número de puerta).

Revision ID: 068
Revises: 067
Create Date: 2026-09-17
"""

from typing import Sequence, Union

from alembic import op

revision: str = "068"
down_revision: Union[str, None] = "067"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS parcela_revision_nota (
            region_id      varchar(50) NOT NULL REFERENCES regions(region_id) ON DELETE CASCADE,
            cca_code       varchar NOT NULL,
            parcela_id     uuid REFERENCES parcelas(parcela_id) ON DELETE SET NULL,
            motivo         varchar(30) NOT NULL,
            nota           text NOT NULL,
            autor          varchar NOT NULL DEFAULT 'cotejo',
            actualizado_at timestamp NOT NULL DEFAULT now(),
            PRIMARY KEY (region_id, cca_code)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_revision_nota_parcela "
               "ON parcela_revision_nota (parcela_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS parcela_revision_nota")
