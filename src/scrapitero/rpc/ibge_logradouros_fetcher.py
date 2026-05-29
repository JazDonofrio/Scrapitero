"""RPC wrapper para IBGELogradourosFetcher.

Uso:
    echo '{"region_id":"vg-mt-br","municipio_codigo":"5108402","estado_uf":"mt"}' | \\
      env $(cat /opt/scrapitero/.env | xargs) \\
      PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \\
      python3 -m scrapitero.rpc.ibge_logradouros_fetcher
"""

import json
import sys

from scrapitero.agents.ibge_logradouros_fetcher import LogradourosInput, run


def main() -> None:
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"JSON inválido: {e}"}))
        sys.exit(1)

    output = run(LogradourosInput(**data))
    print(output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
