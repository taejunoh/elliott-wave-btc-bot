"""Shared helpers for tests/paper: synthetic candles around the 2024-01-01 boundary, a committed
test holdout lock in a temp git repo, a fake Telegram sender and a fake candle updater.

Everything is synthetic (tests/synth.random_bars): no test reads the real cache, the real
<repo>/holdout.lock, or the network."""

from __future__ import annotations

import dataclasses
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ewbot.backtest.holdout import data_fingerprint, holdout_config
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.config import BotConfig
from ewbot.types import H1
from tests.synth import random_bars

PARAMS = (2.0, 0.5, 0.0)  # a grid combo that trades often on the synthetic path
SEED = 2
N_BARS = 3000  # 2023-11-15 00:00 .. 2024-03-18 23:00 UTC
BOUNDARY = 1_704_844_800  # 2024-01-10 00:00 UTC: the synthetic holdout's last bar
HOLDOUT_A_TRADES = 2  # synthetic A trades entering in [HOLDOUT_START, BOUNDARY]


def ts(text: str) -> int:
    """'YYYY-MM-DD HH:MM' (UTC) -> epoch seconds."""
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=UTC).timestamp())


def synth_frame(n: int = N_BARS, seed: int = SEED) -> pd.DataFrame:
    bars = random_bars(seed=seed, n=n, vol=30.0, start=20_000.0)
    return pd.DataFrame(
        {
            "ts": [b.ts for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    ).astype({"ts": "int64"})


def make_lock(df: pd.DataFrame, boundary: int = BOUNDARY, params: tuple[float, ...] = PARAMS,
              n_a: int = HOLDOUT_A_TRADES, **overrides: Any) -> dict[str, Any]:
    """A completed-holdout lock with the real schema (v3) for synthetic data."""
    cfg = holdout_config(BotConfig(), params)
    lock = {
        "version": 3,
        "keys": ["wave.K4", "strategy.Q_MIN", "strategy.S_MIN"],
        "params": list(params),
        "sha256": "d" * 64,
        "config": json.loads(json.dumps(dataclasses.asdict(cfg))),
        "holdout_start": HOLDOUT_START,
        "random_runs": 100,
        "data": {
            "start_ts": HOLDOUT_START,
            "last_bar_ts": boundary,
            "n_bars": int(((df["ts"] >= HOLDOUT_START) & (df["ts"] <= boundary)).sum()),
            "sha256": data_fingerprint(df, HOLDOUT_START, boundary),
        },
        "first_run": {"criteria": {"n_trades": {"A": n_a, "HYBRID": n_a + 1}}},
        "runs": [],
    }
    lock.update(overrides)
    return lock


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True,
    )


def commit_lock(repo: Path, lock: dict[str, Any]) -> Path:
    """Write <repo>/holdout.lock and commit it (initialising the repo if needed)."""
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        git(repo, "init", "-q")
    path = repo / "holdout.lock"
    path.write_text(json.dumps(lock, indent=2), encoding="utf-8")
    git(repo, "add", "holdout.lock")
    staged = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"], capture_output=True)
    if staged.returncode != 0:
        git(repo, "commit", "-q", "-m", "lock")
    return path


class FakeSender:
    """Records every text; raises for the next `fail` calls."""

    def __init__(self, fail: int = 0) -> None:
        self.texts: list[str] = []
        self.fail = fail
        self.calls = 0

    def __call__(self, text: str) -> None:
        self.calls += 1
        if self.fail > 0:
            self.fail -= 1
            raise ConnectionError("telegram down")
        self.texts.append(text)


class FakeFetch:
    """Stands in for update_cache: writes <cache>/BTC-USD_1h.parquet with the bars of `source`
    (a frame, or a callable returning one) that have closed by clock() - ts < clock() // 1h * 1h."""

    def __init__(self, source: pd.DataFrame | Callable[[], pd.DataFrame], clock: Callable[[], int], fail: int = 0) -> None:
        self.source = source
        self.clock = clock
        self.fail = fail
        self.calls = 0

    def __call__(self, cache_dir: Path) -> Path:
        self.calls += 1
        if self.fail > 0:
            self.fail -= 1
            raise ConnectionError("coinbase down")
        df = self.source() if callable(self.source) else self.source
        end = int(self.clock()) // H1 * H1
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / "BTC-USD_1h.parquet"
        df[df["ts"] < end].reset_index(drop=True).to_parquet(path, index=False)
        return path


class Clock:
    def __init__(self, now: int) -> None:
        self.now = int(now)

    def __call__(self) -> int:
        return self.now
