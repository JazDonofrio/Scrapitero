"""VGPipelineRunner — happy path de Várzea Grande compilado en un solo agente.

Ejecuta en orden el pipeline estándar VG (CLAUDE.md):
    SmartGISFetcher → VGBCIFetcher (parseo inline) → BCIParser → EstablecimientoAgrupador
    → HotelFetcher (hoteles del relevamiento; paso OPCIONAL, no tumba el pipeline si falla)
El relevamiento no se marca `completed` hasta terminar todos los pasos (incluidos hoteles).

Reemplaza la orquestación paso-a-paso del LLM por una sola invocación determinista:
el orquestador (Hermes) llama UNA vez y el runner registra cada paso en
`surveys.notes` (mismo formato que `survey-step-update`), honra el stop del operador
y relaya los errores sellados de cada sub-agente tal cual.

Presupuesto de tiempo: el comando que invoca al runner (Hermes) lo mata a ~900s, así
que el runner reparte su `max_runtime_s` entre los pasos y, si no llega al final,
devuelve `parcial=true` + `siguiente` — re-invocarlo con el MISMO input continúa donde
quedó (todos los sub-agentes son incrementales/idempotentes). Invocado sin límite
externo (p. ej. desde la web en un thread), con `max_runtime_s=0` corre hasta terminar.
"""

from __future__ import annotations

import time
from typing import Optional

from loguru import logger
from pydantic import BaseModel

from scrapitero.agents import (
    bci_parser,
    country_fetcher,
    establecimiento_agrupador,
    hotel_fetcher,
    hotel_habitaciones_llm,
    parcela_categoria,
    scope_calles_filter,
    shopping_fetcher,
    smartgis_fetcher,
    varzea_bci_fetcher,
)
from scrapitero.agents._run import agent_run
from scrapitero.rpc.survey_step_update import SurveyStepUpdateInput, run as _step_update

# Margen reservado para registrar el último paso y salir limpio antes del kill externo.
_RESERVA_S = 30
# Presupuesto mínimo con el que vale la pena arrancar un sub-paso.
_MIN_PASO_S = 60


class VGRunnerInput(BaseModel):
    region_id: str
    survey_id: str
    # Presupuesto TOTAL del runner. 0 = sin límite (solo si nadie nos mata desde afuera).
    max_runtime_s: int = 840
    # Tope de re-runs internos de un mismo paso que devuelve parcial (anti-loop).
    max_pasadas: int = 12
    # Al completar todos los pasos, marcar surveys.status='completed'.
    marcar_completado: bool = True


class VGRunnerOutput(BaseModel):
    ok: bool
    pasos_ejecutados: list[str] = []
    # paso → output (dict) de la última corrida de ese sub-agente.
    resumen: dict = {}
    # parcial=True ⇒ se agotó el presupuesto; re-invocar con el mismo input continúa
    # desde `siguiente` (NO es un error).
    parcial: bool = False
    siguiente: Optional[str] = None
    # True si el operador detuvo el survey (stop externo) — no continuar.
    detenido: bool = False
    error: Optional[str] = None


