"""EstablecimientosObservados — qué negocio está HOY en cada lote, según el censo 2022 y Overture.

Para qué: el nombre del inmueble de los entregables (`DSC_NOME_DO_IMOVEL`) salía del complemento
de la ficha del BCI, que conserva el negocio de cuando se registró el lote. Detectado por el
operador el 16-sep-2026 en Várzea Grande: Ponce de Arruda 962 salía «ESTACIONAMENTO AEROPARKING»
y ahí hoy está la locadora Loca10. En VAZ049, 10 de 36 nombres comparables ya no estaban. La
composición del nombre vive en `nombre_inmueble.componer_nombre`; este agente trae el insumo.

Fuentes, las dos gratis:
  · **CNEFE del censo 2022** (IBGE): cada establecimiento que el censista encontró en la calle,
    con lo que anotó (`DSC_ESTABELECIMENTO`), el número de puerta y una coordenada. Un zip por
    município (Várzea Grande: 2,7 MB), cacheado en `SCRAPITERO_CACHE`. Sólo Brasil.
  · **Overture Places**: la misma consulta de `OverturePlacesFetcher` (se importa, no se copia).

Escribe SÓLO `establecimientos_observados` (mig. 067). No toca `comercios`, `establecimientos_poi`
ni `parcelas`: esas tablas alimentan `uf_comercio`, `uso_principal` y el sello de categoría, y un
nombre no tiene que mover ninguna cantidad.

**El censo anota descripciones, no sólo nombres**: en Várzea Grande lo más repetido es «VAGO»
(338), «BAR», «MERCEARIA», «SEM NOME» (157), «BARRACAO», «LAVA JATO». Una palabra que aparece en
`umbral_rubro` (10) o más negocios del município es rubro, no nombre — «LAVA» 197, «OFICINA» 376,
mientras las marcas aparecen 1 a 4 veces («LOCA10» 1, «DROGASIL» 4). No se guarda lo que tiene
una sola palabra y es de rubro («OFICINA», «TRANSPIRTADORA») ni la descripción que se repite
tal cual en el município («LAVA JATO» 74 veces); «CENTER FIBRAS» son dos palabras comunes pero
aparece una sola vez, y es un nombre. El vocabulario sale del padrón de cada município, no de
una lista, y se aplica sólo al censo (ver `run`).

Vínculo a la parcela, medido en VAZ049 contra la caché de Google de julio (que no entra acá):
  1. `numero` (sólo censo) — el lote de ESA calle con el número de puerta que anotó el censista,
     a ≤`numero_max_m`. El punto del censo es la coordenada de la dirección y a menudo cae en el
     lote de al lado: «MAIDANA TURBOS» anota 210 y el punto caía en el 202; «RODRIGO RODRIGUES
     ADVOCACIA» anota 214 y caía en el 185. Google los pone en el 210 y el 214.
  2. `adentro` — el punto cae en el lote.
  3. `cercania` — cae en la vereda: el lote CONSTRUIDO más cercano a ≤`cercania_max_m`. La
     locadora Loca10 del censo cae a 3,6 m de su lote, que no tiene número en la ficha.
  Coincidencia con Google (16-sep-2026): censo atado por número 12 de 14, por el punto 53 de 84;
  Overture adentro del lote 64 de 73, en la vereda 35 de 46.
  ⚠ Probado y descartado: soltar el punto del censo que cae en un lote numerado sin coincidir
  el número. Se equivocaba más de lo que acertaba — cuando no coinciden, muchas veces el número
  mal puesto es el de la FICHA («MISSISSIPI PIZZARIA» anota 782 y la ficha de su lote dice 686),
  y los sin número (Cerrados Park Hotel, GP Pneus, Educandário Jardim Aeroporto) estaban en su
  lote. Queda «CAROL MOTOS» en el 352 cuando está en el 374: es el costo.
  **A Overture el número NO se le aplica**: declara mal la puerta (58 % contra la revisión del
  cliente) y atado por número coincidía con Google 8 de 14, contra 99 de 119 por el punto.
  «Localiza Rent A Car» declara 840 y está bien en el lote que la ficha numera 806.
  ⚠ Probado y descartado: mover un negocio de Overture al lote donde el censo tiene uno de nombre
  parecido. Coincidía con Google en 5 de 22 — se llevaba la locadora Localiza al lote del «Pit
  Stop Localiza» de al lado.

Idempotente: borra y recarga las filas de cada fuente de la región.
"""

from __future__ import annotations

