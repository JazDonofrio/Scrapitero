"""RPC wrapper para ARBACadastralFetcher.

Uso:
    echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<UUID>","partido_id":"136","circunscripcion":"2","seccion":"C","manzana":"184"}' | \\
      env $(cat /opt/scrapitero/.env | xargs) \\
      PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \\
      python3 -m scrapitero.rpc.arba_cadastral_fetcher
"""

import json
import sys

from scrapitero.agents.arba_cadastral_fetcher import ARBAInput, run


def main() -> None:
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"JSON inválido: {e}"}))
        sys.exit(1)

    input_model = ARBAInput(**data)
    output = run(input_model)
    print(output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
