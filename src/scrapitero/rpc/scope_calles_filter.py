import json, sys
from scrapitero.agents.scope_calles_filter import ScopeCallesInput, run

data = json.load(sys.stdin)
result = run(ScopeCallesInput(**data))
print(result.model_dump_json(indent=2))
