from __future__ import annotations

import dataclasses
import json
import math
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from ewbot.backtest.holdout import data_fingerprint
from ewbot.live.audit import AuditError, audit
from ewbot.live.control import Command
from ewbot.live.exchange import (
    Balance,
    ExchangeError,
    KeyPermissions,
    Order,
    PlaceResult,
    Position,
    RateLimited,
    Unavailable,
)
from ewbot.live.feed import TickerFeed
from ewbot.live.health import Heartbeat
from ewbot.live.notify import LiveNotifier
from ewbot.live.preflight import BASELINE_FINGERPRINT
from ewbot.live.journal import Journal
from ewbot.live.reconcile import PlanContext, pending_position
from ewbot.live.runtime import audit_from_journal, status_lines
from ewbot.live.shadow import ShadowExchange
from ewbot.live.sim import SimExchange
from ewbot.live.stage import canary_gates, full_cap, on_incident, paused, restart_stint, resume, shadow_gates
from ewbot.live.target import compute_target
from ewbot.paper.decisions import known_at
from ewbot.paper.notify import SendError
from tests.live.harness import Harness
from tests.live.helpers import Clock, journal, position
from tests.paper.helpers import synth_frame, ts

SIGNAL = ts("2024-03-13 22:00")  # C_END long: entry 23:00, trail 03-14 07:00, TP1 + break-even 03-14 23:00
ENTRY_BAR = SIGNAL + 3600


