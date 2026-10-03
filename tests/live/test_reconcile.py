from __future__ import annotations

import dataclasses

import pytest

from ewbot.live.exchange import Order, Position
from ewbot.live.reconcile import (
    Adopt,
    Alert,
    Cancel,
    Close,
    Enter,
    Leg,
    LiveTrade,
    PlanContext,
    Protect,
    Reduce,
    Skip,
    bracket_cid,
    close_cid,
    desired_legs,
    engine_position,
    entry_cid,
    expected_contracts,
    observe,
    plan,
    reduce_cid,
)
from ewbot.live.target import Target, TargetEntry, TargetPosition

T = 1_790_942_400  # a bar open (signal bar of the trade below)
CTX = PlanContext(now=T + 3630.0, ratio=1.0, tick=5.0, entry_contracts=1)


def tpos(**kw) -> TargetPosition:
    base = dict(trade_key=T + 3600, signal_ts=T, setup="W3", direction=1, entry_price=60_010.0, stop=59_000.0,
                stop0=59_000.0, tp1=61_000.0, tp2=62_000.0, tp1_done=False, contracts=5, remaining=5)
    return TargetPosition(**{**base, **kw})


def tentry(**kw) -> TargetEntry:
    base = dict(signal_ts=T + 7200, setup="C_END", direction=-1, entry_ref=60_000.0, stop=61_000.0, tp1=58_000.0,
                tp2=57_000.0, risk_mult=1.0, contracts=4, reason="r")
    return TargetEntry(**{**base, **kw})


def target(position=None, entry=None, exit_reason=None, bar=T + 3600, **kw) -> Target:
    return Target(bar, 60_000.0, position, exit_reason, entry, (), **kw)


def trade(**kw) -> LiveTrade:
    base = dict(trade_id=T, setup="W3", direction=1, contracts=3, tp1_n=1, entry_price=60_020.0,
                tp1_price=61_000.0, tp2_price=62_000.0, stop_spot=59_000.0, stop_price=59_000.0, opened_ts=T + 3605.0,
                legs=[Leg("tp1", 1, 61_000.0, 59_000.0, bracket_cid(T, "tp1", 1), "o1"),
                      Leg("tp2", 2, 62_000.0, 59_000.0, bracket_cid(T, "tp2", 2), "o2")], gen=2)
    return LiveTrade(**{**base, **kw})


def resting(tr: LiveTrade, **over) -> list[Order]:
    out = []
    for leg in tr.legs:
        o = Order(leg.order_id, leg.client_order_id, -tr.direction, "BRACKET", leg.size, 0, "OPEN",
                  leg.limit_price, leg.stop_price)
        out.append(dataclasses.replace(o, **over.get(leg.name, {})))
    return out


def test_client_order_ids_are_deterministic():
    assert entry_cid(T, 1) == f"ewb-e-{T}-L" and entry_cid(T, -1) == f"ewb-e-{T}-S"
    assert bracket_cid(T, "tp2", 3) == f"ewb-b-{T}-tp2-3"
    assert close_cid(T, 1) == f"ewb-x-{T}-1" and reduce_cid(T, 2) == f"ewb-r-{T}-2"


def test_trade_state_round_trips_through_json():
    tr = trade()
    assert LiveTrade.from_json(tr.to_json()) == tr


def test_flat_and_idle_needs_nothing_but_cleans_leftover_orders():
    assert plan(target(), None, None, [], CTX) == []
    leftover = resting(trade())
    assert plan(target(), None, None, leftover, CTX) == [Cancel(("o1", "o2"), "FLAT")]


def test_flat_with_a_queued_entry_enters_or_records_why_not():
    e = tentry()
    assert plan(target(entry=e), None, None, [], CTX) == [Enter(e, 1)]
    blocked = dataclasses.replace(CTX, entry_block="paused")
    assert plan(target(entry=e), None, None, [], blocked) == [Skip(e.signal_ts, "paused")]
    no_tp2 = tentry(tp2=None)
    assert isinstance(plan(target(entry=no_tp2), None, None, [], CTX)[0], Skip)


