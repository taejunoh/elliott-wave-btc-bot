"""Candle utilities: integrity checks, gap filling, 4h aggregation, wick clipping, calendar."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ewbot.types import H1, H4, Bar

log = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")
_PRICE_COLS = ["open", "high", "low", "close"]
_BASE_COLS = ["ts", "open", "high", "low", "close", "volume"]


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    n: int
    missing_ts: tuple[int, ...]
    duplicate_ts: tuple[int, ...]
    nonpositive_ts: tuple[int, ...]  # bars whose open/high/low/close is <= 0 (corrupt rows)


def load_1h(path: Path) -> pd.DataFrame:
    """Read a parquet cache, enforce the standard dtypes and sort by ts (duplicates are kept)."""
    df = pd.read_parquet(path)
    df = df.astype({"ts": "int64", **{c: "float64" for c in _BASE_COLS[1:]}})
    return df.sort_values("ts", kind="stable").reset_index(drop=True)


def check_integrity(df: pd.DataFrame, tf: int = H1) -> IntegrityReport:
    """Report missing bars (holes in the ``tf`` grid), duplicated ts and non-positive prices."""
    ts = df["ts"].to_numpy(dtype=np.int64)
    if ts.size == 0:
        return IntegrityReport(0, (), (), ())
    uniq, counts = np.unique(ts, return_counts=True)
    dups = uniq[counts > 1]
    grid = np.arange(uniq[0], uniq[-1] + tf, tf, dtype=np.int64)
    missing = np.setdiff1d(grid, uniq, assume_unique=True)
    bad = (df[_PRICE_COLS].to_numpy(dtype=np.float64) <= 0).any(axis=1)
    bad_ts = np.unique(ts[bad])
    return IntegrityReport(
        n=int(ts.size),
        missing_ts=tuple(int(t) for t in missing),
        duplicate_ts=tuple(int(t) for t in dups),
        nonpositive_ts=tuple(int(t) for t in bad_ts),
    )


def fill_gaps(df: pd.DataFrame, tf: int = H1) -> pd.DataFrame:
    """Return a gap-free frame with a bool ``synthetic`` column.

    Duplicate ts keep the first row. A missing bar is flat at the previous close with volume 0
    and ``synthetic=True``. Existing ``synthetic`` flags are preserved (idempotent).
    """
    d = df.drop_duplicates("ts", keep="first").sort_values("ts").reset_index(drop=True)
    had_flag = "synthetic" in d.columns
    flags = d["synthetic"].to_numpy(dtype=bool) if had_flag else np.zeros(len(d), dtype=bool)
    d = d[_BASE_COLS]
    if d.empty:
        return d.assign(synthetic=pd.Series(dtype=bool))

    ts = d["ts"].to_numpy(dtype=np.int64)
    grid = np.arange(ts[0], ts[-1] + tf, tf, dtype=np.int64)
    out = d.set_index("ts").reindex(grid)
    missing = out["close"].isna().to_numpy()
    prev_close = out["close"].ffill()
    for c in ("open", "high", "low"):
        out[c] = out[c].where(~missing, prev_close)
    out["close"] = prev_close
    out["volume"] = out["volume"].fillna(0.0)

    synthetic = np.ones(len(grid), dtype=bool)
    synthetic[~missing] = flags  # rows that existed keep their previous flag
    out["synthetic"] = synthetic
    out = out.rename_axis("ts").reset_index()
    return out.astype({"ts": "int64"})


def clip_wicks(df: pd.DataFrame, atr1h: np.ndarray, k: float) -> tuple[pd.DataFrame, int]:
    """Clip high/low to the candle body +/- k * ATR, using the ATR of the PREVIOUS bar.

    ``atr1h`` is aligned row-for-row with ``df``. The first bar and bars whose previous ATR is
    NaN are left untouched. Returns ``(new_df, n_bars_clipped)``; the input is not modified.
    """
    atr = np.asarray(atr1h, dtype=np.float64)
    if atr.shape != (len(df),):
        raise ValueError(f"atr1h length {atr.shape} does not match df length {len(df)}")
    out = df.copy()
    if len(df) < 2:
        return out, 0

    prev_atr = np.concatenate(([np.nan], atr[:-1]))
    o = df["open"].to_numpy(dtype=np.float64)
    c = df["close"].to_numpy(dtype=np.float64)
    hi = df["high"].to_numpy(dtype=np.float64)
    lo = df["low"].to_numpy(dtype=np.float64)
    with np.errstate(invalid="ignore"):
        hi_cap = np.maximum(o, c) + k * prev_atr
        lo_cap = np.minimum(o, c) - k * prev_atr
        new_hi = np.where(np.isnan(prev_atr), hi, np.minimum(hi, hi_cap))
        new_lo = np.where(np.isnan(prev_atr), lo, np.maximum(lo, lo_cap))
    changed = (new_hi != hi) | (new_lo != lo)
    n_clipped = int(changed.sum())
    out["high"] = new_hi
    out["low"] = new_lo
    if n_clipped:
        log.info("clip_wicks: clipped %d bar(s) at k=%s", n_clipped, k)
    return out, n_clipped


def aggregate_4h(df1h: pd.DataFrame) -> pd.DataFrame:
    """Aggregate 1h bars into 4h bars aligned to UTC 00/04/08/12/16/20.

    Only COMPLETE buckets (exactly 4 distinct 1h bars) are returned; ``synthetic`` is True if
    any constituent was synthetic. Run ``fill_gaps`` first to keep buckets that lost a bar.
    """
    d = df1h.drop_duplicates("ts").sort_values("ts")
    syn = d["synthetic"].astype(bool) if "synthetic" in d.columns else False
    d = d.assign(bucket=(d["ts"] // H4) * H4, synthetic=syn)
    agg = d.groupby("bucket", sort=True).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        synthetic=("synthetic", "any"),
        n=("ts", "size"),
    )
    agg = agg[agg["n"] == H4 // H1].drop(columns="n")
    out = agg.rename_axis("ts").reset_index()
    return out.astype({"ts": "int64", "synthetic": "bool"})


def to_bars(df: pd.DataFrame) -> list[Bar]:
    """Convert a frame into ``Bar`` objects (``synthetic`` defaults to False if absent)."""
    ts = df["ts"].to_numpy(dtype=np.int64)
    o = df["open"].to_numpy(dtype=np.float64)
    h = df["high"].to_numpy(dtype=np.float64)
    lo = df["low"].to_numpy(dtype=np.float64)
    c = df["close"].to_numpy(dtype=np.float64)
    v = df["volume"].to_numpy(dtype=np.float64)
    syn = df["synthetic"].to_numpy(dtype=bool) if "synthetic" in df.columns else np.zeros(len(df), bool)
    return [
        Bar(int(ts[i]), float(o[i]), float(h[i]), float(lo[i]), float(c[i]), float(v[i]), bool(syn[i]))
        for i in range(len(df))
    ]


def slice_ts(df: pd.DataFrame, start_ts: int | None, end_ts: int | None) -> pd.DataFrame:
    """Rows with ``start_ts <= ts < end_ts`` (either bound may be None), index reset."""
    mask = np.ones(len(df), dtype=bool)
    if start_ts is not None:
        mask &= df["ts"].to_numpy() >= start_ts
    if end_ts is not None:
        mask &= df["ts"].to_numpy() < end_ts
    return df.loc[mask].reset_index(drop=True)


def _overlaps_friday_window(ts: int, start_hour: int, end_hour: int) -> bool:
    """True if the 1h bar [ts, ts+3600) overlaps Friday ``start_hour``:00-``end_hour``:00 New York time."""
    for probe in (ts, ts + H1 - 1):  # local dates touched by the bar
        day = datetime.fromtimestamp(probe, _NY).date()
        if day.weekday() != 4:  # Friday
            continue
        w_start = int(datetime(day.year, day.month, day.day, start_hour, tzinfo=_NY).timestamp())
        w_end = int(datetime(day.year, day.month, day.day, end_hour, tzinfo=_NY).timestamp())
        if ts < w_end and ts + H1 > w_start:
            return True
    return False


def futures_closed(ts: int) -> bool:
    """CFM weekly halt: the bar [ts, ts+3600) overlaps Fri 17:00-18:00 America/New_York."""
    return _overlaps_friday_window(ts, 17, 18)


def entry_blackout(ts: int) -> bool:
    """No NEW entries decided on / filled in this bar: overlaps Fri 16:00-18:00 America/New_York."""
    return _overlaps_friday_window(ts, 16, 18)
