"""Backtest runner and the A / C / HYBRID / BH / EMA / RANDOM comparison."""

from __future__ import annotations

import dataclasses
import multiprocessing as mp
from datetime import UTC, datetime
from typing import Any, Sequence

import numpy as np
import pandas as pd

from ewbot.backtest.benchmarks import RandomEntryStrategy, buy_hold, ema_cross_equity
from ewbot.backtest.metrics import Metrics, bootstrap_ci, compute_metrics, trade_stats
from ewbot.config import BotConfig, with_overrides
from ewbot.engine.loop import EngineResult, EquityPoint, TradingEngine, Trade
from ewbot.strategy.hybrid import make_strategy

MODES = ("A", "C", "HYBRID")
_RANDOM_METRICS = ("total_return", "max_dd", "sharpe", "calmar", "avg_r", "n_trades")


def utc_ts(date: str) -> int:
    """'YYYY-MM-DD' (UTC midnight) -> epoch seconds."""
    return int(datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def equity_pairs(equity: Sequence[EquityPoint]) -> list[tuple[int, float]]:
    return [(p.ts, p.equity) for p in equity]


def run_backtest(
    cfg: BotConfig, df1h: pd.DataFrame, start_ts: int | None = None, end_ts: int | None = None
) -> EngineResult:
    """One TradingEngine run with the strategy selected by cfg.strategy.mode."""
    engine = TradingEngine(cfg, make_strategy(cfg.strategy))
    return engine.run(df1h, start_ts, end_ts)


# ----------------------------------------------------------------------------- breakdowns


def breakdowns(res: EngineResult, start_ts: int, end_ts: int) -> dict[str, Any]:
    """Per-setup, long/short and per-calendar-year breakdowns of one engine result."""
    trades = [t for t in res.trades if start_ts <= t.entry_ts < end_ts]
    per_setup = {
        s: trade_stats([t for t in trades if t.setup == s]) for s in sorted({t.setup for t in trades})
    }
    long_short = {
        "long": trade_stats([t for t in trades if t.direction > 0]),
        "short": trade_stats([t for t in trades if t.direction < 0]),
    }
    per_year: dict[str, dict[str, float | int]] = {}
    y0 = datetime.fromtimestamp(start_ts, UTC).year
    y1 = datetime.fromtimestamp(end_ts - 1, UTC).year
    for y in range(y0, y1 + 1):
        lo = max(start_ts, utc_ts(f"{y}-01-01"))
        hi = min(end_ts, utc_ts(f"{y + 1}-01-01"))
        per_year[str(y)] = dataclasses.asdict(compute_metrics(res.equity, res.trades, lo, hi))
    return {"per_setup": per_setup, "long_short": long_short, "per_year": per_year}


# ----------------------------------------------------------------------------- task execution

# Per-process state set by the pool initializer (spawn-safe; each worker gets its own copy).
_STATE: dict[str, Any] = {}


def _init_worker(cfg: BotConfig, df1h: pd.DataFrame, start_ts: int, end_ts: int) -> None:
    _STATE.update(cfg=cfg, df=df1h, start=start_ts, end=end_ts)


def _task(spec: tuple) -> EngineResult:
    cfg: BotConfig = _STATE["cfg"]
    df, start, end = _STATE["df"], _STATE["start"], _STATE["end"]
    if spec[0] == "mode":
        return run_backtest(with_overrides(cfg, **{"strategy.mode": spec[1]}), df, start, end)
    _, seed, refs, n_signals = spec
    strat = RandomEntryStrategy(cfg.strategy, refs, n_signals, (start, end), seed)
    res = TradingEngine(cfg, strat).run(df, start, end)
    return dataclasses.replace(res, signals=())  # keep the pickle back to the parent small


def _map_tasks(
    specs: list[tuple], workers: int, cfg: BotConfig, df1h: pd.DataFrame, start_ts: int, end_ts: int
) -> list[EngineResult]:
    initargs = (cfg, df1h, start_ts, end_ts)
    if workers <= 1 or len(specs) <= 1:
        _init_worker(*initargs)
        return [_task(s) for s in specs]
    with mp.Pool(min(workers, len(specs)), initializer=_init_worker, initargs=initargs) as pool:
        return pool.map(_task, specs, chunksize=1)


# ----------------------------------------------------------------------------- compare


def _dist(values: Sequence[float]) -> dict[str, float]:
    a = np.asarray(values, dtype=np.float64)
    return {
        "mean": float(a.mean()),
        "p05": float(np.percentile(a, 5)),
        "p50": float(np.percentile(a, 50)),
        "p95": float(np.percentile(a, 95)),
    }


def compare(
    cfg: BotConfig,
    df1h: pd.DataFrame,
    start_ts: int,
    end_ts: int,
    random_runs: int = 100,
    workers: int = 1,
) -> dict[str, Any]:
    """Run A, C, HYBRID (same cfg, only strategy.mode differs), buy&hold, EMA 50/200 and
    `random_runs` RandomEntryStrategy runs over [start_ts, end_ts) with identical costs.

    Returns {"period", "comparison": {name: Metrics dict}, "random": {...}, "equity": {name:
    [(ts, equity)]}, "ci": {name: {...}}, "breakdown": {name: {...}}, "trades": {name: [Trade]}}.
    """
    results = dict(zip(MODES, _map_tasks([("mode", m) for m in MODES], workers, cfg, df1h, start_ts, end_ts)))

    equity: dict[str, list[tuple[int, float]]] = {}
    trades: dict[str, list[Trade]] = {}
    comparison: dict[str, dict[str, float | int]] = {}
    ci: dict[str, dict[str, tuple[float, float]]] = {}
    breakdown: dict[str, dict[str, Any]] = {}
    metrics: dict[str, Metrics] = {}

    def add(name: str, eq: Sequence[EquityPoint], trs: Sequence[Trade], res: EngineResult | None) -> None:
        m = compute_metrics(eq, trs, start_ts, end_ts)
        metrics[name] = m
        comparison[name] = dataclasses.asdict(m)
        equity[name] = equity_pairs(eq)
        trades[name] = list(trs)
        win = [t for t in trs if start_ts <= t.entry_ts < end_ts]
        ci[name] = {
            "avg_r": bootstrap_ci(win, "avg_r", seed=cfg.seed),
            "win_rate": bootstrap_ci(win, "win_rate", seed=cfg.seed),
        }
        if res is not None:
            breakdown[name] = breakdowns(res, start_ts, end_ts)

    for mode in MODES:
        add(mode, results[mode].equity, results[mode].trades, results[mode])

    bh_eq, bh_trades = buy_hold(df1h, start_ts, end_ts, cfg.costs, cfg.initial_equity)
    add("BH", bh_eq, bh_trades, None)
    comparison["BH"]["exposure"] = 1.0  # always invested; mark-to-market trade is one END exit
    ema_eq, ema_trades = ema_cross_equity(df1h, start_ts, end_ts, cfg.costs, cfg.initial_equity)
    add("EMA", ema_eq, ema_trades, None)

    # Random control: same number of entries as A actually made, geometry from A's trades.
    a_trades = [t for t in results["A"].trades if start_ts <= t.entry_ts < end_ts]
    n_signals = len(a_trades)
    rand_metrics: list[Metrics] = []
    if random_runs > 0 and n_signals > 0:
        specs = [("random", cfg.seed + k, a_trades, n_signals) for k in range(random_runs)]
        for r in _map_tasks(specs, workers, cfg, df1h, start_ts, end_ts):
            rand_metrics.append(compute_metrics(r.equity, r.trades, start_ts, end_ts))
    random: dict[str, Any] = {"runs": len(rand_metrics), "n_signals": n_signals, "dist": {}, "beaten_by": {}}
    if rand_metrics:
        for key in _RANDOM_METRICS:
            random["dist"][key] = _dist([getattr(m, key) for m in rand_metrics])
        for name in MODES:
            random["beaten_by"][name] = {
                key: float(np.mean([getattr(m, key) < getattr(metrics[name], key) for m in rand_metrics]))
                for key in ("sharpe", "calmar", "total_return")
            }

    return {
        "period": {"start": start_ts, "end": end_ts},
        "comparison": comparison,
        "random": random,
        "equity": equity,
        "ci": ci,
        "breakdown": breakdown,
        "trades": trades,
    }
