"""CadasturLocalFetcher — descarga y consolida Cadastur localmente (tabla `cadastur_hospedagem`).

Cadastur (registro oficial de Meios de Hospedagem, Ministério do Turismo) es la única
fuente del **conteo exacto de habitaciones (UH/leitos)** por CNPJ. El portal CKAN
(`dados.turismo.gov.br`) es intermitente (502). Para que un relevamiento futuro NO dependa
del portal, este agente baja TODOS los trimestres parseables, filtra por município y los
**consolida por CNPJ** en `cadastur_hospedagem` (migración 039). HotelFetcher luego lee esa
tabla primero (portal como fallback).

Catálogo del package `meios-de-hospedagem` (51 recursos, revisado 2026-06):
  • `...cadasturpj.csv` (CSV real, CON UH): 2006 → Q3 2021. VG ~316 hoteles.
  • `.xlsx` (esquema rico, CON UH): Q4 2024 → Q3 2025 + Q1 2026. Se parsean con stdlib
    (zipfile + xml), sin openpyxl. VG Q3 2025 ~12 (solo vigentes).
  • `.xls` BIFF (Q4 2022 – Q2 2024): NO parseables (no hay xlrd/openpyxl, no se instala) → gap.
  • `meio-de-hospedagem-.csv` (Q1 2022, Q4 2021): tipo Receita, SIN UH → se ignoran.

Consolidación: por CNPJ se toma el registro del trimestre más reciente (identidad/situação) y
se **coalesce UH/leitos del más reciente que los traiga** (no perder habitaciones si el snapshot
nuevo las omite). Carga idempotente (upsert por CNPJ).
"""

from __future__ import annotations

import csv
import io
import os
import re
import zipfile
from datetime import date
from typing import Optional
from xml.etree import ElementTree as ET

import httpx
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.hotel_fetcher import _CKAN_PACKAGE, _col, _entero, _norm
from scrapitero.db.engine import get_engine

_HEADERS = {"User-Agent": "Mozilla/5.0 (scrapitero CadasturLocal)"}
_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class CadasturLocalInput(BaseModel):
    municipios: list[str] = ["Várzea Grande"]          # nombres de município a guardar
    uf: str = "MT"
    incluir_xlsx: bool = True                          # parsear también los .xlsx (2024-2026)
    timeout_s: int = 90
    dump_dir: str = os.environ.get("CADASTUR_DUMP_DIR", "/opt/scrapitero/cadastur_dump")


class CadasturLocalOutput(BaseModel):
    ok: bool = True
    recursos_parseados: int = 0
    recursos_omitidos: int = 0          # .xls no parseables + sin UH
    trimestres: list[str] = []          # períodos efectivamente cargados (con UH)
    por_municipio: dict[str, int] = {}  # município → hoteles consolidados
    con_uh: int = 0
    total_consolidados: int = 0
    gap_xls: list[str] = []             # períodos en .xls que no se pudieron parsear
    error: Optional[str] = None


# ── Catálogo: clasificar cada recurso del package ──────────────────────────────

def _periodo_de_nombre(nombre: str) -> Optional[str]:
    """`Terceiro Trimestre 2025` → `2025T3`; `2014` → `2014`. None si no se reconoce."""
    n = _norm(nombre)
    tri = {"primeiro": 1, "segundo": 2, "terceiro": 3, "quarto": 4}
    m = re.search(r"(primeiro|segundo|terceiro|quarto)\s+trimestre\s+(?:de\s+)?(\d{4})", n)
    if m:
        return f"{m.group(2)}T{tri[m.group(1)]}"
    m = re.search(r"\b(19|20)\d{2}\b", n)
    return m.group(0) if m else None


