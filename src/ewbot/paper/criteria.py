"""Go-live status (Plan 1 spec 9.6 criteria 1-4, Plan 2 spec 5.3 / 5.5). Pure functions over the
ledger; the daily compare() that feeds criteria 1-2 runs in paper.runner.

The go-live candidate is A - the only strategy the paper runtime trades and counts (criteria 3 and 4
are about A's trades), so, as in backtest.holdout.evaluate_criteria's `passing`, one candidate must
meet all four. HYBRID's comparison is still computed and shown."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ewbot.backtest.holdout import evaluate_criteria
from ewbot.paper.ledger import GO_LIVE_STRATEGY, Ledger, current_epoch, golive_count
from ewbot.types import H1

PAPER_MIN_DAYS = 28
COVERAGE_MIN = 0.95
MAX_DD = 0.15
_HEADLINE = ("A", "HYBRID", "C", "BH", "EMA")
_KEYS = ("total_return", "max_dd", "sharpe", "calmar", "n_trades")


def summarize_compare(cmp: dict[str, Any], start_ts: int, end_ts: int, epoch_id: int, computed_ts: int) -> dict[str, Any]:
    """What the ledger keeps from one runner.compare() over [holdout start, last bar]."""
    crit = evaluate_criteria(cmp)
    comp = cmp["comparison"]
    rnd = cmp.get("random") or {}
    dist = rnd.get("dist") or {}
    return {
        "computed_ts": int(computed_ts),
        "epoch_id": int(epoch_id),
        "window": [int(start_ts), int(end_ts)],
        "winners": list(crit["winners"]),
        "max_dd_ok": bool(crit["max_dd_ok"]),
        "random_control_evaluated": bool(crit["random_control_evaluated"]),
        "headline": {n: {k: comp[n][k] for k in _KEYS if k in comp[n]} for n in _HEADLINE if n in comp},
        "random": {
            "runs": int(rnd.get("runs", 0)),
            "sharpe_mean": (dist.get("sharpe") or {}).get("mean"),
            "calmar_mean": (dist.get("calmar") or {}).get("mean"),
        },
    }


def run_coverage(runs: list[dict[str, Any]], epoch_id: int, now_ts: int) -> dict[str, Any]:
    """Hours since the epoch's first successful run (inclusive) that have a successful run."""
    ok = [int(r["ts"]) for r in runs if r.get("status") == "ok" and int(r["epoch_id"]) == int(epoch_id)]
    if not ok:
        return {"first_ts": None, "days": 0.0, "hours": 0, "covered": 0, "coverage": 0.0}
    first = min(ok)
    lo, hi = first // H1, int(now_ts) // H1
    covered = len({t // H1 for t in ok if lo <= t // H1 <= hi})
    hours = hi - lo + 1
    return {
        "first_ts": first,
        "days": (int(now_ts) - first) / 86400.0,
        "hours": hours,
        "covered": covered,
        "coverage": covered / hours,
    }


def criterion4(ledger: Ledger, now_ts: int) -> dict[str, Any]:
    """>= 28 days of paper runs, 0 UNEXPLAINED discrepancies in the epoch, >= 95% hourly coverage."""
    cur = current_epoch(ledger)
    cov = run_coverage(ledger.runs, cur["id"], now_ts)
    unexplained = sum(
        1 for d in ledger.discrepancies if d["cause"] == "UNEXPLAINED" and int(d["epoch_id"]) == int(cur["id"])
    )
    met = cov["days"] >= PAPER_MIN_DAYS and unexplained == 0 and cov["coverage"] >= COVERAGE_MIN
    return {**cov, "unexplained": unexplained, "met": met}


def golive_status(ledger: Ledger, now_ts: int) -> dict[str, Any]:
    cur = current_epoch(ledger)
    count = golive_count(ledger)
    crit = ledger.criteria if ledger.criteria and int(ledger.criteria["epoch_id"]) == int(cur["id"]) else None
    c1 = c2 = None
    if crit is not None and count["eligible"]:
        c1 = GO_LIVE_STRATEGY in crit["winners"]
        c2 = c1 and crit["headline"][GO_LIVE_STRATEGY]["max_dd"] <= MAX_DD
    c3 = count["eligible"] and count["total"] >= count["required"]
    c4 = criterion4(ledger, now_ts)
    all_met = bool(count["eligible"] and c1 and c2 and c3 and c4["met"])
    return {"epoch_id": cur["id"], "eligible": count["eligible"], "count": count, "criteria_1_2": crit,
            "c1": c1, "c2": c2, "c3": c3, "c4": c4, "all_met": all_met}


def _mark(v: bool | None) -> str:
    return "미계산" if v is None else ("충족" if v else "미충족")


def _day(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d")


def status_lines(st: dict[str, Any]) -> list[str]:
    """Korean status block shown at the top of report.html and in the daily summary."""
    count, crit, c4 = st["count"], st["criteria_1_2"], st["c4"]
    lines = ["실거래 전환 판정 (Plan 1 명세 §9.6)"]
    if not st["eligible"]:
        lines.append(f"현재 구간 {st['epoch_id']}의 전략 지문이 기준값(64e628a)과 다름: 실거래 전환 판정 대상 아님")
    when = f" ({_day(crit['computed_ts'])} 계산)" if crit else " (다음 일일 요약 실행에서 계산)"
    who = ", ".join(crit["winners"]) if crit and crit["winners"] else "-"
    lines.append(
        f"1. 위험 대비 수익 {GO_LIVE_STRATEGY} > C·BH·RANDOM (샤프·Calmar): {_mark(st['c1'])} [우위: {who}]{when}"
    )
    dd = f" [{GO_LIVE_STRATEGY} {crit['headline'][GO_LIVE_STRATEGY]['max_dd'] * 100:.1f}%]" if crit else ""
    lines.append(f"2. 최대 낙폭 <= {MAX_DD:.0%} ({GO_LIVE_STRATEGY}): {_mark(st['c2'])}{dd}")
    lines.append(
        f"3. 표본 외 거래 {count['total']}/{count['required']} (홀드아웃 {count['holdout']} + 모의 {count['paper']}): "
        f"{_mark(st['c3'])}"
    )
    lines.append(
        f"4. 모의매매 재현: {_mark(c4['met'])} (운영 {c4['days']:.1f}/{PAPER_MIN_DAYS}일, 커버리지 "
        f"{c4['coverage'] * 100:.1f}%, 원인 불명 불일치 {c4['unexplained']}건)"
    )
    lines.append("=> 실거래 전환 검토 가능 (자동 전환하지 않음)" if st["all_met"] else "=> 실거래 전환 검토 불가")
    return lines
