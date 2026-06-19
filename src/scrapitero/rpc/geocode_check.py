import json, sys
from scrapitero.agents.geocode_check import GeocodeCheckInput, run

data = json.load(sys.stdin)
result = run(GeocodeCheckInput(**data))
print(result.model_dump_json(indent=2))
