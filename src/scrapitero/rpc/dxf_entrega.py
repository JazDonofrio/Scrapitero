"""RPC wrapper para DXFEntrega (DXF con el estándar de planos del cliente).

Uso:
    PYTHONPATH=src python -m scrapitero.rpc.dxf_entrega <<< \\
      '{"survey_id":"<UUID>","celula_id":"VAZ049","output_path":"/tmp/entrega.dxf"}'
"""

import json
import sys

from scrapitero.agents.dxf_entrega import EntregaInput, run


def main() -> None:
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"JSON inválido: {e}"}))
        sys.exit(1)

    output = run(EntregaInput(**data))
    print(output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
