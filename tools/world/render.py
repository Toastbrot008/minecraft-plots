#!/usr/bin/env python3
"""Rendert die generierte Welt von oben (wie eine Karte) in Kacheln.

Liest run/world/dimensions/minecraft/overworld/region/*.mca und schreibt nach
public/terrain/:
  r.<rx>.<rz>.webp   512x512 px, 1 Pixel = 1 Block (Kachel = Region-Datei)
  r.<rx>.<rz>.bin    gzip: pro Block oberster Block, Untergrund, Höhe, Biom (Tooltip)
  overview.webp      ganze Karte verkleinert
  index.json         Kachelliste + Namen der Blöcke/Biome
Aufruf: python3 render.py [world-ordner]
"""
import glob
import gzip
import io
import json
import math
import os
import struct
import sys
import zlib
from multiprocessing import Pool

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
WORLD = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "run", "world")
OUT = os.path.join(HERE, "..", "..", "public", "terrain")
PAL = json.load(open(os.path.join(HERE, "palette.json")))
MIN_Y, NY = -64, 384

AIR = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air", "minecraft:light",
       "minecraft:barrier", "minecraft:structure_void"}
WATERY = {"minecraft:water", "minecraft:bubble_column", "minecraft:seagrass", "minecraft:tall_seagrass",
          "minecraft:kelp", "minecraft:kelp_plant"}


# ---------------------------------------------------------------- NBT
class R:
    __slots__ = ("d", "p")

    def __init__(self, d):
        self.d, self.p = d, 0

    def take(self, n):
        p = self.p
        self.p += n
        return self.d[p:p + n]

    def u8(self):
        self.p += 1
        return self.d[self.p - 1]

    def i32(self):
        return struct.unpack(">i", self.take(4))[0]

    def s(self):
        n = struct.unpack(">H", self.take(2))[0]
        return self.take(n).decode("utf-8", "replace")


def payload(r, t):
    if t == 1:
        return struct.unpack(">b", r.take(1))[0]
    if t == 2:
        return struct.unpack(">h", r.take(2))[0]
    if t == 3:
        return r.i32()
    if t == 4:
        return struct.unpack(">q", r.take(8))[0]
    if t == 5:
        return struct.unpack(">f", r.take(4))[0]
    if t == 6:
        return struct.unpack(">d", r.take(8))[0]
    if t == 7:
        n = r.i32()
        r.take(n)
        return None                          # Licht-Arrays brauchen wir nicht
    if t == 8:
        return r.s()
    if t == 9:
        et, n = r.u8(), r.i32()
        return [payload(r, et) for _ in range(n)]
    if t == 10:
        out = {}
        while True:
            tt = r.u8()
            if tt == 0:
                return out
            key = r.s()
            out[key] = payload(r, tt)
    if t == 11:
        n = r.i32()
        return np.frombuffer(r.take(4 * n), ">i4")
    if t == 12:
        n = r.i32()
        return np.frombuffer(r.take(8 * n), ">u8")
    raise ValueError(f"NBT-Tag {t}")


def nbt(data):
    r = R(data)
    t = r.u8()
    r.s()
    return payload(r, t)


def entry_name(p):
    """Palette-Eintrag: "name", {"Name"/"id": ...} oder (gemischte Listen ab 1.21.5) {"": ...}."""
    while isinstance(p, dict):
        p = p.get("Name") or p.get("id") or p.get("")
    return p


def unpack(longs, bits, count):
    """Gepackte Indizes (Einträge überspannen keine Long-Grenze)."""
    per = 64 // bits
    longs = longs.astype(np.uint64)
    shifts = (np.arange(per, dtype=np.uint64) * np.uint64(bits))
    vals = (longs[:, None] >> shifts[None, :]) & np.uint64((1 << bits) - 1)
    return vals.reshape(-1)[:count].astype(np.int32)


