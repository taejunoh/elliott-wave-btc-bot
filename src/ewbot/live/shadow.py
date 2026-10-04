"""SHADOW stage exchange (Plan 3 spec 4.3 `SHADOW`): no order ever reaches Coinbase.

Reads - product, session, quotes, balance, key permissions - come from the real exchange. Every order
the runtime would place is first checked with the real POST /orders/preview (journaled as a PREVIEW
event: SHADOW pass criterion ③) and then executed on the embedded SimExchange, whose book follows the
real futures price: market orders fill at the real bid/ask, brackets rest virtually and fill when the
real price reaches them. Position, open orders and order lookups come from that virtual book, so the
whole runtime - entries, protection, stop moves, closes, the watchdog - runs exactly as in CANARY.

A preview the API could not answer (an ExchangeError, after one more try for a passing one) is no refusal: it
is journaled as PREVIEW_UNVERIFIED, alerted, and asked again by `retry_previews` (the runtime calls it after
every decision cycle) until Coinbase answers - only an answer with errors is a failed PREVIEW; one still
unanswered after PREVIEW_RETRY_LIMIT cycles is recorded failed (never verified).

On a restart `restore` puts the open virtual position back from the journal and reads the real quote first
(best effort; so does every bracket placement): the virtual book never triggers or fills while it has no
real futures price above 0. Virtual order ids carry a prefix new to every process (`v<n>-`, n counted in
the journal), so an id never repeats across restarts. The book stays bounded over a long SHADOW: orders that
ended more than PRUNE_AFTER_S ago, fills beyond FILLS_KEPT and calls beyond CALLS_KEPT are dropped."""

from __future__ import annotations

import dataclasses
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
    Unavailable,
)
from ewbot.live.journal import Journal
from ewbot.live.reconcile import LiveTrade, expected_contracts
from ewbot.live.sim import BIP_SPEC, SimExchange, SimOrder

BOOK_KEY = "shadow_book"  # how many virtual books (processes) the journal has seen: the order id prefix
UNVERIFIED_KEY = "shadow_unverified"  # previews the API could not answer yet, asked again every cycle
PREVIEW_TRIES = 2  # a preview that fails for a passing reason (429, 5xx, timeout) is tried once more at once
PREVIEW_RETRY_LIMIT = 24  # cycles (hours) an unanswered preview is asked again before it counts as failed
PRUNE_AFTER_S = 86_400.0  # a virtual order that ended this long ago is dropped (lookups need minutes, not days)
FILLS_KEPT = 1000
CALLS_KEPT = 1000


