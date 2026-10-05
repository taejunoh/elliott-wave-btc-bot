"""[LIVE] Telegram notifications (Plan 3 spec 3.5): messages prefixed "[LIVE]" (the paper runtime's start with
"[ewbot paper]"), sent by this module's own sendMessage client (`TelegramSender`).

Never on the trading loop: a call only queues the message and returns at once (it never blocks or raises); a
background thread (`start()`) sends the queue in order, one attempt at a time. A send that fails is placed
(`outcome`) by whether Telegram can have posted it:
- UNSENT: no connection was made (a DNS failure, a refused connection, the 5 s connect timeout) - Telegram never
  had it: sent again as it is after a pause that doubles from 2 s to 60 s;
- FLOOD: HTTP 429 - not taken: sent again after that pause, at least Telegram's retry_after;
- REFUSED: any other HTTP 4xx - Telegram gives the same answer again: the message is dropped at once, so it never
  holds the messages behind it; `refusals` counts such refusals in a row (a delivery resets it);
- UNANSWERED: the request went out and no usable answer came back (the 15 s read timeout, a connection dropped
  after it was made, HTTP 5xx, a reply that is not Telegram's ok) - Telegram may have posted it (on 2026-10-05
  one alert reached the chat six times while Telegram took every send but left its answer to time out). Until
  Telegram answers a send again (`unanswered` > 0) every attempt first asks the sender's probe() - getMe, which
  posts nothing - and sends nothing while that goes unanswered too, pausing 60 s doubling to 240 s; the message
  goes again only once Telegram answers, ending RESENT ("may be a copy"), and after MAX_SENDS unanswered sends
  it is dropped as possibly posted.
A message is kept at most 30 minutes (MAX_AGE_S) after it was queued. Its full text reaches stderr when it first
fails, when it is dropped and when it is still undelivered at flush timeout or stop(), so the container log
always has it. The queue holds MAX_PENDING messages: when it is full the oldest waiting one is dropped (`dropped`
counts every drop). The same (kind, text) is never queued twice and is sent at most once per 10 minutes, counted
from its delivery - or from its drop after a send Telegram may have posted (a message that never reached
Telegram is never deduplicated away; after the wall clock stepped back nothing is held back for the size of the
step). A message is cut to MAX_CHARS with "…(잘림)" (Telegram refuses more than 4096); one delivered a minute or
more late says so.

For the runtime's view of Telegram: `failing_since` is when the oldest waiting message that has failed at least
once - a send, or the probe before it - was queued (None when no waiting message has failed: it clears when that
message is delivered or dropped), `oldest_pending` when the oldest waiting message was queued and `last_attempt`
when the worker last tried Telegram (a send or a probe) - waiting messages with no attempt for minutes mean a
stuck or never-started worker. Any `Sender` may be used: a SendError is placed by its text (`outcome`), any other
exception counts as UNSENT, and a sender without probe() is sent to again after the pause. Without a sender every
message goes to stderr."""

from __future__ import annotations

import collections
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError

from ewbot.paper.notify import Sender, SendError

PREFIX = "[LIVE]"
DEDUPE_S = 600.0
MAX_CHARS = 4000  # Telegram's limit is 4096 UTF-16 code units: room for the delay and resend notes
TRUNCATED = "…(잘림)"
RESENT = "(재전송 - 중복일 수 있음)"  # sent again after a send Telegram left unanswered (and may have posted)
MAX_PENDING = 100
MAX_AGE_S = 1800.0  # a message is retried for 30 minutes, then dropped (it is in the container log)
RETRY_BASE_S = 2.0  # after a send that never left or was not taken: 2 s doubling to 60 s
RETRY_MAX_S = 60.0
UNANSWERED_BASE_S = 60.0  # after a send or a probe Telegram left unanswered: 60 s doubling to 240 s
UNANSWERED_MAX_S = 240.0
MAX_SENDS = 3  # unanswered sends of one message (each may have posted it), then it is dropped
LATE_NOTE_S = 60.0
RECENT = 50  # delivered texts kept in `sent`
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 15.0
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
UNSENT, FLOOD, REFUSED, UNANSWERED = "unsent", "flood", "refused", "unanswered"
_HTTP = re.compile(r"telegram HTTP (\d{3})")  # a SendError for a reply that is not ok (this module's and paper's)
_RETRY_AFTER = re.compile(r"""['"]retry_after['"]\s*:\s*(\d+(?:\.\d+)?)""")


