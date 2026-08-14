"""OsmPoiFetcher — POIs de OpenStreetMap que Overture NO publica (GRATIS).

Qué resuelve: Overture es la fuente de comercios fuera de Brasil, pero **tiene rubros que
en Argentina vienen vacíos**. El caso medido: **estaciones de servicio**. En Malvinas
Argentinas —una zona cruzada por la Ruta 8 y la Ruta 202— Overture devolvió **cero**:
ni un `gas_station`, ni un YPF/Shell/Axion/Puma por nombre, en 218 POIs. OSM tiene **tres**
en el mismo bbox, dos adentro de la zona, y una es la que el operador ve desde la vereda
al lado del McDonald's. Un cero así no es un dato, es un agujero
(ver `_try_mirrors`: "el cero hay que ganárselo").

No reemplaza a `OverturePlacesFetcher`: lo **completa**. Por eso los tags son un parámetro
y no una lista fija — cuando aparezca otro rubro vacío se suma acá, no se escribe otro
agente.

Escribe en los mismos dos lugares que Overture, con los mismos roles:
  - **`comercios`** (`source='osm'`) → nombre y punto para el mapa y el CSV.
  - **`establecimientos_poi`** (`fuente='osm_poi'`) → la etiqueta de la taxonomía del
    cliente (POSTO DE GASOLINA → "ESTACIÓN DE SERVICIO" en la web en español), para que
    **`ParcelaCategoria`** la aterrice sobre la parcela y le ponga el **piso de UF=1** si el
    lote no tiene ningún conteo. `fuente='osm_poi'` NO es `'osm'` a propósito:
    `ShoppingFetcher` borra sus POIs por fuente y se los llevaría puestos.

Reusa la **guarda de huella** de `OverturePlacesFetcher._link_to_parcelas` (importada, no
copiada): un POI que cae en un lote sin ninguna construcción se reasigna al lote construido
más cercano y, si no hay, queda sin parcela.

Idempotente: upsert por `(region_id, place_id)` en `comercios` con `place_id='osm:way/123'`,
y borrado+reinserto de los `establecimientos_poi` de fuente `osm_poi` de la región.
"""

from __future__ import annotations

import uuid
from typing import Optional

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.google_places_fetcher import _load_zone
from scrapitero.agents.osm_building_fetcher import _bbox_from_db, _fetch_overpass
from scrapitero.agents.hotel_fetcher import (
    _dist_m, _nombre_contenido, _nombre_fuerte, _norm, _tokens_sig)
from scrapitero.agents.overture_places_fetcher import _agregar_uf, _link_to_parcelas
from scrapitero.db.engine import get_engine

