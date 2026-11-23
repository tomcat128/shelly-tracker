# Installation auf einem Server (Docker Compose + Caddy)

Diese Anleitung geht davon aus, dass auf dem Server **Docker mit Compose** und **Caddy** bereits
laufen. Der Shelly Tracker läuft als ein einzelner Container (App + SQLite-Datenbank). Das fertige
Image wird von Docker Hub geladen: `fehrenbacher/shelly-tracker:latest` (Plattform `linux/amd64`).

Im Folgenden ist `shelly.example.de` ein Platzhalter für deine (Sub-)Domain, und `/opt/shelly-tracker`
das Installationsverzeichnis. Beides kannst du frei wählen.

## 1. Voraussetzungen

- DNS-Eintrag (A/AAAA) für `shelly.example.de` zeigt auf den Server, damit Caddy das Zertifikat holen kann
- Shelly Cloud: Server-Host (z.B. `shelly-282-eu.shelly.cloud`) und Auth-Key
  (Shelly App → Benutzereinstellungen → *Authorization cloud key*)
- Geräte-IDs der Shelly-Geräte

## 2. Verzeichnis anlegen

```bash
sudo mkdir -p /opt/shelly-tracker/{config,data}
sudo chown -R "$USER": /opt/shelly-tracker
cd /opt/shelly-tracker
```

## 3. Dateien bereitstellen

Benötigt werden drei Dateien:

```
/opt/shelly-tracker/
├── docker-compose.yml     # aus deploy/docker-compose.yml im Repo
├── .env                   # Zugangsdaten + Einstellungen
├── config/devices.yaml    # Geräte, Kurzcodes, Aliase
└── data/                  # Datenbank (wird automatisch befüllt)
```

**Von einem Rechner mit bestehender Installation kopieren** (am einfachsten, da `.env` und
`devices.yaml` dort schon fertig sind):

```bash
# auf dem lokalen Rechner, im Projektordner
scp deploy/docker-compose.yml  user@server:/opt/shelly-tracker/docker-compose.yml
scp .env                       user@server:/opt/shelly-tracker/.env
scp config/devices.yaml        user@server:/opt/shelly-tracker/config/devices.yaml
```

**Oder neu anlegen:** `deploy/docker-compose.yml`, `.env.example` (als `.env`) und
`config/devices.example.yaml` (als `config/devices.yaml`) aus dem Repository übernehmen und ausfüllen.

Die Rechte der `.env` einschränken, denn sie enthält den Auth-Key:

```bash
chmod 600 .env
```

## 4. `.env` für den Server anpassen

Mindestens diese Werte prüfen:

```ini
SHELLY_HOST=shelly-282-eu.shelly.cloud
SHELLY_AUTH_KEY=<dein-key>

# Besitzer der Dateien in ./data, also der User, der die Backups macht: Ausgabe von "id -u" / "id -g"
PUID=1000
PGID=1000

# Schutz der API, sobald sie aus dem Internet erreichbar ist (siehe Abschnitt 7)
API_TOKEN=<langer-zufälliger-wert>     # z.B. erzeugen mit: openssl rand -hex 32

TIMEZONE=Europe/Berlin
POLL_INTERVAL_SECONDS=60
```

`HOST_PORT` wird auf dem Server nicht verwendet. Der Port steht direkt in der `docker-compose.yml`.

## 5. Caddy anbinden

Es gibt zwei Varianten, je nachdem wie Caddy läuft.

### Variante A: Caddy läuft direkt auf dem Host (systemd)

Die `docker-compose.yml` bleibt unverändert. Die App ist dann nur unter `127.0.0.1:9090` erreichbar,
nicht von außen. Im `Caddyfile` (meist `/etc/caddy/Caddyfile`) ergänzen:

```caddy
shelly.example.de {
    encode gzip
    reverse_proxy 127.0.0.1:9090
}
```

```bash
sudo systemctl reload caddy
```

### Variante B: Caddy läuft als Docker-Container

1. Den Namen des Netzwerks herausfinden, in dem der Caddy-Container hängt:
   ```bash
   docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' caddy
   ```
2. In `docker-compose.yml` den Block `ports:` entfernen und die `networks`-Blöcke einkommentieren.
   Dabei `caddy` durch den gefundenen Netzwerknamen ersetzen.
3. Im Caddyfile den Containernamen und den internen Port 8000 verwenden:
   ```caddy
   shelly.example.de {
       encode gzip
       reverse_proxy shelly-tracker:8000
   }
   ```
