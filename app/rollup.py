"""Hourly statistics and retention of minute data.

1. Every completed hour is aggregated into hourly_stats (count, sum, min, max per hour
   and metric). Aggregated exports with interval >= 1h read from this small table,
   which keeps long-range queries fast.
2. Minute data older than RAW_RETENTION_MONTHS is deleted - the hourly statistics
   for it already exist. RAW_RETENTION_MONTHS=0 keeps minute data forever.

Both steps work per device, one day at a time, each in its own transaction.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from .aggregate import HOUR, _epoch_bucket
from .db import Device, HourlyStat, Measurement, Reading, utcnow

log = logging.getLogger(__name__)

RUN_EVERY = timedelta(hours=1)
FIRST_RUN_DELAY = 60  # seconds after start, so the first poll is not delayed
SETTLE = timedelta(minutes=10)  # an hour is aggregated once it is over for this long


def floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def floor_day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def subtract_months(dt: datetime, months: int) -> datetime:
    idx = dt.year * 12 + dt.month - 1 - months
    year, month = divmod(idx, 12)
    month += 1
    # clamp day (e.g. 31.05. - 3 months -> 28./29.02.)
    next_month = datetime(year + (month == 12), month % 12 + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    return dt.replace(year=year, month=month, day=min(dt.day, last_day))


def retention_cutoff(months: int, now: datetime | None = None) -> datetime:
    """Start of the hour before which only hourly statistics are kept (naive UTC)."""
    return floor_hour(subtract_months(now or utcnow(), months))


def aggregated_until(session, device_id: str) -> datetime | None:
    """End of the last hour that is contained in hourly_stats (naive UTC)."""
    last = session.scalar(select(func.max(HourlyStat.hour)).where(HourlyStat.device_id == device_id))
    return last + timedelta(hours=1) if last is not None else None


def _device_ids(session_factory: sessionmaker) -> list[str]:
    with session_factory() as session:
        return list(session.scalars(select(Device.id)))


def aggregate_hours(session_factory: sessionmaker, now: datetime | None = None) -> int:
    """Write hourly_stats for all completed hours not aggregated yet. Returns rows written."""
    limit = floor_hour((now or utcnow()) - SETTLE)
    written = 0
    for device_id in _device_ids(session_factory):
        while True:
            with session_factory() as session:
                since = aggregated_until(session, device_id)
                query = select(func.min(Reading.ts)).where(Reading.device_id == device_id)
                if since is not None:
                    query = query.where(Reading.ts >= since)
                first = session.scalar(query)
                if first is None or first >= limit:
                    break
                chunk_start = floor_hour(first)
                chunk_end = min(limit, floor_day(first) + timedelta(days=1))
                rows = _write_hourly(session, device_id, chunk_start, chunk_end)
                session.commit()
                if rows == 0:
                    break
                written += rows
    return written


def _write_hourly(session, device_id: str, start: datetime, end: datetime) -> int:
    dialect = session.get_bind().dialect.name
    bucket = _epoch_bucket(dialect, Reading.ts, HOUR).label("bucket")
    rows = session.execute(
        select(
            bucket,
            Measurement.metric,
            func.count(Measurement.value),
            func.sum(Measurement.value),
            func.min(Measurement.value),
            func.max(Measurement.value),
        )
        .join(Reading, Reading.id == Measurement.reading_id)
        .where(Reading.device_id == device_id, Reading.ts >= start, Reading.ts < end)
        .group_by(bucket, Measurement.metric)
    ).all()
    for epoch, metric, count, total, vmin, vmax in rows:
        hour = datetime.fromtimestamp(int(epoch), timezone.utc).replace(tzinfo=None)
        existing = session.get(HourlyStat, (device_id, hour, metric))
        if existing is None:
            session.add(HourlyStat(device_id=device_id, hour=hour, metric=metric,
                                   count=count, sum=total, min=vmin, max=vmax))
        else:  # should not happen; merge instead of failing
            existing.count += count
            existing.sum += total
            existing.min = min(existing.min, vmin)
            existing.max = max(existing.max, vmax)
    return len(rows)


def expire_minute_data(session_factory: sessionmaker, months: int, now: datetime | None = None) -> int:
    """Delete minute data older than `months` (only where hourly stats exist). Returns readings deleted."""
    if months <= 0:
        return 0
    cutoff = retention_cutoff(months, now)
    deleted = 0
    for device_id in _device_ids(session_factory):
        while True:
            with session_factory() as session:
                until = aggregated_until(session, device_id)
                if until is None:
                    break
                limit = min(cutoff, until)
                oldest = session.scalar(
                    select(func.min(Reading.ts)).where(Reading.device_id == device_id, Reading.ts < limit)
                )
                if oldest is None:
                    break
                chunk_end = min(limit, floor_day(oldest) + timedelta(days=1))
                in_range = (Reading.device_id == device_id, Reading.ts < chunk_end)
                ids = select(Reading.id).where(*in_range).scalar_subquery()
                session.execute(delete(Measurement).where(Measurement.reading_id.in_(ids)))
                deleted += session.execute(delete(Reading).where(*in_range)).rowcount
                session.commit()
    return deleted


def rollup(session_factory: sessionmaker, months: int, now: datetime | None = None) -> dict:
    """Run both steps once. Returns {"cutoff", "hours", "readings"}."""
    hours = aggregate_hours(session_factory, now)
    readings = expire_minute_data(session_factory, months, now)
    cutoff = retention_cutoff(months, now) if months > 0 else None
    if hours or readings:
        log.info("Rollup: %d hourly values written, %d old readings removed", hours, readings)
    return {"cutoff": cutoff, "hours": hours, "readings": readings}


class RollupJob:
    """Runs the rollup shortly after start and then every hour."""

    def __init__(self, session_factory: sessionmaker, months: int):
        self.session_factory = session_factory
        self.months = months
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last_run: datetime | None = None
        self.last_result: dict | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self.months <= 0:
            log.info("RAW_RETENTION_MONTHS=0: minute data is kept forever")
        self._thread = threading.Thread(target=self._run, name="rollup", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)

    def run_once(self) -> dict:
        with self._lock:
            try:
                self.last_result = rollup(self.session_factory, self.months)
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - keep the job alive
                self.last_error = str(exc)
                log.exception("Rollup failed: %s", exc)
            self.last_run = utcnow()
            return self.last_result or {}

    def _run(self) -> None:
        if self._stop.wait(FIRST_RUN_DELAY):
            return
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(RUN_EVERY.total_seconds())