def _clasificar(recurso: dict) -> dict:
    """Devuelve {url, periodo, ext, con_uh, parseable}."""
    url = (recurso.get("url") or "")
    ext = url.rsplit(".", 1)[-1].lower() if "." in url.rsplit("/", 1)[-1] else ""
    nombre = recurso.get("name") or ""
    periodo = _periodo_de_nombre(nombre) or _periodo_de_nombre(url)
    # CON UH: los cadasturpj.csv y todos los .xlsx (esquema rico). Los meio-de-hospedagem-.csv
    # (tipo Receita) NO traen UH.
    es_cadasturpj = "cadasturpj" in url.lower()
    con_uh = es_cadasturpj or ext == "xlsx"
    parseable = (ext == "csv" and es_cadasturpj) or ext == "xlsx"
    return {"url": url, "periodo": periodo, "ext": ext, "con_uh": con_uh,
            "parseable": parseable, "nombre": nombre,
            "last_modified": recurso.get("last_modified") or recurso.get("created") or ""}


# ── Lectores: CSV y XLSX (ambos → (headers, filas)) ────────────────────────────

def _leer_csv(data: bytes) -> tuple[list[str], list[list[str]]]:
    try:
        texto = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = data.decode("latin-1")
    primera = texto.splitlines()[0] if texto.splitlines() else ""
    sep = ";" if primera.count(";") >= primera.count(",") else ","
    filas = list(csv.reader(io.StringIO(texto), delimiter=sep))
    if not filas:
        return [], []
    return [h.strip() for h in filas[0]], filas[1:]


def _col_idx(ref: str) -> int:
    """`B7` → 1 (índice 0-based de la columna)."""
    s = re.match(r"[A-Z]+", ref).group()
    n = 0
    for ch in s:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _leer_xlsx(data: bytes) -> tuple[list[str], list[list[str]]]:
    """Lee la primera hoja de un .xlsx con stdlib (zipfile + xml). Devuelve (headers, filas).
    Mapea cada celda por su letra de columna (el .xlsx omite celdas vacías)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            st = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in st.findall(f"{_XLSX_NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_XLSX_NS}t")))
        hojas = sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml", n))
        if not hojas:
            return [], []
        sx = ET.fromstring(z.read(hojas[0]))
        sd = sx.find(f"{_XLSX_NS}sheetData")
        rows = sd.findall(f"{_XLSX_NS}row") if sd is not None else []

    def valores(row) -> list[str]:
        celdas: dict[int, str] = {}
        for c in row.findall(f"{_XLSX_NS}c"):
            v = c.find(f"{_XLSX_NS}v")
            if v is None or v.text is None:
                continue
            celdas[_col_idx(c.get("r"))] = shared[int(v.text)] if c.get("t") == "s" else v.text
        ancho = (max(celdas) + 1) if celdas else 0
        return [celdas.get(i, "") for i in range(ancho)]

    if not rows:
        return [], []
    headers = [h.strip() for h in valores(rows[0])]
    return headers, [valores(r) for r in rows[1:]]


# ── Mapper: fila (cualquier esquema) → registro común ──────────────────────────

def _pick(*idxs):
    """Primer índice no-None (no usar `a or b`: el índice 0 es válido)."""
    for i in idxs:
        if i is not None:
            return i
    return None


def _registro(headers: list[str], fila: list[str]) -> Optional[dict]:
    hn = {_norm(h): i for i, h in enumerate(headers)}
    col = lambda *ks: _col(hn, *ks)
    cell = lambda i: (fila[i].strip() if (i is not None and i < len(fila)) else "")
    ci = {
        "cnpj": col("cnpj"),
        "razao": col("razao social", "nome da pessoa juridica", "razao"),
        "fantasia": col("nome fantasia",),
        "uf": col("uf",),
        "mun": col("municipio", "localidade"),
        # tipo: preferir "Tipo de Hospedagem" sobre "Atividade(Turística)"
        "tipo": _pick(col("tipo de hospedagem"), col("atividade", "tipo")),
        "uh": col("unidade habitacionais", "unidades habitacionais", "uh"),
        "leitos": col("total de leitos", "leitos"),
        # situação: preferir "Situação da Atividade" (abierto/cerrado) sobre cadastral/trâmite
        "sit": _pick(col("situacao da atividade"), col("situacao", "situa")),
        # dirección: logradouro propio > endereço comercial > endereço (Receita)
        "logr": _pick(col("logradouro"), col("endereco completo comercial"),
                      col("endereco completo", "endereco")),
        # Cadastur NO tiene columna de número de calle (las "Número de…" son CNPJ /
        # certificado). El número, si existe, va dentro del logradouro → se extrae en el
        # dedupe con separar_numero. Acá numero=None siempre.
        "num": None,
        "bairro": col("bairro",),
        "compl": col("complemento",),
        "cep": col("cep",),
        "tel": _pick(col("telefone comercial"), col("telefone institucional"), col("telefone")),
    }
    cnpj = re.sub(r"\D", "", cell(ci["cnpj"]))[:20]
    if not cnpj:
        return None
    # El "Endereço Completo" del xlsx es un blob ("Rua X 50 ... CEP: 78110900 MT"): cortamos
    # en "CEP:" para quedarnos con la calle (el CSV cadasturpj ya trae el logradouro limpio).
    logr = re.split(r"\s*-?\s*CEP[:\s]", cell(ci["logr"]), maxsplit=1)[0].strip()
    return {
        "cnpj": cnpj,
        "razao_social": cell(ci["razao"]) or None,
        "nome_fantasia": cell(ci["fantasia"]) or None,
        "uf": (cell(ci["uf"])[:2].upper() or None),
        "municipio": cell(ci["mun"]) or None,
        "tipo_hospedagem": cell(ci["tipo"])[:80] or None,
        "uh": _entero(cell(ci["uh"])),
        "leitos": _entero(cell(ci["leitos"])),
        "situacao": cell(ci["sit"])[:60] or None,
        "logradouro": logr or None,
        "numero": cell(ci["num"])[:30] or None,
        "bairro": cell(ci["bairro"])[:160] or None,
        "complemento": cell(ci["compl"]) or None,
        "cep": re.sub(r"\D", "", cell(ci["cep"]))[:12] or None,
        "telefone": cell(ci["tel"])[:40] or None,
    }


# ── Descarga con reuso en disco ────────────────────────────────────────────────

def _bajar(client: httpx.Client, url: str, dest: str) -> bytes:
    """Baja el recurso a `dest` reusando si ya existe (no se borra). Devuelve los bytes."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        with open(dest, "rb") as f:
            return f.read()
    r = client.get(url)
    r.raise_for_status()
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(r.content)
    os.replace(tmp, dest)
    return r.content


