"""066 — índice para buscar una parcela por su centroide, que es como se la identifica al re-correr

`ARBACadastralFetcher` no puede deduplicar por `cca_code` —hay inscripciones repartidas en más
de un polígono— así que la identidad de una parcela entre corridas es **su coordenada**: por
cada parcela que baja, busca si ya existe una con el mismo centroide (±0,00005°).

Esa búsqueda estaba escrita como `ABS(centroid_lat - :lat) < 0.00005`, y **`ABS()` impide usar
un índice**: Postgres resolvía cada una escaneando todas las parcelas del survey. Medido en
Merlo con 42.025 parcelas cargadas, `EXPLAIN ANALYZE` decía «Rows Removed by Filter: 42025» y
20 ms por consulta. El costo es **cuadrático** —cada parcela nueva agranda lo que hay que
escanear en la siguiente—, así que una zona de 160.000 parcelas no es cuatro veces más lenta
que una de 40.000 sino dieciséis, con un backend de Postgres al 90% de CPU de forma sostenida.
Se notó desde afuera: la web lenta.

Con este índice y la consulta reescrita a `BETWEEN` (mismo significado, pero indexable), la
misma búsqueda pasa a `Index Scan` y de **20 ms a 0,022 ms**.

⚠ Se crea SIN `CONCURRENTLY` a propósito. Se intentó con él, para no bloquear la web, y el
índice quedó **inválido** (`indisvalid=false`) porque competía con el fetcher que estaba
escribiendo — un índice inválido no se usa y no avisa. Sobre esta tabla la creación tarda
décimas de segundo, así que el bloqueo es irrelevante y la garantía de que quede válido vale
más.

Revision ID: 066
Revises: 065
Create Date: 2026-09-15
"""

from typing import Sequence, Union

from alembic import op

revision: str = "066"
down_revision: Union[str, None] = "065"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_parcelas_survey_centroide
            ON parcelas (survey_id, centroid_lat, centroid_lng)
    """)
    # Sin estadísticas frescas el planner ignora un índice recién creado: verificado acá, el
    # `EXPLAIN` seguía eligiendo el scan por `survey_id` hasta correr esto.
    op.execute("ANALYZE parcelas")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_parcelas_survey_centroide")
