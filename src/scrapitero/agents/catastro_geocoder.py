"""CatastroGeocoder — geocoding por el CATASTRO, la fuente más precisa que tenemos.

Invierte el problema. La cadena clásica es:

    dirección → (API de geocoding) → coordenada → ¿en qué parcela cae? → parcela

y acumula error en dos pasos: la API devuelve el centro de la calle o del barrio, y ese punto
cae en una parcela cualquiera. Medido en VG: **19,3%** de las direcciones del relevamiento
anterior que caen dentro de una parcela, caen en una parcela de **otra calle**.

Acá se va directo:

    dirección → match contra la dirección del catastro (BCI) → **esa parcela** → su centroide

No hay error de geocoding posible en el match exacto: la parcela **es** el objeto que se quiere
identificar, y su dirección la puso el municipio. El catastro de VG trae calle+número+CEP por
parcela (`direccion_source='bci_pdf'`) junto a la geometría, así que funciona de geocoder local
y gratis.

Dos niveles de respuesta:
  - `catastro`        → match exacto calle+número. Devuelve el centroide de ESA parcela y su
                        `parcela_id` (el consumidor ya no necesita un join espacial).
  - `catastro_interp` → la calle existe en el catastro pero ese número no. Interpola entre los
                        números **reales** de esa calle (dos parcelas ancla, la anterior y la
                        posterior). Es mejor que interpolar sobre el eje OSM porque las anclas
                        son direcciones oficiales, no una proporción sobre una polilínea.

El matching de calle usa `direccion_norm.clave_direccion(tolerante=True)` (núcleo del nombre,
sin tipo de vía ni títulos) porque el CSV del cliente y el catastro rotulan distinto la misma
vía ("Rua Gov Pedro Pedrossian" vs "Avenida Pedro Pedrossian"), y
`limpiar_calle_anotacion` para sacar el loteamento entre paréntesis.
"""

from __future__ import annotations

from typing import Optional

from loguru import logger
from sqlalchemy import text

from scrapitero.agents.direccion_norm import (limpiar_calle_anotacion, normalizar_numero,
                                              nucleo_calle)
from scrapitero.db.engine import get_engine


# Solo se aceptan como ancla las direcciones de origen AUTORITATIVO (el organismo que
# cataloga el inmueble). Quedan afuera a propósito `google_maps`/`google`/`mapbox`/`nominatim`
# (vinieron de geocodificar, así que usarlas para validar un geocoder es circular: le creeríamos
# a Google para corregir a Google) y `baseline` (es el CSV del cliente, el dato que queremos
# ubicar, no una referencia). Ampliar con la fuente de catastro de cada país nuevo.
FUENTES_AUTORITATIVAS = ("bci_pdf", "smartgis_vg", "catastro", "salta_idemsa", "salta_idesa",
                         "arba_carto", "arba_idera", "sigef_onr")


