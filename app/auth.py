"""PIN for the start page: unlocks the device list and the export links.

Active only if PAGE_PIN is set. After the correct PIN the browser gets a cookie that holds
an HMAC of the PIN (keyed with the Shelly auth key), never the PIN itself. Changing the PIN
invalidates all cookies. The cookie is also accepted by the API in place of API_TOKEN, so
the export links on the page work after unlocking.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from collections import deque

from fastapi import Request

from .config import Settings

COOKIE_NAME = "shelly_tracker_pin"
COOKIE_MAX_AGE = 30 * 24 * 3600

# Brute-force protection: at most MAX_FAILURES wrong PINs per FAILURE_WINDOW seconds.
# Behind a reverse proxy all clients share one address, so this is effectively global.
MAX_FAILURES = 5
FAILURE_WINDOW = 300


def unlock_token(settings: Settings) -> str:
    key = f"{settings.shelly_auth_key}|{settings.page_pin}".encode()
    return hmac.new(key, b"shelly-tracker-page-unlock", hashlib.sha256).hexdigest()


def page_unlocked(request: Request) -> bool:
    """True if a PIN is configured and the request carries a valid unlock cookie."""
    settings = request.app.state.settings
    if not settings.page_pin:
        return False
    cookie = request.cookies.get(COOKIE_NAME, "")
    return bool(cookie) and hmac.compare_digest(cookie, unlock_token(settings))


def pin_correct(settings: Settings, pin: str) -> bool:
    return hmac.compare_digest(pin.strip().encode(), settings.page_pin.encode())


def _failures(request: Request) -> deque:
    state = request.app.state
    if not hasattr(state, "pin_failures"):
        state.pin_failures = {}
    host = request.client.host if request.client else "?"
    failures = state.pin_failures.setdefault(host, deque())
    while failures and time.monotonic() - failures[0] > FAILURE_WINDOW:
        failures.popleft()
    return failures


def login_locked(request: Request) -> bool:
    return len(_failures(request)) >= MAX_FAILURES


def record_failure(request: Request) -> None:
    _failures(request).append(time.monotonic())


def clear_failures(request: Request) -> None:
    _failures(request).clear()


def is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto.split(",")[0].strip().lower() == "https"
