"""RPC wrapper para HotelReglas.

Uso:
    PYTHONPATH=src python -m scrapitero.rpc.hotel_reglas <<< \\
      '{"region_id":"zona-varzea-grande-vaz049","survey_id":"<UUID>"}'

En seco (no escribe nada, sólo devuelve qué haría):
    PYTHONPATH=src python -m scrapitero.rpc.hotel_reglas <<< \\
      '{"region_id":"...","survey_id":"<UUID>","dry_run":true}'
"""

import json
import sys

from scrapitero.agents.hotel_reglas import HotelReglasInput, run


def main() -> None:
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"JSON inválido: {e}"}))
        sys.exit(1)

    output = run(HotelReglasInput(**data))
    print(output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
