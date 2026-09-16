"""Alturas oficiales del callejero de ARBA → anclas para numerar las parcelas de PBA.

El agujero que tapa: ARBA publica la geometría de la parcela pero **no su domicilio** —
verificado contra todos los endpoints de `carto.arba.gov.ar` (`getInfo`, `getPdoPda`,
`getNomencla`, tabla de capas) y en dos partidos distintos: el bloque «Dirección» viene
vacío siempre. Así que en la provincia de Buenos Aires el número de puerta hay que
inferirlo, y para inferirlo bien hacen falta **anclas**: direcciones con número real y
posición. En Merlo había 165 para 750 calles.

Lo que sí tiene ARBA es un **callejero propio con rangos de altura** y un geocodificador
oficial, los dos gratis y en la misma sesión que ya usa `ARBACartoFetcher`:

    GET partido/loadStreetsByPartido?pdo=72
        → [{"idcalle": 23134, "nombre": "1", "partido": 72, "altura": false}, ...]
          `altura` dice si ARBA conoce numeración de esa calle (240 de 1.193 en Merlo).

    GET partido/getDireccion?idcalle=25616&altura=100&epsg=EPSG:4326
        → {"result": "POINT(-58.698831 -34.667086)", "found": true}
        → {"result": "", "found": false, "message": "La direccion no puede ser encontrada"}

⚠ **Valida contra rangos reales, no interpola a ciegas**: «Palmar 100» devuelve
`found:false` y «Palmar 1700» resuelve, porque la numeración real de esa calle arranca en
1622. Eso es lo que lo hace útil como ancla — y también implica que un `found:false` NO
prueba que la altura no exista: puede ser un hueco del propio callejero (medido: 8 de 20
consultas dieron `false` en calles que sí tienen esas alturas).

⚠ **El número que sale de acá NO es el domicilio declarado del lote**: es interpolación
sobre tramos oficiales, la misma naturaleza que `georef-ar`. Por eso se escribe en
`numero_estimado` con el sello propio `arba_calles`, **nunca** en `parcelas.numero` ni con
el sello `catastro`. La diferencia importa: `parcelas.numero` es lo que declaró el
municipio y es lo que el CSV publica como número oficial.

⚠ **El lote de esquina se mal-asigna**, como siempre: medido, «Palmar 2100» cae en una
parcela que nuestro catastro rotula *Avenida Hipólito Yrigoyen*. Por eso el punto sólo
ancla la parcela si la calle de la parcela **coincide** con la calle consultada.

Medido en Merlo (16-sep-2026): 1.193 calles, 240 con altura; de las 453 calles que
quedaron sin número utilizable, **88 tienen altura en ARBA (5.225 parcelas)**, y hay otras
183 calles (32.532 parcelas) donde sirve para **validar** lo ya estimado. El cruce contra
`DireccionAR` dio Mariano Acosta 1050/1150/1250/1350 contra 1045/1151/1253/1345 — o sea
que la interpolación propia está bien calibrada y esto la ancla y le caza los desvíos.
"""
from __future__ import annotations

import re
import time
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.agents._run import agent_run
from scrapitero.agents.arba_carto_fetcher import (CARTO_BASE, _UA_HEADERS, _extract_jsessionid,
                                                  _load_session)
from scrapitero.agents.direccion_norm import normalizar_calle
from scrapitero.db.engine import get_engine

_FUENTE = "arba_calles"          # sello de `numero_estimado_metodo`
_RE_POINT = re.compile(r"POINT\s*\(\s*(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s*\)", re.I)


class ARBACallesInput(BaseModel):
    region_id: str
    survey_id: str
    jsessionid: Optional[str] = None
    # Cada cuántos números se consulta una calle. 50 es media cuadra: alcanza para anclar
    # el tramo sin pedirle a ARBA una consulta por puerta.
    paso: int = 50
    delay_ms: int = 1300         # ARBA es un organismo público: un hilo y sin apuro
    max_requests: int = 8000     # tope duro; Merlo entero son ~4.800
    solo_calles_sin_numero: bool = True   # False = también las que ya tienen, para validar
    dry_run: bool = False


