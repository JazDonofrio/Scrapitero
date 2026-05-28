"""RPC wrapper para IBGECensusFetcher.

Hermes lo llama así:
    echo '{"region_id":"vg-mt-br","municipio_codigo":"5108402","estado_uf":"mt"}' \
    | python -m scrapitero.rpc.ibge_census_fetcher

Devuelve JSON por stdout.
"""

import json
import sys

from dotenv import load_dotenv
load_dotenv()

from scrapitero.agents.ibge_census_fetcher import IBGEInput, run

if __name__ == "__main__":
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw)
        input_obj = IBGEInput(**data)
        result = run(input_obj)
        print(result.model_dump_json(indent=2))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)
