"""IncidenciasReporter — junta en un solo lugar los casos que sólo un humano puede resolver.

Antes cada señal moría en un log, un aviso de Telegram o una página ad-hoc de un solo tipo (la
asistencia de hoteles). Este agente las escanea todas y las carga en `incidencias` (mig. 046),
que alimenta la página `/incidencias/{survey_id}` donde el operador las resuelve.

Tipos que genera hoy:
  - `hotel_sin_habitaciones`: hotel abierto sin habitaciones en ninguna fuente (Cadastur caído,
    Google no las trae). Misma condición que la vieja asistencia de hoteles.
  - `altura_sin_declarar`: el catastro no declara construcción pero el satélite ve un edificio.
  - `altura_mas_alta`: el satélite ve más pisos que los que sugiere el catastro.
  - `uf_imposible`: la UF declarada no cabe en el volumen visible (m²/UF absurdo) — detecta
    errores de carga tanto del baseline como del BCI. El umbral vive acá (no en
    `altura_fetcher`) para no tocar el criterio de una capa ya corrida.

**Idempotente preservando el trabajo humano**: upsert por `(survey_id, tipo, clave)`, donde
`clave` es natural y estable entre corridas — `<tipo>:<parcela_id>` para parcelas y
`hotel:<cnpj|nombre_norm>` para hoteles (el `hotel_id` NO sirve: `HotelFetcher` borra+reinserta).
El `ON CONFLICT` refresca el contenido del caso pero **nunca** pisa `estado`/`resolucion`/`nota`,
así re-correr 🏨 o 📏 no reabre lo ya resuelto. Las pendientes cuya condición ya no se cumple
pasan a `obsoleta` en vez de quedar colgadas.
"""

from __future__ import annotations

import json
import math
import uuid
from typing import Optional

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.direccion_norm import nucleo_calle
from scrapitero.agents.hotel_fetcher import _norm
from scrapitero.db.engine import get_engine

# Prioridad por tipo (1 = mirar primero). `sin_declarar` y `uf_imposible` son los que más
# cambian el resultado del relevamiento; `mas_alto` suele ser una diferencia de criterio.
_PRIORIDAD = {
    "altura_sin_declarar": 1,
    "uf_imposible": 1,
    # El geocoding dudoso se revisa ANTES de dibujar la zona: si una dirección del
    # relevamiento anterior está mal ubicada, la zona sale mal y se relevan las parcelas
    # equivocadas. Es el que más temprano hay que atender.
    "geocoding_dudoso": 1,
    "hotel_sin_habitaciones": 2,
    "altura_mas_alta": 3,
}


class IncidenciasInput(BaseModel):
    region_id: str
    survey_id: str
    # Piso de m² por UF declarada bajo el cual se considera físicamente inverosímil.
    m2_por_uf_min: float = 25.0
    # Distancia a la parcela más cercana de su MISMA calle a partir de la cual una dirección
    # del relevamiento anterior se considera mal ubicada (ver `_casos_geocoding`).
    lejos_calle_m: float = 150.0


class IncidenciasOutput(BaseModel):
    ok: bool = True
    error: Optional[str] = None
    creadas: int = 0
    actualizadas: int = 0
    obsoletas: int = 0
    pendientes: int = 0
    por_tipo: dict = {}


def _direccion(calle, numero, numero_est=None) -> str:
    """Dirección para el título de la incidencia.

    Cuando el catastro no trae altura (`0` o vacío) y `NumeroEstimator` infirió una, se muestra
    **marcada con ≈** — el operador tiene que poder ubicar el caso en la calle sin confundir el
    número inferido con el oficial. Antes estas tarjetas decían «SALIN NADAF 0», que no ubica nada."""
    num = (numero or "").strip()
    if num in ("", "0") and (numero_est or "").strip():
        num = f"≈{str(numero_est).strip()}"
    d = " ".join(x for x in [(calle or "").strip(), num] if x)
    return d or "(sin dirección)"


# ── Escaneo de cada fuente ────────────────────────────────────────────────────────────

