from __future__ import annotations

import itertools

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ewbot.config import WaveParams
from ewbot.waves.enumerate import _enumerate_raw, _Enumerator, enumerate_counts
from ewbot.waves.patterns import (
    N_POINTS,
    PHASES_ABC,
    PHASES_IMP,
    Invalidation,
    _completed_ok,
    hard_rules_ok,
    next_leg_dir,
)
from ewbot.waves.scoring import fit_q
from ewbot.waves.zigzag import Pivot

P = WaveParams()
T0 = 1_700_006_400


def _pv(rows: list[tuple[int, float, int]]) -> list[Pivot]:
    return [Pivot(i, T0 + i * 14_400, p, k, i + 2) for i, p, k in rows]


# falling lead-in, then the Fib-perfect bull impulse S..P4 (see test_scoring.py)
LEAD = [(0, 1400.0, 1), (4, 1250.0, -1), (8, 1350.0, 1), (12, 1150.0, -1), (16, 1250.0, 1),
        (20, 1080.0, -1), (24, 1095.0, 1)]
IMP = [(28, 1000.0, -1), (32, 1100.0, 1), (36, 1045.0, -1), (44, 1206.8, 1), (48, 1158.26, -1)]
PIVOTS = _pv(LEAD + IMP)


def test_fib_perfect_impulse_top_is_w5() -> None:
    counts, lineages = enumerate_counts(PIVOTS, 1, 1258.26, P)
    top = counts[0]
    assert (top.pattern, top.d, top.phase) == ("IMP", 1, "W5")
    assert top.points == tuple(PIVOTS[7:12])
    assert top.q == pytest.approx(6.0 / 6.5, abs=1e-9)
    assert top.lineage == ("IMP", 1, 28) and top.instance == ("IMP", 1, 28, "W5")
    assert top.prices == (1000.0, 1100.0, 1045.0, 1206.8, 1158.26)
    assert top.inval == Invalidation(1158.26, None) and top.leg_dir == 1
    assert counts[1].phase == "W3" and counts[1].points == tuple(PIVOTS[7:10])
    assert ("IMP", 1, 28) in lineages


def test_parity_only_up_leg_phases() -> None:
    counts, _ = enumerate_counts(PIVOTS, 1, 1230.0, P)
    assert counts and all(c.leg_dir == 1 for c in counts)
    assert {(c.pattern, c.d, c.phase) for c in counts} <= {
        ("IMP", 1, "W3"), ("IMP", 1, "W5"), ("IMP", -1, "W2"), ("IMP", -1, "W4"),
        ("ABC", 1, "C"), ("ABC", -1, "B")}


def test_extreme_constraint_when_skipping_pivots() -> None:
    # S=1000, h=1120, l=1050, h=1100, leg down. P1 must be the 1120 high (>= skipped highs),
    # and E must undercut the skipped 1050 low.
    piv = _pv([(0, 1000.0, -1), (4, 1120.0, 1), (8, 1050.0, -1), (12, 1100.0, 1)])
    w2 = lambda cs: [c for c in cs if (c.pattern, c.d, c.phase) == ("IMP", 1, "W2")]  # noqa: E731
    counts, _ = enumerate_counts(piv, -1, 1040.0, P)
    assert [tuple(p.i for p in c.points) for c in w2(counts)] == [(0, 4)]
    # E=1060 is above the skipped 1050 low -> (0,4) illegal; (0,12) would skip the higher 1120 high
    counts, _ = enumerate_counts(piv, -1, 1060.0, P)
    assert [tuple(p.i for p in c.points) for c in w2(counts)] == [(8, 12)]


def test_lineages_include_deduped_away_counts() -> None:
    piv = PIVOTS + _pv([(60, 1258.26, 1)])
    counts, lineages = enumerate_counts(piv, -1, 1158.26, P)
    raw = _enumerate_raw(piv, -1, 1158.26, P)
    # ABC up with X=P4(1158.26): B/A = 1.0 is valid but loses its dedupe group to X=S
    assert any(c.lineage == ("ABC", 1, 48) for c in raw)
    assert not any(c.lineage == ("ABC", 1, 48) for c in counts)
    assert ("ABC", 1, 48) in lineages


def test_completed_impulse_keeps_its_lineage_alive() -> None:
    # after P5 (1258.26) price fell to 1180, bounced to 1240, now falls with E=1200: no in-progress
    # IMP+ count can start at S=1000 any more, but the completed impulse S..P5 is intact.
    piv = PIVOTS + _pv([(60, 1258.26, 1), (66, 1180.0, -1), (70, 1240.0, 1)])
    counts, lineages = enumerate_counts(piv, -1, 1200.0, P)
    assert not any(c.lineage == ("IMP", 1, 28) for c in counts)
    assert ("IMP", 1, 28) in lineages


def test_dedupe_keeps_max_q_per_group_and_sorting() -> None:
    raw = _enumerate_raw(PIVOTS, 1, 1230.0, P)
    counts, _ = enumerate_counts(PIVOTS, 1, 1230.0, P)
    groups: dict[tuple, float] = {}
    for c in raw:
        g = (c.pattern, c.d, c.phase, c.points[-1].i)
        groups[g] = max(groups.get(g, -1.0), c.q)
    assert len(counts) == len(groups)
    assert all(groups[(c.pattern, c.d, c.phase, c.points[-1].i)] == c.q for c in counts)
    keys = [(-c.q, -len(c.points), -c.points[0].i, tuple(p.i for p in c.points)) for c in counts]
    assert keys == sorted(keys)


