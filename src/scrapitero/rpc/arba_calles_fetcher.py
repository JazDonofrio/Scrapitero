"""RPC de ARBACallesFetcher: alturas oficiales del callejero de ARBA como anclas.

    python -m scrapitero.rpc.arba_calles_fetcher <<< '{"region_id":"zona-merlo-diego",
        "survey_id":"<UUID>"}'
"""
import json
import sys

from dotenv import load_dotenv
load_dotenv()

from scrapitero.agents.arba_calles_fetcher import ARBACallesInput  # noqa: E402
from scrapitero.agents.arba_calles_fetcher import run  # noqa: E402


def main() -> None:
    raw = sys.stdin.read().strip()
    data = json.loads(raw) if raw else {}
    print(run(ARBACallesInput(**data)).model_dump_json(indent=2))


if __name__ == "__main__":
    main()
