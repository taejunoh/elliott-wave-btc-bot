"""End-to-end integration on synthetic data: the REAL stack, no stubs.

candles (fill_gaps, wick clip) -> WaveEngine -> ChochDetector -> strategies A / C / HYBRID ->
sizing + breakers -> PaperBroker -> Trade / EquityPoint -> metrics, benchmarks, random control.
Two years of 1h bars from tests/synth.py (seed 0 stays between ~7.4k and ~24.8k), moved back by
156 whole weeks (2020-11-18 .. 2022-11-18) so that nothing reaches the 2024-01-01 holdout boundary
that run_backtest / compare refuse to cross.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from ewbot.backtest.runner import compare, run_backtest
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.config import SETUPS, BotConfig, BreakerParams, CostParams, with_overrides
from ewbot.data.candles import fill_gaps
from ewbot.types import H1
from tests.synth import random_bars

N_BARS = 2 * 8760  # two years of 1h bars
SHIFT = 156 * 7 * 24 * H1  # whole weeks keep the weekday / hour-of-week layout of tests/synth
WARMUP_H = 40 * 24  # > 200 4h bars of wave-engine warm-up before entries are allowed
MODES = ("A", "C", "HYBRID")
SETUPS_OF = {"A": set(SETUPS), "C": {"FIB"}, "HYBRID": set(SETUPS) | {"FIB"}}
EXIT_REASONS = {"STOP", "TP1", "TP2", "TIME", "LINEAGE", "FLIP", "BREAKER", "END"}


def synth_frame() -> pd.DataFrame:
    bars = random_bars(seed=0, n=N_BARS, vol=30.0, start=20_000.0)
    df = pd.DataFrame(
        {
            "ts": np.array([b.ts - SHIFT for b in bars], dtype=np.int64),
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    )
    return df.drop(index=[3_000, 3_001, 9_000]).reset_index(drop=True)  # 3 missing hours


@pytest.fixture(scope="module")
def synth():
    df = fill_gaps(synth_frame())  # what the CLI does before every run
    start = int(df["ts"].iloc[0]) + WARMUP_H * H1
    end = int(df["ts"].iloc[-1]) + H1
    cfg = BotConfig()
    runs = {m: run_backtest(with_overrides(cfg, **{"strategy.mode": m}), df, start, end) for m in MODES}
    return df, start, end, cfg, runs


@pytest.fixture(scope="module")
def cmp(synth):
    df, start, end, cfg, _ = synth
    return compare(cfg, df, start, end, random_runs=3, workers=1)


def test_synthetic_frame_is_a_valid_positive_price_path(synth):
    df = synth[0]
    assert len(df) == N_BARS and int(df["synthetic"].sum()) == 3
    assert int(df["ts"].max()) < HOLDOUT_START
    assert float(df["low"].min()) > 1_000.0


@pytest.mark.parametrize("mode", MODES)
def test_mode_runs_end_to_end_and_books_every_dollar(synth, mode):
    df, start, end, cfg, runs = synth
    res = runs[mode]
    assert [p.ts for p in res.equity] == list(range(start, end, H1))
    eq = np.array([p.equity for p in res.equity])
    assert np.isfinite(eq).all() and (eq > 0).all()
    # trades sum to the equity change (fees and funding are inside Trade.pnl)
    assert sum(t.pnl for t in res.trades) == pytest.approx(eq[-1] - cfg.initial_equity, abs=1e-6)
    assert res.events[0][0] == int(df["ts"].iloc[0]) and res.events[0][1].startswith("WICKS_CLIPPED:")
    assert len(res.signals) >= len(res.trades)
    assert all(s.ts >= start for s in res.signals)
    prev_exit = start
    for t in res.trades:
        assert start < t.entry_ts <= t.exit_ts < end
        assert t.entry_ts >= prev_exit  # one position at a time (a flip exits and enters on the same open)
        prev_exit = t.exit_ts
        assert t.setup in SETUPS_OF[mode] and t.exit_reason in EXIT_REASONS
        assert t.contracts >= 1 and math.isfinite(t.r_multiple)
        assert t.snapshot["setup"] == t.setup
        if t.setup in SETUPS:  # Elliott gates (spec 5.3) as recorded at signal time
            assert t.snapshot["counts"]
            assert t.snapshot["q_top"] >= cfg.strategy.Q_MIN and t.snapshot["s_rev"] >= cfg.strategy.S_MIN


def test_elliott_strategy_actually_trades(synth):
    runs = synth[4]
    assert len(runs["A"].trades) >= 3  # seed 0: about ten; 0 would mean a wiring/gate problem


def test_compare_on_the_real_stack(synth, cmp):
    df, start, end, cfg, runs = synth
    assert set(cmp["comparison"]) == {"A", "C", "HYBRID", "BH", "EMA"}
    for name, m in cmp["comparison"].items():
        assert all(math.isfinite(float(v)) for v in m.values()), name
        assert 0.0 <= m["max_dd"] <= 1.0
    for mode in MODES:  # compare() drives the same engine: identical trades and equity
        assert cmp["trades"][mode] == list(runs[mode].trades)
        assert cmp["equity"][mode] == [(p.ts, p.equity) for p in runs[mode].equity]
    assert cmp["random"]["runs"] == 3 and cmp["random"]["n_signals"] == len(runs["A"].trades)
    assert set(cmp["random"]["beaten_by"]) == set(MODES)
    # spot buy&hold: raw price multiple minus two fees and two slippages, no funding
    c = cfg.costs
    w = df[(df["ts"] >= start) & (df["ts"] < end)]
    multiple = float(w["close"].iloc[-1]) / float(w["open"].iloc[1])
    expect = multiple * ((1 - c.slippage) / (1 + c.slippage)) * ((1 - c.fee_rate) / (1 + c.fee_rate)) - 1
    assert cmp["comparison"]["BH"]["total_return"] == pytest.approx(expect, rel=1e-9)
    [bh] = cmp["trades"]["BH"]
    assert bh.funding == 0.0 and bh.exit_reason == "END"
    assert cmp["comparison"]["EMA"]["n_trades"] >= 1


@pytest.mark.parametrize("mode", MODES)
def test_price_mirror_flips_every_trade_through_the_whole_stack(synth, mode):
    """Spec 10 symmetry end to end: p' = 2 * p_ref - p gives the same trades at the same times with
    the opposite direction. Costs are zeroed (funding is the one asymmetric cost) and breakers are off
    (they act on % equity, which differs once the leverage cap binds at different price levels)."""
    df, start, end, _, _ = synth
    p_ref = 20_000.0  # on the 1/64 price grid of tests/synth, so mirrored prices are exact
    mirrored = df.assign(
        open=2 * p_ref - df["open"],
        high=2 * p_ref - df["low"],
        low=2 * p_ref - df["high"],
        close=2 * p_ref - df["close"],
    )
    cfg = with_overrides(
        BotConfig(
            costs=CostParams(fee_rate=0.0, slippage=0.0, funding_apr_long=0.0),
            breakers=BreakerParams(daily_loss=1.0, total_dd=1.0, max_consec_losses=10**6),
        ),
        **{"strategy.mode": mode},
    )
    a = run_backtest(cfg, df, start, end)
    b = run_backtest(cfg, mirrored, start, end)

    def key(t, sign):
        return (t.entry_ts, t.exit_ts, t.setup, sign * t.direction, t.exit_reason)

    assert [key(t, 1) for t in a.trades] == [key(t, -1) for t in b.trades]
    assert [(s.ts, s.setup, s.direction) for s in a.signals] == [(s.ts, s.setup, -s.direction) for s in b.signals]
    if mode == "A":
        assert a.trades
