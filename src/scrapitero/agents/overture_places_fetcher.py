"""OverturePlacesFetcher — comercios y equipamiento desde Overture Maps Places (GRATIS).

Qué resuelve: fuera de Brasil no existe un padrón fiscal descargable (ARCA sólo expone
consulta por CUIT con clave fiscal), así que `ReceitaEstabFetcher` no tiene equivalente y
la única fuente de "qué hay en cada parcela" era **Google Places**, que se cobra por
request (~USD 0,032) y en el flujo PBA se usaba sólo para responder "¿hay comercio sí/no?"
sin guardar un nombre. Overture publica sus POIs en **GeoParquet sobre S3 público**, se
consulta con **DuckDB sin credenciales ni token** —el mismo patrón que `FootprintFetcher`
usa para Open Buildings— y trae nombre, categoría, dirección y confianza.

Medido en Hurlingham (0,7 km²): **299 POIs, el 100% con dirección** de calle y número.

Licencia de las fuentes que componen el dataset: **CDLA-Permissive-2.0** (Meta, Microsoft)
y **Apache-2.0** (Foursquare) ⇒ uso comercial permitido.

Escribe en dos lugares, con roles distintos (mismo reparto que Google Places + Shopping):
  - **`comercios`** (`source='overture'`) → nombre y ubicación para el mapa/CSV, y el
    conteo real de `uf_comercio` por parcela (`uf_fuente='overture'`).
  - **`establecimientos_poi`** → sólo los que mapean a una etiqueta de la taxonomía del
    cliente (ESCOLA, HOSPITAL, SHOPPING, SUPERMERCADO…), para que `ParcelaCategoria` los
    aterrice y el "tipo de edificación" de la parcela sea específico en vez de genérico.

Notas de la fuente, verificadas:
  - Se lee **`basic_category`**, no `categories`: esta última está deprecada y se elimina
    en el release de septiembre 2026. Si el release no la tuviera, se cae a `categories`.
  - **`operating_status` viene NULL** en toda la zona medida ⇒ NO sirve como señal de
    abierto/cerrado. No se usa para filtrar, para no borrar comercios que sí existen.
  - El bucket **no permite listar releases** (`glob` devuelve vacío), así que la versión es
    un parámetro con default conocido: si Overture publica una nueva, se pasa `release`.

Idempotente: upsert por `(region_id, place_id)` en `comercios` y borrado+reinserto de los
POI de fuente `overture` de la región.
"""

from __future__ import annotations

import uuid
from typing import Optional

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.direccion_norm import (
    normalizar_calle, normalizar_numero, separar_numero)
from scrapitero.agents.google_places_fetcher import _load_zone
from scrapitero.agents.osm_building_fetcher import _bbox_from_db
from scrapitero.agents.precedencia import UF_FUENTES_POI, UF_FUENTES_PROTEGIDAS
from scrapitero.db.engine import get_engine

# Fuentes de `comercios` que son POIs de negocios y por lo tanto suman `uf_comercio`.
# Overture es la base y OSM completa lo que Overture no publica (medido en Malvinas: 32
# negocios que sólo estaban en OSM). La deduplicación entre las dos la hace
# `OsmPoiFetcher._descartar_duplicados` ANTES de insertar, así contar filas es contar
# negocios. Vive acá —y no en cada agente— por la lección de `precedencia.py`.
FUENTES_POI = "('overture','osm')"

_OVERTURE_BUCKET = "s3://overturemaps-us-west-2/release"
_RELEASE_DEFAULT = "2026-07-22.0"

# Solape mínimo (m²) para dar por construida una parcela al aplicar la guarda de huella.
# Mismo umbral que usa `IncidenciasReporter` para no contar un roce de digitalización.
_HUELLA_MIN_M2 = 25.0

