"""Plan 3 spec 7 `일치 테스트`: synthetic hourly bars through the live loop and the SimExchange; the entry
decisions, stops, TPs and exits must match the paper replay of the same bars.

Three walks cover every exit path of the strategy's trades on the synthetic series: a short closed by
its TIME stop (exit queued at a close, executed by the runtime), a short with a trailing stop that is
stopped out (bracket), and a long with a trailing stop, TP1 + break-even and TP2 (brackets). The basis
ratio is 1.0005, so every futures level is the spot level x 1.0005 on the $5 tick. The walks run in the
first 30 days of FULL (3 contracts: a 1-contract TP1 leg and a 2-contract TP2 leg), so the live trade
follows the engine's TP1 / break-even / TP2 path; a 1-contract CANARY trade exits whole at TP1."""

from __future__ import annotations

import pytest

from ewbot.backtest.holdout import holdout_config
from ewbot.backtest.runner import run_backtest
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.config import BotConfig
from ewbot.live.pricing import map_level
from ewbot.live.target import traced_with_exits
from ewbot.paper.decisions import build_decisions, same_decision
from ewbot.types import H1
from tests.live.harness import Harness
from tests.paper.helpers import PARAMS, synth_frame, ts

RATIO = 1.0005
WALKS = {
    "time_exit": (ts("2024-01-01 18:00"), ts("2024-01-04 19:00")),
    "trailed_stop": (ts("2024-02-15 23:00"), ts("2024-02-18 20:00")),
    "tp1_tp2": (ts("2024-03-13 20:00"), ts("2024-03-16 21:00")),
}


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


def test_stops_and_take_profits_are_the_mapped_engine_levels(walked):
    name, h, start, end, (res, trace, _) = walked
    tr = h.rt.executor.trade()
    engine = next(t for t in res.trades if t.entry_ts == tr.trade_id + H1)
    d = engine.direction
    assert tr.tp1_price == map_level(engine.tp1, RATIO, 5.0, d) and tr.tp2_price == map_level(engine.tp2, RATIO, 5.0, d)
    moves = [s for s in trace.stops if s.trade_key == engine.entry_ts and s.ts <= end - 2 * H1]
    live_moves: list[float] = []
    for e in h.journal.events(kind="STOP_MOVED"):  # the live TP1 moves the stop before the engine's close does
        if not live_moves or live_moves[-1] != e["data"]["stop_spot"]:
            live_moves.append(e["data"]["stop_spot"])
    assert live_moves == [s.new for s in moves]
    first = h.journal.events(kind="PROTECTED_FIRST")[0]["data"]
    assert first["after_entry_s"] <= 30.0
    if name == "tp1_tp2":
        assert [s.kind for s in moves] == ["TRAIL", "BREAKEVEN"] and tr.tp1_filled


def test_exits_happen_in_the_engine_exit_hour(walked):
    name, h, start, end, (res, trace, _) = walked
    tr = h.rt.executor.trade()
    engine = next(t for t in res.trades if t.entry_ts == tr.trade_id + H1)
    closed = [e for e in h.journal.events(kind="CLOSED") if e["data"]["trade_id"] == tr.trade_id]
    assert tr.status == "CLOSED" and len(closed) == 1 and h.ex.position() is None and h.ex.open_orders() == []
    when = closed[0]["ts"]
    assert engine.exit_ts <= when < engine.exit_ts + H1, (name, engine.exit_reason)
    expected = {"time_exit": "TIME", "trailed_stop": "BRACKET", "tp1_tp2": "BRACKET"}[name]
    assert closed[0]["data"]["reason"] == expected
    assert engine.exit_reason == {"time_exit": "TIME", "trailed_stop": "STOP", "tp1_tp2": "TP2"}[name]
