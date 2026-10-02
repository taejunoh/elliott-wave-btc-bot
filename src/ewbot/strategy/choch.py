# src/ewbot/strategy/choch.py
"""1h trigger, spec 5.2: breakout of the E-bar neighbourhood ("CHoCH" in names and comments).

* ``level(+1, leg_start_ts, E_ts)`` = H1: max HIGH of the ``n_bars`` 1h bars immediately BEFORE the
  1h bar where E_live occurred (``E_ts``); the E bar itself and later bars are excluded. Fewer bars
  are used if the history is shorter; with no bar before E the level is None.
* ``level(-1, ...)`` = L1: the mirror image (min LOW).
* ``triggered()``: close_t beyond the level and close_{t-1} not beyond it, where t is the last
  bar fed to ``update()``.

``leg_start_ts`` is accepted for interface stability but unused. Only bars already fed are used,
so the detector cannot look ahead.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence

from ewbot.types import Bar


def _choch_level(
    ts: Sequence[int],
    highs: Sequence[float],
    lows: Sequence[float],
    direction: int,
    E_ts: int,
    n_bars: int,
) -> float | None:
    """Pure core of ``ChochDetector.level``."""
    if direction not in (1, -1):
        raise ValueError(f"direction must be +1 or -1, got {direction}")
    e_idx = bisect_left(ts, E_ts)  # == number of bars strictly before the E bar
    lo = max(0, e_idx - n_bars)
    if lo >= e_idx:
        return None
    if direction == 1:
        return max(highs[lo:e_idx])
    return min(lows[lo:e_idx])


class ChochDetector:
    """Keeps the raw 1h history; call ``update`` once per closed 1h bar before ``triggered``."""

    def __init__(self, n_bars: int = 3) -> None:
        if n_bars < 1:
            raise ValueError("n_bars must be >= 1")
        self._n = n_bars
        self._ts: list[int] = []
        self._high: list[float] = []
        self._low: list[float] = []
        self._close: list[float] = []

    def update(self, bar1h: Bar) -> None:
        if self._ts and bar1h.ts <= self._ts[-1]:
            raise ValueError(f"1h bars must arrive in increasing ts order: {bar1h.ts} after {self._ts[-1]}")
        self._ts.append(bar1h.ts)
        self._high.append(bar1h.high)
        self._low.append(bar1h.low)
        self._close.append(bar1h.close)

    def level(self, direction: int, leg_start_ts: int, E_ts: int) -> float | None:
        return _choch_level(self._ts, self._high, self._low, direction, E_ts, self._n)

    def triggered(self, direction: int, leg_start_ts: int, E_ts: int) -> bool:
        if len(self._close) < 2:
            return False
        lvl = self.level(direction, leg_start_ts, E_ts)
        if lvl is None:
            return False
        c, c_prev = self._close[-1], self._close[-2]
        if direction == 1:
            return c > lvl and c_prev <= lvl
        return c < lvl and c_prev >= lvl
