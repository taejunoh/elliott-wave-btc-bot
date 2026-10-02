from __future__ import annotations

import pytest
import requests

from ewbot.engine.loop import Trade
from ewbot.paper.ledger import new_ledger
from ewbot.paper.notify import (
    GAP_WARN_S,
    SendError,
    TelegramSender,
    close_text,
    daily_summary_due,
    daily_text,
    decision_events,
    deliver,
    entry_text,
    failure_text,
    gap_seconds,
    make_event,
    position_line,
    queue_events,
    sender_from_env,
    stop_move_text,
)
from ewbot.types import H1
from tests.paper.helpers import FakeSender, ts

TOKEN = "123456:SECRET"


class _Resp:
    """`body` is the parsed JSON reply, or an exception that json() raises (a non-JSON reply)."""

    def __init__(self, status: int, body: object) -> None:
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self) -> object:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Session:
    def __init__(self, resp: _Resp | Exception) -> None:
        self.resp = resp
        self.posts: list[tuple[str, dict, float]] = []

    def post(self, url: str, json: dict, timeout: float) -> _Resp:
        self.posts.append((url, json, timeout))
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def _trade(**kw) -> Trade:
    base = dict(
        entry_ts=ts("2024-02-07 21:00"), exit_ts=ts("2024-02-08 22:00"), setup="W3", direction=1, contracts=19,
        entry_price=23_516.46, exit_price_avg=23_010.75, stop0=23_022.26, tp1=24_219.14, tp2=24_751.39,
        pnl=-123.4, fees=4.5, funding=0.2, r_multiple=-1.08, exit_reason="STOP",
        snapshot={"q_top": 0.6, "s_rev": 0.662, "counts": [{"pattern": "IMP", "phase": "W2"}]},
    )
    base.update(kw)
    return Trade(**base)


