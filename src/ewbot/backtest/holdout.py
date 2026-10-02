"""One-shot holdout [WF_END, end) behind a lock file bound to the full run configuration."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from ewbot.backtest.runner import compare, utc_ts
from ewbot.backtest.walkforward import GRID, WF_END, config_for, grid_combos
from ewbot.config import CONSERVATIVE, BotConfig, with_overrides
from ewbot.types import H1

HOLDOUT_START = utc_ts(WF_END)  # single source: the holdout starts exactly where the walk-forward ends
GO_LIVE_MIN_TRADES = 50  # spec 9.6 criterion 3: out-of-sample trades with frozen params (holdout + paper)
# src/ewbot/backtest/holdout.py -> parents[3] is the repository root (src layout, editable install).
# The default lock lives there, never in the current working directory.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LOCK_PATH = PROJECT_ROOT / "holdout.lock"
LOCK_VERSION = 2
_CANDIDATES = ("A", "HYBRID")
_HEADLINE_STRATEGIES = ("A", "HYBRID", "C", "BH", "EMA")
_HEADLINE_KEYS = ("total_return", "cagr", "max_dd", "sharpe", "calmar", "n_trades")


class HoldoutError(RuntimeError):
    """The holdout may not be run: not confirmed, invalid setup, or locked to a different configuration."""


def params_digest(params: Sequence[float]) -> str:
    """sha256 of the grid params alone. Informational (stored as `params_sha256`); the lock is bound
    to `lock_digest`, which covers the whole configuration."""
    payload = json.dumps({"keys": list(GRID), "params": [float(p) for p in params]}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def holdout_config(cfg: BotConfig, params: Sequence[float]) -> BotConfig:
    """The fully resolved config of a holdout run: `cfg` with the grid params applied and
    strategy.mode pinned to "A" (compare() runs A, C and HYBRID itself, so the field has no effect)."""
    return with_overrides(config_for(cfg, tuple(float(p) for p in params)), **{"strategy.mode": "A"})


def lock_digest(cfg: BotConfig, params: Sequence[float], random_runs: int = 100) -> str:
    """sha256 of the sorted JSON of everything that determines the holdout result: every field of
    holdout_config(cfg, params), the grid values, the holdout start and the random-control size."""
    payload = {
        "config": dataclasses.asdict(holdout_config(cfg, params)),
        "grid": {k: list(v) for k, v in GRID.items()},
        "holdout_start": HOLDOUT_START,
        "random_runs": int(random_runs),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


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
    The random control is mandatory: without a random distribution (no runs, or A made no trades
    to replay) no candidate wins and `random_control_evaluated` is False.
    Criterion 3 counts all out-of-sample trades with frozen params (holdout + later paper);
    `n_trades` / `trade_count` state the holdout's count per candidate."""
    comp = cmp["comparison"]
    rnd = (cmp.get("random") or {}).get("dist") or {}
    random_ok = all(isinstance(rnd.get(k), dict) and "mean" in rnd[k] for k in ("sharpe", "calmar"))

    def beats(name: str) -> bool:
        if not random_ok:
            return False
        a = comp[name]
        for other in ("C", "BH"):
            if a["sharpe"] <= comp[other]["sharpe"] or a["calmar"] <= comp[other]["calmar"]:
                return False
        return a["sharpe"] > rnd["sharpe"]["mean"] and a["calmar"] > rnd["calmar"]["mean"]

    winners = [n for n in _CANDIDATES if n in comp and beats(n)]
    passing = [n for n in winners if comp[n]["max_dd"] <= 0.15 and comp[n]["n_trades"] >= GO_LIVE_MIN_TRADES]
    return {
        "winners": winners,
        "max_dd_ok": any(comp[n]["max_dd"] <= 0.15 for n in winners),
        "n_trades_ok": any(comp[n]["n_trades"] >= GO_LIVE_MIN_TRADES for n in winners),
        "n_trades": {n: int(comp[n]["n_trades"]) for n in _CANDIDATES if n in comp},
        "trade_count": trade_count_lines(comp),
        "random_control_evaluated": random_ok,
        "passing": passing,
        "paper_4_weeks": "manual check required",
        "automatic_criteria_met": bool(passing) and random_ok,
    }


# ----------------------------------------------------------------------------- lock file


def _validate(cfg: BotConfig, params: Sequence[float], random_runs: int) -> tuple[float, ...]:
    """Refuse a holdout setup that is not the one the walk-forward / go-live gate is about."""
    combo = tuple(float(p) for p in params)
    if combo not in grid_combos():
        raise HoldoutError(f"params {list(combo)} are not a combo of the walk-forward grid {list(GRID)} = {GRID}")
    if not cfg.breakers.enabled:
        raise HoldoutError("circuit breakers must be on in the holdout (breakers.enabled = True)")
    if cfg.risk != CONSERVATIVE:
        raise HoldoutError(f"the holdout runs the conservative risk profile only ({CONSERVATIVE}); got {cfg.risk}")
    if int(random_runs) < 1:
        raise HoldoutError("random_runs must be >= 1: beating the random-entry control is a go-live criterion")
    return combo


def _default_lock_path() -> Path:
    if not (PROJECT_ROOT / "pyproject.toml").is_file():
        raise HoldoutError(
            f"cannot locate the project root (no pyproject.toml in {PROJECT_ROOT}); pass lock_path explicitly"
        )
    return DEFAULT_LOCK_PATH


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read_lock(path: Path) -> dict[str, Any]:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HoldoutError(f"{path} exists but is unreadable ({exc}); refusing to run - inspect it by hand") from exc
    if not isinstance(lock, dict):
        raise HoldoutError(f"{path} exists but is not a holdout lock; refusing to run - inspect it by hand")
    return lock


