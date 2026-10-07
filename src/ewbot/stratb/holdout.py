"""Strategy-B holdout lock and its one-shot run (Plan 4 spec 6.4-6.5).

create_lock() writes <project root>/holdout-b.lock BEFORE any 2024+ candle can be read (data15 refuses
them until a lock exists): the walk-forward's final combo, the full run config (assets included), the
grid, the B fingerprint, deps_sha256 (fingerprint.deps_fingerprint: the strategy-A modules B imports -
config defaults, costs, sizing, breakers, metrics, buy & hold, walk-forward windows, data hashing, ...),
the sha256 of each asset's pre-2024 15m bars the walk-forward used (wf_data: BTC and ETH), the holdout
start, the replay anchor (2023-10-01), the 1,000-run random control and its seed, and last_bar_ts - the
last 15m bar of the last complete hour at lock time, where the holdout ends (spec 6.4). It refuses when a
tracked file of the lock's git working tree has uncommitted changes. The user commits it.

run_holdout_b() runs the holdout ONCE after the user's approval (confirm=True) and records the result in
first_run. It refuses (HoldoutBError) unless: the lock's sha256 is the digest of its own bound fields and
equals the committed version's (git HEAD); first_run is empty there and in the file (one completed run,
ever: no rerun, no other combo); the running B fingerprint equals code_sha256 (no B code change after the
lock); the lock's keys, grid, config, holdout_start and replay_anchor are what this code writes for its
params (lock_mismatch_b); the strategy-A modules B imports hash to deps_sha256; no tracked file but the
lock itself has uncommitted changes; every asset's pre-2024 bars hash to its wf_data sha256; every
asset's data reaches last_bar_ts; an unfinished earlier attempt saw the same holdout bars. Each attempt
is recorded in the lock before it runs.

Criteria (spec 6.5 as revised 2026-10-07, on the BTC + ETH portfolio, all four): Sharpe above the 50/50
buy & hold and above the 95th percentile of the random control (each asset's own entry frequency); max
drawdown <= 15%; >= 50 trades of both assets together; total return > 0 with fees and slippage x 1.5."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from ewbot.backtest.holdout import data_fingerprint, git_state
from ewbot.backtest.metrics import Metrics, compute_metrics
from ewbot.backtest.report import to_jsonable
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.stratb.baselines import buy_hold_b, random_control
from ewbot.stratb.config import ASSETS, GRID_B, M15, combo_to_json, config_for, normalize_combo, scale_costs
from ewbot.stratb.data15 import DEFAULT_LOCK_PATH, LOCK_KIND, REPLAY_ANCHOR, cache_paths, load_15m, lock_digest_b
from ewbot.stratb.engine import run_engine
from ewbot.stratb.fingerprint import deps_fingerprint, stratb_fingerprint
from ewbot.stratb.research import backtest_b, trades_by_asset
from ewbot.stratb.signals import build_frames, generate_signals
from ewbot.types import H1

LOCK_VERSION = 1
RANDOM_RUNS = 1000
MAX_DD = 0.15
MIN_TRADES = 50
COST_MULT = 1.5


class HoldoutBError(RuntimeError):
    """The strategy-B lock cannot be written, or the holdout may not run."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write(path: Path, lock: dict[str, Any]) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(to_jsonable(lock), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_lock(path: Path) -> dict[str, Any]:
    try:
        lock = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HoldoutBError(f"{path} is missing or unreadable: {exc}") from exc
    if not isinstance(lock, dict) or lock.get("kind") != LOCK_KIND:
        raise HoldoutBError(f"{path} is not a strategy-B holdout lock")
    if lock_digest_b(lock) != lock.get("sha256"):
        raise HoldoutBError(f"{path}: sha256 is not the digest of its bound fields (edited by hand?)")
    return lock


