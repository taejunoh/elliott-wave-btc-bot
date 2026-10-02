import numpy as np
import pandas as pd
import pytest

from ewbot.indicators import ATR, atr_series, ema_series

# Hand-computed fixture, n = 3 (bars: high, low, close)
HIGH = np.array([10.0, 12.0, 13.0, 15.0, 18.0])
LOW = np.array([8.0, 9.0, 11.0, 12.0, 16.0])
CLOSE = np.array([9.0, 11.0, 12.0, 14.0, 17.0])
# TR_0 = 10-8 = 2                       (no previous close)
# TR_1 = max(3, |12-9|, |9-9|)  = 3
# TR_2 = max(2, |13-11|, |11-11|) = 2
# ATR_2 = (2+3+2)/3 = 7/3                 (SMA seed)
# TR_3 = max(3, |15-12|, |12-12|) = 3     ATR_3 = (7/3*2 + 3)/3 = 23/9
# TR_4 = max(2, |18-14|, |16-14|) = 4     ATR_4 = (23/9*2 + 4)/3 = 82/27   (gap up: |H-prev close| wins)
EXPECTED = [None, None, 7 / 3, 23 / 9, 82 / 27]


def test_atr_streaming_hand_computed():
    atr = ATR(3)
    assert atr.value is None
    got = [atr.update(h, l, c) for h, l, c in zip(HIGH, LOW, CLOSE)]
    assert got[0] is None and got[1] is None
    assert got[2] == pytest.approx(7 / 3)
    assert got[3] == pytest.approx(23 / 9)
    assert got[4] == pytest.approx(82 / 27)
    assert atr.value == got[4]


def test_atr_series_hand_computed_has_nan_warmup():
    out = atr_series(HIGH, LOW, CLOSE, n=3)
    assert np.isnan(out[:2]).all()
    np.testing.assert_allclose(out[2:], [7 / 3, 23 / 9, 82 / 27])


def test_atr_series_equals_streaming_exactly_on_random_walk():
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 1, 500))
    high = close + rng.uniform(0.1, 2.0, 500)
    low = close - rng.uniform(0.1, 2.0, 500)
    series = atr_series(high, low, close, n=14)
    atr = ATR(14)
    stream = [atr.update(h, l, c) for h, l, c in zip(high, low, close)]
    assert np.isnan(series[:13]).all() and not np.isnan(series[13:]).any()
    assert all(s is None for s in stream[:13])
    np.testing.assert_array_equal(series[13:], np.array(stream[13:]))  # bit-identical


def test_atr_prefix_property_no_lookahead():
    rng = np.random.default_rng(3)
    close = 50 + np.cumsum(rng.normal(0, 1, 120))
    high, low = close + 1.0, close - 1.0
    full = atr_series(high, low, close, 14)
    cut = atr_series(high[:80], low[:80], close[:80], 14)
    np.testing.assert_array_equal(full[:80], cut)


def test_atr_short_input_all_nan_and_empty():
    out = atr_series(HIGH[:2], LOW[:2], CLOSE[:2], n=3)
    assert np.isnan(out).all()
    assert atr_series(np.array([]), np.array([]), np.array([]), n=3).shape == (0,)


def test_atr_rejects_bad_args():
    with pytest.raises(ValueError):
        ATR(0)
    with pytest.raises(ValueError):
        atr_series(HIGH, LOW[:3], CLOSE, n=3)


def test_ema_series_hand_computed():
    # n = 3 -> alpha = 0.5, seeded with x[0]
    # e0 = 1; e1 = .5*2 + .5*1 = 1.5; e2 = .5*3 + .5*1.5 = 2.25; e3 = .5*4 + .5*2.25 = 3.125
    out = ema_series(np.array([1.0, 2.0, 3.0, 4.0]), 3)
    np.testing.assert_allclose(out, [1.0, 1.5, 2.25, 3.125])


def test_ema_series_matches_pandas_ewm_and_edge_cases():
    rng = np.random.default_rng(1)
    x = 100 + np.cumsum(rng.normal(0, 1, 300))
    for n in (1, 5, 50, 200):
        expected = pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()
        np.testing.assert_allclose(ema_series(x, n), expected, rtol=1e-12)
    assert ema_series(np.array([]), 5).shape == (0,)
    np.testing.assert_array_equal(ema_series(np.array([2.0, 5.0]), 1), [2.0, 5.0])  # n=1 -> alpha=1
    with pytest.raises(ValueError):
        ema_series(x, 0)
