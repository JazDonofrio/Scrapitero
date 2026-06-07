"""RPC wrapper para DasymetricPopulation (habitantes por manzana, dasimétrico).

Uso:
    python3 -m scrapitero.rpc.dasymetric_population <<< '{"region_id":"zona-varzea-sector-sup"}'

Con survey explícito:
    python3 -m scrapitero.rpc.dasymetric_population <<< '{"region_id":"...","survey_id":"<UUID>"}'
"""

import json
import sys

from scrapitero.agents.dasymetric_population import DasymetricInput, run


def main() -> None:
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"JSON inválido: {e}"}))
        sys.exit(1)

    output = run(DasymetricInput(**data))
    print(output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
