"""Operating stages SHADOW -> CANARY -> FULL, their gates, pause and incidents (Plan 3 spec 4.3, 6).

The journal holds the stage history and the pause flag. Promotions need every gate met, a flat book and an
explicit --confirm (the user's approval, spec 6.1); demotions need only --confirm and are written at once, an
open position or not. A running runtime keeps running until it is flat and then exits so Docker restarts it in the
new stage (live/runtime.py), but it sizes every entry and adoption by the more conservative of its own stage and
the journal's (`full_cap` reads the journal's current stage), so a demotion applies to the next order at once.

One CANARY failure definition for the pause and the gates (spec 6.2 CANARY `실패 시 자동으로 일시 정지`):
`CANARY_FAIL` = the major incidents (`MAJOR`: unprotected > 60 s, an unexplained audit mismatch, a breaker, a
transfer-permission key), protection slower than 30 s, the position changed outside our orders (`MANUAL`) and
an order failure the executor did not resolve (`ORDER_UNRESOLVED`, recorded by live/runtime.py: a failed
bracket not replaced within PROTECT_MAX_S, a failed close or reduction not done by the next pass; a failure it
resolved in time is no CANARY failure). In CANARY each one pauses entries (`on_incident`), and the CANARY pass and
FULL entry gates are unmet after any of them in the current CANARY stint, while the runtime is paused and
without a current passing audit. In FULL the major incidents pause and demote to CANARY, and every CANARY
failure restarts the 30-day clean window of the 3-contract cap (spec 6.2 FULL `이상이 없으면 상한을 해제`).
A failed stint is started again with `restart_stint` (flat, --confirm, a STAGE event `restart`): SHADOW after a
fix (spec 6.2 `수정한 뒤 SHADOW를 다시 한다`), CANARY after a CANARY failure (spec 6.2 `재개는 사용자가 결정한다`,
no SHADOW again). Every gate of that stage then counts from the restart - days, signals, cycles, previews,
restarts, trades, failures and the audit's mismatches; without a restart a failed stint keeps the promotion closed.

Gates (spec 6.2), all computed from the journal. Times are the journal's (the VPS clock, NTP-synchronised): a
clock step moves every window with it.
  SHADOW pass   >= 14 days and >= 1 signal first known in the stage; (1) decisions equal the paper ledger's (an
                audit from at or before the stage's start to at most AUDIT_MAX_AGE_S ago, with no mismatch the
                data do not explain: a DATA_REVISION is shown, not counted);
                (2) every hourly bar that closed in the stage has a cycle that ended OK within 2 min of the
                close (a bar with no cycle row at all while the futures session was recorded closed is excused:
                a span from a SESSION event whose state is not OPEN to the next SESSION or STARTUP event, at most
                SESSION_CLOSED_MAX_S; a cycle not yet past its deadline is not due); (3) every would-be order
                passed preview; (4) restart test: at least one restart recovered with no unresolved intent.
  CANARY start  SHADOW pass + paper criteria 1, 2 and 4 met + a trade key without transfer permission.
  CANARY pass   >= 3 closed trades or 90 days; (1) protection within 30 s of every fill; (2) stop moves and
                closes executed as targeted (no unresolved order failure); (3) never unprotected > 60 s; (4) no
                manual intervention; (5) costs measured (a daily summary with fills); no other CANARY failure
                (audit mismatch, breaker, transfer key); not paused; a current audit without an unexplained
                mismatch of a decision known in the stint.
  FULL start    CANARY pass + paper criteria 1-4 all met (incl. 50 out-of-sample trades)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Sequence

from ewbot.live.audit import AuditResult
from ewbot.live.exchange import SESSION_OPEN, KeyPermissions
from ewbot.live.journal import Journal
from ewbot.live.pricing import FULL_CAP_CONTRACTS, FULL_CAP_DAYS
from ewbot.paper.decisions import known_at
from ewbot.types import H1

STAGES = ("SHADOW", "CANARY", "FULL")
DAY = 86_400
SHADOW_MIN_DAYS = 14
CYCLE_DEADLINE_S = 120
CANARY_MIN_TRADES = 3
CANARY_MAX_DAYS = 90
PROTECT_MAX_S = 30.0
AUDIT_MAX_AGE_S = 3 * 3600  # a gate's audit must reach a bar that closed at most this long ago
# the longest closure spec 2 documents (the quarterly 3-hour maintenance; the weekly CFM halt is 1 hour) plus 1 hour:
# a closure whose reopen was never recorded excuses no more than this
SESSION_CLOSED_MAX_S = 4 * 3600
PAUSE_KEY = "paused"
# the manual interventions the runtime's planner still finds, one per (kind, trade id) episode (live/runtime.py):
# {"items": [[kind, trade id], ...], "ended": when the last of them ended (None: never), "stint": their stage row}
RAISED_KEY = "incidents_raised"
MAJOR = ("UNPROTECTED_60S", "AUDIT_MISMATCH", "BREAKER", "TRANSFER_KEY")  # spec 6.2 중대 사건
# the position changed outside our orders: a foreign order or position, more contracts than the trade holds,
# fewer than its legs explain (reconcile.observe), the trade's position on the other side (brackets and closes only
# reduce: only an order of someone else's can flip it)
MANUAL = ("FOREIGN_ORDERS", "UNKNOWN_POSITION", "ADOPTED", "EXCESS_POSITION", "UNEXPLAINED_REDUCTION", "DIRECTION_MISMATCH")
# an order failure the executor did not resolve in time (live/runtime.py watches ORDER_FAILURES)
ORDER_UNRESOLVED = "ORDER_UNRESOLVED"
CANARY_FAIL = MAJOR + ("PROTECT_SLOW",) + MANUAL + (ORDER_UNRESOLVED,)
# the order failures the runtime watches for CANARY gate 2: an order that did not do what was targeted. Not LEG_GAP:
# on a venue that refuses overlapping brackets the executor cancels the old leg first by design (a replacement that
# then fails is BRACKET_FAILED). Each one is resolved or becomes ORDER_UNRESOLVED.
ORDER_FAILURES = ("BRACKET_FAILED", "CLOSE_FAILED", "CLOSE_UNCONFIRMED", "REDUCE_FAILED")

# gate names: transition() checks that a promotion got every one of its gates, met
G_DAYS = "기간 14일 이상"
G_SIGNAL = "신호 1건 이상"
G_AUDIT = "① 판단이 모의매매와 100% 일치"
G_CYCLES = "② 모든 사이클이 마감 후 2분 안에 끝남"
G_PREVIEW = "③ 냈을 주문 전부 preview 통과"
G_RESTART = "④ 장애·재시작 시험 통과"
G_TRADES = "완결 거래 3건 이상 또는 90일"
G_PROTECT = "① 보호 주문이 체결 후 30초 안에 걸림"
G_ORDERS = "② 손절 이동과 청산이 목표와 일치"
G_UNPROTECTED = "③ 보호되지 않은 상태 60초 초과 없음"
G_MANUAL = "④ 수동 개입 없음"
G_COSTS = "⑤ 비용 실측치 비교"
G_MAJOR = "중대 사건 없음 (판단 불일치, 손실 한도, 송금 권한 키)"
G_RUNNING = "일시 정지 아님"
G_AUDIT_NOW = "모의매매 대조 (최근 감사 통과)"
G_C1 = "모의매매 1번 (위험 대비 수익)"
G_C2 = "모의매매 2번 (최대 낙폭)"
G_C4 = "모의매매 4번 (28일, 원인 불명 0건, 커버리지 95%)"
G_C3 = "모의매매 3번 (표본 외 50건)"
G_KEY = "거래 키 권한 (view+trade, 송금 불가)"
SHADOW_PASS = (G_DAYS, G_SIGNAL, G_AUDIT, G_CYCLES, G_PREVIEW, G_RESTART)
CANARY_PASS = (G_TRADES, G_PROTECT, G_ORDERS, G_UNPROTECTED, G_MANUAL, G_COSTS, G_MAJOR, G_RUNNING, G_AUDIT_NOW)
REQUIRED_GATES = {("SHADOW", "CANARY"): SHADOW_PASS + (G_C1, G_C2, G_C4, G_KEY),
                  ("CANARY", "FULL"): CANARY_PASS + (G_C1, G_C2, G_C4, G_C3, G_KEY)}


class StageError(RuntimeError):
    """A stage transition was refused."""


@dataclass(frozen=True)
class Gate:
    name: str
    met: bool
    detail: str


def gate_lines(gates: Sequence[Gate]) -> list[str]:
    return [f"[{'충족' if g.met else '미충족'}] {g.name}: {g.detail}" for g in gates]


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


def paused(journal: Journal) -> dict[str, Any] | None:
    return journal.get(PAUSE_KEY)


def pause(journal: Journal, reason: str, now: float) -> None:
    journal.put(PAUSE_KEY, {"reason": reason, "ts": now})
    journal.add_event(now, "PAUSED", {"reason": reason})


def resume(journal: Journal, now: float) -> None:
    journal.put(PAUSE_KEY, None)
    journal.add_event(now, "RESUMED", {})


def canary_failures(journal: Journal, since: float) -> dict[str, tuple[int, float]]:
    """The CANARY failures since `since`: kind -> (count, latest time). Read from the INCIDENT events
    (`on_incident`) and from the events of those kinds themselves, so a failure journaled but never handed to
    `on_incident` (a crash in between) still counts."""
    found: dict[str, tuple[int, float]] = {}
    incidents: dict[str, list[float]] = {}
    for e in journal.events(kind="INCIDENT", since=since):
        k = e["data"].get("kind")
        if k in CANARY_FAIL:
            incidents.setdefault(k, []).append(float(e["ts"]))
    for k in CANARY_FAIL:
        times = incidents.get(k, []) + [float(e["ts"]) for e in journal.events(kind=k, since=since)]
        if times:
            found[k] = (max(len(incidents.get(k, [])), len(times) - len(incidents.get(k, []))), max(times))
    return found


def full_cap(journal: Journal, now: float) -> int | None:
    """The most contracts the journal's current stage allows - never more than the stage that is in force:
    SHADOW 0 (no real order), CANARY 1, FULL FULL_CAP_CONTRACTS until it has run FULL_CAP_DAYS without a CANARY
    failure (each one restarts the window, counted from the journal's event times) and with no manual intervention
    still in force (RAISED_KEY; the window then counts from its end), None (uncapped) only then."""
    stage, since = journal.stage()
    if stage == "CANARY":
        return 1
    if stage != "FULL" or since is None:
        return 0
    raised = journal.get(RAISED_KEY) or {}
    if raised.get("items"):
        return FULL_CAP_CONTRACTS
    ended = [float(raised["ended"])] if raised.get("ended") is not None and float(raised["ended"]) >= float(since) else []
    clean_from = max([float(since)] + ended + [last for _, last in canary_failures(journal, float(since)).values()])
    return None if now - clean_from >= FULL_CAP_DAYS * DAY else FULL_CAP_CONTRACTS


def on_incident(journal: Journal, kind: str, now: float) -> list[str]:
    """Pause in CANARY for a CANARY failure; in FULL pause and demote to CANARY for a major incident (another
    CANARY failure keeps the 3-contract cap for 30 more days). Returns what was done."""
    stage, _ = journal.stage()
    done: list[str] = []
    if (stage == "CANARY" and kind in CANARY_FAIL) or (stage == "FULL" and kind in MAJOR):
        if paused(journal) is None:
            pause(journal, f"incident {kind}", now)
            done.append("paused")
        if stage == "FULL":
            reason = f"major incident {kind}"
            journal.set_stage("CANARY", reason, now)
            journal.add_event(now, "STAGE", {"from": "FULL", "to": "CANARY", "reason": reason})
            done.append("demoted to CANARY")
    elif stage == "FULL" and kind in CANARY_FAIL:
        done.append(f"{FULL_CAP_CONTRACTS}-contract cap for {FULL_CAP_DAYS} more days")
    journal.add_event(now, "INCIDENT", {"kind": kind, "stage": stage, "done": done})
    return done


def _since(journal: Journal, stage: str) -> float | None:
    hist = journal.stage_history()
    if not hist or hist[-1]["stage"] != stage:
        return None
    return float(hist[-1]["ts"])


def _restarted(journal: Journal) -> bool:
    """The current stint began with `restart_stint`."""
    hist = journal.stage_history()
    return bool(hist) and hist[-1]["reason"] == "restart"


def audit_gate(name: str, res: AuditResult | None, since: float, now: float, after: float | None = None) -> Gate:
    """An audit that covers the stage - its window starts at or before `since` and reaches a bar that closed at most
    AUDIT_MAX_AGE_S ago - with something compared and no unexplained mismatch (a DATA_REVISION is shown in the
    detail but explained); with `after`, only mismatches of decisions first known after it count (a CANARY stint).
    A failed audit (fetch, invalid ledger) is unmet, never the last good one."""
    if res is None:
        return Gate(name, False, "미실행")
    if res.error is not None:
        return Gate(name, False, f"감사 실패: {res.error}"[:200])
    lo, hi = res.window
    if now - (hi + H1) > AUDIT_MAX_AGE_S:
        return Gate(name, False, f"오래된 감사: {_utc(hi)} 봉까지 ({_utc(res.as_of)} 실행)")
    if lo > since:
        return Gate(name, False, f"감사 구간이 단계 시작 뒤부터 ({_utc(lo)} 이후)")
    counted = res.unexplained(after)
    parts = [f"비교 {res.compared}건, 불일치 {len(counted)}건"]
    revisions = sum(1 for m in res.mismatches if m.get("cause") == "DATA_REVISION")
    if revisions:
        parts.append(f"봉 수정 {revisions}건 (원인 설명됨, 미반영)")
    earlier = len(res.unexplained()) - len(counted)
    if earlier:
        parts.append(f"단계 시작 전 불일치 {earlier}건 제외")
    return Gate(name, res.compared > 0 and not counted, ", ".join(parts) + f" ({_utc(hi)} 봉까지)")


def _closed_spans(journal: Journal, since: float, now: float) -> list[tuple[float, float]]:
    """Spans in which the futures session was recorded not OPEN: from a SESSION event whose `state` is not OPEN
    (live/runtime.py records the state with every change and at every start) to the next SESSION event of any state
    or the next STARTUP - a reopen while the runtime was down is never recorded - and never longer than
    SESSION_CLOSED_MAX_S."""
    marks = sorted(journal.events(kind="SESSION", since=since) + journal.events(kind="STARTUP", since=since),
                   key=lambda e: e["id"])
    spans: list[tuple[float, float]] = []
    start: float | None = None
    for e in marks:
        if start is not None:
            spans.append((start, min(float(e["ts"]), start + SESSION_CLOSED_MAX_S)))
            start = None
        state = e["data"].get("state") if e["kind"] == "SESSION" else None
        if state is not None and state != SESSION_OPEN:
            start = float(e["ts"])
    if start is not None:
        spans.append((start, min(now, start + SESSION_CLOSED_MAX_S)))
    return spans


def _cycle_gate(journal: Journal, since: float, now: float) -> Gate:
    first = math.ceil((since - H1) / H1) * H1  # the first bar that closes in the stage
    last = math.floor((now - H1 - CYCLE_DEADLINE_S) / H1) * H1  # the last bar whose deadline has passed
    cycles = {int(c["bar_ts"]): c for c in journal.cycles(since_bar=first)}
    closed = _closed_spans(journal, since, now)
    on_time = excused = due = 0
    for bar in range(first, last + 1, H1):
        close = bar + H1
        c = cycles.get(bar)
        if (c is not None and c["status"] == "OK" and c["finished_ts"] is not None
                and c["finished_ts"] - close <= CYCLE_DEADLINE_S):
            on_time += 1
        elif c is None and any(s <= close + CYCLE_DEADLINE_S and close <= e for s, e in closed):
            excused += 1  # no cycle at all while the session was closed; a cycle that ran and failed counts
            continue
        due += 1
    detail = f"{on_time}/{due}봉" + (f" (장 휴장 제외 {excused}봉)" if excused else "")
    return Gate(G_CYCLES, due > 0 and on_time == due, detail)


def shadow_gates(journal: Journal, now: float, audit_result: AuditResult | None) -> list[Gate]:
    since = _since(journal, "SHADOW")
    if since is None:
        return [Gate("SHADOW 단계", False, "현재 단계가 SHADOW가 아님")]
    days = (now - since) / DAY
    signals = [d for d in journal.decisions() if d["kind"] == "SIGNAL" and known_at(d) > since]
    previews = journal.events(kind="PREVIEW", since=since)
    failed = [p for p in previews if not p["data"].get("ok")]
    recoveries = journal.events(kind="RECOVERY", since=since)
    clean = [r for r in recoveries if not r["data"].get("unknown")]
    return [
        Gate(G_DAYS, days >= SHADOW_MIN_DAYS, f"{days:.1f}/{SHADOW_MIN_DAYS}일"),
        Gate(G_SIGNAL, len(signals) >= 1, f"단계 중 알게 된 신호 {len(signals)}건"),
        # the first SHADOW audits the whole epoch; a restarted one counts the mismatches of its own decisions
        audit_gate(G_AUDIT, audit_result, since, now, after=since if _restarted(journal) else None),
        _cycle_gate(journal, since, now),
        Gate(G_PREVIEW, bool(previews) and not failed, f"{len(previews) - len(failed)}/{len(previews)}"),
        Gate(G_RESTART, len(recoveries) >= 2 and len(clean) == len(recoveries),
             f"재시작 복구 {len(recoveries)}회 (첫 시작 포함), 미해결 {len(recoveries) - len(clean)}회"),
    ]


def _paper_gates(paper_status: dict[str, Any] | None, full: bool) -> list[Gate]:
    if paper_status is None:
        return [Gate("모의매매 판정", False, "paper ledger를 읽지 못함")]
    c4 = paper_status.get("c4") or {}
    gates = [
        Gate(G_C1, bool(paper_status.get("c1")), str(paper_status.get("c1"))),
        Gate(G_C2, bool(paper_status.get("c2")), str(paper_status.get("c2"))),
        Gate(G_C4, bool(c4.get("met")),
             f"{float(c4.get('days', 0.0)):.1f}일, 커버리지 {float(c4.get('coverage', 0.0)) * 100:.1f}%, "
             f"원인 불명 {c4.get('unexplained')}건"),
    ]
    if full:
        count = paper_status.get("count") or {}
        gates.append(Gate(G_C3, bool(paper_status.get("c3")), f"{count.get('total')}/{count.get('required')}"))
    return gates


def _key_gate(perms: KeyPermissions | None) -> Gate:
    if perms is None:
        return Gate("거래 키 권한", False, "확인 못 함")
    ok = perms.can_view and perms.can_trade and not perms.can_transfer
    return Gate(G_KEY, ok, f"view={perms.can_view} trade={perms.can_trade} transfer={perms.can_transfer}")


def canary_entry_gates(journal: Journal, now: float, audit_result: AuditResult | None,
                       paper_status: dict[str, Any] | None, perms: KeyPermissions | None) -> list[Gate]:
    return shadow_gates(journal, now, audit_result) + _paper_gates(paper_status, full=False) + [_key_gate(perms)]


def _counted(found: dict[str, tuple[int, float]], kinds: Sequence[str]) -> tuple[bool, str]:
    hits = [f"{k} {found[k][0]}건" for k in kinds if k in found]
    return not hits, ", ".join(hits) or "없음"


def canary_gates(journal: Journal, now: float, audit_result: AuditResult | None) -> list[Gate]:
    since = _since(journal, "CANARY")
    if since is None:
        return [Gate("CANARY 단계", False, "현재 단계가 CANARY가 아님")]
    days = (now - since) / DAY
    n_closed = len({e["data"]["trade_id"] for e in journal.events(kind="CLOSED", since=since) if e["data"].get("trade_id")})
    protected = journal.events(kind="PROTECTED_FIRST", since=since)
    slow = [p for p in protected if p["data"]["after_entry_s"] > PROTECT_MAX_S]
    found = canary_failures(journal, since)
    costs = journal.events(kind="COSTS", since=since)
    p = paused(journal)
    orders_ok, orders = _counted(found, (ORDER_UNRESOLVED,))
    unprotected_ok, unprotected = _counted(found, ("UNPROTECTED_60S",))
    manual_ok, manual = _counted(found, MANUAL)
    major_ok, major = _counted(found, ("AUDIT_MISMATCH", "BREAKER", "TRANSFER_KEY"))
    slow_ok = not slow and "PROTECT_SLOW" not in found
    return [
        Gate(G_TRADES, n_closed >= CANARY_MIN_TRADES or days >= CANARY_MAX_DAYS, f"{n_closed}건, {days:.1f}일"),
        Gate(G_PROTECT, bool(protected) and slow_ok, f"{len(protected) - len(slow)}/{len(protected)}"),
        Gate(G_ORDERS, orders_ok, f"해결되지 않은 주문 실패: {orders}"),
        Gate(G_UNPROTECTED, unprotected_ok, unprotected),
        Gate(G_MANUAL, manual_ok, manual),
        Gate(G_COSTS, bool(costs), f"비용 보고 {len(costs)}회"),
        Gate(G_MAJOR, major_ok, major),
        Gate(G_RUNNING, p is None, "운영 중" if p is None else f"일시 정지: {p['reason']}"),
        audit_gate(G_AUDIT_NOW, audit_result, since, now, after=since),
    ]


def full_entry_gates(journal: Journal, now: float, audit_result: AuditResult | None,
                     paper_status: dict[str, Any] | None, perms: KeyPermissions | None) -> list[Gate]:
    return canary_gates(journal, now, audit_result) + _paper_gates(paper_status, full=True) + [_key_gate(perms)]


def transition(journal: Journal, to: str, now: float, gates: Sequence[Gate], *, confirm: bool, flat: bool) -> None:
    """Record a stage change, or raise StageError with the reason. A promotion needs a flat book, every gate of
    REQUIRED_GATES[(current, to)] among `gates` and every gate given met (a second guard on the caller's set). A
    demotion needs only --confirm: it is written at once, so a running runtime sizes for it from its next order
    (`flat` is ignored; the CLI reports it)."""
    if to not in STAGES:
        raise StageError(f"unknown stage {to}")
    cur, _ = journal.stage()
    if to == cur:
        raise StageError(f"already in {cur}")
    if not confirm:
        raise StageError("a stage change needs --confirm (the user's explicit approval)")
    promotion = STAGES.index(to) > STAGES.index(cur)
    if promotion and not flat:
        raise StageError("a promotion needs a flat book (no live or virtual position, no resting order)")
    if promotion and STAGES.index(to) - STAGES.index(cur) != 1:
        raise StageError(f"{cur} -> {to}: promote one stage at a time")
    if promotion:
        given = {g.name for g in gates}
        unmet = [f"{g.name} ({g.detail})" for g in gates if not g.met]
        missing = [f"{n} (확인 안 됨)" for n in REQUIRED_GATES[(cur, to)] if n not in given]
        if unmet or missing:
            raise StageError("gates not met: " + "; ".join(unmet + missing))
    reason = "promotion" if promotion else "demotion"
    journal.set_stage(to, reason, now)
    journal.add_event(now, "STAGE", {"from": cur, "to": to, "reason": reason})


def restart_stint(journal: Journal, stage: str, now: float, *, confirm: bool, flat: bool) -> None:
    """Start a new stint of the current stage: SHADOW after a fix (spec 6.2 `수정한 뒤 SHADOW를 다시 한다`), CANARY
    after a CANARY failure instead of going back to SHADOW (spec 6.2 `재개는 사용자가 결정한다`). Only for the current
    stage, with --confirm and a flat book. Every gate of the stage then counts from now (days, signals, cycles,
    previews, restarts, trades, failures, the audit's mismatches); a pause stays until /resume, and a running runtime
    restarts into the new stint once flat."""
    if stage not in ("SHADOW", "CANARY"):
        raise StageError(f"only a SHADOW or CANARY stint can be restarted (not {stage})")
    cur, _ = journal.stage()
    if cur != stage:
        raise StageError(f"only the current stage's stint can be restarted (stage {cur}, not {stage})")
    if not confirm:
        raise StageError("a restart needs --confirm (the user's explicit approval)")
    if not flat:
        raise StageError("a restart needs a flat book (no live or virtual position, no resting order)")
    journal.set_stage(stage, "restart", now)
    journal.add_event(now, "STAGE", {"from": stage, "to": stage, "reason": "restart"})
