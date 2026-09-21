"""RPC wrapper para EstablecimientosObservados (nombre del negocio que está hoy en cada lote).

Uso:
    PYTHONPATH=src python -m scrapitero.rpc.establecimientos_observados <<< \\
      '{"region_id":"zona-varzea-grande-vaz049","survey_id":"<UUID>"}'

Sin costo: el CNEFE del censo 2022 sale del FTP del IBGE y Overture del S3 público. Para una
sola fuente: `"fuentes": ["overture"]`.
"""

import json
import sys

from scrapitero.agents.establecimientos_observados import ObservadosInput, run


def main() -> None:
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"JSON inválido: {e}"}))
        sys.exit(1)

    output = run(ObservadosInput(**data))
    print(output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
