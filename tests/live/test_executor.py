from __future__ import annotations

import pytest

from ewbot.live.exchange import ExchangeError, RateLimited, Unavailable
from ewbot.live.executor import Executor
from ewbot.live.reconcile import (
    Adopt,
    Alert,
    Close,
    Enter,
    Leg,
    Protect,
    Reduce,
    Skip,
    desired_legs,
    expected_contracts,
    observe,
    plan,
)
from ewbot.live.sim import SimCrash
from tests.live.helpers import T0, Alerts, Clock, ctx, entry, journal, position, sim, target


def poll(exe: Executor, ex, clock: Clock, tgt) -> list[str]:
    """One runtime pass: a pending reduce resolved by its id first, then observe -> plan -> execute."""
    exe.resolve_reduce()
    seen, events = observe(exe.trade(), ex.position(), ex.open_orders(), clock())
    exe.save(seen)
    c = ctx(clock)
    exe.execute(plan(tgt, seen, ex.position(), ex.open_orders(), c), c)
    return [k for k, _ in events]


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    ex = sim(clock)
    jr = journal(tmp_path)
    alerts = Alerts()
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep, alert=alerts)
    yield clock, ex, jr, alerts, exe
    jr.close()


def test_entry_sends_one_market_order_then_two_brackets(env):
    clock, ex, jr, alerts, exe = env
    tr = exe.enter(Enter(entry(), 3), ctx(clock))
    assert tr is not None and (tr.contracts, tr.tp1_n, tr.entry_price) == (3, 1, 60_002.5)
    assert ex.position().contracts == 3
    rest = sorted((o.client_order_id, o.size, o.limit_price, o.stop_price) for o in ex.open_orders())
    assert rest == [(f"ewb-b-{T0}-tp1-1", 1, 61_000.0, 59_000.0), (f"ewb-b-{T0}-tp2-2", 2, 62_000.0, 59_000.0)]
    assert jr.intent(f"ewb-e-{T0}-L")["status"] == "ACCEPTED"
    assert [e["kind"] for e in jr.events()] == ["ENTRY", "PROTECTED_FIRST"]
    assert jr.events(kind="PROTECTED_FIRST")[0]["data"]["after_entry_s"] <= 30.0
    assert alerts.kinds() == ["ENTRY"]
    assert exe.enter(Enter(entry(), 3), ctx(clock)) is None  # an entry is never sent twice
    assert ex.calls.count("place_market") == 1


def test_one_contract_uses_one_bracket_and_levels_are_mapped(env):
    clock, ex, jr, _, exe = env
    tr = exe.enter(Enter(entry(stop=59_001.0, tp1=61_003.0), 1), ctx(clock, ratio=1.001))
    assert (tr.stop_price, tr.tp1_price, tr.tp2_price) == (59_060.0, 61_060.0, 62_060.0)  # x1.001, floored
    assert [(o.size, o.limit_price, o.stop_price) for o in ex.open_orders()] == [(1, 61_060.0, 59_060.0)]


def test_a_rejected_entry_is_recorded_and_never_retried(env):
    clock, ex, jr, alerts, exe = env
    ex.set_session("CLOSE")
    assert exe.enter(Enter(entry(), 1), ctx(clock)) is None
    assert jr.intent(f"ewb-e-{T0}-L")["status"] == "REJECTED" and "ENTRY_FAILED" in alerts.kinds()
    ex.set_session("OPEN")
    assert exe.enter(Enter(entry(), 1), ctx(clock)) is None and ex.position() is None


def test_an_ambiguous_entry_is_looked_up_before_it_is_sent_again(env):
    clock, ex, jr, _, exe = env
    ex.fail_next("place_market", Unavailable("503"))  # failed before reaching the venue
    assert exe.enter(Enter(entry(), 1), ctx(clock)) is None
    assert jr.intent(f"ewb-e-{T0}-L")["status"] == "PENDING" and ex.position() is None
    tr = exe.enter(Enter(entry(), 1), ctx(clock))  # lookup: not there -> sent again with the same id
    assert tr is not None and ex.calls.count("place_market") == 2 and ex.position().contracts == 1
    assert [o.client_order_id for o in ex.orders.values()].count(f"ewb-e-{T0}-L") == 1


def test_a_lost_reply_after_the_venue_accepted_is_found_not_resent(env):
    clock, ex, jr, _, exe = env
    ex.crash_after_accept("place_market", Unavailable("read timeout"))
    tr = exe.enter(Enter(entry(), 1), ctx(clock))
    assert tr is not None and ex.calls.count("place_market") == 1 and ex.position().contracts == 1
    assert jr.intent(f"ewb-e-{T0}-L")["note"] == "found after error"


