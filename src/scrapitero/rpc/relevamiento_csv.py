import json, sys
from scrapitero.agents.relevamiento_csv import CSVInput, run

data = json.load(sys.stdin)
result = run(CSVInput(**data))
print(result.model_dump_json(indent=2))
