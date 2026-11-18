"""The pre-registered edge rule (user decision 2026-11-18; Plan 3 spec, amendment of that date): pure functions over
the paper ledger's strategy-A engine trades and the engine's entry times. Synthetic numbers only."""

from __future__ import annotations

from typing import Any

import pytest

from ewbot.live import edge
from ewbot.live.edge import (
    FLOOR_R,
    GAP_MAX_DAYS,
    LEDGER_MAX_AGE_S,
    RECOVER_R,
    RECOVER_TRADES,
    START_TS,
    TRAILING_MAX,
    VERDICT_TRADES,
    VERDICT_TS,
    EdgeError,
    engine_trades,
    evaluate,
    frequency,
    resume_block,
    status_text,
)
from tests.paper.helpers import ts

FP = "e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1"
DAY = 86_400
H1 = 3_600


def rows(rs: list[float], first: int = START_TS + DAY, step: int = 20 * DAY) -> list[tuple[int, int, float]]:
    """(entry_ts, exit_ts, r): one closed trade every `step`, each open two days."""
    return [(first + k * step, first + k * step + 2 * DAY, r) for k, r in enumerate(rs)]


def at(trades: list[tuple[int, int, float]]) -> float:
    """A moment after the last close, before the 24-month date."""
    return float(trades[-1][1] + H1) if trades else float(START_TS + H1)


def ledger(trades: list[dict[str, Any]], through: int, fingerprint: str = FP, epochs: list[dict] | None = None) -> dict:
    return {"schema_version": 1, "baseline": {"fingerprint": FP, "commit": "abc1234"}, "holdout": {},
            "epochs": epochs or [{"id": 1, "fingerprint": fingerprint, "started_ts": ts("2026-10-02 10:00"),
                                  "through_ts": through}],
            "trades": trades}


def rec(entry: int, r: float, epoch: int = 1, hold: int = 2 * DAY) -> dict[str, Any]:
    return {"entry_ts": entry, "exit_ts": entry + hold, "setup": "W3", "direction": 1, "contracts": 1,
            "entry_price": 70_000.0, "exit_price_avg": 71_000.0, "stop0": 68_000.0, "tp1": 72_000.0, "tp2": None,
            "pnl": 10.0, "fees": 1.0, "funding": 0.0, "r_multiple": r, "exit_reason": "TIME", "snapshot": {},
            "epoch_id": epoch}


# ----------------------------------------------------------------------------- the registered numbers
def test_the_registered_numbers():
    assert START_TS == ts("2026-11-06 21:42") and VERDICT_TS == ts("2028-11-06 21:42")
    assert (FLOOR_R, VERDICT_TRADES, RECOVER_R, RECOVER_TRADES) == (5.0, 24, 4.0, 16)
    assert (GAP_MAX_DAYS, TRAILING_MAX, edge.TRAILING_DAYS) == (137, 5, 365)
    assert LEDGER_MAX_AGE_S == 3 * 3600


# ----------------------------------------------------------------------------- reading the ledger
def test_engine_trades_are_the_baseline_trades_entered_at_or_after_the_full_start_in_closing_order():
    before, first, second = START_TS - H1, START_TS, START_TS + 30 * DAY
    led = ledger([rec(second, -1.0), rec(before, 3.0), rec(first, 0.5)], through=second + 3 * DAY)
    now = second + 3 * DAY + H1 + 60
    assert engine_trades(led, FP, now=now) == [(first, first + 2 * DAY, 0.5), (second, second + 2 * DAY, -1.0)]