def test_an_entry_whose_fill_cannot_be_read_is_rebuilt_not_adopted(env):
    clock, ex, jr, alerts, exe = env
    ex.fail_next("get_order", Unavailable("503"), times=25)  # longer than the 10 s fill wait
    assert exe.enter(Enter(entry(), 1), ctx(clock)) is None
    assert ex.position().contracts == 1 and exe.trade() is None and jr.events(kind="ENTRY_UNCONFIRMED")
    ex._faults.clear()
    acts = plan(target(T0 + 3600, ent=entry()), None, ex.position(), ex.open_orders(), ctx(clock))
    exe.execute(acts, ctx(clock))
    assert exe.trade().trade_id == T0 and len(ex.open_orders()) == 1
    assert "RECOVERED_ENTRY" in alerts.kinds() and "ADOPTED" not in alerts.kinds()


def test_a_duplicate_id_error_resolves_to_the_existing_order(tmp_path):
    clock = Clock()
    ex = sim(clock, duplicate="error")
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    ex.place_market(f"ewb-e-{T0}-L", 1, 1)  # e.g. sent by a run killed before it could record the reply
    jr.record_intent(f"ewb-e-{T0}-L", "ENTRY", T0, {}, clock())
    jr.update_intent(f"ewb-e-{T0}-L", "PENDING", clock())
    sent = exe.send(f"ewb-e-{T0}-L", "ENTRY", T0, {}, lambda: ex.place_market(f"ewb-e-{T0}-L", 1, 1))
    assert sent.status == "ACCEPTED" and ex.position().contracts == 1
    jr.close()


def test_stop_moves_replace_place_first_then_cancel(env):
    clock, ex, jr, _, exe = env
    tr = exe.enter(Enter(entry(), 3), ctx(clock))
    legs, stop = desired_legs(tr, 3, 59_500.0, 1.0, 5.0)
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    rest = sorted((o.client_order_id, o.stop_price) for o in ex.open_orders())
    assert rest == [(f"ewb-b-{T0}-tp1-3", 59_500.0), (f"ewb-b-{T0}-tp2-4", 59_500.0)]
    assert exe.trade().stop_spot == 59_500.0 and "STOP_MOVED" in [e["kind"] for e in jr.events()]
    assert not jr.events(kind="CANCEL_FAILED")  # each replaced leg is cancelled once


def test_a_refused_overlap_falls_back_to_cancel_then_place(tmp_path):
    """Probe B: on a venue that refuses overlapping brackets the fallback is the designed path - a LEG_GAP each,
    no BRACKET_FAILED (a CANARY gate 2 failure) and no alert."""
    clock = Clock()
    ex = sim(clock, reject_overlap=True)
    jr = journal(tmp_path)
    alerts = Alerts()
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep, alert=alerts)
    tr = exe.enter(Enter(entry(), 3), ctx(clock))
    legs, stop = desired_legs(tr, 3, 59_500.0, 1.0, 5.0)
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    assert sorted((o.size, o.stop_price) for o in ex.open_orders()) == [(1, 59_500.0), (2, 59_500.0)]
    assert len(jr.events(kind="LEG_GAP")) == 2 and not jr.events(kind="BRACKET_FAILED")
    assert alerts.kinds() == ["ENTRY", "STOP_MOVED"]
    jr.close()


def test_edit_in_place_when_enabled(tmp_path):
    clock = Clock()
    ex = sim(clock, edit_supported=True)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep, edit_stops=True)
    tr = exe.enter(Enter(entry(), 1), ctx(clock))
    legs, stop = desired_legs(tr, 1, 59_500.0, 1.0, 5.0)
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    assert [(o.client_order_id, o.stop_price) for o in ex.open_orders()] == [(f"ewb-b-{T0}-tp1-1", 59_500.0)]
    assert ex.calls.count("place_bracket") == 1
    jr.close()


def test_close_cancels_brackets_and_waits_for_flat(env):
    clock, ex, jr, alerts, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    assert exe.close("TIME", ctx(clock)) is True
    assert ex.position() is None and ex.open_orders() == []
    tr = exe.trade()
    assert (tr.status, tr.exit_reason, tr.closes) == ("CLOSED", "TIME", 1)
    assert jr.intent(f"ewb-x-{T0}-1")["status"] == "ACCEPTED" and "CLOSED" in alerts.kinds()


def test_a_refused_bracket_cancel_does_not_block_the_close(env):
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    ex.fail_next("cancel", ExchangeError("cancel refused: sim-2: NOT_ALLOWED_TO_CANCEL"))
    assert exe.close("TIME", ctx(clock)) is True  # brackets only reduce: the close goes out anyway
    assert ex.position() is None and ex.open_orders() == []
    assert jr.events(kind="CANCEL_FAILED")[0]["data"]["error"].startswith("cancel refused")


def test_close_waits_for_an_open_session_and_alerts_once(env):
    clock, ex, jr, alerts, exe = env
    exe.enter(Enter(entry(), 1), ctx(clock))
    ex.set_session("CLOSE")
    assert exe.close("LINEAGE", ctx(clock, session_open=False)) is False
    assert exe.close("LINEAGE", ctx(clock, session_open=False)) is False
    assert alerts.kinds().count("CLOSE_DEFERRED") == 1 and ex.position() is not None
    ex.set_session("OPEN")
    assert exe.close("LINEAGE", ctx(clock)) is True


