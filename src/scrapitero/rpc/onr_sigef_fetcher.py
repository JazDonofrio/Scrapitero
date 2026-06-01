import json, sys
from scrapitero.agents.onr_sigef_fetcher import SigefInput, run

data = json.load(sys.stdin)
result = run(SigefInput(**data))
print(result.model_dump_json(indent=2))
