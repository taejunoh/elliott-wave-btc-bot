"""Strategy-B research runs (Plan 4 spec 6.2-6.3): one backtest, the 32-combo grid, the walk-forward. Every
run is one portfolio over BTC and ETH ({asset: 15m bars}; one shared combo, engine.run_engine), and the
walk-forward scores the combined portfolio (spec 6.3 as revised 2026-10-07).

The walk-forward uses strategy A's rules (ewbot.backtest.walkforward, read 2026-10-07):
  - windows: A's folds() - 24-month training, 6-month test, rolling by 6 months, tests 2019-01 .. 2023-12
    (10 folds), all inside [WF_START, WF_END) = [2017-01-01, 2024-01-01);
  - every combo runs ONCE over that span with the circuit breakers disabled; the folds slice the runs;
  - eligible in a training window: >= MIN_TRAIN_TRADES (12) trades and a finite Calmar (A's metrics);
  - score: mean Calmar over the combo's grid neighbours (index distance <= 1 on every axis, itself
    included), eligible ones only; selectable only when >= MIN_ELIGIBLE_NEIGHBOUR_FRAC (50%) of its
    in-grid neighbours are eligible; highest score, ties -> higher own Calmar -> smallest grid index;
  - stitched out-of-sample curve: each fold's test-window hourly returns of its pick, compounded;
  - chosen_final: the pick of the last fold that picked (chosen_final_fallback when not the last fold).
A's pick_plateau reads A's own GRID, so the identical rule is implemented here over GRID_B. With two
values per axis every combo neighbours every other: the score is the same for all, the pick is the
highest own Calmar, and it needs >= 17 of the 32 combos eligible. Spec 6.3: a negative stitched
out-of-sample return ends strategy B (proceed = False); so does a fallback pick (A's CLI refuses it too)."""

from __future__ import annotations

import dataclasses
import math
import multiprocessing as mp
import sys
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from ewbot.backtest.metrics import compute_metrics
from ewbot.backtest.runner import utc_ts
from ewbot.backtest.walkforward import (
    HOLDOUT_START,
    MIN_ELIGIBLE_NEIGHBOUR_FRAC,
    MIN_TRAIN_TRADES,
    WF_START,
    folds,
    oos_returns,
)
from ewbot.engine.loop import EquityPoint, Trade
from ewbot.stratb.config import (
    ASSETS,
    GRID_B,
    BConfig,
    Combo,
    StratBParams,
    combo_to_json,
    config_for,
    grid_combos,
)
from ewbot.stratb.engine import BResult, asset_of, run_engine
from ewbot.stratb.signals import Frame15, RefLeg, build_frames, generate_signals, reference_legs
from ewbot.types import H1


def refuse_holdout_bars(df15: pd.DataFrame, end_ts: int | None, include_holdout: bool = False) -> None:
    """ValueError when a run over df15 up to end_ts (exclusive; None = every bar) would see a bar at or
    after HOLDOUT_START, unless include_holdout (only the B holdout and the paper-B runner pass it)."""
    if include_holdout:
        return
    if end_ts is None:
        if len(df15) and int(df15["ts"].max()) >= HOLDOUT_START:
            raise ValueError("df15 has bars from 2024-01-01 on and no end_ts: research runs end by HOLDOUT_START")
    elif end_ts > HOLDOUT_START:
        raise ValueError(f"end_ts {end_ts} reaches into the holdout (HOLDOUT_START = {HOLDOUT_START})")


def _cut(dfs: Mapping[str, pd.DataFrame], end_ts: int | None) -> dict[str, pd.DataFrame]:
    return {a: (df if end_ts is None else df[df["ts"] < end_ts]).reset_index(drop=True) for a, df in dfs.items()}


def backtest_b(
    dfs: Mapping[str, pd.DataFrame],
    cfg: BConfig,
    start_ts: int | None = None,
    end_ts: int | None = None,
    *,
    include_holdout: bool = False,
) -> BResult:
    """One strategy-B run over every asset's bars ({asset: 15m bars}) before end_ts; entries from start_ts."""
    for df in dfs.values():
        refuse_holdout_bars(df, end_ts, include_holdout)
    frames = build_frames(_cut(dfs, end_ts), cfg.params)
    return run_engine(frames, {a: generate_signals(f, cfg.params) for a, f in frames.items()}, cfg, start_ts)


