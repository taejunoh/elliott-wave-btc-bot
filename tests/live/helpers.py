"""Shared builders for tests/live: a settable clock, a recording alerter, engine targets and a
SimExchange with prices already set. Synthetic only - no network, no real account."""

from __future__ import annotations

from ewbot.live.exchange import PlaceResult
from ewbot.live.journal import Journal
from ewbot.live.reconcile import PlanContext
from ewbot.live.sim import SimExchange
from ewbot.live.target import Target, TargetEntry, TargetPosition

T0 = 1_790_942_400  # 2026-10-02 11:00 UTC (a bar open)


class Clock:
    def __init__(self, now: float = T0 + 3630.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += float(s)


class Alerts:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []

    def __call__(self, kind: str, text: str) -> None:
        self.items.append((kind, text))

    def kinds(self) -> list[str]:
        return [k for k, _ in self.items]


def sim(clock: Clock, fut: float = 60_000.0, spot: float = 60_000.0, **kw) -> SimExchange:
    ex = SimExchange(clock=clock, **kw)
    ex.set_prices(fut, spot)
    return ex


def journal(tmp_path, name: str = "journal.sqlite") -> Journal:
    return Journal(tmp_path / name)


def entry(signal_ts: int = T0, direction: int = 1, stop: float = 59_000.0, tp1: float = 61_000.0,
          tp2: float | None = 62_000.0, setup: str = "W3") -> TargetEntry:
    return TargetEntry(signal_ts, setup, direction, 60_000.0, stop, tp1, tp2, 1.0, 5, "test signal")


def position(signal_ts: int = T0, direction: int = 1, stop: float = 59_000.0, tp1_done: bool = False,
             tp1: float = 61_000.0, tp2: float | None = 62_000.0) -> TargetPosition:
    return TargetPosition(signal_ts + 3600, signal_ts, "W3", direction, 60_000.0, stop, 59_000.0, tp1, tp2,
                          tp1_done, 5, 5)


def target(bar_ts: int, pos: TargetPosition | None = None, ent: TargetEntry | None = None,
           exit_reason: str | None = None) -> Target:
    return Target(bar_ts, 60_000.0, pos, exit_reason, ent, ())


def ctx(clock: Clock, ratio: float | None = 1.0, contracts: int = 1, block: str | None = None,
        session_open: bool = True, max_contracts: int | None = None) -> PlanContext:
    return PlanContext(now=clock(), ratio=ratio, tick=5.0, session_open=session_open, entry_contracts=contracts,
                       entry_block=block, max_contracts=max_contracts)


class Lag:
    """On a harness's exchange: the fill is on the order, and /cfm/positions does not show it for `reads` more reads (set
    `left` to 0 to end it). With `refuse`, Coinbase refuses a bracket for want of the position it does not see yet, its own
    answer on a flat account (live/shadow.py, 2026-10-05)."""

    def __init__(self, hs, reads: int, refuse: bool = False) -> None:
        self.left, self.armed = reads, False
        real_pos, real_market, real_bracket = hs.ex.position, hs.ex.place_market, hs.ex.place_bracket

        def position():
            if self.armed and self.left > 0:
                self.left -= 1
                return None
            return real_pos()

        def market(cid, side, size):
            out = real_market(cid, side, size)
            self.armed = True
            return out

        def bracket(cid, side, size, limit, stop):
            if refuse and self.left > 0:
                return PlaceResult(cid, None, False, "BRACKET_ORDER_SIZE_EXCEEDS_POSITION")
            return real_bracket(cid, side, size, limit, stop)

        hs.ex.position, hs.ex.place_market, hs.ex.place_bracket = position, market, bracket


# close_position's answer on the first real test trade, 1.4 s after the fill (2026-11-06, live-v6): /cfm/positions already
# showed the position, and 2 s later, with the same bracket resting, the close was refused as a close conflict instead
NOT_FOUND = ('HTTP 404: {"error":"NOT_FOUND","error_details":"no positions found for product BIP-20DEC30-CDE",'
             '"message":"no positions found for product BIP-20DEC30-CDE"}')


def as_on_2026_11_06(hs, not_found: int = 1) -> None:
    """The venue as the first real test trade met it: close_position answers 404 `not_found` times, and refuses a close
    while our bracket rests (PREVIEW_ORDER_SIZE_EXCEEDS_BRACKETED_POSITION) until the bracket is cancelled."""
    from ewbot.live.exchange import ExchangeError

    hs.ex.close_conflict = True
    if not_found:
        hs.ex.fail_next("close_position", ExchangeError(NOT_FOUND), times=not_found)
