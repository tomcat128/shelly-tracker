import json
import os

os.environ["SHELLY_TRACKER_NO_AUTOAPP"] = "1"

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import DEFAULT_EXCLUDE, DeviceConfig, Settings
from app.main import create_app
from app.metrics import device_timestamp, extract_metrics
from app.shelly import ShellyCloud

WS90 = "XB1234567890"
HT = "7c2c67aabbcc"


class FakeCloud:
    """Mock Shelly Cloud v2 endpoint with controllable device status."""

    def __init__(self):
        self.statuses = {
            WS90: {"temperature:0": {"id": 0, "tC": 5.2}, "humidity:0": {"id": 0, "rh": 80},
                   "bthomesensor:200": {"id": 200, "value": 3.4}, "ts": 1763884800},
            HT.upper(): {"temperature:0": {"id": 0, "tC": 21.0}, "humidity:0": {"id": 0, "rh": 45},
                         "devicepower:0": {"id": 0, "battery": {"percent": 99}},
                         "sys": {"uptime": 100}},
        }
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/devices/api/get"
        assert request.url.params["auth_key"] == "secret"
        body = json.loads(request.content)
        self.requests.append(body)
        out = []
        for dev_id in body["ids"]:
            key = next(k for k in self.statuses if k.lower() == dev_id.lower())
            out.append({"id": key, "code": "X", "gen": "GBLE", "online": 1,
                        "status": self.statuses[key]})
        return httpx.Response(200, json=out)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("app.shelly.MIN_REQUEST_GAP", 0)
    fake = FakeCloud()
    settings = Settings(
        shelly_host="cloud.example", shelly_auth_key="secret",
        database_url=f"sqlite:///{tmp_path}/test.db", timezone="UTC",
        devices=[DeviceConfig(id=WS90, code="WS90", name="Wetter",
                              aliases={"bthomesensor:200.value": "wind_speed"}),
                 DeviceConfig(id=HT, code="HT1")],
        global_aliases={"temperature:0.tC": "temperature", "humidity:0.rh": "humidity"},
    )
    cloud = ShellyCloud(settings.base_url, "secret", transport=httpx.MockTransport(fake.handler))
    app = create_app(settings, cloud=cloud, start_poller=False)
    with TestClient(app) as client:
        yield client, fake, app


def test_flatten_and_exclude():
    status = {"temperature:0": {"id": 0, "tC": 1.5}, "sys": {"uptime": 3},
              "switch:0": {"output": True, "aenergy": {"by_minute": [1, 2], "minute_ts": 9}}}
    assert extract_metrics(status, DEFAULT_EXCLUDE) == {
        "temperature:0.tC": 1.5, "switch:0.output": 1.0}
    assert "sys.uptime" in extract_metrics(status, DEFAULT_EXCLUDE, ["sys.uptime"])


def test_device_timestamp():
    assert device_timestamp({"_updated": "2026-11-23 08:00:00"}).hour == 8
    assert device_timestamp({"sys": {"unixtime": 1763884800}}).year == 2025
    assert device_timestamp({}) is None


def test_poll_and_latest_export(env):
    client, fake, app = env
    assert client.post("/api/poll").json()["stored"] == 2
    assert len(fake.requests) == 1  # both devices in one v2 request

    data = client.get("/api/export").json()
    assert data["mode"] == "latest"
    by_code = {d["code"]: d for d in data["devices"]}
    assert by_code["WS90"]["values"] == {"humidity": 80.0, "temperature": 5.2, "wind_speed": 3.4}
    assert by_code["HT1"]["values"]["devicepower:0.battery.percent"] == 99.0
    assert "sys.uptime" not in by_code["HT1"]["values"]


