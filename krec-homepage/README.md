# Hallen- und Wetterdaten für rollsportanlage.html

Die Seite holt die Werte über `/shelly-api/data.php`. Das Skript fragt den Shelly
Tracker (https://shelly.konstanzer-rec.de) ab und hält den aktuellen Stand 60 Sekunden
im Cache. Die Historie speichert der Tracker, die Homepage braucht keinen Shelly-Key
und keine CSV mehr.

## Dateien

| Datei | Ziel auf dem Webspace |
|---|---|
| `shelly-api/data.php` | `/shelly-api/data.php` (ersetzt das alte Skript) |
| `css/shelly-wetter.css` | `/css/shelly-wetter.css` |
| `js/shelly-wetter.js` | `/js/shelly-wetter.js` |
| `images/wetter/*.svg` | `/images/wetter/` (alle 15 Icons, Pfad steht als `ICON_PFAD` oben im JS) |
| `rollsportanlage-snippet.html` | Inhalt in `rollsportanlage.html` einsetzen |
| `testseite.html` | nur für den lokalen Test, **nicht hochladen** |

## Einbau in rollsportanlage.html

1. Im `<style>`-Block alles ab `#wetter-banner { ... }` bis zum Ende des Blocks
   (Farbklassen, `.luft-kugel`, `@media (max-width: 900px)`) löschen.
   `#panorama` bleibt stehen.
2. Den Block `<div class="anschrift-wetter-wrap clearfix"> ... </div>`
   inklusive der Überschrift durch den Inhalt von `rollsportanlage-snippet.html` ersetzen.
3. Das alte `<script>` mit `fetch('/shelly-api/data.php' ...)` löschen.
   Das neue Skript ist im Schnipsel eingebunden.

## data.php

- Einstellungen stehen oben im Skript: Tracker-Adresse, Gerätecodes (`HT1`, `WS90`),
  Cache-Dauer und die Höhe der Station (für den Luftdruck auf Meereshöhe).
- Der Ordner `shelly-api/` muss für PHP beschreibbar sein
  (für `shelly-cache.json` und `shelly-cache.lock`).
- Ist der Tracker nicht erreichbar, liefert das Skript den letzten Stand. Die Seite
  markiert Werte, die älter als 30 Minuten sind, rot.
- Wird im Tracker später `API_TOKEN` gesetzt, trägt man ihn in `data.php` ein.
  Er bleibt auf dem Server.
- Die alten Dateien `shelly-data.json` und `shelly-history.csv` werden nicht mehr
  gebraucht.

## Lokaler Test

Im Ordner `krec-homepage/`:

```bash
docker run -d --name krec-homepage-test --restart unless-stopped \
  -u "$(id -u):$(id -g)" -p 9998:8080 -v "$PWD":/app -w /app \
  php:8.3-cli-alpine php -S 0.0.0.0:8080
```

Danach http://localhost:9998/testseite.html öffnen.
Beenden: `docker rm -f krec-homepage-test`
