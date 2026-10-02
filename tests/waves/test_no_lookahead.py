from __future__ import annotations

import dataclasses
import math

from hypothesis import given, settings
from hypothesis import strategies as st

from ewbot.config import WaveParams
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