def trades_by_asset(trades: Sequence[Trade]) -> dict[str, int]:
    """{asset: number of trades} in config.ASSETS order."""
    return {a: sum(1 for t in trades if asset_of(t) == a) for a in ASSETS}


# ----------------------------------------------------------------------------- grid

_POOL: dict[str, Any] = {}  # per process: the frames, the start and the legs per (asset, zz_k)


def _pool_init(frames: dict[str, Frame15], start_ts: int) -> None:
    _POOL.clear()
    _POOL.update(frames=frames, start=start_ts, legs={})


def _pool_run(combo: Combo) -> tuple[Combo, BResult]:
    cfg = config_for(combo, breakers_enabled=False)
    frames: dict[str, Frame15] = _POOL["frames"]
    legs: dict[tuple[str, float], list[RefLeg]] = _POOL["legs"]
    sigs = {}
    for a, f in frames.items():
        key = (a, cfg.params.zz_k)
        if key not in legs:
            legs[key] = reference_legs(f, cfg.params.zz_k, cfg.params)
        sigs[a] = generate_signals(f, cfg.params, legs[key])
    return combo, run_engine(frames, sigs, cfg, _POOL["start"])


def run_grid_b(
    dfs: Mapping[str, pd.DataFrame], start_ts: int, end_ts: int, workers: int = 1
) -> dict[Combo, BResult]:
    """Every grid combo once over every asset's bars before end_ts (refused past HOLDOUT_START), breakers off."""
    for df in dfs.values():
        refuse_holdout_bars(df, end_ts)
    frames = build_frames(_cut(dfs, end_ts), StratBParams())
    combos = grid_combos()
    out: dict[Combo, BResult] = {}
    if workers <= 1:
        _pool_init(frames, start_ts)
        for n, c in enumerate(combos, 1):
            out[c] = _pool_run(c)[1]
            print(f"[grid-b] {n}/{len(combos)} {combo_to_json(c)}", file=sys.stderr, flush=True)
    else:
        with mp.Pool(min(workers, len(combos)), initializer=_pool_init, initargs=(frames, start_ts)) as pool:
            for n, (c, res) in enumerate(pool.imap_unordered(_pool_run, combos), 1):
                out[c] = res
                print(f"[grid-b] {n}/{len(combos)} {combo_to_json(c)}", file=sys.stderr, flush=True)
    return {c: out[c] for c in combos}


# ----------------------------------------------------------------------------- plateau


def _index(combo: Combo) -> tuple[int, ...]:
    return tuple(axis.index(v) for axis, v in zip(GRID_B.values(), combo))


def _n_neighbours(index: tuple[int, ...]) -> int:
    n = 1
    for i, axis in zip(index, GRID_B.values()):
        n *= min(i + 1, len(axis) - 1) - max(i - 1, 0) + 1
    return n - 1


def _tie(x: float) -> float:
    return float(f"{x:.12g}")


def pick_plateau_b(
    calmar: Mapping[Combo, float], n_trades: Mapping[Combo, int], min_trades: int = MIN_TRAIN_TRADES
) -> Combo:
    """A's pick_plateau rule over GRID_B (see the module docstring). ValueError if nothing is selectable."""
    valid = {c: float(v) for c, v in calmar.items() if n_trades.get(c, 0) >= min_trades and math.isfinite(v)}
    if not valid:
        raise ValueError(f"no grid combo has >= {min_trades} trades in the training window")
    idx = {c: _index(c) for c in valid}
    best: tuple[tuple[float, float], Combo] | None = None
    for c in sorted(valid, key=lambda k: idx[k]):
        near = [o for o in valid if max(abs(a - b) for a, b in zip(idx[c], idx[o])) <= 1]
        if len(near) - 1 < MIN_ELIGIBLE_NEIGHBOUR_FRAC * _n_neighbours(idx[c]):
            continue
        key = (_tie(float(np.mean([valid[o] for o in near]))), _tie(valid[c]))
        if best is None or key > best[0]:
            best = (key, c)
    if best is None:
        raise ValueError(
            f"no combo with >= {min_trades} trades has {MIN_ELIGIBLE_NEIGHBOUR_FRAC:.0%} of its neighbours eligible"
        )
    return best[1]


