"""Backtest performance metrics (hourly equity, annualised with 8760) and bootstrap CIs."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from ewbot.engine.loop import EquityPoint, Trade

HOURS_PER_YEAR = 8760
_EPS = 1e-15


@dataclass(frozen=True, slots=True)
class Metrics:
    total_return: float
    cagr: float
    max_dd: float
    sharpe: float
    sortino: float
    calmar: float
    win_rate: float
    avg_r: float
    n_trades: int
    exposure: float


def _in_window(ts: int, start_ts: int | None, end_ts: int | None) -> bool:
    return (start_ts is None or ts >= start_ts) and (end_ts is None or ts < end_ts)


def _window_equity(
    equity: Sequence[EquityPoint], start_ts: int | None, end_ts: int | None
) -> tuple[np.ndarray, np.ndarray]:
    """Equity points with start_ts <= ts < end_ts, plus the last point BEFORE start_ts as
    the base, so the first in-window return is measured from the true prior value."""
    n = len(equity)
    ts = np.fromiter((p.ts for p in equity), dtype=np.int64, count=n)
    eq = np.fromiter((p.equity for p in equity), dtype=np.float64, count=n)
    keep = np.ones(n, dtype=bool)
    if start_ts is not None:
        keep &= ts >= start_ts
    if end_ts is not None:
        keep &= ts < end_ts
    idx = np.flatnonzero(keep)
    if idx.size == 0:
        return ts[:0], eq[:0]
    first, last = int(idx[0]), int(idx[-1])
    if first > 0:
        first -= 1
    return ts[first : last + 1], eq[first : last + 1]


def compute_metrics(
    equity: Sequence[EquityPoint],
    trades: Sequence[Trade],
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> Metrics:
    """Hourly-return metrics over [start_ts, end_ts). Trade stats use trades whose
    entry_ts is in the window. calmar = cagr / max_dd, or 0.0 when max_dd == 0."""
    win = [t for t in trades if _in_window(t.entry_ts, start_ts, end_ts)]
    n_tr = len(win)
    win_rate = sum(1 for t in win if t.pnl > 0) / n_tr if n_tr else 0.0
    avg_r = float(np.mean([t.r_multiple for t in win])) if n_tr else 0.0

    ts, eq = _window_equity(equity, start_ts, end_ts)
    if eq.size < 2 or eq[0] <= 0:
        return Metrics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, win_rate, avg_r, n_tr, 0.0)

    prev = eq[:-1]
    safe_prev = np.where(prev > 0, prev, 1.0)
    rets = np.where(prev > 0, eq[1:] / safe_prev - 1.0, 0.0)

    total_return = float(eq[-1] / eq[0] - 1.0)
    years = rets.size / HOURS_PER_YEAR
    if eq[-1] <= 0:
        cagr = -1.0
    else:
        cagr = math.expm1(min(700.0, math.log(eq[-1] / eq[0]) / years))

    peak = np.maximum.accumulate(eq)
    safe_peak = np.where(peak > 0, peak, 1.0)
    max_dd = float(np.max(np.where(peak > 0, (peak - eq) / safe_peak, 0.0)))

    ann = math.sqrt(HOURS_PER_YEAR)
    mean = float(rets.mean())
    std = float(rets.std(ddof=1)) if rets.size > 1 else 0.0
    sharpe = mean / std * ann if std > _EPS else 0.0
    downside = float(math.sqrt(np.mean(np.minimum(rets, 0.0) ** 2)))
    sortino = mean / downside * ann if downside > _EPS else 0.0
    calmar = cagr / max_dd if max_dd > 1e-12 else 0.0

    span = float(ts[-1] - ts[0])
    held = 0.0
    for t in trades:
        lo, hi = max(t.entry_ts, int(ts[0])), min(t.exit_ts, int(ts[-1]))
        if hi > lo:
            held += hi - lo
    exposure = min(1.0, held / span) if span > 0 else 0.0

    return Metrics(
        total_return=total_return,
        cagr=float(cagr),
        max_dd=max_dd,
        sharpe=float(sharpe),
        sortino=float(sortino),
        calmar=float(calmar),
        win_rate=win_rate,
        avg_r=avg_r,
        n_trades=n_tr,
        exposure=float(exposure),
    )


def _trade_values(trades: Sequence[Trade], stat: str) -> np.ndarray:
    if stat == "avg_r":
        return np.array([t.r_multiple for t in trades], dtype=np.float64)
    if stat == "win_rate":
        return np.array([1.0 if t.pnl > 0 else 0.0 for t in trades], dtype=np.float64)
    if stat == "avg_pnl":
        return np.array([t.pnl for t in trades], dtype=np.float64)
    raise ValueError(f"unknown bootstrap stat {stat!r} (use avg_r | win_rate | avg_pnl)")


def bootstrap_ci(
    trades: Sequence[Trade], stat: str = "avg_r", n: int = 2000, seed: int = 0
) -> tuple[float, float]:
    """95% percentile bootstrap CI of the per-trade mean of `stat` (resampling trades)."""
    vals = _trade_values(trades, stat)
    m = vals.size
    if m == 0:
        return (0.0, 0.0)
    rng = np.random.default_rng(seed)
    means = np.empty(n, dtype=np.float64)
    chunk = max(1, 4_000_000 // m)
    for lo in range(0, n, chunk):
        hi = min(n, lo + chunk)
        idx = rng.integers(0, m, size=(hi - lo, m))
        means[lo:hi] = vals[idx].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def trade_stats(trades: Sequence[Trade], seed: int = 0) -> dict[str, float | int]:
    """Trade-level summary used by the per-setup / long-short breakdown tables."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "win_rate": 0.0, "avg_r": 0.0, "pnl": 0.0, "ci_lo": 0.0, "ci_hi": 0.0}
    lo, hi = bootstrap_ci(trades, "avg_r", n=1000, seed=seed)
    return {
        "n": n,
        "win_rate": sum(1 for t in trades if t.pnl > 0) / n,
        "avg_r": float(np.mean([t.r_multiple for t in trades])),
        "pnl": float(sum(t.pnl for t in trades)),
        "ci_lo": lo,
        "ci_hi": hi,
    }


def reliability_by_q(
    trades: Sequence[Trade], edges: Sequence[float] = (0.5, 0.6, 0.7, 0.8, 1.01)
) -> list[dict[str, float | int]]:
    """Hit rate (pnl > 0) per bin of the top-count fit q at signal time (spec 9.5).
    Trades without snapshot['q_top'] are skipped."""
    rows: list[dict[str, float | int]] = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = [t for t in trades if lo <= float(t.snapshot.get("q_top", -1.0)) < hi]
        n = len(sel)
        rows.append(
            {
                "q_lo": lo,
                "q_hi": hi,
                "n": n,
                "hit_rate": sum(1 for t in sel if t.pnl > 0) / n if n else 0.0,
                "avg_r": float(np.mean([t.r_multiple for t in sel])) if n else 0.0,
            }
        )
    return rows
