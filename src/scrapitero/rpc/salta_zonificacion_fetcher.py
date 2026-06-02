"""RPC wrapper para SaltaZonificacionFetcher.

Uso:
    source .venv/bin/activate && export $(cat .env | grep -v DB_HOST | xargs) && export DB_HOST=localhost
    echo '{"region_id":"zona-salta-t-1"}' | PYTHONPATH=src python -m scrapitero.rpc.salta_zonificacion_fetcher

Parámetros:
    region_id   str   Obligatorio
    survey_id   str   Opcional — filtra por survey específico
    overwrite   bool  False (default) — reclasifica solo parcelas sin uso_principal
                      True — reclasifica todas, incluso las ya clasificadas
"""

import json
import sys

from scrapitero.agents.salta_zonificacion_fetcher import SaltaZonifInput, run


def main() -> None:
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"JSON inválido: {e}"}))
        sys.exit(1)

    inp = SaltaZonifInput(**data)
    output = run(inp)
    print(output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
