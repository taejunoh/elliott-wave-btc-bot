from __future__ import annotations

import dataclasses

import pytest

from ewbot.live.exchange import Order, Position
from ewbot.live.reconcile import (
    REDUCE_SETTLE_S,
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
    after_reduce,
    bracket_cid,
    close_cid,
    desired_legs,
    engine_position,
    entry_cid,
    entry_position,
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


def test_the_break_even_stop_is_at_least_the_live_entry_while_the_market_allows():
    """Interpretation 5: after the live TP1 the stop is at least the live entry - a long's rounds up to the tick, a
    short's down. A stop must rest on the loss side of the market (else the venue refuses it or it fills at once):
    while the futures price is less than a tick beyond that level, or unknown, it is the tick on the other side of the
    entry, and once a stop rests at the rounded level an engine move never loosens it."""
    tr = trade(entry_price=60_012.0, tp1_filled=True)
    assert desired_legs(tr, 2, 59_000.0, 1.0, 5.0, price=60_500.0)[1] == 60_015.0  # rounded up: >= the live entry
    assert desired_legs(tr, 2, 59_000.0, 1.0, 5.0, price=60_020.0)[1] == 60_015.0  # a tick below the market
    assert desired_legs(tr, 2, 59_000.0, 1.0, 5.0, price=60_019.0)[1] == 60_010.0  # within a tick: below the entry
    assert desired_legs(tr, 2, 59_000.0, 1.0, 5.0)[1] == 60_010.0  # no price: the same
    held = trade(entry_price=60_012.0, tp1_filled=True, stop_price=60_015.0)  # rests at the rounded-up level
    assert desired_legs(held, 2, 59_100.0, 1.0, 5.0, price=60_016.0)[1] == 60_015.0
    assert desired_legs(trade(tp1_filled=True), 2, 59_000.0, 1.0, 5.0, price=60_021.0)[1] == 60_020.0  # on the grid
    short = trade(direction=-1, entry_price=60_002.0, stop_price=61_000.0, stop_spot=61_000.0, tp1_filled=True)
    assert desired_legs(short, 2, 61_000.0, 1.0, 5.0, price=59_500.0)[1] == 60_000.0  # rounded down: <= the entry
    assert desired_legs(short, 2, 61_000.0, 1.0, 5.0, price=59_996.0)[1] == 60_005.0  # within a tick: above it
    sync = trade(entry_price=60_012.0)  # the planner uses its context's futures price
    acts = plan(target(position=tpos(tp1_done=True)), sync, Position(1, 3, 60_012.0), resting(sync),
                dataclasses.replace(CTX, price=60_500.0))
    assert acts[0] == Reduce(1, "TP1_SYNC") and acts[1].stop_price == 60_015.0
    acts = plan(target(position=tpos(tp1_done=True)), sync, Position(1, 3, 60_012.0), resting(sync), CTX)
    assert acts[1].stop_price == 60_010.0


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


def test_excess_contracts_are_reduced_or_protected_until_they_can_be():
    tr = trade()  # 3 contracts
    pos = Position(1, 5, 60_020.0)
    alert = Alert("EXCESS_POSITION", f"trade {T}: position 5 > 3 expected")
    assert plan(target(position=tpos()), tr, pos, resting(tr), CTX) == [alert, Reduce(2, "EXCESS")]
    closed = dataclasses.replace(CTX, session_open=False)  # no market order now: the legs cover all 5 until then
    acts = plan(target(position=tpos()), tr, pos, resting(tr), closed)
    assert acts[0] == alert and len(acts) == 2 and isinstance(acts[1], Protect)
    assert [(leg.name, leg.size, leg.stop_price) for leg in acts[1].legs] == [("tp1", 1, 59_000.0), ("tp2", 4, 59_000.0)]
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


def short(**kw) -> LiveTrade:
    """trade() mirrored: a 3-contract short at 60_002, stop 61_000, TP1 59_000 (o1, 1) and TP2 58_000 (o2, 2)."""
    base = dict(direction=-1, entry_price=60_002.0, tp1_price=59_000.0, tp2_price=58_000.0, stop_spot=61_000.0,
                stop_price=61_000.0, legs=[Leg("tp1", 1, 59_000.0, 61_000.0, bracket_cid(T, "tp1", 1), "o1"),
                                          Leg("tp2", 2, 58_000.0, 61_000.0, bracket_cid(T, "tp2", 2), "o2")])
    return trade(**{**base, **kw})


def spos(**kw) -> TargetPosition:
    base = dict(direction=-1, entry_price=60_000.0, stop=61_000.0, stop0=61_000.0, tp1=59_000.0, tp2=58_000.0)
    return tpos(**{**base, **kw})


def test_short_trades_protect_reduce_and_fold_like_longs():
    tr = short()
    pos = Position(-1, 3, 60_002.0)
    assert plan(target(position=spos()), tr, pos, resting(tr), CTX) == []  # buy-side brackets above / below
    moved = plan(target(position=spos(stop=60_500.0)), tr, pos, resting(tr), dataclasses.replace(CTX, ratio=1.0005))
    assert [(leg.name, leg.size, leg.limit_price, leg.stop_price) for leg in moved[0].legs] == [
        ("tp1", 1, 59_000.0, 60_535.0), ("tp2", 2, 58_000.0, 60_535.0)]  # 60_530.25 rounded up for a short
    big = Position(-1, 5, 60_002.0)
    alert = Alert("EXCESS_POSITION", f"trade {T}: position 5 > 3 expected")
    assert plan(target(position=spos()), tr, big, resting(tr), CTX) == [alert, Reduce(2, "EXCESS")]
    closed = plan(target(position=spos()), tr, big, resting(tr), dataclasses.replace(CTX, session_open=False))
    assert closed[0] == alert and [(leg.name, leg.size, leg.stop_price) for leg in closed[1].legs] == [
        ("tp1", 1, 61_000.0), ("tp2", 4, 61_000.0)]
    sync = plan(target(position=spos(tp1_done=True)), tr, pos, resting(tr), CTX)
    assert sync[0] == Reduce(1, "TP1_SYNC") and sync[1].stop_price == 60_005.0  # break-even rounds up for a short
    t2, ev = observe(tr, Position(-1, 2, 60_002.0), resting(tr)[1:], now=100.0)  # the TP1 leg bought back 1
    assert t2.tp1_filled and [k for k, _ in ev] == ["TP1_FILLED"]
    acts = plan(target(position=spos(tp1_done=True)), t2, Position(-1, 2, 60_002.0), resting(tr)[1:], CTX)
    assert [(leg.name, leg.size, leg.limit_price, leg.stop_price) for leg in acts[0].legs] == [
        ("tp2", 2, 58_000.0, 60_005.0)]
    t3, ev = observe(tr, Position(-1, 2, 60_002.0), resting(tr), now=100.0)  # bought back by hand: not ours
    assert ev[0] == ("UNEXPLAINED_REDUCTION", {"trade_id": T, "expected": 3, "position": 2})
    assert not t3.tp1_filled and (t3.contracts, t3.tp1_n, t3.stop_price) == (2, 1, 61_000.0)


def tp1_sync_pending(tr: LiveTrade, filled: int | None = None) -> LiveTrade:
    """`tr` once the executor has recorded Reduce(1, TP1_SYNC) against a 3-contract position (before sending it)."""
    pr = {"cid": reduce_cid(T, 1), "size": 1, "reason": "TP1_SYNC", "before": 3, "ts": 100.0,
          "after": after_reduce(tr, 1, "TP1_SYNC"), "filled": filled}
    return dataclasses.replace(tr, pending_reduce=pr)


def test_a_reduce_whose_answer_was_lost_is_ours_once_the_position_shows_it():
    """Probe (a): the TP1_SYNC reduce came back ambiguous (PENDING) but filled. It is ours - never an unexplained
    reduction - and no other reduce is planned while it is pending (the executor resolves it by its id first)."""
    tr = tp1_sync_pending(trade())
    eng = target(position=tpos(tp1_done=True))
    assert plan(eng, tr, Position(1, 3, 60_020.0), resting(tr), CTX) == []  # not in the position yet: both legs stay
    acts = plan(eng, tr, Position(1, 4, 60_020.0), resting(tr), CTX)
    assert acts[0].kind == "EXCESS_POSITION" and not [a for a in acts if isinstance(a, Reduce)]
    seen, ev = observe(tr, Position(1, 2, 60_020.0), resting(tr), now=110.0)  # it filled
    assert ev == [("REDUCE_SETTLED", {"trade_id": T, "reason": "TP1_SYNC", "size": 1, "position": 2})]
    assert seen.tp1_filled and seen.pending_reduce is None and expected_contracts(seen) == 2
    acts = plan(eng, seen, Position(1, 2, 60_020.0), resting(tr), CTX)
    assert len(acts) == 1 and isinstance(acts[0], Protect)  # the rest at break-even, never a second TP1 sale
    assert [(leg.name, leg.size, leg.stop_price) for leg in acts[0].legs] == [("tp2", 2, 60_020.0)]
    # the reduce and a TP2 fill in one interval: the reduce first, then the legs explain the rest
    seen, ev = observe(tr, Position(1, 1, 60_020.0), resting(tr, tp2={"filled": 1}), now=110.0)
    assert [k for k, _ in ev] == ["REDUCE_SETTLED", "PARTIAL_FILL"] and expected_contracts(seen) == 1
    assert observe(tr, Position(1, 3, 60_020.0), resting(tr), now=900.0)[0].pending_reduce is not None  # unknown: kept


def test_a_filled_reduce_the_position_does_not_show_yet_is_not_an_excess():
    """The venue reported the reduce filled (the executor applied it) but the position read lags: no EXCESS, no
    gap in protection. A minute later the position wins (e.g. contracts added by hand meanwhile)."""
    tr = tp1_sync_pending(trade(tp1_filled=True, stop_price=60_020.0,
                                legs=[Leg("tp2", 2, 62_000.0, 60_020.0, bracket_cid(T, "tp2", 3), "o3")]), filled=1)
    lagging = Position(1, 3, 60_020.0)
    eng = target(position=tpos(tp1_done=True))
    assert plan(eng, tr, lagging, resting(tr), CTX) == []
    assert observe(tr, lagging, resting(tr), now=100.0 + REDUCE_SETTLE_S)[1] == []
    seen, ev = observe(tr, lagging, resting(tr), now=101.0 + REDUCE_SETTLE_S)
    assert [k for k, _ in ev] == ["REDUCE_NOT_SEEN", "UNPROTECTED"] and seen.pending_reduce is None
    assert plan(eng, seen, lagging, resting(tr), CTX) == [
        Alert("EXCESS_POSITION", f"trade {T}: position 3 > 2 expected"), Reduce(1, "EXCESS")]
    assert [k for k, _ in observe(tr, Position(1, 2, 60_020.0), resting(tr), now=110.0)[1]] == ["REDUCE_SETTLED"]


def test_a_vanished_tp1_leg_is_never_synced_at_market():
    """Probe (b): the TP1 leg filled between the position read and the order read (or the order list is ahead of
    the position): the position still shows 5 while the TP1 leg is gone. The engine's TP1 must not be synced by a
    market sale of what the leg already sold (the executor looks a vanished leg up before re-placing it); the next
    read folds the fill as the live TP1."""
    tr = five()
    stale, gone = Position(1, 5, 60_020.0), resting(tr)[1:]
    seen, ev = observe(tr, stale, gone, now=100.0)
    assert [k for k, _ in ev] == ["UNPROTECTED"] and not seen.tp1_filled
    eng = target(position=tpos(tp1_done=True))
    assert not [a for a in plan(eng, seen, stale, gone, CTX) if isinstance(a, Reduce)]
    half = resting(tr, tp1={"filled": 1})  # half the TP1 leg filled, the position does not show it yet
    assert plan(eng, tr, stale, half, CTX)[0] == Reduce(1, "TP1_SYNC")  # only what the leg has left
    later, ev = observe(seen, Position(1, 3, 60_020.0), gone, now=110.0)  # the fill shows: the live TP1
    assert [k for k, _ in ev] == ["TP1_FILLED", "PROTECTED"] and expected_contracts(later) == 3


def own_entry(**kw) -> TargetPosition:
    """Our entry intent's payload as the engine position it would be (the runtime: Executor.unsettled_entry)."""
    payload = {"signal_ts": T, "setup": "W3", "direction": 1, "contracts": 1, "stop_spot": 59_000.0, "tp1_spot": 61_000.0,
               "tp2_spot": 62_000.0, "stop": 59_025.0, "tp1": 61_030.0, "tp2": 62_030.0, "ratio": 1.0005,
               "ref": 60_040.0, **kw}
    return entry_position(payload)


def test_an_entry_intent_reads_as_the_engine_position_it_opens():
    p = own_entry()
    assert (p.signal_ts, p.trade_key, p.setup, p.direction, p.stop, p.stop0, p.tp1, p.tp2) == (
        T, T + 3600, "W3", 1, 59_000.0, 59_000.0, 61_000.0, 62_000.0)
    assert p.entry_price == pytest.approx(60_040.0 / 1.0005) and (p.contracts, p.remaining, p.tp1_done) == (1, 1, False)


def test_without_a_target_a_position_on_the_side_of_our_unsettled_entry_is_protected_from_it():
    """Final review A-A: killed after the venue took our entry, restarted while the hour has no target (the candles
    cannot be had): the position is taken over from the entry's own intent at once (the executor settles it as ours
    or foreign: ADOPTED_PROVISIONAL, then RECOVERED_ENTRY or ADOPTED) instead of waiting up to an hour unprotected. It
    is our own entry, no manual intervention: no UNKNOWN_POSITION alert (an incident that would pause CANARY)."""
    own = own_entry()
    pos = Position(1, 1, 60_000.0)
    with_own = dataclasses.replace(CTX, own_entry=own)
    acts = plan(None, None, pos, [], with_own)
    assert acts == [Adopt(own, 1)]
    waiting = [Alert("UNKNOWN_POSITION", "+1 x1: waiting for a target")]
    assert plan(None, None, pos, [], CTX) == waiting  # no unsettled entry of ours
    assert plan(None, None, Position(-1, 1, 60_000.0), [], with_own) == [
        Alert("UNKNOWN_POSITION", "-1 x1: waiting for a target")]  # the other side: not our entry
    capped = dataclasses.replace(with_own, max_contracts=1)
    acts = plan(None, None, Position(1, 3, 60_000.0), [], capped)
    assert acts == [Alert("EXCESS_POSITION", "position 3 > stage maximum 1: adopting 1"), Adopt(own, 1),
                    Reduce(2, "EXCESS")]  # more than our entry: the excess is still an incident
    assert plan(target(position=tpos(direction=-1)), None, pos, [], with_own) == [Close("UNKNOWN_POSITION")]  # a target rules


def test_a_flat_read_while_the_legs_still_rest_whole_needs_a_second_read():
    """Final review A-D: the position reads flat while every leg of the trade still rests with all the trade holds
    - nothing of them filled. No bracket explains that: one wrong read must not close the trade's record, cancel its
    brackets or open the next entry on top of a real position. The next read decides (flat again: CLOSED)."""
    tr = trade()
    t1, ev = observe(tr, None, resting(tr), now=100.0)
    assert (t1.status, t1.flat_seen) == ("OPEN", 100.0) and ev == [("FLAT_UNCONFIRMED", {"trade_id": T})]
    assert plan(target(position=tpos(), entry=tentry()), t1, None, resting(tr), CTX) == []  # no cancel, no entry
    t2, ev = observe(t1, None, resting(tr), now=110.0)
    assert (t2.status, t2.exit_reason) == ("CLOSED", "BRACKET") and ev[0][0] == "CLOSED"
    t3, ev = observe(t1, Position(1, 3, 60_020.0), resting(tr), now=110.0)  # the position again: a wrong read
    assert (t3.status, t3.flat_seen) == ("OPEN", None) and ev == []
    for orders in (resting(tr, tp1={"filled": 1}), resting(tr)[1:], []):  # a leg filled or gone: flat at once
        t4, ev = observe(tr, None, orders, now=100.0)
        assert t4.status == "CLOSED" and ev[0][0] == "CLOSED"
