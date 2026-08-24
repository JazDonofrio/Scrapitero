"""ParcelaCategoria — aterriza los establecimientos del CNPJ sobre las parcelas relevadas.

Para una región: toma los establecimientos geocodificados de `receita_estabelecimentos`
(categoría R/C/E + descripción), los ubica dentro de cada parcela (`ST_Contains`) y **sella
la parcela** con `categoria_uso` / `descripcion_uso` (migración 029). Una parcela con varios
establecimientos lista todas sus descripciones; la categoría es la de mayor "peso"
(E-Especial > C-Comercial > R-Residencial).

Idempotente: resetea los sellos previos de fuente `receita_cnae` en la región antes de
re-aplicar. La capa de detalle (qué establecimientos caen en cada parcela) queda en
`receita_estabelecimentos` (con sus lat/lng), no se duplica.

⚠ **Cómo se aterriza depende de la fuente.** Los POIs que vienen de un fetcher con guardas
(`OverturePlacesFetcher`, `OsmPoiFetcher`) traen en `establecimientos_poi.parcela_id` el
vínculo **ya resuelto** (mig. 057) y se usa ése, no la geometría. Antes se aterrizaba todo
por `ST_Contains` sobre la coordenada cruda, lo que **deshacía las guardas de huella y de
número en silencio**: en Malvinas (ago-2026) quedaron 38 parcelas con etiqueta de comercio y
`uf_comercio=0` —el popup decía «🏢 RESTAURANTE» y abajo «Com: 0»—, entre ellas la de «Calle
Juan» rotulada por el Burger King que ya estaba correctamente en el lote del shopping.
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
                       ST_SetSRID(ST_MakePoint(re.lng, re.lat), 4326) AS geom,
                       NULL::uuid AS parcela_id, false AS resuelto
                FROM receita_estabelecimentos re, bb
                WHERE re.lat IS NOT NULL AND re.categoria IS NOT NULL
                  -- Sólo empresas que siguen en pie. BAIXADA (dio baja) y NULA son cierre
                  -- DEFINITIVO; INAPTA y SUSPENSA no lo son (la empresa sigue abierta, con
                  -- la situação a la vista) — mismo corte que usa `HotelFetcher`. Sin este
                  -- filtro una lanchonete cerrada en 2018 sella el lote igual que una
                  -- farmacia abierta hoy: el sello nació para CLASIFICAR uso de suelo, donde
                  -- eso daba lo mismo, pero hoy es la etiqueta que se ve en el mapa y viaja
                  -- en el plano de entrega. Medido el 20-ago-2026 en VG: 74 de 239 parcelas
                  -- selladas lo estaban sólo por empresas de baja definitiva, y en Av. Couto
                  -- Magalhães 352 se verificó en la calle (el archivo decía LANCHONETE por un
                  -- bar dado de baja; lo que hay es una verdulería que no figura en ninguna
                  -- fuente). BAIXADA es el 49% del dump de MT: el filtro NO es cosmético.
                  AND re.situacao NOT IN ('BAIXADA', 'NULA')
                  AND ST_SetSRID(ST_MakePoint(re.lng, re.lat), 4326) && bb.box
                UNION ALL
                -- POIs no-CNPJ (fetchers de POI, y shoppings de OSM/Google), por región
                SELECT poi.categoria, poi.descripcion,
                       ST_SetSRID(ST_MakePoint(poi.lng, poi.lat), 4326) AS geom,
                       poi.parcela_id, poi.vinculo_resuelto
                FROM establecimientos_poi poi
                WHERE poi.region_id = :rid AND poi.categoria IS NOT NULL
                  -- `vinculo_resuelto` con parcela NULL = una guarda lo desvinculó A
                  -- PROPÓSITO (cayó en un lote sin ninguna construcción). Ese no aterriza
                  -- en ningún lado: si se lo deja pasar, el fallback geométrico de abajo lo
                  -- vuelve a poner justo donde la guarda lo sacó.
                  AND NOT (poi.vinculo_resuelto AND poi.parcela_id IS NULL)
            ),
            hit AS (
                SELECT p.parcela_id,
                       string_agg(DISTINCT e.descripcion, ', ' ORDER BY e.descripcion) AS descripciones,
                       MAX(CASE e.categoria WHEN 'E' THEN 3 WHEN 'C' THEN 2 WHEN 'R' THEN 1 ELSE 0 END) AS catrank
                FROM parcelas p JOIN e
                    ON (CASE WHEN e.resuelto
                        -- Vínculo YA RESUELTO por `_link_to_parcelas` (mig. 057): manda ése
                        -- y NO la geometría. Las guardas de huella y de número corrigen
                        -- `comercios.parcela_id`, y volver a aterrizar por `ST_Contains`
                        -- sobre la coordenada cruda las deshacía en silencio — 38 parcelas
                        -- en Malvinas con etiqueta de comercio y `uf_comercio=0`, entre
                        -- ellas «Calle Juan» rotulada LANCHONETE por el Burger King que ya
                        -- vivía en el lote del shopping.
                        THEN e.parcela_id = p.parcela_id
                        -- Sin vínculo resuelto (Receita, shoppings de `ShoppingFetcher`) se
                        -- aterriza por geometría, con tolerancia de borde de 5 m: un punto
                        -- de geocoding puede caer unos metros afuera del polígono real y
                        -- `ST_Contains` (contención estricta) nunca lo contaría.
                        ELSE ST_Contains(p.geometry, e.geom)
                             OR ST_DWithin(p.geometry::geography, e.geom::geography, 5)
                        END)
                WHERE p.region_id=:rid AND p.geometry IS NOT NULL
                GROUP BY p.parcela_id
            )
            UPDATE parcelas p SET
                categoria_uso = CASE hit.catrank WHEN 3 THEN 'E' WHEN 2 THEN 'C' WHEN 1 THEN 'R' END,
                descripcion_uso = hit.descripciones,
                categoria_uso_fuente = 'receita_cnae'
            FROM hit WHERE p.parcela_id = hit.parcela_id
              -- El reset de arriba respeta el sello `manual` (sólo borra `receita_cnae`) pero
              -- este UPDATE no lo miraba, así que igual lo pisaba: el agregado no filtra por
              -- fuente. Medido el 13-ago-2026 en Malvinas — la parcela del Círculo de
              -- Suboficiales (…3700C), corregida a mano a INSTITUICAO ESPORTIVA después de
              -- verla en campo, volvió a salir LANCHONETE por el buffet del club. Es el
              -- mismo POI que había motivado la corrección.
              AND COALESCE(p.categoria_uso_fuente, '') <> 'manual'
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
        # El mismo piso vale para las ESTACIONES DE SERVICIO que trae `OsmPoiFetcher`: son
        # una unidad comercial confirmada —alguien la ve desde la vereda— sobre un lote al
        # que ni ARBA ni Overture le cuentan comercio. Medido en Malvinas: la YPF de Av.
        # Primera Junta 75 caía en un lote con `uf_comercio=0`. El sello sigue siendo
        # `shopping_min` porque es el mismo concepto (piso de UF por POI de equipamiento, no
        # conteo real) y ya está protegido en `precedencia.py`; renombrarlo obligaría a
        # migrar las filas de Brasil que ya lo tienen puesto.
        conn.execute(text("""
            UPDATE parcelas SET
                uf_comercio = 1,
                unidades_funcionales_estimadas = COALESCE(uf_vivienda, 0) + 1,
                uf_fuente = 'shopping_min'
            WHERE region_id=:rid
              AND (descripcion_uso ILIKE '%SHOPPING%'
                   OR descripcion_uso ILIKE '%POSTO DE GASOLINA%')
              AND COALESCE(uf_comercio, 0) < 1
        """), {"rid": input.region_id})

        # El uso tiene que seguir al piso que acabamos de poner: sin esto la parcela queda
        # con una UF de comercio y `uso_principal='residencial'`, y el operador ve una
        # estación de servicio rotulada como vivienda. Misma regla que el resto del
        # pipeline (comercio + vivienda → mixto; comercio solo → comercial) y misma guarda:
        # `manual` no se pisa nunca.
        conn.execute(text("""
            UPDATE parcelas SET
                uso_principal = CASE WHEN COALESCE(uf_vivienda, 0) > 0
                                     THEN 'mixto' ELSE 'comercial' END,
                uso_fuente = 'shopping_min'
            WHERE region_id=:rid AND COALESCE(uf_fuente, '') = 'shopping_min'
              AND COALESCE(uf_comercio, 0) > 0
              AND COALESCE(uso_fuente, '') <> 'manual'
              AND COALESCE(uso_principal, '') NOT IN ('mixto', 'comercial')
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
