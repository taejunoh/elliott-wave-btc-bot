from __future__ import annotations

import dataclasses
import json
import math
import sqlite3
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable

import pytest

import ewbot.live.runtime as runtime_mod
from ewbot.backtest.holdout import data_fingerprint
from ewbot.live.audit import AuditError, audit
from ewbot.live.control import Command
from ewbot.live.executor import ENTRY_CLAIM_MAX_S, ENTRY_SHOW_MAX_S
from ewbot.live.exchange import (
    Balance,
    ExchangeError,
    KeyPermissions,
    Order,
    PlaceResult,
    Position,
    PreviewResult,
    RateLimited,
    Unavailable,
)
from ewbot.live.feed import Tick, TickerFeed
from ewbot.live.health import Heartbeat
from ewbot.live.notify import RESENT, UNANSWERED, LiveNotifier, TelegramError
from ewbot.live.preflight import BASELINE_FINGERPRINT
from ewbot.live.pricing import MARGIN_ENTRY_MAX, margin_ratio
from ewbot.live.journal import Journal
from ewbot.live.reconcile import ENTRY_NOT_SEEN, Close, Enter, PlanContext, pending_position
from ewbot.live.runtime import (
    AUDIT_FETCH_MAX_S,
    FLATTEN_KEY,
    STARTUP_READ_MAX_S,
    STOP_GRACE_S,
    StartupRefused,
    audit_from_journal,
    status_lines,
)
from ewbot.live.shadow import CALLS_KEPT, FILLS_KEPT, PREVIEW_RETRY_LIMIT, ShadowExchange
from ewbot.live.sim import SimCrash, SimExchange
from ewbot.live.stage import (
    RAISED_KEY,
    canary_failures,
    canary_gates,
    full_cap,
    on_incident,
    paused,
    restart_stint,
    resume,
    shadow_gates,
    transition,
)
from ewbot.live.target import Target, TargetEntry, compute_target
from ewbot.live.watchdog import AccountGuard
from ewbot.paper.decisions import known_at
from ewbot.paper.notify import SendError
from tests.live.harness import Harness
from tests.live.helpers import Clock, Lag, as_on_2026_11_06, journal, position
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


@pytest.fixture
def hf(tmp_path):
    """FULL: the real-account breakers are daily -20% and total -30% (user decision 2026-11-05)."""
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=10, capped=3)
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
    h.ex.fail_next("position", RateLimited("429"), times=2)  # the flatten's pass and this tick's poll cannot read
    h.commands.put(Command("/flatten", ENTRY_BAR + 45))
    h.commands.put(Command("/flatten_confirm", ENTRY_BAR + 48))
    h.at(ENTRY_BAR + 50)
    assert [t for k, t in h.alerts.items if k == "COMMAND"][-1].startswith("청산 요청 - 거래소를 읽지 못함")
    assert h.ex.position() is not None and h.journal.get("flatten")["reason"] == "USER_FLATTEN"  # it stands
    h.at(ENTRY_BAR + 60)  # the next poll sends the close: no second /flatten needed
    assert h.ex.position() is None and h.journal.get("flatten") is None
    assert [t for k, t in h.alerts.items if k == "FLATTEN"] == ["[전량 청산 완료] USER_FLATTEN (요청 후 10초)"]

    def broken(cmd: str, now: float, sent: float | None = None) -> str:
        raise RuntimeError("status unreadable")

    h.rt.handle = broken  # type: ignore[method-assign]
    h.commands.put(Command("/status", ENTRY_BAR + 65))
    h.commands.put(Command("/pause", ENTRY_BAR + 66))
    h.at(ENTRY_BAR + 70)  # each failing command is answered; neither is dropped
    assert [t for k, t in h.alerts.items if k == "COMMAND"][-2:] == [
        "/status 실패: RuntimeError: status unreadable (다시 보내 주세요)",
        "/pause 실패: RuntimeError: status unreadable (다시 보내 주세요)"]


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


def test_the_account_breaker_flattens_and_pauses(hf):
    h = hf
    entered(h)  # the day's first balance read
    h.at(ENTRY_BAR + 90)
    h.at(ENTRY_BAR + 150)  # three reads: the day's reference (their median)
    h.ex.cash -= 2_100.0  # equity 10_000 -> ~7_900: the daily -20% breaker (the total -30% is far off)
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


@pytest.mark.parametrize(("stage", "daily", "total"), [("SHADOW", 0.20, 0.15), ("CANARY", 0.20, 0.15), ("FULL", 0.20, 0.30)])
def test_the_real_account_guard_follows_the_stage(tmp_path, stage, daily, total):
    """User decisions 2026-10-19 and 2026-11-05: daily 20% in every stage, total 15% in SHADOW and CANARY and 30% in FULL; the
    engine's own breakers (cfg.breakers: paper equity, the target replay) keep 3% / 15%."""
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage=stage, stage_days=10)
    assert (hs.rt.guard.params.daily_loss, hs.rt.guard.params.total_dd) == (daily, total)
    assert (hs.rt.cfg.breakers.daily_loss, hs.rt.cfg.breakers.total_dd) == (0.03, 0.15)
    hs.journal.close()


def test_the_guard_params_follow_a_stage_that_is_read_again(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=10, capped=3)
    assert (hs.rt.guard.params.daily_loss, hs.rt.guard.params.total_dd) == (0.20, 0.30)
    hs.journal.set_stage("CANARY", "demotion", ENTRY_BAR)
    hs.rt = hs.make_runtime()  # a restart reads the journal's stage again
    assert hs.rt.stage == "CANARY" and (hs.rt.guard.params.daily_loss, hs.rt.guard.params.total_dd) == (0.20, 0.15)
    hs.rt = hs.make_runtime(stage="FULL")  # a process kept on a stage other than the journal's
    assert hs.rt.stage == "FULL" and (hs.rt.guard.params.daily_loss, hs.rt.guard.params.total_dd) == (0.20, 0.30)
    hs.journal.close()
    fresh = Harness(tmp_path / "fresh", ENTRY_BAR, stage=None)  # a journal without a stage: SHADOW is recorded at start
    assert fresh.rt.guard.params.daily_loss == 0.20
    fresh.price(26_700.0)
    fresh.rt.startup()
    assert fresh.rt.stage == "SHADOW" and fresh.rt.guard.params.daily_loss == 0.20
    fresh.journal.close()


def test_a_minus_five_percent_day_does_not_trip_the_canary_guard(h):
    entered(h)
    h.at(ENTRY_BAR + 90)
    h.at(ENTRY_BAR + 150)  # three reads: the day's reference
    h.ex.cash -= 500.0  # equity 10_000 -> ~9_500
    for k in range(4):
        h.at(ENTRY_BAR + 210 + 60 * k)
    assert "BREAKER" not in h.alerts.kinds() and paused(h.journal) is None and h.ex.position() is not None


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
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=10, capped=3)
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


def test_a_telegram_that_takes_sends_without_answering_is_unusable_after_10_minutes_and_gets_no_extra_copies(h):
    """Telegram takes a send but leaves it unanswered for 15 minutes, getMe too: the worker waits 60 s doubling to
    240 s between probes (longer than NOTIFY_STALL_S, never read as a stuck sender) and sends no copy meanwhile;
    after 10 minutes Telegram is unusable (/fail) as for any failure; once getMe answers the message goes once
    more, marked as a possible copy, and the pings resume."""
    pings = Pings()
    h.rt = h.make_runtime(heartbeat=Heartbeat("https://hc-ping.com/check", session=pings, clock=h.clock))
    t0 = ENTRY_BAR + 100
    h.clock.now = t0 - 60
    h.rt.minute(h.clock.now)  # the day's summary and the first ping, before the notifier under test
    back, chat = t0 + 900, []

    class Hung:  # the live TelegramSender's errors while Telegram takes requests without answering them
        def __call__(self, text: str) -> None:
            chat.append(text)
            if h.clock.now < back:
                raise TelegramError("telegram sendMessage failed: ReadTimeout", UNANSWERED)

        def probe(self) -> None:
            if h.clock.now < back:
                raise TelegramError("telegram getMe failed: ReadTimeout", UNANSWERED)

    n = LiveNotifier(Hung(), clock=h.clock)
    h.rt.d.notify = n
    due: float | None = t0
    h.clock.now = t0
    n("X", "taken but unanswered")
    calls, attempts = [], []
    for k in range(17):
        now = t0 + 60 * k
        while due is not None and due <= now:  # the worker's attempts that fall in this minute, at their times
            h.clock.now = due
            pause = n.pump()
            attempts.append(due - t0)
            due = None if pause is None else due + pause
        h.clock.now = now
        before = len(pings.calls)
        h.rt.minute(now)
        calls.append("".join(c[0][0] for c in pings.calls[before:]))
    assert attempts == [0, 60, 180, 420, 660, 900, 900]  # a send, four probes, then the resend (and idle)
    assert calls == ["G"] * 10 + ["P", "", "", "", "", "G", "G"]  # failing 10 minutes: /fail, pings withheld
    assert chat == ["[LIVE] taken but unanswered", f"[LIVE] taken but unanswered (15분 지연) {RESENT}"]
    reasons = [e["data"]["reasons"] for e in h.journal.events(kind="TELEGRAM_UNUSABLE")]
    assert reasons == [["알림 전송이 10분째 실패 (telegram getMe failed: ReadTimeout)"]]  # never "멈춤"


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


def test_a_stage_change_waits_for_every_order_of_unknown_outcome(h):
    h.journal.record_intent("ewb-x-1-1", "CLOSE", 1, {"reason": "X"}, ENTRY_BAR + 20)  # its outcome is unknown
    h.journal.set_stage("SHADOW", "demotion", ENTRY_BAR + 25)
    h.at(ENTRY_BAR + 30)
    assert h.rt.exit_code is None and h.journal.intents(status="PENDING")  # not flat while it may still fill
    h.journal.update_intent("ewb-x-1-1", "ABANDONED", ENTRY_BAR + 40)
    h.at(ENTRY_BAR + 100)
    assert h.rt.exit_code == 0 and "STAGE" in h.alerts.kinds()


def test_a_process_kept_on_the_real_account_after_a_demotion_says_so_and_ends_once_flat(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="SHADOW")  # the journal: demoted; the process: its last real stage
    hs.rt = hs.make_runtime(stage="CANARY")
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    assert hs.rt.stage == "CANARY" and hs.journal.events(kind="STARTUP")[-1]["data"]["stage"] == "CANARY"
    start = [t for k, t in hs.alerts.items if k == "STARTUP"][-1]
    assert start.startswith("[시작] 단계 CANARY (기록된 단계 SHADOW: 실계좌")
    hs.at(ENTRY_BAR + 30)  # flat: the minute ends the process before this hour's cycle could enter
    assert hs.rt.exit_code == 0 and hs.ex.position() is None and not hs.journal.events(kind="ENTRY")
    hs.journal.close()


def test_a_stop_request_ends_the_loop_after_the_current_tick(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    ticks: list[float] = []
    original = hs.rt.tick

    def tick() -> None:
        assert not ticks, "a tick after the stop request"
        original()
        ticks.append(hs.clock())
        hs.rt.stop_requested = True  # SIGTERM arrived during this tick (`docker stop`)

    hs.rt.tick = tick  # type: ignore[method-assign]
    assert hs.rt.run_forever() == 0 and len(ticks) == 1 and hs.journal.cycles()[-1]["status"] == "OK"
    assert [e["kind"] for e in hs.journal.events(kind="STOPPED")] == ["STOPPED"]
    assert [t for k, t in hs.alerts.items if k == "STOPPED"] == [
        "[종료] 정지 요청: 하던 일을 마치고 종료 (거래소의 보호 주문은 그대로)"]
    hs.journal.close()


def test_the_hourly_audit_fetches_the_paper_ledger_off_the_loop_one_at_a_time(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    led = paper_ledger(SIGNAL, [d for d in hs.journal.decisions() if d["ts"] < SIGNAL])
    release, threads = threading.Event(), []

    def slow() -> dict[str, Any]:
        threads.append(threading.current_thread().name)
        release.wait(10)
        return led

    hs.rt.d.paper_ledger = slow
    t50 = ENTRY_BAR + 3000  # 23:50 UTC: the hour's audit
    begun = time.monotonic()
    hs.at(t50)
    assert time.monotonic() - begun < 5 and hs.journal.get("audit") is None  # the tick never waits on GitHub
    hs.at(t50 + 1)
    assert hs.journal.get("audit") is None and threads == ["ewbot-paper-ledger"]
    release.set()
    hs.rt.audit_job.thread.join(5)
    hs.at(t50 + 2)  # compared on the loop once it is there
    assert audit_from_journal(hs.journal).ok and hs.rt.audit_job is None
    hang = threading.Event()

    def stuck() -> dict[str, Any]:
        threads.append("stuck")
        hang.wait(30)
        return led

    hs.rt.d.paper_ledger = stuck
    hs.at(t50 + 3600)  # the next hour's fetch never answers (a DNS lookup that hangs): given up, a failed audit
    hs.at(t50 + 3600 + AUDIT_FETCH_MAX_S + 1)
    assert audit_from_journal(hs.journal).error == "paper ledger fetch failed: no answer in 60 s"
    hs.at(t50 + 7200)  # still hanging an hour later: no second fetch beside it, the audit fails at once
    assert threads.count("stuck") == 1
    assert audit_from_journal(hs.journal).error == "paper ledger fetch failed: the previous fetch is still running"
    hang.set()
    hs.rt.audit_job.thread.join(5)
    hs.at(t50 + 7201)
    assert hs.rt.audit_job is None
    hs.journal.close()


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
    hs.at(ENTRY_BAR + 3630)  # the cycle of bar ENTRY_BAR cannot download the candles: retried, then held
    hs.at(ENTRY_BAR + 3721)
    assert [c["status"] for c in hs.journal.cycles()][-2:] == ["OK", "HOLD"]
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
        hs = Harness(tmp_path / str(demoted), ENTRY_BAR, stage="FULL", stage_days=10)  # no contract cap in FULL: this
        # fixture's 0.6% stop sizes 186 contracts at FULL_RISK: 105% overnight margin, reduced to the 124 the 70% entry limit holds
        hs.price(float(hs.bar(ENTRY_BAR)["open"]))
        hs.rt.startup()
        hs.at(ENTRY_BAR + 1)  # this minute's stage check is done: the next one would restart the flat runtime
        if demoted:  # a major incident in FULL, then the user's /resume: this process still runs FULL
            assert on_incident(hs.journal, "UNPROTECTED_60S", ENTRY_BAR + 1) == ["paused", "demoted to CANARY"]
            resume(hs.journal, ENTRY_BAR + 2)
            assert hs.rt.stage == "FULL" and hs.rt._ctx(ENTRY_BAR + 3).max_contracts == 1
        hs.at(ENTRY_BAR + 30)
        sizes[demoted] = hs.ex.position().contracts
        hs.journal.close()
    assert sizes[False] == 124 and sizes[True] == 1  # CANARY's one contract at once, not FULL's size


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
            assert full_cap(hs.journal) is None  # recorded and alerted: no cap comes back in FULL (2026-11-05)
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
            assert full_cap(hs.journal) is None  # recorded and alerted: no cap comes back in FULL (2026-11-05)
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
    h.at(ENTRY_BAR + 3630)  # the next cycle has no candles: retried until 2 minutes after the close, then held
    h.at(ENTRY_BAR + 3722)
    assert h.rt.target is None
    h.ex.fail_next("place_bracket", RateLimited("429"))
    h.ex.cancel([o.order_id for o in h.ex.open_orders()])  # the bracket gone
    h.at(ENTRY_BAR + 3732)  # placing it again at the stop in force fails once
    h.at(ENTRY_BAR + 3742)  # placed: the planner cannot judge the legs without a target, but
    h.at(ENTRY_BAR + 3752)  # this pass sees the position covered by the resting stop leg
    for k in range(3):
        h.at(ENTRY_BAR + 3762 + 10 * k)
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
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=10, capped=3)
    hs.ex = RacyExchange(clock=hs.clock)
    hs.rt = hs.make_runtime()
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    assert hs.ex.position().contracts == 3  # TP1 leg 1 @ 27_245, TP2 leg 2 @ 27_610
    hs.ex.race = lambda: hs.ex.set_prices(27_300.0, 27_300.0)  # through the TP1 limit only (spot along: basis band)
    hs.at(ENTRY_BAR + 45)  # the next poll
    tr = hs.rt.executor.trade()
    assert hs.ex.position().contracts == 2 and tr.tp1_filled and hs.ex.calls.count("place_bracket") == 3
    # break-even: the live entry 26_747 rounded up to the tick (the market is far above it)
    assert [(o.size, o.limit_price, o.stop_price) for o in hs.ex.open_orders()] == [(2, 27_610.0, 26_750.0)]
    assert "UNEXPLAINED_REDUCTION" not in hs.alerts.kinds() and "TP1_FILLED" in hs.alerts.kinds()
    hs.journal.close()


def test_an_ambiguous_tp1_sync_is_read_from_the_position_not_sold_again(tmp_path):
    """Probe (a) end to end: the engine's TP1 (spot) filled, the live TP1 limit did not; the market reduce that
    syncs it fills, but its answer is lost and the lookup fails. The next poll reads the smaller position as that
    reduce: one sale, no UNEXPLAINED_REDUCTION incident, the rest at break-even."""
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=10, capped=3)
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
    assert [(o.size, o.limit_price, o.stop_price) for o in hs.ex.open_orders()] == [(2, 27_610.0, 26_750.0)]
    assert paused(hs.journal) is None
    hs.journal.close()


# ---------------------------------------------------------------------------------------------------------------------
# flatten requests (breakers, /flatten_confirm): journaled first, sent at every pass until flat, outcomes as they are


