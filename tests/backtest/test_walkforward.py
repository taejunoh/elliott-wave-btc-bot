from __future__ import annotations

import pandas as pd
import pytest

from ewbot.backtest import walkforward
from ewbot.backtest.runner import utc_ts
from ewbot.backtest.walkforward import (
    GRID,
    MIN_TRAIN_TRADES,
    WF_END,
    config_for,
    evaluate_folds,
    folds,
    grid_combos,
    pick_plateau,
    select_plateau,
)
from ewbot.config import BotConfig
from ewbot.engine.loop import EngineResult, EquityPoint

H = 3600


def _idx(combo):
    return tuple(axis.index(v) for axis, v in zip(GRID.values(), combo))


def _neighbours(combo):
    """In-grid neighbours of `combo` (Chebyshev index distance 1, itself excluded)."""
    ic = _idx(combo)
    return [o for o in grid_combos() if o != combo and max(abs(a - b) for a, b in zip(ic, _idx(o))) <= 1]


def test_grid_has_60_combos_in_key_order():
    combos = grid_combos()
    assert len(combos) == 60 and len(set(combos)) == 60
    assert combos[0] == (2.0, 0.5, 0.0) and combos[-1] == (4.0, 0.7, 0.5)
    cfg = config_for(BotConfig(), (2.5, 0.55, 0.5))
    assert (cfg.wave.K4, cfg.strategy.Q_MIN, cfg.strategy.S_MIN) == (2.5, 0.55, 0.5)


def test_folds_exact_boundaries():
    fl = folds()
    assert len(fl) == 10
    # (train_start, train_end, test_start, test_end)
    assert fl[0] == (1_483_228_800, 1_546_300_800, 1_546_300_800, 1_561_939_200)  # 2017-01-01 | 2019-01-01 .. 2019-07-01
    assert fl[1] == (1_498_867_200, 1_561_939_200, 1_561_939_200, 1_577_836_800)  # 2017-07-01 | 2019-07-01 .. 2020-01-01
    assert fl[-1] == (1_625_097_600, 1_688_169_600, 1_688_169_600, 1_704_067_200)  # 2021-07-01 | 2023-07-01 .. 2024-01-01
    assert fl[-1][3] == utc_ts(WF_END)  # the last test window ends where the holdout starts
    for a, b in zip(fl, fl[1:]):
        assert b[2] == a[3]            # test windows are contiguous
        assert b[0] > a[0]
    assert all(f[1] == f[2] for f in fl)  # training ends where the test starts (no gap, no overlap)
    assert folds(train_months=12, test_months=12, first_test="2020-01-01", last_test_end="2022-01-01") == [
        (1_546_300_800, 1_577_836_800, 1_577_836_800, 1_609_459_200),
        (1_577_836_800, 1_609_459_200, 1_609_459_200, 1_640_995_200),
    ]


def _score_grid(plateau=2.0, base=1.0):
    k, q, s = GRID["wave.K4"], GRID["strategy.Q_MIN"], GRID["strategy.S_MIN"]
    calmar = {}
    for i, kv in enumerate(k):
        for j, qv in enumerate(q):
            for l, sv in enumerate(s):
                calmar[(kv, qv, sv)] = plateau if (i in (1, 2, 3) and j in (1, 2, 3)) else base
    spike = (k[0], q[4], s[2])  # isolated corner cell
    calmar[spike] = 5.0
    return calmar, spike


def test_pick_plateau_prefers_plateau_over_isolated_spike():
    calmar, spike = _score_grid()
    ntr = {c: 100 for c in calmar}
    assert max(calmar, key=calmar.get) == spike           # naive argmax would pick the spike
    # spike neighbourhood mean = 14/8 = 1.75 < 2.0 (interior of the plateau)
    assert pick_plateau(calmar, ntr) == (3.0, 0.6, 0.0)   # smallest grid index among the tied plateau centres


def test_pick_plateau_excludes_combos_below_min_trades_everywhere():
    calmar, _ = _score_grid()
    hot = (4.0, 0.65, 0.25)  # plateau-edge cell that would inflate its neighbours' mean to ~5.6
    calmar[hot] = 100.0
    ntr = {c: 100 for c in calmar}
    ntr[hot] = 5
    assert pick_plateau(calmar, ntr, min_trades=30) == (3.0, 0.6, 0.0)
    ntr[hot] = 100  # once it counts, it dominates its neighbourhood and the pick moves
    assert pick_plateau(calmar, ntr, min_trades=30) != (3.0, 0.6, 0.0)


def test_pick_plateau_raises_when_nothing_qualifies():
    calmar, _ = _score_grid()
    with pytest.raises(ValueError):
        pick_plateau(calmar, {c: 1 for c in calmar}, min_trades=30)


