#!/usr/bin/env python3
"""Refresco MENSUAL de toda fuente que se descarga a disco (cron de Hermes, patrón --no-agent).

**Por qué existe.** Las fuentes que el sistema baja como dump quedan congeladas en la fecha
en que alguien las cargó a mano, y nadie se entera. Medido el 3-sep-2026:

  · `receita_estabelecimentos`            → dump **2026-06**, cargado el 17-jun (2½ meses)
  · `receita_estabelecimentos_hospedagem` → dump **2026-07**, cargado el 24-jul

O sea que las dos mitades del mismo padrón estaban en meses distintos, y la que clasifica
comercios —la que sella `parcelas.descripcion_uso` y viaja al CSV y al plano— era la más
vieja. Peor: **el dump de julio ya estaba bajado en disco** (6,3 GB) y nunca se re-escaneó,
cuando re-escanear no cuesta ni red ni dinero. `CLAUDE.md` decía que un cron mensual
refrescaba la base; no existía: el único job de Hermes era `receita-proxy-watch`, que mira
si el portal responde y no actualiza nada.

Eso importa porque el dato que se envejece no es un número de contexto: es
`situacao` (ATIVA/BAIXADA), que decide si una empresa **sella o no** el uso de una parcela.
Con el corte de junio, la que cerró en julio sigue etiquetando su lote y la que abrió
después no existe.

**Qué refresca**, en orden y best-effort (si una falla, las otras siguen):

  1. `ReceitaCNPJFetcher`   — baja el dump del mes si hay uno nuevo y recarga el universo
                              de hospedagem. Reusa los `.zip` ya guardados.
  2. `ReceitaEstabFetcher`  — re-escanea **ese mismo dump** para las categorías del cliente
                              (comercios, escuelas, hospitales…). No vuelve a descargar.
  3. `CadasturLocalFetcher` — refresca el cache de hoteles con habitaciones (UH).
  4. Purga de dumps viejos  — 6,2 GB por mes; sin esto el disco se llena en un año.
  5. **Overture** — sondea el bucket y avisa si hay un release nuevo. **No lo adopta**:
                              mover el pin cambia los comercios del CSV y del plano, y eso
                              lo decide una persona (elección del operador, 3-sep-2026).

**El alcance sale de la base, no está hardcodeado.** Las UFs y los municípios se derivan de
`regions.municipio_codigo` de los relevamientos que existen, traducidos con la lista oficial
del IBGE (la misma implementación que usa `MunicipioCodigoFetcher`). Si mañana se releva otra
ciudad, este cron la incluye solo — no hay que acordarse de editar una lista con `MT` adentro.

Patrón `--no-agent`: lo que se imprime a stdout va tal cual a Telegram. A diferencia de un
watchdog, **este job SIEMPRE imprime**: es mensual, y un silencio de un mes no distingue
«todo al día» de «el cron se murió».
"""

from __future__ import annotations

import shutil
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/opt/scrapitero/.hermes-packages")
sys.path.insert(0, "/opt/scrapitero/src")

from sqlalchemy import text  # noqa: E402

_LOG = Path("/opt/data/refresco_mensual.log")
_DUMP_DIR = Path("/opt/scrapitero/receita_dump")

# Cuántos períodos de dump de Receita se conservan en disco. Dos: el vigente y el anterior,
# que es lo que permite re-filtrar o comparar sin volver a bajar 6 GB. Con más, el disco
# (96 GB, 56 libres hoy) no aguanta el año.
_DUMPS_A_CONSERVAR = 2



def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with _LOG.open("a") as f:
            f.write(f"{ts}\t{msg}\n")
    except OSError:
        pass


def _alcance(conn) -> tuple[list[str], dict[str, list[str]]]:
    """(UFs, {UF: [municípios]}) de los relevamientos que existen en la base.

    Se deriva de `regions.municipio_codigo` y no de una lista escrita a mano: es la misma
    regla de genericidad del resto del proyecto (nada de husos ni UFs fijas). Una región
    con el código en NULL no aporta nada acá — para eso está `MunicipioCodigoFetcher`.
    """
    from scrapitero.agents.municipio_codigo_fetcher import _municipios_ibge

    codigos = [r[0] for r in conn.execute(text("""
        SELECT DISTINCT municipio_codigo FROM regions
        WHERE country_code = 'BRA' AND municipio_codigo IS NOT NULL
    """))]
    por_id = {m["id"]: m for m in _municipios_ibge()}
    por_uf: dict[str, list[str]] = {}
    for cod in codigos:
        m = por_id.get(str(cod))
        if not m or not m.get("uf"):
            continue
        por_uf.setdefault(m["uf"], [])
        if m["nome"] not in por_uf[m["uf"]]:
            por_uf[m["uf"]].append(m["nome"])
    return sorted(por_uf), por_uf


