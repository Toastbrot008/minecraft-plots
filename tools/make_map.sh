#!/usr/bin/env bash
# Erzeugt public/map.png + public/map.json (Spawn) für den Seed.
# Braucht podman (oder docker) – kompiliert cubiomes in einem gcc-Container.
set -euo pipefail
SEED="${1:-702436}"
HALF=1500
SCALE="${SCALE:-4}"        # 4 = schnell (750x750 px), 1 = jeder Block (3000x3000 px, dauert länger)
cd "$(dirname "$0")"
RUN="${CONTAINER:-podman}"

[ -d cubiomes ] || git clone --depth 1 https://github.com/Cubitect/cubiomes

"$RUN" run --rm -v "$PWD":/w:Z -w /w docker.io/library/gcc:14 sh -c "
  cd cubiomes && make -s libcubiomes >/dev/null && cd .. &&
  gcc -O3 -fwrapv gen.c cubiomes/libcubiomes.a -lm -o gen &&
  ./gen $SEED $HALF $SCALE map.ppm > map.json"

python3 ppm2png.py map.ppm ../public/map.png
mv map.json ../public/map.json
rm -f map.ppm
echo "Fertig: public/map.png  (Spawn: $(cat ../public/map.json))"
