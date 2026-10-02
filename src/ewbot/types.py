from __future__ import annotations

from dataclasses import dataclass

H1 = 3600
H4 = 14400


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV bar. ``ts`` is the bar OPEN time, epoch seconds UTC."""

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    synthetic: bool = False  # True if gap-filled
