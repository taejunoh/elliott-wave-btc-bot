"""ETH-USD 1h candles for A-ETH (Plan 5a spec 5.3): fetch, cache, guarded load.

One cache file, <cache_dir>/ETH-USD_1h.parquet, filled through strategy A's Coinbase fetcher
(ewbot.data.coinbase_history.fetch_1h: the same public endpoint; imported, never changed) in chunks of CHUNK_HOURS,
each saved as it arrives (temp file, then rename), so an interrupted download resumes from the last saved bar. The
hour in progress is never stored and missing bars are not filled in the cache (stage 1, the holdout and the paper
runtime fill them after loading, like A, with fill_gaps). A's update_cache is not used: it always fetches up to now.

2024 guard (spec 5.3): until a valid holdout-aeth.lock exists (lockfile.lock_exists: its kind and digest),
fetch_eth never requests a window that reaches HOLDOUT_START (2024-01-01) and load_eth never returns a bar at or
after it - the parquet read itself filters them out. load_eth(unlock_2024=True) raises HoldoutGuardError until the
lock exists. A cache that holds bars from 2024-01-01 on while no lock exists did not come from A-ETH (strategy A's
`ewbot fetch --product ETH-USD` writes the same file up to now): fetch_eth and load_eth refuse it."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

import pandas as pd

from ewbot.aeth.config import DATA_START, LOCK_PATH, PRODUCT, AethError
from ewbot.aeth.lockfile import lock_exists
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.data.coinbase_history import COLUMNS, fetch_1h
from ewbot.types import H1

CHUNK_HOURS = 3000  # one saved chunk = 10 requests of 300 bars

Fetch = Callable[[str, int, int], pd.DataFrame]


class HoldoutGuardError(AethError):
    """ETH candles at/after 2024-01-01 were requested, read or found before holdout-aeth.lock exists."""


def cache_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / f"{PRODUCT}_1h.parquet"


def _empty() -> pd.DataFrame:
    return pd.DataFrame({"ts": pd.Series(dtype="int64"), **{c: pd.Series(dtype="float64") for c in COLUMNS[1:]}})


def _standard(df: pd.DataFrame) -> pd.DataFrame:
    df = df[COLUMNS].astype({"ts": "int64", **{c: "float64" for c in COLUMNS[1:]}})
    return df.drop_duplicates("ts").sort_values("ts", kind="stable").reset_index(drop=True)


def _read(path: Path, before: int | None) -> pd.DataFrame:
    """The cached bars (standard schema); with `before`, the parquet read keeps only ts < before."""
    return _standard(pd.read_parquet(path, filters=None if before is None else [("ts", "<", before)]))


def _holdout_rows(path: Path) -> int:
    """How many cached bars are at/after 2024-01-01 (only their ts column is read)."""
    return len(pd.read_parquet(path, columns=["ts"], filters=[("ts", ">=", HOLDOUT_START)]))


def _refuse_foreign_2024(path: Path) -> None:
    if path.exists() and _holdout_rows(path):
        raise HoldoutGuardError(f"{path} holds ETH bars from 2024-01-01 on but no holdout-aeth.lock exists: they did "
                                "not come from A-ETH - delete the file and run `ewbot aeth fetch` again")


def _save(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def fetch_eth(
    cache_dir: Path, *, lock_path: Path = LOCK_PATH, now_ts: int | None = None, fetch: Fetch = fetch_1h
) -> Path:
    """Create (from 2017-01-01) or extend the ETH 1h cache up to the last CLOSED hour - and, while no valid
    holdout-aeth.lock exists, never past 2024-01-01. `fetch(product, start_ts, end_ts)` returns the bars with open
    time in [start_ts, end_ts) (A's fetch_1h). Network: only here."""
    path = cache_path(cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    unlocked = lock_exists(lock_path)
    if not unlocked:
        _refuse_foreign_2024(path)
    cur = _read(path, None if unlocked else HOLDOUT_START) if path.exists() else _empty()
    now = int(time.time()) if now_ts is None else int(now_ts)
    end = now // H1 * H1  # exclusive: the hour in progress is never stored
    if not unlocked:
        end = min(end, HOLDOUT_START)
    begin = int(cur["ts"].max()) + H1 if len(cur) else DATA_START
    if not path.exists():
        _save(cur, path)
    while begin < end:
        stop = min(begin + CHUNK_HOURS * H1, end)
        if stop > HOLDOUT_START and not lock_exists(lock_path):
            raise HoldoutGuardError(f"{PRODUCT}: bars from 2024-01-01 on cannot be fetched before holdout-aeth.lock")
        page = fetch(PRODUCT, begin, stop)
        page = page[(page["ts"] >= begin) & (page["ts"] < stop)]
        if len(page):
            cur = _standard(pd.concat([cur, page], ignore_index=True))
            _save(cur, path)
        begin = stop
    return path


def load_eth(path: Path, *, unlock_2024: bool = False, lock_path: Path = LOCK_PATH) -> pd.DataFrame:
    """The cached ETH bars (standard schema, sorted, unique ts). Without unlock_2024 the parquet read filters out
    every bar at/after HOLDOUT_START; unlock_2024=True needs a valid holdout-aeth.lock (HoldoutGuardError)."""
    p = Path(path)
    unlocked = lock_exists(lock_path)
    if unlock_2024 and not unlocked:
        raise HoldoutGuardError(f"{lock_path} is not a valid A-ETH lock: ETH bars from 2024-01-01 on cannot be read")
    if not unlocked:
        _refuse_foreign_2024(p)
    return _read(p, None if unlock_2024 else HOLDOUT_START)