# ── basic_category de Overture → taxonomía del cliente (categoria R/C/E, etiqueta) ──────
# La etiqueta se guarda SIEMPRE en portugués, que es la forma canónica del proyecto; la web
# la traduce al salir según el país (`_tipo_localizado`). Sólo se mapea lo que tiene
# equivalente CLARO: el resto queda como comercio genérico, sin inventar una etiqueta.
_CATEGORIA_MAP: dict[str, tuple[str, str]] = {
    # — gastronomía —
    "restaurant":                  ("C", "RESTAURANTE"),
    "casual_eatery":               ("C", "LANCHONETE"),
    "food_service":                ("C", "LANCHONETE"),
    "fast_food_restaurant":        ("C", "LANCHONETE"),
    "cafe":                        ("C", "LANCHONETE"),
    "coffee_shop":                 ("C", "LANCHONETE"),
    "bakery":                      ("C", "PADARIA"),
    "bar":                         ("C", "BAR"),
    "pub":                         ("C", "BAR"),
    "dance_club":                  ("C", "CASA NOTURNA"),
    "night_club":                  ("C", "CASA NOTURNA"),
    # — comercio de barrio —
    "supermarket":                 ("E", "SUPERMERCADO"),
    "grocery_store":               ("E", "SUPERMERCADO"),
    "farmers_market":              ("E", "SUPERMERCADO"),
    # — servicios profesionales —
    "attorney_or_law_firm":        ("C", "ESCRITÓRIO DE SERVICOS"),
    "professional_service":        ("C", "ESCRITÓRIO DE SERVICOS"),
    "b2b_office_and_professional_service": ("C", "ESCRITÓRIO DE SERVICOS"),
    "design_service":              ("C", "ESCRITÓRIO DE SERVICOS"),
    "accounting_service":          ("C", "ESCRITÓRIO DE SERVICOS"),
    "real_estate_service":         ("C", "IMOBILIÁRIA"),
    "housing_or_property_service": ("C", "IMOBILIÁRIA"),
    "bank_or_credit_union":        ("C", "INSTITUIÇÃO FINANCEIRA"),
    "financial_service":           ("C", "INSTITUIÇÃO FINANCEIRA"),
    # — automotor —
    "automotive_service":          ("C", "OFICINA"),
    "auto_repair_shop":            ("C", "OFICINA"),
    "auto_dealer":                 ("C", "AGÊNCIA DE AUTOMOVEIS"),
    "gas_station":                 ("E", "POSTO DE GASOLINA"),
    # — industria —
    "manufacturer":                ("C", "INDÚSTRIA"),
    # — salud (público/privado se afina por el nombre, ver `_afinar_publico`) —
    "hospital":                    ("E", "HOSPITAL"),
    "specialized_health_care":     ("E", "MÉDICO / HOSPITALAR"),
    "specialized_medical_facility": ("E", "MÉDICO / HOSPITALAR"),
    "medical_service":             ("E", "MÉDICO / HOSPITALAR"),
    "diagnostics_imaging_or_lab_service": ("E", "MÉDICO / HOSPITALAR"),
    "dental_clinic":               ("E", "CONSULTÓRIO PARTICULAR"),
    "vision_or_eye_care_clinic":   ("E", "CONSULTÓRIO PARTICULAR"),
    # — educación —
    "elementary_school":           ("E", "ESCOLA"),
    "high_school":                 ("E", "ESCOLA"),
    "school":                      ("E", "ESCOLA"),
    "specialty_school":            ("E", "ESCOLA"),
    "place_of_learning":           ("E", "ESCOLA"),
    "preschool":                   ("E", "CRECHE"),
    "childcare_service":           ("E", "CRECHE"),
    "college_university":          ("E", "UNIVERSIDADE/FACULDADE"),
    # — otros equipamientos —
    "shopping_mall":               ("E", "SHOPPING"),
    "lodging":                     ("E", "HOTEL"),
    "hotel":                       ("E", "HOTEL"),
    "motel":                       ("E", "MOTEL"),
    "gym":                         ("E", "INSTITUICAO ESPORTIVA"),
    "fitness_studio":              ("E", "INSTITUICAO ESPORTIVA"),
    "sport_or_fitness_facility":   ("E", "INSTITUICAO ESPORTIVA"),
    "sport_or_recreation_club":    ("E", "INSTITUICAO ESPORTIVA"),
    "government_office":           ("E", "ÓRGÃO PÚBLICO"),
    "parking":                     ("E", "ESTACIONAMENTO"),
    "laundry_service":             ("C", "SERVICOS"),
    "personal_or_beauty_service":  ("C", "SERVICOS"),
    "home_service":                ("C", "SERVICOS"),
    "animal_or_pet_service":       ("C", "SERVICOS"),
    "travel_service":              ("C", "SERVICOS"),
    "media_service":               ("C", "SERVICOS"),
    "event_or_party_service":      ("C", "BUFFET"),
}

# Categorías que NO son una unidad funcional de comercio: no suman `uf_comercio`.
# Mismo criterio que el `EXCLUDE_TYPES` de Google Places (plazas, transporte, monumentos).
_NO_COMERCIO: frozenset[str] = frozenset({
    "park", "historic_site", "train_station", "bus_station", "airport",
    "ground_transport_facility_or_service", "campground", "cemetery",
    "monument", "plaza", "beach", "forest", "landmark_and_historical_building",
})

# Pistas de nombre para separar público de privado (la fuente no lo declara).
_PISTAS_PUBLICO = ("municipal", "provincial", "nacional", "publico", "público",
                   "estatal", "del estado", "fiscal")


class OvertureInput(BaseModel):
    region_id: str
    survey_id: str
    release: str = _RELEASE_DEFAULT
    bbox_buffer_deg: float = 0.001
    min_confidence: float = 0.0     # Overture publica `confidence` 0-1; 0 = sin filtro
    set_uso: bool = True            # marcar uso_principal comercial/mixto como GooglePlaces
    aportar_uf: bool = True         # escribir uf_comercio = conteo real de comercios
    # Guarda de huella (ver `_link_to_parcelas`): un POI que cae en un lote sin construcción
    # se reasigna al lote construido más cercano dentro de este radio, y si no hay ninguno
    # se deja sin parcela. `exigir_huella=False` vuelve al comportamiento anterior.
    exigir_huella: bool = True
    reasignar_max_m: float = 40.0
    # Guarda de número (ver `candidatos_numero_ajeno`): el POI declara en su ficha un número
    # de puerta que es de otra parcela construida a menos de `numero_max_m`. Sólo se mueve
    # solo cuando la calle coincide y el salto es de `salto_min`+ números — una cuadra
    # argentina son 100, así que 300 es un salto que un punto corrido no puede explicar.
    # Los casos ambiguos van al panel como `poi_numero_ajeno`, no se tocan acá.
    exigir_numero: bool = True
    numero_max_m: float = 60.0
    salto_min: int = 300


class OvertureOutput(BaseModel):
    ok: bool
    pois_encontrados: int = 0
    comercios_guardados: int = 0
    vinculados_a_parcela: int = 0
    pois_taxonomia: int = 0          # los que mapean a una etiqueta del cliente
    parcelas_con_comercio: int = 0
    total_uf_comercio: int = 0
    parcelas_uso_actualizado: int = 0
    pois_reasignados_por_huella: int = 0   # el punto caía en un lote vacío; se movió al vecino
    pois_sin_edificio: int = 0             # ni el lote ni un vecino tienen construcción
    pois_reasignados_por_numero: int = 0   # su ficha declara el número de otra parcela
    parcelas_uf_limpiada: int = 0          # perdieron la UF de un comercio que ya no está
    release: Optional[str] = None
    bbox_usado: Optional[str] = None
    error: Optional[str] = None


