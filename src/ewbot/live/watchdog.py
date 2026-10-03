"""Always-on safety checks between decision cycles (Plan 3 spec 3.2 `watchdog`, 4.2, 4.5, 5.1).

  StopGapWatch   a bracket stop fills as a limit 5% beyond the stop; when the futures price has passed
                 that limit and the position is still open 10 s later, the runtime closes at market.
  margin_status  overnight-margin projection of the open position: >= 80% warn, >= 90% reduce back to
                 the 70% entry limit (entries above 70% are refused before sending, live/pricing.py).
  AccountGuard   real-account breakers: equity <= the New York day's first equity x 0.97, or <= the peak
                 x 0.85 -> flatten and pause (spec 4.5), on the second observation in a row that breaches.
                 State lives in the journal (survives restarts).
  SessionWatch   session state changes and the next scheduled maintenance (noticed 24 h ahead, or at once
                 when it is already under way).
  PriceSource    the futures price for the checks: the WebSocket ticker while it is fresh (a usable price in
                 the last 60 s), otherwise REST polled every 10 s; no price once the last good one is older
                 than 60 s (spec 5.1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from ewbot.config import BreakerParams
from ewbot.live.exchange import Exchange, ExchangeError, ProductSpec, SessionInfo
from ewbot.live.feed import usable_price
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
    """`now` is the runtime's wall clock: when it steps backwards the timer is armed again at the new time,
    so a close fires at most one grace period late (never as late as the size of the step)."""

    def __init__(self, grace_s: float = GAP_GRACE_S) -> None:
        self.grace_s = grace_s
        self.since: float | None = None

    def check(self, direction: int, stop_price: float, price: float, now: float) -> bool:
        """True when the price has been beyond the stop's limit for `grace_s` seconds."""
        if not stop_gapped(price, stop_price, direction):
            self.since = None
            return False
        if self.since is None or now < self.since:
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


class BadEquity(ValueError):
    """An equity that is not a finite amount above 0: a failed balance read, never an observation."""


def _observed(equity: float) -> float:
    e = usable_price(equity)
    if e is None:
        raise BadEquity(f"equity {equity!r} is not a finite amount above 0")
    return e


class AccountGuard:
    """Real-account breakers (spec 4.5). Only a finite equity above 0 is an observation; anything else raises
    BadEquity and changes nothing (the runtime alerts it). One observation can neither trip a breaker nor
    raise a reference: a breaker trips on the second observation in a row that breaches it (one poll - a
    minute in the runtime - after the first), and the peak only rises to a level two observations in a row
    reached (the lower of the two), so a single wrong balance read, low or high, cannot flatten the account.
    The day's reference is the first observation of the New York day."""

    KEY = "account_guard"

    def __init__(self, journal: Journal, params: BreakerParams | None = None) -> None:
        self.journal = journal
        self.params = BreakerParams() if params is None else params  # daily 3%, total 15% (spec 4.5)

    def state(self) -> dict[str, Any] | None:
        return self.journal.get(self.KEY)

    def update(self, equity: float, now: float) -> str | None:
        """The breaker newly tripped by this equity observation (DAILY_LOSS / TOTAL_DD), else None."""
        e = _observed(equity)
        st = self.state() or {"peak": e, "day": None, "day_ref": e, "daily_tripped": False,
                              "total_tripped": False}
        prev = st.get("equity")
        day = ny_date(now)
        if st["day"] != day:
            st.update(day=day, day_ref=e, daily_tripped=False, daily_breach=False)
        st["peak"] = max(float(st["peak"]), e if prev is None else min(e, float(prev)))
        total = e <= st["peak"] * (1.0 - self.params.total_dd)
        daily = e <= st["day_ref"] * (1.0 - self.params.daily_loss)
        trip = None
        if total and st.get("total_breach") and not st["total_tripped"]:
            st["total_tripped"] = st["daily_tripped"] = True  # one flatten covers both
            trip = "TOTAL_DD"
        elif daily and st.get("daily_breach") and not st["daily_tripped"]:
            st["daily_tripped"] = True
            trip = "DAILY_LOSS"
        st.update(equity=e, total_breach=total, daily_breach=daily)
        self.journal.put(self.KEY, st)
        return trip

    def reset(self, equity: float, now: float) -> list[str]:
        """After the user resumes: only a breaker that tripped is re-based at this equity - the peak after
        TOTAL_DD, the day's reference after DAILY_LOSS (a TOTAL_DD trip marks the day too) - and its name is
        returned. A resume without a trip changes nothing."""
        e = _observed(equity)
        st = self.state()
        done: list[str] = []
        if not st:
            return done
        if st.get("total_tripped"):
            st.update(peak=e, total_tripped=False, total_breach=False)
            done.append("TOTAL_DD")
        if st.get("daily_tripped"):
            st.update(day=ny_date(now), day_ref=e, daily_tripped=False, daily_breach=False)
            done.append("DAILY_LOSS")
        if done:
            st["equity"] = e
            self.journal.put(self.KEY, st)
        return done


class SessionWatch:
    def __init__(self) -> None:
        self.state: str | None = None
        self.noticed: set[int] = set()

    def update(self, info: SessionInfo, now: float) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if self.state is not None and info.state != self.state:
            out.append(("SESSION", f"장 운영 상태 {self.state} → {info.state}"))
        self.state = info.state
        start, end = info.maintenance_start, info.maintenance_end
        if (start is not None and start - now <= MAINTENANCE_NOTICE_S and (end is None or end > now)
                and start not in self.noticed):
            self.noticed.add(start)
            when = "진행 중" if start <= now else "예정"
            until = "미정" if end is None else f"{datetime.fromtimestamp(end, NY):%H:%M}"
            out.append(("MAINTENANCE", f"정기 점검 {when} {datetime.fromtimestamp(start, NY):%Y-%m-%d %H:%M} ~ "
                                       f"{until} (뉴욕)"))
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
        self._rest_ts: float | None = None  # when the last good REST price was read
        self._tried: float | None = None  # the last REST read, good or not

    def futures_price(self) -> float | None:
        """The WebSocket price while a usable one arrived in the last `stale_s`; otherwise the REST quote's
        mid, read at most every `poll_s` (a failed read or a book without a finite bid and ask above 0 keeps
        the last good price) and returned only while it is at most `stale_s` old; else None."""
        now = self.clock()
        if self.feed is not None:
            tick = self.feed.latest(self.ex.futures_id)
            if tick is not None and now - tick.ts <= self.stale_s and usable_price(tick.price) is not None:
                self.mode = "ws"
                return float(tick.price)
        self.mode = "rest"
        if self._tried is None or not 0 <= now - self._tried < self.poll_s:
            self._tried = now
            try:
                q = self.ex.quote(self.ex.futures_id)
                if usable_price(q.bid) is not None and usable_price(q.ask) is not None:
                    self._rest, self._rest_ts = q.mid, now
            except ExchangeError:
                pass
        if self._rest_ts is None or now - self._rest_ts > self.stale_s:
            return None
        return self._rest
