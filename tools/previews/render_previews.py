#!/usr/bin/env python3
"""Vorschaubilder der echten Welt mit Chunky (Path Tracing, Original-Texturen).

Die Karte wird unsichtbar in Felder von CELL x CELL Blöcken geteilt. Pro Feld
entstehen VIEWS Drohnenaufnahmen aus verschiedenen Richtungen und Tageszeiten,
gerendert aus den echten Region-Dateien (tools/world/run/world) mit den
Texturen aus tools/world/client.jar.

Ausgabe: public/previews/<cx>_<cz>_<k>.webp + public/previews/index.json

Aufruf:
  python3 render_previews.py                  alle Felder
  python3 render_previews.py 0,0 -1,0 0,-1    nur diese Felder (Test)
Umgebung: SPP (Samples/Pixel, Std. 256), W/H (Bildgröße), THREADS, RAM
"""
import glob
import gzip
import json
import math
import os
import subprocess
import sys
import time

from array import array
from PIL import Image, ImageChops, ImageEnhance

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
WORLD = os.path.join(ROOT, "tools", "world", "run", "world")
CLIENT_JAR = os.path.join(ROOT, "tools", "world", "client.jar")
TERRAIN = os.path.join(ROOT, "public", "terrain")
OUT = os.path.join(ROOT, "public", "previews")
SCENES = os.path.join(HERE, "scenes")
CHUNKY_LIB = os.path.expanduser("~/.chunky/lib")

CELL = 256                      # Feldgröße in Blöcken (muss zu index.html passen)
HALF = 1500                     # Kartenrand
VIEWS = 3
SPP = int(os.environ.get("SPP", 256))
W, H = int(os.environ.get("W", 1280)), int(os.environ.get("H", 720))
THREADS = os.environ.get("THREADS", str(os.cpu_count() or 4))
RAM = os.environ.get("RAM", "4G")
LOAD_R = 34                     # Chunk-Radius um das Ziel, der geladen wird
C0, C1 = (-HALF) >> 4, (HALF - 1) >> 4   # Chunk-Bereich, den pregen.py komplett generiert

# Drei Stimmungen: (Sonnenhöhe°, Sonne rel. zur Blickrichtung°, Dunst, Belichtung, Dunstfarbe)
# Der Dunst lässt die Ferne weich auslaufen; hinter LOAD_R ist nichts geladen, das
# verschwindet so in derselben Farbe (skyFogDensity 1) statt als graue Fläche.
MOODS = [
    (13, 70, 0.015, 1.40, (1.00, 0.80, 0.62)),    # goldene Stunde, Seitenlicht, warmer Dunst
    (40, 130, 0.010, 1.05, (0.70, 0.82, 1.00)),   # Vormittag, Licht von schräg hinten, klar
    (24, -60, 0.012, 1.20, (0.92, 0.86, 0.80)),   # Nachmittag, andere Seite
]


# ------------------------------------------------------------- Gelände
class Heights:
    """Oberflächenhöhe pro Block aus public/terrain/*.bin (512x512 je Region)."""

    def __init__(self):
        self.cache = {}

    def region(self, rx, rz):
        key = (rx, rz)
        if key not in self.cache:
            p = os.path.join(TERRAIN, f"r.{rx}.{rz}.bin")
            if os.path.exists(p):
                raw = gzip.decompress(open(p, "rb").read())
                n = 512 * 512
                # base:u16, top:u16, height:i16, surf:i16, depth:u8, biome:u8
                off = 2 * n + 2 * n + 2 * n
                surf = array("h")
                surf.frombytes(raw[off:off + 2 * n])
                if sys.byteorder != "little":
                    surf.byteswap()
                self.cache[key] = surf
            else:
                self.cache[key] = None
        return self.cache[key]

    def at(self, x, z):
        g = self.region(x >> 9, z >> 9)
        if g is None:
            return None
        v = g[(z & 511) * 512 + (x & 511)]
        return None if v == -32768 else v

    def area(self, x0, z0, x1, z1, step=2):
        vals = [self.at(x, z) for z in range(z0, z1, step) for x in range(x0, x1, step)]
        vals = [v for v in vals if v is not None]
        return vals


def hash01(*a):
    h = 2166136261
    for v in a:
        h = ((h ^ (v & 0xFFFFFFFF)) * 16777619) & 0xFFFFFFFF
    return h / 0xFFFFFFFF


