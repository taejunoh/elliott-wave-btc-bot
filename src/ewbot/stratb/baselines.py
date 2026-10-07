"""Strategy-B baselines (Plan 4 spec 6.5 item 1, portfolio level since the 2026-10-07 revision): buy & hold
of a 50/50 BTC/ETH portfolio and the random-entry control.

Buy & hold splits the initial equity equally between the assets and runs A's backtest.benchmarks.buy_hold
on each asset's 1h bars built from its 15m frame (spot, the part's whole equity, one fee and one slippage
each way, hourly marks; no rebalancing); its equity is the sum of the parts, after the same starting point
as the engine's curve (the initial equity at the hour before start_ts, when the frames have that hour), so
strategy B and buy & hold are measured over the same hours. The random control keeps
everything of strategy B but the entry times, per asset: the same window, as many entries on each asset
as B's trades on it (each asset's own frequency), that asset's own trade shapes (direction, stop distance
as a fraction of the reference price, TP1 and TP2 in R) in a seeded random order, on uniformly drawn 15m
bars of the asset (not the last bar, no gap-filled bar or fill bar), and the same portfolio engine (exits,
48 h time stop, remainder rule, 0.5% sizing, 3x caps, account breakers, costs). An entry that comes while
its asset holds a position is skipped, as for B."""

from __future__ import annotations

import multiprocessing as mp
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from ewbot.backtest.benchmarks import buy_hold
from ewbot.backtest.metrics import compute_metrics
from ewbot.engine.loop import EquityPoint, Trade
from ewbot.stratb.config import M15, BConfig, asset_costs
from ewbot.stratb.data15 import resample_1h
from ewbot.stratb.engine import asset_of, run_engine
from ewbot.stratb.signals import Frame15, SignalB
from ewbot.types import H1

RANDOM_METRICS = ("sharpe", "max_dd", "total_return", "calmar", "n_trades")


@dataclass(frozen=True, slots=True)
class Shape:
    direction: int
    stop_frac: float  # |entry_ref - stop| / entry_ref
    tp1_r: float  # |tp1 - entry_ref| / |entry_ref - stop|
    tp2_r: float


def trade_shapes(trades: Sequence[Trade]) -> list[Shape]:
    """Shapes of B's trades, from the signal's reference price kept in the trade snapshot."""
    out: list[Shape] = []
    for t in trades:
        ref = float(t.snapshot["entry_ref"])
        dist = abs(ref - t.stop0)
        if ref > 0.0 and dist > 0.0 and t.tp2 is not None:
            out.append(Shape(t.direction, dist / ref, abs(t.tp1 - ref) / dist, abs(t.tp2 - ref) / dist))
    return out


def _assets(frames: Mapping[str, Frame15], cfg: BConfig) -> list[str]:
    return [s.asset for s in cfg.assets if s.asset in frames]


def buy_hold_b(
    frames: Mapping[str, Frame15], start_ts: int, end_ts: int, cfg: BConfig
) -> tuple[list[EquityPoint], list[Trade]]:
    """Equal-weight buy & hold of the frames' assets: (summed hourly equity, every part's trade)."""
    names = _assets(frames, cfg)
    share = cfg.initial_equity / len(names)
    parts: list[list[EquityPoint]] = []
    trades: list[Trade] = []
    for a in names:
        f = frames[a]
        df = pd.DataFrame({"ts": f.ts, "open": f.open, "high": f.high, "low": f.low, "close": f.close,
                           "volume": 0.0, "synthetic": f.synthetic})
        eq, tr = buy_hold(resample_1h(df), start_ts, end_ts, asset_costs(cfg, a), share)
        parts.append(eq)
        trades.extend(tr)
    out: list[EquityPoint] = []
    base = start_ts // H1 * H1 - H1  # run_engine's first point with a start time: the account before the window
    if any(parts) and min(int(frames[a].ts[0]) for a in names) <= base + H1 - M15:
        out.append(EquityPoint(base, float(cfg.initial_equity)))
    cur, nxt = [share] * len(parts), [0] * len(parts)
    for t in sorted({p.ts for eq in parts for p in eq}):
        for k, eq in enumerate(parts):  # each part at its latest point at or before t
            while nxt[k] < len(eq) and eq[nxt[k]].ts <= t:
                cur[k] = eq[nxt[k]].equity
                nxt[k] += 1
        out.append(EquityPoint(t, float(sum(cur))))
    return out, trades


