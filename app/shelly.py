"""Client for the Shelly Cloud Control API (v2 with v1 fallback)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from .config import normalize_device_id

log = logging.getLogger(__name__)

V2_MAX_IDS = 10
MIN_REQUEST_GAP = 1.1  # cloud limit: 1 request / second


@dataclass
class DeviceStatus:
    id: str
    online: bool | None
    status: dict
    model: str | None = None
    gen: str | None = None


class ShellyCloudError(RuntimeError):
    pass


class ShellyCloud:
    def __init__(
        self,
        base_url: str,
        auth_key: str,
        api_version: str = "v2",
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url
        self.auth_key = auth_key
        self.api_version = api_version
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)
        self._last_request = 0.0

    def close(self) -> None:
        self._client.close()

    def _throttle(self) -> None:
        wait = MIN_REQUEST_GAP - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _post(self, url: str, **kwargs) -> httpx.Response:
        for attempt in range(3):
            self._throttle()
            resp = self._client.post(url, **kwargs)
            if resp.status_code == 429:
                log.warning("Rate limited by Shelly Cloud, backing off")
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise ShellyCloudError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            return resp
        raise ShellyCloudError("Rate limit: too many retries")

    def get_statuses(self, device_ids: list[str]) -> list[DeviceStatus]:
        if self.api_version == "v1":
            return [s for d in device_ids if (s := self._get_v1(d)) is not None]
        result: list[DeviceStatus] = []
        for i in range(0, len(device_ids), V2_MAX_IDS):
            result.extend(self._get_v2(device_ids[i : i + V2_MAX_IDS]))
        return result

    def _get_v2(self, ids: list[str]) -> list[DeviceStatus]:
        resp = self._post(
            "/v2/devices/api/get",
            params={"auth_key": self.auth_key},
            json={"ids": ids, "select": ["status"]},
        )
        data = resp.json()
        if not isinstance(data, list):
            raise ShellyCloudError(f"Unexpected v2 response: {str(data)[:300]}")
        return [
            DeviceStatus(
                id=normalize_device_id(item.get("id", "")),
                online=bool(item["online"]) if "online" in item else None,
                status=item.get("status") or {},
                model=item.get("code"),
                gen=item.get("gen"),
            )
            for item in data
        ]

    def _get_v1(self, device_id: str) -> DeviceStatus | None:
        resp = self._post(
            "/device/status", data={"id": device_id, "auth_key": self.auth_key}
        )
        body = resp.json()
        if not body.get("isok"):
            log.error("Shelly v1 error for %s: %s", device_id, body.get("errors"))
            return None
        data = body.get("data") or {}
        status = data.get("device_status") or {}
        return DeviceStatus(
            id=normalize_device_id(device_id),
            online=data.get("online"),
            status=status,
            model=status.get("code") if isinstance(status.get("code"), str) else None,
        )
