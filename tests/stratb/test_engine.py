from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from ewbot.config import BreakerParams, CostParams
from ewbot.execution.costs import fee, funding_per_hour, slip
from ewbot.risk.sizing import size_contracts
from ewbot.stratb.config import RISK_B, BConfig, StratBParams
from ewbot.stratb.engine import run_engine, tp1_contracts
from ewbot.stratb.signals import SignalB, build_frame, generate_signals
from tests.stratb.helpers import T0, random_df

M15, H1 = 900, 3600
C = CostParams()


def bars(rows, t0=T0):
    """rows of (open, high, low, close) -> a 15m frame."""
    return build_frame(pd.DataFrame(
        [(t0 + k * M15, o, h, lo, c, 1.0) for k, (o, h, lo, c) in enumerate(rows)],
        columns=["ts", "open", "high", "low", "close", "volume"]))


def flat(n, px=20_000.0):
    return [(px, px + 5, px - 5, px)] * n


def sig(i, f, stop, tp1, tp2, d=1):
    return SignalB(i=i, ts=int(f.ts[i]), direction=d, entry_ref=float(f.close[i]), stop=stop, tp1=tp1, tp2=tp2,
                   reason="test")


def test_entry_fills_at_the_next_open_with_slippage_fee_and_half_percent_sizing():
    f = bars(flat(10))
    res = run_engine(f, [sig(2, f, 19_900.0, 20_100.0, 20_300.0)], BConfig())
    n = size_contracts(10_000.0, 20_000.0, 19_900.0, 1, RISK_B, 1.0, C)
    assert n == 41 and res.orders[0].contracts == 41 and res.orders[0].tp1_contracts == 20
    entry = res.fills[0]
    px = slip(20_000.0, 1, C)
    assert (entry.kind, entry.ts, entry.price, entry.contracts) == ("ENTRY", int(f.ts[3]), px, 41)
    assert entry.fee == pytest.approx(fee(px, 41, C))
    assert res.signals[0].i == 2 and len(res.trades) == 1 and res.trades[0].exit_reason == "END"


def test_leverage_is_capped_at_3x():
    f = bars(flat(10))
    res = run_engine(f, [sig(2, f, 19_990.0, 20_100.0, 20_300.0)], BConfig())
    assert res.orders[0].contracts == 150  # 3 x 10,000 / 20,000 = 1.5 BTC = 150 contracts of 0.01


def test_the_stop_beats_a_target_touched_in_the_same_bar():
    rows = flat(4) + [(20_000.0, 20_500.0, 19_800.0, 20_000.0)] + flat(3)
    f = bars(rows)
    res = run_engine(f, [sig(2, f, 19_900.0, 20_100.0, 20_300.0)], BConfig())
    (t,) = res.trades
    assert [x.kind for x in res.fills] == ["ENTRY", "STOP"] and res.fills[1].price == slip(19_900.0, -1, C)
    assert t.exit_reason == "STOP" and t.pnl < 0 and t.r_multiple < -1.0


def test_tp1_takes_half_moves_the_rest_to_breakeven_and_tp2_closes_it():
    rows = flat(4) + [(20_000.0, 20_150.0, 19_950.0, 20_120.0), (20_120.0, 20_350.0, 20_100.0, 20_300.0)] + flat(2, 20_300.0)
    f = bars(rows)
    res = run_engine(f, [sig(2, f, 19_900.0, 20_100.0, 20_300.0)], BConfig())
    (t,) = res.trades
    assert [(x.kind, x.contracts, x.price) for x in res.fills[1:]] == [("TP1", 20, 20_100.0), ("TP2", 21, 20_300.0)]
    (mv,) = res.stops
    entry_px = res.fills[0].price
    assert (mv.ts, mv.old, mv.new, mv.kind) == (int(f.ts[4]), 19_900.0, entry_px, "BREAKEVEN")
    gross = (20_100.0 - entry_px) * 20 * 0.01 + (20_300.0 - entry_px) * 21 * 0.01
    assert t.exit_reason == "TP2" and t.pnl == pytest.approx(gross - t.fees - t.funding)
    assert t.fees == pytest.approx(sum(x.fee for x in res.fills))


def test_the_breakeven_rest_stops_at_the_entry_and_original_keeps_the_stop():
    rows = flat(4) + [(20_000.0, 20_150.0, 19_950.0, 20_120.0), (20_120.0, 20_120.0, 19_850.0, 19_860.0)] + flat(2, 19_860.0)
    f = bars(rows)
    s = [sig(2, f, 19_900.0, 20_100.0, 20_300.0)]
    be = run_engine(f, s, BConfig())
    assert [x.kind for x in be.fills] == ["ENTRY", "TP1", "STOP"]
    assert be.fills[2].price == slip(be.fills[0].price, -1, C)
    orig = run_engine(f, s, BConfig(params=StratBParams(remainder="original")))
    assert orig.stops == () and orig.fills[2].price == slip(19_900.0, -1, C)


