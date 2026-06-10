import json, sys
from scrapitero.agents.establecimiento_agrupador import AgrupadorInput, run

data = json.load(sys.stdin)
print(run(AgrupadorInput(**data)).model_dump_json(indent=2))