def test_close_failure_is_retried_with_a_new_id(env):
    clock, ex, jr, alerts, exe = env
    exe.enter(Enter(entry(), 1), ctx(clock))
    ex.fail_next("close_position", RateLimited("429"))
    assert exe.close("TIME", ctx(clock)) is False and "CLOSE_FAILED" in alerts.kinds()
    assert exe.close("TIME", ctx(clock)) is True
    assert jr.intent(f"ewb-x-{T0}-1")["status"] == "PENDING" and jr.intent(f"ewb-x-{T0}-2")["status"] == "ACCEPTED"


def test_tp1_sync_reduces_then_the_rest_is_protected_at_break_even(env):
    clock, ex, jr, _, exe = env
    tr = exe.enter(Enter(entry(), 3), ctx(clock))
    ex.set_prices(60_900.0)  # the engine's TP1 (spot) filled, the live TP1 limit 61_000 did not
    acts = plan(target(T0 + 3600, position(tp1_done=True, stop=60_000.0)), tr, ex.position(), ex.open_orders(), ctx(clock))
    assert isinstance(acts[0], Reduce) and isinstance(acts[1], Protect)
    exe.execute(acts, ctx(clock))
    assert ex.position().contracts == 2 and exe.trade().tp1_filled
    assert [(o.size, o.limit_price, o.stop_price) for o in ex.open_orders()] == [(2, 62_000.0, 60_000.0)]


def test_an_unknown_position_matching_the_engine_is_adopted_and_protected(env):
    clock, ex, jr, alerts, exe = env
    ex.place_market("manual-1", 1, 2)
    acts = plan(target(T0 + 3600, position()), None, ex.position(), ex.open_orders(), ctx(clock))
    exe.execute(acts, ctx(clock))
    tr = exe.trade()
    assert tr.trade_id == T0 and tr.contracts == 2 and "ADOPTED" in alerts.kinds()
    assert sorted(o.size for o in ex.open_orders()) == [1, 1]


def test_an_adopted_position_is_capped_at_the_stage_maximum(env):
    clock, ex, jr, alerts, exe = env
    ex.place_market("manual-1", 1, 3)
    c = ctx(clock, max_contracts=1)  # CANARY
    exe.execute(plan(target(T0 + 3600, position()), None, ex.position(), ex.open_orders(), c), c)
    assert (exe.trade().contracts, ex.position().contracts) == (1, 1)
    assert [(o.size, o.limit_price) for o in ex.open_orders()] == [(1, 61_000.0)]
    assert jr.events(kind="REDUCED")[0]["data"]["reason"] == "EXCESS" and "EXCESS_POSITION" in alerts.kinds()
    ex.place_market("manual-2", 1, 2)  # the executor never adopts more than the stage maximum either
    exe.save(None)
    exe.execute([Adopt(position(), 3)], ctx(clock, max_contracts=2))
    assert exe.trade().contracts == 2 and sum(o.size for o in ex.open_orders()) == 2


def test_a_position_adopted_after_the_engine_tp1_is_the_rest(env):
    clock, ex, jr, _, exe = env
    ex.place_market("manual-1", 1, 2)  # @ 60_002.5
    ex.set_prices(61_200.0)  # past the engine's TP1
    tgt = target(T0 + 3600, position(tp1_done=True, stop=60_000.0))
    exe.execute(plan(tgt, None, ex.position(), ex.open_orders(), ctx(clock)), ctx(clock))
    tr = exe.trade()
    assert tr.tp1_filled and expected_contracts(tr) == 2  # no phantom excess to reduce
    assert [(o.size, o.limit_price, o.stop_price) for o in ex.open_orders()] == [(2, 62_000.0, 60_000.0)]
    assert plan(tgt, tr, ex.position(), ex.open_orders(), ctx(clock)) == []


def test_contracts_added_by_hand_are_reduced_and_the_trade_keeps_its_size(env):
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    ex.place_market("manual-1", 1, 2)
    acts = plan(target(T0 + 3600, position()), exe.trade(), ex.position(), ex.open_orders(), ctx(clock))
    assert acts[0] == Alert("EXCESS_POSITION", f"trade {T0}: position 5 > 3 expected")
    exe.execute(acts, ctx(clock))
    assert ex.position().contracts == 3 and exe.trade().contracts == 3
    assert sorted(o.size for o in ex.open_orders()) == [1, 2]


def test_a_stop_move_without_quotes_is_sent_once_they_are_back(env):
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    moved = target(T0 + 3600, position(stop=59_500.0))
    exe.execute(plan(moved, exe.trade(), ex.position(), ex.open_orders(), ctx(clock, ratio=None)), ctx(clock, ratio=None))
    assert exe.trade().stop_spot == 59_000.0 and not jr.events(kind="STOP_MOVED")
    exe.execute(plan(moved, exe.trade(), ex.position(), ex.open_orders(), ctx(clock)), ctx(clock))
    assert exe.trade().stop_spot == 59_500.0 and jr.events(kind="STOP_MOVED")[0]["data"]["stop"] == 59_500.0
    assert [o.stop_price for o in ex.open_orders()] == [59_500.0, 59_500.0]


