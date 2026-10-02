from __future__ import annotations

import json

import pytest

from ewbot.config import WaveParams
from tests.synth import (
    impulse_abc,
    label_hours,
    make_path,
    nominal_thr,
    recovered,
    recovery_curve,
    run_engine,
)

P = WaveParams(warmup_4h=50)
WAY = impulse_abc()
THR = nominal_thr(WAY, P)
PRICE = {w.label: w.price for w in WAY if w.label}
# leg-end label -> expected top (pattern, d, phase, labels of its confirmed points)
EXPECTED_TOP = {
    "2": ("IMP", 1, "W2", "S1"),      # setup 1 (W3 entry) phase
    "3": ("IMP", 1, "W3", "S12"),
    "4": ("IMP", 1, "W4", "S123"),    # setup 2 (W5 entry) phase
    "5": ("IMP", 1, "W5", "S1234"),   # setup 3 (W5 reversal) phase
    "C": ("ABC", -1, "C", "5AB"),     # setup 4 (C end) phase
}


def test_grammar_ratios() -> None:
    w1 = PRICE["1"] - PRICE["S"]
    assert w1 == 100.0
    assert (PRICE["1"] - PRICE["2"]) / w1 == pytest.approx(0.55)
    assert (PRICE["3"] - PRICE["2"]) / w1 == pytest.approx(1.618, abs=1e-3)
    assert (PRICE["3"] - PRICE["4"]) / (PRICE["3"] - PRICE["2"]) == pytest.approx(0.3, abs=1e-3)
    assert (PRICE["5"] - PRICE["4"]) / w1 == pytest.approx(1.0, abs=1e-3)
    a = PRICE["5"] - PRICE["A"]
    assert (PRICE["B"] - PRICE["A"]) / a == pytest.approx(0.618, abs=1e-3)
    assert (PRICE["B"] - PRICE["C"]) / a == pytest.approx(1.0, abs=1e-3)
    assert 11.0 < THR < 13.0          # ~1 price unit per hour -> 4h ATR ~4 -> thr ~12


@pytest.mark.parametrize("noise", [0.0, 0.1, 0.2, 0.3])
@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_low_noise_recovers_pivots_top_labels_and_setup_phases(noise: float, seed: int) -> None:
    bars, hours = make_path(WAY, noise, seed, P, thr=THR)
    states = run_engine(bars, P)
    assert recovered(states, WAY, hours)
    at = label_hours(WAY, hours)
    for label, (pattern, d, phase, pts) in EXPECTED_TOP.items():
        s = states[at[label]]                      # last 1h bar of the leg (E_live == the waypoint)
        assert s.ready and s.E_live == PRICE[label]
        assert s.top is not None
        assert (s.top.pattern, s.top.d, s.top.phase) == (pattern, d, phase)
        assert s.top.prices == tuple(PRICE[c] for c in pts)
        assert s.s_rev > 0                         # the leg is "done" by consensus at its end


def test_recovery_curve_is_recorded(tmp_path) -> None:
    levels = [0.0, 0.3, 1.0, 2.0, 3.0]
    curve = recovery_curve(levels, range(6), P, WAY)
    out = tmp_path / "recovery_curve.json"
    out.write_text(json.dumps({str(k): v for k, v in curve.items()}))
    print("pivot recovery rate by noise/thr:", curve)
    assert list(curve) == levels
    assert curve[0.0] == curve[0.3] == 1.0
    assert all(0.0 <= v <= 1.0 for v in curve.values())
    assert curve[3.0] < 1.0                       # heavy noise must break exact recovery
