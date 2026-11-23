"""Configuration: environment variables + device definitions from YAML."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Status keys that are noise for a time series (connectivity, firmware, counters).
DEFAULT_EXCLUDE = [
    "sys.*",
    "wifi.*",
    "cloud.*",
    "mqtt.*",
    "ws.*",
    "ble.*",
    "eth.*",
    "script:*",
    "reporter.*",
    "packetid:*",
    "fwversion:*",
    "serial",
    "id",
    "_updated",
    "ts",
    "*.ts",
    "*_ts",
    "*.unixtime",
    "*.id",
    "*.minute_ts",
    "*.by_minute.*",
]


def normalize_device_id(device_id: str) -> str:
    """Hex ids are case-insensitive; BLE/other ids (starting with 'X') are kept as is."""
    device_id = str(device_id).strip()
    if device_id[:1] in ("X", "x") and not _is_hex(device_id):
        return device_id
    return device_id.lower()


def _is_hex(value: str) -> bool:
    try:
        int(value, 16)
        return True
    except ValueError:
        return False


@dataclass
class DeviceConfig:
    id: str
    code: str
    name: str = ""
    aliases: dict[str, str] = field(default_factory=dict)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


@dataclass
class Settings:
    shelly_host: str
    shelly_auth_key: str
    shelly_api_version: str = "v2"
    poll_interval: int = 60
    database_url: str = "sqlite:////data/shelly.db"
    config_file: str = "/config/devices.yaml"
    api_token: str = ""
    store_raw: bool = False
    skip_unchanged: bool = True
    request_timeout: float = 15.0
    timezone: str = "Europe/Berlin"
    raw_retention_months: int = 18  # older minute data is reduced to hourly stats; 0 = keep forever
    devices: list[DeviceConfig] = field(default_factory=list)
    global_aliases: dict[str, str] = field(default_factory=dict)
    global_exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))

    @property
    def base_url(self) -> str:
        host = self.shelly_host.strip().rstrip("/")
        if not host.startswith(("http://", "https://")):
            host = "https://" + host
        return host

    def aliases_for(self, device_id: str) -> dict[str, str]:
        merged = dict(self.global_aliases)
        for dev in self.devices:
            if dev.id == device_id:
                merged.update(dev.aliases)
        return merged


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if value == "":
        return default
    number = int(value)
    if number < 0:
        raise ValueError(f"{name} must be >= 0")
    return number


def load_devices(path: str) -> tuple[list[DeviceConfig], dict[str, str], list[str] | None]:
    file = Path(path)
    if not file.exists():
        return [], {}, None
    data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}

    devices: list[DeviceConfig] = []
    seen_codes: set[str] = set()
    for entry in data.get("devices") or []:
        if not entry.get("id") or not entry.get("code"):
            raise ValueError(f"Device entry needs 'id' and 'code': {entry!r}")
        code = str(entry["code"]).strip()
        if code.lower() in seen_codes:
            raise ValueError(f"Duplicate device code: {code}")
        seen_codes.add(code.lower())
        devices.append(
            DeviceConfig(
                id=normalize_device_id(entry["id"]),
                code=code,
                name=str(entry.get("name") or ""),
                aliases={str(k): str(v) for k, v in (entry.get("aliases") or {}).items()},
                include=[str(p) for p in entry.get("include") or []],
                exclude=[str(p) for p in entry.get("exclude") or []],
            )
        )

    aliases = {str(k): str(v) for k, v in (data.get("aliases") or {}).items()}
    exclude = data.get("exclude")
    return devices, aliases, ([str(p) for p in exclude] if exclude is not None else None)


def load_settings() -> Settings:
    settings = Settings(
        shelly_host=os.environ.get("SHELLY_HOST", ""),
        shelly_auth_key=os.environ.get("SHELLY_AUTH_KEY", ""),
        shelly_api_version=os.environ.get("SHELLY_API_VERSION", "v2").lower(),
        poll_interval=int(os.environ.get("POLL_INTERVAL_SECONDS", "60")),
        database_url=os.environ.get("DATABASE_URL", "sqlite:////data/shelly.db"),
        config_file=os.environ.get("CONFIG_FILE", "/config/devices.yaml"),
        api_token=os.environ.get("API_TOKEN", ""),
        store_raw=_bool("STORE_RAW", False),
        skip_unchanged=_bool("SKIP_UNCHANGED", True),
        request_timeout=float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "15")),
        timezone=os.environ.get("TIMEZONE", "Europe/Berlin"),
        raw_retention_months=_int("RAW_RETENTION_MONTHS", 18),
    )
    devices, aliases, exclude = load_devices(settings.config_file)
    settings.devices = devices
    settings.global_aliases = aliases
    if exclude is not None:
        settings.global_exclude = exclude
    return settings
