import json, sys
from scrapitero.agents.parcela_categoria import ParcelaCategoriaInput, run

data = json.load(sys.stdin)
result = run(ParcelaCategoriaInput(**data))
print(result.model_dump_json(indent=2))
