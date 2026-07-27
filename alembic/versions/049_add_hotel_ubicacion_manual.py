"""049 — corrección manual de la ubicación y dirección de un hotel

Cuando Receita/Cadastur no traen coordenada (`lat IS NULL`), `HotelFetcher` cae al geocoder
externo (Nominatim/Mapbox/Google) sobre la dirección **fiscal**. Cuando esa dirección es rara o
la calle es larga, el punto aterriza lejos y el hotel queda en una parcela ajena — o en ninguna.

Caso que lo motivó: **REAL VILLES HOTEL** (CNPJ 33474110000144). Receita lo declara en
«Filinto Müller 750» sin coordenada; el geocoder externo lo puso a **419 m** del hotel real,
en una cuadra de bancos y estudios jurídicos. El pin de Google Places (`comercios`, rubro=hotel)
lo ubica dentro de la parcela catastral «FILINTO MULLER 750» — la misma que el `CatastroGeocoder`
resuelve por dirección con confianza 1,0, a **14,4 m**. Es decir: el número era correcto, falló
el geocodificador.

Además el hotel usa dos numeraciones distintas y **ambas son válidas**: el catastro lo numera
**750** y la calle/correo **710** (CEP 78110-302). Por eso la tabla guarda también la dirección:
sin eso, el paso de "dirección oficial por reverse contra el catastro" (que corre justo después
del vínculo a parcela) pisaría el 710 con el 750 del BCI — `bci_pdf` es fuente autoritativa.

- `hotel_ubicacion_manual`: coordenada y/o dirección forzadas por el operador, keyed por
  `(region_id, cnpj)` como el resto de los overrides (mig. 030 habitaciones, mig. 048 cerrado).
  `HotelFetcher` la aplica **antes** de vincular la parcela, así el `ST_Contains` corre sobre la
  coordenada corregida y el `parcela_id` sale bien sin intervención extra.

Revision ID: 049
Revises: 048
Create Date: 2026-07-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "049"
down_revision: Union[str, None] = "048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hotel_ubicacion_manual",
        sa.Column("region_id", sa.String(50), nullable=False),
        sa.Column("cnpj", sa.String(20), nullable=False),
        # lat/lng nullables: se puede corregir solo la dirección sin mover el pin, y viceversa.
        sa.Column("lat", sa.Float),
        sa.Column("lng", sa.Float),
        sa.Column("direccion", sa.Text),
        sa.Column("nota", sa.Text),
        sa.Column("autor", sa.String(60)),
        sa.Column("actualizado_at", sa.DateTime, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("region_id", "cnpj"),
    )


def downgrade() -> None:
    op.drop_table("hotel_ubicacion_manual")
