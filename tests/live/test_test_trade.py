"""`ewbot live test-trade` (user decision 2026-11-05): one 1-contract round trip in CANARY, through the same order path as
a strategy entry, run by the running runtime on its own loop. Fake exchange only: nothing here reaches Coinbase, and nothing
here proves what Coinbase itself does (docs/live-trading.md lists what only the real run can show)."""

from __future__ import annotations

import dataclasses
import json
import signal
import threading

import pytest

from ewbot import cli
from ewbot.cli import main
from ewbot.live import app as live_app
from ewbot.live import runtime as rt_mod
from ewbot.live.audit import AuditResult
from ewbot.live.exchange import ExchangeError, KeyPermissions, PlaceResult, Unavailable
from ewbot.live.runtime import FLATTEN_KEY
from ewbot.live.settings import LiveSettings, SettingsError
from ewbot.live.sim import SimCrash
from ewbot.live.stage import (
    RAISED_KEY,
    TEST_ACTIVE_KEY,
    TEST_IDS_KEY,
    TEST_REQUEST_KEY,
    TEST_TRADE,
    TEST_TRADE_MARGIN,
    TEST_TRADE_REFUSED,
    canary_failures,
    canary_gates,
    full_entry_gates,
    gate_lines,
    paused,
    restart_stint,
    resume,
    transition,
)
from ewbot.live.target import Target
from ewbot.types import H1
from tests.live.harness import Harness
from tests.live.helpers import Lag, as_on_2026_11_06, entry, position
from tests.paper.helpers import ts

QUIET = ts("2024-02-01 10:00")  # the synthetic candles' engine is flat and queues nothing around here
KEY = KeyPermissions(True, True, False)
PAPER_OK = {"c1": True, "c2": True, "c3": False, "count": {"total": 36, "required": 50},
            "c4": {"met": True, "days": 30.0, "coverage": 0.99, "unexplained": 0}}
SUMMARY = {
    "total_usd_balance": {"value": "9999.00", "currency": "USD"},  # not one of the journaled fields
    "liquidation_threshold": {"value": "325.90", "currency": "USD"},
    "liquidation_buffer_amount": {"value": "4687.15", "currency": "USD"},
    "liquidation_buffer_percentage": "1438",
    "initial_margin": {"value": "651.80", "currency": "USD"},
    "available_margin": {"value": "4361.25", "currency": "USD"},
    "intraday_margin_window_measure": {"margin_window_type": "FCM_MARGIN_WINDOW_TYPE_INTRADAY",
                                       "margin_level": "MARGIN_LEVEL_TYPE_BASE", "initial_margin": "100.5",
                                       "maintenance_margin": "50.25", "liquidation_buffer": "4900"},
    "overnight_margin_window_measure": {"margin_window_type": "FCM_MARGIN_WINDOW_TYPE_OVERNIGHT",
                                        "margin_level": "MARGIN_LEVEL_TYPE_BASE", "initial_margin": "210.25",
                                        "maintenance_margin": "105.125", "liquidation_buffer": "4800"},
}
FIELDS = ("liquidation_threshold", "liquidation_buffer_amount", "liquidation_buffer_percentage", "initial_margin",
          "available_margin", "intraday_margin_window_measure", "overnight_margin_window_measure")


def started(tmp_path, *, name: str = "journal.sqlite", **kw) -> Harness:
    hs = Harness(tmp_path, QUIET + 30, stage=kw.pop("stage", "CANARY"), stage_days=kw.pop("stage_days", 1.0),
                 journal_name=name, fee_per_contract=0.15, **kw)
    hs.price(float(hs.bar(QUIET)["open"]))
    hs.ex.summary = dict(SUMMARY)
    hs.rt.startup()
    hs.at(QUIET + 30)  # the hour's cycle
    assert hs.rt.target is not None and hs.rt.target.entry is None and hs.rt.target.position is None
    hs.clock.now = float(QUIET + 600)
    return hs


@pytest.fixture
def h(tmp_path):
    hs = started(tmp_path)
    yield hs
    hs.journal.close()


def ask(hs: Harness, side: int = 1, at: float | None = None) -> None:
    """The CLI's request, then one tick of the running runtime."""
    at = hs.clock.now + 1 if at is None else float(at)
    hs.clock.now = at
    hs.journal.put(TEST_REQUEST_KEY, {"id": f"r{int(at)}", "side": side, "ts": at})
    hs.at(at)


def result(hs: Harness) -> dict:
    return hs.journal.events(kind=TEST_TRADE)[-1]["data"]


def ticks(hs: Harness, n: int = 10, step: float = 10.0) -> None:
    for _ in range(n):
        hs.at(hs.clock.now + step)


def assert_nothing_false(hs: Harness) -> None:
    """No breaker, no manual intervention, no incident, no pause: the runtime took none of it for something else."""
    since = hs.journal.stage()[1]
    assert canary_failures(hs.journal, since) == {}
    assert hs.journal.events(kind="INCIDENT") == [] and hs.journal.events(kind="BREAKER") == []
    assert hs.journal.events(kind="ADOPTED") == [] and paused(hs.journal) is None
    assert (hs.journal.get(RAISED_KEY) or {}).get("items", []) == []
    assert hs.journal.stage()[0] == "CANARY"


# ---------------------------------------------------------------------------- the happy path
@pytest.mark.parametrize("side", [1, -1])
def test_a_round_trip_through_the_real_order_path(h, side):
    before = len(h.ex.fills)
    ask(h, side)
    r = result(h)
    assert r["ok"] is True and r["step"] == "close" and r["why"] is None and r["position"] == "flat"
    assert (r["side"], r["contracts"]) == (side, 1)
    fills = h.ex.fills[before:]
    assert [(f["kind"], f["side"], f["size"]) for f in fills] == [("MARKET", side, 1), ("MARKET", -side, 1)]
    # the same client order ids, intents and events as a strategy trade: no parallel order path
    tid = r["trade_id"]
    assert sorted(i["kind"] for i in h.journal.intents(trade_id=tid)) == ["BRACKET", "CLOSE", "ENTRY"]
    side_letter = "L" if side > 0 else "S"
    assert h.journal.intent(f"ewb-e-{tid}-{side_letter}")["status"] == "ACCEPTED"
    assert h.journal.intent(f"ewb-b-{tid}-tp1-1")["status"] == "ACCEPTED"
    assert h.journal.intent(f"ewb-x-{tid}-1")["payload"]["reason"] == "TEST_TRADE"
    assert h.journal.events(kind="PROTECTED_FIRST")[-1]["data"]["trade_id"] == tid
    # the detail the gates need
    assert r["entry_price"] == fills[0]["price"] and r["close_price"] == fills[1]["price"]
    assert r["protect_s"] == pytest.approx(0.0) and r["fees"] == pytest.approx(0.30)
    assert (r["entry_fee"], r["close_fee"]) == (pytest.approx(0.15), pytest.approx(0.15))
    # the stop really is a stop on the loss side, on the tick, about 2% away; nothing rests and nothing is held
    stop, tp = r["stop"], r["take_profit"]
    assert side * (r["entry_price"] - stop) > 0 and side * (tp - r["entry_price"]) > 0 and stop % 5.0 == 0
    assert abs(r["entry_price"] - stop) / r["entry_price"] == pytest.approx(0.02, abs=0.002)
    assert h.ex.position() is None and h.ex.open_orders() == []
    assert h.rt.executor.trade().status == "CLOSED" and h.journal.get(TEST_ACTIVE_KEY) is None
    assert h.journal.get(TEST_REQUEST_KEY) is None and h.journal.get(TEST_IDS_KEY) == [tid]
    assert any(k == TEST_TRADE and t.startswith("[시험 거래 성공]") for k, t in h.alerts.items)
    assert_nothing_false(h)


def test_the_first_real_runs_close_answers_pass(h):
    """2026-11-06, live-v6: 404 "no positions found" 1.4 s after the fill, then the close conflict while the bracket rested,
    then the close after the cancel. The stop was verified and the account ends flat with both fills read: a pass."""
    as_on_2026_11_06(h, not_found=1)
    ask(h)
    r = result(h)
    assert r["ok"] is True and r["position"] == "flat" and r["close_price"] and r["close_fee"] == pytest.approx(0.15)
    assert [e["data"]["reason"] for e in h.journal.events(kind="CLOSED")] == ["TEST_TRADE"]
    assert len(h.journal.events(kind="CLOSE_NOT_FOUND")) == 1 and len(h.journal.events(kind="CLOSE_CONFLICT")) == 1
    assert h.journal.events(kind="CLOSE_FAILED") == [] and h.journal.get(FLATTEN_KEY) is None and paused(h.journal) is None
    assert h.ex.position() is None and h.ex.open_orders() == []
    ticks(h, 3, 61.0)
    assert_nothing_false(h)
    assert h.journal.events(kind="ORDER_UNRESOLVED") == []
    assert all(g.met for g in full_gates(h, h.clock.now + 60)), gate_lines(full_gates(h, h.clock.now + 60))


