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

Kein Passwort für Spieler: Jeder Browser bekommt automatisch ein geheimes Account-Token.
Die Account-ID ist ein HMAC davon, also kann niemand ein fremdes Gebiet ändern.
Wer das Gerät wechselt, findet unter „Mein Gebiet“ einen Login-Link für das andere Gerät.
Browser-Daten löschen = Zugriff weg; dann kann ein Admin das Gebiet bearbeiten oder löschen.

## Was der Server prüft

Alles, was die Seite prüft, prüft `server.py` noch einmal (unter Lock, also kein gleichzeitiges
Überschneiden): eigenes Gebiet, Claimen offen/Anmeldeschluss, Teamname (doppelt?), Spielerzahl,
Farbe, 3–60 Punkte, Kartenrand, Selbstüberschneidung, Mindestfläche, Spawn-Zone,
Mindestabstand zu anderen Gebieten, Grenzlänge. Den Status (freigegeben/offen) setzt nur der
Server bzw. ein Admin. Admins sind von den Geometrie-Regeln ausgenommen.
