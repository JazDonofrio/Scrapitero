import json, sys
from scrapitero.agents.baseline_interp import BaselineInterpInput, run

data = json.load(sys.stdin)
result = run(BaselineInterpInput(**data))
print(result.model_dump_json(indent=2))