# ----------------------------------------------------------------------------- folds


def evaluate_folds_b(
    results: Mapping[Combo, BResult],
    fold_list: Sequence[tuple[int, int, int, int]],
    equity0: float,
    min_trades: int = MIN_TRAIN_TRADES,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    stitched: list[EquityPoint] = []
    oos_trades = []
    cur = float(equity0)
    if fold_list:
        stitched.append(EquityPoint(ts=fold_list[0][2] - H1, equity=cur))
    chosen_final: Combo | None = None
    final_fold: int | None = None
    for k, (tr0, tr1, te0, te1) in enumerate(fold_list):
        cal: dict[Combo, float] = {}
        ntr: dict[Combo, int] = {}
        for c, res in results.items():
            m = compute_metrics(res.equity, res.trades, tr0, tr1)
            cal[c], ntr[c] = m.calmar, m.n_trades
        try:
            chosen: Combo | None = pick_plateau_b(cal, ntr, min_trades)
        except ValueError:
            chosen = None
        row: dict[str, Any] = {"train": (tr0, tr1), "test": (te0, te1), "chosen": chosen,
                               "train_calmar": cal[chosen] if chosen is not None else None, "oos": None}
        if chosen is None:
            stitched.extend(EquityPoint(ts=t, equity=cur) for t in range(te0, te1, H1))
        else:
            res = results[chosen]
            for t, r in oos_returns(res, te0, te1):
                cur *= 1.0 + r
                stitched.append(EquityPoint(ts=t, equity=cur))
            oos_trades.extend(t for t in res.trades if te0 <= t.entry_ts < te1)
            row["oos"] = dataclasses.asdict(compute_metrics(res.equity, res.trades, te0, te1))
            chosen_final, final_fold = chosen, k
        rows.append(row)
    fallback = final_fold is not None and final_fold != len(fold_list) - 1
    metrics = dataclasses.asdict(compute_metrics(stitched, oos_trades))
    proceed = chosen_final is not None and not fallback and metrics["total_return"] >= 0.0
    return {
        "keys": list(GRID_B),
        "folds": rows,
        "stitched_equity": [(p.ts, p.equity) for p in stitched],
        "stitched_metrics": metrics,
        "oos_trades": oos_trades,
        "chosen_final": chosen_final,
        "chosen_final_from_fold": final_fold,
        "chosen_final_fallback": fallback,
        "proceed": proceed,
    }


def walk_forward_b(dfs: Mapping[str, pd.DataFrame], workers: int = 1) -> dict[str, Any]:
    """The full walk-forward over [2017-01-01, 2024-01-01) on the BTC + ETH portfolio (both assets
    required). Bars at/after 2024-01-01 are dropped here."""
    if sorted(dfs) != sorted(ASSETS):
        raise ValueError(f"the walk-forward runs on every asset {list(ASSETS)}, got {sorted(dfs)}")
    start, end = utc_ts(WF_START), HOLDOUT_START
    results = run_grid_b(_cut(dfs, end), start, end, workers)
    out = evaluate_folds_b(results, folds(), BConfig().initial_equity)
    full = {c: compute_metrics(r.equity, r.trades, start, end) for c, r in results.items()}
    out["combo_table"] = [
        {"combo": combo_to_json(c), "calmar": m.calmar, "sharpe": m.sharpe, "n_trades": m.n_trades,
         "n_trades_by_asset": trades_by_asset([t for t in results[c].trades if start <= t.entry_ts < end]),
         "total_return": m.total_return}
        for c, m in full.items()
    ]
    return out