def test_a_partial_tp1_keeps_its_leg_until_the_rest_fills(env):
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 5), ctx(clock))  # TP1 leg 2, TP2 leg 3
    ex.bracket_fill_cap = 1  # the TP1 limit fills one contract at a time

    def poll() -> list[str]:
        seen, events = observe(exe.trade(), ex.position(), ex.open_orders(), clock())
        exe.save(seen)
        exe.execute(plan(target(T0 + 3600, position()), seen, ex.position(), ex.open_orders(), ctx(clock)), ctx(clock))
        return [k for k, _ in events]

    ex.set_prices(61_000.0)
    assert poll() == ["PARTIAL_FILL"] and not exe.trade().tp1_filled
    assert sorted((o.size - o.filled, o.limit_price, o.stop_price) for o in ex.open_orders()) == [
        (1, 61_000.0, 59_000.0), (3, 62_000.0, 59_000.0)]  # nothing replaced
    ex.set_prices(61_000.0)
    assert poll() == ["TP1_FILLED"] and exe.trade().tp1_filled and ex.position().contracts == 3
    assert [(o.size, o.limit_price, o.stop_price) for o in ex.open_orders()] == [(3, 62_000.0, 60_000.0)]


def test_flip_enters_only_after_the_close_is_confirmed(env):
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 1), ctx(clock))
    short = entry(signal_ts=T0 + 3600, direction=-1, stop=61_000.0, tp1=59_000.0, tp2=58_000.0)
    ex.fail_next("close_position", RateLimited("429"))
    exe.execute([Close("FLIP"), Enter(short, 1)], ctx(clock))
    assert ex.position().side == 1 and jr.events(kind="SKIP")[0]["data"]["signal_ts"] == T0 + 3600
    exe2 = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    short2 = entry(signal_ts=T0 + 7200, direction=-1, stop=61_000.0, tp1=59_000.0, tp2=58_000.0)
    exe2.execute([Close("FLIP"), Enter(short2, 1)], ctx(clock))
    assert ex.position().side == -1 and exe2.trade().trade_id == T0 + 7200


def test_skips_and_alerts_are_recorded_once(env):
    clock, ex, jr, alerts, exe = env
    exe.execute([Skip(T0, "paused"), Skip(T0, "paused"), Alert("FOREIGN_ORDERS", "f1"), Alert("FOREIGN_ORDERS", "f1")],
                ctx(clock))
    assert alerts.kinds() == ["SKIP", "FOREIGN_ORDERS"] and len(jr.events(kind="SKIP")) == 1


def test_restart_recovery_rebuilds_an_entry_the_crash_did_not_record(tmp_path):
    clock = Clock()
    ex = sim(clock)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    ex.crash_after_accept("place_market")
    with pytest.raises(SimCrash):
        exe.enter(Enter(entry(), 3), ctx(clock))
    assert jr.intent(f"ewb-e-{T0}-L")["status"] == "PENDING" and exe.trade() is None
    jr.close()
    jr2 = journal(tmp_path)  # restart
    exe2 = Executor(ex, jr2, clock=clock, sleep=clock.sleep)
    out = exe2.recover()
    assert out == {"accepted": 1, "abandoned": 0, "unknown": 0, "rebuilt": 1}
    tr = exe2.trade()
    assert tr.trade_id == T0 and tr.contracts == 3 and ex.open_orders() == []
    acts = plan(None, tr, ex.position(), ex.open_orders(), ctx(clock))
    exe2.execute(acts, ctx(clock))
    assert sorted(o.size for o in ex.open_orders()) == [1, 2] and ex.calls.count("place_market") == 1
    jr2.close()


def test_recovery_abandons_old_intents_that_never_reached_the_venue(env):
    clock, ex, jr, _, exe = env
    jr.record_intent("ewb-x-1-1", "CLOSE", 1, {}, clock() - 700.0)
    jr.record_intent("ewb-x-1-2", "CLOSE", 1, {}, clock() - 10.0)
    ex.fail_next("find_order", Unavailable("503"))
    assert exe.recover() == {"accepted": 0, "abandoned": 0, "unknown": 1, "rebuilt": 0}
    assert exe.recover() == {"accepted": 0, "abandoned": 1, "unknown": 0, "rebuilt": 0}
    assert jr.intent("ewb-x-1-2")["status"] == "PENDING"
    clock.now += 601.0  # while running, the minute job resolves it without a RECOVERY event
    assert exe.resolve_pending()[0]["abandoned"] == 1 and len(jr.events(kind="RECOVERY")) == 2


