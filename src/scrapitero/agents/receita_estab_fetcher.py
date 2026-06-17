"""ReceitaEstabFetcher — universo CNPJ clasificado por categoría (taxonomía del cliente).

Re-escanea los .zip del dump de Receita **ya guardados en disco** (sin re-descargar) y carga
`receita_estabelecimentos` (migración 028) con los establecimientos cuyo CNAE mapea a la
taxonomía R/C/E (`agents/receita_categorias.py`): BAR, RESTAURANTE, ESCOLA, HOSPITAL,
SHOPPING, SUPERMERCADO, etc. — el universo "no hospedagem" que complementa a
`receita_estabelecimentos_hospedagem`.

Como el dump es nacional y rubros como COMÉRCIO/INDÚSTRIA son enormes, **se filtra por UF**
(default MT, el estado de los relevamientos) para no cargar millones de filas. Trae la
`natureza_juridica` del archivo Empresas (público/particular → variante exacta de la
descripción) además de la razão social.

Idempotente por UF (TRUNCATE de las UFs pedidas + reinsert). Se geocodifica aparte con
`GeocodebrFetcher` y se aterriza sobre las parcelas con el linker de `parcela_categoria`.
"""

from __future__ import annotations

import time
from typing import Optional

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.receita_categorias import clasificar
from scrapitero.agents.receita_cnpj_fetcher import (
    _BASE_URL, _E_BAIRRO, _E_BASICO, _E_CEP, _E_CNAE, _E_COMPL, _E_DV, _E_FANTASIA,
    _E_LOGR, _E_MUN, _E_NUM, _E_ORDEM, _E_SIT, _E_TIPO_LOGR, _E_UF, _N_PARTES, _SITUACAO,
    _client, _g, _iter_csv_de_zip, _resolver_periodo, _resolver_proxy,
)
from scrapitero.agents.baseline_geocoder import _tg
from scrapitero.db.engine import get_engine


class ReceitaEstabInput(BaseModel):
    ufs: list[str] = ["MT"]                    # UFs a cargar (filtro por fila); default MT
    periodo: Optional[str] = None              # "AAAA-MM"; None = el más reciente
    proxy: Optional[str] = None
    timeout_s: int = 120
    dump_dir: str = ""                         # "" = default del agente de Receita


class ReceitaEstabOutput(BaseModel):
    ok: bool = True
    error: Optional[str] = None
    periodo: Optional[str] = None
    establecimientos: int = 0
    por_categoria: dict = {}
    por_descripcion: dict = {}


