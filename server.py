#!/usr/bin/env python3
"""EL-SMP Gebiete – Backend, nur Python-Standardbibliothek.

Start:  python3 server.py            (Port 8080)
        python3 server.py 9000       (anderer Port)
Admin:  Beim ersten Start wird das Konto "admin" angelegt und sein Passwort einmal in der
        Konsole ausgegeben (oder ADMIN_USER=… ADMIN_PASSWORD=… python3 server.py).
        Passwort vergessen:  python3 server.py --reset-admin

Daten liegen in data.json, hochgeladene Karten in uploads/.

Konten: Spieler melden sich über Discord an (OAuth2). Beim ersten Login entsteht automatisch ein
Konto, das über die Discord-ID fest zugeordnet bleibt. Der Admin meldet sich mit Name + Passwort an
(PBKDF2-Hash). Ohne Anmeldung liefert der Server nur die Anmeldeseite aus.
Discord einrichten: DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_REDIRECT_URI (siehe README).
"""
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
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
DATA = os.path.join(ROOT, "data.json")
UPLOADS = os.path.join(ROOT, "uploads")
OLD_CLAIMS = os.path.join(ROOT, "claims.json")      # Format der alten Rechteck-Plots
CONFIG_FILE = os.path.join(ROOT, "config.json")     # Discord-Zugang (optional, nicht im Repo)
TOKEN_FILE = os.path.join(ROOT, "admin_token.txt")  # altes Admin-Passwort, wird beim ersten Start übernommen
CALLBACK_PATH = "/auth/discord/callback"            # so in der Discord-App eingetragen

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
PW_ITERATIONS = 300_000
SESSION_DAYS = 30
COOKIE = "elsmp_session"
OPEN_PATHS = {"/", "/index.html", "/backend.js", "/api/me", "/favicon.ico",     # ohne Anmeldung erreichbar
              "/api/discord/login", "/api/discord/callback", CALLBACK_PATH,
              "/api/register", "/api/login"}
DISCORD_API = "https://discord.com/api/v10"
DISCORD_AUTHORIZE = "https://discord.com/oauth2/authorize"
STATE_COOKIE = "elsmp_oauth"
MAX_FAILS, FAIL_WINDOW = 8, 300                     # Fehlversuche pro IP+Name, Sekunden

lock = threading.Lock()
fails, fails_lock = {}, threading.Lock()


class ApiError(Exception):
    def __init__(self, code, msg, status=400):
        super().__init__(msg)
        self.code, self.msg, self.status = code, msg, status


# ------------------------------------------------------------------ Speicher
def load():
    try:
        with open(DATA, encoding="utf-8") as f:
            return upgrade(json.load(f))
    except FileNotFoundError:
        return migrate()


def upgrade(db):
    """Daten aus der Version vor den Konten (Browser-/Discord-Konten {name, discord, login})
    ins neue Format bringen. Die Konto-IDs bleiben gleich, damit Gebiete ihren Besitzer behalten."""
    db.setdefault("users", {})
    for uid, u in db["users"].items():
        if "role" in u:
            continue
        did = u.pop("discord", None)
        login = u.pop("login", None)
        u.update({"pw": None, "role": "player", "active": True, "gen": 0, "mustChange": False,
                  "createdAt": int((login or time.time()) * 1000), "uuid": "",
                  "discordId": str(did) if did else None, "paid": True, "listed": True})
    for c in db.get("claims", {}).values():
        c.setdefault("inviteIds", [])
        c.setdefault("invited", "")
    return db


def save(db):
    db["version"] = db.get("version", 0) + 1
    tmp = DATA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA)


def migrate():
    """Erster Start: neues Format anlegen und alte Rechteck-Plots übernehmen."""
    db = {"version": 1, "key": secrets.token_hex(32), "settings": dict(DEFAULTS), "claims": {}, "users": {}}
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


# ------------------------------------------------------------------ Konten
def hash_pw(pw, salt=None, iters=PW_ITERATIONS):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iters)
    return f"{iters}${salt.hex()}${h.hex()}"


def check_pw(pw, stored):
    iters, salt, _ = stored.split("$")
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(salt), int(iters)), stored)


DUMMY_HASH = hash_pw(secrets.token_hex(8))          # gleiche Laufzeit, wenn es den Namen nicht gibt


def new_password():
    return secrets.token_urlsafe(9)