def test_an_ambiguous_tp1_sync_that_filled_is_never_sold_twice(env):
    """Probe (a): the venue filled the TP1_SYNC reduce, but its answer was lost and the lookup failed. The reduce
    was recorded on the trade before it was sent, so the next poll reads the smaller position as ours: no
    UNEXPLAINED_REDUCTION, no second TP1 sale; the protection planned with the reduce waited for it."""
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    ex.set_prices(60_900.0)  # the engine's TP1 (spot) filled, the live TP1 limit 61_000 did not
    tgt = target(T0 + 3600, position(tp1_done=True))
    ex.crash_after_accept("close_position", Unavailable("read timeout"))  # filled, the answer lost
    ex.fail_next("find_order", Unavailable("503"))  # and the lookup failed: PENDING
    assert poll(exe, ex, clock, tgt) == []
    assert ex.position().contracts == 2 and exe.trade().pending_reduce["cid"] == f"ewb-r-{T0}-1"
    assert sorted(o.size for o in ex.open_orders()) == [1, 2]  # both legs stay until the reduce is known
    clock.now += 10
    assert poll(exe, ex, clock, tgt) == ["REDUCE_SETTLED"]
    tr = exe.trade()
    assert ex.position().contracts == 2 and tr.tp1_filled and tr.pending_reduce is None
    assert ex.calls.count("close_position") == 1 and not jr.events(kind="UNEXPLAINED_REDUCTION")
    assert [(o.size, o.limit_price, o.stop_price) for o in ex.open_orders()] == [(2, 62_000.0, 60_000.0)]


def test_an_ambiguous_reduce_is_resolved_by_its_id_never_sent_under_a_new_one(env):
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    ex.set_prices(60_900.0)
    tgt = target(T0 + 3600, position(tp1_done=True))
    ex.fail_next("close_position", Unavailable("503"))  # failed before it reached the venue
    poll(exe, ex, clock, tgt)
    assert ex.position().contracts == 3 and jr.intent(f"ewb-r-{T0}-1")["status"] == "PENDING"
    assert jr.events(kind="REDUCE_FAILED")[0]["data"]["status"] == "PENDING"
    clock.now += 10
    assert poll(exe, ex, clock, tgt) == ["REDUCE_SETTLED"]  # looked up (not there), sent again under its id
    assert ex.position().contracts == 2 and exe.trade().tp1_filled
    assert jr.intent(f"ewb-r-{T0}-1")["status"] == "ACCEPTED" and jr.intent(f"ewb-r-{T0}-2") is None
    assert [o.client_order_id for o in ex.orders.values()].count(f"ewb-r-{T0}-1") == 1
    assert [(o.size, o.limit_price, o.stop_price) for o in ex.open_orders()] == [(2, 62_000.0, 60_000.0)]


def test_a_vanished_leg_that_filled_is_looked_up_not_placed_again(env):
    """Probe (b): the TP1 leg fills between the position read and the order read. The stale pair plans the TP1 leg
    again; the executor looks the vanished leg up first and does not place a filled one again (a TP1 limit below
    the market would sell TP2 contracts at once); the next poll folds the fill. A leg that left the book unfilled
    (cancelled by hand) is placed again."""
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 5), ctx(clock))  # TP1 leg 2 @ 61_000, TP2 leg 3 @ 62_000
    tgt = target(T0 + 3600, position())
    stale = ex.position()
    ex.set_prices(61_000.0)  # the TP1 leg fills right after the position was read
    orders = ex.open_orders()
    seen, _ = observe(exe.trade(), stale, orders, clock())
    exe.save(seen)
    exe.execute(plan(tgt, seen, stale, orders, ctx(clock)), ctx(clock))
    assert ex.position().contracts == 3 and ex.calls.count("place_bracket") == 2  # not placed again
    assert jr.events(kind="LEG_FILLED")[0]["data"]["leg"] == "tp1"
    clock.now += 10
    assert poll(exe, ex, clock, tgt) == ["TP1_FILLED", "PROTECTED"]
    assert ex.position().contracts == 3
    assert [(o.size, o.limit_price, o.stop_price) for o in ex.open_orders()] == [(3, 62_000.0, 60_000.0)]
    ex.cancel([o.order_id for o in ex.open_orders()])  # cancelled by hand: unfilled
    clock.now += 10
    poll(exe, ex, clock, tgt)
    assert [(o.size, o.limit_price, o.stop_price) for o in ex.open_orders()] == [(3, 62_000.0, 60_000.0)]


def resting(ex) -> list[tuple[int, float, float]]:
    """(contracts left, take-profit, stop) of every resting order, sorted."""
    return sorted((o.size - o.filled, o.limit_price, o.stop_price) for o in ex.open_orders())


@pytest.mark.parametrize("failures", [1, 2])
def test_a_stop_move_never_removes_a_leg_whose_replacement_is_unconfirmed(env, failures):
    """Probe A: a 503 leaves the replacement's outcome unknown (PENDING, not a refusal): the old leg keeps
    protecting - nothing is cancelled first - and the trade keeps the stop in force until every leg moved."""
    clock, ex, jr, _, exe = env
    tr = exe.enter(Enter(entry(), 3), ctx(clock))
    ex.fail_next("place_bracket", Unavailable("503"), times=failures)
    legs, stop = desired_legs(tr, 3, 59_500.0, 1.0, 5.0)
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    moved_tp2 = 59_500.0 if failures == 1 else 59_000.0
    assert resting(ex) == [(1, 61_000.0, 59_000.0), (2, 62_000.0, moved_tp2)]
    assert not jr.events(kind="LEG_GAP") and len(jr.events(kind="BRACKET_FAILED")) == failures
    assert exe.trade().stop_spot == 59_000.0 and not jr.events(kind="STOP_MOVED")
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    assert resting(ex) == [(1, 61_000.0, 59_500.0), (2, 62_000.0, 59_500.0)]
    assert exe.trade().stop_spot == 59_500.0 and len(jr.events(kind="STOP_MOVED")) == 1


