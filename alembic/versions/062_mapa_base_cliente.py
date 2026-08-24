"""062 — mapa_base_cliente: las manzanas y cordones que dibuja el cliente, en la DB

**Para qué.** El entregable DXF del cliente (`plano ejemplo de entrega.dwg`) se dibuja
**sobre su propio mapa base**: las capas `QUADRA` (manzana) y `MEIOFIO` (cordón) no las
producimos nosotros, vienen del MUB municipal que ellos mantienen. Si el plano que
entregamos usa nuestros polígonos de catastro en vez de los suyos, las manzanas no calzan
contra el resto de sus planos y el archivo no sirve para empalmar.

**Por qué en PostGIS y no un archivo en el repo.** El MUB de Várzea Grande son 19.831
polilíneas (~48 × 47 km, la ciudad entera) y cada entrega es **una célula** de ~0,6 km².
Con la geometría en una tabla con índice GiST, el export recorta con un `ST_Intersects`
contra el bbox del relevamiento y toca sólo lo que entra en la hoja. Un DXF de 14 MB
versionado en git, además de pesar, habría que parsearlo entero en cada corrida.

**Qué se guarda: LINESTRING, no POLYGON.** Las polilíneas del cliente no son polígonos
válidos garantizados —hay manzanas abiertas y cordones que son tramos sueltos— y forzarlas
a polígono las rompe o las descarta. Se guarda la geometría tal cual, con `cerrada` para no
perder el dato de si el original venía cerrado: el export necesita ese flag para volver a
escribir la LWPOLYLINE con `close=True` y que el dibujo salga igual al de ellos.

**4326 y no el UTM de origen.** El MUB viene en SIRGAS 2000 / UTM 21S (EPSG:31981,
verificado contra la ubicación real de Várzea Grande). Se guarda en 4326 como todo el resto
del esquema, para que cruce directo contra `parcelas.geometry` sin reproyectar en cada
consulta; el export lo devuelve a UTM al escribir. El ida y vuelta es una transformación
exacta en doble precisión — no es un redondeo a grados.

**`fuente` es la clave de reemplazo.** Cuando el cliente mande un MUB actualizado se borra
por `fuente` y se recarga, sin tocar el resto. Carga: `scripts/cargar_mapa_base.py`.

Revision ID: 062
Revises: 061
Create Date: 2026-08-18
"""

from typing import Sequence, Union

from alembic import op

revision: str = "062"
down_revision: Union[str, None] = "061"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS mapa_base_cliente (
            id          BIGSERIAL PRIMARY KEY,
            fuente      TEXT NOT NULL,
            capa        TEXT NOT NULL,
            cerrada     BOOLEAN NOT NULL DEFAULT FALSE,
            geometry    geometry(LineString, 4326) NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_mapa_base_geom "
               "ON mapa_base_cliente USING GIST (geometry)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_mapa_base_fuente_capa "
               "ON mapa_base_cliente (fuente, capa)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_mapa_base_fuente_capa")
    op.execute("DROP INDEX IF EXISTS ix_mapa_base_geom")
    op.execute("DROP TABLE IF EXISTS mapa_base_cliente")