def test_a_ledger_that_cannot_speak_for_the_strategy_is_an_edge_error_never_a_number():
    t = START_TS + DAY
    good = ledger([rec(t, -6.0)], through=t + 3 * DAY)
    now = t + 3 * DAY + H1 + 60
    assert engine_trades(good, FP, now=now) == [(t, t + 2 * DAY, -6.0)]
    with pytest.raises(EdgeError, match="invalid"):
        engine_trades({"schema_version": 2}, FP, now=now)
    with pytest.raises(EdgeError, match="invalid"):
        engine_trades("nope", FP, now=now)  # type: ignore[arg-type]
    with pytest.raises(EdgeError, match="fingerprint"):
        engine_trades(ledger([rec(t, -6.0)], through=t + 3 * DAY, fingerprint="f" * 64), FP, now=now)
    with pytest.raises(EdgeError, match="stale"):  # its last bar closed more than 3 hours ago
        engine_trades(good, FP, now=t + 3 * DAY + H1 + LEDGER_MAX_AGE_S + 1)
    assert engine_trades(good, FP, now=t + 3 * DAY + H1 + LEDGER_MAX_AGE_S) == [(t, t + 2 * DAY, -6.0)]
    for bad in ({"r_multiple": float("nan")}, {"r_multiple": None}, {"r_multiple": "1"}, {"exit_ts": None},
                {"entry_ts": "x"}, {"r_multiple": True}):
        with pytest.raises(EdgeError, match="malformed"):
            engine_trades(ledger([rec(t, 1.0) | bad], through=t + 3 * DAY), FP, now=now)
    with pytest.raises(EdgeError, match="malformed"):
        engine_trades(ledger(["x"], through=t + 3 * DAY), FP, now=now)  # type: ignore[list-item]
    with pytest.raises(EdgeError, match="malformed"):
        engine_trades(dict(good, epochs=[]), FP, now=now)


def test_trades_of_an_epoch_with_another_fingerprint_are_not_strategy_a():
    """A fingerprint that changed and came back: the middle epoch's trades are another engine's; the current epoch must be
    the baseline's for the ledger to speak at all."""
    t = START_TS + DAY
    epochs = [{"id": 1, "fingerprint": FP, "started_ts": 0, "through_ts": t},
              {"id": 2, "fingerprint": "f" * 64, "started_ts": t, "through_ts": t + 10 * DAY},
              {"id": 3, "fingerprint": FP, "started_ts": t + 10 * DAY, "through_ts": t + 40 * DAY}]
    led = ledger([rec(t, 1.0, 1), rec(t + 5 * DAY, -9.0, 2), rec(t + 20 * DAY, 2.0, 3)], through=0, epochs=epochs)
    assert [r for _, _, r in engine_trades(led, FP, now=t + 40 * DAY + H1)] == [1.0, 2.0]


# ----------------------------------------------------------------------------- the floor
def test_nothing_happens_without_trades_or_above_the_floor():
    s = evaluate([], now=START_TS + H1)
    assert (s["phase"], s["n"], s["cum"], s["stop"], s["verdict"], s["eligible"]) == ("ACTIVE", 0, 0.0, None, None, False)
    tr = rows([-1.0, -1.0, 0.5, -1.0, -1.0, -1.0])  # -4.5
    s = evaluate(tr, now=at(tr))
    assert (s["phase"], s["n"], s["stop"]) == ("ACTIVE", 6, None) and s["cum"] == pytest.approx(-4.5)
    assert s["to_floor"] == pytest.approx(0.5)
    assert resume_block(s) is None and resume_block(None) is None


def test_cumulative_r_at_minus_five_stops_and_the_stop_is_the_first_trade_that_reached_it():
    tr = rows([-1.0, -1.0, -1.0, -1.0, -1.0, 2.0])  # exactly -5 at the fifth
    s = evaluate(tr, now=at(tr))
    assert s["phase"] == "STOPPED" and s["stop"] == {"kind": "FLOOR", "n": 5, "cum": pytest.approx(-5.0),
                                                     "exit_ts": tr[4][1]}
    assert not s["eligible"] and s["best"] == pytest.approx(2.0) and s["left"] == RECOVER_TRADES - 1
    assert "+4" in resume_block(s) and "거부" in resume_block(s)
    just_above = rows([-1.0, -1.0, -1.0, -1.0, -0.99])
    assert evaluate(just_above, now=at(just_above))["phase"] == "ACTIVE"


# ----------------------------------------------------------------------------- the way back
def test_resume_is_allowed_once_r_recovered_four_from_the_stop_within_sixteen_trades_and_stays_allowed():
    stop = [-1.0] * 5
    tr = rows(stop + [1.0, 1.0, 1.0, 0.99])  # +3.99
    assert not evaluate(tr, now=at(tr))["eligible"]
    tr = rows(stop + [1.0, 1.0, 1.0, 1.0, -3.0])  # +4 at the ninth trade, then back down: it was reached
    s = evaluate(tr, now=at(tr))
    assert s["phase"] == "STOPPED" and s["eligible"] and s["best"] == pytest.approx(4.0)
    assert resume_block(s) is None