@pytest.mark.parametrize("not_found", [2, 3, 4])
def test_a_close_that_keeps_finding_no_position_is_retried_then_cancels_and_closes(h, not_found):
    as_on_2026_11_06(h, not_found=not_found)
    ask(h)
    assert result(h)["ok"] is True and h.ex.position() is None and h.journal.events(kind="CLOSE_FAILED") == []
    assert len(h.journal.events(kind="CLOSE_NOT_FOUND")) == not_found  # every one seen, none taken for a failure


def test_a_close_that_never_finds_the_position_fails_with_the_stop_back(h):
    """A real failure stays one: the stop goes back on at once and the close stands."""
    as_on_2026_11_06(h, not_found=1000)
    ask(h)
    r = result(h)
    assert r["ok"] is False and r["step"] == "close" and r["position"] == "protected"
    assert h.ex.position() is not None and [o.kind for o in h.ex.open_orders()] == ["BRACKET"]
    assert h.journal.get(FLATTEN_KEY)["reason"] == "TEST_TRADE_FAILED"
    h.ex.clear_faults()
    h.at(h.clock.now + 11)
    assert h.ex.position() is None and h.journal.get(FLATTEN_KEY) is None


def test_a_flat_read_after_a_not_found_is_the_flat_handling(h):
    """The 404 and then a flat read: what the flat branch always did - a leg that filled closes it as that exit."""
    as_on_2026_11_06(h, not_found=1)
    real = h.ex.close_position

    def fill_the_stop_then(cid, size=None):
        out = real(cid, size)  # the 404, raised from here
        return out

    stop = {}

    def wrapped(cid, size=None):
        if not stop:
            stop["done"] = True
            h.ex.set_prices(next(o.stop_price for o in h.ex.open_orders()) - 1.0)
        return fill_the_stop_then(cid, size)

    h.ex.close_position = wrapped
    ask(h)
    r = result(h)
    assert r["ok"] is False and "먼저" in r["why"] and r["close_reason"] == "BRACKET" and h.ex.position() is None


def test_a_request_is_run_once(h):
    ask(h)
    n = len(h.journal.events(kind=TEST_TRADE))
    fills = len(h.ex.fills)
    ticks(h, 3)
    assert len(h.journal.events(kind=TEST_TRADE)) == n and len(h.ex.fills) == fills


def test_the_verification_uses_the_same_reads_as_every_pass(h):
    """A stop the exchange does not show is not on the exchange, whatever the placement answered: the check is observe() on
    the open orders, as in every reconcile pass. The position is then closed."""
    real = h.ex.open_orders
    h.ex.open_orders = lambda: [o for o in real() if o.kind != "BRACKET"]
    ask(h)
    h.ex.open_orders = real
    r = result(h)
    assert r["ok"] is False and r["step"] == "protect" and h.ex.position() is None
    assert any(e["data"]["reason"] == "TEST_TRADE_FAILED" for e in h.journal.events(kind="CLOSED"))


# ---------------------------------------------------------------------------- failure at each step
def refuse_bracket(cid, side, size, limit, stop):
    return PlaceResult(cid, None, False, "INVALID_LIMIT_PRICE: refused")


def test_an_entry_refused_leaves_nothing_and_says_so(h):
    h.ex.place_market = lambda cid, side, size: PlaceResult(cid, None, False, "INSUFFICIENT_FUND: no money")
    ask(h)
    r = result(h)
    assert r["ok"] is False and r["step"] == "entry" and r["position"] == "flat"
    assert "INSUFFICIENT_FUND" in r["why"] and "시험 거래 실패" in r["message"] and "진입" in r["message"]
    assert h.ex.position() is None and h.journal.get(FLATTEN_KEY) is None
    assert any(k == TEST_TRADE and t.startswith("[시험 거래 실패]") for k, t in h.alerts.items)
    assert_nothing_false(h)  # nothing was opened: no pause either
    ticks(h)
    assert_nothing_false(h)


def test_a_stop_that_cannot_be_attached_closes_the_position_at_once(h):
    h.ex.place_bracket = refuse_bracket
    ask(h)
    r = result(h)
    assert r["ok"] is False and r["step"] == "protect" and r["position"] == "flat"
    assert h.ex.position() is None and "손절" in r["message"] and "포지션 없음" in r["message"]
    assert h.journal.events(kind="BRACKET_FAILED") and h.journal.get(FLATTEN_KEY) is None
    assert paused(h.journal)["reason"].startswith("test trade failed")  # real entries would meet the same refusal
    assert r["margin"].get("fields") is None  # never read while a position has no stop: the close comes first
    assert h.clock.now - r["trade_id"] <= rt_mod.TEST_PROTECT_S + 2  # closed within seconds, never left waiting
    assert any(k == TEST_TRADE and t.startswith("[시험 거래 실패]") for k, t in h.alerts.items)
    resume(h.journal, h.clock.now)
    ticks(h)
    # closed in time: the failed bracket is resolved, not a CANARY failure, and no manual intervention
    assert h.journal.events(kind="ORDER_UNRESOLVED") == []
    assert {e["data"]["kind"] for e in h.journal.events(kind="ORDER_RESOLVED")} == {"BRACKET_FAILED"}
    assert_nothing_false(h)


def test_a_venue_that_keeps_refusing_the_stop_does_not_keep_the_position_bare(h):
    h.ex.place_bracket = refuse_bracket
    ask(h)
    assert len(h.journal.events(kind="BRACKET_FAILED")) == rt_mod.TEST_BRACKET_REFUSALS
    closed = h.journal.events(kind="CLOSED")[-1]
    assert closed["ts"] - result(h)["trade_id"] <= 2 * rt_mod.TEST_RETRY_S + 1  # about two seconds, not the whole 10


def test_a_stop_unanswered_is_tried_until_the_limit_then_the_position_is_closed(h):
    h.ex.fail_next("place_bracket", Unavailable("timeout"), times=1000)
    h.ex.fail_next("find_order", Unavailable("timeout"), times=1000)
    ask(h)
    r = result(h)
    assert r["ok"] is False and r["step"] == "protect" and h.ex.position() is None
    assert h.journal.get(FLATTEN_KEY) is None and h.ex.open_orders() == []


def test_a_fill_right_before_the_close_is_no_pass_and_no_failure_of_the_close(h):
    real = h.ex.balance_summary

    def fills_first():
        stop = next(o.stop_price for o in h.ex.open_orders() if o.kind == "BRACKET")
        h.ex.set_prices(stop - 1.0)
        return real()

    h.ex.balance_summary = fills_first
    ask(h)
    r = result(h)
    assert r["ok"] is False and "먼저" in r["why"] and r["position"] == "flat" and r["close_reason"] == "BRACKET"
    assert_nothing_false(h)


def test_an_unexpected_error_after_the_entry_still_ends_closed(h, monkeypatch):
    def broken(tid, ctx):
        raise KeyError("a bug")

    monkeypatch.setattr(h.rt, "_test_verify_stop", broken)
    ask(h)
    r = result(h)
    assert r["ok"] is False and "KeyError" in r["why"] and h.ex.position() is None and r["position"] == "flat"
    assert paused(h.journal)["reason"].startswith("test trade failed")
    assert h.journal.events(kind="LOOP_ERROR")[-1]["data"]["section"] == "test trade"


def test_a_failing_abort_still_gives_a_result_and_leaves_the_close_standing(h, monkeypatch):
    def broken(tid, ctx):
        raise KeyError("a bug")

    real = h.rt.poll
    monkeypatch.setattr(h.rt, "_test_verify_stop", broken)

    def poll_fails(now):
        raise RuntimeError("the pass failed")

    monkeypatch.setattr(h.rt, "poll", poll_fails)
    ask(h)
    r = result(h)
    assert r["ok"] is False and r["position"] == "protected" and h.journal.get(FLATTEN_KEY)["reason"] == "TEST_TRADE_FAILED"
    monkeypatch.setattr(h.rt, "poll", real)
    h.at(h.clock.now + 11)
    assert h.ex.position() is None and h.journal.get(FLATTEN_KEY) is None


def test_a_stop_refused_once_and_accepted_on_the_next_try_is_a_pass(h):
    real, tries = h.ex.place_bracket, []

    def once(cid, side, size, limit, stop):
        tries.append(cid)
        return refuse_bracket(cid, side, size, limit, stop) if len(tries) == 1 else real(cid, side, size, limit, stop)

    h.ex.place_bracket = once
    ask(h)
    assert result(h)["ok"] is True and len(tries) == 2
    ticks(h)
    assert h.journal.events(kind="ORDER_UNRESOLVED") == []


