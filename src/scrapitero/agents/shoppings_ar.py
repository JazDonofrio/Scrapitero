"""shoppings.com.ar — directorio curado de shoppings argentinos (fuente de `ShoppingFetcher`).

Por qué suma: OSM y Google identifican shoppings por tag/categoría, con falsos positivos y
faltantes; esto es un **directorio editado a mano**, con el nombre comercial real, la
dirección y el partido. Es la fuente de mayor precisión de nombre que tenemos en Argentina.

Lo que NO trae (verificado): **cantidad de locales** —el dato que sigue sin resolverse en
ningún país— ni coordenadas. La ubicación se obtiene geocodificando la dirección con
**georef-ar** (oficial y gratis), así que esta fuente no cuesta nada.

`robots.txt` del sitio: `User-agent: * / Disallow:` (vacío) ⇒ permite el crawleo. Aun así se
baja **una sola página por corrida** y se cachea en memoria del proceso.

Estructura del HTML (Weebly): cada shopping es un `<h2 class="wsite-content-title">` con el
nombre, seguido de un bloque cuyas líneas —separadas por `<br>`— son
`nombre largo / dirección / localidad-partido / provincia / Tel: …`. El parseo preserva los
`<br>` como saltos: sin eso todo queda en un renglón y no se puede separar dirección de
localidad.
"""

from __future__ import annotations

import html as _html
import re
import unicodedata
from typing import Optional

from loguru import logger

_URL_PBA = "https://shoppings.com.ar/provincia-de-buenos-aires.html"
# Páginas de listado del sitio (sitemap). La de provincia es la que pidió el usuario; las
# de zona son subconjuntos del GBA y se pueden sumar por parámetro.
URLS_LISTADO_DEFAULT = [_URL_PBA]
URLS_LISTADO_GBA = [
    "https://shoppings.com.ar/gran-buenos-aires-zona-norte.html",
    "https://shoppings.com.ar/gran-buenos-aires-zona-oeste.html",
    "https://shoppings.com.ar/gran-buenos-aires-zona-sur.html",
]

_UA = {"User-Agent": "ScraperGIS/1.0 (+https://github.com/Meter0r0/Scrapitero)"}
_GEOREF = "https://apis.datos.gob.ar/georef/api"

# Líneas de adorno del bloque que nunca son dirección ni localidad.
_RUIDO = re.compile(
    r"^(como llegar|tel[:.]|fax|provincia de|prov\b|argentina$|ver mapa|web|http)", re.I)
# Abreviaturas del directorio que no matchean el nomenclador oficial de georef.
_ABREV = {
    "ing.": "ingeniero", "gral.": "general", "gob.": "gobernador", "pdte.": "presidente",
    "cnel.": "coronel", "alte.": "almirante", "dr.": "doctor", "sta.": "santa",
    "sto.": "santo", "vte.": "vicente", "bme.": "bartolome", "cap.": "capitan",
}


