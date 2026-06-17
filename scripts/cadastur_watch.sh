#!/usr/bin/env bash
# Wrapper para cron: chequea Cadastur y avisa por Telegram cuando vuelve.
# Crontab sugerido (cada 4 h):
#   0 */4 * * * /opt/scrapitero/scripts/cadastur_watch.sh >> /var/log/cadastur_watch.log 2>&1
set -euo pipefail
cd /opt/scrapitero
# Variables de entorno (Telegram). No necesita DB.
set -a; [ -f .env ] && . ./.env; set +a
exec /opt/scrapitero/.venv/bin/python scripts/cadastur_watch.py