def test_an_unconfirmed_replacement_found_resting_is_taken_over(env):
    clock, ex, jr, _, exe = env
    tr = exe.enter(Enter(entry(), 3), ctx(clock))
    ex.crash_after_accept("place_bracket", Unavailable("read timeout"))  # the new TP1 leg rests, the answer is lost
    ex.fail_next("find_order", Unavailable("503"))  # and the lookup fails: PENDING
    legs, stop = desired_legs(tr, 3, 59_500.0, 1.0, 5.0)
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    assert not jr.events(kind="LEG_GAP") and sum(n for n, _, _ in resting(ex)) == 4  # both TP1 legs rest
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))  # its terms match: taken over, the old one cancelled
    assert resting(ex) == [(1, 61_000.0, 59_500.0), (2, 62_000.0, 59_500.0)]
    assert ex.calls.count("place_bracket") == 4 and jr.events(kind="LEG_ADOPTED")[0]["data"]["leg"] == "tp1"


def test_a_replacement_that_fails_after_the_cancel_puts_the_old_leg_back(tmp_path):
    """The venue refuses the overlap, the old leg is cancelled first, then the replacement fails (503): the old
    terms are placed again at once, and the next pass completes the move."""
    clock = Clock()
    ex = sim(clock, reject_overlap=True)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    tr = exe.enter(Enter(entry(), 3), ctx(clock))
    real, calls = ex.place_bracket, []

    def flaky(*args, **kw):
        calls.append(args[0])
        if len(calls) == 2:  # the replacement sent after the cancel
            raise Unavailable("503")
        return real(*args, **kw)

    ex.place_bracket = flaky
    legs, stop = desired_legs(tr, 3, 59_500.0, 1.0, 5.0)
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    assert resting(ex) == [(1, 61_000.0, 59_000.0), (2, 62_000.0, 59_500.0)]  # TP1 restored, TP2 moved
    assert len(jr.events(kind="LEG_RESTORED")) == 1 and exe.trade().stop_spot == 59_000.0
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    assert resting(ex) == [(1, 61_000.0, 59_500.0), (2, 62_000.0, 59_500.0)] and exe.trade().stop_spot == 59_500.0
    jr.close()


def test_a_replacement_refused_for_its_prices_keeps_the_old_leg(tmp_path):
    """A refusal that names a price (a stop through the market, an off-grid level) would refuse the replacement
    again: the old leg - maybe already triggered and filling - is never cancelled first, even on a venue that
    refuses overlapping brackets."""
    clock = Clock()
    ex = sim(clock, reject_overlap=True)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    tr = exe.enter(Enter(entry(), 3), ctx(clock))
    legs, _ = desired_legs(tr, 3, 59_500.0, 1.0, 5.0)
    bad = tuple(Leg(leg.name, leg.size, leg.limit_price, 59_501.0) for leg in legs)  # off the $5 grid
    exe.protect(Protect(bad, 59_500.0, 59_501.0), ctx(clock))
    assert resting(ex) == [(1, 61_000.0, 59_000.0), (2, 62_000.0, 59_000.0)]
    assert not jr.events(kind="LEG_GAP") and len(jr.events(kind="BRACKET_FAILED")) == 2
    assert exe.trade().stop_price == 59_000.0
    jr.close()


@pytest.mark.parametrize("reject_overlap", [False, True])
def test_brackets_accepted_before_a_crash_are_taken_over_after_the_restart(tmp_path, reject_overlap):
    """Probe C: killed right after the second bracket was accepted - both legs rest, the trade does not know
    them. After the restart they are taken over by their client order id and terms (on a venue that refuses
    overlapping brackets, placing new ones first would fail and cancelling them would leave it unprotected)."""
    clock = Clock()
    ex = sim(clock, reject_overlap=reject_overlap)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    real, calls = ex.place_bracket, []

    def crash_on_second(*args, **kw):
        calls.append(args[0])
        if len(calls) == 2:
            ex.crash_after_accept("place_bracket")
        return real(*args, **kw)

    ex.place_bracket = crash_on_second
    with pytest.raises(SimCrash):
        exe.enter(Enter(entry(), 3), ctx(clock))
    ex.place_bracket = real
    jr.close()
    jr2 = journal(tmp_path)
    exe2 = Executor(ex, jr2, clock=clock, sleep=clock.sleep)
    exe2.recover()
    assert poll(exe2, ex, clock, target(T0 + 3600, position())) == ["UNPROTECTED"]
    assert resting(ex) == [(1, 61_000.0, 59_000.0), (2, 62_000.0, 59_000.0)]
    assert ex.calls.count("place_bracket") == 2 and not jr2.events(kind="BRACKET_FAILED")
    assert [leg.client_order_id for leg in exe2.trade().legs] == [f"ewb-b-{T0}-tp1-1", f"ewb-b-{T0}-tp2-2"]
    assert poll(exe2, ex, clock, target(T0 + 3600, position())) == ["PROTECTED"]
    jr2.close()


