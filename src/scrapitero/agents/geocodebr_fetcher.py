"""GeocodebrFetcher — geocoding offline y gratis de direcciones brasileñas (CNEFE/IBGE).

Capa **gratuita** de geocoding para Brasil, basada en el paquete R **{geocodebr}** (IPEA)
sobre el CNEFE del IBGE. Geocodifica en **lote** (un subproceso `Rscript` carga el CNEFE una
vez y resuelve miles de direcciones) — por eso es un agente batch, no un geocoder por-dirección.

Hoy su uso es poblar las coordenadas de `receita_estabelecimentos_hospedagem` (que llega sin
coordenadas), para que `HotelFetcher` pueda recortar a la zona y vincular a parcela los hoteles
de la fuente Receita. Es **idempotente/resumible**: solo procesa filas sin geocodificar
(`lat IS NULL AND geocode_source IS NULL`).

Cada resultado trae un nivel de `precisao` (numero/numero_aproximado/logradouro/cep/localidade/
municipio) y un `desvio_metros` (incertidumbre). Solo se escriben coordenadas cuando el desvío
es ≤ `max_desvio_m` (default 300 m: incluye número/calle/CEP, descarta bairro≈1,4 km y município≈7 km).
A las filas demasiado gruesas se les sella `geocode_source` (p.ej. `g:municipio`) con lat/lng en
NULL, para no reprocesarlas y poder mandarlas luego a un fallback pago (Google) si se quiere.

Requiere R + el paquete `geocodebr` instalados en el host (`Rscript`). El worker R es
`geocodebr_geocode.R` (en esta misma carpeta).
"""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
import tempfile
from typing import Optional

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.baseline_geocoder import _tg
from scrapitero.db.engine import get_engine

_R_WORKER = os.path.join(os.path.dirname(__file__), "geocodebr_geocode.R")
# Niveles de geocodebr que NO sirven para ubicar (centroides gruesos), aunque pasen el desvío.
_PRECISION_DESCARTE = {"localidade", "municipio"}


# Tablas geocodificables (allowlist: el nombre va inline en el SQL, no parametrizable).
_TABLAS_OK = {"receita_estabelecimentos_hospedagem", "receita_estabelecimentos"}


class GeocodebrInput(BaseModel):
    tabla: str = "receita_estabelecimentos_hospedagem"  # o "receita_estabelecimentos"
    uf: Optional[str] = None                 # filtrar a una UF (p.ej. "MT"); None = todas
    municipio_nome: Optional[str] = None     # filtrar a un município (ILIKE); None = todos
    solo_faltantes: bool = True              # solo filas sin geocodificar (resumible)
    max_desvio_m: float = 300.0              # escribir coords solo si desvio_metros ≤ esto
    chunk: int = 20000                       # filas por corrida del worker R
    limite: Optional[int] = None             # tope de filas (debug)


class GeocodebrOutput(BaseModel):
    ok: bool = True
    error: Optional[str] = None
    candidatos: int = 0
    geocodificados: int = 0                  # con coords escritas (desvío ok)
    descartados: int = 0                     # geocodebr resolvió pero muy grueso
    por_precision: dict = {}


def _rscript_bin() -> Optional[str]:
    return shutil.which("Rscript")


