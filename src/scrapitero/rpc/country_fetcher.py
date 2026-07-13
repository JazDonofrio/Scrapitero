import json, sys
from scrapitero.agents.country_fetcher import CountryFetcherInput, run

data = json.load(sys.stdin)
result = run(CountryFetcherInput(**data))
print(result.model_dump_json(indent=2))
