"""REST API: device overview and data export."""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime, timedelta, timezone
from fnmatch import fnmatchcase
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select

from .config import Settings
from .db import Device, Measurement, Reading, utcnow
from .metrics import extract_metrics, flatten

router = APIRouter()

MAX_LIMIT = 100_000
_RELATIVE = re.compile(r"^-?(\d+)\s*([smhdw])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


# -- helpers -----------------------------------------------------------------
def require_token(request: Request) -> None:
    token = request.app.state.settings.api_token
    if not token:
        return
    auth = request.headers.get("authorization", "")
    supplied = (
        auth[7:] if auth.lower().startswith("bearer ") else None
    ) or request.headers.get("x-api-key") or request.query_params.get("token")
    if supplied != token:
        raise HTTPException(status_code=401, detail="Invalid or missing API token")


def parse_time(value: str | None, tz: ZoneInfo, end_of_day: bool = False) -> datetime | None:
    """Accepts ISO date/datetime, unix seconds or relative values like '24h', '7d', '-30m'.
    Returns naive UTC. Values without timezone are interpreted in the configured TIMEZONE."""
    if value is None or value.strip() == "":
        return None
    value = value.strip()
    if m := _RELATIVE.match(value):
        return utcnow() - timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})
    if re.fullmatch(r"\d{9,11}(\.\d+)?", value):
        return datetime.fromtimestamp(float(value), timezone.utc).replace(tzinfo=None)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            d = date.fromisoformat(value)
            dt = datetime(d.year, d.month, d.day, tzinfo=tz)
            if end_of_day:
                dt += timedelta(days=1, microseconds=-1)
        else:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid time value: {value}") from exc
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def fmt_time(dt: datetime | None, tz: ZoneInfo) -> str | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(tz).isoformat(timespec="seconds")


def split_list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def resolve_devices(session, refs: list[str], include_inactive: bool = False) -> list[Device]:
    query = select(Device).order_by(Device.code)
    if not include_inactive and not refs:
        query = query.where(Device.active.is_(True))
    devices = list(session.scalars(query))
    if not refs:
        return devices
    wanted = {r.lower() for r in refs}
    found = [d for d in devices if d.code.lower() in wanted or d.id.lower() in wanted]
    unknown = wanted - {d.code.lower() for d in found} - {d.id.lower() for d in found}
    if unknown:
        raise HTTPException(status_code=404, detail=f"Unknown device(s): {', '.join(sorted(unknown))}")
    return found


class MetricView:
    """Applies aliases (raw -> nice name) and the optional metric filter for one device."""

    def __init__(self, settings: Settings, device_id: str, metric_filter: list[str]):
        self.aliases = settings.aliases_for(device_id)
        self.filter = metric_filter

    def name(self, raw: str) -> str | None:
        alias = self.aliases.get(raw, raw)
        if self.filter and not any(
            fnmatchcase(alias, p) or fnmatchcase(raw, p) for p in self.filter
        ):
            return None
        return alias

    def apply(self, pairs) -> dict[str, float]:
        out = {}
        for raw, value in pairs:
            name = self.name(raw)
            if name is not None:
                out[name] = value
        return dict(sorted(out.items()))


def device_info(device: Device, tz: ZoneInfo) -> dict:
    return {
        "code": device.code,
        "id": device.id,
        "name": device.name,
        "model": device.model,
        "online": device.online,
        "last_seen": fmt_time(device.last_seen, tz),
    }


# -- endpoints ---------------------------------------------------------------
@router.get("/health", tags=["system"])
def health(request: Request):
    poller = request.app.state.poller
    tz = request.app.state.tz
    return {
        "status": "ok" if poller.last_error is None else "degraded",
        "last_poll": fmt_time(poller.last_run, tz),
        "last_error": poller.last_error,
        "devices": len(request.app.state.settings.devices),
    }


@router.get("/api/devices", tags=["devices"], dependencies=[Depends(require_token)])
def list_devices(request: Request, all: bool = Query(False, description="include removed devices")):
    tz = request.app.state.tz
    with request.app.state.session_factory() as session:
        counts = dict(
            session.execute(select(Reading.device_id, func.count()).group_by(Reading.device_id)).all()
        )
        devices = resolve_devices(session, [], include_inactive=all)
        return [
            {**device_info(d, tz), "active": d.active, "readings": counts.get(d.id, 0)}
            for d in devices
        ]


@router.get("/api/devices/{ref}/raw", tags=["devices"], dependencies=[Depends(require_token)])
def device_raw(request: Request, ref: str):
    """Last raw status JSON as delivered by the Shelly Cloud."""
    with request.app.state.session_factory() as session:
        device = resolve_devices(session, [ref], include_inactive=True)[0]
        return JSONResponse(json.loads(device.last_raw) if device.last_raw else {})


