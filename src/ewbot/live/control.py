"""Telegram commands (Plan 3 spec 3.2 `control`): /status, /pause, /resume, /flatten (confirmed with
/flatten_confirm within 60 s) and /help, accepted only from the configured chat id.

A background thread long-polls getUpdates and queues the command words; the runtime executes them on
its own thread, so no trading state is shared across threads. The update offset is stored in the
journal: a restart never replays an old command. Exceptions from the HTTP layer may contain the bot
token (it is part of the URL), so only their type name is ever reported."""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

import requests

from ewbot.live.journal import Journal

UPDATES_API = "https://api.telegram.org/bot{token}/getUpdates"
COMMANDS = ("/status", "/pause", "/resume", "/flatten", "/flatten_confirm", "/help")
OFFSET_KEY = "telegram_offset"
POLL_TIMEOUT_S = 25
ERROR_BACKOFF_S = 5.0


class ControlError(RuntimeError):
    """getUpdates failed (the message never contains the token)."""


def command_word(text: str) -> str | None:
    """'/pause@ewbot_bot now' -> '/pause'; anything that is not a known command -> None."""
    if not text:
        return None
    word = text.strip().split()[0].split("@")[0].lower()
    return word if word in COMMANDS else None


class TelegramControl:
    def __init__(self, token: str, chat_id: str, journal: Journal, *, session: Any | None = None,
                 timeout: int = POLL_TIMEOUT_S, sleep: Callable[[float], None] = time.sleep) -> None:
        self._url = UPDATES_API.format(token=token)
        self.chat_id = str(chat_id)
        self.journal = journal
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout
        self.sleep = sleep
        self.ignored = 0
        self._stop = threading.Event()

    def fetch(self) -> list[str]:
        params: dict[str, Any] = {"timeout": self.timeout, "allowed_updates": '["message"]'}
        offset = self.journal.get(OFFSET_KEY)
        if offset is not None:
            params["offset"] = int(offset)
        try:
            resp = self.session.get(self._url, params=params, timeout=self.timeout + 10)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - the URL holds the token: type name only
            raise ControlError(f"getUpdates failed: {type(exc).__name__}") from None
        if not isinstance(data, dict) or not data.get("ok"):
            raise ControlError(f"getUpdates failed: HTTP {getattr(resp, 'status_code', '?')}")
        out: list[str] = []
        for upd in data.get("result") or []:
            self.journal.put(OFFSET_KEY, int(upd["update_id"]) + 1)
            msg = upd.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                self.ignored += 1
                continue
            cmd = command_word(msg.get("text") or "")
            if cmd is not None:
                out.append(cmd)
        return out

    def run(self, commands: queue.Queue[str]) -> None:
        while not self._stop.is_set():
            try:
                for cmd in self.fetch():
                    commands.put(cmd)
            except ControlError:
                self.sleep(ERROR_BACKOFF_S)

    def start(self, commands: queue.Queue[str]) -> threading.Thread:
        th = threading.Thread(target=self.run, args=(commands,), name="ewbot-telegram", daemon=True)
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()
