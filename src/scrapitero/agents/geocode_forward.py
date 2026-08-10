"""Geocoding forward (dirección → coordenada) — capa única y compartida.

Hay un geocoder oficial y gratuito por país, y va SIEMPRE primero:
  - **Brasil → geocodebr** (CNEFE/IBGE, offline, batch por `Rscript`).
  - **Argentina → georef-ar** (`apis.datos.gob.ar/georef`, oficial, sin token, batch por
    HTTP: 1.000 direcciones en ~3 s).
Después, para lo que quede sin resolver:
  2. Nominatim (OSM, gratis, ~1 req/s) — por dirección (lo aporta `baseline_geocoder`).
  3. Mapbox (pago barato) — **sólo Brasil**: en Argentina midió peor que Nominatim
     (mediana 810 m y un caso a 398 km) y sus POIs tienen cobertura cero.
  4. Google Geocoding (pago) — fallback final por dirección.

Ambos son **batch por diseño**: geocodebr porque el worker R carga el CNEFE (~4 s) y
georef-ar porque resuelve mil direcciones en un request. NO se llaman por-dirección dentro
de un loop — se geocodifica la lista entera y lo que no ubique cae a las fuentes de abajo.

Lo usan **BaselineGeocoder** (relevamiento anterior) y **HotelFetcher** (hoteles
Cadastur/Receita sin coordenadas). Ver memorias [[project_geocodebr]] y
[[project_mapbox_benchmark]].
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

# Nombre completo del estado → sigla (normalizado: sin acentos, upper, espacios colapsados).
# Muchos CSV traen "MATO GROSSO" / "São Paulo" en vez de la sigla; sin esto quedaba NULL.
_NOMBRE_UF = {
    "RONDONIA": "RO", "ACRE": "AC", "AMAZONAS": "AM", "RORAIMA": "RR", "PARA": "PA",
    "AMAPA": "AP", "TOCANTINS": "TO", "MARANHAO": "MA", "PIAUI": "PI", "CEARA": "CE",
    "RIO GRANDE DO NORTE": "RN", "PARAIBA": "PB", "PERNAMBUCO": "PE", "ALAGOAS": "AL",
    "SERGIPE": "SE", "BAHIA": "BA", "MINAS GERAIS": "MG", "ESPIRITO SANTO": "ES",
    "RIO DE JANEIRO": "RJ", "SAO PAULO": "SP", "PARANA": "PR", "SANTA CATARINA": "SC",
    "RIO GRANDE DO SUL": "RS", "MATO GROSSO DO SUL": "MS", "MATO GROSSO": "MT",
    "GOIAS": "GO", "DISTRITO FEDERAL": "DF",
}


def uf_de_municipio_codigo(cod: Optional[str]) -> str:
    """Sigla de UF a partir del código IBGE de município (sus 2 primeros dígitos)."""
    return _UF_POR_CODIGO.get((cod or "")[:2], "")


def _sin_acentos(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def sigla_uf(valor: Optional[str]) -> str:
    """Normaliza el estado de un CSV a la sigla del estado BR. Acepta el **código IBGE
    numérico** (`51`→`MT`), la **sigla** directa (`MT`) o el **nombre completo**
    (`Mato Grosso`/`MATO GROSSO`). '' si no la reconoce."""
    s = (valor or "").strip().upper()
    if not s:
        return ""
    if s.isdigit():
        return _UF_POR_CODIGO.get(s.zfill(2), "")
    if s in _SIGLAS_UF:
        return s
    return _NOMBRE_UF.get(" ".join(_sin_acentos(s).split()), "")


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

    # geocodebr 0.6.x a veces CRASHEA el batch entero por UNA fila problemática (Binder Error
    # `empate`), incluso con estado válido. Para no perder el geocoding gratis de todas las
    # demás, si el lote falla se parte en dos y se reintenta cada mitad; al llegar a 1 fila,
    # si esa fila sigue crasheando se descarta (cae a Nominatim/Google) sin tumbar al resto.
    def _batch_resiliente(rws: list) -> dict:
        if not rws:
            return {}
        try:
            return geocode_batch(rws)
        except Exception as e:  # noqa: BLE001
            if len(rws) == 1:
                logger.warning(f"geocode_forward: geocodebr descartó 1 dirección que lo "
                               f"crashea (id={rws[0].get('id')}): {str(e)[:120]}")
                return {}
            mid = len(rws) // 2
            logger.info(f"geocode_forward: geocodebr crasheó en lote de {len(rws)} — "
                        "reintento en 2 mitades")
            return {**_batch_resiliente(rws[:mid]), **_batch_resiliente(rws[mid:])}

    res = _batch_resiliente(rows)
    if not res:
        return {}

    out: dict[str, tuple] = {}
    for rid, g in res.items():
        prec, desv = g.get("precisao"), g.get("desvio_metros")
        if (g.get("lat") is not None and prec not in _PRECISION_DESCARTE
                and (desv is None or desv <= max_desvio_m)):
            out[rid] = (g["lat"], g["lng"], f"g:{prec}"[:20])  # geocode_source VARCHAR(20)
    return out


# ── Argentina: georef-ar (Datos Argentina, oficial y gratis) ──────────────────

_GEOREF_URL = "https://apis.datos.gob.ar/georef/api/direcciones"
_GEOREF_LOTE = 500          # medido: 1.000 direcciones en ~3 s; 500 deja margen de timeout


def georef_ar_lote(items: list[dict], *, timeout: float = 90.0) -> dict[str, tuple]:
    """Geocodifica en lote direcciones ARGENTINAS con georef-ar (gratis, sin token).

    `items`: dicts con `id` + `calle`, `numero` y el ámbito administrativo para acotar:
    `provincia` y `departamento` (el partido) y/o `localidad`. El `id` es la clave de
    retorno. Devuelve `{id: (lat, lng, source)}` sólo para las resueltas.

    ⚠ **Sin ámbito NO se consulta.** Verificado: "Arturo Jauretche 1401" filtrando sólo por
    provincia Buenos Aires resuelve en **Olavarría, a 350 km** de la Hurlingham buscada. Una
    fila sin departamento ni localidad se saltea (cae a Nominatim/Google) en vez de arriesgar
    un punto en otro partido — el mismo tipo de error que descartó a Mapbox en Argentina.

    Precisión medida contra el catastro (30 direcciones de Hurlingham): mediana 60 m,
    p90 130 m, peor caso 320 m, 27/30 resueltas.
    """
    if not items:
        return {}
    try:
        import httpx
    except Exception as e:  # noqa: BLE001
        logger.warning(f"geocode_forward: httpx no disponible ({e}) — georef-ar omitido")
        return {}

    # Sólo las filas que traen calle + ámbito: el resto iría a parar a otro partido.
    base: list[tuple[str, str, str, str]] = []   # (id, direccion, provincia, ambito)
    sin_ambito = 0
    for it in items:
        calle = (it.get("calle") or "").strip()
        if not calle:
            continue
        ambito = (it.get("departamento") or it.get("localidad") or "").strip()
        provincia = (it.get("provincia") or "").strip()
        if not ambito or not provincia:
            sin_ambito += 1
            continue
        numero = str(it.get("numero") or "").strip()
        base.append((str(it["id"]), f"{calle} {numero}".strip(), provincia, ambito))
    if sin_ambito:
        logger.warning(
            f"geocode_forward: {sin_ambito}/{len(items)} filas sin provincia+partido/localidad "
            "— se omiten de georef-ar (van a Nominatim/Google). Sin ámbito el geocoder puede "
            "devolver la misma calle en otro partido, a cientos de km.")
    if not base:
        return {}

    def _consultar(client, filas: list, campo: str) -> dict[str, tuple]:
        """Una pasada por lotes, filtrando el ámbito por `campo` (departamento|localidad)."""
        res_out: dict[str, tuple] = {}
        for i in range(0, len(filas), _GEOREF_LOTE):
            trozo = filas[i:i + _GEOREF_LOTE]
            cuerpo = [{"direccion": d, "provincia": p, campo: a, "max": 1}
                      for _, d, p, a in trozo]
            try:
                r = client.post(_GEOREF_URL, json={"direcciones": cuerpo})
                r.raise_for_status()
                resultados = r.json().get("resultados", [])
            except Exception as e:  # noqa: BLE001
                logger.warning(f"geocode_forward: georef-ar falló en un lote de "
                               f"{len(trozo)} ({str(e)[:120]}) — esas direcciones "
                               "siguen con Nominatim/Google")
                continue
            for (rid, *_), res in zip(trozo, resultados):
                dirs = (res or {}).get("direcciones") or []
                if not dirs:
                    continue
                ub = (dirs[0].get("ubicacion") or {})
                lat, lon = ub.get("lat"), ub.get("lon")
                if lat is None or lon is None:
                    continue
                # con altura = interpolada sobre la cuadra; sin altura = eje de la calle
                tiene_altura = (dirs[0].get("altura") or {}).get("valor") is not None
                res_out[rid] = (float(lat), float(lon),
                                "ar:numero" if tiene_altura else "ar:calle")
        return res_out

    out: dict[str, tuple] = {}
    with httpx.Client(timeout=timeout) as client:
        out.update(_consultar(client, base, "departamento"))
        # Segundo intento por LOCALIDAD para lo que no matcheó como partido: en el conurbano
        # el CSV suele traer la localidad ("Villa Tesei"), que no es el nombre del partido
        # ("Hurlingham") y como `departamento` no devuelve nada.
        faltan = [f for f in base if f[0] not in out]
        if faltan:
            recuperadas = _consultar(client, faltan, "localidad")
            if recuperadas:
                logger.info(f"geocode_forward: georef-ar recuperó {len(recuperadas)} "
                            "direcciones filtrando por localidad en vez de partido")
            out.update(recuperadas)
    logger.info(f"geocode_forward: georef-ar resolvió {len(out)}/{len(base)} direcciones "
                "(gratis)")
    return out
