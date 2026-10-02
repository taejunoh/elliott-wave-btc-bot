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


def _four_hour(bars: list[Bar]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """High/low/close of the complete UTC 4h buckets, built straight from the raw 1h bars."""
    buckets: dict[int, list[Bar]] = {}
    for b in bars:
        buckets.setdefault(b.ts // H4, []).append(b)
    keys = sorted(k for k, v in buckets.items() if len(v) == H4 // H1)
    assert keys == list(range(keys[0], keys[0] + len(keys)))
    return (np.array([max(x.high for x in buckets[k]) for k in keys]),
            np.array([min(x.low for x in buckets[k]) for k in keys]),
            np.array([buckets[k][-1].close for k in keys]))


def _assert_pivots_confirmed_by_the_book(bars: list[Bar], params: WaveParams) -> int:
    """Independent oracle for spec 4.1: re-derive, from raw 4h bars and a separate ATR, that every pivot the
    engine reports is confirmed on exactly the first 4h CLOSE that retraced K4*ATR from its extreme, and that
    the state shows it at exactly that bar (the engine's own ``confirmed_at`` is not trusted)."""
    states = run_engine(bars, params)
    high, low, close = _four_hour(bars)
    atr = atr_series(high, low, close, params.atr_len)
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
    for p in states[-1].pivots4h:
        c, k, thr = p.confirmed_at, p.kind, params.K4
        ext = high if k > 0 else low
        assert low[p.i] - 1e-9 <= p.price <= high[p.i] + 1e-9           # the extreme lies in its own bar
        assert all(k * (ext[j] - p.price) <= 1e-9 for j in range(p.i, c + 1))   # nothing went further
        assert not math.isnan(atr[c])
        assert k * (p.price - close[c]) >= thr * atr[c] - 1e-9          # (b) retraced K4*ATR on that close
        for j in range(p.i, c):                                         # ... and on no earlier close
            assert math.isnan(atr[j]) or k * (p.price - close[j]) < thr * atr[j] + 1e-9, (p, j)
    return len(states[-1].pivots4h)


@settings(max_examples=40, deadline=None)
@given(paths)
def test_pivots_are_confirmed_exactly_when_the_close_retraces_k_atr(bars: list[Bar]) -> None:
    _assert_pivots_confirmed_by_the_book(bars, P)


@pytest.mark.parametrize("noise", [0.3, 1.0])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_synthetic_pivots_are_confirmed_by_the_book(noise: float, seed: int) -> None:
    params = WaveParams(warmup_4h=50)
    bars, _ = make_path(impulse_abc(), noise, seed, params)
    assert _assert_pivots_confirmed_by_the_book(bars, params) >= 8