def refused(error: str) -> bool:
    """A send error Telegram will give again for the same message: HTTP 4xx other than 429 (flood control)."""
    m = _HTTP.search(error)
    return m is not None and 400 <= int(m.group(1)) < 500 and int(m.group(1)) != 429


def unsent(exc: BaseException) -> bool:
    """Whether a request exception means the request never left: requests' ConnectTimeout, or a ConnectionError over
    a connection that was never made (urllib3's NewConnectionError - refused, unreachable, a DNS failure - or its
    connect timeout). A read timeout, a connection reset or closed once made, a TLS error, a reply that could not
    be read: the request may have gone out."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    inner = exc.args[0] if isinstance(exc, requests.exceptions.ConnectionError) and exc.args else None
    return isinstance(inner, MaxRetryError) and isinstance(inner.reason, ConnectTimeoutError)


class TelegramError(SendError):
    """A Telegram call that did not succeed: `outcome` (UNSENT, FLOOD, REFUSED or UNANSWERED) and `retry_after`
    (HTTP 429's). Its message is the request's exception type name, or the HTTP status with the start of Telegram's
    reply - never the token."""

    def __init__(self, message: str, outcome: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.retry_after = retry_after


def outcome(exc: BaseException) -> tuple[str, str, float | None]:
    """(outcome, note, retry_after) of a failed send. A TelegramError says. Another SendError (the paper runtime's
    style) by its text: HTTP 429 FLOOD (retry_after when the reply has it), another 4xx REFUSED, any other status
    UNANSWERED; "...failed: ConnectTimeout" UNSENT, any other request failure UNANSWERED. A requests exception by
    `unsent`, noted by its type name only (it may hold the URL, with the token). Any other exception - a sender
    that does not tell - UNSENT, by its type name."""
    if isinstance(exc, TelegramError):
        return exc.outcome, str(exc), exc.retry_after
    if isinstance(exc, SendError):
        note = str(exc)
        m = _HTTP.search(note)
        if m is None:
            return (UNSENT if note.endswith(": ConnectTimeout") else UNANSWERED), note, None
        status = int(m.group(1))
        if status == 429:
            after = _RETRY_AFTER.search(note)
            return FLOOD, note, float(after.group(1)) if after else None
        return (REFUSED if 400 <= status < 500 else UNANSWERED), note, None
    if isinstance(exc, requests.exceptions.RequestException):
        return (UNSENT if unsent(exc) else UNANSWERED), type(exc).__name__, None
    return UNSENT, type(exc).__name__, None


class TelegramSender:
    """The live runtime's sendMessage client (plain text, no parse mode, no link preview): returns when Telegram
    posted the message (HTTP 200 with "ok": true), else raises TelegramError - UNSENT when no connection was made
    (`timeout`'s connect part), UNANSWERED when the request went out without a usable answer (its read part, a
    connection dropped once made, HTTP 5xx, any other reply that is not ok: Telegram may have posted it), FLOOD for
    HTTP 429 (with its retry_after), REFUSED for any other HTTP 4xx. `probe()` asks getMe, which posts nothing: it
    returns when Telegram answers (any HTTP status below 500) and raises TelegramError (UNSENT or UNANSWERED) when
    it does not. The URL holds the bot token: a request's exception is reported by its type name only and the
    TelegramError is raised outside the except block, so neither its __cause__ nor its __context__ holds the
    original; the token is cut out of Telegram's reply too."""

    def __init__(self, token: str, chat_id: str, session: Any | None = None, *,
                 timeout: tuple[float, float] = (CONNECT_TIMEOUT_S, READ_TIMEOUT_S), api: str = TELEGRAM_API) -> None:
        self._token = token
        self._chat_id = chat_id
        self._api = api
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout

    def _call(self, method: str, payload: dict[str, Any] | None) -> tuple[int, Any, str]:
        """(HTTP status, JSON reply or None, the reply's start) of one Bot API call (a GET without a payload);
        raises TelegramError when the request itself failed."""
        failed: tuple[str, str] | None = None
        status, body, reply = 0, None, ""
        try:
            url = self._api.format(token=self._token, method=method)
            if payload is None:
                resp = self.session.get(url, timeout=self.timeout)
            else:
                resp = self.session.post(url, json=payload, timeout=self.timeout)
            status = int(resp.status_code)
            try:
                body = resp.json()
            except ValueError:  # not JSON (requests' JSONDecodeError is a ValueError)
                body = None
            reply = str(resp.text)[:200]
        except Exception as exc:  # noqa: BLE001 - the URL holds the token: type name only
            failed = (UNSENT if unsent(exc) else UNANSWERED), type(exc).__name__
        if failed is not None:
            raise TelegramError(f"telegram {method} failed: {failed[1]}", failed[0])
        if self._token:
            reply = reply.replace(self._token, "<token>")
        return status, body, reply

    def __call__(self, text: str) -> None:
        status, body, reply = self._call("sendMessage", {"chat_id": self._chat_id, "text": text,
                                                         "disable_web_page_preview": True})
        if status == 200 and isinstance(body, dict) and body.get("ok") is True:
            return
        if status == 429:
            params = body.get("parameters") if isinstance(body, dict) else None
            after = params.get("retry_after") if isinstance(params, dict) else None
            wait = float(after) if isinstance(after, (int, float)) and not isinstance(after, bool) else None
            raise TelegramError(f"telegram HTTP 429: {reply}", FLOOD, wait)
        raise TelegramError(f"telegram HTTP {status}: {reply}", REFUSED if 400 <= status < 500 else UNANSWERED)

    def probe(self) -> None:
        status, _, _ = self._call("getMe", None)
        if status >= 500:
            raise TelegramError(f"telegram getMe HTTP {status}", UNANSWERED)


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
    failed: bool = False  # an attempt for it has failed (retried)
    unanswered: int = 0  # its sends Telegram left unanswered (each may have posted it)
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
        self.failures = 0  # failed attempts in a row (a send or a probe)
        self.unanswered = 0  # sends and probes Telegram left unanswered since it last answered a send
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
            self._last = {k: t for k, t in self._last.items() if 0 <= now - t < self.dedupe_s}  # a step back: none
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
        if m.unanswered:  # Telegram may have posted it: deduplicated as if delivered
            self._last[m.key] = self.clock()
        self.dropped += 1
        print(f"[LIVE] telegram message dropped ({why}): {m.body}", file=sys.stderr)
        self._cond.notify_all()

    def pump(self) -> float | None:
        """One delivery attempt for the oldest queued message (the worker thread's step; tests call it directly).
        None when nothing is queued, 0.0 after a delivery or a drop, else the pause before the next attempt. While
        `unanswered`, the attempt starts with the sender's probe() when it has one and sends nothing unless
        Telegram answers it."""
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
        text = (m.body + (f" ({late / 60:.0f}분 지연)" if late >= LATE_NOTE_S else "")
                + (f" {RESENT}" if m.unanswered else ""))
        probe = getattr(self.sender, "probe", None)
        error: Exception | None = None
        sent = False
        try:
            if self.unanswered and callable(probe):
                probe()  # posts nothing: is Telegram answering again?
            sent = True
            self.sender(text)
        except Exception as exc:  # noqa: BLE001 - placed by `outcome`
            error = exc
        with self._cond:
            self._inflight = None
            if error is None:
                if m in self._pending:
                    self._pending.remove(m)
                self._queued.discard(m.key)
                self._last[m.key] = now
                self.sent.append(text)
                self.sent_count += 1
                self.failures = self.refusals = self.unanswered = 0
                self._cond.notify_all()
                return 0.0
            kind, self.last_error, retry_after = outcome(error)
            if sent and kind == REFUSED:  # never accepted on a retry: out of the way at once
                self.refusals += 1
                self.unanswered = 0
                self._drop(m, f"refused: {self.last_error}")
                return 0.0
            self.failures += 1
            m.failed = True
            if not m.logged:
                m.logged = True
                print(f"[LIVE] telegram send failed, retrying ({self.last_error}): {m.body}", file=sys.stderr)
            if sent and kind != UNANSWERED:  # never left, or not taken (429): sent again soon, as it is
                if kind == FLOOD:
                    self.unanswered = 0  # Telegram answered
                pause = min(RETRY_BASE_S * 2.0 ** min(self.failures - 1, 10), RETRY_MAX_S)
                return max(pause, min(retry_after or 0.0, MAX_AGE_S))
            self.unanswered += 1  # the send, or the probe before it, went unanswered
            if sent:
                m.unanswered += 1
                if m.unanswered >= MAX_SENDS:
                    self._drop(m, f"{MAX_SENDS} sends unanswered: Telegram may have posted it")
                    return 0.0
            return min(UNANSWERED_BASE_S * 2.0 ** min(self.unanswered - 1, 10), UNANSWERED_MAX_S)

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
