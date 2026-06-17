import json, sys
from scrapitero.agents.hotel_fetcher import HotelFetcherInput, run

data = json.load(sys.stdin)
result = run(HotelFetcherInput(**data))
print(result.model_dump_json(indent=2))
