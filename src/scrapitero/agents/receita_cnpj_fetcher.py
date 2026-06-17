"""ReceitaCNPJFetcher — universo de establecimientos de hospedagem (CNPJ, dados abertos).

Carga la tabla `receita_estabelecimentos_hospedagem` (migración 027) con TODOS los
establecimientos de hospedagem de Brasil del Cadastro Nacional da Pessoa Jurídica
(dados abertos da Receita Federal), filtrados por CNAE:

  5510-8/01 hotéis · 5510-8/02 apart-hotéis · 5510-8/03 motéis
  5590-6/01 albergues · 5590-6/02 campings · 5590-6/03 pensões · 5590-6/99 outros

Aporta identidad oficial (razão social, nome fantasia, endereço, **situação cadastral**)
para complementar a Cadastur — más cobertura + señal gratuita de abierto/cerrado. **No**
trae habitaciones. Mergea por CNPJ con el resto de fuentes de HotelFetcher.

Fuente: **share público de Nextcloud en `arquivos.receitafederal.gov.br`** (Receita migró
los dados abertos del viejo `dadosabertos.rfb.gov.br`/SERPRO, que bloquea por **ASN de
datacenter/hosting** — ni un VPS en Brasil lo atraviesa, confirmado 2026-06-16). El
Nextcloud es **accesible globalmente** (no geo/ASN-bloqueado) ⇒ **NO requiere proxy**. Se
baja por **WebDAV** con el token del share como usuario de Basic auth
(`RECEITA_SHARE_TOKEN`, password vacío). `RECEITA_PROXY`/`proxy` quedan **opcionales** (override).

Flujo (los .zip se GUARDAN en `dump_dir` y se reusan; el CSV se lee en streaming desde el
zip, nunca entero en memoria):
  1. resuelve el dump mensual más reciente (o el `periodo` pedido),
  2. baja Estabelecimentos{0..9}.zip → filtra CNAE de hospedagem,
  3. baja Empresas{0..9}.zip → razão social por `cnpj_basico` (opcional),
  4. baja Municipios.zip → nombre de município (código RF ≠ IBGE),
  5. recarga la tabla (idempotente: TRUNCATE + bulk insert).

Los .zip quedan en `dump_dir/<AAAA-MM>/` (env `RECEITA_DUMP_DIR`, default
`/opt/scrapitero/receita_dump`) y se **reusan** por tamaño: un re-run o un filtro de OTRO
rubro (escuelas/hospitales/comercios) no re-descarga. Borrar la carpeta libera el espacio.

⚠️ La estructura puede cambiar: el token del share (`RECEITA_SHARE_TOKEN`) y la ruta
(`_BASE_URL` → `…/public.php/webdav/Dados/Cadastros/CNPJ/<AAAA-MM>/`) se resuelven en
runtime (PROPFIND del listado) y conviene validar primero con `Municipios.zip` (~KB).
"""

from __future__ import annotations

import csv
import io
import os
import re
import time
import zipfile
from datetime import datetime, timezone
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.db.engine import get_engine

# ── Layout de los dados abertos (Nextcloud público de arquivos.receitafederal.gov.br) ──
# Overridable por env. El share es global (sin geo/ASN-block); se baja por WebDAV con el
# token del share como usuario de Basic auth (password vacío).
_BASE_URL = os.environ.get(
    "RECEITA_BASE_URL",
    "https://arquivos.receitafederal.gov.br/public.php/webdav/Dados/Cadastros/CNPJ/",
).rstrip("/") + "/"
_SHARE_TOKEN = os.environ.get("RECEITA_SHARE_TOKEN", "gn672Ad4CF8N6TK")
_N_PARTES = 10  # Estabelecimentos0..9, Empresas0..9

# CNAE de hospedagem (7 díg., sin puntuación) — col 11 de ESTABELE
_CNAE_HOSPEDAGEM = {
    "5510801", "5510802", "5510803",          # hotéis / apart-hotéis / motéis
    "5590601", "5590602", "5590603", "5590699",  # albergues / campings / pensões / outros
}

