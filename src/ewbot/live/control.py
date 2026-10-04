"""Telegram commands (Plan 3 spec 3.2 `control`): /status, /pause, /resume, /flatten (confirmed with
/flatten_confirm sent within 60 s of it) and /help.

Accepted only from a private chat with the bot whose chat id is the configured TELEGRAM_CHAT_ID (the user's own
id) and whose sender is that user: group, channel and edited messages, forwarded messages, messages via a bot
and commands addressed to another bot (`/pause@other_bot`; `/pause@<this bot>` only when getMe named it) are
ignored. A background thread long-polls getUpdates and queues `Command(word, sent)`; the runtime executes them on
its main loop, so no trading state is shared across threads. `sent` is the message's Telegram date: the runtime
ignores a command that is old by the time it would run (live/runtime.py COMMAND_MAX_AGE_S) and times the
/flatten confirmation on the messages, not on when it handled them.

The update offset is stored in the journal with the time it was stored, once per batch and before the batch is
handed over: a crash can lose a command (at most once), never repeat one. On the first start (no stored offset)
the backlog is skipped without acting on it. A stored offset older than 24 h is not sent (Telegram keeps
updates 24 h at most, and may number the next update at random after a week without any): getUpdates then
returns what is unconfirmed, and the runtime's age check still applies.

The thread never dies of an error: every exception is counted (`failures` in a row, `last_error`; `conflicts`
counts HTTP 409 - another getUpdates poller or a webhook on this bot), waited out (5 s doubling to 60 s, or
HTTP 429's retry_after; stop() ends the wait) and printed to stderr once per change of state; `alive` tells the
runtime whether the thread runs. The HTTP layer's exceptions may contain the bot token (it is part of the URL),
so only their type name, the HTTP status and Telegram's own `description` are ever reported."""

from __future__ import annotations

import queue
import sys
import threading
import time
from typing import Any, Callable, NamedTuple

import requests

from ewbot.live.journal import Journal

API = "https://api.telegram.org/bot{token}/{method}"
COMMANDS = ("/status", "/pause", "/resume", "/flatten", "/flatten_confirm", "/help")
OFFSET_KEY = "telegram_offset"
OFFSET_MAX_AGE_S = 86_400.0
POLL_TIMEOUT_S = 25
ERROR_BACKOFF_S = 5.0
MAX_BACKOFF_S = 60.0
MAX_RETRY_AFTER_S = 300.0
FORWARDED = ("forward_origin", "forward_from", "forward_from_chat", "forward_sender_name", "forward_date")
HINTS = {401: "the bot token was refused", 409: "another getUpdates poller or a webhook uses this bot"}


class ControlError(RuntimeError):
    """A Telegram call failed. The message never contains the token: an exception's type name, or the HTTP
    status with Telegram's description. `status` and `retry_after` (HTTP 429) when Telegram answered."""

    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class Command(NamedTuple):
    word: str  # one of COMMANDS
    sent: float  # the message's Telegram date (Unix seconds)


def command_word(text: str, bot: str | None = None) -> str | None:
    """'/pause now' -> '/pause'; '/pause@<bot>' only when `bot` is this bot's username (unknown: bare commands
    only); anything that is not a known command -> None."""
    parts = (text or "").split()
    if not parts:
        return None
    word, _, target = parts[0].partition("@")
    if target and (bot is None or target.lower() != bot.lower()):
        return None
    word = word.lower()
    return word if word in COMMANDS else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _stored_offset(stored: Any, now: float) -> int | None:
    """The stored offset when it is an update id stored at most OFFSET_MAX_AGE_S ago, else None (not sent)."""
    s = _dict(stored)
    off, ts = s.get("offset"), s.get("ts")
    if not isinstance(off, int) or isinstance(off, bool) or not isinstance(ts, (int, float)):
        return None
    return off if now - float(ts) <= OFFSET_MAX_AGE_S else None


