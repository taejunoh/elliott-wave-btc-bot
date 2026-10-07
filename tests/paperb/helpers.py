"""Shared helpers for tests/paperb: one synthetic two-asset world (from 2023-10-01 to 2024-08, a BTC long
and an ETH short on the same 15m bar every 232 bars: tests.stratb.helpers.world_dfs), a strategy-B lock
whose holdout really ran on it, a fake 15m fetcher of both assets. A's fake sender, clock and git helper
(tests.paper.helpers) are reused. No network, no real cache, no real lock."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ewbot.paperb.runner import PaperBEnv
from ewbot.stratb.data15 import REPLAY_ANCHOR, cache_paths, load_caches_15m
from ewbot.stratb.holdout import create_lock, run_holdout_b
from tests.paper.helpers import Clock, FakeSender, git
from tests.stratb.helpers import save_world, world_dfs

HS = 1_704_067_200
LOCK_NOW = HS + 150 * 86_400 + 1_234  # the lock's clock: last bar 2024-05-30 00:45 UTC
LAST_BAR = LOCK_NOW // 3600 * 3600 - 900
COMBO = [2.0, [0.382, 0.618], 1.0, 1.272, "breakeven"]
M15 = 900


def world() -> dict[str, pd.DataFrame]:
    return world_dfs(REPLAY_ANCHOR)


def build_passed_lock(tmp: Path) -> str:
    """The text of holdout-b.lock after a real (synthetic) holdout run that passed."""
    repo, cache = tmp / "repo", tmp / "cache"
    repo.mkdir()
    cache.mkdir()
    git(repo, "init", "-q")
    save_world(cache, world())
    lp = repo / "holdout-b.lock"
    create_lock(COMBO, load_caches_15m(cache), params_source="test", now_ts=LOCK_NOW, lock_path=lp, random_runs=2)
    git(repo, "add", lp.name)
    git(repo, "commit", "-q", "-m", "lock")
    run_holdout_b(confirm=True, cache_dir=cache, lock_path=lp)
    lock = json.loads(lp.read_text(encoding="utf-8"))
    assert lock["first_run"]["criteria"]["passed"] is True
    return lp.read_text(encoding="utf-8")


def commit_lock_b(repo: Path, text: str) -> Path:
    """Write <repo>/holdout-b.lock and commit it (initialising the repo if needed)."""
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        git(repo, "init", "-q")
    p = repo / "holdout-b.lock"
    p.write_text(text, encoding="utf-8")
    git(repo, "add", p.name)
    if subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"]).returncode != 0:
        git(repo, "commit", "-q", "-m", "lock")
    return p


class FakeFetchB:
    """Stands in for update_caches_15m: writes each asset's bars of `source` closed by clock() into its cache."""

    def __init__(self, source: dict[str, pd.DataFrame] | Callable[[], dict[str, pd.DataFrame]],
                 clock: Callable[[], int], fail: int = 0):
        self.source, self.clock, self.fail, self.calls = source, clock, fail, 0

    def __call__(self, cache_dir: Path, lock_path: Path) -> dict[str, Path]:
        self.calls += 1
        if self.fail > 0:
            self.fail -= 1
            raise ConnectionError("coinbase down")
        dfs = self.source() if callable(self.source) else self.source
        paths = cache_paths(cache_dir)
        for a, p in paths.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            df = dfs[a]
            df[df["ts"] < int(self.clock()) // M15 * M15].reset_index(drop=True).to_parquet(p, index=False)
        return paths


def make_env(tmp_path: Path, lock_text: str, now: int, source: Any = None, **kw: Any) -> tuple[PaperBEnv, Clock, FakeSender]:
    clock = Clock(now)
    sender = FakeSender()
    env = PaperBEnv(
        ledger_dir=tmp_path / "ledger", lock_path=commit_lock_b(tmp_path / "repo", lock_text),
        cache_dir=tmp_path / "cache", fetch=FakeFetchB(world() if source is None else source, clock),
        sender=sender, clock=clock, sleep=lambda s: None, environment=lambda: {"python": "3.12.13"},
    )
    for k, v in kw.items():
        setattr(env, k, v)
    return env, clock, sender
