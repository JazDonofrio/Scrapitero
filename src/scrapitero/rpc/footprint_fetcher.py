"""RPC wrapper para FootprintFetcher.

Uso:
    PYTHONPATH=src python -m scrapitero.rpc.footprint_fetcher <<< \\
      '{"region_id":"zona-varzea-grande-update","survey_id":"<UUID>"}'
"""

import json
import sys

from scrapitero.agents.footprint_fetcher import FootprintInput, run


def main() -> None:
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"JSON inválido: {e}"}))
        sys.exit(1)

    input_model = FootprintInput(**data)
    output = run(input_model)
    print(output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
