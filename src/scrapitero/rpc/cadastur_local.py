import json, sys
from scrapitero.agents.cadastur_local_fetcher import CadasturLocalInput, run

data = json.load(sys.stdin)
result = run(CadasturLocalInput(**data))
print(result.model_dump_json(indent=2))
