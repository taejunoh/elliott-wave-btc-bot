"""Plan 3 spec 7 `일치 테스트`: synthetic hourly bars through the live loop and the SimExchange; the entry
decisions, stops, TPs and exits must match the paper replay of the same bars.

Three walks cover every exit path of the strategy's trades on the synthetic series: a short closed by
its TIME stop (exit queued at a close, executed by the runtime), a short with a trailing stop that is
stopped out (bracket), and a long with a trailing stop, TP1 + break-even and TP2 (brackets). The basis
ratio is 1.0005, so every futures level is the spot level x 1.0005 on the $5 tick. The walks run in the
first 30 days of FULL (3 contracts: a 1-contract TP1 leg and a 2-contract TP2 leg), so the live trade
follows the engine's TP1 / break-even / TP2 path; a 1-contract CANARY trade exits whole at TP1.

The levels are checked at the venue - every bracket and fill the SimExchange received, each leg named by its
client order id - against futures levels computed here from the engine's spot levels and the rule of Plan 3
interpretation 4 (`fut`), never with the live code's own mapping."""

from __future__ import annotations

import math

import pytest

from ewbot.backtest.holdout import holdout_config
from ewbot.backtest.runner import run_backtest
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.config import BotConfig
from ewbot.live.target import traced_with_exits
from ewbot.paper.decisions import build_decisions, same_decision
from ewbot.types import H1
from tests.live.harness import Harness
from tests.paper.helpers import PARAMS, synth_frame, ts

RATIO = 1.0005
TICK = 5.0
WALKS = {
    "time_exit": (ts("2024-01-01 18:00"), ts("2024-01-04 19:00")),
    "trailed_stop": (ts("2024-02-15 23:00"), ts("2024-02-18 20:00")),
    "tp1_tp2": (ts("2024-03-13 20:00"), ts("2024-03-16 21:00")),
}


def fut(spot: float, direction: int) -> float:
    """Interpretation 4, computed here: spot level x RATIO on the $5 tick, rounded down for a long and up for a short
    (a stop never triggers before the engine's, a take-profit moves toward the entry)."""
    q = spot * RATIO / TICK
    return (math.floor(q) if direction > 0 else math.ceil(q)) * TICK


def leg(order) -> str:  # type: ignore[no-untyped-def]
    """The leg a bracket of ours was placed for: `ewb-b-<trade>-<leg>-<generation>`."""
    return order.client_order_id.split("-")[3]


@pytest.fixture(scope="module")
def reference():
    """The paper runtime's view: one replay over every synthetic bar."""
    df = synth_frame()
    cfg = holdout_config(BotConfig(), PARAMS)
    with traced_with_exits() as (trace, _, _):
        res = run_backtest(cfg, df, HOLDOUT_START, None, allow_holdout=True)
    return res, trace, build_decisions(res, trace, list(res.trades))


@pytest.fixture(scope="module", params=sorted(WALKS))
def walked(request, tmp_path_factory, reference):
    start, end = WALKS[request.param]
    h = Harness(tmp_path_factory.mktemp(request.param), start + 30, stage="FULL", stage_days=10, ratio=RATIO)
    h.price(float(h.bar(start)["open"]))
    h.rt.startup()
    h.walk(start, end)
    yield request.param, h, start, end, reference
    h.journal.close()


def engine_trade(h: Harness, res):  # type: ignore[no-untyped-def]
    tr = h.rt.executor.trade()
    return tr, next(t for t in res.trades if t.entry_ts == tr.trade_id + H1)


def test_recorded_decisions_equal_the_paper_replay(walked):
    _, h, start, end, (res, trace, decisions) = walked
    last_bar = end - 2 * H1  # the last cycle ran at end - 1h + 30 s for the bar end - 2h
    want = {d["id"]: d for d in decisions if d["ts"] <= last_bar}
    got = {d["id"]: d for d in h.journal.decisions()}
    assert sorted(got) == sorted(want) and len(want) >= 3  # at least SIGNAL, ENTRY and the exit
    assert all(same_decision(got[k], want[k]) for k in want)


def test_entries_follow_the_engine_signals(walked):
    _, h, start, end, (res, trace, _) = walked
    signals = [o for o in trace.orders if start - H1 <= o.ts <= end - 2 * H1]
    entries = [e["data"] for e in h.journal.events(kind="ENTRY")]
    assert [(e["trade_id"], e["side"], e["contracts"]) for e in entries] == [(o.ts, o.direction, 3) for o in signals]
    assert len(entries) == 1  # one trade per walk
    sent = [o for o in h.ex.orders.values() if o.kind == "MARKET" and o.client_order_id.startswith("ewb-e-")]
    assert [(o.client_order_id, o.side, o.size) for o in sent] == [
        (f"ewb-e-{o.ts}-{'L' if o.direction > 0 else 'S'}", o.direction, 3) for o in signals]


