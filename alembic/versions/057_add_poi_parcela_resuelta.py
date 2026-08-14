"""057 — establecimientos_poi: a qué parcela pertenece el POI, ya resuelto

**El bug.** `ParcelaCategoria` aterriza las etiquetas de la taxonomía sobre la parcela con un
`ST_Contains` (más 5 m de tolerancia) contra la **coordenada cruda del POI**, sin mirar
`comercios.parcela_id`. O sea: todas las guardas que deciden a qué lote pertenece un comercio
—la de huella (`8ea8564`) y la de número (ago-2026)— corrigen `comercios` y **no llegan a la
etiqueta**, que se vuelve a aterrizar sola sobre el lote equivocado.

Medido en Malvinas (12-ago-2026): **38 parcelas con etiqueta de comercio y `uf_comercio=0`**,
o sea internamente contradictorias — el popup dice «🏢 RESTAURANTE» y abajo «Com: 0». Casos:

  · **«Calle Juan» → LANCHONETE**, que viene del **Burger King** cuyo comercio ya está en el
    lote del shopping. Es el fantasma que la guarda de huella dio por corregido en agosto:
    se arregló `uf_comercio` y la etiqueta siguió saliendo al CSV del cliente.
  · Varias enganchan por la **tolerancia de 5 m** a la parcela VECINA mientras el comercio
    está bien puesto en la de al lado (Peluquería Mujer Bonita etiqueta Darragueira 1020 y
    su comercio está en la 1012).
  · Varias etiquetan un lote cuyo comercio la guarda de huella dejó **sin parcela** por no
    tener una sola construcción: el error del Burger King entrando por la puerta de atrás.

**La solución.** Los fetchers de POI sellan `establecimientos_poi` DESPUÉS de correr
`_link_to_parcelas`, así que en ese momento ya saben la parcela buena: se guarda en
`parcela_id` y se marca `vinculo_resuelto`. `ParcelaCategoria` usa ese vínculo cuando existe.

`vinculo_resuelto` hace falta además de `parcela_id` para distinguir dos NULL distintos:
  · **no resuelto** (`false`) — el POI no viene de un fetcher con guardas (los shoppings de
    `ShoppingFetcher`, que no tienen fila en `comercios`): sigue aterrizando por geometría.
  · **resuelto y sin parcela** (`true` + NULL) — una guarda lo desvinculó a propósito, porque
    el lote no tiene ninguna construcción. Ese NO debe aterrizar en ningún lado. Sin la
    bandera, el fallback geométrico lo volvería a poner justo donde la guarda lo sacó.

Revision ID: 057
Revises: 056
Create Date: 2026-08-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "057"
down_revision: Union[str, None] = "056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("establecimientos_poi",
                  sa.Column("parcela_id", postgresql.UUID(as_uuid=False), nullable=True))
    op.add_column("establecimientos_poi",
                  sa.Column("vinculo_resuelto", sa.Boolean(),
                            nullable=False, server_default=sa.text("false")))
    op.create_index("ix_estab_poi_parcela", "establecimientos_poi", ["parcela_id"])
    # Sin backfill a propósito: el vínculo lo tiene que escribir el fetcher, que es el único
    # que sabe qué decidieron las guardas. Hasta que se re-corran, `vinculo_resuelto=false`
    # deja todo con el comportamiento viejo (geométrico) y nada se rompe.


def downgrade() -> None:
    op.drop_index("ix_estab_poi_parcela", table_name="establecimientos_poi")
    op.drop_column("establecimientos_poi", "vinculo_resuelto")
    op.drop_column("establecimientos_poi", "parcela_id")
