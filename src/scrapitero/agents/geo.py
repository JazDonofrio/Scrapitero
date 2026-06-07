"""Utilidades geográficas compartidas — genéricas para cualquier parte del mundo.

Centraliza lo que antes estaba hardcodeado por zona en varios agentes:
  - `utm_epsg` / `area_m2`: área proyectada al huso UTM correcto según la posición
    (no a un huso fijo como 21S/20S), válido en todo el planeta.
  - `detect_country`: país (ISO-3) de un punto, por reverse-geocoding (Nominatim gratis,
    con fallback a Google). Sirve para autodetectar el país al crear una región desde un
    GeoJSON o una coordenada, sin pedirlo a mano.
"""

from __future__ import annotations

import os
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
_HEADERS = {"User-Agent": "ScrapiteroResearch/1.0 (+https://github.com/Meter0r0/Scrapitero)"}

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