def _casos_hoteles(conn, region_id: str, survey_id: str) -> list[dict]:
    """Hoteles abiertos sin habitaciones. Misma condición que la asistencia original."""
    rows = conn.execute(text("""
        SELECT hotel_id::text, nombre, cnpj, tipo, telefono, direccion,
               COALESCE(business_status, situacion_cadastur) AS estado, fuente,
               ST_Y(location) AS lat, ST_X(location) AS lng, parcela_id::text
        FROM hoteles
        WHERE region_id = :rid
          AND (survey_id = CAST(:sid AS uuid) OR survey_id IS NULL)
          AND NOT cerrado_def AND habitaciones IS NULL AND location IS NOT NULL
        ORDER BY nombre
    """), {"rid": region_id, "sid": survey_id}).fetchall()

    casos = []
    for r in rows:
        cnpj = (r[2] or "").strip()
        # Clave natural: CNPJ si lo hay; si no (Google no trae), nombre normalizado.
        clave = f"hotel:{cnpj}" if cnpj else f"hotel:{_norm(r[1])}"
        casos.append({
            "tipo": "hotel_sin_habitaciones",
            "clave": clave,
            "titulo": f"🏨 {r[1] or '(sin nombre)'} — sin habitaciones",
            "detalle": "Ninguna fuente trae la cantidad de habitaciones (UHs). "
                       "Cargala a mano o marcá que no es un hotel.",
            "lat": float(r[8]) if r[8] is not None else None,
            "lng": float(r[9]) if r[9] is not None else None,
            "parcela_id": r[10],
            "hotel_cnpj": cnpj or None,
            "datos": {
                "hotel_id": r[0],          # volátil: sirve para la acción de esta corrida
                "nombre": r[1], "cnpj": cnpj or None, "tipo_hotel": r[3],
                "telefono": r[4], "direccion": r[5], "situacion": r[6], "fuente": r[7],
            },
        })
    return casos


def _casos_altura(conn, survey_id: str) -> list[dict]:
    """Discrepancias entre la altura satelital y lo que declara el catastro."""
    rows = conn.execute(text("""
        SELECT a.parcela_id::text, a.motivo, a.altura_m, a.pisos_satelital, a.pisos_bci_proxy,
               a.ground_area_m2, a.imagery_year, a.imagery_quality,
               p.calle, p.numero, p.uso_principal, p.area_m2_construida, p.cca_code,
               p.centroid_lat, p.centroid_lng, p.numero_estimado
        FROM parcela_altura a
        JOIN parcelas p ON p.parcela_id = a.parcela_id
        WHERE a.survey_id = :sid AND a.motivo IN ('sin_declarar', 'mas_alto')
    """), {"sid": survey_id}).fetchall()

    casos = []
    for r in rows:
        pid, motivo, altura, pisos_sat, pisos_bci, huella, img_year, img_q = r[0:8]
        calle, numero, uso, area_c, cca = r[8], r[9], r[10], r[11], r[12]
        tipo = "altura_sin_declarar" if motivo == "sin_declarar" else "altura_mas_alta"
        dir_txt = _direccion(calle, numero, r[15])
        if motivo == "sin_declarar":
            titulo = f"🏗 {dir_txt} — construcción no declarada"
            detalle = (f"El catastro no declara área construida (uso «{uso or 's/d'}»), pero el "
                       f"satélite ve un edificio de {altura:.1f} m (~{pisos_sat} piso/s) sobre "
                       f"{huella:.0f} m² de huella.")
        else:
            titulo = f"🏢 {dir_txt} — más alto de lo declarado"
            detalle = (f"El satélite ve {pisos_sat} piso/s ({altura:.1f} m) y el catastro sugiere "
                       f"{pisos_bci} ({area_c:.0f} m² construidos)." if area_c else
                       f"El satélite ve {pisos_sat} piso/s ({altura:.1f} m) vs {pisos_bci} del catastro.")
        casos.append({
            "tipo": tipo,
            "clave": f"{tipo}:{pid}",
            "titulo": titulo,
            "detalle": detalle,
            "lat": float(r[13]) if r[13] is not None else None,
            "lng": float(r[14]) if r[14] is not None else None,
            "parcela_id": pid,
            "hotel_cnpj": None,
            "datos": {
                "direccion": dir_txt, "cca_code": cca, "uso": uso,
                # Número inferido (NumeroEstimator) cuando el catastro no lo trae: va aparte
                # para que la tarjeta pueda aclarar que el ≈ del título es una estimación.
                "numero_estimado": r[15] or None,
                "altura_m": round(float(altura), 1) if altura is not None else None,
                "pisos_satelital": pisos_sat, "pisos_bci_proxy": pisos_bci,
                "ground_area_m2": round(float(huella), 0) if huella else None,
                "area_m2_construida": round(float(area_c), 1) if area_c else None,
                # Obligatorio mostrarlo: en VG el 89% de la imagen es de 2014, así que el
                # dato NO es "estado actual" y el operador tiene que saberlo.
                "imagery_year": img_year, "imagery_quality": img_q,
            },
        })
    return casos


