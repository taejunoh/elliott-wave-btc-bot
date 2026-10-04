from __future__ import annotations

import queue
import sqlite3
import threading
import time
import traceback
from typing import Any

import pytest
import requests

from ewbot.live.control import (
    OFFSET_KEY,
    Command,
    ControlError,
    TelegramControl,
    command_word,
)
from ewbot.live.health import Heartbeat, fail_url
from ewbot.live.notify import LiveNotifier, fit
from tests.live.helpers import Clock, journal
from tests.paper.helpers import FakeSender

TOKEN = "123456:SECRET-TOKEN"


class Resp:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Session:
    """getUpdates (and heartbeat GETs) answer from `answers` in order, then with empty batches; getMe answers
    with the bot `ewbot_bot`; POSTs answer from `posts`."""

    def __init__(self, answers: list, posts: list | None = None) -> None:
        self.answers = answers
        self.posts = posts or []
        self.calls: list[tuple] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, dict(params or {}), timeout))
        if url.endswith("/getMe"):
            a: Any = Resp(200, {"ok": True, "result": {"id": 5, "is_bot": True, "username": "ewbot_bot"}})
        elif self.answers:
            a = self.answers.pop(0)
        else:
            time.sleep(0.01)  # a long poll that brought nothing
            a = ok()
        if isinstance(a, BaseException):
            raise a
        return a

    def post(self, url, data=None, timeout=None, json=None):
        self.calls.append(("POST", url, data, timeout))
        a = self.posts.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a


def ok(*updates: dict) -> Resp:
    return Resp(200, {"ok": True, "result": list(updates)})


def upd(uid: int, text: str, *, chat: int = 42, sender: int = 42, date: int = 1_000, kind: str = "private",
        **extra: Any) -> dict:
    msg = {"message_id": uid, "date": date, "chat": {"id": chat, "type": kind},
           "from": {"id": sender, "is_bot": False}, "text": text, **extra}
    return {"update_id": uid, "message": msg}


def test_command_words():
    assert command_word("/pause") == "/pause" and command_word("/Flatten_Confirm") == "/flatten_confirm"
    assert command_word("/status please") == "/status" and command_word("  /help\n") == "/help"
    assert command_word("/Flatten_Confirm@EwBot_Bot", "ewbot_bot") == "/flatten_confirm"
    assert command_word("/pause@other_bot", "ewbot_bot") is None  # addressed to another bot
    assert command_word("/pause@ewbot_bot") is None  # this bot's name unknown (getMe failed): bare commands only
    assert command_word("pause") is None and command_word("/rm -rf") is None and command_word("/flatten_confirmed") is None
    for blank in ("", "   ", "　", "\xa0", "\n"):
        assert command_word(blank) is None


def test_only_the_users_private_chat_is_obeyed_and_the_offset_is_stored_once_per_batch(tmp_path):
    jr = journal(tmp_path)
    jr.put(OFFSET_KEY, {"offset": 10, "ts": 4_000.0})  # a running bot: updates up to 9 were handled
    s = Session([ok(upd(10, "/pause", date=4_990), upd(11, "/flatten_confirm", chat=999, sender=999),
                    upd(12, "hello"), upd(13, "/status@ewbot_bot", date=4_995),
                    upd(14, "/resume", sender=777),  # someone else
                    upd(15, "/resume", chat=-100, kind="group"),
                    upd(16, "/pause", forward_origin={"type": "user"}), upd(17, "/pause", forward_date=4_000),
                    upd(18, "/pause", via_bot={"id": 5}), upd(19, "/pause@other_bot"),
                    {"update_id": 20, "edited_message": upd(20, "/flatten_confirm")["message"]},
                    {"update_id": 21, "channel_post": {"chat": {"id": 42, "type": "channel"}, "text": "/flatten"}})])
    writes: list[str] = []
    put = jr.put
    jr.put = lambda key, value: (writes.append(key), put(key, value))  # type: ignore[method-assign]
    c = TelegramControl(TOKEN, "42", jr, session=s, clock=Clock(5_000.0))
    c.bot = "ewbot_bot"  # getMe's answer (run() asks once)
    assert c.fetch() == [Command("/pause", 4_990.0), Command("/status", 4_995.0)] and c.ignored == 8
    assert writes == [OFFSET_KEY] and jr.get(OFFSET_KEY) == {"offset": 22, "ts": 5_000.0}  # once, before the batch
    TelegramControl(TOKEN, "42", jr, session=s, clock=Clock(5_010.0)).fetch()  # a restart resumes after update 21
    params = s.calls[-1][2]
    assert s.calls[-1][1].endswith("/getUpdates") and params["offset"] == 22 and params["allowed_updates"] == '["message"]'
    jr.close()