class ShadowExchange(SimExchange):
    # these reads go to the real exchange (no _fault): inject their faults there, `real.fail_next(...)`
    FAULT_METHODS = SimExchange.FAULT_METHODS - {"product", "session", "quote", "balance", "key_permissions"}

    def __init__(self, real: Exchange, journal: Journal, *, clock: Callable[[], float] = time.time,
                 alert: Callable[[str, str], Any] | None = None) -> None:
        try:
            spec = real.product()
        except ExchangeError:  # the runtime's startup reads it again (retried) and sets it
            spec = dataclasses.replace(BIP_SPEC, product_id=real.futures_id)
        super().__init__(spec, equity=0.0, clock=clock, spot_id=real.spot_id)
        self.real = real
        self.journal = journal
        self.alert = alert or (lambda kind, text: None)
        self.book = int(journal.get(BOOK_KEY, 0) or 0) + 1
        journal.put(BOOK_KEY, self.book)
        self._ended: dict[str, float] = {}  # virtual order id -> when it was first seen ended (pruning)

    def restore(self, trade: LiveTrade | None) -> None:
        """After a restart the virtual book is empty: put back the open virtual position from the journal's
        trade (its brackets are re-placed by the next reconcile), so a restart does not close it. The real
        futures price is read first (best effort): nothing triggers before the book has one."""
        self._refresh(best_effort=True)
        if trade is None or trade.status != "OPEN":
            return
        left = expected_contracts(trade)
        if left > 0:
            self.pos = Position(trade.direction, left, trade.entry_price)

    # ------------------------------------------------------------------ real reads
    def product(self) -> ProductSpec:
        self.spec = self.real.product()
        return self.spec

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

    # ------------------------------------------------------------------ the virtual book
    def _match(self) -> list[dict]:
        if not self.fut_price > 0:  # no real futures price yet (a restart before the first quote): nothing moves
            return []
        return super()._match()

    def _fault(self, method: str) -> None:
        if len(self.calls) >= 2 * CALLS_KEPT:
            del self.calls[:-CALLS_KEPT]
        super()._fault(method)

    def _new(self, client_order_id: str, side: int, kind: str, size: int, limit: float | None = None,
             stop: float | None = None) -> SimOrder:
        self._prune()
        o = super()._new(client_order_id, side, kind, size, limit, stop)
        del self.orders[o.order_id]
        o.order_id = f"v{self.book}-{o.order_id}"  # never an id an earlier process recorded
        self.orders[o.order_id] = o
        return o

    def _prune(self) -> None:
        now = self.clock()
        for oid, o in list(self.orders.items()):
            if o.status == "OPEN":
                continue
            if now - self._ended.setdefault(oid, now) > PRUNE_AFTER_S:
                del self.orders[oid], self._ended[oid]
        if len(self.fills) > FILLS_KEPT:
            del self.fills[:-FILLS_KEPT]

    def _refresh(self, best_effort: bool = False) -> None:
        """The real futures quote into the virtual book. A market order needs it (Unavailable without a price
        above 0); a bracket or a restore reads it best effort (a resting bracket waits for a price)."""
        try:
            self.quote(self.futures_id)
        except ExchangeError:
            if not best_effort:
                raise
        if not best_effort and not self.fut_price > 0:
            raise Unavailable("no real futures price for the virtual fill")

    # ------------------------------------------------------------------ previewed virtual orders
    def _ask(self, kind: str, order: dict[str, Any]) -> PreviewResult:
        side, size = int(order["side"]), int(order["size"])
        if kind == "BRACKET":
            return self.real.preview_bracket(side, size, float(order["limit_price"]), float(order["stop_price"]))
        return self.real.preview_market(side, size)

    def _preview(self, kind: str, client_order_id: str, **order: Any) -> None:
        for attempt in range(PREVIEW_TRIES):
            try:
                res = self._ask(kind, order)
                break
            except ExchangeError as exc:
                if exc.retryable and attempt + 1 < PREVIEW_TRIES:
                    continue
                self._unverified(kind, client_order_id, order, exc)
                return
        self._verdict(kind, client_order_id, res, order)

    def _verdict(self, kind: str, client_order_id: str, res: PreviewResult, order: dict[str, Any],
                 **extra: Any) -> None:
        self.journal.add_event(self.clock(), "PREVIEW", {"kind": kind, "client_order_id": client_order_id,
                                                         "ok": res.ok, "errors": list(res.errors),
                                                         "warnings": list(res.warnings), **order, **extra})
        if not res.ok:
            self.alert("PREVIEW_FAILED", f"[SHADOW preview 실패] {kind} {client_order_id}: {', '.join(res.errors)}")

    def _unverified(self, kind: str, client_order_id: str, order: dict[str, Any], exc: ExchangeError) -> None:
        """The API did not answer: no verdict on the order (gate ③ neither passes nor fails on it yet)."""
        why = f"{type(exc).__name__}: {exc}"[:200]
        items = list(self.journal.get(UNVERIFIED_KEY) or [])
        items.append({"kind": kind, "client_order_id": client_order_id, "order": order, "tries": 0,
                      "ts": self.clock()})
        self.journal.put(UNVERIFIED_KEY, items[-200:])
        self.journal.add_event(self.clock(), "PREVIEW_UNVERIFIED", {"kind": kind, "client_order_id": client_order_id,
                                                                    "error": why, **order})
        self.alert("PREVIEW_UNVERIFIED", f"[SHADOW preview 미확인] {kind} {client_order_id}: API 오류 ({why}) - "
                                         "다음 사이클에 다시 확인 (거부가 아님: 기준 ③ 실패 아님)")

    def retry_previews(self) -> int:
        """Ask the API again for every unanswered preview (the runtime calls this after each cycle): an answer
        is its PREVIEW event (`retried`), an error keeps it for the next cycle, and PREVIEW_RETRY_LIMIT
        unanswered cycles record it failed. Returns how many are still unanswered."""
        items = list(self.journal.get(UNVERIFIED_KEY) or [])
        if not items:
            return 0
        left: list[dict[str, Any]] = []
        for it in items:
            try:
                res = self._ask(it["kind"], it["order"])
            except ExchangeError as exc:
                it = {**it, "tries": int(it["tries"]) + 1}
                if it["tries"] < PREVIEW_RETRY_LIMIT:
                    left.append(it)
                    continue
                res = PreviewResult(False, (f"PREVIEW_UNVERIFIED: no answer in {PREVIEW_RETRY_LIMIT} cycles "
                                            f"({type(exc).__name__})",))
            self._verdict(it["kind"], it["client_order_id"], res, it["order"], retried=True)
        self.journal.put(UNVERIFIED_KEY, left)
        return len(left)

    def place_market(self, client_order_id: str, side: int, size: int) -> PlaceResult:
        self._preview("MARKET", client_order_id, side=side, size=size)
        self._refresh()
        return super().place_market(client_order_id, side, size)

    def place_bracket(self, client_order_id: str, side: int, size: int, limit_price: float,
                      stop_price: float) -> PlaceResult:
        self._preview("BRACKET", client_order_id, side=side, size=size, limit_price=limit_price,
                      stop_price=stop_price)
        self._refresh(best_effort=True)
        return super().place_bracket(client_order_id, side, size, limit_price, stop_price)

    def close_position(self, client_order_id: str, size: int | None = None) -> PlaceResult:
        pos = self.pos
        if pos is not None:
            qty = pos.contracts if size is None else min(int(size), pos.contracts)
            self._preview("CLOSE", client_order_id, side=-pos.side, size=qty)
            self._refresh()
        return super().close_position(client_order_id, size)
