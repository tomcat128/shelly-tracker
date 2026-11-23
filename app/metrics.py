"""Turn a (nested) Shelly status JSON into flat numeric metrics."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Any


# Shelly reports unknown/unavailable values as -999 (e.g. precipitation last_1h).
MISSING_VALUES = {-999.0}


def flatten(obj: Any, prefix: str = "") -> dict[str, float]:
    """Return every numeric/boolean leaf as {"path.to.leaf": float}."""
    out: dict[str, float] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            out.update(flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            out.update(flatten(value, f"{prefix}.{idx}" if prefix else str(idx)))
    elif isinstance(obj, bool):
        out[prefix] = 1.0 if obj else 0.0
    elif isinstance(obj, (int, float)):
        out[prefix] = float(obj)
    return out


def matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatchcase(name, p) for p in patterns)


def extract_metrics(
    status: dict, exclude: list[str], include: list[str] | None = None
) -> dict[str, float]:
    """Flatten status and apply exclude patterns; include patterns override excludes."""
    include = include or []
    result = {}
    for name, value in flatten(status).items():
        if value in MISSING_VALUES:
            continue
        if include and matches(name, include):
            result[name] = value
        elif not matches(name, exclude):
            result[name] = value
    return result


def values_hash(values: dict[str, float]) -> str:
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def device_timestamp(status: dict) -> datetime | None:
    """Best-effort: the time the device last reported (naive UTC), if the status contains it."""
    updated = status.get("_updated")
    if isinstance(updated, str):
        try:
            return datetime.strptime(updated, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    for candidate in (status.get("ts"), (status.get("sys") or {}).get("unixtime")):
        if isinstance(candidate, (int, float)) and candidate > 1_000_000_000:
            return datetime.fromtimestamp(candidate, timezone.utc).replace(tzinfo=None)
    return None
