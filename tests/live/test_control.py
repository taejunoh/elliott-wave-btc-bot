from __future__ import annotations

import queue
import socket
import sqlite3
import threading
import time
import traceback
from typing import Any

import pytest
import requests
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError, NameResolutionError, NewConnectionError, ProtocolError

from ewbot.live.control import (
    OFFSET_KEY,
    Command,
    ControlError,
    TelegramControl,
    command_word,
)
from ewbot.live.health import Heartbeat, fail_url
from ewbot.live.notify import (
    FLOOD,
    REFUSED,
    RESENT,
    UNANSWERED,
    UNSENT,
    LiveNotifier,
    TelegramError,
    TelegramSender,
    fit,
)
from ewbot.paper.notify import SendError
from tests.live.helpers import Clock, journal
from tests.paper.helpers import FakeSender

TOKEN = "123456:SECRET-TOKEN"
SEND_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


class Resp:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    @property
    def text(self) -> str:
        return str(self._body)


class Session:
    """getUpdates (and heartbeat GETs) answer from `answers` in order, then with empty batches; getMe answers
    with the bot `ewbot_bot` (after `me_failures` connection errors); POSTs answer from `posts`."""

    def __init__(self, answers: list, posts: list | None = None, me_failures: int = 0) -> None:
        self.answers = answers
        self.posts = posts or []
        self.me_failures = me_failures
        self.calls: list[tuple] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, dict(params or {}), timeout))
        if url.endswith("/getMe") and self.me_failures:
            self.me_failures -= 1
            a: Any = requests.exceptions.ConnectionError("getMe down")
        elif url.endswith("/getMe"):
            a = Resp(200, {"ok": True, "result": {"id": 5, "is_bot": True, "username": "ewbot_bot"}})
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


class Telegram:
    """api.telegram.org as the live TelegramSender's session: sendMessage posts the text to `chat`, getMe answers.
    Until clock time `back` Telegram takes every request but its answer times out (requests' ReadTimeout once the
    15 s read timeout has passed on the clock) - a sendMessage is posted all the same. `fail` holds what the next
    requests get instead: an exception (nothing posted) or a reply."""

    def __init__(self, clock: Clock, back: float = 0.0, fail: list | None = None) -> None:
        self.clock, self.back, self.fail = clock, back, list(fail or [])
        self.chat: list[str] = []
        self.calls: list[tuple[str, Any]] = []

    def _answer(self, url: str, timeout: Any, text: str | None) -> Any:
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, timeout))
        if self.fail:
            a = self.fail.pop(0)
            if isinstance(a, BaseException):
                raise a
            return a
        if text is not None:
            self.chat.append(text)
        if self.clock.now < self.back:
            self.clock.now += 15.0
            raise requests.exceptions.ReadTimeout(f"{url}: Read timed out. (read timeout=15)")
        return Resp(200, {"ok": True, "result": {"message_id": len(self.chat)} if text else {"username": "ewbot_bot"}})

    def post(self, url, json=None, timeout=None):
        return self._answer(url, timeout, json["text"])

    def get(self, url, params=None, timeout=None):
        return self._answer(url, timeout, None)


def worker(n: LiveNotifier, clock: Clock) -> list[float]:
    """The sending thread on a fake clock: an attempt, then its pause, until the queue is empty. The pauses."""
    pauses: list[float] = []
    while (pause := n.pump()) is not None:
        pauses.append(pause)
        clock.now += pause
    return pauses


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


def test_the_thread_survives_any_error_and_stop_ends_its_wait(tmp_path, monkeypatch):
    monkeypatch.setattr("ewbot.live.control.ERROR_BACKOFF_S", 60.0)  # a back-off far longer than the test waits
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
    deadline = time.monotonic() + 10
    while c.failures == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert c.failures == 1 and c.last_error == "OperationalError" and c.alive and q.empty()
    c.stop()  # ends the 60 s back-off: the thread is gone long before it would have ended
    th.join(15)
    assert not th.is_alive() and not c.alive
    jr.close()


