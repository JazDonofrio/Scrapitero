import json, sys
from scrapitero.agents.bci_parser import BCIParserInput, run

data = json.load(sys.stdin)
print(run(BCIParserInput(**data)).model_dump_json(indent=2))