# tag de OSM → (rubro compatible con Overture, categoría R/C/E, etiqueta del cliente).
# El rubro se nombra igual que en Overture para que cualquier lógica que ya filtre por
# rubro (p.ej. `_NO_COMERCIO`) siga valiendo sin enterarse de la fuente.
#
# La etiqueta puede ser **None**: el POI cuenta como comercio (suma `uf_comercio`) pero no
# sella `establecimientos_poi`. Es el mismo criterio que `OverturePlacesFetcher._clasificar`
# —"sólo se mapea lo que tiene equivalente CLARO: el resto queda como comercio genérico, sin
# inventar una etiqueta"—. Las etiquetas salen de `TIPOS_EDIFICACION` (web/app.py), que es el
# contrato del cliente: NO agregar ninguna que no esté en esa lista.
_TAGS: dict[str, tuple[str, str, Optional[str]]] = {
    # — combustible y automotor —
    "amenity=fuel":            ("gas_station", "E", "POSTO DE GASOLINA"),
    "amenity=car_wash":        ("automotive_service", "C", "OFICINA"),
    "shop=car_repair":         ("auto_repair_shop", "C", "OFICINA"),
    "shop=car":                ("auto_dealer", "C", "AGÊNCIA DE AUTOMOVEIS"),
    "shop=car_parts":          ("automotive_service", "C", "COMÉRCIO EM GERAL"),
    "shop=tyres":              ("automotive_service", "C", "OFICINA"),
    "shop=motorcycle":         ("auto_dealer", "C", "AGÊNCIA DE AUTOMOVEIS"),
    # — gastronomía —
    "amenity=restaurant":      ("restaurant", "C", "RESTAURANTE"),
    "amenity=fast_food":       ("fast_food_restaurant", "C", "LANCHONETE"),
    "amenity=cafe":            ("cafe", "C", "LANCHONETE"),
    "amenity=ice_cream":       ("casual_eatery", "C", "LANCHONETE"),
    "amenity=bar":             ("bar", "C", "BAR"),
    "amenity=pub":             ("pub", "C", "BAR"),
    "amenity=nightclub":       ("night_club", "C", "CASA NOTURNA"),
    "shop=bakery":             ("bakery", "C", "PADARIA"),
    "shop=pastry":             ("bakery", "C", "PADARIA"),
    "shop=catering":           ("event_or_party_service", "C", "BUFFET"),
    # — alimentación —
    "shop=supermarket":        ("supermarket", "E", "SUPERMERCADO"),
    "shop=convenience":        ("grocery_store", "C", "COMÉRCIO EM GERAL"),
    "shop=kiosk":              ("grocery_store", "C", "COMÉRCIO EM GERAL"),
    "shop=butcher":            ("grocery_store", "C", "COMÉRCIO EM GERAL"),
    "shop=greengrocer":        ("grocery_store", "C", "COMÉRCIO EM GERAL"),
    "shop=seafood":            ("grocery_store", "C", "COMÉRCIO EM GERAL"),
    "shop=deli":               ("grocery_store", "C", "COMÉRCIO EM GERAL"),
    "shop=spices":             ("grocery_store", "C", "COMÉRCIO EM GERAL"),
    "shop=alcohol":            ("grocery_store", "C", "COMÉRCIO EM GERAL"),
    "shop=wine":               ("grocery_store", "C", "COMÉRCIO EM GERAL"),
    "shop=beverages":          ("grocery_store", "C", "COMÉRCIO EM GERAL"),
    # — salud —
    "amenity=pharmacy":        ("pharmacy", "C", "COMÉRCIO EM GERAL"),
    "amenity=clinic":          ("specialized_health_care", "E", "MÉDICO / HOSPITALAR"),
    "amenity=doctors":         ("medical_service", "E", "CONSULTÓRIO PARTICULAR"),
    "amenity=dentist":         ("dental_clinic", "E", "CONSULTÓRIO PARTICULAR"),
    "amenity=veterinary":      ("animal_or_pet_service", "C", "SERVICOS"),
    "shop=optician":           ("vision_or_eye_care_clinic", "E", "CONSULTÓRIO PARTICULAR"),
    # — servicios personales —
    "shop=hairdresser":        ("personal_or_beauty_service", "C", "SERVICOS"),
    "shop=beauty":             ("personal_or_beauty_service", "C", "SERVICOS"),
    "shop=laundry":            ("laundry_service", "C", "SERVICOS"),
    "shop=dry_cleaning":       ("laundry_service", "C", "SERVICOS"),
    "shop=travel_agency":      ("travel_service", "C", "SERVICOS"),
    "shop=funeral_directors":  ("professional_service", "C", "SERVICOS"),
    "shop=pet":                ("animal_or_pet_service", "C", "SERVICOS"),
    "shop=pet_grooming":       ("animal_or_pet_service", "C", "SERVICOS"),
    "shop=ticket":             ("travel_service", "C", "SERVICOS"),
    "shop=lottery":            ("professional_service", "C", "SERVICOS"),
    # — oficinas y profesionales —
    "amenity=bank":            ("bank_or_credit_union", "C", "INSTITUIÇÃO FINANCEIRA"),
    "office=lawyer":           ("attorney_or_law_firm", "C", "ESCRITÓRIO DE SERVICOS"),
    "office=accountant":       ("accounting_service", "C", "ESCRITÓRIO DE SERVICOS"),
    "office=estate_agent":     ("real_estate_service", "C", "IMOBILIÁRIA"),
    "office=insurance":        ("financial_service", "C", "INSTITUIÇÃO FINANCEIRA"),
    "office=company":          ("professional_service", "C", "ESCRITÓRIO DE SERVICOS"),
    "office=logistics":        ("professional_service", "C", "ESCRITÓRIO DE SERVICOS"),
    "office=research":         ("professional_service", "C", "ESCRITÓRIO DE SERVICOS"),
    "office=yes":              ("professional_service", "C", "ESCRITÓRIO DE SERVICOS"),
    # — construcción, industria y oficios —
    "shop=hardware":           ("home_service", "C", "COMÉRCIO EM GERAL"),
    "shop=doityourself":       ("home_service", "C", "COMÉRCIO EM GERAL"),
    "shop=building_materials": ("home_service", "C", "COMÉRCIO EM GERAL"),
    "shop=paint":              ("home_service", "C", "COMÉRCIO EM GERAL"),
    "shop=bathroom_furnishing": ("home_service", "C", "COMÉRCIO EM GERAL"),
    "shop=trade":              ("home_service", "C", "COMÉRCIO EM GERAL"),
    "craft=window_construction": ("manufacturer", "C", "INDÚSTRIA"),
    "craft=carpenter":         ("manufacturer", "C", "INDÚSTRIA"),
    "craft=metal_construction": ("manufacturer", "C", "INDÚSTRIA"),
    "craft=electrician":       ("home_service", "C", "SERVICOS"),
    "craft=plumber":           ("home_service", "C", "SERVICOS"),
    # — retail general (sin equivalente específico en la taxonomía) —
    "shop=department_store":   ("department_store", "C", "COMÉRCIO EM GERAL"),
    "shop=mall":               ("shopping_mall", "E", "SHOPPING"),
    "shop=clothes":            ("clothing_store", "C", "COMÉRCIO EM GERAL"),
    "shop=shoes":              ("shoe_store", "C", "COMÉRCIO EM GERAL"),
    "shop=jewelry":            ("jewelry_store", "C", "COMÉRCIO EM GERAL"),
    "shop=electronics":        ("electronics_store", "C", "COMÉRCIO EM GERAL"),
    "shop=appliance":          ("electronics_store", "C", "COMÉRCIO EM GERAL"),
    "shop=mobile_phone":       ("electronics_store", "C", "COMÉRCIO EM GERAL"),
    "shop=mobile_phone_accessories": ("electronics_store", "C", "COMÉRCIO EM GERAL"),
    "shop=computer":           ("electronics_store", "C", "COMÉRCIO EM GERAL"),
    "shop=furniture":          ("furniture_store", "C", "COMÉRCIO EM GERAL"),
    "shop=bed":                ("furniture_store", "C", "COMÉRCIO EM GERAL"),
    "shop=florist":            ("store", "C", "COMÉRCIO EM GERAL"),
    "shop=books":              ("store", "C", "COMÉRCIO EM GERAL"),
    "shop=stationery":         ("store", "C", "COMÉRCIO EM GERAL"),
    "shop=toys":               ("store", "C", "COMÉRCIO EM GERAL"),
    "shop=sports":             ("store", "C", "COMÉRCIO EM GERAL"),
    "shop=bicycle":            ("store", "C", "COMÉRCIO EM GERAL"),
    "shop=gift":               ("store", "C", "COMÉRCIO EM GERAL"),
    "shop=variety_store":      ("store", "C", "COMÉRCIO EM GERAL"),
    "shop=fabric":             ("store", "C", "COMÉRCIO EM GERAL"),
    "shop=hifi":               ("electronics_store", "C", "COMÉRCIO EM GERAL"),
    # `shop=yes` es "acá hay un negocio y no sé de qué": cuenta la UF, pero NO se le inventa
    # una etiqueta de la taxonomía (por eso None).
    "shop=yes":                ("store", "C", None),
    "office=government":       ("government_office", "E", "ÓRGÃO PÚBLICO"),
}