def test_an_unknown_position_is_adopted_or_closed():
    pos = Position(1, 2, 60_000.0)
    assert isinstance(plan(None, None, pos, [], CTX)[0], Alert)
    p = tpos()
    assert plan(target(position=p), None, pos, [], CTX) == [Adopt(p, 2)]
    assert plan(target(position=tpos(direction=-1)), None, pos, [], CTX) == [Close("UNKNOWN_POSITION")]
    assert plan(target(), None, pos, [], CTX) == [Close("UNKNOWN_POSITION")]


def test_matching_brackets_need_nothing():
    tr = trade()
    assert plan(target(position=tpos()), tr, Position(1, 3, 60_020.0), resting(tr), CTX) == []
    assert plan(None, tr, Position(1, 3, 60_020.0), resting(tr), CTX) == []  # no target: keep protecting


def test_a_moved_engine_stop_replaces_the_legs_with_the_mapped_stop():
    tr = trade()
    ctx = dataclasses.replace(CTX, ratio=1.0005)
    acts = plan(target(position=tpos(stop=59_500.0)), tr, Position(1, 3, 60_020.0), resting(tr), ctx)
    assert len(acts) == 1 and isinstance(acts[0], Protect)
    p = acts[0]
    assert p.stop_spot == 59_500.0 and p.stop_price == 59_525.0  # 59_529.75 floored to the $5 tick
    assert [(leg.name, leg.size, leg.limit_price, leg.stop_price) for leg in p.legs] == [
        ("tp1", 1, 61_000.0, 59_525.0), ("tp2", 2, 62_000.0, 59_525.0)]


def test_missing_or_partly_filled_legs_are_replaced():
    tr = trade()
    only_tp2 = [o for o in resting(tr) if o.order_id == "o2"]
    assert isinstance(plan(target(position=tpos()), tr, Position(1, 3, 60_020.0), only_tp2, CTX)[0], Protect)
    part = resting(tr, tp2={"filled": 1})
    assert isinstance(plan(target(position=tpos()), tr, Position(1, 3, 60_020.0), part, CTX)[0], Protect)


def test_within_the_entry_hour_the_queued_engine_entry_is_the_position():
    tr = trade()
    e = tentry(signal_ts=T, direction=1, stop=59_000.0, tp1=61_000.0, tp2=62_000.0)
    pos = Position(1, 3, 60_020.0)
    assert plan(target(entry=e), tr, pos, resting(tr), CTX) == []  # no close: the engine fills it at this open
    moved = tentry(signal_ts=T, direction=1, stop=59_500.0, tp1=61_000.0, tp2=62_000.0)
    assert isinstance(plan(target(entry=moved), tr, pos, resting(tr), CTX)[0], Protect)
    adopted = plan(target(entry=e), None, pos, [], CTX)
    assert len(adopted) == 1 and isinstance(adopted[0], Adopt) and adopted[0].target.signal_ts == T


def test_engine_exit_closes_and_a_flip_enters_after_the_close():
    tr = trade()
    pos = Position(1, 3, 60_020.0)
    assert plan(target(), tr, pos, resting(tr), CTX) == [Close("ENGINE_FLAT")]
    assert plan(target(position=tpos(), exit_reason="TIME"), tr, pos, resting(tr), CTX) == [Close("TIME")]
    e = tentry()
    assert plan(target(position=tpos(), exit_reason="FLIP", entry=e), tr, pos, resting(tr), CTX) == [Close("FLIP"), Enter(e, 1)]
    other = tpos(signal_ts=T + 7200, trade_key=T + 10800)
    assert plan(target(position=other), tr, pos, resting(tr), CTX) == [Close("ENGINE_OTHER_TRADE")]
    assert plan(target(position=tpos()), tr, Position(-1, 3, 60_020.0), [], CTX) == [Close("DIRECTION_MISMATCH")]