def tripped(h: Harness, t0: float = ENTRY_BAR + 30) -> None:
    """Three balance reads base the day's reference, then a -21% equity: the next minute's read is the first breach."""
    h.at(t0 + 60)
    h.at(t0 + 120)
    h.ex.cash -= 2_100.0
    h.at(t0 + 180)


def test_a_breaker_flatten_that_fails_is_sent_again_at_the_next_poll(hf):
    h = hf
    entered(h)
    tripped(h)
    h.ex.fail_next("close_position", RateLimited("429"))  # the breaker's close is rate-limited (no order created)
    seen = alerts_with_positions(h)
    h.at(ENTRY_BAR + 270)  # the second breaching read: request, pause, one close
    assert h.ex.position() is not None and paused(h.journal)["reason"] == "breaker DAILY_LOSS"
    assert h.journal.get("flatten")["reason"] == "BREAKER_DAILY_LOSS"
    breaker = [t for k, t, _ in seen if k == "BREAKER"]
    assert len(breaker) == 1 and breaker[0].startswith(
        "[실계좌 손실 한도] DAILY_LOSS: 일시 정지 · 청산 요청 - 아직 청산되지 않음: 매 확인마다 다시 보냄")  # never "flattened"
    h.at(ENTRY_BAR + 280)  # the next poll sends the close again
    assert h.ex.position() is None and h.ex.calls.count("close_position") == 2 and h.journal.get("flatten") is None
    assert [e["data"]["reason"] for e in h.journal.events(kind="CLOSED")] == ["BREAKER_DAILY_LOSS"]
    assert [(t, p) for k, t, p in seen if k == "FLATTEN"] == [("[전량 청산 완료] BREAKER_DAILY_LOSS (요청 후 10초)", None)]
    assert h.journal.events(kind="FLATTENED")[-1]["data"]["closed"] is True
    assert not h.journal.events(kind="ORDER_UNRESOLVED")  # the failed close was done by the next pass
    assert [e["kind"] for e in h.journal.events() if e["kind"] in ("FLATTEN_REQUESTED", "PAUSED")] == [
        "FLATTEN_REQUESTED", "PAUSED"]  # the request is journaled before anything else


def test_a_flatten_confirmed_while_the_venue_is_closed_closes_at_the_reopen(h):
    entered(h)
    h.ex.set_session("CLOSE")  # e.g. the weekly CFM halt
    h.at(ENTRY_BAR + 100)  # the minute sees it
    h.commands.put(Command("/flatten", ENTRY_BAR + 105))
    h.commands.put(Command("/flatten_confirm", ENTRY_BAR + 108))
    h.at(ENTRY_BAR + 110)
    assert [t for k, t in h.alerts.items if k == "COMMAND"][-1].startswith(
        "장이 열려 있지 않아 청산 대기: 장이 열리면 즉시 청산 (보호 주문 유지), 일시 정지")
    for k in range(1, 5):  # polls while closed: the close waits, the bracket keeps protecting
        h.at(ENTRY_BAR + 110 + 10 * k)
        assert h.ex.position() is not None and len(h.ex.open_orders()) == 1
    assert "close_position" not in h.ex.calls and "FLATTEN" not in h.alerts.kinds()  # the reply said it already
    assert "전량 청산 진행 중: USER_FLATTEN (2024-03-13 23:01 UTC부터, 장이 열리면 즉시 청산)" in h.rt.handle(
        "/status", ENTRY_BAR + 155)
    h.ex.set_session("OPEN")
    seen = alerts_with_positions(h)
    h.at(ENTRY_BAR + 160)  # the minute sees OPEN and polls at once: the close goes out
    assert h.ex.position() is None and h.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "USER_FLATTEN"
    assert [(t, p) for k, t, p in seen if k == "FLATTEN"] == [("[전량 청산 완료] USER_FLATTEN (요청 후 50초)", None)]
    assert paused(h.journal)["reason"] == "user /flatten" and "전량 청산 진행 중" not in h.rt.handle("/status", ENTRY_BAR + 170)
    assert [e["data"]["state"] for e in h.journal.events(kind="FLATTEN_DEFERRED")] == ["CLOSE"]


@pytest.mark.parametrize("crash", ["before the request", "before the close"])
def test_a_crash_before_the_breaker_close_went_out_still_flattens_after_the_restart(tmp_path, crash):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=10, capped=3)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    tripped(hs)

    def killed(*args: Any, **kw: Any) -> Any:
        raise SimCrash("killed")

    if crash == "before the request":  # between the breaker's trip (journaled by the guard) and the flatten request
        hs.rt._request_flatten = killed  # type: ignore[method-assign]
    else:  # after the request and the pause, before the close reached the venue
        hs.ex.close_position = killed  # type: ignore[method-assign]
    with pytest.raises(SimCrash):
        hs.at(ENTRY_BAR + 270)
    assert hs.ex.position() is not None
    if crash == "before the close":
        del hs.ex.close_position
        assert paused(hs.journal)["reason"] == "breaker DAILY_LOSS" and hs.journal.get("flatten") is not None
    hs.journal.close()
    hs.journal = Journal(tmp_path / "journal.sqlite")
    hs.clock.now = ENTRY_BAR + 300
    hs.rt = hs.make_runtime()
    hs.rt.startup()  # the request stands (or the guard's trip is still in force): the start's pass closes
    assert hs.ex.position() is None and paused(hs.journal)["reason"] == "breaker DAILY_LOSS"
    assert [e["data"]["reason"] for e in hs.journal.events(kind="CLOSED")] == ["BREAKER_DAILY_LOSS"]
    assert hs.journal.get("flatten") is None
    if crash == "before the request":  # no breaker alert went out before the crash: the restart's says it all
        assert [t for k, t in hs.alerts.items if k == "BREAKER"] == [
            "[실계좌 손실 한도] DAILY_LOSS: 재시작 때 발동 중 - 일시 정지 · 전량 청산 완료"]
    else:
        assert hs.alerts.items[-1] == ("FLATTEN", "[전량 청산 완료] BREAKER_DAILY_LOSS (요청 후 30초)")
    hs.journal.close()


def test_a_flatten_after_an_unseen_bracket_fill_records_the_bracket_exit(h):
    entered(h)
    h.ex.set_prices(27_300.0)  # the TP1 bracket fills on the venue (1-contract CANARY trade: flat) before a poll sees it
    h.rt.handle("/flatten", ENTRY_BAR + 33)
    assert h.rt.handle("/flatten_confirm", ENTRY_BAR + 35) == "포지션 없음 (청산할 것 없음), 일시 정지"
    t = h.rt.executor.trade()
    assert (t.status, t.exit_reason) == ("CLOSED", "BRACKET") and "close_position" not in h.ex.calls
    assert [(e["data"]["leg"], e["data"]["price"]) for e in h.journal.events(kind="BRACKET_FILL")] == [("tp1", 27_245.0)]
    gates = {g.name: g for g in canary_gates(h.journal, ENTRY_BAR + 100, None)}
    assert gates["완결 거래 3건 이상 또는 90일"].detail.startswith("1건")


# ---------------------------------------------------------------------------------------------------------------------
# the decision cycle: a failed download is a late bar, a failure after the replay ends the bar once, restarts


def test_a_failed_candle_download_is_retried_like_a_late_bar(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="SHADOW", stage_days=0.001)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.fetch.fail = 1  # one connection error at hh:00:30; the next download works
    hs.at(ENTRY_BAR + 30)
    assert hs.rt.next_try == ENTRY_BAR + 45 and hs.journal.cycles()[-1]["status"] == "RUNNING"
    hs.at(ENTRY_BAR + 45)
    c = hs.journal.cycles()[-1]
    assert (c["bar_ts"], c["status"]) == (SIGNAL, "OK") and hs.ex.position() is not None  # decided and entered in time
    gates = {g.name: g for g in shadow_gates(hs.journal, ENTRY_BAR + 3600, None)}
    assert gates["② 모든 사이클이 마감 후 2분 안에 끝남"].met
    assert [e["data"]["error"] for e in hs.journal.events(kind="FETCH_ERROR")] == ["ConnectionError: coinbase down"]
    hs.fetch.fail = 100  # a download that keeps failing: held 2 minutes after the close, with its reason
    for dt in (30, 45, 60, 75, 90, 105, 121):
        hs.at(ENTRY_BAR + 3600 + dt)
    c = hs.journal.cycles()[-1]
    assert (c["status"], c["note"]) == ("HOLD", "candles not fetched 2 min after the close: ConnectionError: coinbase down")
    assert hs.rt.target is None and [t for k, t in hs.alerts.items if k == "DATA_LATE"] == [
        "[판단 보류] 2024-03-13 23:00 UTC 봉을 마감 후 2분 안에 받지 못함 (ConnectionError: coinbase down) (보호 주문 유지)"]
    hs.journal.close()