@pytest.fixture
def h(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    yield hs
    hs.journal.close()


def entered(h: Harness) -> None:
    h.at(ENTRY_BAR + 30)
    assert h.ex.position() is not None


def noting(h: Harness, probe: Callable[[], Any]) -> list[tuple[str, str, Any]]:
    """From now on every alert is also recorded with probe() at the moment it was queued."""
    out: list[tuple[str, str, Any]] = []
    alerts = h.rt.d.notify

    def note(kind: str, text: str) -> None:
        out.append((kind, text, probe()))
        alerts(kind, text)

    h.rt.d.notify = note
    return out


def alerts_with_positions(h: Harness) -> list[tuple[str, str, Position | None]]:
    """From now on every alert is also recorded with the exchange position at the moment it was queued."""
    return noting(h, h.ex.position)


def paper_ledger(through: int, decisions: list[dict[str, Any]], runs: list[dict[str, Any]] | None = None) -> dict:
    """A schema-1 paper ledger whose one epoch (the baseline fingerprint) records known_at in (2024-01-10, through]."""
    return {"schema_version": 1, "baseline": {"fingerprint": BASELINE_FINGERPRINT, "commit": "abc1234"}, "holdout": {},
            "epochs": [{"id": 1, "fingerprint": BASELINE_FINGERPRINT, "started_ts": ts("2024-01-10 00:00"),
                        "through_ts": through}], "decisions": decisions, "runs": runs or []}


def test_startup_records_the_initial_stage_and_the_recovery(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR, stage=None)
    hs.price(26_700.0)
    out = hs.rt.startup()
    assert hs.journal.stage()[0] == "SHADOW" and hs.rt.stage == "SHADOW"
    assert out == {"accepted": 0, "abandoned": 0, "unknown": 0, "rebuilt": 0}
    assert [e["kind"] for e in hs.journal.events()][:2] == ["RECOVERY", "STARTUP"] and "STARTUP" in hs.alerts.kinds()
    assert hs.fetch.calls == 1  # the candle history is fetched before the first timed cycle
    hs.journal.close()


def test_the_cycle_enters_one_contract_in_canary_and_protects_it(h):
    h.at(ENTRY_BAR + 30)
    pos = h.ex.position()
    assert (pos.side, pos.contracts, pos.avg_entry) == (1, 1, 26_747.0)
    assert [(o.size, o.limit_price, o.stop_price) for o in h.ex.open_orders()] == [(1, 27_245.0, 26_585.0)]
    c = h.journal.cycles()[-1]
    assert (c["bar_ts"], c["status"]) == (SIGNAL, "OK") and c["finished_ts"] - (SIGNAL + 3600) <= 120
    ids = {d["id"] for d in h.journal.decisions()}
    assert f"SIGNAL:{SIGNAL}:{SIGNAL}" in ids and "ENTRY" in h.alerts.kinds()
    assert h.journal.get("target")["entry"]["signal_ts"] == SIGNAL
    h.at(ENTRY_BAR + 45)  # the same hour again: nothing is sent twice
    assert h.ex.calls.count("place_market") == 1


def test_a_late_bar_is_retried_then_the_decision_is_held(tmp_path):
    df = synth_frame()
    hs = Harness(tmp_path, ENTRY_BAR + 30, df=df[df["ts"] < SIGNAL])
    hs.price(26_700.0)
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    assert hs.rt.next_try == ENTRY_BAR + 45 and hs.journal.cycles()[-1]["status"] == "RUNNING"
    hs.at(ENTRY_BAR + 60)
    hs.at(ENTRY_BAR + 121)
    c = hs.journal.cycles()[-1]
    assert (c["bar_ts"], c["status"]) == (SIGNAL, "HOLD") and "DATA_LATE" in hs.alerts.kinds()
    assert hs.rt.target is None and hs.ex.position() is None and hs.rt.done_bar == SIGNAL
    hs.journal.close()


def test_a_paused_runtime_records_the_skipped_entry(h):
    h.commands.put(Command("/pause", ENTRY_BAR + 25))
    h.at(ENTRY_BAR + 30)
    assert h.ex.position() is None and paused(h.journal)["reason"] == "user /pause"
    skip = h.journal.events(kind="SKIP")[0]["data"]
    assert skip["signal_ts"] == SIGNAL and "일시 정지" in skip["reason"]
    guard = h.rt.guard.state()
    h.commands.put(Command("/resume", ENTRY_BAR + 39))
    h.at(ENTRY_BAR + 40)
    assert paused(h.journal) is None and h.rt.guard.state() == guard  # no breaker tripped: nothing re-based


def test_flatten_needs_a_confirmation_within_sixty_seconds(h):
    entered(h)
    assert h.rt.handle("/flatten_confirm", ENTRY_BAR + 40).startswith("확인할 /flatten 요청이 없음")
    h.rt.handle("/flatten", ENTRY_BAR + 50)
    assert h.rt.handle("/flatten_confirm", ENTRY_BAR + 111).startswith("확인할")  # 61 s later: expired
    h.rt.handle("/flatten", ENTRY_BAR + 120)
    assert h.rt.handle("/flatten_confirm", ENTRY_BAR + 130) == "전량 청산 완료, 일시 정지"
    assert h.ex.position() is None and h.ex.open_orders() == [] and paused(h.journal)["reason"] == "user /flatten"
    assert "단계 CANARY" in h.rt.handle("/status", ENTRY_BAR + 140)
    assert h.rt.handle("/help", ENTRY_BAR + 150).startswith("/status")


def test_a_failing_command_is_reported_and_the_loop_goes_on(h):
    entered(h)
    h.ex.fail_next("position", RateLimited("429"))
    h.commands.put(Command("/flatten", ENTRY_BAR + 45))
    h.commands.put(Command("/flatten_confirm", ENTRY_BAR + 48))
    h.at(ENTRY_BAR + 50)
    assert any("/flatten_confirm 실패" in t for k, t in h.alerts.items if k == "COMMAND")
    h.commands.put(Command("/flatten", ENTRY_BAR + 55))
    h.commands.put(Command("/flatten_confirm", ENTRY_BAR + 58))
    h.at(ENTRY_BAR + 60)
    assert h.ex.position() is None


def test_old_commands_are_ignored_and_the_confirmation_is_timed_on_the_messages(h):
    entered(h)
    h.commands.put(Command("/flatten", ENTRY_BAR - 570))  # sent while the runtime was down, read 10 minutes later
    h.commands.put(Command("/flatten_confirm", ENTRY_BAR + 30))
    h.at(ENTRY_BAR + 40)
    replies = [t for k, t in h.alerts.items if k == "COMMAND"]
    assert replies[0].startswith("오래된 명령 무시: /flatten (610초 전에 보냄")
    assert replies[1].startswith("확인할 /flatten 요청이 없음") and h.ex.position() is not None
    h.commands.put(Command("/flatten", ENTRY_BAR + 45))  # sent 61 s apart, read together
    h.commands.put(Command("/flatten_confirm", ENTRY_BAR + 106))
    h.at(ENTRY_BAR + 110)
    assert h.ex.position() is not None and paused(h.journal) is None
    h.commands.put(Command("/flatten", ENTRY_BAR + 120))  # sent 50 s apart, read 100 s after the /flatten
    h.commands.put(Command("/flatten_confirm", ENTRY_BAR + 170))
    h.at(ENTRY_BAR + 220)
    assert h.ex.position() is None and paused(h.journal)["reason"] == "user /flatten"
    assert [e["data"]["cmd"] for e in h.journal.events(kind="COMMAND_STALE")] == ["/flatten"]


def test_the_stop_gap_watchdog_closes_after_ten_seconds(h):
    entered(h)
    seen = alerts_with_positions(h)
    h.price(25_000.0)  # far below the stop's 5% limit (25_255.75): the bracket cannot fill
    assert h.ex.position() is not None
    for k in range(1, 12):
        h.at(ENTRY_BAR + 40 + k)
    assert h.ex.position() is None and [(k, p) for k, _, p in seen if k == "STOP_GAP"] == [("STOP_GAP", None)]  # closed first
    assert h.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "STOP_GAP"


def test_an_unprotected_position_through_its_stop_is_closed_at_once(h):
    h.ex.fail_next("place_bracket", ExchangeError("INVALID_STOP_PRICE"), times=5)  # the venue refuses the stop
    h.at(ENTRY_BAR + 30)
    assert h.ex.position() is not None and h.ex.open_orders() == []
    h.at(ENTRY_BAR + 40)  # the poll observes the gap in protection
    assert h.rt.executor.trade().unprotected_since == ENTRY_BAR + 40
    seen = alerts_with_positions(h)
    h.price(26_580.0)  # through the 26_585 stop, inside its 5% limit: a resting stop would have filled
    h.at(ENTRY_BAR + 51)  # the next REST price (no WebSocket feed here: polled every 10 s)
    assert h.ex.position() is None and [p for k, _, p in seen if k == "STOP_UNPROTECTED"] == [None]  # closed first
    assert h.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "STOP_UNPROTECTED"


def test_bracket_fills_are_recorded_for_the_cost_report(h):
    entered(h)
    h.price(26_570.0)  # through the 26_585 stop, inside its 5% limit: the bracket's stop fills at 26_570
    h.at(ENTRY_BAR + 45)
    assert h.ex.position() is None
    fill = h.journal.events(kind="BRACKET_FILL")[0]["data"]
    assert (fill["leg"], fill["side"], fill["contracts"], fill["price"], fill["stop"]) == ("tp1", -1, 1, 26_570.0, 26_585.0)
    c = h.rt.costs(ENTRY_BAR)
    assert c["fills"] == 2 and c["stop_slippage_bps"] == pytest.approx(15.0 / 26_585.0 * 1e4)
    assert c["slippage_bps"] == pytest.approx((26_747.0 - 26_744.5) / 26_744.5 * 1e4)


def test_the_account_breaker_flattens_and_pauses(h):
    entered(h)  # the day's first balance read
    h.at(ENTRY_BAR + 90)
    h.at(ENTRY_BAR + 150)  # three reads: the day's reference (their median)
    h.ex.cash -= 400.0  # equity 10_000 -> ~9_600: the daily -3% breaker
    seen = alerts_with_positions(h)
    h.at(ENTRY_BAR + 210)
    assert h.ex.position() is not None and paused(h.journal) is None  # one breaching balance read is not enough
    h.at(ENTRY_BAR + 270)  # the next minute's read breaches again
    assert h.ex.position() is None and paused(h.journal)["reason"] == "breaker DAILY_LOSS"
    assert [p for k, _, p in seen if k == "BREAKER"] == [None]  # flattened before the alert
    assert h.journal.events(kind="INCIDENT")[-1]["data"]["kind"] == "BREAKER"
    peak = h.rt.guard.state()["peak"]
    assert h.rt.handle("/resume", ENTRY_BAR + 280).endswith("(지금과 다음 두 번 조회의 중앙값): DAILY_LOSS")
    g = h.rt.guard.state()
    assert not g["daily_tripped"] and g["day_ref"] == h.ex.balance().equity and g["peak"] == peak  # the peak stays


def test_a_balance_that_is_not_a_finite_amount_never_trips_and_pauses_entries_after_three_reads(h):
    entered(h)
    guard = h.rt.guard.state()
    h.ex.balance = lambda: Balance(math.nan, None)  # a broken balance read
    h.at(ENTRY_BAR + 95)
    h.at(ENTRY_BAR + 155)
    assert h.alerts.kinds().count("BAD_EQUITY") == 1 and paused(h.journal) is None  # alerted once, not an incident yet
    h.at(ENTRY_BAR + 215)  # the third in a row: an incident that pauses entries - never a flatten
    assert paused(h.journal)["reason"] == "incident BAD_EQUITY"
    assert h.journal.events(kind="INCIDENT")[-1]["data"]["kind"] == "BAD_EQUITY"
    assert h.ex.position() is not None and len(h.ex.open_orders()) == 1  # still protected
    assert "BREAKER" not in h.alerts.kinds() and h.rt.guard.state() == guard
    h.at(ENTRY_BAR + 275)
    assert h.alerts.kinds().count("INCIDENT") == 1  # once per episode
    del h.ex.balance  # good reads again
    h.at(ENTRY_BAR + 335)
    assert h.alerts.kinds().count("BAD_EQUITY") == 1 and h.rt.guard.state()["equity"] > 0
    assert paused(h.journal) is not None  # resuming is the user's decision (/resume)


def test_contracts_added_by_hand_are_reduced_and_pause_canary(h):
    entered(h)
    h.ex.place_market("manual-1", 1, 1)  # a manual buy: 2 contracts, the CANARY trade holds 1
    h.at(ENTRY_BAR + 45)  # the next poll
    assert h.ex.position().contracts == 1 and h.rt.executor.trade().contracts == 1
    assert [e["data"]["reason"] for e in h.journal.events(kind="REDUCED")] == ["EXCESS"]
    assert "EXCESS_POSITION" in h.alerts.kinds() and paused(h.journal)["reason"] == "incident EXCESS_POSITION"


def test_a_position_flipped_or_opened_by_hand_is_closed_and_pauses_canary(h):
    entered(h)  # long 1
    h.ex.place_market("manual-1", -1, 2)  # sold 2 by hand: the trade's position is now short
    h.at(ENTRY_BAR + 45)
    assert h.ex.position() is None and paused(h.journal)["reason"] == "incident DIRECTION_MISMATCH"
    resume(h.journal, ENTRY_BAR + 50)
    h.ex.place_market("manual-2", -1, 1)  # a short the engine does not hold: closed, not adopted
    h.at(ENTRY_BAR + 110)
    assert h.ex.position() is None and paused(h.journal)["reason"] == "incident UNKNOWN_POSITION"
    assert [e["data"]["kind"] for e in h.journal.events(kind="INCIDENT")] == ["DIRECTION_MISMATCH", "UNKNOWN_POSITION"]


def test_an_unexplained_reduction_keeps_the_stop_and_protects_the_rest(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=10)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    assert hs.ex.position().contracts == 3
    hs.ex.close_position("manual-x", 1)  # a manual partial close: the TP1 leg still rests
    hs.at(ENTRY_BAR + 45)
    tr = hs.rt.executor.trade()
    assert not tr.tp1_filled and (tr.contracts, tr.stop_price) == (2, 26_585.0)
    assert sorted((o.size, o.limit_price, o.stop_price) for o in hs.ex.open_orders()) == [
        (1, 27_245.0, 26_585.0), (1, 27_610.0, 26_585.0)]
    assert "UNEXPLAINED_REDUCTION" in hs.alerts.kinds()
    hs.journal.close()


def test_margin_warns_then_reduces(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", equity=400.0)
    hs.price(26_744.5)
    hs.rt.startup()
    hs.rt.guard.params = dataclasses.replace(hs.rt.guard.params, daily_loss=1.0, total_dd=1.0)  # tested on their own
    hs.ex.place_market("ewb-e-1-L", 1, 3)  # 3 contracts: overnight margin 168.7 USD, 42% of 400
    hs.rt.executor.adopt(position(signal_ts=SIGNAL, stop=26_000.0, tp1=27_500.0, tp2=28_000.0), hs.rt._ctx(hs.clock()))
    hs.ex.cash = 200.0  # equity ~200: 84% -> warn
    hs.at(ENTRY_BAR + 100)
    assert "MARGIN" in hs.alerts.kinds() and hs.ex.position().contracts == 3
    hs.ex.cash = 170.0  # ~99% -> reduce to <= 70%
    seen = alerts_with_positions(hs)
    hs.at(ENTRY_BAR + 170)
    assert hs.ex.position().contracts == 2 and hs.rt.executor.trade().contracts == 2
    assert [e["data"]["reason"] for e in hs.journal.events(kind="REDUCED")] == ["MARGIN"]
    assert [p.contracts for k, t, p in seen if t.startswith("[증거금 축소]")] == [2]  # reduced before the alert
    hs.journal.close()


def test_a_feed_without_futures_prices_is_alerted_once_per_outage(h):
    feed = TickerFeed([h.ex.futures_id], ws_factory=lambda **kw: None, clock=h.clock)
    h.rt = h.make_runtime(feed=feed)
    h.rt.startup()
    beat = json.dumps({"channel": "heartbeats"})
    tick = json.dumps({"channel": "ticker", "events": [{"tickers": [{"product_id": h.ex.futures_id, "price": "26744.5"}]}]})
    feed.on_message(tick)  # the last futures price, at +30
    feed.failures = 3  # TickerFeed.run's count of failed connections so far
    h.ex.set_session("CLOSE")
    for k in range(8):  # a closed venue has no trades: eight minutes without a futures price are no outage
        feed.on_message(beat)
        h.at(ENTRY_BAR + 30 + 60 * k)
    assert "FEED" not in h.alerts.kinds()
    h.ex.set_session("OPEN")
    for k in range(6):  # open, heartbeats fresh, no futures price: counted from +510, alerted at +810
        if k == 1:
            feed.on_message(json.dumps({"type": "error", "message": "failure to subscribe"}))  # the ticker refused
        if k == 3:
            feed.failures += 2
        feed.on_message(beat)
        h.at(ENTRY_BAR + 510 + 60 * k)
    texts = [t for kind, t in h.alerts.items if kind == "FEED"]
    assert len(texts) == 1 and "REST" in texts[0] and "재연결 실패 2회" in texts[0]
    assert "server error: failure to subscribe" in texts[0]
    h.ex.set_session("CLOSE")  # the venue closes during the outage: still the same outage
    h.at(ENTRY_BAR + 870)
    h.at(ENTRY_BAR + 930)
    feed.on_message(tick)  # futures prices again
    h.at(ENTRY_BAR + 990)
    assert [t for kind, t in h.alerts.items if kind == "FEED"][-1] == "[가격 피드 복구] 웹소켓 선물 가격 수신 재개"
    assert h.alerts.kinds().count("FEED") == 2 and [e["kind"] for e in h.journal.events()
                                                    if e["kind"].startswith("FEED")] == ["FEED_DOWN", "FEED_UP"]
    assert h.journal.events(kind="FEED_UP")[-1]["data"]["down_s"] == 480.0  # since +510, the close included


class FakeControl:
    """The attributes of live.control.TelegramControl the runtime watches."""

    def __init__(self) -> None:
        self.alive, self.failures, self.conflicts, self.last_error, self.starts = True, 0, 0, None, 0
        self.late = 0.0

    def start(self, commands: Any) -> None:
        self.starts += 1
        self.alive = True

    def overdue(self, now: float) -> float:
        return self.late


class Pings:
    """healthchecks.io: GET pings answer `status`; POSTs (/fail) are recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.status = 200

    def get(self, url: str, timeout: float | None = None) -> Any:
        self.calls.append(("GET", url, None))
        return SimpleNamespace(status_code=self.status)

    def post(self, url: str, data: Any = None, timeout: float | None = None) -> Any:
        self.calls.append(("POST", url, data))
        return SimpleNamespace(status_code=200)


def test_the_telegram_thread_is_watched_and_reported_out_of_band(h):
    control, pings = FakeControl(), Pings()
    h.rt = h.make_runtime(control=control, heartbeat=Heartbeat("https://hc-ping.com/check", session=pings, clock=h.clock))
    t0 = ENTRY_BAR + 100

    def minute(k: int) -> list[str]:
        h.clock.now = t0 + 60 * k
        h.rt.minute(h.clock.now)
        return [x for kind, x in h.alerts.items if kind == "TELEGRAM"]

    assert minute(0) == [] and pings.calls == [("GET", "https://hc-ping.com/check", None)]
    control.alive = False  # the thread died
    assert minute(1)[-1].startswith("[텔레그램 명령 수신 이상] 명령 수신 스레드가 멈춰 다시 시작함") and control.starts == 1
    assert minute(2)[-1] == "[텔레그램 복구] 명령 수신 정상"  # it runs again
    control.failures, control.last_error = 5, "getUpdates failed: HTTP 401: Unauthorized (the bot token was refused)"
    for k in range(3, 15):  # twelve minutes of failed polls: one alert; at 10 minutes one /fail, then no success ping
        texts = minute(k)
    assert len(texts) == 3 and "getUpdates 5회 연속 실패" in texts[-1] and "HTTP 401" in texts[-1]
    posts = [c for c in pings.calls if c[0] == "POST"]
    assert len(posts) == 1 and posts[0][1] == "https://hc-ping.com/check/fail" and "HTTP 401" in posts[0][2].decode()
    assert pings.calls[-1][0] == "POST" and len(pings.calls) == 14  # pings at 0-12, the /fail at 13, nothing at 14
    control.failures = 0
    assert minute(15)[-1] == "[텔레그램 복구] 명령 수신 정상" and pings.calls[-1][0] == "GET"
    control.conflicts = 2  # another getUpdates poller on this bot token
    assert "HTTP 409" in minute(16)[-1]
    pings.status = 404  # the check's URL is wrong: three failed pings are alerted once (on Telegram)
    for k in range(17, 21):
        minute(k)
    assert [x for kind, x in h.alerts.items if kind == "HEARTBEAT"] == [
        "[외부 감시 핑 실패] healthchecks.io 핑이 3회 연속 실패 (HTTP 404): VPS가 멈춰도 경보가 오지 않을 수 있음"]
    pings.status = 200
    minute(21)
    assert h.alerts.items[-1] == ("HEARTBEAT", "[외부 감시 핑 복구] healthchecks.io 핑 정상")
    control.late = 130.0  # a request hangs: reported, not replaced (a second thread would poll beside it)
    assert minute(22)[-1].startswith("[텔레그램 명령 수신 이상] 명령 수신 스레드가 2분 넘게 응답 없음") and control.starts == 1
    assert [e["kind"] for e in h.journal.events() if e["kind"].startswith("TELEGRAM")] == [
        "TELEGRAM_DOWN", "TELEGRAM_UP", "TELEGRAM_DOWN", "TELEGRAM_UNUSABLE", "TELEGRAM_UP", "TELEGRAM_DOWN", "TELEGRAM_UP",
        "TELEGRAM_DOWN"]


def test_unusable_telegram_withholds_pings_at_most_30_minutes_and_fails_again_every_3_hours(h):
    control, pings = FakeControl(), Pings()
    h.rt = h.make_runtime(control=control, heartbeat=Heartbeat("https://hc-ping.com/check", session=pings, clock=h.clock))
    control.failures, control.last_error = 5, "getUpdates failed: ConnectionError"  # Telegram out for four hours
    t0 = ENTRY_BAR + 100
    calls: dict[int, str] = {}
    for k in range(240):
        h.clock.now = t0 + 60 * k
        before = len(pings.calls)
        h.rt.minute(h.clock.now)
        calls[k] = "".join(c[0][0] for c in pings.calls[before:])  # G: a ping, P: a /fail
    assert [k for k, c in calls.items() if "P" in c] == [10, 190]  # after 10 minutes, then every 3 hours
    assert [k for k, c in calls.items() if "G" not in c] == list(range(10, 40)) + list(range(190, 220))  # 30 min each
    assert len([x for kind, x in h.alerts.items if kind == "TELEGRAM"]) == 1  # one episode


def test_a_notifier_that_cannot_deliver_counts_as_telegram_unusable(h):
    pings = Pings()
    h.rt = h.make_runtime(heartbeat=Heartbeat("https://hc-ping.com/check", session=pings, clock=h.clock))
    state: dict[str, Exception | None] = {"error": ConnectionError("telegram down")}

    def sender(text: str) -> None:
        if state["error"] is not None:
            raise state["error"]

    t0 = ENTRY_BAR + 100
    h.clock.now = t0 - 60
    h.rt.minute(h.clock.now)  # the day's summary and the first ping, before the notifier under test
    n = LiveNotifier(sender, clock=h.clock)  # its worker's steps are taken here (pump)
    h.rt.d.notify = n

    def minute(k: int, pump: bool = True) -> str:
        h.clock.now = t0 + 60 * k
        if pump:
            n.pump()
        before = len(pings.calls)
        h.rt.minute(h.clock.now)
        return "".join(c[0][0] for c in pings.calls[before:])

    h.clock.now = t0
    n("X", "queued while Telegram is down")
    assert [minute(k) for k in range(12)] == ["G"] * 10 + ["P", ""]  # failing for 10 minutes: /fail, pings withheld
    state["error"] = None  # Telegram is back: the message goes out, and so do the pings
    assert minute(12) == "G" and n.pending == 0
    n("Y", "first alert after the outage")  # waiting, not tried yet: no /fail
    assert minute(13, pump=False) == "G"
    assert [minute(k, pump=False) for k in range(14, 24)] == ["G"] * 8 + ["P", ""]  # 10 min waiting, no attempt: stuck
    assert minute(24) == "G" and n.pending == 0  # the sender runs again
    state["error"] = SendError('telegram HTTP 403: {"ok":false,"description":"Forbidden: bot was blocked by the user"}')
    for i in range(3):
        n("X", f"refused {i}")
        n.pump()
    assert minute(25, pump=False) == "P"  # three refused in a row: unusable at once
    reasons = [e["data"]["reasons"][0] for e in h.journal.events(kind="TELEGRAM_UNUSABLE")]
    assert "알림 전송이 10분째 실패" in reasons[0] and "멈춤" in reasons[1] and "3건을 연속 거부" in reasons[2]


def test_a_broken_telegram_check_never_skips_the_minute_and_loop_errors_are_redacted(h, capsys):
    class Broken(FakeControl):
        @property  # type: ignore[override]
        def alive(self) -> bool:
            raise RuntimeError("control state unreadable")

        @alive.setter
        def alive(self, value: bool) -> None:
            pass

    h.rt = h.make_runtime(control=Broken(), redact=lambda text: text.replace("SECRET", "<secret>"))
    h.ex.cash -= 100.0
    h.rt.minute(ENTRY_BAR + 100)
    assert h.journal.events(kind="LOOP_ERROR")[-1]["data"] == {"section": "telegram", "error": "RuntimeError"}
    assert h.rt.guard.state()["equity"] == h.ex.balance().equity  # the breakers' read still ran
    capsys.readouterr()

    def failing_poll(now: float) -> None:
        raise RuntimeError("https://api.telegram.org/botSECRET/getUpdates refused")

    h.rt.poll = failing_poll  # type: ignore[method-assign]
    h.rt.last_poll = None
    h.rt.tick()
    err = capsys.readouterr().err
    assert "Traceback" in err and "bot<secret>" in err and "SECRET" not in err
    assert h.journal.events(kind="LOOP_ERROR")[-1]["data"]["error"] == (
        "RuntimeError: https://api.telegram.org/bot<secret>/getUpdates refused")
    assert "SECRET" not in [t for k, t in h.alerts.items if k == "LOOP_ERROR"][-1]


def test_a_failed_read_at_the_overnight_margin_check_is_retried_not_lost(h):
    entered(h)
    t = ts("2024-03-14 19:31")  # 15:31 New York (EDT)
    h.ex.fail_next("position", RateLimited("429"), times=2)  # the margin check's position read, then the report's
    h.rt.minute(t)
    assert [x for k, x in h.alerts.items if k == "MARGIN_1530"] == [
        "[15:30 증거금 점검] 조회 실패: 429 (16시 전까지 매분 다시 시도)"]
    h.rt.minute(t + 60)  # still before 16:00: read again, reported
    h.rt.minute(t + 120)
    texts = [x for k, x in h.alerts.items if k == "MARGIN_1530"]
    assert len(texts) == 2 and texts[1].startswith("[15:30 증거금 점검] 16시 오버나이트 기준 예상")
    h.ex.fail_next("balance", RateLimited("429"), times=2)  # the next day: the breaker's balance read, the report's
    h.rt.minute(t + 86_400)
    h.rt.minute(t + 86_460)
    texts = [x for k, x in h.alerts.items if k == "MARGIN_1530"]
    assert texts[2:] == ["[15:30 증거금 점검] 조회 실패: 429 (16시 전까지 매분 다시 시도)", texts[1]]


def test_margin_is_never_acted_on_without_a_futures_price(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", equity=400.0)
    hs.price(26_744.5)
    hs.rt.startup()
    hs.rt.guard.params = dataclasses.replace(hs.rt.guard.params, daily_loss=1.0, total_dd=1.0)  # tested on their own
    hs.ex.place_market("ewb-e-1-L", 1, 3)
    hs.rt.executor.adopt(position(signal_ts=SIGNAL, stop=26_000.0, tp1=27_500.0, tp2=28_000.0), hs.rt._ctx(hs.clock()))
    hs.ex.cash = 170.0  # ~99% of the account at the overnight rate: a reduce, if there were a price
    hs.rt.prices.futures_price = lambda: None  # neither a WebSocket nor a REST price within 60 s
    hs.at(ENTRY_BAR + 100)
    hs.at(ENTRY_BAR + 160)
    assert hs.ex.position().contracts == 3 and not hs.journal.events(kind="REDUCED")  # never on a stand-in price
    assert [t for k, t in hs.alerts.items if k == "MARGIN"] == [
        "[증거금 점검: 가격 없음] 선물 가격을 읽지 못해 증거금 점검을 건너뜀 (가격이 돌아오면 다시 점검)"]
    hs.ex.position = lambda: Position(1, 3, 0.0)  # an entry price the adapter read as 0: reports must not use it
    hs.rt._overnight_report(ENTRY_BAR + 170)
    hs.rt.daily(ENTRY_BAR + 175)
    assert hs.journal.events(kind="MARGIN_1530")[-1]["data"]["ratio"] is None
    assert "오버나이트 증거금: 가격 또는 잔고 없음" in [t for k, t in hs.alerts.items if k == "DAILY"][-1]
    del hs.ex.position
    del hs.rt.prices.futures_price  # prices are back
    hs.at(ENTRY_BAR + 220)
    assert hs.ex.position().contracts == 2 and [e["data"]["reason"] for e in hs.journal.events(kind="REDUCED")] == ["MARGIN"]
    hs.journal.close()


def test_a_stage_change_restarts_the_runtime_once_flat(h):
    entered(h)
    h.journal.set_stage("SHADOW", "demotion", ENTRY_BAR + 35)
    h.at(ENTRY_BAR + 100)
    assert h.rt.exit_code is None  # a position is open
    h.rt.handle("/flatten", ENTRY_BAR + 110)
    h.rt.handle("/flatten_confirm", ENTRY_BAR + 111)
    h.at(ENTRY_BAR + 170)
    assert h.rt.exit_code == 0 and "STAGE" in h.alerts.kinds()


def test_daily_summary_audit_and_status(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    led = paper_ledger(SIGNAL, [d for d in hs.journal.decisions() if d["ts"] < SIGNAL])
    hs.rt.d.paper_ledger = lambda: led
    res = hs.rt.run_audit(ENTRY_BAR + 40)
    assert res.ok and res.compared > 0 and audit_from_journal(hs.journal).ok
    bad = dict(led, decisions=led["decisions"][:-1])  # the paper ledger misses a decision the live runtime made
    hs.rt.d.paper_ledger = lambda: bad
    seen = noting(hs, lambda: paused(hs.journal))
    assert not hs.rt.run_audit(ENTRY_BAR + 50).ok
    assert "AUDIT_PENDING" in hs.alerts.kinds() and paused(hs.journal) is None  # a lag might explain it: pending
    hs.rt.run_audit(ENTRY_BAR + 3650)  # the next audit still finds it: a major incident
    assert "AUDIT_MISMATCH" in hs.alerts.kinds() and paused(hs.journal)["reason"] == "incident AUDIT_MISMATCH"
    assert [p is not None for k, _, p in seen if k == "AUDIT_MISMATCH"] == [True]  # paused before the alert
    hs.rt.daily(ENTRY_BAR + 60)
    daily = [t for k, t in hs.alerts.items if k == "DAILY"][-1]
    assert "일일 요약" in daily and "포지션: W3" not in daily and "체결 24시간: 1건" in daily
    assert hs.journal.events(kind="COSTS")[-1]["data"]["fills"] == 1
    lines = status_lines(hs.journal, ENTRY_BAR + 70)
    assert lines[0].startswith("단계 CANARY") and lines[1].startswith("포지션: C_END 롱 1계약")
    assert any(x.startswith("모의매매 대조: 비교 ") and "불일치 1건" in x for x in lines)
    hs.journal.close()


def test_the_audit_stops_at_the_last_decided_bar_and_never_passes_on_a_failed_one(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)  # the cycle of bar SIGNAL
    hs.fetch.fail = 10
    hs.at(ENTRY_BAR + 3630)  # the cycle of bar ENTRY_BAR fails (no candles): not decided yet
    assert [c["status"] for c in hs.journal.cycles()][-2:] == ["OK", "ERROR"]
    target = compute_target(hs.lock, hs.df[hs.df["ts"] <= ENTRY_BAR], ENTRY_BAR)  # the paper runtime has the bar
    led = paper_ledger(ENTRY_BAR, [d for d in target.decisions if known_at(d) <= ENTRY_BAR])
    lagging = audit(hs.journal.decisions(), led, now=ENTRY_BAR + 3640)  # without the live bound
    assert [m["type"] for m in lagging.mismatches] == ["MISSING_IN_LIVE"]
    hs.rt.d.paper_ledger = lambda: led
    res = hs.rt.run_audit(ENTRY_BAR + 3650)
    assert res.ok and res.window[1] == SIGNAL and "AUDIT_PENDING" not in hs.alerts.kinds()
    # a bar Coinbase revised between the two fetches is alerted, never an incident; a different decision on the
    # same candles is a major incident at once (no lag explains it)
    d = [x for x in hs.journal.decisions() if known_at(x) <= SIGNAL][-1]
    before = data_fingerprint(hs.rt.frame, 0, d["ts"] - 3600)
    runs = [{"id": "r1", "status": "ok", "last_bar_ts": d["ts"] - 3600, "data_sha256": before}]
    revised = {k: v for k, v in d.items() if k != "recorded_ts"} | {"price": d["price"] + 5.0, "data_sha256": "other"}
    hs.rt.d.paper_ledger = lambda: paper_ledger(SIGNAL, [x for x in led["decisions"] if x["id"] != d["id"]] + [revised],
                                                runs)
    res = hs.rt.run_audit(ENTRY_BAR + 3660)
    assert [(m["type"], m["cause"]) for m in res.mismatches] == [("DIFFERENT", "DATA_REVISION")]
    assert "AUDIT_REVISION" in hs.alerts.kinds() and paused(hs.journal) is None
    assert res.ok and audit_from_journal(hs.journal).ok  # explained: it fails no audit gate
    assert any("불일치 0건 (봉 수정 1건 별도)" in x for x in status_lines(hs.journal, ENTRY_BAR + 3660))
    same = revised | {"data_sha256": data_fingerprint(hs.rt.frame, 0, d["ts"])}
    hs.rt.d.paper_ledger = lambda: paper_ledger(SIGNAL, [x for x in led["decisions"] if x["id"] != d["id"]] + [same],
                                                runs)
    hs.rt.run_audit(ENTRY_BAR + 3670)
    assert paused(hs.journal)["reason"] == "incident AUDIT_MISMATCH"
    # a failed audit is stored as failed (the gates never pass on the last good one) and alerted once per episode
    hs.rt.d.paper_ledger = lambda: {"schema_version": 2}
    assert hs.rt.run_audit(ENTRY_BAR + 3680).error.startswith("paper ledger invalid")

    def down() -> dict[str, Any]:
        raise AuditError("paper ledger fetch failed: HTTP 502")

    hs.rt.d.paper_ledger = down
    stored = hs.rt.run_audit(ENTRY_BAR + 3690)
    assert audit_from_journal(hs.journal) == stored and not stored.ok
    assert any(x.startswith("모의매매 대조: 실패 - paper ledger fetch failed: HTTP 502")
               for x in status_lines(hs.journal, ENTRY_BAR + 3700))
    hs.rt.d.paper_ledger = lambda: led
    hs.rt.run_audit(ENTRY_BAR + 3700)
    assert [t.split("]")[0] for k, t in hs.alerts.items if k == "AUDIT"] == ["[모의매매 대조 실패", "[모의매매 대조 복구"]
    hs.journal.close()


def test_a_demotion_applies_to_the_next_entry_while_the_process_still_runs(tmp_path):
    sizes = {}
    for demoted in (False, True):
        hs = Harness(tmp_path / str(demoted), ENTRY_BAR + 30, stage="FULL", stage_days=40)  # the cap is released
        hs.price(float(hs.bar(ENTRY_BAR)["open"]))
        hs.rt.startup()
        if demoted:  # a major incident in FULL, then the user's /resume: this process still runs FULL
            assert on_incident(hs.journal, "UNPROTECTED_60S", ENTRY_BAR + 1) == ["paused", "demoted to CANARY"]
            resume(hs.journal, ENTRY_BAR + 2)
            assert hs.rt.stage == "FULL" and hs.rt._ctx(ENTRY_BAR + 3).max_contracts == 1
        hs.at(ENTRY_BAR + 30)
        sizes[demoted] = hs.ex.position().contracts
        hs.journal.close()
    assert sizes[False] > 3 and sizes[True] == 1  # CANARY's one contract at once, not FULL's uncapped size


def test_an_order_failure_is_a_canary_failure_only_when_not_resolved_in_time(h):
    trail = ts("2024-03-14 07:00")  # the engine trails the long's stop at this close
    h.walk(ENTRY_BAR, trail + 3600)
    h.price(float(h.bar(trail + 3600)["open"]))
    h.ex.fail_next("place_bracket", RateLimited("429"))  # the stop move's replacement: one 429
    t0 = trail + 3600 + 30
    h.at(t0)
    h.at(t0 + 10)  # the next poll replaces it
    assert not h.journal.events(kind="ORDER_RESOLVED")  # the pass that placed it planned it: no proof yet
    h.at(t0 + 20)  # this pass finds the legs as wanted (nothing planned for them): resolved
    assert len(h.journal.events(kind="BRACKET_FAILED", since=t0)) == 1 and h.journal.events(kind="STOP_MOVED", since=t0)
    assert [e["data"]["kind"] for e in h.journal.events(kind="ORDER_RESOLVED")] == ["BRACKET_FAILED"]
    assert paused(h.journal) is None and not h.journal.events(kind="ORDER_UNRESOLVED")
    assert {g.name: g.met for g in canary_gates(h.journal, t0 + 20, None)}["② 손절 이동과 청산이 목표와 일치"]
    seen = noting(h, lambda: paused(h.journal))
    h.ex.fail_next("place_bracket", RateLimited("429"), times=50)
    h.ex.cancel([o.order_id for o in h.ex.open_orders()])  # the bracket cancelled by hand, and no new one goes out
    for k in range(3, 7):
        h.at(t0 + 10 * k)
        assert paused(h.journal) is None  # within 30 s of the failure: still being retried
    h.at(t0 + 70)
    h.at(t0 + 80)  # still failing: the same episode, recorded once
    assert [e["data"]["kind"] for e in h.journal.events(kind="ORDER_UNRESOLVED")] == ["BRACKET_FAILED"]
    assert paused(h.journal)["reason"] == "incident ORDER_UNRESOLVED"
    assert [p is not None for k, _, p in seen if k == "ORDER_UNRESOLVED"] == [True]  # paused before the alert
    assert not {g.name: g.met for g in canary_gates(h.journal, t0 + 80, None)}["② 손절 이동과 청산이 목표와 일치"]


def test_a_stop_move_placed_within_30_s_is_resolved_by_the_pass_that_confirms_it_whenever_it_runs(h):
    trail = ts("2024-03-14 07:00")
    h.walk(ENTRY_BAR, trail + 3600)
    h.price(float(h.bar(trail + 3600)["open"]))
    h.ex.fail_next("place_bracket", RateLimited("429"), times=2)  # the move refused twice, placed at the third pass
    t0 = trail + 3600 + 30
    for dt in (0, 10, 20, 32):  # polls 10 s apart plus the tick and the passes' own time: the fourth after t0 + 30
        h.at(t0 + dt)
    assert len(h.journal.events(kind="BRACKET_FAILED", since=t0)) == 2 and h.journal.events(kind="STOP_MOVED", since=t0)
    assert [e["data"]["kind"] for e in h.journal.events(kind="ORDER_RESOLVED")] == ["BRACKET_FAILED"] * 2
    assert not h.journal.events(kind="ORDER_UNRESOLVED") and paused(h.journal) is None


def test_a_failure_seen_in_a_strict_pass_is_not_resolved_by_passes_without_quotes_alone(h):
    trail = ts("2024-03-14 07:00")
    h.walk(ENTRY_BAR, trail + 3600)
    h.price(float(h.bar(trail + 3600)["open"]))
    h.ex.fail_next("place_bracket", RateLimited("429"), times=50)  # the move keeps failing
    t0 = trail + 3600 + 30
    planned = h.rt._ctx
    no_quotes = {t0 + 10, t0 + 30}  # every other pass reads no quotes: it cannot judge the legs, the old stop rests

    def ctx(now: float) -> PlanContext:
        return dataclasses.replace(planned(now), ratio=None) if now in no_quotes else planned(now)

    h.rt._ctx = ctx
    for dt in (0, 10, 20, 30):
        h.at(t0 + dt)
        assert paused(h.journal) is None
    h.at(t0 + 40)  # the strict passes keep failing: the protection seen in between proves nothing about the move
    assert [e["data"]["kind"] for e in h.journal.events(kind="ORDER_UNRESOLVED")] == ["BRACKET_FAILED"]
    assert paused(h.journal)["reason"] == "incident ORDER_UNRESOLVED"


def test_a_stop_move_refused_while_the_session_is_closed_is_no_canary_failure(h):
    trail = ts("2024-03-14 07:00")
    h.walk(ENTRY_BAR, trail + 3600)
    h.price(float(h.bar(trail + 3600)["open"]))
    h.ex.set_session("CLOSE")
    placed = h.ex.place_bracket

    def entry_disabled(cid, *args, **kw):  # a venue that refuses order entry while the session is closed
        if not h.ex.session().is_open:
            return PlaceResult(cid, None, False, "ORDER_ENTRY_DISABLED")
        return placed(cid, *args, **kw)

    h.ex.place_bracket = entry_disabled
    t0 = trail + 3600 + 30
    for k in range(9):  # 80 s of refused moves while closed; the old leg keeps protecting
        h.at(t0 + 10 * k)
    assert len(h.journal.events(kind="BRACKET_FAILED", since=t0)) >= 5
    assert not h.journal.events(kind="ORDER_UNRESOLVED") and paused(h.journal) is None
    h.ex.set_session("OPEN")
    for k in range(9, 13):
        h.at(t0 + 10 * k)
    assert h.journal.events(kind="STOP_MOVED", since=t0) and not h.journal.events(kind="ORDER_UNRESOLVED")


def failure_pass(hs: Harness, now: float, *failures: tuple[str, dict], quotes: bool = True,
                 guarded: tuple[int, ...] = (), protecting: tuple[int, ...] = ()) -> None:
    """One completed pass after `failures`: with a target and the session OPEN, quotes or not, the trades it found
    protected (`guarded`) and those it planned a Protect for (`protecting`)."""
    for kind, data in failures:
        hs.journal.add_event(now, kind, data)
    hs.rt.target = SimpleNamespace()
    ctx = PlanContext(now=now, ratio=1.0 if quotes else None, tick=5.0, session_open=True)
    hs.rt._order_failures(now, ctx, set(guarded), set(protecting))


def test_a_failure_awaiting_confirmation_does_not_hide_a_later_one(tmp_path):
    s = ENTRY_BAR + 30
    for stage in ("CANARY", "FULL"):
        hs = Harness(tmp_path / stage, s, stage=stage, stage_days=1.0)
        failure_pass(hs, s, ("BRACKET_FAILED", {"trade_id": 7, "leg": "tp1"}))
        failure_pass(hs, s + 10, protecting=(7,))  # placed in time: awaiting its confirmation
        # from now on no pass reads quotes (the old stop rests): the fix is neither confirmed nor escalated
        failure_pass(hs, s + 20, ("CLOSE_FAILED", {"trade_id": 7, "reason": "TIME"}), quotes=False, guarded=(7,))
        failure_pass(hs, s + 30, quotes=False, guarded=(7,))  # the close not done by the next pass
        assert [e["data"]["kind"] for e in hs.journal.events(kind="ORDER_UNRESOLVED")] == ["CLOSE_FAILED"]
        assert hs.journal.get("order_failures")["episodes"] == []  # the awaiting fix keeps no episode alive
        resume(hs.journal, s + 60)
        for k in range(30):  # a day without quotes
            failure_pass(hs, s + 70 + k * 3000, quotes=False, guarded=(7,))
        later = s + 86_400 + 10
        failure_pass(hs, later, ("CLOSE_FAILED", {"trade_id": 7, "reason": "STOP_GAP"}), quotes=False, guarded=(7,))
        failure_pass(hs, later + 10, quotes=False, guarded=(7,))  # the same trade, a new episode, a new incident
        assert [e["data"]["kind"] for e in hs.journal.events(kind="ORDER_UNRESOLVED")] == ["CLOSE_FAILED"] * 2
        if stage == "CANARY":
            assert paused(hs.journal)["reason"] == "incident ORDER_UNRESOLVED"  # paused again after the /resume
        else:
            assert full_cap(hs.journal, s + 31 * 86_400) == 3  # the 30 clean days start again from the new one
        hs.journal.close()


def test_legs_placed_again_at_every_pass_and_never_confirmed_are_unresolved(tmp_path):
    s = ENTRY_BAR + 30
    bracket = ("BRACKET_FAILED", {"trade_id": 7, "leg": "tp1"})
    hs = Harness(tmp_path / "replaced", s, stage="CANARY", stage_days=1.0)
    failure_pass(hs, s, bracket)
    failure_pass(hs, s + 10, protecting=(7,))  # placed in time
    for k in range(1, 10):  # every later strict pass plans the legs again: never as wanted
        failure_pass(hs, s + 10 + 60 * k, protecting=(7,))
        assert not hs.journal.events(kind="ORDER_UNRESOLVED")  # within 10 minutes of the fix
    failure_pass(hs, s + 610, protecting=(7,))  # 2 passes and 10 minutes after the fix, still not confirmed
    assert [e["data"]["kind"] for e in hs.journal.events(kind="ORDER_UNRESOLVED")] == ["BRACKET_FAILED"]
    assert paused(hs.journal)["reason"] == "incident ORDER_UNRESOLVED"
    hs.journal.close()
    hs = Harness(tmp_path / "outage", s, stage="CANARY", stage_days=1.0)
    failure_pass(hs, s, bracket)
    failure_pass(hs, s + 10, protecting=(7,))
    for k in range(1, 60):  # an hour without quotes: no strict pass, so it waits without escalating
        failure_pass(hs, s + 10 + 60 * k, quotes=False, guarded=(7,))
    assert not hs.journal.events(kind="ORDER_UNRESOLVED") and paused(hs.journal) is None
    failure_pass(hs, s + 3700)  # the first strict pass finds the legs as wanted: confirmed
    assert [e["data"]["kind"] for e in hs.journal.events(kind="ORDER_RESOLVED")] == ["BRACKET_FAILED"]
    hs.journal.close()


def test_a_new_unresolved_failure_after_an_episode_is_a_new_incident(tmp_path):
    t0 = ENTRY_BAR + 30
    for stage in ("CANARY", "FULL"):
        hs = Harness(tmp_path / stage, t0, stage=stage, stage_days=1.0)
        failure_pass(hs, t0, ("BRACKET_FAILED", {"trade_id": 7, "leg": "tp1"}))
        failure_pass(hs, t0 + 40)  # not resolved within 30 s
        resume(hs.journal, t0 + 60)
        for k in range(1, 50):  # quiet passes for three days: the episode is over
            failure_pass(hs, t0 + 60 + k * 5000)
        later = t0 + 3 * 86_400
        failure_pass(hs, later, ("CLOSE_FAILED", {"trade_id": 7, "reason": "TIME"}))  # the same trade, a new failure
        failure_pass(hs, later + 10, ("CLOSE_FAILED", {"trade_id": 7, "reason": "TIME"}))
        assert [(e["ts"], e["data"]["kind"]) for e in hs.journal.events(kind="ORDER_UNRESOLVED")] == [
            (t0 + 40, "BRACKET_FAILED"), (later + 10, "CLOSE_FAILED")]
        if stage == "CANARY":
            assert paused(hs.journal)["reason"] == "incident ORDER_UNRESOLVED"  # paused again
        else:
            assert full_cap(hs.journal, t0 + 31 * 86_400) == 3  # the 30 clean days start again from the new one
        hs.journal.close()
    # no trade open: each failure is an episode of its own, and a restarted stint starts afresh
    hs = Harness(tmp_path / "flat", t0, stage="CANARY", stage_days=1.0)
    failure_pass(hs, t0, ("CLOSE_UNCONFIRMED", {"trade_id": 0, "reason": "UNKNOWN_POSITION"}))
    failure_pass(hs, t0 + 10)
    restart_stint(hs.journal, "CANARY", t0 + 100, confirm=True, flat=True)
    resume(hs.journal, t0 + 101)
    t = t0 + 20 * 86_400
    failure_pass(hs, t, ("ACTION_ERROR", {"action": "Close", "error": "read timeout"}))
    failure_pass(hs, t + 10)
    assert [e["ts"] for e in hs.journal.events(kind="ORDER_UNRESOLVED")] == [t0 + 10, t + 10]
    assert paused(hs.journal)["reason"] == "incident ORDER_UNRESOLVED"
    assert not {g.name: g.met for g in canary_gates(hs.journal, t + 20, None)}["② 손절 이동과 청산이 목표와 일치"]
    hs.journal.close()


def test_a_shadow_runtime_restarted_mid_hour_keeps_its_cycle_and_restart_gates(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage=None)  # SHADOW starts with this runtime
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.walk(ENTRY_BAR, ENTRY_BAR + 3 * 3600)
    decided = [(c["bar_ts"], c["status"], c["finished_ts"]) for c in hs.journal.cycles()]
    hs.journal.close()  # the restart of SHADOW gate 4, in the middle of the hour
    hs.journal = Journal(tmp_path / "journal.sqlite")
    hs.clock.now = ENTRY_BAR + 2 * 3600 + 2400
    hs.rt = hs.make_runtime()
    hs.rt.startup()
    assert [(c["bar_ts"], c["status"], c["finished_ts"]) for c in hs.journal.cycles()] == decided  # never rewritten
    assert hs.rt.done_bar == ENTRY_BAR + 3600 and hs.rt.target is not None  # this hour's target, decided again
    hs.walk(ENTRY_BAR + 3 * 3600, ENTRY_BAR + 4 * 3600)
    gates = {g.name: g for g in shadow_gates(hs.journal, ENTRY_BAR + 3 * 3600 + 200, None)}
    for name in ("② 모든 사이클이 마감 후 2분 안에 끝남", "④ 장애·재시작 시험 통과"):
        assert gates[name].met, (name, gates[name].detail)
    hs.journal.close()


def test_a_bracket_failure_in_an_hour_without_a_target_is_resolved_by_the_protection_seen(h):
    entered(h)  # long 1 with its bracket
    h.fetch.fail = 10
    h.at(ENTRY_BAR + 3630)  # the next cycle has no candles: no target this hour
    assert h.rt.target is None
    h.ex.fail_next("place_bracket", RateLimited("429"))
    h.ex.cancel([o.order_id for o in h.ex.open_orders()])  # the bracket gone
    h.at(ENTRY_BAR + 3640)  # placing it again at the stop in force fails once
    h.at(ENTRY_BAR + 3650)  # placed: the planner cannot judge the legs without a target, but
    h.at(ENTRY_BAR + 3660)  # this pass sees the position covered by the resting stop leg
    for k in range(3):
        h.at(ENTRY_BAR + 3670 + 10 * k)
    assert len(h.journal.events(kind="BRACKET_FAILED")) == 1 and h.journal.events(kind="PROTECTED")
    assert [e["data"]["kind"] for e in h.journal.events(kind="ORDER_RESOLVED")] == ["BRACKET_FAILED"]
    assert paused(h.journal) is None and not h.journal.events(kind="ORDER_UNRESOLVED")


def test_a_manual_intervention_is_one_incident_per_episode(h):
    h.fetch.fail = 100  # no candles, no target: the foreign position waits for one, alerted at every poll
    h.ex.place_market("manual-1", 1, 1)
    for k in range(4):
        h.at(ENTRY_BAR + 30 + 60 * k)
    assert [e["data"]["kind"] for e in h.journal.events(kind="INCIDENT")] == ["UNKNOWN_POSITION"]
    assert paused(h.journal)["reason"] == "incident UNKNOWN_POSITION"
    h.ex.close_position("manual-x")
    h.at(ENTRY_BAR + 270)  # gone: the episode is over
    h.ex.place_market("manual-2", 1, 1)
    h.at(ENTRY_BAR + 330)  # another one: a new episode
    assert [e["data"]["kind"] for e in h.journal.events(kind="INCIDENT")] == ["UNKNOWN_POSITION"] * 2


def test_expiry_reminders_and_a_transfer_key_incident(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, permissions=KeyPermissions(True, True, True))
    hs.price(26_700.0)
    hs.rt.startup()
    hs.rt.d.expiry = {"coinbase": "2024-04-01", "github": "2025-01-01"}
    seen = noting(hs, lambda: paused(hs.journal))
    hs.rt.daily(ENTRY_BAR + 60)
    texts = [t for k, t in hs.alerts.items if k == "EXPIRY"]
    assert len(texts) == 1 and "coinbase" in texts[0]
    assert [p is not None for k, _, p in seen if k == "TRANSFER_KEY"] == [True]  # paused before the alert
    hs.journal.close()


def test_the_shadow_exchange_previews_every_order_and_trades_virtually(tmp_path):
    clock = Clock(1_000.0)
    real = SimExchange(clock=clock)
    real.set_prices(60_000.0, 60_010.0)
    jr = journal(tmp_path)
    alerts = []
    sh = ShadowExchange(real, jr, clock=clock, alert=lambda k, t: alerts.append(k))
    with pytest.raises(ValueError):
        sh.fail_next("quote", RateLimited("429"))  # a real read: inject it on `real`
    assert sh.place_market("ewb-e-1-L", 1, 1).ok
    assert sh.place_bracket("ewb-b-1-tp1-1", -1, 1, 61_000.0, 59_000.0).ok
    real.preview_errors = ("PREVIEW_INSUFFICIENT_FUND",)
    real.set_prices(60_500.0, 60_510.0)
    assert sh.close_position("ewb-x-1-1").ok
    previews = [e["data"] for e in jr.events(kind="PREVIEW")]
    assert [(p["kind"], p["ok"]) for p in previews] == [("MARKET", True), ("BRACKET", True), ("CLOSE", False)]
    assert alerts == ["PREVIEW_FAILED"] and real.orders == {} and real.position() is None
    assert sh.fills[0]["price"] == 60_002.5 and sh.fills[-1]["price"] == 60_497.5  # real bid / ask
    assert sh.balance().equity == real.balance().equity and sh.session().is_open
    jr.close()


def test_shadow_runs_the_whole_loop_without_a_real_order(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="SHADOW")
    real = hs.ex  # stands in for the Coinbase account
    hs.ex = ShadowExchange(real, hs.journal, clock=hs.clock)
    hs.rt = hs.make_runtime()
    opening = float(hs.bar(ENTRY_BAR)["open"])
    real.set_prices(opening, opening)
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    assert real.orders == {} and real.position() is None
    assert hs.ex.position().contracts == 1 and len(hs.ex.open_orders()) == 1
    assert [e["data"]["kind"] for e in hs.journal.events(kind="PREVIEW")] == ["MARKET", "BRACKET"]
    real.set_prices(27_300.0, 27_300.0)  # through the virtual TP1 (27_245)
    hs.at(ENTRY_BAR + 45)
    assert hs.ex.position() is None and hs.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "BRACKET"
    hs.journal.close()


def test_a_shadow_restart_keeps_the_virtual_position(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="SHADOW")
    real = hs.ex
    hs.ex = ShadowExchange(real, hs.journal, clock=hs.clock)
    hs.rt = hs.make_runtime()
    opening = float(hs.bar(ENTRY_BAR)["open"])
    real.set_prices(opening, opening)
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    assert hs.ex.position().contracts == 1
    hs.ex = ShadowExchange(real, hs.journal, clock=hs.clock)  # restart: a new, empty virtual book
    hs.rt = hs.make_runtime()
    hs.clock.now = ENTRY_BAR + 60
    hs.rt.startup()
    assert hs.ex.position().contracts == 1 and len(hs.ex.open_orders()) == 1
    assert not hs.journal.events(kind="CLOSED") and len(hs.journal.events(kind="RECOVERY")) == 2
    hs.journal.close()


class RacyExchange(SimExchange):
    """A fill lands between the runtime's position read and its order read (once, when armed)."""

    race: Callable[[], Any] | None = None

    def open_orders(self) -> list[Order]:
        race, self.race = self.race, None
        if race is not None:
            race()
        return super().open_orders()


def test_a_fill_between_the_position_and_the_order_read_is_read_again(tmp_path):
    """Probe (b): the TP1 leg fills after the poll read the position and before it read the orders. The runtime
    reads the position again after the orders and repeats the pair until both agree, so a stale position is
    never paired with a vanished leg (that would place the TP1 limit again below the market, then sell the TP2
    contracts too): the fill is the live TP1 at once."""
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=10)
    hs.ex = RacyExchange(clock=hs.clock)
    hs.rt = hs.make_runtime()
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    assert hs.ex.position().contracts == 3  # TP1 leg 1 @ 27_245, TP2 leg 2 @ 27_610
    hs.ex.race = lambda: hs.ex.set_prices(27_300.0)  # through the TP1 limit only
    hs.at(ENTRY_BAR + 45)  # the next poll
    tr = hs.rt.executor.trade()
    assert hs.ex.position().contracts == 2 and tr.tp1_filled and hs.ex.calls.count("place_bracket") == 3
    assert [(o.size, o.limit_price, o.stop_price) for o in hs.ex.open_orders()] == [(2, 27_610.0, 26_745.0)]
    assert "UNEXPLAINED_REDUCTION" not in hs.alerts.kinds() and "TP1_FILLED" in hs.alerts.kinds()
    hs.journal.close()


def test_an_ambiguous_tp1_sync_is_read_from_the_position_not_sold_again(tmp_path):
    """Probe (a) end to end: the engine's TP1 (spot) filled, the live TP1 limit did not; the market reduce that
    syncs it fills, but its answer is lost and the lookup fails. The next poll reads the smaller position as that
    reduce: one sale, no UNEXPLAINED_REDUCTION incident, the rest at break-even."""
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=10)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    hs.price(27_200.0)  # between the live entry (26_747) and the TP1 limit (27_245); the engine's TP1 has filled:
    t = hs.rt.target
    engine_tp1 = dataclasses.replace(pending_position(t.entry), tp1_done=True)
    hs.rt.target = dataclasses.replace(t, entry=None, position=engine_tp1)
    hs.ex.crash_after_accept("close_position", Unavailable("read timeout"))
    hs.ex.fail_next("find_order", Unavailable("503"))
    hs.at(ENTRY_BAR + 45)
    assert hs.ex.position().contracts == 2 and hs.rt.executor.trade().pending_reduce is not None
    hs.at(ENTRY_BAR + 55)
    tr = hs.rt.executor.trade()
    assert hs.ex.position().contracts == 2 and tr.tp1_filled and tr.pending_reduce is None
    assert [e["data"]["reason"] for e in hs.journal.events(kind="REDUCE_SETTLED")] == ["TP1_SYNC"]
    assert hs.ex.calls.count("close_position") == 1 and "UNEXPLAINED_REDUCTION" not in hs.alerts.kinds()
    assert [(o.size, o.limit_price, o.stop_price) for o in hs.ex.open_orders()] == [(2, 27_610.0, 26_745.0)]
    assert paused(hs.journal) is None
    hs.journal.close()