def test_the_first_start_skips_the_backlog_and_a_day_old_offset_is_not_sent(tmp_path):
    jr = journal(tmp_path)
    clock = Clock(100_000.0)
    s = Session([ok(upd(7, "/flatten", date=20_000)), ok(upd(8, "/status", date=99_990))])
    c = TelegramControl(TOKEN, "42", jr, session=s, clock=clock)
    assert c.fetch() == [] and c.skipped == 1  # yesterday's /flatten is never acted on
    assert (s.calls[0][2]["offset"], s.calls[0][2]["timeout"]) == (-1, 0)  # Telegram drops all but the newest
    assert jr.get(OFFSET_KEY) == {"offset": 8, "ts": 100_000.0}
    assert c.fetch() == [Command("/status", 99_990.0)] and s.calls[1][2]["offset"] == 8
    clock.now += 86_401  # a day without any update: the stored offset is not sent (updates are kept 24 h at most)
    assert c.fetch() == [] and "offset" not in s.calls[2][2]
    empty = TelegramControl(TOKEN, "42", journal(tmp_path, "j2.sqlite"), session=Session([]), clock=clock)
    assert empty.fetch() == [] and empty.skipped == 0 and empty.journal.get(OFFSET_KEY)["offset"] is None
    empty.fetch()  # primed: an ordinary long poll from now on
    assert "offset" not in empty.session.calls[1][2] and empty.session.calls[1][2]["timeout"] == 25
    jr.close()
    empty.journal.close()


def _chain(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    todo: list[BaseException | None] = [exc]
    while todo:
        e = todo.pop()
        if e is not None and e not in out:
            out.append(e)
            todo += [e.__cause__, e.__context__]
    return out


def test_errors_never_carry_the_token(tmp_path):
    jr = journal(tmp_path)
    jr.put(OFFSET_KEY, {"offset": 1, "ts": 0.0})
    leak = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    s = Session([requests.exceptions.ConnectionError(leak), Resp(200, ValueError(leak)),
                 Resp(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})])
    c = TelegramControl(TOKEN, "42", jr, session=s, clock=Clock(0.0))
    for _ in range(3):
        with pytest.raises(ControlError) as err:
            c.fetch()
        assert _chain(err.value) == [err.value]  # neither __cause__ nor __context__ keeps the original
        text = str(err.value) + repr(err.value) + "".join(traceback.format_exception(err.value))
        assert TOKEN not in text and "SECRET" not in text
    jr.close()


