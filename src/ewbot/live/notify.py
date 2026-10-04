"""[LIVE] Telegram notifications (Plan 3 spec 3.5): the paper runtime's TelegramSender, messages prefixed
"[LIVE]" (the paper runtime's start with "[ewbot paper]").

Never on the trading loop: a call only queues the message and returns at once (it never blocks or raises); a
background thread (`start()`) sends the queue in order, one attempt at a time. A send that fails for a reason
that may pass - a transport error, HTTP 429 or 5xx - keeps the message at the head of the queue and retries it
after a pause that doubles from 2 s to 60 s, for up to 30 minutes (MAX_AGE_S) after it was queued. A message
Telegram refuses outright (any other HTTP 4xx) is dropped at once, so it never holds the messages behind it;
`refusals` counts such refusals in a row (a delivery resets it). Every message's full text reaches stderr when
its send first fails, when it is dropped and when it is still undelivered at flush timeout or stop(), so the
container log always has it. The queue holds MAX_PENDING messages: when it is full the oldest waiting one is
dropped (`dropped` counts them). The same (kind, text) is never queued twice and is sent at most once per 10
minutes, counted from its delivery (a failed send is never deduplicated away). A message is cut to MAX_CHARS
with "…(잘림)" (Telegram refuses more than 4096); one delivered a minute or more late says so.

For the runtime's view of Telegram: `failing_since` is when the oldest waiting message that has failed at least
once was queued (None when no waiting message has failed: it clears when that message is delivered or dropped),
`oldest_pending` when the oldest waiting message was queued and `last_attempt` when the worker last tried one -
waiting messages with no attempt for minutes mean a stuck or never-started worker. Without a sender every
message goes to stderr."""

from __future__ import annotations

import collections
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

from ewbot.paper.notify import Sender, deliver, make_event

PREFIX = "[LIVE]"
DEDUPE_S = 600.0
MAX_CHARS = 4000  # Telegram's limit is 4096 UTF-16 code units: room for the delay note
TRUNCATED = "…(잘림)"
MAX_PENDING = 100
MAX_AGE_S = 1800.0  # a message is retried for 30 minutes, then dropped (it is in the container log)
RETRY_BASE_S = 2.0
RETRY_MAX_S = 60.0
LATE_NOTE_S = 60.0
RECENT = 50  # delivered texts kept in `sent`
_HTTP = re.compile(r"telegram HTTP (\d{3})")  # paper TelegramSender's SendError for a reply that is not ok


def refused(error: str) -> bool:
    """A send error Telegram will give again for the same message: HTTP 4xx other than 429 (flood control)."""
    m = _HTTP.search(error)
    return m is not None and 400 <= int(m.group(1)) < 500 and int(m.group(1)) != 429


