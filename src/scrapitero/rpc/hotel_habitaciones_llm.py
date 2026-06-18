import json, sys
from scrapitero.agents.hotel_habitaciones_llm import HotelHabLLMInput, run

data = json.load(sys.stdin)
result = run(HotelHabLLMInput(**data))
print(result.model_dump_json(indent=2))