def test_the_notifier_queues_and_never_blocks_its_caller(capsys):
    entered, gate = threading.Event(), threading.Event()
    texts: list[str] = []

    def hanging(text: str) -> None:  # Telegram not answering
        entered.set()
        gate.wait(30)
        texts.append(text)

    n = LiveNotifier(hanging, max_pending=3)
    n.start()
    assert n("STOP_GAP", "a")
    assert entered.wait(10)
    assert n("BREAKER", "b") and n("X", "c") and n("X", "d") and n("X", "e")  # full: the oldest waiting ones go
    assert texts == [] and n.dropped == 2 and n.pending == 3  # every call returned while the send still hangs
    assert "dropped (queue full): [LIVE] b" in capsys.readouterr().err  # the container log keeps it
    gate.set()
    assert n.flush(30) and texts == ["[LIVE] a", "[LIVE] d", "[LIVE] e"]
    n.stop()


def test_undelivered_messages_go_to_the_log_on_shutdown(capsys):
    gate = threading.Event()
    n = LiveNotifier(lambda text: gate.wait(30))  # Telegram hangs
    n.start()
    n("A", "first (in flight)")
    n("FATAL", "[중단] RuntimeError: boom")
    assert not n.flush(0.2)
    n.stop()
    err = capsys.readouterr().err
    assert err.count("[LIVE] first (in flight)") == 1 and err.count("[LIVE] [중단] RuntimeError: boom") == 1
    gate.set()


def test_a_failed_send_is_kept_retried_and_printed_never_deduplicated_away(capsys):
    clock = Clock(1_000.0)
    sender = FakeSender(fail=2)
    n = LiveNotifier(sender, clock=clock)
    alert = "[중대 사건] 거래 1이 60초 넘게 보호되지 않음"
    assert n("INCIDENT", alert) and not n("INCIDENT", alert)  # queued once
    assert (n.failing_since, n.oldest_pending, n.last_attempt) == (None, 1_000.0, None)  # queued, not tried yet
    assert n.pump() == 2.0 and (n.failing_since, n.pending, n.last_attempt) == (1_000.0, 1, 1_000.0)
    assert f"[LIVE] {alert}" in capsys.readouterr().err  # the full text is in the container log
    clock.now += 2
    assert n.pump() == 4.0 and capsys.readouterr().err == ""  # printed once per message
    clock.now += 64
    assert n.pump() == 0.0 and sender.texts == [f"[LIVE] {alert} (1분 지연)"]
    assert n.failing_since is None and n.oldest_pending is None and n.pump() is None
    assert not n("INCIDENT", alert)  # delivered: deduplicated now
    clock.now += 601
    assert n("INCIDENT", alert)
    stale = LiveNotifier(FakeSender(fail=99), clock=clock)
    stale("X", "y")
    stale.pump()
    assert stale.failing_since == clock.now
    clock.now += 1_801  # kept for 30 minutes at most
    assert stale.pump() == 0.0 and (stale.pending, stale.dropped, stale.failing_since) == (0, 1, None)  # cleared
    assert stale("X", "y")  # it never left: queued again at once


def test_a_refused_message_is_dropped_at_once_and_never_holds_the_queue(capsys):
    clock = Clock(0.0)
    texts: list[str] = []
    answers = {"bad": "telegram HTTP 400: {\"ok\":false,\"description\":\"Bad Request: message text is empty\"}",
               "blocked": "telegram HTTP 403: {\"ok\":false,\"description\":\"Forbidden: bot was blocked by the user\"}",
               "flood": "telegram HTTP 429: {\"ok\":false,\"parameters\":{\"retry_after\":5}}",
               "gateway": "telegram HTTP 502: <html>"}

    def sender(text: str) -> None:
        for word, error in answers.items():
            if word in text:
                raise SendError(error)
        texts.append(text)

    n = LiveNotifier(sender, clock=clock)
    n("AUDIT_MISMATCH", "bad text")
    n("STOP_GAP", "[손절 갭] 시장가 청산")
    assert n.pump() == 0.0 and n.pump() == 0.0 and texts == ["[LIVE] [손절 갭] 시장가 청산"]  # at once, not in 30 min
    assert (n.dropped, n.failing_since, n.refusals, n.pending) == (1, None, 0, 0)  # a delivery resets refusals
    err = capsys.readouterr().err
    assert "HTTP 400" in err and "[LIVE] bad text" in err  # the refused text is in the container log
    for word in ("flood", "gateway"):  # 429 and 5xx are retried
        n("X", word)
        assert n.pump() > 0 and n.pending == 1 and n.failing_since == clock.now
        answers.pop(word)
        assert n.pump() == 0.0 and n.pending == 0
    for i in range(3):
        n("X", f"blocked {i}")
        assert n.pump() == 0.0
    assert n.refusals == 3 and n.pending == 0  # the runtime counts this as Telegram unusable


