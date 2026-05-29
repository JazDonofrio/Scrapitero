#!/bin/bash
# Sincroniza hermes-skills/scrapitero → ~/.hermes/skills/scrapitero (trusted dir)
# Correr cada vez que se agrega o modifica un skill.

SRC="/opt/scrapitero/hermes-skills/scrapitero"
DST_TRUSTED="/docker/hermes-agent-wgnq/data/home/.hermes/skills/scrapitero"
DST_CATALOG="/docker/hermes-agent-wgnq/data/skills/scrapitero"

# Trusted dir (para ejecución)
rsync -avL --delete "$SRC/" "$DST_TRUSTED/"
# Catálogo de descubrimiento (sin symlinks, Hermes no los sigue)
rm -rf "$DST_CATALOG" && cp -rL "$SRC" "$DST_CATALOG"
echo "Skills sincronizados en trusted dir y catálogo."
