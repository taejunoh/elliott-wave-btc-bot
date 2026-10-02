"""Walk-forward validation: one run per grid combo, plateau selection per fold, stitched OOS."""

from __future__ import annotations

import dataclasses
import itertools
import math
import multiprocessing as mp
import sys
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from ewbot.backtest.metrics import compute_metrics
from ewbot.backtest.runner import run_backtest, utc_ts
from ewbot.config import BotConfig, with_overrides
from ewbot.engine.loop import EngineResult, EquityPoint
from ewbot.types import H1

GRID: dict[str, tuple[float, ...]] = {
    "wave.K4": (2.0, 2.5, 3.0, 4.0),
    "strategy.Q_MIN": (0.5, 0.55, 0.6, 0.65, 0.7),
    "strategy.S_MIN": (0.0, 0.25, 0.5),
}
WF_START = "2017-01-01"
WF_END = "2024-01-01"  # holdout starts here; nothing at/after this date is touched
# spec 9.3: a combo needs this many trades in a 24-month training window. A degeneracy floor for
# the Calmar objective, not a power criterion (2017-2023: about 8-13 trades/year).
MIN_TRAIN_TRADES = 12

Combo = tuple[float, ...]


def grid_combos() -> list[Combo]:
    """The 4 x 5 x 3 = 60 combos in GRID key order (K4, Q_MIN, S_MIN)."""
    return list(itertools.product(*GRID.values()))


def config_for(cfg: BotConfig, combo: Combo) -> BotConfig:
    return with_overrides(cfg, **dict(zip(GRID.keys(), combo)))


# ----------------------------------------------------------------------------- grid run

_STATE: dict[str, Any] = {}  # per-process, set by the pool initializer


def _init_worker(cfg: BotConfig, df1h: pd.DataFrame, start_ts: int, end_ts: int) -> None:
    _STATE.update(cfg=cfg, df=df1h, start=start_ts, end=end_ts)


def _run_combo(combo: Combo) -> tuple[Combo, EngineResult]:
    res = run_backtest(config_for(_STATE["cfg"], combo), _STATE["df"], _STATE["start"], _STATE["end"])
    return combo, dataclasses.replace(res, signals=())  # signals are not needed downstream


def run_grid(
    cfg: BotConfig, df1h: pd.DataFrame, start_ts: int, end_ts: int, workers: int
) -> dict[Combo, EngineResult]:
    """Run every combo ONCE over [start_ts, end_ts); folds later slice these single runs.

    Circuit breakers are DISABLED here (breakers.enabled = False): they are a risk overlay validated
    in the holdout and in paper mode, and a TOTAL_HALT latched early in this single continuous run
    would zero the trades of every later fold. The caller's cfg is not modified."""
    cfg = with_overrides(cfg, **{"breakers.enabled": False})
    combos = grid_combos()
    out: dict[Combo, EngineResult] = {}
    initargs = (cfg, df1h, start_ts, end_ts)
    if workers <= 1:
        _init_worker(*initargs)
        for n, c in enumerate(combos, 1):
            out[c] = _run_combo(c)[1]
            print(f"[grid] {n}/{len(combos)} {c}", file=sys.stderr, flush=True)
    else:
        with mp.Pool(min(workers, len(combos)), initializer=_init_worker, initargs=initargs) as pool:
            for n, (c, res) in enumerate(pool.imap_unordered(_run_combo, combos), 1):
                out[c] = res
                print(f"[grid] {n}/{len(combos)} {c}", file=sys.stderr, flush=True)
    return {c: out[c] for c in combos}


# ----------------------------------------------------------------------------- folds


def _add_months(ts: int, months: int) -> int:
    dt = datetime.fromtimestamp(ts, UTC)
    if dt.day != 1 or dt.hour or dt.minute or dt.second:
        raise ValueError("fold boundaries must be first-of-month midnight UTC")
    y, m = divmod(dt.year * 12 + (dt.month - 1) + months, 12)
    return int(datetime(y, m + 1, 1, tzinfo=UTC).timestamp())


def folds(
    train_months: int = 24,
    test_months: int = 6,
    first_test: str = "2019-01-01",
    last_test_end: str = "2024-01-01",
) -> list[tuple[int, int, int, int]]:
    """[(train_start, train_end, test_start, test_end)] as epoch seconds, rolling by test_months.
    Defaults give the 10 folds of spec 9.3 (test windows 2019H1 .. 2023H2)."""
    out: list[tuple[int, int, int, int]] = []
    test_start, limit = utc_ts(first_test), utc_ts(last_test_end)
    while True:
        test_end = _add_months(test_start, test_months)
        if test_end > limit:
            return out
        out.append((_add_months(test_start, -train_months), test_start, test_start, test_end))
        test_start = test_end


# ----------------------------------------------------------------------------- plateau


def _grid_index(combo: Combo) -> tuple[int, ...]:
    return tuple(axis.index(v) for axis, v in zip(GRID.values(), combo))


