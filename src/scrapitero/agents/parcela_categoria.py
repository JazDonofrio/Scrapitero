"""ParcelaCategoria — aterriza los establecimientos del CNPJ sobre las parcelas relevadas.

Para una región: toma los establecimientos geocodificados de `receita_estabelecimentos`
(categoría R/C/E + descripción), los ubica dentro de cada parcela (`ST_Contains`) y **sella
la parcela** con `categoria_uso` / `descripcion_uso` (migración 029). Una parcela con varios
establecimientos lista todas sus descripciones; la categoría es la de mayor "peso"
(E-Especial > C-Comercial > R-Residencial).

Idempotente: resetea los sellos previos de fuente `receita_cnae` en la región antes de
re-aplicar. La capa de detalle (qué establecimientos caen en cada parcela) queda en
`receita_estabelecimentos` (con sus lat/lng), no se duplica.
"""

from __future__ import annotations

from typing import Optional

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.baseline_geocoder import _tg
from scrapitero.db.engine import get_engine


class ParcelaCategoriaInput(BaseModel):
    region_id: str


class ParcelaCategoriaOutput(BaseModel):
    ok: bool = True
    error: Optional[str] = None
    region_id: str = ""
    parcelas_selladas: int = 0
    por_categoria: dict = {}
    por_descripcion: dict = {}


@agent_run
def run(input: ParcelaCategoriaInput) -> ParcelaCategoriaOutput:
    engine = get_engine()
    out = ParcelaCategoriaOutput(region_id=input.region_id)

    with engine.begin() as conn:
        # 0) reset de sellos previos de esta fuente en la región (idempotencia)
        conn.execute(text("""
            UPDATE parcelas SET categoria_uso=NULL, descripcion_uso=NULL, categoria_uso_fuente=NULL
            WHERE region_id=:rid AND categoria_uso_fuente='receita_cnae'
        """), {"rid": input.region_id})

        # 1) link ST_Contains (acotando los establecimientos al bbox de las parcelas de la
        #    región para no escanear toda la UF) + sello agregado por parcela
        res = conn.execute(text("""
            WITH bb AS (
                SELECT ST_Extent(geometry) AS box FROM parcelas
                WHERE region_id=:rid AND geometry IS NOT NULL
            ),
            e AS (
                SELECT re.categoria, re.descripcion,
                       ST_SetSRID(ST_MakePoint(re.lng, re.lat), 4326) AS geom
                FROM receita_estabelecimentos re, bb
                WHERE re.lat IS NOT NULL AND re.categoria IS NOT NULL
                  AND ST_SetSRID(ST_MakePoint(re.lng, re.lat), 4326) && bb.box
                UNION ALL
                -- POIs no-CNPJ (shoppings de OSM/Google), por región
                SELECT poi.categoria, poi.descripcion,
                       ST_SetSRID(ST_MakePoint(poi.lng, poi.lat), 4326) AS geom
                FROM establecimientos_poi poi
                WHERE poi.region_id = :rid AND poi.categoria IS NOT NULL
            ),
            hit AS (
                SELECT p.parcela_id,
                       string_agg(DISTINCT e.descripcion, ', ' ORDER BY e.descripcion) AS descripciones,
                       MAX(CASE e.categoria WHEN 'E' THEN 3 WHEN 'C' THEN 2 WHEN 'R' THEN 1 ELSE 0 END) AS catrank
                FROM parcelas p JOIN e
                    -- tolerancia de borde (5 m): un punto de geocoding/tag puede caer
                    -- unos metros afuera del polígono real y ST_Contains (contención
                    -- estricta) nunca lo cuenta, aunque el establecimiento esté
                    -- claramente pegado a esa parcela
                    ON (ST_Contains(p.geometry, e.geom)
                        OR ST_DWithin(p.geometry::geography, e.geom::geography, 5))
                WHERE p.region_id=:rid AND p.geometry IS NOT NULL
                GROUP BY p.parcela_id
            )
            UPDATE parcelas p SET
                categoria_uso = CASE hit.catrank WHEN 3 THEN 'E' WHEN 2 THEN 'C' WHEN 1 THEN 'R' END,
                descripcion_uso = hit.descripciones,
                categoria_uso_fuente = 'receita_cnae'
            FROM hit WHERE p.parcela_id = hit.parcela_id
            RETURNING p.categoria_uso, p.descripcion_uso
        """), {"rid": input.region_id}).fetchall()

        # Piso mínimo de UF para shoppings: ni Receita (el CNAE de administración de
        # propiedades no distingue locales) ni OSM/Google traen la cantidad de locales de
        # un shopping real, así que BCI/UnidadesEstimator lo dejan con la UF genérica del
        # edificio (con frecuencia 1, muy por debajo de la realidad de un mall con decenas
        # de locales). No hay de dónde sacar el conteo REAL gratis — esto NO lo resuelve,
        # solo evita el 0/1 evidentemente incorrecto para una parcela con actividad
        # comercial confirmada. `uf_fuente='shopping_min'` deja explícito que es un piso,
        # no un conteo exacto (no se pisa un conteo real ya mayor).
        conn.execute(text("""
            UPDATE parcelas SET
                uf_comercio = 1,
                unidades_funcionales_estimadas = COALESCE(uf_vivienda, 0) + 1,
                uf_fuente = 'shopping_min'
            WHERE region_id=:rid AND descripcion_uso ILIKE '%SHOPPING%'
              AND COALESCE(uf_comercio, 0) < 1
        """), {"rid": input.region_id})

    out.parcelas_selladas = len(res)
    for cat, desc in res:
        out.por_categoria[cat or "?"] = out.por_categoria.get(cat or "?", 0) + 1
        for d in (desc or "").split(", "):
            if d:
                out.por_descripcion[d] = out.por_descripcion.get(d, 0) + 1
    out.por_descripcion = dict(sorted(out.por_descripcion.items(), key=lambda x: -x[1]))
    _tg(f"🏷️ <b>Categorías sobre parcelas</b> ({input.region_id}): "
        f"{out.parcelas_selladas} parcelas selladas {out.por_categoria}.")
    logger.info(f"ParcelaCategoria {input.region_id}: {out.parcelas_selladas} selladas "
                f"{out.por_categoria}")
    return out