import csv
import io
import os
import re
import uuid
import zipfile
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.bci_parser import _clave_via
from scrapitero.agents.direccion_norm import separar_numero
from scrapitero.agents.ibge_census_fetcher import _UF_POR_CODIGO
from scrapitero.agents.nombre_inmueble import cerrado, es_generico, normalizar
from scrapitero.agents.overture_places_fetcher import (
    _NO_COMERCIO,
    _RELEASE_DEFAULT,
    _consultar_overture,
)
from scrapitero.db.engine import get_engine

_CNEFE_BASE = ("https://ftp.ibge.gov.br/Cadastro_Nacional_de_Enderecos_para_Fins_Estatisticos/"
               "Censo_Demografico_2022/Arquivos_CNEFE/CSV/Municipio/")
# `COD_ESPECIE` (diccionario IBGE): 1 domicílio particular · 2 domicílio coletivo · 3 estab.
# agropecuário · 4 ensino · 5 saúde · 6 outras finalidades · 7 edificação em construção ·
# 8 religioso. El 1 y el 7 no tienen nombre.
_ESPECIES_CON_NOMBRE = {"2", "3", "4", "5", "6", "8"}
# Lo que se cuenta como VIVIENDA al contrastar el conteo del catastro (mig. 069): el domicílio
# particular. El colectivo (2) es hotel, pensión o asilo y ya viaja como establecimiento con
# nombre, así que no entra acá: contarlo sería sumar el hotel dos veces.
_ESPECIES_DOMICILIO = {"1"}
# `NV_GEO_COORD`: 1 coordenada original de la dirección · 2 modificada (departamentos del mismo
# número) · 3 estimada · 4 face de quadra · 5 localidade · 6 setor. Sólo 1 y 2 ubican el lote.
_NIVELES_DIRECCION = {"1", "2"}
# Lo que el censista anota cuando no hay nombre.
_SIN_NOMBRE = re.compile(r"\bSEM (NOME|DENOMINA\w*|FINALI\w*|IDENTIFICA\w*|PLACA|LETREIRO)\b"
                         r"|\bNAO IDENTIFICAD\w*")
_CEP = re.compile(r"\b\d{5}-?\d{3}\b")


class ObservadosInput(BaseModel):
    region_id: str
    survey_id: str
    fuentes: list[str] = ["censo", "overture"]
    numero_max_m: float = 60.0
    cercania_max_m: float = 10.0
    umbral_rubro: int = 10
    overture_release: str = _RELEASE_DEFAULT
    buffer_deg: float = 0.0005          # ~55 m alrededor de las parcelas del relevamiento


class ObservadosOutput(BaseModel):
    ok: bool = True
    error: Optional[str] = None
    # {fuente: {cargados, descartados_genericos, numero, adentro, cercania, sin_parcela}}
    por_fuente: dict = {}


def _bbox(conn, survey_id: str, buf: float) -> Optional[tuple[float, float, float, float]]:
    """(south, west, north, east) de las parcelas del relevamiento, con margen."""
    r = conn.execute(text("""
        SELECT ST_YMin(e) - :b, ST_XMin(e) - :b, ST_YMax(e) + :b, ST_XMax(e) + :b
        FROM (SELECT ST_Extent(geometry) AS e FROM parcelas
              WHERE survey_id = CAST(:sid AS uuid) AND geometry IS NOT NULL) x
    """), {"sid": survey_id, "b": buf}).fetchone()
    return tuple(float(v) for v in r) if r and r[0] is not None else None


def _zip_cnefe(municipio_codigo: str) -> Path:
    """El zip del CNEFE del município, bajado una sola vez. El nombre del archivo lleva el del
    município («5108402_VARZEA_GRANDE.zip») y se busca en el índice de la carpeta de su UF."""
    cache = Path(os.environ.get("SCRAPITERO_CACHE", "/tmp/scrapitero_cache")) / "cnefe_2022"
    cache.mkdir(parents=True, exist_ok=True)
    destino = cache / f"{municipio_codigo}.zip"
    if destino.exists() and destino.stat().st_size > 0:
        return destino
    uf = _UF_POR_CODIGO.get(municipio_codigo[:2])
    if not uf:
        raise RuntimeError(f"municipio_codigo {municipio_codigo!r} sin UF conocida")
    carpeta = f"{_CNEFE_BASE}{municipio_codigo[:2]}_{uf}/"
    headers = {"User-Agent": "scrapitero/1.0"}
    indice = httpx.get(carpeta, headers=headers, timeout=60, follow_redirects=True)
    indice.raise_for_status()
    m = re.search(rf'href="({municipio_codigo}_[^"]+\.zip)"', indice.text)
    if not m:
        raise RuntimeError(f"el IBGE no publica el CNEFE de {municipio_codigo} en {carpeta}")
    tmp = destino.with_suffix(".part")
    with httpx.stream("GET", carpeta + m.group(1), headers=headers, timeout=600,
                      follow_redirects=True) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)
    tmp.rename(destino)
    logger.info(f"CNEFE 2022 de {municipio_codigo} bajado: {destino.stat().st_size / 1e6:.1f} MB")
    return destino


