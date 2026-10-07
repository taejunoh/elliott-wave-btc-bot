from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from ewbot.stratb.config import StratBParams
from ewbot.stratb.signals import build_frame, generate_signals, reference_legs
from tests.stratb.helpers import SIGNAL_BAR, T0, long_setup_df, mirror, random_df

M15 = 900


def _key(s):
    return (s.i, s.ts, s.direction, s.entry_ref, s.stop, s.tp1, s.tp2, s.p1_ts, s.p2_ts, s.level)


@pytest.fixture(scope="module")
def setup():
    p = StratBParams(zz_k=2.0)
    f = build_frame(long_setup_df(), p)
    return p, f, reference_legs(f, p.zz_k, p)


def test_frame_has_complete_hours_and_flags_gaps():
    df = long_setup_df(T0 + 2 * M15).drop(index=[10])  # starts mid-hour, one bar missing
    f = build_frame(df)
    assert f.ts[0] == T0 + 2 * M15 and f.synthetic.sum() == 1 and f.synthetic[10]
    assert f.h_ts[0] == T0 + 3600 and (f.h_ts % 3600 == 0).all()
    assert (f.ts[f.h_end] == f.h_ts + 2700).all()
    assert f.last_hour[int(f.h_end[0]) - 1] == -1 and f.last_hour[int(f.h_end[0])] == 0


def test_legs_alternate_and_carry_the_earlier_swings(setup):
    _, f, legs = setup
    assert [g.direction for g in legs] == [1, -1, 1, -1, 1]
    up = legs[2]
    assert (up.start, up.end, up.known_i) == (999.5, 1200.5, 135)
    assert up.prior == (1039.5, 1100.5) and f.high[up.end_i] == 1200.5
    assert up.known_i % 4 == 3 and f.ts[up.known_i] % 3600 == 2700


def test_the_long_setup_gives_one_signal_with_the_spec_levels(setup):
    p, f, legs = setup
    (s,) = generate_signals(f, p, legs)
    assert (s.i, s.direction, s.entry_ref, s.level) == (SIGNAL_BAR, 1, 1130.0, 1125.5)
    assert (s.p1_ts, s.p2_ts) == (int(f.ts[160]), int(f.ts[172])) and f.low[172] == 1104.5
    assert s.stop == pytest.approx(1104.5 - 0.2 * f.atr15[SIGNAL_BAR])
    assert s.tp1 == pytest.approx(1130.0 + (1130.0 - s.stop)) and s.tp2 == pytest.approx(999.5 + 1.272 * 201.0)
    assert f.rsi[172] > f.rsi[160]
    assert [x.i for x in generate_signals(build_frame(long_setup_df()), StratBParams(zz_k=3.0))] == [SIGNAL_BAR]


def test_the_mirrored_path_gives_the_mirrored_short(setup):
    p, _, _ = setup
    f = build_frame(mirror(long_setup_df()), p)
    (s,) = generate_signals(f, p)
    assert (s.i, s.direction, s.entry_ref, s.level) == (SIGNAL_BAR, -1, 2400 - 1130.0, 2400 - 1125.5)
    assert s.stop == pytest.approx(2400 - (1104.5 - 0.2 * f.atr15[SIGNAL_BAR]))
    assert s.tp2 == pytest.approx(2400 - (999.5 + 1.272 * 201.0))


def test_no_confluence_no_signal(setup):
    p, f, legs = setup
    legs = [dataclasses.replace(g, prior=(1300.0,)) for g in legs]
    assert generate_signals(f, p, legs) == []


def test_a_close_below_l_invalidates_and_an_undercut_kills_the_pair(setup):
    p, f, legs = setup
    close = f.close.copy()
    close[170] = 999.0  # below L = 999.5, between the leg's confirmation (135) and the signal
    assert generate_signals(dataclasses.replace(f, close=close), p, legs) == []
    close[170] = 1000.0
    assert len(generate_signals(dataclasses.replace(f, close=close), p, legs)) == 1
    low = f.low.copy()
    low[175] = 1104.0  # below the second pivot low 1104.5 after it formed
    assert generate_signals(dataclasses.replace(f, low=low), p, legs) == []


def test_filters_and_expiry(setup):
    p, f, legs = setup
    assert generate_signals(f, dataclasses.replace(p, max_stop_atr1h=0.5), legs) == []  # stop too far
    assert generate_signals(f, dataclasses.replace(p, tp1_r=5.0), legs) == []  # TP2 not beyond TP1
    assert generate_signals(f, dataclasses.replace(p, confirm_hours=10), legs) == []  # 72 h -> 10 h: expired


def test_a_gap_filled_bar_blocks_the_confirmation():
    df = long_setup_df().drop(index=[174]).reset_index(drop=True)
    p = StratBParams(zz_k=2.0)
    f = build_frame(df, p)
    assert f.synthetic[174] and generate_signals(f, p) == []


@pytest.mark.parametrize("seed", [0, 3, 4])
def test_signals_up_to_t_never_change_when_later_bars_are_cut(seed):
    df = random_df(6000, seed)
    for p in (StratBParams(zz_k=2.0), StratBParams(zz_k=3.0, zone=(0.5, 0.786))):
        full = generate_signals(build_frame(df, p), p)
        assert full, "the seed must produce signals"
        cuts = sorted({s.i for s in full} | {s.i - 1 for s in full} | {1000, 2999, 4500, 5998})
        for t in cuts:
            got = generate_signals(build_frame(df.iloc[: t + 1], p), p)
            assert [_key(s) for s in got] == [_key(s) for s in full if s.i <= t], (seed, t)
