# EL-SMP Gebiete-Karte

Teams ziehen vor dem Serverstart die Grenzen ihres Gebiets auf einer echten Karte der Welt
(Seed 702436, Minecraft 26.3). Zusammengeführt aus der EL-SMP-Seite (Oberfläche) und der
Plot-Karte (Backend + gerenderte Welt).

## Starten

```
python3 server.py          # Port 8080, nur Python-Standardbibliothek
python3 server.py 9000     # anderer Port
```

Beim ersten Start wird das Konto **admin** angelegt und sein Passwort einmal in der Konsole
ausgegeben (oder vorgeben: `ADMIN_USER=… ADMIN_PASSWORD=… python3 server.py`).
Passwort vergessen: `python3 server.py --reset-admin`.

## Dateien

```
server.py              Backend: Daten, Prüfregeln, Kartenupload       -> data.json, uploads/
public/index.html      Oberfläche (EL-SMP), Gelände-Ebene + Blockanzeige
public/backend.js      Adapter: ersetzt die claude.ai-Laufzeit (window.claude.use) durch server.py
public/terrain/        gerenderte Welt (Kacheln + Blockdaten), erzeugt von tools/world
public/map.png         Biom-Karte, Fallback falls terrain/ fehlt   (tools/make_map.sh)
public/previews/       Vorschaubilder der Gegend (Popup beim Klick auf die Karte), erzeugt von tools/previews
tools/world/           Welt generieren + rendern: make_terrain.sh
tools/previews/        Vorschaubilder mit Chunky rendern: render_previews.py
old/                   Stand vor dem Zusammenführen (alte Plot-Seite, Original-EL-SMP-Dateien)
```

## Accounts

Spieler melden sich über **Discord** an (OAuth2, nur Scope `identify`: ID + Name, keine E-Mail).
Beim ersten Login entsteht automatisch ein Konto, das über die Discord-ID fest zugeordnet bleibt.
Der Admin meldet sich mit Name + Passwort an. Ohne Anmeldung liefert der Server nur die
Anmeldeseite aus.

Discord einrichten:

1. Auf https://discord.com/developers/applications eine App anlegen (*New Application*).
2. Unter *OAuth2* die **Client ID** und das **Client Secret** kopieren und bei *Redirects*
   `<öffentliche Adresse>/api/discord/callback` eintragen.
3. Server mit den Umgebungsvariablen starten:
   ```
   DISCORD_CLIENT_ID=… DISCORD_CLIENT_SECRET=… \
   DISCORD_REDIRECT_URI=https://<adresse>/api/discord/callback python3 server.py
   ```

## Was der Server prüft

Alles, was die Seite prüft, prüft `server.py` noch einmal (unter Lock, also kein gleichzeitiges
Überschneiden): eigenes Gebiet, Claimen offen/Anmeldeschluss, Teamname (doppelt?), Spielerzahl,
Farbe, 3–60 Punkte, Kartenrand, Selbstüberschneidung, Mindestfläche, Spawn-Zone,
Mindestabstand zu anderen Gebieten, Grenzlänge. Den Status (freigegeben/offen) setzt nur der
Server bzw. ein Admin. Admins sind von den Geometrie-Regeln ausgenommen.

## Vorschaubilder

Klickt man auf die Karte, zeigt ein Popup drei Ansichten der Gegend. Die Welt ist dafür
unsichtbar in Felder von 256 × 256 Blöcken geteilt; pro Feld gibt es drei Drohnenaufnahmen aus
verschiedenen Richtungen und zu verschiedenen Tageszeiten. Die Bilder sind echte Renderings der
generierten Welt mit den Original-Texturen aus der 26.3-`client.jar` (Path Tracing mit
[Chunky](https://chunky-dev.github.io/docs/), danach leichtes Color Grading). Felder ohne Bilder
zeigen kein Popup.

Bilder erzeugen:

1. Welt generieren: `tools/world/make_terrain.sh` (legt `tools/world/run/world` und `client.jar` an).
2. Chunky installieren: `ChunkyLauncher.jar` von https://chunky-dev.github.io/docs/ laden und
   `java -jar ChunkyLauncher.jar --updateAlpha` ausführen (landet in `~/.chunky/lib`).
   Texturen: `cp tools/world/client.jar ~/.chunky/resources/minecraft.jar`
3. Rendern:
   ```
   python3 tools/previews/render_previews.py              # alle Felder (~430 Bilder, viele Stunden)
   python3 tools/previews/render_previews.py -4,1 0,0     # nur bestimmte Felder
   SPP=64 W=640 H=360 python3 tools/previews/render_previews.py -4,1   # schnelle Probe
   ```
   Fertige Bilder werden übersprungen (`FORCE=1` rendert neu). Ergebnis: `public/previews/`.

Aktuell enthalten: nur Testbilder (640 × 360) für drei Felder – Jagged Peaks (um X -1150, Z -150),
Cherry Grove (um X -900, Z 400) und Küste (um X -400, Z 900).