def _leer_cnefe(zip_path: Path, bbox: tuple[float, float, float, float], umbral_rubro: int
                ) -> tuple[list[dict], frozenset[str], frozenset[str], list[dict]]:
    """Los establecimientos del bbox, el vocabulario de rubro del município entero, las
    descripciones que se repiten tal cual, y **los domicilios** del bbox.

    Los domicilios (`COD_ESPECIE` 1 particular y 2 colectivo) no tienen nombre, así que no
    sirven para el `DSC_NOME_DO_IMOVEL` —para lo que nació este agente— pero son la única
    fuente independiente de CUÁNTAS VIVIENDAS hay en cada lote: el censista pasó por la puerta
    y anotó una por una, con el complemento que vio ("CASA 2", "QUITINETE 7", "FUNDOS"). Van a
    `censo_domicilios` (mig. 069) y los usa `IncidenciasReporter` para contrastar la cantidad
    que declara la ficha del catastro. Acá se leen en la MISMA pasada del zip: es el mismo
    archivo y recorrerlo dos veces sería tirar medio minuto por relevamiento.
    """
    south, west, north, east = bbox
    frecuencia: Counter = Counter()
    completas: Counter = Counter()
    en_zona = []
    domicilios = []
    with zipfile.ZipFile(zip_path) as zf:
        nombre_csv = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        with zf.open(nombre_csv) as fh:
            for r in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8", errors="replace"),
                                    delimiter=";"):
                nombre = (r.get("DSC_ESTABELECIMENTO") or "").strip()
                if r.get("COD_ESPECIE") in _ESPECIES_DOMICILIO:
                    if r.get("NV_GEO_COORD") in _NIVELES_DIRECCION:
                        try:
                            dlat, dlng = float(r["LATITUDE"]), float(r["LONGITUDE"])
                        except (TypeError, ValueError):
                            continue
                        if south <= dlat <= north and west <= dlng <= east:
                            via = " ".join(x for x in (r.get("NOM_TIPO_SEGLOGR"),
                                                       r.get("NOM_TITULO_SEGLOGR"),
                                                       r.get("NOM_SEGLOGR")) if x)
                            # El complemento del CNEFE viene partido en pares (tipo, valor):
                            # ("CASA", "2"), ("QUITINETE", "7"), ("FUNDOS", "").
                            compl = " ".join(x.strip() for x in (
                                r.get("NOM_COMP_ELEM1"), r.get("VAL_COMP_ELEM1"),
                                r.get("NOM_COMP_ELEM2"), r.get("VAL_COMP_ELEM2")) if x and x.strip())
                            domicilios.append({
                                "id": r["COD_UNICO_ENDERECO"], "especie": r.get("COD_ESPECIE"),
                                "complemento": compl or None, "lat": dlat, "lng": dlng,
                                "calle": via or None,
                                "numero": (r.get("NUM_ENDERECO") or "").strip().lstrip("0") or None})
                    continue
                if not nombre or r.get("COD_ESPECIE") not in _ESPECIES_CON_NOMBRE:
                    continue
                frecuencia.update({t for t in normalizar(nombre).split()
                                   if len(t) >= 3 and not t.isdigit()})
                completas[normalizar(nombre)] += 1
                if r.get("NV_GEO_COORD") not in _NIVELES_DIRECCION:
                    continue
                try:
                    lat, lng = float(r["LATITUDE"]), float(r["LONGITUDE"])
                except (TypeError, ValueError):
                    continue
                if not (south <= lat <= north and west <= lng <= east):
                    continue
                via = " ".join(x for x in (r.get("NOM_TIPO_SEGLOGR"), r.get("NOM_TITULO_SEGLOGR"),
                                           r.get("NOM_SEGLOGR")) if x)
                numero = (r.get("NUM_ENDERECO") or "").strip()
                en_zona.append({"id": r["COD_UNICO_ENDERECO"], "nombre": nombre,
                                "categoria": r.get("COD_ESPECIE"), "lat": lat, "lng": lng,
                                "direccion": f"{via} {numero}".strip(), "calle": via,
                                "numero": numero.lstrip("0")})
    rubro = frozenset(t for t, n in frecuencia.items() if n >= umbral_rubro)
    repetidas = frozenset(t for t, n in completas.items() if n >= 2)
    return en_zona, rubro, repetidas, domicilios