def _norm(s: Optional[str]) -> str:
    """Minúsculas, sin acentos, abreviaturas expandidas — para comparar contra georef."""
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    for ab, full in _ABREV.items():
        s = s.replace(ab, full)
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def _lineas(fragmento: str) -> list[str]:
    """Texto visible del fragmento, una entrada por línea (los `<br>` marcan el corte)."""
    s = re.sub(r"<script.*?</script>|<style.*?</style>", " ", fragmento, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</h[1-6]>|</td>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return [re.sub(r"\s+", " ", _html.unescape(l)).strip()
            for l in s.split("\n") if l.strip()]


def _ambitos(client, provincia: str) -> dict[str, tuple[str, str, str]]:
    """Diccionario {nombre_norm: (tipo, nombre, partido)} de partidos y localidades.

    Se usa el propio nomenclador oficial de georef como vocabulario, en vez de una lista
    hardcodeada: el directorio escribe "Villa Ballester, Partido de San Martín" o
    "1670 Tigre" y hay que descubrir cuál de esas palabras es el ámbito administrativo real.

    De cada localidad se guarda **su partido**, porque la validación por reverse
    (`/ubicacion`) devuelve departamento/municipio, no localidad: sin esto, «Martínez»
    (Unicenter) no matcheaba contra «San Isidro» y el shopping se descartaba.
    """
    out: dict[str, tuple[str, str, str]] = {}
    try:
        r = client.get(f"{_GEOREF}/departamentos",
                       params={"provincia": provincia, "max": 5000, "campos": "nombre"})
        r.raise_for_status()
        for x in r.json().get("departamentos", []):
            n = _norm(x.get("nombre"))
            if n:
                out[n] = ("departamento", x["nombre"], x["nombre"])
    except Exception as e:  # noqa: BLE001
        logger.warning(f"shoppings_ar: no pude traer departamentos de georef ({e})")
    try:
        r = client.get(f"{_GEOREF}/localidades",
                       params={"provincia": provincia, "max": 5000,
                               "campos": "nombre,departamento.nombre"})
        r.raise_for_status()
        for x in r.json().get("localidades", []):
            n = _norm(x.get("nombre"))
            partido = ((x.get("departamento") or {}).get("nombre")) or x.get("nombre") or ""
            # la localidad es más específica: si el nombre existe en ambos, gana ella
            if n:
                out[n] = ("localidad", x["nombre"], partido)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"shoppings_ar: no pude traer localidades de georef ({e})")
    return out


def _detectar_ambito(lineas: list[str], ambitos: dict) -> Optional[tuple[str, str]]:
    """Busca en el bloque el partido/localidad real. Devuelve (tipo, nombre) o None.

    ⚠ Se llama SIN la línea de dirección a propósito: muchas calles se llaman igual que un
    partido y el match tomaba el nombre de la calle como ámbito ("Norcenter, Esteban
    Echeverria 3750" en Vicente López daba el partido *Esteban Echeverría*, a 60 km).
    """
    mejor: Optional[tuple[str, str, str]] = None
    mejor_len = 0
    for l in lineas:
        ln = _norm(l)
        for clave, (tipo, nombre, partido) in ambitos.items():
            # se exige que el nombre aparezca como secuencia de palabras completa
            if len(clave) > 3 and re.search(rf"\b{re.escape(clave)}\b", ln):
                if len(clave) > mejor_len:      # el match más largo es el más específico
                    mejor, mejor_len = (tipo, nombre, partido), len(clave)
    return mejor


# Una dirección postal tiene altura; "Panamericana Km 50 Ramal Pilar" es una referencia de
# ruta y ningún geocoder la resuelve, así que no se confunde con dirección.
_RE_KM = re.compile(r"\bkm\.?\s*\d", re.I)
_RE_ALTURA = re.compile(r"\d{2,5}")
_RE_ESQUINA = re.compile(r"\s+(y|e|esq\.?|esquina)\s+", re.I)


def _elegir_direccion(cuerpo: list[str], nombre: str) -> Optional[str]:
    """La línea que realmente es la dirección postal del bloque.

    No alcanza con tomar la primera: el directorio suele repetir el nombre comercial en su
    propia línea con una variante ("Las Palmas del Pilar" → "Palmas del Pilar Shopping"),
    y esa línea no se parece lo bastante al título como para descartarla por prefijo.
    """
    import difflib
    n = _norm(nombre)
    candidatos = []
    for l in cuerpo:
        ln = _norm(l)
        if not ln:
            continue
        # descartar la repetición del nombre comercial (prefijo o parecido global)
        if ln.startswith(n[:12]) or n.startswith(ln[:12]) or \
                difflib.SequenceMatcher(None, ln, n).ratio() >= 0.6:
            continue
        if _RE_KM.search(l):            # referencia de ruta, no dirección postal
            continue
        if _RE_ALTURA.search(l) or _RE_ESQUINA.search(l):
            candidatos.append(l)
    if candidatos:
        # la primera con altura es la dirección; las de más abajo suelen ser CP + localidad
        return candidatos[0]
    return None


