"""MunicipioCodigoFetcher — completa `regions.municipio_codigo` (código IBGE) cuando falta.

**Por qué existe.** Media docena de agentes brasileros no arrancan sin el código IBGE del
município: `IBGECensusFetcher`, `IBGELogradourosFetcher`, y las dos fuentes GRATUITAS de
hoteles de `HotelFetcher` (Cadastur local y Receita hospedagem), que ante un código en NULL
se saltean con `'solo Brasil con municipio_codigo IBGE'`. Ese salto no es cosmético: medido
en `zona-version-piloto-varzea-grande-1` el 31-ago-2026, las dos gratuitas quedaron afuera
por eso y —con OSM caído por el 406 de Overpass— **Google Places quedó como única fuente y
se pagó**, para terminar con 3 hoteles de los cuales el operador descartó uno como falso.
El código se resuelve al crear la zona (`GeoJSONZoneFetcher`/`ZonaFetcher` →
`geo.detect_municipio_br`), pero si esa detección falla la región queda en NULL **para
siempre** y en silencio: no hay reintento, y `CLAUDE.md` sólo dice «si está en NULL,
completarlo», sin decir con qué. Esto es ese con qué.

**Escalera de resolución** (de lo gratis y exacto a lo que puede fallar):

  1. `vecino`  — otra región del sistema que YA tiene código y cuyo polígono contiene al
                 punto de ésta. **Offline, gratis y exacto**: si la zona nueva cae adentro
                 de una zona ya relevada, el município es el mismo. En esta base hay 32
                 regiones selladas 5108402, así que cualquier zona nueva de Várzea Grande
                 se resuelve sin salir a la red.
  2. `reverse` — el camino que ya usaba el sistema: Nominatim da (nombre, UF) y el nombre
                 se busca en la lista OFICIAL del IBGE. La diferencia es que acá, si el
                 match exacto falla, hay **fuzzy** — que es justo donde `detect_municipio_br`
                 devolvía None sin explicar nada. Nominatim escribe «Santana do
                 Livramento» y el IBGE «Sant'Ana do Livramento»; la comparación por
                 igualdad estricta no las une y el código se pierde por un apóstrofo.
  3. `nombre`  — último recurso para la región **sin geometría y sin parcelas**, donde no
                 hay punto que geocodificar: se busca el `name` de la región en la lista del
                 IBGE. Exige **match único en todo Brasil** o una UF que desambigüe, porque
                 los homónimos son reales y caros: *Várzea Grande* es **5108402 (MT)** y
                 también **2211407 (PI)**. Ante dos candidatos no se elige ninguno y se
                 devuelven los dos en `motivo` — verificado: pedirlo sin UF devuelve los dos.

**Nunca escribe un código que no exista en la lista del IBGE.** Un código inválido no falla
ruidosamente: rompe los fetchers de censo y logradouros más adelante, lejos de acá. La
lista (`/api/v1/localidades/municipios`, ~5.570 filas) se baja una vez por corrida y sirve
a la vez de validador y de índice de búsqueda por nombre.

Idempotente: por default sólo toca las que están en NULL (`sobrescribir=False`) y `dry_run`
permite ver qué haría sin escribir. Sólo Brasil — la columna es `IBGE/INDEC` y hay una
región argentina con `'136'`, que es INDEC y no se toca.

Reusable desde otro agente sin pasar por el RPC:

    from scrapitero.agents.municipio_codigo_fetcher import resolver_codigo
    codigo = resolver_codigo(conn, region_id)     # None si no se pudo
"""

from __future__ import annotations

import difflib
import json
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from shapely.geometry import Point, shape
from shapely.ops import unary_union
from sqlalchemy import text

from scrapitero.agents import geo
from scrapitero.agents._run import agent_run
from scrapitero.db.engine import get_engine

_IBGE_TODOS_URL = "https://servicodados.ibge.gov.br/api/v1/localidades/municipios"