def test_dedup_and_range(env):
    client, fake, app = env
    client.post("/api/poll")
    # nothing changed -> nothing new (WS90 same device ts, HT same values)
    assert client.post("/api/poll").json()["stored"] == 0

    fake.statuses[WS90]["ts"] += 60
    fake.statuses[WS90]["temperature:0"]["tC"] = 6.0
    fake.statuses[HT.upper()]["temperature:0"]["tC"] = 21.5
    assert client.post("/api/poll").json()["stored"] == 2

    data = client.get("/api/export", params={"devices": "ws90", "from": "2020-01-01",
                                             "metrics": "temp*"}).json()
    assert data["mode"] == "range"
    assert len(data["devices"]) == 1
    readings = data["devices"][0]["readings"]
    assert [r["values"] for r in readings] == [{"temperature": 5.2}, {"temperature": 6.0}]
    assert readings[0]["ts"] == "2025-11-23T08:00:00+00:00"

    csv_text = client.get("/api/export", params={"from": "30d", "format": "csv-wide",
                                                 "devices": "HT1"}).text
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("timestamp;device;device_id;")
    assert len(lines) == 3


def test_unknown_device_and_token(env):
    client, fake, app = env
    assert client.get("/api/export", params={"devices": "nope"}).status_code == 404
    app.state.settings.api_token = "t0ken"
    assert client.get("/api/export").status_code == 401
    assert client.get("/api/export", headers={"Authorization": "Bearer t0ken"}).status_code == 200
    assert client.get("/api/export?token=t0ken").status_code == 200
    assert client.get("/health").status_code == 200


def test_metrics_endpoint(env):
    client, fake, app = env
    client.post("/api/poll")
    rows = {r["path"]: r for r in client.get("/api/devices/HT1/metrics").json()}
    assert rows["temperature:0.tC"]["alias"] == "temperature"
    assert rows["sys.uptime"]["stored"] is False
    assert client.get("/api/devices/HT1/raw").json()["sys"]["uptime"] == 100


def test_real_ws90_payload_filtering():
    status = {"precipitation:0": {"id": 0, "value": 8.2, "last_1h": -999, "last_24h": -999},
              "reporter": {"id": "1415", "rssi": -94}, "packetid:0": {"id": 0, "pid": 97},
              "fwversion:0": {"id": 0, "version": "1.1.4"}, "pressure_slope:0": {"value": None},
              "devicepower:0": {"battery": {"V": None, "percent": 93}}, "serial": 59363,
              "_updated": "2026-11-23 07:53:58"}
    assert extract_metrics(status, DEFAULT_EXCLUDE) == {
        "precipitation:0.value": 8.2, "devicepower:0.battery.percent": 93.0}


def test_index_page(env):
    client, fake, app = env
    client.post("/api/poll")
    page = client.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "badge ok" in page.text and "WS90" in page.text and 'href="api/export"' in page.text
    app.state.settings.api_token = "t0ken"
    page = client.get("/")
    assert page.status_code == 200 and "WS90" not in page.text and "geschützt" in page.text


# -- schema migration & aggregation -------------------------------------------
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.db import Device, Measurement, Reading, init_db, make_engine

OLD_SCHEMA = """
CREATE TABLE devices (id VARCHAR(64) NOT NULL PRIMARY KEY, code VARCHAR(64) NOT NULL, name VARCHAR(255) NOT NULL,
  model VARCHAR(64), gen VARCHAR(16), online BOOLEAN, active BOOLEAN NOT NULL, last_seen DATETIME,
  last_raw TEXT, created_at DATETIME NOT NULL);
CREATE UNIQUE INDEX ix_devices_code ON devices (code);
CREATE TABLE readings (id INTEGER NOT NULL PRIMARY KEY, device_id VARCHAR(64) NOT NULL REFERENCES devices (id),
  ts DATETIME NOT NULL, received_at DATETIME NOT NULL, online BOOLEAN, values_hash VARCHAR(64), raw TEXT,
  CONSTRAINT uq_reading_device_ts UNIQUE (device_id, ts));
CREATE INDEX ix_reading_device_ts ON readings (device_id, ts);
CREATE TABLE measurements (reading_id INTEGER NOT NULL REFERENCES readings (id) ON DELETE CASCADE,
  metric VARCHAR(128) NOT NULL, value FLOAT NOT NULL, PRIMARY KEY (reading_id, metric));
CREATE INDEX ix_measurement_metric ON measurements (metric);
INSERT INTO devices VALUES ('d1','D1','',NULL,NULL,1,1,NULL,NULL,'2026-01-01 00:00:00.000000');
INSERT INTO readings VALUES (1,'d1','2026-01-01 00:00:00.000000','2026-01-01 00:00:00.000000',1,NULL,NULL);
INSERT INTO measurements VALUES (1,'temperature:0.tC',3.5),(1,'humidity:0.rh',80);
"""