# ------------------------------------------------------------- Szene
def make_scene(name, cx, cz, k, hts):
    x0, z0 = cx * CELL, cz * CELL
    # Ziel: Mitte des Felds, leicht versetzt pro Ansicht, damit die 3 Bilder variieren
    jx = (hash01(cx, cz, k, 1) - 0.5) * CELL * 0.35
    jz = (hash01(cx, cz, k, 2) - 0.5) * CELL * 0.35
    tx, tz = x0 + CELL / 2 + jx, z0 + CELL / 2 + jz
    ring = hts.area(int(tx) - 24, int(tz) - 24, int(tx) + 24, int(tz) + 24)
    if not ring:
        return None
    ring.sort()
    ty = float(ring[int(len(ring) * 0.6)])

    # Blickrichtung (Kamera -> Ziel). Nahe am Kartenrand immer grob Richtung Zentrum
    # schauen, sonst sieht man hinter dem Ziel das Ende der generierten Welt.
    edge = HALF - max(abs(tx), abs(tz))
    if edge < 650:
        to_center = math.atan2(-tz, -tx)
        theta = to_center + math.radians((-55, 0, 55)[k % 3] + (hash01(cx, cz, k, 7) - 0.5) * 20)
    else:
        theta = 2 * math.pi * (hash01(cx, cz, 7) + k / VIEWS)
    dist, lift = 125.0, 46.0
    camx, camz = tx - dist * math.cos(theta), tz - dist * math.sin(theta)
    # Kamera nicht im Berg: Gelände unter und vor der Kamera prüfen
    near = hts.area(int(camx) - 8, int(camz) - 8, int(camx) + 8, int(camz) + 8)
    camy = max(ty + lift, (max(near) + 14) if near else ty + lift)
    for s in [0.15 + 0.1 * i for i in range(8)]:                    # Sichtlinie frei halten
        px, pz = camx + (tx - camx) * s, camz + (tz - camz) * s
        h = hts.at(int(px), int(pz))
        if h is not None:
            ray_y = camy + (ty - camy) * s
            if h + 4 > ray_y:
                camy += (h + 4 - ray_y) / max(1 - s, 0.15)
    camy = min(camy, 315)
    down = math.atan2(camy - ty, dist)

    alt, rel, fog, expo, fogc = MOODS[k % len(MOODS)]
    sun_az = theta + math.radians(rel)

    ccx, ccz = int(tx) >> 4, int(tz) >> 4
    chunks = [[x, z] for x in range(ccx - LOAD_R, ccx + LOAD_R + 1)
              for z in range(ccz - LOAD_R, ccz + LOAD_R + 1)
              if (x - ccx) ** 2 + (z - ccz) ** 2 <= LOAD_R ** 2
              # nur fertig generierte Chunks: außerhalb von ±HALF liegen halbfertige Rand-Chunks
              and C0 <= x <= C1 and C0 <= z <= C1]

    scene = {
        "sdfVersion": 10,
        "name": name,
        "width": W, "height": H,
        "exposure": expo,
        "postprocess": "TONEMAP3",           # Hable/Uncharted-Filmkurve
        "outputMode": "PNG",
        "spp": 0, "renderTime": 0,
        "sppTarget": SPP,
        "rayDepth": 6,
        "pathTrace": True,
        "dumpFrequency": 0,
        "saveSnapshots": False,
        "emittersEnabled": True,
        "emitterIntensity": 13,
        "sunSamplingStrategy": "IMPORTANCE",
        "waterShadingStrategy": "SIMPLEX",
        "biomeColorsEnabled": True,
        "biomeBlendingRadius": 3,
        "transparentSky": False,
        "waterWorldEnabled": False,
        "hideUnknownBlocks": False,
        "fog": {"mode": "UNIFORM", "uniformDensity": fog, "skyFogDensity": 1.0,
                "color": {"red": fogc[0], "green": fogc[1], "blue": fogc[2]}, "fastFog": True, "layers": []},
        "world": {"path": WORLD, "dimension": "minecraft:overworld"},
        "camera": {
            "name": "camera 1",
            "position": {"x": camx, "y": camy, "z": camz},
            "orientation": {"roll": 0.0, "pitch": -(math.pi / 2 - down), "yaw": math.pi - theta},
            "projectionMode": "PINHOLE",
            "fov": 62.0,
            "dof": "Infinity",
            "focalOffset": dist,
        },
        "sun": {"altitude": math.radians(alt), "azimuth": sun_az, "intensity": 1.25,
                "drawTexture": True},
        "sky": {"mode": "SIMULATED", "simulatedSky": "Nishita", "skyLight": 1.0,
                "cloudsEnabled": False, "cloudSize": 64,
                "cloudOffset": {"x": hash01(cx, cz, k, 3) * 400, "y": max(camy + 90, 250),
                                "z": hash01(cx, cz, k, 4) * 400}},   # Wolken immer über der Kamera
        "chunkList": chunks,
    }
    return scene, (tx, ty, tz), (camx, camy, camz)