# Umbral del match difuso de nombres. 0,90 es deliberadamente alto: acá no se trata de
# adivinar sino de tolerar la diferencia de grafía entre Nominatim y el IBGE (apóstrofos,
# «d'Oeste», acentos). Bajarlo empieza a unir municípios que de verdad son distintos.
_FUZZY_MIN = 0.90

# Estados por nombre completo, para desambiguar el paso `nombre` cuando la región se llama
# «Várzea Grande, Mato Grosso». Sin esto ese caso es irresoluble: hay dos Várzea Grande.
_UF_POR_NOMBRE = {
    "acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM", "bahia": "BA",
    "ceara": "CE", "distrito federal": "DF", "espirito santo": "ES", "goias": "GO",
    "maranhao": "MA", "mato grosso": "MT", "mato grosso do sul": "MS",
    "minas gerais": "MG", "para": "PA", "paraiba": "PB", "parana": "PR",
    "pernambuco": "PE", "piaui": "PI", "rio de janeiro": "RJ",
    "rio grande do norte": "RN", "rio grande do sul": "RS", "rondonia": "RO",
    "roraima": "RR", "santa catarina": "SC", "sao paulo": "SP", "sergipe": "SE",
    "tocantins": "TO",
}


class MunicipioCodigoInput(BaseModel):
    # None = barre TODAS las regiones brasileras a las que les falta el código.
    region_id: Optional[str] = None
    # Por default sólo completa NULL. Con True re-resuelve y puede corregir uno mal puesto.
    sobrescribir: bool = False
    # Muestra qué haría, sin escribir. Útil antes de un barrido masivo.
    dry_run: bool = False


class ResolucionMunicipio(BaseModel):
    region_id: str
    codigo: Optional[str] = None
    municipio: Optional[str] = None
    uf: Optional[str] = None
    # `vecino` | `reverse` | `nombre` | `ya_tenia`
    metodo: Optional[str] = None
    escrito: bool = False
    # Por qué no se pudo, con el detalle que necesita el operador para destrabarlo.
    motivo: Optional[str] = None


class MunicipioCodigoOutput(BaseModel):
    ok: bool
    procesadas: int = 0
    resueltas: int = 0
    escritas: int = 0
    ya_tenian: int = 0
    sin_resolver: int = 0
    por_metodo: dict[str, int] = {}
    detalle: list[ResolucionMunicipio] = []
    error: Optional[str] = None


# ── Lista oficial del IBGE (validador + índice por nombre) ────────────────────

_municipios_cache: Optional[list[dict]] = None


def _municipios_ibge() -> list[dict]:
    """Los ~5.570 municípios de Brasil: `{'id','nome','uf'}`. Cacheado por proceso.

    Una sola llamada sirve para las dos cosas que hacen falta: **validar** cualquier código
    candidato y **buscar** por nombre. La alternativa —pedir la lista por UF, como hacía
    `detect_municipio_br`— obliga a conocer la UF de antemano, que es justo lo que falta en
    el caso de la región sin geometría.
    """
    global _municipios_cache
    if _municipios_cache is not None:
        return _municipios_cache
    try:
        with httpx.Client(timeout=30, headers=geo._HEADERS, follow_redirects=True) as c:
            r = c.get(_IBGE_TODOS_URL)
        if r.status_code != 200:
            logger.warning(f"IBGE localidades devolvió HTTP {r.status_code}")
            return []
        out = []
        for m in r.json():
            uf = (((m.get("microrregiao") or {}).get("mesorregiao") or {})
                  .get("UF") or {}).get("sigla")
            # Distrito Federal y algún caso raro no traen la cadena microrregião completa;
            # el IBGE los expone por `regiao-imediata`. Sin este respaldo se perderían.
            if not uf:
                uf = ((((m.get("regiao-imediata") or {}).get("regiao-intermediaria") or {})
                       .get("UF") or {}).get("sigla"))
            out.append({"id": str(m.get("id")), "nome": m.get("nome"), "uf": uf})
        _municipios_cache = out
        logger.info(f"IBGE: {len(out)} municípios cargados para validar/buscar")
        return out
    except httpx.HTTPError as e:
        logger.warning(f"IBGE localidades no respondió: {e}")
        return []