@agent_run
def run(input: CadasturLocalInput) -> CadasturLocalOutput:
    out = CadasturLocalOutput()
    munis_norm = {_norm(m) for m in input.municipios}
    uf_norm = _norm(input.uf)
    # cnpj → mejor registro consolidado (+ metadatos de período para coalescer)
    consol: dict[str, dict] = {}

    with httpx.Client(timeout=input.timeout_s, headers=_HEADERS, follow_redirects=True) as client:
        try:
            pkg = (client.get(_CKAN_PACKAGE).json().get("result") or {})
        except Exception as e:  # noqa: BLE001
            out.ok = False
            out.error = f"portal CKAN sin responder: {e!r}"
            return out
        recursos = [_clasificar(r) for r in (pkg.get("resources") or [])]

        # Solo los que aportan UH (cadasturpj.csv + xlsx); ordenar por fecha desc para que el
        # primero que toca cada CNPJ sea el más reciente.
        candidatos = [r for r in recursos if r["con_uh"]]
        if not input.incluir_xlsx:
            candidatos = [r for r in candidatos if r["ext"] != "xlsx"]
        candidatos.sort(key=lambda r: r["last_modified"], reverse=True)

        out.gap_xls = sorted({r["periodo"] or "?" for r in recursos
                              if r["ext"] == "xls" and r["con_uh"]}, reverse=True)
        logger.info(f"Cadastur: {len(recursos)} recursos; {len(candidatos)} con UH a procesar; "
                    f"{len(out.gap_xls)} en .xls no parseables (gap: {', '.join(out.gap_xls) or '—'})")

        for r in candidatos:
            periodo = r["periodo"] or "?"
            ext = r["ext"]
            dest = os.path.join(input.dump_dir, f"{periodo}.{ext}")
            try:
                data = _bajar(client, r["url"], dest)
                headers, filas = _leer_xlsx(data) if ext == "xlsx" else _leer_csv(data)
            except Exception as e:  # noqa: BLE001 — best-effort por recurso
                out.recursos_omitidos += 1
                logger.warning(f"Cadastur {periodo} ({ext}): no se pudo procesar — {e!r}")
                continue
            if not headers:
                out.recursos_omitidos += 1
                continue

            fecha = None
            try:
                fecha = date.fromisoformat(r["last_modified"][:10]) if r["last_modified"] else None
            except ValueError:
                pass

            n_muni = 0
            for fila in filas:
                reg = _registro(headers, fila)
                if not reg:
                    continue
                if uf_norm and reg["uf"] and _norm(reg["uf"]) != uf_norm:
                    continue
                if munis_norm and _norm(reg["municipio"] or "") not in munis_norm:
                    continue
                n_muni += 1
                reg["fonte_trimestre"] = periodo
                reg["fonte_fecha"] = fecha
                _consolidar(consol, reg)
            logger.info(f"Cadastur {periodo} ({ext}): {len(filas)} filas, {n_muni} de los municípios")
            out.recursos_parseados += 1
            if n_muni:
                out.trimestres.append(periodo)

    if not consol:
        out.ok = False
        out.error = ("Cadastur: ningún hotel de los municípios pedidos en los recursos "
                     "parseables (¿município/UF mal escritos?)")
        return out

    _guardar(consol)
    out.total_consolidados = len(consol)
    out.con_uh = sum(1 for v in consol.values() if v.get("uh"))
    for v in consol.values():
        m = v.get("municipio") or "?"
        out.por_municipio[m] = out.por_municipio.get(m, 0) + 1
    out.trimestres = sorted(set(out.trimestres), reverse=True)
    logger.info(f"Cadastur local: {out.total_consolidados} hoteles consolidados "
                f"({out.con_uh} con UH) de {out.recursos_parseados} trimestres → cadastur_hospedagem")
    return out


