#!/bin/bash
# Sincroniza hermes-skills/scrapitero → skills dir propio de Hermes (/opt/data/skills).
# Correr cada vez que se agrega o modifica un skill.
#
# Va a UN solo lugar a propósito: si la misma skill aparece también en un
# skills.external_dirs (p.ej. ~/.hermes/skills), skill_view la rechaza por
# "Ambiguous skill name" y el orquestador no arranca el relevamiento (13-oct-2026).

SRC="/opt/scrapitero/hermes-skills/scrapitero"
DST_CATALOG="/docker/hermes-agent-wgnq/data/skills/scrapitero"

# Sin symlinks: Hermes no los sigue
rm -rf "$DST_CATALOG" && cp -rL "$SRC" "$DST_CATALOG"
echo "Skills sincronizados en $DST_CATALOG."
