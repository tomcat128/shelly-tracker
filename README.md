# Shelly Tracker

Liest die Daten deiner Shelly-Geräte (z.B. **WS90 Wetterstation**, **BLU H&T ZB**) regelmäßig aus der
**Shelly Cloud** und speichert sie in einer Datenbank. Eine REST-Schnittstelle stellt die
aktuellen Werte und historische Daten (gefiltert nach Gerät, Messwert und Zeitraum) als JSON oder CSV bereit.

- Python 3.12, FastAPI, SQLAlchemy
- Standard: **ein Container** (App + SQLite), Daten im Volume `./data`
- Optional: MariaDB/MySQL (oder PostgreSQL) über `DATABASE_URL`
- Shelly Cloud Control API **v2** (bis zu 10 Geräte pro Request, Rate-Limit 1 req/s wird eingehalten),
  v1 als Fallback

## Schnellstart

```bash
cp .env.example .env                              # SHELLY_HOST + SHELLY_AUTH_KEY eintragen
cp config/devices.example.yaml config/devices.yaml  # Geräte-IDs + Kurzcodes eintragen
docker compose up -d --build
```

Danach:

- Startseite mit Status und Export-Links: <http://localhost:9090/>
- API-Doku (Swagger): <http://localhost:9090/docs>
- Letzte Werte: <http://localhost:9090/api/export>

Host und Auth-Key findest du in der Shelly App unter *Benutzereinstellungen → Authorization cloud key*.

**Server-Installation** mit fertigem Image von Docker Hub (`fehrenbacher/shelly-tracker:latest`) hinter
Caddy: siehe [docs/INSTALL-SERVER.md](docs/INSTALL-SERVER.md).

## Geräte konfigurieren (`config/devices.yaml`)

```yaml
devices:
  - id: "XB1234567890"     # Geräte-ID aus der Shelly App
    code: WS90             # Kurzname/Code (eindeutig), wird in der API verwendet
    name: Wetterstation
  - id: "7c2c67aabbcc"
    code: HT1
    name: BLU H&T ZB Wohnzimmer

aliases:                   # Rohpfad im Status -> Name im Export
  "temperature:0.tC": temperature
  "humidity:0.rh": humidity
```

Gespeichert werden alle numerischen Werte aus dem Gerätestatus. Uninteressantes wie WLAN-, Cloud-,
System- und Zeitstempel-Felder wird standardmäßig ausgefiltert. Welche Messwerte ein Gerät liefert,
zeigt nach dem ersten Abruf:

```
GET /api/devices/WS90/metrics   # Pfad, Alias, Wert, wird gespeichert ja/nein
GET /api/devices/WS90/raw       # letztes Roh-JSON aus der Cloud
```

**Aliase** werden erst beim Export angewendet. Du kannst sie also jederzeit ändern, und sie gelten
dann auch für bereits gespeicherte Daten. Nach Änderungen an `devices.yaml` den Container neu
starten: `docker compose restart`.

### Steckdosen (Plug M Gen3, Outdoor Plug S Gen3, …)

Werden wie jedes andere Gerät mit ihrer Geräte-ID eingetragen. Gespeichert werden Leistung,
Spannung, Strom, Frequenz, Schaltzustand, Gerätetemperatur und der Energiezähler
(`switch:0.aenergy.total` in Wh, läuft fortlaufend hoch). Den Verbrauch pro Tag liefert
`interval=1d&agg=min,max&metrics=energy_total` (Max − Min). Die Leistung ist ein Momentanwert
zum Abrufzeitpunkt.

### Shelly BLU TRV

Ein BLU TRV ist in der Shelly Cloud kein eigenes Gerät, sondern Teil des **BLU Gateway Gen3**
(Komponente `blutrv:200`, `blutrv:201`, …). Eingetragen wird deshalb die ID des Gateways plus
die Nummer der Komponente:

```yaml
  - id: "b08184e7c6b0"   # Geräte-ID des BLU Gateway Gen3
    trv: 200             # Komponente blutrv:200
    code: TRV1
    name: Heizkörper Umkleide
```

Jeder TRV wird so zu einem eigenen Gerät mit Raumtemperatur (`temperature:0.tC`),
Solltemperatur (`trv:0.target_C`), Ventilöffnung in % (`trv:0.pos`), Batterie und Funksignal.
Raum- und Solltemperatur kommen aus den BTHome-Werten, die der TRV etwa jede Minute sendet.
Die übrigen Werte aktualisiert das Gateway seltener. Dafür liest der Tracker einmal pro Stunde
zusätzlich die Gateway-Einstellungen (nur mit `SHELLY_API_VERSION=v2`).

## Export-Schnittstelle

`GET /api/export`