def test_a_close_that_fails_leaves_the_position_protected_and_keeps_closing(h):
    real = h.ex.close_position
    h.ex.close_position = lambda cid, size=None: PlaceResult(cid, None, False, "SOME_NEW_REFUSAL: no")
    ask(h)
    r = result(h)
    assert r["ok"] is False and r["step"] == "close" and r["position"] == "protected"
    assert "보호" in r["message"] and "시험 거래 실패" in r["message"]
    pos = h.ex.position()
    assert pos is not None and pos.contracts == 1
    stops = [o for o in h.ex.open_orders() if o.kind == "BRACKET" and o.stop_price is not None]
    assert sum(o.size - o.filled for o in stops) == 1  # the stop never left
    assert h.journal.get(FLATTEN_KEY)["reason"] == "TEST_TRADE_FAILED"  # it stands, also across a restart
    assert paused(h.journal)["reason"].startswith("test trade failed")
    assert any(k == TEST_TRADE and t.startswith("[시험 거래 실패]") for k, t in h.alerts.items)
    h.at(h.clock.now + 11)  # still refused: tried again at the next poll, still protected
    assert h.ex.position() is not None and h.ex.open_orders()
    h.ex.close_position = real  # the venue takes it
    h.at(h.clock.now + 11)
    assert h.ex.position() is None and h.journal.get(FLATTEN_KEY) is None
    assert h.journal.events(kind="FLATTENED")[-1]["data"]["reason"] == "TEST_TRADE_FAILED"
    assert any("[전량 청산 완료] TEST_TRADE_FAILED" in t for _, t in h.alerts.items)
    assert h.journal.events(kind="INCIDENT") == [] or all(
        e["data"]["kind"] == "ORDER_UNRESOLVED" for e in h.journal.events(kind="INCIDENT"))  # the real failure, nothing false
    assert not {"FOREIGN_ORDERS", "UNKNOWN_POSITION", "ADOPTED", "BREAKER"} & set(canary_failures(h.journal, h.journal.stage()[1]))


def test_a_close_whose_answer_is_lost_is_looked_up_never_doubled(h):
    h.ex.crash_after_accept("close_position", Unavailable("timeout"))
    ask(h)
    r = result(h)
    assert r["ok"] is True and h.ex.position() is None  # found by its client order id: the same close
    assert len([f for f in h.ex.fills if f["kind"] == "MARKET"]) == 2


def test_a_bug_in_the_plan_is_a_refusal_and_leaves_no_mark(h, monkeypatch):
    def broken(req, side, now):
        raise KeyError("a bug")

    monkeypatch.setattr(h.rt, "_test_trade_plan", broken)
    ask(h)
    assert "KeyError" in h.journal.events(kind=TEST_TRADE_REFUSED)[-1]["data"]["why"]
    assert h.journal.get(TEST_ACTIVE_KEY) is None and not h.ex.fills


def test_a_position_that_is_gone_before_the_close_is_no_pass(h):
    """The stop or the take-profit filled first (a fast market): flat, nothing to do, but the close path was not shown."""
    real = h.ex.place_bracket

    def then_gap(cid, side, size, limit, stop):
        out = real(cid, side, size, limit, stop)
        h.ex.set_prices(stop - 1.0 if side < 0 else stop + 1.0)
        return out

    h.ex.place_bracket = then_gap
    ask(h)
    r = result(h)
    assert r["ok"] is False and r["position"] == "flat" and "먼저" in r["why"]
    assert h.ex.position() is None and h.journal.get(FLATTEN_KEY) is None


def test_protection_slower_than_30_s_is_no_pass_and_the_real_canary_failure(h):
    real = h.ex.place_bracket

    def slow(cid, side, size, limit, stop):
        h.clock.now += 40.0
        return real(cid, side, size, limit, stop)

    h.ex.place_bracket = slow
    ask(h)
    r = result(h)
    assert r["ok"] is False and r["protect_s"] == pytest.approx(40.0) and h.ex.position() is None
    h.at(h.clock.now + 61)
    assert "PROTECT_SLOW" in canary_failures(h.journal, h.journal.stage()[1])


# ---------------------------------------------------------------------------- the result is always written
def order_view(hs: Harness, prefix: str, reads_until_ok: int = 10_000, **change):
    """get_order answers `change` for the orders of `prefix` for `reads_until_ok` reads, the truth after that."""
    real, left = hs.ex.get_order, {"n": reads_until_ok}

    def view(order_id):
        o = real(order_id)
        if o.client_order_id.startswith(prefix) and left["n"] > 0:
            left["n"] -= 1
            return dataclasses.replace(o, **change)
        return o

    hs.ex.get_order = view
    return left


@pytest.mark.parametrize("prefix,what", [("ewb-e-", "진입"), ("ewb-x-", "청산")])
def test_a_fill_price_never_read_is_an_explicit_failure_with_a_result(h, prefix, what):
    """Review finding: `f"{None:,.1f}"` raised after a good round trip: no TEST_TRADE, TEST_ACTIVE_KEY stuck, every rerun
    refused, the command's exit 3. The runbook says it passes only if its fill was read."""
    order_view(h, prefix, avg_fill_price=None)
    ask(h)
    r = result(h)
    assert r["ok"] is False and what in r["why"] and "체결가" in r["why"] and r["position"] == "flat"
    assert h.journal.get(TEST_ACTIVE_KEY) is None and h.journal.events(kind="LOOP_ERROR") == []
    assert any(t.startswith("[시험 거래 실패]") for _, t in h.alerts.items)
    assert rt_mod.refusal_of_test_trade(h.journal, h.ex, h.clock.now) is None  # it can be run again


@pytest.mark.parametrize("prefix", ["ewb-e-", "ewb-x-"])
def test_a_fill_price_read_a_few_seconds_late_is_a_pass(h, prefix):
    order_view(h, prefix, reads_until_ok=2, avg_fill_price=None)
    ask(h)
    r = result(h)
    assert r["ok"] is True and r["entry_price"] and r["close_price"] and r["fees"] == pytest.approx(0.30)


def test_a_fee_of_zero_on_the_first_read_is_read_again(h):
    """Review finding: a fee not settled yet reads 0 and would count as measured."""
    order_view(h, "ewb-x-", reads_until_ok=1, fee=0.0)
    ask(h)
    r = result(h)
    assert r["ok"] is True and r["close_fee"] == pytest.approx(0.15) and r["fees"] == pytest.approx(0.30)


def test_a_fee_still_zero_is_shown_as_it_is(h):
    order_view(h, "ewb-x-", fee=0.0)
    ask(h)
    r = result(h)
    assert r["ok"] is True and r["close_fee"] == 0.0 and r["fees"] == pytest.approx(0.15)
    assert "0.15" in {g.name: g for g in canary_gates(h.journal, h.clock.now, None)}["⑤ 비용 실측치 비교"].detail


def test_a_fee_that_stays_zero_passes_the_cost_gate_and_says_to_check_it(tmp_path):
    """Re-review L6: 0.00 read as measured. A truly free fill must not lock FULL for ever, so it passes, in plain sight."""
    hs = Harness(tmp_path, QUIET + 30, stage="CANARY", stage_days=1.0)  # no fee on the venue at all
    hs.price(float(hs.bar(QUIET)["open"]))
    hs.ex.summary = {}
    hs.rt.startup()
    hs.at(QUIET + 30)
    hs.clock.now = float(QUIET + 600)
    ask(hs)
    assert result(hs)["ok"] is True and result(hs)["fees"] == 0.0
    gate = {g.name: g for g in canary_gates(hs.journal, hs.clock.now, None)}["⑤ 비용 실측치 비교"]
    assert gate.met and "수수료 0 (확인 필요)" in gate.detail
    hs.journal.close()


def test_a_fee_of_zero_on_one_fill_is_shown_too(h):
    order_view(h, "ewb-x-", fee=0.0)
    ask(h)
    gate = {g.name: g for g in canary_gates(h.journal, h.clock.now, None)}["⑤ 비용 실측치 비교"]
    assert gate.met and "수수료 0 (확인 필요)" in gate.detail and "0.15" in gate.detail


def test_a_positive_fee_is_not_flagged(h):
    ask(h)
    gate = {g.name: g for g in canary_gates(h.journal, h.clock.now, None)}["⑤ 비용 실측치 비교"]
    assert "확인 필요" not in gate.detail and "0.30" in gate.detail


