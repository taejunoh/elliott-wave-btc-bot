# src/ewbot/strategy/fib_swing.py
"""Strategy C: Fibonacci swing baseline (spec 5.5), long and short symmetric.

Trend from the last two confirmed 4h highs and lows: HH+HL = up (+1), LH+LL = down (-1), else none.
Up-trend with the in-progress leg falling: anchor H = last confirmed high, base L = the confirmed
low before it; long when r = (H - E)/(H - L) in [0.382, 0.786], E > L, the 1h trigger up and the R
gate pass. Stop = L - buf_struct_atr*ATR4h, TP1 = E + 1.0*(H - L), TP2 = E + 1.618*(H - L) (setup W3
geometry with S->L, P1->H, so A vs C isolates the Elliott count filter). Down-trend is the mirror image.
Time-stop reference leg = the swing L -> H (base.i -> anchor.i).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from ewbot.config import StrategyParams
from ewbot.strategy.base import Signal, _finalize, time_stop_bars

if TYPE_CHECKING:
    from ewbot.strategy.choch import ChochDetector
    from ewbot.types import Bar
    from ewbot.waves.engine import WaveState
    from ewbot.waves.zigzag import Pivot

_FIB_ZONE = (0.382, 0.786)
_FIB_TP1_SWING = 1.0  # TP1 = E + 1.0 * (H - L)   (same geometry as Elliott setup W3)
_FIB_TP2_SWING = 1.618  # TP2 = E + 1.618 * (H - L)


def _last_of_kind(pivots: Sequence[Pivot], kind: int, n: int, before_i: int | None = None) -> list[Pivot]:
    """Newest-first list of up to n pivots of `kind` (optionally only those with i < before_i)."""
    out: list[Pivot] = []
    for p in reversed(pivots):
        if before_i is not None and p.i >= before_i:
            continue
        if p.kind == kind:
            out.append(p)
            if len(out) == n:
                break
    return out


def _swing_trend(pivots: Sequence[Pivot]) -> int:
    highs = _last_of_kind(pivots, 1, 2)
    lows = _last_of_kind(pivots, -1, 2)
    if len(highs) < 2 or len(lows) < 2:
        return 0
    hh = highs[0].price > highs[1].price
    hl = lows[0].price > lows[1].price
    lh = highs[0].price < highs[1].price
    ll = lows[0].price < lows[1].price
    if hh and hl:
        return 1
    if lh and ll:
        return -1
    return 0


class FibSwingStrategy:
    name = "C"

    def __init__(self, sp: StrategyParams) -> None:
        self.sp = sp

    def on_bar(self, bar: Bar, ws: WaveState, choch: ChochDetector) -> Signal | None:
        sp = self.sp
        if not ws.ready or ws.leg_start is None or ws.atr4h is None:
            return None
        t = _swing_trend(ws.pivots4h)
        if t == 0 or ws.leg_dir != -t:
            return None
        anchors = _last_of_kind(ws.pivots4h, t, 1)
        if not anchors:
            return None
        anchor = anchors[0]
        bases = _last_of_kind(ws.pivots4h, -t, 1, before_i=anchor.i)
        if not bases:
            return None
        base = bases[0]
        swing = anchor.price - base.price
        if t * swing <= 0.0:
            return None
        e = ws.E_live
        r = (anchor.price - e) / swing
        if not (_FIB_ZONE[0] <= r <= _FIB_ZONE[1]) or t * (e - base.price) <= 0.0:
            return None
        if not choch.triggered(t, ws.leg_start.sub_ts, ws.E_live_ts):
            return None
        return _finalize(
            ts=bar.ts,
            setup="FIB",
            direction=t,
            entry_ref=bar.close,
            stop=base.price - t * sp.buf_struct_atr * ws.atr4h,
            tp1=e + _FIB_TP1_SWING * swing,
            tp2=e + _FIB_TP2_SWING * swing,
            risk_mult=1.0,
            time_stop_4h=time_stop_bars(anchor.i - base.i, sp),
            instance=None,
            lineage=None,
            reason=f"FIB trend={t:+d} r={r:.3f} E={e:.2f}",
            sp=sp,
        )
