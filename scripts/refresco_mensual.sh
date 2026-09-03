#!/bin/bash
# Envoltorio del refresco mensual para el cron del host.
#
# Existe porque `cron` corre con /bin/sh y un entorno casi vacío: hace falta activar el venv
# y cargar el `.env`, y eso último NO se puede con `export $(cat .env | xargs)` — el `.env`
# tiene comentarios con espacios y acentos que rompen el export (verificado). De ahí el
# `grep` a líneas `CLAVE=` y el `set -a`, que además necesita bash, no sh.
#
# `DB_HOST` se pisa a `localhost`: el `.env` trae `scrapitero_db`, que es el nombre en la red
# de Docker y desde el host no resuelve.
set -uo pipefail
cd /opt/scrapitero || exit 1

# shellcheck disable=SC1091
source .venv/bin/activate
set -a
# shellcheck disable=SC1090
source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' .env | grep -v '^DB_HOST=')
set +a
export DB_HOST=localhost

exec python3 scripts/refresco_mensual.py --telegram