def _buscar_por_nombre(nombre: str, uf: Optional[str]) -> list[dict]:
    """Municípios cuyo nombre coincide con `nombre`, exacto primero y difuso después.

    Devuelve **todos** los candidatos, no el mejor: la ambigüedad es información que el
    llamador tiene que ver. Con `uf` acota; sin `uf` busca en todo Brasil.
    """
    lista = _municipios_ibge()
    if not lista or not nombre:
        return []
    if uf:
        lista = [m for m in lista if m["uf"] == uf]
    objetivo = geo._norm(nombre)
    exactos = [m for m in lista if geo._norm(m["nome"]) == objetivo]
    if exactos:
        return exactos
    difusos = [
        m for m in lista
        if difflib.SequenceMatcher(None, objetivo, geo._norm(m["nome"])).ratio() >= _FUZZY_MIN
    ]
    return difusos


def _codigo_valido(codigo: Optional[str]) -> bool:
    """El código existe en la lista oficial. Si la lista no se pudo bajar, no se valida
    nada y se devuelve False: es preferible no escribir a escribir un código inventado."""
    if not codigo:
        return False
    return any(m["id"] == str(codigo) for m in _municipios_ibge())


# ── Punto representativo de la región ─────────────────────────────────────────

def _forma(gj_txt: Optional[str]):
    """Geometría de un `zone_geojson`, que en esta base es siempre un FeatureCollection."""
    if not gj_txt:
        return None
    try:
        gj = json.loads(gj_txt) if isinstance(gj_txt, str) else gj_txt
        if gj.get("type") == "FeatureCollection":
            partes = [shape(f["geometry"]) for f in gj.get("features", []) if f.get("geometry")]
            return unary_union(partes) if partes else None
        if gj.get("type") == "Feature":
            return shape(gj["geometry"])
        return shape(gj)
    except Exception as e:  # geometría rota: no es motivo para tumbar el barrido
        logger.warning(f"zone_geojson ilegible: {e}")
        return None


def _punto_de_region(conn, region_id: str, gj_txt: Optional[str]) -> Optional[tuple[float, float]]:
    """(lat, lng) representativo. Primero el polígono de la zona; si no hay, las parcelas.

    `representative_point` y no el centroide: el centroide de una zona en «L» o de un
    corredor por calle puede caer fuera del polígono y, en el borde de un município,
    fuera del município.
    """
    forma = _forma(gj_txt)
    if forma is not None and not forma.is_empty:
        p = forma.representative_point()
        return (p.y, p.x)
    fila = conn.execute(text("""
        SELECT ST_Y(ST_Centroid(ST_Collect(geometry))), ST_X(ST_Centroid(ST_Collect(geometry)))
        FROM parcelas WHERE region_id = :rid AND geometry IS NOT NULL
    """), {"rid": region_id}).one_or_none()
    if fila and fila[0] is not None:
        return (float(fila[0]), float(fila[1]))
    return None


# ── Los tres pasos de la escalera ─────────────────────────────────────────────

def _por_vecino(conn, region_id: str, punto: Optional[tuple[float, float]]) -> Optional[dict]:
    """Región ya resuelta cuyo polígono contiene al punto de ésta. Gratis y exacta."""
    if not punto:
        return None
    p = Point(punto[1], punto[0])
    filas = conn.execute(text("""
        SELECT region_id, municipio_codigo, zone_geojson FROM regions
        WHERE country_code = 'BRA' AND municipio_codigo IS NOT NULL
          AND zone_geojson IS NOT NULL AND region_id <> :rid
    """), {"rid": region_id}).all()
    for rid, codigo, gj in filas:
        forma = _forma(gj)
        if forma is not None and not forma.is_empty and forma.contains(p):
            return {"codigo": str(codigo), "vecino": rid}
    return None