# ── Consulta a Overture (DuckDB sobre S3 público) ─────────────────────────────

def _consultar_overture(bbox: tuple, release: str, min_confidence: float) -> list[dict]:
    """POIs de Overture dentro del bbox. Devuelve dicts ya normalizados."""
    import duckdb

    south, west, north, east = bbox
    url = f"{_OVERTURE_BUCKET}/{release}/theme=places/type=place/*"

    con = duckdb.connect()
    con.execute("INSTALL httpfs"); con.execute("LOAD httpfs")
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")   # ST_X/ST_Y/ST_Centroid
    con.execute("SET s3_region='us-west-2'")
    con.execute("SET s3_url_style='path'")

    # `basic_category` es la propiedad que reemplaza a `categories` (deprecada, se elimina
    # en el release de 2026-09). Se detecta cuál existe para no romper con ninguna versión.
    try:
        cols = {c[0] for c in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{url}') LIMIT 1").fetchall()}
    except Exception as e:
        raise RuntimeError(f"no se pudo leer el release {release!r} de Overture: {e}") from e
    cat_expr = "basic_category" if "basic_category" in cols else "categories.primary"

    rows = con.execute(
        f"""
        SELECT id,
               names.primary                AS nombre,
               {cat_expr}                   AS categoria,
               confidence,
               ST_X(ST_Centroid(geometry))  AS lng,
               ST_Y(ST_Centroid(geometry))  AS lat,
               addresses[1].freeform        AS direccion,
               operating_status             AS estado
        FROM read_parquet('{url}')
        WHERE bbox.xmin BETWEEN ? AND ?
          AND bbox.ymin BETWEEN ? AND ?
          AND COALESCE(confidence, 0) >= ?
        """,
        [west, east, south, north, min_confidence],
    ).fetchall()

    out = []
    for pid, nombre, categoria, confidence, lng, lat, direccion, estado in rows:
        if lat is None or lng is None:
            continue
        out.append({
            "id": str(pid), "nombre": nombre, "categoria": categoria,
            "confidence": float(confidence) if confidence is not None else None,
            "lat": float(lat), "lng": float(lng),
            "direccion": direccion, "estado": estado,
        })
    return out


def _clasificar(categoria: Optional[str], nombre: Optional[str]) -> Optional[tuple[str, str]]:
    """`basic_category` → (categoria R/C/E, etiqueta de la taxonomía). None si no mapea."""
    if not categoria:
        return None
    par = _CATEGORIA_MAP.get(categoria.strip().lower())
    if not par:
        return None
    cat, desc = par
    return cat, _afinar_publico(desc, nombre)


def _afinar_publico(desc: str, nombre: Optional[str]) -> str:
    """La taxonomía separa público de particular, pero la fuente no lo declara: se deduce
    del nombre ("Hospital Municipal de Hurlingham" → HOSPITAL PÚBLICO). Sin pista, se deja
    la variante particular, que es la mayoritaria."""
    if desc not in ("HOSPITAL", "ESCOLA"):
        return desc
    n = (nombre or "").lower()
    publico = any(p in n for p in _PISTAS_PUBLICO)
    if desc == "HOSPITAL":
        return "HOSPITAL PÚBLICO" if publico else "HOSPITAL PARTICULAR"
    return "ESCOLA PÚBLICA" if publico else "ESCOLA PARTICULAR"


# ── Persistencia ──────────────────────────────────────────────────────────────

def _upsert_comercios(region_id: str, survey_id: str, pois: list[dict]) -> int:
    """Upsert por (region_id, place_id), igual que GooglePlacesFetcher."""
    if not pois:
        return 0
    engine = get_engine()
    with engine.begin() as conn:
        for p in pois:
            conn.execute(text("""
                INSERT INTO comercios
                    (comercio_id, survey_id, region_id, place_id, nombre, rubro,
                     tipos, business_status, location, source)
                VALUES
                    (:cid, :sid, :rid, :pid, :nombre, :rubro, :tipos, :bs,
                     ST_SetSRID(ST_MakePoint(:lng, :lat), 4326), 'overture')
                ON CONFLICT (region_id, place_id) DO UPDATE SET
                    survey_id = EXCLUDED.survey_id,
                    nombre = EXCLUDED.nombre,
                    rubro = EXCLUDED.rubro,
                    tipos = EXCLUDED.tipos,
                    business_status = EXCLUDED.business_status,
                    location = EXCLUDED.location,
                    source = EXCLUDED.source
            """), {
                "cid": str(uuid.uuid4()), "sid": survey_id, "rid": region_id,
                "pid": p["id"], "nombre": p.get("nombre"),
                "rubro": p.get("categoria"),
                # la dirección de Overture es el dato diferencial: se conserva para poder
                # aparear por dirección además de por geometría
                "tipos": p.get("direccion"),
                "bs": p.get("estado"),
                "lat": p["lat"], "lng": p["lng"],
            })
    return len(pois)


def _misma_calle(a: Optional[str], b: Optional[str]) -> bool:
    """¿Dos rótulos nombran la misma calle? Comparación deliberadamente ESTRICTA.

    Base: `normalizar_calle`, que ya colapsa "Av. Pres. Arturo Umberto Illia" y "Avenida
    Presidente Arturo Umberto Illia" (tipo de vía y título a su forma corta).

    Se agrega **una sola** tolerancia: que los tokens de un nombre sean subconjunto de los
    del otro, para el patrón "al catastro le falta el nombre de pila" — `José Darragueira`
    ⊃ `Darragueira`, `F. Senillosa` ⊃ `Senillosa`. Con dos condiciones:

      · lo que se descarta tiene que ser **alfabético**. Un número en el nombre es parte de
        la identidad de la calle, no un prefijo omitible: sin esto `9 de Julio` ⊃ `Julio`,
        que son dos calles distintas.
      · el nombre corto tiene que aportar un token de **4+ letras**, o un apellido de tres
        haría match con media ciudad (`Av. Gral. Paz` ⊅ `Paz`).

    NO se usa `nucleo_calle`: ese borra los títulos honoríficos de las DOS puntas y fusiona
    vías distintas. Acá igual queda un residuo —`Gral. Roca` ⊃ `Roca` matchea— pero la calle
    es sólo uno de tres filtros: el número de puerta tiene que coincidir exacto y la parcela
    candidata estar a ≤60 m, así que un homónimo lejano no llega.
    """
    na, nb = normalizar_calle(a), normalizar_calle(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    chico, grande = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if not chico < grande:
        return False
    return (all(t.isalpha() for t in grande - chico)
            and any(len(t) >= 4 and t.isalpha() for t in chico))


def candidatos_numero_ajeno(conn, region_id: str, survey_id: str,
                            max_m: float = 60.0, salto_min: int = 300,
                            source: Optional[str] = None) -> list[dict]:
    """POIs cuya ficha declara un número de puerta que es de OTRA parcela cercana.

    El punto de Overture viene corrido unos metros; la guarda de huella
    (`_link_to_parcelas`) sólo rescata al que cayó en un lote sin construir. Cuando el lote
    equivocado **sí** tiene edificios, el único testigo que queda es la dirección que el
    propio POI declara. Caso que lo destapó (Malvinas, ago-2026): «Questa Pizza» dice *Av.
    Pres. Arturo Umberto Illia 3770* y estaba parada en la parcela «Illia 30» —la del
    McDonald's—, a 7 m del lote 3770 de 131.620 m², que es el shopping.

    Devuelve un dict por caso con `accion`:

      · **`mover`** — la calle declarada es la misma donde el POI está parado, pero el
        número salta `salto_min`+ (Illia 30 vs Illia 3770). Un punto corrido unos metros no
        puede explicar ese salto: una cuadra argentina son 100 números. Es el único caso que
        se corrige solo.
      · **`revisar`** — hay candidata, pero la evidencia no alcanza. Dos sabores, y los dos
        van al panel como `poi_numero_ajeno` en vez de tocar el dato:
          - `esquina`: la calle declarada NO es la de la parcela donde está. Parece el
            señalón más fuerte y es el más traicionero: en Malvinas, 3 de los 4 casos tenían
            una parcela de la calle declarada pegada (≤5 m). Es un lote de esquina, que el
            catastro rotula por una calle y el comercio publicita por la otra — moverlo
            rompería una asignación correcta. Mismo modo de falla que las etiquetas del BCI.
          - `vecino`: misma calle y número contiguo (Artigas 171 declarando Artigas 161, con
            la 161 a 4 m). Ahí no hay forma de saber si el punto está corrido o si el número
            del catastro está mal: moverlo es tirar una moneda y hace bailar filas del CSV
            del cliente sin ganancia verificable.

    Medido en Malvinas: de 137 POIs con dirección, 65 declaran un número distinto al de su
    parcela, 14 tienen una parcela cercana con ese número, y sólo **1** llega a `mover`.
    Los saltos de los casos `vecino` van de 7 a 57; el de Questa Pizza es 3.740 — el umbral
    cae en una banda vacía enorme, no está calibrado contra un caso único.

    Se ignoran los rubros de `_NO_COMERCIO` (plazas, estaciones): no aportan `uf_comercio`,
    así que reasignarlos no cambia el entregable y sólo ensuciaría el panel.

    `source=None` mira todas las fuentes de POI —es lo que quiere el panel de incidencias—;
    el fetcher pasa la suya para no reasignar (ni loguear) los POIs de otro agente.
    """
    filas = conn.execute(text("""
        SELECT c.comercio_id::text, c.place_id, c.nombre, c.tipos, c.rubro, c.source,
               ST_Y(c.location) AS lat, ST_X(c.location) AS lng,
               p.parcela_id::text, p.calle, p.numero
        FROM comercios c JOIN parcelas p ON p.parcela_id = c.parcela_id
        WHERE c.region_id = :rid AND p.survey_id = CAST(:sid AS uuid)
          AND c.tipos IS NOT NULL AND c.tipos <> ''
          AND c.location IS NOT NULL AND p.geometry IS NOT NULL
          AND COALESCE(c.rubro, '') <> ALL(:excluidas)
          -- el CAST no es decorativo: con :src=None, Postgres no puede inferir el tipo del
          -- parámetro y falla con AmbiguousParameter
          AND (CAST(:src AS varchar) IS NULL OR c.source = CAST(:src AS varchar))
    """), {"rid": region_id, "sid": survey_id, "src": source,
           "excluidas": list(_NO_COMERCIO)}).fetchall()

    # Parseo en Python: `separar_numero` ya sabe distinguir la altura del número que es parte
    # del nombre ("Ruta 8 Y 202" → no se lo come como puerta) y descartar el complemento.
    pend: list[dict] = []
    for f in filas:
        calle_dec, num_dec = separar_numero(f.tipos)
        num_dec = normalizar_numero(num_dec)
        if not num_dec or num_dec == normalizar_numero(f.numero):
            continue
        pend.append({"comercio_id": f.comercio_id, "place_id": f.place_id,
                     "nombre": f.nombre, "source": f.source,
                     "direccion_declarada": f.tipos, "calle_declarada": calle_dec,
                     "numero_declarado": num_dec, "lat": f.lat, "lng": f.lng,
                     "parcela_id": f.parcela_id, "calle_actual": f.calle,
                     "numero_actual": f.numero})
    if not pend:
        return []

    # Las 5 parcelas CONSTRUIDAS más cercanas que llevan ese número. Se traen varias y el
    # filtro de calle elige entre ellas: quedarse con la más cercana y recién después mirar
    # la calle descartaría un acierto que estaba segundo.
    casos: list[dict] = []
    for c in pend:
        cands = conn.execute(text("""
            SELECT q.parcela_id::text, q.calle, q.numero,
                   ST_Distance(q.geometry::geography,
                               ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography) AS d
            FROM parcelas q
            WHERE q.survey_id = CAST(:sid AS uuid) AND q.geometry IS NOT NULL
              AND q.parcela_id <> CAST(:pid AS uuid)
              AND ltrim(regexp_replace(COALESCE(q.numero, ''), '\\D', '', 'g'), '0') = :num
              AND ST_DWithin(q.geometry::geography,
                             ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography, :maxm)
              AND EXISTS (
                  SELECT 1 FROM footprints_revision f
                  WHERE f.survey_id = CAST(:sid AS uuid)
                    AND ST_Intersects(q.geometry, f.footprint)
                    AND ST_Area(ST_Intersection(q.geometry, f.footprint)::geography) >= :amin)
            ORDER BY d LIMIT 5
        """), {"sid": survey_id, "pid": c["parcela_id"], "num": c["numero_declarado"],
               "lat": c["lat"], "lng": c["lng"], "maxm": max_m,
               "amin": _HUELLA_MIN_M2}).fetchall()

        q = next((x for x in cands if _misma_calle(c["calle_declarada"], x.calle)), None)
        if q is None:
            # El número coincide pero la calle no: casualidad, no evidencia. En Malvinas cae
            # acá «Virginia Urcelay», que declara Senillosa 2515 y tiene una 2515 a 50 m
            # sobre Dante Alighieri.
            continue

        misma = _misma_calle(c["calle_declarada"], c["calle_actual"])
        salto = None
        num_act = normalizar_numero(c["numero_actual"])
        if misma and num_act:
            salto = abs(int(c["numero_declarado"]) - int(num_act))
        if misma and salto is not None and salto >= salto_min:
            accion, motivo = "mover", "salto"
        else:
            accion, motivo = "revisar", ("vecino" if misma else "esquina")

        casos.append({**c, "accion": accion, "motivo": motivo, "salto": salto,
                      "destino_id": q.parcela_id, "destino_calle": q.calle,
                      "destino_numero": q.numero, "destino_dist_m": round(float(q.d), 1)})
    return casos


def _link_to_parcelas(region_id: str, survey_id: str, exigir_huella: bool = True,
                      reasignar_max_m: float = 40.0,
                      source: str = "overture", exigir_numero: bool = True,
                      numero_max_m: float = 60.0,
                      salto_min: int = 300) -> tuple[int, int, int, int]:
    """Vincula cada comercio de `source` a la parcela que contiene su punto.

    `source` existe para que otros fetchers de POIs (p.ej. `OsmPoiFetcher`, que trae las
    estaciones de servicio que Overture no publica en Argentina) reusen estas guardas en vez
    de copiarlas — la lección de `precedencia.py`: una copia se actualiza y la otra no.

    **Guarda de huella.** El punto de Overture viene corrido unos metros, así que el
    `ST_Contains` puede meter el comercio en el terreno vacío de al lado. Un lote SIN una
    sola construcción no puede tener un comercio adentro: cuando pasa, se le busca al POI
    el lote **construido** más cercano dentro de `reasignar_max_m` y, si no hay ninguno, se
    lo deja sin parcela — el comercio se conserva como POI, pero no le aporta una UF a un
    lote vacío ni lo asciende a `comercial`.

    Caso que lo destapó (Malvinas, ago-2026): un «Burger King» cuya propia ficha dice *BK
    Terrazas de Mayo Shopping* cayó adentro de una plaza de 8.022 m² **sin un solo
    edificio**, a 27 m del lote del shopping. La plaza salió al CSV del cliente como una
    dirección con comercio, con una UF que no existe.

    **Guarda de número** (`exigir_numero`). La de huella sólo rescata al POI que cayó en un
    lote vacío; cuando el lote equivocado también tiene edificios, el testigo es la
    dirección que el POI declara. Ver `candidatos_numero_ajeno`: acá se aplican **sólo** los
    casos `mover` (misma calle, salto de `salto_min`+ números). Los ambiguos —esquinas y
    número contiguo— no se tocan: los levanta `IncidenciasReporter` como `poi_numero_ajeno`.

    Las dos guardas se apoyan en los footprints de `FootprintFetcher`. **Si el relevamiento
    no los tiene cargados, NO se aplican**: "no hay edificio" y "no se bajaron los edificios"
    no son lo mismo, y castigar el segundo caso desvincularía comercios legítimos.

    Devuelve `(vinculados, reasignados, sin_edificio, movidos_por_numero)`.
    """
    engine = get_engine()
    with engine.begin() as conn:
        res = conn.execute(text("""
            UPDATE comercios c SET parcela_id = p.parcela_id
            FROM parcelas p
            WHERE p.region_id = :rid AND c.region_id = :rid
              AND c.source = :src
              AND p.geometry IS NOT NULL AND c.location IS NOT NULL
              AND ST_Contains(p.geometry, c.location)
              AND (c.parcela_id IS NULL OR c.parcela_id <> p.parcela_id)
        """), {"rid": region_id, "src": source})
        vinculados = res.rowcount or 0
        reasignados = huerfanos = por_numero = 0

        if not (exigir_huella or exigir_numero):
            return vinculados, 0, 0, 0

        hay_footprints = conn.execute(text("""
            SELECT EXISTS (SELECT 1 FROM footprints_revision
                            WHERE survey_id = CAST(:sid AS uuid))
        """), {"sid": survey_id}).scalar()
        if not hay_footprints:
            logger.warning("Overture: sin footprints cargados ⇒ no se aplican las guardas "
                           "de huella ni de número (correr FootprintFetcher antes)")
            return vinculados, 0, 0, 0

        sin_construccion = [] if not exigir_huella else conn.execute(text("""
            SELECT c.comercio_id::text
            FROM comercios c JOIN parcelas p ON p.parcela_id = c.parcela_id
            WHERE c.region_id = :rid AND c.source = :src
              AND p.geometry IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM footprints_revision f
                  WHERE f.survey_id = CAST(:sid AS uuid)
                    AND ST_Intersects(p.geometry, f.footprint)
                    AND ST_Area(ST_Intersection(p.geometry, f.footprint)::geography) >= :amin)
        """), {"rid": region_id, "sid": survey_id, "amin": _HUELLA_MIN_M2,
              "src": source}).fetchall()

        for (cid,) in sin_construccion:
            nuevo = conn.execute(text("""
                UPDATE comercios c SET parcela_id = (
                    SELECT p.parcela_id FROM parcelas p
                    WHERE p.region_id = :rid AND p.geometry IS NOT NULL
                      AND ST_DWithin(p.geometry::geography, c.location::geography, :maxm)
                      AND EXISTS (
                          SELECT 1 FROM footprints_revision f
                          WHERE f.survey_id = CAST(:sid AS uuid)
                            AND ST_Intersects(p.geometry, f.footprint)
                            AND ST_Area(ST_Intersection(p.geometry,
                                                        f.footprint)::geography) >= :amin)
                    ORDER BY p.geometry <-> c.location
                    LIMIT 1)
                WHERE c.comercio_id = CAST(:cid AS uuid)
                RETURNING c.parcela_id::text
            """), {"rid": region_id, "sid": survey_id, "cid": cid,
                   "maxm": reasignar_max_m, "amin": _HUELLA_MIN_M2}).scalar()
            if nuevo:
                reasignados += 1
            else:
                huerfanos += 1

        if reasignados or huerfanos:
            logger.info(f"Overture guarda de huella: {reasignados} POIs movidos al lote "
                        f"construido vecino, {huerfanos} sin edificio a la vista")

        # Segunda guarda: el número que el POI declara es de otra parcela. Corre DESPUÉS de
        # la de huella para que trabaje sobre la asignación ya rescatada, y sólo aplica los
        # casos `mover` — el resto los levanta `IncidenciasReporter` como `poi_numero_ajeno`.
        if exigir_numero:
            for caso in candidatos_numero_ajeno(conn, region_id, survey_id,
                                                numero_max_m, salto_min, source=source):
                if caso["accion"] != "mover":
                    continue
                conn.execute(text(
                    "UPDATE comercios SET parcela_id = CAST(:dst AS uuid) "
                    "WHERE comercio_id = CAST(:cid AS uuid)"),
                    {"dst": caso["destino_id"], "cid": caso["comercio_id"]})
                por_numero += 1
                logger.info(
                    f"Overture guarda de número: «{caso['nombre']}» declara "
                    f"{caso['direccion_declarada']!r} y estaba en "
                    f"{caso['calle_actual']} {caso['numero_actual']} ⇒ movido a la parcela "
                    f"{caso['destino_calle']} {caso['destino_numero']} "
                    f"(a {caso['destino_dist_m']} m, salto de {caso['salto']} números)")

        return vinculados, reasignados, huerfanos, por_numero


def _sellar_pois(region_id: str, pois: list[dict]) -> int:
    """Carga en `establecimientos_poi` los POIs con etiqueta de la taxonomía del cliente,
    para que `ParcelaCategoria` los aterrice sobre la parcela. Idempotente por fuente.

    Guarda **el vínculo a parcela que ya resolvió `_link_to_parcelas`** (mig. 057), no la
    coordenada sola. Corre siempre DESPUÉS del vínculo, así que a esta altura `comercios`
    tiene la parcela buena — la que las guardas de huella y de número corrigieron.

    Sin esto, `ParcelaCategoria` re-aterrizaba la etiqueta por geometría cruda y **deshacía
    las guardas por la puerta de atrás**: en Malvinas quedaron 38 parcelas con etiqueta de
    comercio y `uf_comercio=0`, entre ellas la de «Calle Juan» rotulada LANCHONETE por el
    Burger King que ya vivía en el lote del shopping.

    `vinculo_resuelto=True` incluso cuando `parcela_id` queda en NULL: significa "ya se
    decidió, y la decisión fue que no va en ninguna parcela" (la guarda de huella lo sacó de
    un lote sin construcción). Es distinto del NULL de un POI que nunca pasó por el vínculo.
    """
    engine = get_engine()
    n = 0
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM establecimientos_poi "
                          "WHERE region_id = :rid AND fuente = 'overture'"),
                     {"rid": region_id})
        for p in pois:
            par = _clasificar(p.get("categoria"), p.get("nombre"))
            if not par:
                continue
            cat, desc = par
            conn.execute(text("""
                INSERT INTO establecimientos_poi
                    (poi_id, region_id, fuente, categoria, descripcion, nombre, lat, lng,
                     parcela_id, vinculo_resuelto)
                VALUES (:id, :rid, 'overture', :cat, :desc, :nombre, :lat, :lng,
                        -- El CAST no es decorativo: `establecimientos_poi.region_id` es
                        -- `varchar` sin límite (Postgres deduce `text`) y el de `comercios`
                        -- es `varchar(50)`. Con el MISMO parámetro en los dos lugares el
                        -- servidor no puede deducir un tipo único y aborta el INSERT entero
                        -- con «inconsistent types deduced for parameter $2».
                        (SELECT c.parcela_id FROM comercios c
                          WHERE c.region_id = CAST(:rid AS varchar(50))
                            AND c.place_id = :pid), true)
            """), {"id": str(uuid.uuid4()), "rid": region_id, "cat": cat, "desc": desc,
                   "nombre": p.get("nombre"), "lat": p["lat"], "lng": p["lng"],
                   "pid": p["id"]})
            n += 1
    return n


