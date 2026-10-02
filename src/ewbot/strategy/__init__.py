# src/ewbot/strategy/__init__.py
"""Trading strategies (spec section 5)."""

from ewbot.strategy.base import Signal, Strategy, blended_r, time_stop_bars
from ewbot.strategy.choch import ChochDetector
from ewbot.strategy.elliott import ElliottStrategy
from ewbot.strategy.fib_swing import FibSwingStrategy
from ewbot.strategy.hybrid import HybridStrategy, make_strategy

__all__ = [
    "ChochDetector",
    "ElliottStrategy",
    "FibSwingStrategy",
    "HybridStrategy",
    "Signal",
    "Strategy",
    "blended_r",
    "make_strategy",
    "time_stop_bars",
]
