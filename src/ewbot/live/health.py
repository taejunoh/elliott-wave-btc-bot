"""Heartbeat to an external monitor (Plan 3 spec 3.2 `health`, 5.1 `VPS 다운`).

Every minute the runtime's main loop GETs EWBOT_HEALTHCHECK_URL (for example a healthchecks.io check
with period 1 min and grace 2 min, so 3 silent minutes alert through its Telegram integration).
The ping is sent from the main loop only, so a hung loop stops the pings as surely as a dead VPS.
A failed ping is reported once to stderr and never raises; `fail()` pings `<url>/fail` on a fatal exit."""

from __future__ import annotations

import sys
import time
from typing import Any, Callable

import requests

INTERVAL_S = 60.0
TIMEOUT_S = 10.0


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

    def beat(self) -> bool:
        if self.url is None:
            return False
        now = self.clock()
        if self.last is not None and now - self.last < self.interval:
            return False
        self.last = now
        try:
            ok = int(self.session.get(self.url, timeout=self.timeout).status_code) < 400
        except Exception as exc:  # noqa: BLE001 - monitoring must never stop trading
            print(f"[LIVE] heartbeat failed: {type(exc).__name__}", file=sys.stderr)
            ok = False
        self.failures = 0 if ok else self.failures + 1
        return ok

    def fail(self, message: str) -> None:
        if self.url is None:
            return
        try:
            self.session.post(self.url.rstrip("/") + "/fail", data=message[:500].encode("utf-8"), timeout=self.timeout)
        except Exception:  # noqa: BLE001 - best effort on the way down
            pass