def _consolidar(consol: dict, reg: dict) -> None:
    """Une por CNPJ. Como los recursos llegan de más nuevo a más viejo, el PRIMERO que toca un
    CNPJ es el de identidad/situação (más reciente); los siguientes solo rellenan UH/leitos si
    faltan (preserva el conteo de habitaciones más reciente disponible)."""
    cnpj = reg["cnpj"]
    cur = consol.get(cnpj)
    if cur is None:
        reg["uh_fonte_trimestre"] = reg["fonte_trimestre"] if reg.get("uh") else None
        consol[cnpj] = reg
        return
    # ya hay uno más reciente: completar solo lo que falte
    for k in ("razao_social", "nome_fantasia", "tipo_hospedagem", "logradouro", "numero",
              "bairro", "complemento", "cep", "telefone", "situacao", "municipio", "uf"):
        if not cur.get(k) and reg.get(k):
            cur[k] = reg[k]
    if not cur.get("uh") and reg.get("uh"):
        cur["uh"] = reg["uh"]
        cur["uh_fonte_trimestre"] = reg["fonte_trimestre"]
    if not cur.get("leitos") and reg.get("leitos"):
        cur["leitos"] = reg["leitos"]


def _guardar(consol: dict) -> None:
    cols = ["cnpj", "razao_social", "nome_fantasia", "uf", "municipio", "tipo_hospedagem",
            "uh", "leitos", "situacao", "logradouro", "numero", "bairro", "complemento",
            "cep", "telefone", "fonte_trimestre", "fonte_fecha", "uh_fonte_trimestre"]
    sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c != "cnpj")
    sql = text(
        f"INSERT INTO cadastur_hospedagem ({', '.join(cols)}, updated_at) "
        f"VALUES ({', '.join(':' + c for c in cols)}, now()) "
        f"ON CONFLICT (cnpj) DO UPDATE SET {sets}, updated_at=now()"
    )
    engine = get_engine()
    with engine.begin() as conn:
        for reg in consol.values():
            conn.execute(sql, {c: reg.get(c) for c in cols})
