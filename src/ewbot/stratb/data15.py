"""15-minute Coinbase spot candles for strategy B (Plan 4 spec 6.1): fetch, cache, guarded load, 1h bars.

One cache file per asset of config.ASSET_SPECS: <cache_dir>/BTC-USD_15m.parquet and ETH-USD_15m.parquet.
Same public endpoint and request pattern as ewbot.data.coinbase_history (Coinbase Exchange, no auth;
GET .../candles returns at most 300 rows [time, low, high, open, close, volume], newest first, start and
end inclusive bar-open times), with granularity 900. The bar in progress is never stored and missing bars
are not filled in the cache (gap_report lists them). Downloads resume: every page of 300 bars is appended
to the cache file as it arrives (written to a temp file, then renamed), so an interrupted download goes on
from the last saved bar; a connection error or a timeout is retried FETCH_TRIES times with backoff (429 /
5xx are retried by coinbase_history's _get_json).

Holdout guard (spec 6.3): until holdout-b.lock exists, fetch_15m refuses a window that reaches
HOLDOUT_START (2024-01-01), update_cache_15m never asks for one, and load_15m never returns a bar at or
after it - the parquet read itself filters them out. load_15m(include_holdout=True) raises
HoldoutGuardError until the lock exists. A lock "exists" only when its sha256 is the digest of its bound
fields (lock_digest_b, defined here for stratb.holdout, which writes the lock): a hand-made file with the
right name or shape does not unlock 2024."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd
import requests

from ewbot.backtest.runner import utc_ts
from ewbot.backtest.walkforward import HOLDOUT_START, WF_START
from ewbot.data.candles import IntegrityReport, check_integrity
from ewbot.data.coinbase_history import API, COLUMNS, MAX_PER_REQUEST, _get_json
from ewbot.paper.fingerprint import PROJECT_ROOT
from ewbot.stratb.config import ASSET_SPECS, M15
from ewbot.types import H1

PRODUCTS = {s.asset: s.product for s in ASSET_SPECS}  # {"BTC": "BTC-USD", "ETH": "ETH-USD"}
START_TS = utc_ts(WF_START)  # 2017-01-01: the first bar fetched
REPLAY_ANCHOR = utc_ts("2023-10-01")  # spec 6.4: holdout and paper runs both recompute from here
DEFAULT_LOCK_PATH = PROJECT_ROOT / "holdout-b.lock"
LOCK_KIND = "ewbot-stratb-holdout"
LOCK_BOUND = ("kind", "version", "keys", "params", "config", "grid", "holdout_start", "replay_anchor", "last_bar_ts",
              "random_runs", "random_seed", "code_sha256", "deps_sha256", "wf_data", "wf_report_sha256")  # sha256 binds
FETCH_TRIES = 3  # per page, on a connection error or a timeout
FETCH_BACKOFF_S = 1.0  # waits 1 s, then 2 s


class HoldoutGuardError(RuntimeError):
    """Candles at/after 2024-01-01 were requested before holdout-b.lock exists."""


def cache_path(cache_dir: Path, product: str) -> Path:
    return Path(cache_dir) / f"{product}_15m.parquet"


def cache_paths(cache_dir: Path) -> dict[str, Path]:
    """{asset: its cache file}, in config.ASSETS order."""
    return {a: cache_path(cache_dir, p) for a, p in PRODUCTS.items()}


def lock_digest_b(lock: Mapping[str, Any]) -> str:
    """sha256 of the sorted compact JSON of the lock's LOCK_BOUND fields."""
    blob = json.dumps({k: lock.get(k) for k in LOCK_BOUND}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def lock_exists(lock_path: Path) -> bool:
    """True only for a strategy-B holdout lock: a JSON object of kind LOCK_KIND whose sha256 is the digest of
    its bound fields - not for a file that merely has the name or the shape."""
    p = Path(lock_path)
    if not p.is_file():
        return False
    try:
        lock = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(lock, dict) and lock.get("kind") == LOCK_KIND and lock.get("sha256") == lock_digest_b(lock)


def _empty() -> pd.DataFrame:
    return pd.DataFrame({"ts": pd.Series(dtype="int64"), **{c: pd.Series(dtype="float64") for c in COLUMNS[1:]}})


def _standard(df: pd.DataFrame) -> pd.DataFrame:
    df = df[COLUMNS].astype({"ts": "int64", **{c: "float64" for c in COLUMNS[1:]}})
    return df.drop_duplicates("ts").sort_values("ts", kind="stable").reset_index(drop=True)


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows_frame(rows: list[list[float]], first: int, last_open: int) -> pd.DataFrame:
    if not rows:
        return _empty()
    df = pd.DataFrame(rows, columns=["ts", "low", "high", "open", "close", "volume"])
    df["ts"] = df["ts"].astype("int64")
    return _standard(df[(df["ts"] >= first) & (df["ts"] <= last_open)])


def _get_page(sess: requests.Session, url: str, params: dict[str, object]) -> list:
    """_get_json, also retried on a connection error or a timeout (FETCH_TRIES attempts in all)."""
    failed = 0
    while True:
        try:
            return _get_json(sess, url, params)
        except (requests.ConnectionError, requests.Timeout):
            failed += 1
            if failed >= FETCH_TRIES:
                raise
            time.sleep(FETCH_BACKOFF_S * 2 ** (failed - 1))


def fetch_15m(
    product: str,
    start_ts: int,
    end_ts: int,
    session: requests.Session | None = None,
    sleep_s: float = 0.15,
    *,
    lock_path: Path = DEFAULT_LOCK_PATH,
    on_page: Callable[[pd.DataFrame], None] | None = None,
) -> pd.DataFrame:
    """Closed 15m bars with open time in [start_ts, end_ts), ascending, standard schema (maybe empty).
    Walks windows of 300 bars; on_page receives each window's bars as they arrive. HoldoutGuardError,
    before any request, when the window reaches 2024-01-01 and holdout-b.lock does not exist."""
    if end_ts > HOLDOUT_START and not lock_exists(lock_path):
        raise HoldoutGuardError(f"{product}: bars from 2024-01-01 on cannot be fetched before the strategy-B lock")
    first = -(-start_ts // M15) * M15
    last_open = (end_ts - 1) // M15 * M15
    if first > last_open:
        return _empty()
    sess = session if session is not None else requests.Session()
    url = API.format(product=product)
    rows: list[list[float]] = []
    w_start = first
    while w_start <= last_open:
        w_end = min(w_start + (MAX_PER_REQUEST - 1) * M15, last_open)
        page = _get_page(sess, url, {"granularity": M15, "start": _iso(w_start), "end": _iso(w_end)})
        rows.extend(page)
        if on_page is not None:
            on_page(_rows_frame(page, w_start, w_end))
        w_start = w_end + M15
        if w_start <= last_open and sleep_s > 0:
            time.sleep(sleep_s)
    return _rows_frame(rows, first, last_open)


def _save(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def update_cache_15m(
    cache_dir: Path,
    *,
    product: str,
    lock_path: Path = DEFAULT_LOCK_PATH,
    start_ts: int = START_TS,
    now_ts: int | None = None,
    session: requests.Session | None = None,
    sleep_s: float = 0.15,
) -> Path:
    """Create (from start_ts) or extend one product's 15m cache up to the last CLOSED bar - and, while
    holdout-b.lock does not exist, never past HOLDOUT_START. Every page is saved as it arrives, so a failed
    download keeps what it got and the next call goes on from there. Network: only here (`stratb fetch`,
    `stratb holdout run`, the paper-B runner, which starts an empty cache at REPLAY_ANCHOR)."""
    path = cache_path(cache_dir, product)
    path.parent.mkdir(parents=True, exist_ok=True)
    cur = _standard(pd.read_parquet(path)) if path.exists() else _empty()
    now = int(time.time()) if now_ts is None else int(now_ts)
    end = now // M15 * M15  # exclusive: the bar in progress is never stored
    if not lock_exists(lock_path):
        end = min(end, HOLDOUT_START)
    begin = int(cur["ts"].max()) + M15 if len(cur) else int(start_ts)
    if not path.exists():
        _save(cur, path)
    if begin >= end:
        return path

    def append(page: pd.DataFrame) -> None:
        nonlocal cur
        if len(page):
            cur = _standard(pd.concat([cur, page], ignore_index=True))
            _save(cur, path)

    fetch_15m(product, begin, end, session=session, sleep_s=sleep_s, lock_path=lock_path, on_page=append)
    return path


def update_caches_15m(
    cache_dir: Path,
    *,
    lock_path: Path = DEFAULT_LOCK_PATH,
    start_ts: int = START_TS,
    now_ts: int | None = None,
    session: requests.Session | None = None,
    sleep_s: float = 0.15,
) -> dict[str, Path]:
    """update_cache_15m for every asset, with one clock: {asset: cache file}."""
    now = int(time.time()) if now_ts is None else int(now_ts)
    return {
        a: update_cache_15m(cache_dir, product=p, lock_path=lock_path, start_ts=start_ts, now_ts=now,
                            session=session, sleep_s=sleep_s)
        for a, p in PRODUCTS.items()
    }


def load_15m(path: Path, *, include_holdout: bool = False, lock_path: Path = DEFAULT_LOCK_PATH) -> pd.DataFrame:
    """The cached 15m bars (standard schema, sorted, unique ts). Without include_holdout the parquet read
    filters out every bar at/after HOLDOUT_START; include_holdout=True needs holdout-b.lock."""
    if include_holdout and not lock_exists(lock_path):
        raise HoldoutGuardError(
            f"{lock_path} does not exist: candles from 2024-01-01 on cannot be read before the strategy-B lock"
        )
    filters = None if include_holdout else [("ts", "<", HOLDOUT_START)]
    df = pd.read_parquet(Path(path), filters=filters)
    return _standard(df)


def load_caches_15m(cache_dir: Path) -> dict[str, pd.DataFrame]:
    """Every asset's bars before 2024-01-01 (research runs): {asset: load_15m(its cache)}."""
    return {a: load_15m(p) for a, p in cache_paths(cache_dir).items()}


def gap_report(df15: pd.DataFrame) -> IntegrityReport:
    """Missing / duplicate / non-positive 15m bars (A's check_integrity on the 15m grid)."""
    return check_integrity(df15, tf=M15)


def resample_1h(df15: pd.DataFrame) -> pd.DataFrame:
    """1h bars from 15m bars: COMPLETE UTC hours only (exactly 4 distinct bars), synthetic when any
    constituent is (the column is False when the input has none)."""
    d = df15.drop_duplicates("ts").sort_values("ts")
    syn = d["synthetic"].astype(bool) if "synthetic" in d.columns else False
    d = d.assign(bucket=(d["ts"] // H1) * H1, synthetic=syn)
    agg = d.groupby("bucket", sort=True).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum"), synthetic=("synthetic", "any"), n=("ts", "size"),
    )
    agg = agg[agg["n"] == H1 // M15].drop(columns="n")
    return agg.rename_axis("ts").reset_index().astype({"ts": "int64", "synthetic": "bool"})
