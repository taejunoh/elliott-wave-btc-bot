from __future__ import annotations

import dataclasses

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
    assert plan(target(position=p), None, pos, [], CTX) == [Adopt(p)]
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
    assert observe(None, None, [], 1.0) == (None, [])
    assert tr.status == "OPEN" and not tr.tp1_filled  # the input is not modified