# Conjunto por defecto: TODO lo mapeado. Antes el default era sólo `amenity=fuel`, cuando el
# agente existía nada más que para las estaciones de servicio. Hoy es la segunda fuente de
# comercios: medido en Malvinas (ago-2026), Overture no veía 32 negocios que OSM sí —
# Supermercado Luna, Autoservicio Nelly, 3 carnicerías, 4 restaurantes, ferreterías…— y esas
# parcelas salían al CSV como `residencial` con `uf_comercio=0`.
_TAGS_DEFAULT: list[str] = sorted(_TAGS)

_NOMBRE_GENERICO = {"gas_station": "Estación de servicio"}


class OsmPoiInput(BaseModel):
    region_id: str
    survey_id: str
    # Por defecto TODO lo que `_TAGS` sabe mapear (ver `_TAGS_DEFAULT`). Para volver al
    # comportamiento viejo —sólo estaciones de servicio— pasar `tags=["amenity=fuel"]`.
    tags: list[str] = _TAGS_DEFAULT
    timeout_s: float = 90.0
    # No guardar el POI si otra fuente ya trajo el mismo negocio (ver `_descartar_duplicados`).
    dedupe: bool = True
    # Contar `uf_comercio` al terminar, con la misma función que Overture.
    aportar_uf: bool = True
    set_uso: bool = True
    exigir_huella: bool = True
    reasignar_max_m: float = 40.0
    # Misma guarda de número que Overture (ver `candidatos_numero_ajeno`). Las estaciones de
    # servicio de OSM rara vez traen dirección, así que en la práctica casi no dispara; se
    # expone igual para no tener dos fetchers de POI con criterios distintos.
    exigir_numero: bool = True
    numero_max_m: float = 60.0
    salto_min: int = 300


