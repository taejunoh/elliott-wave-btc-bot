"""One-shot holdout [2024-01-01, end) with a parameter lock file."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from ewbot.backtest.runner import compare, utc_ts
from ewbot.backtest.walkforward import GRID, config_for
from ewbot.config import BotConfig
from ewbot.types import H1

HOLDOUT_START = utc_ts("2024-01-01")
GO_LIVE_MIN_TRADES = 50  # spec 9.6 criterion 3: out-of-sample trades with frozen params (holdout + paper)
_CANDIDATES = ("A", "HYBRID")


class HoldoutError(RuntimeError):
    """The holdout may not be run (not confirmed, or locked to different parameters)."""


def params_digest(params: Sequence[float]) -> str:
    payload = json.dumps({"keys": list(GRID), "params": [float(p) for p in params]}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def trade_count_lines(comp: dict[str, Any], required: int = GO_LIVE_MIN_TRADES) -> list[str]:
    """One line per go-live candidate in `comp`: its out-of-sample trade count against the spec 9.6
    minimum. Counts only, no performance statement. Below the minimum the holdout alone cannot meet
    criterion 3 and trades must keep accumulating in paper mode (Plan 2)."""
    lines: list[str] = []
    for name in _CANDIDATES:
        if name not in comp:
            continue
        n = int(comp[name]["n_trades"])
        if n < required:
            lines.append(f"{name}: 표본 외 거래 {n}/{required} — paper 모드로 누적 필요; 실거래 전환 기준 미충족")
        else:
            lines.append(f"{name}: 표본 외 거래 {n}/{required} — 거래 수 기준 충족")
    return lines


def evaluate_criteria(cmp: dict[str, Any]) -> dict[str, Any]:
    """Automatic part of the spec 9.6 go-live gate on the holdout window (criterion 4, 4 weeks of
    paper, is manual).

    A candidate (A or HYBRID) must beat C, BH and the random-control mean on BOTH Sharpe and
    Calmar; among those, one must also have max_dd <= 15% and >= GO_LIVE_MIN_TRADES trades.
    Criterion 3 counts all out-of-sample trades with frozen params (holdout + later paper);
    `n_trades` / `trade_count` state the holdout's count per candidate."""
    comp = cmp["comparison"]
    rnd = cmp["random"].get("dist", {})

    def beats(name: str) -> bool:
        a = comp[name]
        for other in ("C", "BH"):
            if a["sharpe"] <= comp[other]["sharpe"] or a["calmar"] <= comp[other]["calmar"]:
                return False
        if rnd:
            return a["sharpe"] > rnd["sharpe"]["mean"] and a["calmar"] > rnd["calmar"]["mean"]
        return True

    winners = [n for n in _CANDIDATES if n in comp and beats(n)]
    passing = [n for n in winners if comp[n]["max_dd"] <= 0.15 and comp[n]["n_trades"] >= GO_LIVE_MIN_TRADES]
    return {
        "winners": winners,
        "max_dd_ok": any(comp[n]["max_dd"] <= 0.15 for n in winners),
        "n_trades_ok": any(comp[n]["n_trades"] >= GO_LIVE_MIN_TRADES for n in winners),
        "n_trades": {n: int(comp[n]["n_trades"]) for n in _CANDIDATES if n in comp},
        "trade_count": trade_count_lines(comp),
        "passing": passing,
        "paper_4_weeks": "manual check required",
        "automatic_criteria_met": bool(passing),
    }


def run_holdout(
    cfg: BotConfig,
    df1h: pd.DataFrame,
    params: Sequence[float],
    lock_path: Path = Path("holdout.lock"),
    confirm: bool = False,
    *,
    random_runs: int = 100,
    workers: int = 1,
) -> dict[str, Any]:
    """Run A / C / HYBRID / BH / EMA / RANDOM on [2024-01-01, last bar] with `params`
    (K4, Q_MIN, S_MIN) exactly once per parameter set: the lock file stores the params and
    their sha256 and is written BEFORE the run, so a crash cannot be used to retune. Re-running
    identical params is allowed; different params are refused."""
    if not confirm:
        raise HoldoutError("the holdout is single-use: pass confirm=True (CLI: --confirm) to run it")
    if len(params) != len(GRID):
        raise ValueError(f"params must have {len(GRID)} values for {list(GRID)}")
    ts = df1h["ts"]
    if int(ts.iloc[-1]) < HOLDOUT_START:
        raise HoldoutError("no data at or after 2024-01-01 in df1h")
    end_ts = int(ts.iloc[-1]) + H1

    digest = params_digest(params)
    lock_path = Path(lock_path)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    if lock_path.exists():
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        if lock.get("sha256") != digest:
            raise HoldoutError(
                f"{lock_path} is locked to params {lock.get('params')} (sha256 {str(lock.get('sha256'))[:12]}); "
                f"refusing to run with different params {[float(p) for p in params]}"
            )
        lock["runs"].append(now)
    else:
        lock = {"keys": list(GRID), "params": [float(p) for p in params], "sha256": digest, "runs": [now]}
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(lock, indent=2), encoding="utf-8")

    cmp = compare(
        config_for(cfg, tuple(float(p) for p in params)),
        df1h,
        HOLDOUT_START,
        end_ts,
        random_runs=random_runs,
        workers=workers,
    )
    return {
        "params": [float(p) for p in params],
        "sha256": digest,
        "window": (HOLDOUT_START, end_ts),
        "compare": cmp,
        "criteria": evaluate_criteria(cmp),
    }
