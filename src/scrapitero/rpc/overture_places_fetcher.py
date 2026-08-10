"""RPC wrapper para OverturePlacesFetcher.

Uso:
    PYTHONPATH=src python -m scrapitero.rpc.overture_places_fetcher <<< \\
      '{"region_id":"zona-hurlingham","survey_id":"<UUID>"}'

Sin costo por request (S3 público de Overture). Para fijar otra versión del dataset:
    ... <<< '{"region_id":"...","survey_id":"...","release":"2026-08-19.0"}'
"""

import json
import sys

from scrapitero.agents.overture_places_fetcher import OvertureInput, run


def main() -> None:
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"JSON inválido: {e}"}))
        sys.exit(1)

    input_model = OvertureInput(**data)
    output = run(input_model)
    print(output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