def _municipio_por_ciudad(ciudad: str) -> Optional[str]:
    """`municipio_codigo` de una ciudad, tomándolo de las regiones YA relevadas cuyo nombre
    la menciona. Sin red: se apoya en que para relevar esa ciudad antes hubo una región con
    el código IBGE cargado. Devuelve None si no hay ninguna (primer relevamiento de la ciudad
    ⇒ no hay catastro que indexar, y el flujo sigue con geocodebr/APIs)."""
    nuc = " ".join(nucleo_calle(ciudad).split())
    if not nuc:
        return None
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT r.municipio_codigo, count(p.parcela_id) AS n
            FROM regions r
            JOIN parcelas p ON p.region_id = r.region_id
            WHERE r.municipio_codigo IS NOT NULL
              AND translate(lower(r.name), 'áàâãéêíóôõúüç', 'aaaaeeiooouuc') LIKE '%' || :c || '%'
            GROUP BY r.municipio_codigo
            ORDER BY n DESC
            LIMIT 1
        """), {"c": nuc}).fetchone()
    return row[0] if row else None


class CatastroIndex:
    """Índice en memoria de las direcciones del catastro de una ciudad/región.

    Se construye una vez por corrida (una query) y se consulta por dirección."""

    def __init__(self, region_id: Optional[str] = None, municipio_codigo: Optional[str] = None,
                 ciudad: Optional[str] = None):
        self.por_calle_numero: dict[tuple, tuple] = {}   # (calle_nuc, num) → (lat, lng, pid)
        self.por_calle: dict[str, list] = {}             # calle_nuc → [(num_int, lat, lng, pid)]
        self.cep_por_calle: dict[str, str] = {}          # calle_nuc → CEP más frecuente
        # Al crear una ACTUALIZACIÓN la región nueva todavía no tiene `municipio_codigo` (se
        # completa al definir la zona, o sea DESPUÉS de geocodificar) ni parcelas propias:
        # filtrar por `region_id` daría un índice vacío y el catastro no aportaría nada.
        # Con la ciudad del baseline se resuelve el municipio de las regiones ya relevadas.
        if not municipio_codigo and ciudad:
            municipio_codigo = _municipio_por_ciudad(ciudad)
            if municipio_codigo:
                logger.info(f"CatastroIndex: municipio {municipio_codigo} resuelto por "
                            f"ciudad «{ciudad}» (la región nueva aún no lo tiene)")
        self._cargar(region_id, municipio_codigo)

    def _cargar(self, region_id: Optional[str], municipio_codigo: Optional[str]) -> None:
        engine = get_engine()
        # Se toma TODA la ciudad, no solo la región: las parcelas de otras zonas ya relevadas
        # de la misma ciudad son anclas válidas (misma numeración oficial). El filtro por
        # municipio evita mezclar ciudades.
        cond = ["p.calle IS NOT NULL", "p.geometry IS NOT NULL",
                "p.centroid_lat IS NOT NULL", "p.centroid_lng IS NOT NULL",
                "p.direccion_source = ANY(:fuentes)"]
        params: dict = {"fuentes": list(FUENTES_AUTORITATIVAS)}
        if municipio_codigo:
            cond.append("r.municipio_codigo = :muni")
            params["muni"] = municipio_codigo
        elif region_id:
            cond.append("p.region_id = :rid")
            params["rid"] = region_id
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT p.calle, p.numero, p.codigo_postal, p.centroid_lat, p.centroid_lng,
                       p.parcela_id::text
                FROM parcelas p
                JOIN regions r ON r.region_id = p.region_id
                WHERE {' AND '.join(cond)}
            """), params).fetchall()

        ceps: dict[str, dict] = {}
        for calle, numero, cep, lat, lng, pid in rows:
            nuc = nucleo_calle(calle)
            if not nuc:
                continue
            num = normalizar_numero(numero)
            if cep:
                d = ceps.setdefault(nuc, {})
                d[cep] = d.get(cep, 0) + 1
            if num:
                # El primero gana; el catastro puede tener varias unidades en la misma
                # dirección (edificio) y todas comparten el lote, así que da igual cuál.
                self.por_calle_numero.setdefault((nuc, num), (float(lat), float(lng), pid))
                self.por_calle.setdefault(nuc, []).append((int(num), float(lat), float(lng), pid))
        for nuc, lst in self.por_calle.items():
            lst.sort(key=lambda t: t[0])
        for nuc, d in ceps.items():
            self.cep_por_calle[nuc] = max(d, key=d.get)

        logger.info(f"CatastroIndex: {len(rows)} parcelas con dirección → "
                    f"{len(self.por_calle_numero)} (calle,número) exactos, "
                    f"{len(self.por_calle)} calles, {len(self.cep_por_calle)} con CEP")

    # ── Consulta ──────────────────────────────────────────────────────────────────────

    def buscar(self, calle: Optional[str], numero) -> Optional[dict]:
        """Ubica una dirección contra el catastro.

        Devuelve `{lat, lng, parcela_id, fuente, confidence}` o None si la calle no está.
        `fuente` = 'catastro' (exacto) | 'catastro_interp' (interpolado entre números reales).
        """
        calle_limpia, _anot = limpiar_calle_anotacion(calle)
        nuc = nucleo_calle(calle_limpia)
        if not nuc:
            return None
        num = normalizar_numero(numero)

        if num:
            hit = self.por_calle_numero.get((nuc, num))
            if hit:
                lat, lng, pid = hit
                return {"lat": lat, "lng": lng, "parcela_id": pid,
                        "fuente": "catastro", "confidence": 1.0}

        anclas = self.por_calle.get(nuc)
        if not anclas:
            return None
        if not num:
            # Sin número no hay dirección que ubicar: NO se inventa un punto (eso es
            # exactamente lo que hoy produce el "centro de la calle" que cae en una parcela
            # arbitraria). El caller decide qué hacer con el None.
            return None

        n = int(num)
        prev = nxt = None
        for a in anclas:
            if a[0] <= n:
                prev = a
            if a[0] >= n and nxt is None:
                nxt = a
        if prev and nxt and nxt[0] != prev[0]:
            t = (n - prev[0]) / (nxt[0] - prev[0])
            lat = prev[1] + t * (nxt[1] - prev[1])
            lng = prev[2] + t * (nxt[2] - prev[2])
            # Confianza en función de cuán cerca están las anclas en numeración: dos números
            # contiguos dan una interpolación casi exacta; un salto de 500 no.
            salto = nxt[0] - prev[0]
            conf = 0.9 if salto <= 20 else (0.75 if salto <= 100 else 0.6)
            return {"lat": lat, "lng": lng, "parcela_id": None,
                    "fuente": "catastro_interp", "confidence": conf}
        ancla = prev or nxt
        if ancla and abs(ancla[0] - n) <= 50:
            # Extremo de la calle: se usa el ancla más cercana si el número está cerca.
            return {"lat": ancla[1], "lng": ancla[2], "parcela_id": None,
                    "fuente": "catastro_interp", "confidence": 0.55}
        return None

    def cep_de_calle(self, calle: Optional[str]) -> Optional[str]:
        """CEP del catastro para esa calle (el más frecuente). Sirve para completar el CEP
        faltante del CSV del cliente antes de ir a una API paga — en Brasil el CEP es la
        señal de mayor precisión."""
        calle_limpia, _ = limpiar_calle_anotacion(calle)
        return self.cep_por_calle.get(nucleo_calle(calle_limpia))


