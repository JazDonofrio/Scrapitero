import json, sys
from scrapitero.agents.relevamiento_reporter import ReporterInput, run

data = json.load(sys.stdin)
result = run(ReporterInput(**data))
print(result.model_dump_json(indent=2))