def test_an_exit_carried_across_the_halt_or_a_stop_crossed_in_it_closes():
    """At the CFM halt bar the engine still holds the position: an exit queued at the bar before the halt
    is still queued (exit_queued_ts < bar), and a stop the halt bar crossed stops it out at the reopen. Both
    close the live trade (the executor waits for an OPEN session); neither position is adopted."""
    tr = trade()
    pos = Position(1, 3, 60_020.0)
    halt = T + 7200
    carried = target(position=tpos(), exit_reason="LINEAGE", bar=halt, exit_queued_ts=halt - 3600)
    assert plan(carried, tr, pos, resting(tr), CTX) == [Close("LINEAGE")]
    assert plan(carried, tr, pos, resting(tr), dataclasses.replace(CTX, session_open=False)) == [Close("LINEAGE")]
    stopped = target(position=tpos(), bar=halt, stop_pending_reopen=True)
    assert plan(stopped, tr, pos, resting(tr), CTX) == [Close("STOP_HALT")]
    both = target(position=tpos(), exit_reason="BREAKER", bar=halt, exit_queued_ts=halt - 3600, stop_pending_reopen=True)
    assert plan(both, tr, pos, resting(tr), CTX) == [Close("BREAKER")]  # the queued exit fills first in the engine
    assert engine_position(carried) is None and engine_position(stopped) is None
    assert plan(stopped, None, pos, [], CTX) == [Close("UNKNOWN_POSITION")]
    other = tpos(signal_ts=T + 7200, trade_key=T + 10800)
    assert plan(target(position=other, bar=halt, stop_pending_reopen=True), tr, pos, resting(tr), CTX) == [
        Close("ENGINE_OTHER_TRADE")]


def test_engine_tp1_without_the_live_tp1_reduces_at_market_then_protects_the_rest_at_break_even():
    tr = trade()
    acts = plan(target(position=tpos(tp1_done=True, stop=60_010.0)), tr, Position(1, 3, 60_020.0), resting(tr), CTX)
    assert acts[0] == Reduce(1, "TP1_SYNC")
    p = acts[1]
    assert isinstance(p, Protect) and [(leg.name, leg.size) for leg in p.legs] == [("tp2", 2)]
    assert p.stop_price == 60_020.0  # max(engine break-even 60_010, live entry 60_020)
    closed = dataclasses.replace(CTX, session_open=False)  # no market order now: keep both legs until the reopen
    assert plan(target(position=tpos(tp1_done=True, stop=59_000.0)), tr, Position(1, 3, 60_020.0), resting(tr), closed) == []


def test_after_the_live_tp1_the_single_leg_stops_at_break_even():
    tr = trade(tp1_filled=True)
    legs, stop = desired_legs(tr, 2, 59_000.0, 1.0, 5.0)
    assert stop == 60_020.0 and [(leg.name, leg.size, leg.limit_price) for leg in legs] == [("tp2", 2, 62_000.0)]
    short = trade(direction=-1, entry_price=60_002.0, stop_price=61_000.0, stop_spot=61_000.0, tp1_filled=True)
    assert desired_legs(short, 2, 61_000.0, 1.0, 5.0)[1] == 60_005.0  # short break-even rounds up


def test_one_contract_trades_use_one_tp1_bracket():
    tr = trade(contracts=1, tp1_n=1, legs=[Leg("tp1", 1, 61_000.0, 59_000.0, bracket_cid(T, "tp1", 1), "o1")])
    legs, _ = desired_legs(tr, 1, 59_000.0, 1.0, 5.0)
    assert [(leg.name, leg.size, leg.limit_price) for leg in legs] == [("tp1", 1, 61_000.0)]
    assert plan(target(position=tpos()), tr, Position(1, 1, 60_020.0), resting(tr), CTX) == []


def test_foreign_orders_raise_an_alert():
    tr = trade()
    foreign = Order("f1", "manual-1", -1, "OTHER", 1, 0, "OPEN")
    acts = plan(target(position=tpos()), tr, Position(1, 3, 60_020.0), resting(tr) + [foreign], CTX)
    assert acts == [Alert("FOREIGN_ORDERS", "f1")]


