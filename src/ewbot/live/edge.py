"""The pre-registered edge rule of strategy A in live trading, and the signal-frequency review (user decision
2026-11-18; Plan 3 spec, amendment of that date). Pure functions: nothing here reads the exchange, the network or a
clock, and nothing here pauses anything - live/runtime.py calls them from the hourly audit, which already has the
paper ledger, and acts on what they say.

What is counted: strategy A's ENGINE trades - the paper ledger's closed trades of the baseline fingerprint, every
signal the engine traded, whether the live account entered it or not - whose entry is at or after the FULL start
(START_TS, 2026-11-06 21:42 UTC), in closing order, each with the engine's `r_multiple`. The rule was decided before
the first of them closed. The engine's R is the same yardstick as the holdout's 36 trades (mean +0.19R, SD 1.14R);
the live account's own money is watched by the account breakers (live/watchdog.py), not here.

The rule (`evaluate`):
  floor      cumulative R since the start <= -FLOOR_R (-5R): STOPPED. It stays in force after the verdict.
  verdict    at the VERDICT_TRADES-th (24th) trade's close, or at VERDICT_TS (24 months, 2028-11-06 21:42 UTC) with the
             trades closed by then, whichever comes first: cumulative R <= 0 is a failed verdict - STOPPED, the same
             stop as the floor's; above 0 it passed, and the rule must then be registered anew (the runtime says so).
             A verdict that falls due while the rule is already stopped or retired is recorded and changes nothing.
  way back   after a stop, a resume is allowed once the cumulative R has recovered at least RECOVER_R (+4R) from its
             level at the stop within the next RECOVER_TRADES (16) engine trades; having been reached, it stays allowed.
             Sixteen trades without it: RETIRED (NO_RECOVERY).
  resumed    the user's resume (recorded by the runtime with the trade count and time) moves the floor's reference to
             the cumulative R at the resume: -5R from there is the second floor hit, RETIRED (FLOOR_AFTER_RESUME).
             A failed verdict after a resume is a stop with the way back again.
STOPPED and RETIRED mean the same for the runtime - live entries paused, `/resume` refused until the way back is met
(`resume_block`); RETIRED has no way back. Nothing is demoted, deleted or closed by this rule: whether A is retired
from live is the user's decision.

The status is recomputed from the trades and the recorded resumes every time (no latch of its own): the paper
ledger recomputes its current epoch's trades every run, and a resume the trades do not allow is simply not applied.

A ledger that cannot speak for strategy A - unreadable, another schema, a current epoch that is not the baseline
fingerprint, older than LEDGER_MAX_AGE_S, a malformed trade - is an EdgeError: unknown, never a number and never a
stop (`engine_trades`).

The frequency review (`frequency`) reads the engine's ENTRY decisions from the live journal. Every cycle replays the
engine from the holdout start (2024-01-01) and records every decision of the replay (live/target.py), so the journal
holds the entries of the years before it was created; the review still checks that the record covers the trailing
year and says nothing when it does not. No entry for more than GAP_MAX_DAYS (137, the holdout's longest gap) or
TRAILING_MAX (5) or fewer entries in the last TRAILING_DAYS (365) is a review, alerted once when it begins. It changes
no risk: without trades there is none.

Known limitation (epochs): the count relies on the paper ledger's epoch bookkeeping. Should the paper fingerprint ever
change, the rule is unknown for as long as the current epoch is not the baseline's, and trades the engine entered
during such an epoch, or held across an epoch's boundary, are not in the ledger's baseline trades: the cumulative R
would miss them. Nothing here corrects that; a fingerprint change is a major incident of its own (AUDIT_MISMATCH)
and would need a dated decision on how the rule counts from there."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Iterable, Sequence

from ewbot.paper.ledger import Ledger, LedgerError
from ewbot.types import H1

START_TS = 1_794_001_320  # 2026-11-06 21:42 UTC: FULL began (the journal's stage row)
VERDICT_TS = 1_857_159_720  # 2028-11-06 21:42 UTC: 24 months later
FLOOR_R = 5.0
VERDICT_TRADES = 24
RECOVER_R = 4.0
RECOVER_TRADES = 16
GAP_MAX_DAYS = 137  # the holdout's longest gap between entries (2026-01-08 -> 2026-05-25)
TRAILING_DAYS = 365
TRAILING_MAX = 5  # this many entries or fewer in the trailing year: a review (the holdout's years had 14 and 16)
LEDGER_MAX_AGE_S = 3 * 3600  # as the audit gates (stage.AUDIT_MAX_AGE_S): the ledger's last bar closed at most this long ago
DAY = 86_400
_EPS = 1e-9

Row = tuple[int, int, float]  # entry_ts, exit_ts, r_multiple


class EdgeError(ValueError):
    """The paper ledger cannot speak for strategy A's engine trades right now: the rule's status is unknown."""


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"not an integer: {value!r}")
    return value


def _r(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TypeError(f"not a finite number: {value!r}")
    return float(value)


def engine_trades(ledger: Any, baseline_fingerprint: str, *, now: float, start_ts: int | None = None) -> list[Row]:
    """Strategy A's closed engine trades entered at or after the start, in closing order; EdgeError when the ledger is
    not a valid paper ledger, its current epoch is not the baseline fingerprint, its last bar closed more than
    LEDGER_MAX_AGE_S ago or a trade is malformed. Trades of an epoch with another fingerprint are another engine's."""
    start = START_TS if start_ts is None else int(start_ts)
    try:
        led = Ledger.from_dict(ledger)
    except LedgerError as exc:
        raise EdgeError(f"paper ledger invalid: {exc}"[:200]) from None
    try:
        epochs = {_int(e["id"]): str(e["fingerprint"]) for e in led.epochs}
        current = led.epochs[-1]
        through = _int(current["through_ts"])
        if epochs[_int(current["id"])] != baseline_fingerprint:
            raise EdgeError("paper ledger's current fingerprint is not strategy A's baseline")
        if now - (through + H1) > LEDGER_MAX_AGE_S:
            raise EdgeError(f"paper ledger stale: its last bar closed {(now - through - H1) / 3600:.1f} hours ago")
        found: dict[int, Row] = {}
        for t in led.trades:  # a later epoch's record of the same entry replaces an earlier one
            if epochs[_int(t["epoch_id"])] != baseline_fingerprint:
                continue
            entry, exit_ts = _int(t["entry_ts"]), _int(t["exit_ts"])
            if exit_ts < entry:
                raise ValueError(f"trade {entry} closes before it opens")
            if entry >= start:
                found[entry] = (entry, exit_ts, _r(t["r_multiple"]))
    except EdgeError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise EdgeError(f"paper ledger malformed: {type(exc).__name__}: {exc}"[:200]) from None
    return sorted(found.values(), key=lambda row: (row[1], row[0]))