| Parameter  | Beschreibung | Beispiel |
|------------|--------------|----------|
| *(keine)*  | letzte Werte aller Geräte | `/api/export` |
| `devices`  | Gerätecodes oder IDs, kommagetrennt oder mehrfach angegeben (ohne = alle) | `devices=WS90,HT1` oder `devices=WS90&devices=HT1` |
| `from`     | Beginn: ISO-Datum/-Zeit, Unix-Sekunden oder relativ (`30m`, `24h`, `7d`, `2w`) | `from=2026-11-01` |
| `to`       | Ende (ein reines Datum zählt inklusive des ganzen Tages) | `to=2026-11-20` |
| `metrics`  | Messwerte (Alias oder Rohpfad), Wildcards erlaubt | `metrics=temperature,wind*` |
| `format`   | `json` (Standard), `csv` (lang: eine Zeile pro Wert), `csv-wide` (eine Spalte pro Messwert) | `format=csv-wide` |
| `order`    | `asc` / `desc` | `order=desc` |
| `limit`    | max. Messungen bzw. Zeitabschnitte pro Gerät (Standard 10000) | `limit=500` |
| `interval` | Werte zu Zeitabschnitten zusammenfassen: `15m`, `1h`, `6h`, `1d`, `1w`, `1mo` (braucht `from`/`to`) | `interval=1h` |
| `agg`      | mit `interval`: `avg` (Standard), `min`, `max`, kommagetrennt kombinierbar | `agg=avg,min,max` |

Auf der Startseite stellt das Formular „Eigener Export“ solche Abfragen zusammen: Geräte
anhaken, Zeitraum und Format wählen.

Sobald `from` oder `to` angegeben ist, wird der Zeitraum geliefert, sonst nur der letzte Wert.
Zeitangaben ohne Zeitzone werden in `TIMEZONE` (Standard `Europe/Berlin`) interpretiert, und die
Ausgabe erfolgt ebenfalls in dieser Zone.

Beispiele:

```bash
curl 'http://localhost:9090/api/export'
curl 'http://localhost:9090/api/export?devices=WS90&from=24h'
curl 'http://localhost:9090/api/export?devices=HT1&from=2026-11-01&to=2026-11-07&format=csv-wide' -o ht1.csv
# Tageswerte (Mittel, Minimum, Maximum) der Temperatur über ein Jahr
curl 'http://localhost:9090/api/export?devices=WS90&from=2025-11-23&interval=1d&agg=avg,min,max&metrics=temperature'
```

### Zusammenfassen über lange Zeiträume (`interval`)

Ohne `interval` liefert der Export jede einzelne Messung, also etwa eine pro Minute. Für Wochen,
Monate oder Jahre ist das zu viel: 1 Jahr sind 525.600 Messungen pro Gerät, und `limit` schneidet
nach 10.000 ab (etwa 7 Tage). Mit `interval` fasst die Datenbank die Werte je Zeitabschnitt zusammen:

- Stunden, Tage, Wochen (ab Montag) und Monate beginnen in der Zeitzone `TIMEZONE`, auch über
  Sommer- und Winterzeit hinweg. `ts` ist der Beginn des Abschnitts.
- `readings` gibt an, wie viele Messungen in den Abschnitt eingeflossen sind.
- Mit einer Funktion (`agg=avg`) heißen die Werte wie gewohnt (`temperature`). Mit mehreren
  bekommen sie ein Suffix: `temperature_avg`, `temperature_min`, `temperature_max`.
- Hinweise zu einzelnen Messwerten:
  - `wind_direction`: Der Mittelwert ist hier nicht aussagekräftig, weil die Richtung im Kreis
    läuft (350° und 10° ergeben 180°).
  - `rain_total`: Der Wert ist eine laufende Summe. `max` liefert den Stand am Ende des Abschnitts,
    die Differenz zweier Abschnitte die Regenmenge dazwischen.

Antwort (letzte Werte):

```json
{
  "generated_at": "2026-11-23T09:15:02+01:00",
  "mode": "latest",
  "devices": [
    {"code": "WS90", "id": "...", "name": "Wetterstation", "online": true,
     "ts": "2026-11-23T09:14:00+01:00",
     "values": {"temperature": 5.2, "humidity": 80.0, "wind_speed": 3.4}}
  ]
}
```

Weitere Endpunkte: `GET /health`, `GET /api/devices`, `POST /api/poll` (sofort abrufen),
`POST /api/rollup` (Stundenwerte und Aufbewahrung sofort ausführen).

### Absicherung

Ist `API_TOKEN` gesetzt, brauchen alle `/api/*`-Endpunkte den Token als
`Authorization: Bearer <token>`, als `X-API-Key: <token>` oder als `?token=<token>`.

Ist `PAGE_PIN` gesetzt, zeigt die Startseite nur den Status. Geräteliste und Export-Links
erscheinen erst nach Eingabe der PIN. Der Browser bekommt dann für 30 Tage ein Cookie, mit dem
auch die Export-Links ohne `API_TOKEN` funktionieren. „Sperren“ unten auf der Seite löscht es,
eine neue PIN macht alle Cookies ungültig. Nach 5 falschen PINs ist die Eingabe 5 Minuten gesperrt.

Wenn die App aus dem Internet erreichbar ist, gehört sie hinter einen Reverse-Proxy mit HTTPS.

## Konfiguration (`.env`)

