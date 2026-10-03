"""Target vs. actual -> actions (Plan 3 spec 3.2 `reconcile`, 4.2, 4.4). Pure functions: no I/O.

State of the one live trade (`LiveTrade`, persisted by the executor in the journal) is compared with
the engine's target (live/target.py) and the exchange's position and open orders:

  1. `observe` folds exchange facts into the trade: flat -> CLOSED; fewer contracts than entered ->
     the TP1 leg (or our TP1 sync) has filled; open legs that do not cover the position -> unprotected
     since now (a major incident after 60 s, spec 6.2).
  2. `plan` returns the actions, in order:
       - a position the journal does not know: adopt it when the engine holds the same direction,
         else close it (spec 4.4: the actual position always follows the target);
       - within the hour after an entry the replay still shows it as queued (`target.entry`): the live
         trade of that signal is the engine position (`pending_position`), never a reason to close;
       - flat: cancel our leftover orders; enter when the engine queued an entry at the last close;
       - engine flat / another trade / exit queued (TIME, LINEAGE, BREAKER, FLIP, ...): close, then
         enter if the engine also queued an entry (the executor enters only after the close is
         confirmed, spec 4.2 `반대 신호 전환`). An exit queued before the CFM halt is still queued at
         the halt bar (`exit_queued_ts` < bar) and a stop the halt bar crossed stops the engine out at
         the reopen (`stop_pending_reopen`, reason STOP_HALT): both close as well - the executor sends
         the close at the first poll that sees the session OPEN, whichever target is current then;
       - engine TP1 done but the live TP1 leg not filled: reduce by the TP1 size at market (spec 4.4);
       - protection: the brackets must equal the desired legs (spec 4.2): two legs (TP1 size + stop,
         rest + TP2 + stop) before TP1, one leg (rest + TP2 + stop) after it, one leg (TP1 + stop) for
         1 contract; after the live TP1 the stop is at least break-even (live entry price).
     Without a target (data late, spec 4.1 step 1) only protection is maintained.

Client order ids are deterministic (spec 4.2): entry `ewb-e-<signal ts>-<L|S>`, brackets
`ewb-b-<trade>-<leg>-<generation>`, closes `ewb-x-<trade>-<n>`, reductions `ewb-r-<trade>-<n>`."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from ewbot.live.exchange import Order, Position
from ewbot.live.pricing import map_level, to_tick
from ewbot.live.target import Target, TargetEntry, TargetPosition

PREFIX = "ewb-"
UNPROTECTED_MAX_S = 60.0  # spec 6.2: a position unprotected for longer is a major incident


def entry_cid(signal_ts: int, direction: int) -> str:
    return f"{PREFIX}e-{int(signal_ts)}-{'L' if direction > 0 else 'S'}"


def bracket_cid(trade_id: int, leg: str, gen: int) -> str:
    return f"{PREFIX}b-{int(trade_id)}-{leg}-{int(gen)}"


def close_cid(trade_id: int, n: int) -> str:
    return f"{PREFIX}x-{int(trade_id)}-{int(n)}"


def reduce_cid(trade_id: int, n: int) -> str:
    return f"{PREFIX}r-{int(trade_id)}-{int(n)}"


def is_ours(order: Order) -> bool:
    return order.client_order_id.startswith(PREFIX)


@dataclass
class Leg:
    name: str  # "tp1" | "tp2"
    size: int
    limit_price: float  # futures take-profit
    stop_price: float  # futures stop trigger
    client_order_id: str = ""
    order_id: str | None = None


@dataclass
class LiveTrade:
    trade_id: int  # signal ts of the engine trade (its paper trade key is trade_id + 1h)
    setup: str
    direction: int
    contracts: int  # filled at entry
    tp1_n: int
    entry_price: float  # futures average fill
    tp1_price: float  # futures, mapped at entry
    tp2_price: float | None
    stop_spot: float  # engine stop (spot) the legs protect
    stop_price: float  # futures stop of the legs
    opened_ts: float
    tp1_filled: bool = False
    legs: list[Leg] = field(default_factory=list)
    gen: int = 0
    closes: int = 0
    protected_ts: float | None = None
    unprotected_since: float | None = None
    incident_unprotected: bool = False
    status: str = "OPEN"  # OPEN | CLOSED
    exit_reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> LiveTrade:
        d = dict(data)
        d["legs"] = [Leg(**leg) for leg in d.get("legs", [])]
        return cls(**d)


# ----------------------------------------------------------------------------- actions


@dataclass(frozen=True)
class Close:
    reason: str


@dataclass(frozen=True)
class Reduce:
    size: int
    reason: str


@dataclass(frozen=True)
class Protect:
    legs: tuple[Leg, ...]
    stop_spot: float
    stop_price: float


@dataclass(frozen=True)
class Enter:
    entry: TargetEntry
    contracts: int


@dataclass(frozen=True)
class Skip:
    signal_ts: int
    reason: str


@dataclass(frozen=True)
class Cancel:
    order_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Adopt:
    target: TargetPosition


@dataclass(frozen=True)
class Alert:
    kind: str
    detail: str


Action = Close | Reduce | Protect | Enter | Skip | Cancel | Adopt | Alert


@dataclass(frozen=True)
class PlanContext:
    now: float
    ratio: float | None  # futures mid / spot mid now (None: quotes unavailable)
    tick: float
    session_open: bool = True  # market orders (entry, close, reduce) only while the session is OPEN
    entry_contracts: int = 0  # stage size for target.entry (0: skip)
    entry_block: str | None = None  # why no entry may be sent now (paused, session, margin, stale, size)


# ----------------------------------------------------------------------------- observe


def _open_legs(trade: LiveTrade, orders: list[Order]) -> list[tuple[Leg, Order]]:
    by_id = {o.order_id: o for o in orders if o.is_open}
    return [(leg, by_id[leg.order_id]) for leg in trade.legs if leg.order_id in by_id]


def observe(trade: LiveTrade | None, position: Position | None, orders: list[Order], now: float
            ) -> tuple[LiveTrade | None, list[tuple[str, dict[str, Any]]]]:
    """Fold exchange facts into the trade (a modified copy) and return the events they raise."""
    if trade is None or trade.status != "OPEN":
        return trade, []
    t = LiveTrade.from_json(trade.to_json())
    events: list[tuple[str, dict[str, Any]]] = []
    if position is None:
        t.status = "CLOSED"
        t.exit_reason = t.exit_reason or "BRACKET"
        t.unprotected_since = None
        events.append(("CLOSED", {"trade_id": t.trade_id, "reason": t.exit_reason}))
        return t, events
    if position.side != t.direction:
        return t, events  # plan() closes a position on the wrong side
    if not t.tp1_filled and t.contracts > 1 and position.contracts < t.contracts:
        t.tp1_filled = True
        events.append(("TP1_FILLED", {"trade_id": t.trade_id, "contracts": position.contracts}))
    covered = sum(o.size - o.filled for leg, o in _open_legs(t, orders) if o.stop_price is not None)
    if covered >= position.contracts:
        if t.unprotected_since is not None:
            events.append(("PROTECTED", {"trade_id": t.trade_id, "after_s": round(now - t.unprotected_since, 3)}))
        t.unprotected_since = None
    else:
        if t.unprotected_since is None:
            t.unprotected_since = now
            events.append(("UNPROTECTED", {"trade_id": t.trade_id, "covered": covered, "position": position.contracts}))
        elif now - t.unprotected_since > UNPROTECTED_MAX_S and not t.incident_unprotected:
            t.incident_unprotected = True
            events.append(("UNPROTECTED_60S", {"trade_id": t.trade_id, "since": t.unprotected_since}))
    return t, events


# ----------------------------------------------------------------------------- desired legs


def desired_legs(trade: LiveTrade, contracts: int, stop_spot: float, ratio: float | None, tick: float
                 ) -> tuple[list[Leg], float]:
    """The brackets that must rest for `contracts` contracts, and their futures stop. The stored futures
    stop is kept while the engine stop is unchanged (no churn when only the basis moves)."""
    d = trade.direction
    if stop_spot == trade.stop_spot or ratio is None:
        stop = trade.stop_price
    else:
        stop = map_level(stop_spot, ratio, tick, d)
    if trade.tp1_filled:
        be = to_tick(trade.entry_price, tick, d)
        stop = max(stop, be) if d > 0 else min(stop, be)
    if trade.contracts == 1:
        return [Leg("tp1", contracts, trade.tp1_price, stop)], stop
    if not trade.tp1_filled and contracts == trade.contracts:
        assert trade.tp2_price is not None
        return [Leg("tp1", trade.tp1_n, trade.tp1_price, stop),
                Leg("tp2", contracts - trade.tp1_n, trade.tp2_price, stop)], stop
    assert trade.tp2_price is not None
    return [Leg("tp2", contracts, trade.tp2_price, stop)], stop


def legs_match(trade: LiveTrade, orders: list[Order], desired: list[Leg]) -> bool:
    """Every desired leg rests with exactly its terms, and no other leg of ours rests."""
    resting = _open_legs(trade, orders)
    if len(resting) != len(desired):
        return False
    have = {leg.name: (leg, o) for leg, o in resting}
    for want in desired:
        got = have.get(want.name)
        if got is None:
            return False
        leg, o = got
        if (o.size - o.filled, o.limit_price, o.stop_price) != (want.size, want.limit_price, want.stop_price):
            return False
    return True


# ----------------------------------------------------------------------------- plan


def pending_position(e: TargetEntry) -> TargetPosition:
    """The engine position an entry queued at the last close becomes at the next open: within that hour
    the live position already exists while the engine replay still shows it as a queued entry."""
    return TargetPosition(trade_key=e.signal_ts + 3600, signal_ts=e.signal_ts, setup=e.setup, direction=e.direction,
                          entry_price=e.entry_ref, stop=e.stop, stop0=e.stop, tp1=e.tp1, tp2=e.tp2, tp1_done=False,
                          contracts=e.contracts, remaining=e.contracts)


def engine_position(target: Target) -> TargetPosition | None:
    """The engine's position for the hour after the target bar: the open one, unless an exit is queued or
    the halt bar crossed its stop, else the one a queued entry opens."""
    if target.position is not None and target.exit_reason is None and not target.stop_pending_reopen:
        return target.position
    if target.entry is not None:
        return pending_position(target.entry)
    return None


def plan(target: Target | None, trade: LiveTrade | None, position: Position | None, orders: list[Order],
         ctx: PlanContext) -> list[Action]:
    acts: list[Action] = []
    ours = [o for o in orders if o.is_open and is_ours(o)]
    foreign = [o for o in orders if o.is_open and not is_ours(o)]
    if foreign:
        acts.append(Alert("FOREIGN_ORDERS", ",".join(sorted(o.order_id for o in foreign))))
    open_trade = trade if trade is not None and trade.status == "OPEN" else None

    def entry_actions() -> list[Action]:
        if target is None or target.entry is None:
            return []
        e = target.entry
        if ctx.entry_block is not None:
            return [Skip(e.signal_ts, ctx.entry_block)]
        if e.tp2 is None:
            return [Skip(e.signal_ts, "the engine signal has no TP2 (a second bracket needs one)")]
        return [Enter(e, ctx.entry_contracts)]

    if position is not None and open_trade is None:  # a position the journal does not know
        if target is None:
            return acts + [Alert("UNKNOWN_POSITION", f"{position.side:+d} x{position.contracts}: waiting for a target")]
        tp = engine_position(target)
        if tp is not None and tp.direction == position.side:
            return acts + [Adopt(tp)]
        return acts + [Close("UNKNOWN_POSITION")]

    if position is None:
        if ours:
            acts.append(Cancel(tuple(o.order_id for o in ours), "FLAT"))
        return acts + entry_actions()

    assert open_trade is not None
    if position.side != open_trade.direction:
        return acts + [Close("DIRECTION_MISMATCH")]

    contracts = position.contracts
    stop_spot = open_trade.stop_spot
    if target is not None:
        tp = target.position
        queued = target.entry is not None and target.entry.signal_ts == open_trade.trade_id
        if queued:  # entered at this hour's open; the replay still shows the queued entry
            tp = pending_position(target.entry)  # type: ignore[arg-type]
        elif (tp is None or tp.signal_ts != open_trade.trade_id or target.exit_reason is not None
              or target.stop_pending_reopen):
            if target.exit_reason is not None:  # queued at the last close, or before the halt (carried)
                reason = target.exit_reason
            elif tp is None:
                reason = "ENGINE_FLAT"
            elif tp.signal_ts != open_trade.trade_id:
                reason = "ENGINE_OTHER_TRADE"
            else:
                reason = "STOP_HALT"
            return acts + [Close(reason)] + entry_actions()
        assert tp is not None
        if (tp.tp1_done and not open_trade.tp1_filled and open_trade.contracts > 1
                and contracts == open_trade.contracts and ctx.session_open):  # market orders need an OPEN session
            acts.append(Reduce(open_trade.tp1_n, "TP1_SYNC"))
            contracts -= open_trade.tp1_n
            open_trade = LiveTrade.from_json({**open_trade.to_json(), "tp1_filled": True})
        stop_spot = tp.stop

    desired, stop = desired_legs(open_trade, contracts, stop_spot, ctx.ratio, ctx.tick)
    if not legs_match(open_trade, orders, desired) or stop_spot != open_trade.stop_spot:
        acts.append(Protect(tuple(desired), stop_spot, stop))
    return acts
