from __future__ import annotations

import pytest

from ewbot.waves.patterns import (
    N_POINTS,
    Invalidation,
    _completed_ok,
    hard_rules_ok,
    invalidation,
    next_leg_dir,
)

# Reference bull impulse: S=100 P1=120 P2=108 P3=150 P4=138
# W1=20, W2=12 (r2=0.6), W3=42 (2.1 x W1), W4=12 (r4=0.2857); H1 108>100, H2 150>120, H4 138>120
BULL = (100.0, 120.0, 108.0, 150.0, 138.0)
BEAR = tuple(200.0 - p for p in BULL)           # mirror: 100, 80, 92, 50, 62


def test_n_points() -> None:
    assert N_POINTS == {"W2": 2, "W3": 3, "W4": 4, "W5": 5, "B": 2, "C": 3}


@pytest.mark.parametrize(
    ("phase", "E"),
    [("W2", 110.0), ("W3", 130.0), ("W4", 125.0), ("W5", 160.0)],
)
def test_reference_bull_impulse_is_valid_in_every_phase(phase: str, E: float) -> None:
    pts = BULL[: N_POINTS[phase]]
    assert hard_rules_ok("IMP", 1, phase, pts, E)
    assert hard_rules_ok("IMP", -1, phase, tuple(200.0 - p for p in pts), 200.0 - E)


# ---- H1: P2 > S -------------------------------------------------------------------------------
def test_h1_confirmed_p2_below_s_is_invalid() -> None:
    assert not hard_rules_ok("IMP", 1, "W3", (100.0, 120.0, 99.0), 130.0)


def test_h1_in_progress_w2_boundary() -> None:
    assert hard_rules_ok("IMP", 1, "W2", (100.0, 120.0), 100.0)     # touching S is allowed (inval is LOW < S)
    assert not hard_rules_ok("IMP", 1, "W2", (100.0, 120.0), 99.99)
    assert not hard_rules_ok("IMP", -1, "W2", (100.0, 80.0), 100.01)  # bear: E above S


# ---- H2: P3 > P1 ------------------------------------------------------------------------------
def test_h2_confirmed_p3_not_above_p1_is_invalid() -> None:
    assert not hard_rules_ok("IMP", 1, "W4", (100.0, 120.0, 108.0, 119.0), 112.0)
    assert not hard_rules_ok("IMP", 1, "W4", (100.0, 120.0, 108.0, 120.0), 112.0)


def test_h2_not_applied_while_w3_in_progress() -> None:
    assert hard_rules_ok("IMP", 1, "W3", (100.0, 120.0, 108.0), 119.0)   # W3 below P1 is still fine
    assert not hard_rules_ok("IMP", 1, "W3", (100.0, 120.0, 108.0), 107.0)  # E must be above P2


# ---- H3: W3 never the shortest ----------------------------------------------------------------
SHORT_W3 = (100.0, 120.0, 110.0, 128.0, 122.0)  # W1=20, W3=18 < W1 -> W5 cap = 122 + 18 = 140


def test_h3_w5_cap_when_w3_shorter_than_w1() -> None:
    assert hard_rules_ok("IMP", 1, "W5", SHORT_W3, 140.0)
    assert not hard_rules_ok("IMP", 1, "W5", SHORT_W3, 140.5)


def test_h3_no_cap_when_w3_longer_than_w1() -> None:
    assert hard_rules_ok("IMP", 1, "W5", BULL, 1000.0)


def test_h3_completed_impulse() -> None:
    assert _completed_ok("IMP", 1, SHORT_W3 + (139.0,))       # W5 = 17 < W3 = 18 -> W3 not shortest
    assert not _completed_ok("IMP", 1, SHORT_W3 + (141.0,))   # W5 = 19 > W3 = 18 < W1 = 20


# ---- H4: P4 > P1 ------------------------------------------------------------------------------
def test_h4_confirmed_p4_overlapping_p1_is_invalid() -> None:
    assert not hard_rules_ok("IMP", 1, "W5", (100.0, 120.0, 108.0, 150.0, 119.0), 160.0)


def test_h4_in_progress_w4_boundary() -> None:
    assert hard_rules_ok("IMP", 1, "W4", BULL[:4], 120.0)
    assert not hard_rules_ok("IMP", 1, "W4", BULL[:4], 119.5)


# ---- H5: W5 must stay above P4 ----------------------------------------------------------------
def test_h5_w5_end_must_be_above_p4() -> None:
    assert not hard_rules_ok("IMP", 1, "W5", BULL, 137.0)
    assert not hard_rules_ok("IMP", -1, "W5", BEAR, 63.0)


# ---- direction sanity ---------------------------------------------------------------------------
def test_w2_must_retrace_below_p1() -> None:
    assert not hard_rules_ok("IMP", 1, "W3", (100.0, 120.0, 125.0), 130.0)