# Índices de columna del layout ESTABELE (CSV ';' latin-1, sin header)
_E_BASICO, _E_ORDEM, _E_DV = 0, 1, 2
_E_MATRIZ, _E_FANTASIA, _E_SIT = 3, 4, 5
_E_DATA_SIT, _E_DATA_INI, _E_CNAE = 6, 10, 11
_E_TIPO_LOGR, _E_LOGR, _E_NUM, _E_COMPL, _E_BAIRRO = 13, 14, 15, 16, 17
_E_CEP, _E_UF, _E_MUN, _E_DDD1, _E_TEL1 = 18, 19, 20, 21, 22

_SITUACAO = {"01": "NULA", "02": "ATIVA", "03": "SUSPENSA", "04": "INAPTA", "08": "BAIXADA"}

_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


class ReceitaCNPJInput(BaseModel):
    periodo: Optional[str] = None              # "AAAA-MM"; None = el más reciente
    proxy: Optional[str] = None                # default: env RECEITA_PROXY
    ufs: Optional[list[str]] = None            # filtrar a UFs (p.ej. ["MT"]); None = todo Brasil
    incluir_razao_social: bool = True          # False ⇒ no baja Empresas (más rápido/liviano)
    timeout_s: int = 120                       # timeout por request (archivos grandes + proxy)
    # Carpeta donde se GUARDAN los .zip del dump (NO se borran): se reusan en re-runs y para
    # filtrar otros rubros (escuelas/hospitales/comercios) sin re-descargar. Subcarpeta por período.
    dump_dir: str = os.environ.get("RECEITA_DUMP_DIR", "/opt/scrapitero/receita_dump")


class ReceitaCNPJOutput(BaseModel):
    ok: bool = True
    periodo: Optional[str] = None
    estabelecimientos: int = 0
    por_uf: dict[str, int] = {}
    con_razao: int = 0
    error: Optional[str] = None


def _resolver_proxy(inp: ReceitaCNPJInput) -> str:
    """Proxy OPCIONAL: el Nextcloud de arquivos… es global. Se usa solo si se setea
    explícitamente `RECEITA_PROXY`/`proxy` (override). "" = directo, sin proxy."""
    return inp.proxy or os.environ.get("RECEITA_PROXY", "")


def _auth() -> Optional[httpx.BasicAuth]:
    """Basic auth del share público de Nextcloud: el token va como usuario, password vacío."""
    return httpx.BasicAuth(_SHARE_TOKEN, "") if _SHARE_TOKEN else None


def _client(proxy: str, timeout: int) -> httpx.Client:
    """Cliente httpx con auth del share (proxy opcional; compat httpx nuevo/viejo)."""
    kw = dict(timeout=timeout, headers=_HEADERS, follow_redirects=True, auth=_auth())
    if not proxy:
        return httpx.Client(**kw)
    try:
        return httpx.Client(proxy=proxy, **kw)
    except TypeError:
        return httpx.Client(proxies=proxy, **kw)


def _resolver_periodo(client: httpx.Client, periodo: Optional[str]) -> str:
    """Devuelve el "AAAA-MM" del dump a usar. Si no se pidió, lista el directorio del
    share (PROPFIND WebDAV) y toma el AAAA-MM más reciente; si PROPFIND falla, cae a
    probar meses hacia atrás (HEAD a Estabelecimentos0.zip)."""
    if periodo:
        return periodo
    # 1) PROPFIND: listar subcarpetas AAAA-MM del share y tomar la última
    try:
        r = client.request("PROPFIND", _BASE_URL, headers={"Depth": "1"})
        if r.status_code in (200, 207):
            meses = sorted(set(re.findall(r"/(\d{4}-\d{2})/", r.text)))
            if meses:
                return meses[-1]
    except httpx.HTTPError:
        pass
    # 2) Fallback: HEAD mes a mes hacia atrás
    hoy = datetime.now(timezone.utc)
    y, m = hoy.year, hoy.month
    for _ in range(8):
        ym = f"{y:04d}-{m:02d}"
        url = f"{_BASE_URL}{ym}/Estabelecimentos0.zip"
        try:
            if client.head(url).status_code == 200:
                return ym
        except httpx.HTTPError:
            pass
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    raise RuntimeError(f"no encontré ningún dump mensual reciente en {_BASE_URL} "
                       "(¿cambió el token del share o el layout?)")


