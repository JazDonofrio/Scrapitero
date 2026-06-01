import json, sys
from scrapitero.agents.smartgis_fetcher import SmartGISInput, run

data = json.load(sys.stdin)
print(run(SmartGISInput(**data)).model_dump_json(indent=2))
