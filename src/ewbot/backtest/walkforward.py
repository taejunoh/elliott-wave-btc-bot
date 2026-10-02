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
# Single source of the walk-forward / holdout boundary: the holdout starts here, walk_forward() drops
# every bar at/after it before anything runs, runner.run_backtest / compare refuse to cross it unless
# holdout.run_holdout asks them to, and every CLI command except `holdout` cuts its data here.
WF_END = "2024-01-01"
HOLDOUT_START = utc_ts(WF_END)
# spec 9.3: a combo needs this many trades in a 24-month training window. A degeneracy floor for
# the Calmar objective, not a power criterion (2017-2023: about 8-13 trades/year).
MIN_TRAIN_TRADES = 12
# A combo is selectable only if at least this fraction of its in-grid neighbours (itself excluded)
# is eligible too, so a lone eligible cell cannot "win" a plateau made of itself.
MIN_ELIGIBLE_NEIGHBOUR_FRAC = 0.5

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

    Bars at/after end_ts are dropped before any run (the engine never sees them).
    Circuit breakers are DISABLED here (breakers.enabled = False): they are a risk overlay validated
    in the holdout and in paper mode, and a TOTAL_HALT latched early in this single continuous run
    would zero the trades of every later fold. The caller's cfg and frame are not modified."""
    cfg = with_overrides(cfg, **{"breakers.enabled": False})
    df1h = df1h[df1h["ts"] < end_ts].reset_index(drop=True)
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
    last_test_end: str = WF_END,
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


def _n_grid_neighbours(index: tuple[int, ...]) -> int:
    """Number of grid cells at Chebyshev index distance exactly 1 from `index` (edges clipped)."""
    n = 1
    for i, axis in zip(index, GRID.values()):
        n *= min(i + 1, len(axis) - 1) - max(i - 1, 0) + 1
    return n - 1


def _tie_key(x: float) -> float:
    """x rounded to 12 significant digits: equal means of differently sized sets compare equal."""
    return float(f"{x:.12g}")


def pick_plateau(
    calmar: Mapping[Combo, float], n_trades: Mapping[Combo, int], min_trades: int = MIN_TRAIN_TRADES
) -> Combo:
    """Score(c) = mean Calmar over c's grid neighbours (Chebyshev index distance <= 1 on every
    axis, c included), counting only eligible combos (>= min_trades trades, finite Calmar). A combo
    is selectable only if it is eligible and at least MIN_ELIGIBLE_NEIGHBOUR_FRAC of its in-grid
    neighbours (itself excluded) are eligible. Highest score wins; ties go to the higher own Calmar,
    then to the smallest grid index. Raises ValueError if no combo is selectable."""
    valid = {
        c: float(v) for c, v in calmar.items() if n_trades.get(c, 0) >= min_trades and math.isfinite(v)
    }
    if not valid:
        raise ValueError(f"no grid combo has >= {min_trades} trades in the training window")
    idx = {c: _grid_index(c) for c in valid}
    best: tuple[tuple[float, float], Combo] | None = None
    for c in sorted(valid, key=lambda k: idx[k]):
        near = [o for o in valid if max(abs(a - b) for a, b in zip(idx[c], idx[o])) <= 1]
        if len(near) - 1 < MIN_ELIGIBLE_NEIGHBOUR_FRAC * _n_grid_neighbours(idx[c]):
            continue  # an (almost) isolated eligible cell is a spike, not a plateau
        key = (_tie_key(float(np.mean([valid[o] for o in near]))), _tie_key(valid[c]))
        if best is None or key > best[0]:
            best = (key, c)
    if best is None:
        raise ValueError(
            f"no grid combo with >= {min_trades} trades has at least {MIN_ELIGIBLE_NEIGHBOUR_FRAC:.0%} "
            "of its grid neighbours eligible"
        )
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
    selectable combo stays flat (chosen = None).

    chosen_final is the pick of the LAST fold that picked anything; chosen_final_from_fold is that
    fold's index and chosen_final_fallback is True when it is not the last fold (the most recent
    training window had no selectable combo - treat that pick with suspicion)."""
    rows: list[dict[str, Any]] = []
    stitched: list[EquityPoint] = []
    oos_trades = []
    cur = float(equity0)
    if fold_list:
        stitched.append(EquityPoint(ts=fold_list[0][2] - H1, equity=cur))
    chosen_final: Combo | None = None
    chosen_final_from_fold: int | None = None
    for k, (tr0, tr1, te0, te1) in enumerate(fold_list):
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
            chosen_final, chosen_final_from_fold = chosen, k
        rows.append(row)
    fallback = chosen_final_from_fold is not None and chosen_final_from_fold != len(fold_list) - 1
    return {
        "keys": list(GRID),
        "folds": rows,
        "stitched_equity": [(p.ts, p.equity) for p in stitched],
        "stitched_metrics": dataclasses.asdict(compute_metrics(stitched, oos_trades)),
        "oos_trades": oos_trades,
        "chosen_final": chosen_final,
        "chosen_final_from_fold": chosen_final_from_fold,
        "chosen_final_fallback": fallback,
    }


def walk_forward(cfg: BotConfig, df1h: pd.DataFrame, workers: int) -> dict[str, Any]:
    """Full walk-forward over [WF_START, WF_END) = [2017-01-01, 2024-01-01). Every bar at/after
    WF_END is dropped here, before anything runs, so nothing from the holdout can leak in.
    Grid runs have circuit breakers disabled (see run_grid); folds need MIN_TRAIN_TRADES trades."""
    start_ts, end_ts = utc_ts(WF_START), utc_ts(WF_END)
    df1h = df1h[df1h["ts"] < end_ts].reset_index(drop=True)
    results = run_grid(cfg, df1h, start_ts, end_ts, workers)
    out = evaluate_folds(results, folds(), cfg.initial_equity)
    full = {c: compute_metrics(r.equity, r.trades, start_ts, end_ts) for c, r in results.items()}
    out["combo_table"] = [
        {"combo": c, "calmar": m.calmar, "sharpe": m.sharpe, "n_trades": m.n_trades} for c, m in full.items()
    ]
    return out
