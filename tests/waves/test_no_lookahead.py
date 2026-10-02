from __future__ import annotations

import dataclasses
import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ewbot.config import WaveParams
from ewbot.indicators import atr_series
from ewbot.types import H1, H4, Bar
from ewbot.waves.engine import WaveState
from tests.synth import T0, impulse_abc, make_path, random_bars, run_engine

# small K4 / ATR / warmup so a few hundred bars produce many pivots and counts
P = WaveParams(K4=1.5, atr_len=5, warmup_4h=10)
paths = st.builds(random_bars, st.integers(0, 2**31 - 1), st.integers(120, 720),
                  st.sampled_from([3.0, 6.0, 12.0]))


def _assert_causal(states: list[WaveState]) -> None:
    """Spec 10 no-lookahead (3): every pivot used was confirmed on a CLOSED 4h bar <= now."""
    prev: tuple = ()
    for k, s in enumerate(states):
        assert s.ts == T0 + k * H1
        assert s.closed_4h == ((s.ts + H1) % H4 == 0)          # 4h bar visible only at its close
        assert s.i4h == (k + 1) // 4 - 1
        assert s.pivots4h[: len(prev)] == prev                 # append-only
        prev = s.pivots4h
        known = set(s.pivots4h)
        for p in s.pivots4h:
            assert p.i <= p.confirmed_at <= s.i4h and p.sub_ts <= s.ts
        for c in s.counts:
            assert c.leg_dir == s.leg_dir
            for p in c.points:
                assert p in known and p.confirmed_at <= s.i4h and p.sub_ts <= s.ts
        assert s.leg_start == (s.pivots4h[-1] if s.pivots4h else None)
        assert s.E_live_ts <= s.ts
        if s.new_pivot_4h is not None:
            assert s.closed_4h and s.new_pivot_4h == s.pivots4h[-1] and s.new_pivot_4h.confirmed_at == s.i4h
        assert len(s.probs) == len(s.counts)
        assert -1.0 - 1e-9 <= s.s_rev <= 1.0 + 1e-9


@settings(max_examples=40, deadline=None)
@given(paths, st.data())
def test_prefix_invariance(bars: list[Bar], data: st.DataObject) -> None:
    full = run_engine(bars, P)
    _assert_causal(full)
    t = data.draw(st.integers(1, len(bars)))
    assert run_engine(bars[:t], P) == full[:t]


def _poison(b: Bar, how: str) -> Bar:
    if how == "x10":
        return dataclasses.replace(b, open=b.open * 10, high=b.high * 10, low=b.low * 10, close=b.close * 10)
    nan = math.nan
    return dataclasses.replace(b, open=nan, high=nan, low=nan, close=nan)


@settings(max_examples=40, deadline=None)
@given(paths, st.data(), st.sampled_from(["x10", "nan"]))
def test_poisoned_future_does_not_change_the_past(bars: list[Bar], data: st.DataObject, how: str) -> None:
    """Guards hidden cross-run/global state only. In a sequential streaming harness the engine can never
    see the poisoned future, so this cannot catch a pivot whose confirmed_at is backdated: that is the job
    of the independent oracle test below."""
    t = data.draw(st.integers(1, len(bars) - 1))
    clean = run_engine(bars, P)
    poisoned = bars[:t] + [_poison(b, how) for b in bars[t:]]
    assert run_engine(poisoned, P)[:t] == clean[:t]


@settings(max_examples=30, deadline=None)
@given(paths)
def test_streaming_equals_batch_and_cache_equals_fresh(bars: list[Bar]) -> None:
    streamed = run_engine(bars, P)
    assert run_engine(bars, P, batch=True) == streamed           # 4h bars from aggregate_4h
    assert run_engine(bars, P, use_cache=False) == streamed      # fresh enumeration at every 4h close


def test_synthetic_path_is_causal_too() -> None:
    bars, _ = make_path(impulse_abc(), 0.3, 7, WaveParams(warmup_4h=50))
    _assert_causal(run_engine(bars, WaveParams(warmup_4h=50)))


