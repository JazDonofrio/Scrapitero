import json, sys
from scrapitero.agents.direccion_ar import DireccionARInput, run

data = json.load(sys.stdin)
print(run(DireccionARInput(**data)).model_dump_json(indent=2))
