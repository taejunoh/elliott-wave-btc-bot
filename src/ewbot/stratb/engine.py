"""Strategy-B 15-minute portfolio backtest (Plan 4 spec 4.4, 6.2; BTC and ETH since the 2026-10-07
revision). Costs (execution/costs.py, with each asset's contract size: config.asset_costs), sizing
(risk/sizing.py) and the circuit breakers (risk/breakers.py) are strategy A's, imported unchanged.

One account - one cash balance, one set of breakers - and at most one position per asset. The timeline is
every 15m bar of the frames (build_frames makes them end on the same bar; an asset whose bars start later
is skipped before its first bar). Per timeline bar, each step visits the assets in config order (BTC, then
ETH):
  1. market orders queued at the previous close fill at this bar's open +/- slippage: first every queued
     exit (TIME, BREAKER), then every queued entry - dropped when the asset's bar is gap-filled or opens
     at/beyond the entry's stop;
  2. intrabar, as A's PaperBroker: open at/beyond the stop -> stop at the open; stop touched -> stop at the
     stop (it beats any target in the same bar); else TP1 (half, limit, strict cross) then TP2 (the rest).
     Every closed trade goes to the breakers, so consecutive losses count across the assets;
  3. TP1 filled and remainder == "breakeven" -> the rest's stop moves to the entry price, from the next bar;
  4. funding for the quarter hour (A's hourly funding / 4) on each position open at the close;
  5. time stop: a position open 48 h at this close -> its exit is queued for the next open;
  6. at each hour's close (the bar at hh:45): breakers.on_equity(close instant, account equity = cash plus
     every open position marked at its close); TOTAL_HALT -> every open position's exit queued, no more
     entries;
  7. entries (not on the last bar, ts >= start_ts, breakers.can_enter): an asset with a signal on this bar
     and neither a position nor a queued entry is sized with size_contracts(account equity, entry_ref,
     stop, 0.5% risk, 3x cap, its contract size), then cut so that the notional of the open positions (at
     their closes) and queued entries (at their entry_ref) stays within max_total_leverage (3x) of the
     equity; TP1 takes half (1 contract: all of it). A signal on an asset that holds a position or has an
     entry queued is dropped;
  8. last bar: every open position is closed at its close (END, not a decision);
  9. at each hour's close from start_ts's hour boundary on (every close at or after floor(start_ts, 1 h)):
     EquityPoint(hour open ts, account equity at the close) - hourly equity, so A's metrics (annualised with
     8760 hours) apply unchanged. With a start time the first point is the hour before it, the account just
     before the first entry can fill, so compute_metrics(start_ts=...) measures the first hour (an entry's
     costs at hh:15 included) from the starting value; with none, or no bar before it, the curve starts at the
     first hour."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from ewbot.engine.loop import EquityPoint, Trade
from ewbot.execution.costs import fee, funding_per_hour, slip
from ewbot.risk.breakers import CircuitBreakers
from ewbot.risk.sizing import size_contracts
from ewbot.stratb.config import M15, BConfig, asset_costs
from ewbot.stratb.signals import Frame15, SignalB
from ewbot.types import H1

SETUP = "B"
_HOUR_LAST = H1 - M15  # ts % H1 of an hour's last 15m bar
_EPS = 1e-9


def setup_of(asset: str) -> str:
    """Trade.setup of a strategy-B trade, "B-BTC" or "B-ETH" (A's message formats print it)."""
    return f"{SETUP}-{asset}"


def asset_of(t: Trade) -> str:
    return str(t.snapshot["asset"])


@dataclass(frozen=True, slots=True)
class FillB:
    ts: int  # bar open time of the fill
    asset: str
    kind: str  # "ENTRY", "TP1", "TP2", "STOP", "EXIT" ("END" only in BResult.end_fills)
    contracts: int
    price: float
    fee: float
    trade_key: int  # entry ts of the position (one position per asset at a time)


@dataclass(frozen=True, slots=True)
class StopMoveB:
    ts: int  # bar whose close moved the stop (effective from the next bar)
    asset: str
    trade_key: int
    old: float
    new: float
    kind: str = "BREAKEVEN"


@dataclass(frozen=True, slots=True)
class OrderB:
    ts: int  # signal bar
    asset: str
    direction: int
    contracts: int
    tp1_contracts: int


@dataclass(frozen=True)
class BResult:
    trades: tuple[Trade, ...]
    equity: tuple[EquityPoint, ...]
    signals: tuple[SignalB, ...]  # the signals that queued an entry, with their orders below (same order)
    orders: tuple[OrderB, ...]
    fills: tuple[FillB, ...]
    stops: tuple[StopMoveB, ...]
    end_fills: tuple[FillB, ...]  # kind "END": the positions closed at the last bar (not decisions)
    events: tuple[tuple[int, str, str], ...]  # (bar ts, asset or "" for the account's breakers, event)


@dataclass
class _Pos:
    asset: str
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


def run_engine(
    frames: Mapping[str, Frame15],
    signals: Mapping[str, Iterable[SignalB]],
    cfg: BConfig,
    start_ts: int | None = None,
) -> BResult:
    """One run over the frames ({asset: Frame15} of assets of cfg, cut at the end before building them) and
    their signals ({asset: signals}). Entries only from start_ts."""
    allowed = [s.asset for s in cfg.assets]
    names = [a for a in allowed if a in frames]
    if not names or len(names) != len(frames) or not set(signals) <= set(frames):
        raise ValueError(f"frames {sorted(frames)} and signals {sorted(signals)} must be assets of {allowed}")
    if len({int(frames[a].ts[-1]) for a in names}) != 1:
        raise ValueError("the frames must end on the same bar (build them with build_frames)")
    m = len(names)
    costs = [asset_costs(cfg, a) for a in names]
    size = [c.contract_size for c in costs]
    fr = [frames[a] for a in names]
    t0 = min(int(f.ts[0]) for f in fr)
    off = [(int(f.ts[0]) - t0) // M15 for f in fr]  # timeline index of each asset's first bar
    n_bars = (int(fr[0].ts[-1]) - t0) // M15 + 1
    op, hi, lo, cl = ([getattr(f, k).tolist() for f in fr] for k in ("open", "high", "low", "close"))
    syn = [f.synthetic.tolist() for f in fr]
    by_i: list[dict[int, SignalB]] = [{} for _ in names]
    for j, a in enumerate(names):
        for s in signals.get(a, ()):
            by_i[j].setdefault(s.i, s)
    time_stop = cfg.params.time_stop_hours * H1
    breakers = CircuitBreakers(cfg.breakers, cfg.initial_equity)
    cash = float(cfg.initial_equity)
    pos: list[_Pos | None] = [None] * m
    pending: list[tuple[SignalB, int, int] | None] = [None] * m
    exit_req: list[str | None] = [None] * m
    mark_px = [0.0] * m
    trades: list[Trade] = []
    equity: list[EquityPoint] = []
    sigs: list[SignalB] = []
    orders: list[OrderB] = []
    fills: list[FillB] = []
    stops: list[StopMoveB] = []
    events: list[tuple[int, str, str]] = []
    end_fills: list[FillB] = []

    def close(j: int, t: int, kind: str, price: float, qty: int, reason: str | None) -> FillB:
        nonlocal cash
        p = pos[j]
        assert p is not None
        fe = fee(price, qty, costs[j])
        realized = p.direction * (price - p.entry_price) * qty * size[j]
        cash += realized - fe
        p.fees += fe
        p.gross += realized
        p.contracts -= qty
        p.exit_qty += qty
        p.exit_value += price * qty
        fill = FillB(t, p.asset, kind, qty, price, fe, p.entry_ts)
        if p.contracts == 0:
            trade = _trade(p, t, reason or kind, size[j])
            trades.append(trade)
            for ev in breakers.on_trade_closed(t, trade.pnl):
                events.append((t, "", ev))
            pos[j] = None
        return fill

    def account_equity() -> float:
        eq = cash
        for j, p in enumerate(pos):
            if p is not None:
                eq += p.direction * (mark_px[j] - p.entry_price) * p.contracts * size[j]
        return eq

    def committed_notional() -> float:
        out = 0.0
        for j in range(m):
            p, q = pos[j], pending[j]
            if p is not None:
                out += p.contracts * size[j] * mark_px[j]
            if q is not None:
                out += q[1] * size[j] * q[0].entry_ref
        return out

    for k in range(n_bars):
        t = t0 + k * M15
        here = [(j, k - off[j]) for j in range(m) if k >= off[j]]  # (asset, its bar index) with a bar at t
        for j, i in here:  # 1. queued exits first ...
            p = pos[j]
            if exit_req[j] is not None and p is not None:
                fills.append(close(j, t, "EXIT", slip(op[j][i], -p.direction, costs[j]), p.contracts, exit_req[j]))
            exit_req[j] = None
        for j, i in here:  # ... then queued entries
            q = pending[j]
            if q is None:
                continue
            sig, n, n1 = q
            pending[j] = None
            d = sig.direction
            if syn[j][i]:
                events.append((t, names[j], "ENTRY_SKIPPED:gap"))
            elif d * (op[j][i] - sig.stop) <= 0.0:
                events.append((t, names[j], "ENTRY_SKIPPED:open_beyond_stop"))
            else:
                px = slip(op[j][i], d, costs[j])
                fe = fee(px, n, costs[j])
                cash -= fe
                pos[j] = _Pos(names[j], sig, d, n, n, t, px, sig.stop, sig.tp1, sig.tp2, n1, fe,
                              snapshot=_snapshot(sig, names[j]))
                fills.append(FillB(t, names[j], "ENTRY", n, px, fe, t))
        for j, i in here:
            mark_px[j] = cl[j][i]
            p = pos[j]
            if p is None:
                continue
            d = p.direction
            tp1_hit = False
            if d * (op[j][i] - p.stop) <= 0.0:  # 2. intrabar
                fills.append(close(j, t, "STOP", slip(op[j][i], -d, costs[j]), p.contracts, None))
            elif (lo[j][i] <= p.stop) if d > 0 else (hi[j][i] >= p.stop):
                fills.append(close(j, t, "STOP", slip(p.stop, -d, costs[j]), p.contracts, None))
            else:
                fav = hi[j][i] if d > 0 else lo[j][i]
                if p.tp1 is not None and d * (fav - p.tp1) > 0.0:
                    px, p.tp1, tp1_hit = p.tp1, None, True
                    fills.append(close(j, t, "TP1", px, min(p.tp1_n, p.contracts), None))
                if pos[j] is not None and p.tp1 is None and d * (fav - p.tp2) > 0.0:
                    fills.append(close(j, t, "TP2", p.tp2, p.contracts, None))
            if pos[j] is None:
                continue
            if tp1_hit and cfg.params.remainder == "breakeven" and p.stop != p.entry_price:  # 3.
                stops.append(StopMoveB(t, p.asset, p.entry_ts, p.stop, p.entry_price))
                p.stop = p.entry_price
            cost = funding_per_hour(cl[j][i], p.contracts, d, costs[j]) * (M15 / H1)  # 4.
            cash -= cost
            p.funding += cost
            if t + M15 - p.entry_ts >= time_stop:  # 5.
                exit_req[j] = "TIME"
        hour_close = t % H1 == _HOUR_LAST
        if hour_close:  # 6.
            for ev in breakers.on_equity(t + M15, account_equity()):
                events.append((t, "", ev))
            if breakers.halted:
                for j in range(m):
                    if pos[j] is not None:
                        exit_req[j] = "BREAKER"
        if k < n_bars - 1 and (start_ts is None or t >= start_ts):  # 7.
            for j, i in here:
                sig = by_i[j].get(i)
                if sig is None or pos[j] is not None or pending[j] is not None:
                    continue
                if not breakers.can_enter(t + M15):
                    events.append((t, names[j], "SIGNAL_REJECTED:breaker"))
                    continue
                eq = account_equity()
                n = size_contracts(eq, sig.entry_ref, sig.stop, sig.direction, cfg.risk, 1.0, costs[j])
                if n == 0:
                    events.append((t, names[j], "SIGNAL_REJECTED:size"))
                    continue
                room = cfg.max_total_leverage * eq - committed_notional()
                n = min(n, max(0, math.floor(room / (sig.entry_ref * size[j]) + _EPS)))
                if n == 0:
                    events.append((t, names[j], "SIGNAL_REJECTED:leverage"))
                    continue
                n1 = tp1_contracts(n, cfg.tp1_frac)
                pending[j] = (sig, n, n1)
                sigs.append(sig)
                orders.append(OrderB(t, names[j], sig.direction, n, n1))
        if k == n_bars - 1:  # 8.
            for j in range(m):
                pending[j] = None
                p = pos[j]
                if p is not None:
                    key = p.entry_ts
                    f_end = close(j, t, "EXIT", slip(mark_px[j], -p.direction, costs[j]), p.contracts, "END")
                    end_fills.append(FillB(f_end.ts, f_end.asset, "END", f_end.contracts, f_end.price, f_end.fee, key))
        if hour_close and (start_ts is None or t + M15 >= start_ts // H1 * H1):  # 9.
            equity.append(EquityPoint(t - _HOUR_LAST, account_equity()))
    return BResult(tuple(trades), tuple(equity), tuple(sigs), tuple(orders), tuple(fills), tuple(stops),
                   tuple(end_fills), tuple(events))


def _snapshot(sig: SignalB, asset: str) -> dict:
    return {
        "asset": asset, "signal_ts": sig.ts, "entry_ref": sig.entry_ref, "leg": [sig.leg_start, sig.leg_end],
        "pivots": [sig.p1_ts, sig.p2_ts], "level": sig.level, "reason": sig.reason,
    }


def _trade(pos: _Pos, exit_ts: int, reason: str, cs: float) -> Trade:
    pnl = pos.gross - pos.fees - pos.funding
    risk_usd = abs(pos.entry_price - pos.sig.stop) * pos.total * cs
    return Trade(
        entry_ts=pos.entry_ts, exit_ts=exit_ts, setup=setup_of(pos.asset), direction=pos.direction,
        contracts=pos.total, entry_price=pos.entry_price, exit_price_avg=pos.exit_value / pos.exit_qty,
        stop0=pos.sig.stop, tp1=pos.sig.tp1, tp2=pos.sig.tp2, pnl=pnl, fees=pos.fees, funding=pos.funding,
        r_multiple=pnl / risk_usd if risk_usd > 0.0 else 0.0, exit_reason=reason, snapshot=pos.snapshot,
    )
