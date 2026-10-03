"""Executes reconcile actions against the exchange with intent-before-send (Plan 3 spec 4.2, 5.1).

Every order goes through `send`: the intent (deterministic client_order_id, kind, payload) is committed
to the journal as PENDING before the request; the answer marks it ACCEPTED or REJECTED. An ambiguous
failure (429 / 5xx / timeout / process killed) leaves it PENDING, and a PENDING intent is looked up on
the exchange by client_order_id before it is ever sent again - so a restart never doubles an order.

Entries are market IOC orders; the fill is awaited (10 s) and the trade is protected at once with
trigger_bracket_gtc legs. Working protection is never removed before its replacement is in place. Stop
moves edit the resting bracket when `edit_stops` is on (unconfirmed for CFM brackets: off until CANARY
confirms it; the edit sends the order's total size, filled + left), otherwise the new bracket is placed
first and the old one cancelled (brackets only reduce, so a short overlap cannot over-close). Only when
the venue refuses the replacement with a known overlap refusal (`OVERLAP_REFUSALS`, reason codes to be
confirmed in CANARY) and our brackets plus the new leg really exceed the position is the old one
cancelled first - LEG_GAP, the designed path on such a venue and not an order failure - and when the
replacement then fails as well, the old terms are placed again at once (LEG_RESTORED). Any other refusal
(order entry disabled, a price refused, an unknown reason) or an unknown outcome (PENDING: 429 / 5xx /
timeout) keeps the old leg; an unconfirmed new leg that rests on the book (its answer lost) is found
among the open orders at once, so two legs of one name never rest for long. A resting bracket of ours
the trade does not list (accepted right before a crash, an unconfirmed replacement) is taken over when
its client order id names the trade and leg and its terms are the wanted ones (LEG_ADOPTED), else
replaced like a leg of the trade - never one whose cancel is queued; other brackets of ours are cancelled
only once every wanted leg is in place, and the trade records a new stop only once every leg rests at it
(STOP_MOVED). A leg that left the book is looked up before it is placed again: one with fills is not
placed again for LEG_SETTLE_S (the position read may not show the fill yet, and a take-profit placed
again below the market would sell at once what the leg already sold) - the next observe folds the fill.

Closes never cancel protection before the venue took the close: close_position goes out with the brackets
resting (both only reduce); only a known close-conflict refusal (`CLOSE_CONFLICTS`: the venue refuses a
close while brackets rest) cancels them - after a session read, deferring while it is not OPEN - and the
close is sent again at once. The flat position is awaited (30 s; at once over when the IOC close ended
without closing everything); the legs are then resized to the position left, or placed again when they
were cancelled for a close that failed. Reductions (close_position
with a size) are recorded on the trade before they are sent (`LiveTrade.pending_reduce`): a reduce with
an unknown outcome is resolved by its client order id (`resolve_reduce`, before every read of the exchange
state) and never followed by a second one, and a Protect planned with a reduce waits until the reduce is
known to have filled. Market orders wait for an OPEN session; a deferred close is retried on every pass
(spec 4.5: `휴장 중에 필요해진 청산은 재개장 직후`). An entry skipped once is never sent later (never chase).
Client order ids are never reused: a trade rebuilt or taken over under the id of an earlier one goes on
from the journal's highest bracket generation and close number. A position whose entry outcome is still
unknown (our entry PENDING and its lookup failing) is protected at once with a provisional trade built from
the entry's intent, settled as ours or foreign once the lookup answers; a foreign position without quotes
is protected at the spot levels (ratio 1.0, approximate) and re-mapped at the first pass with quotes. At
restart the trade's resting brackets are its legs again (no false UNPROTECTED).

The same code runs every stage: in SHADOW the exchange is a ShadowExchange (virtual fills, real
previews)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from ewbot.engine.position_mgr import _tp1_split
from ewbot.live.exchange import Exchange, ExchangeError, Order, PlaceResult, Position
from ewbot.live.journal import Journal
from ewbot.live.pricing import map_levels
from ewbot.live.reconcile import (
    PREFIX,
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
SCRATCH_KEY = "trade_scratch"  # one trade's bookkeeping (alerts sent once, legs seen filled): replaced with the trade
CANCEL_QUEUED = "CANCEL_QUEUED"  # still on the book, about to leave it: never a leg
# Refusal reason codes (the first token of PlaceResult.error), to be confirmed in CANARY (Task 16 step 8):
# a bracket refused because our brackets would exceed the position (SimExchange: INSUFFICIENT_POSITION_SIZE)...
OVERLAP_REFUSALS = frozenset({"INSUFFICIENT_POSITION_SIZE", "BRACKET_ORDER_SIZE_EXCEEDS_POSITION",
                              "PREVIEW_BRACKET_ORDER_SIZE_EXCEEDS_POSITION"})
# ...and a close refused because brackets rest on the position
CLOSE_CONFLICTS = frozenset({"OPEN_BRACKET_ORDERS", "ORDER_SIZE_EXCEEDS_BRACKETED_POSITION",
                             "PREVIEW_ORDER_SIZE_EXCEEDS_BRACKETED_POSITION"})

Alerter = Callable[[str, str], None]


@dataclass(frozen=True)
class Sent:
    status: str  # ACCEPTED | REJECTED | PENDING
    order_id: str | None = None
    error: str | None = None


def _side(d: int) -> str:
    return "롱" if d > 0 else "숏"


def _terms(o: Order) -> tuple[int, float | None, float | None]:
    """What a resting bracket still covers, at which take-profit and stop."""
    return o.size - o.filled, o.limit_price, o.stop_price


def _code(sent: Sent) -> str | None:
    """The venue's reason code of a refusal (PlaceResult.error is `CODE` or `CODE: message`); None otherwise."""
    if sent.status != "REJECTED" or not sent.error:
        return None
    return str(sent.error).split(":", 1)[0].strip()


