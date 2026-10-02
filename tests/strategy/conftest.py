# tests/strategy/conftest.py
"""Hand-built WaveState / Count / Pivot builders for strategy unit tests.

Exposed through the `sb` fixture so the helpers work under any pytest import mode.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ewbot.types import H1, H4, Bar
from ewbot.waves.engine import WaveState
from ewbot.waves.enumerate import Count
from ewbot.waves.zigzag import Pivot

T0 = 1_600_000_000 - (1_600_000_000 % H4)  # 4h-aligned origin; 4h bar i opens at T0 + i*H4


def piv(i: int, price: float, kind: int) -> Pivot:
    """4h pivot at bar i; the extreme happened in the 2nd 1h bar of that 4h bar."""
    return Pivot(i=i, sub_ts=T0 + i * H4 + H1, price=float(price), kind=kind, confirmed_at=i + 2)


def count(pattern: str, d: int, phase: str, pts: Sequence[Pivot], q: float = 0.7) -> Count:
    return Count(pattern=pattern, d=d, phase=phase, points=tuple(pts), q=q)


def wave_state(
    *,
    top: Count | None,
    E: float,
    leg_dir: int,
    pivots: Sequence[Pivot],
    atr: float | None = 8.0,
    s_rev: float = 0.5,
    i4h: int | None = None,
    ready: bool = True,
) -> WaveState:
    leg_start = pivots[-1] if pivots else None
    if i4h is None:
        i4h = (leg_start.i if leg_start is not None else 0) + 6
    e_ts = T0 + (i4h - 1) * H4 + 2 * H1  # E_live printed in a 1h bar of the previous 4h bar
    now = T0 + (i4h + 1) * H4 + 2 * H1  # the 1h bar that just closed
    counts = (top,) if top is not None else ()
    return WaveState(
        ts=now,
        ready=ready,
        counts=counts,
        probs=(1.0,) * len(counts),
        top=top,
        q_top=top.q if top is not None else 0.0,
        s_rev=s_rev,
        lineages=frozenset(c.lineage for c in counts),
        E_live=float(E),
        E_live_ts=e_ts,
        leg_dir=leg_dir,
        leg_start=leg_start,
        pivots4h=tuple(pivots),
        new_pivot_4h=None,
        atr4h=atr,
        closed_4h=False,
        i4h=i4h,
    )


def bar_for(ws: WaveState, close: float) -> Bar:
    """The just-closed 1h trigger bar of `ws` with the given close."""
    return Bar(ts=ws.ts, open=close, high=close + 1.0, low=close - 1.0, close=float(close), volume=1.0)


class FakeChoch:
    """Stands in for ChochDetector: answers `fire` and records every query."""

    def __init__(self, fire: bool = True) -> None:
        self.fire = fire
        self.calls: list[tuple[int, int, int]] = []

    def triggered(self, direction: int, leg_start_ts: int, E_ts: int) -> bool:
        self.calls.append((direction, leg_start_ts, E_ts))
        return self.fire

    def level(self, direction: int, leg_start_ts: int, E_ts: int) -> float | None:
        return None


def mirror_pivot(p: Pivot, m: float) -> Pivot:
    return replace(p, price=2 * m - p.price, kind=-p.kind)


def mirror_count(c: Count, m: float) -> Count:
    return Count(pattern=c.pattern, d=-c.d, phase=c.phase, points=tuple(mirror_pivot(p, m) for p in c.points), q=c.q)


def mirror_ws(ws: WaveState, m: float) -> WaveState:
    counts = tuple(mirror_count(c, m) for c in ws.counts)
    return replace(
        ws,
        counts=counts,
        top=counts[0] if counts else None,
        lineages=frozenset(c.lineage for c in counts),
        E_live=2 * m - ws.E_live,
        leg_dir=-ws.leg_dir,
        leg_start=mirror_pivot(ws.leg_start, m) if ws.leg_start is not None else None,
        pivots4h=tuple(mirror_pivot(p, m) for p in ws.pivots4h),
        new_pivot_4h=mirror_pivot(ws.new_pivot_4h, m) if ws.new_pivot_4h is not None else None,
    )


def mirror_bar(b: Bar, m: float) -> Bar:
    return Bar(ts=b.ts, open=2 * m - b.open, high=2 * m - b.low, low=2 * m - b.high, close=2 * m - b.close, volume=b.volume)


@pytest.fixture
def sb() -> SimpleNamespace:
    return SimpleNamespace(
        T0=T0,
        piv=piv,
        count=count,
        ws=wave_state,
        bar=bar_for,
        FakeChoch=FakeChoch,
        mirror_ws=mirror_ws,
        mirror_bar=mirror_bar,
    )
