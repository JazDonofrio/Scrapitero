import json, sys
from scrapitero.agents.relevamiento_pdf import PDFInput, run

data = json.load(sys.stdin)
result = run(PDFInput(**data))
print(result.model_dump_json(indent=2))
