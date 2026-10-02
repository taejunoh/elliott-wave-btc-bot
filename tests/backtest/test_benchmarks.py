from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from ewbot.backtest.benchmarks import (
    RandomEntryStrategy,
    buy_hold,
    buy_hold_equity,
    ema_cross_equity,
)
from ewbot.backtest.metrics import compute_metrics
from ewbot.config import CostParams, StrategyParams
from ewbot.strategy.base import time_stop_bars
from ewbot.types import Bar

T0 = 1_577_836_800  # 2020-01-01 00:00 UTC (Wednesday), divisible by 14400
H = 3600


def make_df(opens, closes=None, t0=T0) -> pd.DataFrame:
    closes = closes if closes is not None else opens
    rows = [
        {
            "ts": t0 + i * H,
            "open": float(o),
            "high": float(max(o, c)),
            "low": float(min(o, c)),
            "close": float(c),
            "volume": 1.0,
            "synthetic": False,
        }
        for i, (o, c) in enumerate(zip(opens, closes))
    ]
    return pd.DataFrame(rows)


def test_buy_hold_is_spot_two_fees_no_funding():
    # Funding is deliberately huge: a SPOT hold must ignore it.
    costs = CostParams(fee_rate=0.001, slippage=0.001, funding_apr_long=0.5)
    df = make_df(opens=[90, 100, 104, 108], closes=[100, 105, 110, 120])
    eq = buy_hold_equity(df, T0, T0 + 4 * H, costs, 10_000.0)
    assert [p.ts for p in eq] == [T0 + k * H for k in range(4)]
    # decide at bar 0's close, buy at bar 1's open: 100 * 1.001 = 100.1; spend 10,000 incl. the fee
    qty = 10_000.0 / (100.1 * 1.001)  # 99.8003 BTC
    assert eq[0].equity == 10_000.0
    assert eq[1].equity == pytest.approx(qty * 105.0)
    assert eq[2].equity == pytest.approx(qty * 110.0)
    # sell at the last close: 120 * 0.999 = 119.88, minus one fee
    assert eq[3].equity == pytest.approx(qty * 119.88 * 0.999)
    # == raw price multiple 120/100 minus two fees and two slippages
    assert eq[3].equity == pytest.approx(10_000.0 * 1.2 * (0.999 / 1.001) ** 2)
    m = compute_metrics(eq, [], T0, T0 + 4 * H)
    assert m.total_return == pytest.approx(1.2 * (0.999 / 1.001) ** 2 - 1.0)
    _, trades = buy_hold(df, T0, T0 + 4 * H, costs, 10_000.0)
    [t] = trades
    assert (t.entry_ts, t.exit_ts, t.exit_reason, t.direction, t.setup) == (T0 + H, T0 + 3 * H, "END", 1, "BH")
    assert t.entry_price == pytest.approx(100.1) and t.exit_price_avg == pytest.approx(119.88)
    assert t.funding == 0.0
    assert t.fees == pytest.approx(0.001 * qty * 100.1 + 0.001 * qty * 119.88)
    assert t.pnl == pytest.approx(eq[3].equity - 10_000.0)
    assert t.snapshot["qty_btc"] == pytest.approx(qty) and t.contracts == 9980
    no_funding = buy_hold_equity(df, T0, T0 + 4 * H, dataclasses.replace(costs, funding_apr_long=0.0), 10_000.0)
    assert [p.equity for p in no_funding] == [p.equity for p in eq]


def test_buy_hold_single_bar_window_stays_in_cash():
    df = make_df([100, 100, 100])
    eq, trades = buy_hold(df, T0 + 2 * H, T0 + 3 * H, CostParams(), 10_000.0)
    assert [(p.ts, p.equity) for p in eq] == [(T0 + 2 * H, 10_000.0)] and trades == []


def test_buy_hold_empty_window():
    df = make_df([100, 100])
    assert buy_hold_equity(df, T0 + 10 * H, T0 + 20 * H, CostParams(), 10_000.0) == []