def _units(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def fit(text: str, limit: int = MAX_CHARS) -> str:
    """`text`, or its start with TRUNCATED, within `limit` UTF-16 code units (Telegram's measure)."""
    if _units(text) <= limit:
        return text
    room, used, out = limit - _units(TRUNCATED), 0, []
    for ch in text:
        used += 2 if ord(ch) > 0xFFFF else 1
        if used > room:
            break
        out.append(ch)
    return "".join(out) + TRUNCATED


@dataclass(eq=False)
class _Message:
    kind: str
    key: tuple[str, str]
    body: str
    created: float
    seq: int
    failed: bool = False  # a send of it has failed (retried)
    logged: bool = False  # its text is in stderr
    final: bool = False  # reported undelivered at flush timeout or stop()


class LiveNotifier:
    def __init__(self, sender: Sender | None, *, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], object] | None = None, dedupe_s: float = DEDUPE_S,
                 max_pending: int = MAX_PENDING) -> None:
        self.sender = sender
        self.clock = clock
        self.dedupe_s = dedupe_s
        self.max_pending = max_pending
        self._stop = threading.Event()
        self.sleep = sleep if sleep is not None else self._stop.wait  # the worker's pause (stop() ends it)
        self._cond = threading.Condition()
        self._pending: collections.deque[_Message] = collections.deque()
        self._queued: set[tuple[str, str]] = set()
        self._inflight: _Message | None = None
        self._last: dict[tuple[str, str], float] = {}  # delivery time per (kind, text), kept dedupe_s
        self._seq = 0
        self._thread: threading.Thread | None = None
        self.sent: collections.deque[str] = collections.deque(maxlen=RECENT)
        self.sent_count = 0
        self.dropped = 0
        self.failures = 0  # failed sends in a row (the retry pause)
        self.refusals = 0  # messages refused (HTTP 4xx but 429) in a row
        self.last_attempt: float | None = None
        self.last_error: str | None = None

    @property
    def pending(self) -> int:
        with self._cond:
            return len(self._pending)

    @property
    def oldest_pending(self) -> float | None:
        with self._cond:
            return self._pending[0].created if self._pending else None

    @property
    def failing_since(self) -> float | None:
        with self._cond:
            return next((m.created for m in self._pending if m.failed), None)

    def __call__(self, kind: str, text: str) -> bool:
        """Queue a message. False when it is already queued or was delivered within dedupe_s, or without a
        sender (then it is printed to stderr)."""
        now = self.clock()
        key = (kind, text)
        body = fit(f"{PREFIX} {text}")
        with self._cond:
            self._last = {k: t for k, t in self._last.items() if now - t < self.dedupe_s}
            if key in self._queued or key in self._last:
                return False
            if self.sender is None:
                self._last[key] = now
                print(body, file=sys.stderr)
                return False
            if len(self._pending) >= self.max_pending:
                self._drop(next((m for m in self._pending if m is not self._inflight), None), "queue full")
            self._seq += 1
            self._pending.append(_Message(kind, key, body, now, self._seq))
            self._queued.add(key)
            self._cond.notify_all()
        return True

    def _drop(self, m: _Message | None, why: str) -> None:  # with self._cond held
        if m is None or m not in self._pending:
            return
        self._pending.remove(m)
        self._queued.discard(m.key)
        self.dropped += 1
        print(f"[LIVE] telegram message dropped ({why}): {m.body}", file=sys.stderr)
        self._cond.notify_all()

    def pump(self) -> float | None:
        """One delivery attempt for the oldest queued message (the worker thread's step; tests call it directly).
        None when nothing is queued, 0.0 after a delivery or a drop, else the pause before the next attempt."""
        with self._cond:
            if not self._pending or self.sender is None:
                return None
            m = self._inflight = self._pending[0]
        now = self.clock()
        late = now - m.created
        if late > MAX_AGE_S:
            with self._cond:
                self._inflight = None
                self._drop(m, f"not delivered in {MAX_AGE_S / 60:.0f} min")
            return 0.0
        self.last_attempt = now
        text = m.body + (f" ({late / 60:.0f}분 지연)" if late >= LATE_NOTE_S else "")
        _, sent, errors = deliver([make_event(m.kind, int(m.created), m.seq, text)], [], self.sender, attempts=1)
        with self._cond:
            self._inflight = None
            if sent:
                if m in self._pending:
                    self._pending.remove(m)
                self._queued.discard(m.key)
                self._last[m.key] = now
                self.sent.append(text)
                self.sent_count += 1
                self.failures = self.refusals = 0
                self._cond.notify_all()
                return 0.0
            self.last_error = errors[0] if errors else "send failed"
            if refused(self.last_error):  # never accepted on a retry: out of the way at once
                self.refusals += 1
                self._drop(m, f"refused: {self.last_error}")
                return 0.0
            self.failures += 1
            m.failed = True
            if not m.logged:
                m.logged = True
                print(f"[LIVE] telegram send failed, retrying ({self.last_error}): {m.body}", file=sys.stderr)
            return min(RETRY_BASE_S * 2.0 ** min(self.failures - 1, 10), RETRY_MAX_S)

    def run(self) -> None:
        """The worker: sends while anything is queued, until stop(). Never raises."""
        while not self._stop.is_set():
            with self._cond:
                if not self._pending:
                    self._cond.wait(1.0)
                    continue
            try:
                pause = self.pump()
            except Exception as exc:  # noqa: BLE001 - the worker must outlive any error
                self.last_error = type(exc).__name__
                pause = RETRY_MAX_S
            if pause:
                self.sleep(pause)

    def start(self) -> threading.Thread | None:
        if self.sender is None:
            return None
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self.run, name="ewbot-notify", daemon=True)
            self._thread.start()
        return self._thread

    def _report_undelivered(self, why: str) -> None:  # with self._cond held
        for m in self._pending:
            if not m.final:
                m.final = True
                print(f"[LIVE] telegram message not delivered ({why}): {m.body}", file=sys.stderr)

    def flush(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for the queue to empty (the worker sends it); True when it did, else every
        message still waiting goes to stderr."""
        end = time.monotonic() + timeout
        with self._cond:
            while self._pending:
                left = end - time.monotonic()
                if left <= 0:
                    self._report_undelivered("flush timed out")
                    return False
                self._cond.wait(left)
        return True

    def stop(self) -> None:
        """Stop the worker; every message still waiting goes to stderr."""
        self._stop.set()
        with self._cond:
            self._report_undelivered("stopped")
            self._cond.notify_all()
