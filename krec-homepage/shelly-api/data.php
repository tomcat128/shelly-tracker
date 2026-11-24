<?php

// ============================================================
// Aktuelle Wetter- und Hallendaten für die Homepage
//
// Holt die letzten Messwerte vom Shelly Tracker
// (https://shelly.konstanzer-rec.de) und hält sie
// 60 Sekunden in einer Cache-Datei vor.
//
// Die historischen Daten speichert der Tracker selbst,
// dieses Skript speichert nur noch den aktuellen Stand.
// ============================================================

declare(strict_types=1);


// ------------------------------------------------------------
// Konfiguration
// ------------------------------------------------------------

// Adresse des Shelly Trackers (ohne / am Ende)
const TRACKER_URL = 'https://shelly.konstanzer-rec.de';

// Nur nötig, wenn im Tracker API_TOKEN gesetzt ist.
// Der Token bleibt auf dem Server und erscheint nie im Browser.
const API_TOKEN = '';

// Kurzcodes der Geräte aus der devices.yaml des Trackers
const HALLE_CODE  = 'HT1';   // BLU H&T in der Halle
const WETTER_CODE = 'WS90';  // Wetterstation

// Cache-Dauer in Sekunden
const CACHE_SECONDS = 60;

// Zeitraum, in dem der Regenzähler gestiegen sein muss, damit "es regnet" angezeigt wird
const REGEN_FENSTER = '1h';

// Höhe der Wetterstation über dem Meer in Metern.
// Damit wird der Luftdruck auf Meereshöhe umgerechnet,
// so wie ihn Wetterberichte angeben.
const HOEHE_M = 400;

const ZEITZONE = 'Europe/Berlin';

$cacheFile = __DIR__ . '/shelly-cache.json';
$lockFile  = __DIR__ . '/shelly-cache.lock';


// ------------------------------------------------------------
// Ausgabe
// ------------------------------------------------------------

function sendJson(string $json, int $status = 200): void
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-cache, no-store, must-revalidate');
    echo $json;
    exit;
}

function cacheIsFresh(string $cacheFile): bool
{
    clearstatcache(true, $cacheFile);
    return is_file($cacheFile) && (time() - filemtime($cacheFile)) < CACHE_SECONDS;
}


// ------------------------------------------------------------
// Abruf vom Tracker
// ------------------------------------------------------------

function trackerGet(string $pathAndQuery): array
{
    $headers = ['Accept: application/json'];
    if (API_TOKEN !== '') {
        $headers[] = 'Authorization: Bearer ' . API_TOKEN;
    }

    $ch = curl_init(TRACKER_URL . $pathAndQuery);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER     => $headers,
        CURLOPT_TIMEOUT        => 8,
        CURLOPT_CONNECTTIMEOUT => 4,
    ]);
    $response = curl_exec($ch);
    $httpCode = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $error    = curl_error($ch);
    curl_close($ch);

    if ($response === false) {
        throw new RuntimeException('Tracker nicht erreichbar: ' . $error);
    }
    if ($httpCode !== 200) {
        throw new RuntimeException('Tracker antwortet mit HTTP ' . $httpCode);
    }
    $data = json_decode($response, true);
    if (!is_array($data)) {
        throw new RuntimeException('Ungültige Antwort vom Tracker');
    }
    return $data;
}

function findDevice(array $export, string $code): ?array
{
    foreach ($export['devices'] ?? [] as $device) {
        if (($device['code'] ?? null) === $code) {
            return $device;
        }
    }
    return null;
}


// ------------------------------------------------------------
// Umrechnungen
// ------------------------------------------------------------

function num($value, int $decimals = 1): ?float
{
    return is_numeric($value) ? round((float)$value, $decimals) : null;
}

/** Windgeschwindigkeit in m/s -> Beaufort (0-12) */
function beaufort(float $ms): int
{
    $limits = [0.3, 1.6, 3.4, 5.5, 8.0, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7];
    foreach ($limits as $bft => $limit) {
        if ($ms < $limit) {
            return $bft;
        }
    }
    return 12;
}