def clean_password(pw):
    if not isinstance(pw, str) or not 8 <= len(pw) <= 128:
        raise ApiError("invalid", "Das Passwort braucht 8 bis 128 Zeichen.")
    return pw


def clean_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]{3,16}", name.strip()):
        raise ApiError("invalid", "Der Name braucht 3 bis 16 Zeichen: Buchstaben, Zahlen und _ (wie in Minecraft).")
    return name.strip()


def find_user(db, name):
    for uid, u in db["users"].items():
        if u["name"].lower() == name.lower():
            return uid, u
    return None, None


def add_user(db, name, pw_hash=None, role="player", must_change=False, **extra):
    uid = secrets.token_hex(8)
    db["users"][uid] = {"name": name, "pw": pw_hash, "role": role, "active": True, "gen": 0,
                        "mustChange": must_change, "createdAt": int(time.time() * 1000),
                        "uuid": "", "discordId": None,
                        "paid": True, "listed": True, **extra}
    return uid


def public_user(uid, u):
    return {"id": uid, "name": u["name"], "role": u["role"], "active": u["active"], "paid": u["paid"],
            "listed": u["listed"], "mustChange": u["mustChange"], "createdAt": u["createdAt"]}


def session_token(db, uid):
    payload = f"{uid}.{int(time.time()) + SESSION_DAYS * 86400}.{db['users'][uid]['gen']}"
    sig = hmac.new(db["key"].encode(), ("session:" + payload).encode(), hashlib.sha256).hexdigest()
    return payload + "." + sig


def session_user(db, token):
    """Gibt (uid, Konto) zurück, wenn das Cookie echt, nicht abgelaufen und noch gültig ist."""
    parts = token.split(".")
    if len(parts) != 4:
        return None, None
    uid, exp, gen, sig = parts
    payload = f"{uid}.{exp}.{gen}"
    good = hmac.new(db["key"].encode(), ("session:" + payload).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig.encode(), good.encode()):
        return None, None
    u = db["users"].get(uid)
    if not u or not u["active"] or not u["listed"] or str(u["gen"]) != gen or int(exp) < time.time():
        return None, None
    return uid, u


def too_many_fails(key):
    with fails_lock:
        e = fails.get(key)
        return bool(e and e[0] >= MAX_FAILS and time.time() - e[1] < FAIL_WINDOW)


def note_fail(key):
    now = time.time()
    with fails_lock:
        for k in [k for k, e in fails.items() if now - e[1] >= FAIL_WINDOW]:
            del fails[k]
        e = fails.setdefault(key, [0, now])
        e[0] += 1