def test_observe_detects_close_tp1_and_protection_gaps():
    tr = trade()
    t2, ev = observe(tr, None, [], now=100.0)
    assert t2.status == "CLOSED" and t2.exit_reason == "BRACKET" and ev[0][0] == "CLOSED"
    t3, ev = observe(tr, Position(1, 2, 60_020.0), resting(tr)[1:], now=100.0)
    assert t3.tp1_filled and [k for k, _ in ev] == ["TP1_FILLED"]
    t4, ev = observe(tr, Position(1, 3, 60_020.0), resting(tr)[1:], now=100.0)
    assert t4.unprotected_since == 100.0 and [k for k, _ in ev] == ["UNPROTECTED"]
    t5, ev = observe(t4, Position(1, 3, 60_020.0), resting(tr)[1:], now=161.0)
    assert t5.incident_unprotected and [k for k, _ in ev] == ["UNPROTECTED_60S"]
    t6, ev = observe(t5, Position(1, 3, 60_020.0), resting(tr), now=170.0)
    assert t6.unprotected_since is None and ev == [("PROTECTED", {"trade_id": T, "after_s": 70.0})]
    assert not t6.incident_unprotected  # PROTECTED ends the incident: a later gap is a new one
    t7, _ = observe(t6, Position(1, 3, 60_020.0), resting(tr)[1:], now=200.0)
    t8, ev = observe(t7, Position(1, 3, 60_020.0), resting(tr)[1:], now=261.0)
    assert t8.incident_unprotected and [k for k, _ in ev] == ["UNPROTECTED_60S"]
    assert observe(None, None, [], 1.0) == (None, [])
    assert tr.status == "OPEN" and not tr.tp1_filled  # the input is not modified


def five() -> LiveTrade:
    """A 5-contract trade: a 2-contract TP1 leg (o1) and a 3-contract TP2 leg (o2)."""
    return trade(contracts=5, tp1_n=2, legs=[Leg("tp1", 2, 61_000.0, 59_000.0, bracket_cid(T, "tp1", 1), "o1"),
                                             Leg("tp2", 3, 62_000.0, 59_000.0, bracket_cid(T, "tp2", 2), "o2")])


def after_tp1() -> LiveTrade:
    """trade() after its live TP1: the 2-contract TP2 leg (o3) at break-even."""
    return trade(tp1_filled=True, stop_price=60_020.0,
                 legs=[Leg("tp2", 2, 62_000.0, 60_020.0, bracket_cid(T, "tp2", 3), "o3")])


def test_tp1_is_filled_only_when_the_tp1_leg_explains_the_reduction():
    tr = trade()
    done, ev = observe(tr, Position(1, 2, 60_020.0), resting(tr, tp1={"filled": 1}), now=100.0)  # filled, not yet gone
    assert done.tp1_filled and [k for k, _ in ev] == ["TP1_FILLED"] and expected_contracts(done) == 2
    # one contract less while the TP1 leg still rests unfilled: not the TP1 (no break-even)
    t2, ev = observe(tr, Position(1, 2, 60_020.0), resting(tr), now=100.0)
    assert not t2.tp1_filled and ev == [("UNEXPLAINED_REDUCTION", {"trade_id": T, "expected": 3, "position": 2})]
    assert (t2.contracts, t2.tp1_n, t2.stop_price) == (2, 1, 59_000.0) and expected_contracts(t2) == 2
    acts = plan(target(position=tpos()), t2, Position(1, 2, 60_020.0), resting(tr), CTX)  # protect what is left
    assert len(acts) == 1 and isinstance(acts[0], Protect)
    assert [(leg.name, leg.size, leg.stop_price) for leg in acts[0].legs] == [
        ("tp1", 1, 59_000.0), ("tp2", 1, 59_000.0)]
    assert observe(t2, Position(1, 2, 60_020.0), resting(tr), now=110.0)[1] == []  # reported once
    # down to one contract: a one-contract trade (TP1 + stop)
    t3, ev = observe(tr, Position(1, 1, 60_020.0), resting(tr), now=100.0)
    assert (t3.contracts, t3.tp1_n) == (1, 1) and ev[0][0] == "UNEXPLAINED_REDUCTION"
    assert [(leg.name, leg.size) for leg in desired_legs(t3, 1, 59_000.0, 1.0, 5.0)[0]] == [("tp1", 1)]


