"""RPC wrapper para ComparativaReporter (survey actual vs relevamiento anterior).

Contra otro survey de la misma región (match por cca_code + dirección):
    python3 -m scrapitero.rpc.comparativa_reporter <<< '{"survey_id":"<UUID>","contra_survey_id":"<UUID>"}'

Contra un baseline importado (CSV externo del cliente, match por dirección):
    python3 -m scrapitero.rpc.comparativa_reporter <<< '{"survey_id":"<UUID>","contra_baseline_id":"<UUID>"}'
"""

import json
import sys

from scrapitero.agents.comparativa_reporter import ComparativaInput, run


def main() -> None:
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"JSON inválido: {e}"}))
        sys.exit(1)

    output = run(ComparativaInput(**data))
    print(output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