def _casos_uf_imposible(conn, survey_id: str, m2_min: float) -> list[dict]:
    """UF declarada que no cabe en el volumen visible (altura × huella)."""
    rows = conn.execute(text("""
        SELECT p.parcela_id::text, p.calle, p.numero, p.uf_vivienda, p.uf_comercio, p.uf_fuente,
               p.area_m2_construida, p.cca_code, p.centroid_lat, p.centroid_lng,
               a.altura_m, a.pisos_satelital, a.ground_area_m2, a.imagery_year,
               (a.ground_area_m2 * a.pisos_satelital / p.uf_vivienda) AS m2_por_uf,
               p.numero_estimado
        FROM parcela_altura a
        JOIN parcelas p ON p.parcela_id = a.parcela_id
        WHERE a.survey_id = :sid
          AND p.uf_vivienda > 1
          AND a.pisos_satelital IS NOT NULL
          AND a.ground_area_m2 > 0
          AND (a.ground_area_m2 * a.pisos_satelital / p.uf_vivienda) < :m2min
    """), {"sid": survey_id, "m2min": m2_min}).fetchall()

    casos = []
    for r in rows:
        pid, calle, numero, uf_v, uf_c, uf_fuente = r[0], r[1], r[2], r[3], r[4], r[5]
        area_c, cca, lat, lng = r[6], r[7], r[8], r[9]
        altura, pisos, huella, img_year, m2_uf = r[10], r[11], r[12], r[13], float(r[14])
        dir_txt = _direccion(calle, numero, r[15])
        casos.append({
            "tipo": "uf_imposible",
            "clave": f"uf_imposible:{pid}",
            "titulo": f"⚠ {dir_txt} — {uf_v} UF no caben en lo construido",
            "detalle": (f"Declara {uf_v} unidades de vivienda (fuente «{uf_fuente or 's/d'}») pero el "
                        f"volumen visible es {huella:.0f} m² × {pisos} piso/s ⇒ "
                        f"{m2_uf:.1f} m² por unidad. Verificá el dato y corregí la UF."),
            "lat": float(lat) if lat is not None else None,
            "lng": float(lng) if lng is not None else None,
            "parcela_id": pid,
            "hotel_cnpj": None,
            "datos": {
                "direccion": dir_txt, "cca_code": cca, "numero_estimado": r[15] or None,
                "uf_vivienda": uf_v, "uf_comercio": uf_c, "uf_fuente": uf_fuente,
                "m2_por_uf": round(m2_uf, 1),
                "altura_m": round(float(altura), 1) if altura is not None else None,
                "pisos_satelital": pisos,
                "ground_area_m2": round(float(huella), 0) if huella else None,
                "area_m2_construida": round(float(area_c), 1) if area_c else None,
                "imagery_year": img_year,
            },
        })
    return casos


