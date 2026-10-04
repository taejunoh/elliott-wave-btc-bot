"""SimExchange: an in-memory CFM futures venue (Plan 3 spec 7 `가짜 거래소`).

Used by every live test (no network) and, wrapped by ShadowExchange (live/shadow.py), as the virtual
book of the SHADOW stage. It models what the runtime relies on:
  - order intake is never more forgiving than the venue: a size that is not a whole number of
    base_increment (min one contract), a price off the price_increment grid ($5) or a bracket whose
    take-profit is not beyond its stop in the profit direction is rejected (PlaceResult not ok,
    edit_bracket False, a preview with errors), as is an edit whose total size (filled + left) leaves
    nothing to rest;
  - market orders and close_position are IOC: they fill at the ask (buy) / bid (sell) while the session
    is OPEN, possibly only partly (`market_fill_ratio`), the rest is cancelled; a bracket left with
    nothing to reduce is cancelled at once;
  - trigger_bracket_gtc orders only ever REDUCE the position (help-center behaviour; the API has no
    reduce_only field): a bracket whose position is gone, or would be increased, is cancelled; a fill
    never exceeds the contracts left; `bracket_fill_cap` makes fills partial;
  - a bracket's take-profit fills at its limit when the futures price reaches it; its stop triggers when
    the price reaches the stop and then rests as a limit STOP_LIMIT_OFFSET (5%, the Create Order
    reference) beyond the stop: it fills at the market while the price is inside that limit and does not
    fill while the price has gapped past it (the case the stop-gap watchdog closes);
  - cancel() cancels every OPEN order it is given and then raises ExchangeError naming each order it could
    not cancel (unknown, or already cancelled / expired: the venue's batch_cancel results[].success is
    false); an order that already FILLED no longer rests and is not an error (ORDER_IS_FULLY_FILLED);
  - nothing fills while the session is not OPEN; market orders are rejected then;
  - fault injection: `fail_next(method, exc)` raises on the next call(s) of a method (429 / 5xx /
    outages; `clear_faults()` drops what is still queued) and `crash_after_accept(method)` raises SimCrash
    right AFTER the order was accepted (a process killed between send and journal update); a method that
    does not honour them (FAULT_METHODS, CRASH_METHODS) is refused with ValueError instead of being silently
    ignored;
  - a repeated client_order_id returns the existing order (`duplicate="existing"`) or an error
    (`duplicate="error"`): Coinbase documents both, the real behaviour is confirmed in CANARY."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from ewbot.live.exchange import (
    FUTURES_PRODUCT,
    SESSION_OPEN,
    SPOT_PRODUCT,
    STOP_LIMIT_OFFSET,
    Balance,
    ExchangeError,
    KeyPermissions,
    Order,
    PlaceResult,
    Position,
    PreviewResult,
    ProductSpec,
    Quote,
    SessionInfo,
)

# as recorded 2026-10-03; the venue's contract_expiry is 2089-12-30T16:00:00Z although the id says 20 Dec 2030
BIP_SPEC = ProductSpec(FUTURES_PRODUCT, 0.01, 5.0, 1.0, (0.100079, 0.0999811), (0.21025, 0.256625), expiry=3786796800)
VIEW_TRADE = KeyPermissions(True, True, False, "DEFAULT")  # what the live key must look like


class SimCrash(BaseException):
    """Simulated process death right after the exchange accepted an order (BaseException: nothing
    in the runtime may catch it, like a SIGKILL)."""


@dataclass
class SimOrder:
    order_id: str
    client_order_id: str
    side: int
    kind: str
    size: int
    created_ts: float
    limit_price: float | None = None
    stop_price: float | None = None
    filled: int = 0
    status: str = "OPEN"
    fill_value: float = 0.0
    fees: float = 0.0
    triggered: bool = False

    def view(self) -> Order:
        avg = self.fill_value / self.filled if self.filled else None
        return Order(self.order_id, self.client_order_id, self.side, self.kind, self.size, self.filled,
                     self.status, self.limit_price, self.stop_price, avg, self.created_ts, self.fees)


class SimExchange:
    # fail_next() works on every method that calls _fault(); crash_after_accept() on those that call
    # _accepted() after accepting an order. A subclass overriding one without that call narrows these.
    FAULT_METHODS: frozenset[str] = frozenset({
        "product", "session", "quote", "position", "open_orders", "get_order", "find_order", "balance",
        "key_permissions", "place_market", "place_bracket", "edit_bracket", "cancel", "close_position",
        "preview_market", "preview_bracket",
    })
    CRASH_METHODS: frozenset[str] = frozenset({"place_market", "place_bracket", "close_position"})

    def __init__(
        self,
        product: ProductSpec = BIP_SPEC,
        *,
        equity: float = 10_000.0,
        clock: Callable[[], float] = time.time,
        spot_id: str = SPOT_PRODUCT,
        spread: float = 5.0,
        duplicate: str = "existing",
        reject_overlap: bool = False,
        edit_supported: bool = False,
        fee_per_contract: float = 0.0,
        permissions: KeyPermissions = VIEW_TRADE,
    ) -> None:
        if duplicate not in ("existing", "error"):
            raise ValueError("duplicate must be 'existing' or 'error'")
        self.spec = product
        self.futures_id = product.product_id
        self.spot_id = spot_id
        self.clock = clock
        self.cash = float(equity)
        self.pos: Position | None = None
        self.orders: dict[str, SimOrder] = {}
        self.fut_price = 0.0
        self.spot_price = 0.0
        self.spread = float(spread)
        self.session_state = SESSION_OPEN
        self.maintenance: tuple[int | None, int | None] = (None, None)
        self.duplicate = duplicate
        self.reject_overlap = reject_overlap
        self.edit_supported = edit_supported
        self.fee_per_contract = float(fee_per_contract)
        self.permissions = permissions
        self.market_fill_ratio = 1.0
        self.bracket_fill_cap: int | None = None
        self.preview_errors: tuple[str, ...] = ()
        self.fills: list[dict] = []
        self.calls: list[str] = []
        self._faults: dict[str, list[BaseException]] = {}
        self._crash_after: dict[str, BaseException] = {}
        self._seq = 0

    # ------------------------------------------------------------------ driving (tests, shadow)
    def set_prices(self, fut: float, spot: float | None = None) -> list[dict]:
        """Move the futures last price (and the spot mid); returns the bracket fills it caused."""
        self.fut_price = float(fut)
        if spot is not None:
            self.spot_price = float(spot)
        return self._match()

    def set_session(self, state: str) -> list[dict]:
        self.session_state = state
        return self._match()

    def fail_next(self, method: str, exc: BaseException, times: int = 1) -> None:
        if method not in self.FAULT_METHODS:
            raise ValueError(f"fail_next: {method!r} never raises injected faults")
        self._faults.setdefault(method, []).extend([exc] * times)

    def clear_faults(self, method: str | None = None) -> None:
        """Drop the faults `fail_next` queued and that have not been raised yet: `method`'s, or every method's (the
        venue answers again)."""
        if method is None:
            self._faults.clear()
        else:
            self._faults.pop(method, None)

    def crash_after_accept(self, method: str, exc: BaseException | None = None) -> None:
        """The next `method` call is accepted, then raises `exc` (default SimCrash: the process dies;
        an Unavailable: the reply was lost, e.g. a timeout after the venue created the order)."""
        if method not in self.CRASH_METHODS:
            raise ValueError(f"crash_after_accept: {method!r} does not accept orders")
        self._crash_after[method] = exc if exc is not None else SimCrash(method)

    def _fault(self, method: str) -> None:
        self.calls.append(method)
        queue = self._faults.get(method)
        if queue:
            raise queue.pop(0)

    def _accepted(self, method: str) -> None:
        exc = self._crash_after.pop(method, None)
        if exc is not None:
            raise exc

    # ------------------------------------------------------------------ reads
    def product(self) -> ProductSpec:
        self._fault("product")
        return self.spec

    def session(self) -> SessionInfo:
        self._fault("session")
        return SessionInfo(self.session_state, *self.maintenance)

    def quote(self, product_id: str) -> Quote:
        self._fault("quote")
        if product_id == self.futures_id:
            half = self.spread / 2.0
            return Quote(product_id, self.fut_price - half, self.fut_price + half, self.clock())
        if product_id == self.spot_id:
            return Quote(product_id, self.spot_price - 0.005, self.spot_price + 0.005, self.clock())
        raise ExchangeError(f"unknown product {product_id}")

    def position(self) -> Position | None:
        self._fault("position")
        return self.pos

    def open_orders(self) -> list[Order]:
        self._fault("open_orders")
        return [o.view() for o in self.orders.values() if o.status == "OPEN"]

    def get_order(self, order_id: str) -> Order:
        self._fault("get_order")
        if order_id not in self.orders:
            raise ExchangeError(f"unknown order {order_id}")
        return self.orders[order_id].view()

    def find_order(self, client_order_id: str, since_ts: float) -> Order | None:
        self._fault("find_order")
        for o in self.orders.values():
            if o.client_order_id == client_order_id and o.created_ts >= since_ts:
                return o.view()
        return None

    def balance(self) -> Balance:
        self._fault("balance")
        upnl = 0.0
        if self.pos is not None:
            upnl = self.pos.side * (self.fut_price - self.pos.avg_entry) * self.pos.contracts * self.spec.contract_size
        return Balance(self.cash + upnl, None)

    def key_permissions(self) -> KeyPermissions:
        self._fault("key_permissions")
        return self.permissions

    # ------------------------------------------------------------------ order intake (the venue's checks)
    @staticmethod
    def _on_grid(x: float, step: float) -> bool:
        """x is a positive whole multiple of step."""
        x = float(x)
        if not math.isfinite(x) or x <= 0:
            return False
        n = x / step
        return abs(n - round(n)) < 1e-6

    def _rejected(self, size: float, side: int = 0, limit: float | None = None,
                  stop: float | None = None) -> str | None:
        """The venue's failure reason for this order, or None: the size in whole base increments (at least
        one), prices on the tick grid, a bracket's take-profit beyond its stop in the profit direction."""
        if not self._on_grid(size, self.spec.base_increment):
            return "INVALID_SIZE_PRECISION"
        if limit is not None and not self._on_grid(limit, self.spec.price_increment):
            return "INVALID_PRICE_PRECISION"
        if stop is not None and not self._on_grid(stop, self.spec.price_increment):
            return "INVALID_STOP_PRICE_PRECISION"
        if limit is not None and stop is not None and side * (stop - limit) <= 0:
            return "INVALID_BRACKET_PRICES"
        return None

    # ------------------------------------------------------------------ writes
    def _duplicate(self, client_order_id: str) -> PlaceResult | None:
        for o in self.orders.values():
            if o.client_order_id == client_order_id:
                if self.duplicate == "existing":
                    return PlaceResult(client_order_id, o.order_id, True)
                return PlaceResult(client_order_id, None, False, "DUPLICATE_CLIENT_ORDER_ID", duplicate=True)
        return None

    def _new(self, client_order_id: str, side: int, kind: str, size: int, limit: float | None = None,
             stop: float | None = None) -> SimOrder:
        if side not in (1, -1) or size < 1:
            raise ExchangeError(f"invalid order side={side} size={size}")
        self._seq += 1
        o = SimOrder(f"sim-{self._seq}", client_order_id, side, kind, int(size), self.clock(), limit, stop)
        self.orders[o.order_id] = o
        return o

    def place_market(self, client_order_id: str, side: int, size: int) -> PlaceResult:
        self._fault("place_market")
        dup = self._duplicate(client_order_id)
        if dup is not None:
            return dup
        reason = self._rejected(size)
        if reason is not None:
            return PlaceResult(client_order_id, None, False, reason)
        if self.session_state != SESSION_OPEN:
            return PlaceResult(client_order_id, None, False, "SESSION_NOT_OPEN")
        o = self._new(client_order_id, side, "MARKET", size)
        self._execute_market(o)
        self._accepted("place_market")
        return PlaceResult(client_order_id, o.order_id, True)

    def place_bracket(
        self, client_order_id: str, side: int, size: int, limit_price: float, stop_price: float
    ) -> PlaceResult:
        self._fault("place_bracket")
        dup = self._duplicate(client_order_id)
        if dup is not None:
            return dup
        reason = self._rejected(size, side, limit_price, stop_price)
        if reason is not None:
            return PlaceResult(client_order_id, None, False, reason)
        if self.pos is None or self.pos.side != -side:
            return PlaceResult(client_order_id, None, False, "NO_POSITION_TO_REDUCE")
        if self.reject_overlap:
            resting = sum(o.size - o.filled for o in self.orders.values() if o.kind == "BRACKET" and o.status == "OPEN")
            if resting + size > self.pos.contracts:
                return PlaceResult(client_order_id, None, False, "INSUFFICIENT_POSITION_SIZE")
        self._new(client_order_id, side, "BRACKET", size, float(limit_price), float(stop_price))
        self._match()
        self._accepted("place_bracket")
        o = next(x for x in self.orders.values() if x.client_order_id == client_order_id)
        return PlaceResult(client_order_id, o.order_id, True)

    def edit_bracket(self, order_id: str, size: int, limit_price: float, stop_price: float) -> bool:
        self._fault("edit_bracket")
        o = self.orders.get(order_id)
        if not self.edit_supported or o is None or o.status != "OPEN" or o.triggered:
            return False
        if self._rejected(size, o.side, limit_price, stop_price) is not None or int(size) <= o.filled:
            return False  # the total size (filled + left) must leave something to rest
        o.size, o.limit_price, o.stop_price = int(size), float(limit_price), float(stop_price)
        self._match()
        return True

    def cancel(self, order_ids: Sequence[str]) -> None:
        self._fault("cancel")
        refused: list[str] = []
        for oid in order_ids:
            o = self.orders.get(oid)
            if o is not None and o.status == "OPEN":
                o.status = "CANCELLED"
            elif o is None or o.status != "FILLED":
                refused.append(f"{oid}: UNKNOWN_CANCEL_ORDER")
        if refused:
            raise ExchangeError("cancel refused: " + ", ".join(refused))

    def close_position(self, client_order_id: str, size: int | None = None) -> PlaceResult:
        self._fault("close_position")
        dup = self._duplicate(client_order_id)
        if dup is not None:
            return dup
        reason = None if size is None else self._rejected(size)
        if reason is not None:
            return PlaceResult(client_order_id, None, False, reason)
        if self.session_state != SESSION_OPEN:
            return PlaceResult(client_order_id, None, False, "SESSION_NOT_OPEN")
        if self.pos is None:
            return PlaceResult(client_order_id, None, False, "NO_OPEN_POSITION")
        qty = self.pos.contracts if size is None else min(int(size), self.pos.contracts)
        o = self._new(client_order_id, -self.pos.side, "MARKET", qty)
        self._execute_market(o)
        self._accepted("close_position")
        return PlaceResult(client_order_id, o.order_id, True)

    def preview_market(self, side: int, size: int) -> PreviewResult:
        self._fault("preview_market")
        return self._preview_result(self._rejected(size))

    def preview_bracket(self, side: int, size: int, limit_price: float, stop_price: float) -> PreviewResult:
        self._fault("preview_bracket")
        return self._preview_result(self._rejected(size, side, limit_price, stop_price))

    def _preview_result(self, reason: str | None) -> PreviewResult:
        errors = self.preview_errors + ((f"PREVIEW_{reason}",) if reason else ())
        return PreviewResult(not errors, errors)

    # ------------------------------------------------------------------ matching
    def _execute_market(self, o: SimOrder) -> None:
        """IOC at the touch: market_fill_ratio of the size fills, the unfilled rest is cancelled; a bracket
        left with nothing to reduce is cancelled at once."""
        qty = int(math.floor(o.size * self.market_fill_ratio + 1e-9))
        if qty > 0:
            self._fill(o, qty, self.fut_price + o.side * self.spread / 2.0)
        o.status = "FILLED" if o.filled == o.size else "CANCELLED"
        self._match()

    def _fill(self, o: SimOrder, qty: int, price: float) -> None:
        cs = self.spec.contract_size
        pos = self.pos
        if pos is None:
            self.pos = Position(o.side, qty, price)
        elif pos.side == o.side:
            n = pos.contracts + qty
            self.pos = Position(pos.side, n, (pos.avg_entry * pos.contracts + price * qty) / n)
        else:
            closed = min(qty, pos.contracts)
            self.cash += pos.side * (price - pos.avg_entry) * closed * cs
            left = pos.contracts - closed
            rest = qty - closed
            if left > 0:
                self.pos = Position(pos.side, left, pos.avg_entry)
            elif rest > 0:
                self.pos = Position(o.side, rest, price)
            else:
                self.pos = None
        fee = self.fee_per_contract * qty
        self.cash -= fee
        o.fees += fee
        o.filled += qty
        o.fill_value += price * qty
        self.fills.append({"order_id": o.order_id, "client_order_id": o.client_order_id, "kind": o.kind,
                           "side": o.side, "size": qty, "price": price, "fee": fee, "ts": self.clock()})

    def _match(self) -> list[dict]:
        if self.session_state != SESSION_OPEN:
            return []
        start = len(self.fills)
        last = self.fut_price
        for o in list(self.orders.values()):
            if o.kind != "BRACKET" or o.status != "OPEN":
                continue
            pos = self.pos
            if pos is None or pos.side == o.side:  # reduce-only: nothing (left) to reduce
                o.status = "CANCELLED"
                continue
            d = pos.side
            qty = min(o.size - o.filled, pos.contracts)
            if self.bracket_fill_cap is not None:
                qty = min(qty, self.bracket_fill_cap)
            if not o.triggered and o.limit_price is not None and d * (last - o.limit_price) >= 0:
                self._fill(o, qty, o.limit_price)
            else:
                if not o.triggered and o.stop_price is not None and d * (last - o.stop_price) <= 0:
                    o.triggered = True
                if o.triggered and o.stop_price is not None:
                    limit = o.stop_price * (1.0 - d * STOP_LIMIT_OFFSET)
                    if d * (last - limit) >= 0:
                        self._fill(o, qty, last)
            if o.filled >= o.size:
                o.status = "FILLED"
        return self.fills[start:]
