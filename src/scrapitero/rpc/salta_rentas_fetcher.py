"""RPC wrapper para SaltaRentasFetcher.

Uso:
    source .venv/bin/activate && export $(cat .env | grep -v DB_HOST | xargs) && export DB_HOST=localhost
    echo '{"region_id":"zona-salta-t-2"}' | PYTHONPATH=src python -m scrapitero.rpc.salta_rentas_fetcher

Parámetros:
    region_id   str   Obligatorio
    survey_id   str   Opcional
    overwrite   bool  False — reconsulta parcelas ya marcadas vacante
    delay_ms    int   1500 — throttle entre consultas (reCAPTCHA + cortesía)
    batch_size  int   0 = todas las pendientes
    headless    bool  True
"""

import json
import sys

from scrapitero.agents.salta_rentas_fetcher import SaltaRentasInput, run


def main() -> None:
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"JSON inválido: {e}"}))
        sys.exit(1)

    inp = SaltaRentasInput(**data)
    output = run(inp)
    print(output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
