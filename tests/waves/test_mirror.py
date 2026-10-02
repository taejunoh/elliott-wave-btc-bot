from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from ewbot.config import WaveParams
from ewbot.waves.engine import WaveState
from tests.synth import impulse_abc, make_path, mirror_bars, random_bars, run_engine

P_REF = 4096.0          # p' = 2*P_REF - p is exact on the 1/64 grid


def _view(s: WaveState, flip: int) -> tuple:
    """Direction-free summary: d flips, phases/points/q/probs/S_rev stay equal under the mirror."""
    return (
        s.ready, s.closed_4h, s.i4h, flip * s.leg_dir, s.E_live_ts, s.s_rev, s.probs, s.q_top,
        tuple((c.pattern, flip * c.d, c.phase, tuple((p.i, p.sub_ts, p.confirmed_at) for p in c.points), c.q)
              for c in s.counts),
        frozenset((pat, flip * d, i) for pat, d, i in s.lineages),
        tuple((p.i, p.sub_ts, flip * p.kind, p.confirmed_at) for p in s.pivots4h),
        None if s.top is None else (s.top.pattern, flip * s.top.d, s.top.phase),
    )


def _assert_mirrored(bars: list, params: WaveParams) -> int:
    up = run_engine(bars, params)
    dn = run_engine(mirror_bars(bars, P_REF), params)
    n_counts = 0
    for a, b in zip(up, dn, strict=True):
        assert _view(a, 1) == _view(b, -1)
        if a.leg_dir:
            assert b.E_live == 2 * P_REF - a.E_live
        assert [2 * P_REF - p.price for p in a.pivots4h] == [p.price for p in b.pivots4h]
        n_counts += len(a.counts)
    return n_counts


def test_mirror_of_synthetic_impulse() -> None:
    params = WaveParams(warmup_4h=50)
    bars, _ = make_path(impulse_abc(), 0.2, 3, params)
    assert _assert_mirrored(bars, params) > 0
    up_top = run_engine(bars, params)[-60].top
    dn_top = run_engine(mirror_bars(bars, P_REF), params)[-60].top
    assert up_top.d == -dn_top.d and up_top.phase == dn_top.phase


@settings(max_examples=25, deadline=None)
@given(st.integers(0, 2**31 - 1), st.integers(120, 600))
def test_mirror_property_on_random_paths(seed: int, n: int) -> None:
    _assert_mirrored(random_bars(seed, n), WaveParams(K4=1.5, atr_len=5, warmup_4h=10))