def evaluate(trades: Sequence[Row], now: float, resumes: Sequence[dict[str, Any]] = (), *,
             start_ts: int | None = None, verdict_ts: int | None = None) -> dict[str, Any]:
    """The rule's status for these engine trades (`engine_trades`) at `now`, with the user's recorded resumes
    ({"n": the trade count at the resume, "ts": its time}). A JSON-able dict:
      n, cum       the trades counted and their cumulative R
      phase        ACTIVE | STOPPED | RESUMED | RETIRED
      ref          the floor's reference level (0, or the cumulative R at the last applied resume); to_floor: R left to it
      stop         {"kind": FLOOR | VERDICT, "n", "cum", "exit_ts"} of the stop in force (None in ACTIVE and RESUMED)
      eligible     a resume is allowed (STOPPED only); best: the best recovery since the stop; left: trades left for it
      retire       None | NO_RECOVERY | FLOOR_AFTER_RESUME
      verdict      None | {"result": PASSED | FAILED, "n", "cum", "by": TRADES | DATE}
      resumes      how many recorded resumes the trades allowed and were applied"""
    start = START_TS if start_ts is None else int(start_ts)
    date = VERDICT_TS if verdict_ts is None else int(verdict_ts)
    rows = sorted(trades, key=lambda row: (row[1], row[0]))
    waiting = sorted(({"n": int(r["n"]), "ts": float(r["ts"])} for r in resumes), key=lambda r: (r["n"], r["ts"]))
    by_date = sum(1 for row in rows if row[1] <= date)  # the trades closed by the 24-month date
    date_due = now >= date and by_date < VERDICT_TRADES
    phase, ref, cum, applied = "ACTIVE", 0.0, 0.0, 0
    stop: dict[str, Any] | None = None
    retire: str | None = None
    verdict: dict[str, Any] | None = None
    eligible, best = False, 0.0

    def stopped(kind: str, k: int) -> dict[str, Any]:
        return {"kind": kind, "n": k, "cum": cum, "exit_ts": rows[k - 1][1] if k else None}

    def judge(k: int, by: str) -> None:
        nonlocal phase, stop, verdict, eligible, best
        failed = cum <= _EPS
        verdict = {"result": "FAILED" if failed else "PASSED", "n": k, "cum": cum, "by": by}
        if failed and phase in ("ACTIVE", "RESUMED"):
            phase, stop, eligible, best = "STOPPED", stopped("VERDICT", k), False, 0.0

    def resume(k: int, before: float | None = None) -> None:
        nonlocal phase, ref, stop, eligible, best, applied
        while waiting and waiting[0]["n"] <= k and (before is None or waiting[0]["ts"] < before):
            r = waiting.pop(0)
            if r["n"] == k and phase == "STOPPED" and eligible:
                phase, ref, stop, eligible, best = "RESUMED", cum, None, False, 0.0
                applied += 1

    for k in range(len(rows) + 1):
        if k:
            cum += rows[k - 1][2]
            if phase in ("ACTIVE", "RESUMED"):
                if cum - ref <= -FLOOR_R + _EPS:
                    if phase == "RESUMED":
                        phase, retire = "RETIRED", "FLOOR_AFTER_RESUME"
                    else:
                        phase, eligible, best = "STOPPED", False, 0.0
                    stop = stopped("FLOOR", k)
            elif phase == "STOPPED" and stop is not None:
                gain = cum - float(stop["cum"])
                best = max(best, gain)
                if gain >= RECOVER_R - _EPS:
                    eligible = True  # within the window: a stop older than it is RETIRED or allowed already
                elif not eligible and k - int(stop["n"]) >= RECOVER_TRADES:
                    phase, retire = "RETIRED", "NO_RECOVERY"
        if verdict is None:
            if k == VERDICT_TRADES:
                judge(k, "TRADES")
            elif date_due and k == by_date:
                resume(k, before=float(date))
                judge(k, "DATE")
        resume(k)
    n = len(rows)
    left = max(RECOVER_TRADES - (n - int(stop["n"])), 0) if phase == "STOPPED" and stop is not None else None
    return {"n": n, "cum": cum, "phase": phase, "ref": ref,
            "to_floor": cum - ref + FLOOR_R if phase in ("ACTIVE", "RESUMED") else None,
            "stop": stop, "eligible": eligible, "best": best if phase == "STOPPED" else None, "left": left,
            "retire": retire, "verdict": verdict, "resumes": applied, "start_ts": start, "verdict_ts": date}


