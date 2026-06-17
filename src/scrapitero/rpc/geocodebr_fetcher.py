import json, sys
from scrapitero.agents.geocodebr_fetcher import GeocodebrInput, run

data = json.load(sys.stdin)
result = run(GeocodebrInput(**data))
print(result.model_dump_json(indent=2))
