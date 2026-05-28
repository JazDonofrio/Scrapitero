"""RPC wrapper para ARBACartoFetcher.

Uso normal (con sesión guardada):
    echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<UUID>","partido_id":"136","circunscripcion":"2","seccion":"C","manzana":"184"}' | \\
      env $(cat /opt/scrapitero/.env | xargs) \\
      PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \\
      python3 -m scrapitero.rpc.arba_carto_fetcher

Pasando jsessionid (luego de pedírselo al usuario por Telegram):
    echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<UUID>","partido_id":"136","circunscripcion":"2","seccion":"C","manzana":"184","jsessionid":"ABC123DEF"}' | \\
      env $(cat /opt/scrapitero/.env | xargs) \\
      PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \\
      python3 -m scrapitero.rpc.arba_carto_fetcher

Pasando el header Cookie completo:
    echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<UUID>","partido_id":"136","cookie_header":"JSESSIONID=ABC123; otro=valor"}' | \\
      env $(cat /opt/scrapitero/.env | xargs) \\
      PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \\
      python3 -m scrapitero.rpc.arba_carto_fetcher
"""

import json
import sys

from scrapitero.agents.arba_carto_fetcher import ARBACartoInput, run


def main() -> None:
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"JSON inválido: {e}"}))
        sys.exit(1)

    input_model = ARBACartoInput(**data)
    output = run(input_model)
    print(output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
