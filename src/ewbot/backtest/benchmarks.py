"""Benchmarks under the same cost model: buy&hold, EMA 50/200 (4h) long/short, random entry."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Sequence

import numpy as np
import pandas as pd

from ewbot.config import CostParams, StrategyParams
from ewbot.data.candles import aggregate_4h, entry_blackout, slice_ts
from ewbot.engine.loop import EquityPoint, Trade
from ewbot.execution.costs import fee, funding_per_hour, slip
from ewbot.indicators import ema_series
from ewbot.strategy.base import Signal, time_stop_bars
from ewbot.types import H1, H4, Bar

if TYPE_CHECKING:  # pragma: no cover
    from ewbot.strategy.choch import ChochDetector
    from ewbot.waves.engine import WaveState

_R_PCT_OF_EQUITY = 0.01  # benchmarks have no stop: 1R is defined as 1% of equity at entry


class _TargetSim:
    """1x-notional futures simulator that follows a target direction (+1/-1/0).

    Fills at the bar OPEN with slippage + fee, funding charged per held hour at the bar
    close mark. Integer contracts of `contract_size` BTC, sized on realised equity.
    """

    def __init__(self, costs: CostParams, equity0: float, setup: str) -> None:
        self.c = costs
        self.setup = setup
        self.cash = float(equity0)
        self.dir = 0
        self.n = 0
        self.entry_px = 0.0
        self.entry_ts = 0
        self.entry_fee = 0.0
        self.entry_eq = 0.0
        self.funding = 0.0
        self.trades: list[Trade] = []

    def open(self, ts: int, raw_px: float, direction: int) -> None:
        px = slip(raw_px, direction, self.c)
        n = int(self.cash // (px * self.c.contract_size))
        if n <= 0:
            return
        f = fee(px, n, self.c)
        self.entry_eq = self.cash
        self.cash -= f
        self.dir, self.n, self.entry_px, self.entry_ts = direction, n, px, ts
        self.entry_fee, self.funding = f, 0.0

    def close(self, ts: int, raw_px: float, reason: str) -> None:
        px = slip(raw_px, -self.dir, self.c)
        f = fee(px, self.n, self.c)
        gross = self.dir * self.n * self.c.contract_size * (px - self.entry_px)
        self.cash += gross - f
        pnl = gross - self.entry_fee - f - self.funding
        self.trades.append(
            Trade(
                entry_ts=self.entry_ts,
                exit_ts=ts,
                setup=self.setup,
                direction=self.dir,
                contracts=self.n,
                entry_price=self.entry_px,
                exit_price_avg=px,
                stop0=0.0,
                tp1=0.0,
                tp2=None,
                pnl=pnl,
                fees=self.entry_fee + f,
                funding=self.funding,
                r_multiple=pnl / (_R_PCT_OF_EQUITY * self.entry_eq),
                exit_reason=reason,
                snapshot={},
            )
        )
        self.dir, self.n = 0, 0

    def hold_hour(self, mark: float) -> None:
        if self.dir:
            cost = funding_per_hour(mark, self.n, self.dir, self.c)
            self.cash -= cost
            self.funding += cost

    def mark(self, close: float) -> float:
        if not self.dir:
            return self.cash
        return self.cash + self.dir * self.n * self.c.contract_size * (close - self.entry_px)


def _simulate(
    ts: np.ndarray,
    op: np.ndarray,
    cl: np.ndarray,
    desired: np.ndarray,
    costs: CostParams,
    equity0: float,
    setup: str,
) -> tuple[list[EquityPoint], list[Trade]]:
    sim = _TargetSim(costs, equity0, setup)
    equity: list[EquityPoint] = []
    last = len(ts) - 1
    for i in range(len(ts)):
        t = int(ts[i])
        want = int(desired[i])
        if want != sim.dir:
            if sim.dir != 0:
                sim.close(t, float(op[i]), "FLIP" if want != 0 else "EXIT")
            if want != 0:
                sim.open(t, float(op[i]), want)
        if sim.dir:
            sim.hold_hour(float(cl[i]))
        if i == last and sim.dir:
            sim.close(t + H1, float(cl[i]), "END")
        equity.append(EquityPoint(ts=t, equity=sim.mark(float(cl[i]))))
    return equity, sim.trades


def _window_arrays(df1h: pd.DataFrame, start_ts: int, end_ts: int):
    w = slice_ts(df1h, start_ts, end_ts)
    return (
        w["ts"].to_numpy(dtype=np.int64),
        w["open"].to_numpy(dtype=np.float64),
        w["close"].to_numpy(dtype=np.float64),
    )


def buy_hold(
    df1h: pd.DataFrame, start_ts: int, end_ts: int, costs: CostParams, equity0: float
) -> tuple[list[EquityPoint], list[Trade]]:
    """SPOT buy & hold: fractional BTC, no leverage, NO funding.

    Like every strategy it decides at the close of the first bar in the window and buys at the
    NEXT open (+slippage, one fee on the notional, the whole equity spent including that fee),
    holds, and sells everything at the last close (-slippage, one fee), reason END. So
    final / equity0 = (close_last / open_second) * ((1 - s) / (1 + s)) * ((1 - f) / (1 + f)):
    the raw price multiple minus two fees and two slippages. Equity: one point per 1h bar marked
    at the close (the first point is equity0, the last is after the exit). Returns
    (equity, [the single trade]); no trade when the window has fewer than 2 bars.
    """
    ts, op, cl = _window_arrays(df1h, start_ts, end_ts)
    if ts.size == 0:
        return [], []
    equity = [EquityPoint(ts=int(ts[0]), equity=float(equity0))]
    if ts.size < 2:
        return equity, []
    px_in = slip(float(op[1]), 1, costs)
    qty = equity0 / (px_in * (1.0 + costs.fee_rate))  # BTC; notional + entry fee == equity0
    fee_in = costs.fee_rate * qty * px_in
    for i in range(1, ts.size - 1):
        equity.append(EquityPoint(ts=int(ts[i]), equity=qty * float(cl[i])))
    px_out = slip(float(cl[-1]), -1, costs)
    fee_out = costs.fee_rate * qty * px_out
    final = qty * px_out - fee_out
    equity.append(EquityPoint(ts=int(ts[-1]), equity=final))
    pnl = final - equity0
    trade = Trade(
        entry_ts=int(ts[1]),
        exit_ts=int(ts[-1]),
        setup="BH",
        direction=1,
        contracts=int(qty / costs.contract_size),  # informational; the holding is qty_btc
        entry_price=px_in,
        exit_price_avg=px_out,
        stop0=0.0,
        tp1=0.0,
        tp2=None,
        pnl=pnl,
        fees=fee_in + fee_out,
        funding=0.0,
        r_multiple=pnl / (_R_PCT_OF_EQUITY * equity0),
        exit_reason="END",
        snapshot={"spot": True, "qty_btc": qty},
    )
    return equity, [trade]


def buy_hold_equity(
    df1h: pd.DataFrame, start_ts: int, end_ts: int, costs: CostParams, equity0: float
) -> list[EquityPoint]:
    return buy_hold(df1h, start_ts, end_ts, costs, equity0)[0]


def ema_cross_equity(
    df1h: pd.DataFrame,
    start_ts: int,
    end_ts: int,
    costs: CostParams,
    equity0: float,
    fast: int = 50,
    slow: int = 200,
) -> tuple[list[EquityPoint], list[Trade]]:
    """Always-in-the-market 4h EMA(fast)/EMA(slow) cross, long when fast>slow else short.

    Direction is read from the last CLOSED 4h bar and executed at the next 1h open. EMAs are
    computed on all history in `df1h` (warm-up), flat until `slow` 4h bars exist.
    """
    ts, op, cl = _window_arrays(df1h, start_ts, end_ts)
    if ts.size == 0:
        return [], []
    d4 = aggregate_4h(df1h)
    if len(d4) == 0:
        return _simulate(ts, op, cl, np.zeros(ts.size, dtype=np.int64), costs, equity0, "EMA")
    close4 = d4["close"].to_numpy(dtype=np.float64)
    ts4 = d4["ts"].to_numpy(dtype=np.int64)
    ef, es = ema_series(close4, fast), ema_series(close4, slow)
    dirs = np.where(ef > es, 1, -1).astype(np.int64)
    dirs[:slow] = 0
    closed = np.searchsorted(ts4 + H4, ts, side="right") - 1  # last 4h bar closed at/before bar open
    desired = np.where(closed >= 0, dirs[np.clip(closed, 0, None)], 0)
    return _simulate(ts, op, cl, desired, costs, equity0, "EMA")


@dataclass(frozen=True, slots=True)
class _RefShape:
    stop_frac: float      # stop distance / entry price
    tp1_r: float          # |tp1 - entry| / stop distance
    tp2_r: float | None
    leg_4h: int           # holding time in 4h bars -> time stop via time_stop_bars


def _reference_shape(t: Trade) -> _RefShape | None:
    dist = abs(t.entry_price - t.stop0)
    if t.entry_price <= 0 or dist <= 0:
        return None
    return _RefShape(
        stop_frac=dist / t.entry_price,
        tp1_r=abs(t.tp1 - t.entry_price) / dist,
        tp2_r=None if t.tp2 is None else abs(t.tp2 - t.entry_price) / dist,
        leg_4h=max(1, int(round((t.exit_ts - t.entry_ts) / H4))),
    )


@lru_cache(maxsize=8)
def _eligible_hours(start_ts: int, end_ts: int) -> np.ndarray:
    first = (start_ts + H1 - 1) // H1 * H1
    return np.array(
        [t for t in range(first, end_ts, H1) if not entry_blackout(t) and not entry_blackout(t + H1)],
        dtype=np.int64,
    )


class RandomEntryStrategy:
    """Spec 5.7 control: `n_signals` entries at uniformly random eligible 1h bars in `span`,
    direction 50/50, stop distance (as a fraction of price), TP multiples (in R) and time stop
    drawn from the reference trades. Ignores the wave state and CHoCH. Plan is fixed at
    construction from `seed`; the engine decides whether a signal can be used (flat, breakers).
    """

    name = "RANDOM"

    def __init__(
        self,
        sp: StrategyParams,
        reference_trades: Sequence[Trade],
        n_signals: int,
        span: tuple[int, int],
        seed: int,
    ) -> None:
        self.sp = sp
        self.seed = seed
        refs = [r for r in (_reference_shape(t) for t in reference_trades) if r is not None]
        eligible = _eligible_hours(int(span[0]), int(span[1]))
        k = min(int(n_signals), int(eligible.size)) if refs else 0
        plan: dict[int, tuple[int, _RefShape]] = {}
        if k > 0:
            rng = np.random.default_rng(seed)
            times = np.sort(rng.choice(eligible, size=k, replace=False))
            dirs = rng.choice(np.array([1, -1]), size=k)
            picks = rng.integers(0, len(refs), size=k)
            for t, d, p in zip(times, dirs, picks):
                plan[int(t)] = (int(d), refs[int(p)])
        self._plan = plan

    @property
    def planned_times(self) -> tuple[int, ...]:
        return tuple(sorted(self._plan))

    def on_bar(self, bar: Bar, ws: "WaveState", choch: "ChochDetector") -> Signal | None:
        item = self._plan.get(bar.ts)
        if item is None:
            return None
        d, ref = item
        dist = ref.stop_frac * bar.close
        return Signal(
            ts=bar.ts,
            setup="RANDOM",
            direction=d,
            entry_ref=bar.close,
            stop=bar.close - d * dist,
            tp1=bar.close + d * ref.tp1_r * dist,
            tp2=None if ref.tp2_r is None else bar.close + d * ref.tp2_r * dist,
            risk_mult=1.0,
            time_stop_4h=time_stop_bars(ref.leg_4h, self.sp),
            instance=None,
            lineage=None,
            reason="random entry",
        )
