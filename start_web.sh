#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

source .venv/bin/activate

# DB_HOST=localhost porque el container expone 5432 en el host
# (dentro de Docker se usa el hostname scrapitero_db del .env)
export $(grep -v '^#' .env | grep -v '^$' | grep -v 'DB_HOST' | xargs)
export DB_HOST=localhost

PORT=${PORT:-8765}

echo "Scrapitero Web UI → http://localhost:$PORT"
exec uvicorn scrapitero.web.app:app --host 0.0.0.0 --port "$PORT" "$@"
