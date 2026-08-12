import json, sys
from scrapitero.agents.osm_poi_fetcher import OsmPoiInput, run

data = json.load(sys.stdin)
result = run(OsmPoiInput(**data))
print(result.model_dump_json(indent=2))
