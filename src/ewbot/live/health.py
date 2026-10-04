"""Heartbeat to an external monitor (Plan 3 spec 3.2 `health`, 5.1 `VPS 다운`).

Every minute the runtime's main loop GETs EWBOT_HEALTHCHECK_URL (for example a healthchecks.io check
with period 1 min and grace 2 min, so 3 silent minutes alert through its integrations; a wall clock that
stepped back never holds the next ping back). The ping is sent
from the main loop only, so a hung loop stops the pings as surely as a dead VPS; it times out after 5 s.
A failed ping - an exception (its type name) or an HTTP status of 400 or more (the status, e.g. 404 for a
wrong check UUID) - never raises: it counts in `failures` (in a row, alerted by the runtime) and
`last_error`, and stderr gets one line when the pings start failing and one when they work again.
`fail(message)` POSTs `<url>/fail` (a query string kept) with a short message that names no secret: on a
fatal exit, and when Telegram has been unusable for 10 minutes (live/runtime.py)."""

from __future__ import annotations

import sys
import time
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import requests

INTERVAL_S = 60.0
TIMEOUT_S = 5.0


def fail_url(url: str) -> str:
    """https://hc-ping.com/<uuid>?create=1 -> https://hc-ping.com/<uuid>/fail?create=1."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=parts.path.rstrip("/") + "/fail"))


class Heartbeat:
    def __init__(self, url: str | None, *, session: Any | None = None, clock: Callable[[], float] = time.time,
                 interval: float = INTERVAL_S, timeout: float = TIMEOUT_S) -> None:
        self.url = (url or "").strip() or None
        self.session = session if session is not None else requests.Session()
        self.clock = clock
        self.interval = interval
        self.timeout = timeout
        self.last: float | None = None
        self.failures = 0
        self.last_error: str | None = None

    def beat(self) -> bool:
        if self.url is None:
            return False
        now = self.clock()
        if self.last is not None and 0 <= now - self.last < self.interval:  # a clock stepped back pings at once
            return False
        self.last = now
        error: str | None = None
        try:
            status = int(self.session.get(self.url, timeout=self.timeout).status_code)
            if status >= 400:
                error = f"HTTP {status}"
        except Exception as exc:  # noqa: BLE001 - monitoring must never stop trading (the URL is a secret)
            error = type(exc).__name__
        if error is None:
            if self.failures:
                print(f"[LIVE] heartbeat ok again after {self.failures} failed pings", file=sys.stderr)
            self.failures = 0
            return True
        if self.failures == 0:
            print(f"[LIVE] heartbeat failed: {error}", file=sys.stderr)
        self.failures += 1
        self.last_error = error
        return False

    def fail(self, message: str) -> bool:
        if self.url is None:
            return False
        try:
            r = self.session.post(fail_url(self.url), data=message[:500].encode("utf-8"), timeout=self.timeout)
            return int(r.status_code) < 400
        except Exception:  # noqa: BLE001 - best effort
            return False
