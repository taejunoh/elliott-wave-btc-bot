"""Stage 1 of A-ETH (Plan 5a spec 6): strategy A, frozen, on ETH-USD 2017-01-01..2023-12-31 - one attempt, no search.

run_stage1 runs strategy A's compare() - A, C, HYBRID, buy & hold, EMA 50/200 and the 100-run random-entry control
with A's trade geometry, all with the ETH costs - over [2017-01-01, 2024-01-01), and, for information only, the same
comparison over 2019-01-01..2023-12-31 and A alone with fees and slippage x 2. stage1_verdict is the pass rule:
A's net total return > 0, A's max drawdown <= 15%, and A's Sharpe and Calmar each above C's and above the random
control's mean. write_stage1 runs it ONCE and writes aeth-stage1.json; it refuses when that record exists, when
tracked files have uncommitted changes, when strategy A's fingerprint is not the frozen e0435223..., and when the
bars do not cover 2017..2023 or reach 2024. The record carries what it ran on - the params and the sha256 of
A's holdout.lock, the config, the bars (data_record), the fingerprints, the git state - so the lock can check it
(holdout.create_lock)."""

from __future__ import annotations

import dataclasses
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ewbot.aeth.config import (
    A_LOCK_PATH,
    COST_INFO_MULT,
    DATA_START,
    FROZEN_A_FINGERPRINT,
    FUTURE,
    PRODUCT,
    RANDOM_RUNS,
    STAGE1_PATH,
    SUB_START,
    AethError,
    Combo,
    aeth_config,
    config_json,
    frozen_a_params,
    scale_trading_costs,
)
from ewbot.aeth.fingerprint import aeth_fingerprint
from ewbot.aeth.lockfile import refuse_dirty, write_json
from ewbot.backtest.holdout import code_fingerprint, data_fingerprint, git_state
from ewbot.backtest.metrics import compute_metrics
from ewbot.backtest.report import to_jsonable
from ewbot.backtest.runner import compare, run_backtest
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.data.candles import check_integrity, fill_gaps
from ewbot.paper.fingerprint import current_fingerprint
from ewbot.types import H1

STAGE1_KIND = "ewbot-aeth-stage1"
MAX_DD = 0.15
COVER_SLACK_S = 86_400  # the bars must start within a day of the window start and end within a day of 2024-01-01
_STRATEGIES = ("A", "HYBRID", "C", "BH", "EMA")
_KEYS = ("total_return", "cagr", "max_dd", "sharpe", "calmar", "win_rate", "avg_r", "n_trades")


def headline(cmp: dict[str, Any]) -> dict[str, Any]:
    """The comparison's metrics per strategy and the random control's distribution, as strict JSON."""
    comp = cmp["comparison"]
    head: dict[str, Any] = {n: {k: comp[n][k] for k in _KEYS} for n in _STRATEGIES if n in comp}
    rnd = cmp.get("random") or {}
    dist = rnd.get("dist") or {}
    head["RANDOM"] = {"runs": int(rnd.get("runs", 0)), "n_signals": int(rnd.get("n_signals", 0)),
                      **{k: dist[k] for k in ("sharpe", "calmar", "total_return") if k in dist}}
    return to_jsonable(head)


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def _gt(x: Any, y: Any) -> bool:
    return _num(x) and _num(y) and x > y


def stage1_verdict(head: dict[str, Any]) -> dict[str, Any]:
    """Spec 6: every check must hold. A missing or non-finite number fails its check."""
    a, c = head["A"], head["C"]
    rnd = head.get("RANDOM") or {}
    r_sharpe = (rnd.get("sharpe") or {}).get("mean")
    r_calmar = (rnd.get("calmar") or {}).get("mean")
    checks = {
        "total_return_gt_0": _gt(a["total_return"], 0.0),
        "max_dd_le_15pct": _num(a["max_dd"]) and a["max_dd"] <= MAX_DD,
        "sharpe_gt_c": _gt(a["sharpe"], c["sharpe"]),
        "calmar_gt_c": _gt(a["calmar"], c["calmar"]),
        "sharpe_gt_random_mean": _gt(a["sharpe"], r_sharpe),
        "calmar_gt_random_mean": _gt(a["calmar"], r_calmar),
    }
    return {"checks": checks, "passed": all(checks.values())}


