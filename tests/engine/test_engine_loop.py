from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from ewbot.config import BotConfig, BreakerParams, CostParams
from ewbot.engine.loop import EngineResult, TradingEngine
from ewbot.strategy.base import Signal
from ewbot.types import H1, Bar
from ewbot.waves.engine import WaveState
from ewbot.waves.enumerate import Count
from ewbot.waves.patterns import N_POINTS
from ewbot.waves.zigzag import Pivot

T0 = 1_704_067_200    # Mon 2024-01-01 00:00 UTC (4h aligned)
FRI = 1_704_412_800   # Fri 2024-01-05 00:00 UTC; bar 20 = 15:00 EST, 21 = 16:00, 22 = 17:00 (closed)
FLAT = (50_000.0, 50_100.0, 49_900.0, 50_000.0)
ZERO = CostParams(fee_rate=0.0, slippage=0.0, funding_apr_long=0.0)
CFG = BotConfig(costs=ZERO)  # conservative 1% risk: entry_ref 50_000 / stop 49_000 -> 10 contracts


def ts(i: int, t0: int = T0) -> int:
    return t0 + i * H1


def frame(n: int, rows: dict[int, tuple[float, float, float, float]] | None = None, t0: int = T0) -> pd.DataFrame:
    rows = rows or {}
    data = [rows.get(i, FLAT) for i in range(n)]
    return pd.DataFrame(
        {
            "ts": pd.Series([t0 + i * H1 for i in range(n)], dtype="int64"),
            "open": [r[0] for r in data],
            "high": [r[1] for r in data],
            "low": [r[2] for r in data],
            "close": [r[3] for r in data],
            "volume": [1.0] * n,
        }
    )


def sig(at: int, d: int = 1, **kw: Any) -> Signal:
    if d == 1:
        base: dict[str, Any] = dict(entry_ref=50_000.0, stop=49_000.0, tp1=51_000.0, tp2=52_000.0)
    else:
        base = dict(entry_ref=50_000.0, stop=51_000.0, tp1=49_000.0, tp2=48_000.0)
    base.update(dict(ts=at, setup="W3", direction=d, risk_mult=1.0, time_stop_4h=None,
                     instance=None, lineage=None, reason="fake"))
    base.update(kw)
    return Signal(**base)


def count(i0: int, phase: str = "W2", q: float = 0.72) -> Count:
    """Bull IMP count whose lineage is ("IMP", 1, i0); alternating low/high points."""
    pts = tuple(
        Pivot(i=i0 + k, sub_ts=T0 + (k + 1) * 3_600, price=48_000.0 if k % 2 == 0 else 51_000.0,
              kind=-1 if k % 2 == 0 else 1, confirmed_at=i0 + k + 1)
        for k in range(N_POINTS[phase])
    )
    return Count(pattern="IMP", d=1, phase=phase, points=pts, q=q)


class StubWave:
    """Scripted WaveEngine: default WaveState fields, overridden per 1h ts."""

    def __init__(self, default: dict[str, Any] | None = None, at: dict[int, dict[str, Any]] | None = None) -> None:
        self.default = default or {}
        self.at = at or {}
        self.calls: list[tuple[int, Bar | None, tuple[Bar, ...] | None]] = []

    def on_1h_close(self, bar1h: Bar, bar4h: Bar | None, sub_bars: Any) -> WaveState:
        self.calls.append((bar1h.ts, bar4h, tuple(sub_bars) if sub_bars is not None else None))
        kw: dict[str, Any] = dict(
            ts=bar1h.ts, ready=True, counts=(), probs=(), top=None, q_top=0.0, s_rev=0.0,
            lineages=frozenset(), E_live=bar1h.close, E_live_ts=bar1h.ts, leg_dir=1, leg_start=None,
            pivots4h=(), new_pivot_4h=None, atr4h=400.0, closed_4h=bar4h is not None, i4h=0,
        )
        kw.update(self.default)
        kw.update(self.at.get(bar1h.ts, {}))
        return WaveState(**kw)


