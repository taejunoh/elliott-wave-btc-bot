# src/ewbot/strategy/elliott.py
"""Strategy A: the four Elliott setups of spec 5.1, bull and bear symmetric.

Trade direction per top-count phase (d = Count.d; for ABC d is the direction of wave A):
  W3     = IMP W2 -> trade  d   (buy the end of wave 2)
  W5     = IMP W4 -> trade  d   (buy the end of wave 4)
  W5_REV = IMP W5 -> trade -d   (fade the end of wave 5, risk_mult = setup3_risk_mult)
  C_END  = ABC C  -> trade -d   (bull C_END = ABC d=-1 -> long)
The in-progress leg always runs against the trade, so ws.leg_dir must equal -direction and the
CHoCH is asked for `direction`. All zone checks use ws.E_live; targets are computed from
ws.E_live at signal time and never change afterwards.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ewbot.config import StrategyParams
from ewbot.strategy.base import Signal, _finalize, time_stop_bars

if TYPE_CHECKING:
    from ewbot.strategy.choch import ChochDetector
    from ewbot.types import Bar
    from ewbot.waves.engine import WaveState
    from ewbot.waves.enumerate import Count
    from ewbot.waves.zigzag import Pivot

_SETUP_OF: dict[tuple[str, str], str] = {
    ("IMP", "W2"): "W3",
    ("IMP", "W4"): "W5",
    ("IMP", "W5"): "W5_REV",
    ("ABC", "C"): "C_END",
}
_WITH_TREND = frozenset({"W3", "W5"})

_W3_ZONE = (0.382, 0.786)  # r2 = |W2|/|W1|
_W3_TP1_W1 = 1.0
_W3_TP2_W1 = 1.618
_W5_ZONE = (0.236, 0.5)  # r4 = |W4|/|W3|
_W5_TP2_SP3 = 0.618  # TP2 alt = E + 0.618*(P3 - S)
_REV_MIN_W5_W1 = 0.618  # W5 >= 0.618*|W1|
_REV_TP1_RETR = 0.382  # TP1 = E - 0.382*(E - S)
_C_ZONE = (0.786, 1.786)  # |C|/|A|
_C_MAX_RETRACE = 0.786  # (X - E)/(X - prior swing extreme)
_C_TP1_RETR = 0.618  # TP1 = E + 0.618*(X - E)
_SWING_LOOKBACK = 4  # confirmed 4h pivots before X searched for the prior swing extreme


@dataclass(frozen=True, slots=True)
class _Plan:
    stop: float
    tp1: float
    tp2: float | None
    leg_bars_4h: int
    risk_mult: float
    detail: str


def _h3_cap(d: int, s: float, p1: float, p2: float, p3: float, p4: float) -> float | None:
    """Spec 4.3 H3 while wave 5 runs: if |W3| < |W1| then W5 may not exceed P4 + d*|W3|."""
    w1 = abs(p1 - s)
    w3 = abs(p3 - p2)
    return p4 + d * w3 if w3 < w1 else None


def _in(x: float, zone: tuple[float, float]) -> bool:
    return zone[0] <= x <= zone[1]


def _plan_w3(c: Count, ws: WaveState, sp: StrategyParams, atr: float) -> _Plan | None:
    s, p1 = c.prices
    d, e = c.d, ws.E_live
    w1 = abs(p1 - s)
    if w1 <= 0.0:
        return None
    r2 = (p1 - e) / (p1 - s)
    if not _in(r2, _W3_ZONE):
        return None
    return _Plan(
        stop=s - d * sp.buf_struct_atr * atr,
        tp1=e + d * _W3_TP1_W1 * w1,
        tp2=e + d * _W3_TP2_W1 * w1,
        leg_bars_4h=c.points[1].i - c.points[0].i,  # W1
        risk_mult=1.0,
        detail=f"r2={r2:.3f}",
    )


def _plan_w5(c: Count, ws: WaveState, sp: StrategyParams, atr: float) -> _Plan | None:
    s, p1, p2, p3 = c.prices
    d, e = c.d, ws.E_live
    w1 = abs(p1 - s)
    w3 = abs(p3 - p2)
    if w1 <= 0.0 or w3 <= 0.0:
        return None
    r4 = (p3 - e) / (p3 - p2)
    if not _in(r4, _W5_ZONE) or d * (e - p1) <= 0.0:
        return None
    tp2 = e + d * max(w1, _W5_TP2_SP3 * abs(p3 - s))
    cap = _h3_cap(d, s, p1, p2, p3, e)  # E becomes P4 if wave 4 ends here
    if cap is not None and d * (tp2 - cap) > 0.0:
        tp2 = cap
    return _Plan(
        stop=p1 - d * sp.buf_struct_atr * atr,
        tp1=p3,
        tp2=tp2,
        leg_bars_4h=c.points[1].i - c.points[0].i,  # W1
        risk_mult=1.0,
        detail=f"r4={r4:.3f}",
    )


def _plan_w5_rev(c: Count, ws: WaveState, sp: StrategyParams, atr: float) -> _Plan | None:
    s, p1, p2, p3, p4 = c.prices
    d, e = c.d, ws.E_live
    w1 = abs(p1 - s)
    if w1 <= 0.0:
        return None
    w5 = d * (e - p4)
    if w5 < _REV_MIN_W5_W1 * w1 or d * (e - p3) <= 0.0:
        return None
    cap = _h3_cap(d, s, p1, p2, p3, p4)
    if cap is not None and d * (e - cap) > 0.0:
        return None  # beyond the H3 bound: the count is dead
    stop = e + d * sp.buf_ext_atr * atr
    if cap is not None and d * (stop - cap) > 0.0:
        stop = cap
    return _Plan(
        stop=stop,
        tp1=e - _REV_TP1_RETR * (e - s),
        tp2=p4,
        leg_bars_4h=ws.i4h - c.points[4].i,  # W5 is still in progress
        risk_mult=sp.setup3_risk_mult,
        detail=f"w5/w1={w5 / w1:.3f}",
    )


def _prior_pivots(pivots: Sequence[Pivot], before_i: int, n: int) -> list[Pivot]:
    """Up to n confirmed pivots with index < before_i, scanning back from the newest."""
    out: list[Pivot] = []
    for p in reversed(pivots):
        if p.i >= before_i:
            continue
        out.append(p)
        if len(out) == n:
            break
    return out


def _plan_c_end(c: Count, ws: WaveState, sp: StrategyParams, atr: float) -> _Plan | None:
    x, a_end, b_end = c.prices
    d, e = c.d, ws.E_live  # d = direction of wave A (and of the in-progress wave C)
    a = abs(a_end - x)
    c_len = d * (e - b_end)
    if a <= 0.0 or c_len <= 0.0:
        return None
    c_over_a = c_len / a
    if not _in(c_over_a, _C_ZONE) or d * (e - a_end) <= 0.0:
        return None
    swings = [p.price for p in _prior_pivots(ws.pivots4h, c.points[0].i, _SWING_LOOKBACK) if p.kind == d]
    if not swings:
        return None
    swing = max(swings, key=lambda v: d * v)  # lowest low before X for a bull C_END
    denom = x - swing
    if -d * denom <= 0.0:
        return None
    retrace = (x - e) / denom
    if retrace > _C_MAX_RETRACE:
        return None
    t = -d
    return _Plan(
        stop=e - t * sp.buf_ext_atr * atr,
        tp1=e + _C_TP1_RETR * (x - e),
        tp2=x,
        leg_bars_4h=c.points[1].i - c.points[0].i,  # wave A
        risk_mult=1.0,
        detail=f"c/a={c_over_a:.3f} retr={retrace:.3f}",
    )


_PLANNERS: dict[str, Callable[[Count, WaveState, StrategyParams, float], _Plan | None]] = {
    "W3": _plan_w3,
    "W5": _plan_w5,
    "W5_REV": _plan_w5_rev,
    "C_END": _plan_c_end,
}


class ElliottStrategy:
    name = "A"

    def __init__(self, sp: StrategyParams) -> None:
        self.sp = sp

    def on_bar(self, bar: Bar, ws: WaveState, choch: ChochDetector) -> Signal | None:
        sp = self.sp
        top = ws.top
        if not ws.ready or top is None or ws.leg_start is None or ws.atr4h is None:
            return None
        setup = _SETUP_OF.get((top.pattern, top.phase))
        if setup is None or setup not in sp.setups_enabled:
            return None
        direction = top.d if setup in _WITH_TREND else -top.d
        if ws.leg_dir != -direction:
            return None
        if top.q < sp.Q_MIN or ws.s_rev < sp.S_MIN:
            return None
        plan = _PLANNERS[setup](top, ws, sp, ws.atr4h)
        if plan is None:
            return None
        if not choch.triggered(direction, ws.leg_start.sub_ts, ws.E_live_ts):
            return None
        return _finalize(
            ts=bar.ts,
            setup=setup,
            direction=direction,
            entry_ref=bar.close,
            stop=plan.stop,
            tp1=plan.tp1,
            tp2=plan.tp2,
            risk_mult=plan.risk_mult,
            time_stop_4h=time_stop_bars(plan.leg_bars_4h, sp),
            instance=top.instance,
            lineage=top.lineage,
            reason=(
                f"{setup} {top.pattern}{top.d:+d} {top.phase} {plan.detail} "
                f"q={top.q:.3f} s_rev={ws.s_rev:.3f} E={ws.E_live:.2f}"
            ),
            sp=sp,
        )