def test_a_failed_close_puts_the_protection_back(env):
    """Probe D: the close cancels the brackets first; when the close then fails, the legs go back at the stop in
    force for the position, so a close that keeps failing never leaves it unprotected."""
    clock, ex, jr, alerts, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    ex.fail_next("close_position", RateLimited("429"))
    assert exe.close("TIME", ctx(clock)) is False and "CLOSE_FAILED" in alerts.kinds()
    assert resting(ex) == [(1, 61_000.0, 59_000.0), (2, 62_000.0, 59_000.0)]
    assert poll(exe, ex, clock, target(T0 + 3600, position(), exit_reason="TIME")) == []  # never seen unprotected
    assert ex.position() is None and exe.trade().exit_reason == "TIME"


def test_a_partly_filled_close_returns_at_once_and_protects_the_rest(env):
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    ex.market_fill_ratio = 0.5  # the IOC close fills 1 of 3, its rest is cancelled
    start = clock()
    assert exe.close("TIME", ctx(clock)) is False and jr.events(kind="CLOSE_UNCONFIRMED")
    assert clock() - start < 5.0  # the close order ended: no 30 s wait for a flat position that cannot come
    assert ex.position().contracts == 2 and resting(ex) == [(1, 61_000.0, 59_000.0), (1, 62_000.0, 59_000.0)]


def test_an_edit_keeps_what_a_partly_filled_leg_has_left(tmp_path):
    """Probe E: orders/edit takes the order's total size: a TP1 leg of 2 with 1 filled is edited to 2 (1 left),
    never to 1 (which would end it and leave a contract unprotected)."""
    clock = Clock()
    ex = sim(clock, edit_supported=True)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep, edit_stops=True)
    exe.enter(Enter(entry(), 5), ctx(clock))  # TP1 leg 2, TP2 leg 3
    ex.bracket_fill_cap = 1
    ex.set_prices(61_000.0)
    ex.set_prices(60_500.0)
    ex.bracket_fill_cap = None
    seen, events = observe(exe.trade(), ex.position(), ex.open_orders(), clock())
    exe.save(seen)
    assert [k for k, _ in events] == ["PARTIAL_FILL"] and ex.position().contracts == 4
    legs, stop = desired_legs(exe.trade(), 4, 59_500.0, 1.0, 5.0)
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    assert resting(ex) == [(1, 61_000.0, 59_500.0), (3, 62_000.0, 59_500.0)]
    assert ex.calls.count("place_bracket") == 2 and len(jr.events(kind="STOP_EDITED")) == 2
    jr.close()


def test_a_signal_skipped_once_is_never_entered_later(env):
    """Probe F: the flip's close failed, so the new signal was skipped (alerted); on the next pass the close goes
    through, and the skipped entry is not sent late (never chase)."""
    clock, ex, jr, alerts, exe = env
    exe.enter(Enter(entry(), 1), ctx(clock))
    short = entry(signal_ts=T0 + 3600, direction=-1, stop=61_000.0, tp1=59_000.0, tp2=58_000.0)
    ex.fail_next("close_position", RateLimited("429"))
    exe.execute([Close("FLIP"), Enter(short, 1)], ctx(clock))
    clock.now += 10
    poll(exe, ex, clock, target(T0 + 3600, position(), exit_reason="FLIP", ent=short))
    assert ex.position() is None and ex.calls.count("place_market") == 1
    assert jr.intent(f"ewb-e-{T0 + 3600}-S") is None and alerts.kinds().count("SKIP") == 1


def test_recovery_records_a_close_accepted_before_the_crash(tmp_path):
    """Probe G: killed while waiting for the flat position after the close was accepted: the restart records
    the close with its reason (not as a bracket exit)."""
    clock = Clock()
    ex = sim(clock)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    exe.enter(Enter(entry(), 1), ctx(clock))

    def killed(*_args):
        raise SimCrash("killed during the flat wait")

    exe._await_flat = killed
    with pytest.raises(SimCrash):
        exe.close("TIME", ctx(clock))
    assert jr.intent(f"ewb-x-{T0}-1")["status"] == "ACCEPTED" and ex.position() is None
    jr.close()
    jr2 = journal(tmp_path)
    exe2 = Executor(ex, jr2, clock=clock, sleep=clock.sleep)
    exe2.recover()
    tr = exe2.trade()
    assert (tr.status, tr.exit_reason) == ("CLOSED", "TIME")
    closed = jr2.events(kind="CLOSED")[0]["data"]
    assert closed["recovered"] and closed["reason"] == "TIME" and closed["price"] == 59_997.5
    jr2.close()


