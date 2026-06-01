import json, sys
from scrapitero.agents.onr_carto_identify import CartoInput, run

data = json.load(sys.stdin)
print(run(CartoInput(**data)).model_dump_json(indent=2))