def test_a_send_telegram_took_but_left_unanswered_goes_again_only_once_telegram_answers(capsys):
    """2026-10-05 01:11 UTC: for two minutes Telegram took every sendMessage but its answer timed out (getMe's too).
    Each ReadTimeout was retried as a failure - 2 s doubling - and one alert reached the chat six times, the last two
    with (2분 지연). A send left unanswered may have been posted: getMe, which posts nothing, is asked before
    anything more is sent, the message goes again only once Telegram answers, and says it may be a copy."""
    clock = Clock(1_000.0)
    tg = Telegram(clock, back=1_120.0)
    n = LiveNotifier(TelegramSender(TOKEN, "42", session=tg), clock=clock)
    alert = "[텔레그램 명령 수신 이상] getUpdates 5회 연속 실패 (getUpdates failed: ReadTimeout)"
    assert n("TELEGRAM", alert)
    pauses = worker(n, clock)
    assert tg.chat == [f"[LIVE] {alert}", f"[LIVE] {alert} (4분 지연) {RESENT}"]  # not six copies
    assert [m for m, _ in tg.calls] == ["sendMessage", "getMe", "getMe", "sendMessage"] and pauses == [60.0, 120.0, 0.0]
    assert {t for _, t in tg.calls} == {(5.0, 15.0)}  # connect 5 s, read 15 s: a connect failure is told apart
    err = capsys.readouterr().err
    assert err.count(alert) == 1 and "(telegram sendMessage failed: ReadTimeout)" in err  # in the container log
    assert (n.sent_count, n.unanswered, n.failing_since) == (1, 0, None) and not n("TELEGRAM", alert)


def test_alerts_queued_while_telegram_hangs_wait_for_it_and_go_out_once_each():
    clock = Clock(1_000.0)
    tg = Telegram(clock, back=1_600.0)  # ten minutes
    n = LiveNotifier(TelegramSender(TOKEN, "42", session=tg), clock=clock)
    n("TELEGRAM", "first")
    assert n.pump() == 60.0 and (n.failing_since, n.last_attempt, n.unanswered) == (1_000.0, 1_000.0, 1)
    clock.now += 60.0
    for i in range(3):
        assert n("X", f"alert {i}")
    assert not n("TELEGRAM", "first")  # still queued
    seen = []
    while (pause := n.pump()) is not None:
        seen.append((pause, n.failing_since, n.last_attempt, n.pending))
        clock.now += pause
    assert seen == [(120.0, 1_000.0, 1_075.0, 4), (240.0, 1_000.0, 1_210.0, 4), (240.0, 1_000.0, 1_465.0, 4),
                    (0.0, None, 1_720.0, 3), (0.0, None, 1_720.0, 2), (0.0, None, 1_720.0, 1), (0.0, None, 1_720.0, 0)]
    assert tg.chat == ["[LIVE] first", f"[LIVE] first (12분 지연) {RESENT}",  # nothing sent while getMe hung
                       "[LIVE] alert 0 (11분 지연)", "[LIVE] alert 1 (11분 지연)", "[LIVE] alert 2 (11분 지연)"]
    assert [m for m, _ in tg.calls].count("getMe") == 4 and n.dropped == 0


def test_a_message_whose_sends_go_unanswered_is_sent_three_times_at_most(capsys):
    """A sender without getMe (any Sender): an unanswered send goes again after 60 s, then 120 s, each copy marked;
    after the third the message is dropped as possibly posted, and the same alert is not queued again for 10
    minutes."""
    clock = Clock(1_000.0)
    chat: list[str] = []

    def takes_but_never_answers(text: str) -> None:
        chat.append(text)
        raise TelegramError("telegram sendMessage failed: ReadTimeout", UNANSWERED)

    n = LiveNotifier(takes_but_never_answers, clock=clock)
    alert = "[중대 사건] 거래 1이 60초 넘게 보호되지 않음"
    assert n("INCIDENT", alert)
    assert worker(n, clock) == [60.0, 120.0, 0.0]
    assert chat == [f"[LIVE] {alert}", f"[LIVE] {alert} (1분 지연) {RESENT}", f"[LIVE] {alert} (3분 지연) {RESENT}"]
    assert (n.pending, n.dropped, n.failing_since, n.sent_count) == (0, 1, None, 0)
    err = capsys.readouterr().err
    assert f"dropped (3 sends unanswered: Telegram may have posted it): [LIVE] {alert}" in err
    assert not n("INCIDENT", alert)  # possibly in the chat already: not queued again for 10 minutes
    clock.now += 601
    assert n("INCIDENT", alert)


