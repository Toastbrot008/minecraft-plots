#!/usr/bin/env python3
"""EL-SMP Gebiete – Backend, nur Python-Standardbibliothek.

Start:  python3 server.py            (Port 8080)
        python3 server.py 9000       (anderer Port)
Admin:  Passwort steht in admin_token.txt (oder ADMIN_TOKEN=geheim python3 server.py).
        Einmal  http://server:8080/?admin=geheim  öffnen – der Browser merkt es sich.

Daten liegen in data.json, hochgeladene Karten in uploads/.

Accounts, zwei Modi:
- Ohne config.json (Standard): Jeder Browser erzeugt ein zufälliges Geheimnis und schickt
  es als X-Account. Die Account-ID ist ein HMAC davon.
- Mit Discord (config.json, siehe config.example.json): Login über Discord, Sitzung als
  signiertes Cookie. Account-ID = HMAC der Discord-ID. Alte Browser-Gebiete werden beim
  ersten Discord-Login übernommen (/api/link). Admins: Discord-IDs in "admins".
"""
import base64
import datetime
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
DATA = os.path.join(ROOT, "data.json")
UPLOADS = os.path.join(ROOT, "uploads")
OLD_CLAIMS = os.path.join(ROOT, "claims.json")      # Format der alten Rechteck-Plots
TOKEN_FILE = os.path.join(ROOT, "admin_token.txt")
CONFIG_FILE = os.path.join(ROOT, "config.json")
SESSION_DAYS = 30
DISCORD_AUTHORIZE = os.environ.get("DISCORD_AUTHORIZE", "https://discord.com/oauth2/authorize")
DISCORD_API = os.environ.get("DISCORD_API", "https://discord.com/api/v10")


def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}
    d = cfg.get("discord") or {}
    cfg["discord"] = {"client_id": os.environ.get("DISCORD_CLIENT_ID", d.get("client_id", "")),
                      "client_secret": os.environ.get("DISCORD_CLIENT_SECRET", d.get("client_secret", ""))}
    cfg["public_url"] = os.environ.get("PUBLIC_URL", cfg.get("public_url", "")).rstrip("/")
    cfg["admins"] = {str(a) for a in cfg.get("admins", [])}
    return cfg


CONFIG = load_config()
DISCORD = bool(CONFIG["discord"]["client_id"] and CONFIG["discord"]["client_secret"])


def admin_token():
    t = os.environ.get("ADMIN_TOKEN")
    if t:
        return t
    try:
        return open(TOKEN_FILE).read().strip() or None
    except FileNotFoundError:
        return None

DEFAULTS = {
    "title": "EL-SMP Season 5",
    "subtitle": "Zieht die Grenzen eures Gebiets, bevor der Server startet.",
    "start": "2026-12-27T00:00", "open": True, "deadline": "", "requireApproval": False,
    "minPlayers": 1, "maxPlayers": 8, "baseBorder": 200, "perPlayerBorder": 80, "minGap": 10,
    "spawnX": 0, "spawnZ": 0, "spawnR": 150,
    "minX": -1500, "maxX": 1500, "minZ": -1500, "maxZ": 1500,
    "mapAssetId": "",
}
NUMERIC = {"minPlayers", "maxPlayers", "baseBorder", "perPlayerBorder", "minGap",
           "spawnX", "spawnZ", "spawnR", "minX", "maxX", "minZ", "maxZ"}
COLORS = ["#E8553E", "#F29C38", "#F5D547", "#8BC34A", "#2FA36B", "#36B6C9",
          "#4D7CE0", "#8E5CD6", "#D965B0", "#F4A6C4", "#A0663E", "#F2F2F2"]
MAX_POINTS = 60
CLAIM_FIELDS = {"team", "players", "color", "members", "note", "pts", "x", "z", "status",
                "createdAt", "updatedAt"}

lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, code, msg, status=400):
        super().__init__(msg)
        self.code, self.msg, self.status = code, msg, status


# ------------------------------------------------------------------ Speicher
def load():
    try:
        with open(DATA, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return migrate()


def save(db):
    db["version"] = db.get("version", 0) + 1
    tmp = DATA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA)


