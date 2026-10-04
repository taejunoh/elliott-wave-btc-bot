"""Operating stages SHADOW -> CANARY -> FULL, their gates, pause and incidents (Plan 3 spec 4.3, 6).

The journal holds the stage history and the pause flag. Promotions need every gate met, a flat
book and an explicit --confirm (the user's approval, spec 6.1); demotions need a flat book and
--confirm. A running runtime keeps its stage until it is flat and then exits so Docker restarts it in
the new stage (live/runtime.py).

Gates (spec 6.2), all computed from the journal:
  SHADOW pass   >= 14 days and >= 1 signal; (1) decisions equal the paper ledger's (audit); (2) every
                cycle finished within 2 min of the bar close; (3) every would-be order passed preview;
                (4) restart test: at least one restart recovered with no unresolved intent.
  CANARY start  SHADOW pass + paper criteria 1, 2 and 4 met + a trade key without transfer permission.
  CANARY pass   >= 3 closed trades or 90 days; (1) protection within 30 s of every fill; (2) stop moves
                and closes executed as targeted (no failed / unconfirmed order); (3) never unprotected
                > 60 s; (4) no manual intervention; (5) costs measured (a daily summary with fills).
  FULL start    CANARY pass + paper criteria 1-4 all met (incl. 50 out-of-sample trades).
Incidents: in CANARY the CANARY failure events pause trading (spec 6.2 `자동으로 일시 정지`); in FULL
the major incidents pause and demote to CANARY."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ewbot.live.audit import AuditResult
from ewbot.live.exchange import KeyPermissions
from ewbot.live.journal import Journal
from ewbot.live.pricing import FULL_CAP_CONTRACTS, FULL_CAP_DAYS
from ewbot.types import H1

STAGES = ("SHADOW", "CANARY", "FULL")
DAY = 86_400
SHADOW_MIN_DAYS = 14
CYCLE_DEADLINE_S = 120
CANARY_MIN_TRADES = 3
CANARY_MAX_DAYS = 90
PROTECT_MAX_S = 30.0
PAUSE_KEY = "paused"
MAJOR = ("UNPROTECTED_60S", "AUDIT_MISMATCH", "BREAKER", "TRANSFER_KEY")  # spec 6.2 중대 사건
# the position changed outside our orders: a foreign order or position, more contracts than the trade holds,
# fewer than its legs explain (reconcile.observe)
MANUAL = ("FOREIGN_ORDERS", "UNKNOWN_POSITION", "ADOPTED", "EXCESS_POSITION", "UNEXPLAINED_REDUCTION")
CANARY_FAIL = MAJOR + ("PROTECT_SLOW",) + MANUAL
# CANARY gate 2: an order that did not do what was targeted. Not LEG_GAP: on a venue that refuses overlapping
# brackets the executor cancels the old leg first by design (a replacement that then fails is BRACKET_FAILED)
ORDER_FAILURES = ("BRACKET_FAILED", "CLOSE_FAILED", "CLOSE_UNCONFIRMED", "REDUCE_FAILED")


class StageError(RuntimeError):
    """A stage transition was refused."""


@dataclass(frozen=True)
class Gate:
    name: str
    met: bool
    detail: str


def gate_lines(gates: Sequence[Gate]) -> list[str]:
    return [f"[{'충족' if g.met else '미충족'}] {g.name}: {g.detail}" for g in gates]


def paused(journal: Journal) -> dict[str, Any] | None:
    return journal.get(PAUSE_KEY)


def pause(journal: Journal, reason: str, now: float) -> None:
    journal.put(PAUSE_KEY, {"reason": reason, "ts": now})
    journal.add_event(now, "PAUSED", {"reason": reason})


def resume(journal: Journal, now: float) -> None:
    journal.put(PAUSE_KEY, None)
    journal.add_event(now, "RESUMED", {})


def full_cap(journal: Journal, now: float) -> int | None:
    stage, since = journal.stage()
    if stage == "FULL" and since is not None and now - since < FULL_CAP_DAYS * DAY:
        return FULL_CAP_CONTRACTS
    return None


def on_incident(journal: Journal, kind: str, now: float) -> list[str]:
    """Pause (and in FULL demote to CANARY) for an incident of `kind`; returns what was done."""
    stage, _ = journal.stage()
    done: list[str] = []
    if (stage == "CANARY" and kind in CANARY_FAIL) or (stage == "FULL" and kind in MAJOR):
        if paused(journal) is None:
            pause(journal, f"incident {kind}", now)
            done.append("paused")
        if stage == "FULL":
            journal.set_stage("CANARY", f"major incident {kind}", now)
            done.append("demoted to CANARY")
    journal.add_event(now, "INCIDENT", {"kind": kind, "stage": stage, "done": done})
    return done


def _since(journal: Journal, stage: str) -> float | None:
    hist = journal.stage_history()
    if not hist or hist[-1]["stage"] != stage:
        return None
    return float(hist[-1]["ts"])


def shadow_gates(journal: Journal, now: float, audit_result: AuditResult | None) -> list[Gate]:
    since = _since(journal, "SHADOW")
    if since is None:
        return [Gate("SHADOW 단계", False, "현재 단계가 SHADOW가 아님")]
    days = (now - since) / DAY
    signals = [d for d in journal.decisions() if d["kind"] == "SIGNAL" and d["recorded_ts"] >= since]
    cycles = [c for c in journal.cycles() if c["started_ts"] >= since]
    late = [c for c in cycles if c["status"] != "OK" or c["finished_ts"] is None
            or c["finished_ts"] - (c["bar_ts"] + H1) > CYCLE_DEADLINE_S]
    previews = journal.events(kind="PREVIEW", since=since)
    failed = [p for p in previews if not p["data"].get("ok")]
    recoveries = journal.events(kind="RECOVERY", since=since)
    clean = [r for r in recoveries if not r["data"].get("unknown")]
    audit_ok = audit_result is not None and audit_result.ok
    audit_detail = ("미실행" if audit_result is None else
                    f"비교 {audit_result.compared}건, 불일치 {len(audit_result.mismatches)}건")
    return [
        Gate("기간 14일 이상", days >= SHADOW_MIN_DAYS, f"{days:.1f}/{SHADOW_MIN_DAYS}일"),
        Gate("신호 1건 이상", len(signals) >= 1, f"{len(signals)}건"),
        Gate("① 판단이 모의매매와 100% 일치", audit_ok, audit_detail),
        Gate("② 모든 사이클이 마감 후 2분 안에 끝남", bool(cycles) and not late, f"{len(cycles) - len(late)}/{len(cycles)}"),
        Gate("③ 냈을 주문 전부 preview 통과", bool(previews) and not failed, f"{len(previews) - len(failed)}/{len(previews)}"),
        Gate("④ 장애·재시작 시험 통과", len(recoveries) >= 2 and len(clean) == len(recoveries),
             f"재시작 복구 {len(recoveries)}회 (첫 시작 포함), 미해결 {len(recoveries) - len(clean)}회"),
    ]


def _paper_gates(paper_status: dict[str, Any] | None, full: bool) -> list[Gate]:
    if paper_status is None:
        return [Gate("모의매매 판정", False, "paper ledger를 읽지 못함")]
    c4 = paper_status.get("c4") or {}
    gates = [
        Gate("모의매매 1번 (위험 대비 수익)", bool(paper_status.get("c1")), str(paper_status.get("c1"))),
        Gate("모의매매 2번 (최대 낙폭)", bool(paper_status.get("c2")), str(paper_status.get("c2"))),
        Gate("모의매매 4번 (28일, 원인 불명 0건, 커버리지 95%)", bool(c4.get("met")),
             f"{float(c4.get('days', 0.0)):.1f}일, 커버리지 {float(c4.get('coverage', 0.0)) * 100:.1f}%, "
             f"원인 불명 {c4.get('unexplained')}건"),
    ]
    if full:
        count = paper_status.get("count") or {}
        gates.append(Gate("모의매매 3번 (표본 외 50건)", bool(paper_status.get("c3")),
                          f"{count.get('total')}/{count.get('required')}"))
    return gates


def _key_gate(perms: KeyPermissions | None) -> Gate:
    if perms is None:
        return Gate("거래 키 권한", False, "확인 못 함")
    ok = perms.can_view and perms.can_trade and not perms.can_transfer
    return Gate("거래 키 권한 (view+trade, 송금 불가)", ok,
                f"view={perms.can_view} trade={perms.can_trade} transfer={perms.can_transfer}")


def canary_entry_gates(journal: Journal, now: float, audit_result: AuditResult | None,
                       paper_status: dict[str, Any] | None, perms: KeyPermissions | None) -> list[Gate]:
    return shadow_gates(journal, now, audit_result) + _paper_gates(paper_status, full=False) + [_key_gate(perms)]


def canary_gates(journal: Journal, now: float) -> list[Gate]:
    since = _since(journal, "CANARY")
    if since is None:
        return [Gate("CANARY 단계", False, "현재 단계가 CANARY가 아님")]
    days = (now - since) / DAY
    n_closed = len({e["data"]["trade_id"] for e in journal.events(kind="CLOSED", since=since) if e["data"].get("trade_id")})
    protected = journal.events(kind="PROTECTED_FIRST", since=since)
    slow = [p for p in protected if p["data"]["after_entry_s"] > PROTECT_MAX_S]
    failures = [e for k in ORDER_FAILURES for e in journal.events(kind=k, since=since)]
    unprotected = journal.events(kind="UNPROTECTED_60S", since=since)
    manual = [e for k in MANUAL for e in journal.events(kind=k, since=since)]
    costs = journal.events(kind="COSTS", since=since)
    return [
        Gate("완결 거래 3건 이상 또는 90일", n_closed >= CANARY_MIN_TRADES or days >= CANARY_MAX_DAYS,
             f"{n_closed}건, {days:.1f}일"),
        Gate("① 보호 주문이 체결 후 30초 안에 걸림", bool(protected) and not slow, f"{len(protected) - len(slow)}/{len(protected)}"),
        Gate("② 손절 이동과 청산이 목표와 일치", not failures, f"실패·미확인 주문 {len(failures)}건"),
        Gate("③ 보호되지 않은 상태 60초 초과 없음", not unprotected, f"{len(unprotected)}건"),
        Gate("④ 수동 개입 없음", not manual, f"{len(manual)}건"),
        Gate("⑤ 비용 실측치 비교", bool(costs), f"비용 보고 {len(costs)}회"),
    ]


def full_entry_gates(journal: Journal, now: float, paper_status: dict[str, Any] | None,
                     perms: KeyPermissions | None) -> list[Gate]:
    return canary_gates(journal, now) + _paper_gates(paper_status, full=True) + [_key_gate(perms)]


def transition(journal: Journal, to: str, now: float, gates: Sequence[Gate], *, confirm: bool, flat: bool) -> None:
    """Record a stage change, or raise StageError with the reason."""
    if to not in STAGES:
        raise StageError(f"unknown stage {to}")
    cur, _ = journal.stage()
    if to == cur:
        raise StageError(f"already in {cur}")
    if not confirm:
        raise StageError("a stage change needs --confirm (the user's explicit approval)")
    if not flat:
        raise StageError("a stage change needs a flat book (no live or virtual position, no resting order)")
    promotion = STAGES.index(to) > STAGES.index(cur)
    if promotion and STAGES.index(to) - STAGES.index(cur) != 1:
        raise StageError(f"{cur} -> {to}: promote one stage at a time")
    unmet = [g for g in gates if not g.met]
    if promotion and (not gates or unmet):
        raise StageError("gates not met: " + "; ".join(f"{g.name} ({g.detail})" for g in unmet or gates))
    journal.set_stage(to, "promotion" if promotion else "demotion", now)
    journal.add_event(now, "STAGE", {"from": cur, "to": to})
