"""Utilidades geográficas compartidas — genéricas para cualquier parte del mundo.

Centraliza lo que antes estaba hardcodeado por zona en varios agentes:
  - `utm_epsg` / `area_m2`: área proyectada al huso UTM correcto según la posición
    (no a un huso fijo como 21S/20S), válido en todo el planeta.
  - `detect_country`: país (ISO-3) de un punto, por reverse-geocoding (Nominatim gratis,
    con fallback a Google). Sirve para autodetectar el país al crear una región desde un
    GeoJSON o una coordenada, sin pedirlo a mano.
"""

from __future__ import annotations

import json
import os
import unicodedata
from typing import Optional

import httpx
import pyproj
from loguru import logger
from shapely.geometry import base as _shp_base
from shapely.ops import transform as _shp_transform


# ── Área en UTM (huso correcto por posición) ───────────────────────────────────

_utm_transformers: dict[int, "pyproj.Transformer"] = {}


def utm_epsg(lon: float, lat: float) -> int:
    """EPSG del huso UTM que contiene (lon, lat). Global: cualquier país.

    Norte → 326xx, Sur → 327xx, donde xx es el huso 1..60.
    """
    zone = int((lon + 180.0) // 6.0) + 1
    zone = min(max(zone, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


# CRS de los DXF por país, cuando el cliente fijó uno; sin entrada, huso UTM WGS 84 del
# centroide (`utm_epsg`). Argentina: «PGA98.Argentina-5», el código de AutoCAD Map/Civil 3D
# para POSGAR 98 / Argentina faja 5 = **EPSG:22175** — ojo, NO 22185, que es POSGAR 94.
# Pedido del cliente el 15-sep-2026. Faja FIJA y no por longitud, a propósito: es la base
# con la que trabajan sus planos. Merlo, Hurlingham, Malvinas y Tigre caen en la 5 igual;
# lejos del meridiano -60 (Salta, -65,4) la faja 5 deforma y ahí habría que preguntar.
_CRS_DXF_POR_PAIS = {"ARG": 22175}


def epsg_dxf(pais: Optional[str], lon: float, lat: float) -> int:
    """EPSG en que salen los DXF de un relevamiento. Única implementación: la usan los dos
    agentes de DXF, que tienen que dibujar en el mismo sistema (mismo motivo que `nombre_crs`)."""
    return _CRS_DXF_POR_PAIS.get((pais or "").upper()) or utm_epsg(lon, lat)


def nombre_crs(epsg: int) -> str:
    """Nombre corto del CRS como lo escribe un proyectista: 'UTM84-21S', no 'WGS 84 / UTM 21S'.

    Vive acá y no en un agente de export porque **los dos DXF tienen que decir lo mismo**:
    un DXF no tiene dónde declarar el sistema de coordenadas (el objeto GeoData recién
    aparece en AC1024 y el plano del cliente es R2000), así que el único lugar donde el
    dato viaja es la nota de auditoría — y si cada agente la arma por su cuenta, dos
    planos de la misma zona pueden salir declarando cosas distintas.
    """
    if 32701 <= epsg <= 32760:          # WGS 84 / UTM sur
        return f"UTM84-{epsg - 32700}S"
    if 32601 <= epsg <= 32660:          # WGS 84 / UTM norte
        return f"UTM84-{epsg - 32600}N"
    if 31977 <= epsg <= 31985:          # SIRGAS 2000 / UTM sur
        return f"SIRGAS2000-{epsg - 31960}S"
    if 22171 <= epsg <= 22177:          # POSGAR 98 / Argentina faja N (Gauss-Krüger)
        return f"PGA98.Argentina-{epsg - 22170}"   # el código de AutoCAD Map / Civil 3D
    return f"EPSG:{epsg}"


def area_m2(geom: "_shp_base.BaseGeometry") -> Optional[float]:
    """Área en m² proyectando al huso UTM correcto según el centroide de la geometría.

    Reemplaza los husos fijos (EPSG:32721/32720) que daban área errónea fuera de su
    franja. Válido en cualquier parte del mundo."""
    try:
        c = geom.centroid
        epsg = utm_epsg(c.x, c.y)
        proj = _utm_transformers.get(epsg)
        if proj is None:
            proj = pyproj.Transformer.from_crs(
                "EPSG:4326", f"EPSG:{epsg}", always_xy=True
            ).transform
            _utm_transformers[epsg] = proj
        return round(_shp_transform(proj, geom).area, 2)
    except Exception:
        return None


def area_km2(geom: "_shp_base.BaseGeometry") -> Optional[float]:
    """Área en km² (ver `area_m2`)."""
    m2 = area_m2(geom)
    return None if m2 is None else m2 / 1_000_000.0


# ── Detección de país por reverse-geocoding ────────────────────────────────────

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_HEADERS = {"User-Agent": "ScraperGIS/1.0 (+https://github.com/Meter0r0/Scrapitero)"}

# ISO 3166-1 alpha-2 → alpha-3 (el esquema usa country_code de 3 letras: BRA, ARG…).
_ISO2_TO_3 = {
    "AD": "AND", "AE": "ARE", "AF": "AFG", "AG": "ATG", "AI": "AIA", "AL": "ALB",
    "AM": "ARM", "AO": "AGO", "AQ": "ATA", "AR": "ARG", "AS": "ASM", "AT": "AUT",
    "AU": "AUS", "AW": "ABW", "AX": "ALA", "AZ": "AZE", "BA": "BIH", "BB": "BRB",
    "BD": "BGD", "BE": "BEL", "BF": "BFA", "BG": "BGR", "BH": "BHR", "BI": "BDI",
    "BJ": "BEN", "BL": "BLM", "BM": "BMU", "BN": "BRN", "BO": "BOL", "BQ": "BES",
    "BR": "BRA", "BS": "BHS", "BT": "BTN", "BV": "BVT", "BW": "BWA", "BY": "BLR",
    "BZ": "BLZ", "CA": "CAN", "CC": "CCK", "CD": "COD", "CF": "CAF", "CG": "COG",
    "CH": "CHE", "CI": "CIV", "CK": "COK", "CL": "CHL", "CM": "CMR", "CN": "CHN",
    "CO": "COL", "CR": "CRI", "CU": "CUB", "CV": "CPV", "CW": "CUW", "CX": "CXR",
    "CY": "CYP", "CZ": "CZE", "DE": "DEU", "DJ": "DJI", "DK": "DNK", "DM": "DMA",
    "DO": "DOM", "DZ": "DZA", "EC": "ECU", "EE": "EST", "EG": "EGY", "EH": "ESH",
    "ER": "ERI", "ES": "ESP", "ET": "ETH", "FI": "FIN", "FJ": "FJI", "FK": "FLK",
    "FM": "FSM", "FO": "FRO", "FR": "FRA", "GA": "GAB", "GB": "GBR", "GD": "GRD",
    "GE": "GEO", "GF": "GUF", "GG": "GGY", "GH": "GHA", "GI": "GIB", "GL": "GRL",
    "GM": "GMB", "GN": "GIN", "GP": "GLP", "GQ": "GNQ", "GR": "GRC", "GS": "SGS",
    "GT": "GTM", "GU": "GUM", "GW": "GNB", "GY": "GUY", "HK": "HKG", "HM": "HMD",
    "HN": "HND", "HR": "HRV", "HT": "HTI", "HU": "HUN", "ID": "IDN", "IE": "IRL",
    "IL": "ISR", "IM": "IMN", "IN": "IND", "IO": "IOT", "IQ": "IRQ", "IR": "IRN",
    "IS": "ISL", "IT": "ITA", "JE": "JEY", "JM": "JAM", "JO": "JOR", "JP": "JPN",
    "KE": "KEN", "KG": "KGZ", "KH": "KHM", "KI": "KIR", "KM": "COM", "KN": "KNA",
    "KP": "PRK", "KR": "KOR", "KW": "KWT", "KY": "CYM", "KZ": "KAZ", "LA": "LAO",
    "LB": "LBN", "LC": "LCA", "LI": "LIE", "LK": "LKA", "LR": "LBR", "LS": "LSO",
    "LT": "LTU", "LU": "LUX", "LV": "LVA", "LY": "LBY", "MA": "MAR", "MC": "MCO",
    "MD": "MDA", "ME": "MNE", "MF": "MAF", "MG": "MDG", "MH": "MHL", "MK": "MKD",
    "ML": "MLI", "MM": "MMR", "MN": "MNG", "MO": "MAC", "MP": "MNP", "MQ": "MTQ",
    "MR": "MRT", "MS": "MSR", "MT": "MLT", "MU": "MUS", "MV": "MDV", "MW": "MWI",
    "MX": "MEX", "MY": "MYS", "MZ": "MOZ", "NA": "NAM", "NC": "NCL", "NE": "NER",
    "NF": "NFK", "NG": "NGA", "NI": "NIC", "NL": "NLD", "NO": "NOR", "NP": "NPL",
    "NR": "NRU", "NU": "NIU", "NZ": "NZL", "OM": "OMN", "PA": "PAN", "PE": "PER",
    "PF": "PYF", "PG": "PNG", "PH": "PHL", "PK": "PAK", "PL": "POL", "PM": "SPM",
    "PN": "PCN", "PR": "PRI", "PS": "PSE", "PT": "PRT", "PW": "PLW", "PY": "PRY",
    "QA": "QAT", "RE": "REU", "RO": "ROU", "RS": "SRB", "RU": "RUS", "RW": "RWA",
    "SA": "SAU", "SB": "SLB", "SC": "SYC", "SD": "SDN", "SE": "SWE", "SG": "SGP",
    "SH": "SHN", "SI": "SVN", "SJ": "SJM", "SK": "SVK", "SL": "SLE", "SM": "SMR",
    "SN": "SEN", "SO": "SOM", "SR": "SUR", "SS": "SSD", "ST": "STP", "SV": "SLV",
    "SX": "SXM", "SY": "SYR", "SZ": "SWZ", "TC": "TCA", "TD": "TCD", "TF": "ATF",
    "TG": "TGO", "TH": "THA", "TJ": "TJK", "TK": "TKL", "TL": "TLS", "TM": "TKM",
    "TN": "TUN", "TO": "TON", "TR": "TUR", "TT": "TTO", "TV": "TUV", "TW": "TWN",
    "TZ": "TZA", "UA": "UKR", "UG": "UGA", "UM": "UMI", "US": "USA", "UY": "URY",
    "UZ": "UZB", "VA": "VAT", "VC": "VCT", "VE": "VEN", "VG": "VGB", "VI": "VIR",
    "VN": "VNM", "VU": "VUT", "WF": "WLF", "WS": "WSM", "YE": "YEM", "YT": "MYT",
    "ZA": "ZAF", "ZM": "ZMB", "ZW": "ZWE",
}


_ISO3_TO_2 = {v: k for k, v in _ISO2_TO_3.items()}


def country_iso2(iso3: Optional[str]) -> Optional[str]:
    """ISO-2 (minúscula) a partir del ISO-3. Útil para el sufijo de region_id (`-br`/`-ar`)."""
    if not iso3:
        return None
    code = _ISO3_TO_2.get(iso3.upper())
    return code.lower() if code else None


def _nominatim_country(lat: float, lng: float) -> Optional[str]:
    """ISO-2 del país vía Nominatim (gratis). None si falla."""
    try:
        with httpx.Client(timeout=15, headers=_HEADERS, follow_redirects=True) as c:
            r = c.get(_NOMINATIM_URL, params={
                "format": "jsonv2", "lat": lat, "lon": lng, "zoom": 3,
            })
        if r.status_code == 200:
            cc = (r.json().get("address") or {}).get("country_code")
            return cc.upper() if cc else None
    except httpx.HTTPError:
        pass
    return None


def _google_country(lat: float, lng: float) -> Optional[str]:
    """ISO-2 del país vía Google Geocoding (fallback, requiere API key). None si falla."""
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        return None
    try:
        with httpx.Client(timeout=15, headers=_HEADERS) as c:
            r = c.get(_GOOGLE_GEOCODE_URL, params={
                "latlng": f"{lat},{lng}", "key": key, "result_type": "country",
            })
        if r.status_code == 200:
            for res in r.json().get("results", []):
                for comp in res.get("address_components", []):
                    if "country" in comp.get("types", []):
                        return comp.get("short_name", "").upper() or None
    except httpx.HTTPError:
        pass
    return None


def detect_country(lat: float, lng: float) -> Optional[str]:
    """País (ISO-3, p.ej. 'BRA'/'ARG') de un punto. None si no se pudo determinar.

    Reverse-geocoding: Nominatim (gratis) primero, Google como fallback. Pensado para
    autodetectar el país al crear una región desde un GeoJSON/coordenada, sin pedirlo
    a mano — así el sistema soporta cualquier país sin hardcodear."""
    iso2 = _nominatim_country(lat, lng) or _google_country(lat, lng)
    if not iso2:
        logger.warning(f"detect_country: no se pudo determinar el país de ({lat:.4f},{lng:.4f})")
        return None
    iso3 = _ISO2_TO_3.get(iso2)
    if not iso3:
        logger.warning(f"detect_country: sin mapeo ISO-3 para '{iso2}' ({lat:.4f},{lng:.4f})")
    return iso3


# ── Detección de municipio IBGE (Brasil) ───────────────────────────────────────

_IBGE_MUNICIPIOS_URL = "https://servicodados.ibge.gov.br/api/v1/localidades/estados/{uf}/municipios"


def _norm(s: Optional[str]) -> str:
    """Normaliza para comparar nombres: sin acentos, minúsculas, sin espacios extra."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(s.split()).lower()


def _nominatim_municipio_uf(lat: float, lng: float) -> tuple[Optional[str], Optional[str]]:
    """(nombre_municipio, sigla_UF) vía Nominatim. (None, None) si falla."""
    try:
        with httpx.Client(timeout=15, headers=_HEADERS, follow_redirects=True) as c:
            r = c.get(_NOMINATIM_URL, params={"format": "jsonv2", "lat": lat, "lon": lng, "zoom": 10})
        if r.status_code == 200:
            addr = r.json().get("address") or {}
            uf = None
            iso = addr.get("ISO3166-2-lvl4")          # p.ej. "BR-MT"
            if iso and "-" in iso:
                uf = iso.split("-")[-1].upper()
            nombre = (addr.get("municipality") or addr.get("city") or addr.get("town")
                      or addr.get("village") or addr.get("county"))
            return nombre, uf
    except httpx.HTTPError:
        pass
    return None, None


_ESRI_IMAGERY_URL = ("https://services.arcgisonline.com/arcgis/rest/services/"
                     "World_Imagery/MapServer/identify")
_imagery_cache: dict[tuple, Optional[dict]] = {}


def imagery_fecha(lat: float, lng: float) -> Optional[dict]:
    """Fecha de captura de la imagen satelital que se ve en el basemap de Esri en ese punto.

    Devuelve `{'fecha','resolucion_m','proveedor','sensor'}` o None si no se pudo.

    **Por qué hace falta.** El operador resuelve las incidencias de construcción MIRANDO la
    foto, y hasta ahora no había forma de saber de cuándo era la que tenía en pantalla. La
    contradicción medida el 6-sep-2026 es grande: `AlturaFetcher` mide sobre la imagen de
    Google Solar, que en Várzea Grande es de **2014** en el 87% de los puntos y en Hurlingham
    de **2013**, mientras el basemap de Esri que el panel muestra por default trae
    **10-sep-2025** sobre VG (0,46 m, GeoEye-1) y **13-ene-2026** sobre el conurbano (0,50 m,
    WorldView-2). O sea que la pantalla ya mostraba obra que la medición no podía ver, y
    nadie lo sabía.

    La cobertura de Esri viene en **bloques** (`DYNAMIC_ESRI_Cuiaba_25Q4`,
    `DYNAMIC_ESRI_Buenos_Aires_26Q1`), no por parcela, así que la coordenada se **redondea a
    ~0,05°** (≈5 km) para cachear: una consulta por zona alcanza y el panel no le pega al
    servicio una vez por tarjeta.

    Best-effort y sin credenciales: el endpoint es público. Ante cualquier fallo devuelve
    None y el panel simplemente no muestra la línea — nunca rompe la página.
    """
    clave = (round(lat / 0.05), round(lng / 0.05))
    if clave in _imagery_cache:
        return _imagery_cache[clave]
    dato = None
    try:
        params = {
            "geometry": json.dumps({"x": lng, "y": lat}), "geometryType": "esriGeometryPoint",
            "sr": 4326, "layers": "all", "tolerance": 2, "returnGeometry": "false",
            "mapExtent": f"{lng-0.01},{lat-0.01},{lng+0.01},{lat+0.01}",
            "imageDisplay": "600,600,96", "f": "json",
        }
        with httpx.Client(timeout=12, headers=_HEADERS, follow_redirects=True) as c:
            r = c.get(_ESRI_IMAGERY_URL, params=params)
        if r.status_code == 200:
            mejor = None
            for f in (r.json().get("results") or []):
                a = f.get("attributes") or {}
                res = a.get("SRC_RES")
                try:
                    res = float(res)
                except (TypeError, ValueError):
                    continue
                # Nos interesa la mejor resolución disponible: es la capa que el operador
                # ve al acercarse, no el relleno global de 15 m.
                if res > 0 and (mejor is None or res < mejor[0]):
                    mejor = (res, a)
            if mejor:
                a = mejor[1]
                # `SRC_DATE` es AAAAMMDD numérico y NO tiene ambigüedad; `SRC_DATE2` viene
                # como "9/10/2025", que en formato US es 10-sep pero se lee 9-oct en
                # castellano. Se usa el numérico y se arma la fecha ISO a mano.
                iso = None
                cru = str(a.get("SRC_DATE") or "")
                if len(cru) == 8 and cru.isdigit():
                    iso = f"{cru[:4]}-{cru[4:6]}-{cru[6:]}"
                dato = {"fecha": iso,
                        "resolucion_m": mejor[0],
                        "proveedor": a.get("NICE_DESC"),      # p.ej. "Vantor"
                        "sensor": a.get("SRC_DESC"),          # p.ej. "GE01" / "WV02"
                        "producto": a.get("NICE_NAME")}       # p.ej. "Vivid Advanced"
    except (httpx.HTTPError, ValueError) as e:
        logger.warning(f"imagery_fecha: no se pudo consultar Esri en ({lat:.4f},{lng:.4f}): {e}")
    _imagery_cache[clave] = dato
    return dato


def detect_localidad_provincia(lat: float, lng: float) -> tuple[Optional[str], Optional[str]]:
    """(localidad, provincia/estado) de un punto, con los NOMBRES completos.

    Distinto de `_nominatim_municipio_uf`, que devuelve la **sigla** del ISO-3166-2 (útil
    en Brasil, donde la UF es la sigla, e inservible en Argentina: "AR-B" no es
    "Buenos Aires"). Acá se devuelve `address.state` tal cual, para rellenar las columnas
    de localidad/provincia del entregable cuando el catastro no las trae (ARBA/IDERA no
    publican ni localidad ni provincia por parcela).

    Genérico para cualquier país. (None, None) si el reverse falla.
    """
    try:
        with httpx.Client(timeout=15, headers=_HEADERS, follow_redirects=True) as c:
            r = c.get(_NOMINATIM_URL,
                      params={"format": "jsonv2", "lat": lat, "lon": lng, "zoom": 10})
        if r.status_code == 200:
            addr = r.json().get("address") or {}
            localidad = (addr.get("city") or addr.get("town") or addr.get("village")
                         or addr.get("municipality") or addr.get("suburb")
                         or addr.get("county"))
            provincia = addr.get("state") or addr.get("region")
            return localidad, provincia
    except httpx.HTTPError as e:
        logger.warning(f"detect_localidad_provincia: reverse falló en ({lat:.4f},{lng:.4f}): {e}")
    return None, None


def detect_municipio_br(lat: float, lng: float) -> Optional[str]:
    """Código IBGE de 7 dígitos del municipio (Brasil) que contiene el punto. None si no se pudo.

    Reverse-geocoding (Nominatim) para municipio + UF, y luego match contra la lista
    OFICIAL de municipios del IBGE de esa UF (servicodados.ibge.gov.br). Pensado para
    guardar `municipio_codigo` al crear una zona de Brasil, evitando que el orquestador lo
    adivine (un código inválido rompe IBGECensus/Logradouros). Solo aplica a Brasil."""
    nombre, uf = _nominatim_municipio_uf(lat, lng)
    if not nombre or not uf:
        logger.warning(f"detect_municipio_br: sin municipio/UF para ({lat:.4f},{lng:.4f})")
        return None
    try:
        with httpx.Client(timeout=20, headers=_HEADERS, follow_redirects=True) as c:
            r = c.get(_IBGE_MUNICIPIOS_URL.format(uf=uf))
        if r.status_code == 200:
            target = _norm(nombre)
            for m in r.json():
                if _norm(m.get("nome")) == target:
                    return str(m.get("id"))
    except httpx.HTTPError:
        pass
    logger.warning(f"detect_municipio_br: no se encontró código IBGE para {nombre!r} ({uf})")
    return None
