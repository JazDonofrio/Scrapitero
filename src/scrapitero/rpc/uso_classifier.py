import json, sys
from scrapitero.agents.uso_classifier import ClassifierInput, run

data = json.load(sys.stdin)
result = run(ClassifierInput(**data))
print(result.model_dump_json(indent=2))