def _leg_of(name: str, o: Order) -> Leg:
    """The trade's record of a resting bracket of ours, with the order's own terms."""
    return Leg(name, o.size - o.filled, float(o.limit_price or 0.0), float(o.stop_price or 0.0), o.client_order_id,
               o.order_id)


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
        overlap_refusals: Iterable[str] = OVERLAP_REFUSALS,
        close_conflicts: Iterable[str] = CLOSE_CONFLICTS,
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
        self.overlap_refusals = frozenset(overlap_refusals)
        self.close_conflicts = frozenset(close_conflicts)

    # ------------------------------------------------------------------ state
    def trade(self) -> LiveTrade | None:
        data = self.journal.get(TRADE_KEY)
        return None if data is None else LiveTrade.from_json(data)

    def save(self, trade: LiveTrade | None) -> None:
        self.journal.put(TRADE_KEY, None if trade is None else trade.to_json())
        if trade is None or trade.status != "OPEN":
            self.journal.put(SCRATCH_KEY, None)  # the trade's bookkeeping ends with it

    def event(self, kind: str, data: dict[str, Any], text: str | None = None) -> None:
        self.journal.add_event(self.clock(), kind, data)
        if text is not None:
            self.alert(kind, text)

    def _scratch(self, trade_id: int) -> dict[str, Any]:
        """The bookkeeping of one trade (0: no open trade): `alerted` (alerts sent once) and `leg_filled` (order id
        -> when a leg was first found gone with fills). One journal key, replaced when the trade changes."""
        data = self.journal.get(SCRATCH_KEY)
        if not isinstance(data, dict) or data.get("trade_id") != trade_id:
            data = {"trade_id": trade_id, "alerted": [], "leg_filled": {}}
        return data

    def _counters(self, trade_id: int) -> tuple[int, int]:
        """The highest bracket generation and close / reduce number the journal holds for `trade_id`: a trade
        rebuilt or taken over under the id of an earlier one goes on from there (a reused client order id would
        resolve to the earlier, filled order)."""
        gen = closes = 0
        for row in self.journal.intents(trade_id=trade_id):
            tail = str(row["client_order_id"]).rsplit("-", 1)[-1]
            if not tail.isdigit():
                continue
            if row["kind"] == "BRACKET":
                gen = max(gen, int(tail))
            elif row["kind"] in ("CLOSE", "REDUCE"):
                closes = max(closes, int(tail))
        return gen, closes

    def _recorded(self, trade_id: int) -> bool:
        """A trade with this id was recorded once: it is the journal's trade, or it sent orders of its own."""
        t = self.trade()
        if t is not None and t.trade_id == trade_id:
            return True
        return any(row["kind"] != "ENTRY" for row in self.journal.intents(trade_id=trade_id))

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

    def _await_flat(self, order_id: str | None) -> bool:
        """True once the position is flat. False at the deadline - or at once when the IOC close ended without
        filling all it was sent for (the venue cancelled the rest: the position stays). Read errors are retried
        until the deadline."""
        deadline = self.clock() + self.flat_timeout
        while True:
            try:
                if self.ex.position() is None:
                    return True
                o = self.ex.get_order(order_id) if order_id else None
                if o is not None and o.is_terminal and o.filled < o.size:
                    return False
            except ExchangeError:
                pass
            if self.clock() >= deadline:
                return False
            self.sleep(self.poll_s)

    # ------------------------------------------------------------------ actions
    def execute(self, actions: Sequence[Action], ctx: PlanContext) -> None:
        self._settle(ctx)
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
        """Once per open trade (or once while there is none): the record goes with the trade."""
        t = self.trade()
        scratch = self._scratch(t.trade_id if t is not None and t.status == "OPEN" else 0)
        key = f"{kind}:{detail}"
        if key in scratch["alerted"]:
            return
        scratch["alerted"] = (scratch["alerted"] + [key])[-100:]
        self.journal.put(SCRATCH_KEY, scratch)
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
        if e.signal_ts in self.journal.get(SKIPS_KEY, []):
            return None  # skipped once (paused, a flip's close not confirmed, no quotes...): never sent later
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
        """The trade of our filled entry. It opened when the order was created (a trade rebuilt after an outage
        was unprotected since then: PROTECTED_FIRST measures it); its ids go on from the journal's."""
        n = int(order.filled)
        trade_id = int(payload["signal_ts"])
        gen, closes = self._counters(trade_id)
        return LiveTrade(
            trade_id=trade_id, setup=str(payload["setup"]), direction=int(payload["direction"]),
            contracts=n, tp1_n=_tp1_split(n, self.tp1_frac), entry_price=float(order.avg_fill_price or 0.0),
            tp1_price=float(payload["tp1"]), tp2_price=None if payload["tp2"] is None else float(payload["tp2"]),
            stop_spot=float(payload["stop_spot"]), stop_price=float(payload["stop"]),
            opened_ts=float(order.created_ts or self.clock()), gen=gen, closes=closes,
        )

    def adopt(self, tp: TargetPosition, ctx: PlanContext, contracts: int | None = None) -> None:
        """Take over a position the trade state does not know: at most `contracts` (default: the whole
        position) and never above the stage maximum `ctx.max_contracts`; the planner reduces the rest
        (Reduce EXCESS). Our own entry for that signal (its fill could not be read in time; one still PENDING
        is looked up first) is rebuilt from its intent - unless a trade with that id was recorded already: a
        position after our trade closed is a foreign one. A foreign position (event ADOPTED: manual
        intervention) is taken over at its own entry price, its ids going on from the journal's; without quotes
        the engine's levels cannot be mapped (alerted once while it is not covered). A position the engine
        already took TP1 on is the rest after TP1 (break-even, no TP1 portion)."""
        pos = self.ex.position()
        if pos is None:
            return
        cid = entry_cid(tp.signal_ts, tp.direction)
        own = self.journal.intent(cid)
        if own is not None and own["status"] == "PENDING":  # our entry's outcome is unknown: look it up first
            found, known = self._lookup(cid, float(own["created_ts"]))
            if found is not None:
                self.journal.update_intent(cid, "ACCEPTED", self.clock(), order_id=found.order_id, note="found by lookup")
                own = self.journal.intent(cid)
            elif not known and not self._recorded(tp.signal_ts):  # protected now, settled once the lookup answers
                self._provisional(own, pos, self._take(pos, ctx, contracts), ctx)
                return
        if (own is not None and own["status"] == "ACCEPTED" and own["order_id"]
                and not self._recorded(tp.signal_ts)):
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
        n = self._take(pos, ctx, contracts)
        after_tp1 = tp.tp1_done and n > 1
        approx = ctx.ratio is None  # no quotes: the spot levels as they are (ratio 1.0) until the quotes are back
        ratio = 1.0 if ctx.ratio is None else ctx.ratio
        lv = map_levels(tp.direction, tp.stop, tp.tp1, tp.tp2, ratio, ctx.tick)
        gen, closes = self._counters(tp.signal_ts)
        trade = LiveTrade(
            trade_id=tp.signal_ts, setup=tp.setup, direction=tp.direction, contracts=n,
            tp1_n=0 if after_tp1 else _tp1_split(n, self.tp1_frac), entry_price=pos.avg_entry, tp1_price=lv.tp1,
            tp2_price=lv.tp2, stop_spot=tp.stop, stop_price=lv.stop, opened_ts=self.clock(), tp1_filled=after_tp1,
            gen=gen, closes=closes,
        )
        self.save(trade)
        if approx:
            scratch = self._scratch(trade.trade_id)
            scratch["approx"] = {"stop": tp.stop, "tp1": tp.tp1, "tp2": tp.tp2}
            self.journal.put(SCRATCH_KEY, scratch)
        self.event("ADOPTED", {"trade_id": trade.trade_id, "contracts": n, "position": pos.contracts},
                   f"[경고] 저널에 없던 포지션 {_side(pos.side)} {pos.contracts}계약 중 {n}계약을 엔진 거래 "
                   f"{tp.signal_ts}로 인수")
        if approx:
            self._alert_once("LEVELS_APPROX", f"trade {trade.trade_id}: no quotes - protected at the spot levels "
                                              f"(ratio 1.0, approximate) until the quotes are back")
        legs, stop = desired_legs(trade, n, trade.stop_spot, ratio, ctx.tick)
        self.protect(Protect(tuple(legs), trade.stop_spot, stop), ctx)

    @staticmethod
    def _take(pos: Position, ctx: PlanContext, contracts: int | None) -> int:
        """Contracts to take over: at most `contracts` (default the whole position) and the stage maximum."""
        n = pos.contracts if contracts is None else min(contracts, pos.contracts)
        return n if ctx.max_contracts is None else min(n, ctx.max_contracts)

    def _provisional(self, own: dict[str, Any], pos: Position, n: int, ctx: PlanContext) -> None:
        """Our entry's outcome is unknown and its lookup fails, yet a position is there: protect it at once with a
        provisional trade from the entry's intent (its levels; the position's size and price; opened when the entry
        was sent). `_settle` decides later whether it was ours (RECOVERED_ENTRY) or foreign (ADOPTED)."""
        p = own["payload"]
        trade_id = int(p["signal_ts"])
        gen, closes = self._counters(trade_id)
        trade = LiveTrade(
            trade_id=trade_id, setup=str(p["setup"]), direction=int(p["direction"]), contracts=n,
            tp1_n=_tp1_split(n, self.tp1_frac), entry_price=pos.avg_entry, tp1_price=float(p["tp1"]),
            tp2_price=None if p["tp2"] is None else float(p["tp2"]), stop_spot=float(p["stop_spot"]),
            stop_price=float(p["stop"]), opened_ts=float(own["created_ts"]), gen=gen, closes=closes,
        )
        self.save(trade)
        scratch = self._scratch(trade_id)
        scratch["provisional"] = own["client_order_id"]
        self.journal.put(SCRATCH_KEY, scratch)
        self.event("ADOPTED_PROVISIONAL", {"trade_id": trade_id, "contracts": n, "position": pos.contracts},
                   f"[경고] 진입 {trade_id} 결과 미확인(조회 실패): 포지션 {_side(pos.side)} {pos.contracts}계약 중 "
                   f"{n}계약을 진입 주문의 손절·TP로 임시 보호 - 조회되면 정리")
        legs, stop = desired_legs(trade, n, trade.stop_spot, ctx.ratio, ctx.tick)
        self.protect(Protect(tuple(legs), trade.stop_spot, stop), ctx)

    def _entry_outcome(self, cid: str) -> bool | None:
        """Our entry `cid` reached the venue (True), never did (False), or is still unknown (None)."""
        row = self.journal.intent(cid)
        if row is None or row["status"] in ("REJECTED", "ABANDONED"):
            return False
        if row["status"] == "ACCEPTED":
            return True
        found, known = self._lookup(cid, float(row["created_ts"]))
        if not known:
            return None
        if found is None:
            return False
        self.journal.update_intent(cid, "ACCEPTED", self.clock(), order_id=found.order_id, note="found by lookup")
        return True

    def _settle(self, ctx: PlanContext) -> None:
        """Before every pass: a provisional trade is settled once its entry's outcome is known (ours:
        RECOVERED_ENTRY; not on the venue: a foreign position, ADOPTED); levels taken over without quotes are mapped
        at the first pass with quotes (LEVELS_MAPPED: the next plan re-places the legs)."""
        t = self.trade()
        if t is None or t.status != "OPEN":
            return
        scratch = self._scratch(t.trade_id)
        cid, approx = scratch.get("provisional"), scratch.get("approx")
        if cid:
            own = self._entry_outcome(str(cid))
            if own is not None:
                scratch["provisional"] = None
                self.journal.put(SCRATCH_KEY, scratch)
                if own:
                    self.event("RECOVERED_ENTRY", {"trade_id": t.trade_id, "provisional": True},
                               f"[복구] 진입 {t.trade_id} 확인: 임시 보호한 {t.contracts}계약은 우리 진입")
                else:
                    self.event("ADOPTED", {"trade_id": t.trade_id, "contracts": t.contracts, "provisional": True},
                               f"[경고] 진입 {t.trade_id}는 거래소에 없음: 임시 보호한 {t.contracts}계약은 저널에 없던 "
                               f"포지션 (엔진 거래 {t.trade_id}로 인수)")
        if approx and ctx.ratio is not None:
            lv = map_levels(t.direction, approx["stop"], approx["tp1"], approx["tp2"], ctx.ratio, ctx.tick)
            if t.stop_spot == approx["stop"]:  # a stop moved since was mapped with quotes already
                t.stop_price = lv.stop
            t.tp1_price, t.tp2_price = lv.tp1, lv.tp2
            self.save(t)
            scratch["approx"] = None
            self.journal.put(SCRATCH_KEY, scratch)
            self.event("LEVELS_MAPPED", {"trade_id": t.trade_id, "stop": t.stop_price, "tp1": lv.tp1, "tp2": lv.tp2,
                                         "ratio": ctx.ratio})

    # protection --------------------------------------------------------------
    def _place_leg(self, trade: LiveTrade, want: Leg) -> tuple[Leg | None, Sent]:
        trade.gen += 1
        cid = bracket_cid(trade.trade_id, want.name, trade.gen)
        self.save(trade)  # the generation is persisted before the request
        sent = self.send(cid, "BRACKET", trade.trade_id,
                         {"leg": want.name, "size": want.size, "limit": want.limit_price, "stop": want.stop_price},
                         lambda: self.ex.place_bracket(cid, -trade.direction, want.size, want.limit_price, want.stop_price))
        if sent.status == "PENDING":  # answer and lookup lost: an order resting on the book is ours, found at once
            found = self._resting(cid)
            if found is not None:
                self.journal.update_intent(cid, "ACCEPTED", self.clock(), order_id=found.order_id, note="found resting")
                sent = Sent("ACCEPTED", found.order_id)
        if sent.status != "ACCEPTED":
            return None, sent
        return Leg(want.name, want.size, want.limit_price, want.stop_price, cid, sent.order_id), sent

    def _resting(self, cid: str) -> Order | None:
        """Our order `cid` among the open orders (best effort: None when they cannot be read)."""
        try:
            return next((o for o in self.ex.open_orders() if o.client_order_id == cid and o.status != CANCEL_QUEUED),
                        None)
        except ExchangeError:
            return None

    def _overlap(self, trade: LiveTrade, want: Leg) -> bool:
        """Our resting brackets plus `want` exceed the position (fresh reads): the overlap a refusal names exists."""
        try:
            pos = self.ex.position()
            orders = self.ex.open_orders()
        except ExchangeError:
            return False
        if pos is None:
            return False
        resting = sum(o.size - o.filled for o in orders
                      if is_ours(o) and o.kind == "BRACKET" and o.side == -trade.direction)
        return resting + want.size > pos.contracts

    def _bracket_failed(self, trade: LiveTrade, want: Leg, sent: Sent) -> None:
        self.event("BRACKET_FAILED", {"trade_id": trade.trade_id, "leg": want.name, "status": sent.status,
                                      "error": sent.error},
                   f"[보호 주문 실패] {want.name} {want.size}계약 손절 {want.stop_price:,.0f}: {sent.status} {sent.error}")

    def _edit(self, o: Order, want: Leg) -> bool:
        """orders/edit takes the order's total size: what already filled plus what is to rest. A failed edit
        (refused or unanswered) is replaced like any other leg."""
        try:
            return self.ex.edit_bracket(o.order_id, o.filled + want.size, want.limit_price, want.stop_price)
        except ExchangeError:
            return False

    @staticmethod
    def _orphan(trade: LiveTrade, want: Leg, orders: dict[str, Order], claimed: set[str]) -> Order | None:
        """A resting bracket of ours for this trade's `want.name` leg that the trade does not list (accepted right
        before a crash, or a replacement whose answer was lost) - one with the wanted terms first."""
        prefix = f"{PREFIX}b-{trade.trade_id}-{want.name}-"
        found = [o for oid, o in orders.items() if oid not in claimed and o.kind == "BRACKET"
                 and o.client_order_id.startswith(prefix) and o.side == -trade.direction]
        wanted = (want.size, want.limit_price, want.stop_price)
        return next((o for o in found if _terms(o) == wanted), found[0] if found else None)

    def protect(self, a: Protect, ctx: PlanContext) -> None:
        """Make the resting brackets the wanted legs without ever removing a working leg before its replacement
        rests (module docstring): kept, taken over, edited or replaced (place first, then cancel; cancel first
        only when the venue refuses the overlap)."""
        trade = self.trade()
        if trade is None or trade.status != "OPEN":
            return
        orders = {o.order_id: o for o in self.ex.open_orders() if o.status != CANCEL_QUEUED}  # queued: leaving
        claimed = {str(leg.order_id) for leg in trade.legs if leg.order_id in orders}
        kept: list[Leg] = []
        exact = 0  # wanted legs resting with exactly the wanted terms
        replaced: set[str] = set()  # cancelled below already: not cancelled again as stale
        for want in a.legs:
            terms = (want.size, want.limit_price, want.stop_price)
            cur = next((leg for leg in trade.legs if leg.name == want.name and leg.order_id in orders), None)
            if cur is None:
                gone = next((leg for leg in trade.legs if leg.name == want.name and leg.order_id), None)
                if gone is not None and self._left_with_fills(trade, gone):
                    kept.append(gone)  # not placed again: the next observe folds its fill (or it is placed later)
                    continue
            if cur is not None and _terms(orders[str(cur.order_id)]) == terms:
                kept.append(cur)
                exact += 1
                continue
            orphan = self._orphan(trade, want, orders, claimed)
            if orphan is not None:
                claimed.add(orphan.order_id)
                if _terms(orphan) == terms:  # ours for this leg, its answer lost: taken over, not placed again
                    kept.append(_leg_of(want.name, orphan))
                    exact += 1
                    self.event("LEG_ADOPTED", {"trade_id": trade.trade_id, "leg": want.name, "order_id": orphan.order_id,
                                               "client_order_id": orphan.client_order_id})
                    if cur is not None:
                        self._cancel_quietly([str(cur.order_id)], trade.trade_id)
                        replaced.add(str(cur.order_id))
                    continue
                if cur is None:
                    cur = _leg_of(want.name, orphan)  # replaced like a leg of the trade
            if cur is not None and self.edit_stops:
                o = orders[str(cur.order_id)]
                if (o.size - o.filled, o.limit_price) == (want.size, want.limit_price) and self._edit(o, want):
                    kept.append(Leg(cur.name, want.size, want.limit_price, want.stop_price, cur.client_order_id,
                                    cur.order_id))
                    exact += 1
                    self.event("STOP_EDITED", {"trade_id": trade.trade_id, "leg": want.name, "stop": want.stop_price})
                    continue
            placed, sent = self._place_leg(trade, want)
            moved = placed is not None
            if (placed is None and cur is not None and _code(sent) in self.overlap_refusals
                    and self._overlap(trade, want)):  # the venue refuses overlapping brackets: cancel first
                placed, moved = self._swap(trade, cur, orders[str(cur.order_id)], want, replaced)
            elif placed is None:  # unknown (PENDING), any other refusal or nothing to replace: the old leg stays
                self._bracket_failed(trade, want, sent)
            if placed is not None:
                if cur is not None and str(cur.order_id) not in replaced:
                    self._cancel_quietly([str(cur.order_id)], trade.trade_id)
                    replaced.add(str(cur.order_id))
                kept.append(placed)
                exact += int(moved)
            elif cur is not None and str(cur.order_id) not in replaced:
                kept.append(cur)  # its replacement is not in place: the old leg keeps protecting
        complete = len(kept) == len(a.legs)
        keep = {leg.order_id for leg in kept}
        stale = [oid for oid, o in orders.items()
                 if is_ours(o) and o.kind == "BRACKET" and oid not in keep and oid not in replaced]
        if stale and complete:  # extra brackets only reduce: removed once every wanted leg is in place
            self._cancel_quietly(stale, trade.trade_id)
        trade.legs = kept
        in_place = exact == len(a.legs)  # every leg rests at the new terms: their stop is the stop in force
        moved_stop = in_place and a.stop_price != trade.stop_price
        if in_place:
            trade.stop_spot, trade.stop_price = a.stop_spot, a.stop_price
        if trade.protected_ts is None and kept and complete:
            trade.protected_ts = self.clock()
            self.event("PROTECTED_FIRST", {"trade_id": trade.trade_id,
                                           "after_entry_s": round(trade.protected_ts - trade.opened_ts, 3)})
        elif moved_stop:
            self.event("STOP_MOVED", {"trade_id": trade.trade_id, "stop": a.stop_price, "stop_spot": a.stop_spot},
                       f"[손절 이동] {trade.setup} {_side(trade.direction)} → {a.stop_price:,.0f}")
        self.save(trade)

    def _swap(self, trade: LiveTrade, cur: Leg, o: Order, want: Leg, replaced: set[str]) -> tuple[Leg | None, bool]:
        """The venue refused `want` as an overlap while `cur` rests: cancel `cur` first (LEG_GAP:
        the designed path on such a venue, not an order failure), then place `want`; when that fails as well,
        `cur`'s terms are placed again at once (LEG_RESTORED). Returns the leg now resting (None: none) and whether
        it has the wanted terms."""
        oid = str(cur.order_id)
        try:
            self.ex.cancel([oid])
        except ExchangeError as exc:  # it may still rest: kept, the move is tried again on the next pass
            self.event("CANCEL_FAILED", {"trade_id": trade.trade_id, "order_ids": [oid], "error": str(exc)[:200]})
            self._bracket_failed(trade, want, Sent("REJECTED", None, "overlap refused, the old leg not cancelled"))
            return None, False
        replaced.add(oid)
        self.event("LEG_GAP", {"trade_id": trade.trade_id, "leg": want.name})
        placed, sent = self._place_leg(trade, want)
        if placed is not None:
            return placed, True
        self._bracket_failed(trade, want, sent)
        back = Leg(cur.name, o.size - o.filled, float(o.limit_price or cur.limit_price),
                   float(o.stop_price or cur.stop_price))
        restored, sent = self._place_leg(trade, back)
        if restored is None:
            self._bracket_failed(trade, back, sent)
            return None, False
        self.event("LEG_RESTORED", {"trade_id": trade.trade_id, "leg": cur.name, "stop": back.stop_price})
        return restored, False

    def _left_with_fills(self, trade: LiveTrade, leg: Leg) -> bool:
        """A leg of ours that left the book: True while it has fills the position read may not show yet - for
        LEG_SETTLE_S from when it was first found so (then it is placed again if still wanted). A leg that cannot
        be read is placed again (protection first)."""
        scratch = self._scratch(trade.trade_id)
        first = scratch["leg_filled"].get(str(leg.order_id))
        if first is not None:
            return self.clock() - float(first) < LEG_SETTLE_S
        try:
            o = self.ex.get_order(str(leg.order_id))
        except ExchangeError:
            return False
        if o.filled <= 0:
            return False
        scratch["leg_filled"][str(leg.order_id)] = self.clock()
        self.journal.put(SCRATCH_KEY, scratch)
        self.event("LEG_FILLED", {"trade_id": trade.trade_id, "leg": leg.name, "order_id": leg.order_id,
                                  "filled": o.filled, "status": o.status})
        return True

    def _protect_left(self, ctx: PlanContext, cancelled: bool) -> None:
        """A close that did not go flat: the legs become the ones for the position left, at the stop in force
        (resized after a partial close; placed again when they were cancelled for the close - a leg the cancel left
        resting is taken back by its terms). Brackets only reduce: an excess is covered as well."""
        trade = self.trade()
        if trade is None or trade.status != "OPEN":
            return
        try:
            pos = self.ex.position()
            if pos is None or pos.side != trade.direction:
                return
            if cancelled:
                trade.legs = []  # cancelled for the close: nothing to look up
                self.save(trade)
            legs, stop = desired_legs(trade, pos.contracts, trade.stop_spot, ctx.ratio, ctx.tick)
            self.protect(Protect(tuple(legs), trade.stop_spot, stop), ctx)
        except ExchangeError as exc:  # observe sees the gap (UNPROTECTED); the next pass places them
            self.event("ACTION_ERROR", {"action": "Protect", "error": str(exc)[:200]},
                       f"[주문 오류] Protect: {str(exc)[:200]}")

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
        open_trade = trade if trade is not None and trade.status == "OPEN" else None
        tid = open_trade.trade_id if open_trade is not None else 0
        sent = self._send_close(open_trade, reason)  # the brackets keep protecting until the venue took the close
        cancelled = False
        if _code(sent) in self.close_conflicts:  # refused while brackets rest: cancel them, then close at once
            if not self._session_open():
                self._alert_once("CLOSE_DEFERRED", f"{reason}: session not open")
                return False
            ours = [o.order_id for o in self.ex.open_orders() if is_ours(o) and o.status != CANCEL_QUEUED]
            self.event("CLOSE_CONFLICT", {"trade_id": tid, "reason": reason, "error": sent.error, "cancel": ours})
            if ours:
                self._cancel_quietly(ours, tid)
            cancelled = True
            sent = self._send_close(open_trade, reason)
        if sent.status != "ACCEPTED":
            self.event("CLOSE_FAILED", {"trade_id": tid, "reason": reason, "status": sent.status, "error": sent.error},
                       f"[청산 실패 - 재시도 중] {reason}: {sent.status} {sent.error}")
            if cancelled:
                self._protect_left(ctx, cancelled)
            return False
        if open_trade is not None:  # the position going flat from now on is this close (also after a crash)
            open_trade = self.trade() or open_trade
            open_trade.exit_reason = reason
            self.save(open_trade)
        if not self._await_flat(sent.order_id):
            self.event("CLOSE_UNCONFIRMED", {"trade_id": tid, "reason": reason}, f"[청산 미확인 - 재시도 중] {reason}")
            self._protect_left(ctx, cancelled)
            return False
        if open_trade is not None:
            open_trade.status, open_trade.exit_reason = "CLOSED", reason
            self.save(open_trade)
        fill = self._fill_of(sent.order_id)
        self.event("CLOSED", {"trade_id": tid, "reason": reason, "order_id": sent.order_id, **fill},
                   f"[청산] {reason}" + (f" @ {fill['price']:,.1f}" if fill.get("price") else ""))
        try:  # brackets left on the flat position (the venue cancels them as well)
            left = [o.order_id for o in self.ex.open_orders() if is_ours(o) and o.status != CANCEL_QUEUED]
        except ExchangeError:
            left = []
        if left:
            self._cancel_quietly(left, tid)
        return True

    def _send_close(self, open_trade: LiveTrade | None, reason: str) -> Sent:
        """close_position under a new client order id (the trade's next close number; `ewb-x-0-<n>` without one)."""
        if open_trade is not None:
            current = self.trade() or open_trade
            current.closes += 1
            n, tid = current.closes, current.trade_id
            self.save(current)
        else:
            n, tid = int(self.journal.get(ORPHAN_KEY, 0)) + 1, 0
            self.journal.put(ORPHAN_KEY, n)
        cid = close_cid(tid, n)
        return self.send(cid, "CLOSE", tid, {"reason": reason}, lambda: self.ex.close_position(cid))

    def _session_open(self) -> bool:
        """A fresh session read (a failed read counts as not OPEN: protection stays)."""
        try:
            return self.ex.session().is_open
        except ExchangeError:
            return False

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

    def _recovery_error(self, out: dict[str, int], what: str, exc: ExchangeError) -> None:
        """A read the recovery needs failed: unresolved (counted unknown), the regular passes pick it up."""
        out["unknown"] += 1
        self.event("RECOVERY_ERROR", {"what": what, "error": str(exc)[:200]})

    def _claim_legs(self, trade: LiveTrade, pos: Position, orders: list[Order]) -> None:
        """At restart: a resting bracket of ours named for the trade (`ewb-b-<trade>-<leg>-<gen>`) that the trade does
        not list - accepted right before the crash - is that leg again (LEG_ADOPTED; the newest per leg, never one
        whose cancel is queued), so the first observe does not read the position as unprotected. When they cover the
        position and the trade was not protected yet, it was protected when they were placed (PROTECTED_FIRST)."""
        resting = {o.order_id: o for o in orders if o.status != CANCEL_QUEUED}
        names = {leg.name for leg in trade.legs if leg.order_id in resting}
        listed = {leg.order_id for leg in trade.legs}
        prefix = f"{PREFIX}b-{trade.trade_id}-"
        claimed: list[Order] = []
        for o in sorted(resting.values(), key=lambda o: (o.created_ts or 0.0, o.client_order_id), reverse=True):
            name, _, gen = o.client_order_id[len(prefix):].partition("-")
            if (not o.client_order_id.startswith(prefix) or name not in ("tp1", "tp2") or not gen.isdigit()
                    or o.kind != "BRACKET" or o.side != -trade.direction or o.order_id in listed or name in names):
                continue
            trade.legs = [leg for leg in trade.legs if leg.name != name] + [_leg_of(name, o)]
            names.add(name)
            claimed.append(o)
            self.event("LEG_ADOPTED", {"trade_id": trade.trade_id, "leg": name, "order_id": o.order_id,
                                       "client_order_id": o.client_order_id, "recovered": True})
        if not claimed:
            return
        trade.legs.sort(key=lambda leg: leg.name)
        covered = sum(resting[str(leg.order_id)].size - resting[str(leg.order_id)].filled
                      for leg in trade.legs if leg.order_id in resting)
        if trade.protected_ts is None and covered >= pos.contracts:
            trade.protected_ts = max(float(o.created_ts or self.clock()) for o in claimed)
            self.event("PROTECTED_FIRST", {"trade_id": trade.trade_id,
                                           "after_entry_s": round(trade.protected_ts - trade.opened_ts, 3)})
        self.save(trade)

    def recover(self) -> dict[str, int]:
        """At startup: resolve PENDING intents, record a close accepted right before a crash (the journal's open
        trade, a flat position and an ACCEPTED close of that trade - its reason, not a bracket exit), rebuild the
        trade from an accepted entry the journal did not get to record (only when no trade with that id was
        recorded), and take the open trade's resting brackets back as its legs (`_claim_legs`: a bracket accepted
        right before a crash). A read that fails is recorded (RECOVERY_ERROR, counted unknown) and the recovery
        goes on."""
        out, _ = self.resolve_pending()
        trade = self.trade()
        if trade is not None and trade.status == "OPEN":
            try:
                pos = self.ex.position()
                orders = [] if pos is None else self.ex.open_orders()
            except ExchangeError as exc:
                self._recovery_error(out, "position", exc)
            else:
                mine = self.journal.intents(status="ACCEPTED", kind="CLOSE", trade_id=trade.trade_id)
                if pos is None and mine:  # killed after our close was accepted: record it with its reason
                    row = mine[-1]
                    trade.status, trade.exit_reason = "CLOSED", str(row["payload"].get("reason"))
                    self.save(trade)
                    self.event("CLOSED", {"trade_id": trade.trade_id, "reason": trade.exit_reason, "recovered": True,
                                          "order_id": row["order_id"], **self._fill_of(row["order_id"])})
                elif pos is not None:
                    self._claim_legs(trade, pos, orders)
        trade = self.trade()
        entries = self.journal.intents(status="ACCEPTED", kind="ENTRY")
        if entries and (trade is None or trade.status != "OPEN"):
            row = entries[-1]
            if row["order_id"] and not self._recorded(int(row["trade_id"])):
                try:
                    order = self.ex.get_order(str(row["order_id"]))
                    pos = self.ex.position()
                except ExchangeError as exc:
                    self._recovery_error(out, str(row["client_order_id"]), exc)
                else:
                    if order.filled > 0 and pos is not None and pos.side == int(row["payload"]["direction"]):
                        trade = self._trade_from(row["payload"], order)
                        self.save(trade)
                        out["rebuilt"] += 1
                        self.event("RECOVERED_ENTRY", {"trade_id": trade.trade_id},
                                   f"[재시작 복구] 진입 {trade.trade_id} {_side(trade.direction)} {trade.contracts}계약 복원")
        self.event("RECOVERY", out)
        return out
