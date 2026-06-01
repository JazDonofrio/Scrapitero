import json, sys
from scrapitero.agents.onr_lotes_fetcher import LotesInput, run

data = json.load(sys.stdin)
result = run(LotesInput(**data))
print(result.model_dump_json(indent=2))