class OsmPoiOutput(BaseModel):
    ok: bool = True
    error: Optional[str] = None
    region_id: str = ""
    pois_encontrados: int = 0        # dentro de la zona, ya recortados
    comercios_guardados: int = 0
    vinculados_a_parcela: int = 0
    pois_reasignados_por_huella: int = 0
    pois_sin_edificio: int = 0
    pois_reasignados_por_numero: int = 0
    duplicados_descartados: int = 0   # el mismo negocio ya lo había traído otra fuente
    pois_taxonomia: int = 0           # los que mapean a una etiqueta del cliente
    parcelas_con_comercio: int = 0
    total_uf_comercio: int = 0
    parcelas_uf_limpiada: int = 0
    por_tag: dict = {}


def _query(tags: list[str], s: float, w: float, n: float, e: float,
           timeout_s: float = 90.0) -> str:
    """Nodos Y ways con cada tag. Las estaciones de servicio suelen ser un `way` (el
    polígono del playón), así que pedir sólo nodos deja la mitad afuera: `out center`
    devuelve el centroide del way y lo trata igual que un punto."""
    bbox = f"({s},{w},{n},{e})"
    # Los tags se AGRUPAN por clave en un solo selector con regex. Con un `node`+`way` por
    # tag, el set completo (~90 tags) son ~180 sentencias y Overpass devuelve 504 — medido:
    # dos mirrors seguidos se cayeron con la forma larga y la agrupada contestó en segundos.
    por_clave: dict[str, list[str]] = {}
    sueltas: list[str] = []
    for t in tags:
        k, _, v = t.partition("=")
        if v:
            por_clave.setdefault(k, []).append(v)
        else:
            sueltas.append(k)

    partes = []
    for k, vals in por_clave.items():
        sel = (f'["{k}"="{vals[0]}"]' if len(vals) == 1
               else '["{}"~"^({})$"]'.format(k, "|".join(sorted(vals))))
        partes.append(f"node{sel}{bbox};")
        partes.append(f"way{sel}{bbox};")
    for k in sueltas:
        partes.append(f'node["{k}"]{bbox};')
        partes.append(f'way["{k}"]{bbox};')
    return f"[out:json][timeout:{int(timeout_s)}];({''.join(partes)});out center tags;"


