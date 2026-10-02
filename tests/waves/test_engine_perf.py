from __future__ import annotations

import math
import time

import numpy as np

from ewbot.config import WaveParams
from ewbot.types import H1, Bar
from ewbot.waves.engine import WaveEngine
from tests.synth import T0, feed


def _gbm_bars(n: int, seed: int = 0, vol: float = 0.007) -> list[Bar]:
    """BTC-like hourly GBM (fat tails, ~0.7%/h) starting at 1000."""
    rng = np.random.default_rng(seed)
    c = 1000.0 * np.exp(np.cumsum(rng.standard_t(4, n) * vol / math.sqrt(2.0)))
    o = np.concatenate([[1000.0], c[:-1]])
    up = np.abs(rng.normal(0.0, vol / 2.0, n)) * c
    dn = np.abs(rng.normal(0.0, vol / 2.0, n)) * c
    return [Bar(T0 + k * H1, float(o[k]), float(max(o[k], c[k]) + up[k]), float(min(o[k], c[k]) - dn[k]),
                float(c[k]), 1.0) for k in range(n)]


def test_engine_runs_80k_hourly_bars_well_under_three_minutes() -> None:
    bars = _gbm_bars(80_000)
    for k4 in (2.0, 3.0):                       # the grid's most pivot-dense and the default K4
        eng = WaveEngine(WaveParams(K4=k4))
        t = time.perf_counter()
        n_counts = sum(len(eng.on_1h_close(*x).counts) for x in feed(bars))
        dt = time.perf_counter() - t
        print(f"K4={k4}: 80k bars in {dt:.1f}s, avg counts {n_counts / 80_000:.1f}")
        assert n_counts > 0
        assert dt < 180.0