# ------------------------------------------------------------- Chunky
def chunky(*args):
    cp = os.pathsep.join(sorted(glob.glob(os.path.join(CHUNKY_LIB, "*.jar"))))
    cmd = ["java", f"-Xmx{RAM}", "-Djava.awt.headless=true", "-cp", cp,
           "se.llbit.chunky.main.Chunky", *args]
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def render(name):
    for p in glob.glob(os.path.join(SCENES, name, "**", "*.png"), recursive=True):
        os.remove(p)
    r = chunky("-f", "-threads", THREADS, "-scene-dir", os.path.join(SCENES, name),
               "-reload-chunks", "-target", str(SPP), "-render", name)
    pngs = sorted(glob.glob(os.path.join(SCENES, name, "**", "*.png"), recursive=True),
                  key=os.path.getmtime)
    if not pngs:
        tail = "\n".join(r.stdout.splitlines()[-25:])
        raise RuntimeError(f"Chunky hat kein Bild erzeugt:\n{tail}")
    return pngs[-1], r.stdout


def grade(im):
    """Leichtes Color Grading: Kontrast, Sättigung, Vignette. Ändert keine Inhalte."""
    im = im.convert("RGB")
    im = ImageEnhance.Contrast(im).enhance(1.14)
    im = ImageEnhance.Color(im).enhance(1.18)
    w, h = im.size
    # Vignette: Ränder sanft um bis zu ~22 % abdunkeln
    vg = Image.radial_gradient("L").resize((w, h), Image.BICUBIC)       # 0 Mitte .. 255 außen
    vg = vg.point(lambda v: 255 - int(min(255, max(0, (v - 90) * 0.42))))
    return ImageChops.multiply(im, Image.merge("RGB", (vg, vg, vg)))


def all_cells():
    c0, c1 = (-HALF) // CELL, (HALF - 1) // CELL
    return [(x, z) for z in range(c0, c1 + 1) for x in range(c0, c1 + 1)]


def main():
    cells = [tuple(map(int, a.split(","))) for a in sys.argv[1:]] or all_cells()
    os.makedirs(OUT, exist_ok=True)
    idx_path = os.path.join(OUT, "index.json")
    index = json.load(open(idx_path)) if os.path.exists(idx_path) else {}
    index["cell"] = CELL
    index.setdefault("cells", {})
    hts = Heights()
    t_start = time.time()
    for n, (cx, cz) in enumerate(cells):
        key = f"{cx},{cz}"
        shots = []
        for k in range(VIEWS):
            name = f"p_{cx}_{cz}_{k}".replace("-", "m")
            out = os.path.join(OUT, f"{cx}_{cz}_{k}.webp")
            if os.path.exists(out) and not os.environ.get("FORCE"):
                shots.append(f"{cx}_{cz}_{k}.webp")
                continue
            made = make_scene(name, cx, cz, k, hts)
            if made is None:
                print(f"[{key}] Ansicht {k}: kein Gelände, übersprungen", flush=True)
                continue
            scene, tgt, cam = made
            os.makedirs(os.path.join(SCENES, name), exist_ok=True)
            with open(os.path.join(SCENES, name, name + ".json"), "w") as f:
                json.dump(scene, f, indent=1)
            t0 = time.time()
            png, log = render(name)
            grade(Image.open(png)).save(out, "WEBP", quality=86, method=6)
            shots.append(f"{cx}_{cz}_{k}.webp")
            print(f"[{key}] Ansicht {k}: {time.time() - t0:.0f}s  Kamera {tuple(round(v) for v in cam)} "
                  f"-> Ziel {tuple(round(v) for v in tgt)}", flush=True)
            for p in glob.glob(os.path.join(SCENES, name, "*.octree2")) + \
                    glob.glob(os.path.join(SCENES, name, "*.dump")):
                os.remove(p)            # Octrees sind groß; Szene-JSON bleibt zum Nachrendern
        if shots:
            index["cells"][key] = shots
            with open(idx_path, "w") as f:
                json.dump(index, f, separators=(",", ":"))
        el = time.time() - t_start
        print(f"== {n + 1}/{len(cells)} Felder · Rest ~{el / (n + 1) * (len(cells) - n - 1) / 60:.0f} min",
              flush=True)


if __name__ == "__main__":
    main()
