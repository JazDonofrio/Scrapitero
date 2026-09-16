"""Cuadras oficiales de georef: el rango de alturas de CADA CUADRA, con su geometría.

Por qué existe. `DireccionAR` interpolaba la altura de una parcela contra el rango de la
**calle entera** (`/georef/api/calles`, un solo registro por calle). Una calle de Merlo tiene
**10 cuadras de mediana**, así que ese rango estirado sobre el eje da un número cualquiera: de
las 76.629 estimaciones de Merlo, sólo **4.018 superaban el piso de confianza de 0,4** y el
resto salía al CSV como `S/N`. El dataset de cuadras reemplaza ese único rango por el de cada
cuadra: 17.070 cuadras en Merlo, **10.986 con rango en los dos lados** y 2.000 en uno solo.

⚠ **La geometría existe SÓLO en el bulk.** La API `/v2.0/cuadras` devuelve los rangos pero no
la polilínea: su parámetro `campos` sólo acepta `id`, `calle.*` y `altura.*`, y `formato` no
incluye geojson. Sin geometría no se puede saber qué cuadra le toca a una parcela, así que hay
que bajar el archivo. Y **no se puede bajar sólo un partido**: el bulk acepta Range pero no
está ordenado por departamento (sondeos: 50014 → 10098 → 02056 → 66028 → …), así que la única
forma es una pasada completa filtrando. Por eso se cachea por partido y se baja una sola vez.

Formato de cada línea (NDJSON, medido sobre las 17.070 de Merlo):

    {"id": "0653901000065152352142",
     "calle": {"id": "065390100006515", "nombre": "SANTIAGO DEL ESTERO", ...,
               "departamento": {"id": "06539", "nombre": "Merlo"}, ...},
     "altura": {"inicio": {"derecha": 1301, "izquierda": 1302},
                "fin":    {"derecha": 1399, "izquierda": 1400}},
     "geometria": {"type": "MultiLineString", "coordinates": [[[lon, lat], [lon, lat]]]}}

Tres cosas que cuestan un bug si se ignoran:

* **El faltante de altura se codifica `0`, nunca `null`** (medido: 0 valores None, 20.482 ceros).
  `if altura:` está bien; `if altura is not None:` mete ceros como si fueran alturas.
* **El `MultiLineString` trae siempre UNA sola parte** y el 95,3% son 2 puntos.
* **El sentido de la polilínea no está declarado.** Coincide con el de la altura creciente en
  ~98% de los casos, pero hay 222 cuadras de Merlo al revés, así que cada cuadra se orienta por
  sus propias alturas en vez de asumir el orden de los puntos.

Licencia CC-BY 4.0 (dataset «Servicio de normalización de direcciones y unidades
territoriales», datos.gob.ar).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Optional

import httpx
from loguru import logger

from scrapitero.agents.direccion_norm import normalizar_calle

_URL = "https://infra.datos.gob.ar/georef/cuadras.ndjson"
_GEOREF = "https://apis.datos.gob.ar/georef/api"
# Mismo patrón que `RECEITA_DUMP_DIR`/`CADASTUR_DUMP_DIR`: el dump vive en disco y se reusa.
_DUMP_DIR = Path(os.environ.get("GEOREF_DUMP_DIR", "/opt/scrapitero/georef_dump"))
# El bulk son 1,12 GB sin comprimir: la descarga va en streaming y se filtra al vuelo, así
# nunca se guarda entero (Merlo filtrado son 9 MB).
_TIMEOUT = 900.0


def departamento_id(nombre: str, provincia: str = "buenos aires") -> Optional[str]:
    """Código INDEC del partido (Merlo → '06539'), que es como se filtra el bulk.

    Se resuelve contra la API y no con una tabla escrita a mano: los partidos que toca una
    zona salen de un reverse-geocoding, así que son nombres y pueden venir con acentos o
    variantes ('Morón', 'General San Martín')."""
    try:
        r = httpx.get(f"{_GEOREF}/departamentos", timeout=60,
                      params={"provincia": provincia, "nombre": nombre, "max": 1}).json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"georef_cuadras: no se pudo resolver el partido {nombre!r}: {exc}")
        return None
    deps = r.get("departamentos") or []
    return deps[0]["id"] if deps else None


def _archivo(dep_id: str) -> Path:
    return _DUMP_DIR / f"cuadras_{dep_id}.ndjson"


def asegurar(dep_ids: Iterable[str], forzar: bool = False) -> dict[str, Path]:
    """Los NDJSON de esos partidos en disco; baja del bulk los que falten, en UNA pasada.

    Los partidos que faltan se filtran **todos juntos**: el bulk son 1,12 GB y una zona toca
    varios (Merlo toca Moreno, Morón y La Matanza), así que bajarlo una vez por partido sería
    multiplicar la descarga por cuatro para leer el mismo archivo.

    El filtro es por texto y no por JSON parseado: son 2,08 M de líneas y parsearlas todas
    para quedarse con el 0,8% cuesta minutos de CPU al pedo. El literal es exacto porque el
    archivo viene compacto, sin espacios: `"departamento":{"id":"06539"`.
    """
    dep_ids = [d for d in dict.fromkeys(dep_ids) if d]
    listos = {d: _archivo(d) for d in dep_ids
              if not forzar and _archivo(d).is_file() and _archivo(d).stat().st_size > 0}
    faltan = [d for d in dep_ids if d not in listos]
    if not faltan:
        return listos

    _DUMP_DIR.mkdir(parents=True, exist_ok=True)
    marcas = {d: f'"departamento":{{"id":"{d}"' for d in faltan}
    tmps = {d: _archivo(d).with_suffix(".parcial") for d in faltan}
    n = {d: 0 for d in faltan}
    logger.info(f"georef_cuadras: bajando el bulk ({_URL}) para filtrar {faltan} — "
                "1,12 GB, una sola pasada")
    try:
        fhs = {d: open(t, "w", encoding="utf-8") for d, t in tmps.items()}
        try:
            with httpx.stream("GET", _URL, timeout=_TIMEOUT, follow_redirects=True) as r:
                r.raise_for_status()
                for linea in r.iter_lines():
                    for d, marca in marcas.items():
                        if marca in linea:
                            fhs[d].write(linea + "\n")
                            n[d] += 1
                            break
        finally:
            for fh in fhs.values():
                fh.close()
    except Exception as exc:  # noqa: BLE001 — best-effort: sin cuadras se sigue por calle
        logger.warning(f"georef_cuadras: falló la descarga del bulk: {exc}")
        for t in tmps.values():
            t.unlink(missing_ok=True)
        return listos
    for d, t in tmps.items():
        if n[d]:
            t.replace(_archivo(d))
            listos[d] = _archivo(d)
            logger.info(f"georef_cuadras: {n[d]} cuadras del partido {d} → {_archivo(d)}")
        else:
            t.unlink(missing_ok=True)
            logger.warning(f"georef_cuadras: el bulk no trajo cuadras del partido {d}")
    return listos


def cargar(dep_ids: Iterable[str], descargar: bool = True) -> dict[str, list[dict]]:
    """{calle_norm: [cuadra, ...]}, cada cuadra con sus rangos por lado y su polilínea.

    Cada cuadra sale como `{"nombre", "coords", "ini_der", "fin_der", "ini_izq", "fin_izq"}`,
    ya **orientada** para que la altura crezca en el sentido de los puntos. Las cuadras sin
    ningún rango (4.084 en Merlo) se descartan: su geometría no sirve para numerar.
    """
    out: dict[str, list[dict]] = {}
    dep_ids = [d for d in dict.fromkeys(dep_ids) if d]
    rutas = (asegurar(dep_ids) if descargar
             else {d: _archivo(d) for d in dep_ids if _archivo(d).is_file()})
    for dep_id in dep_ids:
        ruta = rutas.get(dep_id)
        if not ruta:
            continue
        with open(ruta, encoding="utf-8") as fh:
            for linea in fh:
                try:
                    c = json.loads(linea)
                except Exception:  # noqa: BLE001
                    continue
                alt = c.get("altura") or {}
                ini, fin = alt.get("inicio") or {}, alt.get("fin") or {}
                # `0` es el sentinela de "no publicado", no una altura.
                ini_der, fin_der = int(ini.get("derecha") or 0), int(fin.get("derecha") or 0)
                ini_izq, fin_izq = int(ini.get("izquierda") or 0), int(fin.get("izquierda") or 0)
                if not (ini_der or fin_der or ini_izq or fin_izq):
                    continue
                partes = ((c.get("geometria") or {}).get("coordinates") or [])
                coords = partes[0] if partes else []
                if len(coords) < 2:
                    continue
                # Orientar por las alturas propias: el orden de los puntos coincide con el
                # sentido creciente en ~98% de los casos, pero no siempre (222 en Merlo).
                lado = (ini_der, fin_der) if (ini_der and fin_der) else (ini_izq, fin_izq)
                if lado[0] and lado[1] and lado[1] < lado[0]:
                    coords = list(reversed(coords))
                    ini_der, fin_der = fin_der, ini_der
                    ini_izq, fin_izq = fin_izq, ini_izq
                nombre = ((c.get("calle") or {}).get("nombre") or "").strip()
                k = normalizar_calle(nombre)
                if not k:
                    continue
                out.setdefault(k, []).append({
                    "nombre": nombre, "coords": coords,
                    "ini_der": ini_der, "fin_der": fin_der,
                    "ini_izq": ini_izq, "fin_izq": fin_izq,
                })
    return out