def _parse(data: dict, tags: list[str]) -> list[dict]:
    pois = []
    for el in (data or {}).get("elements", []):
        t = el.get("tags") or {}
        lat = el.get("lat") if el.get("lat") is not None else (el.get("center") or {}).get("lat")
        lng = el.get("lon") if el.get("lon") is not None else (el.get("center") or {}).get("lon")
        if lat is None or lng is None:
            continue
        # ¿Qué tag de los pedidos matcheó? (un elemento puede traer varios)
        cfg = None
        for tag in tags:
            k, _, v = tag.partition("=")
            if t.get(k) and (not v or t.get(k) == v):
                cfg = _TAGS.get(tag)
                break
        if not cfg:
            continue
        rubro, categoria, etiqueta = cfg
        # Una estación sin `name` igual existe: se la nombra por la marca y, si tampoco,
        # con el genérico. Descartarla por no tener nombre sería perder el dato que falta.
        nombre = (t.get("name") or t.get("brand") or t.get("operator")
                  or _NOMBRE_GENERICO.get(rubro) or rubro)
        direccion = " ".join(x for x in (t.get("addr:street"), t.get("addr:housenumber")) if x)
        pois.append({
            "place_id": f"osm:{el.get('type')}/{el.get('id')}",
            "nombre": nombre, "rubro": rubro, "categoria": categoria,
            "etiqueta": etiqueta, "direccion": direccion or None,
            "lat": float(lat), "lng": float(lng),
        })
    return pois


