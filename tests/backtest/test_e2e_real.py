"""End-to-end smoke on the REAL Coinbase cache (skipped until `ewbot fetch` has been run)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from ewbot.backtest.runner import compare, utc_ts
from ewbot.config import BotConfig
from ewbot.data.candles import fill_gaps, load_1h

CACHE = Path("data/cache/BTC-USD_1h.parquet")

pytestmark = pytest.mark.skipif(not CACHE.exists(), reason="no real data: run `uv run ewbot fetch`")


def test_compare_runs_on_three_months_of_real_data():
    start, end = utc_ts("2019-01-01"), utc_ts("2019-04-01")
    df = fill_gaps(load_1h(CACHE))
    df = df[(df["ts"] >= utc_ts("2018-10-01")) & (df["ts"] < end)].reset_index(drop=True)  # 3 months warm-up
    assert len(df) > 24 * 150
    cmp = compare(BotConfig(), df, start, end, random_runs=2, workers=1)
    assert set(cmp["comparison"]) == {"A", "C", "HYBRID", "BH", "EMA"}
    for name, m in cmp["comparison"].items():
        assert all(math.isfinite(float(v)) for v in m.values()), name
        assert 0.0 <= m["max_dd"] <= 1.0
    bh = cmp["comparison"]["BH"]
    assert bh["n_trades"] == 1 and bh["exposure"] == 1.0
    assert -0.3 < bh["total_return"] < 0.6  # BTC went ~3.7k -> ~4.1k over Q1 2019 (about +10%)
    # spot hold (T19): raw price multiple minus two fees and two slippages, no funding
    c = BotConfig().costs
    w = df[(df["ts"] >= start) & (df["ts"] < end)]
    multiple = float(w["close"].iloc[-1]) / float(w["open"].iloc[1])
    spot = multiple * ((1 - c.slippage) / (1 + c.slippage)) * ((1 - c.fee_rate) / (1 + c.fee_rate)) - 1
    assert bh["total_return"] == pytest.approx(spot, rel=1e-9)
    assert cmp["random"]["runs"] in (0, 2)
