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
