"""056 — parcelas.uf_catastro: el conteo crudo del catastro, separado de la vivienda

**El problema.** ARBA carto devuelve **cuántas** subparcelas tiene el lote pero **no el
destino de cada una** (el campo `sp` es el número de subparcela, no el uso).
`arba_carto_fetcher` cargaba ese total entero en `uf_vivienda` y marcaba `residencial`.
Medido en Malvinas (ago-2026): **1.875 de 1.974 parcelas con exactamente "1 vivienda"**
puesta por default, incluido el **shopping Terrazas de Mayo** (131.620 m², 32 comercios,
*1 vivienda*) y el lote del McDonald's de Illia 30.

Encima, cuando `OverturePlacesFetcher` encontraba comercios, **sumaba encima**
(`unidades_funcionales_estimadas = uf_vivienda + comercios`), justo al revés de lo que
`UsoClassifier` documenta desde el principio: *"Lo que confirma se DESCUENTA del total de
ARBA, no se suma encima"*. Dos rutas del mismo pipeline haciendo lo contrario.

**Por qué hace falta una columna nueva y no alcanza con restar sobre `uf_vivienda`.** El
descuento tiene que ser idempotente: `uf_vivienda = uf_vivienda - comercios` da un resultado
distinto en cada corrida (arba−n, arba−2n, …). Guardando el conteo crudo aparte, el
descuento siempre se calcula desde la misma base y re-correr el agente converge.

**Sólo aplica donde el catastro NO declara destino.** La escribe `arba_carto_fetcher`; en
Brasil queda **NULL** porque el BCI sí publica el uso de cada unidad, y ahí descontar estaría
mal. Los consumidores tratan `uf_catastro IS NULL` como "seguí con el comportamiento viejo".

Backfill: hoy nadie descontó todavía, así que en las parcelas de `fuente_parcela='arba_carto'`
el `uf_vivienda` actual **es** el conteo crudo y se puede copiar tal cual. Se excluyen las
corregidas a mano (`uf_fuente='manual'`), donde el número ya es una decisión humana sobre
vivienda y no el crudo del catastro.

Revision ID: 056
Revises: 055
Create Date: 2026-08-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "056"
down_revision: Union[str, None] = "055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("parcelas", sa.Column("uf_catastro", sa.Integer(), nullable=True))
    op.execute("""
        UPDATE parcelas SET uf_catastro = uf_vivienda
         WHERE fuente_parcela = 'arba_carto'
           AND uf_vivienda IS NOT NULL
           AND COALESCE(uf_fuente, '') <> 'manual'
    """)


def downgrade() -> None:
    op.drop_column("parcelas", "uf_catastro")
