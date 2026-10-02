from __future__ import annotations

import pytest

import ewbot.engine.loop as loop_mod
from ewbot.backtest.holdout import holdout_config
from ewbot.backtest.runner import run_backtest
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.config import BotConfig
from ewbot.execution.paper import PaperBroker
from ewbot.paper.trace import traced_engine
from ewbot.waves.engine import WaveEngine
from tests.paper.helpers import PARAMS, synth_frame, ts


@pytest.fixture(scope="module")
def runs():
    df = synth_frame()
    cfg = holdout_config(BotConfig(), PARAMS)
    plain = run_backtest(cfg, df, HOLDOUT_START, None, allow_holdout=True)
    with traced_engine() as trace:
        traced = run_backtest(cfg, df, HOLDOUT_START, None, allow_holdout=True)
    cut = df[df["ts"] <= ts("2024-03-15 06:00")]
    with traced_engine() as open_trace:
        open_res = run_backtest(cfg, cut, HOLDOUT_START, None, allow_holdout=True)
    return plain, traced, trace, open_res, open_trace


def test_tracing_does_not_change_the_run(runs):
    plain, traced, trace, _, _ = runs
    assert traced == plain  # trades, equity, signals and events bit-identical
    assert trace.brokers == 1 and len(plain.trades) >= 5


def test_original_classes_are_restored_even_after_an_error():
    with pytest.raises(RuntimeError):
        with traced_engine():
            assert loop_mod.PaperBroker is not PaperBroker
            raise RuntimeError("boom")
    assert loop_mod.PaperBroker is PaperBroker and loop_mod.WaveEngine is WaveEngine


def test_orders_match_signals(runs):
    _, res, trace, _, _ = runs
    assert [o.ts for o in trace.orders] == [s.ts for s in res.signals]
    assert [(o.stop, o.tp1, o.tp2, o.direction) for o in trace.orders] == [
        (s.stop, s.tp1, s.tp2, s.direction) for s in res.signals
    ]


def test_fills_reconstruct_every_trade(runs):
    _, res, trace, _, _ = runs
    assert trace.end_fill is None  # the last trade closed before the data ends
    for t in res.trades:
        mine = [f for f in trace.fills if f.trade_key == t.entry_ts]
        assert mine[0].kind == "ENTRY" and mine[0].ts == t.entry_ts and mine[0].contracts == t.contracts
        assert mine[0].price == t.entry_price
        exits = mine[1:]
        assert sum(f.contracts for f in exits) == t.contracts and exits[-1].ts == t.exit_ts
        avg = sum(f.price * f.contracts for f in exits) / t.contracts
        assert avg == pytest.approx(t.exit_price_avg, rel=1e-12)


def test_stop_moves_breakeven_after_tp1_and_trailing_only_tightens(runs):
    _, res, trace, _, _ = runs
    by_entry = {t.entry_ts: t for t in res.trades}
    kinds = {s.kind for s in trace.stops}
    assert kinds == {"BREAKEVEN", "TRAIL"}
    for s in trace.stops:
        t = by_entry[s.trade_key]
        assert t.direction * (s.new - s.old) > 0  # stops only move in the trade's favour
        assert t.entry_ts <= s.ts <= t.exit_ts
        if s.kind == "BREAKEVEN":
            assert s.new == t.entry_price
            assert any(f.kind == "TP1" and f.ts == s.ts and f.trade_key == s.trade_key for f in trace.fills)
    assert trace.last_state is not None and trace.last_state.ts == int(synth_frame()["ts"].iloc[-1])


def test_a_position_open_at_the_last_bar_is_the_end_fill(runs):
    _, _, _, res, trace = runs
    last = res.trades[-1]
    assert last.exit_reason == "END" and trace.end_fill is not None
    assert trace.end_fill.trade_key == last.entry_ts == ts("2024-03-13 23:00")
    assert trace.end_fill.ts == ts("2024-03-15 06:00")
    assert all(f.kind != "END" for f in trace.fills)  # the END close is not a broker fill