class ARBACallesOutput(BaseModel):
    ok: bool = True
    error: Optional[str] = None
    calles_arba: int = 0
    calles_con_altura: int = 0
    calles_consultadas: int = 0
    requests: int = 0
    puntos_encontrados: int = 0
    puntos_sin_resultado: int = 0
    anclas_en_parcela: int = 0      # cayeron dentro de una parcela CON la misma calle
    anclas_otra_calle: int = 0      # cayeron en una parcela de otra calle (esquinas)
    anclas_fuera: int = 0           # no cayeron en ninguna parcela del survey
    escritas: int = 0
    needs_cookies: bool = False


def _partido_de(conn, survey_id: str) -> Optional[int]:
    """Código de partido de ARBA (Merlo = 72), del prefijo del cca_code de las parcelas.

    Se deduce del dato y no se pide por parámetro: el survey ya trae las parcelas de ARBA,
    y su `cca_code` empieza por el partido en 3 dígitos (`072...`).
    """
    fila = conn.execute(text("""
        SELECT substring(cca_code from 1 for 3) AS pref, count(*) AS n
        FROM parcelas
        WHERE survey_id = CAST(:s AS uuid) AND cca_code IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 1
    """), {"s": survey_id}).fetchone()
    if not fila or not fila[0] or not str(fila[0]).isdigit():
        return None
    return int(fila[0])


def _calles(client: httpx.Client, partido: int) -> list[dict]:
    r = client.get(f"{CARTO_BASE}/partido/loadStreetsByPartido",
                   params={"pdo": str(partido)},
                   headers={"X-Requested-With": "XMLHttpRequest",
                            "Referer": f"{CARTO_BASE}/"}, timeout=60)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else (data.get("result") or [])


def _direccion(client: httpx.Client, idcalle: int, altura: int):
    """(lat, lng) de esa altura, o None si ARBA no la conoce."""
    r = client.get(f"{CARTO_BASE}/partido/getDireccion",
                   params={"idcalle": str(idcalle), "altura": str(altura),
                           "epsg": "EPSG:4326"},
                   headers={"X-Requested-With": "XMLHttpRequest",
                            "Referer": f"{CARTO_BASE}/"}, timeout=30)
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return None
    if not data.get("found"):
        return None
    m = _RE_POINT.search(data.get("result") or "")
    if not m:
        return None
    lng, lat = float(m.group(1)), float(m.group(2))
    return lat, lng


