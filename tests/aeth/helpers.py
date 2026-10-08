"""Shared helpers for tests/aeth: synthetic ETH-priced 1h bars (strategy A's paper-test path,
tests.paper.helpers.synth_frame, with prices x 0.1: the same A trades with 0.1-ETH contracts), a synthetic
strategy-A holdout.lock with the paper tests' params, git repos and fake candle fetchers.

Everything is synthetic: no test uses the network or the real data/cache, and no test writes the real holdout.lock,
aeth-stage1.json or holdout-aeth.lock (tests/aeth/test_config.py only reads the real holdout.lock)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ewbot.aeth.config import PRODUCT
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.types import H1
from tests.paper.helpers import BOUNDARY, git, make_lock, synth_frame

HS = HOLDOUT_START
ETH_SCALE = 0.1
HOLDOUT_TRADES = 2  # synthetic A trades entering in [HOLDOUT_START, BOUNDARY] (as in tests/paper)
LOCK_NOW = BOUNDARY + H1 + 7  # a lock written now has last bar BOUNDARY (2024-01-10 00:00 UTC)


def eth_frame() -> pd.DataFrame:
    """tests.paper.helpers.synth_frame (2023-11-15 .. 2024-03-18 UTC) with prices x 0.1."""
    df = synth_frame()
    for c in ("open", "high", "low", "close"):
        df[c] = df[c] * ETH_SCALE
    return df


def pre2024_frame() -> pd.DataFrame:
    """eth_frame shifted back so that its last bar is 2023-12-31 23:00 UTC (3000 bars, all before 2024)."""
    df = eth_frame()
    df["ts"] = df["ts"] - (int(df["ts"].iloc[-1]) - (HS - H1))
    return df


def save_cache(cache_dir: Path, df: pd.DataFrame) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{PRODUCT}_1h.parquet"
    df.reset_index(drop=True).to_parquet(path, index=False)
    return path


def init_repo(repo: Path) -> Path:
    """A git repo with one committed file, so that HEAD exists."""
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        git(repo, "init", "-q")
        (repo / "README.md").write_text("synthetic\n", encoding="utf-8")
        git(repo, "add", "README.md")
        git(repo, "commit", "-q", "-m", "init")
    return repo


def commit_file(repo: Path, name: str, text: str) -> Path:
    """Write <repo>/<name> and commit it (when it changed)."""
    init_repo(repo)
    p = repo / name
    p.write_text(text, encoding="utf-8")
    git(repo, "add", name)
    if subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"]).returncode != 0:
        git(repo, "commit", "-q", "-m", name)
    return p


def write_a_lock(directory: Path, **overrides: Any) -> Path:
    """A synthetic strategy-A holdout.lock with the paper tests' params (2.0, 0.5, 0.0) and a valid digest
    (tests.paper.helpers.make_lock; `overrides` change its fields before the digest)."""
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / "holdout.lock"
    p.write_text(json.dumps(make_lock(synth_frame(), **overrides), indent=2), encoding="utf-8")
    return p


def a_lock_sha(a_lock: Path) -> str:
    return json.loads(a_lock.read_text(encoding="utf-8"))["sha256"]


class FakeFetch1h:
    """Stands in for A's fetch_1h(product, start_ts, end_ts): the bars of `source` with open time in [start, end).
    Records every call."""

    def __init__(self, source: pd.DataFrame) -> None:
        self.source, self.calls = source, []

    def __call__(self, product: str, start_ts: int, end_ts: int) -> pd.DataFrame:
        self.calls.append((product, int(start_ts), int(end_ts)))
        s = self.source
        return s[(s["ts"] >= start_ts) & (s["ts"] < end_ts)].reset_index(drop=True)


class FakeCacheFetch:
    """Stands in for paper.default_fetch(cache_dir, lock_path): writes the bars of `source` closed by clock() to the
    cache; raises for the next `fail` calls."""

    def __init__(self, source: pd.DataFrame | Callable[[], pd.DataFrame], clock: Callable[[], int], fail: int = 0):
        self.source, self.clock, self.fail, self.calls = source, clock, fail, 0

    def __call__(self, cache_dir: Path, lock_path: Path) -> Path:
        self.calls += 1
        if self.fail > 0:
            self.fail -= 1
            raise ConnectionError("coinbase down")
        df = self.source() if callable(self.source) else self.source
        return save_cache(Path(cache_dir), df[df["ts"] < int(self.clock()) // H1 * H1])