def test_a_send_that_never_left_or_was_not_taken_goes_again_soon_unmarked():
    clock = Clock(1_000.0)
    flood = Resp(429, {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 7",
                       "parameters": {"retry_after": 7}})
    refused_conn = requests.exceptions.ConnectionError(MaxRetryError(None, SEND_URL, NewConnectionError(
        None, "Failed to establish a new connection: [Errno 111] Connection refused")))  # as requests raises them
    no_dns = requests.exceptions.ConnectionError(MaxRetryError(None, SEND_URL, NameResolutionError(
        "api.telegram.org", None, socket.gaierror(8, "nodename nor servname provided, or not known"))))
    connect_timeout = requests.exceptions.ConnectTimeout(MaxRetryError(None, SEND_URL, ConnectTimeoutError(
        None, "Connection to api.telegram.org timed out. (connect timeout=5)")))
    tg = Telegram(clock, fail=[flood, refused_conn, no_dns, connect_timeout])
    n = LiveNotifier(TelegramSender(TOKEN, "42", session=tg), clock=clock)
    n("STOP_MOVED", "[손절 이동] 70,000 → 71,000")
    assert worker(n, clock) == [7.0, 4.0, 8.0, 16.0, 0.0]  # 429's retry_after, then 2 s doubling: as before
    assert tg.chat == ["[LIVE] [손절 이동] 70,000 → 71,000"]  # one copy, no resend note
    assert [m for m, _ in tg.calls] == ["sendMessage"] * 5  # no getMe: no send went unanswered
    assert (n.unanswered, n.failures, n.failing_since) == (0, 0, None)


def test_the_live_sender_places_each_outcome_and_never_carries_the_token():
    clock = Clock(0.0)
    cases: list[tuple[Any, str | None]] = [
        (Resp(200, {"ok": True, "result": {"message_id": 1}}), None),
        (requests.exceptions.ConnectionError(MaxRetryError(None, SEND_URL, NewConnectionError(None, SEND_URL))), UNSENT),
        (requests.exceptions.ConnectTimeout(MaxRetryError(None, SEND_URL, ConnectTimeoutError(None, SEND_URL))), UNSENT),
        (requests.exceptions.ReadTimeout(f"{SEND_URL}: Read timed out. (read timeout=15)"), UNANSWERED),
        (requests.exceptions.ConnectionError(ProtocolError("Connection aborted.", ConnectionResetError(54, SEND_URL))),
         UNANSWERED),  # reset after the request went out
        (requests.exceptions.ChunkedEncodingError(SEND_URL), UNANSWERED),
        (requests.exceptions.SSLError(MaxRetryError(None, SEND_URL, None)), UNANSWERED),
        (RuntimeError(SEND_URL), UNANSWERED),  # anything else: it may have gone out
        (Resp(200, ValueError(SEND_URL)), UNANSWERED),  # 200 but not Telegram's reply
        (Resp(200, {"ok": False}), UNANSWERED),
        (Resp(502, ValueError("<html>502 Bad Gateway</html>")), UNANSWERED),
        (Resp(429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 7}}), FLOOD),
        (Resp(400, {"ok": False, "description": "Bad Request: message text is empty"}), REFUSED),
        (Resp(403, {"ok": False, "description": "Forbidden: bot was blocked by the user"}), REFUSED),
    ]
    tg = Telegram(clock, fail=[c for c, _ in cases])
    send = TelegramSender(TOKEN, "42", session=tg)
    for case, outcome in cases:
        if outcome is None:
            send("posted")
            continue
        with pytest.raises(TelegramError) as err:
            send("x")
        assert err.value.outcome == outcome, case
        assert err.value.retry_after == (7.0 if outcome == FLOOD else None)
        assert _chain(err.value) == [err.value]  # neither __cause__ nor __context__ keeps the original
        text = str(err.value) + repr(err.value) + "".join(traceback.format_exception(err.value))
        assert TOKEN not in text and "SECRET" not in text
    assert "HTTP 403: " in str(err.value) and "bot was blocked" in str(err.value)
    tg.fail = [Resp(200, {"ok": True, "result": {}}), Resp(401, {"ok": False, "description": "Unauthorized"}),
               Resp(502, ValueError("<html>")), requests.exceptions.ReadTimeout(SEND_URL),
               requests.exceptions.ConnectionError(MaxRetryError(None, SEND_URL, NewConnectionError(None, SEND_URL)))]
    send.probe()
    send.probe()  # answered: the send itself is refused, and counted as such
    for outcome in (UNANSWERED, UNANSWERED, UNSENT):
        with pytest.raises(TelegramError) as err:
            send.probe()
        assert err.value.outcome == outcome and TOKEN not in str(err.value) and _chain(err.value) == [err.value]
    assert [m for m, _ in tg.calls[-5:]] == ["getMe"] * 5 and len(tg.chat) == 0  # getMe posts nothing