def pick_plateau(
    calmar: Mapping[Combo, float], n_trades: Mapping[Combo, int], min_trades: int = MIN_TRAIN_TRADES
) -> Combo:
    """Score(c) = mean Calmar over c's grid neighbours (Chebyshev index distance <= 1 on every
    axis, c included), counting only combos with >= min_trades trades. Highest score wins; ties
    go to the higher own Calmar, then to the smallest grid index. Raises ValueError if no combo
    has enough trades."""
    valid = {
        c: float(v) for c, v in calmar.items() if n_trades.get(c, 0) >= min_trades and math.isfinite(v)
    }
    if not valid:
        raise ValueError(f"no grid combo has >= {min_trades} trades in the training window")
    idx = {c: _grid_index(c) for c in valid}
    best: tuple[tuple[float, float], Combo] | None = None
    for c in sorted(valid, key=lambda k: idx[k]):
        near = [valid[o] for o in valid if max(abs(a - b) for a, b in zip(idx[c], idx[o])) <= 1]
        key = (round(float(np.mean(near)), 12), round(valid[c], 12))
        if best is None or key > best[0]:
            best = (key, c)
    assert best is not None
    return best[1]


def _train_scores(
    results: Mapping[Combo, EngineResult], train_start: int, train_end: int
) -> tuple[dict[Combo, float], dict[Combo, int]]:
    calmar: dict[Combo, float] = {}
    ntr: dict[Combo, int] = {}
    for c, res in results.items():
        m = compute_metrics(res.equity, res.trades, train_start, train_end)
        calmar[c], ntr[c] = m.calmar, m.n_trades
    return calmar, ntr


def select_plateau(
    results: Mapping[Combo, EngineResult], train_start: int, train_end: int, min_trades: int = MIN_TRAIN_TRADES
) -> Combo:
    calmar, ntr = _train_scores(results, train_start, train_end)
    return pick_plateau(calmar, ntr, min_trades)


# ----------------------------------------------------------------------------- stitching


def oos_returns(res: EngineResult, ts0: int, ts1: int) -> list[tuple[int, float]]:
    """Hourly returns of the equity points with ts0 <= ts < ts1 (the first one is measured
    against the last point before ts0, so consecutive folds join without a gap)."""
    out: list[tuple[int, float]] = []
    eq = res.equity
    for i in range(1, len(eq)):
        t = eq[i].ts
        if t < ts0:
            continue
        if t >= ts1:
            break
        prev = eq[i - 1].equity
        out.append((t, eq[i].equity / prev - 1.0 if prev > 0 else 0.0))
    return out


def evaluate_folds(
    results: Mapping[Combo, EngineResult],
    fold_list: Sequence[tuple[int, int, int, int]],
    equity0: float,
    min_trades: int = MIN_TRAIN_TRADES,
) -> dict[str, Any]:
    """Pick the plateau combo on each training window, then stitch each fold's OOS slice of
    that combo's (single-run) equity returns into one compounded equity curve. A fold with no
    eligible combo stays flat (chosen = None)."""
    rows: list[dict[str, Any]] = []
    stitched: list[EquityPoint] = []
    oos_trades = []
    cur = float(equity0)
    if fold_list:
        stitched.append(EquityPoint(ts=fold_list[0][2] - H1, equity=cur))
    chosen_final: Combo | None = None
    for tr0, tr1, te0, te1 in fold_list:
        calmar, ntr = _train_scores(results, tr0, tr1)
        try:
            chosen: Combo | None = pick_plateau(calmar, ntr, min_trades)
        except ValueError:
            chosen = None
        row: dict[str, Any] = {
            "train": (tr0, tr1),
            "test": (te0, te1),
            "chosen": chosen,
            "train_calmar": calmar[chosen] if chosen is not None else None,
            "oos": None,
        }
        if chosen is None:
            stitched.extend(EquityPoint(ts=t, equity=cur) for t in range(te0, te1, H1))
        else:
            res = results[chosen]
            for t, r in oos_returns(res, te0, te1):
                cur *= 1.0 + r
                stitched.append(EquityPoint(ts=t, equity=cur))
            oos_trades.extend(t for t in res.trades if te0 <= t.entry_ts < te1)
            row["oos"] = dataclasses.asdict(compute_metrics(res.equity, res.trades, te0, te1))
            chosen_final = chosen
        rows.append(row)
    return {
        "keys": list(GRID),
        "folds": rows,
        "stitched_equity": [(p.ts, p.equity) for p in stitched],
        "stitched_metrics": dataclasses.asdict(compute_metrics(stitched, oos_trades)),
        "oos_trades": oos_trades,
        "chosen_final": chosen_final,
    }


def walk_forward(cfg: BotConfig, df1h: pd.DataFrame, workers: int) -> dict[str, Any]:
    """Full walk-forward over [2017-01-01, 2024-01-01). Nothing from the holdout is used.
    Grid runs have circuit breakers disabled (see run_grid); folds need MIN_TRAIN_TRADES trades."""
    start_ts, end_ts = utc_ts(WF_START), utc_ts(WF_END)
    results = run_grid(cfg, df1h, start_ts, end_ts, workers)
    out = evaluate_folds(results, folds(), cfg.initial_equity)
    full = {c: compute_metrics(r.equity, r.trades, start_ts, end_ts) for c, r in results.items()}
    out["combo_table"] = [
        {"combo": c, "calmar": m.calmar, "sharpe": m.sharpe, "n_trades": m.n_trades} for c, m in full.items()
    ]
    return out