def _agregar_uf(region_id: str, survey_id: str, set_uso: bool) -> tuple[int, int, int, int]:
    """uf_comercio = cantidad de comercios de las fuentes de POI en la parcela.

    Cuenta **todas** las fuentes de `FUENTES_POI` (Overture + OSM), no sólo la propia: una
    parcela con un supermercado que Overture no publica y OSM sí tiene que salir igual con su
    comercio. La deduplicación entre fuentes la hace `OsmPoiFetcher._descartar_duplicados`
    **antes** de insertar, así que acá contar filas es contar negocios.

    Respeta la precedencia de fuentes (ver CLAUDE.md): no pisa `manual` ni las fuentes más
    específicas que ya escribieron ese campo. A diferencia de GooglePlacesFetcher —que
    escribe sin guarda— acá el sello se actualiza en la MISMA sentencia que el valor, para
    que `uf_fuente` no quede mintiendo.
    """
    engine = get_engine()
    with engine.begin() as conn:
        counts = conn.execute(text(f"""
            SELECT c.parcela_id::text, COUNT(*)
            FROM comercios c
            JOIN parcelas p ON p.parcela_id = c.parcela_id
            WHERE c.region_id = :rid AND c.parcela_id IS NOT NULL
              AND c.source IN {FUENTES_POI}
              AND p.survey_id = :sid
              AND COALESCE(c.rubro, '') <> ALL(:excluidas)
            GROUP BY c.parcela_id
        """), {"rid": region_id, "sid": survey_id,
               "excluidas": list(_NO_COMERCIO)}).fetchall()

        parcelas_con = total_uf = uso_upd = 0
        for pid, n in counts:
            n = int(n)
            # La guarda es contra OTRAS fuentes, no contra la propia: sin `UF_FUENTES_POI`
            # el agente se auto-bloqueaba con el sello que él mismo dejó y no podía
            # actualizar su conteo cuando Overture publica un release nuevo.
            res = conn.execute(text(f"""
                UPDATE parcelas SET
                    uf_comercio = :n,
                    -- DESCUENTO, no suma. Donde el catastro dio un conteo SIN destino
                    -- (`uf_catastro`, mig. 056 — ARBA no publica el uso de la subparcela),
                    -- los comercios confirmados no son unidades nuevas: son parte de ese
                    -- mismo total que estaba mal rotulado como vivienda. Es lo que
                    -- `UsoClassifier` documenta desde el principio ("lo que confirma se
                    -- DESCUENTA del total de ARBA, no se suma encima") y esta ruta hacía
                    -- al revés. Sin esto el shopping Terrazas de Mayo salía con
                    -- «1 vivienda + 32 comercios» y el lote del McDonald's con
                    -- «1 vivienda + 2 comercios».
                    uf_vivienda = CASE WHEN uf_catastro IS NULL THEN uf_vivienda
                                       ELSE GREATEST(uf_catastro - :n, 0) END,
                    -- Cuando hay más comercios que unidades declaradas, el que se queda
                    -- corto es el catastro (el shopping es UNA partida con 32 locales):
                    -- el total pasa a ser el conteo real, no el declarado.
                    unidades_funcionales_estimadas =
                        CASE WHEN uf_catastro IS NULL THEN COALESCE(uf_vivienda, 0) + :n
                             ELSE GREATEST(uf_catastro, :n) END,
                    uf_fuente = 'poi'
                WHERE parcela_id = :pid
                  AND (COALESCE(uf_fuente, '') NOT IN {UF_FUENTES_PROTEGIDAS}
                       OR COALESCE(uf_fuente, '') IN {UF_FUENTES_POI})
            """), {"n": n, "pid": pid})
            if res.rowcount:
                parcelas_con += 1
                total_uf += n
            if set_uso:
                # El uso sale de las UF, que es el dato, no del `uso_principal` anterior.
                # Antes el CASE ascendía a 'mixto' sólo si la parcela ya venía marcada
                # 'residencial'; pero fuera de Brasil el catastro no clasifica uso (ARBA no
                # publica el destino de la subparcela), así que llegan en 'sin_datos' y
                # caían en el ELSE → 'comercial', tapando las viviendas que el propio
                # agente acababa de contar. Medido en Hurlingham: 27 de 28 parcelas con
                # vivienda Y comercio quedaron 'comercial', 0 mixtas.
                # Misma regla que el BCI (`bci_parser._parse_bci`): comercio + vivienda →
                # mixto; comercio solo → comercial. Se leen las dos UF de la fila y no se
                # asume que el UPDATE de arriba haya entrado: si la guarda de `uf_fuente` lo
                # bloqueó, el uso tiene que seguir al dato que quedó, no al que quisimos poner.
                r2 = conn.execute(text("""
                    UPDATE parcelas SET
                        uso_principal = CASE
                            WHEN COALESCE(uf_comercio, 0) > 0
                             AND COALESCE(uf_vivienda, 0) > 0 THEN 'mixto'
                            WHEN COALESCE(uf_comercio, 0) > 0 THEN 'comercial'
                            ELSE uso_principal
                        END,
                        uso_fuente = 'poi'
                    WHERE parcela_id = :pid AND COALESCE(uso_fuente, '') <> 'manual'
                """), {"pid": pid})
                uso_upd += r2.rowcount or 0

        # Barrido de las que DEJARON de tener comercios: un release nuevo que ya no publica
        # el POI, o una guarda que lo desvinculó del lote. Sin esto el conteo viejo queda
        # pegado y el sello sigue firmando una UF que ya no tiene nada detrás — el mismo modo
        # de falla que el sello que miente de `precedencia.py`, pero por omisión.
        limpiadas = conn.execute(text(f"""
            UPDATE parcelas p SET
                uf_comercio = 0,
                -- Sin comercios el descuento se DESHACE: la vivienda vuelve al conteo crudo
                -- del catastro. Es la otra mitad de que el descuento sea idempotente — si
                -- acá quedara el valor ya restado, un POI que aparece y desaparece entre dos
                -- releases iría comiéndose una unidad por vuelta.
                uf_vivienda = COALESCE(p.uf_catastro, p.uf_vivienda),
                unidades_funcionales_estimadas =
                    COALESCE(p.uf_catastro, p.uf_vivienda, 0)
            WHERE p.survey_id = :sid AND COALESCE(p.uf_fuente, '') IN {UF_FUENTES_POI}
              AND COALESCE(p.uf_comercio, 0) > 0
              AND NOT EXISTS (
                  SELECT 1 FROM comercios c
                  WHERE c.parcela_id = p.parcela_id AND c.source IN {FUENTES_POI}
                    AND COALESCE(c.rubro, '') <> ALL(:excluidas))
        """), {"sid": survey_id, "excluidas": list(_NO_COMERCIO)}).rowcount or 0

        if set_uso:
            # El uso vuelve a seguir al dato: sin comercios, una parcela con viviendas es
            # residencial y una sin nada queda sin clasificar (NULL), no 'comercial'.
            conn.execute(text(f"""
                UPDATE parcelas p SET
                    uso_principal = CASE WHEN COALESCE(p.uf_vivienda, 0) > 0
                                         THEN 'residencial' ELSE NULL END,
                    uso_fuente = CASE WHEN COALESCE(p.uf_vivienda, 0) > 0
                                      THEN 'poi' ELSE NULL END
                WHERE p.survey_id = :sid AND COALESCE(p.uso_fuente, '') IN {UF_FUENTES_POI}
                  AND COALESCE(p.uf_comercio, 0) = 0
            """), {"sid": survey_id})

    return parcelas_con, total_uf, uso_upd, limpiadas