def test_pick_plateau_ignores_an_eligible_spike_whose_neighbours_are_ineligible():
    calmar, spike = _score_grid()
    ntr = {c: 100 for c in calmar}
    for o in _neighbours(spike):
        ntr[o] = 0  # every neighbour of the spike fails the trade floor
    # without the neighbour rule the spike's "neighbourhood mean" is its own Calmar 5.0 and it wins
    assert pick_plateau(calmar, ntr) == (3.0, 0.6, 0.0)


def test_pick_plateau_needs_half_of_the_in_grid_neighbourhood_eligible():
    calmar, spike = _score_grid()
    nb = sorted(_neighbours(spike))
    assert len(nb) == 7  # corner of the 4 x 5 x 3 grid
    ntr = {c: 0 for c in calmar}
    ntr[spike] = 100
    for o in nb[:4]:
        ntr[o] = 100  # 4/7 eligible: the spike qualifies; its neighbours (4/11, 4/17, 4/26) do not
    assert pick_plateau(calmar, ntr) == spike
    ntr[nb[3]] = 0  # 3/7 < half: nothing is selectable
    with pytest.raises(ValueError, match="neighbour"):
        pick_plateau(calmar, ntr)


def test_min_train_trades_default_is_12():
    calmar, _ = _score_grid()
    assert MIN_TRAIN_TRADES == 12
    assert pick_plateau(calmar, {c: 12 for c in calmar}) == (3.0, 0.6, 0.0)  # default floor
    with pytest.raises(ValueError):
        pick_plateau(calmar, {c: 11 for c in calmar})


def test_run_grid_disables_breakers_and_cuts_at_end_without_touching_the_input(monkeypatch):
    seen = []

    def fake_run_backtest(cfg, df1h, start_ts, end_ts):
        seen.append((cfg, int(df1h["ts"].max()), end_ts))
        return EngineResult(trades=(), equity=(), signals=(), events=())

    monkeypatch.setattr(walkforward, "run_backtest", fake_run_backtest)
    base = BotConfig()
    df = pd.DataFrame({"ts": [0, H, 2 * H, 3 * H], "close": 1.0})
    out = walkforward.run_grid(base, df, 0, 2 * H, workers=1)
    assert list(out) == grid_combos() and len(seen) == 60
    assert all(c.breakers.enabled is False for c, _, _ in seen)
    assert {(c.wave.K4, c.strategy.Q_MIN, c.strategy.S_MIN) for c, _, _ in seen} == set(grid_combos())
    assert all(last == H and end == 2 * H for _, last, end in seen)  # nothing at/after end_ts reaches a run
    assert base.breakers.enabled is True and len(df) == 4  # the caller's config and frame are untouched


def _result(value_fn, n_points, trade_hours, mk_trade):
    eq = tuple(EquityPoint(ts=i * H, equity=float(value_fn(i))) for i in range(n_points))
    trades = tuple(mk_trade(entry_ts=h * H, exit_ts=(h + 1) * H, pnl=1.0) for h in trade_hours)
    return EngineResult(trades=trades, equity=eq, signals=(), events=())


A_CENTRE = (3.0, 0.6, 0.0)  # smallest grid index whose whole neighbourhood runs result A


@pytest.fixture
def grid_results(mk_trade):
    """All 60 combos: result A on K4 in {2.5, 3.0, 4.0} x Q_MIN in {0.55, 0.6, 0.65} (every S_MIN),
    result B elsewhere. A has the higher Calmar on every window used below."""
    a = _result(lambda i: 100 + i - (5 if 40 <= i < 45 else 0), 200, (10, 20, 30, 120), mk_trade)
    b = _result(lambda i: 100 + 0.5 * i - (10 if 40 <= i < 45 else 0), 200, (10, 20, 30), mk_trade)

    def in_block(c):
        i, j, _ = _idx(c)
        return i >= 1 and 1 <= j <= 3

    return {c: (a if in_block(c) else b) for c in grid_combos()}


def test_select_plateau_uses_train_window_and_min_trades(grid_results):
    assert select_plateau(grid_results, 0, 100 * H, min_trades=3) == A_CENTRE
    with pytest.raises(ValueError):
        select_plateau(grid_results, 0, 100 * H, min_trades=4)   # only 3 trades in the window
    with pytest.raises(ValueError):
        select_plateau(grid_results, 150 * H, 190 * H, min_trades=1)  # no trades in that window