def test_a_partial_tp1_keeps_the_tp1_leg_for_the_rest():
    tr = five()
    part = resting(tr, tp1={"filled": 1})
    t2, ev = observe(tr, Position(1, 4, 60_020.0), part, now=100.0)
    assert not t2.tp1_filled and (t2.contracts, t2.tp1_n) == (4, 1)
    assert ev == [("PARTIAL_FILL", {"trade_id": T, "contracts": 4})]
    assert plan(target(position=tpos()), t2, Position(1, 4, 60_020.0), part, CTX) == []  # the TP1 leg keeps 1
    moved = plan(target(position=tpos(stop=59_500.0)), t2, Position(1, 4, 60_020.0), part, CTX)
    assert [(leg.name, leg.size) for leg in moved[0].legs] == [("tp1", 1), ("tp2", 3)]  # replaced for the rest only
    t3, ev = observe(t2, Position(1, 3, 60_020.0), part[1:], now=110.0)  # the rest of the TP1 leg filled
    assert t3.tp1_filled and [k for k, _ in ev] == ["TP1_FILLED"] and expected_contracts(t3) == 3
    sync = plan(target(position=tpos(tp1_done=True, stop=60_010.0)), t2, Position(1, 4, 60_020.0), part, CTX)
    assert sync[0] == Reduce(1, "TP1_SYNC")  # the engine's TP1: only what is left of the live TP1 leg


def test_tp1_and_a_tp2_fill_in_one_interval_and_reductions_after_tp1():
    tr = five()
    t2, ev = observe(tr, Position(1, 2, 60_020.0), resting(tr, tp2={"filled": 1})[1:], now=100.0)
    assert t2.tp1_filled and [k for k, _ in ev] == ["TP1_FILLED"] and expected_contracts(t2) == 2
    after = after_tp1()
    t3, ev = observe(after, Position(1, 1, 60_020.0), resting(after, tp2={"filled": 1}), now=100.0)
    assert ev == [("PARTIAL_FILL", {"trade_id": T, "contracts": 1})] and expected_contracts(t3) == 1
    t4, ev = observe(after, Position(1, 1, 60_020.0), resting(after), now=100.0)
    assert ev[0] == ("UNEXPLAINED_REDUCTION", {"trade_id": T, "expected": 2, "position": 1})
    assert t4.tp1_filled and expected_contracts(t4) == 1 and t4.stop_price == 60_020.0


def test_excess_contracts_are_reduced_and_the_legs_cover_only_the_expected_size():
    tr = trade()  # 3 contracts
    pos = Position(1, 5, 60_020.0)
    alert = Alert("EXCESS_POSITION", f"trade {T}: position 5 > 3 expected")
    assert plan(target(position=tpos()), tr, pos, resting(tr), CTX) == [alert, Reduce(2, "EXCESS")]
    closed = dataclasses.replace(CTX, session_open=False)  # no market order now: the legs stay at 3
    assert plan(target(position=tpos()), tr, pos, resting(tr), closed) == [alert]
    acts = plan(None, tr, pos, [], CTX)  # without a target too; the missing legs are placed for 3, never 5
    assert acts[:2] == [alert, Reduce(2, "EXCESS")] and isinstance(acts[2], Protect)
    assert [(leg.name, leg.size) for leg in acts[2].legs] == [("tp1", 1), ("tp2", 2)]
    after = after_tp1()
    acts = plan(target(position=tpos(tp1_done=True)), after, Position(1, 3, 60_020.0), resting(after), CTX)
    assert acts == [Alert("EXCESS_POSITION", f"trade {T}: position 3 > 2 expected"), Reduce(1, "EXCESS")]


