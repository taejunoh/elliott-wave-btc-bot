"""Plan 3 spec 7 `장애 주입`: a process killed in the middle of an order, restart reconciliation, WebSocket
drop and sequence gaps (REST fallback and resync), 429s and outages, a session closure with an exit
waiting for the reopen, and the weekly CFM halt (an exit queued before it and a stop the spot price
crossed during it close at the reopen, not an hour later). The process is "killed" by SimCrash (a
BaseException nothing in the runtime catches) between the journaled intent and the send, right after the
venue accepted an entry, a leg or a close, or in the middle of a stop move (the new leg accepted, the old one
not yet cancelled); the restart is a new LiveRuntime on the same journal file and the same exchange. Legs are
told apart by the leg their client order id names (`ewb-b-<trade>-<leg>-<generation>`)."""

from __future__ import annotations

import json

import pytest

import ewbot.engine.loop as loop_mod
from ewbot.live.exchange import PlaceResult, RateLimited, Unavailable
from ewbot.live.feed import TickerFeed
from ewbot.live.journal import Journal
from ewbot.live.sim import SimCrash
from ewbot.live.stage import paused
from ewbot.live.target import compute_target
from tests.live.harness import Harness
from tests.paper.helpers import make_lock, synth_frame, ts

SIGNAL = ts("2024-03-13 22:00")
ENTRY_BAR = SIGNAL + 3600
TIME_SIGNAL = ts("2024-01-01 19:00")  # W5_REV short whose TIME exit is queued at 2024-01-04 15:00
LONG_ENTRY = ts("2024-01-12 15:00")  # a W3 long still open at the CFM halt below
HALT = ts("2024-01-12 22:00")  # Fri 17:00 New York (EST): the weekly CFM halt bar


