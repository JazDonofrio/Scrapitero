"""070 — el especial deja de ser el lote entero: una parcela puede tener vivienda Y especial

Hasta ahora «especial» era un ESTADO del lote (`uso_principal='equipamiento'`, con las unidades
guardadas en `uf_comercio`). Con eso un lote es R **o** E, nunca las dos cosas, y los lotes con
la casa adelante y la escuela o el gimnasio atrás no se podían escribir: o se perdía la vivienda,
o el especial salía rotulado como comercio.

Que existe el caso lo dice el propio cliente. En el DWG que devolvió el 2-sep hay 56 lotes con
DOS fichas adentro (una `C` y una `R`), las 56 con el mismo número y colocadas una detrás de la
otra hacia el fondo del lote (mediana 14,9 m sobre el eje largo y 0,4 m sobre el frente). Y
cuando el especial tiene número propio, él parte el lote en dos (nº 910, 26 y 11: la ficha `E` y
la `R` caen en lotes vecinos distintos). Criterio de Jaz (22-sep-2026): la residencia va normal
con sus pisos y el especial se agrega detrás, con su número si es otro.

Medido en VAZ049: 4 lotes con vivienda y etiqueta de categoría E, más Jaime Benevides 190 (la
Escola Raio de Luz, que declaran el censo 2022 y Overture) y João Libânio 644 (Acqua Fitness,
que Overture ve adentro del lote).

**Los hoteles y el shopping NO se migran:** siguen con `uso_principal='equipamiento'` y sus
unidades en `uf_comercio`, como los dejó la decisión del 18-sep (opción A). Esta columna es para
el especial que CONVIVE con otro uso en el mismo lote.

Revision ID: 070
Revises: 069
Create Date: 2026-09-22
"""

from typing import Sequence, Union

from alembic import op

revision: str = "070"
down_revision: Union[str, None] = "069"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE parcelas
            ADD COLUMN IF NOT EXISTS uf_especial      integer,
            ADD COLUMN IF NOT EXISTS especial_tipo    varchar(64),
            ADD COLUMN IF NOT EXISTS especial_numero  varchar(32)
    """)
    op.execute("""
        COMMENT ON COLUMN parcelas.uf_especial IS
        'Unidades de la etiqueta E que conviven con otro uso en el lote (escuela, iglesia, consultorio). '
        'Normalmente 1: el especial existe o no existe. El hotel y el shopping NO usan esta columna: '
        'son el lote entero (uso_principal=equipamiento).'
    """)
    op.execute("""
        COMMENT ON COLUMN parcelas.especial_tipo IS
        'Etiqueta de la tabla del cliente para esa unidad (ESCOLA PARTICULAR, INSTITUICAO ESPORTIVA…). '
        'Va aparte de parcela_tipo_manual, que rotula el lote y acá sigue diciendo el uso principal.'
    """)
    op.execute("""
        COMMENT ON COLUMN parcelas.especial_numero IS
        'Número de puerta propio del especial, cuando difiere del lote. NULL = usa el del lote.'
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE parcelas
            DROP COLUMN IF EXISTS uf_especial,
            DROP COLUMN IF EXISTS especial_tipo,
            DROP COLUMN IF EXISTS especial_numero
    """)