def test_a_restart_retries_the_target_of_an_hour_already_decided(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    decided = [(c["bar_ts"], c["status"], c["finished_ts"]) for c in hs.journal.cycles()]
    hs.journal.close()
    hs.journal = Journal(tmp_path / "journal.sqlite")
    hs.clock.now = ENTRY_BAR + 1800
    hs.fetch.fail = 2  # the start's download and the target's refresh fail
    hs.rt = hs.make_runtime()
    hs.rt.startup()
    # retried, not given up for the hour; meanwhile the hour's decision stored before the restart stands
    assert hs.rt.target is not None and hs.rt.target.bar_ts == SIGNAL and hs.rt.next_try == ENTRY_BAR + 1815
    hs.at(ENTRY_BAR + 1815)
    assert hs.rt.target is not None and hs.rt.done_bar == SIGNAL
    assert [(c["bar_ts"], c["status"], c["finished_ts"]) for c in hs.journal.cycles()] == decided  # its row untouched
    hs.journal.close()


def test_a_failure_after_the_replay_ends_the_cycle_once(h):
    def broken(now: float) -> None:
        raise KeyError("a planner bug on this state")

    h.rt.reconcile = broken  # type: ignore[method-assign]
    fetched = h.fetch.calls
    for k in range(30):  # 30 one-second ticks after hh:00:30
        h.at(ENTRY_BAR + 30 + k)
    c = h.journal.cycles()[-1]
    assert (c["status"], c["note"], h.rt.done_bar) == ("ERROR", "KeyError: 'a planner bug on this state'", SIGNAL)
    assert h.fetch.calls - fetched == 1  # one download and one replay, not one per tick
    assert h.rt.target is not None and h.rt.target.bar_ts == SIGNAL  # the failure was in its reconcile: target kept
    assert [e["data"]["section"] for e in h.journal.events(kind="LOOP_ERROR")] == ["cycle"]
    assert h.alerts.kinds().count("CYCLE_ERROR") == 1


def test_a_held_hour_stays_held_after_a_restart_and_a_crashed_cycle_runs_again(tmp_path, monkeypatch):
    hs = Harness(tmp_path / "held", ENTRY_BAR + 30)
    full = hs.df
    hs.fetch.source = full[full["ts"] < SIGNAL]  # the SIGNAL bar does not arrive in time
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    for dt in (30, 45, 60, 75, 90, 105, 121):
        hs.at(ENTRY_BAR + dt)
    assert hs.journal.cycles()[-1]["status"] == "HOLD"
    hs.fetch.source = full  # it arrives; the runtime restarts within the entry's 5 minutes
    hs.journal.close()
    hs.journal = Journal(tmp_path / "held" / "journal.sqlite")
    hs.clock.now = ENTRY_BAR + 200
    hs.rt = hs.make_runtime()
    hs.rt.startup()
    hs.at(ENTRY_BAR + 210)
    assert hs.journal.cycles()[-1]["status"] == "HOLD" and hs.rt.done_bar == SIGNAL  # the hold stands, as without one
    assert hs.ex.position() is None and hs.rt.target is None
    hs.journal.close()
    # a cycle a crash left RUNNING runs again, under the usual deadlines
    hs = Harness(tmp_path / "crashed", ENTRY_BAR + 30)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()

    def killed(*args: Any, **kw: Any) -> Any:
        raise SimCrash("killed")

    monkeypatch.setattr("ewbot.live.runtime.compute_target", killed)
    with pytest.raises(SimCrash):
        hs.at(ENTRY_BAR + 30)
    monkeypatch.undo()
    assert hs.journal.cycles()[-1]["status"] == "RUNNING"
    hs.journal.close()
    hs.journal = Journal(tmp_path / "crashed" / "journal.sqlite")
    hs.clock.now = ENTRY_BAR + 60
    hs.rt = hs.make_runtime()
    hs.rt.startup()
    hs.at(ENTRY_BAR + 61)
    c = hs.journal.cycles()[-1]
    assert (c["bar_ts"], c["status"], c["started_ts"]) == (SIGNAL, "OK", ENTRY_BAR + 30) and hs.ex.position() is not None
    hs.journal.close()


def test_a_stage_change_ends_the_tick_before_the_cycle_can_enter(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 20, stage="SHADOW")
    real = hs.ex
    hs.ex = ShadowExchange(real, hs.journal, clock=hs.clock)
    hs.rt = hs.make_runtime()
    real.set_prices(float(hs.bar(ENTRY_BAR)["open"]), float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.journal.set_stage("CANARY", "promotion (flat book)", ENTRY_BAR + 25)  # `ewbot live stage canary --confirm`
    hs.at(ENTRY_BAR + 30)  # the minute sees the new stage while flat: the process ends, this hour's cycle never runs
    assert hs.rt.exit_code == 0 and hs.ex.position() is None and hs.journal.cycles() == []
    assert hs.rt.executor.trade() is None and not hs.journal.events(kind="ENTRY")
    hs.journal.close()


def test_an_entry_is_not_sent_after_its_deadline_even_when_the_pass_began_in_time(h):
    attempts: list[float] = []
    placed = h.ex.place_market

    def busy(cid: str, side: int, size: int) -> PlaceResult:  # the venue rate-limits the entry for minutes
        attempts.append(h.clock())
        if h.clock() < ENTRY_BAR + 290:
            raise RateLimited("429")
        return placed(cid, side, size)

    h.ex.place_market = busy  # type: ignore[method-assign]
    h.at(ENTRY_BAR + 30)
    assert h.journal.intent(f"ewb-e-{SIGNAL}-L")["status"] == "PENDING"
    balance = h.ex.balance

    def slow() -> Balance:  # the pass's own reads take 5 s: it began at +298, the send would be at +303
        h.clock.now += 5.0
        return balance()

    h.ex.balance = slow  # type: ignore[method-assign]
    h.at(ENTRY_BAR + 298)
    assert len(attempts) == 1 and h.ex.position() is None
    assert h.journal.events(kind="SKIP")[-1]["data"]["reason"] == "봉 마감 후 5분이 지나 진입하지 않음"


# ---------------------------------------------------------------------------------------------------------------------
# restarts, the loop's clock, the watchdog, the minute


def test_a_restart_polls_fast_and_watches_from_the_journal_even_when_its_first_reads_fail(h):
    entered(h)
    h.rt = h.make_runtime()  # a restart in the middle of the hour; the venue answers 503 to the first position reads
    h.clock.now = ENTRY_BAR + 600
    h.ex.fail_next("position", Unavailable("503"), times=3)
    h.rt.startup()
    assert h.rt.active  # from the journal's open trade, before any read succeeded
    h.price(25_000.0)  # gapped far beyond the stop's 5% limit (25_255.75)
    for k in range(1, 13):
        h.at(ENTRY_BAR + 600 + k)
    assert h.ex.position() is None and h.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "STOP_GAP"


def test_an_entry_accepted_right_before_a_crash_is_protected_at_the_first_fast_poll(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.ex.crash_after_accept("place_market")
    with pytest.raises(SimCrash):
        hs.at(ENTRY_BAR + 30)
    hs.journal.close()
    hs.journal = Journal(tmp_path / "journal.sqlite")
    hs.clock.now = ENTRY_BAR + 45  # restarted 15 s later; the venue answers 503 for a few reads
    hs.rt = hs.make_runtime()
    hs.ex.fail_next("find_order", Unavailable("503"), times=3)
    hs.ex.fail_next("position", Unavailable("503"), times=2)
    hs.rt.startup()
    assert hs.rt.active  # the PENDING entry: 10 s polls from the start
    k = next(k for k in range(1, 61) if hs.at(ENTRY_BAR + 45 + k) or hs.ex.open_orders())
    assert k <= 10 and not hs.journal.events(kind="INCIDENT")  # no PROTECT_SLOW: protected within 30 s of the entry
    hs.journal.close()


def test_the_loop_runs_on_a_monotonic_clock_through_a_backward_wall_clock_step(h):
    entered(h)
    mono = Clock(1_000.0)
    h.rt = h.make_runtime(monotonic=mono)
    h.rt.startup()
    polls, minutes = h.ex.calls.count("open_orders"), h.ex.calls.count("session")
    for k in range(1, 301):  # the wall clock stepped back 10 minutes and runs on for 5 minutes
        mono.now += 1.0
        h.at(ENTRY_BAR + 30 - 600 + k)
    assert 29 <= h.ex.calls.count("open_orders") - polls <= 31  # a poll every 10 s (a trade is open)
    assert h.ex.calls.count("session") - minutes == 5 and h.journal.get("heartbeat") == ENTRY_BAR + 30 - 600 + 241


def test_a_failing_watchdog_close_is_tried_once_per_poll_interval_with_limited_alerts(h):
    h.ex.fail_next("place_bracket", ExchangeError("INVALID_STOP_PRICE"), times=500)  # no stop rests
    h.at(ENTRY_BAR + 30)
    h.at(ENTRY_BAR + 40)
    h.price(26_580.0)  # through the 26_585 stop
    h.ex.fail_next("close_position", RateLimited("429"), times=500)
    n0, c0 = len(h.alerts.items), h.ex.calls.count("close_position")
    for k in range(1, 31):
        h.at(ENTRY_BAR + 40 + k)
    kinds = [k for k, _ in h.alerts.items[n0:]]
    assert h.ex.calls.count("close_position") - c0 == 3  # at +50, +60, +70 (the REST price is read every 10 s)
    assert kinds.count("STOP_UNPROTECTED") == 1 and kinds.count("CLOSE_FAILED") == 1 and kinds.count("BRACKET_FAILED") == 0
    assert [t for k, t in h.alerts.items if k == "STOP_UNPROTECTED"] == [
        "[손절] 보호 주문 없이 가격 26,580.0이 손절 26,585을 지남: 시장가 청산 실패 - 10초마다 재시도"]
    h.ex.clear_faults("close_position")  # the venue takes the close again
    h.at(ENTRY_BAR + 75)
    assert h.ex.position() is not None  # 5 s after the last attempt: not yet
    h.at(ENTRY_BAR + 80)
    assert h.ex.position() is None and h.alerts.items[-1] == (
        "STOP_UNPROTECTED", "[손절] 보호 주문 없이 가격 26,580.0이 손절 26,585을 지남: 시장가 청산 완료")


def test_a_failing_margin_step_never_skips_the_rest_of_the_minute(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", equity=400.0)
    hs.price(26_744.5)
    hs.rt.startup()
    hs.rt.guard.params = dataclasses.replace(hs.rt.guard.params, daily_loss=1.0, total_dd=1.0)  # tested on their own
    hs.ex.place_market("ewb-e-1-L", 1, 3)
    hs.rt.executor.adopt(position(signal_ts=SIGNAL, stop=26_000.0, tp1=27_500.0, tp2=28_000.0), hs.rt._ctx(hs.clock()))
    hs.ex.cash = 170.0  # ~99%: a reduce

    def down(size: int, reason: str, ctx: PlanContext) -> bool:
        raise Unavailable("503")

    hs.rt.executor.reduce = down  # type: ignore[method-assign]
    hs.at(ENTRY_BAR + 100)
    assert hs.ex.position().contracts == 3 and hs.journal.events(kind="MARGIN_ERROR")
    assert [t for k, t in hs.alerts.items if k == "MARGIN"] == [
        "[증거금 축소 실패] 오버나이트 기준 99%: 1계약 축소를 하지 못함 - 1분 뒤 다시 점검"]
    assert hs.journal.get("once:daily") == "2024-03-13"  # the rest of the minute ran (the daily summary)
    del hs.rt.executor.reduce
    hs.at(ENTRY_BAR + 160)
    assert hs.ex.position().contracts == 2 and [t for k, t in hs.alerts.items if k == "MARGIN"][-1] == (
        "[증거금 축소] 오버나이트 기준 99%: 1계약 축소")

    def broken(equity: float, now: float) -> str | None:
        raise RuntimeError("guard state unreadable")

    hs.rt.guard.update = broken  # type: ignore[method-assign]
    hs.at(ENTRY_BAR + 220)
    assert hs.journal.events(kind="LOOP_ERROR")[-1]["data"] == {"section": "money",
                                                                 "error": "RuntimeError: guard state unreadable"}
    assert hs.journal.get("audit") is None and hs.journal.get("heartbeat") == ENTRY_BAR + 220
    hs.journal.close()


def test_the_daily_summary_and_key_check_are_done_for_the_day_only_once_they_succeed(h):
    entered(h)
    day2 = ts("2024-03-14 13:01")  # 09:01 New York the next day
    h.journal.add_event(day2 - 600, "ENTRY", {"trade_id": 7, "side": 1, "contracts": 1, "price": 0.0, "ref": 26_744.5,
                                              "fee": 0.0})  # a fill price the adapter could not read
    h.rt.d.expiry = {"coinbase": "2024-04-01", "github": "not-a-date"}
    costs = h.rt.costs

    def broken(since: float) -> dict[str, Any]:
        raise RuntimeError("cost report bug")

    h.rt.costs = broken  # type: ignore[method-assign]
    h.ex.fail_next("key_permissions", Unavailable("503"))
    h.clock.now = day2
    h.rt.minute(day2)
    assert h.journal.get("once:daily") == "2024-03-13" and h.journal.get("once:keycheck") != "2024-03-14"
    h.rt.costs = costs  # type: ignore[method-assign]
    h.clock.now = day2 + 60
    h.rt.minute(day2 + 60)  # the key check is read again at once; the summary waits DAILY_RETRY_S
    assert h.journal.get("once:keycheck") == "2024-03-14" and h.journal.get("once:daily") == "2024-03-13"
    h.clock.now = day2 + 660
    h.rt.minute(day2 + 660)
    assert h.journal.get("once:daily") == "2024-03-14"
    daily = [t for k, t in h.alerts.items if k == "DAILY"][-1]
    assert daily.startswith("[일일 요약] 2024-03-14") and "체결 24시간: 1건 · 진입 슬리피지 +0.9 bps" in daily
    assert [t for k, t in h.alerts.items if k == "EXPIRY"][-2:] == [
        "[만료 예정] coinbase: 2024-04-01 (18일 남음) - 갱신 필요",
        "[만료 예정] github: 날짜 'not-a-date'를 읽지 못함 - EWBOT_EXPIRY 확인"]


def test_bracket_fills_are_read_back_until_the_read_succeeds_and_partial_fills_count(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=10, capped=3)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)  # 3 contracts: TP1 leg 1 @ 27_245, TP2 leg 2 @ 27_610
    hs.ex.bracket_fill_cap = 1  # one contract per leg and price
    hs.price(27_620.0)  # the TP1 leg fills, the TP2 leg only one of its two
    hs.ex.fail_next("get_order", Unavailable("503"))  # the TP1 leg's read back fails once
    hs.at(ENTRY_BAR + 45)

    def fills() -> list[tuple[str, int, float]]:
        return [(e["data"]["leg"], e["data"]["contracts"], e["data"]["price"]) for e in hs.journal.events(kind="BRACKET_FILL")]

    assert fills() == [("tp2", 1, 27_610.0)]  # the partial fill of the resting leg, from the open orders
    hs.at(ENTRY_BAR + 55)
    assert fills()[:2] == [("tp2", 1, 27_610.0), ("tp1", 1, 27_245.0)]  # read back at the next pass
    hs.ex.bracket_fill_cap = None
    for k in range(3):
        hs.at(ENTRY_BAR + 65 + 10 * k)
    assert hs.ex.position() is None and sum(n for _, n, _ in fills()) == 3  # every contract once
    assert hs.rt.costs(ENTRY_BAR)["fills"] == 1 + len(fills())
    hs.journal.close()


# ---------------------------------------------------------------------------------------------------------------------
# start, SHADOW, redaction


def test_a_start_that_cannot_read_the_venue_retries_then_refuses(tmp_path):
    hs = Harness(tmp_path / "blip", ENTRY_BAR + 30)
    hs.price(26_700.0)
    hs.ex.fail_next("session", Unavailable("503"), times=2)
    hs.rt.startup()  # two failed reads, then it starts
    assert [e["data"]["wait_s"] for e in hs.journal.events(kind="STARTUP_RETRY")] == [2.0, 4.0]
    assert "STARTUP" in hs.alerts.kinds()
    hs.journal.close()
    hs = Harness(tmp_path / "down", ENTRY_BAR + 30)
    hs.ex.fail_next("product", Unavailable("503"), times=100)
    with pytest.raises(StartupRefused, match="product 조회 실패"):
        hs.rt.startup()
    assert 0 < hs.clock() - (ENTRY_BAR + 30) <= STARTUP_READ_MAX_S
    assert not hs.journal.events(kind="STARTUP") and hs.journal.events(kind="STARTUP_REFUSED")
    hs.journal.close()


def test_a_maintenance_within_a_day_is_announced_at_start(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.ex.maintenance = (ENTRY_BAR + 6 * 3600, ENTRY_BAR + 9 * 3600)  # the quarterly maintenance in 6 hours
    hs.rt.startup()
    for k in range(3):
        hs.at(ENTRY_BAR + 100 + 60 * k)
    assert [t for k, t in hs.alerts.items if k == "MAINTENANCE"] == [
        "[장 상태] 정기 점검 예정 2024-03-14 01:00 ~ 04:00 (뉴욕)"]
    assert len(hs.journal.events(kind="MAINTENANCE")) == 1
    hs.journal.close()


def test_a_shadow_restart_without_a_quote_triggers_nothing_and_never_reuses_an_order_id(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="SHADOW")
    real = hs.ex
    hs.ex = ShadowExchange(real, hs.journal, clock=hs.clock)
    hs.rt = hs.make_runtime()
    opening = float(hs.bar(ENTRY_BAR)["open"])
    real.set_prices(opening, opening)
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    first = set(hs.ex.orders)
    hs.ex = ShadowExchange(real, hs.journal, clock=hs.clock)  # restart: a new, empty virtual book
    hs.rt = hs.make_runtime()
    hs.clock.now = ENTRY_BAR + 600
    real.fail_next("quote", Unavailable("503"), times=2)  # the real quote fails during the start
    hs.rt.startup()
    assert hs.ex.position().contracts == 1 and [o.triggered for o in hs.ex.orders.values()] == [False]
    assert not first & set(hs.ex.orders) and all(oid.startswith("v2-") for oid in hs.ex.orders)
    hs.at(ENTRY_BAR + 610)  # the real price (above the 26_585 stop) is back
    assert hs.ex.position().contracts == 1 and not hs.journal.events(kind="CLOSED")
    hs.journal.close()


def test_a_preview_the_api_could_not_answer_is_asked_again_and_only_a_refusal_fails(tmp_path):
    clock = Clock(1_000.0)
    real = SimExchange(clock=clock)
    real.set_prices(60_000.0, 60_010.0)
    jr = journal(tmp_path)
    alerts: list[tuple[str, str]] = []
    sh = ShadowExchange(real, jr, clock=clock, alert=lambda k, t: alerts.append((k, t)))
    real.fail_next("preview_market", Unavailable("503"), times=2)  # both tries
    assert sh.place_market("ewb-e-1-L", 1, 1).ok  # the virtual order goes on
    assert not jr.events(kind="PREVIEW") and [k for k, _ in alerts] == ["PREVIEW_UNVERIFIED"]
    real.fail_next("preview_market", RateLimited("429"))  # a passing error: tried again at once
    assert sh.close_position("ewb-x-1-1").ok

    def previews() -> list[tuple[str, bool, bool]]:
        return [(e["data"]["client_order_id"], e["data"]["ok"], bool(e["data"].get("retried")))
                for e in jr.events(kind="PREVIEW")]

    assert previews() == [("ewb-x-1-1", True, False)]
    assert sh.retry_previews() == 0  # the next cycle asks again: answered, verified
    assert previews()[-1] == ("ewb-e-1-L", True, True)
    real.fail_next("preview_market", Unavailable("503"), times=2 + PREVIEW_RETRY_LIMIT)
    sh.place_market("ewb-e-2-L", 1, 1)
    for _ in range(PREVIEW_RETRY_LIMIT - 1):
        assert sh.retry_previews() == 1
    assert sh.retry_previews() == 0 and previews()[-1] == ("ewb-e-2-L", False, True)  # never answered: failed
    real.preview_errors = ("PREVIEW_INSUFFICIENT_FUND",)  # a refusal fails at once
    sh.close_position("ewb-x-2-1")
    assert previews()[-1] == ("ewb-x-2-1", False, False) and alerts[-1][0] == "PREVIEW_FAILED"
    jr.close()


def test_a_shadow_cycle_asks_again_for_an_unanswered_entry_preview(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="SHADOW")
    real = hs.ex
    hs.ex = ShadowExchange(real, hs.journal, clock=hs.clock)
    hs.rt = hs.make_runtime()
    opening = float(hs.bar(ENTRY_BAR)["open"])
    real.set_prices(opening, opening)
    hs.rt.startup()
    real.fail_next("preview_market", Unavailable("503"), times=3)  # the entry's two tries and the cycle's retry
    hs.at(ENTRY_BAR + 30)
    assert hs.ex.position().contracts == 1 and hs.journal.events(kind="PREVIEW_UNVERIFIED")
    assert [e["data"]["kind"] for e in hs.journal.events(kind="PREVIEW")] == ["BRACKET"]
    hs.at(ENTRY_BAR + 3630)  # the next cycle
    assert [(e["data"]["kind"], e["data"]["ok"]) for e in hs.journal.events(kind="PREVIEW")][-1] == ("MARKET", True)
    gates = {g.name: g for g in shadow_gates(hs.journal, ENTRY_BAR + 3640, None)}
    assert gates["③ 냈을 주문 전부 preview 통과"].met
    hs.journal.close()


POSITION_REFUSAL = "PREVIEW_BRACKET_ORDER_SIZE_EXCEEDS_POSITION"
PREVIEW_GATE = "③ 냈을 주문 전부 preview 통과"


class FlatAccount(SimExchange):
    """The real account of SHADOW as Coinbase answered on 2026-10-05 (flat, View key): a market preview is checked -
    size, funds -, while every bracket preview is refused with POSITION_REFUSAL alone, whatever the bracket (valid,
    its stop on the wrong side, a marketable take-profit, 1000 contracts): Coinbase checks a bracket's size against
    the position before anything else. `bracket_errors` replaces that answer."""

    bracket_errors: tuple[str, ...] = (POSITION_REFUSAL,)

    def preview_bracket(self, side: int, size: int, limit_price: float, stop_price: float) -> PreviewResult:
        self._fault("preview_bracket")
        return PreviewResult(False, self.bracket_errors)


def flat_shadow(tmp_path, name: str = "journal.sqlite") -> tuple[ShadowExchange, FlatAccount, Journal, Clock,
                                                                  list[str]]:
    """A ShadowExchange over a FlatAccount, its journal in SHADOW; the alert kinds it sends."""
    clock = Clock(1_000.0)
    real = FlatAccount(clock=clock)
    real.set_prices(60_000.0, 60_010.0)
    jr = journal(tmp_path, name)
    jr.set_stage("SHADOW", "test", 0.0)
    alerts: list[str] = []
    return ShadowExchange(real, jr, clock=clock, alert=lambda k, t: alerts.append(k)), real, jr, clock, alerts


def preview_gate(jr: Journal, now: float):
    return {g.name: g for g in shadow_gates(jr, now, None)}[PREVIEW_GATE]


def test_shadow_gate_3_is_met_when_coinbase_refuses_the_brackets_only_for_want_of_a_position(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="SHADOW")
    real = FlatAccount(clock=hs.clock)  # stands in for the flat Coinbase account
    alerts: list[str] = []
    hs.ex = ShadowExchange(real, hs.journal, clock=hs.clock, alert=lambda k, t: alerts.append(k))
    hs.rt = hs.make_runtime()
    opening = float(hs.bar(ENTRY_BAR)["open"])
    real.set_prices(opening, opening)
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)  # the entry and its bracket, both previewed on the real account
    gate = preview_gate(hs.journal, ENTRY_BAR + 40)
    assert gate.met, gate.detail
    assert gate.detail == "1/1, 포지션이 없어 확인 못 한 브래킷 1건 (CANARY 첫 거래에서 확인)"
    assert [(e["data"]["kind"], e["data"]["ok"]) for e in hs.journal.events(kind="PREVIEW")] == [("MARKET", True)]
    assert [(e["data"]["kind"], e["data"]["errors"]) for e in hs.journal.events(kind="PREVIEW_POSITION_DEPENDENT")] == [
        ("BRACKET", [POSITION_REFUSAL])]
    assert alerts == [] and hs.ex.position().contracts == 1 and len(hs.ex.open_orders()) == 1  # protected virtually
    assert real.orders == {} and real.position() is None
    assert not {"place_market", "place_bracket", "edit_bracket", "cancel", "close_position"} & set(real.calls)
    hs.journal.close()


def test_any_other_preview_refusal_still_fails_gate_3(tmp_path):
    for i, (kind, errors) in enumerate([
            ("BRACKET", ("PREVIEW_INVALID_BRACKET_PRICES",)),  # another refusal
            ("BRACKET", (POSITION_REFUSAL, "PREVIEW_INSUFFICIENT_FUNDS_FOR_FUTURES")),  # mixed with another one
            ("BRACKET", ("PREVIEW_ORDER_SIZE_EXCEEDS_BRACKETED_POSITION",)),  # brackets resting: not a flat account's
            ("BRACKET", ()),  # refused without a reason
            ("MARKET", (POSITION_REFUSAL,))]):  # a market preview stays strict
        sh, real, jr, clock, alerts = flat_shadow(tmp_path, f"journal-{i}.sqlite")
        if kind == "BRACKET":
            real.bracket_errors = errors
        else:
            real.preview_errors = errors
        assert sh.place_market("ewb-e-1-L", 1, 1).ok
        assert sh.place_bracket("ewb-b-1-tp1-1", -1, 1, 61_000.0, 59_000.0).ok  # the virtual book takes it
        assert not preview_gate(jr, clock.now).met and alerts == ["PREVIEW_FAILED"], (kind, errors)
        jr.close()


def test_a_bracket_the_venue_model_refuses_fails_gate_3_without_asking_coinbase(tmp_path):
    # the flat account would answer each of these POSITION_REFUSAL like a valid one: the size, the tick grid and the
    # sides are checked before the real preview (the virtual book's intake, SimExchange._rejected)
    for i, (side, size, tp, stop, code) in enumerate([
            (-1, 1, 59_000.0, 61_000.0, "PREVIEW_INVALID_BRACKET_PRICES"),  # a long's stop above its take-profit
            (1, 1, 61_000.0, 59_000.0, "PREVIEW_INVALID_BRACKET_PRICES"),  # a short's stop below its take-profit
            (-1, 1, 61_002.0, 59_000.0, "PREVIEW_INVALID_PRICE_PRECISION"),  # off the $5 tick
            (-1, 1, 61_000.0, 59_001.0, "PREVIEW_INVALID_STOP_PRICE_PRECISION"),
            (-1, 1.5, 61_000.0, 59_000.0, "PREVIEW_INVALID_SIZE_PRECISION")]):
        sh, real, jr, clock, alerts = flat_shadow(tmp_path, f"journal-{i}.sqlite")
        assert sh.place_market("ewb-e-1-x", -side, 1).ok
        assert not sh.place_bracket("ewb-b-1-tp1-1", side, size, tp, stop).ok  # refused virtually too
        assert "preview_bracket" not in real.calls and not jr.events(kind="PREVIEW_POSITION_DEPENDENT")
        assert [e["data"]["errors"] for e in jr.events(kind="PREVIEW") if not e["data"]["ok"]] == [[code]], code
        assert not preview_gate(jr, clock.now).met and alerts == ["PREVIEW_FAILED"]
        jr.close()


def test_a_bracket_preview_answered_later_for_want_of_a_position_is_set_apart_too(tmp_path):
    sh, real, jr, clock, alerts = flat_shadow(tmp_path)
    assert sh.place_market("ewb-e-1-L", 1, 1).ok
    real.fail_next("preview_bracket", Unavailable("503"), times=2)  # both tries unanswered
    assert sh.place_bracket("ewb-b-1-tp1-1", -1, 1, 61_000.0, 59_000.0).ok
    assert alerts == ["PREVIEW_UNVERIFIED"] and not jr.events(kind="PREVIEW_POSITION_DEPENDENT")
    assert sh.retry_previews() == 0  # the next cycle: answered, for want of a position only
    assert [e["data"]["retried"] for e in jr.events(kind="PREVIEW_POSITION_DEPENDENT")] == [True]
    assert preview_gate(jr, clock.now).met and alerts == ["PREVIEW_UNVERIFIED"]
    jr.close()


def test_the_shadow_book_stays_bounded(tmp_path):
    clock = Clock(1_000.0)
    real = SimExchange(clock=clock)
    real.set_prices(60_000.0, 60_010.0)
    jr = journal(tmp_path)
    sh = ShadowExchange(real, jr, clock=clock)
    for i in range(600):  # 1200 virtual fills over 10 hours
        sh.place_market(f"ewb-e-{i}-L", 1, 1)
        sh.close_position(f"ewb-x-{i}-1")
        clock.now += 60.0
    assert len(sh.fills) <= FILLS_KEPT + 1 and len(sh.orders) == 1200  # an ended order is dropped a day later
    clock.now += 86_400.0 + 1
    sh.place_market("ewb-e-late-L", 1, 1)
    clock.now += 86_400.0 + 1
    sh.place_market("ewb-e-later-L", 1, 1)
    assert len(sh.orders) <= 3
    for _ in range(2500):
        sh.position()
    assert len(sh.calls) <= 2 * CALLS_KEPT
    jr.close()


def test_error_texts_in_alerts_and_the_journal_are_redacted(h):
    h.rt = h.make_runtime(redact=lambda text: text.replace("SECRET", "<secret>"))
    h.rt.startup()
    h.ex.fail_next("place_market", ExchangeError("refused for botSECRET"))  # the entry refused, the reason has a secret
    h.at(ENTRY_BAR + 30)
    h.ex.fail_next("position", RateLimited("429 at botSECRET"))
    h.at(ENTRY_BAR + 100)
    h.ex.fail_next("session", Unavailable("503 at botSECRET"))
    h.at(ENTRY_BAR + 160)

    def broken(cmd: str, now: float, sent: float | None = None) -> str:
        raise RuntimeError("botSECRET")

    h.rt.handle = broken  # type: ignore[method-assign]
    h.commands.put(Command("/status", ENTRY_BAR + 165))
    h.at(ENTRY_BAR + 170)
    assert not [t for _, t in h.alerts.items if "SECRET" in t] and "ENTRY_FAILED" in h.alerts.kinds()
    runtime_events = ("POLL_ERROR", "SESSION_ERROR", "COMMAND_ERROR")
    assert all("<secret>" in json.dumps(e["data"]) and "SECRET" not in json.dumps(e["data"]).replace("<secret>", "")
               for k in runtime_events for e in h.journal.events(kind=k)) and all(
        h.journal.events(kind=k) for k in runtime_events)



# ---------------------------------------------------------------------------------------------------------------------
# final fix


def test_a_fill_price_the_venue_reports_as_nan_never_leaves_the_entry_unprotected(h):
    """Final fix (P0, probe t12rev/r2/f2_nan.py): a market order read back with a NaN fill price and fee. The trade
    used to be refused by the journal - a filled entry with no trade and no bracket, the cycle ERROR, and the rebuild
    raising the same way at every pass. Now it is saved (a stand-in entry price, alerted) and protected at once."""
    real = h.ex.get_order

    def nan_fill(order_id: str) -> Order:
        o = real(order_id)
        return dataclasses.replace(o, avg_fill_price=math.nan, fee=math.nan) if o.kind == "MARKET" else o

    h.ex.get_order = nan_fill
    for k in range(13):
        h.at(ENTRY_BAR + 30 + 10 * k)
    t = h.rt.executor.trade()
    assert t is not None and t.status == "OPEN" and t.entry_price == h.ex.position().avg_entry == 26_747.0
    assert [(o.size, o.stop_price) for o in h.ex.open_orders()] == [(1, 26_585.0)]
    assert not h.journal.events(kind="LOOP_ERROR") and h.journal.cycles()[-1]["status"] == "OK"
    assert "FILL_PRICE_UNREAD" in h.alerts.kinds() and not h.journal.events(kind="UNPROTECTED")


def test_a_hung_candle_download_never_stops_the_loop(h, monkeypatch):
    """Final fix (Task 12): the download (15 s timeouts, retries) ran on the loop - the watchdog and the polls stopped
    while it hung. It runs on a worker thread now: waited for at most FETCH_WAIT_S when it starts, never again while it
    runs (the 15 s retries only look), and the cycle runs at the first tick after it ended."""
    monkeypatch.setattr(runtime_mod, "FETCH_WAIT_S", 0.5)
    release = threading.Event()
    real = h.rt.d.fetch

    def hung(cache_dir):
        release.wait(5)
        return real(cache_dir)

    h.rt.d.fetch = hung
    begun = time.monotonic()
    h.at(ENTRY_BAR + 30)  # the cycle starts the download, waits 0.5 s for it and the tick goes on
    h.at(ENTRY_BAR + 31)
    h.at(ENTRY_BAR + 46)  # the 15 s retry: the download still runs - looked at, not waited for
    assert time.monotonic() - begun < 0.9 and h.rt.done_bar != SIGNAL and h.ex.position() is None
    assert h.journal.cycles()[-1]["status"] == "RUNNING"
    release.set()
    h.rt.candle_job.thread.join(5)
    h.at(ENTRY_BAR + 47)  # the download ended: decided at this tick, not at the next 15 s retry
    assert h.rt.done_bar == SIGNAL and h.ex.position() is not None and h.journal.cycles()[-1]["status"] == "OK"
    assert h.rt.d.fetch is hung and h.rt.candle_job is None


def test_a_deferred_flatten_reads_the_session_every_pass_and_drops_its_waiting_wording(h):
    """Final fix (Task 12): while a flatten waits for the reopen the session is read at every pass (not only every
    minute), and once its close goes out again /status no longer says it waits for the reopen."""
    entered(h)
    h.ex.set_session("CLOSE")
    h.at(ENTRY_BAR + 100)  # the minute sees it
    h.commands.put(Command("/flatten", ENTRY_BAR + 105))
    h.commands.put(Command("/flatten_confirm", ENTRY_BAR + 108))
    h.at(ENTRY_BAR + 110)
    assert h.journal.get("flatten")["deferred"] is True
    h.ex.set_session("OPEN")
    h.ex.fail_next("close_position", RateLimited("429"))  # the first close after the reopen fails
    h.at(ENTRY_BAR + 120)  # a poll 10 s later - the next minute is 40 s away: the session is read, the close goes out
    assert h.ex.calls.count("close_position") == 1 and h.ex.position() is not None
    assert h.journal.get("flatten")["deferred"] is False and h.journal.events(kind="FLATTEN_RESUMED")
    assert "(2024-03-13 23:01 UTC부터, 매 확인마다 청산 주문)" in h.rt.handle("/status", ENTRY_BAR + 125)
    h.at(ENTRY_BAR + 130)
    assert h.ex.position() is None and h.journal.get("flatten") is None


def test_the_overnight_margin_report_without_the_product_is_alerted(h):
    """Final fix (Task 8): a position at 15:30 New York whose margin cannot be computed is reported - never the day
    marked done in silence (here: no product spec)."""
    entered(h)
    h.rt.product = None
    h.rt._overnight_report(ts("2024-03-14 19:31"))  # 15:31 New York (EDT)
    assert [x for k, x in h.alerts.items if k == "MARGIN_1530"] == [
        "[15:30 증거금 점검] 가격, 잔고 또는 상품 정보를 읽지 못해 계산하지 못함 (1계약)"]
    assert h.journal.events(kind="MARGIN_1530")[-1]["data"]["ratio"] is None


def test_a_stop_request_ends_the_start_while_it_retries_a_read(tmp_path):
    """Final fix (Task 13 N-5): `docker stop` while the start retries the session read (an outage) ends the process at
    once - the retry waits run in steps of a second - instead of retrying for 5 minutes past the stop grace period."""
    hs = Harness(tmp_path, ENTRY_BAR + 30)
    hs.ex.fail_next("session", Unavailable("503"), times=1000)
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        hs.clock.sleep(s)
        if sum(slept) >= 3:
            hs.rt.stop_requested = True  # SIGTERM during the second wait

    hs.rt = hs.make_runtime(sleep=sleep)
    assert hs.rt.run_forever(max_ticks=5) == 0
    assert max(slept) <= 1.0 and sum(slept) == 3
    assert [e["data"].get("during") for e in hs.journal.events(kind="STOPPED")] == ["startup"]
    assert not hs.journal.events(kind="STARTUP") and not hs.journal.events(kind="STARTUP_REFUSED")
    assert [t for k, t in hs.alerts.items if k == "STOPPED"] == [
        "[종료] 정지 요청: session 조회를 다시 시도하던 중 - 시작하지 않고 종료"]
    hs.journal.close()


@pytest.mark.parametrize("failures", [3, 1000])
def test_a_stop_request_waits_while_the_open_trade_has_no_resting_stop(tmp_path, failures):
    """Final fix (Task 13 N-5): a stop while the open trade has no resting stop (its brackets refused) lets the loop go
    on until the next passes placed it - at most STOP_GRACE_S - then ends; past that it ends anyway and says so."""
    hs = Harness(tmp_path, ENTRY_BAR + 30)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.ex.fail_next("place_bracket", RateLimited("429"), times=failures)
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)  # entered; its bracket refused
    assert hs.ex.position() is not None and not hs.ex.open_orders()
    hs.rt = hs.make_runtime()  # a process that gets `docker stop` right after its start
    hs.rt.stop_requested = True
    assert hs.rt.run_forever(max_ticks=500) == 0
    stopped = hs.journal.events(kind="STOPPED")[-1]["data"]
    assert len(hs.journal.events(kind="STOP_DEFERRED")) == 1
    if failures == 3:
        assert [(o.size, o.stop_price) for o in hs.ex.open_orders()] == [(1, 26_585.0)]
        assert stopped["unprotected"] is False and 0 < stopped["ticks"] < 30
    else:
        assert not hs.ex.open_orders() and stopped["unprotected"] is True
        assert STOP_GRACE_S - 1 <= stopped["ticks"] <= STOP_GRACE_S + 1
        assert "경고" in [t for k, t in hs.alerts.items if k == "STOPPED"][-1]
    hs.journal.close()


# ----------------------------------------------------------------------------- final review fix round


def covered(hs: Harness) -> bool:
    """The venue's position is covered by resting stops of ours."""
    pos = hs.ex.position()
    return pos is not None and sum(o.size - o.filled for o in hs.ex.open_orders() if o.stop_price) >= pos.contracts


def crashed_after_entry(tmp_path) -> Harness:
    hs = Harness(tmp_path, ENTRY_BAR + 30)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.ex.crash_after_accept("place_market")  # filled, then the process dies before the journal hears back
    with pytest.raises(SimCrash):
        hs.at(ENTRY_BAR + 30)
    hs.journal.close()
    hs.journal = Journal(tmp_path / "journal.sqlite")
    return hs


def test_a_restart_without_a_target_protects_our_unsettled_entry_at_once(tmp_path):
    """Final review A-A (probe pA): killed right after the venue took our entry, restarted in the same hour while the
    lookups fail and no candles can be had (no target all hour). The start's own pass takes the position over from the
    entry's intent (provisional, its levels) - it was left unprotected for the hour -, alerted as such
    (ADOPTED_PROVISIONAL) but no manual-intervention incident: it is our own entry (re-review A)."""
    hs = crashed_after_entry(tmp_path)
    hs.clock.now = ENTRY_BAR + 70

    def down(cid, since):
        hs.ex.calls.append("find_order")
        raise Unavailable("503")

    hs.ex.find_order = down
    hs.fetch.fail = 10_000
    hs.rt = hs.make_runtime()
    hs.rt.startup()
    assert hs.rt.target is None and covered(hs) and hs.rt.active
    assert hs.journal.events(kind="ADOPTED_PROVISIONAL")[0]["data"]["why"] == "LOOKUP_FAILED"
    assert "ADOPTED_PROVISIONAL" in hs.alerts.kinds() and "UNKNOWN_POSITION" not in hs.alerts.kinds()
    # no manual-intervention incident (the 40 s the crash left it unprotected is still measured: PROTECT_SLOW)
    assert [e["data"]["kind"] for e in hs.journal.events(kind="INCIDENT")] == ["PROTECT_SLOW"]
    tr = hs.rt.executor.trade()
    assert (tr.trade_id, tr.stop_price) == (SIGNAL, hs.journal.intent(f"ewb-e-{SIGNAL}-L")["payload"]["stop"])
    hs.journal.close()


def test_the_start_protects_before_it_downloads_the_candles(tmp_path):
    """Final review B minor 5: the start's recovery and first pass run before the candle download (minutes with an
    empty cache), so a crash right after a fill is protected at once, not after the download."""
    hs = crashed_after_entry(tmp_path)
    hs.clock.now = ENTRY_BAR + 70
    seen: list[bool] = []
    fetch = hs.fetch

    def recording(cache_dir):
        seen.append(covered(hs))
        return fetch(cache_dir)

    hs.rt = hs.make_runtime(fetch=recording)
    hs.rt.startup()
    assert seen and seen[0] is True and covered(hs)
    hs.journal.close()


def test_an_accepted_entry_whose_fill_is_unread_keeps_the_fast_polls(h):
    """Final review A-B (probe pB): the entry is accepted and fills, its fill read fails through the 10 s wait
    (ENTRY_UNCONFIRMED): nothing is OPEN or PENDING, yet the runtime keeps polling every 10 s - covered at the next
    poll, not a minute later (PROTECT_SLOW)."""
    real = h.ex.get_order

    def degraded(oid):
        if h.clock() < ENTRY_BAR + 45:
            h.ex.calls.append("get_order")
            raise Unavailable("503")
        return real(oid)

    h.ex.get_order = degraded
    h.at(ENTRY_BAR + 30)
    assert h.journal.events(kind="ENTRY_UNCONFIRMED") and h.rt.executor.trade() is None and h.rt.active
    t = int(h.clock()) + 1
    while not covered(h) and t < ENTRY_BAR + 120:
        h.at(t)
        t += 1
    assert covered(h) and h.clock() <= ENTRY_BAR + 51
    assert not [e for e in h.journal.events(kind="INCIDENT") if e["data"]["kind"] == "PROTECT_SLOW"]


class RefusesCloseBesideBrackets(SimExchange):
    """close_position refused while a bracket of ours rests, with a code CLOSE_CONFLICTS does not know."""

    def close_position(self, client_order_id, size=None):
        if any(o.kind == "BRACKET" and o.status == "OPEN" for o in self.orders.values()):
            self.calls.append("close_position")
            return PlaceResult(client_order_id, None, False, "INVALID_CLOSE_POSITION_REQUEST: brackets rest")
        return super().close_position(client_order_id, size)


def test_a_gapped_stops_close_refused_with_an_unknown_code_still_goes_out(tmp_path):
    """Final review A-C (probe pC): a gapped stop's watchdog close refused with an unknown code was retried every 10 s
    forever (the triggered stop's limit never fills). The watchdog's close takes any refusal for a conflict: the
    brackets are cancelled and the close goes out."""
    hs = Harness(tmp_path, ENTRY_BAR + 30)
    hs.ex = RefusesCloseBesideBrackets(clock=hs.clock)
    hs.rt = hs.make_runtime()
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    t = hs.rt.executor.trade()
    for k in range(6):  # a tick every 10 s at a price 7% below the stop: past its 5% limit
        hs.price(t.stop_price * 0.93)
        hs.at(ENTRY_BAR + 60 + 10 * k)
    assert hs.ex.position() is None and hs.journal.events(kind="CLOSE_CONFLICT")
    assert hs.rt.executor.trade().exit_reason == "STOP_GAP"
    hs.journal.close()


def test_the_extra_refusal_codes_reach_the_executor(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30)
    rt = hs.make_runtime(close_conflicts=("BRACKETS_REST",), overlap_refusals=("TOO_MANY_BRACKETS",))
    assert {"BRACKETS_REST", "OPEN_BRACKET_ORDERS"} <= rt.executor.close_conflicts
    assert {"TOO_MANY_BRACKETS", "INSUFFICIENT_POSITION_SIZE"} <= rt.executor.overlap_refusals
    hs.journal.close()


class Blind(SimExchange):
    """The position read says flat (a reply of another shape) while `blind` is set."""

    blind = False

    def position(self):
        p = super().position()
        return None if self.blind else p


def test_a_flat_read_its_resting_legs_contradict_keeps_the_trade_and_its_brackets(tmp_path):
    """Final review A-D (probe pD): one flat read while the trade's legs all still rest whole closed the trade's record,
    cancelled its brackets and freed the next entry to stack on the real position. The trade stays OPEN with its
    brackets until a second read agrees; a standing flatten is not marked done by it either."""
    hs = Harness(tmp_path, ENTRY_BAR + 30)
    hs.ex = Blind(clock=hs.clock)
    hs.rt = hs.make_runtime()
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    hs.rt._request_flatten("USER_FLATTEN", "USER", ENTRY_BAR + 40)  # a flatten stands when the wrong read comes
    hs.ex.blind = True
    hs.at(ENTRY_BAR + 45)
    assert hs.rt.executor.trade().status == "OPEN" and hs.journal.events(kind="FLAT_UNCONFIRMED")
    assert [o.size for o in hs.ex.open_orders()] == [1] and hs.ex.pos is not None  # the bracket still rests
    assert hs.journal.get(runtime_mod.FLATTEN_KEY) is not None and not hs.journal.events(kind="FLATTENED")
    hs.ex.blind = False
    hs.at(ENTRY_BAR + 60)  # the position reads again: the flatten closes it
    assert hs.ex.position() is None and hs.journal.get(runtime_mod.FLATTEN_KEY) is None
    assert hs.journal.events(kind="FLATTENED")[-1]["data"]["closed"] is True
    hs.journal.close()


def test_a_basis_outside_its_band_holds_entries_and_stop_moves_and_is_alerted(tmp_path):
    """Final review A minor 3: futures and spot mids more than 2% apart are no basis: no level is mapped and no entry
    sized with it (the entry is skipped, the stop in force stays), alerted with limited repeats."""
    hs = Harness(tmp_path, ENTRY_BAR + 30, ratio=1.03)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    for k in range(5):
        hs.at(ENTRY_BAR + 30 + 10 * k)
    assert hs.ex.position() is None and "호가" in hs.journal.events(kind="SKIP")[0]["data"]["reason"]
    band = [t for k, t in hs.alerts.items if k == "BASIS"]
    assert len(band) == 1 and "2%" in band[0] and hs.journal.events(kind="BASIS_BAND")
    hs.journal.close()


def test_an_entry_is_never_sized_without_a_margin_rate(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.ex.spec = dataclasses.replace(hs.ex.spec, overnight_margin=(0.0, 0.0))  # the entry reads the rates again: a missing one reads 0
    hs.at(ENTRY_BAR + 30)
    assert hs.ex.position() is None and "증거금률" in hs.journal.events(kind="SKIP")[0]["data"]["reason"]
    hs.journal.close()


def sized(tmp_path, stage: str, equity: float, *, direction: int = 1, stop_dist: float = 240.0,
          stage_days: float = 60.0, risk_mult: float = 1.0, manual: bool = False,
          overnight: tuple[float, float] | None = None) -> tuple[int, str | None, Harness]:
    """`_entry_size` for a hand-made entry at 26,740 (ratio 1.0, so entry == entry_ref) with the stop `stop_dist` away,
    on a `stage` harness. `manual`: a manual intervention still in force (the only cap FULL has). `overnight`: the
    exchange's overnight margin rates (long, short) after the runtime started (the entry reads them again)."""
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage=stage, stage_days=stage_days, equity=equity)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    if overnight is not None:
        hs.ex.spec = dataclasses.replace(hs.ex.spec, overnight_margin=overnight)
    if manual:
        hs.journal.put(RAISED_KEY, {"items": [["FOREIGN_ORDERS", 7]], "stint": hs.journal.stage()[1]})
    ent = TargetEntry(SIGNAL, "W3", direction, 26_740.0, 26_740.0 - direction * stop_dist,
                      26_740.0 + direction * 760.0, None, risk_mult, 5, "test signal")
    hs.rt.target = Target(SIGNAL, 26_740.0, None, None, ent, ())
    n, block = hs.rt._entry_size(ENTRY_BAR + 30, 1.0, True)
    return n, block, hs


def test_full_enters_at_the_margin_maximum_when_the_risk_size_would_exceed_the_limit(tmp_path):
    """User decision 2026-11-05: FULL reduces an entry the 70% overnight-margin limit cannot hold instead of refusing it.
    Long, equity 10,000, entry 26,740, stop 26,500: the 5x leverage cap sizes 186 contracts (the 10% risk would allow 375);
    186 contracts are 186 * 0.01 * 26,740 * 0.21025 / 10,000 = 105% of the account, above 70%. One contract needs
    0.01 * 26,740 * 0.21025 = 56.22 USD, so the most that fits is floor(0.70 * 10,000 / 56.22) = floor(124.5) = 124."""
    n, block, hs = sized(tmp_path, "FULL", 10_000.0)
    assert (n, block) == (124, None)
    assert margin_ratio(124, 26_740.0, 1, hs.rt.product, 10_000.0) <= MARGIN_ENTRY_MAX
    assert margin_ratio(125, 26_740.0, 1, hs.rt.product, 10_000.0) > MARGIN_ENTRY_MAX
    hs.journal.close()


def test_full_has_no_contract_cap_from_its_first_day(tmp_path):
    """User decision 2026-11-05: the first hour of FULL sizes like any later one, min(risk 10%, 5x, margin maximum with the
    70% overnight check): the very same 124 as after 60 days, no 3 anywhere. No cap reaches the sizing, the planner's stage
    maximum for a position taken over, or the status line."""
    n, block, hs = sized(tmp_path, "FULL", 10_000.0, stage_days=0.01)
    assert (n, block) == (124, None) and hs.rt._entry_reduction["cap"] is None
    assert hs.rt._limits() == ("FULL", None) and hs.rt._ctx(ENTRY_BAR + 30).max_contracts is None
    assert status_lines(hs.journal, ENTRY_BAR + 30)[0].count("상한 없음") == 1
    hs.journal.close()
    wide, block, hs = sized(tmp_path / "wide", "FULL", 10_000.0, stage_days=0.01, stop_dist=2_000.0)  # the risk size binds
    assert (wide, block) == (49, None) and wide > 3 and hs.rt._entry_reduction is None
    hs.journal.close()


def test_a_manual_intervention_still_in_force_holds_full_to_3_contracts(tmp_path):
    """This safety behavior never depended on the 30 days and stays: the minimum of the risk size (186), the cap (3) and the
    margin maximum (124) is the cap: nothing is reduced, and the status says why."""
    n, block, hs = sized(tmp_path, "FULL", 10_000.0, manual=True)
    assert (n, block) == (3, None) and hs.rt._entry_reduction is None
    assert "수동 개입 중 상한 3계약" in status_lines(hs.journal, ENTRY_BAR + 30)[0]
    hs.journal.close()


def test_a_foreign_order_first_read_in_the_cycles_pass_caps_that_entry(tmp_path):
    """Review finding: the cap came from the journal, written at the end of the pass that first read the order: that pass's
    own entry went out at 124 contracts, and only the next one was capped at 3."""
    hs = Harness(tmp_path, ENTRY_BAR, stage="FULL", stage_days=10)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 1)
    real = hs.ex.open_orders
    foreign = Order("f1", "manual-1", -1, "OTHER", 1, 0, "OPEN")
    hs.ex.open_orders = lambda: real() + [foreign]  # placed after the last poll, first read by the cycle's pass
    assert full_cap(hs.journal) is None
    hs.at(ENTRY_BAR + 30)
    assert hs.ex.position().contracts == 3 and full_cap(hs.journal) == 3
    hs.ex.open_orders = real  # gone: this process no longer holds to it either
    hs.at(ENTRY_BAR + 41)
    assert hs.rt._limits() == ("FULL", None)
    hs.journal.close()


def guarded(hs: Harness, equity: float | None) -> None:
    hs.journal.put(AccountGuard.KEY, {"equity": equity, "peak": equity, "day": "2024-03-13", "day_ref": equity,
                                      "daily_tripped": False, "total_tripped": False})


def sizing(tmp_path, *reads, stage: str = "FULL", reference: float | None = 10_000.0) -> tuple[Harness, list]:
    """A FULL entry at 26,740 with its stop 240 away (one contract's overnight margin: 56.22 USD), the guard's last observation
    at `reference`, and balance() answering `reads` in turn (an exception raises), the last one from then on."""
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage=stage, stage_days=60.0)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    if reference is not None:
        guarded(hs, reference)
    ent = TargetEntry(SIGNAL, "W3", 1, 26_740.0, 26_500.0, 27_500.0, None, 1.0, 5, "test signal")
    hs.rt.target = Target(SIGNAL, 26_740.0, None, None, ent, ())
    calls: list[float] = []

    def balance() -> Balance:
        x = reads[min(len(calls), len(reads) - 1)]
        calls.append(x)
        if isinstance(x, Exception):
            raise x
        return Balance(float(x), None)

    hs.ex.balance = balance
    return hs, calls