def _periodo_en_db(conn, tabla: str) -> str | None:
    try:
        return conn.execute(text(f"SELECT MAX(periodo) FROM {tabla}")).scalar()
    except Exception:  # noqa: BLE001 — tabla ausente en una base nueva
        return None


def _purgar_dumps(conservar: int) -> list[str]:
    """Borra las carpetas de período más viejas y devuelve cuáles borró.

    Sólo toca subcarpetas con forma `AAAA-MM` dentro de `receita_dump/`: cualquier otra
    cosa que alguien haya dejado ahí se queda donde está.
    """
    if not _DUMP_DIR.exists():
        return []
    periodos = sorted(
        (d for d in _DUMP_DIR.iterdir()
         if d.is_dir() and len(d.name) == 7 and d.name[4] == "-" and d.name.replace("-", "").isdigit()),
        key=lambda d: d.name,
    )
    borrados = []
    for d in periodos[:-conservar] if conservar > 0 else []:
        try:
            gb = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 1e9
            shutil.rmtree(d)
            borrados.append(f"{d.name} ({gb:.1f} GB)")
        except OSError as e:
            _log(f"purga falló en {d}: {e}")
    return borrados


def _paso(nombre: str, fn) -> tuple[str, str]:
    """Corre un paso y devuelve (línea para Telegram, 'ok'|'error').

    Best-effort a propósito: que Receita esté caída no puede impedir refrescar Cadastur.
    Ante fallo, la línea lleva el `error` **textual** del agente — es la regla de
    mensajería del proyecto: el operador destraba si el mensaje dice qué falló.
    """
    try:
        return fn(), "ok"
    except Exception as e:  # noqa: BLE001
        _log(f"{nombre} EXCEPCIÓN: {traceback.format_exc()}")
        return f"❌ {nombre}: {type(e).__name__}: {e}"[:400], "error"


