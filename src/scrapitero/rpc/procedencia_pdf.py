import json, sys
from scrapitero.agents.procedencia_pdf import ProcedenciaInput, run

data = json.load(sys.stdin)
print(run(ProcedenciaInput(**data)).model_dump_json(indent=2))
