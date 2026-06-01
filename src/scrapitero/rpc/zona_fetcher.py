import json, sys
from scrapitero.agents.zona_fetcher import ZonaInput, run

data = json.load(sys.stdin)
result = run(ZonaInput(**data))
print(result.model_dump_json(indent=2))