def _parsear(doc: str, ambitos: dict) -> list[dict]:
    """Extrae los shoppings de una página de listado."""
    salida: list[dict] = []
    bloques = re.split(r'<h2[^>]*class="wsite-content-title"[^>]*>', doc)[1:]
    for b in bloques:
        if "</h2>" not in b:
            continue
        cab = _lineas(b.split("</h2>")[0])
        if not cab:
            continue
        nombre = cab[0]
        # los encabezados de sección son <h2> también ("Shoppings de … Zona Norte")
        if _norm(nombre).startswith("shoppings de") or len(nombre) < 3:
            continue
        cuerpo = [l for l in _lineas(b.split("</h2>")[1][:3000]) if not _RUIDO.match(l)]
        if not cuerpo:
            continue
        telefono = ""
        for l in _lineas(b.split("</h2>")[1][:3000]):
            if re.match(r"^tel[:.]", l, re.I):
                telefono = re.sub(r"^tel[:.]\s*", "", l, flags=re.I).strip()
                break
        direccion = _elegir_direccion(cuerpo, nombre)
        if not direccion:
            continue
        # el ámbito se busca en TODO menos en la dirección (ver _detectar_ambito)
        amb = _detectar_ambito([l for l in cuerpo if l != direccion], ambitos)
        salida.append({
            "nombre": nombre,
            "direccion": direccion,
            "ambito_tipo": amb[0] if amb else None,
            "ambito": amb[1] if amb else None,
            "partido": amb[2] if amb else None,
            "telefono": telefono,
            "fuente": "shoppings_ar",
        })
    return salida


def fetch_shoppings_ar(urls: Optional[list[str]] = None,
                       provincia: str = "Buenos Aires",
                       timeout: float = 45.0) -> list[dict]:
    """Shoppings del directorio, **ya geocodificados** con georef-ar.

    Devuelve dicts con `nombre`, `lat`, `lng`, `fuente='shoppings_ar'` (+ `direccion`,
    `ambito`, `telefono`). Los que no se puedan ubicar se descartan con un warning: sin
    coordenada no sirven para el mapa ni para `ParcelaCategoria`.
    """
    import httpx

    from scrapitero.agents.geocode_forward import georef_ar_lote

    urls = urls or URLS_LISTADO_DEFAULT
    crudos: list[dict] = []
    with httpx.Client(timeout=timeout, headers=_UA, follow_redirects=True) as client:
        ambitos = _ambitos(client, provincia)
        if not ambitos:
            logger.warning("shoppings_ar: sin nomenclador de georef, no puedo ubicar los "
                           "shoppings por partido — se omite la fuente")
            return []
        for url in urls:
            try:
                r = client.get(url)
                r.raise_for_status()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"shoppings_ar: no pude bajar {url}: {e}")
                continue
            enc = _parsear(r.text, ambitos)
            logger.info(f"shoppings_ar: {len(enc)} shoppings en {url}")
            crudos.extend(enc)

    # dedupe por nombre dentro del propio directorio (las páginas de zona se solapan con
    # la de provincia)
    vistos: dict[str, dict] = {}
    for s in crudos:
        vistos.setdefault(_norm(s["nombre"]), s)
    unicos = list(vistos.values())

    # Geocoding gratis. Sin ámbito no se consulta (georef devolvería la misma calle en otro
    # partido, a cientos de km) — ver geocode_forward.georef_ar_lote.
    items = [{"id": str(i), "calle": s["direccion"], "numero": "",
              "provincia": provincia, "departamento": s.get("ambito") or ""}
             for i, s in enumerate(unicos) if s.get("ambito")]
    coords = georef_ar_lote(items) if items else {}

    salida: list[dict] = []
    faltan: list[tuple[int, dict]] = []
    for i, s in enumerate(unicos):
        c = coords.get(str(i))
        if c:
            salida.append({**s, "lat": c[0], "lng": c[1], "geocode": "georef"})
        else:
            faltan.append((i, s))

    # Fallback por NOMBRE: un shopping es un POI propio y conocido, no una casa. Muchos están
    # sobre accesos de ruta ("Panamericana Km 50") sin dirección postal que georef pueda
    # interpolar, pero OSM los tiene como lugar. Cada resultado se **valida con el reverse
    # oficial de georef**: si el punto no cae en el partido esperado, se descarta.
    if faltan:
        recuperados = _buscar_por_nombre(faltan, provincia, timeout)
        salida.extend(recuperados)
        faltan = [(i, s) for i, s in faltan
                  if not any(r["nombre"] == s["nombre"] for r in recuperados)]

    if faltan:
        logger.warning(f"shoppings_ar: {len(faltan)} sin coordenada: "
                       f"{', '.join(s['nombre'] for _, s in faltan[:8])}")
    logger.info(f"shoppings_ar: {len(salida)}/{len(unicos)} shoppings ubicados (gratis)")
    return salida


