"""061 — `hotel_min`: dejar de firmar «cadastur» un piso de UF que no salió de Cadastur

`HotelFetcher` aporta `uf_comercio` por hotel y sellaba SIEMPRE `uf_fuente='cadastur'`, aunque
el número no fuera un conteo de habitaciones sino el piso `GREATEST(COALESCE(habitaciones,1),1)`
— o sea "hay un hotel acá", sin saber cuántos cuartos tiene.

Fuera de Brasil eso es directamente falso: **Cadastur es el padrón hotelero brasilero y en
Argentina no existe**, no hay padrón descargable y el pipeline nunca lo consultó ni podía. Los
hoteles argentinos los trae Google/OSM y **ninguno publica habitaciones** (medido: 7 de 7 en
Malvinas), así que las 4 parcelas con hotel salían al CSV declarando un origen imposible.

El daño no es sólo el rótulo. `cadastur` está en `UF_FUENTES_PROTEGIDAS` porque significa
"conteo oficial exacto": el piso quedaba blindado con la autoridad de un registro estatal que
nunca se tocó, y ninguna fuente mejor podía corregirlo después. Es el mismo modo de falla que
`'overture'` → `'poi'`, ya documentado en `precedencia.py`.

Esta migración **no cambia ningún número**: sólo corrige el sello de las filas donde el aporte
del hotel es un piso y no un conteo. Aplica el mismo criterio que el agente a partir de ahora
(exacto = todos los hoteles de la parcela con `habitaciones` y `habitaciones_fuente` en
cadastur/osm/manual). Vale para los dos países: en Brasil también hay hoteles sin UHs.

Revision ID: 061
Revises: 060
Create Date: 2026-08-14
"""

from typing import Sequence, Union

from alembic import op

revision: str = "061"
down_revision: Union[str, None] = "060"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Parcelas selladas 'cadastur' donde NO todos los hoteles vinculados traen conteo real.
_PISOS = """
    SELECT p.parcela_id FROM parcelas p
    WHERE p.uf_fuente = 'cadastur'
      AND NOT COALESCE((
            SELECT bool_and(h.habitaciones IS NOT NULL
                            AND COALESCE(h.habitaciones_fuente,'')
                                IN ('cadastur','osm','manual'))
            FROM hoteles h
            WHERE h.parcela_id = p.parcela_id AND NOT h.cerrado_def), false)
"""


def upgrade() -> None:
    op.execute(f"""
        UPDATE parcelas SET uf_fuente = 'hotel_min'
        WHERE parcela_id IN ({_PISOS})
    """)
    # El `uso_fuente` acompaña, pero sólo donde lo puso este agente: si el operador lo fijó
    # a mano (`manual`) o lo puso otra fuente, no se toca.
    op.execute("""
        UPDATE parcelas SET uso_fuente = 'hotel_min'
        WHERE uf_fuente = 'hotel_min' AND uso_fuente = 'cadastur'
    """)


def downgrade() -> None:
    op.execute("UPDATE parcelas SET uso_fuente = 'cadastur' WHERE uso_fuente = 'hotel_min'")
    op.execute("UPDATE parcelas SET uf_fuente = 'cadastur' WHERE uf_fuente = 'hotel_min'")