4. Caddy neu laden, z.B. `docker exec caddy caddy reload --config /etc/caddy/Caddyfile`
   (Pfad je nach Setup).

## 6. Starten und prüfen

```bash
cd /opt/shelly-tracker
docker compose pull
docker compose up -d
docker compose logs -f          # Strg+C beendet die Anzeige
```

Im Log sollte nach wenigen Sekunden stehen:

```
INFO shelly-tracker: Tracking 2 device(s): WS90=..., HT1=...
INFO app.poller: Poll done: 2 device(s), 2 new reading(s)
```

Test:

```bash
curl -s https://shelly.example.de/health
curl -s -H "Authorization: Bearer <API_TOKEN>" https://shelly.example.de/api/export
```

`/health` ist ohne Token erreichbar. Wenn dort `"last_error": null` steht, funktioniert der Abruf aus der Cloud.

## 7. Absicherung

Sobald die App über Caddy öffentlich erreichbar ist, **`API_TOKEN` setzen**. Dann verlangen alle
`/api/*`-Endpunkte den Token, und zwar auf eine dieser Arten:

- Header `Authorization: Bearer <token>`
- Header `X-API-Key: <token>`
- Parameter `?token=<token>`, praktisch für Browser oder Excel. Der Token steht dann aber im Caddy-Access-Log.

Statt des Tokens oder zusätzlich dazu kann Caddy einen Passwortschutz vorschalten:

```bash
caddy hash-password            # bzw. docker exec -it caddy caddy hash-password
```

```caddy
shelly.example.de {
    basic_auth /api/* {
        admin <hash-aus-hash-password>
    }
    reverse_proxy 127.0.0.1:9090
}
```

Die interaktive API-Doku unter `/docs` ist öffentlich lesbar, ausführen lassen sich die Endpunkte
aber nur mit Token. Wer auch die Doku verbergen will, schützt sie mit `basic_auth /docs*` (bzw. `/*`).

## 8. Update

```bash
cd /opt/shelly-tracker
docker compose pull
docker compose up -d
docker image prune -f           # alte Images entfernen
```

Die Datenbank in `./data` bleibt dabei erhalten. Neue Tabellen oder Spalten legt die App beim Start selbst an.

## 9. Konfiguration ändern

| Geändert | Befehl |
|----------|--------|
| `config/devices.yaml` (Geräte, Namen, Aliase) | `docker compose restart` |
| `.env` | `docker compose up -d --force-recreate` |

## 10. Backup

Alle Daten liegen in `/opt/shelly-tracker/data/shelly.db`. Eine konsistente Kopie im laufenden
Betrieb erzeugt dieser Befehl:

```bash
docker compose exec -T shelly-tracker python -c \
  "import sqlite3; s=sqlite3.connect('/data/shelly.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close()"
```

Täglich per Cron, z.B. mit `crontab -e`:

```cron
15 3 * * * cd /opt/shelly-tracker && docker compose exec -T shelly-tracker python -c "import sqlite3; s=sqlite3.connect('/data/shelly.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close()" && cp data/backup.db /pfad/zum/backup/shelly-$(date +\%F).db
```

Zum Wiederherstellen den Container stoppen, die Sicherung als `data/shelly.db` zurückkopieren
(dabei vorhandene `shelly.db-wal` und `shelly.db-shm` löschen) und den Container wieder starten.

## Fehlersuche

| Symptom | Ursache / Lösung |
|---------|------------------|
| `/health` zeigt `last_error: HTTP 401` | `SHELLY_AUTH_KEY` falsch |
| `last_error` mit Verbindungsfehler | `SHELLY_HOST` falsch, oder der Server darf nicht ins Internet |
| `No status returned for: ...` im Log | Geräte-ID falsch, oder das Gerät gehört nicht zu diesem Cloud-Konto |
| Caddy meldet `502 Bad Gateway` | Container läuft nicht (`docker compose ps`), oder bei Variante B das falsche Netzwerk |
| `exec format error` beim Start | Server ist ARM (z.B. Raspberry Pi), das Image gibt es aber nur für amd64. Dann das Image auf dem Server selbst bauen: Repo klonen und `docker build -t fehrenbacher/shelly-tracker:latest .` |
| `unable to open database file` | Rechte von `./data`: `PUID`/`PGID` in `.env` prüfen |
