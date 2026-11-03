#!/usr/bin/env bash
# Gelände-Karte (echte Blöcke, Bäume, Wasser) für public/terrain/ erzeugen.
#   1. lädt server.jar + client.jar der Version von Mojang (nur beim ersten Mal)
#   2. palette.py  – Blockfarben aus den Texturen, Biom-Tönung (Gras/Laub/Wasser)
#   3. pregen.py   – Vanilla-Server generiert ±HALF vor (~10 min, braucht ~4 GB RAM)
#   4. render.py   – Kacheln + Blockdaten für den Tooltip
# Aufruf: tools/world/make_terrain.sh [seed] [version]
# Eigene Welt rendern statt generieren:  python3 render.py /pfad/zur/welt
set -euo pipefail
SEED="${1:-702436}"
VERSION="${2:-26.3}"
HALF="${HALF:-1500}"
RAM="${RAM:-4G}"
cd "$(dirname "$0")"

if [ ! -f server.jar ] || [ ! -f client.jar ]; then
  URL=$(curl -s https://piston-meta.mojang.com/mc/game/version_manifest_v2.json |
        python3 -c "import json,sys; print([v['url'] for v in json.load(sys.stdin)['versions'] if v['id']=='$VERSION'][0])")
  curl -s "$URL" -o version.json
  curl -s -o server.jar "$(python3 -c "import json; print(json.load(open('version.json'))['downloads']['server']['url'])")"
  curl -s -o client.jar "$(python3 -c "import json; print(json.load(open('version.json'))['downloads']['client']['url'])")"
fi

python3 palette.py
if [ "${SKIP_PREGEN:-0}" != 1 ]; then python3 -u pregen.py "$SEED" "$HALF" "$RAM"; fi
python3 render.py
