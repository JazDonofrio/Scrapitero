"""RPC wrapper para UnidadesEstimator.

Uso:
    source .venv/bin/activate && export $(cat .env | grep -v DB_HOST | xargs) && export DB_HOST=localhost
    echo '{"region_id":"zona-salta-t-1"}' | PYTHONPATH=src python -m scrapitero.rpc.unidades_estimator

Parámetros:
    region_id     str    Obligatorio
    survey_id     str    Opcional — filtra por survey específico
    overwrite     bool   False (default) — solo parcelas aún sin estimar (footprints_count=0).
                         True — recalcula todas, incluso las ya estimadas.
    m2_vivienda   float  80.0  — tamaño típico de vivienda para el proxy geométrico
    m2_comercio   float  50.0  — tamaño típico de local comercial
    pisos_default int    1     — pisos asumidos si OSM no trae building:levels
"""

import json
import sys

from scrapitero.agents.unidades_estimator import UnidadesInput, run


def main() -> None:
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"JSON inválido: {e}"}))
        sys.exit(1)

    inp = UnidadesInput(**data)
    output = run(inp)
    print(output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
