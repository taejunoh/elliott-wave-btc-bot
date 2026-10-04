"""The live runtime (Plan 3 spec 3.2 `scheduler`, 4.1, 5.1, 5.2): one process, one main loop, ticking
every second. Only the WebSocket feed, the Telegram long-poll and the Telegram sender run in their own
threads; they hand data over (prices, queued commands, queued alerts), and every trading decision is taken
on the main loop. An alert is only queued (live/notify.py sends it), so Telegram never delays the loop, and
every protective action - a stop-gap or unprotected close, a breaker flatten, a margin reduction, a pause -
is taken before its alert.

  commands  Telegram /status /pause /resume /flatten /flatten_confirm /help (live/control.py); a command sent
            more than 2 minutes before the loop reaches it is ignored with a reply (sent again if wanted),
            and /flatten_confirm counts only when sent within 60 s of its /flatten (the messages' dates)
  watch     every second while a trade is open: the stop-gap check on the futures price, and an
            immediate close when no stop rests (refused bracket) and the price is through the stop
  poll      every 10 s while a trade is open (60 s otherwise, at once after a WebSocket resync): a reduce
            with an unknown outcome is resolved by its client order id first; then position and open
            orders are re-read as one state (the position read again after the orders, repeated until
            both reads agree: a fill between the reads never pairs a stale position with a vanished
            leg), observed and reconciled against the last target (protection right after fills, the
            TP1 break-even, deferred closes). Every entry and adoption is sized by the more conservative of
            the stage this process runs in and the journal's (a demotion applies at once). After each pass an
            order failure is resolved or recorded unresolved (CANARY gate 2: a bracket not replaced within
            30 s, a close or reduction not done by the next pass - a CANARY failure that pauses), and a manual
            intervention or an unresolved order failure is one incident per episode, not one per pass
  minute    journal heartbeat; the Telegram command thread (started again when it died; a dead thread, a
            hung one, 5 failed polls in a row or a conflict with another poller alerted once per episode)
            and the external heartbeat: a success ping while Telegram works - Telegram unusable (10 minutes
            of failures, a stuck sender, 3 refused messages in a row) sends healthchecks.io a /fail (its
            email reaches the user without Telegram) and withholds the pings for at most 30 minutes, the
            /fail repeated every 3 hours while it lasts; 3 failed pings in a row are alerted. A failure of
            this check never skips the rest of the minute. Then session state, account breakers (a balance
            that is not
            a finite amount above 0 is alerted, never a sample; three such reads in a row are an incident
            that pauses entries), margin (80% warn, 90% reduce; never acted on without a futures price), the
            WebSocket feed's health (no futures price for 5 minutes while the session is OPEN: alerted once
            per outage, and when it is back),
            pending intents resolved (or abandoned after 10 min), the 15:30 New York
            overnight-margin report (its reads retried every minute until 16:00), the daily summary (09:00 New York)
            with expiry reminders and the daily key-permission check, the paper audit (from minute 50, up
            to the last bar whose cycle ended OK: a revised bar is alerted only, a decision missing on one side
            is pending until the next audit confirms it, a different decision is a major incident at once; a
            failed audit is stored as failed and alerted), and the stage-change restart
  cycle     at hh:00:30 for the bar that just closed: candles, engine replay (target), decisions,
            reconcile. Retried every 15 s while the bar is missing; 2 minutes after the close the
            decision is held and only protection is kept (spec 4.1 step 1). A bar whose cycle ended OK is
            never decided again into its cycles row: a restart takes the last such bar as done and only
            refreshes its target (SHADOW gate 2 keeps the on-time row)."""

from __future__ import annotations

import queue
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ewbot.backtest.holdout import data_fingerprint
from ewbot.data.candles import fill_gaps, load_1h
from ewbot.execution.costs import fee as model_fee
from ewbot.live.audit import AuditError, AuditResult, audit
from ewbot.live.control import Command
from ewbot.live.exchange import STOP_LIMIT_OFFSET, Exchange, ExchangeError, Order, Position, ProductSpec, SessionInfo
from ewbot.live.executor import Executor
from ewbot.live.feed import usable_price
from ewbot.live.journal import Journal
from ewbot.live.preflight import BASELINE_FINGERPRINT
from ewbot.live.pricing import MARGIN_ENTRY_MAX, basis_ratio, map_level, margin_ratio, stage_contracts
from ewbot.live.reconcile import Alert, Close, PlanContext, Protect, is_ours, observe, plan
from ewbot.live.shadow import ShadowExchange
from ewbot.live.stage import (
    MAJOR,
    ORDER_FAILURES,
    ORDER_UNRESOLVED,
    PROTECT_MAX_S,
    RAISED_KEY,
    STAGES,
    full_cap,
    on_incident,
    pause,
    paused,
    resume,
)
from ewbot.live.target import Target, TargetError, compute_target, frozen_config
from ewbot.live.watchdog import (
    NY,
    WS_STALE_S,
    AccountGuard,
    BadEquity,
    PriceSource,
    SessionWatch,
    StopGapWatch,
    margin_status,
    ny_date,
)
from ewbot.types import H1

CYCLE_OFFSET_S = 30
CYCLE_RETRY_S = 15
DATA_DEADLINE_S = 120
ENTRY_DEADLINE_S = 300
POLL_ACTIVE_S = 10.0
POLL_IDLE_S = 60.0
AUDIT_MINUTE = 50
DAILY_HOUR_NY = 9
MARGIN_CHECK_NY = (15, 30)
FLATTEN_CONFIRM_S = 60.0  # /flatten_confirm must be sent within this of its /flatten (Telegram message dates)
COMMAND_MAX_AGE_S = 120.0  # a command sent longer ago than this when the loop reaches it is ignored, with a reply
CONTROL_ALERT_FAILURES = 5  # failed getUpdates polls in a row that are alerted
CONTROL_STUCK_S = 120.0  # the command thread this far behind its own schedule (a hung request) is alerted
TELEGRAM_DOWN_S = 600.0  # Telegram failing this long is unusable: healthchecks.io /fail
NOTIFY_STALL_S = 180.0  # messages waiting TELEGRAM_DOWN_S with no send attempt for this long: the sender is stuck
NOTIFY_REFUSALS = 3  # messages Telegram refused in a row (HTTP 4xx, e.g. the bot blocked): Telegram unusable
HC_WITHHOLD_S = 1800.0  # after a Telegram /fail the success pings are withheld at most this long
HC_REFAIL_S = 10_800.0  # and the /fail is repeated this often while Telegram stays unusable
HEARTBEAT_ALERT_FAILURES = 3  # failed healthchecks.io pings in a row that are alerted (on Telegram)
EXPIRY_WARN_DAYS = 30
SNAPSHOT_TRIES = 3  # position / orders / position reads until both position reads agree
FEED_ALERT_S = 300.0  # no WebSocket futures price this long while OPEN is alerted (the watchdog's prices come from REST)
BAD_EQUITY_PAUSE_READS = 3  # balance reads in a row that are not a finite amount above 0: incident, entries paused
HEARTBEAT_KEY = "heartbeat"
AUDIT_KEY = "audit"
AUDIT_PENDING_KEY = "audit_pending"  # mismatches a lag could explain, seen in the last audit (id:type)
ORDER_FAIL_KEY = "order_failures"  # order failures not resolved yet, and the last event id looked at
# what an order failure is about: a bracket (protection), a close or a reduction
FAILURE_CLASS = {"BRACKET_FAILED": "bracket", "CLOSE_FAILED": "close", "CLOSE_UNCONFIRMED": "close",
                 "REDUCE_FAILED": "reduce"}
ACTION_CLASS = {"Protect": "bracket", "Close": "close", "Reduce": "reduce"}  # an ACTION_ERROR (a read failed)
CONFIRM_PASSES = 2  # a bracket fix not confirmed by this many further strict passes ...
CONFIRM_MAX_S = 600.0  # ... and this long after it was placed is unresolved: legs placed again, never as wanted
MANUAL_ALERTS = ("FOREIGN_ORDERS", "UNKNOWN_POSITION", "EXCESS_POSITION")  # planned alerts that are incidents
MANUAL_CLOSES = ("UNKNOWN_POSITION", "DIRECTION_MISMATCH")  # planned closes of a position changed outside our orders