@router.get("/api/devices/{ref}/metrics", tags=["devices"], dependencies=[Depends(require_token)])
def device_metrics(request: Request, ref: str):
    """All numeric fields of the last status: raw path, alias and whether it is stored.
    Use this to choose aliases/include/exclude in devices.yaml."""
    settings: Settings = request.app.state.settings
    with request.app.state.session_factory() as session:
        device = resolve_devices(session, [ref], include_inactive=True)[0]
        status = json.loads(device.last_raw) if device.last_raw else {}
    cfg = next((d for d in settings.devices if d.id == device.id), None)
    stored = extract_metrics(
        status, settings.global_exclude + (cfg.exclude if cfg else []), cfg.include if cfg else []
    )
    aliases = settings.aliases_for(device.id)
    return [
        {"path": path, "alias": aliases.get(path), "value": value, "stored": path in stored}
        for path, value in sorted(flatten(status).items())
    ]


@router.post("/api/poll", tags=["system"], dependencies=[Depends(require_token)])
def poll_now(request: Request):
    """Trigger an immediate poll of the Shelly Cloud."""
    stored = request.app.state.poller.poll_once()
    return {"stored": stored, "error": request.app.state.poller.last_error}


@router.get("/api/export", tags=["export"], dependencies=[Depends(require_token)])
def export(
    request: Request,
    devices: str | None = Query(None, description="Comma separated device codes or ids"),
    from_: str | None = Query(
        None, alias="from", description="Start: ISO date/datetime, unix seconds or relative (24h, 7d)"
    ),
    to: str | None = Query(None, description="End: ISO date/datetime, unix seconds or relative"),
    metrics: str | None = Query(None, description="Comma separated metric names, wildcards allowed"),
    format: Literal["json", "csv", "csv-wide"] = Query("json"),
    order: Literal["asc", "desc"] = Query("asc"),
    limit: int = Query(10_000, ge=1, le=MAX_LIMIT, description="Max readings per device"),
):
    """Without `from`/`to`: the latest values of each device.
    With `from` and/or `to`: all readings in that time range."""
    settings: Settings = request.app.state.settings
    tz: ZoneInfo = request.app.state.tz
    start = parse_time(from_, tz)
    end = parse_time(to, tz, end_of_day=True)
    if start and end and start > end:
        raise HTTPException(status_code=400, detail="'from' must be before 'to'")
    metric_filter = split_list(metrics)
    latest_mode = start is None and end is None

    result = []
    with request.app.state.session_factory() as session:
        for device in resolve_devices(session, split_list(devices)):
            view = MetricView(settings, device.id, metric_filter)
            query = select(Reading).where(Reading.device_id == device.id)
            if start:
                query = query.where(Reading.ts >= start)
            if end:
                query = query.where(Reading.ts <= end)
            if latest_mode:
                query = query.order_by(Reading.ts.desc()).limit(1)
            else:
                query = query.order_by(Reading.ts.asc() if order == "asc" else Reading.ts.desc())
                query = query.limit(limit)
            readings = list(session.scalars(query))

            values_by_reading: dict[int, list[tuple[str, float]]] = {r.id: [] for r in readings}
            ids = list(values_by_reading)
            for i in range(0, len(ids), 500):
                rows = session.execute(
                    select(Measurement.reading_id, Measurement.metric, Measurement.value).where(
                        Measurement.reading_id.in_(ids[i : i + 500])
                    )
                )
                for reading_id, metric, value in rows:
                    values_by_reading[reading_id].append((metric, value))

            entries = [
                {"ts": fmt_time(r.ts, tz), "values": view.apply(values_by_reading[r.id])}
                for r in readings
            ]
            info = device_info(device, tz)
            if latest_mode:
                latest = entries[0] if entries else {"ts": None, "values": {}}
                result.append({**info, **latest})
            else:
                result.append({**info, "count": len(entries), "readings": entries})

    meta = {
        "generated_at": fmt_time(utcnow(), tz),
        "from": fmt_time(start, tz),
        "to": fmt_time(end, tz),
        "timezone": settings.timezone,
    }
    if format == "json":
        return {**meta, "mode": "latest" if latest_mode else "range", "devices": result}
    return _csv(result, latest_mode, wide=format == "csv-wide")


def _csv(result: list[dict], latest_mode: bool, wide: bool) -> Response:
    rows = []
    for dev in result:
        entries = [dev] if latest_mode else dev["readings"]
        for e in entries:
            if e["ts"] is not None:
                rows.append((e["ts"], dev["code"], dev["id"], e["values"]))

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    if wide:
        metric_names = sorted({m for *_, values in rows for m in values})
        writer.writerow(["timestamp", "device", "device_id", *metric_names])
        for ts, code, dev_id, values in rows:
            writer.writerow([ts, code, dev_id, *[values.get(m, "") for m in metric_names]])
    else:
        writer.writerow(["timestamp", "device", "device_id", "metric", "value"])
        for ts, code, dev_id, values in rows:
            for metric, value in values.items():
                writer.writerow([ts, code, dev_id, metric, value])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="shelly-export.csv"'},
    )
