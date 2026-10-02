"""Hourly BTC-USD history from the public Coinbase Exchange API (no auth).

GET {API}?granularity=3600&start=<iso>&end=<iso> returns at most 300 rows
``[time, low, high, open, close, volume]``, newest first. ``time`` is the bar OPEN
time (epoch seconds). Both ``start`` and ``end`` are inclusive bar-open times.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import requests

from ewbot.types import H1

API = "https://api.exchange.coinbase.com/products/{product}/candles"
MAX_PER_REQUEST = 300
COLUMNS = ["ts", "open", "high", "low", "close", "volume"]
_RETRY_TRIES = 3
_BACKOFF_S = 1.0
_TIMEOUT_S = 15.0


def _empty() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts": pd.Series(dtype="int64"),
            **{c: pd.Series(dtype="float64") for c in COLUMNS[1:]},
        }
    )


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_ts() -> int:
    return int(time.time())


def _get_json(session: requests.Session, url: str, params: dict[str, object]) -> list:
    """GET with up to 3 tries; retries (exponential backoff) on HTTP 429 / 5xx."""
    last = ""
    for attempt in range(_RETRY_TRIES):
        resp = session.get(url, params=params, timeout=_TIMEOUT_S)
        status = resp.status_code
        if status == 200:
            return resp.json()
        if status == 429 or status >= 500:
            last = f"HTTP {status}"
            if attempt < _RETRY_TRIES - 1:
                time.sleep(_BACKOFF_S * 2**attempt)
            continue
        raise RuntimeError(f"Coinbase candles request failed: HTTP {status} {resp.text[:200]}")
    raise RuntimeError(f"Coinbase candles request failed after {_RETRY_TRIES} tries ({last})")


def fetch_1h(
    product: str,
    start_ts: int,
    end_ts: int,
    session: requests.Session | None = None,
    sleep_s: float = 0.15,
) -> pd.DataFrame:
    """Fetch closed 1h bars with open time in ``[start_ts, end_ts)``.

    Walks ascending in windows of 300 bars, dedupes and sorts. Returns the standard schema
    (ts:int64, open/high/low/close/volume:float64, RangeIndex), possibly empty.
    """
    first = -(-start_ts // H1) * H1  # round start up to the hour grid
    last_open = (end_ts - 1) // H1 * H1  # last bar open time inside [start_ts, end_ts)
    if first > last_open:
        return _empty()
    sess = session if session is not None else requests.Session()
    url = API.format(product=product)

    rows: list[list[float]] = []
    w_start = first
    while w_start <= last_open:
        w_end = min(w_start + (MAX_PER_REQUEST - 1) * H1, last_open)
        params = {"granularity": H1, "start": _iso(w_start), "end": _iso(w_end)}
        data = _get_json(sess, url, params)
        rows.extend(data)
        w_start = w_end + H1
        if w_start <= last_open and sleep_s > 0:
            time.sleep(sleep_s)

    if not rows:
        return _empty()
    df = pd.DataFrame(rows, columns=["ts", "low", "high", "open", "close", "volume"])
    df["ts"] = df["ts"].astype("int64")
    df = df[(df["ts"] >= first) & (df["ts"] <= last_open)]
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    for c in COLUMNS[1:]:
        df[c] = df[c].astype("float64")
    return df[COLUMNS]


def update_cache(
    product: str = "BTC-USD",
    cache_dir: Path = Path("data/cache"),
    start_ts: int = 1483228800,
) -> Path:
    """Create/extend ``{cache_dir}/{product}_1h.parquet`` up to the last CLOSED hour."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{product}_1h.parquet"

    old = pd.read_parquet(path) if path.exists() else _empty()
    begin = int(old["ts"].max()) + H1 if len(old) else start_ts
    end = _now_ts() // H1 * H1  # exclusive: the in-progress hour is not stored
    if begin >= end:
        if not path.exists():
            old.to_parquet(path, index=False)
        return path

    new = fetch_1h(product, begin, end)
    out = pd.concat([old, new], ignore_index=True)
    out = out.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    out.to_parquet(path, index=False)
    return path
