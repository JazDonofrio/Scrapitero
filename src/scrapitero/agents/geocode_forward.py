"""Geocoding forward (dirección → coordenada) — capa única y compartida.

Orden de preferencia, **geocodebr PRIMERO para Brasil**:
  1. **geocodebr** (CNEFE/IBGE, gratis, offline) — **batch** (un `Rscript` carga el CNEFE
     una sola vez y resuelve toda la lista).
  2. Nominatim (OSM, gratis, ~1 req/s) — por dirección (lo aporta `baseline_geocoder`).
  3. Google Geocoding (pago) — fallback por dirección.

geocodebr es **batch por diseño**: el arranque del worker R carga el CNEFE (~4 s), así que
NO se llama por-dirección dentro de un loop — se geocodifica una lista de una sola vez y lo
que no ubique con desvío aceptable cae a Nominatim/Google.

Lo usan **BaselineGeocoder** (relevamiento anterior) y **HotelFetcher** (hoteles
Cadastur/Receita sin coordenadas), para que geocodebr sea la primera opción en TODO camino
forward de Brasil. Ver memoria [[project_geocodebr]].
"""

from __future__ import annotations

from typing import Optional

from loguru import logger

# IBGE: los 2 primeros dígitos del código de município codifican la UF → sigla.
# geocodebr usa la sigla de estado para desambiguar municípios homónimos entre estados.
_UF_POR_CODIGO = {
    "11": "RO", "12": "AC", "13": "AM", "14": "RR", "15": "PA", "16": "AP", "17": "TO",
    "21": "MA", "22": "PI", "23": "CE", "24": "RN", "25": "PB", "26": "PE", "27": "AL",
    "28": "SE", "29": "BA", "31": "MG", "32": "ES", "33": "RJ", "35": "SP",
    "41": "PR", "42": "SC", "43": "RS", "50": "MS", "51": "MT", "52": "GO", "53": "DF",
}


_SIGLAS_UF = set(_UF_POR_CODIGO.values())


def uf_de_municipio_codigo(cod: Optional[str]) -> str:
    """Sigla de UF a partir del código IBGE de município (sus 2 primeros dígitos)."""
    return _UF_POR_CODIGO.get((cod or "")[:2], "")


def sigla_uf(valor: Optional[str]) -> str:
    """Normaliza un COD_UF de un CSV a la sigla del estado BR. Acepta el **código IBGE
    numérico** (`51`→`MT`, `35`→`SP`) o la **sigla** directa (`MT`). '' si no la reconoce."""
    s = (valor or "").strip().upper()
    if not s:
        return ""
    if s.isdigit():
        return _UF_POR_CODIGO.get(s.zfill(2), "")
    return s if s in _SIGLAS_UF else ""


def geocodebr_lote(items: list[dict], *, uf: Optional[str] = None,
                   max_desvio_m: float = 300.0) -> dict[str, tuple]:
    """Geocodifica en lote direcciones brasileñas con geocodebr (CNEFE/IBGE, gratis/offline).

    `items`: dicts con `id` + campos estructurados `logradouro`, `numero`, `bairro`,
    `municipio` (opcionales `estado`, `cep`). El `id` es la clave de retorno.

    Devuelve `{id: (lat, lng, source)}` **solo** para las direcciones ubicadas con precisión
    útil (descarta los centroides gruesos `localidade`/`municipio`) y desvío ≤ `max_desvio_m`.
    Las que no entren no aparecen en el dict → el llamador las manda a Nominatim/Google.

    Si geocodebr no está disponible (sin Rscript) o el worker falla, devuelve `{}` (no rompe:
    el flujo sigue con las fuentes de abajo).
    """
    if not items:
        return {}
    try:
        from scrapitero.agents.geocodebr_fetcher import (
            geocode_batch, _rscript_bin, _PRECISION_DESCARTE)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"geocode_forward: geocodebr no importable ({e}) — sigo con Nominatim/Google")
        return {}
    if not _rscript_bin():
        logger.info("geocode_forward: Rscript no disponible — geocodebr omitido (sigue Nominatim/Google)")
        return {}

    rows = [{"id": str(it["id"]), "logradouro": it.get("logradouro") or "",
             "numero": it.get("numero") or "", "bairro": it.get("bairro") or "",
             "municipio": it.get("municipio") or "",
             "estado": (it.get("estado") or uf or "").strip(), "cep": it.get("cep") or ""}
            for it in items]
    # geocodebr 0.6.x CRASHEA el batch entero ante un `estado` vacío (Binder Error sobre
    # la columna interna `empate`). definir_campos exige estado, así que no se puede omitir:
    # las filas sin UF se saltean (caen a Nominatim/Google) para no tumbar a las demás.
    sin_uf = sum(1 for r in rows if not r["estado"])
    if sin_uf:
        logger.warning(
            f"geocode_forward: {sin_uf}/{len(rows)} filas sin estado/UF — se omiten de "
            "geocodebr (irían a Nominatim/Google). Completá municipio_codigo de la región "
            "o el COD_UF del CSV para geocodificarlas gratis.")
        rows = [r for r in rows if r["estado"]]
    if not rows:
        return {}
    try:
        res = geocode_batch(rows)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"geocode_forward: geocodebr falló (sigo con Nominatim/Google): {e}")
        return {}

    out: dict[str, tuple] = {}
    for rid, g in res.items():
        prec, desv = g.get("precisao"), g.get("desvio_metros")
        if (g.get("lat") is not None and prec not in _PRECISION_DESCARTE
                and (desv is None or desv <= max_desvio_m)):
            out[rid] = (g["lat"], g["lng"], f"g:{prec}"[:20])  # geocode_source VARCHAR(20)
    return out