def _descartar_duplicados(region_id: str, pois: list[dict],
                          dist_nombre_m: float = 600.0,
                          dist_nombre_corto_m: float = 250.0,
                          dist_sin_nombre_m: float = 10.0,
                          dist_contenido_m: float = 25.0) -> tuple[list[dict], int]:
    """Saca de la lista los POIs que ya trajo OTRA fuente, para no contar dos veces la misma
    UF. Devuelve `(los que quedan, cuántos se descartaron)`.

    **Sólo se deduplica por NOMBRE**, nunca por cercanía sola, y eso salió de medirlo: en
    Malvinas hay **21 pares de negocios DISTINTOS a menos de 20 m** —«Il Cappo» a 5 m de
    «Kiosko San Miguel», «Ferretería Bulonera» a 10 m de «Pintureriapanacebo»—, que es lo
    normal en una tira de locales sobre una avenida. Deduplicar por proximidad habría tirado
    21 comercios reales, que es justo lo contrario de para qué se sumó OSM.

    El radio es **escalonado por lo distintivo del nombre**, que es lo que decidieron los
    datos. Los pares con nombre idéntico medidos en Malvinas:

      · `Terrazas de Mayo Shopping` a **175 m** — el mismo shopping: OSM apunta al centro del
        polígono y Overture al POI de una tienda ancla. Es el mismo caso que ya documenta
        `shopping_fetcher` (de ahí que `dist_nombre_m` valga 600, igual que su
        `merge_dist_fuerte_m`: un shopping ocupa una manzana).
      · `Repair Car Shop` a 131 m y `Starbucks` a 124 m — el mismo negocio.
      · `Elizabeth` a **675 m** — dos peluquerías **distintas**. Un nombre de pila no
        identifica un negocio, y por eso un nombre de UN SOLO token significativo se compara
        con el radio corto (`dist_nombre_corto_m`). Con el radio ancho para todos, esta se
        fusionaba con la de la otra punta del barrio.

    Excepción acotada: un POI de OSM **sin nombre** pegado (≤ `dist_sin_nombre_m`) a un
    comercio ya conocido es casi seguro el mismo mal tageado. Son pocos (2 en Malvinas) y se
    prefiere perderlos antes que inflar la UF del entregable.
    """
    if not pois:
        return pois, 0
    engine = get_engine()
    with engine.connect() as conn:
        previos = conn.execute(text("""
            SELECT nombre, ST_Y(location) AS lat, ST_X(location) AS lng
            FROM comercios
            WHERE region_id = :rid AND source <> 'osm' AND location IS NOT NULL
        """), {"rid": region_id}).fetchall()
    if not previos:
        return pois, 0

    quedan, dup = [], 0
    for p in pois:
        nombre = (p.get("nombre") or "").strip()
        # `_NOMBRE_GENERICO` rellena el nombre de una estación sin `name`: para comparar no
        # vale como nombre propio, es una etiqueta que le pusimos nosotros.
        propio = nombre and nombre != _NOMBRE_GENERICO.get(p["rubro"]) and nombre != p["rubro"]
        # Un solo token significativo ("Elizabeth", "Starbucks") identifica mucho menos que
        # varios ("Terrazas de Mayo Shopping"): se le exige que el otro esté cerca.
        radio = dist_nombre_m if len(_tokens_sig(_norm(nombre))) >= 2 else dist_nombre_corto_m
        golpe = None
        for q in previos:
            d = _dist_m(p["lat"], p["lng"], q.lat, q.lng)
            if propio and d <= radio and _nombre_fuerte(nombre, q.nombre):
                golpe = (q.nombre, d); break
            if not propio and d <= dist_sin_nombre_m:
                golpe = (q.nombre, d); break
        # Segunda pasada, sólo si el nombre no matcheó FUERTE con nadie: un núcleo CONTENIDO
        # en el del otro («Sauce Ranch» ⊂ «Parrilla Sauce Ranch», «Textil Obrero» ⊂
        # «Proveeduría Textil Obrero», «Clínica Neuropsiquiátrica San Miguel» ⊂ «Clínica
        # Privada Neuropsiquiátrica Día San Miguel»). Va DESPUÉS y no mezclada en el mismo
        # bucle porque la contención es señal débil y no debe ganarle a una igualdad.
        #
        # Dos condiciones extra, las dos medidas:
        #  · **≤ `dist_contenido_m`** (25 m). Con el radio ancho, «Terrazas de Mayo» se comía
        #    a sus propios locales —*LOCAL 47*, *Destel*, el *Patio de comidas*, a 109-153 m—
        #    que son inquilinos, no el shopping. El shopping de verdad igual se fusiona, pero
        #    por la vía fuerte: `shopping` es palabra genérica y los dos núcleos son iguales.
        #  · **match ÚNICO** en todo el radio. Si el nombre está contenido en VARIOS negocios
        #    distintos no identifica a ninguno: es lo que delata a «San Miguel» (contenido en
        #    8) sin necesidad de un padrón de topónimos — y hace falta, porque `localidad` y
        #    `municipio` vienen vacíos en PBA y el partido vecino no sale del nombre de la
        #    región. La ambigüedad es la señal.
        if golpe is None and propio:
            cand = [(q.nombre, _dist_m(p["lat"], p["lng"], q.lat, q.lng)) for q in previos]
            cand = [(n, d) for n, d in cand if d <= radio and _nombre_contenido(nombre, n)]
            if len(cand) == 1 and cand[0][1] <= dist_contenido_m:
                golpe = cand[0]
            elif len(cand) > 1:
                logger.debug(f"OsmPoi: «{nombre}» está contenido en {len(cand)} negocios "
                             f"distintos ⇒ no identifica a ninguno, no se deduplica")
        if golpe:
            dup += 1
            logger.debug(f"OsmPoi: «{nombre or '(sin nombre)'}» ya estaba como "
                         f"«{golpe[0]}» a {golpe[1]:.0f} m ⇒ no se duplica")
        else:
            quedan.append(p)
    if dup:
        logger.info(f"OsmPoi: {dup} POIs descartados por estar ya en otra fuente")
    return quedan, dup


