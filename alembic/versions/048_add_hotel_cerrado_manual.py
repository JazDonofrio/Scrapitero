"""048 — cierre manual de un hotel + nota visible

`cerrado_def` lo calcula `HotelFetcher` desde las fuentes: situação BAIXADA/NULA de Receita,
Inativo/Cancelado de Cadastur o `CLOSED_PERMANENTLY` de Google. No había forma de que un humano
lo corrigiera, y hay casos que ninguna fuente resuelve.

El disparador: **MONTANA PALACE HOTEL** (CNPJ 37492337000173) figura en Receita como **ATIVA** en
Av. Filinto Müller 1059, pero en ese local opera hoy el **Hotel Zazori** — Montana no está. Es el
mismo patrón de re-registro que ya vimos en la cuadra (Hartmann → Zazori en 1059, Lagares → Real
en 1065, Sol Rios → Gabi en Couto Magalhães 1234): el CNPJ viejo sigue "activo" ante Receita
mucho después de que el hotel dejó de operar.

Hasta ahora la única salida era marcarlo "no es hotel", que lo **borra** de `hoteles` — y eso
pierde información: sí era un hotel, y al cliente le importa ver que ahí hubo uno que cerró.

- `hotel_cerrado_manual`: override humano de abierto/cerrado, keyed por `(region_id, cnpj)` igual
  que `hotel_habitaciones_manual` (mig. 030), para que sobreviva al borra+reinserta del botón 🏨.
  `HotelFetcher` lo aplica **al final** (gana sobre las fuentes) y **antes** de agregar
  `uf_comercio`, para que un hotel cerrado a mano deje de aportar UF a su parcela.
- `hoteles.nota`: comentario libre del operador, visible en el popup del mapa. Se re-hidrata
  desde el override en cada corrida. Sirve para explicar POR QUÉ está cerrado y qué hay hoy en
  su lugar ("acá opera el Hotel Zazori"), que es justo lo que se perdía al descartarlo.

Revision ID: 048
Revises: 047
Create Date: 2026-07-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "048"
down_revision: Union[str, None] = "047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hotel_cerrado_manual",
        sa.Column("region_id", sa.String(50), nullable=False),
        sa.Column("cnpj", sa.String(20), nullable=False),
        # false permite además REABRIR a mano un hotel que una fuente cerró por error
        # (p.ej. un CLOSED_PERMANENTLY viejo de Google sobre un hotel que sigue operando).
        sa.Column("cerrado", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("nota", sa.Text),
        sa.Column("autor", sa.String(60)),
        sa.Column("actualizado_at", sa.DateTime, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("region_id", "cnpj"),
    )
    op.add_column("hoteles", sa.Column("nota", sa.Text))


def downgrade() -> None:
    op.drop_column("hoteles", "nota")
    op.drop_table("hotel_cerrado_manual")