def test_ok_needs_the_end_state_to_be_flat(h, monkeypatch):
    """Review finding: `ok` was set before the last read. A position in that read is no pass, and it is taken back."""
    real, end_state = h.ex.position, h.rt._test_end_state

    def a_position_shows():
        h.ex.position = lambda: rt_mod.Position(1, 1, 22_000.0)
        return end_state()

    monkeypatch.setattr(h.rt, "_test_end_state", a_position_shows)
    ask(h)
    r = result(h)
    assert r["ok"] is False and r["position"] in ("protected", "unprotected") and "끝" in r["why"]
    assert "포지션 없음" not in r["message"] and h.journal.get(TEST_ACTIVE_KEY) is None
    assert h.rt.executor.trade().status == "OPEN" and h.journal.get(FLATTEN_KEY) is not None  # back on the record, its close stands
    h.ex.position = real
    h.at(h.clock.now + 11)
    assert h.journal.get(FLATTEN_KEY) is None and h.ex.position() is None
    assert_no_false_manual(h)


def test_an_end_state_that_cannot_be_read_is_no_pass(h, monkeypatch):
    end_state = h.rt._test_end_state

    def down():
        h.ex.fail_next("position", Unavailable("down"), times=1000)
        return end_state()

    monkeypatch.setattr(h.rt, "_test_end_state", down)
    ask(h)
    h.ex.clear_faults()
    r = result(h)
    assert r["ok"] is False and r["position"] == "unread" and "읽지 못함" in r["message"]


def test_a_bug_while_writing_the_result_still_writes_it_and_clears_the_active_key(h, monkeypatch):
    def broken(r, tid):
        raise KeyError("a bug")

    monkeypatch.setattr(h.rt, "_test_fills", broken)
    ask(h)
    r = result(h)
    assert r["ok"] is False and "KeyError" in r["why"] and "시험 거래 실패" in r["message"]
    assert h.journal.get(TEST_ACTIVE_KEY) is None
    assert any(t.startswith("[시험 거래 실패]") for _, t in h.alerts.items)


# ---------------------------------------------------------------------------- a position read that lags the fill
def assert_no_false_manual(hs: Harness) -> None:
    assert hs.journal.events(kind="ADOPTED") == [] and (hs.journal.get(RAISED_KEY) or {}).get("items", []) == []
    assert not {"UNKNOWN_POSITION", "ADOPTED", "FOREIGN_ORDERS", "EXCESS_POSITION", "UNEXPLAINED_REDUCTION"} & set(
        canary_failures(hs.journal, hs.journal.stage()[1]))


@pytest.mark.parametrize("refuse", [True, False])
def test_a_short_lag_is_waited_for_and_the_trade_passes(h, refuse):
    """A flat read right after the fill is not believed: no leg read back with a fill explains it."""
    Lag(h, 4, refuse=refuse)
    ask(h)
    r = result(h)
    assert r["ok"] is True and r["position"] == "flat" and r["why"] is None
    assert h.ex.position() is None and h.journal.get(FLATTEN_KEY) is None
    assert [e["data"]["reason"] for e in h.journal.events(kind="CLOSED")] == ["TEST_TRADE"]  # never read as a bracket's exit
    ticks(h, 3, 61.0)
    assert_nothing_false(h)
    assert h.journal.events(kind="ORDER_UNRESOLVED") == []


def test_a_refusal_for_want_of_the_position_does_not_count_while_it_is_not_seen(h):
    Lag(h, 13, refuse=True)  # 4 s of flat reads, each with a refusal: more than TEST_BRACKET_REFUSALS, none of them about the terms
    ask(h)
    r = result(h)
    assert r["ok"] is True and len(h.journal.events(kind="BRACKET_FAILED")) > rt_mod.TEST_BRACKET_REFUSALS


def unseen_lag(hs: Harness, refuse: bool = True) -> tuple[Lag, dict]:
    lag = Lag(hs, 10_000, refuse=refuse)
    ask(hs)
    return lag, result(hs)


def assert_unseen(hs: Harness, r: dict) -> None:
    assert r["ok"] is False and r["step"] == "protect" and r["position"] == "unseen"
    assert "포지션 없음" not in r["message"] and "먼저" not in r["message"] and "읽히지 않음" in r["message"]
    t = hs.rt.executor.trade()
    assert t.trade_id == r["trade_id"] and t.status == "OPEN" and hs.rt.executor.entry_unseen(t)  # never closed on the record
    assert hs.journal.get(FLATTEN_KEY)["reason"] == "TEST_TRADE_FAILED" and paused(hs.journal) is not None
    assert hs.rt.active is True  # fast polls, across which it is looked for
    assert hs.rt.flat() is False  # no promotion, no new test trade, no restart into a new stage while it may show
    assert any(k == TEST_TRADE and t.startswith("[시험 거래 실패]") and "읽히지 않음" in t for k, t in hs.alerts.items)


def test_a_position_not_seen_within_the_limit_and_shown_later_with_no_target_is_protected_and_closed(h):
    """The reviewer's probe: no target (a held hour), and it sat unprotected until the next hour with `포지션 없음` said."""
    h.rt.target = None
    lag, r = unseen_lag(h)
    assert_unseen(h, r)
    assert "보호한 뒤" not in r["message"] and "바로 청산" in r["message"]  # what this path does: the close comes first
    assert h.ex.pos is not None  # the real one, on the venue
    lag.left = 0
    h.at(h.clock.now + 11)
    assert h.ex.position() is None and h.ex.open_orders() == []  # its standing close went out at the first read that shows it
    assert h.journal.get(FLATTEN_KEY) is None
    assert h.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "TEST_TRADE_FAILED"
    assert_no_false_manual(h)


def test_a_late_position_whose_close_fails_gets_its_stop(h):
    """The close is first (market, with nothing else to do), and when it fails the position is protected at once."""
    h.rt.target = None
    lag, r = unseen_lag(h)
    real, placed = h.ex.close_position, []
    h.ex.close_position = lambda cid, size=None: PlaceResult(cid, None, False, "SOME_NEW_REFUSAL: no")
    bracket = h.ex.place_bracket
    h.ex.place_bracket = lambda *a: placed.append(a) or bracket(*a)
    lag.left = 0
    h.at(h.clock.now + 11)
    assert h.ex.position() is not None and placed and [o.kind for o in h.ex.open_orders()] == ["BRACKET"]
    assert h.journal.get(FLATTEN_KEY) is not None
    h.ex.close_position = real
    h.at(h.clock.now + 11)
    assert h.ex.position() is None and h.journal.get(FLATTEN_KEY) is None
    assert_no_false_manual(h)


def test_a_late_position_with_a_flat_engine_is_no_unknown_position(h):
    lag, r = unseen_lag(h)
    assert_unseen(h, r)
    lag.left = 0
    h.at(h.clock.now + 11)
    assert h.ex.position() is None and h.journal.get(FLATTEN_KEY) is None
    assert_no_false_manual(h)
    assert not [a for a in h.journal.events(kind="CLOSED") if a["data"]["reason"] == "UNKNOWN_POSITION"]


def test_a_late_position_on_the_engines_side_is_not_adopted(h):
    h.rt.target = Target(QUIET, 22_000.0, position(QUIET - 7200), None, None, ())
    lag, r = unseen_lag(h)
    assert_unseen(h, r)
    lag.left = 0
    h.at(h.clock.now + 11)
    assert h.ex.position() is None
    assert_no_false_manual(h)


def test_its_stop_stays_while_the_position_may_still_show(h):
    """Bracket accepted, the position late: the leg is not cancelled as FLAT in the meantime, and no second one is placed."""
    lag, r = unseen_lag(h, refuse=False)
    assert_unseen(h, r)
    h.at(h.clock.now + 11)
    h.at(h.clock.now + 11)
    assert [o.kind for o in h.ex.open_orders()] == ["BRACKET"]
    lag.left = 0
    h.at(h.clock.now + 11)
    assert h.ex.position() is None and h.ex.open_orders() == []
    assert_no_false_manual(h)


def test_a_restart_keeps_looking_for_it(h):
    lag, r = unseen_lag(h)
    assert_unseen(h, r)
    h.clock.now += 30
    h.rt = h.make_runtime()
    assert h.rt.active is True
    lag.left = 0
    h.rt.startup()
    assert h.ex.position() is None and h.journal.get(FLATTEN_KEY) is None
    assert_no_false_manual(h)


def test_a_position_that_never_shows_ends_the_wait_says_so_and_leaves_the_pause(h):
    lag, r = unseen_lag(h, refuse=False)
    assert_unseen(h, r)
    for _ in range(35):
        h.at(h.clock.now + 11)
        if h.rt.executor.trade().status != "OPEN":
            break
    assert h.rt.executor.trade().exit_reason == "ENTRY_NOT_SEEN" and h.journal.get(FLATTEN_KEY) is None
    assert paused(h.journal) is not None and any("나타나지 않음" in t for _, t in h.alerts.items)
    h.at(h.clock.now + 11)
    assert h.ex.open_orders() == []  # the wait is over: the planner cancels what rests on a book it reads flat