def _upsert_comercios(region_id: str, survey_id: str, pois: list[dict]) -> int:
    engine = get_engine()
    with engine.begin() as conn:
        # Barrido de los POIs de OSM que ya no corresponden: los que desaparecieron del mapa
        # y —sobre todo— los que una corrida anterior guardó y el dedupe de hoy descarta.
        # Sin esto un duplicado que se coló queda pegado para siempre inflando `uf_comercio`,
        # que es el mismo modo de falla por omisión que el barrido de `_agregar_uf`.
        # Sólo corre con la lista ya en la mano: si Overpass falló, `run` devolvió ok:false
        # mucho antes de llegar acá y no se borra nada.
        conn.execute(text("""
            DELETE FROM comercios
             WHERE region_id = :rid AND source = 'osm'
               AND place_id <> ALL(:vigentes)
        """), {"rid": region_id, "vigentes": [p["place_id"] for p in pois]})
        for p in pois:
            conn.execute(text("""
                INSERT INTO comercios (comercio_id, survey_id, region_id, place_id, nombre,
                                       rubro, tipos, location, source, fetched_at)
                VALUES (:cid, CAST(:sid AS uuid), :rid, :pid, :nombre, :rubro, :dir,
                        ST_SetSRID(ST_MakePoint(:lng, :lat), 4326), 'osm', now())
                ON CONFLICT (region_id, place_id) DO UPDATE SET
                    nombre = EXCLUDED.nombre, rubro = EXCLUDED.rubro,
                    tipos = EXCLUDED.tipos, location = EXCLUDED.location,
                    survey_id = EXCLUDED.survey_id, fetched_at = now()
            """), {"cid": str(uuid.uuid4()), "sid": survey_id, "rid": region_id,
                   "pid": p["place_id"], "nombre": p["nombre"], "rubro": p["rubro"],
                   "dir": p["direccion"], "lat": p["lat"], "lng": p["lng"]})
    return len(pois)


def _sellar_pois(region_id: str, pois: list[dict]) -> int:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM establecimientos_poi "
                          "WHERE region_id = :rid AND fuente = 'osm_poi'"), {"rid": region_id})
        n = 0
        for p in pois:
            # Sin etiqueta de la taxonomía no se sella: el POI ya cuenta como comercio en
            # `comercios`, y sellarlo con una etiqueta inventada le pondría a la parcela un
            # tipo de edificación falso (mismo criterio que `_clasificar` en Overture).
            if not p.get("etiqueta"):
                continue
            # Igual que en Overture: se guarda el vínculo YA RESUELTO por `_link_to_parcelas`
            # (mig. 057), no la coordenada sola, para que `ParcelaCategoria` no re-aterrice
            # la etiqueta por geometría cruda y deshaga las guardas.
            conn.execute(text("""
                INSERT INTO establecimientos_poi
                    (poi_id, region_id, fuente, categoria, descripcion, nombre, lat, lng,
                     parcela_id, vinculo_resuelto)
                VALUES (:id, :rid, 'osm_poi', :cat, :desc, :nombre, :lat, :lng,
                        (SELECT c.parcela_id FROM comercios c
                          WHERE c.region_id = :rid AND c.place_id = :pid), true)
            """), {"id": str(uuid.uuid4()), "rid": region_id, "cat": p["categoria"],
                   "pid": p["place_id"],
                   "desc": p["etiqueta"], "nombre": p["nombre"],
                   "lat": p["lat"], "lng": p["lng"]})
            n += 1
    return n