def size_now(hs: Harness) -> tuple[int, str | None]:
    return hs.rt._entry_size(ENTRY_BAR + 30, 1.0, True)


@pytest.mark.parametrize("read", [10_000.0, 11_900.0, 8_100.0])
def test_a_balance_read_in_the_band_is_read_once(tmp_path, read):
    hs, calls = sizing(tmp_path, read)
    assert size_now(hs)[0] > 0 and calls == [read] and hs.journal.events(kind="EQUITY_CHANGED") == []
    hs.journal.close()


@pytest.mark.parametrize("reads,median,contracts", [
    ((30_000.0, 30_100.0, 29_950.0), 30_000.0, 373),  # a deposit: floor(0.70 * 30,000 / 56.22)
    ((5_000.0, 5_010.0, 4_990.0), 5_000.0, 62),  # a withdrawal, or a stale reference after downtime
])
def test_three_reads_that_agree_size_the_entry_even_outside_the_band_and_it_says_so(tmp_path, reads, median, contracts):
    """User decision 2026-11-05: signals come about once a month. A real change of the balance, or a reference gone stale, is
    no reason to skip one: the median of three reads that agree within 1% sizes it."""
    hs, calls = sizing(tmp_path, *reads)
    start = hs.clock.now
    assert size_now(hs) == (contracts, None) and calls == list(reads)
    assert hs.rt._entry_reduction["equity"] == median
    assert 0 < hs.clock.now - start <= 5  # within a few seconds
    assert hs.journal.events(kind="EQUITY_CHANGED")[-1]["data"]["median"] == median
    said = [t for k, t in hs.alerts.items if k == "EQUITY_CHANGED"]
    assert len(said) == 1 and said[0].startswith("[잔고 변경]") and "10,000.00" in said[0] and f"{median:,.2f}" in said[0]
    assert size_now(hs)[0] > 0 and len([k for k in hs.alerts.kinds() if k == "EQUITY_CHANGED"]) == 1  # once a signal
    hs.journal.close()


