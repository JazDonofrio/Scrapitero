"""IncidenciasReporter — junta en un solo lugar los casos que sólo un humano puede resolver.

Antes cada señal moría en un log, un aviso de Telegram o una página ad-hoc de un solo tipo (la
asistencia de hoteles). Este agente las escanea todas y las carga en `incidencias` (mig. 046),
que alimenta la página `/incidencias/{survey_id}` donde el operador las resuelve.

Tipos que genera hoy:
  - `hotel_sin_habitaciones`: hotel abierto sin habitaciones en ninguna fuente (Cadastur caído,
    Google no las trae). Misma condición que la vieja asistencia de hoteles.
  - `hotel_duplicado`: dos registros abiertos del MISMO establecimiento (re-registro societario
    o pin suelto de Google). Duplica habitaciones en el total y en el `uf_comercio` de la
    parcela. Se detecta por asimetría de evidencia, no por cercanía sola.
  - `altura_sin_declarar`: el catastro no declara construcción pero el satélite ve un edificio.
  - `altura_mas_alta`: el satélite ve más pisos que los que sugiere el catastro.
  - `numero_faltante`: el catastro no trae el número de puerta y `NumeroEstimator` tampoco pudo
    estimarlo (calle con numeración incoherente o sin anclas). Se carga a mano y queda en
    `parcela_numero_manual` (mig. 051), que sobrevive al re-scrape.
  - `uf_imposible`: la UF declarada no cabe en el volumen visible (m²/UF absurdo) — detecta
    errores de carga tanto del baseline como del BCI. El umbral vive acá (no en
    `altura_fetcher`) para no tocar el criterio de una capa ya corrida.
  - `uf_sin_declarar`: el espejo del anterior — hay construcción y CERO unidades declaradas, o
    sea la parcela aporta 0 al relevamiento. Dos vías de evidencia: el **área construida** del
    catastro donde la publican (BCI: la contradicción está dentro del propio dato, sin
    satélite), y la **huella de los footprints** donde no la publican (ARBA la deja en NULL, así
    que fuera de Brasil el caso era invisible).
  - `poi_numero_ajeno`: el comercio está parado en una parcela cuyo número no es el que él mismo
    declara, y a pocos metros hay otra que sí lo lleva. Los casos con evidencia dura los mueve
    solo `OverturePlacesFetcher`; acá quedan los ambiguos (esquinas y número contiguo).

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
from scrapitero.agents.hotel_fetcher import _nombre_fuerte, _norm
# La MISMA función que usa el CSV Operadora para decidir qué parte del complemento es
# dirección. Se importa en vez de re-implementar el criterio: si el panel y el entregable
# no coinciden en qué cuenta como identificación, el operador ve casos ya resueltos.
from scrapitero.agents.logradouro_br import clasificar_complemento
from scrapitero.agents.overture_places_fetcher import candidatos_numero_ajeno
from scrapitero.db.engine import get_engine

# Incidencias que el operador abre A MANO desde el mapa (no las produce este agente). Se
# declaran acá porque este módulo es el que tiene que saber ignorarlas: no salen de ningún
# escaneo, así que el barrido a `obsoleta` del final las borraría a todas.
TIPO_MANUAL = "revision_manual"

# Prioridad por tipo (1 = mirar primero). `sin_declarar` y `uf_imposible` son los que más
# cambian el resultado del relevamiento; `mas_alto` suele ser una diferencia de criterio.
_PRIORIDAD = {
    "altura_sin_declarar": 1,
    "uf_imposible": 1,
    # Contradicción interna del catastro (obra declarada, 0 unidades): son UF que faltan en el
    # entregable y se detecta sin satélite, así que es de las más confiables.
    "uf_sin_declarar": 1,
    # El geocoding dudoso se revisa ANTES de dibujar la zona: si una dirección del
    # relevamiento anterior está mal ubicada, la zona sale mal y se relevan las parcelas
    # equivocadas. Es el que más temprano hay que atender.
    "geocoding_dudoso": 1,
    "hotel_sin_habitaciones": 2,
    # Un duplicado abierto cuenta DOS VECES el mismo establecimiento en `habitaciones_total` y
    # en el `uf_comercio` de la parcela. Es plata mal contada, pero se resuelve con un click
    # (cerrar el sobrante) y no bloquea nada aguas arriba: misma prioridad que la asistencia.
    "hotel_duplicado": 2,
    # Sin número la parcela no aparea contra el relevamiento anterior ni entra al CSV de
    # operadora, pero se resuelve mirando el frente: importante, no urgente.
    "numero_faltante": 2,
    "altura_mas_alta": 3,
    # El POI declara el número de otra parcela. Va último a propósito: son los casos que la
    # guarda automática NO se anima a mover, y la lectura de los 11 primeros (Malvinas,
    # ago-2026) es que la mayoría está bien donde está. Mueve como mucho 1 UF por caso.
    "poi_numero_ajeno": 3,
}

# Fuentes de `habitaciones` que cuentan como DATO EXACTO del establecimiento. Son las que
# distinguen al hotel vigente del re-registro societario: quien tiene UHs de Cadastur/OSM (o
# un número puesto a mano) es el que opera. `llm` y `bci_proxy` son estimaciones y no sirven
# para desempatar — de hecho el LLM le puso 76 y 30 al MISMO hotel en dos corridas.
_HAB_EXACTA = ("cadastur", "osm", "manual")

# Situações que delatan una sociedad que ya no opera. Conviven en `situacion_cadastur` las de
# Receita (ATIVA/INAPTA/SUSPENSA/BAIXADA/NULA) y las de Cadastur (Em Operação/Inativo), porque
# `HotelFetcher` guarda en esa columna la de la fuente que ganó el merge.
_SITUACION_MUERTA = ("inapta", "suspensa", "baixada", "nula", "inativo", "cancelado")


class IncidenciasInput(BaseModel):
    region_id: str
    survey_id: str
    # Piso de m² por UF declarada bajo el cual se considera físicamente inverosímil.
    m2_por_uf_min: float = 25.0
    # Distancia a la parcela más cercana de su MISMA calle a partir de la cual una dirección
    # del relevamiento anterior se considera mal ubicada (ver `_casos_geocoding`).
    lejos_calle_m: float = 150.0
    # Confianza mínima para dar por bueno un número estimado. Por debajo, la parcela va a
    # `numero_faltante` para que un humano confirme la altura. El corte en 0,4 deja afuera
    # justo las extrapolaciones (más allá del último ancla de la calle), que es donde el
    # estimador mide peor. MISMO valor que usa el export — si se cambia, cambiar los dos.
    numero_conf_min: float = 0.4
    # Superficie construida mínima para reclamar UF a una parcela que declara 0 unidades. Por
    # debajo puede ser un galpón/garaje sin unidad propia. En VG con 30 m² salen 13 casos
    # (mínimo 54). Aplica a las DOS vías de evidencia: el área construida del catastro donde la
    # publican (BCI) y la huella de los footprints donde no (ARBA) — ver `_casos_uf_sin_declarar`.
    area_construida_min: float = 30.0
    # Radio para emparejar dos registros del mismo hotel por cercanía (ver `_casos_duplicados`).
    # 80 m alcanza para el pin de Google contra la dirección fiscal geocodificada de la misma
    # cuadra, sin llegar al hotel vecino: en VG, Express y Diplomata —ambos reales— están a 72 m,
    # pero los dos tienen UHs de Cadastur, así que la asimetría de evidencia igual los excluye.
    dup_dist_m: float = 80.0
    # Los dos umbrales de `poi_numero_ajeno`. Tienen que ser LOS MISMOS que usa la guarda de
    # `OverturePlacesFetcher`: si el reporter fuera más ancho levantaría como incidencia algo
    # que el fetcher ya movió solo, y si fuera más angosto habría casos que no ve nadie.
    poi_numero_max_m: float = 60.0
    poi_salto_min: int = 300


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


def _casos_duplicados(conn, region_id: str, survey_id: str, dup_dist_m: float) -> list[dict]:
    """Hoteles abiertos que son el MISMO establecimiento que otro también abierto.

    En Brasil el dueño cierra una sociedad y abre otra para el mismo hotel sin cambiar el
    cartel ni la dirección, y Google agrega su propio pin sin CNPJ. `HotelFetcher` ya fusiona
    lo que puede, pero lo que queda afuera entra dos veces al relevamiento y **duplica las
    habitaciones** en `habitaciones_total` y en el `uf_comercio` de la parcela.

    La regla es una **asimetría de evidencia**, no la mera cercanía. En los dos casos se exige
    que UNO tenga dato exacto de habitaciones (Cadastur/OSM/manual) —el vigente— y el otro no,
    pero la vía de proximidad pide además una señal dura:

      1. **mismo cartel** (`_nombre_fuerte`, el mismo criterio con que el fetcher fusiona), a
         cualquier distancia — las coordenadas de Cadastur/Receita son la dirección fiscal
         geocodificada y pueden caer lejos del hotel real. Basta con la asimetría: dos registros
         con el mismo cartel donde solo uno tiene UHs es un re-registro societario
         (`HOTEL LAS VELAS LTDA` a 2,1 km de `HOTEL LAS VELAS`, ambos abiertos).
      2. **proximidad** ≤ `dup_dist_m`, y **solo** si el sospechoso además no trae CNPJ (pin
         suelto de Google) o su situação está dada de baja. Sin esa exigencia la vía de
         proximidad se dispara sola en una avenida con hoteles pegados: `FLY HOTEL` y
         `HOTEL TAINA` están a 23 m y son distintos — Fly aparecía como duplicado nada más
         que porque ese día le faltaban las habitaciones.

    Un hotel que solo carece de habitaciones (con CNPJ y situação activa) **no** entra acá: ese
    es el caso de `hotel_sin_habitaciones`, que ya lo reporta.

    Se emite **una incidencia por hotel sospechoso** (no por par), apuntando a su mejor
    candidato: así el operador cierra el sobrante de un click y no ve el mismo caso dos veces.
    El candidato nombrado es el más cercano, que no siempre es "el" original cuando hay tres
    registros del mismo lugar — no cambia la acción, que es cerrar el sospechoso igual.
    """
    rows = conn.execute(text("""
        SELECT hotel_id::text, nombre, cnpj, direccion, habitaciones, habitaciones_fuente,
               COALESCE(business_status, situacion_cadastur) AS estado, fuente,
               ST_Y(location) AS lat, ST_X(location) AS lng, parcela_id::text
        FROM hoteles
        WHERE region_id = :rid
          AND (survey_id = CAST(:sid AS uuid) OR survey_id IS NULL)
          AND NOT cerrado_def AND location IS NOT NULL
        ORDER BY nombre
    """), {"rid": region_id, "sid": survey_id}).fetchall()

    hoteles = [{
        "hotel_id": r[0], "nombre": r[1], "cnpj": (r[2] or "").strip() or None,
        "direccion": r[3], "hab": r[4], "hab_fuente": (r[5] or "").lower(),
        "estado": r[6], "fuente": r[7],
        "lat": float(r[8]), "lng": float(r[9]), "parcela_id": r[10],
    } for r in rows]

    def exacto(h) -> bool:
        return h["hab"] is not None and h["hab_fuente"] in _HAB_EXACTA

    def senal_dura(h) -> bool:
        """No parece un registro vivo: sin CNPJ (pin suelto de Google) o dado de baja."""
        return h["cnpj"] is None or _norm(h["estado"]) in _SITUACION_MUERTA

    def dist_m(a, b) -> float:
        cos_lat = math.cos(math.radians(a["lat"]))
        dx = (b["lng"] - a["lng"]) * 111320.0 * cos_lat
        dy = (b["lat"] - a["lat"]) * 110540.0
        return math.hypot(dx, dy)

    vigentes = [h for h in hoteles if exacto(h)]
    casos = []
    for h in hoteles:
        if exacto(h):
            continue                      # tiene el dato duro: es el vigente, no el sobrante
        # Mejor candidato: primero por cartel (a cualquier distancia), si no el más cercano.
        # Dentro de cada vía gana el más próximo, para no colgarle el caso a un hotel lejano
        # cuando el de al lado explica mejor la duplicación.
        por_nombre = sorted(
            (v for v in vigentes if _nombre_fuerte(h["nombre"], v["nombre"])),
            key=lambda v: dist_m(h, v))
        # La vía de proximidad exige la señal dura: si no, cualquier hotel real al que ese día
        # le falten las habitaciones sale marcado por tener un Cadastur en la misma cuadra.
        por_cerca = sorted(
            (v for v in vigentes if dist_m(h, v) <= dup_dist_m),
            key=lambda v: dist_m(h, v)) if senal_dura(h) else []
        if por_nombre:
            cand, motivo = por_nombre[0], "mismo_nombre"
        elif por_cerca:
            cand, motivo = por_cerca[0], "proximidad"
        else:
            continue

        d = dist_m(h, cand)
        senal = ("Mismo cartel" if motivo == "mismo_nombre"
                 else f"A {d:.0f} m")
        por_que = []
        if h["cnpj"] is None:
            por_que.append("no trae CNPJ (pin de Google)")
        if _norm(h["estado"]) in _SITUACION_MUERTA:
            por_que.append(f"situação {h['estado']}")
        if h["hab"] is None:
            por_que.append("ninguna fuente le da habitaciones")
        elif not exacto(h):
            por_que.append(f"sus {h['hab']} habitaciones son una estimación "
                           f"({h['hab_fuente'] or 's/d'})")

        casos.append({
            "tipo": "hotel_duplicado",
            "clave": f"hotel_duplicado:{h['cnpj'] or _norm(h['nombre'])}",
            "titulo": f"👯 {h['nombre'] or '(sin nombre)'} — ¿duplicado de "
                      f"{cand['nombre'] or '(sin nombre)'}?",
            "detalle": (
                f"{senal} de «{cand['nombre']}», que tiene {cand['hab']} habitaciones de "
                f"{cand['hab_fuente']}. Este registro, en cambio, {'; '.join(por_que)}. "
                "Si es el mismo establecimiento, cerrá ESTE (queda el pin rojo y sus "
                "habitaciones dejan de contarse dos veces); si son hoteles distintos, "
                "descartá el caso."),
            "lat": h["lat"], "lng": h["lng"],
            "parcela_id": h["parcela_id"],
            "hotel_cnpj": h["cnpj"],
            "datos": {
                "hotel_id": h["hotel_id"],      # volátil: la acción de ESTA corrida
                "nombre": h["nombre"], "cnpj": h["cnpj"], "direccion": h["direccion"],
                "situacion": h["estado"], "fuente": h["fuente"],
                "habitaciones": h["hab"], "habitaciones_fuente": h["hab_fuente"] or None,
                "motivo_duplicado": motivo,
                "distancia_m": round(d, 1),
                "candidato_nombre": cand["nombre"], "candidato_cnpj": cand["cnpj"],
                "candidato_direccion": cand["direccion"],
                "candidato_habitaciones": cand["hab"],
                "candidato_habitaciones_fuente": cand["hab_fuente"],
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
                       f"satélite ve un edificio de {altura:.1f} m (~{pisos_sat} planta/s) sobre "
                       f"{huella:.0f} m² de huella.")
        else:
            titulo = f"🏢 {dir_txt} — más alto de lo declarado"
            detalle = (f"El satélite ve {pisos_sat} planta/s ({altura:.1f} m) y el catastro sugiere "
                       f"{pisos_bci} ({area_c:.0f} m² construidos)." if area_c else
                       f"El satélite ve {pisos_sat} planta/s ({altura:.1f} m) vs {pisos_bci} del catastro.")
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
                "plantas_satelital": pisos_sat, "plantas_bci_proxy": pisos_bci,
                "ground_area_m2": round(float(huella), 0) if huella else None,
                "area_m2_construida": round(float(area_c), 1) if area_c else None,
                # Obligatorio mostrarlo: en VG el 89% de la imagen es de 2014, así que el
                # dato NO es "estado actual" y el operador tiene que saberlo.
                "imagery_year": img_year, "imagery_quality": img_q,
            },
        })
    return casos


def _casos_numero_faltante(conn, survey_id: str, conf_min: float) -> list[dict]:
    """Parcelas cuya altura no se puede dar por buena: ni el catastro la trae ni la
    interpolación llegó a una estimación confiable.

    **La parcela NO está en duda.** Viene del catastro con su inscrição, su polígono, su calle
    y su CEP: sabemos exactamente qué inmueble es y dónde está. Lo único que falta es el número
    de puerta, que el municipio dejó en `0` o en blanco (verificado contra los PDFs del BCI, no
    es un fallo del parser). Por eso la tarjeta lo dice explícitamente — si no, el operador cree
    que tiene que verificar la ubicación, que es justo lo que ya está resuelto.

    Entran dos situaciones:
      · **sin estimación** — `NumeroEstimator` se negó: la calle no tiene anclas suficientes o su
        numeración no sigue el orden espacial (coherencia < 0,6 — en VG `CLOVIS HUGNEY` 0,42 ·
        `JOAO LIBANIO` 0,41 · `MAL RONDON` 0,55 · `SÃO BERNARDO` 0,56, con error de ~220 números
        en el p90).
      · **estimación floja** (confianza < `conf_min`) — típicamente extrapolaciones más allá del
        último ancla de la calle. Se muestra el valor inferido como punto de partida, pero no
        entra al entregable hasta que un humano lo confirme.

    Se incluyen los vecinos con número al alcance de la mano: son la referencia con la que el
    operador deduce la altura sin salir de la tarjeta."""
    rows = conn.execute(text("""
        SELECT p.parcela_id::text, p.calle, p.numero, p.cca_code, p.barrio,
               p.centroid_lat, p.centroid_lng, p.uso_principal, p.area_m2_terreno,
               -- vecinos CON número más cercanos de la misma calle (referencia para el humano)
               (SELECT string_agg(v.numero, ' · ' ORDER BY v.d)
                  FROM (SELECT q.numero,
                               ST_Distance(q.geometry::geography, p.geometry::geography) AS d
                        FROM parcelas q
                        WHERE q.survey_id = p.survey_id AND q.parcela_id <> p.parcela_id
                          AND q.calle = p.calle
                          AND q.numero IS NOT NULL AND q.numero <> '' AND q.numero <> '0'
                        ORDER BY d LIMIT 4) v) AS vecinos,
               p.numero_estimado, p.numero_estimado_metodo, p.numero_estimado_confianza,
               p.codigo_postal, p.complemento
        FROM parcelas p
        WHERE p.survey_id = :sid
          AND p.calle IS NOT NULL AND p.calle <> ''
          AND (p.numero IS NULL OR p.numero = '' OR p.numero = '0')
          AND (p.numero_estimado IS NULL
               OR COALESCE(p.numero_estimado_confianza, 0) < :conf)
    """), {"sid": survey_id, "conf": conf_min}).fetchall()

    casos = []
    for r in rows:
        pid, calle, _numero, cca, barrio = r[0], r[1], r[2], r[3], r[4]
        lat, lng, uso, area_t, vecinos = r[5], r[6], r[7], r[8], r[9]
        est, metodo, conf, cep, compl = r[10], r[11], r[12], r[13], r[14]
        # NO es un caso si el inmueble ya está identificado por otra vía. En barrios
        # loteados de Várzea Grande la dirección NO es la altura de calle sino la
        # quadra + lote ("Q68 L20 A"), y el BCI la publica en COMPLEMENTO: pedirle al
        # operador que "cargue la altura" es pedirle un dato que no existe para ese
        # inmueble. Medido en la zona piloto: de 382 casos, 360 tenían quadra/lote y
        # quedaban 22 reales. El corte usa `clasificar_complemento`, la MISMA
        # implementación que arma la dirección del CSV Operadora, para que el panel y el
        # entregable no puedan discrepar sobre qué cuenta como identificación; y es
        # deliberadamente conservadora — las notas registrales ("DESMEMBRADO E CARREGA
        # O COMPLEMENTO...", "AREA DESMEMBRADA") no cuentan y siguen siendo caso.
        unidad, _nombre = clasificar_complemento(compl)
        if unidad:
            continue
        # La ubicación está resuelta; lo que falta es sólo el rótulo. Decirlo evita que el
        # operador salga a verificar algo que ya es dato del municipio.
        ubicacion = (f"Ubicación confirmada por catastro (inscrição {cca}"
                     + (f", CEP {cep}" if cep else "") + "): la parcela y su calle son correctas, "
                     "falta únicamente el número de puerta, que el municipio no declaró. ")
        if est:
            motivo = (f"La interpolación sugiere ≈{est}, pero con confianza baja "
                      f"({conf:.2f}{', extrapolado' if (metodo or '').endswith('_extrap') else ''}), "
                      "así que no se usa en el relevamiento hasta confirmarla. ")
        else:
            motivo = ("La numeración de esta calle no permite interpolarlo con confianza "
                      "(sin anclas suficientes o fuera de orden espacial). ")
        casos.append({
            "tipo": "numero_faltante",
            "clave": f"numero_faltante:{pid}",
            "titulo": f"🔢 {calle} (sin número) — cargar la altura",
            "detalle": (ubicacion + motivo
                        + (f"Vecinos con número: {vecinos}. " if vecinos else "")
                        + "Cargá la altura mirando el frente."),
            "lat": float(lat) if lat is not None else None,
            "lng": float(lng) if lng is not None else None,
            "parcela_id": pid,
            "hotel_cnpj": None,
            "datos": {
                "direccion": f"{calle} (sin número)", "cca_code": cca, "barrio": barrio,
                "uso": uso, "vecinos_con_numero": vecinos,
                "numero_estimado": est, "numero_estimado_metodo": metodo,
                "numero_estimado_confianza": float(conf) if conf is not None else None,
                "ubicacion_confirmada": True,
                "area_m2_terreno": round(float(area_t), 0) if area_t else None,
            },
        })
    return casos


def _casos_poi_numero_ajeno(conn, region_id: str, survey_id: str,
                            max_m: float, salto_min: int) -> list[dict]:
    """POIs parados en una parcela cuyo número NO es el que ellos mismos declaran.

    La detección vive en `OverturePlacesFetcher.candidatos_numero_ajeno` —una sola
    implementación, la lección de `precedencia.py`— y devuelve los casos ya clasificados.
    El fetcher aplica solo los `mover`; acá se levantan los `revisar`, que son los que la
    evidencia no alcanza para resolver sin ojo humano:

      · **esquina** — la calle declarada no es la del catastro. Parece el señalón más fuerte
        y es el más traicionero: el lote de esquina tiene dos frentes, el catastro lo rotula
        por uno y el comercio se publicita por el otro. En Malvinas, 3 de 4 tenían una
        parcela de la calle declarada pegada (≤5 m). Lo más probable es que estén BIEN.
      · **vecino** — misma calle, número contiguo, la otra parcela a pocos metros. No se
        puede saber si el punto está corrido o si el número del catastro está mal.

    Se resuelven mirando el frente por satélite. Mientras estén pendientes **el dato no se
    toca**: el POI sigue donde está y sigue aportando su `uf_comercio`.
    """
    casos = []
    for c in candidatos_numero_ajeno(conn, region_id, survey_id, max_m, salto_min):
        if c["accion"] != "revisar":
            continue
        aqui = _direccion(c["calle_actual"], c["numero_actual"])
        alla = _direccion(c["destino_calle"], c["destino_numero"])
        if c["motivo"] == "esquina":
            porque = (f"El catastro rotula esta parcela sobre {c['calle_actual']}, pero el "
                      f"comercio se publicita sobre {c['calle_declarada']}. Suele ser un "
                      f"lote de esquina con dos frentes —en ese caso está BIEN donde está— "
                      f"y no un POI mal ubicado. ")
        else:
            porque = (f"Misma calle, pero el número que declara salta "
                      f"{c['salto']} respecto del de la parcela, con la {alla} a "
                      f"{c['destino_dist_m']} m. Un salto chico no distingue un punto "
                      f"corrido de un número mal cargado por el catastro. ")
        casos.append({
            "tipo": "poi_numero_ajeno",
            "clave": f"poi_numero_ajeno:{c['place_id']}",
            "titulo": f"🧭 «{c['nombre']}» declara {alla} y está en {aqui}",
            "detalle": (
                f"«{c['nombre']}» ({c['source']}) figura en la parcela {aqui}, pero su ficha "
                f"dice «{c['direccion_declarada']}». A {c['destino_dist_m']} m hay una "
                f"parcela construida que sí lleva ese número: {alla}. " + porque
                + "Mirá el frente por satélite: si el comercio es de la otra parcela, movelo "
                  "con 📌; si está bien acá, descartá el caso. Hasta entonces el dato queda "
                  "como está y su UF se sigue contando en esta parcela."),
            "lat": float(c["lat"]) if c["lat"] is not None else None,
            "lng": float(c["lng"]) if c["lng"] is not None else None,
            "parcela_id": c["parcela_id"],
            "hotel_cnpj": None,
            "datos": {
                "poi": c["nombre"], "source": c["source"], "place_id": c["place_id"],
                "direccion_declarada": c["direccion_declarada"],
                "motivo": c["motivo"], "salto": c["salto"],
                "parcela_actual": aqui, "parcela_candidata": alla,
                "parcela_candidata_id": c["destino_id"],
                "distancia_m": c["destino_dist_m"],
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
                        f"volumen visible es {huella:.0f} m² × {pisos} planta/s ⇒ "
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
                "plantas_satelital": pisos,
                "ground_area_m2": round(float(huella), 0) if huella else None,
                "area_m2_construida": round(float(area_c), 1) if area_c else None,
                "imagery_year": img_year,
            },
        })
    return casos


def _casos_uf_sin_declarar(conn, survey_id: str, area_min: float) -> list[dict]:
    """El catastro declara ÁREA CONSTRUIDA pero NINGUNA unidad. Se contradice solo.

    Es el espejo de `uf_imposible`: allá la UF declarada no cabe en el volumen; acá hay volumen
    construido y la UF es cero. A diferencia de las señales de altura, ésta **no necesita
    satélite** — la contradicción está dentro del propio BCI, así que es barata y no depende de
    la imagen (que en VG es de 2014 en el 89% de los puntos).

    Importa porque son **UF que faltan en el entregable**: una parcela con obra declarada y 0
    unidades sale del relevamiento aportando nada. Caso que lo destapó: `SAO BENTO 156`
    (cca 101192), `uso='vacante'` con **703 m² construidos sobre 519 m² de terreno** — ratio
    1,36, o sea más de una planta.

    `area_min` filtra lo que puede ser un galpón, tapera o garaje sin unidad propia; en VG con
    30 m² quedan 13 casos, el menor de 54 m². Se adjunta la medición satelital cuando existe:
    no hace falta para detectar, pero al operador le sirve para decidir sin salir de la tarjeta.

    **Dos vías de evidencia, porque el área construida no existe en todos lados.** El criterio
    original sólo miraba `area_m2_construida`, que la publica el BCI brasilero; **ARBA no la
    publica** (viene NULL en las 2.059 parcelas de Malvinas Argentinas), así que fuera de Brasil
    el caso no se detectaba nunca aunque estuviera a la vista. La segunda vía usa los
    **footprints de Open Buildings** que ya cargó `FootprintFetcher`: si hay huella suficiente
    dentro de la parcela y CERO unidades, la contradicción es la misma. Medido en Malvinas el
    11-ago-2026: **18 parcelas** con edificios adentro y 0 UF, la peor con 9 edificios y 488 m²
    de huella sobre 864 m² de terreno.

    La pertenencia es estricta —el **centroide** del footprint dentro del polígono de la
    parcela—, no "el edificio más cercano": es la misma guarda que `AlturaFetcher` necesitó
    cuando `findClosest` le hacía medir la casa del vecino en los lotes vacíos, y acá el sesgo
    apuntaría al mismo lado, porque el caso se dispara justo donde no hay nada declarado.
    """
    rows = conn.execute(text("""
        WITH sin_uf AS (
            SELECT p.* FROM parcelas p
            WHERE p.survey_id = :sid
              AND COALESCE(p.uf_vivienda, 0) + COALESCE(p.uf_comercio, 0) = 0
        )
        SELECT p.parcela_id::text, p.calle, p.numero, p.cca_code, p.uso_principal, p.uf_fuente,
               p.area_m2_terreno, p.area_m2_construida, p.centroid_lat, p.centroid_lng,
               p.numero_estimado, p.barrio,
               a.altura_m, a.pisos_satelital, a.ground_area_m2, a.imagery_year,
               f.n_edificios, f.huella_m2
        FROM sin_uf p
        LEFT JOIN parcela_altura a ON a.parcela_id = p.parcela_id
        LEFT JOIN LATERAL (
            -- La huella se RECORTA al lote: un galpón que cubre varias parcelas no es
            -- "901 m² construidos" en cada una. Medido en Malvinas: Illia 4836 (206 m² de
            -- terreno) recibía entero un edificio de 901 m² porque su centroide caía adentro,
            -- y la tarjeta le mostraba al operador un ratio de 4,37 — un número imposible que
            -- le hace desconfiar del caso entero. Se suma sólo el área de intersección, que es
            -- el techo que efectivamente pisa ESTE lote, y por eso los demás lotes que el mismo
            -- galpón cubre también aparecen, cada uno con su parte.
            SELECT COUNT(*) AS n_edificios,
                   COALESCE(SUM(ST_Area(ST_Intersection(p.geometry, fr.footprint)::geography)), 0)
                       AS huella_m2
            FROM footprints_revision fr
            WHERE fr.survey_id = :sid
              AND p.geometry IS NOT NULL
              AND ST_Intersects(p.geometry, fr.footprint)
              -- Solape mínimo: un footprint que apenas roza el borde (error de digitalización
              -- entre lote y lote) no es un edificio de esta parcela.
              AND ST_Area(ST_Intersection(p.geometry, fr.footprint)::geography) >= 10
        ) f ON TRUE
        WHERE COALESCE(p.area_m2_construida, 0) >= :amin
           OR COALESCE(f.huella_m2, 0) >= :amin
    """), {"sid": survey_id, "amin": area_min}).fetchall()

    casos = []
    for r in rows:
        pid, calle, numero, cca, uso, uf_fuente = r[0], r[1], r[2], r[3], r[4], r[5]
        area_t, area_c, lat, lng, num_est, barrio = r[6], r[7], r[8], r[9], r[10], r[11]
        altura, pisos, huella, img_year = r[12], r[13], r[14], r[15]
        n_edif, huella_fp = int(r[16] or 0), float(r[17] or 0)
        dir_txt = _direccion(calle, numero, num_est)
        area_c = float(area_c) if area_c else 0.0
        # Vía de evidencia: el área construida del catastro si la publica, la huella satelital
        # si no. Se nombra en el texto para que el operador sepa qué está mirando — no es lo
        # mismo "el municipio se contradice" que "el municipio calla y el satélite ve un techo".
        por_catastro = area_c >= area_min
        superficie = area_c if por_catastro else huella_fp
        ratio = (superficie / float(area_t)) if area_t else None
        # Un ratio construido/terreno > 1 sólo se explica con más de una planta: es la señal
        # más fuerte de que la parcela tiene unidades sin declarar.
        extra = (f" El construido supera al terreno (ratio {ratio:.2f}), así que hay más de una "
                 f"planta." if ratio and ratio > 1 else "")
        if pisos:
            extra += f" El satélite ve {pisos} planta/s sobre {float(huella or 0):.0f} m² de huella."
        if por_catastro:
            titulo = f"🧮 {dir_txt} — {area_c:.0f} m² construidos y 0 UF"
            detalle = (f"El catastro la da como «{uso or 's/d'}» con **{area_c:.0f} m² "
                       f"construidos** sobre {float(area_t or 0):.0f} m² de terreno, pero no "
                       f"declara ninguna unidad. Se contradice solo, y así la parcela aporta 0 "
                       f"al relevamiento.{extra} Cargá las UF que corresponden.")
        else:
            plural = "s" if n_edif != 1 else ""
            titulo = f"🧮 {dir_txt} — {n_edif} edificio{plural} visible{plural} y 0 UF"
            detalle = (f"El catastro la da como «{uso or 's/d'}» y **no declara ninguna unidad**, "
                       f"pero el satélite ve **{n_edif} edificio{plural}** "
                       f"dentro del lote, con {huella_fp:.0f} m² de huella sobre "
                       f"{float(area_t or 0):.0f} m² de terreno. Acá el catastro no publica la "
                       f"superficie construida (ARBA no la da), así que la contradicción la "
                       f"muestra la imagen.{extra} Verificá en el frente y cargá las UF.")
        casos.append({
            "tipo": "uf_sin_declarar",
            "clave": f"uf_sin_declarar:{pid}",
            "titulo": titulo,
            "detalle": detalle,
            "lat": float(lat) if lat is not None else None,
            "lng": float(lng) if lng is not None else None,
            "parcela_id": pid,
            "hotel_cnpj": None,
            "datos": {
                "direccion": dir_txt, "cca_code": cca, "barrio": barrio,
                "numero_estimado": num_est or None, "uso": uso, "uf_fuente": uf_fuente,
                "uf_vivienda": 0, "uf_comercio": 0,
                "area_m2_terreno": round(float(area_t), 0) if area_t else None,
                "area_m2_construida": round(area_c, 0) if area_c else None,
                "ratio_constr_terreno": round(ratio, 2) if ratio else None,
                # De dónde salió la evidencia y, si fue el satélite, cuánta: sin esto la
                # tarjeta de una parcela argentina mostraría área construida vacía y el
                # operador no sabría por qué se la reclama.
                "evidencia": "catastro" if por_catastro else "footprints",
                "footprints_dentro": n_edif or None,
                "footprints_huella_m2": round(huella_fp, 0) if huella_fp else None,
                "altura_m": round(float(altura), 1) if altura is not None else None,
                "plantas_satelital": pisos,
                "ground_area_m2": round(float(huella), 0) if huella else None,
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
                 + _casos_duplicados(conn, input.region_id, input.survey_id,
                                     input.dup_dist_m)
                 + _casos_altura(conn, input.survey_id)
                 + _casos_uf_imposible(conn, input.survey_id, input.m2_por_uf_min)
                 + _casos_numero_faltante(conn, input.survey_id, input.numero_conf_min)
                 + _casos_uf_sin_declarar(conn, input.survey_id, input.area_construida_min)
                 + _casos_geocoding(conn, input.survey_id, input.region_id,
                                    input.lejos_calle_m)
                 + _casos_poi_numero_ajeno(conn, input.region_id, input.survey_id,
                                           input.poi_numero_max_m, input.poi_salto_min))

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
        # EXCEPTO las que abrió el operador a mano desde el mapa (`revision_manual`): no salen
        # de ningún escaneo, así que este UPDATE las mataría a todas en la primera corrida.
        claves = [f"{c['tipo']}|{c['clave']}" for c in casos]
        r = conn.execute(text("""
            UPDATE incidencias SET estado = 'obsoleta', actualizada_at = now()
            WHERE survey_id = CAST(:sid AS uuid) AND estado = 'pendiente'
              AND tipo <> :tipo_manual
              AND (tipo || '|' || clave) <> ALL(:claves)
        """), {"sid": input.survey_id, "claves": claves or [""],
               "tipo_manual": TIPO_MANUAL})
        out.obsoletas = r.rowcount or 0

        out.pendientes = conn.execute(text(
            "SELECT count(*) FROM incidencias WHERE survey_id = CAST(:sid AS uuid) "
            "AND estado = 'pendiente'"), {"sid": input.survey_id}).scalar() or 0

    out.por_tipo = por_tipo
    logger.info(f"IncidenciasReporter {input.region_id}: creadas={out.creadas} "
                f"actualizadas={out.actualizadas} obsoletas={out.obsoletas} "
                f"pendientes={out.pendientes} por_tipo={por_tipo}")
    return out
