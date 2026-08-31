"""063 — ejes_calle: el eje de cada calle CON su nombre, sacado de OpenStreetMap

**El problema que resuelve.** El rótulo de calle del plano se venía ubicando por deducción:
desde el frente de una cara de manzana, caminando hacia afuera hasta encontrar la manzana de
enfrente, y el punto medio se tomaba como eje de calzada. Funciona en la cuadra regular y
falla en todo lo demás — esquinas, avenidas curvas, manzanas de borde —, y el nombre termina
cayendo adentro de una manzana, encima de los lotes.

La tabla `logradouros` (IBGE) tiene los ejes pero **no los nombres**: 3.522 tramos con sólo
23 `nome_logradouro` distintos, el resto `NULL`. Sirve para rotar un texto, no para saber qué
calle es. Y en Argentina directamente no hay nada: los ejes salen de un PCA sobre las
parcelas, que son rectas de mínimos cuadrados que atraviesan manzanas enteras.

**OpenStreetMap sí trae las dos cosas**, geometría y `name`, gratis y sin API paga (ver
[[no-usar-apis-pagas]]). Con el eje real y su nombre, el rótulo se apoya sobre la calzada de
la calle que corresponde en vez de deducirla.

**Una fila por tramo, sin coser.** Overpass devuelve la vía partida en tramos y así se
guarda: coserlos es trabajo del export, que además tiene que recortar contra la hoja. La
clave de reemplazo es `fuente` (`OSM_<region_id>`), igual que en `mapa_base_cliente`: se
borra y se recarga sin tocar nada más.

Carga: `scripts/cargar_calles_osm.py <survey_id>`.

Revision ID: 063
Revises: 062
Create Date: 2026-08-27
"""

from typing import Sequence, Union

from alembic import op

revision: str = "063"
down_revision: Union[str, None] = "062"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS ejes_calle (
            id          BIGSERIAL PRIMARY KEY,
            fuente      TEXT NOT NULL,
            region_id   TEXT,
            nombre      TEXT NOT NULL,
            tipo_via    TEXT,
            geometry    geometry(LineString, 4326) NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_ejes_calle_geom "
               "ON ejes_calle USING GIST (geometry)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ejes_calle_fuente "
               "ON ejes_calle (fuente)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_ejes_calle_fuente")
    op.execute("DROP INDEX IF EXISTS ix_ejes_calle_geom")
    op.execute("DROP TABLE IF EXISTS ejes_calle")
