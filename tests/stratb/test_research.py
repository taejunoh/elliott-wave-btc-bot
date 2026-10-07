from __future__ import annotations

import pandas as pd
import pytest

from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.engine.loop import EquityPoint, Trade
from ewbot.stratb import research
from ewbot.stratb.config import BConfig, StratBParams, grid_combos
from ewbot.stratb.engine import BResult, run_engine
from ewbot.stratb.research import (
    backtest_b,
    evaluate_folds_b,
    pick_plateau_b,
    run_grid_b,
    trades_by_asset,
    walk_forward_b,
)
from ewbot.stratb.signals import build_frames, generate_signals
from tests.stratb.helpers import SIGNAL_BAR, long_setup_df, mirror, random_df

H1 = 3600
COMBOS = grid_combos()


def _result(points, trades=()):
    return BResult(tuple(trades), tuple(EquityPoint(t, e) for t, e in points), (), (), (), (), (), ())


def _trade(ts):
    return Trade(ts, ts + H1, "B-BTC", 1, 1, 100.0, 101.0, 99.0, 101.0, 102.0, 1.0, 0.0, 0.0, 1.0, "TP2",
                 {"asset": "BTC"})


def test_research_runs_refuse_2024_bars():
    early = random_df(400, 2, t0=HOLDOUT_START - 600 * 900)  # ETH ends before 2024, BTC does not
    dfs = {"BTC": random_df(400, 1, t0=HOLDOUT_START - 200 * 900), "ETH": early}
    with pytest.raises(ValueError):
        backtest_b(dfs, BConfig())
    with pytest.raises(ValueError):
        backtest_b({"ETH": early}, BConfig(), end_ts=HOLDOUT_START + 900)
    with pytest.raises(ValueError):
        run_grid_b(dfs, int(early["ts"].iloc[0]), HOLDOUT_START + 900)
    assert backtest_b(dfs, BConfig(), end_ts=HOLDOUT_START).equity[-1].ts < HOLDOUT_START
    assert backtest_b(dfs, BConfig(), include_holdout=True).equity[-1].ts >= HOLDOUT_START


def test_a_backtest_is_the_engine_on_each_asset_rule_signals():
    dfs = {"BTC": long_setup_df(), "ETH": mirror(long_setup_df())}
    p = StratBParams(zz_k=2.0)
    res = backtest_b(dfs, BConfig(params=p))
    frames = build_frames(dfs, p)
    assert res == run_engine(frames, {a: generate_signals(f, p) for a, f in frames.items()}, BConfig(params=p))
    assert [(s.i, o.asset, o.direction) for s, o in zip(res.signals, res.orders)] == [(SIGNAL_BAR, "BTC", 1),
                                                                                       (SIGNAL_BAR, "ETH", -1)]
    assert {t.entry_ts for t in res.trades} == {int(frames["BTC"].ts[SIGNAL_BAR + 1])}
    assert trades_by_asset(res.trades) == {"BTC": 1, "ETH": 1}


def test_on_a_two_level_grid_the_plateau_is_the_best_own_calmar():
    cal = {c: float(k) for k, c in enumerate(COMBOS)}
    assert pick_plateau_b(cal, {c: 12 for c in COMBOS}) == COMBOS[-1]
    tie = {c: 1.0 for c in COMBOS}
    assert pick_plateau_b(tie, {c: 12 for c in COMBOS}) == COMBOS[0]  # ties: smallest grid index


def test_the_plateau_needs_17_eligible_combos_with_12_trades_and_a_finite_calmar():
    cal = {c: float(k) for k, c in enumerate(COMBOS)}
    few = {c: (12 if k < 16 else 11) for k, c in enumerate(COMBOS)}
    with pytest.raises(ValueError):
        pick_plateau_b(cal, few)
    few[COMBOS[20]] = 12
    assert pick_plateau_b(cal, few) == COMBOS[20]
    cal[COMBOS[20]] = float("inf")
    with pytest.raises(ValueError):
        pick_plateau_b(cal, few)


