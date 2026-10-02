from __future__ import annotations

import os

import pytest

from ewbot.backtest import report as bt_report
from ewbot.engine.loop import EquityPoint, Trade
from ewbot.paper.criteria import (
    criterion4,
    golive_status,
    run_coverage,
    status_lines,
    summarize_compare,
)
from ewbot.paper.ledger import ensure_epoch, new_ledger, replace_epoch_trades
from ewbot.paper.report import write_paper_report
from ewbot.types import H1
from tests.paper.helpers import BOUNDARY, synth_frame

LOCK = {
    "sha256": "a" * 64, "params": [2.0, 0.5, 0.0], "holdout_start": 1_704_067_200,
    "data": {"last_bar_ts": BOUNDARY, "sha256": "b" * 64},
    "first_run": {"criteria": {"n_trades": {"A": 36, "HYBRID": 40}}},
}
T0 = BOUNDARY + 10 * H1
DAY = 86_400


def _m(sharpe, calmar, dd=0.05, n=40):
    return {"total_return": 0.1, "max_dd": dd, "sharpe": sharpe, "calmar": calmar, "n_trades": n}


def _cmp(a_sharpe=1.0, h_sharpe=0.4, a_dd=0.05):
    return {
        "comparison": {"A": _m(a_sharpe, 2.0, a_dd), "HYBRID": _m(h_sharpe, 2.0), "C": _m(0.5, 1.0),
                       "BH": _m(0.6, 1.5, 0.5), "EMA": _m(0.2, 0.3)},
        "random": {"runs": 2, "n_signals": 40, "dist": {"sharpe": {"mean": 0.1}, "calmar": {"mean": 0.2}}},
    }


def _trade(entry_ts: int, exit_ts: int) -> Trade:
    return Trade(entry_ts, exit_ts, "W3", 1, 2, 100.0, 101.0, 95.0, 105.0, 110.0, 2.0, 0.1, 0.0, 0.2, "TP2",
                 {"q_top": 0.7, "s_rev": 0.6, "counts": [{"pattern": "IMP", "d": 1, "phase": "W2", "q": 0.7,
                                                          "points": [[entry_ts - 40 * H1, 99.0], [entry_ts - 8 * H1, 103.0]]}]})


def _hourly_runs(start: int, hours: int, skip: set[int] = frozenset(), epoch_id: int = 1) -> list[dict]:
    return [{"ts": start + h * H1 + 420, "status": "ok", "epoch_id": epoch_id, "last_bar_ts": start + h * H1 - H1}
            for h in range(hours) if h not in skip]


def test_run_coverage_counts_distinct_hours():
    runs = _hourly_runs(T0, 100, skip={3, 4, 50})
    runs.append({**runs[0], "ts": runs[0]["ts"] + 60})  # a second run in the same hour counts once
    runs.append({**runs[1], "epoch_id": 2})  # another epoch is ignored
    cov = run_coverage(runs, 1, T0 + 99 * H1 + 600)
    assert cov["hours"] == 100 and cov["covered"] == 97 and cov["coverage"] == pytest.approx(0.97)
    assert run_coverage([], 1, T0)["coverage"] == 0.0


def test_criterion4_needs_28_days_95_percent_and_no_unexplained():
    led = new_ledger("fp", "c" * 40, LOCK)
    hours = 28 * 24 + 1
    led.runs = _hourly_runs(T0, hours, skip=set(range(10, 30)))
    now = T0 + (hours - 1) * H1 + 600
    c4 = criterion4(led, now)
    assert c4["days"] >= 28 and c4["coverage"] > 0.95 and c4["unexplained"] == 0 and c4["met"]
    assert not criterion4(led, T0 + 27 * DAY)["met"]  # too short
    led.discrepancies.append({"cause": "UNEXPLAINED", "epoch_id": 1})
    assert not criterion4(led, now)["met"]
    led.discrepancies[0]["cause"] = "DATA_REVISION"
    assert criterion4(led, now)["met"]
    led.runs = _hourly_runs(T0, hours, skip=set(range(10, 60)))  # 50 missing hours: < 95%
    assert not criterion4(led, now)["met"]


def test_summarize_compare_and_status_lines():
    led = new_ledger("fp", "c" * 40, LOCK)
    st = golive_status(led, T0)
    assert st["c1"] is None and st["c2"] is None and st["c3"] is False and not st["all_met"]
    lines = status_lines(st)
    assert "미계산" in lines[1] and "3. 표본 외 거래 36/50 (홀드아웃 36 + 모의 0): 미충족" in lines[3]
    assert lines[-1] == "=> 실거래 전환 검토 불가"

    led.criteria = summarize_compare(_cmp(), 1_704_067_200, T0, 1, T0)
    assert led.criteria["winners"] == ["A"] and led.criteria["max_dd_ok"] is True
    assert led.criteria["headline"]["A"]["sharpe"] == 1.0 and led.criteria["random"]["runs"] == 2
    st = golive_status(led, T0)
    assert st["c1"] is True and st["c2"] is True
    assert "1. 위험 대비 수익 A > C·BH·RANDOM (샤프·Calmar): 충족 [우위: A]" in status_lines(st)[1]
    assert "2. 최대 낙폭 <= 15% (A): 충족 [A 5.0%]" in status_lines(st)[2]
    led.criteria = summarize_compare(_cmp(a_dd=0.2), 1_704_067_200, T0, 1, T0)
    assert golive_status(led, T0)["c2"] is False  # A wins on risk-adjusted return but draws down 20%
    led.criteria = summarize_compare(_cmp(a_sharpe=0.3, h_sharpe=1.0), 1_704_067_200, T0, 1, T0)
    st = golive_status(led, T0)
    assert st["c1"] is False and st["c2"] is False  # HYBRID wins, but the paper runtime trades and counts A
    assert "[우위: HYBRID]" in status_lines(st)[1]


