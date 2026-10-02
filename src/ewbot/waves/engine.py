"""WaveEngine: streaming 4h Elliott state, updated on every closed 1h bar (spec 3 event order, 4.1-4.4).

Per closed 1h bar:
 1. if it closes a 4h bucket: ZigZag(4h) update; E_live := E; enumerate (skeleton cache, see
    enumerate.py) -> drop counts on the dead list -> drop counts the 1h history already invalidated
    (below) -> dedupe -> hysteresis q' -> sort -> p = softmax(q'/T);
 2. otherwise: E_live/E_live_ts := the 1h bar's high (leg up) / low (leg down) if strictly beyond;
 3. invalidation: a count (or a completed pattern kept for lineage bookkeeping) dies if the bar went
    strictly beyond its level. On the E side the level is compared with E_live, on the opposite side
    with the bar's low (leg up) / high (leg down). Dead keys are remembered; any later count whose
    labeled points EXTEND a dead key (same pattern and d) stays dead, so a wick through P2/P4 that
    never became a pivot cannot be "forgotten" at the next 4h re-enumeration;
 4. probabilities fixed at the 4h close are renormalised over the survivors; done-ness and S_rev are
    recomputed with E_live.

1h history (step 1): a count can become enumerable only later (E crosses a skipped pivot, a new pivot
opens new combinations), after a wick that would have killed it. So at every 4h close each enumerated
count / completed pattern is also checked against EVERY closed 1h bar strictly after its last labeled
point (that point's sub_ts) up to and including the current bar: min LOW < lo or max HIGH > hi kills
it and its key joins the dead list. The suffix extremes come from two monotonic stacks of closed 1h
bars, so a query costs O(log n) and only bars that are already closed are ever seen.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from typing import Sequence

from ewbot.config import WaveParams
from ewbot.types import H1, H4, Bar
from ewbot.waves.enumerate import Count, _Completed, _dedupe_sorted, _Enumerator, _sort_key
from ewbot.waves.scoring import is_done, softmax
from ewbot.waves.zigzag import Pivot, ZigZag

_DeadKey = tuple[str, int, tuple[int, ...]]


@dataclass(frozen=True, slots=True)
class WaveState:
    ts: int
    ready: bool
    counts: tuple[Count, ...]
    probs: tuple[float, ...]
    top: Count | None
    q_top: float
    s_rev: float
    lineages: frozenset[tuple[str, int, int]]
    E_live: float
    E_live_ts: int
    leg_dir: int
    leg_start: Pivot | None
    pivots4h: tuple[Pivot, ...]
    new_pivot_4h: Pivot | None
    atr4h: float | None
    closed_4h: bool
    i4h: int


@dataclass(frozen=True, slots=True)
class _Item:
    key: _DeadKey
    lo: float | None
    hi: float | None
    lineage: tuple[str, int, int]
    since: int                      # sub_ts of the last labeled point (1h history starts after it)


def _count_item(c: Count) -> _Item:
    inv = c.inval
    return _Item((c.pattern, c.d, tuple(p.i for p in c.points)), inv.lo, inv.hi, c.lineage, c.points[-1].sub_ts)


def _completed_item(c: _Completed, end_ts: int) -> _Item:
    return _Item((c.lineage[0], c.lineage[1], c.key), c.inval.lo, c.inval.hi, c.lineage, end_ts)


class _SuffixExtremes:
    """min LOW / max HIGH over the closed 1h bars with ts > t, for any t: lows are kept on a stack that
    strictly increases (a new bar pops every low >= its own), highs on one that strictly decreases. The
    first entry after t is then the extreme of everything after t."""

    def __init__(self) -> None:
        self._lo_ts: list[int] = []
        self._lo: list[float] = []
        self._hi_ts: list[int] = []
        self._hi: list[float] = []

    def push(self, bar: Bar) -> None:
        lo_ts, lo, hi_ts, hi = self._lo_ts, self._lo, self._hi_ts, self._hi
        while lo and lo[-1] >= bar.low:
            lo.pop()
            lo_ts.pop()
        lo.append(bar.low)
        lo_ts.append(bar.ts)
        while hi and hi[-1] <= bar.high:
            hi.pop()
            hi_ts.pop()
        hi.append(bar.high)
        hi_ts.append(bar.ts)

    def after(self, t: int) -> tuple[float, float]:
        """(min LOW, max HIGH) over the bars with ts > t; (inf, -inf) if there are none."""
        k = bisect_right(self._lo_ts, t)
        lo = self._lo[k] if k < len(self._lo) else math.inf
        k = bisect_right(self._hi_ts, t)
        hi = self._hi[k] if k < len(self._hi) else -math.inf
        return lo, hi

    def forget_until(self, t: int) -> None:
        """Drop bars with ts <= t (no later query starts before t)."""
        k = bisect_right(self._lo_ts, t)
        del self._lo[:k], self._lo_ts[:k]
        k = bisect_right(self._hi_ts, t)
        del self._hi[:k], self._hi_ts[:k]


class WaveEngine:
    def __init__(self, params: WaveParams) -> None:
        self.params = params
        self._zz = ZigZag(params.K4, params.atr_len)
        self._enum = _Enumerator(params)
        self._use_cache = True                 # tests flip this to compare against fresh enumeration
        self._i4h = -1
        self._E_live = 0.0
        self._E_live_ts = -1
        self._counts: list[Count] = []         # deduped, sorted by q'
        self._keys: list[_DeadKey] = []
        self._prices: list[tuple[float, ...]] = []
        self._p0: list[float] = []             # probabilities fixed at the last 4h close
        self._items: list[_Item] = []          # every alive raw count + alive completed pattern
        self._max_lo = float("-inf")
        self._min_hi = float("inf")
        self._dead: set[_DeadKey] = set()
        self._prev_top: Count | None = None
        self._pivots_t: tuple[Pivot, ...] = ()
        self._lineages: frozenset[tuple[str, int, int]] = frozenset()
        self._hist = _SuffixExtremes()         # closed 1h bars after the oldest pivot in the window
        self._sub_ts: dict[tuple[int, int], int] = {}   # (i, kind) -> sub_ts of the window's pivots

    # ------------------------------------------------------------------ public
    def on_1h_close(self, bar1h: Bar, bar4h: Bar | None, sub_bars: Sequence[Bar] | None) -> WaveState:
        zz = self._zz
        new_piv: Pivot | None = None
        if bar4h is not None:
            if (bar1h.ts + H1) % H4 != 0 or bar4h.ts != bar1h.ts + H1 - H4:
                raise ValueError("bar4h must be the UTC 4h bucket that bar1h closes")
            if sub_bars is not None and (len(sub_bars) == 0 or sub_bars[-1].ts != bar1h.ts):
                raise ValueError("sub_bars must end with the 1h bar that closes the 4h bucket")
        self._hist.push(bar1h)
        if bar4h is not None:
            self._i4h += 1
            new_piv = zz.update(self._i4h, bar4h, sub_bars)
            if new_piv is not None:
                self._pivots_t = tuple(zz.pivots)
                self._on_new_pivot()
            if zz.leg_dir != 0:
                self._E_live, self._E_live_ts = zz.E, zz.E_sub_ts
                self._rescore()
        elif zz.leg_dir != 0:
            ext = bar1h.high if zz.leg_dir > 0 else bar1h.low
            if zz.leg_dir * (ext - self._E_live) > 0:
                self._E_live, self._E_live_ts = ext, bar1h.ts
        if zz.leg_dir != 0:
            self._kill(bar1h)
        state = self._state(bar1h.ts, new_piv, bar4h is not None)
        self._prev_top = state.top
        return state

    # ----------------------------------------------------------------- private
    def _on_new_pivot(self) -> None:
        """Slide the M-pivot window: forget dead keys whose first point left it (they can never recur)
        and 1h bars before its first pivot (no count's history starts there any more)."""
        win = self._zz.pivots[-self.params.M:]
        lo_i = win[0].i
        if self._dead and len(self._zz.pivots) > self.params.M:
            self._dead = {k for k in self._dead if k[2][0] >= lo_i}
        self._hist.forget_until(win[0].sub_ts)
        self._sub_ts = {(p.i, p.kind): p.sub_ts for p in win}

    def _is_dead(self, pattern: str, d: int, idx: tuple[int, ...]) -> bool:
        dead = self._dead
        if not dead:
            return False
        return any((pattern, d, idx[:k]) in dead for k in range(2, len(idx) + 1))

    def _history_kills(self, items: list[_Item]) -> set[_DeadKey]:
        """Keys of the items that some closed 1h bar after their last labeled point went beyond."""
        ext: dict[int, tuple[float, float]] = {}
        killed: set[_DeadKey] = set()
        for it in items:
            e = ext.get(it.since)
            if e is None:
                e = ext[it.since] = self._hist.after(it.since)
            if (it.lo is not None and e[0] < it.lo) or (it.hi is not None and e[1] > it.hi):
                killed.add(it.key)
        return killed

    def _rescore(self) -> None:
        zz, p = self._zz, self.params
        assert not zz.pivots or zz.pivots[-1].confirmed_at <= self._i4h, "lookahead: future pivot"
        enum = self._enum if self._use_cache else _Enumerator(p)
        raw, done = enum.run(zz.pivots, zz.leg_dir, zz.E)
        raw = [c for c in raw if not self._is_dead(c.pattern, c.d, tuple(x.i for x in c.points))]
        done = [c for c in done if not self._is_dead(c.lineage[0], c.lineage[1], c.key)]
        raw_items = [_count_item(c) for c in raw]
        # the end point of a completed pattern is the window pivot with .i == key[-1] and kind == d
        done_items = [_completed_item(c, self._sub_ts[(c.key[-1], c.lineage[1])]) for c in done]
        killed = self._history_kills(raw_items + done_items)
        if killed:
            self._dead |= killed
            raw = [c for c, it in zip(raw, raw_items) if it.key not in killed]
            raw_items = [it for it in raw_items if it.key not in killed]
            done_items = [it for it in done_items if it.key not in killed]
        counts = _dedupe_sorted(raw)
        prev = self._prev_top
        qp = [
            c.q + p.h
            if prev is not None and c.lineage == prev.lineage and c.points[:len(prev.points)] == prev.points
            else c.q
            for c in counts
        ]
        order = sorted(range(len(counts)), key=lambda k: _sort_key(counts[k], qp[k]))
        self._counts = [counts[k] for k in order]
        self._keys = [(c.pattern, c.d, tuple(x.i for x in c.points)) for c in self._counts]
        self._prices = [c.prices for c in self._counts]
        self._p0 = softmax([qp[k] for k in order], p.T)
        self._items = raw_items + done_items
        self._refresh_items()

    def _refresh_items(self) -> None:
        los = [it.lo for it in self._items if it.lo is not None]
        his = [it.hi for it in self._items if it.hi is not None]
        self._max_lo = max(los) if los else float("-inf")
        self._min_hi = min(his) if his else float("inf")
        self._lineages = frozenset(it.lineage for it in self._items)

    def _kill(self, bar1h: Bar) -> None:
        if self._zz.leg_dir > 0:
            lo_val, hi_val = bar1h.low, self._E_live
        else:
            lo_val, hi_val = self._E_live, bar1h.high
        if lo_val >= self._max_lo and hi_val <= self._min_hi:
            return
        killed = {
            it.key for it in self._items
            if (it.lo is not None and lo_val < it.lo) or (it.hi is not None and hi_val > it.hi)
        }
        self._dead |= killed
        self._items = [it for it in self._items if it.key not in killed]
        self._refresh_items()
        keep = [k for k, key in enumerate(self._keys) if key not in killed]
        if len(keep) != len(self._keys):
            self._counts = [self._counts[k] for k in keep]
            self._keys = [self._keys[k] for k in keep]
            self._prices = [self._prices[k] for k in keep]
            self._p0 = [self._p0[k] for k in keep]

    def _state(self, ts: int, new_piv: Pivot | None, closed_4h: bool) -> WaveState:
        zz = self._zz
        s = sum(self._p0)
        probs = tuple(x / s for x in self._p0) if s > 0 else ()
        e = self._E_live
        s_rev = 0.0
        for c, pr, pp in zip(self._counts, self._prices, probs):
            s_rev += pp if is_done(c.pattern, c.d, c.phase, pr, e) else -pp
        top = self._counts[0] if self._counts else None
        ready = self._i4h + 1 >= self.params.warmup_4h and zz.leg_dir != 0
        return WaveState(
            ts=ts, ready=ready, counts=tuple(self._counts), probs=probs, top=top,
            q_top=top.q if top is not None else 0.0, s_rev=s_rev, lineages=self._lineages,
            E_live=e, E_live_ts=self._E_live_ts, leg_dir=zz.leg_dir,
            leg_start=zz.pivots[-1] if zz.pivots else None, pivots4h=self._pivots_t,
            new_pivot_4h=new_piv, atr4h=zz.atr, closed_4h=closed_4h, i4h=self._i4h,
        )
