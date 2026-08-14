"""059 — habitaciones cargadas a mano: que también sobrevivan SIN CNPJ (Argentina)

**El bug.** `hotel_habitaciones_manual` tiene PK `(region_id, cnpj)` y `cnpj NOT NULL`. En
Brasil está perfecto: el CNPJ viene del padrón de la Receita y todo hotel lo tiene. En
Argentina **no existe ese dato** — no hay padrón hotelero descargable y ni Google ni OSM
publican identificador fiscal, así que los 7 hoteles de Malvinas tienen `cnpj = NULL`.

Consecuencia, medida el 14-ago-2026: cargar las habitaciones a mano **no servía para nada**.
`HotelFetcher` al re-correr (1) borra las filas de `hoteles` de la región, (2) las vuelve a
traer de Google/OSM sin habitaciones y (3) re-aplica los overrides con
`WHERE h.cnpj = m.cnpj`. Ese apareo con dos NULL **nunca da verdadero** (`NULL = NULL` es
NULL, no TRUE), así que el número cargado se borraba en el paso 1 y no volvía en el 3. Sin
error, sin warning, sin nada en el log: la tarjeta volvía a decir «sin habitaciones».
Y el override ni siquiera se podía guardar, porque `cnpj` era NOT NULL.

**La solución: la MISMA clave alternativa que ya usa `hotel_descartado`** — nombre
normalizado + coordenada — que existe justamente porque los hoteles de Google/OSM no traen
CNPJ. No se inventa un criterio nuevo.

  · `cnpj` pasa a NULLABLE y la PK se reemplaza por dos índices únicos PARCIALES:
      - `(region_id, cnpj)` donde `cnpj IS NOT NULL`  → **Brasil, idéntico a antes**
      - `(region_id, nombre_norm)` donde `cnpj IS NULL` → Argentina
  · `nombre_norm` / `lat` / `lng` para poder aparear cuando no hay CNPJ.

⚠ La clave argentina es sólo `nombre_norm`, sin la coordenada: dos hoteles con el mismo
nombre normalizado en una misma región se pisarían. Es a propósito — meter la coordenada en
la clave haría que mover el pin unos metros creara una fila nueva y el override quedara
huérfano, que es peor. La coordenada se usa igual al RE-APLICAR (≤200 m, como
`_esta_descartado`), así que un homónimo lejano no se lleva el dato del otro.

Revision ID: 059
Revises: 058
Create Date: 2026-08-14
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "059"
down_revision: Union[str, None] = "058"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("hotel_habitaciones_manual",
                  sa.Column("nombre_norm", sa.String(), nullable=True))
    op.add_column("hotel_habitaciones_manual", sa.Column("lat", sa.Float(), nullable=True))
    op.add_column("hotel_habitaciones_manual", sa.Column("lng", sa.Float(), nullable=True))

    op.drop_constraint("hotel_habitaciones_manual_pkey",
                       "hotel_habitaciones_manual", type_="primary")
    op.alter_column("hotel_habitaciones_manual", "cnpj",
                    existing_type=sa.String(), nullable=True)

    # Brasil: exactamente la unicidad que había (region_id + cnpj).
    op.create_index("uq_hab_manual_cnpj", "hotel_habitaciones_manual",
                    ["region_id", "cnpj"], unique=True,
                    postgresql_where=sa.text("cnpj IS NOT NULL"))
    # Argentina y cualquier fuente sin identificador fiscal.
    op.create_index("uq_hab_manual_nombre", "hotel_habitaciones_manual",
                    ["region_id", "nombre_norm"], unique=True,
                    postgresql_where=sa.text("cnpj IS NULL"))
    # Una fila sin CNPJ y sin nombre no se puede aparear con nada: no tiene sentido guardarla.
    op.create_check_constraint(
        "ck_hab_manual_clave", "hotel_habitaciones_manual",
        "cnpj IS NOT NULL OR nombre_norm IS NOT NULL")


def downgrade() -> None:
    # Las filas sin CNPJ no entran en la PK vieja: se van (son las que la 059 vino a permitir).
    op.execute("DELETE FROM hotel_habitaciones_manual WHERE cnpj IS NULL")
    op.drop_constraint("ck_hab_manual_clave", "hotel_habitaciones_manual", type_="check")
    op.drop_index("uq_hab_manual_nombre", table_name="hotel_habitaciones_manual")
    op.drop_index("uq_hab_manual_cnpj", table_name="hotel_habitaciones_manual")
    op.alter_column("hotel_habitaciones_manual", "cnpj",
                    existing_type=sa.String(), nullable=False)
    op.create_primary_key("hotel_habitaciones_manual_pkey", "hotel_habitaciones_manual",
                          ["region_id", "cnpj"])
    op.drop_column("hotel_habitaciones_manual", "lng")
    op.drop_column("hotel_habitaciones_manual", "lat")
    op.drop_column("hotel_habitaciones_manual", "nombre_norm")
