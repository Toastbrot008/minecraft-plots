#!/usr/bin/env python3
"""Baut palette.json: Block -> Farte von oben (Mittelwert der Textur) + Tönung,
und Biom -> Gras/Laub/Wasser-Farbe. Liest client.jar und server.jar (Mojang)."""
import io
import json
import os
import sys
import zipfile

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
client = zipfile.ZipFile(os.path.join(HERE, "client.jar"))
outer = zipfile.ZipFile(os.path.join(HERE, "server.jar"))
inner_name = next(n for n in outer.namelist() if n.startswith("META-INF/versions/") and n.endswith(".jar"))
server = zipfile.ZipFile(io.BytesIO(outer.read(inner_name)))

A = "assets/minecraft/"


def rd(z, p):
    try:
        return json.loads(z.read(p))
    except KeyError:
        return None


def strip(n):
    return n.split(":", 1)[-1]


def model_chain(name):
    """Textures + elements eines Modells inkl. Eltern auflösen."""
    tex, elements = {}, None
    while name:
        m = rd(client, f"{A}models/{strip(name)}.json")
        if m is None:
            break
        for k, v in m.get("textures", {}).items():
            tex.setdefault(k, v)
        if elements is None and "elements" in m:
            elements = m["elements"]
        name = m.get("parent")
    return tex, elements or []


def resolve(tex, ref, depth=0):
    if isinstance(ref, dict):
        ref = ref.get("sprite") or ref.get("texture")
    while ref and ref.startswith("#") and depth < 10:
        ref, depth = tex.get(ref[1:]), depth + 1
        if isinstance(ref, dict):
            ref = ref.get("sprite") or ref.get("texture")
    return ref


_cache = {}


def avg(texname):
    p = f"{A}textures/{strip(texname)}.png"
    if p in _cache:
        return _cache[p]
    try:
        im = np.asarray(Image.open(io.BytesIO(client.read(p))).convert("RGBA"), dtype=np.float64)
    except KeyError:
        _cache[p] = None
        return None
    w = im.shape[1]
    im = im[:w]                         # animierte Texturen: nur erstes Bild
    a = im[..., 3:4] / 255
    if a.sum() < 1:
        _cache[p] = None
        return None
    rgb = (im[..., :3] * a).sum((0, 1)) / a.sum()
    _cache[p] = ([int(round(c)) for c in rgb], float(a.mean()))
    return _cache[p]


def first_model(bs):
    if "variants" in bs:
        v = next(iter(bs["variants"].values()))
    else:
        v = bs["multipart"][0]["apply"]
    return (v[0] if isinstance(v, list) else v)["model"]


FOLIAGE_CONST = {"birch_leaves": "#80a755", "spruce_leaves": "#619961", "lily_pad": "#208030",
                 "mangrove_leaves": "#92c648"}
FALLBACK = {"water": "block/water_still", "lava": "block/lava_still", "bubble_column": "block/water_still"}

blocks = {}
for path in client.namelist():
    if not path.startswith(A + "blockstates/") or not path.endswith(".json"):
        continue
    name = path.rsplit("/", 1)[1][:-5]
    bs = rd(client, path)
    tex, elements = model_chain(first_model(bs))
    ref, tinted = None, False
    # Größte Fläche, die nach oben zeigt
    best = -1
    for e in elements:
        up = e.get("faces", {}).get("up")
        if not up:
            continue
        f, t = e["from"], e["to"]
        area = (t[0] - f[0]) * (t[2] - f[2])
        if area > best:
            best, ref, tinted = area, up["texture"], "tintindex" in up
    if ref is None:
        for e in elements:
            for face in e.get("faces", {}).values():
                ref, tinted = face["texture"], "tintindex" in face
                break
            if ref:
                break
    texname = resolve(tex, ref) if ref else None
    if texname is None:
        for k in ("top", "up", "end", "all", "texture", "cross", "plant", "side", "particle"):
            if k in tex:
                texname = resolve(tex, tex[k])
                if texname:
                    break
    texname = FALLBACK.get(name, texname)
    r = avg(texname) if texname else None
    if r is None:
        continue
    rgb, cover = r
    tint = None
    if name in FOLIAGE_CONST:
        tint = FOLIAGE_CONST[name]
    elif name in ("water", "bubble_column", "water_cauldron"):
        tint = "water"
    elif tinted or name in ("grass_block", "short_grass", "tall_grass", "fern", "large_fern", "vine"):
        tint = "foliage" if ("leaves" in name or name == "vine") else "grass"
    if name in ("cherry_leaves", "azalea_leaves", "flowering_azalea_leaves", "pale_oak_leaves"):
        tint = None
    blocks["minecraft:" + name] = {"c": rgb, "t": tint, "a": round(cover, 2)}


def colormap(n):
    return np.asarray(Image.open(io.BytesIO(client.read(f"{A}textures/colormap/{n}.png"))).convert("RGB"))


grass_cm, fol_cm = colormap("grass"), colormap("foliage")


def cm(img, temp, rain):
    t = min(max(temp, 0), 1)
    r = min(max(rain, 0), 1) * t
    x, y = int((1 - t) * 255), int((1 - r) * 255)
    return [int(v) for v in img[y, x]]


def hx(s):
    s = s.lstrip("#") if isinstance(s, str) else f"{s:06x}"
    return [int(s[i:i + 2], 16) for i in (0, 2, 4)]


biomes = {}
pre = "data/minecraft/worldgen/biome/"
for p in server.namelist():
    if not p.startswith(pre) or not p.endswith(".json"):
        continue
    d = json.loads(server.read(p))
    eff = d.get("effects", {})
    t, rain = d.get("temperature", 0.5), d.get("downfall", 0.5)
    g = hx(eff["grass_color"]) if "grass_color" in eff else cm(grass_cm, t, rain)
    if eff.get("grass_color_modifier") == "swamp":
        g = [106, 112, 57]
    elif eff.get("grass_color_modifier") == "dark_forest":
        g = [((g[0] + 0x28) >> 1), ((g[1] + 0x34) >> 1), ((g[2] + 0x0a) >> 1)]
    f = hx(eff["foliage_color"]) if "foliage_color" in eff else cm(fol_cm, t, rain)
    w = hx(eff.get("water_color", "#3f76e4"))
    biomes["minecraft:" + p[len(pre):-5]] = {"grass": g, "foliage": f, "water": w,
                                             "snow": t < 0.15}

json.dump({"blocks": blocks, "biomes": biomes}, open(os.path.join(HERE, "palette.json"), "w"))
print(f"{len(blocks)} Blöcke, {len(biomes)} Biome -> palette.json")
for k in ("grass_block", "oak_leaves", "water", "sand", "stone", "poppy", "oak_log", "short_grass", "snow"):
    print(k, blocks.get("minecraft:" + k))
