"""Synthetic 15m candles for the strategy-B tests (no network, no real cache).

LONG_SETUP is a hand-made path whose 1h ZigZag (k = 2 or 3) confirms an up leg L = 999.5 -> H = 1200.5
with an earlier swing high (1100.5) inside the 38.2-61.8% zone; the retracement makes two 15m lows
1109.5 (RSI 13.9) and 1104.5 (RSI 25.4, inside the zone) and the close 1130 at bar 177 breaks the 1125.5
swing between them: one long signal at bar 177. Prices sit on a 1/64 grid, so mirror() is exact.
world_dfs() is the two-asset world of the holdout, paper and CLI tests: BTC repeats LONG_SETUP, ETH is
its mirror, so both assets signal on the same bars (a BTC long and an ETH short every 232 bars)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from ewbot.stratb.data15 import cache_paths

T0 = 1_700_006_400  # 2023-11-15 00:00 UTC (hour aligned)
RANDOM_T0 = 1_690_848_000  # 2023-08-01 00:00 UTC: 6,000 random bars end before 2024
M15 = 900
LONG_SETUP = [(40, 1040), (30, 1100), (25, 1000), (25, 1200), (40, 1110), (5, 1125), (7, 1105), (20, 1205), (40, 1180)]
LONG_START = 1060.0
SIGNAL_BAR = 177


def path(start: float, segments: list[tuple[int, float]]) -> list[float]:
    """Closes along straight segments: (number of bars, target close) each, from `start`."""
    out = [float(start)]
    for n, target in segments:
        a = out[-1]
        out += [a + (target - a) * (k + 1) / n for k in range(n)]
    return out


def frame_df(closes, t0: int = T0, wick: float = 0.5) -> pd.DataFrame:
    """15m bars: open = previous close, high/low = body -/+ wick (prices rounded to 1/64)."""
    c = np.round(np.asarray(closes, dtype=float) * 64) / 64
    o = np.concatenate([[c[0]], c[:-1]])
    return pd.DataFrame(
        {"ts": t0 + M15 * np.arange(c.size), "open": o, "high": np.maximum(o, c) + wick,
         "low": np.minimum(o, c) - wick, "close": c, "volume": 1.0}
    ).astype({"ts": "int64"})


def long_setup_df(t0: int = T0) -> pd.DataFrame:
    return frame_df(path(LONG_START, LONG_SETUP), t0)


def mirror(df: pd.DataFrame, ref: float = 1200.0) -> pd.DataFrame:
    """p -> 2 ref - p (high and low swap): every long structure becomes the mirrored short one."""
    out = df.copy()
    out["open"], out["close"] = 2 * ref - df["open"], 2 * ref - df["close"]
    out["high"], out["low"] = 2 * ref - df["low"], 2 * ref - df["high"]
    return out


def random_df(n: int, seed: int, t0: int = RANDOM_T0, start: float = 20_000.0) -> pd.DataFrame:
    """Random piecewise-linear 15m path (segments of 4-39 bars) with noise and random wicks."""
    rng = np.random.default_rng(seed)
    c = [start]
    while len(c) < n:
        n_seg = int(rng.integers(4, 40))
        target = c[-1] + rng.normal(0, 1) * n_seg * 12
        step = (target - c[-1]) / n_seg
        c += [c[-1] + step * (k + 1) + rng.normal(0, 4) for k in range(n_seg)]
    c = np.round(np.array(c[:n]) * 64) / 64
    o = np.concatenate([[c[0]], c[:-1]])
    up = np.round(np.abs(rng.normal(0, 6, n)) * 64) / 64
    dn = np.round(np.abs(rng.normal(0, 6, n)) * 64) / 64
    return pd.DataFrame(
        {"ts": t0 + M15 * np.arange(n), "open": o, "high": np.maximum(o, c) + up, "low": np.minimum(o, c) - dn,
         "close": c, "volume": 1.0}
    ).astype({"ts": "int64"})


def world_dfs(t0: int, reps: int = 130) -> dict[str, pd.DataFrame]:
    btc = frame_df(path(LONG_START, LONG_SETUP * reps), t0)
    return {"BTC": btc, "ETH": mirror(btc)}


def save_world(cache_dir: Path, dfs: dict[str, pd.DataFrame]) -> None:
    """Each asset's bars into its 15m cache file below cache_dir."""
    for a, p in cache_paths(cache_dir).items():
        dfs[a].to_parquet(p, index=False)