# ------------------------------------------------------------------ Discord-Anmeldung
def file_config():
    """config.json (aus der Version davor): {"public_url", "discord": {client_id, client_secret}, "admins"}."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


CONFIG = file_config()
DISCORD_ADMINS = {str(a) for a in CONFIG.get("admins", [])}   # Discord-IDs, die Admin-Rechte bekommen


def discord_config(host=None):
    """Umgebungsvariablen haben Vorrang, sonst config.json. Ohne public_url wird die Adresse aus
    dem Host-Header gebaut (wie in der alten Version)."""
    d = CONFIG.get("discord", {})
    cid = os.environ.get("DISCORD_CLIENT_ID") or d.get("client_id")
    secret = os.environ.get("DISCORD_CLIENT_SECRET") or d.get("client_secret")
    redirect = os.environ.get("DISCORD_REDIRECT_URI")
    if not redirect:
        base = (os.environ.get("PUBLIC_URL") or CONFIG.get("public_url") or "").rstrip("/")
        if not base and host:
            base = "http://" + host
        redirect = base + CALLBACK_PATH if base else None
    return (cid, secret, redirect) if cid and secret and redirect else None


def discord_enabled():
    d = CONFIG.get("discord", {})
    return bool((os.environ.get("DISCORD_CLIENT_ID") or d.get("client_id")) and
                (os.environ.get("DISCORD_CLIENT_SECRET") or d.get("client_secret")))


def discord_request(url, data=None, token=None):
    # Ohne eigenen User-Agent blockt Discord die Anfrage
    headers = {"User-Agent": "DiscordBot (el-smp-gebiete, 1.0)", "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = urllib.parse.urlencode(data).encode() if data else None
    with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=10) as f:
        return json.load(f)


def discord_profile(code, cfg):
    """Tauscht den Code gegen das Discord-Profil. Das Token wird danach nicht mehr gebraucht."""
    cid, secret, redirect = cfg
    token = discord_request(DISCORD_API + "/oauth2/token", {"client_id": cid, "client_secret": secret, "code": code,
                                                            "grant_type": "authorization_code", "redirect_uri": redirect})
    me = discord_request(DISCORD_API + "/users/@me", token=token["access_token"])
    return str(me["id"]), str(me["username"])


def discord_account(db, did, username):
    """Findet das Konto zur Discord-ID oder legt es beim ersten Login an."""
    other = find_user(db, username)[0]
    for uid, u in db["users"].items():
        if u["discordId"] == did:
            if not other:                        # Name bei Discord geändert: hier mitziehen
                u["name"] = username
            return uid, u
    name = f"{username}_{did[-4:]}" if other else username
    uid = add_user(db, name, discordId=did)
    return uid, db["users"][uid]


def ensure_admin(reset=False):
    """Legt beim ersten Start das Admin-Konto an; reset=True vergibt ein neues Passwort."""
    with lock:
        db = load()
        db.setdefault("users", {})
        uid = next((k for k, u in db["users"].items() if u["role"] == "admin"), None)
        if uid and not reset:
            return
        given = os.environ.get("ADMIN_PASSWORD")
        if not given and not uid and os.path.isfile(TOKEN_FILE):
            given = open(TOKEN_FILE, encoding="utf-8").read().strip() or None
            if given:
                print("Admin-Passwort aus admin_token.txt übernommen")
        pw = given or new_password()
        if uid:
            u = db["users"][uid]
            u.update(pw=hash_pw(pw), gen=u["gen"] + 1, active=True, mustChange=not given)
        else:
            name = clean_name(os.environ.get("ADMIN_USER", "admin"))
            uid = add_user(db, name, hash_pw(pw), role="admin", must_change=not given)
        save(db)
        print(f"Admin-Konto: {db['users'][uid]['name']}")
        if not given:
            print(f"Passwort (wird nur jetzt angezeigt): {pw}")


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


# ------------------------------------------------------------------ Teams
def member_ids(cid, c):
    """Konten eines Gebiets. Der Schlüssel des Gebiets (cid) ist das Konto, das es erstellt hat."""
    return c.get("memberIds") or [cid]


def team_of(db, uid):
    for cid, c in db["claims"].items():
        if uid in member_ids(cid, c):
            return cid, c
    return None, None


def names_of(db, ids):
    return ", ".join(db["users"][i]["name"] for i in ids if i in db["users"] and db["users"][i]["role"] != "admin")


def set_members(db, c, ids, invites=None):
    """memberIds = im Team (zählen für Spielerzahl und Grenzlänge), inviteIds = eingeladen, noch nicht angenommen."""
    c["memberIds"] = ids
    c["members"] = names_of(db, ids)
    inv = [i for i in (c.get("inviteIds", []) if invites is None else invites) if i not in ids]
    c["inviteIds"] = inv
    c["invited"] = names_of(db, inv)


def resolve_members(db, cid, text, editor, current=()):
    """Macht aus den eingetragenen Namen Konten. Ersteller und Bearbeiter sind immer dabei.
    Wer neu eingetragen wird, ist erst eingeladen und muss selbst annehmen (editor=None: Admin trägt direkt ein).
    Gibt (Mitglieder, Eingeladene) zurück. Niemand darf in zwei Teams stehen."""
    ids, invites = [], []
    for uid in (cid, editor):
        u = db["users"].get(uid)
        if u and u["role"] != "admin" and uid not in ids:
            ids.append(uid)
    wanted = []
    for raw in text.split(","):
        name = raw.strip()
        if not name:
            continue
        uid, u = find_user(db, name)
        if not u or u["role"] == "admin":
            raise ApiError("invalid", f"„{name}“ hat sich noch nie angemeldet.")
        if uid not in wanted:
            wanted.append(uid)
    for uid in wanted:
        if uid in ids:
            continue
        (ids if editor is None or uid in current else invites).append(uid)
    for uid in ids + invites:
        other, team = team_of(db, uid)
        if other and other != cid:
            raise ApiError("invalid", f"„{db['users'][uid]['name']}“ ist schon im Team „{team['team']}“.")
    return ids, invites


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
    existing = claims.get(cid)
    if not admin:
        if uid not in (member_ids(cid, existing) if existing else [cid]):
            raise ApiError("forbidden", "Du kannst nur dein eigenes Gebiet ändern.", 403)
        other, team = team_of(db, uid)
        if other and other != cid:
            raise ApiError("invalid", f"Du bist schon im Team „{team['team']}“ und kannst kein eigenes Gebiet erstellen.")
    if merge and existing is None:
        raise ApiError("not_found", "Gebiet existiert nicht.", 404)
    patch = clean_claim(body)
    new = {**existing, **patch} if merge else patch
    now = int(time.time() * 1000)
    old_ids = member_ids(cid, existing) if existing else []
    ids = old_ids
    if "members" in patch or not existing:
        ids, invites = resolve_members(db, cid, patch.get("members", ""), None if admin else uid, old_ids)
        set_members(db, new, ids, invites)

    if not admin:
        if not db["users"][uid]["paid"]:
            raise ApiError("not_paid", "Deine Zahlung ist noch nicht eingetragen. Frag einen Admin.", 403)
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
        # Die Spielerzahl zählt der Server selbst, sie bestimmt auch die erlaubte Grenzlänge
        new["players"] = len(ids)
        # Eingeladene zählen für Min./Max. mit (sonst könnte ein neues Team nie entstehen),
        # für die Grenzlänge aber erst, wenn sie angenommen haben
        total = new["players"] + len(new.get("inviteIds", []))
        if not st["minPlayers"] <= total <= st["maxPlayers"]:
            raise ApiError("invalid", f"Erlaubt sind {st['minPlayers']} bis {st['maxPlayers']} Spieler pro Team "
                                      f"(mit Eingeladenen), ihr wärt {total}.")
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
    elif ids != old_ids and "players" not in patch:
        new["players"] = max(1, len(ids))
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
            self.send_header("Cache-Control", "private, max-age=3600")
        elif not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_json(self, code, obj, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_header("Set-Cookie", self.cookie_header(cookie))
        self.end_headers()
        self.wfile.write(body)

    def body(self, limit=64_000):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ApiError("too_large", "Anfrage zu groß.", 413)
        return self.rfile.read(n)

    def cookie_header(self, value):
        # Leerer Wert = abmelden. Secure nur hinter einem HTTPS-Proxy, sonst käme das Cookie nie zurück
        age = SESSION_DAYS * 86400 if value else 0
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return f"{COOKIE}={value}; Path=/; Max-Age={age}; HttpOnly; SameSite=Strict{secure}"

    def json_body(self):
        body = json.loads(self.body() or b"{}")
        if not isinstance(body, dict):
            raise ApiError("invalid", "Ungültige Daten.")
        return body

    def cookie(self, name):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def who(self, db):
        token = self.cookie(COOKIE)
        if token is None:
            return None, False
        uid, u = session_user(db, token)
        return uid, bool(u and (u["role"] == "admin" or (u.get("discordId") and u["discordId"] in DISCORD_ADMINS)))

    def redirect(self, location, *cookies):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        for c in cookies:
            self.send_header("Set-Cookie", c)
        self.end_headers()

    def state_cookie(self, value):
        # Lax statt Strict: Das Cookie muss den Rücksprung von discord.com überstehen
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        age = 600 if value else 0
        return f"{STATE_COOKIE}={value}; Path=/api/discord; Max-Age={age}; HttpOnly; SameSite=Lax{secure}"

    def require_admin(self, db):
        uid, admin = self.who(db)
        if not admin:
            raise ApiError("forbidden", "Nur Admins.", 403)
        return uid

    def api(self, fn):
        try:
            fn()
        except ApiError as e:
            self.send_json(e.status, {"code": e.code, "error": e.msg})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            self.send_json(400, {"code": "invalid", "error": "Ungültige Daten."})

    # --- GET
    def blocked(self, path):
        """Alles außer der Anmeldeseite gibt es nur mit Anmeldung."""
        if path in OPEN_PATHS:
            return False
        with lock:
            db = load()
        return not self.who(db)[0]

    def do_HEAD(self):
        if self.blocked(self.path.split("?", 1)[0]):
            return self.send_error(401)
        return super().do_HEAD()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if self.blocked(path):
            return self.send_json(401, {"code": "login", "error": "Bitte melde dich an."})
        if path == "/api/discord/login":
            return self.discord_login()
        if path in ("/api/discord/callback", CALLBACK_PATH):
            return self.discord_callback()
        if path == "/api/state":
            return self.api(self.get_state)
        if path == "/api/me":
            return self.api(self.get_me)
        if path == "/api/users":
            return self.api(self.get_users)
        if path == "/api/names":
            return self.api(self.get_names)
        if path.startswith("/_blob/"):
            return self.get_blob(path[7:])
        return super().do_GET()

    def get_state(self):
        q = dict(p.split("=", 1) for p in self.path.partition("?")[2].split("&") if "=" in p)
        with lock:
            db = load()
        if q.get("v") == str(db["version"]):
            return self.send_json(200, {"version": db["version"], "unchanged": True})
        self.send_json(200, {"version": db["version"], "settings": db["settings"], "claims": db["claims"]})

    def get_me(self):
        with lock:
            db = load()
        uid, admin = self.who(db)
        u = db["users"].get(uid) if uid else None
        self.send_json(200, {"id": uid, "isAdmin": admin, "name": u["name"] if u else None,
                             "mustChange": bool(u and u["mustChange"]), "paid": bool(u and u["paid"]),
                             "discordEnabled": discord_enabled(),
                             "hasPassword": bool(u and u.get("pw"))})

    def get_users(self):
        with lock:
            db = load()
        self.require_admin(db)
        users = [public_user(k, u) for k, u in db["users"].items()]
        self.send_json(200, {"users": sorted(users, key=lambda u: u["name"].lower())})

    def get_names(self):
        """Minecraft-Namen für die Vorschläge bei „Mitglieder“; alles andere bleibt beim Admin."""
        with lock:
            db = load()
        names = [u["name"] for u in db["users"].values() if u["role"] != "admin" and u["listed"]]
        self.send_json(200, {"names": sorted(names, key=str.lower)})

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
    def do_PUT(self):
        self.api(lambda: self.write(merge=False))

    def do_PATCH(self):
        if m := re.fullmatch(r"/api/users/([0-9a-f]{16})", self.path):
            return self.api(lambda: self.update_user(m.group(1)))
        self.api(lambda: self.write(merge=True))

    def do_DELETE(self):
        self.api(self.delete)

    def do_POST(self):
        routes = {"/api/map": self.upload_map, "/api/login": self.login, "/api/logout": self.logout,
                  "/api/password": self.change_password,
                  "/api/register": self.register}
        if self.path in routes:
            return self.api(routes[self.path])
        if m := re.fullmatch(r"/api/claims/([0-9a-f]{16})/owner", self.path):
            return self.api(lambda: self.move_claim(m.group(1)))
        if m := re.fullmatch(r"/api/claims/([0-9a-f]{16})/leave", self.path):
            return self.api(lambda: self.leave_team(m.group(1)))
        if m := re.fullmatch(r"/api/claims/([0-9a-f]{16})/(accept|decline)", self.path):
            return self.api(lambda: self.answer_invite(m.group(1), m.group(2) == "accept"))
        self.send_json(404, {"code": "not_found", "error": "Nicht gefunden"})

    # --- Anmeldung
    def discord_login(self):
        cfg = discord_config(self.headers.get("Host"))
        if not cfg:
            return self.redirect("/?login_error=not_configured")
        state = secrets.token_urlsafe(24)
        query = urllib.parse.urlencode({"client_id": cfg[0], "redirect_uri": cfg[2], "response_type": "code",
                                        "scope": "identify", "state": state})
        self.redirect(DISCORD_AUTHORIZE + "?" + query, self.state_cookie(state))

    def discord_callback(self):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        clear = self.state_cookie("")
        fail = lambda code: self.redirect("/?login_error=" + code, clear)
        state, expected = q.get("state", [""])[0], self.cookie(STATE_COOKIE)
        if not state or not expected or not hmac.compare_digest(state.encode(), expected.encode()):
            return fail("state")
        cfg = discord_config(self.headers.get("Host"))
        if not cfg:
            return fail("not_configured")
        if "code" not in q:
            return fail("cancelled")
        try:
            did, username = discord_profile(q["code"][0], cfg)
        except (OSError, ValueError, KeyError, urllib.error.URLError) as e:
            sys.stderr.write(f"Discord-Anmeldung fehlgeschlagen: {type(e).__name__}\n")
            return fail("discord_failed")
        with lock:
            db = load()
            uid, u = discord_account(db, did, username)
            if not u["active"]:
                return fail("locked")
            save(db)
            token = session_token(db, uid)
        self.redirect("/", clear, self.cookie_header(token))

    def register(self):
        """Ohne Discord: Spieler legen selbst ein Konto mit Minecraft-Name + Passwort an."""
        if discord_enabled():
            raise ApiError("forbidden", "Bitte melde dich mit Discord an.", 403)
        body = self.json_body()
        name, pw = clean_name(body.get("name")), clean_password(body.get("password"))
        key = (self.client_address[0], "#register")
        if too_many_fails(key):
            raise ApiError("rate_limited", "Zu viele neue Konten. Versuch es in ein paar Minuten noch einmal.", 429)
        pw_hash = hash_pw(pw)
        with lock:
            db = load()
            if find_user(db, name)[1]:
                raise ApiError("invalid", f"Den Namen „{name}“ gibt es schon. Melde dich an oder nimm einen anderen.")
            uid = add_user(db, name, pw_hash)
            save(db)
            token = session_token(db, uid)
        note_fail(key)                               # zählt neue Konten pro IP (max. MAX_FAILS in FAIL_WINDOW)
        self.send_json(200, {"ok": True}, cookie=token)

    def login(self):
        """Admin-Konto und Spieler mit Passwort (ohne Discord). Discord-Konten haben kein Passwort."""
        body = self.json_body()
        name, pw = body.get("name"), body.get("password")
        wrong = ApiError("wrong_login", "Name oder Passwort falsch.", 401)
        if not isinstance(name, str) or not isinstance(pw, str) or len(name) > 32 or len(pw) > 128:
            raise wrong
        name = name.strip()
        key = (self.client_address[0], name.lower())
        if too_many_fails(key):
            raise ApiError("rate_limited", "Zu viele Fehlversuche. Versuch es in ein paar Minuten noch einmal.", 429)
        with lock:
            db = load()
        uid, u = find_user(db, name)
        # Hash außerhalb des Locks: dauert absichtlich lang und soll niemanden sonst aufhalten
        has_pw = bool(u and u.get("pw") and u["active"])
        if not check_pw(pw, u["pw"] if has_pw else DUMMY_HASH) or not has_pw:
            note_fail(key)
            raise wrong
        with fails_lock:
            fails.pop(key, None)
        self.send_json(200, {"ok": True}, cookie=session_token(db, uid))

    def logout(self):
        self.send_json(200, {"ok": True}, cookie="")

    def change_password(self):
        body = self.json_body()
        new = clean_password(body.get("new"))
        old = body.get("old")
        with lock:
            db = load()
        uid, _ = self.who(db)
        if not uid or not db["users"][uid].get("pw"):
            raise ApiError("forbidden", "Dieses Konto hat kein Passwort (Anmeldung über Discord).", 403)
        if not isinstance(old, str) or not check_pw(old, db["users"][uid]["pw"]):
            raise ApiError("wrong_login", "Das aktuelle Passwort stimmt nicht.", 403)
        pw_hash = hash_pw(new)
        with lock:
            db = load()
            u = db["users"][uid]
            u.update(pw=pw_hash, gen=u["gen"] + 1, mustChange=False)   # meldet andere Geräte ab
            save(db)
            token = session_token(db, uid)
        self.send_json(200, {"ok": True}, cookie=token)

    # --- Konten (nur Admin)
    def update_user(self, uid):
        body = self.json_body()
        with lock:
            db = load()
            self.require_admin(db)
            u = db["users"].get(uid)
            if not u:
                raise ApiError("not_found", "Konto existiert nicht.", 404)
            if u["role"] == "admin":
                raise ApiError("invalid", "Das Admin-Konto lässt sich hier nicht ändern.")
            if "active" in body:
                u["active"] = bool(body["active"])
                u["gen"] += 1
            save(db)
        self.send_json(200, {"ok": True})

    def move_claim(self, cid):
        """Gibt ein Gebiet einem anderen Konto (auch für Gebiete aus der Zeit vor den Konten)."""
        target = self.json_body().get("uid")
        with lock:
            db = load()
            self.require_admin(db)
            if cid not in db["claims"]:
                raise ApiError("not_found", "Gebiet existiert nicht.", 404)
            if target not in db["users"]:
                raise ApiError("not_found", "Konto existiert nicht.", 404)
            other, team = team_of(db, target)
            if other and other != cid:
                raise ApiError("invalid", f"Dieses Konto ist schon im Team „{team['team']}“.")
            c = db["claims"].pop(cid)
            ids = [target] + [i for i in member_ids(cid, c) if i not in (cid, target)]
            set_members(db, c, ids)
            c["players"] = len(ids)
            db["claims"][target] = c
            save(db)
        self.send_json(200, {"ok": True})

    def leave_team(self, cid):
        """Wer von einem anderen eingetragen wurde, kommt so wieder raus und kann selbst claimen."""
        with lock:
            db = load()
            uid, _ = self.who(db)
            c = db["claims"].get(cid)
            if not uid or not c or uid not in member_ids(cid, c):
                raise ApiError("forbidden", "Du bist nicht in diesem Team.", 403)
            if uid == cid:
                raise ApiError("invalid", "Du hast dieses Gebiet erstellt. Du kannst es freigeben, aber nicht verlassen.")
            if not claims_open(db["settings"]):
                raise ApiError("closed", "Das Claimen ist geschlossen. Frag einen Admin.", 403)
            ids = [i for i in member_ids(cid, c) if i != uid]
            set_members(db, c, ids)
            c["players"] = len(ids)
            save(db)
        self.send_json(200, {"ok": True})

    def answer_invite(self, cid, accept):
        """Eingeladene nehmen an (kommen ins Team) oder lehnen ab."""
        with lock:
            db = load()
            uid, _ = self.who(db)
            c = db["claims"].get(cid)
            if not uid or not c or uid not in c.get("inviteIds", []):
                raise ApiError("not_found", "Diese Einladung gibt es nicht mehr.", 404)
            if accept:
                other, team = team_of(db, uid)
                if other:
                    raise ApiError("invalid", f"Du bist schon im Team „{team['team']}“. Verlass es zuerst.")
                if not claims_open(db["settings"]):
                    raise ApiError("closed", "Das Claimen ist geschlossen. Frag einen Admin.", 403)
                ids = member_ids(cid, c) + [uid]
                if len(ids) > db["settings"]["maxPlayers"]:
                    raise ApiError("invalid", f"Das Team ist schon voll ({db['settings']['maxPlayers']} Spieler).")
                set_members(db, c, ids)
                c["players"] = len(ids)
                for k, o in db["claims"].items():          # andere Einladungen erledigen sich damit
                    if k != cid and uid in o.get("inviteIds", []):
                        set_members(db, o, member_ids(k, o), [i for i in o["inviteIds"] if i != uid])
            else:
                set_members(db, c, member_ids(cid, c), [i for i in c["inviteIds"] if i != uid])
            save(db)
        self.send_json(200, {"ok": True})

    def write(self, merge):
        path = self.path.split("?", 1)[0]
        body = self.json_body()
        with lock:
            db = load()
            uid, admin = self.who(db)
            if path == "/api/doc/settings/main":
                if not admin:
                    raise ApiError("forbidden", "Nur Admins.", 403)
                db["settings"] = write_settings({**db["settings"], **body} if merge else body)
            elif m := re.fullmatch(r"/api/doc/claims/([0-9a-f]{16})", path):
                if not uid and not admin:
                    raise ApiError("forbidden", "Bitte melde dich an.", 403)
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
                raise ApiError("forbidden", "Nur wer das Gebiet erstellt hat oder ein Admin kann es freigeben.", 403)
            if not admin and not claims_open(db["settings"]):
                raise ApiError("closed", "Das Claimen ist geschlossen.", 403)
            db["claims"].pop(cid, None)
            save(db)
            v = db["version"]
        self.send_json(200, {"ok": True, "version": v})

    def upload_map(self):
        with lock:
            db = load()
        self.require_admin(db)
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
    args = [a for a in sys.argv[1:] if a != "--reset-admin"]
    if "--reset-admin" in sys.argv:
        ensure_admin(reset=True)
        sys.exit(0)
    port = int(args[0]) if args else 8080
    ensure_admin()                               # legt data.json an / migriert claims.json
    host = os.environ.get("HOST", "0.0.0.0")      # hinter einem Proxy: HOST=127.0.0.1
    print(f"Gebiete-Karte läuft auf http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