def _buscar_por_nombre(faltan: list[tuple[int, dict]], provincia: str,
                       timeout: float) -> list[dict]:
    """Ubica shoppings por su NOMBRE en Nominatim, validando el partido con georef.

    Nominatim es gratis pero limita a ~1 req/s, así que se va despacio a propósito. La
    validación es lo que hace confiable al fallback: sin ella, una búsqueda por nombre puede
    devolver cualquier homónimo del país.
    """
    import time

    import httpx

    out: list[dict] = []
    with httpx.Client(timeout=timeout, headers=_UA, follow_redirects=True) as client:
        for _, s in faltan:
            consulta = " ".join(x for x in (s["nombre"], s.get("ambito"), provincia,
                                            "Argentina") if x)
            try:
                r = client.get("https://nominatim.openstreetmap.org/search",
                               params={"q": consulta, "format": "json", "limit": 1,
                                       "countrycodes": "ar"})
                r.raise_for_status()
                js = r.json()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"shoppings_ar: Nominatim falló para {s['nombre']!r}: {e}")
                time.sleep(1.1)
                continue
            time.sleep(1.1)      # límite de uso de Nominatim
            if not js:
                continue
            lat, lng = float(js[0]["lat"]), float(js[0]["lon"])
            # se valida contra el PARTIDO: el reverse devuelve departamento/municipio, y el
            # ámbito del directorio suele ser la localidad ("Martínez" → San Isidro).
            esperado = s.get("partido") or s.get("ambito")
            if not _valida_ambito(client, lat, lng, esperado):
                logger.info(f"shoppings_ar: descarto {s['nombre']!r} — el punto de OSM no "
                            f"cae en {esperado!r}")
                continue
            out.append({**s, "lat": lat, "lng": lng, "geocode": "osm_nombre"})
    if out:
        logger.info(f"shoppings_ar: {len(out)} ubicados por nombre en OSM "
                    "(validados contra el partido con georef)")
    return out


def _valida_ambito(client, lat: float, lng: float, ambito: Optional[str]) -> bool:
    """El punto tiene que caer en el partido/localidad que declara el directorio."""
    if not ambito:
        return False
    try:
        r = client.get(f"{_GEOREF}/ubicacion",
                       params={"lat": lat, "lon": lng,
                               "campos": "departamento,municipio,provincia"})
        r.raise_for_status()
        ub = (r.json().get("ubicacion") or {})
    except Exception:  # noqa: BLE001
        return False
    esperado = _norm(ambito)
    for clave in ("departamento", "municipio"):
        nombre = _norm((ub.get(clave) or {}).get("nombre"))
        if nombre and (esperado in nombre or nombre in esperado):
            return True
    return False
