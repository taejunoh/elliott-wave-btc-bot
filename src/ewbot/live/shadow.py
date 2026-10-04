"""SHADOW stage exchange (Plan 3 spec 4.3 `SHADOW`): no order ever reaches Coinbase.

Reads - product, session, quotes, balance, key permissions - come from the real exchange. Every order
the runtime would place is first checked with the real POST /orders/preview (journaled as a PREVIEW
event: SHADOW pass criterion ③) and then executed on the embedded SimExchange, whose book follows the
real futures price: market orders fill at the real bid/ask, brackets rest virtually and fill when the
real price reaches them. Position, open orders and order lookups come from that virtual book, so the
whole runtime - entries, protection, stop moves, closes, the watchdog - runs exactly as in CANARY. On a
restart `restore` puts the open virtual position back from the journal."""

from __future__ import annotations

import time
from typing import Any, Callable

from ewbot.live.exchange import (
    Balance,
    Exchange,
    ExchangeError,
    KeyPermissions,
    PlaceResult,
    Position,
    PreviewResult,
    ProductSpec,
    Quote,
    SessionInfo,
)
from ewbot.live.journal import Journal
from ewbot.live.reconcile import LiveTrade, expected_contracts
from ewbot.live.sim import SimExchange


class ShadowExchange(SimExchange):
    # these reads go to the real exchange (no _fault): inject their faults there, `real.fail_next(...)`
    FAULT_METHODS = SimExchange.FAULT_METHODS - {"product", "session", "quote", "balance", "key_permissions"}

    def __init__(self, real: Exchange, journal: Journal, *, clock: Callable[[], float] = time.time,
                 alert: Callable[[str, str], Any] | None = None) -> None:
        super().__init__(real.product(), equity=0.0, clock=clock, spot_id=real.spot_id)
        self.real = real
        self.journal = journal
        self.alert = alert or (lambda kind, text: None)

    def restore(self, trade: LiveTrade | None) -> None:
        """After a restart the virtual book is empty: put back the open virtual position from the journal's
        trade (its brackets are re-placed by the next reconcile), so a restart does not close it."""
        if trade is None or trade.status != "OPEN":
            return
        left = expected_contracts(trade)
        if left > 0:
            self.pos = Position(trade.direction, left, trade.entry_price)

    # ------------------------------------------------------------------ real reads
    def product(self) -> ProductSpec:
        return self.real.product()

    def session(self) -> SessionInfo:
        info = self.real.session()
        self.set_session(info.state)
        return info

    def quote(self, product_id: str) -> Quote:
        q = self.real.quote(product_id)
        if product_id == self.futures_id:
            self.spread = max(q.ask - q.bid, 0.0)
            self.set_prices(q.mid)
        elif product_id == self.spot_id:
            self.spot_price = q.mid
        return q

    def balance(self) -> Balance:
        return self.real.balance()  # sizing and the 2% check use the real account

    def key_permissions(self) -> KeyPermissions:
        return self.real.key_permissions()

    # ------------------------------------------------------------------ previewed virtual orders
    def _preview(self, kind: str, client_order_id: str, call: Callable[[], PreviewResult], **order: Any) -> None:
        try:
            res = call()
        except ExchangeError as exc:
            res = PreviewResult(False, (f"preview request failed: {exc}"[:200],))
        self.journal.add_event(self.clock(), "PREVIEW", {"kind": kind, "client_order_id": client_order_id,
                                                         "ok": res.ok, "errors": list(res.errors),
                                                         "warnings": list(res.warnings), **order})
        if not res.ok:
            self.alert("PREVIEW_FAILED", f"[SHADOW preview 실패] {kind} {client_order_id}: {', '.join(res.errors)}")

    def _refresh(self) -> None:
        self.quote(self.futures_id)

    def place_market(self, client_order_id: str, side: int, size: int) -> PlaceResult:
        self._preview("MARKET", client_order_id, lambda: self.real.preview_market(side, size), side=side, size=size)
        self._refresh()
        return super().place_market(client_order_id, side, size)

    def place_bracket(self, client_order_id: str, side: int, size: int, limit_price: float,
                      stop_price: float) -> PlaceResult:
        self._preview("BRACKET", client_order_id,
                      lambda: self.real.preview_bracket(side, size, limit_price, stop_price),
                      side=side, size=size, limit_price=limit_price, stop_price=stop_price)
        return super().place_bracket(client_order_id, side, size, limit_price, stop_price)

    def close_position(self, client_order_id: str, size: int | None = None) -> PlaceResult:
        pos = self.pos
        if pos is not None:
            qty = pos.contracts if size is None else min(int(size), pos.contracts)
            self._preview("CLOSE", client_order_id, lambda: self.real.preview_market(-pos.side, qty),
                          side=-pos.side, size=qty)
            self._refresh()
        return super().close_position(client_order_id, size)