@agent_run
def run(input: VGRunnerInput) -> VGRunnerOutput:
    deadline = (time.monotonic() + input.max_runtime_s) if input.max_runtime_s > 0 else None
    out = VGRunnerOutput(ok=True)

    def restante() -> Optional[int]:
        if deadline is None:
            return None
        return int(deadline - time.monotonic())

    def registrar(paso: str, resultado: Optional[dict], status: Optional[str] = None) -> bool:
        """Registra el paso en surveys.notes. True ⇒ stop externo: hay que frenar."""
        r = _step_update(SurveyStepUpdateInput(
            survey_id=input.survey_id, paso=paso, resultado=resultado, status=status,
        ))
        return r.should_stop

    def correr_paso(paso: str, fn, make_input, progreso=None) -> Optional[str]:
        """Corre un sub-agente, reintentando mientras devuelva parcial=True.

        `make_input(presupuesto_s)` arma el input del sub-agente (presupuesto_s puede
        ser None = sin límite). Devuelve None si completó; 'parcial' | 'stop' | 'error'.

        `progreso(d)` (opcional): True si la pasada hizo trabajo NUEVO. Si una pasada
        vuelve `parcial` pero SIN progreso nuevo, el paso se da por completo y se avanza
        (evita loops infinitos: p.ej. SmartGIS que nunca termina de recorrer el grid en
        el presupuesto pero ya encontró todas las parcelas alcanzables de la zona).
        """
        for _ in range(input.max_pasadas):
            rest = restante()
            presupuesto = None if rest is None else rest - _RESERVA_S
            if presupuesto is not None and presupuesto < _MIN_PASO_S:
                return "parcial"

            logger.info(f"VGRunner: ▶ {paso} (presupuesto={presupuesto or 'sin límite'}s)")
            r = fn(make_input(presupuesto))
            d = r.model_dump()
            out.resumen[paso] = d
            stop = registrar(paso, d)

            if not r.ok:
                # El error ya viene sellado por el agent_run del sub-agente: relayar tal cual.
                out.error = r.error
                return "error"
            if stop:
                return "stop"
            if not getattr(r, "parcial", False):
                out.pasos_ejecutados.append(paso)
                return None
            # parcial: si la pasada no aportó nada nuevo, ya convergió → avanzar igual.
            if progreso is not None and not progreso(d):
                logger.info(f"VGRunner: {paso} parcial pero sin progreso nuevo — "
                            f"se da por completo y se avanza al siguiente paso")
                out.pasos_ejecutados.append(paso)
                return None
            logger.info(f"VGRunner: {paso} devolvió parcial (con progreso) — re-ejecutando")
        return "parcial"

    # Cada paso: (nombre, fn, make_input, progreso?). `progreso(d)` distingue una
    # pasada parcial QUE AVANZA de una estancada. Para SmartGIS, "avanza" = insertó
    # parcelas nuevas; si una pasada parcial no inserta ninguna, ya recorrió todo lo
    # alcanzable de la zona → se da por completo (no re-ejecutar al infinito).
    # Tupla: (nombre, fn, make_input, progreso?, opcional?). `opcional=True` ⇒ si el paso
    # falla NO aborta el relevamiento (se registra el error y se sigue): los hoteles son
    # enriquecimiento, no deben tumbar el pipeline. Hoteles usa fuentes GRATIS (sin Google
    # pago) — para el descubrimiento con Google está el botón 🏨 de la web con su tilde.
    pasos = [
        ("smartgis_fetcher", smartgis_fetcher.run,
         lambda s: smartgis_fetcher.SmartGISInput(
             region_id=input.region_id, survey_id=input.survey_id,
             **({"max_runtime_s": s} if s is not None else {})),
         lambda d: (d.get("parcelas_insertadas") or 0) > 0, False),
        ("varzea_bci_fetcher", varzea_bci_fetcher.run,
         lambda s: varzea_bci_fetcher.BCIInput(
             region_id=input.region_id, survey_id=input.survey_id,
             **({"max_runtime_s": s} if s is not None else {"max_runtime_s": 0})),
         None, False),
        ("bci_parser", bci_parser.run,
         lambda s: bci_parser.BCIParserInput(
             region_id=input.region_id, survey_id=input.survey_id),
         None, False),
        # Modo "actualización por calle+rango": ya con las direcciones (BCI), acotar el survey
        # a las calles/rangos del scope (no-op si el survey no tiene scope_calles). Best-effort.
        ("scope_calles_filter", scope_calles_filter.run,
         lambda s: scope_calles_filter.ScopeCallesInput(survey_id=input.survey_id),
         None, True),
        ("establecimiento_agrupador", establecimiento_agrupador.run,
         lambda s: establecimiento_agrupador.AgrupadorInput(
             region_id=input.region_id, survey_id=input.survey_id),
         None, False),
        # Shoppings (OSM gratis; Google es pago → se corre aparte) + sellar categorías CNPJ
        # sobre las parcelas (tipo de edificación). Opcionales/best-effort.
        ("shopping_fetcher", shopping_fetcher.run,
         lambda s: shopping_fetcher.ShoppingFetcherInput(
             region_id=input.region_id, survey_id=input.survey_id, fuentes=["osm"],
             zona_buffer_m=150.0),   # captar shoppings retirados de la calle en modo corredor
         None, True),
        ("parcela_categoria", parcela_categoria.run,
         lambda s: parcela_categoria.ParcelaCategoriaInput(region_id=input.region_id),
         None, True),
        # Barrios cerrados / condomínios desde OSM → marca parcelas.es_country (capa 🏘 Country).
        ("country_fetcher", country_fetcher.run,
         lambda s: country_fetcher.CountryFetcherInput(
             region_id=input.region_id, survey_id=input.survey_id),
         None, True),
        ("hotel_fetcher", hotel_fetcher.run,
         lambda s: hotel_fetcher.HotelFetcherInput(
             region_id=input.region_id, survey_id=input.survey_id,
             fuentes=["cadastur", "receita", "osm"], set_uf=True),
         None, True),
        # Completa con IA (Gemini + web) las habitaciones que ninguna fuente trajo.
        # Opcional/best-effort; pago por hotel (tope max_hoteles).
        ("hotel_habitaciones_llm", hotel_habitaciones_llm.run,
         lambda s: hotel_habitaciones_llm.HotelHabLLMInput(
             region_id=input.region_id, survey_id=input.survey_id),
         None, True),
    ]

    for paso, fn, make_input, progreso, opcional in pasos:
        res = correr_paso(paso, fn, make_input, progreso)
        if res is None:
            continue
        if res == "parcial":
            out.parcial = True
            out.siguiente = paso
            logger.info(f"VGRunner: presupuesto agotado — continuar desde '{paso}' re-invocando")
            return out
        if res == "stop":
            out.detenido = True
            logger.info("VGRunner: stop externo del operador — frenando")
            return out
        # error en un paso opcional: registrar y seguir (no tumba el relevamiento)
        if res == "error" and opcional:
            logger.warning(f"VGRunner: paso opcional '{paso}' falló ({out.error}) — se continúa")
            out.error = None
            continue
        # error fatal: abortar relayando la causa sellada del sub-agente
        out.ok = False
        return out

    if input.marcar_completado:
        registrar("completado", {"runner": "vg_pipeline_runner",
                                 "pasos": out.pasos_ejecutados}, status="completed")
    logger.info(f"VGRunner: pipeline VG completo — {out.pasos_ejecutados}")
    return out