def _es_nombre(nombre: str, rubro: frozenset[str] = frozenset(),
               repetidas: frozenset[str] = frozenset()) -> bool:
    n = normalizar(nombre)
    if not n or cerrado(n) or _SIN_NOMBRE.search(n) or es_generico(n):
        return False
    if not rubro or not es_generico(n, rubro):
        return True
    palabras = [t for t in n.split() if len(t) >= 3 and not t.isdigit()]
    return len(palabras) > 1 and n not in repetidas


def _clave_calle(calle: Optional[str]) -> str:
    """Clave de la vía para comparar la que declara la fuente con la de la ficha. Sobre
    `_clave_via` del BCI: «MAL» es «MARECHAL» y las letras dobles no cuentan («MOTTA» / «MOTA»)."""
    k = re.sub(r"\bMAL\b", "MARECHAL", _clave_via(calle or ""))
    return re.sub(r"([A-Z])\1+", r"\1", k)


def _misma_calle(a: Optional[str], b: Optional[str]) -> bool:
    ka, kb = _clave_calle(a), _clave_calle(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    ta, tb = set(ka.split()), set(kb.split())
    if (ta <= tb or tb <= ta) and any(len(t) >= 5 for t in ta & tb):
        return True
    return SequenceMatcher(None, ka, kb).ratio() >= 0.85


def _cargar(conn, region_id: str, survey_id: str, fuente: str, filas: list[dict]) -> None:
    conn.execute(text("DELETE FROM establecimientos_observados "
                      "WHERE region_id = :rid AND fuente = :f"), {"rid": region_id, "f": fuente})
    if not filas:
        return
    conn.execute(text("""
        INSERT INTO establecimientos_observados
            (id, region_id, survey_id, fuente, fuente_id, nombre, categoria_fuente, direccion,
             calle_declarada, numero_declarado, geom)
        VALUES (:id, :rid, CAST(:sid AS uuid), :f, :fid, :nombre, :cat, :dir, :calle, :num,
                ST_SetSRID(ST_MakePoint(:lng, :lat), 4326))
        ON CONFLICT (region_id, fuente, fuente_id) DO NOTHING
    """), [{"id": str(uuid.uuid4()), "rid": region_id, "sid": survey_id, "f": fuente,
            "fid": x["id"], "nombre": x["nombre"], "cat": x.get("categoria"),
            "dir": x.get("direccion"), "calle": x.get("calle") or None,
            "num": x.get("numero") or None, "lat": x["lat"], "lng": x["lng"]} for x in filas])


def _cargar_domicilios(conn, region_id: str, survey_id: str, filas: list[dict]) -> None:
    """Los domicilios del censo 2022 de la zona (mig. 069). No tocan ninguna cantidad: son el
    insumo con el que `IncidenciasReporter` contrasta lo que declara la ficha del catastro."""
    conn.execute(text("DELETE FROM censo_domicilios WHERE region_id = :rid"), {"rid": region_id})
    if not filas:
        return
    conn.execute(text("""
        INSERT INTO censo_domicilios
            (id, region_id, survey_id, cod_unico, especie, complemento, calle_declarada,
             numero_declarado, geom)
        VALUES (:id, :rid, CAST(:sid AS uuid), :cod, :esp, :compl, :calle, :num,
                ST_SetSRID(ST_MakePoint(:lng, :lat), 4326))
        ON CONFLICT (region_id, cod_unico) DO NOTHING
    """), [{"id": str(uuid.uuid4()), "rid": region_id, "sid": survey_id, "cod": x["id"],
            "esp": x.get("especie"), "compl": x.get("complemento"), "calle": x.get("calle"),
            "num": x.get("numero"), "lat": x["lat"], "lng": x["lng"]} for x in filas])


def _vincular(conn, region_id: str, survey_id: str, numero_max_m: float,
              cercania_max_m: float, tabla: str = "establecimientos_observados",
              filtro_numero: str = "AND fuente = 'censo_2022'") -> None:
    """Ata cada punto a su lote. `tabla` permite correr la MISMA regla sobre los
    establecimientos con nombre y sobre los domicilios del censo (mig. 069): son el mismo tipo
    de dato —un punto que el censista tomó en una puerta— y separar el criterio en dos copias
    es la forma conocida de que se desincronicen. `filtro_numero` acota a qué filas se les
    aplica la regla del número de puerta (a Overture no: declara mal la altura)."""
    # prefiltros en grados para que las búsquedas usen el índice espacial (1° ≈ 111 km)
    p = {"rid": region_id, "sid": survey_id, "maxm": cercania_max_m,
         "maxdeg": cercania_max_m / 90000.0, "nmaxm": numero_max_m,
         "nmaxdeg": numero_max_m / 90000.0}
    conn.execute(text(f"""
        UPDATE {tabla} SET parcela_id = NULL, vinculo = NULL,
               survey_id = CAST(:sid AS uuid)
        WHERE region_id = :rid
    """), p)
    conn.execute(text(f"""
        UPDATE {tabla} e SET parcela_id = q.parcela_id, vinculo = 'adentro'
        FROM parcelas q
        WHERE e.region_id = :rid AND q.survey_id = CAST(:sid AS uuid)
          AND q.geometry IS NOT NULL AND ST_Contains(q.geometry, e.geom)
    """), p)
    conn.execute(text(f"""
        UPDATE {tabla} e SET vinculo = 'cercania', parcela_id = (
            SELECT q.parcela_id FROM parcelas q
            WHERE q.survey_id = CAST(:sid AS uuid) AND q.geometry IS NOT NULL
              AND (COALESCE(q.area_m2_construida, 0) > 0 OR COALESCE(q.huella_m2, 0) > 0)
              AND ST_DWithin(q.geometry, e.geom, :maxdeg)
              AND ST_DWithin(q.geometry::geography, e.geom::geography, :maxm)
            ORDER BY q.geometry <-> e.geom LIMIT 1)
        WHERE e.region_id = :rid AND e.parcela_id IS NULL
    """), p)
    conn.execute(text(f"""
        UPDATE {tabla} SET vinculo = NULL
        WHERE region_id = :rid AND parcela_id IS NULL
    """), p)

    # El número de puerta que anota el censo manda sobre su punto (ver docstring).
    actual = dict(conn.execute(text(f"""
        SELECT id::text, parcela_id::text FROM {tabla}
        WHERE region_id = :rid {filtro_numero}
    """), p).fetchall())
    candidatos: dict[str, list] = {}
    for eid, calle_dec, pid, calle_lote, dist in conn.execute(text(f"""
        SELECT e.id::text, e.calle_declarada, q.parcela_id::text, q.calle,
               ST_Distance(q.geometry::geography, e.geom::geography)
        FROM {tabla} e
        JOIN parcelas q ON q.survey_id = CAST(:sid AS uuid) AND q.geometry IS NOT NULL
         AND ltrim(regexp_replace(COALESCE(q.numero, ''), '\\D', '', 'g'), '0')
             = ltrim(e.numero_declarado, '0')
         AND ST_DWithin(q.geometry, e.geom, :nmaxdeg)
         AND ST_DWithin(q.geometry::geography, e.geom::geography, :nmaxm)
        WHERE e.region_id = :rid {filtro_numero.replace("fuente", "e.fuente")}
          AND COALESCE(ltrim(e.numero_declarado, '0'), '') <> ''
    """), p):
        if _misma_calle(calle_dec, calle_lote):
            candidatos.setdefault(eid, []).append((dist, pid))
    for eid, cands in candidatos.items():
        destino = min(cands)[1]
        if destino != actual.get(eid):
            conn.execute(text(f"UPDATE {tabla} SET vinculo = 'numero', "
                              "parcela_id = CAST(:d AS uuid) WHERE id = CAST(:id AS uuid)"),
                         {"d": destino, "id": eid})


@agent_run
def run(input: ObservadosInput) -> ObservadosOutput:
    out = ObservadosOutput()
    engine = get_engine()
    with engine.connect() as conn:
        bbox = _bbox(conn, input.survey_id, input.buffer_deg)
        meta = conn.execute(text("SELECT country_code, municipio_codigo FROM regions "
                                 "WHERE region_id = :rid"), {"rid": input.region_id}).fetchone()
    if bbox is None:
        return ObservadosOutput(ok=False, error="el relevamiento no tiene parcelas con geometría")

    cargas: dict[str, list[dict]] = {}
    descartados: dict[str, int] = {}
    dom_censo: list[dict] = []          # domicilios del censo (mig. 069): control del conteo
    if "censo" in input.fuentes:
        pais, muni = (meta[0] or "").upper() if meta else "", (meta[1] or "") if meta else ""
        if pais != "BRA" or not re.fullmatch(r"\d{7}", muni):
            logger.warning(f"EstablecimientosObservados: el censo sólo aplica a Brasil con "
                           f"municipio_codigo (región {input.region_id}: {pais} {muni!r}) "
                           f"— se saltea")
        else:
            try:
                filas, rubro, repetidas, domicilios = _leer_cnefe(_zip_cnefe(muni), bbox,
                                                                  input.umbral_rubro)
                cargas["censo_2022"] = [x for x in filas
                                        if _es_nombre(x["nombre"], rubro, repetidas)]
                descartados["censo_2022"] = len(filas) - len(cargas["censo_2022"])
                dom_censo = domicilios
            except Exception as e:  # noqa: BLE001 — una fuente caída no tumba la otra
                logger.warning(f"EstablecimientosObservados: censo falló: {e}")
    if "overture" in input.fuentes:
        try:
            filas = []
            for x in _consultar_overture(bbox, input.overture_release, 0.0):
                nombre = (x.get("nombre") or "").strip()
                if not nombre or (x.get("categoria") or "").strip().lower() in _NO_COMERCIO:
                    continue
                calle, numero = separar_numero(_CEP.split(x.get("direccion") or "")[0])
                filas.append({"id": x["id"], "nombre": nombre, "categoria": x.get("categoria"),
                              "lat": x["lat"], "lng": x["lng"], "direccion": x.get("direccion"),
                              "calle": calle, "numero": (numero or "").lstrip("0")})
            # El vocabulario del censo NO se le aplica: Overture trae el nombre que el negocio
            # se pone, y muchos son palabras comunes — «Banco do Brasil», «Aeroporto Grill
            # Churrascaria», «Rodrigues Miranda Advocacia» (118 descartes así en VAZ049).
            cargas["overture"] = [x for x in filas if _es_nombre(x["nombre"])]
            descartados["overture"] = len(filas) - len(cargas["overture"])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"EstablecimientosObservados: Overture falló: {e}")
    if not cargas:
        return ObservadosOutput(ok=False, error="no se pudo leer ninguna fuente")

    with engine.begin() as conn:
        for fuente, filas in cargas.items():
            _cargar(conn, input.region_id, input.survey_id, fuente, filas)
        _vincular(conn, input.region_id, input.survey_id, input.numero_max_m,
                  input.cercania_max_m)
        # Los domicilios van a su propia tabla y con la MISMA regla de vínculo. Separados de
        # `establecimientos_observados` a propósito: esa tabla alimenta el nombre del inmueble
        # del entregable y una vivienda no tiene nombre — meterlos ahí haría salir «CASA 2»
        # como DSC_NOME_DO_IMOVEL.
        if dom_censo:
            _cargar_domicilios(conn, input.region_id, input.survey_id, dom_censo)
            _vincular(conn, input.region_id, input.survey_id, input.numero_max_m,
                      input.cercania_max_m, tabla="censo_domicilios", filtro_numero="")
            r = conn.execute(text("""
                SELECT count(*), count(*) FILTER (WHERE parcela_id IS NOT NULL)
                FROM censo_domicilios WHERE region_id = :rid
            """), {"rid": input.region_id}).fetchone()
            out.por_fuente["censo_domicilios"] = {"cargados": r[0], "en_parcela": r[1]}
        for fuente in cargas:
            r = conn.execute(text("""
                SELECT count(*),
                       count(*) FILTER (WHERE vinculo = 'numero'),
                       count(*) FILTER (WHERE vinculo = 'adentro'),
                       count(*) FILTER (WHERE vinculo = 'cercania'),
                       count(*) FILTER (WHERE parcela_id IS NULL)
                FROM establecimientos_observados WHERE region_id = :rid AND fuente = :f
            """), {"rid": input.region_id, "f": fuente}).fetchone()
            out.por_fuente[fuente] = {
                "cargados": r[0], "descartados_genericos": descartados.get(fuente, 0),
                "numero": r[1], "adentro": r[2], "cercania": r[3],
                "sin_parcela": r[4]}
    logger.info(f"EstablecimientosObservados {input.region_id}: {out.por_fuente}")
    return out
