from __future__ import annotations

import math

import pytest

from ewbot.backtest.metrics import (
    Metrics,
    bootstrap_ci,
    compute_metrics,
    reliability_by_q,
    trade_stats,
)

ANN = math.sqrt(8760)


def test_max_dd_hand_computed(mk_equity):
    m = compute_metrics(mk_equity([100, 110, 99]), [])
    assert isinstance(m, Metrics)
    assert m.max_dd == pytest.approx(0.1)          # peak 110 -> trough 99
    assert m.total_return == pytest.approx(-0.01)  # 99/100 - 1
    assert m.sharpe == pytest.approx(0.0, abs=1e-9)    # returns +10% / -10%: mean 0
    assert m.sortino == pytest.approx(0.0, abs=1e-9)
    assert m.calmar == pytest.approx(m.cagr / 0.1)
    assert m.cagr == pytest.approx(-1.0, abs=1e-9)  # -1% over 2 hours, annualised
    assert m.n_trades == 0 and m.win_rate == 0.0 and m.exposure == 0.0


def test_sharpe_and_sortino_hand_computed(mk_equity):
    # returns: +0.1, +0.1, -0.1 -> mean 0.1/3, sample var = 0.0266667/2, downside ms = 0.01/3
    m = compute_metrics(mk_equity([100, 110, 121, 108.9]), [])
    mean = 0.1 / 3
    assert m.sharpe == pytest.approx(mean / math.sqrt(0.02666666666666667 / 2) * ANN, rel=1e-6)
    assert m.sortino == pytest.approx(mean / math.sqrt(0.01 / 3) * ANN, rel=1e-6)
    assert m.max_dd == pytest.approx(0.1)


def test_window_uses_prior_point_as_base(mk_equity):
    eq = mk_equity([100, 110, 121, 108.9])
    m = compute_metrics(eq, [], start_ts=2 * 3600)  # points 121, 108.9 + base 110
    assert m.total_return == pytest.approx(108.9 / 110 - 1)
    m2 = compute_metrics(eq, [], start_ts=0, end_ts=2 * 3600)  # points 100, 110
    assert m2.total_return == pytest.approx(0.1)
    assert m2.max_dd == 0.0 and m2.calmar == 0.0


def test_trade_stats_and_exposure(mk_equity, mk_trade):
    eq = mk_equity([100, 101, 102, 103])  # ts 0, 3600, 7200, 10800 -> span 3 hours
    trades = [
        mk_trade(entry_ts=3600, exit_ts=7200, pnl=5.0, r=2.0),
        mk_trade(entry_ts=7200, exit_ts=10800, pnl=-2.0, r=-1.0),
        mk_trade(entry_ts=10800, exit_ts=14400, pnl=3.0, r=1.0),
    ]
    m = compute_metrics(eq, trades)
    assert m.n_trades == 3
    assert m.win_rate == pytest.approx(2 / 3)
    assert m.avg_r == pytest.approx(2 / 3)
    assert m.exposure == pytest.approx(2 / 3)  # held 1h + 1h out of 3h (3rd trade is clipped away)
    only_first = compute_metrics(eq, trades, start_ts=0, end_ts=7200)
    assert only_first.n_trades == 1 and only_first.win_rate == 1.0


def test_degenerate_inputs_do_not_crash(mk_equity):
    assert compute_metrics([], []).total_return == 0.0
    flat = compute_metrics(mk_equity([100, 100, 100]), [])
    assert flat.sharpe == 0.0 and flat.max_dd == 0.0 and flat.calmar == 0.0
    dead = compute_metrics(mk_equity([100, 50, 0]), [])
    assert dead.cagr == -1.0 and dead.max_dd == pytest.approx(1.0)


def test_bootstrap_ci_deterministic_and_sane(mk_trade):
    trades = [mk_trade(r=r, pnl=r) for r in (2.0, -1.0, 1.0, 3.0, -1.0, 0.5, 2.5, -1.0)]
    a = bootstrap_ci(trades, "avg_r", n=500, seed=3)
    b = bootstrap_ci(trades, "avg_r", n=500, seed=3)
    c = bootstrap_ci(trades, "avg_r", n=500, seed=4)
    assert a == b
    assert a != c
    mean = sum(t.r_multiple for t in trades) / len(trades)
    assert a[0] <= mean <= a[1]
    const = bootstrap_ci([mk_trade(r=2.0) for _ in range(5)], "avg_r", n=100, seed=0)
    assert const == (2.0, 2.0)
    assert bootstrap_ci([], "avg_r") == (0.0, 0.0)
    wr = bootstrap_ci([mk_trade(pnl=1.0), mk_trade(pnl=-1.0)] * 10, "win_rate", n=200, seed=0)
    assert 0.0 <= wr[0] <= 0.5 <= wr[1] <= 1.0
    with pytest.raises(ValueError):
        bootstrap_ci(trades, "bogus")


def test_trade_stats_and_reliability(mk_trade):
    ts = trade_stats([mk_trade(pnl=4.0, r=2.0), mk_trade(pnl=-2.0, r=-1.0)])
    assert ts["n"] == 2 and ts["win_rate"] == 0.5 and ts["avg_r"] == 0.5 and ts["pnl"] == 2.0
    assert ts["ci_lo"] <= 0.5 <= ts["ci_hi"]
    assert trade_stats([])["n"] == 0

    trades = [
        mk_trade(pnl=1.0, r=1.0, snapshot={"q_top": 0.65}),
        mk_trade(pnl=-1.0, r=-1.0, snapshot={"q_top": 0.66}),
        mk_trade(pnl=2.0, r=2.0, snapshot={"q_top": 0.75}),
        mk_trade(pnl=-1.0, r=-1.0, snapshot={"q_top": 0.55}),
        mk_trade(pnl=1.0, r=1.0, snapshot={}),  # no q_top -> skipped
    ]
    rows = {(r["q_lo"], r["q_hi"]): r for r in reliability_by_q(trades)}
    assert rows[(0.6, 0.7)]["n"] == 2 and rows[(0.6, 0.7)]["hit_rate"] == 0.5
    assert rows[(0.7, 0.8)]["n"] == 1 and rows[(0.7, 0.8)]["hit_rate"] == 1.0
    assert rows[(0.5, 0.6)]["n"] == 1 and rows[(0.5, 0.6)]["hit_rate"] == 0.0
    assert rows[(0.8, 1.01)]["n"] == 0