def geocode_batch(rows: list[dict], rscript: Optional[str] = None) -> dict[str, dict]:
    """Geocodifica una lista de direcciones BR con geocodebr (subproceso Rscript).

    rows: dicts con `id`, `logradouro`, `numero`, `bairro`, `municipio`, `estado`, `cep`.
    Devuelve {id: {lat, lng, precisao, desvio_metros, cod_setor, endereco_encontrado}}.
    """
    rscript = rscript or _rscript_bin()
    if not rscript:
        raise RuntimeError("Rscript no encontrado: instalá R + el paquete geocodebr")
    if not rows:
        return {}
    cols = ["id", "logradouro", "numero", "bairro", "municipio", "estado", "cep"]
    tmpdir = tempfile.mkdtemp(prefix="geocodebr_")
    inp, outp = os.path.join(tmpdir, "in.csv"), os.path.join(tmpdir, "out.csv")
    try:
        with open(inp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({c: ("" if r.get(c) is None else str(r.get(c))) for c in cols})
        proc = subprocess.run([rscript, _R_WORKER, inp, outp],
                              capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0 or not os.path.exists(outp):
            raise RuntimeError(f"worker geocodebr falló (rc={proc.returncode}): "
                               f"{(proc.stderr or proc.stdout or '').strip()[:300]}")
        res: dict[str, dict] = {}
        with open(outp, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                def _f(k):
                    v = (row.get(k) or "").strip()
                    try:
                        return float(v) if v else None
                    except ValueError:
                        return None
                res[row["id"]] = {
                    "lat": _f("lat"), "lng": _f("lon"),
                    "precisao": (row.get("precisao") or "").strip() or None,
                    "desvio_metros": _f("desvio_metros"),
                    "cod_setor": (row.get("cod_setor") or "").strip() or None,
                    "endereco_encontrado": (row.get("endereco_encontrado") or "").strip() or None,
                }
        return res
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@agent_run
def run(input: GeocodebrInput) -> GeocodebrOutput:
    if not _rscript_bin():
        return GeocodebrOutput(ok=False, error="Rscript no encontrado: instalá R + geocodebr")
    if input.tabla not in _TABLAS_OK:
        return GeocodebrOutput(ok=False, error=f"tabla no permitida: {input.tabla}")
    tabla = input.tabla

    engine = get_engine()
    filtros, params = [], {}
    if input.solo_faltantes:
        filtros.append("lat IS NULL AND geocode_source IS NULL")
    if input.uf:
        filtros.append("uf = :uf"); params["uf"] = input.uf.upper()
    if input.municipio_nome:
        filtros.append("municipio_nome ILIKE :mun"); params["mun"] = input.municipio_nome
    where = (" WHERE " + " AND ".join(filtros)) if filtros else ""
    lim = f" LIMIT {int(input.limite)}" if input.limite else ""

    with engine.connect() as conn:
        cand = conn.execute(text(f"""
            SELECT cnpj,
                   TRIM(COALESCE(tipo_logradouro,'')||' '||COALESCE(logradouro,'')) AS logradouro,
                   COALESCE(numero,'') AS numero, COALESCE(bairro,'') AS bairro,
                   COALESCE(municipio_nome,'') AS municipio, COALESCE(uf,'') AS estado,
                   COALESCE(cep,'') AS cep
            FROM {tabla}{where}{lim}
        """), params).fetchall()

    out = GeocodebrOutput(candidatos=len(cand))
    if not cand:
        _tg("📍 geocodebr: 0 direcciones para geocodificar.")
        return out
    _tg(f"📍 <b>geocodebr</b>: geocodificando {len(cand)} direcciones "
        f"({input.uf or 'todas'}{'/' + input.municipio_nome if input.municipio_nome else ''})…")

    rows = [{"id": r.cnpj, "logradouro": r.logradouro, "numero": r.numero, "bairro": r.bairro,
             "municipio": r.municipio, "estado": r.estado, "cep": r.cep} for r in cand]

    for i in range(0, len(rows), max(input.chunk, 1)):
        lote = rows[i:i + input.chunk]
        res = geocode_batch(lote)
        with engine.begin() as conn:
            for cnpj, g in res.items():
                prec = g.get("precisao")
                desv = g.get("desvio_metros")
                out.por_precision[prec or "?"] = out.por_precision.get(prec or "?", 0) + 1
                usable = (g["lat"] is not None and prec not in _PRECISION_DESCARTE
                          and (desv is None or desv <= input.max_desvio_m))
                if usable:
                    conn.execute(text(f"""
                        UPDATE {tabla}
                        SET lat=:lat, lng=:lng, geocode_source=:src WHERE cnpj=:cnpj
                    """), {"lat": g["lat"], "lng": g["lng"],
                           "src": f"g:{prec}"[:20], "cnpj": cnpj})
                    out.geocodificados += 1
                else:
                    # sellar para no reprocesar (lat queda NULL → candidato a fallback pago)
                    conn.execute(text(f"""
                        UPDATE {tabla}
                        SET geocode_source=:src WHERE cnpj=:cnpj
                    """), {"src": f"g:{prec}"[:20] if prec else "g:?", "cnpj": cnpj})
                    out.descartados += 1
        logger.info(f"geocodebr: lote {i//input.chunk + 1} → "
                    f"{out.geocodificados} ok / {out.descartados} grueso (acum.)")

    out.por_precision = dict(sorted(out.por_precision.items(), key=lambda x: -x[1]))
    _tg(f"📍 <b>geocodebr</b>: {out.geocodificados} geocodificados (≤{input.max_desvio_m:.0f} m), "
        f"{out.descartados} muy gruesos. Niveles: {out.por_precision}.")
    logger.info(f"geocodebr fin: cand={out.candidatos} ok={out.geocodificados} "
                f"descartados={out.descartados} {out.por_precision}")
    return out
