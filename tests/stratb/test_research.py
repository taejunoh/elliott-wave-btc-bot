from __future__ import annotations

import pandas as pd
import pytest

from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.engine.loop import EquityPoint, Trade
from ewbot.stratb import research
from ewbot.stratb.config import BConfig, StratBParams, grid_combos
from ewbot.stratb.engine import BResult, run_engine
from ewbot.stratb.research import backtest_b, evaluate_folds_b, pick_plateau_b, run_grid_b, walk_forward_b
from ewbot.stratb.signals import build_frame, generate_signals
from tests.stratb.helpers import SIGNAL_BAR, T0, long_setup_df, random_df

H1 = 3600
COMBOS = grid_combos()


def _result(points, trades=()):
    return BResult(tuple(trades), tuple(EquityPoint(t, e) for t, e in points), (), (), (), (), None, ())


def _trade(ts):
    return Trade(ts, ts + H1, "B", 1, 1, 100.0, 101.0, 99.0, 101.0, 102.0, 1.0, 0.0, 0.0, 1.0, "TP2", {})


def test_research_runs_refuse_2024_bars():
    df = random_df(400, 1, t0=HOLDOUT_START - 200 * 900)
    with pytest.raises(ValueError):
        backtest_b(df, BConfig())
    with pytest.raises(ValueError):
        backtest_b(df, BConfig(), end_ts=HOLDOUT_START + 900)
    with pytest.raises(ValueError):
        run_grid_b(df, int(df["ts"].iloc[0]), HOLDOUT_START + 900)
    assert backtest_b(df, BConfig(), end_ts=HOLDOUT_START).equity[-1].ts < HOLDOUT_START
    assert backtest_b(df, BConfig(), include_holdout=True).equity[-1].ts >= HOLDOUT_START


def test_a_backtest_is_the_engine_on_the_rule_signals():
    df = long_setup_df()
    p = StratBParams(zz_k=2.0)
    res = backtest_b(df, BConfig(params=p))
    f = build_frame(df, p)
    assert res == run_engine(f, generate_signals(f, p), BConfig(params=p))
    assert res.signals[0].i == SIGNAL_BAR and res.trades[0].entry_ts == int(f.ts[SIGNAL_BAR + 1])


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


def test_the_grid_runs_every_combo_once_with_the_breakers_off():
    df = random_df(3000, 3)
    start = int(df["ts"].iloc[400])
    out = run_grid_b(df, start, int(df["ts"].iloc[-1]) + 900, workers=1)
    assert list(out) == COMBOS and all(r.equity[0].ts >= start for r in out.values())
    assert any(r.trades for r in out.values())
    assert run_grid_b(df, start, int(df["ts"].iloc[-1]) + 900, workers=2) == out


def test_walk_forward_cuts_2024_and_uses_strategy_a_folds(monkeypatch):
    seen = {}

    def fake_grid(df, start, end, workers):
        seen.update(max_ts=int(df["ts"].max()), start=start, end=end)
        return {c: _result([(start, 10_000.0), (end - H1, 10_000.0)]) for c in COMBOS}

    monkeypatch.setattr(research, "run_grid_b", fake_grid)
    df = pd.concat([random_df(50, 1, t0=HOLDOUT_START - 50 * 900), random_df(50, 2, t0=HOLDOUT_START)])
    out = walk_forward_b(df, workers=1)
    assert seen == {"max_ts": HOLDOUT_START - 900, "start": 1_483_228_800, "end": HOLDOUT_START}
    assert len(out["folds"]) == 10 and out["folds"][0]["test"][0] == 1_546_300_800  # 2019-01-01
    assert out["chosen_final"] is None and out["proceed"] is False and len(out["combo_table"]) == 32
