"""Strategy-B 15-minute backtest loop (Plan 4 spec 4.4, 6.2). Costs (execution/costs.py), sizing
(risk/sizing.py) and the circuit breakers (risk/breakers.py) are strategy A's, imported unchanged.

Per 15m bar i, in this order:
  1. market orders queued at the previous close fill at this bar's open +/- slippage: a queued exit (TIME,
     BREAKER) first, then a queued entry - dropped when this bar is gap-filled or opens at/beyond its stop;
  2. intrabar, as A's PaperBroker: open at/beyond the stop -> stop at the open; stop touched -> stop at the
     stop (it beats any target in the same bar); else TP1 (half, limit, strict cross) then TP2 (the rest);
  3. TP1 filled and remainder == "breakeven" -> the rest's stop moves to the entry price, from the next bar;
  4. funding for the quarter hour (A's hourly funding / 4) on the position open at the close;
  5. time stop: open 48 h at this close -> exit queued for the next open;
  6. at each hour's close (the bar at hh:45): breakers.on_equity(close instant, equity); TOTAL_HALT -> exit
     queued, no more entries;
  7. entry: not on the last bar, ts >= start_ts, flat with nothing queued, breakers.can_enter, a signal on
     this bar -> size_contracts(equity, entry_ref, stop, 0.5% risk, 3x cap); TP1 takes half (1 contract:
     all of it);
  8. last bar: an open position is closed at the close (END, not a decision);
  9. at each hour's close with hour >= start_ts: EquityPoint(hour open ts, equity at the close) - hourly
     equity, so A's metrics (annualised with 8760 hours) apply unchanged."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

from ewbot.engine.loop import EquityPoint, Trade
from ewbot.execution.costs import fee, funding_per_hour, slip
from ewbot.risk.breakers import CircuitBreakers
from ewbot.risk.sizing import size_contracts
from ewbot.stratb.config import M15, BConfig
from ewbot.stratb.signals import Frame15, SignalB
from ewbot.types import H1

SETUP = "B"
_HOUR_LAST = H1 - M15  # ts % H1 of an hour's last 15m bar


@dataclass(frozen=True, slots=True)
class FillB:
    ts: int  # bar open time of the fill
    kind: str  # "ENTRY", "TP1", "TP2", "STOP", "EXIT" ("END" only as BResult.end_fill)
    contracts: int
    price: float
    fee: float
    trade_key: int  # entry ts of the position


@dataclass(frozen=True, slots=True)
class StopMoveB:
    ts: int  # bar whose close moved the stop (effective from the next bar)
    trade_key: int
    old: float
    new: float
    kind: str = "BREAKEVEN"


@dataclass(frozen=True, slots=True)
class OrderB:
    ts: int  # signal bar
    direction: int
    contracts: int
    tp1_contracts: int


@dataclass(frozen=True)
class BResult:
    trades: tuple[Trade, ...]
    equity: tuple[EquityPoint, ...]
    signals: tuple[SignalB, ...]  # the signals that queued an entry, with their orders below
    orders: tuple[OrderB, ...]
    fills: tuple[FillB, ...]
    stops: tuple[StopMoveB, ...]
    end_fill: FillB | None
    events: tuple[tuple[int, str], ...]


@dataclass
class _Pos:
    sig: SignalB
    direction: int
    total: int
    contracts: int
    entry_ts: int
    entry_price: float
    stop: float
    tp1: float | None
    tp2: float
    tp1_n: int
    fees: float
    gross: float = 0.0
    funding: float = 0.0
    exit_qty: int = 0
    exit_value: float = 0.0
    snapshot: dict = field(default_factory=dict)


def tp1_contracts(n: int, frac: float) -> int:
    """Contracts closed at TP1: n if n <= 1, else max(1, floor(n x frac))."""
    return n if n <= 1 else min(n, max(1, math.floor(n * frac + 1e-9)))


def run_engine(f: Frame15, signals: Iterable[SignalB], cfg: BConfig, start_ts: int | None = None) -> BResult:
    """One run over the whole frame (cut it at the end before building it). Entries only from start_ts."""
    c, cs = cfg.costs, cfg.costs.contract_size
    by_i: dict[int, SignalB] = {}
    for s in signals:
        by_i.setdefault(s.i, s)
    ts, op, hi, lo, cl = (a.tolist() for a in (f.ts, f.open, f.high, f.low, f.close))
    syn = f.synthetic.tolist()
    time_stop = cfg.params.time_stop_hours * H1
    breakers = CircuitBreakers(cfg.breakers, cfg.initial_equity)
    cash = float(cfg.initial_equity)
    pos: _Pos | None = None
    pending: tuple[SignalB, int, int] | None = None
    exit_req: str | None = None
    trades: list[Trade] = []
    equity: list[EquityPoint] = []
    sigs: list[SignalB] = []
    orders: list[OrderB] = []
    fills: list[FillB] = []
    stops: list[StopMoveB] = []
    events: list[tuple[int, str]] = []
    end_fill: FillB | None = None

    def close(t: int, kind: str, price: float, qty: int, reason: str | None) -> FillB:
        nonlocal cash, pos
        assert pos is not None
        fe = fee(price, qty, c)
        realized = pos.direction * (price - pos.entry_price) * qty * cs
        cash += realized - fe
        pos.fees += fe
        pos.gross += realized
        pos.contracts -= qty
        pos.exit_qty += qty
        pos.exit_value += price * qty
        fill = FillB(t, kind, qty, price, fe, pos.entry_ts)
        if pos.contracts == 0:
            trade = _trade(pos, t, reason or kind, cs)
            trades.append(trade)
            for ev in breakers.on_trade_closed(t, trade.pnl):
                events.append((t, ev))
            pos = None
        return fill

    def mark(price: float) -> float:
        if pos is None:
            return cash
        return cash + pos.direction * (price - pos.entry_price) * pos.contracts * cs

    last = len(ts) - 1
    for i in range(len(ts)):
        t = ts[i]
        if exit_req is not None and pos is not None:  # 1. queued market orders
            fills.append(close(t, "EXIT", slip(op[i], -pos.direction, c), pos.contracts, exit_req))
        exit_req = None
        if pending is not None:
            sig, n, n1 = pending
            pending = None
            d = sig.direction
            if syn[i]:
                events.append((t, "ENTRY_SKIPPED:gap"))
            elif d * (op[i] - sig.stop) <= 0.0:
                events.append((t, "ENTRY_SKIPPED:open_beyond_stop"))
            else:
                px = slip(op[i], d, c)
                fe = fee(px, n, c)
                cash -= fe
                pos = _Pos(sig, d, n, n, t, px, sig.stop, sig.tp1, sig.tp2, n1, fe, snapshot=_snapshot(sig))
                fills.append(FillB(t, "ENTRY", n, px, fe, t))
        if pos is not None:  # 2. intrabar
            d = pos.direction
            tp1_hit = False
            if d * (op[i] - pos.stop) <= 0.0:
                fills.append(close(t, "STOP", slip(op[i], -d, c), pos.contracts, None))
            elif (lo[i] <= pos.stop) if d > 0 else (hi[i] >= pos.stop):
                fills.append(close(t, "STOP", slip(pos.stop, -d, c), pos.contracts, None))
            else:
                fav = hi[i] if d > 0 else lo[i]
                if pos.tp1 is not None and d * (fav - pos.tp1) > 0.0:
                    px, pos.tp1, tp1_hit = pos.tp1, None, True
                    fills.append(close(t, "TP1", px, min(pos.tp1_n, pos.contracts), None))
                if pos is not None and pos.tp1 is None and d * (fav - pos.tp2) > 0.0:
                    fills.append(close(t, "TP2", pos.tp2, pos.contracts, None))
            if pos is not None and tp1_hit and cfg.params.remainder == "breakeven" and pos.stop != pos.entry_price:
                stops.append(StopMoveB(t, pos.entry_ts, pos.stop, pos.entry_price))  # 3.
                pos.stop = pos.entry_price
        if pos is not None:  # 4. funding for the quarter hour
            cost = funding_per_hour(cl[i], pos.contracts, pos.direction, c) * (M15 / H1)
            cash -= cost
            pos.funding += cost
            if t + M15 - pos.entry_ts >= time_stop:  # 5.
                exit_req = "TIME"
        hour_close = t % H1 == _HOUR_LAST
        if hour_close:  # 6.
            for ev in breakers.on_equity(t + M15, mark(cl[i])):
                events.append((t, ev))
            if breakers.halted and pos is not None:
                exit_req = "BREAKER"
        if i < last and pos is None and pending is None and (start_ts is None or t >= start_ts):  # 7.
            sig = by_i.get(i)
            if sig is not None:
                if not breakers.can_enter(t + M15):
                    events.append((t, "SIGNAL_REJECTED:breaker"))
                else:
                    n = size_contracts(cash, sig.entry_ref, sig.stop, sig.direction, cfg.risk, 1.0, c)
                    if n == 0:
                        events.append((t, "SIGNAL_REJECTED:size"))
                    else:
                        n1 = tp1_contracts(n, cfg.tp1_frac)
                        pending = (sig, n, n1)
                        sigs.append(sig)
                        orders.append(OrderB(t, sig.direction, n, n1))
        if i == last:  # 8.
            pending = None
            if pos is not None:
                key = pos.entry_ts
                f_end = close(t, "EXIT", slip(cl[i], -pos.direction, c), pos.contracts, "END")
                end_fill = FillB(f_end.ts, "END", f_end.contracts, f_end.price, f_end.fee, key)
        if hour_close and (start_ts is None or t - _HOUR_LAST >= start_ts):  # 9.
            equity.append(EquityPoint(t - _HOUR_LAST, mark(cl[i])))
    return BResult(tuple(trades), tuple(equity), tuple(sigs), tuple(orders), tuple(fills), tuple(stops), end_fill,
                   tuple(events))


def _snapshot(sig: SignalB) -> dict:
    return {
        "signal_ts": sig.ts, "entry_ref": sig.entry_ref, "leg": [sig.leg_start, sig.leg_end],
        "pivots": [sig.p1_ts, sig.p2_ts], "level": sig.level, "reason": sig.reason,
    }


def _trade(pos: _Pos, exit_ts: int, reason: str, cs: float) -> Trade:
    pnl = pos.gross - pos.fees - pos.funding
    risk_usd = abs(pos.entry_price - pos.sig.stop) * pos.total * cs
    return Trade(
        entry_ts=pos.entry_ts, exit_ts=exit_ts, setup=SETUP, direction=pos.direction, contracts=pos.total,
        entry_price=pos.entry_price, exit_price_avg=pos.exit_value / pos.exit_qty, stop0=pos.sig.stop,
        tp1=pos.sig.tp1, tp2=pos.sig.tp2, pnl=pnl, fees=pos.fees, funding=pos.funding,
        r_multiple=pnl / risk_usd if risk_usd > 0.0 else 0.0, exit_reason=reason, snapshot=pos.snapshot,
    )