def test_one_wild_read_followed_by_normal_ones_loses_no_signal(tmp_path):
    """Three reads always, once one is out of the band: the median discards the wild one, and nothing says the balance
    changed, because the median is in the band."""
    hs, calls = sizing(tmp_path, 100_000.0, 10_000.0, 10_000.0)
    assert size_now(hs) == (124, None) and calls == [100_000.0, 10_000.0, 10_000.0]
    assert hs.journal.events(kind="EQUITY_CHANGED") == []
    assert hs.journal.events(kind="EQUITY_READ_OFF")[-1]["data"]["reads"] == calls
    assert any(t.startswith("[잔고 조회 이상]") for _, t in hs.alerts.items)
    hs.journal.close()


def test_flapping_reads_with_a_poisoned_reference_size_on_the_truth(tmp_path):
    """Re-review M3: the true read looked out of the band, a wrong second read looked back in it, and the entry doubled
    (248 for 124). The reference is 20,000 from a wrong minute; the truth is 10,000."""
    hs, calls = sizing(tmp_path, 10_000.0, 20_000.0, 10_000.0, reference=20_000.0)
    assert size_now(hs) == (124, None) and len(calls) == 3
    hs.journal.close()


def test_two_of_three_agreeing_above_the_band_are_used_and_said(tmp_path):
    hs, calls = sizing(tmp_path, 30_000.0, 30_000.0, 10_000.0)
    assert size_now(hs) == (373, None) and len(calls) == 3
    assert hs.journal.events(kind="EQUITY_CHANGED")[-1]["data"]["median"] == 30_000.0
    hs.journal.close()


@pytest.mark.parametrize("reads,why", [
    ((30_000.0, 20_000.0, 10_000.0), "서로 다름"),  # no two within 1%
    ((5_000.0, 5_200.0, 5_400.0), "서로 다름"),  # a real move between the reads: nothing to size on
    ((30_000.0, 30_000.0, float("nan")), "읽지 못함"),
    ((float("inf"), float("inf"), float("inf")), "읽지 못함"),
    ((30_000.0, Unavailable("HTTP 503")), "읽지 못함"),
    ((30_000.0, 30_000.0, RateLimited("429")), "읽지 못함"),
])
def test_reads_that_disagree_or_fail_refuse_the_entry_and_say_why(tmp_path, reads, why):
    hs, _ = sizing(tmp_path, *reads)
    hs.rt.poll(ENTRY_BAR + 31)  # a whole pass: skipped, and the skip is alerted
    assert hs.ex.position() is None and hs.journal.events(kind="EQUITY_CHANGED") == []
    said = [t for k, t in hs.alerts.items if k == "SKIP"]
    assert len(said) == 1 and why in said[0] and hs.journal.events(kind="SKIP")[-1]["data"]["signal_ts"] == SIGNAL
    hs.journal.close()


def test_a_signal_already_traded_or_skipped_is_not_read_again(tmp_path):
    hs, calls = sizing(tmp_path, 30_000.0)
    hs.journal.put("skipped_signals", [SIGNAL])
    size_now(hs)
    assert calls == [30_000.0]
    hs.journal.close()


def test_the_balance_check_is_for_full_only_and_needs_a_reference(tmp_path):
    for name, stage, ref, wanted in (("canary", "CANARY", 5_000.0, 1), ("first", "FULL", None, 124)):
        # far from the read: CANARY's one contract has its own risk and margin checks
        hs, calls = sizing(tmp_path / name, 10_000.0, stage=stage, reference=ref)
        assert size_now(hs) == (wanted, None) and calls == [10_000.0]
        hs.journal.close()


def test_the_margin_maximum_wins_when_it_is_smaller_than_the_risk_size(tmp_path):
    """Equity 200: the risk size is 3 (the 5x cap: floor(5 * 200 / 267.4) = 3), 3 contracts are 3 * 56.22 / 200 = 84% > 70%;
    the most that fits is floor(0.70 * 200 / 56.22) = floor(2.49) = 2."""
    n, block, hs = sized(tmp_path, "FULL", 200.0)
    assert (n, block) == (2, None)
    hs.journal.close()


def test_full_at_or_under_the_margin_limit_is_unchanged(tmp_path):
    """A wide stop (26,740 to 24,740: 2,000 + 0.0005 * 51,480 = 2,025.7 per BTC, 20.26 USD per contract) sizes
    floor(1,000 / 20.26) = 49 contracts at 10% risk: 49 * 56.22 / 10,000 = 27.5%, under 70%: entered whole, no reduction."""
    n, block, hs = sized(tmp_path, "FULL", 10_000.0, stop_dist=2_000.0)
    assert (n, block) == (49, None) and hs.rt._entry_reduction is None
    hs.journal.close()


def test_full_refuses_when_not_even_one_contract_fits(tmp_path):
    """Equity 54: 1 contract is 56.22 / 54 = 104% > 70% and floor(0.70 * 54 / 56.22) = floor(0.67) = 0: refused,
    with the message the refusal always had."""
    n, block, hs = sized(tmp_path, "FULL", 54.0)
    assert (n, block) == (0, "오버나이트 증거금 예상 104% > 70%") and hs.rt._entry_reduction is None
    hs.journal.close()


@pytest.mark.parametrize("stage", ["CANARY", "SHADOW"])
def test_canary_and_shadow_above_the_limit_still_refuse(tmp_path, stage):
    """They trade 1 contract, never a reduced FULL size: equity 54 projects 104% for it."""
    n, block, hs = sized(tmp_path, stage, 54.0)
    assert (n, block) == (0, "오버나이트 증거금 예상 104% > 70%") and hs.rt._entry_reduction is None
    hs.journal.close()


def test_a_short_is_reduced_with_the_higher_short_margin_rate(tmp_path):
    """Short, equity 10,000, entry 26,740: 186 contracts again, at the short overnight rate 0.256625 one contract needs
    0.01 * 26,740 * 0.256625 = 68.62 USD, so floor(0.70 * 10,000 / 68.62) = floor(102.0) = 102 (a long fits 124)."""
    n, block, hs = sized(tmp_path, "FULL", 10_000.0, direction=-1)
    assert (n, block) == (102, None)
    assert margin_ratio(102, 26_740.0, -1, hs.rt.product, 10_000.0) <= MARGIN_ENTRY_MAX
    assert margin_ratio(103, 26_740.0, -1, hs.rt.product, 10_000.0) > MARGIN_ENTRY_MAX
    hs.journal.close()


