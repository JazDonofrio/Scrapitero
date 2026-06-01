import json, sys
from scrapitero.agents.geojson_zone_fetcher import GeoJSONZoneInput, run

data = json.load(sys.stdin)
print(run(GeoJSONZoneInput(**data)).model_dump_json(indent=2))