def migrate():
    """Erster Start: neues Format anlegen und alte Rechteck-Plots übernehmen."""
    db = {"version": 1, "key": secrets.token_hex(32), "settings": dict(DEFAULTS), "claims": {}}
    try:
        old = json.load(open(OLD_CLAIMS, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        old = []
    used = set()
    for o in old:
        x1, z1, x2, z2 = o["x1"], o["z1"], o["x2"], o["z2"]
        pts = [{"x": x1, "z": z1}, {"x": x2, "z": z1}, {"x": x2, "z": z2}, {"x": x1, "z": z2}]
        per = 2 * ((x2 - x1) + (z2 - z1))
        s = DEFAULTS
        players = max(s["minPlayers"], math.ceil((per - s["baseBorder"]) / s["perPlayerBorder"]))
        color = next((c for c in COLORS if c not in used), COLORS[0])
        used.add(color)
        uid = account_id(db, o["token"])          # alter Token im Browser = neues Account-Geheimnis
        db["claims"][uid] = {
            "team": o.get("name") or o["owner"], "players": min(players, s["maxPlayers"]),
            "color": color, "members": o["owner"], "note": "", "pts": pts,
            "x": (x1 + x2) // 2, "z": (z1 + z2) // 2, "status": "approved",
            "createdAt": o.get("created", time.time()) * 1000, "updatedAt": int(time.time() * 1000),
        }
    save(db)
    if old:
        print(f"{len(old)} alte Plots aus claims.json übernommen")
    return db


def account_id(db, secret):
    return hmac.new(db["key"].encode(), secret.encode(), hashlib.sha256).hexdigest()[:16]


# ------------------------------------------------------------------ Discord-Login + Sitzungen
def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(db, payload):
    return b64(hmac.new(db["key"].encode(), b"session:" + payload.encode(), hashlib.sha256).digest())


def make_session(db, uid, did):
    payload = b64(json.dumps({"u": uid, "d": did, "e": int(time.time()) + SESSION_DAYS * 86400}).encode())
    return payload + "." + sign(db, payload)


def read_session(db, value):
    try:
        payload, sig = value.split(".", 1)
        if not hmac.compare_digest(sig, sign(db, payload)):
            return None
        s = json.loads(unb64(payload))
        return s if s["e"] > time.time() else None
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def discord_request(url, data=None, token=None):
    headers = {"User-Agent": "EL-SMP-Gebiete (https://github.com/Toastbrot008/minecraft-plots, 1.0)",
               "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, body, headers), timeout=15) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        print("Discord-Anfrage fehlgeschlagen:", url, e, file=sys.stderr)
        return None


# ------------------------------------------------------------------ Geometrie (wie in index.html)
def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def perim(p):
    return sum(dist(p[i - 1], p[i]) for i in range(1, len(p))) + (dist(p[-1], p[0]) if len(p) > 2 else 0)


def area(p):
    return abs(sum(p[i][0] * p[(i + 1) % len(p)][1] - p[(i + 1) % len(p)][0] * p[i][1]
                   for i in range(len(p))) / 2)


def in_poly(pt, p):
    inside, j = False, len(p) - 1
    for i in range(len(p)):
        a, b = p[i], p[j]
        if (a[1] > pt[1]) != (b[1] > pt[1]) and pt[0] < (b[0] - a[0]) * (pt[1] - a[1]) / (b[1] - a[1]) + a[0]:
            inside = not inside
        j = i
    return inside


def orient(a, b, c):
    v = (b[1] - a[1]) * (c[0] - b[0]) - (b[0] - a[0]) * (c[1] - b[1])
    return 0 if abs(v) < 1e-9 else 1 if v > 0 else 2


def on_seg(a, b, c):
    return min(a[0], c[0]) <= b[0] <= max(a[0], c[0]) and min(a[1], c[1]) <= b[1] <= max(a[1], c[1])


def seg_inter(a, b, c, d):
    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    if o1 != o2 and o3 != o4:
        return True
    return ((o1 == 0 and on_seg(a, c, b)) or (o2 == 0 and on_seg(a, d, b)) or
            (o3 == 0 and on_seg(c, a, d)) or (o4 == 0 and on_seg(c, b, d)))


def pt_seg(p, a, b):
    dx, dz = b[0] - a[0], b[1] - a[1]
    ln = dx * dx + dz * dz
    t = max(0, min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dz) / ln)) if ln else 0
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dz)


