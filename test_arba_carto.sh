#!/bin/bash
# Test directo de arba_carto_fetcher
# Uso: bash test_arba_carto.sh [JSESSIONID]
# Si no se pasa JSESSIONID, usa el del script (editarlo abajo)

JSESSIONID="${1:-462CD59F82795EF89FD61E7C388A7326}"
SURVEY_ID="86f0d083-b9a7-4d6a-a214-437b621d05c3"

INPUT=$(cat <<EOF
{
  "region_id": "ituzaingo-ba-ar",
  "survey_id": "${SURVEY_ID}",
  "partido_id": "136",
  "circunscripcion": "2",
  "seccion": "C",
  "manzana": "184",
  "jsessionid": "${JSESSIONID}"
}
EOF
)

echo "=== INPUT ==="
echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); d['jsessionid']=d['jsessionid'][:8]+'...'; print(json.dumps(d,indent=2))"
echo ""
echo "=== OUTPUT ==="

echo "$INPUT" | \
  env $(cat /opt/scrapitero/.env | xargs) DB_HOST=localhost \
  PYTHONPATH=/opt/scrapitero/src \
  /opt/scrapitero/.venv/bin/python3 -m scrapitero.rpc.arba_carto_fetcher 2>&1