def test_evaluate_folds_stitches_oos_returns(grid_results):
    fold_list = [
        (0, 100 * H, 100 * H, 150 * H),         # fold 0: A block wins on train; OOS = hours 100..149
        (100 * H, 150 * H, 150 * H, 200 * H),   # fold 1: only 1 trade in train -> nothing eligible -> flat
    ]
    out = evaluate_folds(grid_results, fold_list, equity0=1000.0, min_trades=3)
    f0, f1 = out["folds"]
    assert f0["chosen"] == A_CENTRE and f0["oos"] is not None
    assert f1["chosen"] is None and f1["oos"] is None
    st = out["stitched_equity"]
    assert len(st) == 1 + 100                                  # base point + 50 + 50 flat hours
    assert st[0] == (100 * H - H, 1000.0)
    # A: equity(99) = 199, equity(149) = 249 -> stitched value after fold 0 = 1000 * 249/199
    assert st[50][1] == pytest.approx(1000.0 * 249.0 / 199.0)
    assert st[-1][1] == pytest.approx(st[50][1])               # fold 1 is flat
    assert out["stitched_metrics"]["n_trades"] == 1            # A's trade at hour 120 only
    assert [o.entry_ts for o in out["oos_trades"]] == [120 * H]
    # the last fold chose nothing: chosen_final is fold 0's pick and is flagged as a fallback
    assert out["chosen_final"] == A_CENTRE
    assert out["chosen_final_from_fold"] == 0 and out["chosen_final_fallback"] is True


def test_chosen_final_from_the_last_fold_is_not_a_fallback(grid_results):
    fold_list = [(0, 100 * H, 100 * H, 150 * H), (0, 150 * H, 150 * H, 200 * H)]  # both trainable
    out = evaluate_folds(grid_results, fold_list, equity0=1000.0, min_trades=3)
    assert out["folds"][1]["chosen"] is not None
    assert out["chosen_final"] == out["folds"][1]["chosen"]
    assert out["chosen_final_from_fold"] == 1 and out["chosen_final_fallback"] is False
    none = evaluate_folds(grid_results, [(100 * H, 150 * H, 150 * H, 200 * H)], equity0=1000.0, min_trades=3)
    assert none["chosen_final"] is None and none["chosen_final_from_fold"] is None
    assert none["chosen_final_fallback"] is False


def test_walk_forward_never_sees_bars_at_or_after_wf_end(monkeypatch, mk_trade):
    """A frame with poisoned bars after WF_END (NaN / x10) must give exactly the walk-forward of the
    clean truncated frame, even with a 'leaky' engine that reads every bar it is handed."""
    wf_end = utc_ts(WF_END)
    ts = list(range(utc_ts("2016-12-01"), utc_ts("2024-07-01"), 24 * H))  # daily bars are enough here
    px = [100.0 + (i % 50) for i in range(len(ts))]
    full = pd.DataFrame({"ts": ts, "open": px, "high": px, "low": px, "close": px, "volume": 1.0})
    post = full["ts"] >= wf_end
    full.loc[post, ["open", "high", "low", "close"]] *= 10.0
    full.loc[post & (full.index % 2 == 0), "close"] = float("nan")
    clean = full[~post].reset_index(drop=True)
    assert post.sum() > 100 and clean["close"].notna().all()

    seen_bt, seen_grid = [], []

    def leaky_run_backtest(cfg, df1h, start_ts, end_ts):
        seen_bt.append((int(df1h["ts"].max()), end_ts))
        leak = float(df1h["close"].sum())  # any bar at/after WF_END changes (or NaNs) every equity value
        slope = cfg.wave.K4 + cfg.strategy.Q_MIN + cfg.strategy.S_MIN
        eq = tuple(
            EquityPoint(ts=int(t), equity=10_000.0 + slope * i + (i % 7) * leak * 1e-5)
            for i, t in enumerate(df1h["ts"])
        )
        trades = tuple(mk_trade(entry_ts=int(t), exit_ts=int(t) + H, pnl=1.0) for t in df1h["ts"].iloc[::20])
        return EngineResult(trades=trades, equity=eq, signals=(), events=())

    real_run_grid = walkforward.run_grid

    def spy_run_grid(cfg, df1h, start_ts, end_ts, workers):
        seen_grid.append((start_ts, end_ts, int(df1h["ts"].max())))
        return real_run_grid(cfg, df1h, start_ts, end_ts, workers)

    monkeypatch.setattr(walkforward, "run_backtest", leaky_run_backtest)
    monkeypatch.setattr(walkforward, "run_grid", spy_run_grid)
    out_full = walkforward.walk_forward(BotConfig(), full, workers=1)
    out_clean = walkforward.walk_forward(BotConfig(), clean, workers=1)

    assert any(f["chosen"] is not None for f in out_clean["folds"])  # the comparison is not vacuous
    assert out_clean["stitched_metrics"]["max_dd"] > 0
    assert out_full == out_clean
    assert [g[:2] for g in seen_grid] == [(utc_ts("2017-01-01"), wf_end)] * 2  # run_grid gets end_ts == WF_END
    assert all(last < wf_end for _, _, last in seen_grid)
    assert len(seen_bt) == 120 and all(last < wf_end and end == wf_end for last, end in seen_bt)