def random_signals(
    f: Frame15, shapes: Sequence[Shape], n: int, start_ts: int, end_ts: int, seed: int | Sequence[int]
) -> list[SignalB]:
    """n entries (at most one per bar) on eligible bars of [start_ts, end_ts), shapes in a seeded order."""
    if n <= 0 or not shapes:
        return []
    last = len(f) - 1
    idx = np.flatnonzero((f.ts >= start_ts) & (f.ts < end_ts))
    idx = idx[idx < last]
    idx = idx[~f.synthetic[idx] & ~f.synthetic[idx + 1]]
    k = min(int(n), int(idx.size))
    if k == 0:
        return []
    rng = np.random.default_rng(seed)
    times = np.sort(rng.choice(idx, size=k, replace=False))
    order = rng.permutation(len(shapes))
    out: list[SignalB] = []
    for m, i in enumerate(times.tolist()):
        s = shapes[int(order[m % len(shapes)])]
        ref = float(f.close[i])
        dist = s.stop_frac * ref
        d = s.direction
        out.append(SignalB(i=i, ts=int(f.ts[i]), direction=d, entry_ref=ref, stop=ref - d * dist,
                           tp1=ref + d * s.tp1_r * dist, tp2=ref + d * s.tp2_r * dist, reason="random entry"))
    return out


_POOL: dict[str, Any] = {}


def _pool_init(frames: dict[str, Frame15], cfg: BConfig, shapes: dict[str, list[Shape]], n: dict[str, int],
               start_ts: int, end_ts: int) -> None:
    _POOL.clear()
    _POOL.update(frames=frames, cfg=cfg, shapes=shapes, n=n, start=start_ts, end=end_ts)


def _pool_run(seed: int) -> dict[str, float]:
    s = _POOL
    sigs = {a: random_signals(f, s["shapes"][a], s["n"][a], s["start"], s["end"], [seed, k])
            for k, (a, f) in enumerate(s["frames"].items())}
    res = run_engine(s["frames"], sigs, s["cfg"], s["start"])
    m = compute_metrics(res.equity, res.trades, s["start"], s["end"])
    return {k: float(getattr(m, k)) for k in RANDOM_METRICS}


def _dist(values: Sequence[float]) -> dict[str, float]:
    a = np.asarray(values, dtype=np.float64)
    return {"mean": float(a.mean()), "p05": float(np.percentile(a, 5)), "p50": float(np.percentile(a, 50)),
            "p95": float(np.percentile(a, 95))}


def random_control(
    frames: Mapping[str, Frame15],
    cfg: BConfig,
    b_trades: Sequence[Trade],
    start_ts: int,
    end_ts: int,
    runs: int = 1000,
    seed: int = 0,
    workers: int = 1,
) -> dict[str, Any]:
    """`runs` random-entry runs over [start_ts, end_ts): run r draws the entries of the run's k-th asset
    (config order) with the seed [seed + r, k]. n_signals = {asset: B's trades on it in the window};
    dist[metric] = {mean, p05, p50, p95}; no runs when B made no trade in the window (nothing to imitate)."""
    names = _assets(frames, cfg)
    mine = [t for t in b_trades if start_ts <= t.entry_ts < end_ts]
    per = {a: [t for t in mine if asset_of(t) == a] for a in names}
    shapes = {a: trade_shapes(per[a]) for a in names}
    n = {a: len(per[a]) for a in names}
    out: dict[str, Any] = {"runs": 0, "n_signals": n, "seed": seed, "dist": {}}
    if runs <= 0 or not any(shapes.values()):
        return out
    seeds = list(range(seed, seed + runs))
    args = ({a: frames[a] for a in names}, cfg, shapes, n, start_ts, end_ts)
    if workers <= 1:
        _pool_init(*args)
        rows = [_pool_run(s) for s in seeds]
    else:
        with mp.Pool(min(workers, runs), initializer=_pool_init, initargs=args) as pool:
            rows = pool.map(_pool_run, seeds, chunksize=max(1, runs // (4 * workers)))
    out["runs"] = len(rows)
    out["dist"] = {k: _dist([r[k] for r in rows]) for k in RANDOM_METRICS}
    return out