# ---------------------------------------------------------------- Region
def render_region(path):
    rx, rz = map(int, os.path.basename(path).split(".")[1:3])
    names, ids = [], {}
    bnames, bids = [], {}

    def gid(n):
        if n not in ids:
            ids[n] = len(names)
            names.append(n)
        return ids[n]

    def bid(n):
        if n not in bids:
            bids[n] = len(bnames)
            bnames.append(n)
        return bids[n]

    top = np.full((512, 512), 0xFFFF, np.uint16)    # Pflanze/Overlay über dem Untergrund
    base = np.full((512, 512), 0xFFFF, np.uint16)   # erster fester Block
    height = np.full((512, 512), -32768, np.int16)  # Höhe des Untergrunds
    surf = np.full((512, 512), -32768, np.int16)    # Höhe der Oberfläche (inkl. Wasser)
    depth = np.zeros((512, 512), np.uint8)          # Wassertiefe
    biome = np.zeros((512, 512), np.uint8)

    raw = open(path, "rb").read()
    if len(raw) < 8192:
        return None
    for i in range(1024):
        off = int.from_bytes(raw[i * 4:i * 4 + 3], "big") * 4096
        if not off:
            continue
        ln, ct = int.from_bytes(raw[off:off + 4], "big"), raw[off + 4]
        body = raw[off + 5:off + 4 + ln]
        if ct == 2:
            data = zlib.decompress(body)
        elif ct == 1:
            data = gzip.decompress(body)
        elif ct == 3:
            data = body
        else:
            continue
        ch = nbt(data)
        if ch.get("Status") != "minecraft:full":
            continue
        lx, lz = (i % 32) * 16, (i // 32) * 16
        col3 = np.zeros((NY, 16, 16), np.uint16)
        bio_sec = {}
        for sec in ch.get("sections", []):
            sy = sec["Y"]
            y0 = sy * 16 - MIN_Y
            if not 0 <= y0 < NY:
                continue
            bs = sec.get("block_states")
            if bs:
                pal = [gid(entry_name(p)) for p in bs["palette"]]
                if len(pal) == 1:
                    col3[y0:y0 + 16] = pal[0]
                else:
                    bits = max(4, math.ceil(math.log2(len(pal))))
                    idx = unpack(bs["data"], bits, 4096)
                    col3[y0:y0 + 16] = np.asarray(pal, np.uint16)[idx].reshape(16, 16, 16)
            bm = sec.get("biomes")
            if bm:
                bp = [bid(entry_name(p)) for p in bm["palette"]]
                if len(bp) == 1:
                    bio_sec[sy] = np.full((4, 4, 4), bp[0], np.uint8)
                else:
                    bits = math.ceil(math.log2(len(bp)))
                    idx = unpack(bm["data"], bits, 64)
                    bio_sec[sy] = np.asarray(bp, np.uint8)[idx].reshape(4, 4, 4)
        lut = np.array([0 if n in AIR else 1 if n in WATERY else
                        2 if PAL["blocks"].get(n, {"a": 1})["a"] < 0.5 else 3 for n in names], np.uint8)
        cat = lut[col3][::-1]                                  # oben -> unten
        nonair = cat != 0
        i_top = nonair.argmax(0)
        solid = cat == 3
        i_base = solid.argmax(0)
        has = solid[i_base, np.arange(16)[:, None], np.arange(16)[None, :]]
        rev = col3[::-1]
        zz, xx = np.meshgrid(np.arange(16), np.arange(16), indexing="ij")
        b_ids = rev[i_base, zz, xx]
        t_ids = rev[i_top, zz, xx]
        t_cat = cat[i_top, zz, xx]
        wcount = ((cat == 1) & (np.arange(NY)[:, None, None] < i_base[None])).sum(0)
        # Overlay = oberste Pflanze direkt über dem Untergrund
        ov_mask = (cat == 2) & (np.arange(NY)[:, None, None] < i_base[None])
        i_ov = np.where(ov_mask.any(0), ov_mask.argmax(0), 0)
        ov_ids = np.where(ov_mask.any(0), rev[i_ov, zz, xx], 0xFFFF)
        hy = (NY - 1 - i_base) + MIN_Y
        sy_ = (NY - 1 - i_top) + MIN_Y
        sl = (slice(lz, lz + 16), slice(lx, lx + 16))
        base[sl] = np.where(has, b_ids, 0xFFFF)
        top[sl] = np.where(has, ov_ids, 0xFFFF)
        height[sl] = np.where(has, hy, -32768)
        surf[sl] = np.where(has & (t_cat != 0), sy_, hy)
        depth[sl] = np.clip(wcount, 0, 255)
        # Biom am Untergrund
        for z in range(0, 16, 4):
            for x in range(0, 16, 4):
                y = int(hy[z, x])
                s = bio_sec.get(y // 16)
                if s is not None:
                    biome[lz + z:lz + z + 4, lx + x:lx + x + 4] = s[(y % 16) // 4, z // 4, x // 4]
    return dict(rx=rx, rz=rz, names=names, bnames=bnames, top=top, base=base,
                height=height, surf=surf, depth=depth, biome=biome)


# ---------------------------------------------------------------- Farben
def colorize(g, names, bnames):
    blocks, biomes = PAL["blocks"], PAL["biomes"]
    n = len(names)
    col = np.zeros((n + 1, 3), np.float32)
    alpha = np.ones(n + 1, np.float32)
    tint = np.zeros(n + 1, np.int8)                    # 0 keine, 1 Gras, 2 Laub, 3 Wasser, 4 fest
    const = np.zeros((n + 1, 3), np.float32)
    for i, nm in enumerate(names):
        b = blocks.get(nm)
        if not b:
            col[i] = (255, 0, 255)
            continue
        col[i] = b["c"]
        alpha[i] = b["a"]
        t = b["t"]
        if t == "grass":
            tint[i] = 1
        elif t == "foliage":
            tint[i] = 2
        elif t == "water":
            tint[i] = 3
        elif t:
            tint[i] = 4
            const[i] = [int(t[k:k + 2], 16) for k in (1, 3, 5)]
    col[n] = 0
    nb = max(len(bnames), 1)
    bcol = np.zeros((nb, 3, 3), np.float32)
    for i, bn in enumerate(bnames):
        b = biomes.get(bn, biomes["minecraft:plains"])
        bcol[i] = [b["grass"], b["foliage"], b["water"]]

    def shade(ids):
        k = np.where(ids == 0xFFFF, n, ids)
        c = col[k].copy()
        t = tint[k]
        bc = bcol[g["biome"]]
        for code in (1, 2, 3):
            m = t == code
            c[m] = c[m] * bc[m, code - 1] / 255
        m = t == 4
        c[m] = c[m] * const[k][m] / 255
        return c, alpha[k]

    rgb, _ = shade(g["base"])
    ov, oa = shade(g["top"])
    has_ov = g["top"] != 0xFFFF
    a = np.where(has_ov, np.clip(oa * 1.6, 0, 1), 0)[..., None]
    rgb = rgb * (1 - a) + ov * a

    # Relief: Licht von Nordwesten
    h = g["height"].astype(np.float32)
    hn = np.roll(h, 1, 0)
    hw = np.roll(h, 1, 1)
    slope = (h - hn) + (h - hw)
    slope[0, :] = slope[:, 0] = 0
    f = 1 + np.clip(slope * 0.05, -0.28, 0.22)
    rgb *= f[..., None]

    # Wasser
    d = g["depth"].astype(np.float32)
    wid = names.index("minecraft:water") if "minecraft:water" in names else None
    if wid is not None:
        wc = col[wid] * bcol[g["biome"]][:, :, 2, :] / 255
        wa = np.where(d > 0, np.clip(0.6 + d * 0.05, 0.6, 0.93), 0)[..., None]
        rgb = rgb * (1 - wa) + wc * wa

    rgba = np.zeros((512, 512, 4), np.uint8)
    rgba[..., :3] = np.clip(rgb, 0, 255)
    rgba[..., 3] = np.where(g["base"] == 0xFFFF, 0, 255)
    return rgba


def work(path):
    g = render_region(path)
    if g is None:
        return None
    rgba = colorize(g, g["names"], g["bnames"])
    return g, rgba


def main():
    files = sorted(glob.glob(os.path.join(WORLD, "dimensions", "minecraft", "overworld", "region", "*.mca"))
                   or glob.glob(os.path.join(WORLD, "region", "*.mca")))
    if not files:
        sys.exit(f"Keine Region-Dateien in {WORLD}")
    os.makedirs(OUT, exist_ok=True)
    nid, bnid = {}, {}
    tiles = []
    imgs = {}
    procs = max(1, min(len(files), (os.cpu_count() or 2) - 1))
    with Pool(procs) as pool:
        for k, res in enumerate(pool.imap_unordered(work, files)):
            print(f"\rRegion {k + 1}/{len(files)}", end="", flush=True)
            if res is None:
                continue
            g, rgba = res
            # lokale IDs -> globale IDs
            m = np.array([nid.setdefault(n, len(nid)) for n in g["names"]] + [0xFFFF], np.int64)
            mb = np.array([bnid.setdefault(n, len(bnid)) for n in g["bnames"]] or [0], np.int64)
            remap = lambda a: m[np.where(a == 0xFFFF, len(m) - 1, a)].astype(np.uint16)
            base, top = remap(g["base"]), remap(g["top"])
            bio = mb[g["biome"]].astype(np.uint8)
            stem = f"r.{g['rx']}.{g['rz']}"
            Image.fromarray(rgba).save(os.path.join(OUT, stem + ".webp"), lossless=True, quality=80, method=4)
            blob = base.tobytes() + top.tobytes() + g["height"].tobytes() + g["surf"].tobytes() \
                + g["depth"].tobytes() + bio.tobytes()
            with open(os.path.join(OUT, stem + ".bin"), "wb") as f:
                f.write(gzip.compress(blob, 6))
            tiles.append([g["rx"], g["rz"]])
            imgs[(g["rx"], g["rz"])] = rgba
    print()
    xs, zs = [t[0] for t in tiles], [t[1] for t in tiles]
    x0, z0 = min(xs), min(zs)
    W, H = (max(xs) - x0 + 1) * 512, (max(zs) - z0 + 1) * 512
    big = Image.new("RGBA", (W, H))
    for (rx, rz), a in imgs.items():
        big.paste(Image.fromarray(a), ((rx - x0) * 512, (rz - z0) * 512))
    step = 3
    ov = big.resize((W // step, H // step), Image.Resampling.BOX)
    ov.save(os.path.join(OUT, "overview.webp"), quality=88)
    pretty = lambda n: n.split(":", 1)[-1].replace("_", " ").title()
    json.dump({"tiles": tiles, "tileSize": 512,
               "overview": {"x": x0 * 512, "z": z0 * 512, "size": [W, H], "step": step},
               "blocks": [pretty(n) for n in nid], "biomes": [pretty(n) for n in bnid],
               "layout": "base:u16,top:u16,height:i16,surf:i16,depth:u8,biome:u8 (je 512x512, little endian, z-major)"},
              open(os.path.join(OUT, "index.json"), "w"))
    print(f"{len(tiles)} Kacheln -> {os.path.normpath(OUT)}")


if __name__ == "__main__":
    main()
