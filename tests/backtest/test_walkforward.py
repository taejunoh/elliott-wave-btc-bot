from __future__ import annotations

import pandas as pd
import pytest

from ewbot.backtest import walkforward
from ewbot.backtest.walkforward import (
    GRID,
    MIN_TRAIN_TRADES,
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


def test_min_train_trades_default_is_12():
    calmar, _ = _score_grid()
    assert MIN_TRAIN_TRADES == 12
    assert pick_plateau(calmar, {c: 12 for c in calmar}) == (3.0, 0.6, 0.0)  # default floor
    with pytest.raises(ValueError):
        pick_plateau(calmar, {c: 11 for c in calmar})


def test_run_grid_disables_breakers_without_touching_the_input(monkeypatch):
    seen = []

    def fake_run_backtest(cfg, df1h, start_ts, end_ts):
        seen.append(cfg)
        return EngineResult(trades=(), equity=(), signals=(), events=())

    monkeypatch.setattr(walkforward, "run_backtest", fake_run_backtest)
    base = BotConfig()
    out = walkforward.run_grid(base, pd.DataFrame(), 0, H, workers=1)
    assert list(out) == grid_combos() and len(seen) == 60
    assert all(c.breakers.enabled is False for c in seen)
    assert {(c.wave.K4, c.strategy.Q_MIN, c.strategy.S_MIN) for c in seen} == set(grid_combos())
    assert base.breakers.enabled is True  # the caller's config is untouched


def _result(value_fn, n_points, trade_hours, mk_trade):
    eq = tuple(EquityPoint(ts=i * H, equity=float(value_fn(i))) for i in range(n_points))
    trades = tuple(mk_trade(entry_ts=h * H, exit_ts=(h + 1) * H, pnl=1.0) for h in trade_hours)
    return EngineResult(trades=trades, equity=eq, signals=(), events=())


@pytest.fixture
def two_results(mk_trade):
    a = _result(lambda i: 100 + i - (5 if 40 <= i < 45 else 0), 200, (10, 20, 30, 120), mk_trade)
    b = _result(lambda i: 100 + 0.5 * i - (10 if 40 <= i < 45 else 0), 200, (10, 20, 30), mk_trade)
    return {(3.0, 0.6, 0.25): a, (2.0, 0.5, 0.0): b}


def test_select_plateau_uses_train_window_and_min_trades(two_results):
    assert select_plateau(two_results, 0, 100 * H, min_trades=3) == (3.0, 0.6, 0.25)  # higher Calmar
    with pytest.raises(ValueError):
        select_plateau(two_results, 0, 100 * H, min_trades=4)   # only 3 trades in the window
    with pytest.raises(ValueError):
        select_plateau(two_results, 150 * H, 190 * H, min_trades=1)  # no trades in that window


def test_evaluate_folds_stitches_oos_returns(two_results):
    fold_list = [
        (0, 100 * H, 100 * H, 150 * H),         # fold 0: A wins on train; OOS = hours 100..149
        (100 * H, 150 * H, 150 * H, 200 * H),   # fold 1: only 1 trade in train -> nothing eligible -> flat
    ]
    out = evaluate_folds(two_results, fold_list, equity0=1000.0, min_trades=3)
    f0, f1 = out["folds"]
    assert f0["chosen"] == (3.0, 0.6, 0.25) and f0["oos"] is not None
    assert f1["chosen"] is None and f1["oos"] is None
    st = out["stitched_equity"]
    assert len(st) == 1 + 100                                  # base point + 50 + 50 flat hours
    assert st[0] == (100 * H - H, 1000.0)
    # A: equity(99) = 199, equity(149) = 249 -> stitched value after fold 0 = 1000 * 249/199
    assert st[50][1] == pytest.approx(1000.0 * 249.0 / 199.0)
    assert st[-1][1] == pytest.approx(st[50][1])               # fold 1 is flat
    assert out["stitched_metrics"]["n_trades"] == 1            # A's trade at hour 120 only
    assert out["chosen_final"] == (3.0, 0.6, 0.25)
    assert [o.entry_ts for o in out["oos_trades"]] == [120 * H]