def main() -> int:
    from scrapitero.db.engine import get_engine

    eng = get_engine()
    lineas: list[str] = []
    errores = 0

    with eng.begin() as conn:
        ufs, municipios_por_uf = _alcance(conn)
        antes_hosp = _periodo_en_db(conn, "receita_estabelecimentos_hospedagem")
        antes_estab = _periodo_en_db(conn, "receita_estabelecimentos")

    if not ufs:
        print("⚠️ Refresco mensual: ninguna región brasilera tiene `municipio_codigo`, "
              "así que no hay UF que refrescar. Correr `municipio_codigo_fetcher` primero.")
        return 0

    # 1) Universo de hospedagem (baja el dump del mes si hay uno nuevo).
    #    ⚠ SIN filtro de UF, a propósito. La primera versión de este script le pasaba las
    #    UFs relevadas y eso **achicó la tabla de 202.087 filas (todo Brasil) a 5.790 (MT)**:
    #    una pérdida de cobertura que nadie habría notado hasta crear una zona en otro
    #    estado y encontrarla sin hoteles. Filtrar acá no ahorra descarga —los .zip se bajan
    #    enteros igual, el filtro es por fila— y el CNAE de hospedagem es tan angosto que
    #    todo Brasil entra en 200k filas. El filtro por UF sí hace falta en el paso 2, donde
    #    lo nacional son millones.
    def _hospedagem() -> str:
        from scrapitero.agents.receita_cnpj_fetcher import ReceitaCNPJInput
        from scrapitero.agents.receita_cnpj_fetcher import run as run_cnpj
        out = run_cnpj(ReceitaCNPJInput())
        if not out.ok:
            raise RuntimeError(out.error or "sin detalle")
        nuevo = out.periodo != antes_hosp
        return (f"{'🆕' if nuevo else '✅'} Receita hospedagem: dump {out.periodo}"
                f"{f' (antes {antes_hosp})' if nuevo else ' — ya estaba al día'}, "
                f"{out.estabelecimientos:,} establecimientos (todo Brasil)".replace(",", "."))

    linea, estado = _paso("Receita hospedagem", _hospedagem)
    lineas.append(linea)
    errores += estado == "error"

    # 2) Categorías del cliente sobre el MISMO dump. Es el paso que estaba atrasado dos
    #    meses teniendo el zip nuevo en disco: re-escanear no cuesta red ni dinero.
    def _categorias() -> str:
        from scrapitero.agents.receita_estab_fetcher import ReceitaEstabInput
        from scrapitero.agents.receita_estab_fetcher import run as run_estab
        out = run_estab(ReceitaEstabInput(ufs=ufs))
        if not out.ok:
            raise RuntimeError(out.error or "sin detalle")
        nuevo = out.periodo != antes_estab
        return (f"{'🆕' if nuevo else '✅'} Receita categorías: dump {out.periodo}"
                f"{f' (antes {antes_estab})' if nuevo else ' — ya estaba al día'}, "
                f"{out.establecimientos:,} en {'/'.join(ufs)}".replace(",", "."))

    linea, estado = _paso("Receita categorías", _categorias)
    lineas.append(linea)
    errores += estado == "error"

    # 3) Cadastur: el padrón con las habitaciones reales. El portal CKAN es intermitente
    #    (502), así que si falla no arrastra al resto — el cache local sigue sirviendo.
    def _cadastur() -> str:
        from scrapitero.agents.cadastur_local_fetcher import CadasturLocalInput
        from scrapitero.agents.cadastur_local_fetcher import run as run_cad
        partes, fallos = [], []
        for uf, munis in municipios_por_uf.items():
            out = run_cad(CadasturLocalInput(municipios=munis, uf=uf))
            if not out.ok:
                fallos.append(f"{uf}: {out.error}")
                continue
            partes.append(f"{uf} {out.total_consolidados} hoteles ({out.con_uh} con UH)")
        if fallos and not partes:
            raise RuntimeError("; ".join(fallos))
        aviso = f" · ⚠️ {'; '.join(fallos)}" if fallos else ""
        return f"✅ Cadastur: {' · '.join(partes)}{aviso}"

    linea, estado = _paso("Cadastur", _cadastur)
    lineas.append(linea)
    errores += estado == "error"

    # 4) Purga: el disco es el recurso que se agota en silencio.
    borrados = _purgar_dumps(_DUMPS_A_CONSERVAR)
    if borrados:
        lineas.append(f"🧹 Dumps viejos borrados: {', '.join(borrados)}")
    try:
        libre_gb = shutil.disk_usage("/opt").free / 1e9
        if libre_gb < 20:
            lineas.append(f"⚠️ Quedan {libre_gb:.0f} GB libres en /opt — un dump de Receita "
                          f"pesa ~6 GB, revisar antes del próximo refresco")
    except OSError:
        pass

    # 5) Overture: se DETECTA el release nuevo pero NO se adopta.
    #    Decisión del operador (3-sep-2026): adoptar un release cambia los comercios que
    #    salen al CSV y al plano del cliente, así que el pin lo mueve una persona. Acá sólo
    #    se avisa — que es lo que faltaba: estábamos dos releases atrás sin saberlo.
    def _overture() -> str:
        from scrapitero.agents.overture_places_fetcher import (
            _RELEASE_DEFAULT, release_mas_nuevo)
        nuevo_rel = release_mas_nuevo()
        if not nuevo_rel:
            return f"✅ Overture: {_RELEASE_DEFAULT} sigue siendo el último publicado"
        return (f"📌 Overture: hay release nuevo **{nuevo_rel}** y seguimos en "
                f"{_RELEASE_DEFAULT}. NO se adoptó solo a propósito: cambia los comercios "
                f"del CSV y del plano. Para adoptarlo, `_RELEASE_DEFAULT` en "
                f"`overture_places_fetcher` y re-correr Overture sobre las zonas activas.")

    linea, estado = _paso("Overture", _overture)
    lineas.append(linea)
    errores += estado == "error"

    cabecera = ("🗓️ Refresco mensual de fuentes descargadas — "
                + ("todo OK" if not errores else f"{errores} con error"))
    salida = cabecera + "\n" + "\n".join(lineas)
    _log(salida.replace("\n", " | "))
    print(salida)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
