import json, sys
from scrapitero.agents.receita_cnpj_fetcher import ReceitaCNPJInput, run

data = json.load(sys.stdin)
result = run(ReceitaCNPJInput(**data))
print(result.model_dump_json(indent=2))
