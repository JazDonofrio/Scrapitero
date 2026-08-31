"""064 — fichas_cliente: dónde puso el cliente cada ficha en su plano

**Para qué.** Con `mapa_base_cliente` ya calcamos sus manzanas y sus lotes, pero la ficha
—el bloque `SDU`/`MDU` con el rótulo— la seguíamos colocando nosotros, centrada en el lote.
En la calle común la diferencia es de centímetros, pero en las avenidas con lote profundo se
va lejos: medido contra su propio plano, en la **Av. Gov. João Ponce de Arruda** nuestras
fichas quedaban a 16,9 m del eje de la calle y las suyas a 40,5 m — **23,6 m corridas**; en
**Filinto Müller** 13,8 m, en **Ulisses Pompeu de Campos** 10,2 m y en **Santos Dumont**
10,1 m. Jaz lo vio como "los HP están desplazados", y tiene razón.

Guardando el punto y la rotación de cada ficha suya, el plano sale con la ficha **donde él
la puso** y con nuestros datos adentro. Sus 645 fichas y nuestras 645 cruzan 1 a 1 por
rótulo, así que el anclaje es directo.

**Punto y no polígono.** El bloque se inserta en un punto con una rotación; no hay más
geometría que guardar. La rotación va en grados, como la escribe el DXF.

**`etiqueta` es la clave de cruce**, no un id: es el texto visible del rótulo
(`N_C1_3_TP_QT` en el SDU, `NUMERO` en el MDU) — "374R", "486R-2", "(418)C-2", "VAZ". Puede
repetirse, así que el cruce se desempata por cercanía.

**`fuente` es la clave de reemplazo**, igual que en `mapa_base_cliente`: cuando el cliente
mande un plano nuevo se borra por `fuente` y se recarga. Carga:
`scripts/cargar_base_dwg.py`.

Revision ID: 064
Revises: 063
Create Date: 2026-08-27
"""

from typing import Sequence, Union

from alembic import op

revision: str = "064"
down_revision: Union[str, None] = "063"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS fichas_cliente (
            id          BIGSERIAL PRIMARY KEY,
            fuente      TEXT NOT NULL,
            capa_dwg    TEXT NOT NULL,
            etiqueta    TEXT NOT NULL,
            rotacion    DOUBLE PRECISION NOT NULL DEFAULT 0,
            geometry    geometry(Point, 4326) NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_fichas_cliente_geom "
               "ON fichas_cliente USING GIST (geometry)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_fichas_cliente_fuente "
               "ON fichas_cliente (fuente, etiqueta)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_fichas_cliente_fuente")
    op.execute("DROP INDEX IF EXISTS ix_fichas_cliente_geom")
    op.execute("DROP TABLE IF EXISTS fichas_cliente")
