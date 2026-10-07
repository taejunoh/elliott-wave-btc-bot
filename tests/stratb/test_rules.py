from __future__ import annotations

import math

import numpy as np
import pytest

from ewbot.stratb.rules import (
    breakout_level,
    extension_target,
    first_target,
    has_confluence,
    is_divergence,
    pivot_highs,
    pivot_lows,
    retracement_zone,
    rsi_series,
    skip_reason,
    stop_level,
)


def test_rsi_matches_a_hand_computed_wilder_value_and_is_causal():
    close = np.array([10, 11, 10, 12, 11, 13], dtype=float)
    r = rsi_series(close, n=3)
    assert np.isnan(r[:3]).all()
    ag, al = 3 / 3, 1 / 3  # first 3 changes: +1 -1 +2
    assert r[3] == pytest.approx(100 - 100 / (1 + ag / al))
    ag, al = (ag * 2 + 0) / 3, (al * 2 + 1) / 3  # change -1
    assert r[4] == pytest.approx(100 - 100 / (1 + ag / al))
    rng = np.random.default_rng(1)
    long = 100 + rng.normal(0, 1, 300).cumsum()
    full = rsi_series(long)
    for t in (20, 99, 250):
        assert np.array_equal(rsi_series(long[: t + 1]), full[: t + 1], equal_nan=True)


def test_rsi_extremes():
    assert rsi_series(np.arange(20.0), 14)[-1] == 100.0
    assert rsi_series(np.full(20, 5.0), 14)[-1] == 50.0
    assert rsi_series(np.arange(20.0)[::-1], 14)[-1] == 0.0


def test_pivots_are_strict_on_the_left_and_inclusive_on_the_right():
    low = np.array([5, 4, 3, 1, 2, 1, 3, 4, 5, 6], dtype=float)
    assert np.flatnonzero(pivot_lows(low, 3)).tolist() == [3]  # bar 5 ties bar 3 on its left: not a pivot
    assert pivot_lows(low[:6], 3).sum() == 0  # bar 3 needs 3 bars after it
    high = -low
    assert np.flatnonzero(pivot_highs(high, 3)).tolist() == [3]
    assert pivot_lows(np.ones(5), 3).sum() == 0


def test_zone_extension_and_confluence_for_both_legs():
    assert retracement_zone(1000.0, 1200.0, 0.382, 0.618) == pytest.approx((1076.4, 1123.6))
    assert retracement_zone(1200.0, 1000.0, 0.382, 0.618) == pytest.approx((1076.4, 1123.6))
    assert retracement_zone(1000.0, 1200.0, 0.5, 0.786) == pytest.approx((1042.8, 1100.0))
    assert extension_target(1000.0, 1200.0, 1.272) == pytest.approx(1254.4)
    assert extension_target(1200.0, 1000.0, 1.618) == pytest.approx(876.4)
    zone = (1076.4, 1123.6)
    assert has_confluence(zone, [900.0, 1100.0], 0.0)
    assert not has_confluence(zone, [1070.0, 1130.0], 5.0)
    assert has_confluence(zone, [1070.0], 6.5) and has_confluence(zone, [1130.0], 6.5)


def test_divergence_both_sides_and_nan():
    assert is_divergence(1, 100.0, 99.0, 20.0, 25.0)
    assert not is_divergence(1, 100.0, 99.0, 25.0, 20.0)
    assert not is_divergence(1, 99.0, 100.0, 20.0, 25.0)
    assert is_divergence(-1, 100.0, 101.0, 80.0, 75.0)
    assert not is_divergence(-1, 100.0, 101.0, 75.0, 80.0)
    assert not is_divergence(1, 100.0, 99.0, math.nan, 25.0)


def test_breakout_level_is_the_swing_between_the_pivots():
    high = np.array([5, 9, 7, 8, 4], dtype=float)
    low = np.array([1, 3, 2, 4, 0], dtype=float)
    assert breakout_level(1, high, low, 0, 4) == 9.0
    assert breakout_level(-1, high, low, 0, 4) == 2.0
    with pytest.raises(ValueError):
        breakout_level(1, high, low, 2, 3)


def test_stop_and_first_target():
    assert stop_level(1, 100.0, 10.0, 0.2) == 98.0 and stop_level(-1, 100.0, 10.0, 0.2) == 102.0
    assert first_target(110.0, 100.0, 1.5) == 125.0 and first_target(90.0, 100.0, 1.0) == 80.0


def test_skip_reasons():
    assert skip_reason(1, 110.0, 100.0, 120.0, 130.0, atr1h=5.0, max_stop_atr1h=2.5) is None
    assert skip_reason(1, 110.0, 100.0, 120.0, 130.0, atr1h=3.9, max_stop_atr1h=2.5) == "stop_too_far"
    assert skip_reason(1, 110.0, 100.0, 120.0, 120.0, atr1h=5.0, max_stop_atr1h=2.5) == "tp2_not_beyond_tp1"
    assert skip_reason(1, 100.0, 100.0, 100.0, 130.0, atr1h=5.0, max_stop_atr1h=2.5) == "stop_wrong_side"
    assert skip_reason(-1, 90.0, 100.0, 80.0, 70.0, atr1h=5.0, max_stop_atr1h=2.5) is None
    assert skip_reason(-1, 90.0, 100.0, 80.0, 85.0, atr1h=5.0, max_stop_atr1h=2.5) == "tp2_not_beyond_tp1"