class NullChoch:
    def update(self, bar1h: Bar) -> None:
        return None


class FakeStrategy:
    name = "FAKE"

    def __init__(self, plan: dict[int, Signal]) -> None:
        self.plan = plan
        self.calls: list[int] = []

    def on_bar(self, bar: Bar, ws: WaveState, choch: Any) -> Signal | None:
        self.calls.append(bar.ts)
        return self.plan.get(bar.ts)


def run(
    df: pd.DataFrame,
    plan: dict[int, Signal],
    wave: StubWave | None = None,
    cfg: BotConfig = CFG,
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> tuple[EngineResult, FakeStrategy, StubWave]:
    strat = FakeStrategy(plan)
    w = wave or StubWave()
    eng = TradingEngine(cfg, strat, wave_engine=w, choch=NullChoch())  # type: ignore[arg-type]
    return eng.run(df, start_ts=start_ts, end_ts=end_ts), strat, w


def test_entry_at_next_open_end_close_snapshot_and_equity() -> None:
    c1, c2 = count(7), count(9, q=0.6)
    decision = dict(counts=(c1, c2), probs=(0.7, 0.3), top=c1, q_top=0.72, s_rev=0.4, E_live=49_950.0)
    wave = StubWave(at={ts(2): decision})
    s = sig(ts(2))
    df = frame(12, {3: (50_050.0, 50_100.0, 49_900.0, 50_000.0), 11: (50_000.0, 50_250.0, 49_900.0, 50_200.0)})
    res, _, _ = run(df, {ts(2): s}, wave)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert (t.entry_ts, t.exit_ts, t.direction, t.contracts, t.exit_reason) == (ts(3), ts(11), 1, 10, "END")
    assert t.entry_price == 50_050.0 and t.exit_price_avg == 50_200.0
    assert (t.stop0, t.tp1, t.tp2, t.setup) == (49_000.0, 51_000.0, 52_000.0, "W3")
    assert t.pnl == pytest.approx(15.0)                     # (50_200 - 50_050) * 10 * 0.01
    assert t.r_multiple == pytest.approx(15.0 / 105.0)      # risk = (50_050 - 49_000) * 0.1
    assert t.snapshot["counts"][0] == {
        "pattern": "IMP", "d": 1, "phase": "W2",
        "points": [(T0 + 3_600, 48_000.0), (T0 + 7_200, 51_000.0)], "q": 0.72, "p": 0.7,
    }
    assert len(t.snapshot["counts"]) == 2
    snap = t.snapshot
    assert (snap["q_top"], snap["s_rev"], snap["E_live"], snap["reason"]) == (0.72, 0.4, 49_950.0, "fake")
    assert res.signals == (s,)
    assert [p.ts for p in res.equity] == [ts(i) for i in range(12)]
    assert res.equity[2].equity == 10_000.0
    assert res.equity[3].equity == pytest.approx(9_995.0)   # marked at close 50_000
    assert res.equity[-1].equity == pytest.approx(10_015.0)


def test_end_ts_truncates_and_closes_at_last_bar() -> None:
    res, _, _ = run(frame(12), {ts(1): sig(ts(1))}, end_ts=ts(8))
    assert [(t.exit_ts, t.exit_reason) for t in res.trades] == [(ts(7), "END")]
    assert len(res.equity) == 8


def test_4h_bars_are_built_from_utc_buckets() -> None:
    start = T0 + H1  # data starts mid-bucket: the first bucket is incomplete
    rows = {
        3: (100.0, 110.0, 95.0, 105.0),
        4: (105.0, 130.0, 100.0, 120.0),
        5: (120.0, 125.0, 90.0, 92.0),
        6: (92.0, 99.0, 91.0, 98.0),
    }
    _, _, wave = run(frame(9, rows, t0=start), {})
    with_4h = [(t, b4, sub) for t, b4, sub in wave.calls if b4 is not None]
    assert len(with_4h) == 1
    t, b4, sub = with_4h[0]
    assert t == T0 + 7 * H1
    assert b4 == Bar(ts=T0 + 4 * H1, open=100.0, high=130.0, low=90.0, close=98.0, volume=4.0, synthetic=False)
    assert sub is not None and [b.ts for b in sub] == [T0 + k * H1 for k in (4, 5, 6, 7)]
    assert [c[0] for c in wave.calls] == [start + k * H1 for k in range(9)]


def test_missing_bar_is_gap_filled_before_bucketing() -> None:
    df = frame(8).drop(index=5).reset_index(drop=True)
    _, _, wave = run(df, {})
    assert [c[0] for c in wave.calls] == [ts(i) for i in range(8)]
    b4 = [c[1] for c in wave.calls if c[1] is not None]
    assert len(b4) == 2 and b4[1] is not None and b4[1].synthetic


class RecChoch:
    """No-op CHoCH that records the 1h bars it is fed."""

    def __init__(self) -> None:
        self.bars: list[Bar] = []

    def update(self, bar1h: Bar) -> None:
        self.bars.append(bar1h)


def test_wicks_beyond_6_atr1h_are_clipped_for_waves_but_not_for_fills() -> None:
    # FLAT bars have TR = 200, so ATR1h(14) = 200 from bar 13 on and the cap is body + 6 * 200.
    # Bar 30 prints a 60_000 wick: the wave engine, the 4h bar (bucket 28..31) and the CHoCH see
    # 51_200, while the broker still fills TP1 55_000 and TP2 59_000 on the raw wick.
    df = frame(40, {30: (50_000.0, 60_000.0, 49_900.0, 50_000.0)})
    s = sig(ts(25), tp1=55_000.0, tp2=59_000.0)
    strat, wave, choch = FakeStrategy({ts(25): s}), StubWave(), RecChoch()
    res = TradingEngine(CFG, strat, wave_engine=wave, choch=choch).run(df)  # type: ignore[arg-type]
    assert (ts(0), "WICKS_CLIPPED:1") in res.events
    t31, b4, sub = wave.calls[31]
    assert t31 == ts(31) and b4 is not None and sub is not None
    assert b4.high == 51_200.0 and sub[2].ts == ts(30) and sub[2].high == 51_200.0
    assert choch.bars[30].high == 51_200.0 and choch.bars[30].close == 50_000.0
    assert [b.high for b in choch.bars[:30]] == [50_100.0] * 30
    [t] = res.trades
    assert (t.entry_ts, t.exit_ts, t.exit_reason) == (ts(26), ts(30), "TP2")
    assert t.pnl == pytest.approx(5 * 0.01 * 5_000.0 + 5 * 0.01 * 9_000.0)  # 250 + 450
    res0, _, _ = run(frame(20), {})
    assert (ts(0), "WICKS_CLIPPED:0") in res0.events


def test_tp1_moves_stop_to_breakeven_from_next_bar() -> None:
    rows = {
        3: (50_000.0, 51_100.0, 49_950.0, 51_000.0),  # TP1 5 @ 51_000; low below BE but BE not active yet
        4: (50_500.0, 50_600.0, 49_990.0, 50_100.0),  # BE stop 50_000 hit
    }
    res, _, _ = run(frame(10, rows), {ts(1): sig(ts(1))})
    [t] = res.trades
    assert (t.entry_ts, t.exit_ts, t.exit_reason) == (ts(2), ts(4), "STOP")
    assert t.exit_price_avg == pytest.approx(50_500.0)
    assert t.pnl == pytest.approx(50.0)


def test_trailing_stop_on_new_4h_pivot_long() -> None:
    wave = StubWave(at={
        ts(3): dict(new_pivot_4h=Pivot(i=1, sub_ts=ts(2), price=49_800.0, kind=-1, confirmed_at=0), atr4h=400.0),
        ts(7): dict(new_pivot_4h=Pivot(i=2, sub_ts=ts(5), price=50_600.0, kind=1, confirmed_at=1), atr4h=400.0),
    })
    rows = {
        8: (50_500.0, 50_550.0, 50_450.0, 50_500.0),  # would stop out if the HIGH pivot had been used
        9: (50_000.0, 50_050.0, 49_650.0, 49_800.0),  # trailed stop 49_800 - 0.25*400 = 49_700 hit
    }
    res, _, _ = run(frame(12, rows), {ts(1): sig(ts(1))}, wave)
    [t] = res.trades
    assert (t.exit_ts, t.exit_reason) == (ts(9), "STOP")
    assert t.exit_price_avg == pytest.approx(49_700.0)
    assert t.pnl == pytest.approx(-30.0)


def test_trailing_stop_short_never_loosens() -> None:
    wave = StubWave(at={
        ts(3): dict(new_pivot_4h=Pivot(i=1, sub_ts=ts(2), price=50_200.0, kind=1, confirmed_at=0), atr4h=400.0),
        ts(7): dict(new_pivot_4h=Pivot(i=2, sub_ts=ts(6), price=51_500.0, kind=1, confirmed_at=1), atr4h=400.0),
    })
    rows = {8: (50_000.0, 50_350.0, 49_950.0, 50_100.0)}  # stop 50_300 (not loosened to 51_600)
    res, _, _ = run(frame(12, rows), {ts(1): sig(ts(1), d=-1)}, wave)
    [t] = res.trades
    assert (t.direction, t.exit_ts, t.exit_reason) == (-1, ts(8), "STOP")
    assert t.exit_price_avg == pytest.approx(50_300.0)
    assert t.pnl == pytest.approx(-30.0)


def test_time_stop_counts_4h_closes_since_entry() -> None:
    # entry fills at bar 4 (ts % H4 == 0); 4h closes at bars 7 and 11 -> exit at bar 12 open
    rows = {12: (50_300.0, 50_400.0, 50_200.0, 50_300.0)}
    res, _, _ = run(frame(16, rows), {ts(3): sig(ts(3), time_stop_4h=2)})
    [t] = res.trades
    assert (t.entry_ts, t.exit_ts, t.exit_reason) == (ts(4), ts(12), "TIME")
    assert t.exit_price_avg == 50_300.0
    assert t.pnl == pytest.approx(30.0)


def test_tp1_disarms_time_stop() -> None:
    rows: dict[int, tuple[float, float, float, float]] = {5: (50_500.0, 51_100.0, 50_400.0, 50_900.0)}
    rows.update({i: (50_900.0, 51_000.0, 50_800.0, 50_900.0) for i in range(6, 16)})
    res, _, _ = run(frame(16, rows), {ts(3): sig(ts(3), time_stop_4h=2)})
    [t] = res.trades
    assert (t.exit_ts, t.exit_reason) == (ts(15), "END")
    assert t.pnl == pytest.approx(95.0)  # 5 * 0.01 * 1_000 + 5 * 0.01 * 900


def test_lineage_kill_exits_at_next_open() -> None:
    lin = ("IMP", 1, 7)
    wave = StubWave(default=dict(lineages=frozenset({lin})), at={ts(5): dict(lineages=frozenset())})
    rows = {6: (50_100.0, 50_200.0, 50_000.0, 50_100.0)}
    res, _, _ = run(frame(10, rows), {ts(1): sig(ts(1), lineage=lin)}, wave)
    [t] = res.trades
    assert (t.exit_ts, t.exit_reason, t.exit_price_avg) == (ts(6), "LINEAGE", 50_100.0)
    assert t.pnl == pytest.approx(10.0)


def test_flip_on_opposite_signal_and_ignore_same_direction() -> None:
    long1, long2, short = sig(ts(1)), sig(ts(3)), sig(ts(5), d=-1, setup="W5_REV")
    rows = {6: (50_200.0, 50_300.0, 50_100.0, 50_200.0), 9: (50_100.0, 50_200.0, 50_000.0, 50_100.0)}
    res, _, _ = run(frame(10, rows), {ts(1): long1, ts(3): long2, ts(5): short})
    assert [(t.direction, t.entry_ts, t.exit_ts, t.exit_reason) for t in res.trades] == [
        (1, ts(2), ts(6), "FLIP"),
        (-1, ts(6), ts(9), "END"),
    ]
    assert res.trades[0].pnl == pytest.approx(20.0)
    assert res.trades[1].pnl == pytest.approx(10.0)
    assert res.trades[1].setup == "W5_REV"
    assert (ts(3), "SIGNAL_IGNORED:same_direction") in res.events
    assert res.signals == (long1, short)


def test_cooldown_after_stop_until_phase_changes() -> None:
    inst = ("IMP", 1, 7, "W2")
    wave = StubWave(at={
        ts(4): dict(top=count(9, phase="W3")),   # other lineage: no release
        ts(5): dict(top=count(7, phase="W2")),   # same lineage, same phase: no release
        ts(6): dict(top=count(7, phase="W3")),   # same lineage, phase changed: release
    })
    rows = {3: (49_500.0, 49_600.0, 48_900.0, 49_000.0)}  # stop at 49_000
    plan = {ts(1): sig(ts(1), instance=inst), ts(5): sig(ts(5), instance=inst), ts(7): sig(ts(7), instance=inst)}
    res, _, _ = run(frame(12, rows), plan, wave)
    assert [(t.entry_ts, t.exit_reason) for t in res.trades] == [(ts(2), "STOP"), (ts(8), "END")]
    assert res.trades[0].pnl == pytest.approx(-100.0)
    assert res.trades[1].contracts == 9  # 1% of 9_900 / 1_000 = 0.099 BTC
    assert (ts(5), "SIGNAL_REJECTED:cooldown") in res.events


def test_entry_blackout_and_futures_close_block_new_entries() -> None:
    plan = {ts(k, FRI): sig(ts(k, FRI)) for k in (20, 21, 22, 24)}
    res, strat, _ = run(frame(30, t0=FRI), plan)
    for k in (20, 21, 22):   # fill bar in blackout / decision bar in blackout / futures closed
        assert ts(k, FRI) not in strat.calls
    assert ts(24, FRI) in strat.calls
    assert [(t.entry_ts, t.exit_reason) for t in res.trades] == [(ts(25, FRI), "END")]


def test_start_ts_gates_entries_and_equity_points() -> None:
    plan = {ts(2): sig(ts(2)), ts(6): sig(ts(6))}
    res, strat, wave = run(frame(10), plan, start_ts=ts(5))
    assert ts(2) not in strat.calls
    assert len(wave.calls) == 10  # warm-up bars are still fed to the wave engine
    assert [t.entry_ts for t in res.trades] == [ts(7)]
    assert [p.ts for p in res.equity] == [ts(i) for i in range(5, 10)]


def test_total_halt_flattens_and_blocks_entries() -> None:
    cfg = BotConfig(costs=ZERO, breakers=BreakerParams(total_dd=0.005))
    rows = {
        3: (50_000.0, 50_000.0, 49_350.0, 49_400.0),  # equity 9_940 <= 10_000 * 0.995
        4: (49_450.0, 49_500.0, 49_400.0, 49_450.0),
    }
    res, strat, _ = run(frame(10, rows), {ts(1): sig(ts(1)), ts(6): sig(ts(6))}, cfg=cfg)
    assert (ts(3), "TOTAL_HALT") in res.events
    [t] = res.trades
    assert (t.exit_ts, t.exit_reason, t.exit_price_avg) == (ts(4), "BREAKER", 49_450.0)
    assert t.pnl == pytest.approx(-55.0)
    assert ts(6) not in strat.calls