def test_folds_pick_on_training_stitch_the_test_windows_and_decide_proceed():
    t0 = 1_600_000_000 // H1 * H1
    folds = [(t0, t0 + 10 * H1, t0 + 10 * H1, t0 + 20 * H1), (t0 + 10 * H1, t0 + 20 * H1, t0 + 20 * H1, t0 + 30 * H1)]
    flat = [(t0 + k * H1, 100.0) for k in range(30)]
    win = [(t0 + k * H1, 100.0 + k + (-3.0 if k % 5 == 4 else 0.0)) for k in range(30)]
    results = {c: _result(flat) for c in COMBOS}
    results[COMBOS[7]] = _result(win, [_trade(t0 + 2 * H1), _trade(t0 + 22 * H1)])
    out = evaluate_folds_b(results, folds, 1_000.0, min_trades=0)
    assert [f["chosen"] for f in out["folds"]] == [COMBOS[7], COMBOS[7]]
    assert out["chosen_final"] == COMBOS[7] and out["chosen_final_from_fold"] == 1 and not out["chosen_final_fallback"]
    assert out["stitched_equity"][-1][1] == pytest.approx(1_000.0 * win[29][1] / win[9][1])
    assert out["proceed"] is True and len(out["oos_trades"]) == 1
    turn = [(t, e if k < 10 else 109.0 - 2.0 * (k - 9)) for k, (t, e) in enumerate(win)]  # wins, then loses
    results[COMBOS[7]] = _result(turn, [_trade(t0 + 2 * H1)])
    out = evaluate_folds_b(results, folds, 1_000.0, min_trades=0)
    assert [f["chosen"] for f in out["folds"]] == [COMBOS[7], COMBOS[0]]
    assert out["stitched_metrics"]["total_return"] < 0 and out["proceed"] is False
    first = [_trade(t0 + 2 * H1)]  # trades only in fold 1's training window: fold 2 has no selectable combo
    results = {c: _result(win if k == 7 else flat, first if k < 17 else ()) for k, c in enumerate(COMBOS)}
    out = evaluate_folds_b(results, folds, 1_000.0, min_trades=1)
    assert [f["chosen"] for f in out["folds"]] == [COMBOS[7], None]
    assert out["chosen_final"] == COMBOS[7] and out["chosen_final_fallback"] is True and out["proceed"] is False


def test_the_grid_runs_every_combo_once_on_both_assets_with_the_breakers_off():
    dfs = {"BTC": random_df(3000, 3), "ETH": random_df(3000, 4)}
    start, end = int(dfs["BTC"]["ts"].iloc[400]), int(dfs["BTC"]["ts"].iloc[-1]) + 900
    out = run_grid_b(dfs, start, end, workers=1)
    assert list(out) == COMBOS and all(r.equity[0] == EquityPoint(start - H1, 10_000.0) for r in out.values())
    assert {a for r in out.values() for a, n in trades_by_asset(r.trades).items() if n} == {"BTC", "ETH"}
    assert run_grid_b(dfs, start, end, workers=2) == out


def test_walk_forward_cuts_2024_and_uses_strategy_a_folds(monkeypatch):
    seen = {}

    def fake_grid(dfs, start, end, workers):
        assert sorted(dfs) == ["BTC", "ETH"]
        seen.update(max_ts=max(int(df["ts"].max()) for df in dfs.values()), start=start, end=end)
        return {c: _result([(start, 10_000.0), (end - H1, 10_000.0)]) for c in COMBOS}

    monkeypatch.setattr(research, "run_grid_b", fake_grid)
    df = pd.concat([random_df(50, 1, t0=HOLDOUT_START - 50 * 900), random_df(50, 2, t0=HOLDOUT_START)])
    out = walk_forward_b({"BTC": df, "ETH": df}, workers=1)
    assert seen == {"max_ts": HOLDOUT_START - 900, "start": 1_483_228_800, "end": HOLDOUT_START}
    assert len(out["folds"]) == 10 and out["folds"][0]["test"][0] == 1_546_300_800  # 2019-01-01
    assert out["chosen_final"] is None and out["proceed"] is False and len(out["combo_table"]) == 32
    assert out["combo_table"][0]["n_trades_by_asset"] == {"BTC": 0, "ETH": 0}
    with pytest.raises(ValueError, match="every asset"):
        walk_forward_b({"BTC": df}, workers=1)