def _loopback(hold: bool) -> int:
    """A one-connection server on 127.0.0.1 that reads the request, then closes the connection at once or (`hold`)
    keeps it a second without an answer."""
    srv = socket.create_server(("127.0.0.1", 0))

    def run() -> None:
        conn, _ = srv.accept()
        with conn, srv:
            conn.recv(65_536)
            if hold:
                time.sleep(1.0)

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


def test_the_real_http_stack_tells_a_request_that_never_left_on_loopback_sockets():
    """requests and urllib3 as installed, on loopback sockets only: nobody listening (the connection is refused) -
    the request never left; a server that reads it and closes, or never answers - it went out."""
    free = socket.create_server(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    session = requests.Session()
    session.trust_env = False  # never through a proxy
    for p, outcome in ((port, UNSENT), (_loopback(False), UNANSWERED), (_loopback(True), UNANSWERED)):
        send = TelegramSender(TOKEN, "42", session=session, timeout=(2.0, 0.3),
                              api=f"http://127.0.0.1:{p}/bot{{token}}/{{method}}")
        with pytest.raises(TelegramError) as err:
            send("x")
        assert err.value.outcome == outcome and TOKEN not in str(err.value), str(err.value)


def test_get_me_is_retried_and_a_thread_that_stops_answering_is_overdue(tmp_path, capsys):
    jr = journal(tmp_path)
    jr.put(OFFSET_KEY, {"offset": 1, "ts": 0.0})
    clock = Clock(0.0)
    s = Session([], me_failures=2)
    c = TelegramControl(TOKEN, "42", jr, session=s, clock=clock, sleep=lambda wait: None)
    q: queue.Queue[Command] = queue.Queue()

    def asked() -> int:
        return sum(1 for call in s.calls if call[1].endswith("/getMe"))

    for t, calls in ((0.0, 1), (10.0, 1), (70.0, 2), (150.0, 2), (200.0, 3)):  # again after 60 s, then 120 s
        clock.now = t
        c.poll_once(q)
        assert asked() == calls
    assert c.bot == "ewbot_bot" and c.failures == 0  # getMe failing never counts as a failed poll
    clock.now = 300.0
    c.poll_once(q)
    assert asked() == 3  # known: not asked again
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 2 and "getMe failed" in err[0] and "getMe ok" in err[1]
    assert c.overdue(300.0 + 45) == 0.0 and c.overdue(300.0 + 45 + 120) == 120.0  # a long poll takes up to 35 s
    s.answers.append(Resp(429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 200}}))
    c.poll_once(q)  # waits 200 s: no lateness while it waits
    assert c.overdue(300.0 + 200 + 45) == 0.0 and c.overdue(300.0 + 200 + 45 + 60) == 60.0
    jr.close()


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


def test_a_wall_clock_step_back_never_withholds_a_ping_or_an_alert():
    """Final review B minor 7: the heartbeat paces on the wall clock - after it steps back the next ping goes out at
    once instead of after the size of the step (which looks like a dead VPS to healthchecks.io); the notifier's
    10-minute dedupe never holds a message back for the size of a step either."""
    clock = Clock(10_000.0)
    s = Session([Resp(200, {}), Resp(200, {})])
    hb = Heartbeat("https://hc-ping.com/abc", session=s, clock=clock)
    assert hb.beat()
    clock.now -= 3_600.0
    assert hb.beat() and len(s.calls) == 2
    sender = FakeSender()
    n = LiveNotifier(sender, clock=clock)
    assert n("BREAKER", "same text") and n.pump() == 0.0
    clock.now -= 3_600.0
    assert n("BREAKER", "same text") and n.pump() == 0.0 and len(sender.texts) == 2