def frequency(entries: Iterable[int], now: float, begin: int | None = None) -> dict[str, Any]:
    """The engine's entry times up to `now`: the last one, the days since it, how many fell in the trailing
    TRAILING_DAYS, and the two review conditions - `gap` (more than GAP_MAX_DAYS since the last entry) and `few`
    (TRAILING_MAX or fewer in the trailing year). `begin` is when the record these entries come from begins (the
    earliest engine decision it holds); `covered` says that it reaches back the whole trailing year - only then are the
    two conditions facts, and the runtime reviews nothing otherwise."""
    seen = sorted(int(e) for e in entries if e <= now)
    last = seen[-1] if seen else None
    gap_days = None if last is None else (now - last) / DAY
    trailing = sum(1 for e in seen if e > now - TRAILING_DAYS * DAY)
    return {"last_entry": last, "gap_days": gap_days, "trailing": trailing,
            "gap": gap_days is not None and gap_days > GAP_MAX_DAYS, "few": trailing <= TRAILING_MAX,
            "begin": begin, "covered": begin is not None and begin <= now - TRAILING_DAYS * DAY}


# ----------------------------------------------------------------------------- the words
def day_text(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


STOP_WORD = {"FLOOR": f"손실 하한 -{FLOOR_R:g}R", "VERDICT": "판정 실패 (누적 R 0 이하)"}
RETIRE_WORD = {"NO_RECOVERY": f"중단 뒤 {RECOVER_TRADES}건 안에 +{RECOVER_R:g}R을 회복하지 못함",
               "FLOOR_AFTER_RESUME": f"재개한 뒤 다시 -{FLOOR_R:g}R"}


def way_back(status: dict[str, Any]) -> str:
    """Where a STOPPED status stands on its way back."""
    if status.get("eligible"):
        return f"재개 조건 충족 (중단 뒤 최고 {float(status['best']):+.2f}R): /resume 가능"
    stop = status["stop"]
    gain = float(status["cum"]) - float(stop["cum"])
    return (f"재개 조건: 중단 뒤 {RECOVER_TRADES}건 안에 +{RECOVER_R:g}R 회복 (지금 {gain:+.2f}R, 남은 거래 {status['left']}건) - "
            f"못 채우면 A는 실거래에서 은퇴")


def stop_text(status: dict[str, Any]) -> str:
    """One line for a STOPPED or RETIRED status: what stopped it and what comes next."""
    stop = status["stop"]
    what = f"{STOP_WORD[stop['kind']]}: 엔진 거래 {stop['n']}건째에 누적 R {float(stop['cum']):+.2f}"
    if status["phase"] == "RETIRED":
        return (f"은퇴 권고 ({RETIRE_WORD[status['retire']]}; {what}) - 신규 진입 정지, /resume 거부. "
                "A를 실거래에서 은퇴시킬지는 사용자가 결정 (자동으로 강등하거나 지우지 않음)")
    return f"{what} - 신규 진입 정지. {way_back(status)}"


def resume_block(status: dict[str, Any] | None) -> str | None:
    """Why `/resume` is refused under this status, or None when the rule does not stand in its way: never evaluated,
    ACTIVE, RESUMED, or STOPPED with the way back met."""
    if not status or status.get("phase") not in ("STOPPED", "RETIRED"):
        return None
    if status["phase"] == "STOPPED" and status.get("eligible"):
        return None
    return f"재개 거부 (엣지 규칙, 2026-11-18 등록): {stop_text(status)}"


def rule_line(status: dict[str, Any]) -> str:
    n, cum = int(status["n"]), float(status["cum"])
    head = f"실전 기간 엔진 거래 {n}건 · 누적 R {cum:+.2f}"
    v = status.get("verdict")
    if v is None:
        judged = f"판정: {VERDICT_TRADES}번째 거래 또는 {day_text(float(status['verdict_ts']))} 중 먼저 (누적 R > 0 필요)"
    elif v["result"] == "PASSED":
        judged = f"판정 통과 ({v['n']}건, 누적 R {float(v['cum']):+.2f}): 규칙 재등록 필요"
    else:
        judged = f"판정 실패 ({v['n']}건, 누적 R {float(v['cum']):+.2f})"
    phase = status["phase"]
    if phase == "RETIRED":
        return f"{stop_text(status)} · {head} · {judged}"
    if phase == "STOPPED":
        refused = "" if status.get("eligible") else " · /resume 거부 중"
        return f"중단 ({stop_text(status)}){refused} · {head} · {judged}"
    since = f" (재개 기준 {float(status['ref']):+.2f}에서)" if phase == "RESUMED" else ""
    word = "재개됨 - 여기서 다시 하한이면 은퇴" if phase == "RESUMED" else "정상"
    return f"{word} · {head} · 하한까지 {float(status['to_floor']):.2f}R{since} · {judged}"


def status_text(state: dict[str, Any] | None, freq: dict[str, Any] | None) -> list[str]:
    """The status lines (`ewbot live status`, /status, the daily summary) from what the runtime stored: `state` =
    {"status": the last good `evaluate` or None, "as_of", "error": why the last try could not evaluate or None,
    "error_ts"}, `freq` = the last `frequency` with "as_of" and "error". Nothing stored: no line."""
    lines: list[str] = []
    if state:
        status, error = state.get("status"), state.get("error")
        if error:
            last = (f" · 마지막 정상 평가 {_utc(float(state['as_of']))}: {rule_line(status)}" if status
                    else " · 정상 평가가 아직 없음")
            lines.append(f"엣지 규칙: 알 수 없음 ({str(error)[:120]}){last}")
        elif status:
            lines.append(f"엣지 규칙: {rule_line(status)} ({_utc(float(state['as_of']))} 평가)")
    if freq:
        if freq.get("error"):
            lines.append(f"신호 빈도: 알 수 없음 ({str(freq['error'])[:120]})")
        else:
            last = ("엔진 진입 기록 없음" if freq.get("last_entry") is None else
                    f"마지막 엔진 진입 {day_text(float(freq['last_entry']))} ({float(freq['gap_days']):.0f}일 전, 한도 {GAP_MAX_DAYS}일)")
            review = "검토 필요 (위험 변경 없음) · " if freq.get("gap") or freq.get("few") else ""
            lines.append(f"신호 빈도: {review}{last} · 최근 {TRAILING_DAYS}일 {freq['trailing']}건 "
                         f"({TRAILING_MAX}건 이하이면 검토)")
    return lines
