"""058 — parcelas.huella_m2: los m² construidos que ve el satélite, materializados

**El bug.** `_tipo_edificacion` (web/app.py) decide "¿hay algo construido acá?" mirando
`area_m2_construida` **o** `uf_vivienda`. Las dos señales fallan a la vez en Argentina:
ARBA no publica área construida (NULL en las 2.059 parcelas de Malvinas) y una parcela
puramente comercial tiene `uf_vivienda=0`. Resultado: sale **LOTE VAZIO**.

Medido el 13-ago-2026 sobre Malvinas + Hurlingham: **59 parcelas rotuladas LOTE BALDÍO,
52 de ellas con huella satelital real**. Caso que lo destapó: *José María Márquez 1245*
—4.085 m² de terreno con 5 edificios adentro que suman ~1.216 m²— salía «🏢 LOTE BALDÍO»
y justo debajo «Com: ≈ 1», o sea contradiciéndose sola en el mismo popup. El mismo modo de
falla que la mig. 057: la etiqueta unificada afirmando algo que el resto de la ficha niega.

**Por qué una columna y no un EXISTS en cada consulta.** El criterio correcto es el mismo
que usan las guardas de `OverturePlacesFetcher`: solape parcela↔footprint ≥ 25 m². Calcularlo
al vuelo cuesta ~700 ms por relevamiento (medido en Malvinas, 2.059 parcelas × 11.987
huellas) y `_tipo_edificacion` se llama desde el mapa, el panel, el DXF y el CSV. Materializado
es un `COALESCE` sobre una columna: cero costo, y encima tapa un hueco real del entregable
argentino —ARBA no da área construida y acá queda una medida satelital de ella—.

**NULL ≠ 0, a propósito.** Es la misma distinción que `_link_to_parcelas` documenta para sus
guardas: *"no hay edificio" y "no se bajaron los edificios" no son lo mismo*.

  · `NULL` → el relevamiento no tiene footprints cargados. Nadie puede afirmar nada.
  · `0`    → sí se bajaron y esta parcela no tiene ni una construcción. Es un baldío de verdad.

Un consumidor que trate el NULL como 0 vuelve a rotular baldío medio país; por eso la regla
nueva pregunta `> 0`, que es falso para los dos pero sólo cambia el veredicto junto al resto
de las señales (área del catastro, UF de vivienda, UF de comercio).

**Backfill acá y no en el agente**: es una derivación pura de datos que ya están en la base
(a diferencia de la 057, donde el vínculo sólo lo sabe el fetcher). Corre en <1 s sobre los
4 relevamientos con footprints y evita tener que re-bajar los edificios para arreglar la
etiqueta de un relevamiento ya entregado.

Revision ID: 058
Revises: 057
Create Date: 2026-08-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "058"
down_revision: Union[str, None] = "057"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("parcelas", sa.Column("huella_m2", sa.Float(), nullable=True))

    # Suma del solape parcela↔footprint. `ST_Union` antes de medir: dos huellas de la misma
    # fuente pueden pisarse (un galpón digitalizado en dos piezas) y sumar los solapes por
    # separado inflaría el área. El umbral de 25 m² es por huella y descarta el roce de
    # digitalización del edificio del vecino, igual que las guardas de Overture.
    op.execute("""
        UPDATE parcelas p SET huella_m2 = s.m2
        FROM (
            SELECT p2.parcela_id,
                   ST_Area(ST_Union(ST_Intersection(p2.geometry, f.footprint))::geography) AS m2
            FROM parcelas p2
            JOIN footprints_revision f
              ON f.survey_id = p2.survey_id
             AND ST_Intersects(p2.geometry, f.footprint)
            WHERE p2.geometry IS NOT NULL
              AND ST_Area(ST_Intersection(p2.geometry, f.footprint)::geography) >= 25
            GROUP BY p2.parcela_id
        ) s
        WHERE p.parcela_id = s.parcela_id
    """)

    # El 0 explícito es la mitad informativa del backfill: sin esto, un baldío de verdad
    # queda NULL y es indistinguible de un relevamiento sin footprints.
    op.execute("""
        UPDATE parcelas p SET huella_m2 = 0
         WHERE p.huella_m2 IS NULL
           AND EXISTS (SELECT 1 FROM footprints_revision f WHERE f.survey_id = p.survey_id)
    """)


def downgrade() -> None:
    op.drop_column("parcelas", "huella_m2")
