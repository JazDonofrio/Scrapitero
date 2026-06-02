"""RPC wrapper para SaltaCatastroFetcher.

Uso (desde host):
    source .venv/bin/activate && export $(cat .env | grep -v DB_HOST | xargs) && export DB_HOST=localhost
    echo '{"region_id":"zona-salta-capital-ejemplo","fuente":"auto"}' | \\
        PYTHONPATH=src python -m scrapitero.rpc.salta_catastro_fetcher

Uso (desde container Hermes):
    echo '{"region_id":"...","survey_id":"..."}' | \\
        PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \\
        python3 -m scrapitero.rpc.salta_catastro_fetcher

Fuentes:
    "auto"      → detecta automáticamente según centroide de la zona
    "capital"   → fuerza IDEMSA (geocloud.municipalidadsalta.gob.ar)
    "provincia" → fuerza IDESA (geoportal.idesa.gob.ar)
"""

import json
import sys

from scrapitero.agents.salta_catastro_fetcher import SaltaCatastroInput, run


def main() -> None:
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"JSON inválido: {e}"}))
        sys.exit(1)

    inp = SaltaCatastroInput(**data)
    output = run(inp)
    print(output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
