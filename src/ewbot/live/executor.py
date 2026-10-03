"""Executes reconcile actions against the exchange with intent-before-send (Plan 3 spec 4.2, 5.1).

Every order goes through `send`: the intent (deterministic client_order_id, kind, payload) is committed
to the journal as PENDING before the request; the answer marks it ACCEPTED or REJECTED. An ambiguous
failure (429 / 5xx / timeout / process killed) leaves it PENDING, and a PENDING intent is looked up on
the exchange by client_order_id before it is ever sent again - so a restart never doubles an order.

Entries are market IOC orders; the fill is awaited (10 s) and the trade is protected at once with
trigger_bracket_gtc legs. Stop moves edit the resting bracket when `edit_stops` is on (unconfirmed for
CFM brackets: off until CANARY confirms it), otherwise the new bracket is placed first and the old
one cancelled (brackets only reduce, so a short overlap cannot over-close); if the exchange refuses the
overlap, the old one is cancelled first and the gap is tracked as unprotected time. A leg that left the
book is looked up before it is placed again: one with fills is not placed again for LEG_SETTLE_S (the
position read may not show the fill yet, and a take-profit placed again below the market would sell at
once what the leg already sold) - the next observe folds the fill. Closes cancel our brackets (a refused
cancel is recorded, CANCEL_FAILED, and never blocks the close) and call close_position; the flat
position is awaited (30 s). Reductions (close_position with a size) are recorded on the trade before they
are sent (`LiveTrade.pending_reduce`): a reduce with an unknown outcome is resolved by its client order
id (`resolve_reduce`, before every read of the exchange state) and never followed by a second one, and a
Protect planned with a reduce waits until the reduce is known to have filled. Market orders wait for an
OPEN session; a deferred close is retried on every pass (spec 4.5: `휴장 중에 필요해진 청산은 재개장 직후`).

The same code runs every stage: in SHADOW the exchange is a ShadowExchange (virtual fills, real
previews)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from ewbot.engine.position_mgr import _tp1_split
from ewbot.live.exchange import Exchange, ExchangeError, Order, PlaceResult
from ewbot.live.journal import Journal
from ewbot.live.pricing import map_levels
from ewbot.live.reconcile import (
    Action,
    Adopt,
    Alert,
    Cancel,
    Close,
    Enter,
    Leg,
    LiveTrade,
    PlanContext,
    Protect,
    Reduce,
    Skip,
    after_reduce,
    bracket_cid,
    close_cid,
    desired_legs,
    entry_cid,
    expected_contracts,
    is_ours,
    reduce_cid,
)
from ewbot.live.target import TargetPosition

FILL_TIMEOUT_S = 10.0
FLAT_TIMEOUT_S = 30.0
POLL_S = 0.5
LOOKUP_WINDOW_S = 300.0  # find_order searches orders created up to 5 min before the intent
ABANDON_AFTER_S = 600.0  # a PENDING intent not found on the exchange after 10 min was never sent
LEG_SETTLE_S = 30.0  # a leg that left the book with fills is not placed again for this long (the position catches up)
TRADE_KEY = "trade"
SKIPS_KEY = "skipped_signals"
ORPHAN_KEY = "orphan_closes"

Alerter = Callable[[str, str], None]


@dataclass(frozen=True)
class Sent:
    status: str  # ACCEPTED | REJECTED | PENDING
    order_id: str | None = None
    error: str | None = None


def _side(d: int) -> str:
    return "롱" if d > 0 else "숏"


class Executor:
    def __init__(
        self,
        exchange: Exchange,
        journal: Journal,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        alert: Alerter | None = None,
        tp1_frac: float = 0.5,
        edit_stops: bool = False,
        fill_timeout: float = FILL_TIMEOUT_S,
        flat_timeout: float = FLAT_TIMEOUT_S,
        poll_s: float = POLL_S,
    ) -> None:
        self.ex = exchange
        self.journal = journal
        self.clock = clock
        self.sleep = sleep
        self.alert = alert or (lambda kind, text: None)
        self.tp1_frac = tp1_frac
        self.edit_stops = edit_stops
        self.fill_timeout = fill_timeout
        self.flat_timeout = flat_timeout
        self.poll_s = poll_s

    # ------------------------------------------------------------------ state
    def trade(self) -> LiveTrade | None:
        data = self.journal.get(TRADE_KEY)
        return None if data is None else LiveTrade.from_json(data)

    def save(self, trade: LiveTrade | None) -> None:
        self.journal.put(TRADE_KEY, None if trade is None else trade.to_json())

    def event(self, kind: str, data: dict[str, Any], text: str | None = None) -> None:
        self.journal.add_event(self.clock(), kind, data)
        if text is not None:
            self.alert(kind, text)

    # ------------------------------------------------------------------ sending
    def _lookup(self, client_order_id: str, created_ts: float) -> tuple[Order | None, bool]:
        """(order, known): known False when the lookup itself failed (outcome still unknown)."""
        try:
            return self.ex.find_order(client_order_id, created_ts - LOOKUP_WINDOW_S), True
        except ExchangeError:
            return None, False

    def send(self, cid: str, kind: str, trade_id: int | None, payload: dict[str, Any],
             call: Callable[[], PlaceResult]) -> Sent:
        now = self.clock()
        row, created = self.journal.record_intent(cid, kind, trade_id, payload, now)
        if not created:
            if row["status"] == "ACCEPTED":
                return Sent("ACCEPTED", row["order_id"])
            if row["status"] in ("REJECTED", "ABANDONED"):
                return Sent("REJECTED", None, row["note"])
            found, known = self._lookup(cid, row["created_ts"])
            if not known:
                return Sent("PENDING", None, "lookup failed")
            if found is not None:
                self.journal.update_intent(cid, "ACCEPTED", self.clock(), order_id=found.order_id, note="found by lookup")
                return Sent("ACCEPTED", found.order_id)
        try:
            res = call()
        except ExchangeError as exc:
            if not exc.retryable:
                self.journal.update_intent(cid, "REJECTED", self.clock(), note=str(exc)[:200])
                return Sent("REJECTED", None, str(exc))
            found, known = self._lookup(cid, row["created_ts"])
            if found is not None:
                self.journal.update_intent(cid, "ACCEPTED", self.clock(), order_id=found.order_id, note="found after error")
                return Sent("ACCEPTED", found.order_id)
            return Sent("PENDING", None, f"{type(exc).__name__}: {exc}")
        if res.ok:
            self.journal.update_intent(cid, "ACCEPTED", self.clock(), order_id=res.order_id)
            return Sent("ACCEPTED", res.order_id)
        if res.duplicate:
            found, _ = self._lookup(cid, row["created_ts"])
            if found is not None:
                self.journal.update_intent(cid, "ACCEPTED", self.clock(), order_id=found.order_id, note="duplicate id")
                return Sent("ACCEPTED", found.order_id)
            return Sent("PENDING", None, res.error)
        self.journal.update_intent(cid, "REJECTED", self.clock(), note=res.error)
        return Sent("REJECTED", None, res.error)

    def _await_fill(self, order_id: str) -> Order:
        """The order once terminal (or at the deadline); read errors are retried until the deadline."""
        deadline = self.clock() + self.fill_timeout
        while True:
            try:
                o = self.ex.get_order(order_id)
                if o.is_terminal or self.clock() >= deadline:
                    return o
            except ExchangeError:
                if self.clock() >= deadline:
                    raise
            self.sleep(self.poll_s)

    def _cancel_quietly(self, order_ids: list[str], trade_id: int) -> None:
        """A failed cancel of a replaced leg is harmless (brackets only reduce): the next protect() cancels
        it as a stale order of ours."""
        try:
            self.ex.cancel(order_ids)
        except ExchangeError as exc:
            self.event("CANCEL_FAILED", {"trade_id": trade_id, "order_ids": order_ids, "error": str(exc)[:200]})

    def _await_flat(self) -> bool:
        deadline = self.clock() + self.flat_timeout
        while True:
            if self.ex.position() is None:
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(self.poll_s)

    # ------------------------------------------------------------------ actions
    def execute(self, actions: Sequence[Action], ctx: PlanContext) -> None:
        closed = True
        reduced = True  # a Protect planned with a Reduce assumes it: kept back while the reduce is not known filled
        for a in actions:
            try:
                if isinstance(a, Alert):
                    self._alert_once(a.kind, a.detail)
                elif isinstance(a, Skip):
                    self._skip(a)
                elif isinstance(a, Cancel):
                    self.ex.cancel(list(a.order_ids))
                    self.event("CANCELLED", {"order_ids": list(a.order_ids), "reason": a.reason})
                elif isinstance(a, Close):
                    closed = self.close(a.reason, ctx)
                elif isinstance(a, Reduce):
                    reduced = self.reduce(a.size, a.reason, ctx)
                elif isinstance(a, Protect):
                    if reduced:
                        self.protect(a, ctx)
                elif isinstance(a, Adopt):
                    self.adopt(a.target, ctx, a.contracts)
                elif isinstance(a, Enter):
                    if closed:
                        self.enter(a, ctx)
                    else:
                        self._skip(Skip(a.entry.signal_ts, "the previous position is not closed yet (flip)"))
            except ExchangeError as exc:
                self.event("ACTION_ERROR", {"action": type(a).__name__, "error": str(exc)[:200]},
                           f"[주문 오류] {type(a).__name__}: {str(exc)[:200]}")
                if isinstance(a, Close):
                    closed = False
                elif isinstance(a, Reduce):
                    reduced = False

    def _alert_once(self, kind: str, detail: str) -> None:
        key = f"alerted:{kind}:{detail}"
        if not self.journal.get(key, False):
            self.journal.put(key, True)
            self.event(kind, {"detail": detail}, f"[경고] {kind}: {detail}")

    def _skip(self, a: Skip) -> None:
        done = self.journal.get(SKIPS_KEY, [])
        if a.signal_ts in done:
            return
        self.journal.put(SKIPS_KEY, (done + [a.signal_ts])[-200:])
        self.event("SKIP", {"signal_ts": a.signal_ts, "reason": a.reason}, f"[진입 건너뜀] 신호 {a.signal_ts}: {a.reason}")

    # entry -----------------------------------------------------------------
    def enter(self, a: Enter, ctx: PlanContext) -> LiveTrade | None:
        e = a.entry
        cid = entry_cid(e.signal_ts, e.direction)
        row = self.journal.intent(cid)
        if row is not None and row["status"] != "PENDING":
            return None  # already entered or refused once: an entry is never sent twice
        if ctx.ratio is None:
            self._skip(Skip(e.signal_ts, "no futures/spot quotes"))
            return None
        lv = map_levels(e.direction, e.stop, e.tp1, e.tp2, ctx.ratio, ctx.tick)
        payload = {"signal_ts": e.signal_ts, "setup": e.setup, "direction": e.direction, "contracts": a.contracts,
                   "stop_spot": e.stop, "tp1_spot": e.tp1, "tp2_spot": e.tp2, "stop": lv.stop, "tp1": lv.tp1,
                   "tp2": lv.tp2, "ratio": ctx.ratio}
        sent = self.send(cid, "ENTRY", e.signal_ts, payload, lambda: self.ex.place_market(cid, e.direction, a.contracts))
        if sent.status != "ACCEPTED":
            self.event("ENTRY_FAILED", {"signal_ts": e.signal_ts, "status": sent.status, "error": sent.error},
                       f"[진입 실패] {e.setup} {_side(e.direction)} {a.contracts}계약: {sent.status} {sent.error}")
            return None
        try:
            order = self._await_fill(str(sent.order_id))
        except ExchangeError as exc:  # accepted but unreadable: the next pass rebuilds it from the intent
            self.event("ENTRY_UNCONFIRMED", {"signal_ts": e.signal_ts, "error": str(exc)[:200]})
            return None
        if order.filled == 0:
            self.event("ENTRY_UNFILLED", {"signal_ts": e.signal_ts}, f"[진입 미체결] {e.setup} {_side(e.direction)}")
            return None
        trade = self._trade_from(payload, order)
        self.save(trade)
        self.event("ENTRY", {"trade_id": trade.trade_id, "side": e.direction, "contracts": trade.contracts,
                             "price": trade.entry_price, "ref": e.entry_ref * ctx.ratio, "fee": order.fee,
                             "order_id": order.order_id},
                   f"[진입] {e.setup} {_side(e.direction)} {trade.contracts}계약 @ {trade.entry_price:,.1f} · 손절 "
                   f"{trade.stop_price:,.0f} · TP1 {trade.tp1_price:,.0f} · TP2 {trade.tp2_price or 0:,.0f}")
        legs, stop = desired_legs(trade, trade.contracts, trade.stop_spot, ctx.ratio, ctx.tick)
        self.protect(Protect(tuple(legs), trade.stop_spot, stop), ctx)
        return self.trade()

    def _trade_from(self, payload: dict[str, Any], order: Order) -> LiveTrade:
        n = int(order.filled)
        return LiveTrade(
            trade_id=int(payload["signal_ts"]), setup=str(payload["setup"]), direction=int(payload["direction"]),
            contracts=n, tp1_n=_tp1_split(n, self.tp1_frac), entry_price=float(order.avg_fill_price or 0.0),
            tp1_price=float(payload["tp1"]), tp2_price=None if payload["tp2"] is None else float(payload["tp2"]),
            stop_spot=float(payload["stop_spot"]), stop_price=float(payload["stop"]), opened_ts=self.clock(),
        )

    def adopt(self, tp: TargetPosition, ctx: PlanContext, contracts: int | None = None) -> None:
        """Take over a position the trade state does not know: at most `contracts` (default: the whole
        position) and never above the stage maximum `ctx.max_contracts`; the planner reduces the rest
        (Reduce EXCESS). Our own accepted entry for that signal (its fill could not be read in time) is
        rebuilt from its intent; anything else is a foreign position (event ADOPTED: manual intervention).
        A position the engine already took TP1 on is the rest after TP1 (break-even, no TP1 portion)."""
        pos = self.ex.position()
        if pos is None or ctx.ratio is None:
            return
        own = self.journal.intent(entry_cid(tp.signal_ts, tp.direction))
        if own is not None and own["status"] == "ACCEPTED" and own["order_id"]:
            order = self.ex.get_order(str(own["order_id"]))
            if order.filled > 0:
                trade = self._trade_from(own["payload"], order)
                self.save(trade)
                self.event("RECOVERED_ENTRY", {"trade_id": trade.trade_id},
                           f"[복구] 진입 {trade.trade_id} {_side(trade.direction)} {trade.contracts}계약 복원")
                size = min(expected_contracts(trade), pos.contracts)
                legs, stop = desired_legs(trade, size, trade.stop_spot, ctx.ratio, ctx.tick)
                self.protect(Protect(tuple(legs), trade.stop_spot, stop), ctx)
                return
        n = pos.contracts if contracts is None else min(contracts, pos.contracts)
        if ctx.max_contracts is not None:
            n = min(n, ctx.max_contracts)
        after_tp1 = tp.tp1_done and n > 1
        lv = map_levels(tp.direction, tp.stop, tp.tp1, tp.tp2, ctx.ratio, ctx.tick)
        trade = LiveTrade(
            trade_id=tp.signal_ts, setup=tp.setup, direction=tp.direction, contracts=n,
            tp1_n=0 if after_tp1 else _tp1_split(n, self.tp1_frac), entry_price=pos.avg_entry, tp1_price=lv.tp1,
            tp2_price=lv.tp2, stop_spot=tp.stop, stop_price=lv.stop, opened_ts=self.clock(), tp1_filled=after_tp1,
        )
        self.save(trade)
        self.event("ADOPTED", {"trade_id": trade.trade_id, "contracts": n, "position": pos.contracts},
                   f"[경고] 저널에 없던 포지션 {_side(pos.side)} {pos.contracts}계약 중 {n}계약을 엔진 거래 "
                   f"{tp.signal_ts}로 인수")
        legs, stop = desired_legs(trade, n, trade.stop_spot, ctx.ratio, ctx.tick)
        self.protect(Protect(tuple(legs), trade.stop_spot, stop), ctx)

    # protection --------------------------------------------------------------
    def _place_leg(self, trade: LiveTrade, want: Leg) -> Leg | None:
        trade.gen += 1
        cid = bracket_cid(trade.trade_id, want.name, trade.gen)
        leg = Leg(want.name, want.size, want.limit_price, want.stop_price, cid, None)
        self.save(trade)  # the generation is persisted before the request
        sent = self.send(cid, "BRACKET", trade.trade_id,
                         {"leg": want.name, "size": want.size, "limit": want.limit_price, "stop": want.stop_price},
                         lambda: self.ex.place_bracket(cid, -trade.direction, want.size, want.limit_price, want.stop_price))
        if sent.status == "ACCEPTED":
            leg.order_id = sent.order_id
            return leg
        self.event("BRACKET_FAILED", {"trade_id": trade.trade_id, "leg": want.name, "status": sent.status,
                                      "error": sent.error},
                   f"[보호 주문 실패] {want.name} {want.size}계약 손절 {want.stop_price:,.0f}: {sent.status} {sent.error}")
        return None

    def protect(self, a: Protect, ctx: PlanContext) -> None:
        trade = self.trade()
        if trade is None or trade.status != "OPEN":
            return
        orders = {o.order_id: o for o in self.ex.open_orders()}
        kept: list[Leg] = []
        replaced: set[str] = set()  # cancelled below already: not cancelled again as stale
        for want in a.legs:
            cur = next((leg for leg in trade.legs if leg.name == want.name and leg.order_id in orders), None)
            gone = next((leg for leg in trade.legs if leg.name == want.name and leg.order_id), None)
            if cur is None and gone is not None and self._left_with_fills(trade, gone):
                kept.append(gone)  # not placed again: the next observe folds its fill (or it is placed later)
                continue
            if cur is not None:
                o = orders[str(cur.order_id)]
                if (o.size - o.filled, o.limit_price, o.stop_price) == (want.size, want.limit_price, want.stop_price):
                    kept.append(cur)
                    continue
                if (self.edit_stops and (o.size - o.filled, o.limit_price) == (want.size, want.limit_price)
                        and self.ex.edit_bracket(str(cur.order_id), want.size, want.limit_price, want.stop_price)):
                    kept.append(Leg(cur.name, want.size, want.limit_price, want.stop_price, cur.client_order_id, cur.order_id))
                    self.event("STOP_EDITED", {"trade_id": trade.trade_id, "leg": want.name, "stop": want.stop_price})
                    continue
            placed = self._place_leg(trade, want)
            if placed is None and cur is not None:  # the venue refused the overlap: cancel first, then place
                self.ex.cancel([str(cur.order_id)])
                replaced.add(str(cur.order_id))
                self.event("LEG_GAP", {"trade_id": trade.trade_id, "leg": want.name})
                cur = None
                placed = self._place_leg(trade, want)
            if placed is not None:
                if cur is not None:
                    self._cancel_quietly([str(cur.order_id)], trade.trade_id)
                    replaced.add(str(cur.order_id))
                kept.append(placed)
            elif cur is not None:
                kept.append(cur)
        keep = {leg.order_id for leg in kept}
        stale = [oid for oid, o in orders.items()
                 if is_ours(o) and o.kind == "BRACKET" and oid not in keep and oid not in replaced]
        if stale:
            self._cancel_quietly(stale, trade.trade_id)
        moved = a.stop_price != trade.stop_price
        trade.legs, trade.stop_spot, trade.stop_price = kept, a.stop_spot, a.stop_price
        if trade.protected_ts is None and kept and len(kept) == len(a.legs):
            trade.protected_ts = self.clock()
            self.event("PROTECTED_FIRST", {"trade_id": trade.trade_id,
                                           "after_entry_s": round(trade.protected_ts - trade.opened_ts, 3)})
        elif moved:
            self.event("STOP_MOVED", {"trade_id": trade.trade_id, "stop": a.stop_price, "stop_spot": a.stop_spot},
                       f"[손절 이동] {trade.setup} {_side(trade.direction)} → {a.stop_price:,.0f}")
        self.save(trade)

    def _left_with_fills(self, trade: LiveTrade, leg: Leg) -> bool:
        """A leg of ours that left the book: True while it has fills the position read may not show yet - for
        LEG_SETTLE_S from when it was first found so (then it is placed again if still wanted). A leg that cannot
        be read is placed again (protection first)."""
        key = f"leg_filled:{leg.order_id}"
        first = self.journal.get(key)
        if first is not None:
            return self.clock() - float(first) < LEG_SETTLE_S
        try:
            o = self.ex.get_order(str(leg.order_id))
        except ExchangeError:
            return False
        if o.filled <= 0:
            return False
        self.journal.put(key, self.clock())
        self.event("LEG_FILLED", {"trade_id": trade.trade_id, "leg": leg.name, "order_id": leg.order_id,
                                  "filled": o.filled, "status": o.status})
        return True

    # exits -------------------------------------------------------------------
    def close(self, reason: str, ctx: PlanContext) -> bool:
        trade = self.trade()
        if self.ex.position() is None:
            if trade is not None and trade.status == "OPEN":
                trade.status, trade.exit_reason = "CLOSED", trade.exit_reason or reason
                self.save(trade)
            return True
        if not ctx.session_open:
            self._alert_once("CLOSE_DEFERRED", f"{reason}: session not open")
            return False
        ours = [o.order_id for o in self.ex.open_orders() if is_ours(o)]
        if ours:  # a refused cancel never blocks the close: brackets only reduce
            self._cancel_quietly(ours, trade.trade_id if trade is not None else 0)
        open_trade = trade if trade is not None and trade.status == "OPEN" else None
        if open_trade is not None:
            open_trade.closes += 1
            n, tid = open_trade.closes, open_trade.trade_id
            self.save(open_trade)
        else:
            n, tid = int(self.journal.get(ORPHAN_KEY, 0)) + 1, 0
            self.journal.put(ORPHAN_KEY, n)
        cid = close_cid(tid, n)
        sent = self.send(cid, "CLOSE", tid, {"reason": reason}, lambda: self.ex.close_position(cid))
        if sent.status != "ACCEPTED":
            self.event("CLOSE_FAILED", {"trade_id": tid, "reason": reason, "status": sent.status, "error": sent.error},
                       f"[청산 실패 - 재시도 중] {reason}: {sent.status} {sent.error}")
            return False
        if not self._await_flat():
            self.event("CLOSE_UNCONFIRMED", {"trade_id": tid, "reason": reason}, f"[청산 미확인 - 재시도 중] {reason}")
            return False
        if open_trade is not None:
            open_trade.status, open_trade.exit_reason = "CLOSED", reason
            self.save(open_trade)
        fill = self._fill_of(sent.order_id)
        self.event("CLOSED", {"trade_id": tid, "reason": reason, "order_id": sent.order_id, **fill},
                   f"[청산] {reason}" + (f" @ {fill['price']:,.1f}" if fill.get("price") else ""))
        return True

    def _fill_of(self, order_id: str | None) -> dict[str, Any]:
        """Fill price, size and fee of a market order, for the cost report (best effort)."""
        if not order_id:
            return {}
        try:
            o = self.ex.get_order(order_id)
        except ExchangeError:
            return {}
        return {"price": o.avg_fill_price, "contracts": o.filled, "side": o.side, "fee": o.fee}

    def reduce(self, size: int, reason: str, ctx: PlanContext) -> bool:
        """Close `size` contracts at market (close_position with a size: it only reduces). Intent before state: the
        reduce is recorded on the trade (`pending_reduce`, with the position it was sent against and the trade's
        size fields once done, `reconcile.after_reduce`) before the request, so an ambiguous answer can never lead
        to a second reduce - while one is pending, `reduce` resolves that one instead. True when the venue reported
        it filled: its after-state is applied at once (TP1_SYNC marks the live TP1 done, EXCESS leaves the trade
        unchanged, any other reason - MARGIN - makes the trade smaller) and the record stays until the position
        shows it (`reconcile.observe`)."""
        trade = self.trade()
        if trade is None or trade.status != "OPEN" or not ctx.session_open:
            return False
        if trade.pending_reduce is not None:  # never a second reduce while one is pending
            self.resolve_reduce()
            return False
        pos = self.ex.position()
        if pos is None or pos.side != trade.direction:
            return False
        trade.closes += 1
        cid = reduce_cid(trade.trade_id, trade.closes)
        left = max(trade.contracts - size, 0)
        tp1_n = None if reason in ("TP1_SYNC", "EXCESS") or trade.tp1_filled else _tp1_split(left, self.tp1_frac)
        trade.pending_reduce = {"cid": cid, "size": size, "reason": reason, "before": pos.contracts,
                                "ts": self.clock(), "after": after_reduce(trade, size, reason, tp1_n), "filled": None}
        self.save(trade)  # recorded before the request
        sent = self.send(cid, "REDUCE", trade.trade_id, {"size": size, "reason": reason},
                         lambda: self.ex.close_position(cid, size))
        return self._reduce_outcome(trade, sent, first=True)

    def resolve_reduce(self) -> None:
        """A reduce whose outcome is still unknown (an ambiguous answer, a killed process, a fill not read yet) is
        resolved by its client order id: looked up first and sent again under the same id only when the venue
        does not have it and the position does not show a reduction yet (then observe attributes it) - never a
        second reduce. The runtime calls this before every read of the exchange state."""
        trade = self.trade()
        if trade is None or trade.status != "OPEN" or trade.pending_reduce is None:
            return
        pr = trade.pending_reduce
        if pr.get("filled"):
            return  # reported filled: the position will show it (observe settles it)
        pos = self.ex.position()
        if pos is None or pos.contracts < int(pr["before"]):
            return  # the position already shows a reduction: observe attributes it; the minute job resolves the id
        cid, size = str(pr["cid"]), int(pr["size"])
        sent = self.send(cid, "REDUCE", trade.trade_id, {"size": size, "reason": pr["reason"]},
                         lambda: self.ex.close_position(cid, size))
        self._reduce_outcome(trade, sent)

    def _reduce_outcome(self, trade: LiveTrade, sent: Sent, first: bool = False) -> bool:
        pr = dict(trade.pending_reduce or {})
        tid, reason, size = trade.trade_id, pr.get("reason"), pr.get("size")
        if sent.status == "PENDING":  # unknown: kept, resolved by its id on the next pass
            if first:
                self.event("REDUCE_FAILED", {"trade_id": tid, "reason": reason, "status": "PENDING", "error": sent.error},
                           f"[수량 축소 미확인] {size}계약 · {reason}: 주문 번호로 확인 중 ({sent.error})")
            return False
        order: Order | None = None
        if sent.status == "ACCEPTED":
            try:
                order = self._await_fill(str(sent.order_id))
            except ExchangeError:
                return False  # accepted, the fill not readable yet: read again on the next pass
            if not order.is_terminal:
                return False
        if order is None or order.filled == 0:  # refused, abandoned or nothing filled: nothing was sold
            status = sent.status if order is None else "UNFILLED"
            trade.pending_reduce = None
            self.save(trade)
            self.event("REDUCE_FAILED", {"trade_id": tid, "reason": reason, "status": status, "error": sent.error},
                       f"[수량 축소 실패] {size}계약 · {reason}: {status} {sent.error or ''}".rstrip())
            return False
        for k, v in pr["after"].items():
            setattr(trade, k, v)
        pr["filled"] = int(order.filled)
        trade.pending_reduce = pr  # kept until the position shows it (reconcile.observe settles it)
        self.save(trade)
        self.event("REDUCED", {"trade_id": tid, "size": size, "reason": reason, "price": order.avg_fill_price,
                               "contracts": order.filled, "side": order.side, "fee": order.fee},
                   f"[수량 축소] {order.filled}계약 · {reason}")
        return True

    def flatten(self, reason: str, ctx: PlanContext) -> bool:
        """Cancel every order of ours and close the whole position (/flatten, breakers, margin)."""
        return self.close(reason, ctx)

    # ------------------------------------------------------------------ restart recovery (spec 5.1)
    def resolve_pending(self) -> tuple[dict[str, int], list[dict[str, Any]]]:
        """Look every PENDING intent up by client_order_id: found -> ACCEPTED; not found and older than 10
        minutes -> ABANDONED (it never reached the venue); lookup failed -> still unknown. Returns the counts
        and the CLOSE intents found. The runtime also calls this every minute while intents are pending."""
        now = self.clock()
        out = {"accepted": 0, "abandoned": 0, "unknown": 0, "rebuilt": 0}
        closes: list[dict[str, Any]] = []
        for row in self.journal.intents(status="PENDING"):
            found, known = self._lookup(row["client_order_id"], row["created_ts"])
            if not known:
                out["unknown"] += 1
            elif found is not None:
                self.journal.update_intent(row["client_order_id"], "ACCEPTED", now, order_id=found.order_id,
                                           note="found by lookup")
                out["accepted"] += 1
                if row["kind"] == "CLOSE":
                    closes.append(row)
            elif now - row["created_ts"] > ABANDON_AFTER_S:
                self.journal.update_intent(row["client_order_id"], "ABANDONED", now, note="not on the exchange")
                out["abandoned"] += 1
        return out, closes

    def recover(self) -> dict[str, int]:
        """At startup: resolve PENDING intents, rebuild the trade from an accepted entry the journal did not
        get to record, record a close accepted right before a crash. A bracket accepted right before a crash
        is not in the trade's legs: the next protect() cancels it as a stale order of ours."""
        out, closes = self.resolve_pending()
        trade = self.trade()
        if trade is not None and trade.status == "OPEN" and self.ex.position() is None:
            mine = [r for r in closes if r["trade_id"] == trade.trade_id]
            if mine:  # killed after our close was accepted: record it with its reason
                trade.status, trade.exit_reason = "CLOSED", str(mine[-1]["payload"].get("reason"))
                self.save(trade)
                self.event("CLOSED", {"trade_id": trade.trade_id, "reason": trade.exit_reason, "recovered": True})
        entries = [r for r in self.journal.intents(status="ACCEPTED", kind="ENTRY")]
        if entries and (trade is None or trade.trade_id != entries[-1]["trade_id"]):
            row = entries[-1]
            order = self.ex.get_order(str(row["order_id"]))
            pos = self.ex.position()
            if order.filled > 0 and pos is not None and pos.side == int(row["payload"]["direction"]):
                trade = self._trade_from(row["payload"], order)
                self.save(trade)
                out["rebuilt"] += 1
                self.event("RECOVERED_ENTRY", {"trade_id": trade.trade_id},
                           f"[재시작 복구] 진입 {trade.trade_id} {_side(trade.direction)} {trade.contracts}계약 복원")
        self.event("RECOVERY", out)
        return out