class TelegramControl:
    def __init__(self, token: str, chat_id: str, journal: Journal, *, session: Any | None = None,
                 timeout: int = POLL_TIMEOUT_S, sleep: Callable[[float], object] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._token = token
        self.chat_id = str(chat_id).strip()
        self.journal = journal
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout
        self.clock = clock
        self._stop = threading.Event()
        self.sleep = sleep if sleep is not None else self._stop.wait  # the back-off pause (stop() ends it)
        self.bot: str | None = None  # this bot's username, from getMe when the thread starts
        self.ignored = 0  # updates not from the user's private chat (or forwarded, edited, via a bot)
        self.skipped = 0  # backlog updates skipped on the first start
        self.failures = 0  # failed polls in a row
        self.conflicts = 0  # HTTP 409 answers so far
        self.last_error: str | None = None
        self.last_ok: float | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ Telegram calls
    def _call(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        """GET a Bot API method -> its result. Raises ControlError outside any except block, so neither its
        __cause__ nor its __context__ holds the token-bearing exception."""
        failed: str | None = None
        status: int | None = None
        resp: Any = None
        data: Any = None
        try:
            resp = self.session.get(API.format(token=self._token, method=method), params=params, timeout=timeout)
            status = int(resp.status_code)
        except Exception as exc:  # noqa: BLE001 - the URL holds the token: type name only
            failed = type(exc).__name__
        if failed is None:
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001 - not JSON (an HTML error page): the status tells
                data = None
        if failed is not None:
            raise ControlError(f"{method} failed: {failed}")
        body = _dict(data)
        if body.get("ok") is True:
            return body.get("result")
        desc = str(body.get("description") or "")[:120]
        if self._token:
            desc = desc.replace(self._token, "<token>")
        retry = _dict(body.get("parameters")).get("retry_after")
        hint = HINTS.get(status or 0)
        raise ControlError(f"{method} failed: HTTP {status}" + (f": {desc}" if desc else "") + (f" ({hint})" if hint else ""),
                           status=status, retry_after=float(retry) if isinstance(retry, (int, float)) else None)

    def _ids(self, result: Any) -> tuple[list[dict[str, Any]], list[int]]:
        updates = [u for u in (result if isinstance(result, list) else []) if isinstance(u, dict)]
        ids = [u["update_id"] for u in updates if isinstance(u.get("update_id"), int) and not isinstance(u["update_id"], bool)]
        return updates, ids

    def _accept(self, upd: dict[str, Any]) -> Command | None:
        msg = upd.get("message")
        chat, sender = _dict(_dict(msg).get("chat")), _dict(_dict(msg).get("from"))
        if (not isinstance(msg, dict) or chat.get("type") != "private" or str(chat.get("id")) != self.chat_id
                or str(sender.get("id")) != self.chat_id or any(f in msg for f in FORWARDED)
                or "via_bot" in msg or not isinstance(msg.get("date"), (int, float))):
            self.ignored += 1
            return None
        text = msg.get("text")
        word = command_word(text if isinstance(text, str) else "", self.bot)
        return None if word is None else Command(word, float(msg["date"]))

    def fetch(self) -> list[Command]:
        """One getUpdates long poll -> the user's commands, in order. Raises on any failure."""
        stored = self.journal.get(OFFSET_KEY)
        if stored is None:
            return self._skip_backlog()
        params: dict[str, Any] = {"timeout": self.timeout, "allowed_updates": '["message"]'}
        offset = _stored_offset(stored, self.clock())
        if offset is not None:
            params["offset"] = offset
        updates, ids = self._ids(self._call("getUpdates", params, self.timeout + 10))
        if ids:  # once per batch, before anything is handed over: a crash loses a command, never repeats one
            self.journal.put(OFFSET_KEY, {"offset": max(ids) + 1, "ts": self.clock()})
        out: list[Command] = []
        for upd in updates:
            cmd = self._accept(upd)
            if cmd is not None:
                out.append(cmd)
        return out

    def _skip_backlog(self) -> list[Command]:
        """First start: offset -1 makes Telegram drop every waiting update but the newest; the offset after it is
        stored and nothing is acted on (an empty backlog stores no update id: from now on ordinary polls)."""
        _, ids = self._ids(self._call("getUpdates", {"offset": -1, "timeout": 0, "allowed_updates": '["message"]'}, 15))
        self.skipped += len(ids)
        self.journal.put(OFFSET_KEY, {"offset": max(ids) + 1 if ids else None, "ts": self.clock()})
        return []

    def _username(self) -> str | None:
        try:
            name = _dict(self._call("getMe", {}, 10)).get("username")
        except Exception as exc:  # noqa: BLE001 - without the name only bare /commands are accepted
            print(f"[LIVE] telegram getMe failed ({self._why(exc)}): only bare /commands are accepted", file=sys.stderr)
            return None
        return name if isinstance(name, str) and name else None

    # ------------------------------------------------------------------ the thread
    @staticmethod
    def _why(exc: Exception) -> str:
        return str(exc) if isinstance(exc, ControlError) else type(exc).__name__

    def poll_once(self, commands: queue.Queue[Command]) -> None:
        """One long poll; its commands go to `commands`. Any error is counted and waited out, never raised."""
        try:
            batch = self.fetch()
        except Exception as exc:  # noqa: BLE001 - the thread must outlive any error (type name only: the token)
            why = self._why(exc)
            if self.failures == 0 or why != self.last_error:
                print(f"[LIVE] telegram commands: {why} (retrying)", file=sys.stderr)
            self.last_error = why  # before the count: the runtime reads them from another thread
            if isinstance(exc, ControlError) and exc.status == 409:
                self.conflicts += 1
            self.failures += 1
            wait = min(ERROR_BACKOFF_S * 2.0 ** min(self.failures - 1, 10), MAX_BACKOFF_S)
            if isinstance(exc, ControlError) and exc.retry_after:
                wait = min(max(exc.retry_after, 1.0), MAX_RETRY_AFTER_S)
            self.sleep(wait)
            return
        if self.failures:
            print(f"[LIVE] telegram commands ok again after {self.failures} failed polls", file=sys.stderr)
        self.failures = 0
        self.last_ok = self.clock()
        for cmd in batch:
            commands.put(cmd)

    def run(self, commands: queue.Queue[Command]) -> None:
        if self.bot is None:
            self.bot = self._username()
        while not self._stop.is_set():
            self.poll_once(commands)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, commands: queue.Queue[Command]) -> threading.Thread:
        self._stop.clear()
        th = threading.Thread(target=self.run, args=(commands,), name="ewbot-telegram", daemon=True)
        th.start()
        self._thread = th
        return th

    def stop(self) -> None:
        self._stop.set()