def _local_dest(url: str, dump_dir: str) -> str:
    """Ruta local persistente del .zip: <dump_dir>/<periodo>/<archivo>.zip (de la URL)."""
    parts = url.rstrip("/").split("/")
    periodo, fname = parts[-2], parts[-1]
    return os.path.join(dump_dir, periodo, fname)


def _ensure_zip(client: httpx.Client, url: str, dest: str, reintentos: int = 5) -> str:
    """Asegura `dest` en disco y lo CONSERVA. Reuso: si ya está y su tamaño coincide con
    el Content-Length remoto, no re-descarga. Descarga atómica vía `.part`, con reintentos
    + validación de tamaño (el Nextcloud corta conexiones a mitad de archivos grandes)."""
    if os.path.exists(dest):
        try:
            exp = int(client.head(url).headers.get("content-length", "0"))
        except httpx.HTTPError:
            exp = 0
        if exp and os.path.getsize(dest) == exp:
            logger.info(f"ReceitaCNPJ: reuso {os.path.basename(dest)} (ya en disco, {exp} bytes)")
            return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    ult = ""
    for intento in range(1, reintentos + 1):
        try:
            with client.stream("GET", url) as r:
                r.raise_for_status()
                exp = int(r.headers.get("content-length", "0"))
                escrito = 0
                with open(part, "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=1 << 20):
                        f.write(chunk)
                        escrito += len(chunk)
            if exp and escrito != exp:           # truncado sin excepción
                raise httpx.RemoteProtocolError(f"descarga incompleta: {escrito}/{exp} bytes")
            os.replace(part, dest)
            return dest
        except httpx.HTTPError as e:
            ult = f"{type(e).__name__}: {e}"
            logger.warning(f"ReceitaCNPJ: fallo bajando {os.path.basename(dest)} "
                           f"(intento {intento}/{reintentos}): {ult}")
            time.sleep(min(30, 5 * intento))
    raise RuntimeError(f"no pude bajar {url} tras {reintentos} intentos: {ult}")


def _iter_csv_de_zip(client: httpx.Client, url: str, dump_dir: str):
    """Asegura el .zip en disco (reuso por tamaño; **no se borra**, queda para re-filtrar
    otros rubros) y yieldea cada fila (lista) de su CSV (';' latin-1, sin header).
    Streaming desde el zip: nunca carga el CSV entero en memoria."""
    dest = _ensure_zip(client, url, _local_dest(url, dump_dir))
    with zipfile.ZipFile(dest) as zf:
        for name in zf.namelist():
            with zf.open(name) as raw:
                wrapper = io.TextIOWrapper(raw, encoding="latin-1", newline="")
                yield from csv.reader(wrapper, delimiter=";", quotechar='"')


def _g(row: list, i: int) -> str:
    return (row[i].strip() if i < len(row) else "") or ""


@agent_run
def run(input: ReceitaCNPJInput) -> ReceitaCNPJOutput:
    out = ReceitaCNPJOutput()
    proxy = _resolver_proxy(input)  # "" = directo (el share es global, sin proxy)

    ufs = {u.upper() for u in input.ufs} if input.ufs else None
    t0 = time.time()

    with _client(proxy, input.timeout_s) as client:
        periodo = _resolver_periodo(client, input.periodo)
        out.periodo = periodo
        base = f"{_BASE_URL}{periodo}/"
        logger.info(f"ReceitaCNPJ: dump {periodo} (UFs={ufs or 'todas'})")

        # 1) Estabelecimentos: filtrar CNAE hospedagem (+ UF opcional)
        estab: dict[str, dict] = {}
        for n in range(_N_PARTES):
            url = f"{base}Estabelecimentos{n}.zip"
            for row in _iter_csv_de_zip(client, url, input.dump_dir):
                if _g(row, _E_CNAE) not in _CNAE_HOSPEDAGEM:
                    continue
                uf = _g(row, _E_UF)
                if ufs and uf not in ufs:
                    continue
                basico = _g(row, _E_BASICO)
                cnpj = f"{basico}{_g(row, _E_ORDEM)}{_g(row, _E_DV)}"
                sit = _g(row, _E_SIT)
                estab[cnpj] = {
                    "cnpj": cnpj, "cnpj_basico": basico,
                    "matriz_filial": _g(row, _E_MATRIZ) or None,
                    "razao_social": None, "nome_fantasia": _g(row, _E_FANTASIA) or None,
                    "cnae_principal": _g(row, _E_CNAE),
                    "situacao_cadastral": sit or None, "situacao": _SITUACAO.get(sit),
                    "data_situacao": _g(row, _E_DATA_SIT) or None,
                    "data_inicio_atividade": _g(row, _E_DATA_INI) or None,
                    "tipo_logradouro": _g(row, _E_TIPO_LOGR) or None,
                    "logradouro": _g(row, _E_LOGR) or None,
                    "numero": _g(row, _E_NUM) or None,
                    "complemento": _g(row, _E_COMPL) or None,
                    "bairro": _g(row, _E_BAIRRO) or None,
                    "cep": _g(row, _E_CEP) or None, "uf": uf or None,
                    "municipio_rf": _g(row, _E_MUN) or None, "municipio_nome": None,
                    "telefone": (f"{_g(row, _E_DDD1)}{_g(row, _E_TEL1)}" or None),
                    "lat": None, "lng": None, "geocode_source": None,
                    "periodo": periodo,
                }
            logger.info(f"ReceitaCNPJ: Estabelecimentos{n} → {len(estab)} hospedagem acum.")

        if not estab:
            return ReceitaCNPJOutput(ok=False, periodo=periodo,
                                     error="0 establecimientos de hospedagem (¿CNAE/layout?)")

        # 2) Empresas: razão social por cnpj_basico (opcional)
        if input.incluir_razao_social:
            basicos = {e["cnpj_basico"] for e in estab.values()}
            razao: dict[str, str] = {}
            for n in range(_N_PARTES):
                url = f"{base}Empresas{n}.zip"
                for row in _iter_csv_de_zip(client, url, input.dump_dir):
                    b = _g(row, 0)
                    if b in basicos and b not in razao:
                        razao[b] = _g(row, 1) or None
            for e in estab.values():
                e["razao_social"] = razao.get(e["cnpj_basico"])
            out.con_razao = sum(1 for e in estab.values() if e["razao_social"])

        # 3) Municipios: código RF → nome
        try:
            muni: dict[str, str] = {}
            for row in _iter_csv_de_zip(client, f"{base}Municipios.zip", input.dump_dir):
                if len(row) >= 2:
                    muni[_g(row, 0)] = _g(row, 1)
            for e in estab.values():
                if e["municipio_rf"]:
                    e["municipio_nome"] = muni.get(e["municipio_rf"])
        except httpx.HTTPError as e:
            logger.warning(f"ReceitaCNPJ: Municipios.zip falló ({e}); sigo sin nombre de município")

    # 4) Carga idempotente: TRUNCATE + bulk insert
    filas = list(estab.values())
    cols = list(filas[0].keys())
    engine = get_engine()
    insert_sql = text(
        f"INSERT INTO receita_estabelecimentos_hospedagem ({', '.join(cols)}) "
        f"VALUES ({', '.join(':' + c for c in cols)})")
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE receita_estabelecimentos_hospedagem"))
        for i in range(0, len(filas), 5000):
            conn.execute(insert_sql, filas[i:i + 5000])

    out.estabelecimientos = len(filas)
    por_uf: dict[str, int] = {}
    for e in filas:
        por_uf[e["uf"] or "??"] = por_uf.get(e["uf"] or "??", 0) + 1
    out.por_uf = dict(sorted(por_uf.items(), key=lambda x: -x[1]))
    logger.info(f"ReceitaCNPJ: cargados {len(filas)} en {time.time()-t0:.0f}s (dump {periodo})")
    return out
