"""Close-confirmed ATR ZigZag (spec 4.1). Used by the 4h structure engine (WaveEngine).

Rules implemented here:
- threshold thr_t = k * ATR(atr_len) computed on this ZigZag's own bars up to and including bar t;
- extremes are tracked on high/low (wicks); a pivot is confirmed only when a bar CLOSE has retraced
  >= thr from the provisional extreme E; at most one pivot is confirmed per bar;
- every bar is processed as a sequence of "atoms" (its 1h sub-bars when given, else the bar itself).
  When a reversal is confirmed, the next leg's initial E is computed from the CLOSE of the atom that
  made the old extreme plus the full high/low of the atoms after it. That is spec 4.1's same-4h-bar
  rule ("only the close of the extreme 1h bar and the later 1h bars"), applied uniformly to every bar,
  so it holds whether or not the reversal happens inside the extreme's own 4h bar;
- pivots are immutable and append-only; ``confirmed_at`` is the index of the confirming bar.
Wick clipping (spec 4.1, 6 x ATR1h) is NOT done here: callers feed already-clipped bars.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from ewbot.indicators import ATR
from ewbot.types import Bar


@dataclass(frozen=True, slots=True)
class Pivot:
    i: int             # index of the bar (in this ZigZag's own bar sequence) where the extreme occurred
    sub_ts: int        # ts of the 1h bar where the extreme occurred (for a 1h ZigZag = that bar's ts)
    price: float       # extreme price (high for kind=+1, low for kind=-1)
    kind: int          # +1 high, -1 low
    confirmed_at: int  # index of the bar on whose CLOSE the pivot was confirmed (>= i)


# atom = (bar index, ts, high, low, close)
_Atom = tuple[int, int, float, float, float]


class ZigZag:
    def __init__(self, k: float, atr_len: int = 14) -> None:
        if k <= 0:
            raise ValueError("k must be > 0")
        self.k = k
        self._atr = ATR(atr_len)
        self.atr: float | None = None
        self.pivots: list[Pivot] = []
        self.leg_dir: int = 0
        self.E: float = math.nan
        self.E_i: int = -1
        self.E_sub_ts: int = -1
        # After init: atoms from the atom holding E (index 0) to the latest atom.
        # Before init: every atom seen so far.
        self._atoms: list[_Atom] = []
        self._last_i: int | None = None

    # ------------------------------------------------------------------ public
    def update(self, i: int, bar: Bar, sub_bars: Sequence[Bar] | None = None) -> Pivot | None:
        """Feed the CLOSED bar ``i``; return the pivot confirmed on its close, else None."""
        if self._last_i is not None and i <= self._last_i:
            raise ValueError(f"bar index must increase: got {i} after {self._last_i}")
        self._last_i = i
        if sub_bars:
            atoms = [(i, b.ts, b.high, b.low, b.close) for b in sub_bars]
        else:
            atoms = [(i, bar.ts, bar.high, bar.low, bar.close)]
        self.atr = self._atr.update(bar.high, bar.low, bar.close)
        if self.leg_dir == 0:
            return self._update_uninitialised(i, bar.close, atoms)
        self._extend(atoms)
        if self.atr is None:
            return None
        if self.leg_dir * (self.E - bar.close) >= self.k * self.atr:
            return self._confirm(i)
        return None

    # ----------------------------------------------------------------- private
    def _extend(self, atoms: list[_Atom]) -> None:
        d = self.leg_dir
        for a in atoms:
            ext = a[2] if d > 0 else a[3]
            if d * (ext - self.E) > 0:          # strictly beyond -> first occurrence wins ties
                self.E, self.E_i, self.E_sub_ts = ext, a[0], a[1]
                self._atoms = [a]
            else:
                self._atoms.append(a)

    def _confirm(self, i: int) -> Pivot:
        d = self.leg_dir
        piv = Pivot(self.E_i, self.E_sub_ts, self.E, d, i)
        self.pivots.append(piv)
        nd = -d
        atoms = self._atoms
        # atom 0 holds the old extreme: only its close may seed the new leg (spec 4.1 same-bar rule)
        best_k, best_v = 0, atoms[0][4]
        for k in range(1, len(atoms)):
            v = atoms[k][2] if nd > 0 else atoms[k][3]
            if nd * (v - best_v) > 0:
                best_k, best_v = k, v
        a = atoms[best_k]
        self.leg_dir = nd
        self.E, self.E_i, self.E_sub_ts = best_v, a[0], a[1]
        self._atoms = atoms[best_k:]
        return piv

    def _update_uninitialised(self, i: int, close: float, atoms: list[_Atom]) -> Pivot | None:
        self._atoms.extend(atoms)
        if self.atr is None:
            return None
        thr = self.k * self.atr
        all_atoms = self._atoms
        hi_k = lo_k = 0
        for k in range(1, len(all_atoms)):
            if all_atoms[k][2] > all_atoms[hi_k][2]:
                hi_k = k
            if all_atoms[k][3] < all_atoms[lo_k][3]:
                lo_k = k
        cond_hi = all_atoms[hi_k][2] - close >= thr     # close retraced thr below the highest high
        cond_lo = close - all_atoms[lo_k][3] >= thr     # close retraced thr above the lowest low
        if cond_hi and cond_lo:
            if hi_k == lo_k:                             # ambiguous single atom: wait (keeps mirror symmetry)
                return None
            use_hi = hi_k > lo_k                         # the more recent extreme becomes the first pivot
        elif cond_hi or cond_lo:
            use_hi = cond_hi
        else:
            return None
        k0 = hi_k if use_hi else lo_k
        a = all_atoms[k0]
        self.leg_dir = 1 if use_hi else -1
        self.E = a[2] if use_hi else a[3]
        self.E_i, self.E_sub_ts = a[0], a[1]
        self._atoms = all_atoms[k0:]
        return self._confirm(i)
