"""071 — perfiles de nomenclatura del DXF: cómo se rotula cada tipo de unidad

El DXF sale con el estándar del cliente de Brasil: `R`/`C`/`E` en el rótulo (`350R`, `25R-2`,
`S/NE`), `V`/`C`/`E` en el resumen (`2V 1C`), `VAZ`/`VAZIO` para el baldío y las capas
`SDU_RES`/`SDU_COM`/`SDU_ESP`/`SDU_VAZ`/`MDU_*`. Eso queda como está —es el perfil «Estándar BL
(Brasil)», que no se guarda acá porque es el código—, y un cliente con otra nomenclatura tiene un
perfil propio que el operador arma desde la web y elige al bajar el DXF (Jaz, 8-oct-2026).

`config` guarda, por tipo (vivienda, comercio, especial, baldío), la letra del rótulo, la del
resumen y las capas. Se aplica sobre el DXF ya generado: el generador no cambia.

Revision ID: 071
Revises: 070
Create Date: 2026-10-08
"""

from typing import Sequence, Union

from alembic import op

revision: str = "071"
down_revision: Union[str, None] = "070"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS dxf_nomenclatura_perfil (
            perfil_id      serial PRIMARY KEY,
            nombre         varchar(80) NOT NULL UNIQUE,
            config         jsonb NOT NULL,
            creado_at      timestamp NOT NULL DEFAULT now(),
            actualizado_at timestamp NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dxf_nomenclatura_perfil")