def test_migration_from_old_schema(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(OLD_SCHEMA)
    con.close()

    engine = make_engine(f"sqlite:///{path}")
    init_db(engine)
    init_db(engine)  # idempotent
    engine.dispose()

    con = sqlite3.connect(path)
    ddl = con.execute("select sql from sqlite_master where name='measurements'").fetchone()[0]
    indexes = {r[0] for r in con.execute("select name from sqlite_master where type='index'")}
    assert "WITHOUT ROWID" in ddl.upper()
    assert "ix_measurement_metric" not in indexes and "ix_reading_device_ts" not in indexes
    assert sorted(con.execute("select metric, value from measurements")) == [
        ("humidity:0.rh", 80.0), ("temperature:0.tC", 3.5)]
    assert con.execute("pragma foreign_key_check").fetchall() == []


def _fill(app, device_id, start, minutes, value_fn):
    with app.state.session_factory() as s:
        for i in range(minutes):
            r = Reading(device_id=device_id, ts=start + timedelta(minutes=i), received_at=start)
            r.measurements = [Measurement(metric="temperature:0.tC", value=value_fn(i)),
                              Measurement(metric="humidity:0.rh", value=50.0)]
            s.add(r)
        s.commit()


def test_aggregate_hourly(env):
    client, fake, app = env
    _fill(app, HT, datetime(2026, 11, 20, 10, 0), 180, lambda i: float(i))  # 3 hours, UTC

    data = client.get("/api/export", params={"devices": "HT1", "from": "2026-11-20",
                                             "interval": "1h", "agg": "avg,min,max",
                                             "metrics": "temperature"}).json()
    assert data["mode"] == "aggregate" and data["interval"] == "1h"
    rows = data["devices"][0]["readings"]
    assert [r["ts"] for r in rows] == ["2026-11-20T10:00:00+00:00", "2026-11-20T11:00:00+00:00",
                                       "2026-11-20T12:00:00+00:00"]
    assert rows[0]["readings"] == 60
    assert rows[0]["values"] == {"temperature_avg": 29.5, "temperature_max": 59.0, "temperature_min": 0.0}
    assert rows[2]["values"]["temperature_min"] == 120.0

    single = client.get("/api/export", params={"devices": "HT1", "from": "2026-11-20",
                                               "interval": "15m", "order": "desc", "limit": 2}).json()
    rows = single["devices"][0]["readings"]
    assert len(rows) == 2 and rows[0]["ts"] == "2026-11-20T12:45:00+00:00"
    assert rows[0]["values"] == {"humidity": 50.0, "temperature": 172.0}


def test_aggregate_daily_local_time_and_csv(env):
    client, fake, app = env
    app.state.tz = ZoneInfo("Europe/Berlin")
    # 22:30-01:30 UTC on 20./21.11. = 23:30-02:30 local -> local day 20.11. gets the first 30 min
    _fill(app, HT, datetime(2026, 11, 20, 22, 30), 180, lambda i: 1.0 if i < 30 else 2.0)

    data = client.get("/api/export", params={"devices": "HT1", "from": "2026-11-20", "to": "2026-11-21",
                                             "interval": "1d", "metrics": "temperature"}).json()
    rows = data["devices"][0]["readings"]
    assert [(r["ts"], r["readings"], r["values"]["temperature"]) for r in rows] == [
        ("2026-11-20T00:00:00+01:00", 30, 1.0), ("2026-11-21T00:00:00+01:00", 150, 2.0)]

    month = client.get("/api/export", params={"from": "2026-11-01", "interval": "1mo",
                                              "devices": "HT1", "format": "csv-wide"}).text
    lines = month.strip().splitlines()
    assert lines[0] == "timestamp;device;device_id;humidity;temperature"
    assert lines[1].startswith("2026-11-01T00:00:00+01:00;HT1;")


def test_aggregate_validation(env):
    client, fake, app = env
    assert client.get("/api/export", params={"interval": "1h"}).status_code == 400
    assert client.get("/api/export", params={"from": "1d", "interval": "3x"}).status_code == 400
    assert client.get("/api/export", params={"from": "1d", "interval": "1h", "agg": "median"}).status_code == 400


# -- rollup of old minute data to hourly statistics ----------------------------
from app.config import load_settings
from app.db import HourlyStat
from app.rollup import retention_cutoff, rollup, subtract_months


def test_retention_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "none.yaml"))
    monkeypatch.delenv("RAW_RETENTION_MONTHS", raising=False)
    assert load_settings().raw_retention_months == 18
    monkeypatch.setenv("RAW_RETENTION_MONTHS", "")
    assert load_settings().raw_retention_months == 18
    monkeypatch.setenv("RAW_RETENTION_MONTHS", "24")
    assert load_settings().raw_retention_months == 24
    monkeypatch.setenv("RAW_RETENTION_MONTHS", "0")
    assert load_settings().raw_retention_months == 0


