"""Elliott patterns, hard rules (spec 4.3), invalidation levels and leg parity.

Everything is evaluated in "u-space": u = d * price. In u-space every pattern looks bullish:
IMP = S(low) P1(high) P2(low) P3(high) P4(low) P5(high); ABC = X(low) A(high) B(low) C(high).
For ABC, ``d`` is the direction of wave A (d=-1: A down, B up, C down).

Positions: IMP S=0,P1=1,...,P5=5; ABC X=0,A=1,B=2,C=3. For a phase with n confirmed points the
provisional end E (the in-progress wave's end) sits at position n.

Hard rules (bull form):
  IMP  W1 dir  P1 > S          W2 dir  P2 < P1         H1  P2 > S
       W3 dir  P3 > P2         H2  P3 > P1 (only once P3 is confirmed; while W3 runs: inval P2)
       W4 dir  P4 < P3         H4  P4 > P1             W5 dir (H5) P5 > P4
       H3  W3 not the shortest of W1/W3/W5 (while W5 runs: if |W3|<|W1| then P5 <= P4+|W3|)
  ABC  A dir X < A, B dir B < A, A1 |B| <= 1.382|A|, C dir C > B, A2 |C| <= 2.618|A|
Confirmed points use strict inequalities except A1/A2 ("<=" in the spec). Rules that bound the
provisional end E use the invalidation level with "E may touch the level" so that
``hard_rules_ok(..., E)`` is True exactly when no closed bar has gone strictly beyond
``invalidation(...)`` on the E side (the engine kills on LOW < lo / HIGH > hi).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

PHASES_IMP = ("W2", "W3", "W4", "W5")
PHASES_ABC = ("B", "C")
N_POINTS = {"W2": 2, "W3": 3, "W4": 4, "W5": 5, "B": 2, "C": 3}

A1_MAX = 1.382   # |B|/|A| upper limit
A2_MAX = 2.618   # |C|/|A| upper limit
_N_COMPLETE = {"IMP": 6, "ABC": 4}   # S..P5 / X..C all confirmed (used for lineage bookkeeping only)


@dataclass(frozen=True, slots=True)
class Invalidation:
    lo: float | None   # count dies if a closed 1h bar LOW < lo
    hi: float | None   # count dies if a closed 1h bar HIGH > hi


_Pred = Callable[..., bool]


def _h3(u0: float, u1: float, u2: float, u3: float, u4: float, u5: float) -> bool:
    w1, w3, w5 = u1 - u0, u3 - u2, u5 - u4
    return not (w3 < w1 and w3 < w5)


def _a1(u0: float, u1: float, u2: float) -> bool:
    return u2 >= u1 - A1_MAX * (u1 - u0)


def _a2(u0: float, u1: float, u2: float, u3: float) -> bool:
    return u3 <= u2 + A2_MAX * (u1 - u0)


# Rules between CONFIRMED points: (positions, predicate on the u-values at those positions).
# A rule applies to a phase with n confirmed points iff max(positions) < n.
_STATIC_RULES: dict[str, tuple[tuple[tuple[int, ...], _Pred], ...]] = {
    "IMP": (
        ((0, 1), lambda a, b: b > a),            # W1 up
        ((1, 2), lambda a, b: b < a),            # W2 down
        ((0, 2), lambda a, b: b > a),            # H1  P2 > S
        ((2, 3), lambda a, b: b > a),            # W3 up
        ((1, 3), lambda a, b: b > a),            # H2  P3 > P1
        ((3, 4), lambda a, b: b < a),            # W4 down
        ((1, 4), lambda a, b: b > a),            # H4  P4 > P1
        ((4, 5), lambda a, b: b > a),            # H5  P5 > P4      (completed impulses only)
        ((0, 1, 2, 3, 4, 5), _h3),               # H3 final         (completed impulses only)
    ),
    "ABC": (
        ((0, 1), lambda a, b: b > a),            # A
        ((1, 2), lambda a, b: b < a),            # B against A
        ((0, 1, 2), _a1),                        # A1 |B| <= 1.382 |A|
        ((2, 3), lambda a, b: b > a),            # C            (completed ABC only)
        ((0, 1, 2, 3), _a2),                     # A2           (completed ABC only)
    ),
}


def _pattern_of(phase: str) -> str:
    if phase in PHASES_IMP:
        return "IMP"
    if phase in PHASES_ABC:
        return "ABC"
    raise ValueError(f"unknown phase {phase!r}")


def _static_rules(pattern: str, n: int) -> tuple[tuple[tuple[int, ...], _Pred], ...]:
    return tuple(r for r in _STATIC_RULES[pattern] if max(r[0]) < n)


def _u_leg(phase: str) -> int:
    """Direction (in u-space) of the in-progress wave of ``phase``."""
    return 1 if phase in ("W3", "W5", "C") else -1


def _bounds_u(phase: str, u: Sequence[float]) -> tuple[float | None, float | None]:
    """(lower, upper) bounds in u-space for prices while ``phase`` is in progress (spec 4.3 column 3)."""
    if phase == "W2":
        return u[0], None                                    # H1: E_live > S
    if phase == "W3":
        return u[2], None                                    # H2 in progress: price > P2
    if phase == "W4":
        return u[1], None                                    # H4: E_live > P1
    if phase == "W5":
        w1, w3 = u[1] - u[0], u[3] - u[2]
        return u[4], (u[4] + w3 if w3 < w1 else None)        # H5: > P4 ; H3: <= P4 + |W3| if |W3| < |W1|
    if phase == "B":
        return u[1] - A1_MAX * (u[1] - u[0]), None           # A1: B upper limit
    if phase == "C":
        return None, u[2] + A2_MAX * (u[1] - u[0])          # A2: C upper limit
    raise ValueError(f"unknown phase {phase!r}")


def _e_ok_u(phase: str, u_last: float, lo: float | None, hi: float | None, ue: float) -> bool:
    """Rules on the provisional end E (u-space): strict direction vs the last point + level bounds."""
    if _u_leg(phase) * (ue - u_last) <= 0:
        return False
    if lo is not None and ue < lo:
        return False
    if hi is not None and ue > hi:
        return False
    return True


def _check_static(pattern: str, n: int, u: Sequence[float]) -> bool:
    for pos, fn in _static_rules(pattern, n):
        if not fn(*(u[p] for p in pos)):
            return False
    return True


def _check_args(pattern: str, phase: str, pts: Sequence[float]) -> int:
    if _pattern_of(phase) != pattern:
        raise ValueError(f"phase {phase} does not belong to {pattern}")
    n = N_POINTS[phase]
    if len(pts) != n:
        raise ValueError(f"{phase} needs {n} confirmed points, got {len(pts)}")
    return n


def hard_rules_ok(pattern: str, d: int, phase: str, pts: Sequence[float], E: float) -> bool:
    """True iff the count (pts = confirmed labeled prices in order, E = provisional end) obeys spec 4.3."""
    n = _check_args(pattern, phase, pts)
    u = [d * p for p in pts]
    if not _check_static(pattern, n, u):
        return False
    lo, hi = _bounds_u(phase, u)
    return _e_ok_u(phase, u[-1], lo, hi, d * E)


def _completed_ok(pattern: str, d: int, pts: Sequence[float]) -> bool:
    """All hard rules for a fully confirmed pattern (IMP: S..P5, ABC: X..C)."""
    n = _N_COMPLETE[pattern]
    if len(pts) != n:
        raise ValueError(f"completed {pattern} needs {n} points")
    return _check_static(pattern, n, [d * p for p in pts])


def _to_price_bounds(d: int, lo_u: float | None, hi_u: float | None) -> Invalidation:
    if d > 0:
        return Invalidation(lo_u, hi_u)
    return Invalidation(None if hi_u is None else -hi_u, None if lo_u is None else -lo_u)


def invalidation(pattern: str, d: int, phase: str, pts: Sequence[float]) -> Invalidation:
    """Price levels that kill the in-progress count (spec 4.3 'invalidation while in progress')."""
    _check_args(pattern, phase, pts)
    lo_u, hi_u = _bounds_u(phase, [d * p for p in pts])
    return _to_price_bounds(d, lo_u, hi_u)


def next_leg_dir(pattern: str, d: int, phase: str) -> int:
    """Price direction of the in-progress leg (e.g. IMP,+1,W2 -> -1; ABC,-1,B -> +1)."""
    if _pattern_of(phase) != pattern:
        raise ValueError(f"phase {phase} does not belong to {pattern}")
    return d * _u_leg(phase)