def _casos_geocoding(conn, survey_id: str, region_id: str,
                     lejos_calle_m: float = 150.0) -> list[dict]:
    """Direcciones del relevamiento anterior cuya ubicación no es de fiar.

    **Por qué importa antes que nada:** con estos puntos se define la zona a relevar (el
    polígono se arma proyectando cada dirección sobre el eje de su calle). Si una dirección
    está mal ubicada, la zona sale mal y **se bajan las parcelas equivocadas** — medido en VG:
    la zona generada con las coordenadas viejas era casi el doble de grande y el 17% de las
    parcelas relevadas no correspondía.

    Dos señales, ambas por geometría (sin costo):
      - `parcela_ajena`: la coordenada cae dentro de una parcela cuya calle NO es la de la
        dirección. Es el error que se puede demostrar.
      - `lejos_de_su_calle`: no cae en ninguna parcela Y está a más de `lejos_calle_m` de
        **toda** parcela de su propia calle → el punto quedó en otro tramo o en otra parte de
        la ciudad.

    Lo que **no** se marca (y es la diferencia entre una lista útil y ruido): caer *entre*
    parcelas. `catastro_interp` interpola entre dos anclas de la misma calle, así que el punto
    aterriza normalmente en la vía o en un lote sin dirección — está en el tramo correcto. Con
    el criterio ingenuo "no cae en ninguna parcela" salían 163 casos en VG, de los cuales 144
    eran esto; midiendo la distancia a su propia calle quedan los que de verdad están mal.

    Se excluyen las fuentes exactas del catastro/geocodebr (`catastro`, `g:numero`): ubican
    por dirección oficial y marcarlas sería ruido."""
    rows = conn.execute(text("""
        WITH bd AS (
            SELECT d.id::text AS id, d.calle, d.numero, d.direccion_raw, d.lat, d.lng,
                   d.geocode_source, d.geocode_confidence
            FROM baseline_direcciones d
            JOIN surveys s ON s.baseline_id = d.baseline_id
            WHERE s.survey_id = CAST(:sid AS uuid)
              AND d.lat IS NOT NULL AND d.calle IS NOT NULL
              AND COALESCE(d.geocode_source, '') NOT IN ('catastro', 'g:numero')
        )
        SELECT bd.id, bd.calle, bd.numero, bd.direccion_raw, bd.lat, bd.lng,
               bd.geocode_source, bd.geocode_confidence,
               p.calle AS calle_parcela, p.numero AS numero_parcela, p.cca_code
        FROM bd
        LEFT JOIN parcelas p
               ON p.region_id = :rid AND p.geometry IS NOT NULL AND p.calle IS NOT NULL
              AND ST_Contains(p.geometry, ST_SetSRID(ST_MakePoint(bd.lng, bd.lat), 4326))
    """), {"sid": survey_id, "rid": region_id}).fetchall()

    # Centroides de las parcelas relevadas, agrupados por núcleo de calle. Sirven para medir
    # a qué distancia está la dirección de SU propia calle (el criterio que evita marcar los
    # puntos que simplemente cayeron entre dos lotes).
    por_calle: dict[str, list] = {}
    for calle_p, la, ln in conn.execute(text("""
        SELECT calle, centroid_lat, centroid_lng FROM parcelas
        WHERE region_id = :rid AND calle IS NOT NULL
          AND centroid_lat IS NOT NULL AND centroid_lng IS NOT NULL
    """), {"rid": region_id}).fetchall():
        nuc_p = nucleo_calle(calle_p)
        if nuc_p:
            por_calle.setdefault(nuc_p, []).append((float(la), float(ln)))

    def _dist_min_m(lat: float, lng: float, nuc: str) -> Optional[float]:
        """Distancia (m) a la parcela relevada más cercana de esa misma calle. None si esa
        calle no se relevó (entonces no es un problema de geocoding)."""
        pts = por_calle.get(nuc)
        if not pts:
            return None
        cos_lat = math.cos(math.radians(lat))
        mejor = float("inf")
        for pla, pln in pts:
            dx = (pln - lng) * 111320.0 * cos_lat
            dy = (pla - lat) * 110540.0
            d = math.hypot(dx, dy)
            if d < mejor:
                mejor = d
        return mejor

    casos = []
    for (did, calle, numero, raw, lat, lng, src, conf,
         calle_p, numero_p, cca) in rows:
        nuc = nucleo_calle(calle)
        if not nuc:
            continue
        dir_txt = _direccion(calle, numero)
        if calle_p is not None:
            if nucleo_calle(calle_p) == nuc:
                continue                      # cae en su propia calle → está bien
            motivo, señal = "parcela_ajena", (
                f"La coordenada cae dentro de una parcela de «{calle_p}"
                f"{' ' + numero_p if numero_p else ''}», que no es su calle.")
        else:
            d = _dist_min_m(float(lat), float(lng), nuc)
            if d is None or d <= lejos_calle_m:
                # Cae entre parcelas pero junto a su calle (normal en un punto interpolado),
                # o esa calle no se relevó → no es un problema de geocoding.
                continue
            motivo, señal = "lejos_de_su_calle", (
                f"La coordenada está a ~{d:.0f} m de la parcela más cercana de su propia "
                f"calle — quedó en otro tramo o en otra zona.")

        casos.append({
            "tipo": "geocoding_dudoso",
            "clave": f"geocoding_dudoso:{did}",
            "titulo": f"📍 {dir_txt} — ubicación dudosa",
            "detalle": (f"{señal} Fuente del geocoding: «{src or 's/d'}». "
                        "Verificá la ubicación antes de definir la zona a relevar."),
            "lat": float(lat), "lng": float(lng),
            # No se vincula a la parcela ajena a propósito: la incidencia es de la DIRECCIÓN,
            # y colgarla de esa parcela invitaría a "corregirla" con acciones de parcela.
            "parcela_id": None,
            "hotel_cnpj": None,
            "datos": {
                "direccion": dir_txt, "direccion_raw": raw,
                "motivo_geo": motivo,
                "geocode_source": src,
                "geocode_confidence": round(float(conf), 3) if conf is not None else None,
                "calle_parcela": calle_p, "numero_parcela": numero_p, "cca_code": cca,
                "baseline_direccion_id": did,
            },
        })
    return casos


