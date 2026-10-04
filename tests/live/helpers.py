"""Shared builders for tests/live: a settable clock, a recording alerter, engine targets and a
SimExchange with prices already set. Synthetic only - no network, no real account."""

from __future__ import annotations

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