def test_a_flat_read_at_the_close_with_no_fill_is_not_called_a_bracket_exit(h):
    """Verified, then flat with the leg unfilled: the close has nothing to send. No fill says the position left."""
    lag = Lag(h, 0)
    real = h.ex.balance_summary

    def hide():
        lag.left = 10_000
        return real()

    h.ex.balance_summary = hide
    ask(h)
    r = result(h)
    assert r["ok"] is False and r["position"] == "unseen" and "먼저" not in r["why"] and "포지션 없음" not in r["message"]
    assert h.rt.executor.trade().status == "OPEN" and h.journal.events(kind="CLOSED") == []  # no bracket's exit on the record
    lag.left = 0
    h.at(h.clock.now + 11)
    assert h.ex.position() is None and h.journal.get(FLATTEN_KEY) is None
    assert h.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "TEST_TRADE_FAILED"
    assert_no_false_manual(h)


def test_a_new_test_trade_is_refused_while_the_last_position_may_still_show(h):
    lag, r = unseen_lag(h)
    resume(h.journal, h.clock.now)
    assert "읽히지 않" in rt_mod.refusal_of_test_trade(h.journal, h.ex, h.clock.now)
    assert "전량 청산" not in rt_mod.refusal_of_test_trade(h.journal, h.ex, h.clock.now)  # it says what is really going on


def test_two_refusals_of_the_stop_are_at_least_a_retry_apart(h):
    """Review finding: the second refusal came 0.0 s after the first, so the limit of two meant no retry at all."""
    h.ex.place_bracket = refuse_bracket
    ask(h)
    times = [e["ts"] for e in h.journal.events(kind="BRACKET_FAILED")]
    assert len(times) == rt_mod.TEST_BRACKET_REFUSALS and times[1] - times[0] >= rt_mod.TEST_RETRY_S


# ---------------------------------------------------------------------------- an entry whose outcome is unknown
def test_an_entry_answer_and_lookup_lost_is_protected_and_closed_as_ours(h):
    h.ex.crash_after_accept("place_market", Unavailable("timeout"))
    h.ex.fail_next("find_order", Unavailable("down"), times=3)
    ask(h)
    r = result(h)
    assert r["ok"] is False and h.ex.position() is None and r["position"] == "flat"
    assert h.journal.events(kind="ADOPTED_PROVISIONAL") and h.journal.events(kind="BRACKET_FAILED") == []
    assert h.journal.events(kind="ADOPTED") == []
    ticks(h)
    assert h.journal.events(kind="ADOPTED") == [] and (h.journal.get(RAISED_KEY) or {}).get("items", []) == []
    found = canary_failures(h.journal, h.journal.stage()[1])
    assert not {"UNKNOWN_POSITION", "ADOPTED", "FOREIGN_ORDERS"} & set(found)


def test_a_position_that_appears_after_the_test_gave_up_is_not_taken_for_a_manual_one(h):
    """The position read lags past the whole settle time: the test ends, the entry still unknown. The next pass finds the
    position: our test entry's, protected and closed - no UNKNOWN_POSITION, no manual incident."""
    h.ex.crash_after_accept("place_market", Unavailable("timeout"))
    h.ex.fail_next("find_order", Unavailable("down"), times=1000)
    real = h.ex.position
    h.ex.position = lambda: None
    ask(h)
    r = result(h)
    assert r["ok"] is False and r["step"] == "entry" and "확인하지 못함" in r["why"]
    assert r["position"] == "unseen" and "포지션 없음" not in r["message"]  # not read, which says nothing of it being gone
    h.ex.position = real
    h.ex.clear_faults()
    ticks(h, 3, 11.0)
    assert h.ex.position() is None and h.journal.get(FLATTEN_KEY) is None
    assert h.journal.events(kind="ADOPTED") == [] and (h.journal.get(RAISED_KEY) or {}).get("items", []) == []
    assert not {"UNKNOWN_POSITION", "ADOPTED"} & set(canary_failures(h.journal, h.journal.stage()[1]))
    assert h.journal.events(kind="RECOVERED_ENTRY")


def test_an_unfilled_entry_is_a_plain_failure(h):
    h.ex.market_fill_ratio = 0.0
    ask(h)
    r = result(h)
    assert r["ok"] is False and r["step"] == "entry" and r["position"] == "flat" and "체결" in r["why"]
    assert_nothing_false(h)


# ---------------------------------------------------------------------------- a crash in the middle
@pytest.mark.parametrize("method", ["place_market", "place_bracket", "close_position"])
def test_a_crash_at_any_step_ends_closed_after_the_restart_without_a_false_incident(h, method):
    h.ex.crash_after_accept(method)
    with pytest.raises(SimCrash):
        ask(h)
    assert h.journal.get(TEST_ACTIVE_KEY) is not None  # the killed process wrote nothing more
    h.clock.now += 20
    h.rt = h.make_runtime()
    h.rt.startup()
    ticks(h, 3, 11.0)
    r = result(h)
    assert r["ok"] is False and r["step"] == "interrupted" and h.journal.get(TEST_ACTIVE_KEY) is None
    assert h.ex.position() is None and h.ex.open_orders() == [] and h.journal.get(FLATTEN_KEY) is None
    assert any(t.startswith("[시험 거래 중단]") for _, t in h.alerts.items)
    assert h.journal.events(kind="ADOPTED") == [] and (h.journal.get(RAISED_KEY) or {}).get("items", []) == []
    assert not {"UNKNOWN_POSITION", "ADOPTED", "FOREIGN_ORDERS", "BREAKER"} & set(canary_failures(h.journal, h.journal.stage()[1]))
    # never run again, and never a strategy trade
    assert h.journal.get(TEST_REQUEST_KEY) is None
    assert h.rt.costs(0.0)["fills"] == 0
    assert canary_gates(h.journal, h.clock.now, None)[0].detail.startswith("0건")


