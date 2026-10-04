from __future__ import annotations

import pytest
import requests

from ewbot.live.audit import AuditError, AuditResult, audit, fetch_paper_ledger
from ewbot.live.exchange import KeyPermissions
from ewbot.live.stage import (
    StageError,
    canary_entry_gates,
    canary_gates,
    full_cap,
    full_entry_gates,
    gate_lines,
    on_incident,
    pause,
    paused,
    resume,
    shadow_gates,
    transition,
)
from ewbot.types import H1
from tests.live.helpers import journal

DAY = 86_400
S0 = 1_791_000_000.0  # SHADOW start
KEY = KeyPermissions(True, True, False)
PAPER_OK = {"c1": True, "c2": True, "c3": False, "count": {"total": 37, "required": 50},
            "c4": {"met": True, "days": 28.5, "coverage": 0.99, "unexplained": 0}}
GOOD_AUDIT = AuditResult((0, 1), 12, 12, ())


def dec(kind: str, ts: int, key: int, price: float = 1.0, **kw) -> dict:
    return {"id": f"{kind}:{ts}:{key}", "kind": kind, "ts": ts, "price": price, "contracts": 1, "reason": kind,
            "trade_key": key, "levels": [], "setup": "W3", "direction": 1, **kw}


def ledger(decisions: list[dict], started: int = 1000, through: int = 100_000, fp: str = "fp") -> dict:
    return {"epochs": [{"id": 1, "fingerprint": fp, "started_ts": started, "through_ts": through}],
            "decisions": decisions}


def test_audit_matches_identical_decisions_and_reports_each_difference():
    paper = [dec("SIGNAL", 3600, 3600), dec("ENTRY", 7200, 7200, 2.0), dec("TRAIL", 10800, 7200, 3.0),
             dec("ENTRY", 500, 500)]  # known before the epoch start: outside the window
    live = [dec("SIGNAL", 3600, 3600), dec("ENTRY", 7200, 7200, 2.0 + 1e-12), dec("TRAIL", 10800, 7200, 3.5),
            dec("STOP", 14400, 7200), dec("SIGNAL", 200_000, 200_000)]  # known after the paper's last bar
    res = audit(live, ledger(paper), baseline_fingerprint="fp")
    assert (res.window, res.compared, res.matched) == ((1000, 100_000), 4, 2)
    assert [(m["id"], m["type"]) for m in res.mismatches] == [("STOP:14400:7200", "MISSING_IN_PAPER"),
                                                             ("TRAIL:10800:7200", "DIFFERENT")]
    assert not res.ok and audit(paper, ledger(paper)).ok
    other = audit(paper, ledger(paper, fp="new"), baseline_fingerprint="fp")
    assert other.mismatches[0]["type"] == "PAPER_FINGERPRINT"
    assert audit([], ledger(paper), since=8000).compared == 1  # only TRAIL is after `since`
    assert audit([], ledger([])).ok is False  # nothing compared is not a pass


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class Sess:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_fetch_paper_ledger_uses_the_contents_api_and_hides_the_token():
    s = Sess(Resp(200, ledger([])))
    assert fetch_paper_ledger("me/repo", "ghp_SECRET", s)["epochs"][0]["id"] == 1
    url, params, headers = s.calls[0]
    assert url == "https://api.github.com/repos/me/repo/contents/ledger.json" and params == {"ref": "paper"}
    assert headers["Accept"] == "application/vnd.github.raw+json" and headers["Authorization"] == "Bearer ghp_SECRET"
    for answer in (Resp(404, {"message": "Not Found"}), requests.exceptions.ConnectionError("ghp_SECRET")):
        with pytest.raises(AuditError) as err:
            fetch_paper_ledger("me/repo", "ghp_SECRET", Sess(answer))
        assert "ghp_SECRET" not in str(err.value)


def shadow_journal(tmp_path, days: float = 15.0):
    jr = journal(tmp_path)
    jr.set_stage("SHADOW", "initial", S0)
    jr.add_event(S0 + 10, "RECOVERY", {"accepted": 0, "abandoned": 0, "unknown": 0, "rebuilt": 0})
    jr.add_event(S0 + 5 * DAY, "RECOVERY", {"accepted": 1, "abandoned": 0, "unknown": 0, "rebuilt": 0})
    for k in range(3):
        bar = int(S0) // H1 * H1 + k * H1
        jr.start_cycle(bar, bar + H1 + 30, "SHADOW")
        jr.finish_cycle(bar, bar + H1 + 75, "OK")
    jr.add_decisions([dec("SIGNAL", int(S0) + 7200, int(S0) + 7200)], now=S0 + 7300)
    jr.add_event(S0 + 7300, "PREVIEW", {"ok": True, "errors": []})
    return jr, S0 + days * DAY


def test_shadow_gates_all_met(tmp_path):
    jr, now = shadow_journal(tmp_path)
    gates = shadow_gates(jr, now, GOOD_AUDIT)
    assert all(g.met for g in gates), gate_lines(gates)
    jr.close()


def test_each_shadow_gate_can_fail(tmp_path):
    jr, now = shadow_journal(tmp_path, days=13.0)
    bar = int(S0) // H1 * H1 + 10 * H1
    jr.start_cycle(bar, bar + H1 + 30, "SHADOW")
    jr.finish_cycle(bar, bar + H1 + 121, "OK")  # 121 s after the close
    jr.add_event(S0 + 9000, "PREVIEW", {"ok": False, "errors": ["PREVIEW_INSUFFICIENT_FUND"]})
    jr.add_event(S0 + 9 * DAY, "RECOVERY", {"accepted": 0, "abandoned": 0, "unknown": 1, "rebuilt": 0})
    bad = AuditResult((0, 1), 3, 2, ({"id": "x", "type": "DIFFERENT"},))
    met = {g.name: g.met for g in shadow_gates(jr, now, bad)}
    assert met == {"기간 14일 이상": False, "신호 1건 이상": True, "① 판단이 모의매매와 100% 일치": False,
                   "② 모든 사이클이 마감 후 2분 안에 끝남": False, "③ 냈을 주문 전부 preview 통과": False,
                   "④ 장애·재시작 시험 통과": False}
    jr.close()


