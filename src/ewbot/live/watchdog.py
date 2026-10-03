"""Always-on safety checks between decision cycles (Plan 3 spec 3.2 `watchdog`, 4.2, 4.5, 5.1).

  StopGapWatch   a bracket stop fills as a limit 5% beyond the stop; when the futures price has passed
                 that limit and the position is still open 10 s later, the runtime closes at market.
  margin_status  overnight-margin projection of the open position: >= 80% warn, >= 90% reduce back to
                 the 70% entry limit (entries above 70% are refused before sending, live/pricing.py).
  AccountGuard   real-account breakers: equity <= the New York day's first equity x 0.97, or <= the peak
                 x 0.85 -> flatten and pause (spec 4.5). State lives in the journal (survives restarts).
  SessionWatch   session state changes and the next scheduled maintenance (noticed 24 h ahead).
  PriceSource    the futures price for the checks: the WebSocket ticker while it is fresh (a message in
                 the last 60 s), otherwise REST polled every 10 s (spec 5.1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from ewbot.config import BreakerParams
from ewbot.live.exchange import Exchange, ExchangeError, ProductSpec, SessionInfo
from ewbot.live.journal import Journal
from ewbot.live.pricing import (
    MARGIN_ENTRY_MAX,
    MARGIN_REDUCE,
    MARGIN_WARN,
    margin_ratio,
    max_contracts_for_margin,
    stop_gapped,
)

NY = ZoneInfo("America/New_York")
GAP_GRACE_S = 10.0
WS_STALE_S = 60.0
REST_POLL_S = 10.0
MAINTENANCE_NOTICE_S = 24 * 3600


class StopGapWatch:
    def __init__(self, grace_s: float = GAP_GRACE_S) -> None:
        self.grace_s = grace_s
        self.since: float | None = None

    def check(self, direction: int, stop_price: float, price: float, now: float) -> bool:
        """True when the price has been beyond the stop's limit for `grace_s` seconds."""
        if not stop_gapped(price, stop_price, direction):
            self.since = None
            return False
        if self.since is None:
            self.since = now
        return now - self.since >= self.grace_s

    def reset(self) -> None:
        self.since = None


@dataclass(frozen=True, slots=True)
class MarginStatus:
    ratio: float
    level: str  # OK | WARN | REDUCE
    reduce_by: int = 0


def margin_status(contracts: int, price: float, side: int, product: ProductSpec, equity: float) -> MarginStatus:
    ratio = margin_ratio(contracts, price, side, product, equity)
    if ratio >= MARGIN_REDUCE:
        keep = max_contracts_for_margin(price, side, product, equity, MARGIN_ENTRY_MAX)
        return MarginStatus(ratio, "REDUCE", max(contracts - keep, 0))
    if ratio >= MARGIN_WARN:
        return MarginStatus(ratio, "WARN")
    return MarginStatus(ratio, "OK")


def ny_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, NY).date().isoformat()


class AccountGuard:
    KEY = "account_guard"

    def __init__(self, journal: Journal, params: BreakerParams | None = None) -> None:
        self.journal = journal
        self.params = BreakerParams() if params is None else params  # daily 3%, total 15% (spec 4.5)

    def state(self) -> dict[str, Any] | None:
        return self.journal.get(self.KEY)

    def update(self, equity: float, now: float) -> str | None:
        """The breaker newly tripped by this equity observation (DAILY_LOSS / TOTAL_DD), else None."""
        st = self.state() or {"peak": equity, "day": None, "day_ref": equity, "daily_tripped": False,
                              "total_tripped": False}
        day = ny_date(now)
        if st["day"] != day:
            st.update(day=day, day_ref=equity, daily_tripped=False)
        st["peak"] = max(float(st["peak"]), equity)
        trip = None
        if not st["total_tripped"] and equity <= st["peak"] * (1.0 - self.params.total_dd):
            st["total_tripped"] = st["daily_tripped"] = True  # one flatten covers both
            trip = "TOTAL_DD"
        elif not st["daily_tripped"] and equity <= st["day_ref"] * (1.0 - self.params.daily_loss):
            st["daily_tripped"] = True
            trip = "DAILY_LOSS"
        st["equity"] = equity
        self.journal.put(self.KEY, st)
        return trip

    def reset(self, equity: float, now: float) -> None:
        """After the user resumes: the current equity is the new peak and day reference."""
        self.journal.put(self.KEY, {"peak": equity, "day": ny_date(now), "day_ref": equity, "daily_tripped": False,
                                    "total_tripped": False, "equity": equity})


class SessionWatch:
    def __init__(self) -> None:
        self.state: str | None = None
        self.noticed: set[int] = set()

    def update(self, info: SessionInfo, now: float) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if self.state is not None and info.state != self.state:
            out.append(("SESSION", f"장 운영 상태 {self.state} → {info.state}"))
        self.state = info.state
        start = info.maintenance_start
        if start is not None and 0 < start - now <= MAINTENANCE_NOTICE_S and start not in self.noticed:
            self.noticed.add(start)
            end = info.maintenance_end
            out.append(("MAINTENANCE", f"정기 점검 예정 {datetime.fromtimestamp(start, NY):%Y-%m-%d %H:%M} ~ "
                                       f"{'' if end is None else f'{datetime.fromtimestamp(end, NY):%H:%M}'} (뉴욕)"))
        return out


class Feed(Protocol):
    def latest(self, product_id: str) -> Any: ...


class PriceSource:
    def __init__(self, exchange: Exchange, feed: Feed | None = None, *, clock: Callable[[], float],
                 stale_s: float = WS_STALE_S, poll_s: float = REST_POLL_S) -> None:
        self.ex = exchange
        self.feed = feed
        self.clock = clock
        self.stale_s = stale_s
        self.poll_s = poll_s
        self.mode = "rest"
        self._rest: float | None = None
        self._rest_ts: float | None = None

    def futures_price(self) -> float | None:
        now = self.clock()
        if self.feed is not None:
            tick = self.feed.latest(self.ex.futures_id)
            if tick is not None and now - tick.ts <= self.stale_s:
                self.mode = "ws"
                return float(tick.price)
        self.mode = "rest"
        if self._rest_ts is None or now - self._rest_ts >= self.poll_s:
            try:
                self._rest = self.ex.quote(self.ex.futures_id).mid
                self._rest_ts = now
            except ExchangeError:
                pass
        return self._rest
