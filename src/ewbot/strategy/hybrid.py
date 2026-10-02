# src/ewbot/strategy/hybrid.py
"""Hybrid A+C (spec 5.6) and the strategy factory.

Every 1h bar the Elliott state is classified (checked in this order):
  CONFIDENT  top exists, top.q >= Q_MIN and S_rev >= S_MIN -> only A (Elliott) signals
  CONTRARY   top exists and S_rev <= -S_MIN                  -> no signal at all (C blocked too)
  UNCERTAIN  everything else (incl. no alive count)          -> only C (Fib swing) signals
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from ewbot.config import StrategyParams
from ewbot.strategy.base import Signal, Strategy
from ewbot.strategy.elliott import ElliottStrategy
from ewbot.strategy.fib_swing import FibSwingStrategy

if TYPE_CHECKING:
    from ewbot.strategy.choch import ChochDetector
    from ewbot.types import Bar
    from ewbot.waves.engine import WaveState

CONFIDENT = "CONFIDENT"
CONTRARY = "CONTRARY"
UNCERTAIN = "UNCERTAIN"


class HybridStrategy:
    name = "HYBRID"

    def __init__(self, sp: StrategyParams) -> None:
        self.sp = sp
        self.elliott = ElliottStrategy(sp)
        self.fib = FibSwingStrategy(sp)

    def classify(self, ws: WaveState) -> str:
        top = ws.top
        if top is not None and top.q >= self.sp.Q_MIN and ws.s_rev >= self.sp.S_MIN:
            return CONFIDENT
        if top is not None and ws.s_rev <= -self.sp.S_MIN:
            return CONTRARY
        return UNCERTAIN

    def on_bar(self, bar: Bar, ws: WaveState, choch: ChochDetector) -> Signal | None:
        state = self.classify(ws)
        if state == CONFIDENT:
            sig = self.elliott.on_bar(bar, ws, choch)
        elif state == UNCERTAIN:
            sig = self.fib.on_bar(bar, ws, choch)
        else:
            return None
        if sig is None:
            return None
        return replace(sig, reason=f"{state}: {sig.reason}")


def make_strategy(sp: StrategyParams) -> Strategy:
    if sp.mode == "A":
        return ElliottStrategy(sp)
    if sp.mode == "C":
        return FibSwingStrategy(sp)
    if sp.mode == "HYBRID":
        return HybridStrategy(sp)
    raise ValueError(f"unknown strategy mode {sp.mode!r} (expected 'A', 'C' or 'HYBRID')")