def test_subtract_months():
    assert subtract_months(datetime(2026, 11, 23, 10, 30), 18) == datetime(2025, 5, 23, 10, 30)
    assert subtract_months(datetime(2026, 5, 31), 3) == datetime(2026, 2, 28)
    assert retention_cutoff(18, datetime(2026, 11, 23, 10, 30, 5)) == datetime(2025, 5, 23, 10, 0)


def test_rollup_replaces_old_minute_data(env):
    client, fake, app = env
    now = datetime(2026, 11, 23, 12, 0)
    old_start = datetime(2025, 5, 20, 22, 30)  # 2,5 h of old data, crosses midnight
    _fill(app, HT, old_start, 150, lambda i: float(i))
    _fill(app, HT, datetime(2026, 11, 23, 10, 0), 60, lambda i: 100.0)  # recent

    res = rollup(app.state.session_factory, 18, now=now)
    # hourly stats for all completed hours (3 old + 1 recent) x 2 metrics; old minute data removed
    assert res["readings"] == 150 and res["hours"] == 4 * 2
    assert rollup(app.state.session_factory, 18, now=now)["readings"] == 0  # idempotent

    with app.state.session_factory() as s:
        assert s.query(Reading).filter(Reading.device_id == HT).count() == 60
        stats = {(h.hour, h.metric): h for h in s.query(HourlyStat)}
    first = stats[(datetime(2025, 5, 20, 22, 0), "temperature:0.tC")]
    assert (first.count, first.sum, first.min, first.max) == (30, sum(range(30)), 0.0, 29.0)
    last = stats[(datetime(2025, 5, 21, 0, 0), "temperature:0.tC")]
    assert (last.count, last.min, last.max) == (60, 90.0, 149.0)

    # aggregated export over old (hourly) and recent (minute) data
    data = client.get("/api/export", params={"devices": "HT1", "from": "2025-05-20", "interval": "1h",
                                             "metrics": "temperature", "agg": "avg,max"}).json()
    rows = data["devices"][0]["readings"]
    assert [(r["ts"], r["readings"]) for r in rows] == [
        ("2025-05-20T22:00:00+00:00", 30), ("2025-05-20T23:00:00+00:00", 60),
        ("2025-05-21T00:00:00+00:00", 60), ("2026-11-23T10:00:00+00:00", 60)]
    assert rows[0]["values"] == {"temperature_avg": 14.5, "temperature_max": 29.0}
    assert rows[3]["values"] == {"temperature_avg": 100.0, "temperature_max": 100.0}

    yearly = client.get("/api/export", params={"devices": "HT1", "from": "2025-01-01", "interval": "1mo",
                                               "metrics": "temperature"}).json()
    assert [r["readings"] for r in yearly["devices"][0]["readings"]] == [150, 60]

    # raw export points to the hourly data
    raw = client.get("/api/export", params={"devices": "HT1", "from": "2025-05-01"}).json()
    assert raw["devices"][0]["count"] == 60
    assert raw["devices"][0]["hourly_only_before"] == "2026-11-23T10:00:00+00:00"
    assert "interval=1h" in raw["note"]
    assert "note" not in client.get("/api/export", params={"devices": "HT1", "from": "2026-11-01"}).json()


