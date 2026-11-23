"""Time bucket aggregation (avg/min/max) for the export.

The database groups values into buckets of `interval` (minute intervals and 1h) or of one
hour (longer intervals). Longer intervals are then combined in Python so that hours, days,
weeks and months start at local time boundaries (TIMEZONE), including DST changes.

Completed hours are also kept as hourly statistics (hourly_stats, see rollup.py):
- interval >= 1h: hourly_stats for aggregated hours + minute data for the rest (fast).
- interval < 1h: minute data; periods whose minute data was removed after
  RAW_RETENTION_MONTHS come from hourly_stats and appear as one bucket per hour.
With interval >= 1h, the hours at the edges of from/to are included as whole hours.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import BigInteger, Integer, cast, extract, func, literal, select, text
from sqlalchemy.orm import Session

from .db import HourlyStat, Measurement, Reading

AGGREGATES = ("avg", "min", "max")
_INTERVAL = re.compile(r"^(\d+)\s*(m|h|d|w|mo)$")
HOUR = 3600


@dataclass(frozen=True)
class Interval:
    n: int
    unit: str  # m, h, d, w, mo

    @property
    def db_seconds(self) -> int:
        """Bucket size used in the database query."""
        if self.unit == "m":
            return self.n * 60
        return HOUR

    @property
    def needs_rebucket(self) -> bool:
        return not (self.unit == "m" or (self.unit == "h" and self.n == 1))

    def __str__(self) -> str:
        return f"{self.n}{self.unit}"


def parse_interval(value: str) -> Interval:
    m = _INTERVAL.match(value.strip().lower())
    if not m or int(m.group(1)) < 1:
        raise ValueError(f"Invalid interval '{value}', expected e.g. 15m, 1h, 6h, 1d, 1w, 1mo")
    return Interval(int(m.group(1)), m.group(2))


def parse_aggregates(value: str) -> list[str]:
    aggs = [a.strip().lower() for a in value.split(",") if a.strip()]
    bad = [a for a in aggs if a not in AGGREGATES]
    if bad or not aggs:
        raise ValueError(f"Invalid agg '{value}', allowed: {', '.join(AGGREGATES)}")
    return list(dict.fromkeys(aggs))


def _epoch_bucket(dialect: str, column, seconds: int):
    """SQL expression: unix epoch of `column` (naive UTC) floored to `seconds`."""
    if dialect == "sqlite":
        epoch = cast(func.strftime("%s", column), Integer)
    elif dialect in ("mysql", "mariadb"):
        epoch = func.timestampdiff(text("SECOND"), literal("1970-01-01 00:00:00"), column)
    else:  # postgresql and others supporting EXTRACT(EPOCH ...)
        epoch = cast(extract("epoch", column), BigInteger)
    return (epoch // seconds) * seconds


def _local_bucket_start(epoch: int, interval: Interval, tz: ZoneInfo) -> datetime:
    local = datetime.fromtimestamp(epoch, timezone.utc).astimezone(tz)
    n = interval.n
    if interval.unit == "h":
        return local.replace(hour=(local.hour // n) * n, minute=0, second=0, microsecond=0)
    if interval.unit == "d":
        day = local.date()
        if n > 1:
            day = date.fromordinal(day.toordinal() - day.toordinal() % n)
        return datetime(day.year, day.month, day.day, tzinfo=tz)
    if interval.unit == "w":
        monday = local.date() - timedelta(days=local.weekday())
        if n > 1:  # date.fromordinal(1) is a Monday
            weeks = (monday.toordinal() - 1) // 7
            monday = date.fromordinal((weeks - weeks % n) * 7 + 1)
        return datetime(monday.year, monday.month, monday.day, tzinfo=tz)
    if interval.unit == "mo":
        idx = local.year * 12 + local.month - 1
        idx -= idx % n
        return datetime(idx // 12, idx % 12 + 1, 1, tzinfo=tz)
    raise ValueError(interval.unit)


def aggregate(
    session: Session,
    device_id: str,
    interval: Interval,
    tz: ZoneInfo,
    start: datetime | None,
    end: datetime | None,
    raw_metrics: list[str] | None,
) -> list[tuple[datetime, int, dict[str, tuple[int, float, float, float]]]]:
    """Return [(bucket_start_local, readings, {raw_metric: (count, sum, min, max)})] sorted by time."""
    dialect = session.get_bind().dialect.name
    last_hour = session.scalar(select(func.max(HourlyStat.hour)).where(HourlyStat.device_id == device_id))
    raw_from = hourly_before = None
    if last_hour is not None:
        if interval.unit == "m":  # minute data wherever it still exists, hourly before that
            first_raw = session.scalar(select(func.min(Reading.ts)).where(Reading.device_id == device_id))
            hourly_before = first_raw.replace(minute=0, second=0, microsecond=0) if first_raw else None
        else:  # hourly stats wherever they exist, minute data only for the newest hours
            raw_from = last_hour + timedelta(hours=1)

    bucket = _epoch_bucket(dialect, Reading.ts, interval.db_seconds).label("bucket")
    query = (
        select(
            bucket,
            Measurement.metric,
            func.count(Measurement.value),
            func.sum(Measurement.value),
            func.min(Measurement.value),
            func.max(Measurement.value),
        )
        .join(Reading, Reading.id == Measurement.reading_id)
        .where(Reading.device_id == device_id)
        .group_by(bucket, Measurement.metric)
    )
    if start:
        query = query.where(Reading.ts >= start)
    if end:
        query = query.where(Reading.ts <= end)
    if raw_metrics is not None:
        query = query.where(Measurement.metric.in_(raw_metrics))
    if raw_from is not None:
        query = query.where(Reading.ts >= raw_from)

    hourly = select(
        HourlyStat.hour, HourlyStat.metric, HourlyStat.count, HourlyStat.sum,
        HourlyStat.min, HourlyStat.max,
    ).where(HourlyStat.device_id == device_id)
    if start:
        hourly = hourly.where(HourlyStat.hour >= start.replace(minute=0, second=0, microsecond=0))
    if end:
        hourly = hourly.where(HourlyStat.hour <= end)
    if raw_metrics is not None:
        hourly = hourly.where(HourlyStat.metric.in_(raw_metrics))
    if hourly_before is not None:
        hourly = hourly.where(HourlyStat.hour < hourly_before)
    use_hourly = last_hour is not None  # no hourly stats yet -> minute data only

    def rows():
        yield from session.execute(query)
        if not use_hourly:
            return
        for hour, metric, count, total, vmin, vmax in session.execute(hourly):
            yield int(hour.replace(tzinfo=timezone.utc).timestamp()), metric, count, total, vmin, vmax

    buckets: dict[datetime, dict[str, list]] = {}
    for epoch, metric, count, total, vmin, vmax in rows():
        epoch = int(epoch)
        key = (
            _local_bucket_start(epoch, interval, tz)
            if interval.needs_rebucket
            else datetime.fromtimestamp(epoch, timezone.utc).astimezone(tz)
        )
        slot = buckets.setdefault(key, {})
        acc = slot.get(metric)
        if acc is None:
            slot[metric] = [count, total, vmin, vmax]
        else:
            acc[0] += count
            acc[1] += total
            acc[2] = min(acc[2], vmin)
            acc[3] = max(acc[3], vmax)

    return [
        (key, max(a[0] for a in slot.values()), {m: tuple(a) for m, a in slot.items()})
        for key, slot in sorted(buckets.items())
    ]


def bucket_values(stats: tuple[int, float, float, float], aggs: list[str]) -> dict[str, float]:
    count, total, vmin, vmax = stats
    values = {"avg": round(total / count, 4) if count else None, "min": vmin, "max": vmax}
    return {a: values[a] for a in aggs}