# ── REVERSE: coordenada → dirección oficial ───────────────────────────────────────────
# El sentido inverso es **más confiable** que el forward: no hay que interpretar texto, es
# `ST_Contains` puro. Si el punto cae dentro del lote, la dirección de ese lote ES la
# dirección de ese punto, y la puso el municipio. Resuelve casos como el Amazon Aeroporto
# Hotel, cuyo pin físico caía en la parcela de Filinto Müller 62 mientras Receita daba su
# dirección FISCAL (Ponce de Arruda 50, geocodificada a 909 m).

def reverse_lote(puntos: list[tuple], region_id: Optional[str] = None,
                 municipio_codigo: Optional[str] = None,
                 solo_autoritativas: bool = True) -> dict:
    """{(lat,lng): {...}} con la dirección oficial de la parcela que contiene cada punto.

    `puntos` = [(lat, lng), …]. En lote y en **una sola query** (PostGIS resuelve el
    `ST_Contains` con el índice GiST), no una consulta por punto.
    Cada valor: `{parcela_id, calle, numero, codigo_postal, barrio, cca_code}`.
    Los puntos que no caen en ninguna parcela no aparecen en el resultado."""
    if not puntos:
        return {}
    cond = ["p.geometry IS NOT NULL", "p.calle IS NOT NULL"]
    params: dict = {
        "lats": [float(la) for la, _ in puntos],
        "lngs": [float(ln) for _, ln in puntos],
    }
    if solo_autoritativas:
        cond.append("p.direccion_source = ANY(:fuentes)")
        params["fuentes"] = list(FUENTES_AUTORITATIVAS)
    if municipio_codigo:
        cond.append("r.municipio_codigo = :muni")
        params["muni"] = municipio_codigo
    elif region_id:
        cond.append("p.region_id = :rid")
        params["rid"] = region_id

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            WITH pts AS (
                SELECT * FROM unnest(CAST(:lats AS float8[]), CAST(:lngs AS float8[]))
                         AS t(lat, lng)
            )
            SELECT DISTINCT ON (pts.lat, pts.lng)
                   pts.lat, pts.lng, p.parcela_id::text, p.calle, p.numero,
                   p.codigo_postal, p.barrio, p.cca_code
            FROM pts
            JOIN parcelas p ON ST_Contains(p.geometry, ST_SetSRID(ST_MakePoint(pts.lng, pts.lat), 4326))
            JOIN regions r ON r.region_id = p.region_id
            WHERE {' AND '.join(cond)}
            -- si varias parcelas se superponen (regiones distintas), gana la que tiene número
            ORDER BY pts.lat, pts.lng, (p.numero IS NULL), p.parcela_id
        """), params).fetchall()

    return {(float(r[0]), float(r[1])): {
        "parcela_id": r[2], "calle": r[3], "numero": r[4],
        "codigo_postal": r[5], "barrio": r[6], "cca_code": r[7],
    } for r in rows}


def reverse(lat: float, lng: float, region_id: Optional[str] = None,
            municipio_codigo: Optional[str] = None) -> Optional[dict]:
    """Dirección oficial de la parcela que contiene el punto, o None. Ver `reverse_lote`
    (preferir el lote cuando hay varios puntos)."""
    res = reverse_lote([(lat, lng)], region_id, municipio_codigo)
    return res.get((float(lat), float(lng)))