def run_stage1(
    df1h: pd.DataFrame,
    params: Combo,
    *,
    random_runs: int = RANDOM_RUNS,
    workers: int = 1,
    start_ts: int = DATA_START,
    sub_start_ts: int = SUB_START,
    end_ts: int = HOLDOUT_START,
) -> dict[str, Any]:
    """The stage-1 comparisons on gap-filled bars that all end before 2024-01-01."""
    if end_ts > HOLDOUT_START or (len(df1h) and int(df1h["ts"].max()) >= HOLDOUT_START):
        raise AethError("stage 1 reads ETH bars before 2024-01-01 only")
    cfg = aeth_config(params)
    full = compare(cfg, df1h, start_ts, end_ts, random_runs=random_runs, workers=workers)
    sub = compare(cfg, df1h, sub_start_ts, end_ts, random_runs=random_runs, workers=workers)
    dear = dataclasses.replace(cfg, costs=scale_trading_costs(cfg.costs, COST_INFO_MULT))
    res = run_backtest(dear, df1h, start_ts, end_ts)
    dear_m = compute_metrics(res.equity, res.trades, start_ts, end_ts)
    head, sub_head = headline(full), headline(sub)
    return {
        "kind": STAGE1_KIND,
        "version": 1,
        "product": PRODUCT,
        "future": FUTURE,
        "params": [float(p) for p in params],
        "config": config_json(params),
        "window": [int(start_ts), int(end_ts)],
        "random_runs": int(random_runs),
        "headline": head,
        "criteria": stage1_verdict(head),
        "per_year_A": to_jsonable(full["breakdown"]["A"]["per_year"]),
        "info": {
            "sub_period": {"window": [int(sub_start_ts), int(end_ts)], "headline": sub_head,
                           "checks_info_only": stage1_verdict(sub_head)},
            "cost_x2": {"fee_rate": dear.costs.fee_rate, "slippage": dear.costs.slippage,
                        "A": to_jsonable(dataclasses.asdict(dear_m))},
        },
    }


def data_record(raw: pd.DataFrame) -> dict[str, Any]:
    """The cached (not gap-filled) ETH bars before 2024-01-01: first and last ts, count, missing hours, sha256
    (A's data_fingerprint)."""
    w = raw[raw["ts"] < HOLDOUT_START].reset_index(drop=True)
    return {
        "first_ts": int(w["ts"].iloc[0]) if len(w) else None,
        "last_ts": int(w["ts"].iloc[-1]) if len(w) else None,
        "n_bars": int(len(w)),
        "missing": len(check_integrity(w).missing_ts),
        "sha256": data_fingerprint(w, 0, HOLDOUT_START - 1),
    }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def write_stage1(
    raw: pd.DataFrame,
    *,
    out_path: Path = STAGE1_PATH,
    a_lock_path: Path = A_LOCK_PATH,
    random_runs: int = RANDOM_RUNS,
    workers: int = 1,
    start_ts: int = DATA_START,
    sub_start_ts: int = SUB_START,
    run: Callable[..., dict[str, Any]] = run_stage1,
    a_fingerprint: Callable[[], str] = current_fingerprint,
) -> dict[str, Any]:
    """Stage 1, once: refuse, or run on the cached bars `raw` (before 2024) and write the record to out_path."""
    out = Path(out_path)
    if out.exists():
        raise AethError(f"{out} exists: stage 1 runs once (spec 6) - no rerun, whatever its result")
    a_fp = a_fingerprint()
    if a_fp != FROZEN_A_FINGERPRINT:
        raise AethError(f"strategy A's fingerprint is {a_fp[:12]}, not the frozen {FROZEN_A_FINGERPRINT[:12]}: "
                        "refusing")
    refuse_dirty(out)
    params, a_sha = frozen_a_params(a_lock_path)
    if raw.empty or int(raw["ts"].max()) >= HOLDOUT_START:
        raise AethError("stage 1 needs the cached ETH bars before 2024-01-01 (and none after)")
    first, last = int(raw["ts"].min()), int(raw["ts"].max())
    if first > start_ts + COVER_SLACK_S or last < HOLDOUT_START - H1 - COVER_SLACK_S:
        raise AethError(f"the ETH bars span {first}..{last}, not {start_ts}..{HOLDOUT_START - H1}: "
                        "run `ewbot aeth fetch` to completion first")
    started = _now()
    rec = run(fill_gaps(raw), params, random_runs=random_runs, workers=workers, start_ts=start_ts,
              sub_start_ts=sub_start_ts)
    rec["provenance"] = {
        "a_lock_sha256": a_sha,
        "a_fingerprint": a_fp,
        "aeth_sha256": aeth_fingerprint(),
        "code_sha256": code_fingerprint(),
        "data": data_record(raw),
        "started": started,
        "completed": _now(),
        **git_state(),
    }
    write_json(out, rec)
    return rec
