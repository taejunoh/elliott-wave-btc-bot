from __future__ import annotations

import pytest

from ewbot.backtest.benchmarks import buy_hold
from ewbot.engine.loop import Trade
from ewbot.stratb.baselines import buy_hold_b, random_control, random_signals, trade_shapes
from ewbot.stratb.config import BConfig, StratBParams, asset_costs
from ewbot.stratb.data15 import resample_1h
from ewbot.stratb.research import backtest_b, trades_by_asset
from ewbot.stratb.signals import build_frame, build_frames
from tests.stratb.helpers import RANDOM_T0, random_df

M15 = 900


def _trade(d, ref, stop, tp1, tp2, ts=0):
    return Trade(ts, ts + 900, "B-BTC", d, 3, ref, ref, stop, tp1, tp2, 0.0, 0.0, 0.0, 0.0, "TP2", {"entry_ref": ref})


def test_buy_hold_is_a_50_50_portfolio_of_strategy_a_buy_holds():
    dfs = {"BTC": random_df(1000, 1), "ETH": random_df(1000, 2, start=2_000.0)}
    frames = build_frames(dfs)
    start, end = int(frames["BTC"].ts[100]) // 3600 * 3600, int(frames["BTC"].ts[900]) // 3600 * 3600
    eq, trades = buy_hold_b(frames, start, end, BConfig())
    parts = [buy_hold(resample_1h(dfs[a]), start, end, asset_costs(BConfig(), a), 5_000.0) for a in ("BTC", "ETH")]
    assert [p.ts for p in eq] == [p.ts for p in parts[0][0]] == [p.ts for p in parts[1][0]]
    assert [p.equity for p in eq] == pytest.approx([a.equity + b.equity for a, b in zip(parts[0][0], parts[1][0])])
    assert trades == parts[0][1] + parts[1][1] and eq[0].equity == 10_000.0 and trades[0].setup == "BH"


def test_shapes_come_from_the_signal_reference_price():
    (a, b) = trade_shapes([_trade(1, 100.0, 98.0, 103.0, 105.0), _trade(-1, 50.0, 51.0, 48.5, 47.0)])
    assert (a.direction, a.stop_frac, a.tp1_r, a.tp2_r) == (1, 0.02, 1.5, 2.5)
    assert (b.direction, b.stop_frac, b.tp1_r, b.tp2_r) == (-1, 0.02, 1.5, 3.0)


def test_random_entries_are_seeded_eligible_and_use_each_shape_once():
    df = random_df(2000, 2).drop(index=[500]).reset_index(drop=True)
    f = build_frame(df)
    shapes = trade_shapes([_trade(1, 100.0, 98.0, 102.0, 104.0), _trade(-1, 100.0, 103.0, 97.0, 94.0),
                           _trade(1, 100.0, 99.0, 101.5, 103.0)])
    start, end = int(f.ts[200]), int(f.ts[1999])
    a = random_signals(f, shapes, 3, start, end, seed=5)
    assert a == random_signals(f, shapes, 3, start, end, seed=5) != random_signals(f, shapes, 3, start, end, seed=6)
    assert random_signals(f, shapes, 3, start, end, seed=[5, 1]) != a
    assert len(a) == 3 and sorted(s.direction for s in a) == [-1, 1, 1]
    for s in a:
        assert start <= s.ts < end and s.i < len(f) - 1 and not f.synthetic[s.i] and not f.synthetic[s.i + 1]
        assert s.entry_ref == f.close[s.i] and s.direction * (s.entry_ref - s.stop) > 0
    many = random_signals(f, shapes, 1500, start, end, seed=1)
    assert len({s.i for s in many}) == len(many) and all(not f.synthetic[s.i + 1] for s in many)


def test_the_random_control_imitates_each_asset_and_is_deterministic():
    dfs = {"BTC": random_df(6000, 3), "ETH": random_df(6000, 3, t0=RANDOM_T0 - 20 * M15)}
    p = StratBParams(zz_k=3.0, zone=(0.5, 0.786))
    cfg = BConfig(params=p)
    res = backtest_b(dfs, cfg)
    frames = build_frames(dfs, p)
    start, end = int(frames["BTC"].ts[0]), int(frames["BTC"].ts[-1]) + M15
    n = trades_by_asset([t for t in res.trades if start <= t.entry_ts < end])
    assert n["BTC"] > 0 and n["ETH"] > 0
    one = random_control(frames, cfg, res.trades, start, end, runs=6, seed=10, workers=1)
    assert one["runs"] == 6 and one["n_signals"] == n
    assert set(one["dist"]) == {"sharpe", "max_dd", "total_return", "calmar", "n_trades"}
    assert set(one["dist"]["sharpe"]) == {"mean", "p05", "p50", "p95"}
    assert 0 <= one["dist"]["n_trades"]["p95"] <= sum(n.values())
    assert random_control(frames, cfg, res.trades, start, end, runs=6, seed=10, workers=2) == one


def test_no_b_trade_means_no_random_run():
    frames = build_frames({"BTC": random_df(500, 1), "ETH": random_df(500, 2)})
    f = frames["BTC"]
    out = random_control(frames, BConfig(), [], int(f.ts[0]), int(f.ts[-1]), runs=10)
    assert out == {"runs": 0, "n_signals": {"BTC": 0, "ETH": 0}, "seed": 0, "dist": {}}
