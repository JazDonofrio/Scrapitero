"""RPC para que el LLM registre el progreso de un relevamiento en la DB.

Hermes lo llama así:
    echo '{"survey_id":"<UUID>","paso":"smartgis","resultado":{...}}' | \
      python3 -m scrapitero.rpc.survey_step_update

    echo '{"survey_id":"<UUID>","paso":"completado","status":"completed"}' | \
      python3 -m scrapitero.rpc.survey_step_update
"""

import json
import sys
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.db.engine import get_engine


class SurveyStepUpdateInput(BaseModel):
    survey_id: str
    paso: str                        # "smartgis" | "bci" | "parser" | "completado" | "error" | "detenido"
    resultado: Optional[dict] = None # output del agente para este paso
    status: Optional[str] = None     # si se provee, actualiza surveys.status ("completed"|"failed"|"stopped")


class SurveyStepUpdateOutput(BaseModel):
    ok: bool
    survey_id: str
    paso: str
    survey_status: str   # estado actual del survey en la DB
    should_stop: bool    # True si el survey fue marcado "stopped" externamente


def run(inp: SurveyStepUpdateInput) -> SurveyStepUpdateOutput:
    engine = get_engine()

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT notes, status FROM surveys WHERE survey_id = :sid"),
            {"sid": inp.survey_id},
        ).fetchone()

    if not row:
        return SurveyStepUpdateOutput(
            ok=False, survey_id=inp.survey_id, paso=inp.paso,
            survey_status="not_found", should_stop=True,
        )

    notes_raw, current_status = row[0], row[1]

    # Si fue detenido externamente, no pisamos el estado
    if current_status == "stopped":
        return SurveyStepUpdateOutput(
            ok=True, survey_id=inp.survey_id, paso=inp.paso,
            survey_status="stopped", should_stop=True,
        )

    # Actualizar notes
    notas: dict = {}
    if notes_raw:
        try:
            notas = json.loads(notes_raw)
        except Exception:
            pass

    notas["paso_actual"] = inp.paso
    if inp.resultado is not None:
        notas.setdefault("pasos", {})[inp.paso] = inp.resultado

    new_status = inp.status or current_status

    is_final = new_status in ("completed", "failed", "stopped")
    sql = """
        UPDATE surveys
        SET notes = :n, status = :s
        {finished}
        WHERE survey_id = :sid
    """.format(finished=", finished_at = NOW()" if is_final else "")

    with engine.begin() as conn:
        conn.execute(text(sql), {"n": json.dumps(notas), "s": new_status, "sid": inp.survey_id})

    return SurveyStepUpdateOutput(
        ok=True,
        survey_id=inp.survey_id,
        paso=inp.paso,
        survey_status=new_status,
        should_stop=new_status in ("stopped", "failed"),
    )


if __name__ == "__main__":
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
        result = run(SurveyStepUpdateInput(**data))
        print(result.model_dump_json(indent=2))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)