def test_all_four_criteria_met_reads_review_possible():
    led = new_ledger("fp", "c" * 40, LOCK)
    hours = 28 * 24 + 1
    led.runs = _hourly_runs(T0, hours)
    replace_epoch_trades(led, 1, [_trade(T0 + i * 3 * H1, T0 + i * 3 * H1 + H1) for i in range(14)])
    led.criteria = summarize_compare(_cmp(), 1_704_067_200, T0, 1, T0)
    st = golive_status(led, T0 + (hours - 1) * H1 + 600)
    assert st["count"]["total"] == 50 and st["all_met"]
    assert status_lines(st)[-1] == "=> 실거래 전환 검토 가능 (자동 전환하지 않음)"


def test_other_epoch_is_not_eligible_and_old_criteria_do_not_count():
    led = new_ledger("fp", "c" * 40, LOCK)
    led.criteria = summarize_compare(_cmp(), 1_704_067_200, T0, 1, T0)
    ensure_epoch(led, "edited", T0)
    st = golive_status(led, T0)
    assert not st["eligible"] and st["c1"] is None and st["count"]["total"] == 0
    assert "실거래 전환 판정 대상 아님" in status_lines(st)[1]


def _ledger_for_report():
    led = new_ledger("fp" * 32, "c" * 40, LOCK)
    replace_epoch_trades(led, 1, [_trade(T0, T0 + 5 * H1)])
    led.runs = [{"id": "r1", "ts": T0 + 30 * H1, "status": "ok", "epoch_id": 1, "last_bar_ts": T0 + 29 * H1,
                 "holdout_data_ok": False}]
    led.discrepancies = [{"id": "CHANGED:ENTRY:1:1", "type": "CHANGED", "decision_id": "ENTRY:1:1",
                          "cause": "DATA_REVISION", "epoch_id": 1, "detected_ts": T0, "detected_run": "r1",
                          "recorded": None, "recomputed": None}]
    led.open_position = {"entry_ts": T0 + 20 * H1, "setup": "C_END", "direction": -1, "contracts": 4, "remaining": 2,
                         "entry_price": 100.0, "stop": 101.0, "pnl_if_closed": -3.0, "counted": True}
    return led


def test_paper_report_sections(tmp_path):
    led = _ledger_for_report()
    led.criteria = summarize_compare(_cmp(), 1_704_067_200, T0, 1, T0)
    eq = [EquityPoint(BOUNDARY + i * H1, 10_000.0 + i) for i in range(-5, 40)]
    top = {"ts": T0, "pattern": "IMP", "d": 1, "phase": "W3", "q": 0.71, "q_top": 0.71, "s_rev": 0.4, "E_live": 21_000.0}
    path = write_paper_report(tmp_path / "report.html", led, golive_status(led, T0 + 40 * H1), eq,
                              [_trade(T0, T0 + 5 * H1)], synth_frame(), top, T0 + 40 * H1)
    html = path.read_text(encoding="utf-8")
    for part in ("실거래 전환 판정", "1. 위험 대비 수익", "4. 모의매매 재현", "현재 1등 카운트", "IMP d=1 W3",
                 "포지션: C_END 숏 2/4계약", "불일치 (재현성 손상 기록)", "Trades - epoch 1 (current) (1)",
                 "CHANGED:ENTRY:1:1", "DATA_REVISION", "eq-chart", "dd-chart", "A paper", "criteria 1-2 window"):
        assert part in html, part
    assert html.index("실거래 전환 판정") < html.index("eq-chart")  # the go-live block is at the top
    assert sorted(os.listdir(tmp_path)) == ["report.html"]


def test_paper_report_write_is_atomic(tmp_path, monkeypatch):
    led = _ledger_for_report()
    target = tmp_path / "report.html"
    target.write_text("previous", encoding="utf-8")

    def boom(path, title, sections):
        path.write_text("half", encoding="utf-8")
        raise RuntimeError("render failed")

    monkeypatch.setattr("ewbot.paper.report.write_report", boom)
    with pytest.raises(RuntimeError):
        write_paper_report(target, led, golive_status(led, T0), [], [], synth_frame(), None, T0)
    assert target.read_text(encoding="utf-8") == "previous" and sorted(os.listdir(tmp_path)) == ["report.html"]
    assert bt_report.write_report is not boom  # the shared backtest writer itself is untouched