def test_not_initialised_returns_nothing() -> None:
    assert enumerate_counts([], 0, 0.0, P) == ([], frozenset())


# ----------------------------------------------------------------------------- brute force oracle
def _extreme_ok(w: list[Pivot], seq: list[int], E: float, leg_dir: int) -> bool:
    n = len(seq) - 1                                  # seq[-1] == len(w) stands for E
    for k in range(n + 1):
        lo = seq[k - 1] if k > 0 else seq[k]
        hi = seq[k + 1] if k < n else seq[k]
        price = E if k == n else w[seq[k]].price
        kind = leg_dir if k == n else w[seq[k]].kind
        for j in range(lo + 1, hi):
            if j != seq[k] and w[j].kind == kind and kind * (w[j].price - price) > 0:
                return False
    return True


def _brute(pivots: list[Pivot], leg_dir: int, E: float, M: int) -> tuple[set, set]:
    w = pivots[-M:]
    keys, lineages = set(), set()
    for pattern, phases in (("IMP", PHASES_IMP), ("ABC", PHASES_ABC)):
        for d in (1, -1):
            for phase in phases:
                if next_leg_dir(pattern, d, phase) != leg_dir:
                    continue
                n = N_POINTS[phase]
                for combo in itertools.combinations(range(len(w)), n):
                    pts = [w[j] for j in combo]
                    if any(pts[k].kind != leg_dir * (-1) ** (n - k) for k in range(n)):
                        continue
                    if not _extreme_ok(w, list(combo) + [len(w)], E, leg_dir):
                        continue
                    if hard_rules_ok(pattern, d, phase, [p.price for p in pts], E):
                        keys.add((pattern, d, phase, tuple(p.i for p in pts)))
                        lineages.add((pattern, d, pts[0].i))
            # completed patterns: end point is a u-high and stays the extreme afterwards
            n = 6 if pattern == "IMP" else 4
            for combo in itertools.combinations(range(len(w)), n):
                pts = [w[j] for j in combo]
                if any(pts[k].kind != d * (-1) ** (n - 1 - k) for k in range(n)):
                    continue
                seq = list(combo)
                ok = all(
                    not (w[j].kind == pts[k].kind and pts[k].kind * (w[j].price - pts[k].price) > 0)
                    for k in range(n)
                    for j in range(seq[k - 1] + 1 if k > 0 else seq[k] + 1, seq[k + 1] if k < n - 1 else len(w))
                    if j != seq[k]
                )
                if ok and leg_dir == d and d * E > d * pts[-1].price:
                    ok = False
                if ok and _completed_ok(pattern, d, [p.price for p in pts]):
                    lineages.add((pattern, d, pts[0].i))
    return keys, lineages


@st.composite
def pivot_windows(draw: st.DrawFn) -> tuple[list[Pivot], int, float]:
    n = draw(st.integers(min_value=2, max_value=14))
    kind = draw(st.sampled_from([1, -1]))
    price = 1000.0
    rows = []
    for k in range(n):
        if k:
            price += kind * draw(st.floats(min_value=1.0, max_value=200.0, allow_nan=False))
        rows.append((3 * k, round(price, 4), kind))
        kind = -kind
    leg_dir = kind                                   # = -(last pivot kind)
    E = rows[-1][1] + leg_dir * draw(st.floats(min_value=0.5, max_value=300.0, allow_nan=False))
    return _pv(rows), leg_dir, round(E, 4)


@settings(max_examples=300, deadline=None)
@given(pivot_windows())
def test_enumeration_matches_brute_force(case: tuple[list[Pivot], int, float]) -> None:
    pivots, leg_dir, E = case
    en = _Enumerator(P)
    raw, _ = en.run(pivots, leg_dir, E)
    assert en.nodes < 10_000
    counts, lineages = enumerate_counts(pivots, leg_dir, E, P)
    keys, brute_lineages = _brute(pivots, leg_dir, E, P.M)
    assert {(c.pattern, c.d, c.phase, tuple(p.i for p in c.points)) for c in raw} == keys
    assert lineages == frozenset(brute_lineages)
    for c in raw:
        assert c.leg_dir == leg_dir
        # ctx ends at S so fit_q's "most recent pivot with S's price" lookup is exact even with ties
        ctx = pivots[: pivots.index(c.points[0]) + 1]
        assert c.q == fit_q(c.pattern, c.d, c.phase, c.prices, E, ctx, P)
        assert 0.0 < c.q < 1.0
    # dedupe key unique, sort order
    assert len({(c.pattern, c.d, c.phase, c.points[-1].i) for c in counts}) == len(counts)
    keys_sorted = [(-c.q, -len(c.points), -c.points[0].i, tuple(p.i for p in c.points)) for c in counts]
    assert keys_sorted == sorted(keys_sorted)


@settings(max_examples=100, deadline=None)
@given(pivot_windows(), st.lists(st.floats(min_value=0.5, max_value=300.0), min_size=1, max_size=4))
def test_cached_enumerator_equals_fresh(case: tuple[list[Pivot], int, float], moves: list[float]) -> None:
    pivots, leg_dir, E = case
    en = _Enumerator(P)
    for mv in [0.0] + moves:
        e = E + leg_dir * mv
        assert en.run(pivots, leg_dir, e) == _Enumerator(P).run(pivots, leg_dir, e)