def test_sixteen_trades_without_the_recovery_retire_a_and_a_later_recovery_does_not_count():
    stop = [-1.0] * 5
    tr = rows(stop + [0.2] * 15)  # +3.0 after 15: still waiting, one trade left
    s = evaluate(tr, now=at(tr))
    assert s["phase"] == "STOPPED" and s["left"] == 1 and s["retire"] is None
    tr = rows(stop + [0.2] * 16 + [5.0])  # the 16th did not bring it: retired; the 17th's +5 is too late
    s = evaluate(tr, now=at(tr))
    assert (s["phase"], s["retire"], s["eligible"]) == ("RETIRED", "NO_RECOVERY", False)
    assert "은퇴" in resume_block(s)
    tr = rows(stop + [0.2] * 15 + [1.0])  # +4.0 exactly on the 16th: in time
    assert evaluate(tr, now=at(tr))["eligible"]


def test_a_resume_moves_the_floor_to_its_level_and_a_second_floor_hit_retires():
    tr = rows([-1.0] * 5 + [1.0] * 4)  # stop at -5 (n 5), back to -1 (n 9): allowed
    resumed = [{"n": 9, "ts": float(tr[8][1] + 60), "cum": -1.0}]
    s = evaluate(tr, now=at(tr), resumes=resumed)
    assert (s["phase"], s["ref"], s["stop"], s["resumes"]) == ("RESUMED", pytest.approx(-1.0), None, 1)
    assert s["to_floor"] == pytest.approx(5.0) and resume_block(s) is None
    tr2 = tr + rows([-1.0] * 4, first=tr[-1][0] + 20 * DAY)  # -5 from start again, only -4 from the resume: still on
    assert evaluate(tr2, now=at(tr2), resumes=resumed)["phase"] == "RESUMED"
    tr3 = tr + rows([-1.0] * 5, first=tr[-1][0] + 20 * DAY)  # -5 from the resume level
    s = evaluate(tr3, now=at(tr3), resumes=resumed)
    assert (s["phase"], s["retire"]) == ("RETIRED", "FLOOR_AFTER_RESUME")
    assert s["stop"]["kind"] == "FLOOR" and s["stop"]["n"] == 14 and "은퇴" in resume_block(s)


def test_a_resume_the_trades_do_not_allow_is_not_applied():
    tr = rows([-1.0] * 5 + [1.0] * 2)  # stopped, +2 only
    s = evaluate(tr, now=at(tr), resumes=[{"n": 7, "ts": at(tr), "cum": -3.0}])
    assert s["phase"] == "STOPPED" and s["resumes"] == 0
    tr = rows([-1.0] * 3)  # never stopped: a resume of some other pause
    s = evaluate(tr, now=at(tr), resumes=[{"n": 3, "ts": at(tr), "cum": -3.0}])
    assert s["phase"] == "ACTIVE" and s["resumes"] == 0 and s["ref"] == 0.0


# ----------------------------------------------------------------------------- the verdict
def test_the_twenty_fourth_trade_is_the_verdict_above_zero_passes_and_the_floor_stays():
    tr = rows([0.1] * 23, step=10 * DAY)
    assert evaluate(tr, now=at(tr))["verdict"] is None
    tr = rows([0.1] * 24, step=10 * DAY)
    s = evaluate(tr, now=at(tr))
    assert s["phase"] == "ACTIVE" and s["verdict"] == {"result": "PASSED", "n": 24, "cum": pytest.approx(2.4), "by": "TRADES"}
    tr = rows([0.1] * 24 + [-1.0] * 8, step=10 * DAY)  # 2.4 - 8 = -5.6: the floor from the start still holds
    s = evaluate(tr, now=at(tr))
    assert s["phase"] == "STOPPED" and s["stop"]["kind"] == "FLOOR" and s["stop"]["n"] == 32
    assert s["verdict"]["result"] == "PASSED"


