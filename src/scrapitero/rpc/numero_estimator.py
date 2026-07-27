import json, sys
from scrapitero.agents.numero_estimator import NumeroEstimatorInput, run

data = json.load(sys.stdin)
result = run(NumeroEstimatorInput(**data))
print(result.model_dump_json(indent=2))