def test_poll_failures_are_counted_reported_once_per_change_and_backed_off(tmp_path, capsys):
    jr = journal(tmp_path)
    jr.put(OFFSET_KEY, {"offset": 1, "ts": 0.0})
    conflict = {"ok": False, "error_code": 409,
                "description": "Conflict: terminated by other getUpdates request; make sure that only one bot instance is running"}
    s = Session([Resp(409, conflict), Resp(409, conflict), Resp(401, {"ok": False, "description": "Unauthorized"}),
                 Resp(429, {"ok": False, "description": "Too Many Requests: retry after 30", "parameters": {"retry_after": 30}}),
                 Resp(502, requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)),
                 ok(upd(1, "/status", date=0))])
    waits: list[float] = []
    c = TelegramControl(TOKEN, "42", jr, session=s, clock=Clock(0.0), sleep=waits.append)
    q: queue.Queue[Command] = queue.Queue()
    c.poll_once(q)
    assert c.last_error is not None and c.last_error.startswith("getUpdates failed: HTTP 409: Conflict: terminated by other")
    assert c.last_error.endswith("(another getUpdates poller or a webhook uses this bot)")
    c.poll_once(q)
    c.poll_once(q)
    assert c.last_error == "getUpdates failed: HTTP 401: Unauthorized (the bot token was refused)"
    c.poll_once(q)
    c.poll_once(q)
    assert (c.failures, c.conflicts, c.last_error) == (5, 2, "getUpdates failed: HTTP 502")  # the status, not JSONDecodeError
    assert waits == [5.0, 10.0, 20.0, 30.0, 60.0]  # doubling to 60 s; HTTP 429: Telegram's retry_after
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 4 and "HTTP 409" in lines[0] and "HTTP 401" in lines[1] and "HTTP 429" in lines[2]
    assert "HTTP 502" in lines[3]  # the second 409 in a row is not printed again
    c.poll_once(q)
    assert c.failures == 0 and c.last_ok == 0.0 and q.get_nowait() == Command("/status", 0.0)
    assert capsys.readouterr().err == "[LIVE] telegram commands ok again after 5 failed polls\n"
    jr.close()


def test_the_thread_survives_any_error_and_stop_ends_its_wait(tmp_path):
    jr = journal(tmp_path)
    jr.put(OFFSET_KEY, {"offset": 1, "ts": time.time()})
    s = Session([ok(upd(1, "　")), ok({"message": {"chat": {"id": 42}, "text": "/pause"}}),  # no update_id
                 ok(upd(2, "/flatten", date=int(time.time())))])
    c = TelegramControl(TOKEN, "42", jr, session=s)
    q: queue.Queue[Command] = queue.Queue()
    th = c.start(q)
    assert q.get(timeout=5).word == "/flatten" and c.alive and c.bot == "ewbot_bot"

    def broken(key, value):
        raise sqlite3.OperationalError("database is locked")

    jr.put = broken  # type: ignore[method-assign]
    s.answers.append(ok(upd(3, "/status")))
    deadline = time.monotonic() + 5
    while c.failures == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert c.failures == 1 and c.last_error == "OperationalError" and c.alive and q.empty()
    t0 = time.monotonic()
    c.stop()  # ends the 5 s back-off at once
    th.join(2)
    assert not th.is_alive() and not c.alive and time.monotonic() - t0 < 1.5
    jr.close()


def test_the_notifier_queues_and_never_blocks_its_caller(capsys):
    entered, gate = threading.Event(), threading.Event()
    texts: list[str] = []

    def hanging(text: str) -> None:  # Telegram not answering
        entered.set()
        gate.wait(5)
        texts.append(text)

    n = LiveNotifier(hanging, max_pending=3)
    n.start()
    t0 = time.monotonic()
    assert n("STOP_GAP", "a")
    assert entered.wait(2)
    assert n("BREAKER", "b") and n("X", "c") and n("X", "d") and n("X", "e")  # full: the oldest waiting ones go
    assert time.monotonic() - t0 < 0.5 and n.dropped == 2 and n.pending == 3
    assert "dropped (queue full): [LIVE] b" in capsys.readouterr().err  # the container log keeps it
    gate.set()
    assert n.flush(5) and texts == ["[LIVE] a", "[LIVE] d", "[LIVE] e"]
    n.stop()