def poly_dist(P, Q):
    if in_poly(P[0], Q) or in_poly(Q[0], P):
        return 0
    m = math.inf
    for i in range(len(P)):
        a, b = P[i], P[(i + 1) % len(P)]
        for j in range(len(Q)):
            c, d = Q[j], Q[(j + 1) % len(Q)]
            if seg_inter(a, b, c, d):
                return 0
            m = min(m, pt_seg(a, c, d), pt_seg(b, c, d), pt_seg(c, a, b), pt_seg(d, a, b))
    return m


def self_intersects(p):
    n = len(p)
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue
            if seg_inter(p[i], p[(i + 1) % n], p[j], p[(j + 1) % n]):
                return True
    return False


def spawn_dist(p, st):
    s = (st["spawnX"], st["spawnZ"])
    if in_poly(s, p):
        return 0
    return min(pt_seg(s, p[i], p[(i + 1) % len(p)]) for i in range(len(p)))


def check_poly(p, st, others):
    if len(p) < 3:
        return "Eine Grenze braucht mindestens 3 Punkte."
    if any(x < st["minX"] or x > st["maxX"] or z < st["minZ"] or z > st["maxZ"] for x, z in p):
        return "Die Grenze ragt über den Kartenrand."
    if self_intersects(p):
        return "Die Grenze kreuzt sich selbst."
    if area(p) < 100:
        return "Das Gebiet ist zu klein."
    if spawn_dist(p, st) < st["spawnR"]:
        return "Zu nah am Spawn. Der Bereich rund um den Spawn bleibt frei."
    for c in others:
        q = [(o["x"], o["z"]) for o in c.get("pts", [])]
        if len(q) >= 3 and poly_dist(p, q) < st["minGap"]:
            return f"Überschneidet sich mit „{c['team']}“ oder liegt näher als {st['minGap']} Blöcke dran."
    return None


# ------------------------------------------------------------------ Regeln
def claims_open(st):
    if not st.get("open"):
        return False
    if st.get("deadline"):
        try:
            if datetime.datetime.fromisoformat(st["deadline"]) < datetime.datetime.now():
                return False
        except ValueError:
            pass
    return True


def clean_pts(raw):
    if not isinstance(raw, list) or not 3 <= len(raw) <= MAX_POINTS:
        raise ApiError("invalid", f"Eine Grenze braucht 3 bis {MAX_POINTS} Punkte.")
    try:
        return [{"x": int(round(float(p["x"]))), "z": int(round(float(p["z"])))} for p in raw]
    except (KeyError, TypeError, ValueError):
        raise ApiError("invalid", "Ungültige Grenzpunkte.")


def clean_claim(d):
    out = {}
    for k, v in d.items():
        if k not in CLAIM_FIELDS:
            continue
        if k == "pts":
            v = clean_pts(v)
        elif k in ("team", "members", "note"):
            v = str(v).strip()[:32 if k == "team" else 200]
        elif k in ("players", "x", "z", "createdAt", "updatedAt"):
            try:
                v = int(float(v))
            except (TypeError, ValueError):
                raise ApiError("invalid", f"Ungültiger Wert für {k}.")
        elif k == "color" and v not in COLORS:
            raise ApiError("invalid", "Unbekannte Teamfarbe.")
        elif k == "status" and v not in ("approved", "pending"):
            raise ApiError("invalid", "Ungültiger Status.")
        out[k] = v
    return out


def write_claim(db, uid, cid, body, merge, admin):
    """set (merge=False) oder update (merge=True) von claims/<cid>, mit Prüfung."""
    st, claims = db["settings"], db["claims"]
    if not admin and uid != cid:
        raise ApiError("forbidden", "Du kannst nur dein eigenes Gebiet ändern.", 403)
    existing = claims.get(cid)
    if merge and existing is None:
        raise ApiError("not_found", "Gebiet existiert nicht.", 404)
    patch = clean_claim(body)
    new = {**existing, **patch} if merge else patch
    now = int(time.time() * 1000)

    if not admin:
        if not claims_open(st):
            raise ApiError("closed", "Das Claimen ist geschlossen.", 403)
        # Status bestimmt der Server, nicht der Browser
        new["status"] = existing["status"] if existing else ("pending" if st["requireApproval"] else "approved")
        new["createdAt"] = existing.get("createdAt", now) if existing else now
        if len(new.get("team", "")) < 2:
            raise ApiError("invalid", "Gib einen Teamnamen mit mindestens 2 Zeichen ein.")
        dup = next((c for k, c in claims.items() if k != cid and c["team"].lower() == new["team"].lower()), None)
        if dup:
            raise ApiError("invalid", f"Den Teamnamen „{dup['team']}“ gibt es schon.")
        if not st["minPlayers"] <= new.get("players", 0) <= st["maxPlayers"]:
            raise ApiError("invalid", f"Erlaubt sind {st['minPlayers']} bis {st['maxPlayers']} Spieler pro Team.")
        if new.get("color") not in COLORS:
            raise ApiError("invalid", "Bitte eine Teamfarbe wählen.")
        pts = [(p["x"], p["z"]) for p in new.get("pts", [])]
        err = check_poly(pts, st, [c for k, c in claims.items() if k != cid])
        if err:
            raise ApiError("invalid", err)
        budget = st["baseBorder"] + st["perPlayerBorder"] * new["players"]
        if perim(pts) > budget + 0.5:
            raise ApiError("invalid", f"Die Grenze ist {perim(pts) - budget:.0f} Blöcke zu lang.")
        new["x"] = round(sum(p[0] for p in pts) / len(pts))
        new["z"] = round(sum(p[1] for p in pts) / len(pts))
    elif not new.get("pts"):
        raise ApiError("invalid", "Gebiet ohne Grenzpunkte.")
    new["updatedAt"] = now
    claims[cid] = new