/** Windrichtung in Grad -> N, NNO, NO, ... */
function himmelsrichtung(float $grad): string
{
    $namen = ['N', 'NNO', 'NO', 'ONO', 'O', 'OSO', 'SO', 'SSO',
              'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
    return $namen[(int)round(fmod($grad + 360, 360) / 22.5) % 16];
}

/** Gefühlte Temperatur: Windchill bei Kälte und Wind, Hitzeindex bei Hitze, sonst die gemessene Temperatur */
function gefuehlt(?float $t, ?float $rh, ?float $windMs): ?float
{
    if ($t === null) {
        return null;
    }
    $kmh = ($windMs ?? 0) * 3.6;
    if ($t <= 10 && $kmh >= 5) {
        $v = pow($kmh, 0.16);
        return round(13.12 + 0.6215 * $t - 11.37 * $v + 0.3965 * $t * $v, 1);
    }
    if ($t >= 27 && $rh !== null && $rh >= 40) {
        $f  = $t * 9 / 5 + 32;
        $hi = -42.379 + 2.04901523 * $f + 10.14333127 * $rh - 0.22475541 * $f * $rh
            - 0.00683783 * $f * $f - 0.05481717 * $rh * $rh + 0.00122874 * $f * $f * $rh
            + 0.00085282 * $f * $rh * $rh - 0.00000199 * $f * $f * $rh * $rh;
        return round(($hi - 32) * 5 / 9, 1);
    }
    return $t;
}

/** Luftdruck an der Station -> Luftdruck auf Meereshöhe */
function luftdruckMeereshoehe(?float $hpa, ?float $t): ?float
{
    if ($hpa === null) {
        return null;
    }
    $t  = $t ?? 10.0;
    $gh = 0.0065 * HOEHE_M;
    return round($hpa * pow(1 - $gh / ($t + $gh + 273.15), -5.257), 1);
}

function uvStufe(?float $uv): ?string
{
    if ($uv === null) {
        return null;
    }
    if ($uv < 3) {
        return 'niedrig';
    }
    if ($uv < 6) {
        return 'mäßig';
    }
    if ($uv < 8) {
        return 'hoch';
    }
    if ($uv < 11) {
        return 'sehr hoch';
    }
    return 'extrem';
}


// ------------------------------------------------------------
// Daten zusammenstellen
// ------------------------------------------------------------

function buildData(): array
{
    // Letzte Werte beider Geräte
    $latest = trackerGet('/api/export?devices=' . rawurlencode(HALLE_CODE . ',' . WETTER_CODE));

    // Tageswerte der Wetterstation seit Mitternacht (Min/Max)
    $today = (new DateTime('now', new DateTimeZone(ZEITZONE)))->format('Y-m-d');
    $day   = [];
    try {
        $agg = trackerGet('/api/export?' . http_build_query([
            'devices'  => WETTER_CODE,
            'from'     => $today,
            'interval' => '1d',
            'agg'      => 'min,max',
            'metrics'  => 'temperature,rain_total',
        ]));
        $wsDay = findDevice($agg, WETTER_CODE);
        $day   = $wsDay['readings'][0]['values'] ?? [];
    } catch (RuntimeException $e) {
        // Tageswerte sind optional, die aktuellen Werte reichen
    }

    // Regenmenge der letzten Stunde. Der Regensensor meldet "nass" auch dann noch,
    // wenn es längst aufgehört hat (die Oberfläche trocknet bei Kälte stundenlang nicht ab).
    // Als "es regnet" zählt deshalb nur, wenn der Zähler in dieser Zeit gestiegen ist.
    $rainLastHour = null;
    try {
        $recent  = trackerGet('/api/export?' . http_build_query([
            'devices' => WETTER_CODE,
            'from'    => REGEN_FENSTER,
            'metrics' => 'rain_total',
        ]));
        $wsRecent = findDevice($recent, WETTER_CODE);
        $prev = null;
        $rainLastHour = 0.0;
        foreach ($wsRecent['readings'] ?? [] as $reading) {
            $value = $reading['values']['rain_total'] ?? null;
            if (!is_numeric($value)) {
                continue;
            }
            // nur Anstiege zählen, damit ein zurückgesetzter Zähler nicht als Regen gilt
            if ($prev !== null && $value > $prev) {
                $rainLastHour += $value - $prev;
            }
            $prev = (float)$value;
        }
    } catch (RuntimeException $e) {
        // ohne Verlauf bleibt nur die Meldung des Regensensors
    }

    $result = [
        'erzeugt' => (new DateTime('now', new DateTimeZone(ZEITZONE)))->format(DATE_ATOM),
        'halle'   => null,
        'wetter'  => null,
    ];

    $ht = findDevice($latest, HALLE_CODE);
    if ($ht !== null) {
        $v = $ht['values'] ?? [];
        $result['halle'] = [
            'name'        => $ht['name'] ?? null,
            'online'      => $ht['online'] ?? null,
            'stand'       => $ht['ts'] ?? null,
            'temperatur'  => num($v['temperature'] ?? null),
            'luftfeuchte' => num($v['humidity'] ?? null, 0),
            'batterie'    => num($v['battery'] ?? null, 0),
        ];
    }

    $ws = findDevice($latest, WETTER_CODE);
    if ($ws !== null) {
        $v     = $ws['values'] ?? [];
        $temp  = num($v['temperature'] ?? null);
        $rh    = num($v['humidity'] ?? null, 0);
        $wind  = num($v['wind_speed'] ?? null);
        $boeen = num($v['wind_gust'] ?? null);
        $dir   = num($v['wind_direction'] ?? null, 0);
        $uv    = num($v['uv_index'] ?? null);
        $rain  = num($v['rain_total'] ?? null);
        $rainMin = num($day['rain_total_min'] ?? null);
        $nass  = isset($v['rain_detected']) ? ((float)$v['rain_detected'] > 0) : null;
        // Ohne Verlauf (Abruf fehlgeschlagen) wie bisher nur nach dem Sensor gehen
        $regnet = $nass === null ? null : ($nass && ($rainLastHour === null || $rainLastHour > 0));

        $result['wetter'] = [
            'name'              => $ws['name'] ?? null,
            'online'            => $ws['online'] ?? null,
            'stand'             => $ws['ts'] ?? null,
            'temperatur'        => $temp,
            'gefuehlt'          => gefuehlt($temp, $rh, $wind),
            'temp_min_heute'    => num($day['temperature_min'] ?? null),
            'temp_max_heute'    => num($day['temperature_max'] ?? null),
            'luftfeuchte'       => $rh,
            'taupunkt'          => num($v['dewpoint'] ?? null),
            'luftdruck'         => luftdruckMeereshoehe(num($v['pressure'] ?? null), $temp),
            'wind_kmh'          => $wind !== null ? round($wind * 3.6) : null,
            'boeen_kmh'         => $boeen !== null ? round($boeen * 3.6) : null,
            'beaufort'          => $wind !== null ? beaufort($wind) : null,
            'wind_richtung_grad' => $dir,
            'wind_richtung'     => $dir !== null ? himmelsrichtung($dir) : null,
            'regen_aktiv'       => $regnet,
            'sensor_nass'       => $nass,
            'regen_letzte_stunde_mm' => $rainLastHour !== null ? round($rainLastHour, 1) : null,
            // Regenzähler läuft fortlaufend hoch: Menge heute = aktueller Stand - Stand um Mitternacht
            'regen_heute_mm'    => ($rain !== null && $rainMin !== null) ? round(max(0, $rain - $rainMin), 1) : null,
            'uv_index'          => $uv,
            'uv_stufe'          => uvStufe($uv),
            'helligkeit_lux'    => num($v['illuminance'] ?? null, 0),
        ];
    }

    return $result;
}


// ------------------------------------------------------------
// Ablauf
// ------------------------------------------------------------

date_default_timezone_set(ZEITZONE);

if (cacheIsFresh($cacheFile)) {
    sendJson((string)file_get_contents($cacheFile));
}

// Nur ein Aufruf gleichzeitig fragt den Tracker ab,
// die anderen warten kurz und lesen dann den neuen Cache.
$lock = fopen($lockFile, 'c');
if ($lock !== false) {
    flock($lock, LOCK_EX);
}

if (cacheIsFresh($cacheFile)) {
    sendJson((string)file_get_contents($cacheFile));
}

try {
    $json = json_encode(buildData(), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);

    $tmpFile = $cacheFile . '.tmp';
    if (file_put_contents($tmpFile, $json) !== false) {
        rename($tmpFile, $cacheFile);
    }
    sendJson($json);
} catch (RuntimeException $e) {
    error_log('shelly-api: ' . $e->getMessage());

    // Tracker gerade nicht erreichbar: letzten Stand weiter ausliefern.
    // Die Seite erkennt am Zeitstempel "stand", wenn die Werte veraltet sind.
    if (is_file($cacheFile)) {
        touch($cacheFile);  // nicht bei jedem Aufruf erneut versuchen
        sendJson((string)file_get_contents($cacheFile));
    }
    sendJson(json_encode(['fehler' => 'Daten aktuell nicht verfügbar'], JSON_UNESCAPED_UNICODE), 502);
}