def test_a_failed_send_is_kept_retried_and_printed_never_deduplicated_away(capsys):
    clock = Clock(1_000.0)
    sender = FakeSender(fail=2)
    n = LiveNotifier(sender, clock=clock)
    alert = "[중대 사건] 거래 1이 60초 넘게 보호되지 않음"
    assert n("INCIDENT", alert) and not n("INCIDENT", alert)  # queued once
    assert n.pump() == 2.0 and (n.failing_since, n.pending) == (1_000.0, 1)
    assert f"[LIVE] {alert}" in capsys.readouterr().err  # the full text is in the container log
    clock.now += 2
    assert n.pump() == 4.0 and capsys.readouterr().err == ""  # printed once per message
    clock.now += 64
    assert n.pump() == 0.0 and sender.texts == [f"[LIVE] {alert} (1분 지연)"]
    assert n.failing_since is None and n.pump() is None and not n("INCIDENT", alert)  # delivered: deduplicated now
    clock.now += 601
    assert n("INCIDENT", alert)
    stale = LiveNotifier(FakeSender(fail=99), clock=clock)
    stale("X", "y")
    stale.pump()
    clock.now += 1_801  # kept for 30 minutes at most
    assert stale.pump() == 0.0 and (stale.pending, stale.dropped) == (0, 1)


def test_live_notifier_prefixes_truncates_dedupes_and_prunes():
    clock = Clock(1_000.0)
    sender = FakeSender()
    n = LiveNotifier(sender, clock=clock)
    assert n("ENTRY", "진입 W3 롱") and n.pump() == 0.0 and sender.texts == ["[LIVE] 진입 W3 롱"]
    assert not n("ENTRY", "진입 W3 롱")  # within 10 minutes
    clock.now += 601
    assert n("ENTRY", "진입 W3 롱") and n.pump() == 0.0 and len(sender.texts) == 2
    assert n("DAILY", "x" * 5_000) and n.pump() == 0.0
    assert len(sender.texts[-1]) == 4_000 and sender.texts[-1].endswith("…(잘림)")  # Telegram's limit is 4096
    assert len(fit("😀" * 3_000).encode("utf-16-le")) // 2 <= 4_000  # counted in UTF-16 units, as Telegram does
    for i in range(1_000):
        n("COMMAND", f"reply {i}")
        n.pump()
    clock.now += 601
    assert n("COMMAND", "one more") and len(n._last) == 0  # entries older than 10 minutes are pruned
    assert n.pump() == 0.0 and n.sent_count == 1_004 and len(n.sent) == 50
    assert LiveNotifier(None, clock=clock)("X", "y") is False  # no sender: stderr only


def test_heartbeat_pings_once_a_minute_and_logs_state_changes(capsys):
    clock = Clock(0.0)
    s = Session([Resp(200, {}), Resp(404, {}), requests.exceptions.ConnectionError("down"), Resp(200, {})],
                posts=[Resp(200, {})])
    hb = Heartbeat("https://hc-ping.com/abc?create=1", session=s, clock=clock)
    assert hb.beat() and not hb.beat() and s.calls[0][3] == 5.0
    clock.now = 60.0
    assert not hb.beat() and (hb.failures, hb.last_error) == (1, "HTTP 404")  # e.g. a wrong check UUID
    clock.now = 120.0
    assert not hb.beat() and (hb.failures, hb.last_error) == (2, "ConnectionError")
    clock.now = 180.0
    assert hb.beat() and hb.failures == 0
    assert capsys.readouterr().err.splitlines() == ["[LIVE] heartbeat failed: HTTP 404",
                                                    "[LIVE] heartbeat ok again after 2 failed pings"]
    assert hb.fail("fatal: boom")
    assert s.calls[-1][:3] == ("POST", "https://hc-ping.com/abc/fail?create=1", b"fatal: boom")
    assert fail_url("https://hc-ping.com/abc/") == "https://hc-ping.com/abc/fail"
    assert Heartbeat(None).beat() is False and Heartbeat("  ").url is None