def write_settings(body):
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k not in body:
            continue
        v = body[k]
        if k in NUMERIC:
            try:
                v = int(float(v))
            except (TypeError, ValueError):
                raise ApiError("invalid", f"Ungültiger Wert für {k}.")
        elif isinstance(DEFAULTS[k], bool):
            v = bool(v)
        else:
            v = str(v)[:200]
        out[k] = v
    if out["maxX"] <= out["minX"] or out["maxZ"] <= out["minZ"]:
        raise ApiError("invalid", "Die Kartenränder passen nicht.")
    if not 1 <= out["minPlayers"] <= out["maxPlayers"]:
        raise ApiError("invalid", "Min. und max. Spieler passen nicht zusammen.")
    if out["mapAssetId"] and not re.fullmatch(r"[0-9a-f]{16}\.(png|jpg|webp)", out["mapAssetId"]):
        raise ApiError("invalid", "Unbekannte Karte.")
    return out


# ------------------------------------------------------------------ HTTP
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kw):
        super().__init__(*args, directory=PUBLIC, **kw)

    def log_message(self, fmt, *args):
        if self.command != "GET":
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def end_headers(self):
        # Kacheln dürfen gecacht werden, die Seite selbst nicht (sonst sieht man Updates nicht)
        if self.path.startswith("/terrain/") or self.path.startswith("/_blob/"):
            self.send_header("Cache-Control", "public, max-age=3600")
        elif not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self, limit=64_000):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ApiError("too_large", "Anfrage zu groß.", 413)
        return self.rfile.read(n)

    def cookie(self, name):
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        return c[name].value if name in c else None

    def session(self, db):
        v = self.cookie("elsmp_session")
        return read_session(db, v) if v else None

    def who(self, db):
        expected = admin_token()
        given = self.headers.get("X-Admin", "")
        admin = bool(expected and given and hmac.compare_digest(given, expected))
        if DISCORD:
            s = self.session(db)
            uid = s["u"] if s else None
            admin = admin or bool(s and s["d"] in CONFIG["admins"])
        else:
            secret = self.headers.get("X-Account", "")
            uid = account_id(db, secret) if len(secret) >= 16 else None
        return uid, admin

    def base_url(self):
        return CONFIG["public_url"] or "http://" + self.headers.get("Host", "localhost")

    def set_cookie_header(self, name, value, max_age, path="/"):
        secure = "; Secure" if self.base_url().startswith("https://") else ""
        self.send_header("Set-Cookie", f"{name}={value}; Path={path}; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}")

    def redirect(self, to, cookies=()):
        self.send_response(302)
        self.send_header("Location", to)
        for c in cookies:
            self.set_cookie_header(*c)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def auth_page(self, msg):
        body = (f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                f'<title>Login</title><body style="font-family:sans-serif;padding:24px">'
                f'<p>{msg}</p><p><a href="/">Zurück zur Karte</a></p>').encode()
        self.send_response(400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def auth_start(self):
        if not DISCORD:
            return self.auth_page("Discord-Login ist auf diesem Server nicht eingerichtet.")
        state = secrets.token_urlsafe(16)
        q = urllib.parse.urlencode({"client_id": CONFIG["discord"]["client_id"], "response_type": "code",
                                    "redirect_uri": self.base_url() + "/auth/discord/callback",
                                    "scope": "identify", "state": state, "prompt": "none"})
        self.redirect(DISCORD_AUTHORIZE + "?" + q, [("elsmp_oauth", state, 600, "/auth")])

    def auth_callback(self):
        if not DISCORD:
            return self.auth_page("Discord-Login ist auf diesem Server nicht eingerichtet.")
        q = urllib.parse.parse_qs(self.path.partition("?")[2])
        code, state = q.get("code", [""])[0], q.get("state", [""])[0]
        expected = self.cookie("elsmp_oauth") or ""
        if not code or not expected or not hmac.compare_digest(state, expected):
            return self.auth_page("Login abgebrochen oder abgelaufen. Bitte noch einmal versuchen.")
        tok = discord_request(DISCORD_API + "/oauth2/token", {
            "client_id": CONFIG["discord"]["client_id"], "client_secret": CONFIG["discord"]["client_secret"],
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": self.base_url() + "/auth/discord/callback"})
        user = tok and tok.get("access_token") and discord_request(DISCORD_API + "/users/@me", token=tok["access_token"])
        if not user or "id" not in user:
            return self.auth_page("Discord hat den Login nicht bestätigt. Bitte noch einmal versuchen.")
        did = str(user["id"])
        name = str(user.get("global_name") or user.get("username") or "?")[:40]
        with lock:
            db = load()
            uid = account_id(db, "discord:" + did)
            db.setdefault("users", {})[uid] = {"name": name, "discord": did, "login": int(time.time())}
            save(db)
            cookie = make_session(db, uid, did)
        self.redirect("/", [("elsmp_session", cookie, SESSION_DAYS * 86400), ("elsmp_oauth", "", 0, "/auth")])

    def auth_logout(self):
        self.redirect("/", [("elsmp_session", "", 0)])

    def api(self, fn):
        try:
            fn()
        except ApiError as e:
            self.send_json(e.status, {"code": e.code, "error": e.msg})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            self.send_json(400, {"code": "invalid", "error": "Ungültige Daten."})

    # --- GET
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/state":
            return self.api(self.get_state)
        if path == "/api/me":
            return self.api(self.get_me)
        if path == "/auth/discord":
            return self.auth_start()
        if path == "/auth/discord/callback":
            return self.auth_callback()
        if path == "/auth/logout":
            return self.auth_logout()
        if path.startswith("/_blob/"):
            return self.get_blob(path[7:])
        return super().do_GET()

    def get_state(self):
        q = dict(p.split("=", 1) for p in self.path.partition("?")[2].split("&") if "=" in p)
        with lock:
            db = load()
        if q.get("v") == str(db["version"]):
            return self.send_json(200, {"version": db["version"], "unchanged": True})
        users = db.get("users", {})
        names = {k: users[k]["name"] for k in db["claims"] if k in users}
        self.send_json(200, {"version": db["version"], "settings": db["settings"], "claims": db["claims"],
                             "names": names})

    def get_me(self):
        with lock:
            db = load()
        uid, admin = self.who(db)
        self.send_json(200, {"id": uid, "isAdmin": admin, "adminConfigured": bool(admin_token()),
                             "auth": "discord" if DISCORD else "browser",
                             "name": db.get("users", {}).get(uid, {}).get("name") if uid else None})

    def get_blob(self, name):
        if not re.fullmatch(r"[0-9a-f]{16}\.(png|jpg|webp)", name):
            return self.send_error(404)
        p = os.path.join(UPLOADS, name)
        if not os.path.isfile(p):
            return self.send_error(404)
        data = open(p, "rb").read()
        self.send_response(200)
        ext = name.rsplit(".", 1)[1]
        self.send_header("Content-Type", {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[ext])
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # --- Schreiben
    def csrf_ok(self):
        # Eigener Header erzwingt bei fremden Seiten einen CORS-Preflight, den wir nie erlauben
        if self.headers.get("X-ELSMP") == "1":
            return True
        self.send_json(403, {"code": "forbidden", "error": "Anfrage abgelehnt."})
        return False

    def do_PUT(self):
        if self.csrf_ok():
            self.api(lambda: self.write(merge=False))

    def do_PATCH(self):
        if self.csrf_ok():
            self.api(lambda: self.write(merge=True))

    def do_DELETE(self):
        if self.csrf_ok():
            self.api(self.delete)

    def do_POST(self):
        if not self.csrf_ok():
            return
        if self.path == "/api/map":
            return self.api(self.upload_map)
        if self.path == "/api/link":
            return self.api(self.link_legacy)
        self.send_json(404, {"code": "not_found", "error": "Nicht gefunden"})

    def link_legacy(self):
        """Gebiet eines alten Browser-Accounts auf den Discord-Account übertragen."""
        secret = self.headers.get("X-Account", "")
        with lock:
            db = load()
            s = self.session(db) if DISCORD else None
            if not s:
                raise ApiError("forbidden", "Nicht angemeldet.", 403)
            if len(secret) < 16:
                return self.send_json(200, {"moved": False, "reason": "none"})
            old, new = account_id(db, secret), s["u"]
            claims = db["claims"]
            if old not in claims:
                return self.send_json(200, {"moved": False, "reason": "none"})
            if new in claims:
                return self.send_json(200, {"moved": False, "reason": "both"})
            claims[new] = claims.pop(old)
            save(db)
        print(f"Gebiet {old} -> Discord-Account {new} übertragen", file=sys.stderr)
        self.send_json(200, {"moved": True})

    def write(self, merge):
        path = self.path.split("?", 1)[0]
        body = json.loads(self.body() or b"{}")
        if not isinstance(body, dict):
            raise ApiError("invalid", "Ungültige Daten.")
        with lock:
            db = load()
            uid, admin = self.who(db)
            if path == "/api/doc/settings/main":
                if not admin:
                    raise ApiError("forbidden", "Nur Admins.", 403)
                db["settings"] = write_settings({**db["settings"], **body} if merge else body)
            elif m := re.fullmatch(r"/api/doc/claims/([0-9a-f]{16})", path):
                if not uid and not admin:
                    raise ApiError("forbidden", "Kein Account.", 403)
                write_claim(db, uid, m.group(1), body, merge, admin)
            else:
                raise ApiError("not_found", "Nicht gefunden", 404)
            save(db)
            v = db["version"]
        self.send_json(200, {"ok": True, "version": v})

    def delete(self):
        m = re.fullmatch(r"/api/doc/claims/([0-9a-f]{16})", self.path.split("?", 1)[0])
        if not m:
            raise ApiError("not_found", "Nicht gefunden", 404)
        with lock:
            db = load()
            uid, admin = self.who(db)
            cid = m.group(1)
            if not admin and uid != cid:
                raise ApiError("forbidden", "Nur der Ersteller oder ein Admin kann das Gebiet freigeben.", 403)
            if not admin and not claims_open(db["settings"]):
                raise ApiError("closed", "Das Claimen ist geschlossen.", 403)
            db["claims"].pop(cid, None)
            save(db)
            v = db["version"]
        self.send_json(200, {"ok": True, "version": v})

    def upload_map(self):
        with lock:
            db = load()
        _, admin = self.who(db)
        if not admin:
            raise ApiError("forbidden", "Nur Admins.", 403)
        data = self.body(limit=15_000_000)
        ext = ("png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "jpg" if data[:3] == b"\xff\xd8\xff"
               else "webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None)
        if not ext:
            raise ApiError("invalid", "Nur PNG, JPEG oder WebP.")
        name = hashlib.sha256(data).hexdigest()[:16] + "." + ext
        os.makedirs(UPLOADS, exist_ok=True)
        with open(os.path.join(UPLOADS, name), "wb") as f:
            f.write(data)
        self.send_json(200, {"id": name, "url": "/_blob/" + name})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    with lock:
        load()                                   # legt data.json an / migriert claims.json
    if not admin_token():
        with open(TOKEN_FILE, "w") as f:
            f.write(secrets.token_urlsafe(12) + "\n")
        os.chmod(TOKEN_FILE, 0o600)
    print(f"Admin-Login: http://<server>:{port}/?admin={admin_token()}")
    if DISCORD:
        print(f"Discord-Login aktiv, Redirect-URL: {CONFIG['public_url'] or 'http://<host>:' + str(port)}/auth/discord/callback")
        if not CONFIG["admins"]:
            print("Hinweis: keine Discord-Admins in config.json – Admin nur über ?admin=…")
    else:
        print("Login: pro Browser (für Discord-Login config.json anlegen, siehe config.example.json)")
    print(f"Gebiete-Karte läuft auf http://0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