def _write_lock(path: Path, lock: dict[str, Any]) -> None:
    """Atomic replace, so a crash mid-write cannot leave a truncated lock behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(lock, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _changed_keys(old: Any, new: Any, prefix: str = "") -> list[str]:
    """Dotted keys whose values differ between two JSON-like mappings (for the refusal message)."""
    if isinstance(old, dict) and isinstance(new, dict):
        return [d for k in sorted(set(old) | set(new)) for d in _changed_keys(old.get(k), new.get(k), f"{prefix}{k}.")]
    return [] if old == new else [prefix.rstrip(".") or "<root>"]


def _headline(cmp: dict[str, Any], criteria: dict[str, Any]) -> dict[str, Any]:
    comp = cmp["comparison"]
    head: dict[str, Any] = {
        n: {k: comp[n][k] for k in _HEADLINE_KEYS if k in comp[n]} for n in _HEADLINE_STRATEGIES if n in comp
    }
    rnd = cmp.get("random") or {}
    dist = rnd.get("dist") or {}
    head["RANDOM"] = {
        "runs": rnd.get("runs"),
        "n_signals": rnd.get("n_signals"),
        **{k: dist[k] for k in ("sharpe", "calmar", "total_return") if k in dist},
    }
    keep = ("winners", "passing", "n_trades", "random_control_evaluated", "automatic_criteria_met")
    return {"headline": head, "criteria": {k: criteria[k] for k in keep}}


# ----------------------------------------------------------------------------- run


def run_holdout(
    cfg: BotConfig,
    df1h: pd.DataFrame,
    params: Sequence[float],
    lock_path: Path | str | None = None,
    confirm: bool = False,
    *,
    random_runs: int = 100,
    workers: int = 1,
) -> dict[str, Any]:
    """Run A / C / HYBRID / BH / EMA / RANDOM on [HOLDOUT_START, last bar] with `params`
    (K4, Q_MIN, S_MIN), once per configuration.

    Checked before anything is written: confirm=True; params are a GRID combo; circuit breakers on;
    the conservative risk profile; random_runs >= 1; data at/after HOLDOUT_START.
    The lock (default DEFAULT_LOCK_PATH = <project root>/holdout.lock, independent of the CWD) is
    bound to lock_digest(cfg, params, random_runs) - every config field, the grid, the holdout start
    and the random-control size - and is written BEFORE the run, so a crash cannot be used to retune.
    Re-running the identical configuration is allowed (e.g. later, with more data); anything else is
    refused. The first completed run's window and headline metrics are recorded in the lock and
    never overwritten."""
    if not confirm:
        raise HoldoutError("the holdout is single-use: pass confirm=True (CLI: --confirm) to run it")
    combo = _validate(cfg, params, random_runs)
    ts = df1h["ts"]
    if ts.empty or int(ts.iloc[-1]) < HOLDOUT_START:
        raise HoldoutError(f"no data at or after {WF_END} in df1h")
    last_bar = int(ts.iloc[-1])
    end_ts = last_bar + H1
    lock_path = _default_lock_path() if lock_path is None else Path(lock_path)
    run_cfg = holdout_config(cfg, combo)
    digest = lock_digest(cfg, combo, random_runs)
    config_json = json.loads(json.dumps(dataclasses.asdict(run_cfg)))

    run: dict[str, Any] = {"started": _now(), "end_ts": end_ts, "last_bar_ts": last_bar, "completed": None}
    if lock_path.exists():
        lock = _read_lock(lock_path)
        if lock.get("sha256") != digest:
            changed = _changed_keys(
                {"params": lock.get("params"), "config": lock.get("config"), "random_runs": lock.get("random_runs")},
                {"params": list(combo), "config": config_json, "random_runs": int(random_runs)},
            )
            raise HoldoutError(
                f"{lock_path} is locked to params {lock.get('params')} (sha256 {str(lock.get('sha256'))[:12]}); "
                f"refusing to run a different configuration (params {list(combo)}; changed: {changed or 'unknown'})"
            )
        lock.setdefault("runs", []).append(run)
    else:
        lock = {
            "version": LOCK_VERSION,
            "keys": list(GRID),
            "params": list(combo),
            "sha256": digest,
            "params_sha256": params_digest(combo),
            "config": config_json,
            "grid": {k: list(v) for k, v in GRID.items()},
            "holdout_start": HOLDOUT_START,
            "random_runs": int(random_runs),
            "first_run": None,
            "runs": [run],
        }
    _write_lock(lock_path, lock)

    cmp = compare(run_cfg, df1h, HOLDOUT_START, end_ts, random_runs=random_runs, workers=workers)
    criteria = evaluate_criteria(cmp)

    run["completed"] = _now()
    if lock.get("first_run") is None:
        lock["first_run"] = {
            "started": run["started"],
            "completed": run["completed"],
            "start_ts": HOLDOUT_START,
            "end_ts": end_ts,
            "last_bar_ts": last_bar,
            **_headline(cmp, criteria),
        }
    _write_lock(lock_path, lock)
    return {
        "params": list(combo),
        "sha256": digest,
        "lock_path": str(lock_path),
        "window": (HOLDOUT_START, end_ts),
        "compare": cmp,
        "criteria": criteria,
    }
