"""069 — domicilios del censo 2022 por lote: el control que le faltaba al conteo de viviendas

El CNEFE que ya baja `EstablecimientosObservados` trae, además de los establecimientos con
nombre, **un registro por vivienda** (`COD_ESPECIE=1`), con la coordenada que el censista tomó
en la puerta. Hasta ahora se leían y se tiraban: el agente sólo guarda lo que tiene nombre,
porque nació para resolver el `DSC_NOME_DO_IMOVEL` del entregable.

Por qué hace falta guardarlos: **la cantidad de viviendas de cada lote sale de una sola fuente
—la ficha del BCI— y nada la controla.** Medido en VAZ049 el 18-sep-2026: de 458 lotes, sólo
128 tenían un punto del censo adentro para comparar (83 coincidían, 45 no) y 289 no se
contrastaban con nada. Todos los errores de conteo que aparecieron esta semana son del mismo
tipo y los destapó esta comparación a mano: las vilas de Pedrossian 363 (12 → 14), João Libânio
97 (12 → 7), Filinto 326 (10 → 3) y Da Liberdade 109 (24 → 18).

La tabla es sólo el insumo: quien la usa es `IncidenciasReporter`, que levanta una tarjeta
`censo_viviendas` cuando la ficha y el censo no coinciden. **No cambia ninguna cantidad.**

Se guarda por `(region_id, cod_unico)` —el identificador del domicilio en el CNEFE— para que
recargar la región sea idempotente. `parcela_id` se recalcula en cada corrida con las mismas
tres reglas que los establecimientos (número de puerta, adentro del lote, cercanía).

Revision ID: 069
Revises: 068
Create Date: 2026-09-20
"""

from typing import Sequence, Union

from alembic import op

revision: str = "069"
down_revision: Union[str, None] = "068"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS censo_domicilios (
            id           uuid PRIMARY KEY,
            region_id    varchar(50) NOT NULL REFERENCES regions(region_id) ON DELETE CASCADE,
            survey_id    uuid REFERENCES surveys(survey_id) ON DELETE SET NULL,
            cod_unico    varchar NOT NULL,
            especie      varchar(2),          -- 1 domicílio particular · 2 domicílio coletivo
            complemento  varchar,             -- "CASA 2", "QUITINETE 7", "FUNDOS": cómo lo anotó el censista
            calle_declarada varchar,
            numero_declarado varchar,
            geom         geometry(Point, 4326) NOT NULL,
            parcela_id   uuid REFERENCES parcelas(parcela_id) ON DELETE SET NULL,
            vinculo      varchar(12),         -- numero | adentro | cercania
            fetched_at   timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_censo_domicilios_region_cod UNIQUE (region_id, cod_unico)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_censo_dom_geom ON censo_domicilios USING gist (geom)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_censo_dom_parcela ON censo_domicilios (parcela_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_censo_dom_region ON censo_domicilios (region_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS censo_domicilios")