def test_a_reduced_full_entry_is_sent_recorded_once_and_left_alone_by_the_margin_watchdog(tmp_path):
    """The cycle enters the reduced size (the fixture's long: the margin maximum floor(0.70 * 10,000 / 56.23) = 124),
    journals and alerts the needed and the entered contracts once, and the minute checks (warn 80%, reduce 90% of the
    overnight projection) leave a position entered just under 70% alone."""
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=60.0)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    assert hs.ex.position().contracts == 124
    rows = hs.journal.events(kind="ENTRY_REDUCED")
    assert len(rows) == 1
    d = rows[0]["data"]
    assert (d["entered"], d["margin_max"], d["cap"], d["stage"]) == (124, 124, None, "FULL") and d["needed"] > 124
    texts = [t for k, t in hs.alerts.items if k == "ENTRY_REDUCED"]
    assert len(texts) == 1 and texts[0].startswith("[진입 축소]") and f"{d['needed']}계약" in texts[0] and "124계약" in texts[0]
    for k in range(1, 8):
        hs.at(ENTRY_BAR + 30 + 60 * k)
    assert len(hs.journal.events(kind="ENTRY_REDUCED")) == 1 and hs.ex.position().contracts == 124
    assert "MARGIN" not in hs.alerts.kinds() and not hs.journal.events(kind="REDUCED")
    hs.journal.close()


def test_a_rebase_request_rebases_both_breakers_at_the_next_minute(h):
    """Final review B-2: `ewbot live rebase --confirm` journals a request; the runtime - the breakers' one writer - applies
    it at its next minute with that minute's balance read (the first of three basing reads), journaled and alerted
    with before and after; refused while a flatten stands."""
    entered(h)
    h.at(ENTRY_BAR + 100)
    h.ex.cash -= 1_000.0  # a withdrawal of 10%: two reads would trip both breakers
    h.journal.put(runtime_mod.REBASE_KEY, {"ts": ENTRY_BAR + 120})
    h.at(ENTRY_BAR + 160)
    assert h.journal.get(runtime_mod.REBASE_KEY) is None
    rebased = h.journal.events(kind="REBASED")[0]["data"]
    assert rebased["after"]["day_ref"] == h.ex.balance().equity and rebased["before"]["day_ref"] > rebased["after"]["day_ref"]
    assert [t for k, t in h.alerts.items if k == "REBASE"][0].startswith("[손실 한도 기준 재설정]")
    for k in range(2, 6):
        h.at(ENTRY_BAR + 100 + 60 * k)
    assert "BREAKER" not in h.alerts.kinds() and h.ex.position() is not None
    h.rt._request_flatten("USER_FLATTEN", "USER", ENTRY_BAR + 500)
    h.journal.put(runtime_mod.REBASE_KEY, {"ts": ENTRY_BAR + 500})
    h.rt.minute(ENTRY_BAR + 520)
    assert h.journal.events(kind="REBASE_REFUSED") and h.journal.get(runtime_mod.REBASE_KEY) is None
    h.rt.daily(ENTRY_BAR + 600)  # re-review B: a rebase absorbs what the breach flags held - it stays visible
    daily = [t for k, t in h.alerts.items if k == "DAILY"][-1]
    assert "손실 한도 기준 재설정 24시간: 1회" in daily


class GappyFeed:
    """A feed that asks for a resync at every tick (a gap a second) and counts its gaps."""

    def __init__(self) -> None:
        self.gaps = self.connects = self.failures = 0
        self.last_error = None

    def take_resync(self) -> bool:
        self.gaps += 3
        return True

    def latest(self, product_id):
        return None


def test_resync_polls_are_paced_and_the_feed_gaps_reach_the_daily_summary(tmp_path):
    """Final review B-3: a sequence gap asks for a REST poll; however often it asks, a resync poll runs at most once
    per POLL_ACTIVE_S (it forced one every second), and the daily summary shows the gap count."""
    hs = Harness(tmp_path, ENTRY_BAR + 1000)
    feed = GappyFeed()
    hs.rt = hs.make_runtime(feed=feed)
    hs.price(26_700.0)
    hs.rt.startup()
    polls = 0
    real = hs.rt.poll

    def counting(now):
        nonlocal polls
        polls += 1
        real(now)

    hs.rt.poll = counting
    for k in range(1, 61):
        hs.at(ENTRY_BAR + 1000 + k)
    assert 5 <= polls <= 7  # about every 10 s, not every second
    hs.rt.daily(ENTRY_BAR + 1100)
    daily = [t for k, t in hs.alerts.items if k == "DAILY"][-1]
    assert "순서 끊김" in daily and hs.journal.events(kind="WEBSOCKET_STATS")[-1]["data"]["gaps"] > 0
    hs.journal.close()


def test_each_late_step_of_the_minute_is_guarded(h):
    """Final review B minor 9: one step of the minute that keeps failing (here the feed check) never skips the daily
    summary, the audit or the stage-change exit after it."""
    def broken(now):
        raise RuntimeError("feed state unreadable")

    h.rt._feed_health = broken
    h.journal.set_stage("SHADOW", "demotion", ENTRY_BAR + 40)
    h.rt.minute(ENTRY_BAR + 100)
    assert h.journal.events(kind="LOOP_ERROR")[-1]["data"]["section"] == "feed"
    assert h.journal.get("once:daily") is not None and h.rt.exit_code == 0


def test_alert_wording_matches_what_happened(h):
    """Final review B minor 11: /resume says entries stay blocked under a SHADOW demotion; an unresolved order failure
    names what it does in this stage; three bad balance reads are not called a major incident."""
    entered(h)
    h.journal.set_stage("SHADOW", "demotion", ENTRY_BAR + 40)
    reply = h.rt.handle("/resume", ENTRY_BAR + 50)
    assert "SHADOW" in reply and "다음 신호부터 진입" not in reply
    h.journal.set_stage("FULL", "test", ENTRY_BAR + 60)
    assert "FULL에서는 기록만" in h.rt._unresolved_note() and "CANARY 기준 ② 실패" not in h.rt._unresolved_note()
    h.journal.set_stage("CANARY", "test", ENTRY_BAR + 70)
    assert "CANARY 기준 ② 실패" in h.rt._unresolved_note()
    h.ex.balance = lambda: Balance(math.nan, None)
    for k in range(3):
        h.rt.minute(ENTRY_BAR + 100 + 60 * k)
    texts = [t for k, t in h.alerts.items if k == "INCIDENT"]
    assert texts and not texts[-1].startswith("[중대 사건]")


def test_an_unreadable_account_without_exposure_is_alerted_after_fifteen_minutes(tmp_path):
    """Final review B-1: with nothing to manage, a Coinbase account that cannot be read is alerted after about 15
    minutes (3 with a trade, an order of unknown outcome or a flatten: test_chaos), and again when it reads."""
    hs = Harness(tmp_path, ENTRY_BAR + 1000)
    hs.price(26_700.0)
    hs.rt.startup()
    for m in ("position", "open_orders", "balance", "session"):
        hs.ex.fail_next(m, ExchangeError("HTTP 401: Unauthorized"), times=10_000)
    t0 = ENTRY_BAR + 1000
    for k in range(1, 18):
        hs.at(t0 + 60 * k)
        if k < 14:
            assert "ACCOUNT" not in hs.alerts.kinds()
    first = [t for k, t in hs.alerts.items if k == "ACCOUNT"]
    assert len(first) == 1 and first[0].startswith("[계좌 조회 불가]") and "HTTP 401" in first[0]
    hs.ex.clear_faults()
    hs.at(t0 + 60 * 19)
    assert [t for k, t in hs.alerts.items if k == "ACCOUNT"][-1].startswith("[계좌 조회 복구]")
    assert hs.journal.events(kind="ACCOUNT_READABLE")
    hs.journal.close()


# ----------------------------------------------------------------------------- final small round (re-reviews)


def test_the_feed_check_judges_a_tick_with_its_own_clock(tmp_path):
    """Re-review B (probe probe_feed_fresh): the feed check ran with the tick's `now`, read before the minute's earlier
    steps (a ping, the session and balance reads); a futures tick that arrived during those seconds looked "from the
    future" and the healthy feed was called down. It reads the clock itself."""
    hs = Harness(tmp_path, ENTRY_BAR + 1000)

    class LiveFeed:  # always a fresh futures price: one that arrived 0.2 s before it is read
        failures, last_error, gaps, connects = 0, None, 0, 1

        def latest(self, product_id):
            return Tick(60_000.0, hs.clock() - 0.2)

        def take_resync(self):
            return False

    hs.rt = hs.make_runtime(feed=LiveFeed())
    hs.price(26_700.0)
    hs.rt.startup()
    real = hs.ex.balance

    def slow():  # the minute's REST reads take 2 s of wall time
        hs.clock.now += 2.0
        return real()

    hs.ex.balance = slow
    t = ENTRY_BAR + 1060
    for _ in range(8):
        hs.clock.now = t
        hs.rt.minute(t)
        t += 60
    assert hs.rt.session.is_open and "FEED" not in hs.alerts.kinds()
    hs.journal.close()


class BlindReads(SimExchange):
    """The next `blind` position reads say flat."""

    blind = 0

    def position(self):
        p = super().position()
        if self.blind > 0:
            self.blind -= 1
            return None
        return p


def test_a_trade_closed_on_flat_reads_keeps_the_fast_polls_for_five_minutes(tmp_path):
    """Re-review A (probe pF 5b): two wrong flat snapshots in a row close the trade (the documented limit). The runtime
    then polled once a minute and the position stayed without its cancelled brackets for about 60 s; it keeps the
    10 s polls for OBSERVED_CLOSE_FAST_S after a close only the reads showed, so a wrong one is corrected at once."""
    hs = Harness(tmp_path, ENTRY_BAR + 30)
    hs.ex = BlindReads(clock=hs.clock)
    hs.rt = hs.make_runtime()
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    hs.ex.blind = 4  # two snapshots (two position reads each) say flat
    hs.at(ENTRY_BAR + 45)
    hs.at(ENTRY_BAR + 55)
    assert hs.rt.executor.trade().status == "CLOSED" and hs.rt.active
    s = next(s for s in range(1, 120) if hs.at(ENTRY_BAR + 55 + s) or hs.ex.open_orders())
    assert s <= 11 and covered(hs)
    hs.journal.close()


class RefusesNewOrders(SimExchange):
    """Order entry disabled while the session reads OPEN; `frozen`: the venue triggers nothing (matching paused)."""

    entry_disabled = False
    frozen = False

    def place_bracket(self, cid, side, size, limit_price, stop_price):
        if self.entry_disabled:
            self.calls.append("place_bracket")
            return PlaceResult(cid, None, False, "ORDER_ENTRY_DISABLED")
        return super().place_bracket(cid, side, size, limit_price, stop_price)

    def close_position(self, cid, size=None):
        if self.entry_disabled:
            self.calls.append("close_position")
            return PlaceResult(cid, None, False, "ORDER_ENTRY_DISABLED")
        return super().close_position(cid, size)

    def _match(self):
        return [] if self.frozen else super()._match()


def test_a_watchdog_close_never_cancels_a_leg_that_still_protects(tmp_path):
    """Re-review A (probe pG): FULL, 3 contracts, the TP2 leg gone and order entry disabled; the price through the stop
    the venue has not triggered yet. The any-refusal close applies to a gapped stop and to a trade with no leg resting
    only: here the TP1 leg still protects a contract and is kept."""
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=10, capped=3)
    hs.ex = RefusesNewOrders(clock=hs.clock)
    hs.rt = hs.make_runtime()
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    t = hs.rt.executor.trade()
    legs = {leg.name: leg.order_id for leg in t.legs}
    hs.ex.entry_disabled = True
    hs.ex.cancel([legs["tp2"]])
    hs.at(ENTRY_BAR + 45)
    assert hs.rt.executor.trade().unprotected_since is not None
    hs.ex.frozen = True
    hs.price(t.stop_price - 10)
    for k in range(4):
        hs.at(ENTRY_BAR + 60 + k)
    assert [o.order_id for o in hs.ex.open_orders()] == [legs["tp1"]] and hs.journal.events(kind="STOP_UNPROTECTED")
    assert not hs.journal.events(kind="CLOSE_CONFLICT")
    hs.journal.close()


class SlowCancels(SimExchange):
    """Cancels take `delay` seconds (CANCEL_QUEUED meanwhile); a close is refused while a bracket of ours rests."""

    delay = 8.0

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.leaving: dict[str, float] = {}

    def cancel(self, order_ids):
        self.calls.append("cancel")
        for oid in order_ids:
            self.leaving.setdefault(oid, self.clock())

    def open_orders(self):
        out = []
        for o in super().open_orders():
            since = self.leaving.get(o.order_id)
            if since is None:
                out.append(o)
            elif self.clock() - since < self.delay:
                out.append(dataclasses.replace(o, status="CANCEL_QUEUED"))
            else:
                SimExchange.cancel(self, [o.order_id])
        return out

    def close_position(self, client_order_id, size=None):
        if any(o.kind == "BRACKET" for o in self.open_orders()):
            self.calls.append("close_position")
            return PlaceResult(client_order_id, None, False, "OPEN_BRACKET_ORDERS")
        return super().close_position(client_order_id, size)


def test_a_gapped_stops_close_ends_when_cancels_take_longer_than_the_wait(tmp_path):
    """Re-review A (watchdog path): with cancels that take 8 s the close waited 5 s, the observing pass of the next
    attempt placed the cancelled legs again, the close was refused again - a loop that never closed. The legs the
    waiting close cancelled are not placed again before its next attempt: the position is closed."""
    hs = Harness(tmp_path, ENTRY_BAR + 30)
    hs.ex = SlowCancels(clock=hs.clock)
    hs.rt = hs.make_runtime()
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    t = hs.rt.executor.trade()
    placed = hs.ex.calls.count("place_bracket")
    for k in range(60):
        hs.price(t.stop_price * 0.93)  # past the stop's 5% limit
        hs.at(ENTRY_BAR + 60 + k)
        if hs.ex.position() is None:
            break
    assert hs.ex.position() is None and hs.ex.calls.count("place_bracket") == placed
    assert hs.rt.executor.trade().exit_reason in ("STOP_GAP", "STOP_UNPROTECTED")
    hs.journal.close()


def test_the_margin_rates_are_read_again_right_before_an_entry_is_sized(tmp_path):
    """The rates were read at the start (0.21025 long); the exchange raised them to 0.30 since. The entry re-reads the
    product: the cycle sizes with 0.30. One contract needs 0.01 * 26,747 * 0.30 = 80.24 USD, so the most that fits the 70%
    limit is floor(0.70 * 10,000 / 80.24) = floor(87.2) = 87 (124 with the start's rate)."""
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=60.0)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    reads = hs.ex.calls.count("product")
    hs.ex.spec = dataclasses.replace(hs.ex.spec, overnight_margin=(0.30, 0.30))
    hs.at(ENTRY_BAR + 30)
    assert hs.ex.calls.count("product") > reads and hs.rt.product.overnight_margin == (0.30, 0.30)
    assert hs.ex.position().contracts == 87
    assert margin_ratio(87, 26_747.0, 1, hs.rt.product, 10_000.0) <= MARGIN_ENTRY_MAX < margin_ratio(88, 26_747.0, 1, hs.rt.product, 10_000.0)
    hs.journal.close()


@pytest.mark.parametrize("stage", ["SHADOW", "CANARY", "FULL"])
def test_every_stage_reads_the_rates_again_before_sizing(tmp_path, stage):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage=stage, stage_days=60.0)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    reads = hs.ex.calls.count("product")
    hs.at(ENTRY_BAR + 30)
    assert hs.ex.calls.count("product") == reads + 1  # one re-read for the entry, none for the passes after it
    hs.at(ENTRY_BAR + 40)
    assert hs.ex.calls.count("product") == reads + 1
    hs.journal.close()


def test_a_higher_margin_rate_gives_fewer_contracts(tmp_path):
    """Equity 10,000, entry 26,740, stop 26,500: the 5x cap sizes 186 whatever the rates are; the 70% limit holds
    floor(7,000 / (0.01 * 26,740 * rate)) of them: rate 0.21025 -> floor(124.5) = 124; 0.25 -> floor(104.7) = 104;
    0.30 -> floor(87.26) = 87."""
    got = {}
    for k, rate in enumerate((0.21025, 0.25, 0.30)):
        n, block, hs = sized(tmp_path / str(k), "FULL", 10_000.0, overnight=(rate, rate))
        assert block is None
        got[rate] = n
        hs.journal.close()
    assert got == {0.21025: 124, 0.25: 104, 0.30: 87}


@pytest.mark.parametrize("bad", [Unavailable("429 or a timeout"), (0.0, 0.0), (0.21, float("nan")), (float("inf"), 0.25),
                                 (-0.2, 0.2), (0.21025, 0.0)])
def test_a_failed_or_invalid_refresh_refuses_the_entry_and_keeps_the_last_rates(tmp_path, bad):
    """Entering on stale rates is the risk: a failed read or a rate that is missing (reads 0), closed or not finite refuses
    this signal with a Korean reason (the SKIP alert); the watchdog keeps the last good product. The rates are fine the
    next pass, and the signal stays skipped (a skip is final: an entry is never sent later)."""
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=60.0)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    last = hs.rt.product
    if isinstance(bad, BaseException):
        hs.ex.fail_next("product", bad)
    else:
        hs.ex.spec = dataclasses.replace(hs.ex.spec, overnight_margin=bad)
    hs.at(ENTRY_BAR + 30)
    assert hs.ex.position() is None and hs.rt.product is last
    skip = hs.journal.events(kind="SKIP")
    assert len(skip) == 1 and "증거금률" in skip[0]["data"]["reason"] and "SKIP" in hs.alerts.kinds()
    hs.ex.clear_faults()
    hs.ex.spec = dataclasses.replace(hs.ex.spec, overnight_margin=last.overnight_margin)
    hs.at(ENTRY_BAR + 40)
    assert hs.ex.position() is None and hs.rt.product is not None
    hs.journal.close()