def test_telegram_sender_posts_plain_text():
    sess = _Session(_Resp(200, {"ok": True}))
    TelegramSender(TOKEN, "42", session=sess)("hello")
    url, payload, timeout = sess.posts[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert payload == {"chat_id": "42", "text": "hello", "disable_web_page_preview": True} and timeout > 0


@pytest.mark.parametrize("resp", [_Resp(429, {"ok": False}), _Resp(200, {"ok": False}), requests.ConnectionError(f"x {TOKEN}")])
def test_telegram_sender_raises_without_leaking_the_token(resp):
    with pytest.raises(SendError) as exc:
        TelegramSender(TOKEN, "42", session=_Session(resp))("hello")
    assert TOKEN not in str(exc.value)


@pytest.mark.parametrize(
    "resp, expected",
    [
        (_Resp(502, ValueError("<html>bad gateway</html>")), "telegram HTTP 502"),  # non-JSON reply
        (_Resp(200, ValueError("not json")), "telegram HTTP 200"),
        (_Resp(200, ["ok"]), "telegram HTTP 200"),  # JSON, but not an object
        (_Resp(200, None), "telegram HTTP 200"),
        (_Resp(200, RuntimeError(f"decoder broke on {TOKEN}")), "RuntimeError"),  # any other failure: type only
    ],
)
def test_telegram_sender_turns_odd_replies_into_send_errors(resp, expected):
    with pytest.raises(SendError) as exc:
        TelegramSender(TOKEN, "42", session=_Session(resp))("hello")
    assert expected in str(exc.value) and TOKEN not in str(exc.value) and exc.value.__cause__ is None


def test_deliver_notes_only_the_error_type_unless_it_is_a_send_error():
    def leaky(text: str) -> None:
        raise ConnectionError(f"POST https://api.telegram.org/bot{TOKEN}/sendMessage failed")

    _, _, notes = deliver(_events(1), [], leaky, sleep=lambda s: None)
    assert notes == ["send failed for ENTRY:1000:0: ConnectionError"]

    def rejected(text: str) -> None:
        raise SendError("telegram HTTP 429: too many requests")

    _, _, notes = deliver(_events(1), [], rejected, sleep=lambda s: None)
    assert notes == ["send failed for ENTRY:1000:0: telegram HTTP 429: too many requests"]


def test_sender_from_env():
    assert sender_from_env({}) is None
    assert sender_from_env({"TELEGRAM_BOT_TOKEN": TOKEN}) is None
    assert sender_from_env({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": " "}) is None
    assert isinstance(sender_from_env({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}), TelegramSender)


def _events(n: int) -> list[dict]:
    return [make_event("ENTRY", 1000 + i, i, f"msg {i}") for i in range(n)]


def test_deliver_without_a_token_skips_and_drops():
    pending, sent, notes = deliver(_events(2), [], None)
    assert pending == [] and sent == [] and "not configured" in notes[0]
    assert deliver([], [], None) == ([], [], [])


def test_deliver_retries_three_times_then_succeeds():
    sender, sleeps = FakeSender(fail=2), []
    pending, sent, notes = deliver(_events(2), [], sender, sleep=sleeps.append)
    assert pending == [] and sent == ["ENTRY:1000:0", "ENTRY:1001:1"] and notes == []
    assert sender.texts == ["msg 0", "msg 1"] and sleeps == [1.0, 2.0]


def test_deliver_keeps_the_failed_event_and_everything_after_it_pending():
    evs = _events(3)
    sender = FakeSender(fail=0)
    first = deliver(evs[:1], [], sender, sleep=lambda s: None)
    assert first[1] == ["ENTRY:1000:0"]
    sender.fail = 3
    pending, sent, notes = deliver(evs, ["ENTRY:1000:0"], sender, sleep=lambda s: None)
    assert sent == [] and [e["id"] for e in pending] == ["ENTRY:1001:1", "ENTRY:1002:2"]
    assert pending[0]["attempts"] == 1 and "send failed" in notes[0]
    pending, sent, _ = deliver(pending, ["ENTRY:1000:0"], sender, sleep=lambda s: None)  # next run: retried
    assert pending == [] and sent == ["ENTRY:1001:1", "ENTRY:1002:2"]
    assert sender.texts == ["msg 0", "msg 1", "msg 2"]  # each exactly once


@pytest.mark.parametrize(
    "now, last, expected",
    [
        ("2024-03-09 13:59", None, None),  # 08:59 EST
        ("2024-03-09 14:00", None, "2024-03-09"),  # 09:00 EST
        ("2024-03-10 12:59", "2024-03-09", None),  # DST starts: 08:59 EDT
        ("2024-03-10 13:00", "2024-03-09", "2024-03-10"),  # 09:00 EDT
        ("2024-03-10 15:00", "2024-03-10", None),  # once per New York day
        ("2024-03-11 03:00", "2024-03-10", None),  # 23:00 EDT is still 2024-03-10 in New York
        ("2024-11-03 13:30", "2024-11-02", None),  # DST ends: 08:30 EST
        ("2024-11-03 14:07", "2024-11-02", "2024-11-03"),  # 09:07 EST
        ("2024-07-01 13:07", "2024-06-30", "2024-07-01"),  # summer: 09:07 EDT
    ],
)
def test_daily_summary_first_run_after_nine_new_york(now, last, expected):
    assert daily_summary_due(ts(now), last) == expected


def test_gap_seconds_since_the_last_successful_run():
    runs = [{"ts": 1000, "status": "ok"}, {"ts": 5000, "status": "ok"}]
    assert gap_seconds(5000 + 3 * H1, runs) == 3 * H1 and not gap_seconds(5000 + 3 * H1, runs) > GAP_WARN_S
    assert gap_seconds(5000 + 3 * H1 + 1, runs) > GAP_WARN_S
    assert gap_seconds(9999, []) is None


def test_entry_close_stop_and_failure_formats():
    t = _trade()
    text = entry_text(t, 0.5)
    for part in ("신규 진입", "W3 롱", "2024-02-07 21:00 UTC", "23,516.46", "23,022.26", "24,219.14", "24,751.39",
                 "blended R", "IMP W2", "q 0.600", "S_rev 0.662", "19계약"):
        assert part in text
    text = close_text(t, {"total": 37, "required": 50}, counted=True)
    assert "청산 · STOP" in text and "-123.40" in text and "R -1.08" in text and "37/50" in text
    assert "카운트 제외" in close_text(t, {"total": 36, "required": 50}, counted=False)
    dec = {"kind": "BREAKEVEN", "ts": ts("2024-02-08 03:00"), "price": 23_516.46, "contracts": 9, "levels": [23_022.26]}
    text = stop_move_text(dec, t)
    assert "손절 이동 (본전)" in text and "23,022.26 → 23,516.46" in text and "잔여 9계약" in text
    assert "(추적)" in stop_move_text({**dec, "kind": "TRAIL"}, t)
    text = failure_text("data", "coinbase down", "https://github.com/o/r/actions/runs/1")
    assert "단계 data" in text and "coinbase down" in text and "actions/runs/1" in text
    assert "로그" not in failure_text("data", "x", None)
    assert daily_text("2024-02-08", "p", "e", "c", ["s1", "s2"]).splitlines()[1:] == ["p", "e", "c", "s1", "s2"]
    assert position_line(None) == "포지션: 없음"


def _dec(kind: str, ts_: int, key: int, **kw) -> dict:
    return {"id": f"{kind}:{ts_}:{key}", "kind": kind, "ts": ts_, "trade_key": key, "price": 1.0, "contracts": 1,
            "levels": [0.5], **kw}


def test_decision_events_entry_stop_moves_and_closes():
    a = _trade(entry_ts=100 * H1, exit_ts=110 * H1, contracts=4, exit_reason="TP2")
    b = _trade(entry_ts=120 * H1, exit_ts=125 * H1, exit_reason="END")  # still open
    fresh = [
        _dec("SIGNAL", 99 * H1, 99 * H1),
        _dec("ENTRY", 100 * H1, 100 * H1),
        _dec("TRAIL", 103 * H1, 100 * H1),
        _dec("TP1", 105 * H1, 100 * H1),
        _dec("BREAKEVEN", 105 * H1, 100 * H1),
        _dec("TP2", 110 * H1, 100 * H1),
        _dec("ENTRY", 120 * H1, 120 * H1),
    ]
    count = {"holdout": 36, "required": 50}
    evs = decision_events(fresh, {a.entry_ts: a, b.entry_ts: b}, b.entry_ts, 50 * H1, count, [a], 0.5)
    assert [e["id"] for e in evs] == [
        f"ENTRY:{100 * H1}:{100 * H1}", f"TRAIL:{103 * H1}:{100 * H1}", f"BREAKEVEN:{105 * H1}:{100 * H1}",
        f"CLOSE:{110 * H1}:{100 * H1}", f"ENTRY:{120 * H1}:{120 * H1}",
    ]
    assert "37/50" in evs[3]["text"]  # 36 holdout + this paper trade


def test_queue_events_never_duplicates():
    led = new_ledger("f", "c" * 40, {
        "sha256": "a", "params": [2.0, 0.7, 0.5], "holdout_start": 1, "data": {"last_bar_ts": 2, "sha256": "b"},
        "first_run": {"criteria": {"n_trades": {"A": 36}}},
    })
    evs = _events(3)
    led.sent_event_ids.append(evs[0]["id"])
    assert queue_events(led, evs + [evs[1]]) == [evs[1]["id"], evs[2]["id"]]
    assert queue_events(led, evs) == []
    assert [e["id"] for e in led.pending_events] == [evs[1]["id"], evs[2]["id"]]
