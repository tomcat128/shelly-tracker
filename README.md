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

- API-Doku (Swagger): <http://localhost:8080/docs>
- Letzte Werte: <http://localhost:8080/api/export>

Host und Auth-Key findest du in der Shelly App unter *Benutzereinstellungen → Authorization cloud key*.

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

## Export-Schnittstelle

`GET /api/export`

| Parameter  | Beschreibung | Beispiel |
|------------|--------------|----------|
| *(keine)*  | letzte Werte aller Geräte | `/api/export` |
| `devices`  | Gerätecodes oder IDs, kommagetrennt | `devices=WS90,HT1` |
| `from`     | Beginn: ISO-Datum/-Zeit, Unix-Sekunden oder relativ (`30m`, `24h`, `7d`, `2w`) | `from=2026-11-01` |
| `to`       | Ende (ein reines Datum zählt inklusive des ganzen Tages) | `to=2026-11-20` |
| `metrics`  | Messwerte (Alias oder Rohpfad), Wildcards erlaubt | `metrics=temperature,wind*` |
| `format`   | `json` (Standard), `csv` (lang: eine Zeile pro Wert), `csv-wide` (eine Spalte pro Messwert) | `format=csv-wide` |
| `order`    | `asc` / `desc` | `order=desc` |
| `limit`    | max. Messungen pro Gerät (Standard 10000) | `limit=500` |

Sobald `from` oder `to` angegeben ist, wird der Zeitraum geliefert, sonst nur der letzte Wert.
Zeitangaben ohne Zeitzone werden in `TIMEZONE` (Standard `Europe/Berlin`) interpretiert, und die
Ausgabe erfolgt ebenfalls in dieser Zone.

Beispiele:

```bash
curl 'http://localhost:8080/api/export'
curl 'http://localhost:8080/api/export?devices=WS90&from=24h'
curl 'http://localhost:8080/api/export?devices=HT1&from=2026-11-01&to=2026-11-07&format=csv-wide' -o ht1.csv
```

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

Weitere Endpunkte: `GET /health`, `GET /api/devices`, `POST /api/poll` (sofort abrufen).

### Absicherung

Ist `API_TOKEN` gesetzt, brauchen alle `/api/*`-Endpunkte den Token als
`Authorization: Bearer <token>`, als `X-API-Key: <token>` oder als `?token=<token>`.
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
| `STORE_RAW` | `false` | Roh-JSON jeder Messung speichern |
| `SKIP_UNCHANGED` | `true` | identische Werte ohne Geräte-Zeitstempel nicht erneut speichern |
| `PUID` / `PGID` | `1000` | Besitzer der Dateien in `./data` |
| `HOST_PORT` | `8080` | Port auf dem Host |

## Datenmodell

- `devices`: Geräte-ID, Code, Name, Modell, online, letzter Kontakt, letzter Roh-Status
- `readings`: eine Messung pro Gerät und Zeitpunkt. Der Zeitpunkt kommt aus dem Gerät, wenn er im
  Status enthalten ist, sonst ist es die Abrufzeit. Doppelte Messungen werden verworfen.
- `measurements`: Messwerte (`metric`, `value`) einer Messung

## Backup

Alle Daten liegen in `./data`. Für ein konsistentes Backup im laufenden Betrieb:

```bash
docker compose exec shelly-tracker python -c \
  "import sqlite3; s=sqlite3.connect('/data/shelly.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d)"
```

Alternativ den Container kurz stoppen (`docker compose stop`) und `./data` kopieren.

## MariaDB statt SQLite

```bash
# in .env: DATABASE_URL=mysql+pymysql://shelly:<passwort>@db:3306/shelly  und  MARIADB_PASSWORD=<passwort>
docker compose -f docker-compose.yml -f docker-compose.mariadb.yml up -d --build
```

## Entwicklung

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest
```

## Hinweis

Eine frühere PHP-Variante liegt unter:
`ssh://konstanzer-rec.de:/var/www/nextcloud/data/KREC/files/Homepage/Visitenkarte/docs_php/shelly-api/data.php`
