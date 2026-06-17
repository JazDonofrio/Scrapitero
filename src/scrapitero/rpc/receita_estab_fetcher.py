import json, sys
from scrapitero.agents.receita_estab_fetcher import ReceitaEstabInput, run

data = json.load(sys.stdin)
result = run(ReceitaEstabInput(**data))
print(result.model_dump_json(indent=2))