def test_the_brackets_rest_at_the_mapped_engine_levels(walked):
    """Every bracket the venue received, by leg and in order: the entry's TP1 leg (1 contract) and TP2 leg (2) at the
    engine's mapped TPs and initial stop; both moved at the cycle after each engine stop move; after the live TP1 the
    TP2 leg alone with its stop at least the live entry (break-even), placed before the next cycle - not an hour
    later with the engine's BREAKEVEN. Each STOP_MOVED carries the stop the venue received."""
    name, h, start, end, (res, trace, _) = walked
    tr, engine = engine_trade(h, res)
    d = engine.direction
    tp1, tp2 = fut(engine.tp1, d), fut(engine.tp2, d)
    entry = next(f for f in h.ex.fills if f["client_order_id"].startswith(f"ewb-e-{tr.trade_id}-"))
    be = (math.ceil(entry["price"] / TICK) if d > 0 else math.floor(entry["price"] / TICK)) * TICK  # >= the entry
    moves = [s for s in trace.stops if s.trade_key == engine.entry_ts and s.ts <= end - 2 * H1]
    live_tp1 = [e["ts"] for e in h.journal.events(kind="TP1_FILLED")]
    # the engine moves its stop at the close of bar s.ts: the cycle at s.ts + 1 h + 30 s moves the legs
    timeline = [(entry["ts"], "entry", None)] + sorted(
        [(s.ts + H1 + 30, "engine", s.new) for s in moves] + [(t, "tp1", None) for t in live_tp1], key=lambda e: e[0])
    spot, stop, after_tp1 = engine.stop0, fut(engine.stop0, d), False
    resting: dict[str, tuple[int, float, float]] = {}
    want: list[tuple[float, float, dict[str, tuple[int, float, float]]]] = []  # (placed from, until, legs placed)
    moved: list[tuple[float, float]] = []  # (futures stop, engine stop) of each STOP_MOVED
    for when, kind, new in timeline:
        until = when + 1  # in that very pass
        if kind == "tp1":  # the live TP1: before the next cycle
            after_tp1, until = True, when // H1 * H1 + H1 + 30
        elif kind == "engine":
            spot, stop = new, fut(new, d)
        if after_tp1:
            stop = max(stop, be) if d > 0 else min(stop, be)
        legs = {"tp2": (2, tp2, stop)} if after_tp1 else {"tp1": (1, tp1, stop), "tp2": (2, tp2, stop)}
        if new_legs := {k: v for k, v in legs.items() if resting.get(k) != v}:
            want.append((when, until, new_legs))
        if resting and stop != resting["tp2"][2]:
            moved.append((stop, spot))
        resting = legs

    placed: list[tuple[float, dict[str, tuple[int, float | None, float | None]]]] = []
    for o in h.ex.orders.values():  # every order, in the order the venue received them
        if o.kind != "BRACKET":
            continue
        if not placed or placed[-1][0] != o.created_ts:
            placed.append((o.created_ts, {}))
        placed[-1][1][leg(o)] = (o.size, o.limit_price, o.stop_price)
    assert [legs for _, legs in placed] == [legs for _, _, legs in want]
    assert all(lo <= when < hi for (when, _), (lo, hi, _) in zip(placed, want))
    assert [(e["data"]["stop"], e["data"]["stop_spot"]) for e in h.journal.events(kind="STOP_MOVED")] == moved
    assert h.journal.events(kind="PROTECTED_FIRST")[0]["data"]["after_entry_s"] <= 30.0
    if name == "tp1_tp2":
        assert [s.kind for s in moves] == ["TRAIL", "BREAKEVEN"] and tr.tp1_filled and len(live_tp1) == 1
        assert want[2][2] == {"tp2": (2, tp2, be)}  # the live break-even, above the trailed stop
    else:
        assert live_tp1 == [] and len(moves) == {"time_exit": 0, "trailed_stop": 1}[name]


def test_exits_happen_in_the_engine_exit_hour(walked):
    name, h, start, end, (res, trace, _) = walked
    tr, engine = engine_trade(h, res)
    d = engine.direction
    closed = [e for e in h.journal.events(kind="CLOSED") if e["data"]["trade_id"] == tr.trade_id]
    assert tr.status == "CLOSED" and len(closed) == 1 and h.ex.position() is None and h.ex.open_orders() == []
    when = closed[0]["ts"]
    assert engine.exit_ts <= when < engine.exit_ts + H1, (name, engine.exit_reason)
    expected = {"time_exit": "TIME", "trailed_stop": "BRACKET", "tp1_tp2": "BRACKET"}[name]
    assert closed[0]["data"]["reason"] == expected
    assert engine.exit_reason == {"time_exit": "TIME", "trailed_stop": "STOP", "tp1_tp2": "TP2"}[name]
    last = h.ex.fills[-1]  # the venue's last fill flattened the trade
    o = h.ex.orders[last["order_id"]]
    if name == "time_exit":  # the runtime's close at market
        assert (o.kind, o.client_order_id, last["side"], last["size"]) == ("MARKET", f"ewb-x-{tr.trade_id}-1", -d, 3)
        return
    assert o.kind == "BRACKET" and leg(o) == "tp2" and last["size"] == 2
    assert h.journal.events(kind="BRACKET_FILL")[-1]["data"]["leg"] == "tp2"
    if name == "tp1_tp2":  # TP2 filled at its limit
        assert last["price"] == o.limit_price == fut(engine.tp2, d)
    else:  # the trailed stop triggered: filled at the market beyond it
        trailed = [s.new for s in trace.stops if s.trade_key == engine.entry_ts and s.ts < engine.exit_ts]
        assert o.stop_price == fut(trailed[-1], d) and d * (last["price"] - o.stop_price) <= 0
