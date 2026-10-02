"""Strategy plumbing shared by every strategy: Signal, R gate, time stop, Strategy protocol."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ewbot.config import StrategyParams

if TYPE_CHECKING:
    from ewbot.strategy.choch import ChochDetector
    from ewbot.types import Bar
    from ewbot.waves.engine import WaveState


@dataclass(frozen=True, slots=True)
class Signal:
    ts: int
    setup: str  # "W3","W5","W5_REV","C_END","FIB","RANDOM","EMA"
    direction: int
    entry_ref: float  # trigger bar close (R gate / sizing estimate)
    stop: float
    tp1: float
    tp2: float | None
    risk_mult: float  # 1.0 or setup3_risk_mult
    time_stop_4h: int | None  # 4h bars until time stop (None = no time stop)
    instance: tuple[Any, ...] | None  # cooldown key; Count.instance for Elliott
    lineage: tuple[Any, ...] | None  # lineage-kill key; None for non-Elliott
    reason: str


def blended_r(
    direction: int, entry: float, stop: float, tp1: float, tp2: float | None, tp1_frac: float
) -> float:
    """tp1_frac*R_TP1 + (1-tp1_frac)*R_TP2, R = (target-entry)/(entry-stop) signed by direction.

    tp2=None means the whole position is assumed to exit at tp1. A stop on the wrong side of
    entry (zero or negative risk) returns -inf so that every R gate rejects it.
    """
    if direction not in (1, -1):
        raise ValueError(f"direction must be +1 or -1, got {direction}")
    risk = direction * (entry - stop)
    if risk <= 0.0:
        return float("-inf")
    r1 = direction * (tp1 - entry) / risk
    if tp2 is None:
        return r1
    r2 = direction * (tp2 - entry) / risk
    return tp1_frac * r1 + (1.0 - tp1_frac) * r2


def time_stop_bars(leg_bars_4h: int, p: StrategyParams) -> int:
    """clip(round_half_up(time_stop_mult * leg), time_stop_min_4h, time_stop_max_4h)."""
    raw = math.floor(p.time_stop_mult * max(0, leg_bars_4h) + 0.5)
    return int(min(p.time_stop_max_4h, max(p.time_stop_min_4h, raw)))


def _finalize(
    *,
    ts: int,
    setup: str,
    direction: int,
    entry_ref: float,
    stop: float,
    tp1: float,
    tp2: float | None,
    risk_mult: float,
    time_stop_4h: int | None,
    instance: tuple[Any, ...] | None,
    lineage: tuple[Any, ...] | None,
    reason: str,
    sp: StrategyParams,
) -> Signal | None:
    """Common tail of every setup (strategy-package private).

    1. Order targets near -> far (TP1 is always the nearer one; only setup W5_REV can need a swap).
    2. Reject a stop that is not on the loss side of entry_ref, or a TP1 that is not on the profit side.
    3. R gate: blended_r(direction, entry_ref, stop, tp1, tp2, sp.tp1_frac) >= sp.r_gate.
    """
    if tp2 is not None and direction * (tp2 - tp1) < 0:
        tp1, tp2 = tp2, tp1
    if direction * (entry_ref - stop) <= 0:
        return None
    if direction * (tp1 - entry_ref) <= 0:
        return None
    r = blended_r(direction, entry_ref, stop, tp1, tp2, sp.tp1_frac)
    if r < sp.r_gate:
        return None
    return Signal(
        ts=ts,
        setup=setup,
        direction=direction,
        entry_ref=entry_ref,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        risk_mult=risk_mult,
        time_stop_4h=time_stop_4h,
        instance=instance,
        lineage=lineage,
        reason=f"{reason} R={r:.2f}",
    )


@runtime_checkable
class Strategy(Protocol):
    name: str

    def on_bar(self, bar: Bar, ws: WaveState, choch: ChochDetector) -> Signal | None:
        """Called every closed 1h bar AFTER WaveEngine and ChochDetector were updated.

        Must NOT check flat/breakers/blackout/cooldown (the engine does). MUST apply the q/S_rev
        gates, the entry zone, the CHoCH trigger and the R gate.
        """