def _por_reverse(punto: Optional[tuple[float, float]]) -> Optional[dict]:
    """Nominatim → (nombre, UF) → lista del IBGE, con fuzzy si el exacto falla."""
    if not punto:
        return None
    nombre, uf = geo._nominatim_municipio_uf(punto[0], punto[1])
    if not nombre:
        return None
    candidatos = _buscar_por_nombre(nombre, uf)
    if len(candidatos) == 1:
        m = candidatos[0]
        return {"codigo": m["id"], "municipio": m["nome"], "uf": m["uf"],
                "reverse_nombre": nombre}
    if len(candidatos) > 1:
        return {"ambiguo": [f'{m["nome"]} ({m["uf"]}) {m["id"]}' for m in candidatos],
                "reverse_nombre": nombre}
    return {"sin_match": f"{nombre} ({uf or 'sin UF'})"}


def _por_nombre_region(nombre_region: Optional[str]) -> Optional[dict]:
    """El nombre de la región contra la lista del IBGE. Sólo si queda UN candidato.

    La UF puede venir dentro del propio nombre («Várzea Grande, Mato Grosso»), que es lo
    único que separa el 5108402 de MT del 2211407 de PI.
    """
    if not nombre_region:
        return None
    limpio = geo._norm(nombre_region)
    uf = None
    for estado, sigla in _UF_POR_NOMBRE.items():
        if estado in limpio:
            uf = sigla
            limpio = limpio.replace(estado, "").strip(" ,-")
            break
    limpio = limpio.strip(" ,-")
    if len(limpio) < 4:
        return None
    candidatos = _buscar_por_nombre(limpio, uf)
    if len(candidatos) == 1:
        m = candidatos[0]
        return {"codigo": m["id"], "municipio": m["nome"], "uf": m["uf"]}
    if len(candidatos) > 1:
        return {"ambiguo": [f'{m["nome"]} ({m["uf"]}) {m["id"]}' for m in candidatos]}
    return None


# ── API reusable ──────────────────────────────────────────────────────────────

def resolver(conn, region_id: str, name: Optional[str] = None,
             gj_txt: Optional[str] = None) -> ResolucionMunicipio:
    """Resuelve el código de UNA región sin escribir nada. Es el corazón del agente y lo
    que puede llamar cualquier otro (ver `resolver_codigo`)."""
    if name is None or gj_txt is None:
        fila = conn.execute(text(
            "SELECT name, zone_geojson FROM regions WHERE region_id = :rid"
        ), {"rid": region_id}).one_or_none()
        if fila is None:
            return ResolucionMunicipio(region_id=region_id, motivo="la región no existe")
        name, gj_txt = fila[0], fila[1]

    punto = _punto_de_region(conn, region_id, gj_txt)

    v = _por_vecino(conn, region_id, punto)
    if v:
        # El código del vecino ya está en uso y fue validado cuando se escribió; se
        # revalida igual salvo que la lista del IBGE no esté disponible, porque en ese
        # caso rechazarlo sería perder la única vía que no necesita red.
        lista = _municipios_ibge()
        if not lista or _codigo_valido(v["codigo"]):
            m = next((x for x in lista if x["id"] == v["codigo"]), {})
            return ResolucionMunicipio(region_id=region_id, codigo=v["codigo"],
                                       municipio=m.get("nome"), uf=m.get("uf"),
                                       metodo="vecino",
                                       motivo=f'heredado de {v["vecino"]}')

    r = _por_reverse(punto)
    if r and r.get("codigo") and _codigo_valido(r["codigo"]):
        return ResolucionMunicipio(region_id=region_id, codigo=r["codigo"],
                                   municipio=r.get("municipio"), uf=r.get("uf"),
                                   metodo="reverse")

    n = _por_nombre_region(name)
    if n and n.get("codigo") and _codigo_valido(n["codigo"]):
        return ResolucionMunicipio(region_id=region_id, codigo=n["codigo"],
                                   municipio=n.get("municipio"), uf=n.get("uf"),
                                   metodo="nombre")

    # Sin código: el motivo tiene que decirle al operador qué le falta a ESTA región.
    if not _municipios_ibge():
        motivo = ("la lista de municípios del IBGE no respondió "
                  "(servicodados.ibge.gov.br) — sin ella no se valida ningún código")
    elif not punto:
        amb = (n or {}).get("ambiguo")
        motivo = ("la región no tiene ni polígono ni parcelas, así que no hay punto que "
                  "geocodificar; " +
                  (f'y el nombre {name!r} da varios municípios: {", ".join(amb)}' if amb
                   else f'y el nombre {name!r} no matchea ningún município del IBGE'))
    elif r and r.get("ambiguo"):
        motivo = (f'el reverse devolvió {r["reverse_nombre"]!r} y matchea varios '
                  f'municípios: {", ".join(r["ambiguo"])}')
    elif r and r.get("sin_match"):
        motivo = (f'el reverse devolvió {r["sin_match"]} y ese nombre no está en la lista '
                  f'del IBGE ni con match difuso')
    else:
        motivo = ("el reverse de Nominatim no devolvió município para "
                  f"({punto[0]:.4f},{punto[1]:.4f})")
    return ResolucionMunicipio(region_id=region_id, motivo=motivo)