@agent_run
def run(input: ARBACallesInput) -> ARBACallesOutput:
    out = ARBACallesOutput()
    engine = get_engine()

    jsessionid = input.jsessionid or _load_session()
    if jsessionid and "=" in jsessionid:
        jsessionid = _extract_jsessionid(jsessionid) or jsessionid
    if not jsessionid:
        return ARBACallesOutput(ok=False, needs_cookies=True,
                                error="Sin JSESSIONID de carto.arba.gov.ar. Conseguir la "
                                      "cookie como para ARBACartoFetcher.")

    with engine.connect() as conn:
        partido = _partido_de(conn, input.survey_id)
        if not partido:
            return ARBACallesOutput(ok=False, error=(
                "No se pudo deducir el partido: las parcelas del survey no tienen cca_code. "
                "Correr ARBACartoFetcher/ARBACadastralFetcher primero."))
        # Las calles del relevamiento y cuáles ya tienen número utilizable.
        filas = conn.execute(text("""
            SELECT calle,
                   count(*) AS n,
                   count(*) FILTER (WHERE COALESCE(NULLIF(numero, '0'), '') <> ''
                                       OR numero_estimado_metodo = 'georef_cuadra') AS con_num
            FROM parcelas
            WHERE survey_id = CAST(:s AS uuid) AND calle IS NOT NULL AND calle <> ''
            GROUP BY 1
        """), {"s": input.survey_id}).fetchall()
    del_survey = {normalizar_calle(c): (c, n, con) for c, n, con in filas}
    logger.info(f"arba_calles: partido {partido}, {len(del_survey)} calles en el relevamiento")

    with httpx.Client(follow_redirects=True, headers=_UA_HEADERS) as client:
        client.get(f"{CARTO_BASE}/", timeout=15)
        client.cookies.set("JSESSIONID", jsessionid, domain="carto.arba.gov.ar")
        try:
            calles = _calles(client, partido)
        except Exception as exc:  # noqa: BLE001
            return ARBACallesOutput(ok=False, error=f"loadStreetsByPartido falló: {exc}")
        out.calles_arba = len(calles)

        objetivo = []
        for c in calles:
            if not c.get("altura"):
                continue
            out.calles_con_altura += 1
            k = normalizar_calle(c.get("nombre") or "")
            enc = del_survey.get(k)
            if not enc:
                continue                       # calle que el relevamiento no toca
            _, n, con_num = enc
            if input.solo_calles_sin_numero and con_num >= n:
                continue                       # esa calle ya está numerada
            objetivo.append((c, k))
        logger.info(f"arba_calles: {out.calles_con_altura} calles con altura en ARBA, "
                    f"{len(objetivo)} a consultar")
        if input.dry_run:
            out.calles_consultadas = len(objetivo)
            return out

        anclas: list[tuple] = []               # (calle_norm, altura, lat, lng)
        for c, k in objetivo:
            if out.requests >= input.max_requests:
                logger.warning(f"arba_calles: tope de {input.max_requests} requests")
                break
            out.calles_consultadas += 1
            # Barrido de alturas: sin conocer el rango, se prueba de `paso` en `paso` hasta
            # que se acumulan fallos seguidos — así una calle corta no gasta 100 consultas.
            fallos = 0
            for altura in range(input.paso, 9000, input.paso):
                if out.requests >= input.max_requests or fallos >= 6:
                    break
                out.requests += 1
                pt = _direccion(client, int(c["idcalle"]), altura)
                time.sleep(input.delay_ms / 1000)
                if pt is None:
                    out.puntos_sin_resultado += 1
                    fallos += 1
                    continue
                fallos = 0
                out.puntos_encontrados += 1
                anclas.append((k, altura, pt[0], pt[1]))

    if not anclas:
        logger.info("arba_calles: ARBA no devolvió ninguna altura para esas calles")
        return out

    # Vinculación: el punto ancla la parcela que lo contiene, y sólo si la calle coincide.
    # Un punto de esquina cae en el lote del frente transversal (medido: «Palmar 2100» en un
    # lote rotulado Av. Hipólito Yrigoyen), y ahí el número pertenece a la otra numeración.
    escritas = 0
    with engine.begin() as conn:
        for k, altura, lat, lng in anclas:
            fila = conn.execute(text("""
                SELECT parcela_id::text, calle FROM parcelas
                WHERE survey_id = CAST(:s AS uuid)
                  AND geometry IS NOT NULL
                  AND ST_Contains(geometry, ST_SetSRID(ST_MakePoint(:lng, :lat), 4326))
                LIMIT 1
            """), {"s": input.survey_id, "lat": lat, "lng": lng}).fetchone()
            if not fila:
                out.anclas_fuera += 1
                continue
            if normalizar_calle(fila[1] or "") != k:
                out.anclas_otra_calle += 1
                continue
            out.anclas_en_parcela += 1
            # ⚠ Va a `numero_estimado`, NO a `parcelas.numero`: es interpolación oficial,
            # no el domicilio declarado. Y no pisa a `manual` ni al número del municipio.
            escritas += conn.execute(text("""
                UPDATE parcelas SET numero_estimado = :num,
                                    numero_estimado_metodo = :met,
                                    numero_estimado_confianza = 0.9
                WHERE parcela_id = CAST(:pid AS uuid)
                  AND COALESCE(NULLIF(numero, '0'), '') = ''
                  AND COALESCE(direccion_source, '') <> 'manual'
            """), {"num": str(altura), "met": _FUENTE, "pid": fila[0]}).rowcount or 0
    out.escritas = escritas
    logger.info(f"arba_calles: {out.puntos_encontrados} alturas oficiales, "
                f"{out.anclas_en_parcela} ancladas a parcela, {escritas} escritas "
                f"({out.anclas_otra_calle} de otra calle, {out.anclas_fuera} fuera)")
    return out