# ---------------------------------------------------------------------------- the runtime's own view
def test_the_next_cycle_and_the_minutes_take_it_for_nothing_else(h):
    decisions = len(h.journal.decisions())
    target = h.rt.target
    ask(h)
    assert result(h)["ok"] is True and h.rt.target is target
    for at in range(QUIET + 3600 + 30, QUIET + 3 * H1, 300):
        h.price(float(h.bar(int(at) // H1 * H1)["open"]))
        h.at(at)
    assert_nothing_false(h)
    assert h.ex.position() is None
    # the engine's decisions and so the paper comparison know nothing of it
    assert not any(str(d.get("setup")) == "TEST" for d in h.journal.decisions())
    assert result(h)["trade_id"] not in {int(d["trade_key"]) for d in h.journal.decisions()}
    assert len(h.journal.decisions()) >= decisions


def test_an_engine_position_the_account_does_not_hold_changes_nothing(h):
    """The engine holds a trade the live account skipped: the test trade is neither that trade nor adopted as it."""
    h.rt.target = Target(QUIET, 40_000.0, position(QUIET - 7200), None, None, ())
    ask(h)
    assert result(h)["ok"] is True and h.ex.position() is None
    assert_nothing_false(h)


def test_it_never_counts_as_a_strategy_trade(h):
    ask(h)
    tid = result(h)["trade_id"]
    assert [e["data"]["trade_id"] for e in h.journal.events(kind="CLOSED")] == [tid]
    gates = {g.name: g for g in canary_gates(h.journal, h.clock.now, None)}
    assert gates["완결 거래 3건 이상 또는 90일"].detail.startswith("0건")  # counted apart
    c = h.rt.costs(0.0)  # the cost report compares the strategy's fills with the paper model
    assert c["fills"] == 0 and c["fees"] == 0.0 and c["model_fees"] == 0.0 and c["slippage_bps"] is None
    h.rt._summary(h.clock.now)
    assert h.journal.events(kind="COSTS") == []
    # a real trade beside it is still counted
    h.journal.add_event(h.clock.now, "CLOSED", {"trade_id": QUIET, "reason": "BRACKET", "price": 40_000.0, "contracts": 1,
                                                "fee": 0.1, "side": 1})
    assert h.rt.costs(0.0)["fills"] == 1
    assert {g.name: g for g in canary_gates(h.journal, h.clock.now, None)}["완결 거래 3건 이상 또는 90일"].detail.startswith("1건")


# ---------------------------------------------------------------------------- refusals on the loop
@pytest.mark.parametrize("why,arrange", [
    ("CANARY", lambda hs: hs.journal.set_stage("FULL", "test", hs.clock.now)),
    ("CANARY", lambda hs: hs.journal.set_stage("SHADOW", "test", hs.clock.now)),
    ("일시 정지", lambda hs: hs.journal.put("paused", {"reason": "user /pause", "ts": hs.clock.now})),
    ("장이 열려", lambda hs: hs.ex.set_session("CLOSE")),
    ("포지션", lambda hs: hs.ex.set_session("OPEN") or setattr(hs.ex, "position", lambda: rt_mod.Position(1, 1, 40_000.0))),
    ("대기 주문", lambda hs: setattr(hs.ex, "open_orders", lambda: [rt_mod.Order("o", "ewb-b-1-tp1-1", -1, "BRACKET", 1, 0, "OPEN")])),
    ("거래 키", lambda hs: setattr(hs.ex, "permissions", KeyPermissions(True, False, False))),
    ("거래 키", lambda hs: setattr(hs.ex, "permissions", KeyPermissions(True, True, True))),
    ("거래소를 읽지 못함", lambda hs: hs.ex.fail_next("key_permissions", Unavailable("down"))),
    ("전량 청산", lambda hs: hs.journal.put(FLATTEN_KEY, {"reason": "X", "origin": "USER", "ts": hs.clock.now, "deferred": False})),
    ("오래됨", lambda hs: None),
])
def test_refused_on_the_loop_with_no_order_sent(h, why, arrange):
    arrange(h)
    fills = len(h.ex.fills)
    at = h.clock.now + 1
    if why == "오래됨":
        h.clock.now = at
        h.journal.put(TEST_REQUEST_KEY, {"id": "r", "side": 1, "ts": at - 600})
        h.rt._test_trade_due(at)
    else:
        h.clock.now = at
        h.journal.put(TEST_REQUEST_KEY, {"id": "r", "side": 1, "ts": at})
        h.rt._test_trade_due(at)
    refused = h.journal.events(kind=TEST_TRADE_REFUSED)[-1]["data"]
    assert why in refused["why"] and refused["request"] == "r"
    assert h.journal.events(kind=TEST_TRADE) == [] and len(h.ex.fills) == fills
    assert h.journal.get(TEST_REQUEST_KEY) is None and h.journal.get(TEST_IDS_KEY) is None


@pytest.mark.parametrize("minute", [55, 59, 0, 5])
def test_refused_around_the_hours_cycle(h, minute):
    at = QUIET + 3600 + minute * 60 if minute < 55 else QUIET + minute * 60
    h.clock.now = float(at)
    h.journal.put(TEST_REQUEST_KEY, {"id": "r", "side": 1, "ts": at})
    h.rt._test_trade_due(at)
    assert "06분" in h.journal.events(kind=TEST_TRADE_REFUSED)[-1]["data"]["why"]


def test_refused_while_the_hour_has_a_strategy_signal(h):
    h.rt.target = Target(QUIET, 40_000.0, None, None, entry(QUIET - H1), ())  # its 5 minutes are over: no entry from it
    ask(h)
    assert "신호" in h.journal.events(kind=TEST_TRADE_REFUSED)[-1]["data"]["why"] and h.ex.position() is None


def test_refused_when_the_account_cannot_carry_one_contract(tmp_path):
    hs = started(tmp_path, equity=60.0)  # one contract's margin alone is above 70% of it
    ask(hs)
    refused = hs.journal.events(kind=TEST_TRADE_REFUSED)[-1]["data"]["why"]
    assert "%" in refused and hs.ex.position() is None and not hs.ex.fills
    hs.journal.close()


def test_a_request_written_after_the_tick_began_is_not_refused_as_old(h):
    """Review finding: the tick's `now` predates a request written while the tick ran: a negative age, refused as old."""
    at = h.clock.now + 1
    h.clock.now = at + 1.0  # the tick took its `now` a second ago
    h.journal.put(TEST_REQUEST_KEY, {"id": "r", "side": 1, "ts": at + 0.4})
    h.rt._test_trade_due(at)
    assert h.journal.events(kind=TEST_TRADE_REFUSED) == [] and result(h)["ok"] is True


def test_an_entry_that_ends_unfilled_after_its_wait_is_settled(h):
    """Review finding: the IOC read open past the 10 s wait, then ended unfilled, and stayed "unsettled" for up to 2 hours:
    fast polls, no flat book, every rerun refused."""
    h.ex.market_fill_ratio = 0.0
    real, start = h.ex.get_order, {"t": None}

    def late(order_id):
        o = real(order_id)
        if o.client_order_id.startswith("ewb-e-"):
            start["t"] = h.clock.now if start["t"] is None else start["t"]
            if h.clock.now - start["t"] <= h.rt.executor.fill_timeout:
                return dataclasses.replace(o, status="OPEN")
        return o

    h.ex.get_order = late
    ask(h)
    r = result(h)
    assert r["ok"] is False and r["position"] == "flat" and "포지션 없음" in r["message"]
    assert h.rt.executor.unsettled_entry(h.clock.now) is None and h.rt._busy() is False and h.rt.flat() is True
    assert h.journal.events(kind="ENTRY_UNFILLED")[-1]["data"]["terminal"] is True
    ask(h)
    assert len(h.journal.events(kind=TEST_TRADE)) == 2  # not refused


def test_a_demotion_during_the_plans_reads_sends_nothing_and_the_book_shows_it_held(h):
    """Re-review L3: a demotion to SHADOW during the plan's reads still sent two real fills, and `_held` saw nothing."""
    real, seen = h.ex.balance, {}

    def demoted_here():
        if not seen:
            seen["held"] = cli._test_trade_held(h.journal)
            h.journal.set_stage("SHADOW", "demotion", h.clock.now)
        return real()

    at = h.clock.now + 1
    h.clock.now = at
    h.journal.put(TEST_REQUEST_KEY, {"id": "r", "side": 1, "ts": at})
    h.ex.balance = demoted_here
    h.rt._test_trade_due(at)
    assert seen["held"] is True and h.ex.fills == [] and h.ex.position() is None
    assert "CANARY" in h.journal.events(kind=TEST_TRADE_REFUSED)[-1]["data"]["why"]
    assert h.journal.get(TEST_ACTIVE_KEY) is None and h.journal.get(TEST_REQUEST_KEY) is None


def test_a_crash_in_the_plan_is_a_refusal_not_a_failed_test_trade(h):
    """Nothing was sent: no failed last test trade for the gates, no pause, no standing close."""
    h.journal.put(TEST_ACTIVE_KEY, {"request": "r", "side": 1, "ts": h.clock.now})
    h.clock.now += 20
    h.rt = h.make_runtime()
    h.rt.startup()
    assert h.journal.get(TEST_ACTIVE_KEY) is None and h.journal.events(kind=TEST_TRADE) == []
    assert h.journal.events(kind=TEST_TRADE_REFUSED)[-1]["data"]["request"] == "r" and paused(h.journal) is None


def test_every_refusal_clears_the_mark_the_book_is_held_by(h):
    h.journal.put("paused", {"reason": "user /pause", "ts": h.clock.now})
    ask(h)
    assert h.journal.events(kind=TEST_TRADE_REFUSED) and h.journal.get(TEST_ACTIVE_KEY) is None


def test_refused_without_quotes_or_fresh_margin_rates(h):
    h.ex.fail_next("quote", Unavailable("down"), times=2)
    ask(h)
    assert "호가" in h.journal.events(kind=TEST_TRADE_REFUSED)[-1]["data"]["why"]
    h.ex.clear_faults()
    h.ex.fail_next("product", Unavailable("down"))
    ask(h)
    assert "증거금률" in h.journal.events(kind=TEST_TRADE_REFUSED)[-1]["data"]["why"]
    assert not h.ex.fills


# ---------------------------------------------------------------------------- the margin measurement
def test_the_margin_fields_are_journaled_raw_while_the_position_is_open(h):
    seen = {}
    real = h.ex.balance_summary

    def read():
        seen["position"] = h.ex.pos
        return real()

    h.ex.balance_summary = read
    ask(h)
    r = result(h)
    assert seen["position"] is not None and seen["position"].contracts == 1  # read with the position open
    event = h.journal.events(kind=TEST_TRADE_MARGIN)[-1]["data"]
    assert event["trade_id"] == r["trade_id"] and event["position"]["contracts"] == 1
    assert event["position"]["side"] == 1 and event["position"]["avg_entry"] == r["entry_price"]
    assert event["fields"] == {k: SUMMARY[k] for k in FIELDS}  # exactly as sent, no unit guessed
    assert "total_usd_balance" not in event["fields"] and event["missing"] == []
    assert r["margin"]["fields"] == event["fields"] and r["ok"] is True


@pytest.mark.parametrize("fault", [Unavailable("down"), ExchangeError("refused")])
def test_a_failed_margin_read_only_warns(h, fault):
    h.ex.fail_next("balance_summary", fault)
    ask(h)
    r = result(h)
    assert r["ok"] is True and h.ex.position() is None  # closed all the same
    assert "fields" not in r["margin"] and str(fault) in r["margin"]["error"]
    assert h.journal.events(kind=TEST_TRADE_MARGIN)[-1]["data"]["error"] == r["margin"]["error"]
    assert any(t.startswith("[시험 거래 경고]") for _, t in h.alerts.items)
    assert_nothing_false(h)


def test_one_failed_read_at_the_close_is_no_canary_failure(h):
    """Review finding: the close's first position read failed once, its ACTION_ERROR named no trade, and so the CLOSED of the
    standing close never matched it: ORDER_UNRESOLVED, a pause, a failed stint, for a position that was closed in seconds."""
    real = h.ex.balance_summary

    def arm():
        h.ex.fail_next("position", Unavailable("HTTP 503"), times=1)
        return real()

    h.ex.balance_summary = arm
    ask(h)
    r = result(h)
    assert r["ok"] is False and r["position"] == "flat" and h.ex.position() is None
    action = h.journal.events(kind="ACTION_ERROR")[-1]["data"]
    assert action["trade_id"] == r["trade_id"]
    resume(h.journal, h.clock.now)
    ticks(h, 3, 61.0)
    assert h.journal.events(kind="ORDER_UNRESOLVED") == [] and paused(h.journal) is None
    assert "ORDER_UNRESOLVED" not in canary_failures(h.journal, h.journal.stage()[1])
    assert [e["data"]["kind"] for e in h.journal.events(kind="ORDER_RESOLVED")] == ["ACTION_ERROR"]


def test_an_unexpected_error_in_the_margin_read_only_warns(h):
    def broken():
        raise KeyError("unexpected")

    h.ex.balance_summary = broken
    ask(h)
    assert result(h)["ok"] is True and "KeyError" in result(h)["margin"]["error"] and h.ex.position() is None


def test_a_reply_the_journal_cannot_hold_only_warns(h):
    h.ex.summary = {"liquidation_threshold": float("nan")}
    ask(h)
    r = result(h)
    assert r["ok"] is True and "error" in r["margin"] and h.ex.position() is None


def test_a_hung_margin_read_never_holds_the_close_back(h, monkeypatch):
    monkeypatch.setattr(rt_mod, "TEST_MARGIN_WAIT_S", 0.05)
    release = threading.Event()
    answered = threading.Event()
    summary = dict(SUMMARY)

    def hung():
        release.wait(5.0)
        answered.set()
        return summary

    h.ex.balance_summary = hung
    ask(h)
    r = result(h)
    assert not answered.is_set() and h.ex.position() is None and r["ok"] is True  # closed while the read still hung
    assert "error" in r["margin"] and "fields" not in r["margin"]
    n = len(h.journal.events(kind=TEST_TRADE_MARGIN))
    release.set()
    assert answered.wait(5.0)
    ticks(h, 2)
    assert len(h.journal.events(kind=TEST_TRADE_MARGIN)) == n  # a late answer describes no open position: dropped


def test_a_field_coinbase_does_not_send_is_named_not_invented(h):
    h.ex.summary = {"liquidation_threshold": {"value": "1", "currency": "USD"}}
    ask(h)
    margin = result(h)["margin"]
    assert margin["fields"]["liquidation_threshold"] == {"value": "1", "currency": "USD"}
    assert margin["fields"]["overnight_margin_window_measure"] is None
    assert set(margin["missing"]) == set(FIELDS) - {"liquidation_threshold"}


# ---------------------------------------------------------------------------- the CANARY gates
def fresh(now: float) -> AuditResult:
    return AuditResult((0, int(now) // H1 * H1 - 2 * H1), 0, 0, (), now - 600)


def full_gates(hs: Harness, now: float) -> list:
    return full_entry_gates(hs.journal, now, fresh(now), PAPER_OK, KEY)


def test_one_passed_test_trade_opens_full_without_a_signal(h):
    now = h.clock.now
    unmet = {g.name for g in full_gates(h, now) if not g.met}
    assert unmet == {"완결 거래 3건 이상 또는 90일", "① 보호 주문이 체결 후 30초 안에 걸림", "⑤ 비용 실측치 비교"}
    ask(h)
    now = h.clock.now + 60
    gates = full_gates(h, now)
    assert all(g.met for g in gates), gate_lines(gates)
    details = {g.name: g.detail for g in gates}
    assert "시험 거래 성공 1건" in details["완결 거래 3건 이상 또는 90일"]
    assert "시험 거래" in details["① 보호 주문이 체결 후 30초 안에 걸림"]
    assert "0.30" in details["⑤ 비용 실측치 비교"]
    transition(h.journal, "FULL", now, gates, confirm=True, flat=True)
    assert h.journal.stage()[0] == "FULL"


@pytest.mark.parametrize("arrange", [
    lambda hs: setattr(hs.ex, "place_market", lambda cid, side, size: PlaceResult(cid, None, False, "NO")),
    lambda hs: setattr(hs.ex, "place_bracket", refuse_bracket),
    lambda hs: setattr(hs.ex, "close_position", lambda cid, size=None: PlaceResult(cid, None, False, "NO")),
])
def test_a_failed_test_trade_opens_nothing(h, arrange):
    arrange(h)
    ask(h)
    assert result(h)["ok"] is False
    gates = {g.name: g for g in full_gates(h, h.clock.now + 60)}
    assert not gates["완결 거래 3건 이상 또는 90일"].met
    assert not gates["① 보호 주문이 체결 후 30초 안에 걸림"].met
    assert not gates["⑤ 비용 실측치 비교"].met


def test_a_later_failed_test_trade_closes_the_gates_until_one_passes_again(h):
    ask(h)
    assert result(h)["ok"] is True
    h.clock.now += 300
    h.ex.place_bracket = refuse_bracket
    ask(h)
    del h.ex.place_bracket
    assert result(h)["ok"] is False
    resume(h.journal, h.clock.now)
    ticks(h, 4, 61.0)
    unmet = {g.name for g in full_gates(h, h.clock.now) if not g.met}
    assert unmet == {"완결 거래 3건 이상 또는 90일", "① 보호 주문이 체결 후 30초 안에 걸림", "⑤ 비용 실측치 비교"}
    ask(h)
    assert result(h)["ok"] is True and all(g.met for g in full_gates(h, h.clock.now + 60))


def test_a_pass_with_no_fee_read_does_not_measure_the_costs(tmp_path):
    hs = started(tmp_path)
    real = hs.ex.get_order

    def feeless(order_id):
        o = real(order_id)
        return rt_mod.Order(o.order_id, o.client_order_id, o.side, o.kind, o.size, o.filled, o.status, o.limit_price,
                            o.stop_price, o.avg_fill_price, o.created_ts, None)

    hs.ex.get_order = feeless
    ask(hs)
    assert result(hs)["ok"] is True and result(hs)["fees"] is None
    gates = {g.name: g for g in full_gates(hs, hs.clock.now + 60)}
    assert gates["완결 거래 3건 이상 또는 90일"].met and not gates["⑤ 비용 실측치 비교"].met
    hs.journal.close()


def test_the_other_canary_gates_still_bind_after_a_pass(h):
    ask(h)
    now = h.clock.now + 60
    assert all(g.met for g in full_gates(h, now))
    h.journal.add_event(now, "UNPROTECTED_60S", {"trade_id": 9})
    assert not all(g.met for g in full_gates(h, now + 1))
    gates = {g.name: g for g in full_gates(h, now + 1)}
    assert not gates["③ 보호되지 않은 상태 60초 초과 없음"].met
    # a restarted stint needs its own test trade
    restart_stint(h.journal, "CANARY", now + 2, confirm=True, flat=True)
    gates = {g.name: g for g in full_gates(h, now + 3)}
    assert not gates["완결 거래 3건 이상 또는 90일"].met and gates["③ 보호되지 않은 상태 60초 초과 없음"].met
    # paused, a mismatch of this stint, a missing audit
    h.journal.put("paused", {"reason": "x", "ts": now})
    assert not {g.name: g for g in full_gates(h, now + 3)}["일시 정지 아님"].met


# ---------------------------------------------------------------------------- the command
@pytest.fixture
def env(tmp_path, monkeypatch):
    """The command beside a runtime: same journal, same fake exchange; the command's wait lets the runtime tick."""
    data = tmp_path / "data"
    data.mkdir()
    hs = started(tmp_path, name="data/journal.sqlite")
    settings = LiveSettings(data_dir=data)
    assert settings.journal_path == tmp_path / "data/journal.sqlite"
    monkeypatch.setattr(cli, "_live_settings", lambda: settings)
    monkeypatch.setattr(live_app, "real_exchange", lambda s: hs.ex)
    monkeypatch.setattr("ewbot.cli.time.time", hs.clock)
    monkeypatch.setattr("ewbot.cli.time.sleep", lambda s: hs.at(hs.clock.now + 1))
    hs.journal.put(rt_mod.HEARTBEAT_KEY, hs.clock.now)
    yield hs
    hs.journal.close()


def test_the_command_runs_it_through_the_runtime_and_prints_the_raw_margin_values(env, capsys):
    fills_before = len(env.ex.fills)
    assert main(["live", "test-trade", "--confirm"]) == 0
    out = capsys.readouterr().out
    assert "시험 거래 성공" in out and "롱" in out and "포지션 없음" in out
    assert len(env.ex.fills) == fills_before + 2 and env.ex.position() is None
    for k in FIELDS:
        line = next(x for x in out.splitlines() if x.strip().startswith(f"{k}:"))
        assert json.loads(line.split(":", 1)[1]) == SUMMARY[k]  # raw, as sent
    assert "105.125" in out and "MARGIN_LEVEL_TYPE_BASE" in out and "1438" in out


def test_the_command_sends_no_order_itself(env, capsys, monkeypatch):
    """Only the runtime sends: with no tick the command places nothing, says no result came, and takes its request back."""
    monkeypatch.setattr("ewbot.cli.time.sleep", lambda s: None)
    monkeypatch.setattr(cli, "TEST_TRADE_WAIT_S", 0.0)
    calls = list(env.ex.calls)
    assert main(["live", "test-trade", "--confirm"]) == 3
    err = capsys.readouterr().err
    assert "답하지 않음" in err and "거둠" in err
    new = env.ex.calls[len(calls):]
    assert not {"place_market", "place_bracket", "close_position", "cancel"} & set(new)
    assert env.journal.get(TEST_REQUEST_KEY) is None and not env.ex.fills


def test_the_side_option(env, capsys):
    assert main(["live", "test-trade", "--confirm", "--side", "short"]) == 0
    assert result(env)["side"] == -1 and "숏" in capsys.readouterr().out


def test_the_command_exits_non_zero_with_a_clear_message_on_each_failure(env, capsys):
    env.ex.place_market = lambda cid, side, size: PlaceResult(cid, None, False, "INSUFFICIENT_FUND: no")
    assert main(["live", "test-trade", "--confirm"]) == 1
    err = capsys.readouterr().err
    assert "시험 거래 실패" in err and "INSUFFICIENT_FUND" in err
    del env.ex.place_market
    env.ex.place_bracket = refuse_bracket
    assert main(["live", "test-trade", "--confirm"]) == 1
    err = capsys.readouterr().err
    assert "시험 거래 실패" in err and "손절" in err and "포지션 없음" in err


def test_the_command_warns_about_a_failed_margin_read_and_still_passes(env, capsys):
    env.ex.fail_next("balance_summary", Unavailable("down"))
    assert main(["live", "test-trade", "--confirm"]) == 0
    captured = capsys.readouterr()
    assert "시험 거래 성공" in captured.out and "경고" in captured.err and "down" in captured.err


def test_the_command_refuses_without_confirm(env, capsys):
    assert main(["live", "test-trade"]) == 2
    assert "--confirm" in capsys.readouterr().err and env.journal.get(TEST_REQUEST_KEY) is None and not env.ex.fills


@pytest.mark.parametrize("why,arrange", [
    ("CANARY", lambda hs: hs.journal.set_stage("SHADOW", "test", hs.clock.now)),
    ("CANARY", lambda hs: hs.journal.set_stage("FULL", "test", hs.clock.now)),
    ("일시 정지", lambda hs: hs.journal.put("paused", {"reason": "user /pause", "ts": hs.clock.now})),
    ("장이 열려", lambda hs: hs.ex.set_session("CLOSE")),
    ("포지션", lambda hs: setattr(hs.ex, "position", lambda: rt_mod.Position(1, 1, 40_000.0))),
    ("거래 키", lambda hs: setattr(hs.ex, "permissions", KeyPermissions(True, False, False))),
    ("런타임", lambda hs: hs.journal.put(rt_mod.HEARTBEAT_KEY, hs.clock.now - 600)),
    ("이미", lambda hs: hs.journal.put(TEST_REQUEST_KEY, {"id": "other", "side": 1, "ts": hs.clock.now})),
])
def test_the_command_refuses(env, capsys, why, arrange):
    arrange(env)
    assert main(["live", "test-trade", "--confirm"]) == 2
    assert why in capsys.readouterr().err
    assert env.journal.events(kind=TEST_TRADE) == [] and not env.ex.fills


def test_the_command_refuses_with_the_trade_key_missing(env, capsys, monkeypatch):
    def missing(s):
        raise SettingsError("EWBOT_COINBASE_KEY_FILE is not set")

    monkeypatch.setattr(live_app, "real_exchange", missing)
    assert main(["live", "test-trade", "--confirm"]) == 2
    assert "EWBOT_COINBASE_KEY_FILE" in capsys.readouterr().err and env.journal.get(TEST_REQUEST_KEY) is None


def test_the_command_refuses_without_a_journal(tmp_path, monkeypatch, capsys):
    """Refused, nothing sent: not exit 1, which says a test trade ran and failed."""
    monkeypatch.setattr(cli, "_live_settings", lambda: LiveSettings(data_dir=tmp_path / "none"))
    assert main(["live", "test-trade", "--confirm"]) == 2
    assert "no journal" in capsys.readouterr().err


def test_ctrl_c_withdraws_a_request_not_taken_yet(env, capsys, monkeypatch):
    def interrupted(s):
        raise KeyboardInterrupt

    monkeypatch.setattr("ewbot.cli.time.sleep", interrupted)
    assert main(["live", "test-trade", "--confirm"]) == 3
    err = capsys.readouterr().err
    assert "거둠" in err and env.journal.get(TEST_REQUEST_KEY) is None
    env.at(env.clock.now + 1)
    assert env.journal.events(kind=TEST_TRADE) == [] and not env.ex.fills  # never runs later


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_sigterm_and_sighup_withdraw_a_request_not_taken_yet(env, capsys, monkeypatch, sig):
    """Re-review: `docker compose exec` ended, a closed terminal: the request would have stood for up to 2 minutes."""
    before = signal.getsignal(sig)
    monkeypatch.setattr("ewbot.cli.time.sleep", lambda s: signal.raise_signal(sig))
    assert main(["live", "test-trade", "--confirm"]) == 3
    assert "거둠" in capsys.readouterr().err and env.journal.get(TEST_REQUEST_KEY) is None
    assert signal.getsignal(sig) is before  # the process's handlers are back
    env.at(env.clock.now + 1)
    assert env.journal.events(kind=TEST_TRADE) == [] and not env.ex.fills


def test_ctrl_c_after_the_runtime_took_it_says_it_may_be_running(env, capsys, monkeypatch):
    def taken_then_interrupted(s):
        env.journal.put(TEST_REQUEST_KEY, None)
        env.journal.put(TEST_ACTIVE_KEY, {"request": "x", "trade_id": 1, "side": 1, "ts": env.clock.now})
        raise KeyboardInterrupt

    monkeypatch.setattr("ewbot.cli.time.sleep", taken_then_interrupted)
    assert main(["live", "test-trade", "--confirm"]) == 3
    assert "실행 중" in capsys.readouterr().err


@pytest.mark.parametrize("key,value", [(TEST_REQUEST_KEY, {"id": "r", "side": 1, "ts": 1.0}),
                                       (TEST_ACTIVE_KEY, {"request": "r", "trade_id": 1, "side": 1, "ts": 1.0})])
def test_a_test_trade_under_way_holds_the_book_for_deploy_rebase_and_promotions(env, capsys, key, value):
    """Review finding: `live status --require-flat`, `rebase` and the promotions did not see a test trade asked for or under
    way, either of which can be an order of the next seconds. One whose position is not read yet is an open trade."""
    env.journal.put(key, value)
    assert main(["live", "status", "--json", "--require-flat", "--exchange"]) == 3
    captured = capsys.readouterr()
    assert "test trade" in captured.err and json.loads(captured.out)["test_trade"] is True
    assert main(["live", "rebase", "--confirm"]) == 2 and "test trade" in capsys.readouterr().err
    assert main(["live", "stage", "canary", "--restart", "--confirm"]) == 2 and "test trade" in capsys.readouterr().err
    env.journal.put(key, None)
    assert main(["live", "status", "--json", "--require-flat", "--exchange"]) == 0
    assert json.loads(capsys.readouterr().out)["test_trade"] is False


def test_the_command_reports_the_runtimes_own_refusal(env, capsys):
    """The state changed after the command's own check: the runtime's check is the one that counts."""
    env.rt.target = Target(QUIET, 40_000.0, None, None, entry(QUIET - H1), ())  # its 5 minutes are over: no entry from it
    assert main(["live", "test-trade", "--confirm"]) == 2
    assert "신호" in capsys.readouterr().err and not env.ex.fills
