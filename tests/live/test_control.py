from __future__ import annotations

import queue

import pytest
import requests

from ewbot.live.control import ControlError, TelegramControl, command_word
from ewbot.live.health import Heartbeat
from ewbot.live.notify import LiveNotifier
from tests.live.helpers import Clock, journal
from tests.paper.helpers import FakeSender

TOKEN = "123456:SECRET-TOKEN"


class Resp:
    def __init__(self, status: int, body) -> None:
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Session:
    def __init__(self, answers: list) -> None:
        self.answers = answers
        self.calls: list[tuple] = []

    def _next(self):
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params))
        return self._next()

    def post(self, url, data=None, timeout=None, json=None):
        self.calls.append(("POST", url, data))
        return self._next()


def upd(uid: int, chat: int, text: str) -> dict:
    return {"update_id": uid, "message": {"message_id": uid, "chat": {"id": chat}, "text": text}}


def test_command_words():
    assert command_word("/pause") == "/pause" and command_word("/Flatten_Confirm@ewbot_bot") == "/flatten_confirm"
    assert command_word("/status please") == "/status"
    assert command_word("pause") is None and command_word("/rm -rf") is None and command_word("") is None


def test_only_the_allowlisted_chat_is_obeyed_and_the_offset_persists(tmp_path):
    jr = journal(tmp_path)
    s = Session([Resp(200, {"ok": True, "result": [upd(10, 42, "/pause"), upd(11, 999, "/flatten_confirm"),
                                                   upd(12, 42, "hello"), upd(13, 42, "/status@ewbot_bot")]}),
                 Resp(200, {"ok": True, "result": []})])
    c = TelegramControl(TOKEN, "42", jr, session=s)
    assert c.fetch() == ["/pause", "/status"] and c.ignored == 1
    assert jr.get("telegram_offset") == 14
    TelegramControl(TOKEN, "42", jr, session=s).fetch()  # a restart resumes after update 13
    assert s.calls[1][2]["offset"] == 14 and s.calls[0][1].endswith("/getUpdates")
    jr.close()


def test_errors_never_carry_the_token(tmp_path):
    jr = journal(tmp_path)
    s = Session([requests.exceptions.ConnectionError(f"https://api.telegram.org/bot{TOKEN}/getUpdates"),
                 Resp(401, {"ok": False, "description": "Unauthorized"})])
    c = TelegramControl(TOKEN, "42", jr, session=s)
    for _ in range(2):
        with pytest.raises(ControlError) as err:
            c.fetch()
        assert TOKEN not in str(err.value) and err.value.__cause__ is None
    jr.close()


def test_run_queues_commands_and_backs_off_on_errors(tmp_path):
    jr = journal(tmp_path)
    waits: list[float] = []
    c = TelegramControl(TOKEN, "42", jr, sleep=waits.append, session=None)
    answers = [ControlError("x"), ["/status"], ["/pause", "/resume"]]

    def fake_fetch():
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        if not answers:
            c.stop()
        return a

    c.fetch = fake_fetch  # type: ignore[method-assign]
    q: queue.Queue[str] = queue.Queue()
    c.run(q)
    assert [q.get_nowait() for _ in range(3)] == ["/status", "/pause", "/resume"] and waits == [5.0]
    jr.close()


def test_live_notifier_prefixes_dedupes_and_never_raises():
    clock = Clock(1_000.0)
    sender = FakeSender()
    n = LiveNotifier(sender, clock=clock, sleep=lambda s: None)
    assert n("ENTRY", "진입 W3 롱") and sender.texts == ["[LIVE] 진입 W3 롱"]
    assert not n("ENTRY", "진입 W3 롱")  # within 10 minutes
    clock.now += 601
    assert n("ENTRY", "진입 W3 롱") and len(sender.texts) == 2
    failing = LiveNotifier(FakeSender(fail=5), clock=clock, sleep=lambda s: None)
    assert failing("X", "y") is False
    assert LiveNotifier(None, clock=clock)("X", "y") is False


def test_heartbeat_pings_once_a_minute_and_survives_errors():
    clock = Clock(0.0)
    s = Session([Resp(200, {}), requests.exceptions.ConnectionError("down"), Resp(200, {}), Resp(200, {})])
    hb = Heartbeat("https://hc-ping.com/abc", session=s, clock=clock)
    assert hb.beat() and not hb.beat()
    clock.now = 60.0
    assert not hb.beat() and hb.failures == 1
    clock.now = 120.0
    assert hb.beat() and hb.failures == 0
    hb.fail("fatal: boom")
    assert s.calls[-1] == ("POST", "https://hc-ping.com/abc/fail", b"fatal: boom")
    assert Heartbeat(None).beat() is False and Heartbeat("  ").url is None