| Variable | Standard | Beschreibung |
|----------|----------|--------------|
| `SHELLY_HOST` | – | Cloud-Server, z.B. `shelly-XX-eu.shelly.cloud` |
| `SHELLY_AUTH_KEY` | – | Cloud Auth-Key |
| `SHELLY_API_VERSION` | `v2` | `v2` oder `v1` |
| `POLL_INTERVAL_SECONDS` | `60` | Abrufintervall |
| `DATABASE_URL` | `sqlite:////data/shelly.db` | SQLAlchemy-URL |
| `TIMEZONE` | `Europe/Berlin` | Zeitzone für Ein-/Ausgabe |
| `API_TOKEN` | leer | Token für die API |
| `PAGE_PIN` | leer | PIN für Geräteliste und Export-Links auf der Startseite; leer = ohne PIN |
| `STORE_RAW` | `false` | Roh-JSON jeder Messung speichern |
| `RAW_RETENTION_MONTHS` | `18` | Minutenwerte so viele Monate aufbewahren, danach nur Stundenwerte; `0` = nie löschen |
| `SKIP_UNCHANGED` | `true` | identische Werte ohne Geräte-Zeitstempel nicht erneut speichern |
| `PUID` / `PGID` | `1000` | Besitzer der Dateien in `./data` |
| `HOST_PORT` | `9090` | Port auf dem Host |

## Datenmodell

- `devices`: Geräte-ID, Code, Name, Modell, online, letzter Kontakt, letzter Roh-Status
- `readings`: eine Messung pro Gerät und Zeitpunkt. Der Zeitpunkt kommt aus dem Gerät, wenn er im
  Status enthalten ist, sonst ist es die Abrufzeit. Doppelte Messungen werden verworfen.
- `measurements`: Messwerte (`metric`, `value`) einer Messung
- `hourly_stats`: Stundenwerte pro Gerät, Stunde und Messwert (Anzahl, Summe, Minimum, Maximum)

### Stundenwerte und Aufbewahrung

Ein Hintergrund-Job läuft eine Minute nach dem Start und danach stündlich. Er fasst jede
abgeschlossene Stunde in `hourly_stats` zusammen. Exporte mit `interval` ab `1h` lesen aus dieser
kleinen Tabelle und bleiben deshalb auch über Jahre schnell.

Minutenwerte, die älter als `RAW_RETENTION_MONTHS` sind (Standard 18 Monate), löscht der Job, weil
ihre Stundenwerte bereits gespeichert sind. Für diese Zeit gilt:

- Mittel, Minimum und Maximum pro Stunde, Tag, Woche und Monat bleiben exakt erhalten.
- Einzelne Minutenwerte und Zusammenfassungen feiner als 1 h gibt es nicht mehr. `interval=15m`
  liefert dort einen Wert pro Stunde.
- Ein Export ohne `interval` enthält nur noch vorhandene Minutenwerte. In der Antwort steht dann
  `hourly_only_before` mit einem Hinweis (`note`).
- **Das Löschen ist endgültig.** Wer die Minutenwerte behalten will, setzt `RAW_RETENTION_MONTHS=0`.

Den Job kannst du mit `POST /api/rollup` sofort auslösen.

Datenmenge mit WS90 und BLU H&T bei einer Messung pro Minute: etwa 1,2 MB pro Tag. Mit dem Standard
von 18 Monaten bleibt die Datenbank dauerhaft bei etwa 0,7 GB, danach wächst sie nur noch um rund
13 MB pro Jahr. Ältere Datenbanken stellt die App beim Start automatisch auf das kompaktere Format um.

## Backup

Alle Daten liegen in `./data`. Für ein konsistentes Backup im laufenden Betrieb:

```bash
docker compose exec -T -u shelly shelly-tracker python -c \
  "import sqlite3; s=sqlite3.connect('/data/shelly.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d)"
```

Alternativ den Container kurz stoppen (`docker compose stop`) und `./data` kopieren.

## MariaDB statt SQLite

```bash
# in .env: DATABASE_URL=mysql+pymysql://shelly:<passwort>@db:3306/shelly  und  MARIADB_PASSWORD=<passwort>
docker compose -f docker-compose.yml -f docker-compose.mariadb.yml up -d --build
```

## Docker-Image bauen und veröffentlichen

```bash
docker build -t fehrenbacher/shelly-tracker:latest .
docker push fehrenbacher/shelly-tracker:latest
```

## Homepage-Schnipsel (`krec-homepage/`)

Wetter- und Hallendaten für die Vereinsseite: ein kleines PHP-Skript auf dem Webspace
holt die letzten Werte über `/api/export` und hält sie 60 Sekunden im Cache, dazu
HTML-Schnipsel, JavaScript, CSS und Icons. Einbau und lokaler Test stehen in
[krec-homepage/README.md](krec-homepage/README.md).

## Entwicklung

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest
```

## Hinweis

Eine frühere PHP-Variante liegt unter:
`ssh://konstanzer-rec.de:/var/www/nextcloud/data/KREC/files/Homepage/Visitenkarte/docs_php/shelly-api/data.php`