def test_a_verdict_at_or_below_zero_stops_like_the_floor_with_the_same_way_back():
    tr = rows([0.5, -0.5] * 12, step=10 * DAY)  # 0.0 at the 24th
    s = evaluate(tr, now=at(tr))
    assert s["phase"] == "STOPPED" and s["stop"]["kind"] == "VERDICT" and s["stop"]["n"] == 24
    assert s["verdict"]["result"] == "FAILED" and "거부" in resume_block(s)
    tr2 = tr + rows([1.0] * 4, first=tr[-1][0] + 10 * DAY)
    s = evaluate(tr2, now=at(tr2))
    assert s["phase"] == "STOPPED" and s["eligible"]
    s = evaluate(tr2, now=at(tr2), resumes=[{"n": 28, "ts": at(tr2), "cum": 4.0}])
    assert s["phase"] == "RESUMED" and s["ref"] == pytest.approx(4.0)


def test_twenty_four_months_bring_the_verdict_with_the_trades_closed_by_then():
    tr = rows([1.0, -0.5, -0.4], step=100 * DAY)  # +0.1 after three trades, all long before the date
    assert evaluate(tr, now=VERDICT_TS - 1)["verdict"] is None
    s = evaluate(tr, now=VERDICT_TS)
    assert s["phase"] == "ACTIVE" and s["verdict"] == {"result": "PASSED", "n": 3, "cum": pytest.approx(0.1), "by": "DATE"}
    late = tr + [(VERDICT_TS - DAY, VERDICT_TS + DAY, -3.0)]  # closed after the date: not in the verdict
    s = evaluate(late, now=VERDICT_TS + 2 * DAY)
    assert s["verdict"]["result"] == "PASSED" and s["verdict"]["n"] == 3 and s["n"] == 4 and s["phase"] == "ACTIVE"
    flat = rows([1.0, -1.0], step=100 * DAY)
    s = evaluate(flat, now=VERDICT_TS)
    assert s["phase"] == "STOPPED" and s["stop"] == {"kind": "VERDICT", "n": 2, "cum": pytest.approx(0.0),
                                                     "exit_ts": flat[1][1]}
    s = evaluate([], now=VERDICT_TS)  # no trade at all in 24 months: 0 is not above 0
    assert s["phase"] == "STOPPED" and s["stop"]["kind"] == "VERDICT" and s["stop"]["n"] == 0
    assert evaluate(rows([0.1] * 24, step=10 * DAY), now=VERDICT_TS + DAY)["verdict"]["by"] == "TRADES"  # the 24th came first


def test_a_failed_verdict_while_already_stopped_changes_nothing_and_after_a_resume_stops_again():
    tr = rows([-1.0] * 5 + [0.1] * 19, step=10 * DAY)  # floor at 5, -3.1 at 24
    s = evaluate(tr, now=at(tr))
    assert s["verdict"]["result"] == "FAILED" and s["retire"] == "NO_RECOVERY"  # 16 trades after the floor passed at 21
    tr = rows([-1.0] * 5 + [1.0] * 4 + [0.0] * 15, step=10 * DAY)  # floor at 5, allowed at 9, -1.0 at 24
    s = evaluate(tr, now=at(tr))
    assert s["phase"] == "STOPPED" and s["stop"]["kind"] == "FLOOR" and s["eligible"] and s["verdict"]["result"] == "FAILED"
    s = evaluate(tr, now=at(tr), resumes=[{"n": 9, "ts": float(tr[8][1] + 60), "cum": -1.0}])
    assert s["phase"] == "STOPPED" and s["stop"]["kind"] == "VERDICT" and s["stop"]["n"] == 24 and not s["eligible"]
    assert s["resumes"] == 1


def test_a_resume_before_the_date_is_applied_before_the_dates_verdict():
    tr = rows([-1.0] * 5 + [1.0] * 5, step=30 * DAY)  # 0.0 after ten trades, all before the date
    resume = [{"n": 10, "ts": float(VERDICT_TS - DAY), "cum": 0.0}]
    s = evaluate(tr, now=VERDICT_TS + H1, resumes=resume)
    assert s["resumes"] == 1 and s["phase"] == "STOPPED" and s["stop"]["kind"] == "VERDICT"
    after = [{"n": 10, "ts": float(VERDICT_TS + 60), "cum": 0.0}]  # the date's verdict came first: recorded only
    s = evaluate(tr, now=VERDICT_TS + H1, resumes=after)
    assert s["verdict"]["result"] == "FAILED" and s["phase"] == "RESUMED" and s["stop"] is None