def started(tmp_path, start: float = ENTRY_BAR + 30, **kw) -> Harness:
    h = Harness(tmp_path, int(start), **kw)
    h.price(float(h.bar(int(start) // 3600 * 3600)["open"]))
    h.rt.startup()
    return h


def restart(h: Harness, now: float) -> None:
    h.journal.close()
    h.journal = Journal(h.tmp / "journal.sqlite")
    h.clock.now = now
    h.rt = h.make_runtime()
    h.rt.startup()


def legs(h: Harness) -> dict[str, tuple[int, float | None, float | None]]:
    """Our resting brackets by the leg their client order id names: what each still covers, at which take-profit
    and stop (read from the venue's book; two resting for one leg fail here)."""
    out: dict[str, tuple[int, float | None, float | None]] = {}
    for o in h.ex.orders.values():
        if o.kind == "BRACKET" and o.status == "OPEN":
            name = o.client_order_id.split("-")[3]
            assert name not in out, f"two {name} legs rest"
            out[name] = (o.size - o.filled, o.limit_price, o.stop_price)
    return out


def entries(h: Harness) -> list[str]:
    """The client order ids of every entry the venue received."""
    return [o.client_order_id for o in h.ex.orders.values() if o.client_order_id.startswith("ewb-e-")]


def resent_only_after_a_lookup(h: Harness, method: str) -> bool:
    """Spec 5.1 (`조회한 뒤에만 다시 보낸다`): every call of `method` after the first went out right after a
    find_order lookup (the executor looks a PENDING intent up by its client order id before sending it again)."""
    at = [i for i, c in enumerate(h.ex.calls) if c == method]
    return all(h.ex.calls[i - 1] == "find_order" for i in at[1:])


def crash_on(h: Harness, method: str, nth: int) -> None:
    """The process dies right after the venue accepted the `nth` `method` call from now on."""
    real = getattr(h.ex, method)
    seen: list[int] = []

    def call(*args, **kw):  # type: ignore[no-untyped-def]
        seen.append(1)
        if len(seen) == nth:
            h.ex.crash_after_accept(method)
        return real(*args, **kw)

    setattr(h.ex, method, call)


LEGS = {"tp1": (1, 27_245.0, 26_585.0), "tp2": (2, 27_610.0, 26_585.0)}  # the long's mapped levels (ratio 1.0)


@pytest.mark.parametrize(("method", "nth"), [("place_market", 1), ("place_bracket", 1), ("place_bracket", 2)])
def test_killed_right_after_an_order_was_accepted(tmp_path, method, nth):
    """Killed right after the venue accepted the entry, its TP1 leg or both legs: the restart looks every PENDING
    intent up and takes the accepted orders over - the entry rebuilt, a resting leg claimed by its client order id -
    so nothing is sent twice: one entry, exactly the two legs, and no unprotected time when both legs rested."""
    h = started(tmp_path, stage="FULL", stage_days=10)
    crash_on(h, method, nth)
    with pytest.raises(SimCrash):
        h.at(ENTRY_BAR + 30)
    delattr(h.ex, method)
    assert h.journal.intents(status="PENDING")  # the journal never heard back
    restart(h, ENTRY_BAR + 50)
    h.at(ENTRY_BAR + 51)  # the interrupted cycle runs again
    h.at(ENTRY_BAR + 70)
    assert entries(h) == [f"ewb-e-{SIGNAL}-L"] and h.ex.calls.count("place_market") == 1
    assert h.ex.position().contracts == 3 and h.rt.executor.trade().trade_id == SIGNAL
    assert legs(h) == LEGS and h.ex.calls.count("place_bracket") == 2  # an accepted leg is claimed, never re-placed
    assert not h.journal.intents(status="PENDING") and "cancel" not in h.ex.calls
    if nth == 2:  # both legs rested through the crash: the restart's first look finds the position protected
        assert not h.journal.events(kind="UNPROTECTED", since=ENTRY_BAR + 50)


def test_killed_between_the_journaled_intent_and_the_send(tmp_path):
    """Killed after the entry's intent was committed and before the request left: nothing reached the venue. The
    restart finds the intent PENDING and not on the exchange (younger than 10 minutes: kept), and the interrupted
    cycle looks it up again before sending it once under the same client order id."""
    h = started(tmp_path, stage="FULL", stage_days=10)
    cid = f"ewb-e-{SIGNAL}-L"
    h.ex.fail_next("place_market", SimCrash("killed before the send"))  # raised before the venue sees it
    with pytest.raises(SimCrash):
        h.at(ENTRY_BAR + 30)
    assert h.journal.intent(cid)["status"] == "PENDING" and h.ex.orders == {}
    restart(h, ENTRY_BAR + 50)
    assert h.journal.intent(cid)["status"] == "PENDING" and "find_order" in h.ex.calls  # looked up: not there
    h.at(ENTRY_BAR + 51)  # the interrupted cycle runs again
    h.at(ENTRY_BAR + 70)
    assert entries(h) == [cid] and h.journal.intent(cid)["status"] == "ACCEPTED" and h.ex.position().contracts == 3
    assert resent_only_after_a_lookup(h, "place_market") and h.ex.calls.count("place_market") == 2
    assert legs(h) == LEGS and h.rt.executor.trade().trade_id == SIGNAL


def test_killed_right_after_the_close_was_accepted(tmp_path):
    h = started(tmp_path)
    h.at(ENTRY_BAR + 30)
    h.ex.crash_after_accept("close_position")
    h.rt.handle("/flatten", ENTRY_BAR + 40)
    with pytest.raises(SimCrash):
        h.rt.handle("/flatten_confirm", ENTRY_BAR + 41)
    assert h.ex.position() is None and h.rt.executor.trade().status == "OPEN"  # the journal never heard back
    restart(h, ENTRY_BAR + 60)
    tr = h.rt.executor.trade()
    assert (tr.status, tr.exit_reason) == ("CLOSED", "USER_FLATTEN") and h.ex.calls.count("close_position") == 1
    assert h.journal.intent(f"ewb-x-{SIGNAL}-1")["status"] == "ACCEPTED"


def ticker(product: str, price: float, seq: int) -> str:
    return json.dumps({"channel": "ticker", "sequence_num": seq,
                       "events": [{"tickers": [{"product_id": product, "price": str(price)}]}]})


def test_websocket_drop_falls_back_to_rest_polling_and_a_gap_forces_a_resync(tmp_path):
    h = started(tmp_path)
    feed = TickerFeed([h.ex.futures_id], ws_factory=lambda **kw: None, clock=h.clock)
    h.rt = h.make_runtime(feed=feed)
    h.rt.startup()
    feed.on_message(ticker(h.ex.futures_id, 26_750.0, 1))  # the futures book's mid is 26_744.5
    h.at(ENTRY_BAR + 30)
    assert h.rt.active
    quotes = h.ex.calls.count("quote")
    h.at(ENTRY_BAR + 31)
    assert h.rt.prices.mode == "ws" and h.rt.prices.futures_price() == 26_750.0  # the watchdog used the fresh feed
    assert h.ex.calls.count("quote") == quotes
    h.at(ENTRY_BAR + 95)  # 65 s without a message: REST
    assert h.rt.prices.mode == "rest" and h.rt.prices.futures_price() == h.ex.fut_price  # the REST mid
    polls = len([c for c in h.ex.calls if c == "position"])
    feed.on_message(ticker(h.ex.futures_id, 26_750.0, 2))
    feed.on_message(ticker(h.ex.futures_id, 26_750.0, 9))  # 3..8 lost
    h.at(ENTRY_BAR + 96)  # a resync request polls at once, before the 10 s poll interval
    assert len([c for c in h.ex.calls if c == "position"]) > polls and feed.gaps == 1


def test_rate_limits_and_outages_never_stop_the_loop(tmp_path):
    h = started(tmp_path)
    h.ex.fail_next("place_market", RateLimited("429"))
    h.at(ENTRY_BAR + 30)
    assert h.ex.position() is None and h.journal.intent(f"ewb-e-{SIGNAL}-L")["status"] == "PENDING"
    h.at(ENTRY_BAR + 45)  # next poll: looked up (not there), sent again with the same id
    assert h.ex.position().contracts == 1 and h.ex.calls.count("place_market") == 2
    assert resent_only_after_a_lookup(h, "place_market") and entries(h) == [f"ewb-e-{SIGNAL}-L"]
    h.ex.fail_next("position", Unavailable("503"), times=2)
    h.at(ENTRY_BAR + 60)
    assert h.journal.events(kind="POLL_ERROR")
    h.at(ENTRY_BAR + 75)
    assert h.ex.position().contracts == 1 and h.rt.executor.trade().status == "OPEN"


def test_a_stop_move_during_a_quote_outage_goes_out_with_the_quotes(tmp_path):
    """The engine trails the stop at a close while the quotes cannot be read: the stop in force stays (no
    ratio to map the new one) and the move goes out at the first poll with quotes - never lost."""
    trail = ts("2024-03-14 07:00")  # the engine trails the long's stop at this close
    h = started(tmp_path)
    h.walk(ENTRY_BAR, trail + 3600)
    before = h.rt.executor.trade().stop_spot
    h.price(float(h.bar(trail + 3600)["open"]))
    h.ex.fail_next("quote", Unavailable("503"), times=50)
    h.at(trail + 3600 + 30)  # the cycle sees the new engine stop; every quote fails
    h.ex.clear_faults()
    assert h.rt.target.position.stop != before and h.rt.executor.trade().stop_spot == before
    h.at(trail + 3600 + 45)  # the next poll has quotes
    tr = h.rt.executor.trade()
    assert tr.stop_spot == h.rt.target.position.stop
    assert h.journal.events(kind="STOP_MOVED")[-1]["data"]["stop_spot"] == tr.stop_spot


def covered(h: Harness) -> bool:
    """Our resting brackets cover the whole position."""
    pos = h.ex.position()
    return pos is not None and sum(o.size - o.filled for o in h.ex.open_orders() if o.stop_price) >= pos.contracts


def test_a_429_storm_during_a_stop_move_keeps_the_position_protected(tmp_path):
    """Every bracket request is rate-limited while the engine trails the stop: the replacement's outcome is
    unknown (never a refusal), so the resting leg keeps protecting - it is never cancelled first - and the stop in
    force stays recorded; the move goes out at the first poll the venue answers."""
    trail = ts("2024-03-14 07:00")  # the engine trails the long's stop at this close
    h = started(tmp_path)
    h.walk(ENTRY_BAR, trail + 3600)
    before = h.rt.executor.trade().stop_spot
    h.price(float(h.bar(trail + 3600)["open"]))
    h.ex.fail_next("place_bracket", RateLimited("429"), times=50)
    t0 = trail + 3600 + 30
    for k in range(4):  # the cycle sees the new engine stop, then three polls into the storm
        h.at(t0 + 10 * k)
        assert covered(h) and h.rt.executor.trade().stop_spot == before
    assert h.journal.events(kind="BRACKET_FAILED", since=t0) and not h.journal.events(kind="LEG_GAP")
    h.ex.clear_faults()
    h.at(t0 + 40)
    tr = h.rt.executor.trade()
    assert tr.stop_spot == h.rt.target.position.stop != before
    assert [o.stop_price for o in h.ex.open_orders()] == [tr.stop_price] and covered(h)
    assert not h.journal.events(kind="UNPROTECTED", since=t0)
    # the first failed move was not replaced within 30 s: a CANARY failure (spec 6.2), entries paused
    assert [e["data"]["kind"] for e in h.journal.events(kind="ORDER_UNRESOLVED")] == ["BRACKET_FAILED"]
    assert paused(h.journal)["reason"] == "incident ORDER_UNRESOLVED"


def test_killed_in_the_middle_of_a_stop_move(tmp_path):
    """The engine trails the stop; the process dies right after the venue accepted the new TP1 leg, before the old
    one was cancelled and before the TP2 leg moved. The restart keeps the trade's resting legs, the interrupted cycle
    takes the accepted new leg over by its client order id instead of placing it again, moves the TP2 leg and
    cancels the old ones: exactly one set of legs at the new stop, the position covered all along."""
    trail = ts("2024-03-14 07:00")  # the engine trails the long's stop at this close
    h = started(tmp_path, stage="FULL", stage_days=10)
    h.walk(ENTRY_BAR, trail + 3600)
    assert legs(h) == LEGS
    h.price(float(h.bar(trail + 3600)["open"]))
    h.ex.crash_after_accept("place_bracket")
    t0 = trail + 3600 + 30
    with pytest.raises(SimCrash):
        h.at(t0)  # the cycle sees the new engine stop: the new TP1 leg is accepted, then the process dies
    resting = [o for o in h.ex.orders.values() if o.kind == "BRACKET" and o.status == "OPEN"]
    assert [(o.client_order_id.split("-")[3], o.stop_price) for o in resting] == [
        ("tp1", 26_585.0), ("tp2", 26_585.0), ("tp1", 26_620.0)] and covered(h)
    restart(h, t0 + 20)
    assert covered(h)
    for k in range(1, 4):
        h.at(t0 + 20 + 10 * k - 9)  # the interrupted cycle, then the polls
        assert covered(h)
    tr = h.rt.executor.trade()
    assert (tr.stop_spot, tr.stop_price) == (h.rt.target.position.stop, 26_620.0)
    assert legs(h) == {"tp1": (1, 27_245.0, 26_620.0), "tp2": (2, 27_610.0, 26_620.0)}
    tp1s = [o for o in h.ex.orders.values() if o.kind == "BRACKET" and o.client_order_id.split("-")[3] == "tp1"]
    assert len(tp1s) == 2 and [e["data"]["leg"] for e in h.journal.events(kind="LEG_ADOPTED", since=t0)] == ["tp1"]
    assert h.journal.events(kind="STOP_MOVED")[-1]["data"]["stop"] == 26_620.0
    assert not h.journal.events(kind="UNPROTECTED", since=t0)


def test_a_close_that_keeps_failing_keeps_the_position_protected(tmp_path):
    """The engine queues the TIME exit. The venue first refuses the close because our brackets rest on the position
    (a close conflict: they are cancelled after a session read and the close is sent again at once), then
    rate-limits it three times. After every failed close the legs are made the ones for the position again - placed
    anew after the conflict's cancel - so no poll ever finds the position unprotected, and the fifth close goes
    through with the engine's reason."""
    exit_bar = ts("2024-01-04 15:00")  # the engine queues the TIME exit at this close
    h = started(tmp_path, start=TIME_SIGNAL + 3600 + 30)
    h.at(TIME_SIGNAL + 3600 + 30)
    assert h.ex.position().side == -1
    first = legs(h)
    h.price(float(h.bar(exit_bar + 3600)["open"]))

    def conflict(cid, size=None):  # type: ignore[no-untyped-def]  # once; then the venue's own answers
        del h.ex.close_position
        h.ex.calls.append("close_position")
        return PlaceResult(cid, None, False, "OPEN_BRACKET_ORDERS")

    h.ex.close_position = conflict
    h.ex.fail_next("close_position", RateLimited("429"), times=3)
    t0 = exit_bar + 3600 + 30
    for k in range(3):
        h.at(t0 + 10 * k)
        assert covered(h) and legs(h) == first
    assert h.journal.events(kind="CLOSE_CONFLICT") and h.ex.calls.count("cancel") == 1  # the legs went, and came back
    h.at(t0 + 30)
    assert h.ex.position() is None and h.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "TIME"
    assert len(h.journal.events(kind="CLOSE_FAILED")) == 3 and not h.journal.events(kind="UNPROTECTED", since=t0)
    assert h.ex.calls.count("close_position") == 5
    # a close not done by the next pass is a CANARY failure, recorded once for the trade's episode; the last failed
    # one was done by its next pass
    assert [e["data"]["kind"] for e in h.journal.events(kind="ORDER_UNRESOLVED")] == ["CLOSE_FAILED"]
    assert [e["data"]["kind"] for e in h.journal.events(kind="INCIDENT")] == ["ORDER_UNRESOLVED"]
    assert paused(h.journal)["reason"] == "incident ORDER_UNRESOLVED"


@pytest.mark.parametrize("ours", [True, False])
def test_an_entry_whose_answer_and_lookups_are_lost_is_protected_at_once(tmp_path, ours):
    """The entry's answer is lost and the order lookups fail for a minute while a position is there: it is protected
    from the first poll with a provisional trade from the entry's intent. Once the lookups answer it is settled -
    our entry (no incident), or, when the entry never reached the venue and the position was opened by hand, a
    foreign position (ADOPTED: a CANARY incident, entries paused)."""
    h = started(tmp_path)
    until = ENTRY_BAR + 30 + 65
    real = h.ex.find_order

    def degraded(cid, since):
        if h.clock() < until:
            h.ex.calls.append("find_order")
            raise Unavailable("503")
        return real(cid, since)

    h.ex.find_order = degraded
    if ours:
        h.ex.crash_after_accept("place_market", Unavailable("read timeout"))  # filled, the answer lost
    else:
        h.ex.fail_next("place_market", Unavailable("503"))  # never reached the venue
    h.at(ENTRY_BAR + 30)
    assert h.journal.intent(f"ewb-e-{SIGNAL}-L")["status"] == "PENDING"
    if not ours:
        h.ex.place_market("manual-1", 1, 1)  # the engine's long, opened by hand
    for k in range(1, 7):
        h.at(ENTRY_BAR + 30 + 10 * k)
        assert covered(h) and paused(h.journal) is None
    h.at(ENTRY_BAR + 30 + 70)  # the lookups answer again
    kinds = h.alerts.kinds()
    assert ("RECOVERED_ENTRY" in kinds) is ours and ("ADOPTED" in kinds) is not ours
    assert (paused(h.journal) is None) is ours and covered(h) and h.ex.calls.count("place_market") == 1 + (not ours)


def test_a_venue_refusing_new_orders_keeps_the_brackets_while_the_close_waits(tmp_path):
    """The engine queues the TIME exit while the venue takes cancels but refuses every new order (cancel-only): the
    close is refused at every poll and the brackets are never touched; it goes through once orders are taken."""
    exit_bar = ts("2024-01-04 15:00")  # the engine queues the TIME exit at this close
    h = started(tmp_path, start=TIME_SIGNAL + 3600 + 30)
    h.at(TIME_SIGNAL + 3600 + 30)
    before = sorted(o.order_id for o in h.ex.open_orders())
    h.price(float(h.bar(exit_bar + 3600)["open"]))

    def refuse(cid, *args, **kw):
        return PlaceResult(cid, None, False, "ORDER_ENTRY_DISABLED")

    h.ex.close_position = refuse
    h.ex.place_bracket = refuse
    t0 = exit_bar + 3600 + 30
    for k in range(3):
        h.at(t0 + 10 * k)
        assert sorted(o.order_id for o in h.ex.open_orders()) == before and covered(h)
    assert len(h.journal.events(kind="CLOSE_FAILED")) == 3 and "cancel" not in h.ex.calls
    del h.ex.close_position, h.ex.place_bracket
    h.at(t0 + 30)
    assert h.ex.position() is None and h.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "TIME"


def test_an_exit_waits_for_the_reopen(tmp_path):
    exit_bar = ts("2024-01-04 15:00")  # the engine queues the TIME exit at this close
    h = started(tmp_path, start=TIME_SIGNAL + 3600 + 30)
    h.at(TIME_SIGNAL + 3600 + 30)
    assert h.ex.position().side == -1
    h.ex.set_session("CLOSE")
    h.price(float(h.bar(exit_bar + 3600)["open"]))
    h.at(exit_bar + 3600 + 30)  # the cycle sees the TIME exit while the session is closed
    assert h.ex.position() is not None and "CLOSE_DEFERRED" in h.alerts.kinds()
    h.ex.set_session("OPEN")
    h.at(exit_bar + 3600 + 100)  # the minute job sees OPEN and polls at once
    assert h.ex.position() is None
    assert h.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "TIME"
    # the state seen at start, then every change (SHADOW gate 2 excuses only hours recorded closed)
    assert [e["data"]["state"] for e in h.journal.events(kind="SESSION")] == ["OPEN", "CLOSE", "OPEN"]


def test_an_exit_queued_before_the_cfm_halt_closes_at_the_first_open_poll(tmp_path, monkeypatch):
    """The engine queues LINEAGE at the Fri 16:00 New York close; the venue halts, so the close waits. The
    venue still reports the halt at the 18:00:05 minute check, so the halt bar's cycle (18:00:30) replaces
    the target before any poll sees OPEN: that target must still carry the exit, and the first poll that
    sees OPEN closes - not the next cycle an hour later (the engine's exit fills at the 18:00 open)."""
    fri16 = HALT - 3600

    class Forced(loop_mod._Run):  # type: ignore[misc, valid-type]
        def _may_enter(self, bar, t_close):  # type: ignore[no-untyped-def]
            if bar.ts in (fri16, HALT):  # a broken lineage (the engine repeats the request while it holds)
                self._request_exit("LINEAGE")
            return super()._may_enter(bar, t_close)

    monkeypatch.setattr(loop_mod, "_Run", Forced)
    h = started(tmp_path, start=LONG_ENTRY + 30)
    h.at(LONG_ENTRY + 30)
    assert h.ex.position().side == 1
    h.ex.set_session("CLOSE")  # the CFM halt
    h.price(float(h.bar(HALT)["open"]))
    h.at(HALT + 30)  # the cycle for the Fri 16:00 bar sees the exit while the venue is halted
    assert h.rt.target.exit_queued_ts == fri16 and "CLOSE_DEFERRED" in h.alerts.kinds()
    h.price(float(h.bar(HALT + 3600)["open"]))
    h.at(HALT + 3600 + 5)  # still halted at this minute check
    assert h.ex.position() is not None
    h.ex.set_session("OPEN")
    h.at(HALT + 3600 + 30)  # the halt bar's cycle: the exit queued at 16:00 is still the target
    assert (h.rt.target.bar_ts, h.rt.target.exit_reason, h.rt.target.exit_queued_ts) == (HALT, "LINEAGE", fri16)
    h.at(HALT + 3600 + 65)  # the minute job sees OPEN and polls at once
    assert h.ex.position() is None
    assert h.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "LINEAGE"


def test_a_stop_crossed_during_the_cfm_halt_closes_at_the_reopen(tmp_path):
    """Spot trades through the long's stop during the halt hour while the futures venue is closed: the engine
    stops out at the 18:00 open; the live futures reopen above the resting stop, so the bracket does not
    trigger - the halt bar's cycle closes the trade (STOP_HALT) instead of an hour later (ENGINE_FLAT)."""
    df = synth_frame()
    stop = compute_target(make_lock(df), df, HALT - 3600).position.stop  # the stop in force during the halt bar
    df.loc[df["ts"] == HALT, "low"] = stop - 10.0
    h = started(tmp_path, start=LONG_ENTRY + 30, df=df)
    h.at(LONG_ENTRY + 30)
    assert h.ex.position().side == 1
    h.ex.set_session("CLOSE")
    h.price(float(h.bar(HALT)["open"]))
    h.at(HALT + 30)
    h.price(stop - 10.0)  # nothing fills while the venue is halted
    h.at(HALT + 1800)
    assert h.ex.position() is not None and h.rt.executor.trade().status == "OPEN"
    h.price(float(h.bar(HALT + 3600)["open"]))  # the reopen, above the stop: the bracket stays
    h.ex.set_session("OPEN")
    h.at(HALT + 3600 + 30)  # minute (OPEN, nothing to close yet), then the halt bar's cycle
    assert h.rt.target.stop_pending_reopen and h.ex.position() is None
    assert h.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "STOP_HALT"
