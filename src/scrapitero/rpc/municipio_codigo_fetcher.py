"""Completa `regions.municipio_codigo` (código IBGE) donde falta.

    # una región puntual
    PYTHONPATH=src python -m scrapitero.rpc.municipio_codigo_fetcher \
        <<< '{"region_id":"zona-varzea-test-2"}'

    # ver qué haría con TODAS las brasileras que están en NULL, sin escribir
    PYTHONPATH=src python -m scrapitero.rpc.municipio_codigo_fetcher <<< '{"dry_run":true}'

    # barrido real
    PYTHONPATH=src python -m scrapitero.rpc.municipio_codigo_fetcher <<< '{}'
"""
import json, sys
from scrapitero.agents.municipio_codigo_fetcher import MunicipioCodigoInput, run

data = json.load(sys.stdin)
result = run(MunicipioCodigoInput(**data))
print(result.model_dump_json(indent=2))