def _four_hour(bars: list[Bar]) -> tuple[int, np.ndarray, np.ndarray, np.ndarray]:
    """First complete UTC 4h bucket (ts // H4) and the high/low/close of every complete bucket, built straight
    from the raw 1h bars."""
    buckets: dict[int, list[Bar]] = {}
    for b in bars:
        buckets.setdefault(b.ts // H4, []).append(b)
    keys = sorted(k for k, v in buckets.items() if len(v) == H4 // H1)
    assert keys == list(range(keys[0], keys[0] + len(keys)))
    return (keys[0],
            np.array([max(x.high for x in buckets[k]) for k in keys]),
            np.array([min(x.low for x in buckets[k]) for k in keys]),
            np.array([buckets[k][-1].close for k in keys]))


def _assert_pivots_confirmed_by_the_book(bars: list[Bar], params: WaveParams) -> int:
    """Independent oracle for spec 4.1: re-derive, from raw 1h/4h bars and a separate ATR, that every pivot the
    engine reports is the extreme of its own leg and is confirmed on exactly the first 4h CLOSE of that leg that
    retraced K4*ATR from it, and that the state shows it at exactly that bar (the engine's own ``confirmed_at``
    is not trusted). A pivot's leg starts at the previous pivot's 1h bar (only that bar's CLOSE counts: spec 4.1
    same-bar rule) and can confirm it only from the bar after the previous pivot was confirmed (pivots alternate,
    at most one per bar), even when its extreme lies before that bar. The init rule's "ambiguous single atom:
    wait" branch is not modelled (never reached by these generators)."""
    states = run_engine(bars, params)
    k0, high, low, close = _four_hour(bars)
    atr = atr_series(high, low, close, params.atr_len)
    close1h = {b.ts: b.close for b in bars}
    seen = 0
    for s in states:
        new = s.pivots4h[seen:]
        assert len(new) <= 1
        if new:
            # (a) the pivot first appears on the 4h close at which it claims to be confirmed
            assert s.closed_4h and s.new_pivot_4h == new[0] and s.i4h == new[0].confirmed_at
        else:
            assert s.new_pivot_4h is None
        seen = len(s.pivots4h)
    pivots = states[-1].pivots4h
    for n, p in enumerate(pivots):
        c, k, thr = p.confirmed_at, p.kind, params.K4
        prev = pivots[n - 1] if n else None
        assert low[p.i] - 1e-9 <= p.price <= high[p.i] + 1e-9           # the extreme lies in its own bar
        leg = [b.high if k > 0 else b.low for b in bars                 # nothing in its leg went further
               if 0 <= b.ts // H4 - k0 <= c and (prev is None or b.ts > prev.sub_ts)]
        leg += [close1h[prev.sub_ts]] if prev is not None else []
        assert all(k * (x - p.price) <= 1e-9 for x in leg), p
        assert not math.isnan(atr[c])
        assert k * (p.price - close[c]) >= thr * atr[c] - 1e-9          # (b) retraced K4*ATR on that close
        start = p.i if prev is None else max(p.i, prev.confirmed_at + 1)
        for j in range(start, c):                                       # ... and on no earlier close of its leg
            assert math.isnan(atr[j]) or k * (p.price - close[j]) < thr * atr[j] + 1e-9, (p, j)
    return len(pivots)


@settings(max_examples=40, deadline=None)
@given(paths)
def test_pivots_are_confirmed_exactly_when_the_close_retraces_k_atr(bars: list[Bar]) -> None:
    _assert_pivots_confirmed_by_the_book(bars, P)


def _bars_from_hlc(hlc: list[tuple[float, float, float]]) -> list[Bar]:
    """1h bars from T0 with open = previous close (the first open = its own close), as random_bars builds them."""
    bars, o = [], hlc[0][2]
    for k, (h, lo, c) in enumerate(hlc):
        bars.append(Bar(T0 + k * H1, o, h, lo, c, 1.0))
        o = c
    return bars


# 1h (high, low, close) of random_bars(245, 584, 3.0)[:60] = 15 complete 4h bars: the Hypothesis counterexample
# that the first version of the oracle (which checked closes from the extreme's own bar on) rejected.
_LATE_LEG_HLC = [
    (5002.46875, 4999.40625, 4999.65625), (5000.71875, 4996.109375, 4998.828125),
    (5000.34375, 4997.765625, 4999.328125), (5001.359375, 4990.015625, 4991.21875),
    (4991.5625, 4987.140625, 4987.921875), (4988.25, 4985.703125, 4986.40625),
    (4993.859375, 4986.34375, 4993.265625), (5011.171875, 4991.734375, 5010.234375),
    (5010.6875, 4999.0, 5001.46875), (5003.015625, 4999.1875, 4999.703125),
    (5000.171875, 4994.890625, 4996.203125), (5003.359375, 4994.078125, 5001.546875),
    (5001.921875, 5000.328125, 5000.59375), (5001.71875, 4996.9375, 4998.015625),
    (5001.234375, 4997.09375, 5001.109375), (5004.8125, 4999.375, 4999.625),
    (5002.46875, 4997.59375, 5001.921875), (5004.046875, 4996.96875, 4997.84375),
    (4999.859375, 4996.984375, 4997.75), (5000.515625, 4992.078125, 4995.78125),
    (4997.890625, 4989.53125, 4989.640625), (4990.09375, 4986.171875, 4988.265625),
    (4993.171875, 4988.046875, 4993.140625), (4995.1875, 4991.9375, 4992.28125),
    (4995.6875, 4991.328125, 4994.421875), (4997.84375, 4993.859375, 4995.828125),
    (4998.296875, 4995.703125, 4997.046875), (4998.546875, 4996.59375, 4997.96875),
    (5000.1875, 4995.8125, 4999.1875), (5000.5, 4997.390625, 4999.984375),
    (5001.78125, 4999.484375, 5000.265625), (5001.234375, 4992.3125, 4994.546875),
    (4995.640625, 4994.3125, 4995.234375), (4999.75, 4993.875, 4999.171875),
    (5002.640625, 4998.21875, 5001.5), (5001.734375, 5001.328125, 5001.40625),
    (5004.28125, 5001.046875, 5001.765625), (5008.171875, 5001.453125, 5005.34375),
    (5009.515625, 5003.96875, 5007.109375), (5007.859375, 5001.15625, 5003.234375),
    (5009.625, 5002.765625, 5008.09375), (5008.8125, 5002.296875, 5003.1875),
    (5006.78125, 5002.921875, 5002.96875), (5006.25, 5001.796875, 5003.40625),
    (5003.484375, 4999.796875, 5002.078125), (5006.296875, 5001.015625, 5006.046875),
    (5006.609375, 4995.375, 4996.4375), (4997.734375, 4993.421875, 4994.125),
    (4997.03125, 4993.90625, 4996.59375), (4999.71875, 4995.96875, 4996.890625),
    (4999.125, 4996.484375, 4997.484375), (5002.5, 4996.65625, 5000.59375),
    (5002.1875, 4992.640625, 4994.921875), (4999.078125, 4994.28125, 4997.53125),
    (5002.546875, 4997.28125, 5002.203125), (5004.484375, 5001.921875, 5002.203125),
    (5006.890625, 5000.609375, 5004.375), (5005.46875, 5002.5625, 5005.171875),
    (5005.296875, 4999.78125, 5003.6875), (5003.890625, 5002.078125, 5003.234375),
]


def test_extreme_retraced_before_its_leg_began_is_confirmed_only_after_the_previous_pivot() -> None:
    """The low at 4h bar 5 is already 1.5 ATR below the bar-9 close, but it only becomes a leg extreme when the
    high at bar 1 is confirmed (bar 11); the first close of its own leg that retraces K4*ATR is bar 14's."""
    bars = _bars_from_hlc(_LATE_LEG_HLC)
    piv = run_engine(bars, P)[-1].pivots4h
    assert [(p.i, p.price, p.kind, p.confirmed_at) for p in piv] == [
        (1, 4985.703125, -1, 9), (1, 5011.171875, 1, 11), (5, 4986.171875, -1, 14)]
    _, high, low, close = _four_hour(bars)
    atr = atr_series(high, low, close, P.atr_len)
    assert close[9] - 4986.171875 >= P.K4 * atr[9]          # what the first oracle mistook for a missed confirmation
    assert _assert_pivots_confirmed_by_the_book(bars, P) == 3


def test_previous_pivots_own_1h_bar_counts_only_by_its_close() -> None:
    """Spec 4.1 same-bar rule: the 1h bar of the low pivot (95) spikes to 100.75, but only its close seeds the up
    leg, so the high pivot is 99.75 although its 4h bar's high is 100.75."""
    bars = _bars_from_hlc([(101.0, 99.0, 100.0)] * 8 + [
        (100.75, 95.0, 95.5), (98.0, 95.25, 97.75), (99.75, 97.5, 99.5), (99.6, 99.0, 99.25),
        (99.5, 97.0, 97.25), (97.5, 95.75, 96.0), (96.25, 95.5, 95.75), (96.0, 95.25, 95.5)])
    params = WaveParams(K4=1.0, atr_len=2, warmup_4h=2)
    piv = run_engine(bars, params)[-1].pivots4h
    assert [(p.i, p.price, p.kind, p.confirmed_at) for p in piv] == [(2, 95.0, -1, 2), (2, 99.75, 1, 3)]
    assert _four_hour(bars)[1][2] == 100.75
    assert _assert_pivots_confirmed_by_the_book(bars, params) == 2


@pytest.mark.parametrize("noise", [0.3, 1.0])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_synthetic_pivots_are_confirmed_by_the_book(noise: float, seed: int) -> None:
    params = WaveParams(warmup_4h=50)
    bars, _ = make_path(impulse_abc(), noise, seed, params)
    assert _assert_pivots_confirmed_by_the_book(bars, params) >= 8
