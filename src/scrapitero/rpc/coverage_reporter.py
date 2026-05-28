"""RPC wrapper para CoverageReporter.

Hermes lo llama así:
    echo '{"region_id":"vg-mt-br"}' | python -m scrapitero.rpc.coverage_reporter
"""

import json
import sys

from dotenv import load_dotenv
load_dotenv()

from scrapitero.agents.coverage_reporter import CoverageInput, run

if __name__ == "__main__":
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw)
        result = run(CoverageInput(**data))
        print(result.model_dump_json(indent=2))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)
