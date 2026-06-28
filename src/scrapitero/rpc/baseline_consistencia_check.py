import json, sys
from scrapitero.agents.baseline_consistencia_check import ConsistenciaInput, run

data = json.load(sys.stdin)
result = run(ConsistenciaInput(**data))
print(result.model_dump_json(indent=2))
