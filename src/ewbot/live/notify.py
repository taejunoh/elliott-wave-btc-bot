"""[LIVE] Telegram notifications (Plan 3 spec 3.5): the paper runtime's TelegramSender and delivery
with retries, prefixed "[LIVE]" (the paper runtime's messages start with "[ewbot paper]"). A failed
send never raises into the trading loop; the same (kind, text) is sent at most once per 10 minutes."""

from __future__ import annotations

import sys
import time
from typing import Callable

from ewbot.paper.notify import Sender, deliver, make_event

PREFIX = "[LIVE]"
DEDUPE_S = 600.0
SEND_ATTEMPTS = 2  # keeps a Telegram outage from stalling the loop for more than ~1 s per message


class LiveNotifier:
    def __init__(self, sender: Sender | None, *, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, dedupe_s: float = DEDUPE_S) -> None:
        self.sender = sender
        self.clock = clock
        self.sleep = sleep
        self.dedupe_s = dedupe_s
        self._last: dict[tuple[str, str], float] = {}
        self.sent: list[str] = []

    def __call__(self, kind: str, text: str) -> bool:
        now = self.clock()
        key = (kind, text)
        last = self._last.get(key)
        if last is not None and now - last < self.dedupe_s:
            return False
        self._last[key] = now
        body = f"{PREFIX} {text}"
        if self.sender is None:
            print(body, file=sys.stderr)
            return False
        event = make_event(kind, int(now), len(self.sent), body)
        _, sent, errors = deliver([event], [], self.sender, attempts=SEND_ATTEMPTS, sleep=self.sleep)
        if sent:
            self.sent.append(body)
            return True
        print(f"[LIVE] telegram send failed: {errors}", file=sys.stderr)
        return False
