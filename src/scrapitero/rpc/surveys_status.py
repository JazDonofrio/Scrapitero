"""RPC para listar el estado de todos los surveys activos.

Hermes lo llama así:
    echo '{}' | python3 -m scrapitero.rpc.surveys_status
    echo '{"solo_activos": false}' | python3 -m scrapitero.rpc.surveys_status
"""

import json
import sys
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from pydantic import BaseModel
from sqlalchemy import text

from scrapitero.db.engine import get_engine


class SurveysStatusInput(BaseModel):
    solo_activos: bool = True  # si False, incluye completed/failed también


class SurveyResumen(BaseModel):
    survey_id: str
    nombre: str           # nombre visible del relevamiento (= regions.name)
    region_id: str
    status: str
    paso_actual: Optional[str]  # ej: "smartgis", "bci", "parser", "completado"
    started_at: str
    duracion_minutos: Optional[int]
    parcelas: int
    edificios: int
    steps: int


class SurveysStatusOutput(BaseModel):
    total: int
    surveys: list[SurveyResumen]


def run(inp: SurveysStatusInput) -> SurveysStatusOutput:
    engine = get_engine()

    with engine.connect() as conn:
        where = "WHERE s.status = 'running'" if inp.solo_activos else ""
        rows = conn.execute(text(f"""
            SELECT
                s.survey_id::text,
                s.region_id,
                r.name AS region_nombre,
                s.status,
                s.started_at,
                s.finished_at,
                s.steps_count,
                s.notes
            FROM surveys s
            JOIN regions r ON r.region_id = s.region_id
            {where}
            ORDER BY s.started_at DESC
            LIMIT 20
        """)).fetchall()

        surveys = []
        for row in rows:
            survey_id = row[0]

            parcelas = conn.execute(text(
                "SELECT COUNT(*) FROM parcelas WHERE survey_id = :sid"
            ), {"sid": survey_id}).scalar() or 0

            edificios = conn.execute(text(
                "SELECT COUNT(*) FROM edificios WHERE survey_id = :sid"
            ), {"sid": survey_id}).scalar() or 0

            started_at: datetime = row[4]
            finished_at: Optional[datetime] = row[5]
            now = datetime.now(timezone.utc)
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            end = finished_at.replace(tzinfo=timezone.utc) if finished_at else now
            duracion = int((end - started_at).total_seconds() / 60)

            # Parsear notes para extraer paso actual
            notes_raw = row[7]
            paso_actual = None
            if notes_raw:
                try:
                    notes_json = json.loads(notes_raw)
                    paso_actual = notes_json.get("paso_actual")
                except (json.JSONDecodeError, AttributeError):
                    pass

            surveys.append(SurveyResumen(
                survey_id=survey_id,
                nombre=row[2],  # nombre visible del relevamiento
                region_id=row[1],
                status=row[3],
                paso_actual=paso_actual,
                started_at=started_at.strftime("%Y-%m-%d %H:%M UTC"),
                duracion_minutos=duracion,
                parcelas=int(parcelas),
                edificios=int(edificios),
                steps=row[6] or 0,
            ))

    return SurveysStatusOutput(total=len(surveys), surveys=surveys)


if __name__ == "__main__":
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
        result = run(SurveysStatusInput(**data))
        print(result.model_dump_json(indent=2))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)
