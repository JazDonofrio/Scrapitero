"""RPC wrapper para DXFExport.

Uso:
    PYTHONPATH=src python -m scrapitero.rpc.dxf_export <<< \\
      '{"survey_id":"<UUID>","max_parcelas":50,"output_path":"/tmp/muestra.dxf"}'
"""

import json
import sys

from scrapitero.agents.dxf_export import DXFInput, run


def main() -> None:
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"JSON inválido: {e}"}))
        sys.exit(1)

    input_model = DXFInput(**data)
    output = run(input_model)
    print(output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
