"""HotelReglas — en qué lote está cada hotel y qué es, según la tabla del cliente. Sólo Brasil.

Para qué: medido en VAZ049 (18-sep-2026), el pipeline solo dejaba mal las 16 situaciones de
hospedaje de la zona:
  · **El hotel no llega a su lote.** `HotelFetcher` vincula con `ST_Contains(parcela, pin)` y el
    pin del registro es la dirección FISCAL geocodificada, que cae en la calle o lejos: el
    Slaviero Slim (132 UH, 8 pisos) quedó sin lote y el lote figuraba baldío; Cerrados Park,
    Ceolatto, Los Angeles y Tainá, igual.
  · **O llega al lote de otro.** El pin del Cadastur del Hotel Express caía en el Pit Stop de
    Localiza (802) y le sumaba 38 habitaciones; en la zona de prueba el del Hotel Casa Nova caía
    en una casa de Jaime Benevides, a 290 m del hotel.
  · **Y cuenta mal.** Las habitaciones entraban como `uf_comercio` (146 en el Amazon, 117 en el
    Hits…), pero la tabla del cliente ("4.5 TIPOS DE EDIFICAÇÃO", Drive `Edificaciones_01-03`)
    pone HOTEL, MOTEL y FLAT en **E — especial**, y PENSÃO en R. Decisión de Jaz el 18-sep: el
    hotel es especial pero conserva sus unidades (la base del operador cuenta 123 HP en el Amazon).

Reglas:
  H1 · **Ubicar por consenso de pines.** Se juntan los pines del MISMO negocio en las fuentes de
       mapa —el censo 2022 (coordenada tomada en el lugar), Overture, OSM y los registros de Google
       de `hoteles`— y cada pin vota el lote que lo contiene o el más cercano a ≤`dist_lote_m`. Se
       asigna si votan ≥2 tipos de fuente y TODOS coinciden. El nombre se compara sin las palabras
       de razón social (HOTELARIA, EMPREENDIMENTOS…, que comparten hoteles distintos) y sin lo que
       va entre paréntesis («Hotel Los Angeles (SOB NOVA DIREÇÃO …)» se confundía con el Casa NOVA).
  H1b · **Contrastar el vínculo del pipeline.** Apoyo de un lote = tipos de fuente con un pin a
       ≤`dist_lote_m`. Sin ningún apoyo, el vínculo se deshace (Casa Nova). Si otro lote tiene MÁS
       apoyo, se marca y no se toca (Express: registro en el 802 con 1 fuente, visto en el 780 por
       3). Un vínculo que viene de `hotel_ubicacion_manual` es decisión humana: no se contrasta.
  H2 · **Clasificar.** Hotel/motel/flat → especial (uso `equipamiento`) CON sus habitaciones como
       unidades, igual que la base del operador. Pousada con kitnets, dormitório, pensão
       → PENSÃO (residencial) con las viviendas del censo o de la ficha, sin comercio.
       Marca en vez de cambiar si en el lote hay otro negocio visto por 2 fuentes (los servicios
       del propio hotel —restaurante, bar, eventos— no cuentan), viviendas del censo o de la ficha
       (una casa con un hotel al lado es R + E, no un especial: ver el caso SINTEP), o si el lote
       ya tiene un tipo o una UF corregidos a mano.
  Sólo cuenta un hotel que existe físicamente: ≥2 fuentes de mapa, o 1 + Cadastur vigente. Un
  CNPJ de Receita sin pines es una oficina o una filial (ver `hotel-filial-administrativa`).

Medido en VAZ049 a ciegas (sin las correcciones a mano): 8 casos resueltos iguales a la decisión
humana, 0 roturas, 8 marcados (Tainá, Garcia, Diplomata, Ceolatto, Express, …).

Escribe: `hoteles.parcela_id` (H1/H1b), `parcelas` con el sello `regla_hoteles` (H2, nunca sobre
`manual`) y las marcas en `parcela_revision_nota` con el prefijo `[hoteles]`. Idempotente: al
empezar deshace lo que dejó la corrida anterior (sellos y marcas propias).
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections import defaultdict
from typing import Optional

from loguru import logger
from pydantic import BaseModel
from shapely import wkb
from shapely.geometry import Point
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.establecimientos_observados import _bbox, _zip_cnefe
from scrapitero.agents.nombre_inmueble import cerrado, es_generico, mismo_negocio, normalizar
from scrapitero.db.engine import get_engine

SELLO = "regla_hoteles"
PREFIJO_NOTA = "[hoteles] "

_HOSPEDAJE = re.compile(r"hotel|pousada|motel|hostel|albergue|\bpens[aã]o\b|pensionato|\bflat\b|"
                        r"apart ?hotel|hospedagem|hotelaria|dormit[oó]rio|kit ?net|\binn\b|lodging|"
                        r"guest_house", re.I)
# «hotel» que no hospeda personas: hotel para mascotas, guardería («hotelzinho»), el estacionamiento.
_NO_HOSPEDAJE = re.compile(r"\bpet\b|cachorro|canin|hotelzinho|hotelsinho|educand|infantil|\bbaby\b|"
                           r"estacionamento|lavanderia", re.I)
# La categoría de Overture/OSM que dice hospedaje aunque el nombre no lo diga.
_CAT_HOSPEDAJE = re.compile(r"hotel|motel|lodging|\binn\b|hostel|guest", re.I)
_PENSAO = re.compile(r"\bpens[aã]o\b|pensionato|dormit[oó]rio|kit ?nets?|quitinete|rep[uú]blica|"
                     r"aluguel mensal", re.I)
_MOTEL = re.compile(r"motel|love_hotel|drive", re.I)
_FLAT = re.compile(r"\bflat\b|apart ?hotel", re.I)
# Lo que un hotel tiene adentro: no es "otro negocio" del lote.
_DEL_HOTEL = re.compile(r"restaurante|cozinha|rooftop|\bbar\b|lanchonete|caf[eé]|pizzaria|churrasc|"
                        r"eventos|conven[cç][oõ]es|charging|recarga", re.I)
# Palabras de razón social que comparten hoteles distintos: «PENDRAGON HOTELARIA, EMPREENDIMENTOS
# E PARTICIPACOES» (Los Angeles) cruzaba con «HOTELARIA JA FERREIRA» (Tainá) y con «HOTEL CA
# MARTINS EMPREENDIMENTOS» (Slim).
_SOCIETARIO = frozenset("""HOTELARIA HOTELEIRO HOTELEIRA HOTEIS EMPREENDIMENTOS EMPREENDIMENTO
PARTICIPACOES INVESTIMENTOS HOSPEDAGEM HOSPEDAGENS TURISMO ADMINISTRACAO GESTAO NEGOCIOS SERVICOS
PRESTACAO REDE GRUPO CIA ASSOCIADOS PALACE PARK""".split())
_ESPECIES_HOSPEDAJE = {"2"}                 # CNEFE: domicílio coletivo (hotel, pensão, asilo…)
_ESPECIES_NEGOCIO = {"4", "5", "6", "8"}
_NIVELES = {"1", "2"}                       # NV_GEO_COORD que ubican el lote (ver establecimientos_observados)


class HotelReglasInput(BaseModel):
    region_id: str
    survey_id: str
    dry_run: bool = False
    dist_lote_m: float = 15.0      # pin → lote
    hueco_m: float = 25.0          # más lejos que esto de todo lote: fuera de la zona
    buffer_deg: float = 0.005      # ~550 m: para ver dónde está de verdad un hotel mal vinculado
    cadastur_vigente_desde: int = 2023
    cadastur_viejo_antes: int = 2022
    # Sólo para medir la regla con `dry_run` (prueba a ciegas): decidir como si no hubiera
    # correcciones a mano, y reemplazar vínculos {nombre del hotel: cca_code}.
    ignorar_manual: bool = False
    vinculos_forzados: dict[str, str] = {}


class HotelReglasOutput(BaseModel):
    ok: bool = True
    error: Optional[str] = None
    omitido: Optional[str] = None
    vinculados: list[str] = []
    desvinculados: list[str] = []
    especiales: list[str] = []
    pensoes: list[str] = []
    marcas: list[str] = []
    respetados: list[str] = []      # lotes con tipo/UF corregidos a mano: la regla no opina
    ignorados: int = 0
    fuentes: dict = {}


def mismo(a: Optional[str], b: Optional[str]) -> bool:
    """`mismo_negocio` sin lo que va entre paréntesis ni las palabras de razón social."""
    a, b = re.sub(r"\(.*?\)", " ", a or ""), re.sub(r"\(.*?\)", " ", b or "")
    ta = [t for t in normalizar(a).split() if t not in _SOCIETARIO]
    tb = [t for t in normalizar(b).split() if t not in _SOCIETARIO]
    return bool(ta and tb) and mismo_negocio(" ".join(ta), " ".join(tb))


def _cnefe(muni: str, bbox: tuple) -> tuple[list[tuple], list[tuple]]:
    """(pines de hospedaje, domicilios) del censo 2022 en el bbox. Sólo coordenadas de dirección."""
    s, w, n, e = bbox
    pines, domicilios = [], []
    with zipfile.ZipFile(_zip_cnefe(muni)) as zf:
        nombre_csv = next(x for x in zf.namelist() if x.lower().endswith(".csv"))
        with zf.open(nombre_csv) as fh:
            for r in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8", errors="replace"),
                                    delimiter=";"):
                if r.get("NV_GEO_COORD") not in _NIVELES:
                    continue
                try:
                    lat, lng = float(r["LATITUDE"]), float(r["LONGITUDE"])
                except (TypeError, ValueError):
                    continue
                if not (s <= lat <= n and w <= lng <= e):
                    continue
                esp, nom = r.get("COD_ESPECIE"), (r.get("DSC_ESTABELECIMENTO") or "").strip()
                if esp == "1":
                    domicilios.append((lat, lng))
                elif nom and (esp in _ESPECIES_HOSPEDAJE or (esp in _ESPECIES_NEGOCIO
                                                              and _HOSPEDAJE.search(nom))) \
                        and not _NO_HOSPEDAJE.search(nom):
                    pines.append(("censo", nom, lat, lng, True))
    return pines, domicilios


def _osm(bbox: tuple) -> list[tuple]:
    from scrapitero.agents.osm_building_fetcher import _fetch_overpass
    s, w, n, e = bbox
    q = (f'[out:json][timeout:120];(nwr["tourism"~"hotel|motel|guest_house|hostel|apartment"]'
         f'({s},{w},{n},{e});nwr["amenity"="love_hotel"]({s},{w},{n},{e}););out tags center;')
    pines = []
    for el in _fetch_overpass(q, timeout=180).get("elements", []):
        t = el.get("tags", {})
        c = el.get("center") or {"lat": el.get("lat"), "lon": el.get("lon")}
        if t.get("name") and c.get("lat") is not None and not _NO_HOSPEDAJE.search(t["name"]):
            suf = " [love_hotel]" if t.get("amenity") == "love_hotel" else ""
            pines.append(("osm", t["name"] + suf, float(c["lat"]), float(c["lon"]), True))
    return pines


@agent_run
def run(input: HotelReglasInput) -> HotelReglasOutput:
    out = HotelReglasOutput()
    eng = get_engine()
    D = input.dist_lote_m
    with eng.begin() as cx:
        meta = cx.execute(text("SELECT upper(coalesce(country_code,'')), coalesce(municipio_codigo,'') "
                               "FROM regions WHERE region_id = :r"), {"r": input.region_id}).fetchone()
        if not meta or meta[0] != "BRA":
            # La tabla R/C/E es el pliego del cliente brasilero; fuera de Brasil un hotel se sigue
            # contando como hasta ahora.
            out.omitido = f"sólo Brasil (región {input.region_id}: {meta[0] if meta else '?'})"
            return out
        bbox = _bbox(cx, input.survey_id, input.buffer_deg)
        if bbox is None:
            return HotelReglasOutput(ok=False, error="el relevamiento no tiene parcelas con geometría")

        # ── pines ──────────────────────────────────────────────────────────────────────
        pins: list[tuple] = []          # (fuente, nombre, lat, lng, es_hospedaje)
        for n, cat, f, la, lo in cx.execute(text("""
                SELECT nombre, coalesce(categoria_fuente,''), fuente, ST_Y(geom), ST_X(geom)
                FROM establecimientos_observados WHERE region_id = :r AND fuente = 'overture'
                  AND geom IS NOT NULL"""), {"r": input.region_id}):
            pins.append(("overture", n, la, lo,
                         bool(_CAT_HOSPEDAJE.search(cat) or _HOSPEDAJE.search(n))
                         and not _NO_HOSPEDAJE.search(n)))
        for n, f, la, lo in cx.execute(text("""
                SELECT nombre, fuente, ST_Y(location), ST_X(location) FROM hoteles
                WHERE survey_id = CAST(:s AS uuid) AND fuente IN ('google','osm')
                  AND location IS NOT NULL AND NOT cerrado_def"""), {"s": input.survey_id}):
            pins.append((f, n, la, lo, True))
        domicilios: list[tuple] = []
        if re.fullmatch(r"\d{7}", meta[1]):
            try:
                p_censo, domicilios = _cnefe(meta[1], bbox)
                pins += p_censo
            except Exception as e:  # noqa: BLE001 — una fuente caída no tumba la regla
                logger.warning(f"HotelReglas: censo no disponible ({e})")
        try:
            pins += _osm(bbox)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"HotelReglas: OSM no disponible ({e})")
        out.fuentes = {f: sum(1 for p in pins if p[0] == f and p[4])
                       for f in ("censo", "overture", "osm", "google")}

        def lote_de(la, lo):
            return cx.execute(text("""
                SELECT p.parcela_id::text, p.calle || ' ' || coalesce(p.numero, 'S/N'),
                       ST_Distance(p.geometry::geography,
                                   ST_SetSRID(ST_MakePoint(:lo, :la), 4326)::geography)
                FROM parcelas p WHERE p.survey_id = CAST(:s AS uuid) AND p.geometry IS NOT NULL
                ORDER BY p.geometry <-> ST_SetSRID(ST_MakePoint(:lo, :la), 4326) LIMIT 1
            """), {"s": input.survey_id, "la": la, "lo": lo}).fetchone()

        def dist(pid, la, lo) -> float:
            return float(cx.execute(text("""
                SELECT ST_Distance(geometry::geography, ST_SetSRID(ST_MakePoint(:lo, :la), 4326)::geography)
                FROM parcelas WHERE parcela_id = CAST(:p AS uuid)"""), {"p": pid, "la": la, "lo": lo}).scalar())

        def nombre_lote(pid) -> str:
            return cx.execute(text("SELECT calle || ' ' || coalesce(numero, 'S/N') FROM parcelas "
                                   "WHERE parcela_id = CAST(:p AS uuid)"), {"p": pid}).scalar() or "?"

        # ── entidades: los registros del pipeline + hospedajes que sólo ven las fuentes de mapa ──
        regs = cx.execute(text("""
            SELECT h.hotel_id::text, h.nombre, h.cnpj, h.parcela_id::text,
                   coalesce(c.razao_social, r.razao_social, ''),
                   coalesce(c.uh_fonte_trimestre, c.fonte_trimestre, ''), coalesce(c.situacao, h.situacion_cadastur, ''),
                   EXISTS (SELECT 1 FROM hotel_ubicacion_manual m
                            WHERE m.region_id = h.region_id AND m.cnpj = h.cnpj) AS ubic_manual
            FROM hoteles h
            LEFT JOIN cadastur_hospedagem c ON c.cnpj = h.cnpj
            LEFT JOIN receita_estabelecimentos r ON r.cnpj = h.cnpj
            WHERE h.survey_id = CAST(:s AS uuid) AND NOT h.cerrado_def
        """), {"s": input.survey_id}).fetchall()
        entidades, usados = [], set()
        if input.vinculos_forzados and not input.dry_run:
            return HotelReglasOutput(ok=False, error="vinculos_forzados es sólo para dry_run")
        for hid, nom, cnpj, pid, razao, trim, sit, ubic_manual in regs:
            if nom in input.vinculos_forzados:
                pid = cx.execute(text("SELECT parcela_id::text FROM parcelas WHERE survey_id = CAST(:s AS uuid) "
                                      "AND cca_code = :c"), {"s": input.survey_id, "c": input.vinculos_forzados[nom]}).scalar()
                ubic_manual = False
            if input.ignorar_manual:
                ubic_manual = False
            nombres = [nom] + ([razao] if razao and razao.upper() != (nom or "").upper() else [])
            mios = [i for i, p in enumerate(pins) if p[4] and any(mismo(x, p[1]) for x in nombres)]
            usados.update(mios)
            anio = int(trim[:4]) if trim[:4].isdigit() else None
            entidades.append(dict(
                ids=[hid], nombre=nom, pid=pid, ubic_manual=ubic_manual, pins=[pins[i] for i in mios],
                vigente=bool(anio and anio >= input.cadastur_vigente_desde and "opera" in sit.lower()),
                viejo=bool(anio and anio < input.cadastur_viejo_antes)))
        sueltos = [p for i, p in enumerate(pins) if p[4] and i not in usados and not cerrado(p[1])]
        while sueltos:
            p0 = sueltos.pop(0)
            grupo = [p0] + [p for p in sueltos if mismo(p0[1], p[1])]
            sueltos = [p for p in sueltos if p not in grupo]
            if not any(_HOSPEDAJE.search(p[1]) for p in grupo) and len({p[0] for p in grupo}) < 2:
                continue
            entidades.append(dict(ids=[], nombre=p0[1], pid=None, ubic_manual=False, pins=grupo,
                                  vigente=False, viejo=False))

        # ── H1 / H1b ─────────────────────────────────────────────────────────────────────
        marcas: dict[str, list[str]] = defaultdict(list)   # parcela_id → textos
        vincular, desvincular, una_fuente = [], [], []
        por_lote: dict[str, list[dict]] = defaultdict(list)
        for en in entidades:
            tipos = {p[0] for p in en["pins"]}
            votos: dict[str, set] = defaultdict(set)
            dmin, cerca = 1e9, None
            for f, n, la, lo, _ in en["pins"]:
                l = lote_de(la, lo)
                if not l:
                    continue
                if float(l[2]) < dmin:
                    dmin, cerca = float(l[2]), l[0]
                if float(l[2]) <= D:
                    votos[l[0]].add(f)
            existe = len(tipos) >= 2 or (len(tipos) == 1 and en["vigente"])
            if en["pid"] and en["pins"] and not en["ubic_manual"]:
                cands = {en["pid"]} | set(votos)
                apoyo = {q: {f for f, n, la, lo, _ in en["pins"] if dist(q, la, lo) <= D} for q in cands}
                mejor = max((q for q in cands if q != en["pid"]), key=lambda q: len(apoyo[q]), default=None)
                if not apoyo[en["pid"]]:
                    desvincular.append((en, en["pid"]))
                    en["pid"] = None
                elif mejor and len(apoyo[mejor]) > len(apoyo[en["pid"]]):
                    txt = (f"{en['nombre']}: el registro lo pone en {nombre_lote(en['pid'])} "
                           f"({'+'.join(sorted(apoyo[en['pid']]))}) pero lo ven en {nombre_lote(mejor)} "
                           f"({'+'.join(sorted(apoyo[mejor]))})")
                    marcas[en["pid"]].append(txt); marcas[mejor].append(txt)
                    continue
            if not en["pid"]:
                if not en["pins"] or dmin > input.hueco_m:
                    out.ignorados += 1
                    continue
                if not existe:
                    if en["viejo"]:
                        out.ignorados += 1
                        continue
                    una_fuente.append((en, "+".join(sorted(tipos)), dict(votos), dmin, cerca))
                    continue
                todos = {f for v in votos.values() for f in v}
                if len(votos) == 1 and len(todos) < 2:
                    q = next(iter(votos))
                    marcas[q].append(f"{en['nombre']}: lo ve sólo {'+'.join(sorted(todos))} en este lote"
                                     + (" (el Cadastur lo da en operación)" if en["vigente"] else ""))
                    continue
                if len(votos) == 1 and len(todos) >= 2:
                    en["pid"] = next(iter(votos))
                    if en["ids"]:
                        vincular.append(en)
                elif not votos:
                    marcas[cerca].append(f"{en['nombre']}: el hotel cae entre nuestros lotes, a "
                                         f"{dmin:.0f} m de este, y en ninguno")
                    continue
                else:
                    lotes = " | ".join(f"{nombre_lote(q)} ({'+'.join(sorted(v))})" for q, v in votos.items())
                    for q in votos:
                        marcas[q].append(f"{en['nombre']}: las fuentes lo ubican en lotes distintos → {lotes}")
                    continue
            por_lote[en["pid"]].append(en)

        # Hospedajes que ve una sola fuente: si caen en el lote de un hotel ya ubicado son ese
        # mismo hotel con otro nombre («Ami Hotel CGB» en el lote del Los Angeles, bajo nueva
        # dirección); si no, van a revisión.
        for en, fte, votos, dmin, cerca in una_fuente:
            if votos and set(votos) <= set(por_lote):
                continue
            for q in (votos or [cerca]):
                marcas[q].append(f"{en['nombre']}: hospedaje que ve sólo {fte}"
                                 + ("" if votos else f", a {dmin:.0f} m de este lote y en ninguno"))

        # ── H2 ───────────────────────────────────────────────────────────────────────────
        especiales, pensoes = [], []
        for pid, ens in por_lote.items():
            vig = [e for e in ens if not e["viejo"]] or ens
            nombres = [y for e in vig for y in [e["nombre"]] + [p[1] for p in e["pins"]]]
            tipo = ("PENSÃO" if any(_PENSAO.search(n) for n in nombres) else
                    "MOTEL" if any(_MOTEL.search(n) for n in nombres) else
                    "FLAT" if any(_FLAT.search(n) for n in nombres) else "HOTEL")
            lot = cx.execute(text("""
                SELECT p.uf_vivienda, p.uf_comercio, coalesce(p.uf_fuente,''), coalesce(p.uso_fuente,''),
                       ST_AsBinary(p.geometry),
                       (SELECT autor FROM parcela_tipo_manual t WHERE t.parcela_id = p.parcela_id)
                FROM parcelas p WHERE p.parcela_id = CAST(:p AS uuid)"""), {"p": pid}).fetchone()
            v, c, uff, usf, geom, autor_tipo = lot
            poly = wkb.loads(bytes(geom))
            dom = sum(1 for la, lo in domicilios if poly.contains(Point(lo, la)))
            obs = cx.execute(text("""
                SELECT fuente, nombre FROM establecimientos_observados
                WHERE region_id = :r AND parcela_id = CAST(:p AS uuid)"""),
                {"r": input.region_id, "p": pid}).fetchall()
            cand = [(f, n) for f, n in obs if not _HOSPEDAJE.search(n) and not _DEL_HOTEL.search(n)
                    and not any(mismo(n, y) for y in nombres) and not cerrado(n) and not es_generico(n)]
            otros = sorted({n for f, n in cand if len({f2 for f2, n2 in cand if mismo(n, n2)}) >= 2})
            nombre = vig[0]["nombre"]
            if not input.ignorar_manual and (uff == "manual" or usf == "manual"
                                             or (autor_tipo and autor_tipo != SELLO)):
                out.respetados.append(f"{nombre_lote(pid)} ({vig[0]['nombre']})")
                continue            # decisión humana: la regla no opina
            if tipo == "PENSÃO":
                pensoes.append((pid, max(v or 0, dom, 1), vig))
                if otros:
                    marcas[pid].append(f"{nombre}: pensão con otro negocio en el lote ({', '.join(otros[:3])})")
            elif otros:
                marcas[pid].append(f"{nombre}: {tipo} con otro negocio en el lote ({', '.join(otros[:3])})")
            elif dom or (v or 0) > 0 and uff not in ("cadastur", "hotel_min", SELLO):
                marcas[pid].append(f"{nombre}: {tipo} con {max(dom, v or 0)} vivienda(s) en el lote "
                                   f"({'censo' if dom else 'ficha'}): ¿especial, o vivienda + especial?")
            else:
                especiales.append((pid, tipo, vig))

        out.vinculados = [f"{e['nombre']} → {nombre_lote(e['pid'])}" for e in vincular]
        out.desvinculados = [f"{e['nombre']} ✗ {nombre_lote(p)}" for e, p in desvincular]
        out.especiales = [f"{nombre_lote(p)}: {t} ({vs[0]['nombre']})" for p, t, vs in especiales]
        out.pensoes = [f"{nombre_lote(p)}: {n}v ({vs[0]['nombre']})" for p, n, vs in pensoes]
        out.marcas = [f"{nombre_lote(p)} — {' / '.join(ts)}" for p, ts in marcas.items()]
        if input.dry_run:
            return out

        # ── escritura ───────────────────────────────────────────────────────────────────
        # 1) deshacer la corrida anterior: sellos propios que ya no corresponden y marcas propias.
        vigentes = [p for p, *_ in especiales] + [p for p, *_ in pensoes]
        cx.execute(text(f"""
            UPDATE parcelas SET uf_fuente = NULL,
                   uso_principal = CASE WHEN uso_fuente = '{SELLO}' THEN NULL ELSE uso_principal END,
                   uso_fuente = CASE WHEN uso_fuente = '{SELLO}' THEN NULL ELSE uso_fuente END
            WHERE survey_id = CAST(:s AS uuid) AND uf_fuente = '{SELLO}'
              AND NOT (parcela_id::text = ANY(:v))"""), {"s": input.survey_id, "v": vigentes})
        cx.execute(text("DELETE FROM parcela_revision_nota WHERE region_id = :r AND autor = :a"),
                   {"r": input.region_id, "a": SELLO})
        cx.execute(text("""
            UPDATE parcela_revision_nota
               SET nota = regexp_replace(nota, ' · \\[hoteles\\] .*$', '')
             WHERE region_id = :r AND nota LIKE '%· [hoteles] %'"""), {"r": input.region_id})
        # 2) H1 / H1b
        for en in vincular:
            cx.execute(text("UPDATE hoteles SET parcela_id = CAST(:p AS uuid) WHERE hotel_id::text = ANY(:ids)"),
                       {"p": en["pid"], "ids": en["ids"]})
        for en, viejo in desvincular:
            cx.execute(text("UPDATE hoteles SET parcela_id = NULL WHERE hotel_id::text = ANY(:ids)"),
                       {"ids": en["ids"]})
            # La UF que el hotel le había sumado al lote (mismo barrido que `hotel_fetcher`).
            cx.execute(text(f"""
                UPDATE parcelas SET uf_comercio = 0, unidades_funcionales_estimadas = COALESCE(uf_vivienda, 0),
                       uf_fuente = NULL,
                       uso_principal = CASE WHEN uso_fuente IN ('cadastur','hotel_min','{SELLO}') THEN NULL ELSE uso_principal END,
                       uso_fuente = CASE WHEN uso_fuente IN ('cadastur','hotel_min','{SELLO}') THEN NULL ELSE uso_fuente END
                WHERE parcela_id = CAST(:p AS uuid) AND uf_fuente IN ('cadastur','hotel_min','{SELLO}')
                  AND NOT EXISTS (SELECT 1 FROM hoteles h WHERE h.parcela_id = CAST(:p AS uuid) AND NOT h.cerrado_def)
            """), {"p": viejo})
        # 3) H2
        for pid, tipo, vs in especiales:
            # Especial CON sus unidades (Jaz, 18-sep-2026: "tipo especial pero con su cantidad
            # de unidades"): las habitaciones de los hoteles vivos del lote —el mismo piso de 1
            # que `hotel_fetcher` cuando no hay conteo—. Van en `uf_comercio` para que sumen en
            # todos los totales; el uso `equipamiento` es lo que las hace E en el plano y los CSV.
            hab = cx.execute(text("""
                SELECT COALESCE(SUM(GREATEST(COALESCE(habitaciones, 1), 1)), 0) FROM hoteles
                WHERE parcela_id = CAST(:p AS uuid) AND NOT cerrado_def"""), {"p": pid}).scalar() or 1
            cx.execute(text(f"""
                UPDATE parcelas SET uf_vivienda = 0, uf_comercio = :u, unidades_funcionales_estimadas = :u,
                       uf_fuente = '{SELLO}', uso_principal = 'equipamiento', uso_fuente = '{SELLO}'
                WHERE parcela_id = CAST(:p AS uuid)"""), {"p": pid, "u": int(hab)})
        for pid, nv, vs in pensoes:
            cx.execute(text(f"""
                UPDATE parcelas SET uf_vivienda = :v, uf_comercio = 0, unidades_funcionales_estimadas = :v,
                       uf_fuente = '{SELLO}', uso_principal = 'residencial', uso_fuente = '{SELLO}'
                WHERE parcela_id = CAST(:p AS uuid)"""), {"p": pid, "v": nv})
            ids = [i for e in vs for i in e["ids"]]
            if ids:   # el rótulo del mapa sale de `hoteles.tipo` (`_hotel_tipo_label`: 'pens' → PENSÃO)
                cx.execute(text("UPDATE hoteles SET tipo = 'pensao' WHERE hotel_id::text = ANY(:ids)"),
                           {"ids": ids})
        # 4) marcas en la columna de revisión (una nota por inscrição; si ya hay una de otro
        #    autor, la de hoteles se agrega al final y la próxima corrida la vuelve a armar).
        for pid, ts in marcas.items():
            cca = cx.execute(text("SELECT cca_code FROM parcelas WHERE parcela_id = CAST(:p AS uuid)"),
                             {"p": pid}).scalar()
            if not cca:
                continue
            txt = PREFIJO_NOTA + " / ".join(dict.fromkeys(ts)) + ". Revisar a ojo."
            previa = cx.execute(text("SELECT nota FROM parcela_revision_nota WHERE region_id = :r AND cca_code = :c"),
                                {"r": input.region_id, "c": cca}).scalar()
            if previa is None:
                cx.execute(text("""INSERT INTO parcela_revision_nota (region_id, cca_code, parcela_id, motivo, nota, autor)
                                   VALUES (:r, :c, CAST(:p AS uuid), 'hotel', :n, :a)"""),
                           {"r": input.region_id, "c": cca, "p": pid, "n": txt, "a": SELLO})
            else:
                cx.execute(text("UPDATE parcela_revision_nota SET nota = nota || ' · ' || :n, actualizado_at = now() "
                                "WHERE region_id = :r AND cca_code = :c"),
                           {"r": input.region_id, "c": cca, "n": txt})
    logger.info(f"HotelReglas {input.region_id}: {len(out.vinculados)} vinculados, "
                f"{len(out.desvinculados)} desvinculados, {len(out.especiales)} especiales, "
                f"{len(out.pensoes)} pensões, {len(out.marcas)} lotes marcados")
    return out
