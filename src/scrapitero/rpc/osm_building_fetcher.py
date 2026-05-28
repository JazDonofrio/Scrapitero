"""RPC wrapper para OSMBuildingFetcher.

Uso:
    echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<UUID>"}' | \\
      env $(cat /opt/scrapitero/.env | xargs) \\
      PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \\
      python3 -m scrapitero.rpc.osm_building_fetcher

Con bbox explícita:
    echo '{"region_id":"ituzaingo-ba-ar","survey_id":"<UUID>","bbox_south":-34.67,"bbox_west":-58.68,"bbox_north":-34.66,"bbox_east":-58.67}' | \\
      env $(cat /opt/scrapitero/.env | xargs) \\
      PYTHONPATH=/opt/scrapitero/.hermes-packages:/opt/scrapitero/src \\
      python3 -m scrapitero.rpc.osm_building_fetcher
"""

import json
import sys

from scrapitero.agents.osm_building_fetcher import OSMInput, run


def main() -> None:
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"JSON inválido: {e}"}))
        sys.exit(1)

    input_model = OSMInput(**data)
    output = run(input_model)
    print(output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
