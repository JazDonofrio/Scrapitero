"""033 — hotel_habitaciones_llm_cache (reusar respuestas de Gemini, ahorrar costo)

HotelHabitacionesLLM le pregunta a Gemini (pago, con grounding de Google Search) cuántas
habitaciones tiene cada hotel sin dato. El resultado se escribe en `hoteles.habitaciones`,
pero esa tabla se **borra y reconstruye** en cada re-corte del botón 🏨 (HotelFetcher) →
el valor del LLM se pierde y se vuelve a pagar. Peor: los hoteles que el LLM NO encontró
quedan `habitaciones IS NULL` y se re-consultan en **cada** corrida.

Este caché guarda la respuesta del LLM **por hotel** (CNPJ, o nombre normalizado + ciudad
cuando no hay CNPJ — los de Google/OSM no traen CNPJ), incluyendo los **"no encontrado"**,
para no re-pagarle a Gemini por el mismo hotel. Tiene `created_at` para un TTL (un hotel
sin dato hoy puede aparecer en la web más adelante → se re-consulta pasado el TTL). Es el
análogo "automático" de `hotel_habitaciones_manual` (mig. 030, carga humana por CNPJ).

Revision ID: 033
Revises: 032
Create Date: 2026-06-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "033"
down_revision: Union[str, None] = "032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hotel_habitaciones_llm_cache",
        # clave = "cnpj:<cnpj>"  ó  "nom:<nombre_norm>|<ciudad_norm>" (sin CNPJ)
        sa.Column("clave", sa.Text, primary_key=True),
        sa.Column("habitaciones", sa.Integer, nullable=True),   # NULL = consultado, no encontrado
        sa.Column("encontrado", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("nombre", sa.Text, nullable=True),            # referencia legible
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("hotel_habitaciones_llm_cache")