def test_the_watchdog_refreshes_the_rates_and_keeps_the_last_good_ones_when_a_refresh_fails(tmp_path):
    """An entry at the 70% limit (124 contracts, long rate 0.21025) is 70% of the account. The long rate rises to 0.26:
    the true ratio is 0.70 * 0.26 / 0.21025 = 86.6%. Within the refresh interval plus a minute the watchdog reads
    that (the 80% warning); a failing refresh keeps the last good rates and is alerted, limited."""
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=60.0)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    assert hs.ex.position().contracts == 124
    t = ENTRY_BAR + 60
    for k in range(1, 6):
        hs.at(t + 60 * k)
    assert "MARGIN" not in hs.alerts.kinds() and hs.rt.product.overnight_margin[0] == 0.21025
    raised = dataclasses.replace(hs.ex.spec, overnight_margin=(0.26, 0.26))
    hs.ex.spec = raised
    for k in range(6, 6 + int(runtime_mod.PRODUCT_REFRESH_S // 60) + 3):
        hs.at(t + 60 * k)
    assert hs.rt.product.overnight_margin == (0.26, 0.26)
    warn = [x for kind, x in hs.alerts.items if kind == "MARGIN"]
    assert len(warn) == 1 and "86%" in warn[0] and hs.journal.events(kind="MARGIN_RATES_CHANGED")
    t2 = t + 60 * (6 + int(runtime_mod.PRODUCT_REFRESH_S // 60) + 3)
    hs.ex.fail_next("product", Unavailable("down"), times=50)
    for k in range(1, 2 * int(runtime_mod.PRODUCT_REFRESH_S // 60) + 3):
        hs.at(t2 + 60 * k)
    assert hs.rt.product.overnight_margin == (0.26, 0.26)  # the last good rates stay in force
    failed = [x for kind, x in hs.alerts.items if kind == "MARGIN_RATE"]
    assert 1 <= len(failed) <= 2 and "마지막" in failed[0] and hs.journal.events(kind="PRODUCT_REFRESH_FAILED")
    hs.journal.close()


@pytest.mark.parametrize(("risk_mult", "needed", "pct"), [(1.0, 186, "3.3%"), (0.5, 186, "3.3%"), (0.4, 150, "3.3%")])
def test_the_reduced_entry_alert_states_the_real_risk_of_the_entered_contracts(tmp_path, risk_mult, needed, pct):
    """Equity 10,000, long 26,740, stop 26,500: distance 240 + 0.0005 * (26,740 + 26,500) = 266.62 USD per BTC, 2.6662 USD per
    contract. The 5x cap allows floor(50,000 / 26,740 / 0.01) = 186; the risk size at risk_mult m is
    floor(1,000 * m / 266.62 / 0.01): 375 at 1.0 and 187 at 0.5 (the cap binds: 186 either way), 150 at 0.4 (the risk binds).
    The 70% limit holds 124, so 124 enter: real risk 124 * 2.6662 / 10,000 = 3.306%. The old text said 10% * 124 / needed:
    6.7% (needed 186), 8.3% (150)."""
    n, block, hs = sized(tmp_path, "FULL", 10_000.0, risk_mult=risk_mult)
    assert (n, block) == (124, None) and hs.rt._entry_reduction["needed"] == needed
    hs.rt.executor.trade = lambda: SimpleNamespace(trade_id=SIGNAL, contracts=n)
    hs.rt._record_entry_reduction(hs.rt._entry_reduction, ENTRY_BAR + 31)
    text = [t for k, t in hs.alerts.items if k == "ENTRY_REDUCED"][0]
    assert f"기준 {needed}계약" in text and f"실제 위험은 약 {pct}" in text
    assert "6.7%" not in text and "8.3%" not in text
    hs.journal.close()


def _demote(hs: Harness, how: str, now: float) -> None:
    if how == "incident":
        hs.rt._incident("UNPROTECTED_60S", now)  # a major incident in FULL: paused and demoted to CANARY
    else:
        transition(hs.journal, "CANARY", now, [], confirm=True, flat=False)  # the CLI's demotion


@pytest.mark.parametrize("how", ["incident", "cli"])
@pytest.mark.parametrize("restart_first", [False, True])
def test_a_demotion_to_canary_at_a_loss_rebases_the_peak_and_trips_nothing(tmp_path, how, restart_first):
    """FULL with a position open (the process keeps running), peak 9,996.90 (the entry fee), equity 8,096.90 (-19%: FULL's
    30% and the daily 20% are fine). The demotion to CANARY (15%) leaves a request; the first balance read after it - in
    this process or after a restart - re-bases the peak to the read (journaled REBASED), so the next reads never trip
    TOTAL_DD and a /resume sent in between is not paused again."""
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=60.0)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    entered(hs)
    for k in range(3):
        hs.at(ENTRY_BAR + 60 * k + 90)  # the day's reference and the peak: three reads
    peak = hs.rt.guard.state()["peak"]
    assert peak == pytest.approx(10_000.0, abs=10.0)
    hs.ex.cash -= 1_900.0
    now = ENTRY_BAR + 300
    hs.at(now)
    _demote(hs, how, now + 1)
    assert hs.journal.stage()[0] == "CANARY"
    hs.rt.handle("/resume", now + 2)
    if restart_first:
        hs.rt = hs.make_runtime()  # CANARY's 15% from the start
        hs.rt.startup()
        assert hs.rt.guard.params.total_dd == 0.15 and hs.rt.guard.state()["peak"] == peak
    for k in range(1, 8):
        hs.at(now + 60 * k + 2)
    assert not hs.journal.events(kind="BREAKER") and paused(hs.journal) is None
    assert hs.ex.position() is not None  # nothing was flattened
    g = hs.rt.guard.state()
    assert g["peak"] == pytest.approx(peak - 1_900.0, abs=5.0) and not g["total_tripped"]
    rows = hs.journal.events(kind="REBASED")
    assert len(rows) == 1 and rows[0]["data"]["before"]["peak"] == peak and "FULL" in rows[0]["data"]["reason"]
    assert rows[0]["data"]["after"]["peak"] == pytest.approx(peak - 1_900.0, abs=5.0)
    assert hs.journal.get(runtime_mod.PEAK_REBASE_KEY) is None and "REBASE" in hs.alerts.kinds()
    hs.journal.close()


def test_a_canary_drawdown_from_the_rebased_peak_still_trips_the_breaker(tmp_path):
    """After the re-base (peak about 8,097) CANARY's 15% is real: equity 8,097 - 1,300 = 6,797 (-16%) breaches on two reads
    in a row and flattens, pauses and files the BREAKER incident as it always did."""
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=60.0)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    entered(hs)
    for k in range(3):
        hs.at(ENTRY_BAR + 60 * k + 90)
    hs.ex.cash -= 1_900.0
    hs.at(ENTRY_BAR + 300)
    hs.rt._incident("UNPROTECTED_60S", ENTRY_BAR + 301)
    hs.rt.handle("/resume", ENTRY_BAR + 302)
    hs.rt = hs.make_runtime()
    hs.rt.startup()
    for k in range(1, 6):
        hs.at(ENTRY_BAR + 300 + 60 * k)
    assert not hs.journal.events(kind="BREAKER") and hs.ex.position() is not None
    st = hs.rt.guard.state()
    assert st["peak"] == pytest.approx(8_097.0, abs=5.0)
    st.update(day_ref=st["peak"], day_reads=None)  # only the total drawdown is in play
    hs.journal.put(hs.rt.guard.KEY, st)
    hs.ex.cash -= 1_300.0
    hs.at(ENTRY_BAR + 660)
    assert paused(hs.journal) is None  # one breaching read is not enough
    hs.at(ENTRY_BAR + 720)
    assert paused(hs.journal)["reason"] == "breaker TOTAL_DD" and hs.ex.position() is None
    assert hs.journal.events(kind="BREAKER")[-1]["data"]["kind"] == "TOTAL_DD" and "BREAKER" in hs.alerts.kinds()
    hs.journal.close()


def test_a_breaker_trip_in_full_still_flattens_pauses_and_demotes_and_the_resume_rebases(tmp_path):
    """A TOTAL_DD trip in FULL (-30%): flatten, pause and the major incident as today (demoted to CANARY); the peak is
    re-based by the demotion's request at the next read and again by /resume (the tripped breaker's own re-base)."""
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=60.0)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    entered(hs)
    for k in range(3):
        hs.at(ENTRY_BAR + 60 * k + 90)
    hs.ex.cash -= 3_100.0  # -31%
    hs.at(ENTRY_BAR + 300)
    hs.at(ENTRY_BAR + 360)
    assert hs.ex.position() is None and paused(hs.journal)["reason"].startswith("breaker ")
    assert hs.journal.stage()[0] == "CANARY" and hs.journal.get(runtime_mod.PEAK_REBASE_KEY) is not None
    assert hs.rt.exit_code == 0  # flat and demoted: this process ends; the restart (CANARY) applies the request
    hs.rt = hs.make_runtime()
    hs.rt.startup()
    hs.at(ENTRY_BAR + 420)
    assert hs.journal.get(runtime_mod.PEAK_REBASE_KEY) is None and hs.rt.guard.state()["peak"] < 7_500.0
    assert hs.rt.handle("/resume", ENTRY_BAR + 430).startswith("재개")
    assert not hs.rt.guard.state()["total_tripped"]
    hs.journal.close()


# ---------------------------------------------------------------------------- a position read that lags a strategy entry
def lagged_entry(tmp_path, stage: str, refuse: bool = True, reads: int = 10_000) -> tuple[Harness, Lag]:
    """A strategy entry on a venue whose positions read does not show it yet. The probe's result before the fix: FULL's 124
    contracts with no stop for about 30 s, the journal saying CLOSED/BRACKET, and then a false ADOPTED."""
    hs = Harness(tmp_path / stage / str(refuse), ENTRY_BAR, stage=stage, stage_days=60.0)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 1)
    lag = Lag(hs, reads, refuse=refuse)
    hs.at(ENTRY_BAR + 30)
    return hs, lag


def real_stops(hs: Harness) -> int:
    return sum(o.size - o.filled for o in hs.ex.open_orders() if o.kind == "BRACKET" and o.stop_price is not None)


def assert_nothing_false(hs: Harness) -> None:
    """No false exit, no false adoption, no incident."""
    assert hs.journal.events(kind="CLOSED") == [] and hs.journal.events(kind="ADOPTED") == []
    assert hs.journal.events(kind="INCIDENT") == [] and paused(hs.journal) is None
    assert (hs.journal.get(RAISED_KEY) or {}).get("items", []) == []
    assert canary_failures(hs.journal, hs.journal.stage()[1]) == {}


@pytest.mark.parametrize("stage,contracts", [("CANARY", 1), ("FULL", 124)])
def test_a_strategy_entry_not_read_yet_is_kept_and_protected_as_soon_as_it_shows(tmp_path, stage, contracts):
    hs, lag = lagged_entry(tmp_path, stage)
    t = hs.rt.executor.trade()
    assert t.status == "OPEN" and hs.ex.pos.contracts == contracts and real_stops(hs) == 0
    for k in range(1, 4):  # read every second: not closed, nothing cancelled or entered
        hs.at(ENTRY_BAR + 30 + k)
        assert hs.rt.executor.trade().status == "OPEN" and real_stops(hs) == 0
    assert len(hs.journal.events(kind="POSITION_NOT_SEEN")) == 1 and hs.alerts.kinds().count("POSITION_NOT_SEEN") == 1
    assert hs.rt.active is True and hs.rt.flat() is False
    tries = len(hs.journal.events(kind="BRACKET_FAILED"))
    assert tries >= 2  # its stop kept being tried: Coinbase may take it before the read shows
    lag.left = 0
    hs.at(ENTRY_BAR + 34)  # the first pass that reads it places the stop
    assert real_stops(hs) == contracts
    assert hs.rt.executor.entry_unseen(hs.rt.executor.trade()) is False
    for k in range(5, 8):
        hs.at(ENTRY_BAR + 30 + 10 * k)
    assert real_stops(hs) == contracts and hs.ex.position().contracts == contracts
    assert hs.rt.executor.trade().unprotected_since is None  # the next pass reads it covered
    assert_nothing_false(hs)
    assert hs.journal.events(kind="ORDER_UNRESOLVED") == []
    hs.journal.close()


@pytest.mark.parametrize("stage,contracts", [("CANARY", 1), ("FULL", 124)])
def test_a_bracket_coinbase_takes_while_the_read_lags_is_kept_not_doubled(tmp_path, stage, contracts):
    hs, lag = lagged_entry(tmp_path, stage, refuse=False)
    placed = hs.ex.open_orders()
    assert real_stops(hs) == contracts
    for k in range(1, 4):
        hs.at(ENTRY_BAR + 30 + k)
    assert [o.order_id for o in hs.ex.open_orders()] == [o.order_id for o in placed]  # never cancelled as FLAT, never again
    lag.left = 0
    hs.at(ENTRY_BAR + 34)
    assert [o.order_id for o in hs.ex.open_orders()] == [o.order_id for o in placed]
    assert_nothing_false(hs)
    hs.journal.close()


def test_a_short_lag_is_nothing_but_one_more_read(tmp_path):
    hs, _ = lagged_entry(tmp_path, "CANARY", reads=1)  # the entry's own read did not show it
    assert hs.rt.executor.entry_unseen(hs.rt.executor.trade()) is True
    hs.at(ENTRY_BAR + 31)
    assert hs.rt.executor.entry_unseen(hs.rt.executor.trade()) is False and real_stops(hs) == 1
    assert hs.journal.events(kind="POSITION_NOT_SEEN") == []
    assert_nothing_false(hs)
    hs.journal.close()


def test_an_entry_the_read_shows_at_once_is_never_held(tmp_path):
    """No lag: as before the fix, a flat read after it is believed at once when its brackets do not contradict it."""
    hs = Harness(tmp_path, ENTRY_BAR, stage="CANARY", stage_days=60.0)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    assert hs.rt.executor.entry_unseen(hs.rt.executor.trade()) is False
    hs.journal.close()


def test_a_leg_that_filled_explains_the_flat_read_at_once(tmp_path):
    hs, _ = lagged_entry(tmp_path, "CANARY", refuse=False)
    stop = next(o.stop_price for o in hs.ex.open_orders())
    hs.ex.set_prices(stop - 1.0)  # stopped out while the positions read still lags
    hs.at(ENTRY_BAR + 31)
    t = hs.rt.executor.trade()
    assert (t.status, t.exit_reason) == ("CLOSED", "BRACKET") and hs.journal.events(kind="POSITION_NOT_SEEN") == []
    assert hs.journal.events(kind="ADOPTED") == [] and paused(hs.journal) is None
    hs.journal.close()


def test_an_entry_that_never_shows_is_given_up_after_5_minutes_and_says_so(tmp_path):
    hs, lag = lagged_entry(tmp_path, "CANARY", refuse=False)
    opened = hs.rt.executor.trade().opened_ts
    now = ENTRY_BAR + 30
    while now < opened + ENTRY_SHOW_MAX_S - 10:
        now += 10
        hs.at(now)
        assert hs.rt.executor.trade().status == "OPEN" and hs.journal.events(kind="CLOSED") == []
    hs.at(opened + ENTRY_SHOW_MAX_S + 1)
    t = hs.rt.executor.trade()
    assert (t.status, t.exit_reason) == ("CLOSED", ENTRY_NOT_SEEN) and paused(hs.journal) is not None
    assert any("나타나지 않음" in text for _, text in hs.alerts.items)
    assert hs.journal.events(kind="CLOSED")[-1]["data"]["reason"] == ENTRY_NOT_SEEN  # never called a bracket's exit
    hs.at(opened + ENTRY_SHOW_MAX_S + 12)
    assert hs.ex.open_orders() == []  # the wait is over: what rests on a book read flat is cancelled
    hs.journal.close()


def test_a_restart_keeps_the_entry_it_has_not_seen(tmp_path):
    hs, lag = lagged_entry(tmp_path, "FULL")
    hs.clock.now = ENTRY_BAR + 60
    hs.rt = hs.make_runtime()
    assert hs.rt.active is True
    hs.rt.startup()
    assert hs.rt.executor.trade().status == "OPEN" and hs.journal.events(kind="CLOSED") == []
    lag.left = 0
    hs.at(ENTRY_BAR + 71)
    assert real_stops(hs) == 124 and hs.journal.events(kind="ADOPTED") == []
    hs.journal.close()


def test_a_close_of_an_entry_not_seen_records_no_exit(tmp_path):
    """The watchdog's and any other close reach `Executor.close`, whose flat branch would call this a bracket's exit."""
    hs, lag = lagged_entry(tmp_path, "CANARY")
    assert hs.rt.executor.close("STOP_GAP", hs.rt._ctx(ENTRY_BAR + 31)) is False
    assert hs.rt.executor.trade().status == "OPEN" and hs.journal.events(kind="CLOSED") == []
    lag.left = 0
    assert hs.rt.executor.close("STOP_GAP", hs.rt._ctx(ENTRY_BAR + 32)) is True
    assert hs.ex.pos is None and hs.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "STOP_GAP"
    hs.journal.close()


def test_a_flatten_stands_through_the_lag_and_closes_when_it_shows(tmp_path):
    hs, lag = lagged_entry(tmp_path, "FULL")
    hs.rt._flatten("USER_FLATTEN", "USER", "user /flatten", ENTRY_BAR + 31)
    assert hs.journal.get(FLATTEN_KEY) is not None and hs.rt.executor.trade().status == "OPEN"
    hs.at(ENTRY_BAR + 32)
    assert hs.journal.get(FLATTEN_KEY) is not None
    lag.left = 0
    hs.at(ENTRY_BAR + 33)
    assert hs.ex.pos is None and hs.journal.get(FLATTEN_KEY) is None
    assert hs.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "USER_FLATTEN"
    assert hs.journal.events(kind="ADOPTED") == []
    hs.journal.close()


def two_legs(tmp_path, refuse: bool = False) -> tuple[Harness, Lag]:
    """FULL at 4 contracts: a 2-contract TP1 leg and a 2-contract TP2 leg, the position read lagging."""
    hs = Harness(tmp_path, ENTRY_BAR, stage="FULL", stage_days=10, capped=4)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 1)
    lag = Lag(hs, 10_000, refuse=refuse)
    hs.at(ENTRY_BAR + 30)
    return hs, lag


def test_one_filled_leg_with_another_resting_explains_nothing(tmp_path):
    """Re-review M1: TP1 filled while the read lags, and the trade closed as BRACKET: the planner cancelled TP2 and the stop,
    leaving 2 real contracts with no orders."""
    hs, lag = two_legs(tmp_path)
    t = hs.rt.executor.trade()
    assert [(leg.name, leg.size) for leg in t.legs] == [("tp1", 2), ("tp2", 2)]
    hs.ex.set_prices(next(leg for leg in t.legs if leg.name == "tp1").limit_price + 1.0)
    assert hs.ex.pos.contracts == 2
    hs.at(ENTRY_BAR + 31)
    assert hs.rt.executor.trade().status == "OPEN" and hs.journal.events(kind="CLOSED") == []
    assert real_stops(hs) == 2 and hs.journal.events(kind="UNPROTECTED") == []  # what is left is covered
    assert hs.rt.executor.close("STOP_GAP", hs.rt._ctx(ENTRY_BAR + 32)) is False  # the close's flat branch agrees
    lag.left = 0
    hs.at(ENTRY_BAR + 33)
    t = hs.rt.executor.trade()
    assert t.status == "OPEN" and t.tp1_filled and hs.ex.position().contracts == 2 and real_stops(hs) == 2
    assert hs.journal.events(kind="ADOPTED") == [] and (hs.journal.get(RAISED_KEY) or {}).get("items", []) == []
    hs.journal.close()


def test_every_leg_gone_with_a_fill_is_a_bracket_exit(tmp_path):
    hs, _ = two_legs(tmp_path)
    t = hs.rt.executor.trade()
    hs.ex.set_prices(t.stop_price - 1.0)  # both legs stopped out while the read lags
    assert hs.ex.pos is None
    hs.at(ENTRY_BAR + 31)
    t = hs.rt.executor.trade()
    assert (t.status, t.exit_reason) == ("CLOSED", "BRACKET")
    hs.journal.close()


@pytest.mark.parametrize("stage", ["CANARY", "FULL"])
def test_no_stop_during_the_wait_counts_as_unprotected(tmp_path, stage):
    """Re-review M2: the unprotected clock, the major incident and FULL's demotion stayed silent through a hold."""
    hs, lag = lagged_entry(tmp_path, stage)
    hs.at(ENTRY_BAR + 31)
    assert hs.rt.executor.trade().unprotected_since is not None and hs.journal.events(kind="UNPROTECTED")
    assert hs.rt._stop_waits() is True  # a stop request gives the passes that place it their time
    now = float(ENTRY_BAR + 31)
    while now < ENTRY_BAR + 31 + 65:
        now += 10
        hs.at(now)
    assert hs.journal.events(kind="UNPROTECTED_60S") and paused(hs.journal) is not None
    assert hs.journal.stage()[0] == "CANARY"  # FULL demoted: the next order at once, this position as it is
    t = hs.rt.executor.trade()
    assert t.status == "OPEN" and hs.ex.pos is not None and hs.rt._ctx(now).max_contracts == 1
    lag.left = 0
    hs.at(now + 10)
    assert real_stops(hs) == t.contracts  # protected when it shows, at its real size
    hs.journal.close()


def test_stops_that_rest_through_the_wait_raise_no_alarm(tmp_path):
    hs, _ = lagged_entry(tmp_path, "FULL", refuse=False)
    for k in range(1, 8):
        hs.at(ENTRY_BAR + 30 + 10 * k)
    assert hs.rt.executor.trade().unprotected_since is None and hs.journal.events(kind="UNPROTECTED") == []
    assert hs.rt._stop_waits() is False and paused(hs.journal) is None and hs.journal.stage()[0] == "FULL"
    hs.journal.close()


def test_a_stop_during_the_wait_says_so_and_a_price_through_the_stop_sends_no_blind_close(tmp_path):
    hs, _ = lagged_entry(tmp_path, "CANARY")
    hs.at(ENTRY_BAR + 31)
    stop = hs.rt.executor.trade().stop_price
    hs.rt.stop_requested = True
    hs.rt.stop_since = hs.rt.mono() - STOP_GRACE_S - 1.0  # its grace is over
    assert hs.rt.run_forever() == 0
    said = [t for k, t in hs.alerts.items if k == "STOPPED"]
    assert said and "거래소 손절 주문이 없음" in said[-1] and "보호 주문은 그대로" not in said[-1]
    hs.journal.close()
    hs, _ = lagged_entry(tmp_path / "price", "CANARY")
    hs.at(ENTRY_BAR + 31)
    calls = len(hs.ex.calls)
    hs.ex.set_prices(stop - 50.0)
    hs.at(ENTRY_BAR + 42)
    assert hs.journal.events(kind="STOP_UNPROTECTED") and "close_position" not in hs.ex.calls[calls:]
    assert hs.rt.executor.trade().status == "OPEN" and hs.journal.events(kind="CLOSED") == []
    said = [t for k, t in hs.alerts.items if k == "STOP_UNPROTECTED"]
    assert said and "읽히지 않아" in said[-1]
    hs.journal.close()


def test_the_entrys_position_read_never_holds_back_its_first_stop(tmp_path):
    """Re-review L1: three timeouts and their back-off on the read before the stop made a real PROTECT_SLOW."""
    hs = Harness(tmp_path, ENTRY_BAR, stage="CANARY", stage_days=10)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 1)
    market, position, state = hs.ex.place_market, hs.ex.position, {"armed": False}

    def m(*a):
        out = market(*a)
        state["armed"] = True
        return out

    def p():
        if state["armed"]:
            state["armed"] = False
            hs.clock.now += 33.0
        return position()

    hs.ex.place_market, hs.ex.position = m, p
    hs.at(ENTRY_BAR + 30)
    assert [e["data"]["after_entry_s"] for e in hs.journal.events(kind="PROTECTED_FIRST")] == [0.0]
    hs.at(hs.clock.now + 11)
    assert "PROTECT_SLOW" not in canary_failures(hs.journal, hs.journal.stage()[1])
    hs.journal.close()


