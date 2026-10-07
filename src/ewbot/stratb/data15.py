"""15-minute BTC-USD candles for strategy B (Plan 4 spec 6.1): fetch, cache, guarded load, 1h bars.

Same public endpoint and request pattern as ewbot.data.coinbase_history (Coinbase Exchange, no auth;
GET .../candles returns at most 300 rows [time, low, high, open, close, volume], newest first, start and
end inclusive bar-open times), with granularity 900. The cache is <cache_dir>/BTC-USD_15m.parquet; the
bar in progress is never stored and missing bars are not filled in it (gap_report lists them).

Holdout guard (spec 6.3): until holdout-b.lock exists, update_cache_15m never downloads a bar at or after
HOLDOUT_START (2024-01-01) and load_15m never returns one - the parquet read itself filters them out.
load_15m(include_holdout=True) raises HoldoutGuardError until the lock exists."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import requests

from ewbot.backtest.runner import utc_ts
from ewbot.backtest.walkforward import HOLDOUT_START, WF_START
from ewbot.data.candles import IntegrityReport, check_integrity
from ewbot.data.coinbase_history import API, COLUMNS, MAX_PER_REQUEST, _get_json
from ewbot.paper.fingerprint import PROJECT_ROOT
from ewbot.stratb.config import M15
from ewbot.types import H1

PRODUCT = "BTC-USD"
START_TS = utc_ts(WF_START)  # 2017-01-01: the first bar fetched
REPLAY_ANCHOR = utc_ts("2023-10-01")  # spec 6.4: holdout and paper runs both recompute from here
DEFAULT_LOCK_PATH = PROJECT_ROOT / "holdout-b.lock"
LOCK_KIND = "ewbot-stratb-holdout"


class HoldoutGuardError(RuntimeError):
    """Candles at/after 2024-01-01 were requested before holdout-b.lock exists."""


def cache_path(cache_dir: Path, product: str = PRODUCT) -> Path:
    return Path(cache_dir) / f"{product}_15m.parquet"


def lock_exists(lock_path: Path) -> bool:
    """True only for a strategy-B holdout lock (a JSON object of kind LOCK_KIND with a sha256), not for
    any file that happens to have the name."""
    p = Path(lock_path)
    if not p.is_file():
        return False
    try:
        lock = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(lock, dict) and lock.get("kind") == LOCK_KIND and isinstance(lock.get("sha256"), str)


def _empty() -> pd.DataFrame:
    return pd.DataFrame({"ts": pd.Series(dtype="int64"), **{c: pd.Series(dtype="float64") for c in COLUMNS[1:]}})


def _standard(df: pd.DataFrame) -> pd.DataFrame:
    df = df[COLUMNS].astype({"ts": "int64", **{c: "float64" for c in COLUMNS[1:]}})
    return df.drop_duplicates("ts").sort_values("ts", kind="stable").reset_index(drop=True)


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_15m(
    product: str, start_ts: int, end_ts: int, session: requests.Session | None = None, sleep_s: float = 0.15
) -> pd.DataFrame:
    """Closed 15m bars with open time in [start_ts, end_ts), ascending, standard schema (maybe empty).
    Walks windows of 300 bars; retries 429 / 5xx like coinbase_history (its _get_json)."""
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
        rows.extend(_get_json(sess, url, {"granularity": M15, "start": _iso(w_start), "end": _iso(w_end)}))
        w_start = w_end + M15
        if w_start <= last_open and sleep_s > 0:
            time.sleep(sleep_s)
    if not rows:
        return _empty()
    df = pd.DataFrame(rows, columns=["ts", "low", "high", "open", "close", "volume"])
    df["ts"] = df["ts"].astype("int64")
    return _standard(df[(df["ts"] >= first) & (df["ts"] <= last_open)])


def update_cache_15m(
    cache_dir: Path,
    *,
    product: str = PRODUCT,
    lock_path: Path = DEFAULT_LOCK_PATH,
    start_ts: int = START_TS,
    now_ts: int | None = None,
    session: requests.Session | None = None,
    sleep_s: float = 0.15,
) -> Path:
    """Create (from start_ts) or extend the 15m cache up to the last CLOSED bar - and, while holdout-b.lock
    does not exist, never past HOLDOUT_START. Network: only here (`stratb fetch`, `stratb holdout run`,
    the paper-B runner, which starts an empty cache at REPLAY_ANCHOR)."""
    path = cache_path(cache_dir, product)
    path.parent.mkdir(parents=True, exist_ok=True)
    old = _standard(pd.read_parquet(path)) if path.exists() else _empty()
    now = int(time.time()) if now_ts is None else int(now_ts)
    end = now // M15 * M15  # exclusive: the bar in progress is never stored
    if not lock_exists(lock_path):
        end = min(end, HOLDOUT_START)
    begin = int(old["ts"].max()) + M15 if len(old) else int(start_ts)
    if begin >= end:
        if not path.exists():
            old.to_parquet(path, index=False)
        return path
    new = fetch_15m(product, begin, end, session=session, sleep_s=sleep_s)
    _standard(pd.concat([old, new], ignore_index=True)).to_parquet(path, index=False)
    return path


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