def resolver_codigo(conn, region_id: str) -> Optional[str]:
    """Atajo para otros agentes: el código, o None. NO escribe en la DB."""
    return resolver(conn, region_id).codigo


# ── Agente ────────────────────────────────────────────────────────────────────

@agent_run
def run(input: MunicipioCodigoInput) -> MunicipioCodigoOutput:
    eng = get_engine()
    detalle: list[ResolucionMunicipio] = []
    por_metodo: dict[str, int] = {}
    escritas = ya_tenian = 0

    with eng.begin() as conn:
        if input.region_id:
            filas = conn.execute(text("""
                SELECT region_id, name, country_code, municipio_codigo, zone_geojson
                FROM regions WHERE region_id = :rid
            """), {"rid": input.region_id}).all()
            if not filas:
                return MunicipioCodigoOutput(
                    ok=False, error=f"región no encontrada: {input.region_id}")
        else:
            # Barrido: sólo Brasil y sólo lo que falta. Las regiones argentinas quedan
            # afuera a propósito — su código es INDEC y esta lista es del IBGE.
            filas = conn.execute(text("""
                SELECT region_id, name, country_code, municipio_codigo, zone_geojson
                FROM regions
                WHERE country_code = 'BRA'
                  AND (:sobre OR municipio_codigo IS NULL)
                ORDER BY region_id
            """), {"sobre": input.sobrescribir}).all()

        for rid, name, cc, actual, gj in filas:
            if cc != "BRA":
                detalle.append(ResolucionMunicipio(
                    region_id=rid, codigo=actual, motivo=
                    f"país {cc}: el código IBGE es de Brasil, no se toca"))
                continue
            if actual and not input.sobrescribir:
                ya_tenian += 1
                detalle.append(ResolucionMunicipio(
                    region_id=rid, codigo=str(actual), metodo="ya_tenia"))
                continue

            res = resolver(conn, rid, name=name, gj_txt=gj)

            if res.codigo and not input.dry_run and str(actual or "") != res.codigo:
                conn.execute(text("""
                    UPDATE regions SET municipio_codigo = :cod WHERE region_id = :rid
                """), {"cod": res.codigo, "rid": rid})
                res.escrito = True
                escritas += 1
            if res.metodo:
                por_metodo[res.metodo] = por_metodo.get(res.metodo, 0) + 1
            detalle.append(res)

    resueltas = sum(1 for d in detalle if d.codigo and d.metodo != "ya_tenia")
    sin_resolver = sum(1 for d in detalle if not d.codigo)
    logger.info(
        f"MunicipioCodigoFetcher: {len(detalle)} regiones · resueltas={resueltas} "
        f"escritas={escritas} ya_tenían={ya_tenian} sin_resolver={sin_resolver} "
        f"por_método={por_metodo}{' [dry-run]' if input.dry_run else ''}")

    return MunicipioCodigoOutput(
        ok=True, procesadas=len(detalle), resueltas=resueltas, escritas=escritas,
        ya_tenian=ya_tenian, sin_resolver=sin_resolver, por_metodo=por_metodo,
        detalle=detalle)