# ── Entry point ───────────────────────────────────────────────────────────────────────

@agent_run
def run(input: IncidenciasInput) -> IncidenciasOutput:
    out = IncidenciasOutput()
    engine = get_engine()

    with engine.connect() as conn:
        casos = (_casos_hoteles(conn, input.region_id, input.survey_id)
                 + _casos_altura(conn, input.survey_id)
                 + _casos_uf_imposible(conn, input.survey_id, input.m2_por_uf_min)
                 + _casos_geocoding(conn, input.survey_id, input.region_id,
                                    input.lejos_calle_m))

    por_tipo: dict[str, int] = {}
    for c in casos:
        por_tipo[c["tipo"]] = por_tipo.get(c["tipo"], 0) + 1

    with engine.begin() as conn:
        for c in casos:
            # ON CONFLICT refresca SOLO el contenido del caso. `estado`, `resolucion`, `nota`,
            # `autor` y `resuelta_at` quedan intactos: el trabajo del operador no se pisa.
            res = conn.execute(text("""
                INSERT INTO incidencias
                    (incidencia_id, region_id, survey_id, tipo, clave, prioridad,
                     titulo, detalle, datos, lat, lng, parcela_id, hotel_cnpj)
                VALUES
                    (:iid, :rid, :sid, :tipo, :clave, :prio,
                     :titulo, :detalle, CAST(:datos AS jsonb), :lat, :lng,
                     CAST(:pid AS uuid), :cnpj)
                ON CONFLICT (survey_id, tipo, clave) DO UPDATE SET
                    titulo = EXCLUDED.titulo,
                    detalle = EXCLUDED.detalle,
                    datos = EXCLUDED.datos,
                    lat = EXCLUDED.lat,
                    lng = EXCLUDED.lng,
                    parcela_id = EXCLUDED.parcela_id,
                    hotel_cnpj = EXCLUDED.hotel_cnpj,
                    prioridad = EXCLUDED.prioridad,
                    actualizada_at = now(),
                    -- una incidencia marcada obsoleta que vuelve a aparecer se reabre
                    estado = CASE WHEN incidencias.estado = 'obsoleta'
                                  THEN 'pendiente' ELSE incidencias.estado END
                -- xmax = 0 distingue INSERT de UPDATE en un upsert (comparar creada_at con
                -- actualizada_at no sirve: now() es constante dentro de la transacción).
                RETURNING (xmax = 0) AS es_nueva
            """), {
                "iid": str(uuid.uuid4()), "rid": input.region_id, "sid": input.survey_id,
                "tipo": c["tipo"], "clave": c["clave"], "prio": _PRIORIDAD.get(c["tipo"], 2),
                "titulo": c["titulo"], "detalle": c["detalle"],
                "datos": json.dumps(c["datos"], ensure_ascii=False),
                "lat": c["lat"], "lng": c["lng"],
                "pid": c["parcela_id"], "cnpj": c["hotel_cnpj"],
            }).fetchone()
            if res and res[0]:
                out.creadas += 1
            else:
                out.actualizadas += 1

        # Las pendientes que ya no aparecen en el escaneo se marcan obsoletas (el dato se
        # corrigió por otra vía, o un re-run de altura/hoteles cambió el número).
        claves = [f"{c['tipo']}|{c['clave']}" for c in casos]
        r = conn.execute(text("""
            UPDATE incidencias SET estado = 'obsoleta', actualizada_at = now()
            WHERE survey_id = CAST(:sid AS uuid) AND estado = 'pendiente'
              AND (tipo || '|' || clave) <> ALL(:claves)
        """), {"sid": input.survey_id, "claves": claves or [""]})
        out.obsoletas = r.rowcount or 0

        out.pendientes = conn.execute(text(
            "SELECT count(*) FROM incidencias WHERE survey_id = CAST(:sid AS uuid) "
            "AND estado = 'pendiente'"), {"sid": input.survey_id}).scalar() or 0

    out.por_tipo = por_tipo
    logger.info(f"IncidenciasReporter {input.region_id}: creadas={out.creadas} "
                f"actualizadas={out.actualizadas} obsoletas={out.obsoletas} "
                f"pendientes={out.pendientes} por_tipo={por_tipo}")
    return out
