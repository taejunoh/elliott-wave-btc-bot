"""TradingEngine: the single event loop shared by backtest and (later) paper/live modes (spec 3, 5.3, 5.4, 6.3).

Before the loop (spec 4.1): fill_gaps, then clip 1h wicks beyond wick_clip_atr1h x ATR1h (ATR of the
previous bar). The CLIPPED bars feed the 4h aggregation, the WaveEngine, the CHoCH detector and the
strategy; the PaperBroker, equity marks and the END close use the RAW bars (real traded prices).
The number of clipped bars is logged once as (first bar ts, "WICKS_CLIPPED:<n>").

Per closed 1h bar:
  1. broker.process_bar(bar)                      fills of orders queued at the previous close
  2. handle fills                                 ENTRY -> open trade; TP1 -> stop to breakeven;
                                                  full exit -> Trade, breakers.on_trade_closed, cooldown on STOP
  3. wave.on_1h_close(cbar, bar4h, sub_bars)      bar4h built here from UTC 4h buckets of clipped bars
  4. choch.update(cbar)
  5. position management                          trailing on new 4h pivot, time stop, lineage kill
                                                  (only while the lineage's S is in the last-M pivots)
  6. breakers.on_equity(close instant, equity)    TOTAL_HALT -> flatten at next open, no more entries
  7. cooldown release; entry / flip evaluation    (bar.ts >= start_ts, no pending entry, can_enter,
                                                   no blackout on this bar or the fill bar, not the last bar)
  8. last bar: close any open position at bar.close (reason END)
  9. EquityPoint(bar.ts, equity marked at bar.close) for bar.ts >= start_ts
"""
from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

from ewbot.config import BotConfig
from ewbot.data.candles import clip_wicks, entry_blackout, fill_gaps, to_bars
from ewbot.engine.position_mgr import (
    EngineResult,
    EquityPoint,
    Trade,
    _make_snapshot,
    _OpenTrade,
    _PendingEntry,
    _tighten,
    _tp1_split,
    _trail_candidate,
)
from ewbot.execution.paper import Fill, PaperBroker
from ewbot.indicators import atr_series
from ewbot.risk.breakers import CircuitBreakers
from ewbot.risk.sizing import size_contracts
from ewbot.strategy.base import Signal, Strategy
from ewbot.strategy.choch import ChochDetector
from ewbot.types import H1, H4, Bar
from ewbot.waves.engine import WaveEngine, WaveState
from ewbot.waves.zigzag import Pivot

__all__ = ["EngineResult", "EquityPoint", "Trade", "TradingEngine"]

_CLOSING_KIND_REASON = {"STOP": "STOP", "TP1": "TP1", "TP2": "TP2"}


def _in_window(lineage: Sequence, pivots4h: Sequence[Pivot], m: int) -> bool:
    """True while the lineage's S pivot (lineage[2] = S.i) is inside the last-M-pivot enumeration
    window. Outside it the enumerator cannot see the lineage at all, so its absence from
    ws.lineages is not a hard-rule break (spec 5.4) and must not trigger a LINEAGE exit."""
    return len(pivots4h) < m or lineage[2] >= pivots4h[-m].i


def _agg_4h(sub: Sequence[Bar]) -> Bar:
    return Bar(
        ts=sub[0].ts,
        open=sub[0].open,
        high=max(b.high for b in sub),
        low=min(b.low for b in sub),
        close=sub[-1].close,
        volume=sum(b.volume for b in sub),
        synthetic=any(b.synthetic for b in sub),
    )


class TradingEngine:
    def __init__(
        self,
        cfg: BotConfig,
        strategy: Strategy,
        *,
        wave_engine: WaveEngine | None = None,
        choch: ChochDetector | None = None,
    ) -> None:
        """wave_engine / choch: optional injected instances (tests). When None, fresh real ones are
        built for every run(). Injected instances are stateful: use a new TradingEngine per run."""
        self.cfg = cfg
        self.strategy = strategy
        self._wave_in = wave_engine
        self._choch_in = choch

    def run(self, df1h: pd.DataFrame, start_ts: int | None = None, end_ts: int | None = None) -> EngineResult:
        df = fill_gaps(df1h)  # idempotent (T3); guarantees a continuous hourly series for bucketing
        if end_ts is not None:
            df = df[df["ts"] < end_ts]
        df = df.reset_index(drop=True)
        if df.empty:
            return EngineResult((), (), (), ())
        atr1h = atr_series(
            df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), self.cfg.wave.atr_len
        )
        clipped, n_clipped = clip_wicks(df, atr1h, self.cfg.wave.wick_clip_atr1h)  # spec 4.1
        wave = self._wave_in if self._wave_in is not None else WaveEngine(self.cfg.wave)
        choch = (
            self._choch_in
            if self._choch_in is not None
            else ChochDetector(self.cfg.strategy.choch_bars)
        )
        run = _Run(self.cfg, self.strategy, wave, choch, start_ts)
        return run.execute(to_bars(df), to_bars(clipped), n_clipped)


