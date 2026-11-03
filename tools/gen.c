// Biome-Karte aus einem Seed erzeugen (nutzt cubiomes).
// Aufruf: gen <seed> <halbe_größe> <blöcke_pro_pixel: 1 oder 4> out.ppm
#include "cubiomes/generator.h"
#include "cubiomes/finders.h"
#include "cubiomes/util.h"
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv) {
    if (argc < 5) { fprintf(stderr, "usage: gen seed half scale out.ppm\n"); return 1; }
    uint64_t seed = strtoull(argv[1], 0, 10);
    int half = atoi(argv[2]), scale = atoi(argv[3]);

    Generator g;
    setupGenerator(&g, MC_NEWEST, 0);
    applySeed(&g, DIM_OVERWORLD, seed);

    int n = 2 * half / scale;
    int y = scale == 1 ? 63 : 63 >> 2;            // Meereshöhe
    Range r = { scale, -half / scale, -half / scale, n, n, y, 1 };
    int *ids = allocCache(&g, r);
    genBiomes(&g, ids, r);

    unsigned char col[256][3];
    initBiomeColors(col);
    FILE *f = fopen(argv[4], "wb");
    fprintf(f, "P6\n%d %d\n255\n", n, n);
    for (int i = 0; i < n * n; i++) fwrite(col[ids[i] & 255], 1, 3, f);
    fclose(f);

    Pos s = getSpawn(&g);
    printf("{\"spawnX\":%d,\"spawnZ\":%d}\n", s.x, s.z);
    return 0;
}