@agent_run
def run(input: OsmPoiInput) -> OsmPoiOutput:
    out = OsmPoiOutput(region_id=input.region_id)

    desconocidos = [t for t in input.tags if t not in _TAGS]
    if desconocidos:
        return OsmPoiOutput(ok=False, region_id=input.region_id,
                            error=f"tags sin mapeo a la taxonomía: {desconocidos} "
                                  f"(agregarlos a _TAGS con su etiqueta del cliente)")

    bbox, zone = _load_zone(input.region_id, input.survey_id)
    if bbox is None:
        bbox = _bbox_from_db(input.region_id, 0.001)
    if bbox is None:
        return OsmPoiOutput(ok=False, region_id=input.region_id,
                            error="sin zona ni parcelas para derivar el bbox")

    south, west, north, east = bbox
    try:
        data = _fetch_overpass(_query(input.tags, south, west, north, east,
                                      input.timeout_s),
                               timeout=input.timeout_s)
    except Exception as e:  # noqa: BLE001
        return OsmPoiOutput(ok=False, region_id=input.region_id,
                            error=f"Overpass falló: {e}")
    if not data:
        # `_fetch_overpass` ya distingue "sin resultados" de "todos los mirrors caídos".
        return OsmPoiOutput(ok=False, region_id=input.region_id,
                            error="Overpass no devolvió una respuesta confiable "
                                  "(mirrors caídos): el cero NO es dato")

    pois = _parse(data, input.tags)

    # Recorte exacto a la zona: el bbox es un rectángulo y la zona puede ser un polígono
    # rotado, como el de Malvinas — una de las tres estaciones del bbox cae afuera.
    if zone is not None and not zone.is_empty:
        from shapely.geometry import Point
        pois = [p for p in pois if zone.covers(Point(p["lng"], p["lat"]))]

    out.pois_encontrados = len(pois)
    if not pois:
        logger.info(f"OsmPoi {input.region_id}: sin POIs {input.tags} dentro de la zona")
        return out

    # Dedupe ANTES de insertar: lo que ya trajo otra fuente no se guarda de nuevo, o el mismo
    # negocio contaría dos veces en `uf_comercio`.
    if input.dedupe:
        pois, out.duplicados_descartados = _descartar_duplicados(input.region_id, pois)
        if not pois:
            logger.info(f"OsmPoi {input.region_id}: los {out.pois_encontrados} POIs ya "
                        f"estaban todos en otra fuente")
            return out
    for p in pois:
        out.por_tag[p["rubro"]] = out.por_tag.get(p["rubro"], 0) + 1

    out.comercios_guardados = _upsert_comercios(input.region_id, input.survey_id, pois)
    (out.vinculados_a_parcela, out.pois_reasignados_por_huella, out.pois_sin_edificio,
     out.pois_reasignados_por_numero) = _link_to_parcelas(
        input.region_id, input.survey_id, input.exigir_huella, input.reasignar_max_m,
        source="osm", exigir_numero=input.exigir_numero,
        numero_max_m=input.numero_max_m, salto_min=input.salto_min)
    out.pois_taxonomia = _sellar_pois(input.region_id, pois)

    # El conteo de UF lo hace la MISMA función que usa Overture (cuenta las dos fuentes):
    # sin esto un supermercado que sólo está en OSM se guardaba como comercio pero la parcela
    # seguía saliendo al CSV con `uf_comercio=0`, que es justo el agujero que vino a tapar.
    if input.aportar_uf:
        (out.parcelas_con_comercio, out.total_uf_comercio,
         _uso, out.parcelas_uf_limpiada) = _agregar_uf(
            input.region_id, input.survey_id, input.set_uso)

    logger.info(f"OsmPoi {input.region_id}: {out.pois_encontrados} POIs "
                f"({out.duplicados_descartados} ya estaban en otra fuente) {out.por_tag} · "
                f"{out.vinculados_a_parcela} vinculados · "
                f"{out.pois_reasignados_por_huella} reasignados por huella · "
                f"{out.pois_reasignados_por_numero} por número · "
                f"uf_comercio={out.total_uf_comercio} en {out.parcelas_con_comercio} parcelas")
    return out