@agent_run
def run(input: ReceitaEstabInput) -> ReceitaEstabOutput:
    import os
    out = ReceitaEstabOutput()
    proxy = _resolver_proxy(input)             # opcional (Nextcloud es global)
    ufs = {u.upper() for u in input.ufs}
    if not ufs:
        return ReceitaEstabOutput(ok=False, error="hay que indicar al menos una UF (p.ej. ['MT'])")
    dump_dir = input.dump_dir or os.environ.get("RECEITA_DUMP_DIR", "/opt/scrapitero/receita_dump")
    t0 = time.time()

    with _client(proxy, input.timeout_s) as client:
        periodo = _resolver_periodo(client, input.periodo)
        out.periodo = periodo
        base = f"{_BASE_URL}{periodo}/"
        logger.info(f"ReceitaEstab: dump {periodo}, UFs={sorted(ufs)}")
        _tg(f"🏢 <b>Receita establecimientos</b> {sorted(ufs)} (dump {periodo}): escaneando…")

        # 1) Estabelecimentos: quedarse con los CNAE de la taxonomía, de las UFs pedidas
        estab: dict[str, dict] = {}
        for n in range(_N_PARTES):
            for row in _iter_csv_de_zip(client, f"{base}Estabelecimentos{n}.zip", dump_dir):
                uf = _g(row, _E_UF)
                if uf not in ufs:
                    continue
                cnae = _g(row, _E_CNAE)
                if clasificar(cnae) is None:           # no mapea a la taxonomía
                    continue
                basico = _g(row, _E_BASICO)
                cnpj = f"{basico}{_g(row, _E_ORDEM)}{_g(row, _E_DV)}"
                sit = _g(row, _E_SIT)
                estab[cnpj] = {
                    "cnpj": cnpj, "cnpj_basico": basico,
                    "razao_social": None, "nome_fantasia": _g(row, _E_FANTASIA) or None,
                    "cnae_principal": cnae, "categoria": None, "descripcion": None,
                    "natureza_juridica": None,
                    "situacao_cadastral": sit or None, "situacao": _SITUACAO.get(sit),
                    "tipo_logradouro": _g(row, _E_TIPO_LOGR) or None,
                    "logradouro": _g(row, _E_LOGR) or None, "numero": _g(row, _E_NUM) or None,
                    "complemento": _g(row, _E_COMPL) or None, "bairro": _g(row, _E_BAIRRO) or None,
                    "cep": _g(row, _E_CEP) or None, "uf": uf or None,
                    "municipio_rf": _g(row, _E_MUN) or None, "municipio_nome": None,
                    "lat": None, "lng": None, "geocode_source": None, "periodo": periodo,
                }
            logger.info(f"ReceitaEstab: Estabelecimentos{n} → {len(estab)} acum.")

        if not estab:
            return ReceitaEstabOutput(ok=False, periodo=periodo,
                                      error=f"0 establecimientos en {sorted(ufs)} (¿UF/CNAE?)")

        # 2) Empresas: razão social + natureza jurídica por cnpj_basico
        basicos = {e["cnpj_basico"] for e in estab.values()}
        razao: dict[str, str] = {}
        natur: dict[str, str] = {}
        for n in range(_N_PARTES):
            for row in _iter_csv_de_zip(client, f"{base}Empresas{n}.zip", dump_dir):
                b = _g(row, 0)
                if b in basicos:
                    razao.setdefault(b, _g(row, 1) or None)
                    natur.setdefault(b, _g(row, 2) or None)   # col 2 = natureza jurídica

        # 3) Municipios: código RF → nome
        muni: dict[str, str] = {}
        try:
            for row in _iter_csv_de_zip(client, f"{base}Municipios.zip", dump_dir):
                if len(row) >= 2:
                    muni[_g(row, 0)] = _g(row, 1)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ReceitaEstab: Municipios.zip falló ({e}); sigo sin nombre")

    # 4) Clasificación final (con natureza) + enriquecimiento
    for e in estab.values():
        b = e["cnpj_basico"]
        e["razao_social"] = razao.get(b)
        e["natureza_juridica"] = natur.get(b)
        cls = clasificar(e["cnae_principal"], e["natureza_juridica"])
        if cls:
            e["categoria"], e["descripcion"] = cls
        if e["municipio_rf"]:
            e["municipio_nome"] = muni.get(e["municipio_rf"])

    # 5) Carga idempotente por UF (TRUNCATE de esas UFs + bulk insert)
    filas = list(estab.values())
    cols = list(filas[0].keys())
    engine = get_engine()
    insert_sql = text(f"INSERT INTO receita_estabelecimentos ({', '.join(cols)}) "
                      f"VALUES ({', '.join(':' + c for c in cols)})")
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM receita_estabelecimentos WHERE uf = ANY(:ufs)"),
                     {"ufs": sorted(ufs)})
        for i in range(0, len(filas), 5000):
            conn.execute(insert_sql, filas[i:i + 5000])

    out.establecimientos = len(filas)
    for e in filas:
        out.por_categoria[e["categoria"] or "?"] = out.por_categoria.get(e["categoria"] or "?", 0) + 1
        out.por_descripcion[e["descripcion"] or "?"] = out.por_descripcion.get(e["descripcion"] or "?", 0) + 1
    out.por_descripcion = dict(sorted(out.por_descripcion.items(), key=lambda x: -x[1]))
    _tg(f"🏢 <b>Receita establecimientos</b> {sorted(ufs)}: {len(filas)} cargados "
        f"({out.por_categoria}).")
    logger.info(f"ReceitaEstab fin: {len(filas)} en {time.time()-t0:.0f}s {out.por_categoria}")
    return out
