"""Read-only instrumentation of one TradingEngine run (Plan 2 spec 5.3 decisions).

EngineResult has trades, equity and signals but not the individual fills (TP1 partials), the stop
moves (breakeven, structural trailing) or the order sizes - all of which the paper ledger records as
decisions. `traced_engine()` swaps the PaperBroker and WaveEngine names that ewbot.engine.loop looks
up at run time for subclasses that call the original methods unchanged and only log what passed
through them. No fingerprinted file is edited and the run's results are bit-identical (tested).
The swap is process-global for the duration of the `with` block: run nothing else concurrently."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field

import ewbot.engine.loop as loop_mod
from ewbot.execution.paper import Fill, PaperBroker
from ewbot.types import Bar
from ewbot.waves.engine import WaveEngine, WaveState


@dataclass(frozen=True, slots=True)
class OrderRec:
    ts: int  # bar whose close queued the entry (= Signal.ts)
    direction: int
    contracts: int
    stop: float
    tp1: float
    tp2: float | None


@dataclass(frozen=True, slots=True)
class FillRec:
    ts: int  # bar open time of the fill
    kind: str  # "ENTRY","STOP","TP1","TP2","EXIT" (and "END" for the end-of-data close)
    contracts: int
    price: float
    trade_key: int  # entry_ts of the position the fill belongs to


@dataclass(frozen=True, slots=True)
class StopRec:
    ts: int  # bar whose processing moved the stop (effective from the next bar)
    trade_key: int
    old: float
    new: float
    kind: str  # "BREAKEVEN" (stop moved to the entry price after TP1) | "TRAIL"


@dataclass
class EngineTrace:
    orders: list[OrderRec] = field(default_factory=list)
    fills: list[FillRec] = field(default_factory=list)  # every process_bar fill, in order (END excluded)
    stops: list[StopRec] = field(default_factory=list)
    end_fill: FillRec | None = None  # the END close of a position still open at the last bar
    last_state: WaveState | None = None  # WaveState after the last bar
    last_ts: int | None = None  # last bar processed by the broker
    brokers: int = 0  # PaperBroker instances created inside the block (1 per engine run)


def _classes(trace: EngineTrace) -> tuple[type[PaperBroker], type[WaveEngine]]:
    class TracingBroker(PaperBroker):
        def __init__(self, costs, equity) -> None:  # type: ignore[no-untyped-def]
            super().__init__(costs, equity)
            trace.brokers += 1

        def process_bar(self, bar: Bar) -> list[Fill]:
            key = self.position.entry_ts if self.position is not None else None
            fills = super().process_bar(bar)
            trace.last_ts = bar.ts
            for f in fills:
                if f.kind == "ENTRY":
                    key = f.ts
                assert key is not None, f"{f.kind} fill without a position"
                trace.fills.append(FillRec(f.ts, f.kind, f.contracts, f.price, int(key)))
            return fills

        def queue_entry(self, direction, contracts, stop, tp1, tp2, tp1_contracts) -> None:  # type: ignore[no-untyped-def]
            super().queue_entry(direction, contracts, stop, tp1, tp2, tp1_contracts)
            assert trace.last_ts is not None
            trace.orders.append(OrderRec(trace.last_ts, direction, contracts, stop, tp1, tp2))

        def set_stop(self, price: float) -> None:
            pos = self.position
            if pos is not None and price != pos.stop:
                assert trace.last_ts is not None
                kind = "BREAKEVEN" if price == pos.entry_price else "TRAIL"
                trace.stops.append(StopRec(trace.last_ts, pos.entry_ts, pos.stop, price, kind))
            super().set_stop(price)

        def close_now(self, ts: int, price: float) -> Fill | None:
            pos = self.position
            f = super().close_now(ts, price)
            if f is not None and pos is not None:
                trace.end_fill = FillRec(f.ts, "END", f.contracts, f.price, pos.entry_ts)
            return f

    class TracingWave(WaveEngine):
        def on_1h_close(self, bar1h: Bar, bar4h: Bar | None, sub_bars: Sequence[Bar] | None) -> WaveState:
            ws = super().on_1h_close(bar1h, bar4h, sub_bars)
            trace.last_state = ws
            return ws

    return TracingBroker, TracingWave


@contextmanager
def traced_engine() -> Iterator[EngineTrace]:
    """Every TradingEngine.run() inside the block records into the yielded EngineTrace (use one
    run per block). The original classes are restored on exit, also after an exception."""
    trace = EngineTrace()
    broker_cls, wave_cls = _classes(trace)
    orig_broker, orig_wave = loop_mod.PaperBroker, loop_mod.WaveEngine
    loop_mod.PaperBroker, loop_mod.WaveEngine = broker_cls, wave_cls  # type: ignore[misc]
    try:
        yield trace
    finally:
        loop_mod.PaperBroker, loop_mod.WaveEngine = orig_broker, orig_wave  # type: ignore[misc]
