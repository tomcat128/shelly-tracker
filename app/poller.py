"""Periodically fetch device status from the Shelly Cloud and store it."""
from __future__ import annotations

import json
import logging
import threading
import time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .blutrv import trv_status
from .config import DeviceConfig, Settings
from .db import Device, Measurement, Reading, utcnow
from .metrics import device_timestamp, extract_metrics, values_hash
from .shelly import DeviceStatus, ShellyCloud

log = logging.getLogger(__name__)

# Gateway settings (which BTHome sensor belongs to which BLU TRV) rarely change
GATEWAY_SETTINGS_MAX_AGE = 3600
GATEWAY_SETTINGS_RETRY = 300


def sync_devices(session_factory: sessionmaker, devices: list[DeviceConfig]) -> None:
    """Make the devices table match the YAML config (codes/names); keep history of removed ones."""
    with session_factory() as session:
        configured = {d.id: d for d in devices}
        codes = {d.code.lower(): d.id for d in devices}

        for db_dev in session.scalars(select(Device)):
            if db_dev.id not in configured:
                db_dev.active = False
            owner = codes.get(db_dev.code.lower())
            if owner is not None and owner != db_dev.id:
                db_dev.code = f"_{db_dev.id}"  # free the code for its new owner
        session.flush()

        for cfg in devices:
            db_dev = session.get(Device, cfg.id)
            if db_dev is None:
                db_dev = Device(id=cfg.id)
                session.add(db_dev)
            db_dev.code = cfg.code
            db_dev.name = cfg.name
            db_dev.active = True
        session.commit()


class Poller:
    def __init__(self, settings: Settings, session_factory: sessionmaker, cloud: ShellyCloud):
        self.settings = settings
        self.session_factory = session_factory
        self.cloud = cloud
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.last_run = None
        self.last_error: str | None = None
        self._gw_settings: dict[str, dict] = {}
        self._gw_settings_due = 0.0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="shelly-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def trigger(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._wake.wait(self.settings.poll_interval)
            self._wake.clear()

    # -- work --------------------------------------------------------------
    def poll_once(self) -> int:
        """Fetch all configured devices once. Returns the number of new readings stored."""
        with self._lock:
            by_cloud_id: dict[str, list[DeviceConfig]] = {}
            for cfg in self.settings.devices:
                by_cloud_id.setdefault(cfg.cloud_id, []).append(cfg)
            if not by_cloud_id:
                log.warning("No devices configured (%s)", self.settings.config_file)
                return 0
            try:
                statuses = self.cloud.get_statuses(list(by_cloud_id))
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                self.last_error = str(exc)
                log.error("Polling Shelly Cloud failed: %s", exc)
                return 0
            gateways = [cid for cid, cfgs in by_cloud_id.items() if any(c.trv is not None for c in cfgs)]
            gw_settings = self._gateway_settings(gateways)

            stored = devices = 0
            for st in statuses:
                cfgs = by_cloud_id.get(st.id)
                if cfgs is None:
                    log.warning("Cloud returned unknown device id %s", st.id)
                    continue
                for cfg in cfgs:
                    dev_st = st if cfg.trv is None else self._trv_status(st, cfg, gw_settings.get(st.id))
                    if dev_st is None:
                        log.warning("Gateway %s has no BLU TRV blutrv:%s (%s)", st.id, cfg.trv, cfg.code)
                        continue
                    devices += 1
                    try:
                        with self.session_factory() as session:
                            stored += int(self._store(session, cfg, dev_st))
                    except Exception as exc:  # noqa: BLE001
                        log.exception("Storing status of %s failed: %s", cfg.code, exc)

            missing = set(by_cloud_id) - {s.id for s in statuses}
            if missing:
                log.warning("No status returned for: %s", ", ".join(sorted(missing)))
            self.last_run = utcnow()
            self.last_error = None
            log.info("Poll done: %d device(s), %d new reading(s)", devices, stored)
            return stored

    def _gateway_settings(self, gateway_ids: list[str]) -> dict[str, dict]:
        """Settings of the BLU gateways, cached; empty if they cannot be fetched."""
        if not gateway_ids:
            return {}
        if time.monotonic() >= self._gw_settings_due or not set(gateway_ids) <= set(self._gw_settings):
            try:
                self._gw_settings.update(self.cloud.get_settings(gateway_ids))
                self._gw_settings_due = time.monotonic() + GATEWAY_SETTINGS_MAX_AGE
            except Exception as exc:  # noqa: BLE001 - TRVs still work from their full status
                log.warning("Fetching gateway settings failed: %s", exc)
                self._gw_settings_due = time.monotonic() + GATEWAY_SETTINGS_RETRY
            for gid in gateway_ids:
                self._gw_settings.setdefault(gid, {})
        return self._gw_settings

    @staticmethod
    def _trv_status(gw: DeviceStatus, cfg: DeviceConfig, settings: dict | None) -> DeviceStatus | None:
        result = trv_status(gw.status, cfg.trv, settings)
        if result is None:
            return None
        status, online, model = result
        return DeviceStatus(
            id=cfg.id,
            online=online and gw.online is not False,
            status=status,
            model=model or "BluTRV",
            gen="GBLE",
        )

    def _store(self, session: Session, cfg: DeviceConfig, st: DeviceStatus) -> bool:
        now = utcnow()
        raw = json.dumps(st.status, separators=(",", ":"))

        device = session.get(Device, cfg.id)
        if device is None:  # config changed at runtime
            device = Device(id=cfg.id, code=cfg.code, name=cfg.name)
            session.add(device)
        device.online = st.online
        device.model = st.model or device.model
        device.gen = st.gen or device.gen
        device.last_seen = now
        device.last_raw = raw

        values = extract_metrics(
            st.status, self.settings.global_exclude + cfg.exclude, cfg.include
        )
        if not values:
            session.commit()
            return False

        dev_ts = device_timestamp(st.status)
        ts = dev_ts or now
        digest = values_hash(values)
        last = session.scalars(
            select(Reading).where(Reading.device_id == cfg.id).order_by(Reading.ts.desc()).limit(1)
        ).first()
        if last is not None:
            if dev_ts is not None and dev_ts <= last.ts:
                session.commit()
                return False  # device has not reported anything new
            if dev_ts is None and self.settings.skip_unchanged and last.values_hash == digest:
                session.commit()
                return False

        reading = Reading(
            device_id=cfg.id,
            ts=ts,
            received_at=now,
            online=st.online,
            values_hash=digest,
            raw=raw if self.settings.store_raw else None,
        )
        reading.measurements = [Measurement(metric=k, value=v) for k, v in values.items()]
        session.add(reading)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return False
        return True
