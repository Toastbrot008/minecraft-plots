# EL-SMP Gebiete-Karte

Teams ziehen vor dem Serverstart die Grenzen ihres Gebiets auf einer echten Karte der Welt
(Seed 702436, Minecraft 26.3). Zusammengeführt aus der EL-SMP-Seite (Oberfläche) und der
Plot-Karte (Backend + gerenderte Welt).

## Starten

```
python3 server.py          # Port 8080, nur Python-Standardbibliothek
python3 server.py 9000     # anderer Port
```

Beim Start steht in der Konsole der **Admin-Link** (`/?admin=…`). Einmal im Browser öffnen,
dann ist der Admin-Tab da. Das Passwort steht in `admin_token.txt` (oder `ADMIN_TOKEN=…`).

## Dateien

```
server.py              Backend: Daten, Prüfregeln, Kartenupload       -> data.json, uploads/
public/index.html      Oberfläche (EL-SMP), Gelände-Ebene + Blockanzeige
public/backend.js      Adapter: ersetzt die claude.ai-Laufzeit (window.claude.use) durch server.py
public/terrain/        gerenderte Welt (Kacheln + Blockdaten), erzeugt von tools/world
public/map.png         Biom-Karte, Fallback falls terrain/ fehlt   (tools/make_map.sh)
tools/world/           Welt generieren + rendern: make_terrain.sh
old/                   Stand vor dem Zusammenführen (alte Plot-Seite, Original-EL-SMP-Dateien)
```

## Accounts

**Standard (ohne `config.json`):** Kein Passwort für Spieler. Jeder Browser bekommt automatisch
ein geheimes Account-Token, die Account-ID ist ein HMAC davon. Wer das Gerät wechselt, findet
unter „Mein Gebiet“ einen Login-Link. Browser-Daten löschen = Zugriff weg.

**Mit Discord-Login:**

1. Auf https://discord.com/developers/applications eine App anlegen (*New Application*).
2. Unter *OAuth2* die **Client ID** und das **Client Secret** kopieren und bei *Redirects*
   `<öffentliche Adresse>/auth/discord/callback` eintragen, z. B.
   `https://mc.elektronic.online/auth/discord/callback`.
3. `config.example.json` nach `config.json` kopieren und ausfüllen (`public_url` ist dieselbe
   Adresse ohne `/auth/...`). Unter `admins` die Discord-User-IDs der Admins eintragen.
4. Server neu starten. Die Konsole zeigt die Redirect-URL, die in Discord stehen muss.

Danach kann nur noch claimen, wer mit Discord angemeldet ist. Der Server fragt nur den Scope
`identify` ab (ID + Name, keine E-Mail). Die Sitzung ist ein signiertes Cookie (30 Tage).
Wer schon vorher im Browser-Modus ein Gebiet hatte, behält es: Beim ersten Discord-Login im
selben Browser wird es automatisch übertragen. Der Discord-Name wird in der Gebietsansicht als
„Geclaimt von“ angezeigt. Das Admin-Passwort (`?admin=…`) funktioniert weiterhin.

`config.json` enthält das Client Secret und ist deshalb nicht im Repo.

## Was der Server prüft

Alles, was die Seite prüft, prüft `server.py` noch einmal (unter Lock, also kein gleichzeitiges
Überschneiden): eigenes Gebiet, Claimen offen/Anmeldeschluss, Teamname (doppelt?), Spielerzahl,
Farbe, 3–60 Punkte, Kartenrand, Selbstüberschneidung, Mindestfläche, Spawn-Zone,
Mindestabstand zu anderen Gebieten, Grenzlänge. Den Status (freigegeben/offen) setzt nur der
Server bzw. ein Admin. Admins sind von den Geometrie-Regeln ausgenommen.
