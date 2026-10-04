"""A LiveRuntime on the synthetic candles of tests/paper/helpers with a SimExchange whose futures and spot
prices follow those candles (basis ratio `ratio`). Used by the runtime, parity and chaos tests.

`walk(start, end)` replays the hours [start, end): at hh:00:30 the decision cycle runs with the price at
the bar's open (the engine fills market orders at the next open), then the bar's path is played in
four steps - open, the adverse extreme first (stop before TP, as the engine assumes), the favourable
extreme, the close - with a runtime tick after each step (poll -> observe -> reconcile)."""

from __future__ import annotations

import queue
from pathlib import Path

import pandas as pd

from ewbot.live.control import Command
from ewbot.live.journal import Journal
from ewbot.live.runtime import LiveDeps, LiveRuntime
from ewbot.live.sim import SimExchange
from ewbot.types import H1
from tests.live.helpers import Alerts, Clock
from tests.paper.helpers import FakeFetch, make_lock, synth_frame


class Harness:
    def __init__(self, tmp_path: Path, start: int, *, stage: str | None = "CANARY", stage_days: float = 60.0,
                 equity: float = 10_000.0, ratio: float = 1.0, df: pd.DataFrame | None = None,
                 exchange: SimExchange | None = None, journal_name: str = "journal.sqlite", **sim_kw) -> None:
        self.tmp = tmp_path
        self.df = synth_frame() if df is None else df
        self.lock = make_lock(self.df)  # a temp holdout lock (PARAMS) for these candles
        self.ratio = ratio
        self.clock = Clock(start)
        self.ex = exchange if exchange is not None else SimExchange(clock=self.clock, equity=equity, **sim_kw)
        self.journal = Journal(tmp_path / journal_name)
        if stage is not None and self.journal.stage()[1] is None:  # None: a fresh journal without a stage
            self.journal.set_stage(stage, "test", start - stage_days * 86_400)
        self.alerts = Alerts()
        self.commands: queue.Queue[Command] = queue.Queue()
        self.fetch = FakeFetch(self.df, self.clock)
        self.by_ts = self.df.set_index("ts")
        self.rt = self.make_runtime()

    def make_runtime(self, **kw) -> LiveRuntime:
        """A runtime on this harness; the test clock drives the wall clock and the loop's cadence (`monotonic`,
        replaceable through `kw` like every other dependency)."""
        deps = {"journal": self.journal, "exchange": self.ex, "lock": self.lock, "fetch": self.fetch,
                "cache_dir": self.tmp / "cache", "notify": self.alerts, "clock": self.clock, "sleep": self.clock.sleep,
                "monotonic": self.clock, "commands": self.commands, **kw}
        return LiveRuntime(LiveDeps(**deps))

    def price(self, spot: float) -> None:
        self.ex.set_prices(spot * self.ratio, spot)

    def bar(self, ts: int) -> pd.Series:
        return self.by_ts.loc[ts]

    def at(self, now: float) -> None:
        self.clock.now = float(now)
        self.rt.tick()

    def hour(self, h: int) -> None:
        """Bar h (open time) runs: cycle for bar h - 1 at h + 30 s, then the bar's path."""
        b = self.bar(h)
        self.price(float(b["open"]))
        self.at(h + 30)
        pos = self.ex.position()
        down_first = pos is None or pos.side > 0
        path = [b["low"], b["high"]] if down_first else [b["high"], b["low"]]
        for k, p in enumerate(path + [b["close"]]):
            self.price(float(p))
            self.at(h + 600 * (k + 1) + 30)

    def walk(self, start: int, end: int) -> None:
        for h in range(int(start), int(end), H1):
            self.hour(h)
