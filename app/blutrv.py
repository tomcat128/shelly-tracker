"""Shelly BLU TRV: status of one thermostat, taken from its BLU Gateway Gen3.

A BLU TRV is not a cloud device of its own. It is a component of the gateway
(blutrv:<n>), so the poller fetches the gateway and builds a separate status per TRV:

- blutrv_rstatus:<n>   full TRV status (valve position, target/current temperature).
                       The gateway only refreshes it every now and then (often 30+ minutes).
- bthomesensor:<k>     BTHome values the TRV broadcasts about every minute. Which sensor
                       belongs to which TRV is only in the gateway *settings* (addr/obj_id/idx).
- blutrv:<n>           battery, signal strength and the time of the last BLE packet.

The result looks like the status of a standalone device, so the usual paths and aliases
apply ("temperature:0.tC", "devicepower:0.battery.percent", "trv:0.pos", ...).
"""
from __future__ import annotations

import time

# BTHome object ids (see bthome.io): 0x01 battery, 0x45 temperature (0.1 °C).
# The TRV sends two temperatures: index 0 = target, index 1 = measured.
BTHOME_TEMPERATURE = 0x45
TARGET_IDX = 0
CURRENT_IDX = 1

# Without a BLE packet for this long the TRV counts as offline
OFFLINE_AFTER_SECONDS = 2 * 3600


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _bthome_temperatures(gw_status: dict, gw_settings: dict | None, addr: str | None) -> dict:
    """{idx: (value, last_updated_ts)} of the TRV's BTHome temperature sensors."""
    if not gw_settings or not addr:
        return {}
    result = {}
    for key, cfg in gw_settings.items():
        if not key.startswith("bthomesensor:") or not isinstance(cfg, dict):
            continue
        if str(cfg.get("addr", "")).lower() != addr or cfg.get("obj_id") != BTHOME_TEMPERATURE:
            continue
        sensor = gw_status.get(key) or {}
        value = _num(sensor.get("value"))
        if value is not None:
            result[cfg.get("idx")] = (value, _num(sensor.get("last_updated_ts")) or 0)
    return result


def trv_status(
    gw_status: dict, component: int, gw_settings: dict | None = None, now: float | None = None
) -> tuple[dict, bool, str | None] | None:
    """Status of TRV blutrv:<component>: (status, online, model), or None if the gateway lacks it."""
    info = gw_status.get(f"blutrv:{component}")
    if not isinstance(info, dict):
        return None
    rstatus = gw_status.get(f"blutrv_rstatus:{component}") or {}
    full = rstatus.get("status") or {}
    rstatus_ts = _num(rstatus.get("ts")) or 0

    status: dict = {
        "temperature:0": {k: v for k, v in (full.get("temperature:0") or {}).items() if k != "errors"},
        "trv:0": {k: v for k, v in (full.get("trv:0") or {}).items() if k != "errors"},
    }
    if _num(info.get("battery")) is not None:
        status["devicepower:0"] = {"battery": {"percent": info["battery"]}}
    if _num(info.get("rssi")) is not None:
        status["rssi"] = info["rssi"]

    # Newer BTHome values replace the older ones from the full status
    trv_cfg = (gw_settings or {}).get(f"blutrv:{component}") or {}
    addr = str(trv_cfg.get("addr") or "").lower() or None
    temps = _bthome_temperatures(gw_status, gw_settings, addr)
    if TARGET_IDX in temps and temps[TARGET_IDX][1] >= rstatus_ts:
        status["trv:0"]["target_C"] = temps[TARGET_IDX][0]
    if CURRENT_IDX in temps and temps[CURRENT_IDX][1] >= rstatus_ts:
        current = temps[CURRENT_IDX][0]
        status["temperature:0"]["tC"] = current
        status["temperature:0"]["tF"] = round(current * 9 / 5 + 32, 2)
        if not trv_cfg.get("temp_sensors"):  # regulates on its own sensor
            status["trv:0"]["current_C"] = current

    last_packet = _num(info.get("last_updated_ts")) or 0
    newest = max([last_packet, rstatus_ts] + [ts for _, ts in temps.values()])
    if newest > 0:
        status["ts"] = int(newest)
    now = time.time() if now is None else now
    online = last_packet > 0 and now - last_packet < OFFLINE_AFTER_SECONDS

    model = ((gw_status.get(f"blutrv_rinfo:{component}") or {}).get("device_info") or {}).get("model")
    return status, online, model