# ----------------------------------------------------------------------------- the frequency review
def test_frequency_counts_the_gap_and_the_trailing_year():
    now = float(ts("2026-11-18 12:00"))
    entries = [ts(x) for x in ("2025-11-21 14:00", "2025-12-07 17:00", "2025-12-15 02:00", "2026-01-08 16:00",
                               "2026-05-25 21:00", "2026-06-15 10:00", "2026-08-05 01:00", "2026-08-09 23:00",
                               "2026-08-30 21:00", "2025-11-11 06:00")]
    f = frequency(entries, now)
    assert f["last_entry"] == ts("2026-08-30 21:00") and f["trailing"] == 9 and not f["gap"] and not f["few"]
    assert f["gap_days"] == pytest.approx(79.625)
    f = frequency(entries, float(ts("2026-08-30 21:00") + GAP_MAX_DAYS * DAY))
    assert not f["gap"]  # 137 days exactly: not more than 137
    f = frequency(entries, float(ts("2026-08-30 21:00") + GAP_MAX_DAYS * DAY + 1))
    assert f["gap"] and f["few"] and f["trailing"] == 5  # 2027-01-14: the 2026-01-08 entry has left the trailing year
    f = frequency(entries, float(ts("2026-12-16 00:00")))
    assert f["trailing"] == 6 and not f["few"]
    assert frequency(entries + [ts("2027-06-01 00:00")], now)["trailing"] == 9  # an entry after `now` is not counted
    f = frequency([], now)
    assert f["last_entry"] is None and f["gap_days"] is None and not f["gap"] and f["few"] and f["trailing"] == 0


# ----------------------------------------------------------------------------- the words
def test_status_text_shows_the_numbers_the_way_back_and_what_is_unknown():
    tr = rows([-1.0, 0.5])
    s = evaluate(tr, now=at(tr))
    text = status_text({"status": s, "as_of": at(tr), "error": None}, None)
    assert len(text) == 1 and text[0].startswith("엣지 규칙: 정상") and "2건" in text[0] and "-0.50" in text[0]
    assert "하한까지 4.50R" in text[0] and "24번째 거래 또는 2028-11-06" in text[0]
    tr = rows([-1.0] * 5 + [1.0])
    s = evaluate(tr, now=at(tr))
    line = status_text({"status": s, "as_of": at(tr), "error": None}, None)[0]
    assert line.startswith("엣지 규칙: 중단") and "+1.00R" in line and "15건" in line and "/resume 거부" in line
    unknown = status_text({"status": s, "as_of": at(tr), "error": "paper ledger fetch failed: HTTP 502",
                           "error_ts": at(tr) + H1}, None)[0]
    assert unknown.startswith("엣지 규칙: 알 수 없음") and "HTTP 502" in unknown and "중단" in unknown
    never = status_text({"status": None, "as_of": None, "error": "paper ledger stale", "error_ts": 1.0}, None)[0]
    assert never.startswith("엣지 규칙: 알 수 없음") and "stale" in never
    assert status_text(None, None) == []
    f = frequency([ts("2026-08-30 21:00")], float(ts("2027-02-01 00:00")))
    lines = status_text(None, {**f, "as_of": 1.0, "error": None})
    assert len(lines) == 1 and lines[0].startswith("신호 빈도: 검토 필요") and "154일" in lines[0] and "1건" in lines[0]
    assert status_text(None, {"error": "no cycle ended OK in the last 3 hours", "as_of": 1.0})[0].startswith(
        "신호 빈도: 알 수 없음")
    retired = rows([-1.0] * 5 + [0.0] * 16)
    line = status_text({"status": evaluate(retired, now=at(retired)), "as_of": 1.0, "error": None}, None)[0]
    assert line.startswith("엣지 규칙: 은퇴 권고") and "사용자" in line
