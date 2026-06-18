import json, sys
from scrapitero.agents.shopping_fetcher import ShoppingFetcherInput, run

data = json.load(sys.stdin)
result = run(ShoppingFetcherInput(**data))
print(result.model_dump_json(indent=2))