def committed_lock(path: Path) -> dict[str, Any]:
    """The version of the lock in git HEAD (the lock must be committed before the holdout runs)."""
    p = Path(path)
    try:
        r = subprocess.run(["git", "-C", str(p.parent), "show", f"HEAD:./{p.name}"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HoldoutBError(f"git is not available to check {p}: {exc}") from exc
    if r.returncode != 0:
        raise HoldoutBError(f"{p} is not committed: commit holdout-b.lock before the holdout")
    try:
        return json.loads(r.stdout.decode("utf-8"))
    except ValueError as exc:
        raise HoldoutBError(f"the committed {p.name} is not JSON") from exc


def _dirty(path: Path) -> list[str]:
    """Tracked files of the lock's git working tree with uncommitted changes, the lock itself excepted."""
    p = Path(path)
    cmd = ["git", "-C", str(p.parent), "status", "--porcelain", "--untracked-files=no", "--", ":/", f":!{p.name}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HoldoutBError(f"git is not available to check the working tree: {exc}") from exc
    if r.returncode != 0:
        raise HoldoutBError(f"{p.parent} is not a git working tree (the lock is committed there): {r.stderr.strip()}")
    return [line[3:] for line in r.stdout.splitlines() if line.strip()]


def _refuse_dirty(path: Path) -> None:
    dirty = _dirty(path)
    if dirty:
        raise HoldoutBError(f"uncommitted changes to tracked files {dirty[:5]}{' ...' if len(dirty) > 5 else ''}: "
                            "the lock is written and the holdout runs on a clean tree (commit or discard them)")


def lock_mismatch_b(lock: Mapping[str, Any]) -> list[str]:
    """The lock fields that are not what this code writes for the lock's params: keys, grid, config
    (config_for(params)), holdout_start, replay_anchor; ["params"] when the params are not a grid combo."""
    try:
        combo = normalize_combo(lock.get("params") or [])
    except (TypeError, ValueError):
        return ["params"]
    want = {"keys": list(GRID_B), "grid": to_jsonable(GRID_B), "config": to_jsonable(dataclasses.asdict(config_for(combo))),
            "holdout_start": HOLDOUT_START, "replay_anchor": REPLAY_ANCHOR}
    return [k for k, v in want.items() if lock.get(k) != v]


def wf_data_record(dfs_wf: Mapping[str, pd.DataFrame]) -> dict[str, Any]:
    """{asset: {first_ts, last_ts, n_bars, sha256}} of every asset's walk-forward bars (all before 2024)."""
    if sorted(dfs_wf) != sorted(ASSETS):
        raise HoldoutBError(f"the lock needs the walk-forward bars of every asset {list(ASSETS)}, got {sorted(dfs_wf)}")
    out: dict[str, Any] = {}
    for a in ASSETS:
        df = dfs_wf[a]
        if df.empty:
            raise HoldoutBError(f"no {a} 15m bars before 2024-01-01")
        out[a] = {"first_ts": int(df["ts"].iloc[0]), "last_ts": int(df["ts"].iloc[-1]), "n_bars": int(len(df)),
                  "sha256": data_fingerprint(df, 0, HOLDOUT_START - 1)}
    return out


def data_fingerprint_b(dfs: Mapping[str, pd.DataFrame], start_ts: int, last_ts: int) -> str:
    """One sha256 over every asset's bars with start_ts <= ts <= last_ts: of the lines "<asset>:<A's
    data_fingerprint>" in config.ASSETS order (an asset without bars there hashes as an empty window)."""
    lines = "".join(f"{a}:{data_fingerprint(dfs[a], start_ts, last_ts)}\n" for a in ASSETS if a in dfs)
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def create_lock(
    params: Sequence[Any],
    dfs_wf: Mapping[str, pd.DataFrame],
    *,
    params_source: str,
    now_ts: int,
    lock_path: Path = DEFAULT_LOCK_PATH,
    random_runs: int = RANDOM_RUNS,
    random_seed: int = 0,
) -> dict[str, Any]:
    """Write the lock (once). dfs_wf: every asset's walk-forward 15m bars, all before 2024-01-01."""
    path = Path(lock_path)
    if path.exists():
        raise HoldoutBError(f"{path} exists: the strategy-B lock is written once")
    try:
        combo = normalize_combo(params)
    except ValueError as exc:
        raise HoldoutBError(str(exc)) from exc
    if any(len(df) and int(df["ts"].max()) >= HOLDOUT_START for df in dfs_wf.values()):
        raise HoldoutBError("the walk-forward data must end before 2024-01-01")
    last_bar = int(now_ts) // H1 * H1 - M15
    if last_bar < HOLDOUT_START:
        raise HoldoutBError("the lock must be written after 2024-01-01")
    if int(random_runs) < 1:
        raise HoldoutBError("random_runs must be >= 1 (criterion 1 compares with the random control)")
    _refuse_dirty(path)
    lock: dict[str, Any] = {
        "kind": LOCK_KIND,
        "version": LOCK_VERSION,
        "keys": list(GRID_B),
        "params": combo_to_json(combo),
        "config": to_jsonable(dataclasses.asdict(config_for(combo))),
        "grid": to_jsonable(GRID_B),
        "holdout_start": HOLDOUT_START,
        "replay_anchor": REPLAY_ANCHOR,
        "last_bar_ts": last_bar,
        "random_runs": int(random_runs),
        "random_seed": int(random_seed),
        "code_sha256": stratb_fingerprint(),
        "deps_sha256": deps_fingerprint(),
        "wf_data": wf_data_record(dfs_wf),
    }
    lock.update(sha256=lock_digest_b(lock), params_source=params_source, locked_at=_now(), **git_state(),
                attempts=[], first_run=None)
    _write(path, lock)
    return lock


def evaluate_criteria_b(b: Metrics, bh: Metrics, rnd: dict[str, Any], cost: Metrics) -> dict[str, Any]:
    p95 = (rnd.get("dist") or {}).get("sharpe", {}).get("p95") if rnd.get("runs") else None
    c1 = p95 is not None and b.sharpe > bh.sharpe and b.sharpe > p95
    c2 = b.max_dd <= MAX_DD
    c3 = b.n_trades >= MIN_TRADES
    c4 = cost.total_return > 0.0
    return {
        "sharpe": b.sharpe, "bh_sharpe": bh.sharpe, "random_sharpe_p95": p95, "random_runs": int(rnd.get("runs", 0)),
        "c1_sharpe_beats_bh_and_random_p95": bool(c1),
        "max_dd": b.max_dd, "c2_max_dd_le_15pct": bool(c2),
        "n_trades": int(b.n_trades), "c3_trades_ge_50": bool(c3),
        "cost_x1_5_total_return": cost.total_return, "c4_cost_x1_5_net_profit": bool(c4),
        "passed": bool(c1 and c2 and c3 and c4),
    }


def run_holdout_b(
    *,
    confirm: bool,
    cache_dir: Path,
    lock_path: Path = DEFAULT_LOCK_PATH,
    workers: int = 1,
    fingerprint: Callable[[], str] = stratb_fingerprint,
    deps: Callable[[], str] = deps_fingerprint,
) -> dict[str, Any]:
    """The one-shot holdout over [2024-01-01, last_bar_ts], recomputed from REPLAY_ANCHOR (2023-10-01)."""
    if not confirm:
        raise HoldoutBError("the holdout is single-use: pass confirm=True (CLI: --confirm) after the user's approval")
    path = Path(lock_path)
    lock = read_lock(path)
    head = committed_lock(path)
    if head.get("sha256") != lock["sha256"]:
        raise HoldoutBError(f"{path} differs from the committed lock: commit the lock as written by `holdout lock`")
    if lock.get("first_run") is not None or head.get("first_run") is not None:
        raise HoldoutBError("the strategy-B holdout has already run once: no rerun, no other combo (spec 6.4)")
    if fingerprint() != lock["code_sha256"]:
        raise HoldoutBError("the strategy-B code changed after the lock (B fingerprint differs): refusing")
    stale = lock_mismatch_b(lock)
    if stale:
        raise HoldoutBError(f"the lock's {stale} are not this code's (keys, grid, config_for(params), holdout start, "
                            "replay anchor): refusing")
    if deps() != lock.get("deps_sha256"):
        raise HoldoutBError("the strategy-A modules strategy B imports changed after the lock (deps_sha256 differs): "
                            "refusing")
    _refuse_dirty(path)
    dfs = {a: load_15m(p, include_holdout=True, lock_path=path) for a, p in cache_paths(cache_dir).items()}
    wf = wf_data_record({a: df[df["ts"] < HOLDOUT_START] for a, df in dfs.items()})
    changed = [a for a in ASSETS if wf[a]["sha256"] != lock["wf_data"][a]["sha256"]]
    if changed:
        raise HoldoutBError(f"the {changed} 15m bars before 2024-01-01 differ from the ones the walk-forward used")
    last = int(lock["last_bar_ts"])
    short = [a for a, df in dfs.items() if df.empty or int(df["ts"].iloc[-1]) < last]
    if short:
        raise HoldoutBError(f"the {short} cache ends before the lock's last bar {last}: run `ewbot stratb fetch` first")
    win = {a: df[(df["ts"] >= REPLAY_ANCHOR) & (df["ts"] <= last)].reset_index(drop=True) for a, df in dfs.items()}
    data = {"start_ts": REPLAY_ANCHOR, "last_bar_ts": last, "n_bars": {a: int(len(df)) for a, df in win.items()},
            "sha256": data_fingerprint_b(win, REPLAY_ANCHOR, last),
            "assets": {a: data_fingerprint(df, REPLAY_ANCHOR, last) for a, df in win.items()}}
    for a in lock["attempts"]:
        if a["data_sha256"] != data["sha256"]:
            raise HoldoutBError("an earlier unfinished attempt saw other holdout bars: refusing (inspect by hand)")
    attempt = {"started": _now(), "data_sha256": data["sha256"], **git_state()}
    lock["attempts"].append(attempt)
    _write(path, lock)

    cfg = config_for(normalize_combo(lock["params"]))
    end = last + M15
    res = backtest_b(win, cfg, HOLDOUT_START, end, include_holdout=True)
    frames = build_frames(win, cfg.params)
    b = compute_metrics(res.equity, res.trades, HOLDOUT_START, end)
    bh_eq, bh_trades = buy_hold_b(frames, HOLDOUT_START, end, cfg)
    bh = compute_metrics(bh_eq, bh_trades, HOLDOUT_START, end)
    rnd = random_control(frames, cfg, res.trades, HOLDOUT_START, end, runs=int(lock["random_runs"]),
                         seed=int(lock["random_seed"]), workers=workers)
    dear = dataclasses.replace(cfg, costs=scale_costs(cfg.costs, COST_MULT))
    signals = {a: generate_signals(f, cfg.params) for a, f in frames.items()}
    res_dear = run_engine(frames, signals, dear, HOLDOUT_START)
    cost = compute_metrics(res_dear.equity, res_dear.trades, HOLDOUT_START, end)
    criteria = evaluate_criteria_b(b, bh, rnd, cost)
    attempt["completed"] = _now()
    lock["first_run"] = to_jsonable({
        "started": attempt["started"], "completed": attempt["completed"], "start_ts": HOLDOUT_START, "end_ts": end,
        "last_bar_ts": last, "data": data,
        "metrics": {"B": dataclasses.asdict(b), "BH": dataclasses.asdict(bh), "B_cost_x1_5": dataclasses.asdict(cost)},
        "n_trades_by_asset": trades_by_asset([t for t in res.trades if HOLDOUT_START <= t.entry_ts < end]),
        "random": rnd, "criteria": criteria,
    })
    _write(path, lock)
    return {"lock_path": str(path), "result": res, "first_run": lock["first_run"]}