# ---- ABC (d = direction of A) --------------------------------------------------------------------
# d=-1: X=200 high, A_end=150 (|A| = 50), B up, C down. B cap = 150 + 1.382*50 = 219.1
def test_a1_b_in_progress_boundary() -> None:
    assert hard_rules_ok("ABC", -1, "B", (200.0, 150.0), 219.1)
    assert not hard_rules_ok("ABC", -1, "B", (200.0, 150.0), 219.2)


def test_a1_confirmed_b_too_long() -> None:
    assert hard_rules_ok("ABC", -1, "C", (200.0, 150.0, 219.0), 170.0)
    assert not hard_rules_ok("ABC", -1, "C", (200.0, 150.0, 220.0), 170.0)


def test_a2_c_in_progress_boundary() -> None:
    # B_end = 180, C floor = 180 - 2.618*50 = 49.1
    assert hard_rules_ok("ABC", -1, "C", (200.0, 150.0, 180.0), 49.1)
    assert not hard_rules_ok("ABC", -1, "C", (200.0, 150.0, 180.0), 49.0)


def test_abc_up_correction_mirror() -> None:
    assert hard_rules_ok("ABC", 1, "C", (0.0, 50.0, 20.0), 150.9)
    assert not hard_rules_ok("ABC", 1, "C", (0.0, 50.0, 20.0), 151.0)


def test_wrong_number_of_points_raises() -> None:
    with pytest.raises(ValueError):
        hard_rules_ok("IMP", 1, "W3", (100.0, 120.0), 130.0)
    with pytest.raises(ValueError):
        hard_rules_ok("ABC", 1, "W3", (100.0, 120.0, 110.0), 130.0)


# ---- invalidation levels -------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("pattern", "d", "phase", "pts", "expected"),
    [
        ("IMP", 1, "W2", BULL[:2], Invalidation(100.0, None)),             # H1
        ("IMP", 1, "W3", BULL[:3], Invalidation(108.0, None)),             # H2 in progress: > P2
        ("IMP", 1, "W4", BULL[:4], Invalidation(120.0, None)),             # H4: > P1
        ("IMP", 1, "W5", BULL, Invalidation(138.0, None)),                 # H5, W3 >= W1 -> no cap
        ("IMP", 1, "W5", SHORT_W3, Invalidation(122.0, 140.0)),            # H5 + H3 cap
        ("IMP", -1, "W2", BEAR[:2], Invalidation(None, 100.0)),
        ("IMP", -1, "W3", BEAR[:3], Invalidation(None, 92.0)),
        ("IMP", -1, "W4", BEAR[:4], Invalidation(None, 80.0)),
        ("IMP", -1, "W5", tuple(200 - p for p in SHORT_W3), Invalidation(60.0, 78.0)),
        ("ABC", -1, "B", (200.0, 150.0), Invalidation(None, pytest.approx(219.1))),   # A1
        ("ABC", -1, "C", (200.0, 150.0, 180.0), Invalidation(pytest.approx(49.1), None)),  # A2
        ("ABC", 1, "B", (100.0, 150.0), Invalidation(pytest.approx(80.9), None)),
        ("ABC", 1, "C", (100.0, 150.0, 120.0), Invalidation(None, pytest.approx(250.9))),
    ],
)
def test_invalidation_levels(pattern: str, d: int, phase: str, pts: tuple[float, ...],
                             expected: Invalidation) -> None:
    assert invalidation(pattern, d, phase, pts) == expected


def test_invalidation_matches_hard_rule_boundary_on_e_side() -> None:
    inv = invalidation("IMP", 1, "W5", SHORT_W3)
    assert hard_rules_ok("IMP", 1, "W5", SHORT_W3, inv.hi)
    assert not hard_rules_ok("IMP", 1, "W5", SHORT_W3, inv.hi + 1e-9)
    inv = invalidation("ABC", -1, "C", (200.0, 150.0, 180.0))
    assert hard_rules_ok("ABC", -1, "C", (200.0, 150.0, 180.0), inv.lo)
    assert not hard_rules_ok("ABC", -1, "C", (200.0, 150.0, 180.0), inv.lo - 1e-9)


@pytest.mark.parametrize(
    ("pattern", "d", "phase", "leg"),
    [("IMP", 1, "W2", -1), ("IMP", 1, "W3", 1), ("IMP", 1, "W4", -1), ("IMP", 1, "W5", 1),
     ("IMP", -1, "W2", 1), ("IMP", -1, "W5", -1),
     ("ABC", -1, "B", 1), ("ABC", -1, "C", -1), ("ABC", 1, "B", -1), ("ABC", 1, "C", 1)],
)
def test_next_leg_dir(pattern: str, d: int, phase: str, leg: int) -> None:
    assert next_leg_dir(pattern, d, phase) == leg