def _ema_df():
    # one price per 4h bar: down-trend, then a jump, then a collapse (fast=2, slow=4)
    p4 = [100, 90, 80, 70, 60, 60, 100, 100, 100, 60]
    opens = [p for p in p4 for _ in range(4)]
    return make_df(opens)


def test_ema_cross_tiny_series():
    costs = CostParams(fee_rate=0.0, slippage=0.0, funding_apr_long=0.0)
    df = _ema_df()
    end = T0 + 40 * H
    eq, trades = ema_cross_equity(df, T0, end, costs, 10_000.0, fast=2, slow=4)
    assert len(eq) == 40
    # flat until 4h bar index 4 has closed (hour 20); short through hour 27; long from hour 28
    assert eq[19].equity == 10_000.0
    assert [t.direction for t in trades] == [-1, 1]
    short, long_ = trades
    assert short.entry_ts == T0 + 20 * H and short.exit_ts == T0 + 28 * H and short.exit_reason == "FLIP"
    assert long_.entry_ts == T0 + 28 * H and long_.exit_reason == "END"
    n_short = int(10_000 // (60 * 0.01))
    assert short.contracts == n_short
    assert short.pnl == pytest.approx(-n_short * 0.01 * (100 - 60))
    cash = 10_000 + short.pnl
    n_long = int(cash // (100 * 0.01))
    assert long_.contracts == n_long
    assert long_.pnl == pytest.approx(n_long * 0.01 * (60 - 100))
    assert eq[-1].equity == pytest.approx(cash + long_.pnl)
    assert short.setup == "EMA"


def test_random_entry_deterministic_and_geometry(mk_trade):
    sp = StrategyParams()
    refs = [mk_trade(entry_ts=T0, exit_ts=T0 + 8 * H, entry=100.0, stop0=95.0, tp1=105.0, tp2=110.0)]
    span = (T0, T0 + 48 * H)
    a = RandomEntryStrategy(sp, refs, 5, span, seed=7)
    b = RandomEntryStrategy(sp, refs, 5, span, seed=7)
    c = RandomEntryStrategy(sp, refs, 5, span, seed=8)
    assert len(a.planned_times) == 5
    assert a.planned_times == b.planned_times
    assert a.planned_times != c.planned_times
    assert all(T0 <= t < T0 + 48 * H and t % H == 0 for t in a.planned_times)
    assert a.name == "RANDOM"

    dirs = set()
    for ts in a.planned_times:
        bar = Bar(ts, 100.0, 101.0, 99.0, 100.0, 1.0)
        sig = a.on_bar(bar, None, None)
        assert sig is not None and sig.setup == "RANDOM" and sig.ts == ts
        d = sig.direction
        dirs.add(d)
        assert sig.entry_ref == 100.0
        assert sig.stop == pytest.approx(100.0 - d * 5.0)
        assert sig.tp1 == pytest.approx(100.0 + d * 5.0)   # 1R
        assert sig.tp2 == pytest.approx(100.0 + d * 10.0)  # 2R
        assert sig.time_stop_4h == time_stop_bars(2, sp)   # 8h hold = 2 four-hour bars
        assert sig.instance is None and sig.lineage is None
    assert dirs <= {1, -1}
    assert a.on_bar(Bar(T0 + 100 * H, 100, 101, 99, 100, 1.0), None, None) is None


def test_random_entry_skips_blackout_and_handles_no_reference(mk_trade):
    sp = StrategyParams()
    # Fri 2020-01-03: the NY Friday 16:00-18:00 blackout is 21:00-23:00 UTC (winter, UTC-5).
    # A signal at 20:00 UTC would fill in the 21:00 bar, so 20:00, 21:00 and 22:00 are all excluded.
    fri = T0 + 2 * 86400
    r = RandomEntryStrategy(sp, [mk_trade()], 24, (fri, fri + 86400), seed=1)
    assert len(r.planned_times) == 21
    assert not set(r.planned_times) & {fri + 20 * H, fri + 21 * H, fri + 22 * H}
    empty = RandomEntryStrategy(sp, [], 10, (T0, T0 + 48 * H), seed=1)
    assert empty.planned_times == ()
