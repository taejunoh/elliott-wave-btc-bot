"""Count enumeration (spec 4.2): DFS backwards from E over the last M confirmed pivots.

- parity: only (pattern, d, phase) whose in-progress leg direction equals the ZigZag leg direction;
  labeled points alternate kinds and the last confirmed one has kind -leg_dir;
- pivot skipping with the extreme constraint: a labeled high is >= every pivot high strictly between
  its labeled neighbours, a labeled low <= every pivot low between them; S only has its right side
  ([S, P1]) and E only its left side (E vs the pivots after the last labeled point);
- hard rules (patterns.py) are checked incrementally: a rule is tested as soon as the DFS has assigned
  all of its positions, so dead prefixes are pruned (rules on E are applied when E is attached);
- dedupe: per (pattern, d, phase, .i of the last confirmed labeled point) keep the max-q count;
- order: q desc -> #points desc -> S.i desc -> tuple of pivot .i ascending -> IMP before ABC.

Incremental strategy: the confirmed-point part of a count (point choice, extreme constraints between
confirmed points, static hard rules, g_start and completed-wave scores) is a "skeleton" cached per
pivot window. A new E only re-runs the E checks and the in-progress score term, so a 4h close without
a new pivot costs O(#skeletons); a new pivot rebuilds the skeletons (< 10k DFS nodes).

Lineages: (pattern, d, S.i). A lineage is alive if any valid in-progress count has it OR the pattern
has completed (IMP S..P5 / ABC X..C all confirmed with every hard rule, end point still the extreme:
no later same-kind pivot and no E beyond it). Finishing W5/C is not a hard-rule violation, so the
lineage-kill exit must not fire just because the pattern completed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Sequence

from ewbot.config import WaveParams
from ewbot.waves.patterns import (
    _N_COMPLETE,
    N_POINTS,
    PHASES_ABC,
    PHASES_IMP,
    Invalidation,
    _bounds_u,
    _e_ok_u,
    _static_rules,
    invalidation,
    next_leg_dir,
)
from ewbot.waves.scoring import _combine, _g_start_at, _ip_term, _static_sums
from ewbot.waves.zigzag import Pivot

_COMBOS: tuple[tuple[str, int, str], ...] = tuple(
    (pat, d, ph)
    for pat, phases in (("IMP", PHASES_IMP), ("ABC", PHASES_ABC))
    for d in (1, -1)
    for ph in phases
)
_CTX_BACK = 8                      # g_start looks at most 8 pivots before S (4 same-kind pivots)
_PATTERN_RANK = {"IMP": 0, "ABC": 1}
_Rule = tuple[tuple[int, ...], Callable[..., bool]]


@dataclass(frozen=True, slots=True)
class Count:
    pattern: str
    d: int
    phase: str
    points: tuple[Pivot, ...]     # confirmed labeled points, len == N_POINTS[phase]
    q: float                      # fit at the 4h close it was scored on

    @property
    def prices(self) -> tuple[float, ...]:
        return tuple(p.price for p in self.points)

    @property
    def lineage(self) -> tuple[str, int, int]:
        return (self.pattern, self.d, self.points[0].i)

    @property
    def instance(self) -> tuple[str, int, int, str]:
        return (self.pattern, self.d, self.points[0].i, self.phase)

    @property
    def inval(self) -> Invalidation:
        return invalidation(self.pattern, self.d, self.phase, self.prices)

    @property
    def leg_dir(self) -> int:
        return next_leg_dir(self.pattern, self.d, self.phase)


@dataclass(frozen=True, slots=True)
class _Skeleton:
    pattern: str
    d: int
    phase: str
    points: tuple[Pivot, ...]
    prices: tuple[float, ...]
    ws: float                 # sum w*s over g_start + completed-wave terms
    w: float                  # sum w over the same terms
    e_floor: float            # leg_dir * E must be >= this (E vs skipped E-kind pivots)
    u_last: float             # d * price of the last confirmed point
    lo_u: float | None        # E bounds in u-space (patterns._bounds_u)
    hi_u: float | None


@dataclass(frozen=True, slots=True)
class _Completed:
    lineage: tuple[str, int, int]
    key: tuple[int, ...]      # .i of every point (engine dead-set key)
    inval: Invalidation       # dies if price goes beyond the pattern end
    end_u: float              # d * end price


def _sort_key(c: Count, q: float | None = None) -> tuple:
    """Spec 4.4 order with q (or q' if given). Exact ties left after the spec keys (IMP W2/W3 and
    ABC B/C on the same points score identically when both zones agree) go to IMP; the key is total."""
    return (-(c.q if q is None else q), -len(c.points), -c.points[0].i,
            tuple(p.i for p in c.points), _PATTERN_RANK[c.pattern], c.phase)


def _dedupe_sorted(counts: Sequence[Count]) -> list[Count]:
    best: dict[tuple[str, int, str, int], Count] = {}
    for c in counts:
        g = (c.pattern, c.d, c.phase, c.points[-1].i)
        cur = best.get(g)
        if cur is None or _sort_key(c) < _sort_key(cur):
            best[g] = c
    return sorted(best.values(), key=_sort_key)


class _Window:
    """The last-M pivots with O(1) 'max high / min low strictly between two positions' lookups."""

    def __init__(self, pivots: Sequence[Pivot], M: int) -> None:
        self.base = max(0, len(pivots) - M)
        self.w = list(pivots[self.base:])
        m = self.m = len(self.w)
        self.hi_btw = [[-math.inf] * (m + 1) for _ in range(m + 1)]
        self.lo_btw = [[math.inf] * (m + 1) for _ in range(m + 1)]
        for a in range(m):
            mh, ml = -math.inf, math.inf
            for b in range(a + 1, m + 1):
                self.hi_btw[a][b], self.lo_btw[a][b] = mh, ml
                if b < m:
                    if self.w[b].kind > 0:
                        mh = max(mh, self.w[b].price)
                    else:
                        ml = min(ml, self.w[b].price)

    def extreme_ok(self, j: int, a: int, b: int) -> bool:
        """Pivot at position j is >= every high (kind +1) / <= every low (kind -1) strictly in (a, b)."""
        pv = self.w[j]
        return pv.price >= self.hi_btw[a][b] if pv.kind > 0 else pv.price <= self.lo_btw[a][b]


class _Enumerator:
    """Skeleton cache keyed on the pivot window; ``run`` is a pure function of its arguments."""

    def __init__(self, params: WaveParams) -> None:
        self.params = params
        self._key: tuple | None = None
        self._skels: list[_Skeleton] = []
        self._done: list[_Completed] = []
        self.nodes = 0                      # DFS nodes visited by the last skeleton build

    def run(self, pivots: Sequence[Pivot], leg_dir: int, E: float) -> tuple[list[Count], list[_Completed]]:
        """(all valid in-progress counts before dedupe, completed patterns still intact)."""
        if leg_dir == 0 or not pivots:
            return [], []
        key = (leg_dir, len(pivots), tuple(pivots[-(self.params.M + _CTX_BACK):]))
        if key != self._key:
            self._build(pivots, leg_dir)
            self._key = key
        p = self.params
        le = leg_dir * E
        counts: list[Count] = []
        for sk in self._skels:
            if le < sk.e_floor or not _e_ok_u(sk.phase, sk.u_last, sk.lo_u, sk.hi_u, sk.d * E):
                continue
            ip_w, ip_s = _ip_term(sk.pattern, sk.d, sk.phase, sk.prices, E, p.sigma)
            counts.append(Count(sk.pattern, sk.d, sk.phase, sk.points,
                                _combine(sk.ws, sk.w, ip_w, ip_s, p.w0)))
        done = [c for c in self._done if not (leg_dir == c.lineage[1] and c.lineage[1] * E > c.end_u)]
        return counts, done

    # ---------------------------------------------------------------- building
    def _dfs(self, win: _Window, kinds: list[int], rules_at: list[tuple[_Rule, ...]], d: int,
             k: int, right: int, js: list[int], out: list[tuple[int, ...]]) -> None:
        """Assign point k (kind kinds[k]) left of window position ``right``, backwards."""
        w = win.w
        for j in range(right - 1, k - 1, -1):
            if w[j].kind != kinds[k]:
                continue
            self.nodes += 1
            if not win.extreme_ok(j, j, right):                          # point k vs (j, right)
                continue
            if right < win.m and not win.extreme_ok(right, j, right):    # right neighbour's left side
                continue
            js[k] = j
            if any(not fn(*(d * w[js[p]].price for p in pos)) for pos, fn in rules_at[k]):
                continue
            if k > 0:
                self._dfs(win, kinds, rules_at, d, k - 1, j, js, out)
            else:
                out.append(tuple(js))

    def _points(self, win: _Window, pattern: str, d: int, n: int, last_kind: int) -> list[tuple[int, ...]]:
        rules = _static_rules(pattern, n)
        rules_at = [tuple(r for r in rules if min(r[0]) == k) for k in range(n)]
        kinds = [last_kind * (-1) ** (n - 1 - k) for k in range(n)]
        out: list[tuple[int, ...]] = []
        if n <= win.m:
            self._dfs(win, kinds, rules_at, d, n - 1, win.m, [0] * n, out)
        return out

    def _build(self, pivots: Sequence[Pivot], leg_dir: int) -> None:
        self.nodes = 0
        win = _Window(pivots, self.params.M)
        sigma = self.params.sigma
        skels: list[_Skeleton] = []
        for pattern, d, phase in _COMBOS:
            if next_leg_dir(pattern, d, phase) != leg_dir:
                continue
            n = N_POINTS[phase]
            for js in self._points(win, pattern, d, n, -leg_dir):
                pts = tuple(win.w[j] for j in js)
                prices = tuple(p.price for p in pts)
                g0 = _g_start_at(pattern, d, prices, win.base + js[0], pivots)
                ws, wsum = _static_sums(pattern, d, phase, prices, g0, sigma)
                e_floor = win.hi_btw[js[-1]][win.m] if leg_dir > 0 else -win.lo_btw[js[-1]][win.m]
                lo_u, hi_u = _bounds_u(phase, [d * x for x in prices])
                skels.append(_Skeleton(pattern, d, phase, pts, prices, ws, wsum, e_floor,
                                       d * prices[-1], lo_u, hi_u))
        done: list[_Completed] = []
        for pattern in ("IMP", "ABC"):
            n = _N_COMPLETE[pattern]
            for d in (1, -1):
                for js in self._points(win, pattern, d, n, d):       # end point is a u-high
                    pts = [win.w[j] for j in js]
                    end = pts[-1].price
                    inval = Invalidation(None, end) if d > 0 else Invalidation(end, None)
                    done.append(_Completed((pattern, d, pts[0].i), tuple(p.i for p in pts), inval, d * end))
        self._skels, self._done = skels, done


def _enumerate_raw(pivots: Sequence[Pivot], leg_dir: int, E: float, params: WaveParams) -> list[Count]:
    """All valid in-progress counts before dedupe (fresh enumerator, no cache)."""
    return _Enumerator(params).run(pivots, leg_dir, E)[0]


def enumerate_counts(pivots: Sequence[Pivot], leg_dir: int, E: float,
                     params: WaveParams) -> tuple[list[Count], frozenset[tuple[str, int, int]]]:
    """(deduped counts in spec 4.4 tie-break order, every alive lineage before dedupe)."""
    raw, done = _Enumerator(params).run(pivots, leg_dir, E)
    lineages = frozenset(c.lineage for c in raw) | frozenset(c.lineage for c in done)
    return _dedupe_sorted(raw), lineages
