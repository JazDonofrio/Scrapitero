import json, sys
from scrapitero.agents.baseline_geocoder import BaselineGeocoderInput, run

data = json.load(sys.stdin)
result = run(BaselineGeocoderInput(**data))
print(result.model_dump_json(indent=2))
