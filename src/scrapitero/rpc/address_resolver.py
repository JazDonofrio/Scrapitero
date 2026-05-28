"""RPC wrapper para AddressResolver.

Uso:
    echo '{"region_id":"vg-mt-br","survey_id":"<UUID>"}' | \\
      env $(cat /opt/scrapitero/.env | xargs) \\
      PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \\
      python3 -m scrapitero.rpc.address_resolver
"""

import json
import sys

from scrapitero.agents.address_resolver import AddressResolverInput, run


def main() -> None:
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"JSON inválido: {e}"}))
        sys.exit(1)

    input_model = AddressResolverInput(**data)
    output = run(input_model)
    print(output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