def test_a_rebuilt_trade_measures_protection_from_its_fill(tmp_path):
    """Probe H: an entry rebuilt after a 5-minute outage was unprotected for 5 minutes: PROTECTED_FIRST says so
    (PROTECT_SLOW, CANARY gate 1), it does not start the clock at the restart."""
    clock = Clock()
    ex = sim(clock)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    ex.crash_after_accept("place_market")
    with pytest.raises(SimCrash):
        exe.enter(Enter(entry(), 3), ctx(clock))
    jr.close()
    clock.now += 300
    jr2 = journal(tmp_path)
    exe2 = Executor(ex, jr2, clock=clock, sleep=clock.sleep)
    exe2.recover()
    exe2.execute(plan(None, exe2.trade(), ex.position(), ex.open_orders(), ctx(clock)), ctx(clock))
    assert jr2.events(kind="PROTECTED_FIRST")[0]["data"]["after_entry_s"] == 300.0
    jr2.close()


def test_a_foreign_position_after_our_trade_closed_is_adopted_under_fresh_ids(env):
    """probe3: our 1-contract trade left at its TP1; a manual position in the engine's direction follows while
    the engine still holds that trade. It is a foreign position (ADOPTED, its own entry price), not our entry
    rebuilt from its intent, and its brackets go on from the journal's ids (a reused id would resolve to the
    filled TP1 leg and leave the position unprotected)."""
    clock, ex, jr, alerts, exe = env
    exe.enter(Enter(entry(), 1), ctx(clock))
    ex.set_prices(61_000.0)
    assert poll(exe, ex, clock, target(T0 + 3600, position())) == ["CLOSED"]
    ex.set_prices(60_500.0)
    ex.place_market("manual-1", 1, 1)
    clock.now += 60
    poll(exe, ex, clock, target(T0 + 3600, position()))
    tr = exe.trade()
    assert (tr.status, tr.entry_price, tr.gen) == ("OPEN", 60_502.5, 2)
    assert [o.client_order_id for o in ex.open_orders()] == [f"ewb-b-{T0}-tp1-2"]
    assert "ADOPTED" in alerts.kinds() and "RECOVERED_ENTRY" not in alerts.kinds()


def test_an_own_entry_still_pending_is_looked_up_before_a_position_is_called_foreign(env):
    clock, ex, jr, alerts, exe = env
    ex.crash_after_accept("place_market", Unavailable("read timeout"))  # filled, the answer lost
    ex.fail_next("find_order", Unavailable("503"))  # and the lookup failed: PENDING
    assert exe.enter(Enter(entry(), 1), ctx(clock)) is None and jr.intent(f"ewb-e-{T0}-L")["status"] == "PENDING"
    poll(exe, ex, clock, target(T0 + 3600, ent=entry()))
    assert exe.trade().trade_id == T0 and len(ex.open_orders()) == 1
    assert "RECOVERED_ENTRY" in alerts.kinds() and "ADOPTED" not in alerts.kinds()


def test_a_position_that_cannot_be_adopted_without_quotes_is_alerted_once(env):
    clock, ex, jr, alerts, exe = env
    ex.place_market("manual-1", 1, 2)
    for _ in range(2):
        exe.execute([Adopt(position(), 2)], ctx(clock, ratio=None))
    assert exe.trade() is None and ex.open_orders() == [] and alerts.kinds() == ["ADOPT_NO_QUOTES"]


def test_alerts_are_once_per_trade_and_the_journal_keys_stay_bounded(env):
    clock, ex, jr, alerts, exe = env
    for signal in (T0, T0 + 7200):
        exe.enter(Enter(entry(signal_ts=signal), 1), ctx(clock))
        for _ in range(2):
            exe.close("LINEAGE", ctx(clock, session_open=False))
        assert exe.close("LINEAGE", ctx(clock)) is True
    assert alerts.kinds().count("CLOSE_DEFERRED") == 2  # once for each trade
    for k in range(50):
        exe.execute([Alert("FOREIGN_ORDERS", f"f{k}")], ctx(clock))
    keys = [r[0] for r in jr._exec("SELECT key FROM kv").fetchall()]
    assert len(keys) <= 4 and not [k for k in keys if k.startswith(("alerted:", "leg_filled:"))]


def test_recovery_survives_an_unreadable_exchange(tmp_path):
    clock = Clock()
    ex = sim(clock)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    ex.crash_after_accept("place_market")
    with pytest.raises(SimCrash):
        exe.enter(Enter(entry(), 1), ctx(clock))
    ex.fail_next("get_order", Unavailable("503"))
    assert exe.recover() == {"accepted": 1, "abandoned": 0, "unknown": 1, "rebuilt": 0}
    assert jr.events(kind="RECOVERY_ERROR") and exe.trade() is None
    assert exe.recover()["rebuilt"] == 1 and exe.trade().trade_id == T0
    jr.close()