def test_rollup_disabled_keeps_minute_data(env):
    client, fake, app = env
    _fill(app, HT, datetime(2020, 1, 1), 10, lambda i: 1.0)
    res = rollup(app.state.session_factory, 0)
    assert res["readings"] == 0 and res["hours"] == 2  # hourly stats are still built
    with app.state.session_factory() as s:
        assert s.query(Reading).count() == 10
    raw = client.get("/api/export", params={"devices": "HT1", "from": "2019-12-31"}).json()
    assert raw["devices"][0]["count"] == 10 and "note" not in raw


def test_hourly_stats_give_same_results_as_minute_data(env):
    client, fake, app = env
    app.state.tz = ZoneInfo("Europe/Berlin")
    _fill(app, HT, datetime(2026, 3, 28, 20, 0), 60 * 30, lambda i: (i % 97) / 3)  # crosses DST change
    queries = [
        {"interval": "1h", "agg": "avg,min,max"}, {"interval": "6h"}, {"interval": "1d", "agg": "avg,max"},
        {"interval": "1w"}, {"interval": "1mo", "agg": "min"}, {"interval": "15m"},
    ]
    def run():
        return [client.get("/api/export", params={"devices": "HT1", "from": "2026-03-01", **q}).json()
                ["devices"][0]["readings"] for q in queries]
    before = run()
    assert rollup(app.state.session_factory, 0, now=datetime(2026, 4, 1))["hours"] == 30 * 2
    assert run() == before


def test_rollup_endpoint(env):
    client, fake, app = env
    _fill(app, HT, datetime(2020, 1, 1), 10, lambda i: 1.0)
    res = client.post("/api/rollup").json()
    assert res["readings_removed"] == 10 and res["hourly_values_written"] == 2 and res["error"] is None


# ---------- BLU TRV at a BLU Gateway Gen3 ----------

GW = "b08184e7c6b0"
GW_STATUS = {
    "ts": 1795505246.5,
    "blutrv:200": {"id": 200, "last_updated_ts": 1795505246, "rssi": -50, "battery": 90},
    "blutrv_rinfo:200": {"device_info": {"model": "BluTRV"}},
    "blutrv_rstatus:200": {"ts": 1795503592, "status": {
        "sys": {"uptime": 5, "unixtime": 1795503583},
        "temperature:0": {"id": 0, "tC": 16.73, "tF": 62.12, "errors": []},
        "trv:0": {"id": 0, "pos": 12, "steps": 375695, "current_C": 16.78, "target_C": 15,
                  "schedule_rev": 15, "errors": []}}},
    "bthomesensor:202": {"id": 202, "last_updated_ts": 1795505246, "value": 18},
    "bthomesensor:203": {"id": 203, "last_updated_ts": 1795505246, "value": 16.5},
    "bthomesensor:204": {"id": 204, "last_updated_ts": 1795505246, "value": 21.0},
}
GW_SETTINGS = {
    "blutrv:200": {"id": 200, "addr": "f8:44:77:2a:4d:e9", "temp_sensors": []},
    "bthomesensor:202": {"addr": "f8:44:77:2a:4d:e9", "obj_id": 69, "idx": 0},
    "bthomesensor:203": {"addr": "f8:44:77:2a:4d:e9", "obj_id": 69, "idx": 1},
    "bthomesensor:204": {"addr": "28:68:47:eb:0d:b9", "obj_id": 69, "idx": 1},  # other TRV
}


