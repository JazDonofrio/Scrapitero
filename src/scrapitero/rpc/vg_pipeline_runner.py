import json, sys
from scrapitero.agents.vg_pipeline_runner import VGRunnerInput, run

data = json.load(sys.stdin)
result = run(VGRunnerInput(**data))
print(result.model_dump_json(indent=2))