def test_canary_needs_shadow_paper_and_a_trade_key(tmp_path):
    jr, now = shadow_journal(tmp_path)
    assert all(g.met for g in canary_entry_gates(jr, now, GOOD_AUDIT, PAPER_OK, KEY))
    no_c4 = {**PAPER_OK, "c4": {**PAPER_OK["c4"], "met": False}}
    assert not all(g.met for g in canary_entry_gates(jr, now, GOOD_AUDIT, no_c4, KEY))
    assert not all(g.met for g in canary_entry_gates(jr, now, GOOD_AUDIT, PAPER_OK, KeyPermissions(True, True, True)))
    assert not all(g.met for g in canary_entry_gates(jr, now, GOOD_AUDIT, None, KEY))
    jr.close()


def test_transitions_need_confirm_flat_book_and_met_gates(tmp_path):
    jr, now = shadow_journal(tmp_path)
    gates = canary_entry_gates(jr, now, GOOD_AUDIT, PAPER_OK, KEY)
    with pytest.raises(StageError, match="--confirm"):
        transition(jr, "CANARY", now, gates, confirm=False, flat=True)
    with pytest.raises(StageError, match="flat"):
        transition(jr, "CANARY", now, gates, confirm=True, flat=False)
    with pytest.raises(StageError, match="one stage at a time"):
        transition(jr, "FULL", now, gates, confirm=True, flat=True)
    with pytest.raises(StageError, match="gates not met"):
        transition(jr, "CANARY", now, canary_entry_gates(jr, now, None, PAPER_OK, KEY), confirm=True, flat=True)
    transition(jr, "CANARY", now, gates, confirm=True, flat=True)
    assert jr.stage() == ("CANARY", now)
    transition(jr, "SHADOW", now + 1, [], confirm=True, flat=True)  # a demotion needs no gates
    assert jr.stage()[0] == "SHADOW"
    with pytest.raises(StageError):
        transition(jr, "SHADOW", now + 2, [], confirm=True, flat=True)
    jr.close()


def canary_journal(tmp_path, closed: int = 3):
    jr = journal(tmp_path)
    jr.set_stage("CANARY", "promotion", S0)
    for k in range(closed):
        jr.add_event(S0 + k * DAY, "PROTECTED_FIRST", {"trade_id": k + 1, "after_entry_s": 2.5})
        jr.add_event(S0 + k * DAY + 3600, "CLOSED", {"trade_id": k + 1, "reason": "BRACKET"})
    jr.add_event(S0 + 3 * DAY, "COSTS", {"fills": 6})
    return jr


def test_canary_pass_and_full_entry(tmp_path):
    jr = canary_journal(tmp_path)
    now = S0 + 20 * DAY
    assert all(g.met for g in canary_gates(jr, now)), gate_lines(canary_gates(jr, now))
    assert not all(g.met for g in full_entry_gates(jr, now, PAPER_OK, KEY))  # 37/50
    assert all(g.met for g in full_entry_gates(jr, now, {**PAPER_OK, "c3": True}, KEY))
    jr.add_event(now, "UNPROTECTED_60S", {"trade_id": 9})
    jr.add_event(now, "ADOPTED", {"trade_id": 9})
    jr.add_event(now, "CLOSE_FAILED", {"trade_id": 9})
    met = {g.name: g.met for g in canary_gates(jr, now)}
    assert not met["② 손절 이동과 청산이 목표와 일치"] and not met["③ 보호되지 않은 상태 60초 초과 없음"]
    assert not met["④ 수동 개입 없음"]
    jr.close()


def test_canary_needs_three_trades_or_ninety_days(tmp_path):
    jr = canary_journal(tmp_path, closed=1)
    assert not canary_gates(jr, S0 + 30 * DAY)[0].met and canary_gates(jr, S0 + 91 * DAY)[0].met
    jr.close()


def test_pause_resume_incidents_and_the_full_cap(tmp_path):
    jr = journal(tmp_path)
    jr.set_stage("SHADOW", "initial", S0)
    assert on_incident(jr, "UNPROTECTED_60S", S0 + 1) == [] and paused(jr) is None  # SHADOW: recorded only
    jr.set_stage("CANARY", "promotion", S0 + 2)
    assert on_incident(jr, "FOREIGN_ORDERS", S0 + 3) == ["paused"] and paused(jr)["reason"] == "incident FOREIGN_ORDERS"
    resume(jr, S0 + 4)
    assert paused(jr) is None
    jr.set_stage("FULL", "promotion", S0 + 5)
    assert full_cap(jr, S0 + 6) == 3 and full_cap(jr, S0 + 5 + 30 * DAY) is None
    assert on_incident(jr, "FOREIGN_ORDERS", S0 + 7) == []  # not a major incident in FULL
    assert on_incident(jr, "BREAKER", S0 + 8) == ["paused", "demoted to CANARY"] and jr.stage()[0] == "CANARY"
    pause(jr, "user", S0 + 9)
    assert [e["kind"] for e in jr.events(kind="INCIDENT")] == ["INCIDENT"] * 4
    jr.close()