def test_blutrv_status_prefers_newer_bthome_values():
    from app.blutrv import trv_status

    status, online, model = trv_status(GW_STATUS, 200, GW_SETTINGS, now=1795505300)
    assert online and model == "BluTRV"
    assert extract_metrics(status, DEFAULT_EXCLUDE) == {
        "temperature:0.tC": 16.5, "temperature:0.tF": 61.7, "trv:0.pos": 12.0,
        "trv:0.current_C": 16.5, "trv:0.target_C": 18.0,
        "devicepower:0.battery.percent": 90.0, "rssi": -50.0}
    assert device_timestamp(status).isoformat() == "2026-11-24T07:27:26"

    # without settings (or with older BTHome values) the full TRV status is used
    status, _, _ = trv_status(GW_STATUS, 200, None, now=1795505300)
    assert status["trv:0"]["target_C"] == 15 and status["temperature:0"]["tC"] == 16.73
    # no BLE packet for hours -> offline; unknown component -> None
    assert trv_status(GW_STATUS, 200, GW_SETTINGS, now=1795505246 + 3 * 3600)[1] is False
    assert trv_status(GW_STATUS, 201, GW_SETTINGS) is None


def test_trv_config(tmp_path):
    from app.config import load_devices

    file = tmp_path / "devices.yaml"
    file.write_text(f"devices:\n  - {{id: '{GW.upper()}', trv: 200, code: TRV1}}\n"
                    f"  - {{id: '{GW}', trv: 201, code: TRV2}}\n")
    devices, _, _ = load_devices(str(file))
    assert [(d.id, d.cloud_id, d.trv) for d in devices] == [
        (f"{GW}:blutrv:200", GW, 200), (f"{GW}:blutrv:201", GW, 201)]


def test_poll_trv_from_gateway(tmp_path, monkeypatch):
    from app.config import trv_device_id

    monkeypatch.setattr("app.shelly.MIN_REQUEST_GAP", 0)
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["ids"] == [GW]
        key = body["select"][0]
        value = GW_STATUS if key == "status" else GW_SETTINGS
        return httpx.Response(200, json=[{"id": GW, "code": "S3GW-1DBT001", "gen": "G2",
                                          "online": 1, key: value}])

    settings = Settings(
        shelly_host="cloud.example", shelly_auth_key="secret",
        database_url=f"sqlite:///{tmp_path}/test.db", timezone="UTC",
        devices=[DeviceConfig(id=trv_device_id(GW, 200), cloud_id=GW, trv=200, code="TRV1")],
        global_aliases={"temperature:0.tC": "temperature", "trv:0.target_C": "target_temperature"},
    )
    cloud = ShellyCloud(settings.base_url, "secret", transport=httpx.MockTransport(handler))
    app = create_app(settings, cloud=cloud, start_poller=False)
    with TestClient(app) as client:
        assert client.post("/api/poll").json()["stored"] == 1
        client.post("/api/poll")
        # one status request per poll, settings only once (cached)
        assert [r["select"] for r in requests] == [["status"], ["settings"], ["status"]]
        dev = client.get("/api/export", params={"devices": "TRV1"}).json()["devices"][0]
        assert dev["model"] == "BluTRV"
        assert dev["values"]["temperature"] == 16.5
        assert dev["values"]["target_temperature"] == 18.0
