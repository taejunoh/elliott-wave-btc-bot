"""Target vs. actual -> actions (Plan 3 spec 3.2 `reconcile`, 4.2, 4.4). Pure functions: no I/O.

State of the one live trade (`LiveTrade`, persisted by the executor in the journal) is compared with
the engine's target (live/target.py) and the exchange's position and open orders:

  1. `observe` folds exchange facts into the trade: flat -> CLOSED; a reduce of ours that the executor
     recorded before sending it (`pending_reduce`, intent before state) and that now shows in the position
     is ours whatever its answer was (REDUCE_SETTLED: TP1_SYNC -> the live TP1, MARGIN -> a smaller trade;
     a filled reduce the position still does not show after REDUCE_SETTLE_S yields to the position,
     REDUCE_NOT_SEEN); fewer contracts than expected (`expected_contracts`: all before the live TP1, the
     rest after it) are otherwise explained only by our resting legs - a reduction is a bracket fill when
     the legs still cover exactly the position: the TP1 leg used up -> TP1_FILLED (break-even), else
     PARTIAL_FILL (the legs keep their remainders); any other reduction is UNEXPLAINED_REDUCTION (the trade
     shrinks to the position, the stop stays where it is, no break-even). Open legs that do not cover the
     position -> unprotected since now (a major incident after 60 s, spec 6.2; PROTECTED ends it, a later
     gap is a new incident).
  2. `plan` returns the actions, in order:
       - a position the journal does not know: adopt it when the engine holds the same direction (at
         most the stage maximum `ctx.max_contracts`, the rest is reduced: EXCESS), else close it
         (spec 4.4: the actual position always follows the target);
       - within the hour after an entry the replay still shows it as queued (`target.entry`): the live
         trade of that signal is the engine position (`pending_position`), never a reason to close;
       - flat: cancel our leftover orders; enter when the engine queued an entry at the last close
         (never a signal the live trade already traded: closed early by its bracket, it is not
         re-entered);
       - engine flat / another trade / exit queued (TIME, LINEAGE, BREAKER, FLIP, ...): close, then
         enter if the engine also queued an entry (the executor enters only after the close is
         confirmed, spec 4.2 `반대 신호 전환`). An exit queued before the CFM halt is still queued at
         the halt bar (`exit_queued_ts` < bar) and a stop the halt bar crossed stops the engine out at
         the reopen (`stop_pending_reopen`, reason STOP_HALT): both close as well - the executor sends
         the close at the first poll that sees the session OPEN, whichever target is current then -
         until then the position stays protected (missing legs are placed at the stop in force);
       - more contracts than expected (a manual order, a reduction that did not fill): reduce the excess
         at market while the session is OPEN (spec 4.4) and alert; the legs cover the expected size, or the
         whole position while the excess cannot be reduced (session closed, a reduce pending: brackets
         only reduce, so the excess is never left unprotected);
       - engine TP1 done but the live TP1 leg not filled: reduce by what the resting TP1 leg has left at
         market (spec 4.4) - never while that leg is missing (a vanished leg may have filled with the
         position read before it: the executor looks it up before re-placing it);
       - never a second reduce while one is pending: until its outcome is known it is resolved by its
         client order id (executor), and the trade is planned as it was; once the venue reported it
         filled, as it will be (the position may still lag behind it);
       - protection: the brackets must equal the desired legs (spec 4.2): two legs (TP1 size + stop,
         rest + TP2 + stop) before TP1, one leg (rest + TP2 + stop) after it, one leg (TP1 + stop) for
         1 contract; after the live TP1 the stop is at least break-even (the live entry price on the tick:
         a long's rounded up, a short's down - while the futures price is less than a tick beyond that, or
         unknown, the entry rounded the other way, as a stop through the market is refused or fills at
         once). Legs never exceed the position. A moved engine stop is mapped with the basis ratio at the time; without
         quotes the stop in force is kept and the move is planned again once quotes are back.
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
REDUCE_SETTLE_S = 60.0  # a reduce the venue reported filled but the position does not show by then: the position wins


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
    # a reduce sent and not yet seen in the position, recorded by the executor before the request: {"cid", "size",
    # "reason", "before" (position contracts when sent), "ts", "after" (`after_reduce`: the trade's size fields
    # once done), "filled" (contracts the venue reported filled; None while the outcome is unknown)}
    pending_reduce: dict[str, Any] | None = None

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
    contracts: int  # contracts to take over (at most the stage maximum); the rest is reduced (EXCESS)


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
    max_contracts: int | None = None  # stage maximum for a position taken over (None: no cap)
    price: float | None = None  # futures mid now, read with `ratio` (None: quotes unavailable): the break-even stop

    def __post_init__(self) -> None:
        if self.max_contracts is not None and self.max_contracts < 1:
            raise ValueError(f"bad max_contracts {self.max_contracts}")


# ----------------------------------------------------------------------------- observe


def _open_legs(trade: LiveTrade, orders: list[Order]) -> list[tuple[Leg, Order]]:
    by_id = {o.order_id: o for o in orders if o.is_open}
    return [(leg, by_id[leg.order_id]) for leg in trade.legs if leg.order_id in by_id]


def expected_contracts(trade: LiveTrade) -> int:
    """Contracts the live trade holds: all of them before the live TP1, the rest after it."""
    return trade.contracts - trade.tp1_n if trade.tp1_filled else trade.contracts


def after_reduce(trade: LiveTrade, size: int, reason: str, tp1_n: int | None = None) -> dict[str, Any]:
    """The trade's size fields once a reduce of `size` is done: TP1_SYNC marks the live TP1 done, EXCESS changes
    nothing (it closes contracts above the trade), any other reason (MARGIN) makes the trade smaller - `tp1_n`
    is then its new TP1 portion before the live TP1 (the executor splits it like the engine)."""
    after = {"contracts": trade.contracts, "tp1_n": trade.tp1_n, "tp1_filled": trade.tp1_filled}
    if reason == "TP1_SYNC":
        after["tp1_filled"] = True
    elif reason != "EXCESS":
        after["contracts"] = max(trade.contracts - size, 0)
        if not trade.tp1_filled and tp1_n is not None:
            after["tp1_n"] = tp1_n
    return after


def _settled(trade: LiveTrade) -> LiveTrade:
    """A copy of the trade with its pending reduce done (its `after` fields; nothing pending)."""
    d = trade.to_json()
    if trade.pending_reduce is not None:
        d.update(trade.pending_reduce["after"])
    d["pending_reduce"] = None
    return LiveTrade.from_json(d)


def _held(trade: LiveTrade, n: int) -> int:
    """The contracts the trade holds once a reduce the venue reported filled shows in a position of `n` that
    still lags behind it; `n` otherwise (nothing pending, the outcome unknown, or already shown)."""
    pr = trade.pending_reduce
    if pr is not None and pr.get("filled") and n >= pr["before"]:
        return max(n - int(pr["filled"]), 0)
    return n


def _leg_rest(trade: LiveTrade, orders: list[Order], name: str) -> int | None:
    """Contracts our `name` leg still has resting (0 once it left the book); None: the trade has no such leg."""
    leg = next((x for x in trade.legs if x.name == name), None)
    if leg is None:
        return None
    o = next((o for x, o in _open_legs(trade, orders) if x is leg), None)
    return 0 if o is None else max(o.size - o.filled, 0)


def _fold_reduction(t: LiveTrade, n: int, orders: list[Order]) -> tuple[str, dict[str, Any]]:
    """`t` (a copy) holds fewer contracts (`n`) than expected: fold the reduction into it and name it.

    A bracket fill reduces its leg and the position alike, so a reduction is ours when the legs still
    cover exactly the position. Before the live TP1 (two legs): the TP1 leg used up -> TP1_FILLED (a TP2
    fill in the same interval is folded in), else PARTIAL_FILL (the TP1 leg keeps its remainder). After it
    (one leg): PARTIAL_FILL. Anything else is UNEXPLAINED_REDUCTION: the trade shrinks to the position,
    the TP1 portion kept as far as possible, the stop unchanged (no break-even)."""
    expected = expected_contracts(t)
    if t.tp1_filled:
        rest2 = _leg_rest(t, orders, "tp2")
        t.contracts = n + t.tp1_n
        if rest2 == n:
            return "PARTIAL_FILL", {"trade_id": t.trade_id, "contracts": n}
    else:
        rest1, rest2 = _leg_rest(t, orders, "tp1"), _leg_rest(t, orders, "tp2")
        if t.contracts > 1 and rest1 is not None and rest2 is not None and rest1 + rest2 == n:
            if rest1 == 0:
                t.tp1_filled, t.contracts = True, n + t.tp1_n
                return "TP1_FILLED", {"trade_id": t.trade_id, "contracts": n}
            t.contracts, t.tp1_n = n, rest1
            return "PARTIAL_FILL", {"trade_id": t.trade_id, "contracts": n}
        t.contracts, t.tp1_n = n, max(1, min(t.tp1_n, n - 1))
    return "UNEXPLAINED_REDUCTION", {"trade_id": t.trade_id, "expected": expected, "position": n}


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
    pr = t.pending_reduce
    if pr is not None and position.contracts < pr["before"]:  # our reduce shows in the position: ours
        t = _settled(t)
        events.append(("REDUCE_SETTLED", {"trade_id": t.trade_id, "reason": pr["reason"], "size": pr["size"],
                                          "position": position.contracts}))
    elif pr is not None and pr.get("filled") and now - float(pr["ts"]) > REDUCE_SETTLE_S:
        t.pending_reduce = None  # reported filled, still not in the position: the position wins (plan: EXCESS)
        events.append(("REDUCE_NOT_SEEN", {"trade_id": t.trade_id, "reason": pr["reason"], "filled": pr["filled"],
                                           "position": position.contracts}))
    held = _held(t, position.contracts)
    if held < expected_contracts(t):
        events.append(_fold_reduction(t, held, orders))
    covered = sum(o.size - o.filled for leg, o in _open_legs(t, orders) if o.stop_price is not None)
    if covered >= held:
        if t.unprotected_since is not None:
            events.append(("PROTECTED", {"trade_id": t.trade_id, "after_s": round(now - t.unprotected_since, 3)}))
        t.unprotected_since = None
        t.incident_unprotected = False  # the incident ends here: a later gap is a new one
    else:
        if t.unprotected_since is None:
            t.unprotected_since = now
            events.append(("UNPROTECTED", {"trade_id": t.trade_id, "covered": covered, "position": held}))
        elif now - t.unprotected_since > UNPROTECTED_MAX_S and not t.incident_unprotected:
            t.incident_unprotected = True
            events.append(("UNPROTECTED_60S", {"trade_id": t.trade_id, "since": t.unprotected_since}))
    return t, events


# ----------------------------------------------------------------------------- desired legs


def _leg_sizes(trade: LiveTrade, contracts: int) -> list[tuple[str, int]]:
    """Leg names and sizes covering `contracts` (normally `expected_contracts`; never more than held)."""
    if contracts <= 0:
        return []
    if trade.tp1_filled:
        return [("tp2", contracts)]
    if trade.contracts == 1 or contracts == 1:
        return [("tp1", contracts)]
    k = min(trade.tp1_n, contracts - 1)
    return [("tp1", k), ("tp2", contracts - k)]


def _break_even(trade: LiveTrade, tick: float, price: float | None) -> float:
    """The futures stop after the live TP1 (spec 4.2, interpretation 5: at least the live entry price): the entry on
    the tick toward the profit side - a long's rounded up, a short's down. A stop rests on the loss side of the market
    (a long's sell stop above the last trade is refused, or triggers at once), so while the futures price is less than
    a tick beyond that level - or unknown - it is the entry on the tick toward the loss side (at most a tick short of
    break-even, the nearest the grid allows), moved up once the price allows; a stop resting at or beyond the
    profit-side level keeps it (never loosened)."""
    d = trade.direction
    up = to_tick(trade.entry_price, tick, -d)
    if d * (trade.stop_price - up) >= 0 or (price is not None and d * (price - up) >= tick):
        return up
    return to_tick(trade.entry_price, tick, d)


def desired_legs(trade: LiveTrade, contracts: int, stop_spot: float, ratio: float | None, tick: float,
                 price: float | None = None) -> tuple[list[Leg], float]:
    """The brackets that must rest for `contracts` contracts, and their futures stop. The stored futures
    stop is kept while the engine stop is unchanged (no churn when only the basis moves) or without
    quotes; after the live TP1 it is at least break-even (`_break_even`, judged against the futures `price`).
    Raises ValueError when a TP2 leg is needed and the trade has no TP2."""
    d = trade.direction
    if stop_spot == trade.stop_spot or ratio is None:
        stop = trade.stop_price
    else:
        stop = map_level(stop_spot, ratio, tick, d)
    if trade.tp1_filled:
        be = _break_even(trade, tick, price)
        stop = max(stop, be) if d > 0 else min(stop, be)
    legs: list[Leg] = []
    for name, size in _leg_sizes(trade, contracts):
        if name == "tp1":
            legs.append(Leg("tp1", size, trade.tp1_price, stop))
        elif trade.tp2_price is None:
            raise ValueError(f"trade {trade.trade_id}: a TP2 leg of {size} needs a TP2 price")
        else:
            legs.append(Leg("tp2", size, trade.tp2_price, stop))
    return legs, stop


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


def _engine_view(target: Target, trade: LiveTrade) -> TargetPosition | str:
    """The engine position of the live trade, or the reason the live trade must close."""
    e = target.entry
    if e is not None and e.signal_ts == trade.trade_id:
        return pending_position(e)  # entered at this hour's open; the replay still shows the queued entry
    if target.exit_reason is not None:  # queued at the last close, or before the halt (carried)
        return target.exit_reason
    tp = target.position
    if tp is None:
        return "ENGINE_FLAT"
    if tp.signal_ts != trade.trade_id:
        return "ENGINE_OTHER_TRADE"
    if target.stop_pending_reopen:
        return "STOP_HALT"
    return tp


def _protection(trade: LiveTrade, orders: list[Order], contracts: int, stop_spot: float,
                ctx: PlanContext) -> list[Action]:
    desired, stop = desired_legs(trade, contracts, stop_spot, ctx.ratio, ctx.tick, ctx.price)
    if not legs_match(trade, orders, desired) or stop_spot != trade.stop_spot:
        return [Protect(tuple(desired), stop_spot, stop)]
    return []


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
        if trade is not None and trade.trade_id == e.signal_ts:
            return []  # this signal was traded (open, or closed early by its bracket): never entered twice
        if ctx.entry_block is not None:
            return [Skip(e.signal_ts, ctx.entry_block)]
        if e.tp2 is None:
            return [Skip(e.signal_ts, "the engine signal has no TP2 (a second bracket needs one)")]
        return [Enter(e, ctx.entry_contracts)]

    if position is None:
        if ours:
            acts.append(Cancel(tuple(o.order_id for o in ours), "FLAT"))
        return acts + entry_actions()

    n = position.contracts
    if open_trade is None:  # a position the journal does not know
        if target is None:
            return acts + [Alert("UNKNOWN_POSITION", f"{position.side:+d} x{n}: waiting for a target")]
        held = engine_position(target)
        if held is None or held.direction != position.side:
            return acts + [Close("UNKNOWN_POSITION")]
        take = n if ctx.max_contracts is None else min(n, ctx.max_contracts)
        if take == n:
            return acts + [Adopt(held, n)]
        acts += [Alert("EXCESS_POSITION", f"position {n} > stage maximum {ctx.max_contracts}: adopting {take}"),
                 Adopt(held, take)]
        return acts + ([Reduce(n - take, "EXCESS")] if ctx.session_open else [])

    if position.side != open_trade.direction:
        acts.append(Close("DIRECTION_MISMATCH"))
        if not ctx.session_open:  # the trade's legs only reduce its own side: nothing can protect this
            acts.append(Alert("DIRECTION_MISMATCH", f"trade {open_trade.trade_id} {open_trade.direction:+d}: "
                                                    f"position {position.side:+d} x{n} unprotected until the reopen"))
        return acts

    pr = open_trade.pending_reduce
    if pr is not None and n < pr["before"]:  # our reduce shows in the position (observe settles it as well)
        open_trade, pr = _settled(open_trade), None
    have = _held(open_trade, n)  # a filled reduce the position does not show yet counts as done
    expected = expected_contracts(open_trade)
    excess = have - expected
    reducible = ctx.session_open and pr is None  # market orders need an OPEN session; one reduce at a time
    # the legs cover the expected size, never more than the position; an excess that cannot be reduced now
    # is covered as well (brackets only reduce: protected until the reduce at the reopen resizes them)
    size = have if excess > 0 and not reducible else min(have, expected)
    stop_spot = open_trade.stop_spot
    tp: TargetPosition | None = None
    if target is not None:
        view = _engine_view(target, open_trade)
        if isinstance(view, str):
            acts.append(Close(view))
            if not ctx.session_open:  # the close waits for the reopen: keep the position protected meanwhile
                acts += _protection(open_trade, orders, size, stop_spot, ctx)
            return acts + entry_actions()
        tp = view
        if ctx.ratio is not None:  # without quotes the stop in force stays; the move is planned again later
            stop_spot = tp.stop

    if excess > 0:  # spec 4.4: the excess is closed at market (a manual order, an unfilled reduction)
        acts.append(Alert("EXCESS_POSITION", f"trade {open_trade.trade_id}: position {n} > {expected} expected"))
        if reducible:
            acts.append(Reduce(excess, "EXCESS"))
    elif (excess == 0 and tp is not None and tp.tp1_done and not open_trade.tp1_filled and open_trade.contracts > 1
          and reducible):
        rest1 = _leg_rest(open_trade, orders, "tp1")
        if rest1:  # only against a resting TP1 leg, by what it has left: a vanished one may have filled already
            k = min(open_trade.tp1_n, rest1)
            acts.append(Reduce(k, "TP1_SYNC"))
            open_trade = LiveTrade.from_json({**open_trade.to_json(), "tp1_filled": True})
            size = min(have - k, expected_contracts(open_trade))
    return acts + _protection(open_trade, orders, size, stop_spot, ctx)