@dataclass
class LiveDeps:
    journal: Journal
    exchange: Exchange
    lock: dict[str, Any]  # the preflight-checked holdout lock: the only source of the strategy configuration
    fetch: Callable[[Path], Path]  # candle cache update -> parquet path (data.coinbase_history.update_cache)
    cache_dir: Path
    notify: Callable[[str, str], Any] = lambda kind, text: None
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    feed: Any | None = None  # live.feed.TickerFeed
    heartbeat: Any | None = None  # live.health.Heartbeat
    commands: queue.Queue[Command] | None = None
    control: Any | None = None  # live.control.TelegramControl: watched every minute, started again when it died
    paper_ledger: Callable[[], dict[str, Any]] | None = None
    edit_stops: bool = False
    expiry: dict[str, str] = field(default_factory=dict)
    redact: Callable[[str], str] = lambda text: text  # removes the settings' secrets from error texts (live.app.redact)


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


def _side(d: int) -> str:
    return "롱" if d > 0 else "숏"


class LiveRuntime:
    def __init__(self, deps: LiveDeps) -> None:
        self.d = deps
        self.journal = deps.journal
        self.ex = deps.exchange
        self.clock = deps.clock
        self.cfg = frozen_config(deps.lock)  # holdout_config(BotConfig(), lock params), checked
        self.stage, self.stage_since = self.journal.stage()  # the stint this process runs (a restart starts a new one)
        self.executor = Executor(self.ex, self.journal, clock=deps.clock, sleep=deps.sleep, alert=self.alert,
                                 tp1_frac=self.cfg.strategy.tp1_frac, edit_stops=deps.edit_stops)
        self.prices = PriceSource(self.ex, deps.feed, clock=deps.clock)
        self.gap = StopGapWatch()
        self.guard = AccountGuard(self.journal, self.cfg.breakers)
        self.sessions = SessionWatch()
        self.target: Target | None = None
        self.frame: pd.DataFrame | None = None  # the candles of the last cycle (the audit's DATA_REVISION check)
        self.product: ProductSpec | None = None
        self.session: SessionInfo | None = None
        self.done_bar: int | None = None
        self.next_try: float | None = None
        self.last_poll: float | None = None
        self.last_minute: float | None = None
        self.active = False
        self.flatten_sent: float | None = None  # the Telegram date of a /flatten awaiting its /flatten_confirm
        self.exit_code: int | None = None
        self.bad_equity_reads = 0  # balance reads in a row that were not an observation
        self.feed_down_since: float | None = None
        self.feed_failures = 0  # the feed's failure count when this outage began
        self.feed_alerted = False
        self.margin_no_price = False  # the margin check was skipped for want of a futures price (alerted once)
        self.tg_down_since: float | None = None  # a Telegram command-thread problem (alerted) since then
        self.tg_conflicts = 0  # the control's HTTP 409 count at the last minute
        self.hc_failed_at: float | None = None  # the latest /fail for Telegram of this episode
        self.hb_alerted = False  # failing healthchecks.io pings were alerted
        raised = self.journal.get(RAISED_KEY) or {}  # the planned manual incidents in force, and their stint
        self.raised = {(str(k), int(t)) for k, t in raised.get("items", [])}
        self.raised_stint, self.raised_ended = raised.get("stint"), raised.get("ended")

    # ------------------------------------------------------------------ helpers
    def alert(self, kind: str, text: str) -> None:
        self.d.notify(kind, text)  # queued (live/notify.py sends it from its own thread): never waits on Telegram

    def _once(self, key: str, value: Any = True) -> bool:
        """True when the journal's `key` is not `value` yet (and records it): periodic jobs keep one row
        per key (e.g. the last New York date of the daily summary), alerts one row per item."""
        if self.journal.get(f"once:{key}") == value:
            return False
        self.journal.put(f"once:{key}", value)
        return True

    def _incident(self, kind: str, now: float) -> None:
        done = on_incident(self.journal, kind, now)
        if done:
            label = "중대 사건" if kind in MAJOR else "CANARY 실패"
            again = " (재개는 /resume)" if "paused" in done else ""
            self.alert("INCIDENT", f"[{label}] {kind}: {', '.join(done)}{again}")

    def _limits(self, now: float) -> tuple[str, int | None]:
        """(stage to size with, contract cap), read from the journal before every entry and adoption: the more
        conservative of the stage this process runs in and the journal's - an incident's or the CLI's demotion
        applies to the next order at once, not only after the restart - and `stage.full_cap`, the journal stage's
        maximum (0 in SHADOW: no real order; 1 in CANARY; 3 in FULL until released)."""
        cur, _ = self.journal.stage()
        stage = min(self.stage, cur if cur in STAGES else "SHADOW", key=STAGES.index)
        return stage, full_cap(self.journal, now)

    def _ctx(self, now: float) -> PlanContext:
        ratio = None
        try:
            ratio = basis_ratio(self.ex.quote(self.ex.futures_id).mid, self.ex.quote(self.ex.spot_id).mid)
        except (ExchangeError, ValueError):
            pass
        session_open = self.session is not None and self.session.is_open
        n, block = self._entry_size(now, ratio, session_open)
        tick = self.product.price_increment if self.product is not None else 5.0
        stage, cap = self._limits(now)
        most = cap if stage == "FULL" else 1  # the stage maximum for a position taken over (a real one in SHADOW too)
        return PlanContext(now=now, ratio=ratio, tick=tick, session_open=session_open, entry_contracts=n,
                           entry_block=block, max_contracts=most)

    def _entry_size(self, now: float, ratio: float | None, session_open: bool) -> tuple[int, str | None]:
        t = self.target
        if t is None or t.entry is None:
            return 0, None
        e = t.entry
        p = paused(self.journal)
        if p is not None:
            return 0, f"일시 정지 중 ({p['reason']})"
        if not session_open:
            return 0, f"장 운영 상태 {self.session.state if self.session else 'UNKNOWN'}"
        if now > e.signal_ts + H1 + ENTRY_DEADLINE_S:
            return 0, "봉 마감 후 5분이 지나 진입하지 않음"
        if ratio is None or self.product is None:
            return 0, "호가를 읽지 못함"
        try:
            equity = self.ex.balance().equity
        except ExchangeError as exc:
            return 0, f"잔고를 읽지 못함: {exc}"
        g = self.guard.state() or {}
        if g.get("total_tripped") or (g.get("daily_tripped") and g.get("day") == ny_date(now)):
            return 0, "실계좌 손실 한도 발동 중"
        stage, cap = self._limits(now)
        if cap == 0 and self.stage != "SHADOW":
            return 0, "단계가 SHADOW로 바뀜: 실주문을 내지 않음 (포지션이 없으면 재시작)"
        tick = self.product.price_increment
        entry = e.entry_ref * ratio
        stop = map_level(e.stop, ratio, tick, e.direction)
        n, why = stage_contracts(stage, equity=equity, entry=entry, stop=stop, direction=e.direction,
                                 risk_mult=e.risk_mult, cfg=self.cfg, full_cap=cap if stage == "FULL" else None)
        if n == 0:
            return 0, why
        projected = margin_ratio(n, entry, e.direction, self.product, equity)
        if projected > MARGIN_ENTRY_MAX:
            return 0, f"오버나이트 증거금 예상 {projected:.0%} > {MARGIN_ENTRY_MAX:.0%}"
        return n, None

    def flat(self) -> bool:
        t = self.executor.trade()
        if t is not None and t.status == "OPEN":
            return False
        return self.ex.position() is None and not [o for o in self.ex.open_orders() if is_ours(o)]

    # ------------------------------------------------------------------ startup
    def startup(self) -> dict[str, int]:
        now = self.clock()
        if self.journal.stage()[1] is None:
            self.journal.set_stage("SHADOW", "initial", now)
            self.stage, self.stage_since = self.journal.stage()
        self.product = self.ex.product()
        self.session = self.ex.session()
        self.sessions.update(self.session, now)
        try:  # the first download of the candle history takes minutes: never inside a timed cycle
            self._frame()
        except Exception as exc:  # noqa: BLE001 - the cycle retries the fetch
            self.journal.add_event(now, "FETCH_ERROR", {"error": self.d.redact(f"{type(exc).__name__}: {exc}")[:200]})
        if isinstance(self.ex, ShadowExchange):
            self.ex.restore(self.executor.trade())
        out = self.executor.recover()
        self.journal.add_event(now, "STARTUP", {"stage": self.stage})
        # the session state seen at start (a change is journaled by minute(); a reopen while down never is)
        self.journal.add_event(now, "SESSION", {"text": f"시작 시 장 운영 상태 {self.session.state}",
                                                "state": self.session.state})
        self.alert("STARTUP", f"[시작] 단계 {self.stage} · 재시작 복구 {out} · {_utc(now)}")
        decided = self._decided_until()  # never decided again into its row: the restart keeps an on-time cycle
        if decided:
            self.done_bar = decided
            if decided == int(now) // H1 * H1 - H1:  # this hour's bar: its target again, its row untouched
                self.cycle(decided, now)
        self.poll(now)
        return out

    # ------------------------------------------------------------------ decision cycle
    def _frame(self) -> pd.DataFrame:
        raw = load_1h(Path(self.d.fetch(Path(self.d.cache_dir))))
        if raw.empty:
            raise TargetError("the candle cache is empty")
        self.frame = fill_gaps(raw)
        return self.frame

    def _cycle_ok(self, bar_ts: int) -> bool:
        return any(int(c["bar_ts"]) == bar_ts and c["status"] == "OK" for c in self.journal.cycles(since_bar=bar_ts))

    def cycle(self, bar_ts: int, now: float) -> str:
        """OK | LATE (bar not there yet) | ERROR. A bar whose cycle already ended OK (before a restart) is decided
        again for its target, its cycles row untouched."""
        decided = self._cycle_ok(bar_ts)
        if self.next_try is None and not decided:
            self.journal.start_cycle(bar_ts, now, self.stage)
        try:
            df = self._frame()
            if int(df["ts"].iloc[-1]) < bar_ts:
                return "LATE"
            target = compute_target(self.d.lock, df, bar_ts)
        except Exception as exc:  # noqa: BLE001 - the cycle fails, protection goes on
            why = self.d.redact(f"{type(exc).__name__}: {exc}")
            if not decided:
                self.journal.finish_cycle(bar_ts, self.clock(), "ERROR", why[:300])
            self.alert("CYCLE_ERROR", f"[사이클 오류] {_utc(bar_ts)} 봉: {why[:200]}")
            self.target = None
            return "ERROR"
        new = self.journal.add_decisions(target.decisions, self.clock())
        self.target = target
        self.journal.put("target", target.to_json())
        note = f"{len(new)} new decisions"
        try:
            self.reconcile(self.clock())
        except ExchangeError as exc:  # the next poll reconciles against this target
            note += f"; reconcile deferred: {exc}"[:200]
        if not decided:
            self.journal.finish_cycle(bar_ts, self.clock(), "OK", note)
        return "OK"

    def _cycle_due(self, now: float) -> None:
        bar = int(now) // H1 * H1 - H1
        if self.done_bar is not None and bar <= self.done_bar:
            return
        if now < bar + H1 + CYCLE_OFFSET_S or (self.next_try is not None and now < self.next_try):
            return
        status = self.cycle(bar, now)
        if status == "LATE" and now < bar + H1 + DATA_DEADLINE_S:
            self.next_try = now + CYCLE_RETRY_S
            return
        if status == "LATE":
            if not self._cycle_ok(bar):
                self.journal.finish_cycle(bar, self.clock(), "HOLD", "bar missing 2 min after the close")
            self.target = None
            self.alert("DATA_LATE", f"[판단 보류] {_utc(bar)} 봉이 마감 후 2분 안에 오지 않음 (보호 주문 유지)")
        self.done_bar, self.next_try = bar, None

    # ------------------------------------------------------------------ reconcile
    def _snapshot(self) -> tuple[Position | None, list[Order]]:
        """Position and open orders as one state: the position is read again after the orders and the pair is
        used only when both position reads agree (a leg filling between the reads would pair a stale position
        with orders that already miss it); otherwise read again, and after SNAPSHOT_TRIES the poll is skipped."""
        for _ in range(SNAPSHOT_TRIES):
            pos = self.ex.position()
            orders = self.ex.open_orders()
            if self.ex.position() == pos:
                return pos, orders
        raise ExchangeError(f"the position changed during {SNAPSHOT_TRIES} reads of the orders")

    def reconcile(self, now: float) -> None:
        self.executor.resolve_reduce()  # a reduce with an unknown outcome: looked up by its id, never a second one
        pos, orders = self._snapshot()
        trade = self.executor.trade()
        seen, events = observe(trade, pos, orders, now)
        # the trades this pass found protected: resting stop legs cover the position (reconcile.observe)
        guarded = ({seen.trade_id} if seen is not None and seen.status == "OPEN" and seen.unprotected_since is None
                   and pos is not None and pos.side == seen.direction else set())
        if seen is not None and seen != trade:
            self.executor.save(seen)
        if trade is not None and any(kind in ("CLOSED", "TP1_FILLED") for kind, _ in events):
            self._bracket_fills(trade, {o.order_id for o in orders if o.is_open}, now)
        for kind, data in events:
            self.journal.add_event(now, kind, data)
            if kind == "CLOSED":
                self.alert(kind, f"[청산 확인] 거래 {data['trade_id']} · {data['reason']}")
            elif kind == "TP1_FILLED":
                self.alert(kind, f"[TP1 체결] 거래 {data['trade_id']} · 잔여 {data['contracts']}계약 · 손절 본전으로")
            elif kind == "UNPROTECTED_60S":
                self._incident(kind, now)
                self.alert(kind, f"[중대 사건] 거래 {data['trade_id']}가 60초 넘게 보호되지 않음")
            elif kind == "REDUCE_NOT_SEEN":
                self.alert(kind, f"[경고] 거래 {data['trade_id']}: 체결된 수량 축소({data['reason']} {data['filled']}계약)가 "
                                 f"1분 넘게 포지션에 반영되지 않음 - 포지션 {data['position']}계약 기준으로 정리")
            elif kind == "UNEXPLAINED_REDUCTION":
                self._incident(kind, now)
                self.alert(kind, f"[경고] 거래 {data['trade_id']}: 주문 없이 {data['expected']}계약 → "
                                 f"{data['position']}계약 (손절 유지, 남은 수량 보호)")
        ctx = self._ctx(now)
        actions = plan(self.target, seen, pos, orders, ctx)
        self.executor.execute(actions, ctx)
        tid = seen.trade_id if seen is not None and seen.status == "OPEN" else 0
        # a manual intervention the planner found: an alert, or the close of a position opened or flipped outside our
        # orders. One incident per (kind, trade) episode - until a pass no longer plans it -, not one per pass
        raised = ({(a.kind, tid) for a in actions if isinstance(a, Alert) and a.kind in MANUAL_ALERTS}
                  | {(a.reason, tid) for a in actions if isinstance(a, Close) and a.reason in MANUAL_CLOSES})
        stint = self.journal.stage()[1]  # a new stint (a restart or a stage change) starts new episodes
        for kind, _ in sorted(raised - (self.raised if self.raised_stint == stint else set())):
            self._incident(kind, now)
        if raised != self.raised or stint != self.raised_stint:
            ended = now if self.raised and not raised else self.raised_ended  # the FULL cap's clean days count from it
            self.raised, self.raised_stint, self.raised_ended = raised, stint, ended
            self.journal.put(RAISED_KEY, {"items": sorted([k, t] for k, t in raised), "ended": ended, "stint": stint})
        if self.journal.events(kind="ADOPTED", since=now):  # a foreign position: taken over now, or a provisional
            self._incident("ADOPTED", now)  # trade settled as foreign (never our own entry rebuilt from its intent)
        t = self.executor.trade()
        # poll fast while a trade is open or an order's outcome is still unknown (looked up, then resent)
        self.active = (t is not None and t.status == "OPEN") or bool(self.journal.intents(status="PENDING"))
        if (t is not None and t.protected_ts is not None and t.protected_ts - t.opened_ts > PROTECT_MAX_S
                and self._once(f"slow:{t.trade_id}")):
            self._incident("PROTECT_SLOW", now)
        protecting = {tid} if tid and any(isinstance(a, Protect) for a in actions) else set()
        self._order_failures(now, ctx, guarded, protecting)

    def _order_failures(self, start: float, ctx: PlanContext, guarded: set[int], protecting: set[int]) -> None:
        """CANARY gate 2 and its pause (spec 6.2), after every completed reconcile pass (begun at `start`; `guarded`: the
        trades it found protected; `protecting`: the trades it planned a Protect for). Each order failure -
        stage.ORDER_FAILURES, or an ACTION_ERROR of a Protect, Close or Reduce - is watched from the pass after it. It
        is resolved when its trade has closed since (a CLOSED event). A bracket must be fixed within PROTECT_MAX_S of
        its failure: by a strict pass (a target, quotes, the session OPEN) that placed the wanted legs - it planned a
        Protect for the trade and had no failure for it -, `fixed`, then confirmed by a later strict pass that
        planned nothing for the trade and had no failure for it (whenever it runs; a pass that fails again or plans
        again leaves it open); a strict pass that finds the legs as wanted within the deadline resolves it at once.
        A pass that cannot judge or place the legs (no target or quotes: a data-late hour; the session not OPEN: a
        venue that refuses order entry while closed) can only show the protection itself - the trade's resting stop
        legs covering its position -: that resolves a failure first seen in such a pass, and is provisional
        (`covered`) for one first seen in a strict pass, which the next strict pass must confirm. A reduction is
        resolved by a strict pass with no reduction pending and no order failure for the trade, or by its
        REDUCED / REDUCE_SETTLED; a close or a reduction must be resolved by the next pass. Otherwise a failure is
        unresolved, a CANARY failure (incident: it pauses CANARY, fails the CANARY gate and in FULL keeps the
        3-contract cap) - so is a fix still not confirmed after CONFIRM_PASSES further strict passes and
        CONFIRM_MAX_S (legs placed again and again, never as wanted); without strict passes (a data or quote outage)
        it waits without escalating. Unresolved failures are grouped in episodes - per
        trade, or per failure while no trade is open - recorded once (ORDER_UNRESOLVED event, incident, then alert):
        an episode ends at the first completed pass where its trade has no failure still inside its window (one
        awaiting its confirmation does not count), and with the stint."""
        stint = self.journal.stage()[1]
        st = self.journal.get(ORDER_FAIL_KEY) or {"seen": 0, "ts": 0.0, "open": [], "episodes": [], "stint": stint}
        t = self.executor.trade()
        open_id = t.trade_id if t is not None and t.status == "OPEN" else 0
        new: list[dict[str, Any]] = []
        for kind in ORDER_FAILURES + ("ACTION_ERROR",):
            for e in self.journal.events(kind=kind, since=float(st["ts"]) - 600.0):
                cls = FAILURE_CLASS.get(kind) or ACTION_CLASS.get(str(e["data"].get("action")))
                if e["id"] > int(st["seen"]) and cls is not None:
                    tid = int(e["data"].get("trade_id") or open_id)
                    new.append({"id": e["id"], "kind": kind, "cls": cls, "trade_id": tid, "ts": float(e["ts"]),
                                "episode": f"t{tid}" if tid else f"e{e['id']}",  # no trade: an episode of its own
                                "strict": None})  # set below: whether the pass that registers it is strict
        failing = {f["trade_id"] for f in new if f["ts"] >= start}  # trades with an order failure in this pass
        whole = self.target is not None and ctx.ratio is not None  # the pass saw what the trade should look like
        strict = whole and ctx.session_open  # ... and could place the legs: they are judged, not just the protection
        reducible = strict and (t is None or t.pending_reduce is None)
        for f in new:
            f["strict"] = strict

        def after(f: dict[str, Any], kinds: tuple[str, ...]) -> float | None:
            """When the first event of `kinds` for f's trade after f was recorded (None: none yet)."""
            return min((e["ts"] for k in kinds for e in self.journal.events(kind=k, since=f["ts"])
                        if e["id"] > f["id"] and int(e["data"].get("trade_id") or 0) == f["trade_id"]), default=None)

        kept: list[dict[str, Any]] = []
        done: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        marked = False  # a kept failure got its fix or its provisional protection recorded
        for f in st["open"]:
            closed = after(f, ("CLOSED",))
            if f["cls"] == "bracket":
                deadline, tid = f["ts"] + PROTECT_MAX_S, f["trade_id"]
                fixed, covered = f.get("fixed"), f.get("covered")
                if closed is not None and (closed <= deadline or fixed is not None):
                    done.append(f)
                elif strict and tid not in failing and tid not in protecting:  # the legs are the wanted ones
                    (done if fixed is not None or covered is not None or start <= deadline else unresolved).append(f)
                elif strict:  # this pass planned the trade's legs or failed again: no confirmation
                    if f.pop("covered", None) is not None:
                        marked = True
                    if tid not in failing and fixed is None and (start <= deadline or covered is not None):
                        f["fixed"], f["waits"], marked = start, 0, True  # placed in time: the next clean pass confirms
                    elif fixed is not None:
                        f["waits"], marked = int(f.get("waits", 0)) + 1, True
                    stale = (fixed is not None and f["waits"] >= CONFIRM_PASSES
                             and start - float(fixed) >= CONFIRM_MAX_S)  # placed again and again, never as wanted
                    (kept if (f.get("fixed") is not None or start <= deadline) and not stale else unresolved).append(f)
                elif tid in guarded and start <= deadline and not f.get("strict", True):
                    done.append(f)  # first seen where the legs could not be judged: the protection is the evidence
                else:  # the protection seen is provisional for a failure first seen in a strict pass
                    if tid in guarded and start <= deadline and covered is None and fixed is None:
                        f["covered"], marked = start, True
                    held = fixed is not None or f.get("covered") is not None or start <= deadline
                    (kept if held else unresolved).append(f)
            elif f["cls"] == "close":
                (done if closed is not None else unresolved).append(f)
            else:
                ok = (closed is not None or (reducible and f["trade_id"] not in failing)
                      or after(f, ("REDUCED", "REDUCE_SETTLED")) is not None)
                (done if ok else unresolved).append(f)
        # an episode lasts while its trade has an open failure (and within its stint): a later failure is a new one
        episodes = set(st.get("episodes", [])) if st.get("stint") == stint else set()
        opened = sorted({f["episode"] for f in unresolved} - episodes)
        # only a failure still inside its window keeps its episode alive, not one awaiting its confirmation
        waiting = {f["episode"] for f in kept + new if f.get("fixed") is None and f.get("covered") is None}
        alive = (episodes | set(opened)) & waiting
        if new or done or unresolved or marked or alive != episodes or st.get("stint") != stint:  # written on change
            seen = max([int(st["seen"])] + [f["id"] for f in new])
            self.journal.put(ORDER_FAIL_KEY, {"seen": seen, "ts": start, "open": kept + new,
                                              "episodes": sorted(alive), "stint": stint})
        for f in done:
            self.journal.add_event(start, "ORDER_RESOLVED", {"kind": f["kind"], "trade_id": f["trade_id"],
                                                             "failed_ts": f["ts"], "after_s": round(start - f["ts"], 3)})
        if not opened:
            return
        for ep in opened:
            first = min((f for f in unresolved if f["episode"] == ep), key=lambda f: f["id"])
            self.journal.add_event(start, ORDER_UNRESOLVED, {"kind": first["kind"], "trade_id": first["trade_id"],
                                                            "failed_ts": first["ts"], "event_id": first["id"]})
        self._incident(ORDER_UNRESOLVED, start)  # the pause first, the alert second
        for ep in opened:
            mine = [f for f in unresolved if f["episode"] == ep]
            kinds = ", ".join(sorted({f["kind"] for f in mine}))
            self.alert(ORDER_UNRESOLVED, f"[주문 실패 미해결] 거래 {mine[0]['trade_id']}: {kinds} - 제시간에 해결되지 않음 "
                                         "(CANARY 기준 ② 실패)")

    def _bracket_fills(self, trade: Any, still_open: set[str], now: float) -> None:
        """Record the fills of the legs that left the book (TP or stop), for the cost report."""
        for leg in trade.legs:
            if not leg.order_id or leg.order_id in still_open or not self._once(f"fill:{leg.order_id}"):
                continue
            try:
                o = self.ex.get_order(leg.order_id)
            except ExchangeError:
                continue
            if o.filled > 0 and o.avg_fill_price:
                self.journal.add_event(now, "BRACKET_FILL", {
                    "trade_id": trade.trade_id, "leg": leg.name, "side": o.side, "contracts": o.filled,
                    "price": o.avg_fill_price, "fee": o.fee, "stop": leg.stop_price, "limit": leg.limit_price})

    def poll(self, now: float) -> None:
        self.last_poll = now
        try:
            self.reconcile(now)
        except ExchangeError as exc:
            self.journal.add_event(now, "POLL_ERROR", {"error": str(exc)[:200]})

    def watch(self, now: float) -> None:
        t = self.executor.trade()
        if t is None or t.status != "OPEN":
            self.gap.reset()
            return
        price = self.prices.futures_price()
        if price is None:
            return
        if isinstance(self.ex, ShadowExchange):
            self.ex.set_prices(price)  # the virtual brackets see every price the watchdog sees
        if t.unprotected_since is not None and t.direction * (price - t.stop_price) <= 0:
            # no resting stop (e.g. the venue refused the bracket) and the price is through the stop: close, then alert
            self.journal.add_event(now, "STOP_UNPROTECTED", {"trade_id": t.trade_id, "price": price, "stop": t.stop_price})
            try:
                self.executor.close("STOP_UNPROTECTED", self._ctx(now))
            finally:
                self.alert("STOP_UNPROTECTED", f"[손절] 보호 주문 없이 가격 {price:,.1f}이 손절 {t.stop_price:,.0f}을 지남: 시장가 청산")
            return
        if not self.gap.check(t.direction, t.stop_price, price, now):
            return
        self.journal.add_event(now, "STOP_GAP", {"trade_id": t.trade_id, "price": price, "stop": t.stop_price})
        try:
            self.executor.close("STOP_GAP", self._ctx(now))
            self.gap.reset()
        finally:
            self.alert("STOP_GAP", f"[손절 갭] 가격 {price:,.1f}이 손절 {t.stop_price:,.0f}의 {STOP_LIMIT_OFFSET:.0%} 지정가를 10초 넘게 지남: 시장가 청산")

    # ------------------------------------------------------------------ every minute
    def minute(self, now: float) -> None:
        self.last_minute = now
        self.journal.put(HEARTBEAT_KEY, now)
        try:
            self._telegram_health(now)
        except Exception as exc:  # noqa: BLE001 - never at the cost of the session, breaker and margin checks
            self.journal.add_event(now, "LOOP_ERROR", {"section": "telegram", "error": type(exc).__name__})
            print(f"[LIVE] telegram and heartbeat check failed: {type(exc).__name__}", file=sys.stderr)
        try:
            was_open = self.session is not None and self.session.is_open
            self.session = self.ex.session()
            for kind, text in self.sessions.update(self.session, now):
                self.journal.add_event(now, kind, {"text": text, "state": self.session.state} if kind == "SESSION"
                                       else {"text": text})
                self.alert(kind, f"[장 상태] {text}")
            if self.session.is_open and not was_open:
                self.poll(now)  # deferred closes run right after the reopen (spec 4.5)
        except ExchangeError as exc:
            self.journal.add_event(now, "SESSION_ERROR", {"error": str(exc)[:200]})
        self._money(now)
        self._feed_health(now)
        if self.journal.intents(status="PENDING"):
            try:
                self.executor.resolve_pending()
            except ExchangeError:
                pass
        local = datetime.fromtimestamp(now, NY)
        today = local.date().isoformat()
        if (local.hour, local.minute) >= MARGIN_CHECK_NY and local.hour < 16 and self.journal.get("once:margin1530") != today:
            self._overnight_report(now)  # marks the day done once its reads succeed
        if local.hour >= DAILY_HOUR_NY and self._once("daily", today):
            self.daily(now)
        if datetime.fromtimestamp(now, UTC).minute >= AUDIT_MINUTE and self._once("audit", int(now) // H1):
            self.run_audit(now)
        stage, since = self.journal.stage()
        if (stage != self.stage or since != self.stage_since) and self.flat():  # a new stage or a new stint
            again = " (다시 시작)" if stage == self.stage else ""
            self.alert("STAGE", f"[단계 변경] {self.stage} → {stage}{again}: 재시작해 적용")
            self.exit_code = 0

    def _control_problem(self, now: float) -> str | None:
        """What is wrong with the Telegram command thread this minute, or None. A dead thread is started again. A
        thread CONTROL_STUCK_S behind its own schedule (a hung request) is only reported: a second thread would poll
        the same bot beside the hung one (HTTP 409) and leak; restarting the container ends both."""
        c = self.d.control
        if c is None:
            return None
        conflicts, self.tg_conflicts = c.conflicts - self.tg_conflicts, c.conflicts
        if not c.alive:
            if self.d.commands is not None:
                c.start(self.d.commands)
            return f"명령 수신 스레드가 멈춰 다시 시작함 (마지막 오류 {c.last_error or '-'})"
        overdue = getattr(c, "overdue", None)  # TelegramControl.overdue; a control without it is never late
        late = float(overdue(now)) if callable(overdue) else 0.0
        if late >= CONTROL_STUCK_S:
            return f"명령 수신 스레드가 {late / 60:.0f}분 넘게 응답 없음 (걸린 요청: 컨테이너를 다시 시작해야 함)"
        if c.failures >= CONTROL_ALERT_FAILURES:
            return f"getUpdates {c.failures}회 연속 실패 ({c.last_error or '-'})"
        if conflicts > 0:
            return f"getUpdates 충돌 {conflicts}회 (HTTP 409: 같은 봇 토큰으로 다른 프로세스나 웹훅이 명령을 받음)"
        return None

    def _notify_problem(self, now: float) -> str | None:
        """Why the notifier (live.notify.LiveNotifier) cannot deliver, or None: its oldest failing message has waited
        TELEGRAM_DOWN_S; messages have waited that long with no send attempt for NOTIFY_STALL_S (a stuck or stopped
        sender thread); or Telegram refused NOTIFY_REFUSALS messages in a row. A recorder without these attributes
        (tests) never has a problem."""
        n: Any = self.d.notify
        failing, oldest = getattr(n, "failing_since", None), getattr(n, "oldest_pending", None)
        attempt, refusals = getattr(n, "last_attempt", None), getattr(n, "refusals", 0)
        why = getattr(n, "last_error", None) or "-"
        if failing is not None and now - failing >= TELEGRAM_DOWN_S:
            return f"알림 전송이 {(now - failing) / 60:.0f}분째 실패 ({why})"
        if (oldest is not None and now - oldest >= TELEGRAM_DOWN_S
                and (attempt is None or now - attempt >= NOTIFY_STALL_S)):
            return f"알림 전송 스레드가 멈춤 (대기 {getattr(n, 'pending', 0)}건, {(now - oldest) / 60:.0f}분째 시도 없음)"
        if refusals >= NOTIFY_REFUSALS:
            return f"텔레그램이 알림 {refusals}건을 연속 거부 ({why})"
        return None

    def _telegram_health(self, now: float) -> None:
        """The Telegram command thread and the external heartbeat (spec 3.2 `control`, `health`). A command-thread
        problem - the thread died (it is started again) or hangs, CONTROL_ALERT_FAILURES failed polls in a row (e.g.
        HTTP 401: the token refused) or a new HTTP 409 (another poller or a webhook on this bot: commands would be
        split) - is alerted once per episode and once more when it is over. Telegram is unusable while such a problem
        has lasted TELEGRAM_DOWN_S or the notifier cannot deliver (`_notify_problem`): healthchecks.io then gets a
        /fail naming it (the check's email reaches the user without Telegram) and the success pings are withheld
        for at most HC_WITHHOLD_S, so the check stays down; then the pings resume - a hung loop or a dead VPS
        still stops them - and the /fail is repeated every HC_REFAIL_S while Telegram stays unusable. Otherwise the
        minute's success ping goes out (from this loop: a hung loop stops it), and HEARTBEAT_ALERT_FAILURES failed
        pings in a row are alerted once."""
        problem = self._control_problem(now)
        if problem is None and self.tg_down_since is not None:
            self.journal.add_event(now, "TELEGRAM_UP", {"down_s": now - self.tg_down_since})
            self.alert("TELEGRAM", "[텔레그램 복구] 명령 수신 정상")
            self.tg_down_since = None
        elif problem is not None and self.tg_down_since is None:
            self.tg_down_since = now
            self.journal.add_event(now, "TELEGRAM_DOWN", {"problem": problem})
            self.alert("TELEGRAM", f"[텔레그램 명령 수신 이상] {problem} - 명령이 듣지 않으면 VPS에서 ewbot live status")
        down: list[str] = []
        if problem is not None and self.tg_down_since is not None and now - self.tg_down_since >= TELEGRAM_DOWN_S:
            down.append(f"명령 수신: {problem}")
        sending = self._notify_problem(now)
        if sending is not None:
            down.append(sending)
        hb = self.d.heartbeat
        if down:
            if self.hc_failed_at is None or now - self.hc_failed_at >= HC_REFAIL_S:
                self.hc_failed_at = now
                self.journal.add_event(now, "TELEGRAM_UNUSABLE", {"reasons": down})
                if hb is not None:
                    hb.fail("[LIVE] 텔레그램 사용 불가 - " + "; ".join(down))
            if now - self.hc_failed_at < HC_WITHHOLD_S:
                return  # no success ping yet: the check stays down (a failed /fail brings it down after its grace)
        else:
            self.hc_failed_at = None
        if hb is None:
            return
        hb.beat()
        if hb.failures >= HEARTBEAT_ALERT_FAILURES and not self.hb_alerted:
            self.hb_alerted = True
            self.journal.add_event(now, "HEARTBEAT_FAILING", {"failures": hb.failures, "error": hb.last_error})
            self.alert("HEARTBEAT", f"[외부 감시 핑 실패] healthchecks.io 핑이 {hb.failures}회 연속 실패 "
                                    f"({hb.last_error}): VPS가 멈춰도 경보가 오지 않을 수 있음")
        elif hb.failures == 0 and self.hb_alerted:
            self.hb_alerted = False
            self.alert("HEARTBEAT", "[외부 감시 핑 복구] healthchecks.io 핑 정상")

    def _money(self, now: float) -> None:
        try:
            equity = self.ex.balance().equity
            trip = self.guard.update(equity, now)  # trips on the second breaching read in a row
        except ExchangeError:
            return
        except BadEquity:  # not an observation: no breaker sample and no margin check this minute
            self.bad_equity_reads += 1
            if self.bad_equity_reads == 1:
                self.journal.add_event(now, "BAD_EQUITY", {"equity": repr(equity)})
                self.alert("BAD_EQUITY", f"[잔고 이상] 자산 값 {equity!r}: 손실 한도와 증거금 점검을 건너뜀 "
                                         "(정상 값이 읽히면 다시 점검)")
            if self.bad_equity_reads == BAD_EQUITY_PAUSE_READS:  # persistent: no new risk without a balance
                if paused(self.journal) is None:
                    pause(self.journal, "incident BAD_EQUITY", now)
                self._incident("BAD_EQUITY", now)
                self.alert("INCIDENT", f"[중대 사건] 잔고 이상이 {BAD_EQUITY_PAUSE_READS}회 연속: 신규 진입 일시 정지 "
                                       "(보호 주문과 청산은 계속, 재개는 /resume)")
            return
        self.bad_equity_reads = 0
        if trip is not None:  # pause and flatten first, alert second
            self.journal.add_event(now, "BREAKER", {"kind": trip, "equity": equity})
            if paused(self.journal) is None:
                pause(self.journal, f"breaker {trip}", now)
            try:
                self.executor.flatten(f"BREAKER_{trip}", self._ctx(now))
            finally:
                self._incident("BREAKER", now)
                self.alert("BREAKER", f"[실계좌 손실 한도] {trip}: 전량 청산하고 일시 정지 (자산 {equity:,.2f} USD)")
            return
        t = self.executor.trade()
        try:
            pos = self.ex.position() if t is not None and t.status == "OPEN" else None
        except ExchangeError:
            return  # the next minute checks the margin again (the rest of the minute still runs)
        if pos is None or self.product is None:
            return
        price = self.prices.futures_price()
        if price is None:  # never reduce or close on a stand-in (the entry price may read 0): skip, alert once
            if not self.margin_no_price:
                self.journal.add_event(now, "MARGIN_NO_PRICE", {"contracts": pos.contracts})
                self.alert("MARGIN", "[증거금 점검: 가격 없음] 선물 가격을 읽지 못해 증거금 점검을 건너뜀 "
                                     "(가격이 돌아오면 다시 점검)")
            self.margin_no_price = True
            return
        self.margin_no_price = False
        st = margin_status(pos.contracts, price, pos.side, self.product, equity)
        if st.level == "WARN" and self._once("marginwarn", int(now) // H1):
            self.alert("MARGIN", f"[증거금 경고] 오버나이트 기준 {st.ratio:.0%} (80% 이상)")
        elif st.level == "REDUCE" and st.reduce_by > 0:
            try:
                if st.reduce_by >= pos.contracts:
                    self.executor.close("MARGIN", self._ctx(now))
                else:
                    self.executor.reduce(st.reduce_by, "MARGIN", self._ctx(now))
            finally:
                self.alert("MARGIN", f"[증거금 축소] 오버나이트 기준 {st.ratio:.0%}: {st.reduce_by}계약 축소")

    def _feed_health(self, now: float) -> None:
        """The watchdog's WebSocket price: no futures tick for FEED_ALERT_S while the session is OPEN - a dead
        connection, or a refused subscription while the heartbeats still flow - is alerted once per outage
        (prices come from REST meanwhile), and once more when futures ticks are back. A closed or halted venue
        has no trades: that is no outage."""
        feed = self.d.feed
        if feed is None:
            return
        tick = feed.latest(self.ex.futures_id)
        if tick is not None and now - tick.ts <= WS_STALE_S:
            if self.feed_alerted:
                self.journal.add_event(now, "FEED_UP", {"down_s": now - (self.feed_down_since or now)})
                self.alert("FEED", "[가격 피드 복구] 웹소켓 선물 가격 수신 재개")
            self.feed_down_since, self.feed_alerted = None, False
            return
        if self.session is None or not self.session.is_open:
            if not self.feed_alerted:  # no trades while the venue is not OPEN: only OPEN time counts to an alert
                self.feed_down_since = None
            return
        if self.feed_down_since is None:
            self.feed_down_since = now
            self.feed_failures = feed.failures
        down = now - self.feed_down_since
        if self.feed_alerted or down < FEED_ALERT_S:
            return
        self.feed_alerted = True
        failures = feed.failures - self.feed_failures
        self.journal.add_event(now, "FEED_DOWN", {"down_s": down, "failures": failures, "last_error": feed.last_error,
                                                  "mode": self.prices.mode})
        self.alert("FEED", f"[가격 피드 끊김] 웹소켓 선물 가격이 {down / 60:.0f}분 넘게 없음: 감시 가격은 REST 10초 폴링 "
                           f"(재연결 실패 {failures}회, 마지막 오류 {feed.last_error or '-'})")

    def _overnight_report(self, now: float) -> None:
        """The 15:30 New York check before the 16:00 overnight margin. Failed reads are retried every minute until
        16:00 (the first failure of the day is alerted); the day is marked done once the reads succeed."""
        today = datetime.fromtimestamp(now, NY).date().isoformat()
        try:
            pos = self.ex.position()
            balance = self.ex.balance() if pos is not None else None
        except ExchangeError as exc:
            if self._once("margin1530fail", today):
                self.journal.add_event(now, "MARGIN_1530", {"ratio": None, "error": str(exc)[:200]})
                self.alert("MARGIN_1530", f"[15:30 증거금 점검] 조회 실패: {str(exc)[:120]} (16시 전까지 매분 다시 시도)")
            return
        self._once("margin1530", today)
        if pos is None or balance is None or self.product is None:
            return
        equity = usable_price(balance.equity)
        price = self.prices.futures_price() or usable_price(pos.avg_entry)  # a report: the entry price may stand in
        if price is None or equity is None:
            self.journal.add_event(now, "MARGIN_1530", {"ratio": None, "contracts": pos.contracts})
            self.alert("MARGIN_1530", f"[15:30 증거금 점검] 가격 또는 잔고를 읽지 못해 계산하지 못함 ({pos.contracts}계약)")
            return
        r = margin_ratio(pos.contracts, price, pos.side, self.product, equity)
        self.journal.add_event(now, "MARGIN_1530", {"ratio": r})
        self.alert("MARGIN_1530", f"[15:30 증거금 점검] 16시 오버나이트 기준 예상 {r:.0%} ({pos.contracts}계약)")

    # ------------------------------------------------------------------ audit, summary, reminders
    def _data_hash(self) -> Callable[[int], str | None] | None:
        """The live candles' `data_fingerprint(df, 0, ts)` (the paper ledger records the same hash per decision and per
        run) for the audit's DATA_REVISION check; None before candles were loaded, and for a bar after them."""
        df = self.frame
        if df is None or df.empty:
            return None
        last = int(df["ts"].iloc[-1])
        memo: dict[int, str] = {}

        def digest(ts: int) -> str | None:
            if ts > last:
                return None
            if ts not in memo:
                memo[ts] = data_fingerprint(df, 0, ts)
            return memo[ts]

        return digest

    def _decided_until(self) -> int:
        """The last bar whose cycle ended OK (0: none): the live runtime has decided every bar up to it."""
        return max((int(c["bar_ts"]) for c in self.journal.cycles() if c["status"] == "OK"), default=0)

    def run_audit(self, now: float) -> AuditResult | None:
        """The journal's decisions against the paper ledger (spec 3.5), up to the last bar whose cycle ended OK. A
        mismatch the candles explain (DATA_REVISION: the bar changed between the two fetches) is alerted once and is
        no incident; a decision missing on one side is AUDIT_PENDING first (alerted once) and AUDIT_MISMATCH - a
        major incident, its pause before its alert - only when the next audit still finds it; a different decision
        or a paper fingerprint change, which no lag causes, is one at once. A failed audit (the fetch, an invalid
        ledger, anything else) is stored as failed - the gates never pass on the last good one - and alerted once
        per failing episode."""
        if self.d.paper_ledger is None:
            return None
        try:
            res = audit(self.journal.decisions(), self.d.paper_ledger(), BASELINE_FINGERPRINT,
                        until=self._decided_until(), now=now, data_hash=self._data_hash())
        except Exception as exc:  # noqa: BLE001 - every failure is a failed audit, never the last good one
            why = self.d.redact(str(exc) if isinstance(exc, AuditError) else f"{type(exc).__name__}: {exc}")[:300]
            res = AuditResult.failed(why, now)
            self.journal.put(AUDIT_KEY, res.to_json())
            self.journal.add_event(now, "AUDIT_ERROR", {"error": why})
            if self._once("auditfail", True):
                self.alert("AUDIT", f"[모의매매 대조 실패] {why[:200]} (대조가 다시 성공할 때까지 단계 기준 미충족)")
            return res
        self.journal.put(AUDIT_KEY, res.to_json())
        if self.journal.get("once:auditfail") is True:
            self.journal.put("once:auditfail", False)
            self.alert("AUDIT", "[모의매매 대조 복구] 대조가 다시 성공함")

        def key(m: dict[str, Any]) -> str:
            return f"{m['id']}:{m['type']}"

        before = set(self.journal.get(AUDIT_PENDING_KEY) or [])
        laggable = {key(m) for m in res.mismatches
                    if m["cause"] != "DATA_REVISION" and m["type"] in ("MISSING_IN_LIVE", "MISSING_IN_PAPER")}
        self.journal.put(AUDIT_PENDING_KEY, sorted(laggable))
        revised = [m for m in res.mismatches if m["cause"] == "DATA_REVISION" and self._once(f"revision:{key(m)}")]
        pending = [m for m in res.mismatches if key(m) in laggable - before and self._once(f"pending:{key(m)}")]
        major = [m for m in res.mismatches if m["cause"] != "DATA_REVISION" and (key(m) not in laggable or key(m) in before)
                 and self._once(f"mismatch:{key(m)}")]

        def lines(items: list[dict[str, Any]]) -> str:
            return "\n".join(f"{m['type']} {m['id']}" for m in items[:10])

        if revised:
            self.journal.add_event(now, "AUDIT_REVISION", {"items": revised[:20]})
            self.alert("AUDIT_REVISION", "[판단 불일치: 봉 수정] 두 런타임이 받은 캔들이 그 봉에서 다름 "
                                         "(중대 사건 아님, 확인 필요)\n" + lines(revised))
        if pending:
            self.journal.add_event(now, "AUDIT_PENDING", {"items": pending[:20]})
            self.alert("AUDIT_PENDING", "[판단 불일치 확인 중] 다음 대조에서도 남으면 중대 사건\n" + lines(pending))
        if major:
            self.journal.add_event(now, "AUDIT_MISMATCH", {"items": major[:20]})
            self._incident("AUDIT_MISMATCH", now)
            self.alert("AUDIT_MISMATCH", "[판단 불일치] 실거래 vs 모의매매\n" + lines(major))
        return res

    def costs(self, since: float) -> dict[str, Any]:
        """Measured costs since `since`: fills, entry slippage vs. the engine reference, stop slippage vs. the
        stop (positive = worse), fees vs. the paper cost model. Funding is in the balance, not measured here."""
        kinds = ("ENTRY", "CLOSED", "REDUCED", "BRACKET_FILL")
        fills = [e["data"] for k in kinds for e in self.journal.events(kind=k, since=since) if e["data"].get("price")]
        entries = [e["data"] for e in self.journal.events(kind="ENTRY", since=since) if e["data"].get("ref")]
        entry_bps = [d["side"] * (d["price"] - d["ref"]) / d["ref"] * 1e4 for d in entries]
        stops = [d for d in (e["data"] for e in self.journal.events(kind="BRACKET_FILL", since=since))
                 if abs(d["price"] - d["stop"]) < abs(d["price"] - d["limit"])]
        stop_bps = [d["side"] * (d["price"] - d["stop"]) / d["stop"] * 1e4 for d in stops]
        fees = sum(float(f.get("fee") or 0.0) for f in fills)
        model = sum(model_fee(float(f["price"]), int(f.get("contracts") or 0), self.cfg.costs) for f in fills)
        return {"fills": len(fills), "slippage_bps": (sum(entry_bps) / len(entry_bps)) if entry_bps else None,
                "stop_slippage_bps": (sum(stop_bps) / len(stop_bps)) if stop_bps else None, "fees": fees,
                "model_fees": model}

    def daily(self, now: float) -> None:
        local = datetime.fromtimestamp(now, NY).date()
        c = self.costs(now - 86_400)
        if c["fills"]:
            self.journal.add_event(now, "COSTS", c)
        lines = [f"[일일 요약] {local} (뉴욕)"] + status_lines(self.journal, now)
        slip = "-" if c["slippage_bps"] is None else f"{c['slippage_bps']:+.1f} bps"
        stop_slip = "-" if c["stop_slippage_bps"] is None else f"{c['stop_slippage_bps']:+.1f} bps"
        lines.append(f"체결 24시간: {c['fills']}건 · 진입 슬리피지 {slip} · 손절 슬리피지 {stop_slip} · 수수료 실측 "
                     f"{c['fees']:.2f} USD (모의 가정 {c['model_fees']:.2f} USD) · 펀딩은 잔고 변화에 포함")
        try:
            equity = self.ex.balance().equity
            lines.append(f"자산 {equity:,.2f} USD")
            pos = self.ex.position()
            if pos is not None and self.product is not None:
                price = self.prices.futures_price() or usable_price(pos.avg_entry)  # display only
                if price is None or usable_price(equity) is None:
                    lines.append("오버나이트 증거금: 가격 또는 잔고 없음")
                else:
                    lines.append(f"오버나이트 증거금 {margin_ratio(pos.contracts, price, pos.side, self.product, equity):.0%}")
        except ExchangeError:
            lines.append("잔고를 읽지 못함")
        self.alert("DAILY", "\n".join(lines))
        for name, day in sorted(self.d.expiry.items()):
            left = (datetime.fromisoformat(day).date() - local).days
            if left <= EXPIRY_WARN_DAYS:
                self.alert("EXPIRY", f"[만료 예정] {name}: {day} ({left}일 남음) - 갱신 필요")
        try:
            if self.ex.key_permissions().can_transfer:
                self._incident("TRANSFER_KEY", now)
                self.alert("TRANSFER_KEY", "[중대 사건] API 키에 송금 권한이 있음: 즉시 키를 교체")
        except ExchangeError:
            pass

    # ------------------------------------------------------------------ Telegram commands
    def handle(self, cmd: str, now: float, sent: float | None = None) -> str:
        """One Telegram command. `sent` is the message's Telegram date (`now` when not given): /flatten_confirm
        counts only when sent within FLATTEN_CONFIRM_S of the /flatten it confirms."""
        sent = now if sent is None else float(sent)
        stamp = datetime.fromtimestamp(now, NY).strftime("%H:%M:%S")
        if cmd == "/status":
            reply = "\n".join(status_lines(self.journal, now))
        elif cmd == "/pause":
            pause(self.journal, "user /pause", now)
            reply = "일시 정지: 신규 진입 중단 (보호 주문과 청산은 계속)"
        elif cmd == "/resume":
            resume(self.journal, now)
            reply = "재개: 다음 신호부터 진입"
            try:  # only a breaker that tripped is re-based at the current equity
                rebased: list[str] | None = self.guard.reset(self.ex.balance().equity, now)
            except (ExchangeError, BadEquity):
                rebased = None
            if rebased:
                reply += f" · 손실 한도 기준을 다시 잡음 (지금과 다음 두 번 조회의 중앙값): {', '.join(rebased)}"
            elif rebased is None:
                reply += " · 잔고를 읽지 못해 손실 한도 기준은 그대로 (발동 중이면 다시 /resume)"
        elif cmd == "/flatten":
            self.flatten_sent = sent
            reply = "60초 안에 /flatten_confirm 을 보내면 전량 청산하고 일시 정지합니다"
        elif cmd == "/flatten_confirm":
            if self.flatten_sent is None or not 0.0 <= sent - self.flatten_sent <= FLATTEN_CONFIRM_S:
                reply = "확인할 /flatten 요청이 없음 (먼저 /flatten)"
            else:
                self.flatten_sent = None
                pause(self.journal, "user /flatten", now)
                ok = self.executor.flatten("USER_FLATTEN", self._ctx(now))
                reply = "전량 청산 완료, 일시 정지" if ok else "청산 요청을 보냈으나 아직 확인되지 않음 (계속 재시도)"
        else:
            reply = "/status · /pause · /resume · /flatten (+ /flatten_confirm 60초 안)"
        self.journal.add_event(now, "COMMAND", {"cmd": cmd, "reply": reply[:200]})
        self.alert("COMMAND", f"{reply}\n({stamp} 뉴욕)")
        return reply

    # ------------------------------------------------------------------ main loop
    def _commands(self, now: float) -> None:
        q = self.d.commands
        while q is not None and not q.empty():
            cmd = q.get_nowait()
            if now - cmd.sent > COMMAND_MAX_AGE_S:  # e.g. sent while the runtime restarted: never acted on late
                self.journal.add_event(now, "COMMAND_STALE", {"cmd": cmd.word, "sent": cmd.sent})
                self.alert("COMMAND", f"오래된 명령 무시: {cmd.word} ({now - cmd.sent:.0f}초 전에 보냄 - 필요하면 다시 보내 주세요)")
                continue
            try:
                self.handle(cmd.word, now, cmd.sent)
            except ExchangeError as exc:
                self.alert("COMMAND", f"{cmd.word} 실패: {str(exc)[:200]} (다시 보내 주세요)")

    def tick(self) -> None:
        now = self.clock()
        sections: list[tuple[str, Callable[[float], None], bool]] = [
            ("commands", self._commands, self.d.commands is not None),
            ("watch", self.watch, self.active),
            ("poll", self.poll, self.last_poll is None or now - self.last_poll >= (POLL_ACTIVE_S if self.active else POLL_IDLE_S)
             or (self.d.feed is not None and self.d.feed.take_resync())),
            ("minute", self.minute, self.last_minute is None or now - self.last_minute >= 60.0),
            ("cycle", self._cycle_due, True),
        ]
        for name, fn, due in sections:
            if not due:
                continue
            try:
                fn(now)
            except Exception as exc:  # noqa: BLE001 - one failing section must not stop the others
                why = self.d.redact(f"{type(exc).__name__}: {exc}")
                self.journal.add_event(now, "LOOP_ERROR", {"section": name, "error": why[:300]})
                self.alert("LOOP_ERROR", f"[루프 오류] {name}: {why[:200]}")
                print(self.d.redact(traceback.format_exc()), file=sys.stderr, end="")

    def run_forever(self, max_ticks: int | None = None) -> int:
        self.startup()
        n = 0
        while self.exit_code is None and (max_ticks is None or n < max_ticks):
            self.tick()
            n += 1
            self.d.sleep(1.0)
        return 0 if self.exit_code is None else self.exit_code


def status_lines(journal: Journal, now: float) -> list[str]:
    """Journal-only status (no exchange call): `ewbot live status`, /status and the daily summary."""
    stage, since = journal.stage()
    days = "" if since is None else f" (D+{(now - since) / 86_400:.1f})"
    cap = ""
    if stage == "FULL":
        most = full_cap(journal, now)
        cap = " · 상한 해제" if most is None else f" · 상한 {most}계약"
    p = paused(journal)
    lines = [f"단계 {stage}{days}{cap} · {'일시 정지: ' + p['reason'] if p else '운영 중'}"]
    t = journal.get("trade")
    if t and t.get("status") == "OPEN":
        legs = ", ".join(f"{leg['name']} {leg['size']}@{leg['limit_price']:,.0f}" for leg in t.get("legs", []))
        lines.append(f"포지션: {t['setup']} {_side(t['direction'])} {t['contracts']}계약 @ {t['entry_price']:,.1f} · "
                     f"손절 {t['stop_price']:,.0f} · TP1 {'체결' if t.get('tp1_filled') else '대기'} · 묶음 [{legs}]")
    else:
        lines.append("포지션: 없음")
    cycles = journal.cycles()
    if cycles:
        c = cycles[-1]
        took = "-" if c["finished_ts"] is None else f"마감 후 {c['finished_ts'] - (c['bar_ts'] + H1):.0f}초"
        lines.append(f"마지막 사이클: {_utc(c['bar_ts'])} 봉 · {c['status']} · {took}")
    a = audit_from_journal(journal)
    if a is not None and a.error is not None:
        lines.append(f"모의매매 대조: 실패 - {a.error[:120]} ({_utc(a.as_of)})")
    elif a is not None:
        explained = len(a.mismatches) - len(a.unexplained())
        lines.append(f"모의매매 대조: 비교 {a.compared}건, 일치 {a.matched}건, 불일치 {len(a.unexplained())}건"
                     + (f" (봉 수정 {explained}건 별도)" if explained else "") + f" ({_utc(a.window[1])} 봉까지, {_utc(a.as_of)})")
    hb = journal.get(HEARTBEAT_KEY)
    if hb is not None:
        lines.append(f"루프 하트비트: {now - float(hb):.0f}초 전")
    return lines


def audit_from_journal(journal: Journal) -> AuditResult | None:
    """The last audit the runtime stored - a failed one included (`AuditResult.error`)."""
    a = journal.get(AUDIT_KEY)
    return None if not a else AuditResult.from_json(a)
