"""RPC wrapper para SaltaRegistroFetcher.

Uso:
    source .venv/bin/activate && export $(cat .env | grep -v DB_HOST | xargs) && export DB_HOST=localhost
    echo '{"region_id":"zona-salta-t-2"}' | PYTHONPATH=src python -m scrapitero.rpc.salta_registro_fetcher

Parámetros:
    region_id   str   Obligatorio
    survey_id   str   Opcional
    overwrite   bool  False (default) — solo parcelas sin uso
    batch_size  int   100 — nomenclaturas por request al SIGSA
"""

import json
import sys

from scrapitero.agents.salta_registro_fetcher import SaltaRegistroInput, run


def main() -> None:
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"JSON inválido: {e}"}))
        sys.exit(1)

    inp = SaltaRegistroInput(**data)
    output = run(inp)
    print(output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