def test_a_gap_through_the_stop_fills_at_the_open():
    rows = flat(4) + [(19_800.0, 19_850.0, 19_700.0, 19_750.0)] + flat(2, 19_750.0)
    f = bars(rows)
    res = run_engine(f, [sig(2, f, 19_900.0, 20_100.0, 20_300.0)], BConfig())
    assert res.fills[1].kind == "STOP" and res.fills[1].price == slip(19_800.0, -1, C)


def test_the_time_stop_closes_at_the_open_48_hours_after_the_entry_and_funding_is_quarter_hourly():
    f = bars(flat(250))
    res = run_engine(f, [sig(2, f, 19_900.0, 20_100.0, 20_300.0)], BConfig())
    (t,) = res.trades
    assert t.exit_reason == "TIME" and t.exit_ts == t.entry_ts + 48 * H1 and res.fills[-1].kind == "EXIT"
    assert res.fills[-1].price == slip(20_000.0, -1, C)
    held = (t.exit_ts - t.entry_ts) // M15  # bars from the entry bar to the one before the exit
    assert t.funding == pytest.approx(held * funding_per_hour(20_000.0, 41, 1, C) / 4)


def test_a_single_contract_closes_at_tp1():
    f = bars(flat(4, 100.0) + [(100.0, 103.0, 99.5, 102.0)] + flat(2, 102.0))
    res = run_engine(f, [sig(2, f, 90.0, 101.0, 105.0)], BConfig(initial_equity=30.0))
    assert res.orders[0].contracts == 1 and res.orders[0].tp1_contracts == 1
    assert res.trades[0].exit_reason == "TP1" and tp1_contracts(1, 0.5) == 1 and tp1_contracts(7, 0.5) == 3


def test_end_of_data_entries_from_start_and_hourly_equity():
    f = bars(flat(16))
    s = [sig(2, f, 19_900.0, 20_100.0, 20_300.0), sig(15, f, 19_900.0, 20_100.0, 20_300.0)]
    res = run_engine(f, s, BConfig(), start_ts=T0 + H1)
    assert res.orders == ()  # bar 2 is before start_ts, bar 15 is the last bar
    assert [p.ts for p in res.equity] == [T0 + H1, T0 + 2 * H1, T0 + 3 * H1]
    res = run_engine(f, s[:1], BConfig())
    assert res.end_fill is not None and res.end_fill.kind == "END" and res.trades[-1].exit_reason == "END"
    assert [x.kind for x in res.fills] == ["ENTRY"] and res.equity[-1].equity == pytest.approx(res.trades[-1].pnl + 10_000.0)


def test_entries_are_dropped_on_a_gap_bar_or_an_open_beyond_the_stop():
    df = pd.DataFrame([(T0 + k * M15, 20_000.0, 20_005.0, 19_995.0, 20_000.0, 1.0) for k in range(10) if k != 3],
                      columns=["ts", "open", "high", "low", "close", "volume"])
    f = build_frame(df)
    res = run_engine(f, [sig(2, f, 19_900.0, 20_100.0, 20_300.0)], BConfig())
    assert res.fills == () and (int(f.ts[3]), "ENTRY_SKIPPED:gap") in res.events
    f = bars(flat(3) + [(19_890.0, 19_900.0, 19_880.0, 19_890.0)] + flat(3))
    res = run_engine(f, [sig(2, f, 19_900.0, 20_100.0, 20_300.0)], BConfig())
    assert res.fills == () and (int(f.ts[3]), "ENTRY_SKIPPED:open_beyond_stop") in res.events


def test_the_total_drawdown_breaker_flattens_and_blocks_entries():
    rows = flat(4) + [(20_000.0, 20_005.0, 19_950.0, 19_960.0)] * 4 + flat(8, 19_960.0)
    f = bars(rows)
    cfg = BConfig(breakers=dataclasses.replace(BreakerParams(), total_dd=0.0001))
    res = run_engine(f, [sig(2, f, 19_000.0, 21_000.0, 22_000.0), sig(10, f, 19_000.0, 21_000.0, 22_000.0)], cfg)
    assert ("TOTAL_HALT" in {e for _, e in res.events}) and res.trades[0].exit_reason == "BREAKER"
    assert (int(f.ts[10]), "SIGNAL_REJECTED:breaker") in res.events and len(res.trades) == 1


@pytest.mark.parametrize("seed", [3, 4])
def test_decisions_up_to_t_never_change_when_later_bars_are_cut(seed):
    df = random_df(6000, seed)
    p = StratBParams(zz_k=2.0, zone=(0.5, 0.786))
    full_f = build_frame(df, p)
    full = run_engine(full_f, generate_signals(full_f, p), BConfig(params=p))
    assert full.fills
    for t in sorted({x.ts // M15 * M15 for x in full.fills} | {int(df["ts"].iloc[2999])}):
        k = int((t - int(df["ts"].iloc[0])) // M15)
        f = build_frame(df.iloc[: k + 1], p)
        cut = run_engine(f, generate_signals(f, p), BConfig(params=p))
        assert [x for x in cut.fills] == [x for x in full.fills if x.ts <= t]
        assert list(cut.stops) == [x for x in full.stops if x.ts <= t]
        assert [o for o in cut.orders] == [o for o in full.orders if o.ts < t]
