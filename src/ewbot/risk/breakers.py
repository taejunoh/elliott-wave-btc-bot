"""Circuit breakers (spec 6.3).

- DAILY_HALT: equity <= (first equity observed on this America/New_York calendar day) * (1 - daily_loss).
  Blocks new entries until the next NY midnight; open positions are kept.
- TOTAL_HALT: equity <= running peak * (1 - total_dd). Latched forever in backtests (the engine flattens).
- CONSEC_PAUSE: max_consec_losses losing trades in a row -> no new entries for consec_pause_hours; streak resets.
- enabled=False (walk-forward grid runs only): every method is a no-op; can_enter is always True.

`ts` arguments are observation instants (epoch s UTC). The engine passes the bar CLOSE instant
(bar.ts + H1) so that the observation at exactly 00:00 New York opens the new day.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from ewbot.config import BreakerParams

_NY = ZoneInfo("America/New_York")


def _ny_date(ts: int) -> date:
    return datetime.fromtimestamp(ts, tz=_NY).date()


class CircuitBreakers:
    def __init__(self, p: BreakerParams, equity0: float) -> None:
        self.p = p
        self._peak = float(equity0)
        self._day: date | None = None
        self._day_ref = float(equity0)
        self._daily_halted = False
        self._halted = False
        self._consec = 0
        self._pause_until: int | None = None

    def on_equity(self, ts: int, equity: float) -> list[str]:
        if not self.p.enabled:
            return []
        events: list[str] = []
        day = _ny_date(ts)
        if day != self._day:
            self._day = day
            self._day_ref = equity
            self._daily_halted = False
        self._peak = max(self._peak, equity)
        if not self._daily_halted and equity <= self._day_ref * (1.0 - self.p.daily_loss):
            self._daily_halted = True
            events.append("DAILY_HALT")
        if not self._halted and equity <= self._peak * (1.0 - self.p.total_dd):
            self._halted = True
            events.append("TOTAL_HALT")
        return events

    def on_trade_closed(self, ts: int, pnl: float) -> list[str]:
        if not self.p.enabled:
            return []
        if pnl >= 0.0:
            self._consec = 0
            return []
        self._consec += 1
        if self._consec >= self.p.max_consec_losses:
            self._consec = 0
            self._pause_until = ts + self.p.consec_pause_hours * 3600
            return ["CONSEC_PAUSE"]
        return []

    def can_enter(self, ts: int) -> bool:
        if self._halted:
            return False
        if self._daily_halted and _ny_date(ts) == self._day:
            return False
        if self._pause_until is not None and ts < self._pause_until:
            return False
        return True

    @property
    def halted(self) -> bool:
        return self._halted