def test_an_adopted_position_is_capped_at_the_stage_maximum():
    pos = Position(1, 3, 60_000.0)
    p = tpos()
    canary = dataclasses.replace(CTX, max_contracts=1)
    alert = Alert("EXCESS_POSITION", "position 3 > stage maximum 1: adopting 1")
    assert plan(target(position=p), None, pos, [], canary) == [alert, Adopt(p, 1), Reduce(2, "EXCESS")]
    closed = dataclasses.replace(canary, session_open=False)
    assert plan(target(position=p), None, pos, [], closed) == [alert, Adopt(p, 1)]
    assert plan(target(position=p), None, pos, [], dataclasses.replace(CTX, max_contracts=3)) == [Adopt(p, 3)]
    with pytest.raises(ValueError):
        dataclasses.replace(CTX, max_contracts=0)


def test_a_stop_move_without_quotes_waits_for_them():
    tr = trade()
    pos = Position(1, 3, 60_020.0)
    noq = dataclasses.replace(CTX, ratio=None)
    moved = target(position=tpos(stop=59_500.0))
    assert plan(moved, tr, pos, resting(tr), noq) == []  # the engine stop (spot) is not recorded as moved
    acts = plan(moved, tr, pos, resting(tr)[1:], noq)  # a missing leg is re-placed at the stop in force
    assert len(acts) == 1 and (acts[0].stop_spot, acts[0].stop_price) == (59_000.0, 59_000.0)
    acts = plan(moved, tr, pos, resting(tr), dataclasses.replace(CTX, ratio=1.0005))  # quotes are back: the move
    assert len(acts) == 1 and (acts[0].stop_spot, acts[0].stop_price) == (59_500.0, 59_525.0)


def test_a_signal_already_traded_is_never_entered_again():
    e = tentry(signal_ts=T, direction=1, stop=59_000.0, tp1=61_000.0, tp2=62_000.0)
    closed_early = trade(status="CLOSED", exit_reason="BRACKET")  # stopped out within its entry hour
    assert plan(target(entry=e), closed_early, None, [], CTX) == []
    assert plan(target(entry=e), trade(), None, [], CTX) == []
    assert plan(target(entry=tentry()), closed_early, None, [], CTX) == [Enter(tentry(), 1)]  # another signal


def test_a_close_waiting_for_the_session_keeps_the_position_protected():
    tr = trade()
    pos = Position(1, 3, 60_020.0)
    closed = dataclasses.replace(CTX, session_open=False)
    halt = T + 7200
    carried = target(position=tpos(stop=59_500.0), exit_reason="LINEAGE", bar=halt, exit_queued_ts=halt - 3600)
    assert plan(carried, tr, pos, resting(tr), closed) == [Close("LINEAGE")]  # the legs rest: kept
    acts = plan(carried, tr, pos, resting(tr)[1:], closed)
    assert acts[0] == Close("LINEAGE") and isinstance(acts[1], Protect) and len(acts) == 2
    assert (acts[1].stop_spot, acts[1].stop_price) == (59_000.0, 59_000.0)  # the stop in force, not moved
    assert [(leg.name, leg.size) for leg in acts[1].legs] == [("tp1", 1), ("tp2", 2)]
    assert plan(carried, tr, pos, resting(tr)[1:], CTX) == [Close("LINEAGE")]  # open: the close goes out now
    assert plan(target(), tr, pos, [], closed)[1:] == [acts[1]]  # ENGINE_FLAT as well
    wrong = plan(target(position=tpos()), tr, Position(-1, 3, 60_020.0), [], closed)
    assert wrong[0] == Close("DIRECTION_MISMATCH") and isinstance(wrong[1], Alert) and len(wrong) == 2


def test_a_tp2_leg_without_a_tp2_price_is_refused():
    with pytest.raises(ValueError, match="TP2"):
        desired_legs(trade(tp2_price=None), 3, 59_000.0, 1.0, 5.0)
    assert desired_legs(trade(tp2_price=None, contracts=1, tp1_n=1), 1, 59_000.0, 1.0, 5.0)[0][0].name == "tp1"
