import json, sys
from scrapitero.agents.varzea_bci_fetcher import BCIInput, run

data = json.load(sys.stdin)
print(run(BCIInput(**data)).model_dump_json(indent=2))