def test_a_failed_first_stop_still_marks_the_entry_not_shown(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR, stage="CANARY", stage_days=10)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 1)
    Lag(hs, 10_000)
    real = hs.ex.open_orders

    def fails_once_filled():
        if hs.ex.pos is not None:
            raise Unavailable("503")
        return real()

    hs.ex.open_orders = fails_once_filled
    hs.at(ENTRY_BAR + 30)
    assert hs.journal.events(kind="ENTRY") and hs.journal.events(kind="ACTION_ERROR")
    assert hs.rt.executor.entry_unseen(hs.rt.executor.trade()) is True
    hs.journal.close()


def test_a_clock_behind_coinbase_is_no_expired_wait(tmp_path):
    """Re-review L2: `created_time` ahead of this clock read as a negative age, which counted as expired: given up at once."""
    hs = Harness(tmp_path, ENTRY_BAR, stage="CANARY", stage_days=10)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 1)
    real = hs.ex.get_order

    def ahead(order_id):
        o = real(order_id)
        return dataclasses.replace(o, created_ts=hs.clock.now + 5.0) if o.client_order_id.startswith("ewb-e-") else o

    hs.ex.get_order = ahead
    lag = Lag(hs, 10_000)
    hs.at(ENTRY_BAR + 30)
    hs.at(hs.clock.now + 1)
    assert hs.rt.executor.trade().status == "OPEN" and paused(hs.journal) is None
    assert hs.rt.executor.entry_unseen(hs.rt.executor.trade()) and lag.left > 0
    hs.journal.close()


def test_a_locked_journal_in_the_ticks_cadence_read_does_not_end_the_process(tmp_path):
    """Re-review L4: this read sat outside the sections' guard, and `database is locked` would have ended the process."""
    hs, _ = lagged_entry(tmp_path, "CANARY")

    def locked():
        raise sqlite3.OperationalError("database is locked")

    hs.rt.executor.trade = locked
    hs.clock.now += 1
    hs.rt.tick()  # survives: the failing sections are guarded, and the cadence falls back to the normal poll
    hs.journal.close()


@pytest.mark.parametrize("with_target", [False, True])
def test_a_position_that_shows_after_the_wait_is_claimed_and_protected(tmp_path, with_target):
    """Re-review L5: with no target it got an alert only, no stop. Within ENTRY_CLAIM_MAX_S it is that entry's again."""
    hs, lag = lagged_entry(tmp_path, "CANARY", refuse=False)
    opened = hs.rt.executor.trade().opened_ts
    hs.at(opened + ENTRY_SHOW_MAX_S + 1)
    assert hs.rt.executor.trade().exit_reason == ENTRY_NOT_SEEN
    hs.at(opened + ENTRY_SHOW_MAX_S + 12)  # what rested was cancelled: the book read flat
    assert hs.ex.open_orders() == [] and hs.ex.pos is not None
    if not with_target:
        hs.rt.target = None
    lag.left = 0
    hs.at(opened + ENTRY_SHOW_MAX_S + 72)
    t = hs.rt.executor.trade()
    assert t.status == "OPEN" and t.exit_reason is None and real_stops(hs) == 1
    assert any(text.startswith("[경고]") and "늦게 나타남" in text for _, text in hs.alerts.items)
    assert hs.journal.events(kind="ADOPTED") == [] and (hs.journal.get(RAISED_KEY) or {}).get("items", []) == []
    assert not {"UNKNOWN_POSITION", "ADOPTED"} & set(canary_failures(hs.journal, hs.journal.stage()[1]))
    assert paused(hs.journal) is not None  # the pause stays the user's to lift
    hs.journal.close()


def test_a_position_that_shows_past_the_claim_is_a_foreign_one_as_before(tmp_path):
    hs, lag = lagged_entry(tmp_path, "CANARY", refuse=False)
    opened = hs.rt.executor.trade().opened_ts
    hs.at(opened + ENTRY_SHOW_MAX_S + 1)
    hs.rt.target = None
    lag.left = 0
    hs.at(opened + ENTRY_CLAIM_MAX_S + 1)
    assert hs.journal.events(kind="ENTRY_SHOWN_LATE") == []
    # read as anything else the journal does not know: the hour's cycle in this tick adopts it, the engine holding that side
    assert hs.journal.events(kind="ADOPTED") or (hs.journal.get(RAISED_KEY) or {}).get("items")
    hs.journal.close()


def ended_late(hs: Harness, after: float) -> None:
    """Our IOC entry reads open for `after` seconds from its first read, unfilled, and has ended with nothing since."""
    hs.ex.market_fill_ratio = 0.0
    real, start = hs.ex.get_order, {"t": None}

    def late(order_id):
        o = real(order_id)
        if o.client_order_id.startswith("ewb-e-"):
            start["t"] = hs.clock.now if start["t"] is None else start["t"]
            if hs.clock.now - start["t"] <= after:
                return dataclasses.replace(o, status="OPEN")
        return o

    hs.ex.get_order = late


def test_a_strategy_entry_that_ends_unfilled_settles_at_the_next_read(tmp_path):
    """Finding 8 again, on the strategy path: it stayed "unsettled" for up to 2 hours (fast polls, no flat book, so no stage
    change either), after an IOC that read open past the 10 s wait and then ended with nothing."""
    hs = Harness(tmp_path, ENTRY_BAR, stage="CANARY", stage_days=60.0)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 1)
    ended_late(hs, hs.rt.executor.fill_timeout)
    hs.at(ENTRY_BAR + 30)
    assert hs.rt.executor.unsettled_entry(hs.clock.now) is not None and hs.rt._busy() is True  # may still fill: watched
    assert [e["data"]["terminal"] for e in hs.journal.events(kind="ENTRY_UNFILLED")] == [False]
    hs.at(hs.clock.now + 11)
    assert hs.rt.executor.unsettled_entry(hs.clock.now) is None and hs.rt._busy() is False and hs.rt.flat() is True
    assert [e["data"]["terminal"] for e in hs.journal.events(kind="ENTRY_UNFILLED")] == [False, True]
    assert any(text.startswith("[진입 미체결 확정]") for _, text in hs.alerts.items)
    n = len(hs.journal.events(kind="ENTRY_UNFILLED"))
    hs.at(hs.clock.now + 11)
    assert len(hs.journal.events(kind="ENTRY_UNFILLED")) == n and hs.ex.position() is None
    hs.journal.close()


def test_an_entry_still_open_stays_watched_and_one_that_fills_late_is_ours(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR, stage="CANARY", stage_days=60.0)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 1)
    ended_late(hs, 1_000.0)
    hs.at(ENTRY_BAR + 30)
    hs.at(hs.clock.now + 11)
    assert hs.rt.executor.unsettled_entry(hs.clock.now) is not None  # it has not ended: not settled by the read
    assert [e["data"]["terminal"] for e in hs.journal.events(kind="ENTRY_UNFILLED")] == [False]
    hs.journal.close()


# ---------------------------------------------------------------------------- close_position's 404 (2026-11-06)
def opened(tmp_path, stage: str = "CANARY") -> Harness:
    hs = Harness(tmp_path, ENTRY_BAR, stage=stage, stage_days=60.0)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    assert hs.ex.position() is not None and hs.ex.open_orders()
    return hs


def close_via(hs: Harness, path: str) -> None:
    now = ENTRY_BAR + 40
    hs.clock.now = float(now)
    if path == "flatten":
        hs.rt._flatten("USER_FLATTEN", "USER", "user /flatten", now)
    elif path == "watchdog":
        assert hs.rt.executor.close("STOP_GAP", hs.rt._ctx(now), any_refusal=True) is True
    elif path == "planned":
        hs.rt.executor.execute([Close("ENGINE_FLAT")], hs.rt._ctx(now))
    elif path == "flip":
        t = hs.rt.executor.trade()
        flip = TargetEntry(ENTRY_BAR, "W3", -t.direction, 26_740.0, 27_500.0, 26_000.0, 25_500.0, 1.0, 1, "flip")
        hs.rt.executor.execute([Close("FLIP"), Enter(flip, 1)], hs.rt._ctx(now))


@pytest.mark.parametrize("path", ["flatten", "watchdog", "planned", "flip"])
def test_every_close_path_takes_a_not_found_for_the_close_not_seeing_the_position_yet(tmp_path, path):
    hs = opened(tmp_path / path)
    side = hs.ex.position().side
    as_on_2026_11_06(hs, not_found=1)
    close_via(hs, path)
    closed = hs.journal.events(kind="CLOSED")
    assert closed and closed[-1]["data"]["reason"] in ("USER_FLATTEN", "STOP_GAP", "ENGINE_FLAT", "FLIP")
    assert hs.journal.events(kind="CLOSE_NOT_FOUND") and hs.journal.events(kind="CLOSE_FAILED") == []
    if path == "flip":
        assert hs.ex.position().side == -side  # the entry after it went out
    else:
        assert hs.ex.position() is None
    hs.journal.close()


def test_a_not_found_the_retries_do_not_end_re_protects_and_counts_as_a_failed_close(tmp_path):
    hs = opened(tmp_path)
    as_on_2026_11_06(hs, not_found=1000)
    ctx = hs.rt._ctx(ENTRY_BAR + 40)
    start = hs.clock.now
    assert hs.rt.executor.close("ENGINE_FLAT", ctx) is False
    assert hs.ex.position() is not None and [o.kind for o in hs.ex.open_orders()] == ["BRACKET"]  # the stop is back
    assert len(hs.journal.events(kind="CLOSE_FAILED")) == 1 and hs.journal.events(kind="CLOSE_CONFLICT")
    assert hs.clock.now - start <= 15  # bounded: the retries and one cancel-and-close
    hs.journal.close()


def test_a_not_found_with_the_position_read_flat_is_the_flat_handling(tmp_path):
    hs = opened(tmp_path)
    as_on_2026_11_06(hs, not_found=1)
    real = hs.ex.close_position

    def gone(cid, size=None):
        hs.ex.set_prices(next(o.stop_price for o in hs.ex.open_orders()) - 1.0)  # its stop fills meanwhile
        return real(cid, size)

    hs.ex.close_position = gone
    assert hs.rt.executor.close("ENGINE_FLAT", hs.rt._ctx(ENTRY_BAR + 40)) is True
    t = hs.rt.executor.trade()
    assert (t.status, t.exit_reason) == ("CLOSED", "BRACKET") and hs.journal.events(kind="CLOSE_FAILED") == []
    hs.journal.close()


def test_a_close_with_a_resting_bracket_on_this_venue_closes_in_one_call(tmp_path):
    """No 404, the venue only refusing the close while the bracket rests (seen 2026-11-06): one close() does it."""
    hs = opened(tmp_path)
    as_on_2026_11_06(hs, not_found=0)
    assert hs.rt.executor.close("ENGINE_FLAT", hs.rt._ctx(ENTRY_BAR + 40)) is True and hs.ex.position() is None
    assert len(hs.journal.events(kind="CLOSE_CONFLICT")) == 1 and hs.journal.events(kind="CLOSE_FAILED") == []
    hs.journal.close()