class _Run:
    """State of one engine run (kept out of TradingEngine so run() has no hidden carry-over)."""

    def __init__(
        self,
        cfg: BotConfig,
        strategy: Strategy,
        wave: WaveEngine,
        choch: ChochDetector,
        start_ts: int | None,
    ) -> None:
        self.cfg = cfg
        self.strategy = strategy
        self.wave = wave
        self.choch = choch
        self.start_ts = start_ts
        self.broker = PaperBroker(cfg.costs, cfg.initial_equity)
        self.breakers = CircuitBreakers(cfg.breakers, cfg.initial_equity)
        self.trades: list[Trade] = []
        self.equity: list[EquityPoint] = []
        self.signals: list[Signal] = []
        self.events: list[tuple[int, str]] = []
        self.open: _OpenTrade | None = None
        self.pending: _PendingEntry | None = None
        self.cooldown: set[tuple] = set()

    # ------------------------------------------------------------------ main loop
    def execute(self, bars: list[Bar], clipped: list[Bar], n_clipped: int) -> EngineResult:
        """bars: raw 1h bars (broker, marks, END close); clipped: the same bars after clip_wicks
        (4h aggregation, wave engine, CHoCH, strategy). Same length and ts."""
        self.events.append((bars[0].ts, f"WICKS_CLIPPED:{n_clipped}"))
        bucket: list[Bar] = []
        last = len(bars) - 1
        for idx, (bar, cbar) in enumerate(zip(bars, clipped)):
            if bucket and bucket[0].ts // H4 != cbar.ts // H4:
                bucket = []
            bucket.append(cbar)
            bar4h: Bar | None = None
            sub: tuple[Bar, ...] | None = None
            if cbar.ts % H4 == 3 * H1:
                if len(bucket) == 4:
                    sub = tuple(bucket)
                    bar4h = _agg_4h(sub)
                bucket = []

            for f in self.broker.process_bar(bar):  # 1-2 (raw prices)
                self._on_fill(f)
            ws = self.wave.on_1h_close(cbar, bar4h, sub)  # 3
            self.choch.update(cbar)  # 4
            self._manage(ws, closed_4h=bar4h is not None)  # 5

            t_close = bar.ts + H1  # 6
            for ev in self.breakers.on_equity(t_close, self.broker.equity(bar.close)):
                self.events.append((bar.ts, ev))
            if self.breakers.halted:
                self._request_exit("BREAKER")

            self._release_cooldown(ws)  # 7
            if idx < last and self._may_enter(bar, t_close):
                sig = self.strategy.on_bar(cbar, ws, self.choch)
                if sig is not None:
                    self._consider(sig, bar, ws)

            if idx == last:  # 8
                self._close_end(bar)
            if self.start_ts is None or bar.ts >= self.start_ts:  # 9
                self.equity.append(EquityPoint(bar.ts, self.broker.equity(bar.close)))

        return EngineResult(tuple(self.trades), tuple(self.equity), tuple(self.signals), tuple(self.events))

    # ------------------------------------------------------------------ fills
    def _on_fill(self, f: Fill) -> None:
        if f.kind == "ENTRY":
            pe = self.pending
            assert pe is not None, "ENTRY fill without a pending signal"
            self.pending = None
            self.open = _OpenTrade(
                sig=pe.sig,
                snapshot=pe.snapshot,
                contract_size=self.cfg.costs.contract_size,
                entry_ts=f.ts,
                entry_price=f.price,
                contracts=f.contracts,
                pos=self.broker.position,  # None if it was already closed inside the entry bar
                fees=f.fee,
            )
            return
        ot = self.open
        assert ot is not None, f"{f.kind} fill without an open trade"
        if ot.add_exit_fill(f):
            reason = _CLOSING_KIND_REASON.get(f.kind) or ot.exit_reason or "END"
            self._finish(ot, f.ts, reason)
        elif f.kind == "TP1":
            pos = self.broker.position
            if pos is not None:
                self.broker.set_stop(_tighten(ot.direction, pos.stop, ot.entry_price))

    def _finish(self, ot: _OpenTrade, exit_ts: int, reason: str) -> None:
        trade = ot.to_trade(exit_ts, reason)
        self.trades.append(trade)
        self.open = None
        for ev in self.breakers.on_trade_closed(exit_ts, trade.pnl):
            self.events.append((exit_ts, ev))
        if reason == "STOP" and ot.sig.instance is not None:
            self.cooldown.add(tuple(ot.sig.instance))

    # ------------------------------------------------------------------ management
    def _manage(self, ws: WaveState, closed_4h: bool) -> None:
        ot = self.open
        pos = self.broker.position
        if ot is None or pos is None:
            return
        d = ot.direction
        cand = _trail_candidate(d, ws.new_pivot_4h, ws.atr4h, self.cfg.strategy.buf_struct_atr)
        if cand is not None:
            new_stop = _tighten(d, pos.stop, cand)
            if new_stop != pos.stop:
                self.broker.set_stop(new_stop)
        if closed_4h and not ot.tp1_done:
            ot.n4h += 1
            if ot.sig.time_stop_4h is not None and ot.n4h >= ot.sig.time_stop_4h:
                self._request_exit("TIME")
        lineage = ot.sig.lineage
        if (
            lineage is not None
            and ws.ready
            and tuple(lineage) not in ws.lineages
            and _in_window(lineage, ws.pivots4h, self.cfg.wave.M)
        ):
            self._request_exit("LINEAGE")

    def _request_exit(self, reason: str) -> None:
        ot = self.open
        if ot is None or self.broker.position is None or ot.exit_reason is not None:
            return
        ot.exit_reason = reason
        self.broker.queue_exit()

    def _release_cooldown(self, ws: WaveState) -> None:
        top = ws.top
        if top is None or not self.cooldown:
            return
        lineage = tuple(top.lineage)
        self.cooldown = {i for i in self.cooldown if not (tuple(i[:3]) == lineage and i[3] != top.phase)}

    # ------------------------------------------------------------------ entries
    def _may_enter(self, bar: Bar, t_close: int) -> bool:
        if self.start_ts is not None and bar.ts < self.start_ts:
            return False
        if self.pending is not None:
            return False
        if not self.breakers.can_enter(t_close):
            return False
        return not (entry_blackout(bar.ts) or entry_blackout(bar.ts + H1))

    def _consider(self, sig: Signal, bar: Bar, ws: WaveState) -> None:
        held = 0
        if self.open is not None and self.broker.position is not None and self.open.exit_reason is None:
            held = self.open.direction
        if sig.direction == held:
            self.events.append((bar.ts, "SIGNAL_IGNORED:same_direction"))
            return
        if sig.instance is not None and tuple(sig.instance) in self.cooldown:
            self.events.append((bar.ts, "SIGNAL_REJECTED:cooldown"))
            return
        n = size_contracts(
            self.broker.equity(bar.close),
            sig.entry_ref,
            sig.stop,
            sig.direction,
            self.cfg.risk,
            sig.risk_mult,
            self.cfg.costs,
        )
        if n == 0:
            self.events.append((bar.ts, "SIGNAL_REJECTED:size"))
            return
        if held == -sig.direction:
            self._request_exit("FLIP")
        tp1_n = _tp1_split(n, self.cfg.strategy.tp1_frac)
        self.broker.queue_entry(sig.direction, n, sig.stop, sig.tp1, sig.tp2, tp1_n)
        self.pending = _PendingEntry(sig, _make_snapshot(ws, sig, self.cfg.wave.top_k))
        self.signals.append(sig)

    # ------------------------------------------------------------------ end of data
    def _close_end(self, bar: Bar) -> None:
        self.pending = None
        ot = self.open
        if ot is None or self.broker.position is None:
            return
        ot.exit_reason = "END"
        f = self.broker.close_now(bar.ts, bar.close)
        if f is not None:
            self._on_fill(f)
