"""RPC wrapper para GooglePlacesFetcher.

Uso (host):
    source .venv/bin/activate && export $(cat .env | grep -v DB_HOST | xargs) && export DB_HOST=localhost
    PYTHONPATH=src python -m scrapitero.rpc.google_places_fetcher <<< '{"region_id":"zona-salta-t-1"}'

Parámetros:
    region_id       str    Obligatorio
    survey_id       str    Opcional — si se omite usa el último survey de la región
    cell_size_m     float  150.0 — lado de la celda inicial de búsqueda
    min_cell_m      float  40.0  — no subdividir por debajo de esto (zonas densas)
    max_requests    int    400   — tope de llamadas (corta resultado parcial)
    included_types  list   override de tipos comerciales de Google (Table A)
    set_uso         bool   true  — sube uso_principal a comercial/mixto en parcelas con comercio
    overwrite       bool   false — re-tesselar aunque ya haya comercios cargados
    bbox_south/west/north/east  float  override de bbox (zonas con features dispersos)

Requiere GOOGLE_MAPS_API_KEY con Places API (New) habilitada.
Correr DESPUÉS de UnidadesEstimator: solo sobrescribe uf_comercio con el conteo real.
"""

import json
import sys

from scrapitero.agents.google_places_fetcher import GooglePlacesInput, run


def main() -> None:
    raw = sys.stdin.read().strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"JSON inválido: {e}"}))
        sys.exit(1)

    inp = GooglePlacesInput(**data)
    output = run(inp)
    print(output.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