# ── Entry point ───────────────────────────────────────────────────────────────

@agent_run
def run(input: OvertureInput) -> OvertureOutput:
    bbox, zone = _load_zone(input.region_id, input.survey_id)
    if bbox is None:
        bbox = _bbox_from_db(input.region_id, input.bbox_buffer_deg)
    if bbox is None:
        return OvertureOutput(ok=False, error=(
            "sin zona ni parcelas para derivar el bbox: crear la región con "
            "zone_geojson o correr primero el fetcher de parcelas"))

    try:
        pois = _consultar_overture(bbox, input.release, input.min_confidence)
    except Exception as e:  # noqa: BLE001
        return OvertureOutput(ok=False, release=input.release,
                              error=f"consulta a Overture falló: {e}")

    # Recorte exacto a la zona (el bbox es un rectángulo; la zona puede ser un corredor).
    if zone is not None and not zone.is_empty:
        from shapely.geometry import Point
        pois = [p for p in pois if zone.covers(Point(p["lng"], p["lat"]))]

    if not pois:
        return OvertureOutput(ok=True, release=input.release,
                              bbox_usado=",".join(f"{c}" for c in bbox),
                              error=None)

    guardados = _upsert_comercios(input.region_id, input.survey_id, pois)
    vinculados, reasignados, sin_edificio, por_numero = _link_to_parcelas(
        input.region_id, input.survey_id, input.exigir_huella, input.reasignar_max_m,
        exigir_numero=input.exigir_numero, numero_max_m=input.numero_max_m,
        salto_min=input.salto_min)
    con_taxonomia = _sellar_pois(input.region_id, pois)

    parcelas_con = total_uf = uso_upd = limpiadas = 0
    if input.aportar_uf:
        parcelas_con, total_uf, uso_upd, limpiadas = _agregar_uf(
            input.region_id, input.survey_id, input.set_uso)

    logger.info(
        f"Overture {input.region_id}: {len(pois)} POIs · {vinculados} vinculados · "
        f"{con_taxonomia} con etiqueta del cliente · uf_comercio={total_uf} "
        f"en {parcelas_con} parcelas · guarda de huella: {reasignados} reasignados, "
        f"{sin_edificio} sin edificio · guarda de número: {por_numero} reasignados · "
        f"{limpiadas} parcelas con UF limpiada")

    return OvertureOutput(
        ok=True,
        pois_encontrados=len(pois),
        comercios_guardados=guardados,
        vinculados_a_parcela=vinculados,
        pois_taxonomia=con_taxonomia,
        parcelas_con_comercio=parcelas_con,
        total_uf_comercio=total_uf,
        parcelas_uso_actualizado=uso_upd,
        pois_reasignados_por_huella=reasignados,
        pois_sin_edificio=sin_edificio,
        pois_reasignados_por_numero=por_numero,
        parcelas_uf_limpiada=limpiadas,
        release=input.release,
        bbox_usado=",".join(f"{c}" for c in bbox),
    )
