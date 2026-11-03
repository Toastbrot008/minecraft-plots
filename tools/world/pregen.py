#!/usr/bin/env python3
"""Generiert das Gebiet ±HALF mit einem Vanilla-Server vor (ohne Mods).

Startet server.jar headless, lädt über RCON Blöcke von 16x16 Chunks per
/forceload, wartet bis sie komplett generiert sind (inkl. Bäume) und gibt sie
wieder frei. Danach liegen die Region-Dateien in run/world/region/.

Aufruf: python3 pregen.py [seed] [half] [RAM, z. B. 3G]
"""
import os
import socket
import struct
import subprocess
import sys
import time

SEED = sys.argv[1] if len(sys.argv) > 1 else "702436"
HALF = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
RAM = sys.argv[3] if len(sys.argv) > 3 else "3G"
HERE = os.path.dirname(os.path.abspath(__file__))
RUN = os.path.join(HERE, "run")
PORT, PW = 25599, "pregen"
BATCH = 14  # Chunks pro Seite; +1 Rand ringsum = 16x16 (forceload erlaubt max. 256)
STALL = 90  # Sekunden ohne Fortschritt -> Chunk überspringen statt ewig warten


class Rcon:
    def __init__(self):
        self.s = socket.create_connection(("127.0.0.1", PORT), timeout=120)
        self.rid = 0
        self._send(3, PW)

    def _send(self, kind, body):
        self.rid += 1
        data = struct.pack("<ii", self.rid, kind) + body.encode() + b"\0\0"
        self.s.sendall(struct.pack("<i", len(data)) + data)
        n = struct.unpack("<i", self._read(4))[0]
        resp = self._read(n)
        rid = struct.unpack("<i", resp[:4])[0]
        if rid == -1:
            raise RuntimeError("RCON-Login fehlgeschlagen")
        return resp[8:-2].decode(errors="replace")

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            part = self.s.recv(n - len(buf))
            if not part:
                raise ConnectionError("RCON getrennt")
            buf += part
        return buf

    def cmd(self, c):
        return self._send(2, c)


def setup():
    os.makedirs(RUN, exist_ok=True)
    with open(os.path.join(RUN, "eula.txt"), "w") as f:
        f.write("eula=true\n")
    props = {
        "level-seed": SEED, "enable-rcon": "true", "rcon.port": str(PORT),
        "rcon.password": PW, "server-port": "25598", "online-mode": "false",
        "view-distance": "4", "simulation-distance": "4", "spawn-monsters": "false",
        "spawn-animals": "false", "sync-chunk-writes": "false", "server-ip": "127.0.0.1",
        "max-tick-time": "-1", "pause-when-empty-seconds": "-1",  # sonst pausiert der Server ohne Spieler
    }
    with open(os.path.join(RUN, "server.properties"), "w") as f:
        f.writelines(f"{k}={v}\n" for k, v in props.items())


def main():
    setup()
    log = open(os.path.join(RUN, "pregen-server.log"), "w")
    proc = subprocess.Popen(
        ["java", f"-Xmx{RAM}", "-Xms1G", "-jar", os.path.join(HERE, "server.jar"), "nogui"],
        cwd=RUN, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT)
    print("Server startet …", flush=True)
    rc = None
    for _ in range(600):
        if proc.poll() is not None:
            sys.exit("Server ist abgestürzt – siehe run/pregen-server.log")
        try:
            rc = Rcon()
            break
        except OSError:
            time.sleep(1)
    if rc is None:
        sys.exit("Keine RCON-Verbindung")

    c0, c1 = (-HALF) >> 4, (HALF - 1) >> 4
    batches = [(cx, cz) for cz in range(c0, c1 + 1, BATCH) for cx in range(c0, c1 + 1, BATCH)]
    total = (c1 - c0 + 1) ** 2
    t0 = time.time()
    done, skipped = 0, []
    try:
        for i, (bx, bz) in enumerate(batches):
            ex, ez = min(bx + BATCH - 1, c1), min(bz + BATCH - 1, c1)
            # Ab 26.x gilt ein Chunk erst als "loaded", wenn seine Nachbarn auch geladen
            # sind -> 1 Chunk Rand mitladen, aber nur auf das Innere warten.
            rc.cmd(f"forceload add {(bx - 1) * 16} {(bz - 1) * 16} {(ex + 1) * 16} {(ez + 1) * 16}")
            pending = [(x, z) for x in range(bx, ex + 1) for z in range(bz, ez + 1)]
            n, last = len(pending), time.time()
            while pending:
                time.sleep(0.5)
                left = [(x, z) for x, z in pending
                        if "passed" not in rc.cmd(f"execute if loaded {x * 16} 0 {z * 16}")]
                if len(left) < len(pending):
                    last = time.time()
                elif time.time() - last > STALL:
                    skipped += left
                    print(f"\n  {len(left)} Chunks hängen, überspringe: {left[:5]} …", flush=True)
                    break
                pending = left
            rc.cmd("forceload remove all")
            done += n
            el = time.time() - t0
            print(f"\r{i + 1}/{len(batches)} Blöcke · ~{done / el:.0f} Chunks/s · "
                  f"Rest ~{(total - done) / (done / el) / 60:.1f} min   ", end="", flush=True)
        print("\nSpeichere …", flush=True)
        if skipped:
            print(f"Achtung: {len(skipped)} Chunks evtl. unvollständig (erscheinen als Lücke).")
        rc.cmd("save-all flush")
    finally:
        try:
            rc.cmd("stop")
        except Exception:
            proc.terminate()
        proc.wait()
    print("Fertig:", os.path.join(RUN, "world"))


if __name__ == "__main__":
    main()
