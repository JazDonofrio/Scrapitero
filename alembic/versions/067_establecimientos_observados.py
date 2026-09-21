"""067 — negocios que se VEN hoy en cada lote (censo 2022 + Overture), sólo para el nombre

El nombre del inmueble del CSV del cliente (`DSC_NOME_DO_IMOVEL`) salía del complemento de la
ficha del BCI, que conserva el negocio que había cuando se registró el lote. Detectado por el
operador el 16-sep-2026: Ponce de Arruda 962 salía «ESTACIONAMENTO AEROPARKING» y ahí hoy está
la locadora Loca10 (el Aero Park se mudó al 1034). Medido en VAZ049: de 36 nombres comparables,
10 ya no estaban y 1 lo exportábamos aunque la propia ficha dijera «(FECHADO)».

Esta tabla guarda lo que las fuentes gratuitas ven HOY: el CNEFE del censo 2022 (IBGE, cada
establecimiento con nombre y coordenada relevada en campo) y Overture Places. Es una tabla
PROPIA y no `comercios` ni `establecimientos_poi` a propósito: esas dos alimentan `uf_comercio`,
`uso_principal` y el sello de categoría, y estas filas no tienen que mover ninguna cantidad.
Sólo sirven para decir cómo se llama lo que está en el lote.

`vinculo` dice cómo se llegó a la parcela: `numero` (el lote de esa calle con el número de puerta
que declara el censo), `adentro` (el punto cae en el lote) o `cercania` (cae en la vereda y se
toma el lote construido más cercano).

Revision ID: 067
Revises: 066
Create Date: 2026-09-16
"""

from typing import Sequence, Union

from alembic import op

revision: str = "067"
down_revision: Union[str, None] = "066"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS establecimientos_observados (
            id               uuid PRIMARY KEY,
            region_id        varchar(50) NOT NULL REFERENCES regions(region_id) ON DELETE CASCADE,
            survey_id        uuid REFERENCES surveys(survey_id) ON DELETE SET NULL,
            fuente           varchar(20) NOT NULL,
            fuente_id        text NOT NULL,
            nombre           text NOT NULL,
            categoria_fuente text,
            direccion        text,
            calle_declarada  text,
            numero_declarado text,
            geom             geometry(Point, 4326) NOT NULL,
            parcela_id       uuid REFERENCES parcelas(parcela_id) ON DELETE SET NULL,
            vinculo          varchar(12),
            fetched_at       timestamp NOT NULL DEFAULT now(),
            UNIQUE (region_id, fuente, fuente_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_estab_obs_parcela "
               "ON establecimientos_observados (parcela_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_estab_obs_geom "
               "ON establecimientos_observados USING gist (geom)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS establecimientos_observados")
