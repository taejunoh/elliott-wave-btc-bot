from __future__ import annotations

import copy
import json

import pytest
import requests

from ewbot.config import BotConfig
from ewbot.live.audit import AuditError, AuditResult, audit, fetch_paper_ledger
from ewbot.live.exchange import KeyPermissions
from ewbot.live.pricing import stage_contracts
from ewbot.live.stage import (
    CANARY_FAIL,
    ORDER_FAILURES,
    Gate,
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
S0 = 1_791_000_000.0  # SHADOW start (on the hour)
NOW = 1_791_500_000.0
KEY = KeyPermissions(True, True, False)
PAPER_OK = {"c1": True, "c2": True, "c3": False, "count": {"total": 37, "required": 50},
            "c4": {"met": True, "days": 28.5, "coverage": 0.99, "unexplained": 0}}
PAPER_ALL = {**PAPER_OK, "c3": True, "count": {"total": 50, "required": 50}}
AUDIT_GATE = "① 판단이 모의매매와 100% 일치"
CYCLE_GATE = "② 모든 사이클이 마감 후 2분 안에 끝남"


def good(now: float) -> AuditResult:
    """A passing audit from before any stage start to the bar that closed an hour before `now`."""
    return AuditResult((0, int(now) // H1 * H1 - 2 * H1), 12, 12, (), now - 600)


def dec(kind: str, ts: int, key: int, price: float = 1.0, **kw) -> dict:
    return {"id": f"{kind}:{ts}:{key}", "kind": kind, "ts": ts, "price": price, "contracts": 1, "reason": kind,
            "trade_key": key, "levels": [], "setup": "W3", "direction": 1, **kw}


def ledger(decisions: list[dict], started: int = 1000, through: int = 100_000, fp: str = "fp",
           epochs: list[dict] | None = None, runs: list[dict] | None = None) -> dict:
    """A schema-1 paper ledger (paper.ledger.Ledger)."""
    return {"schema_version": 1, "baseline": {"fingerprint": "fp", "commit": "abc1234"}, "holdout": {},
            "epochs": epochs or [{"id": 1, "fingerprint": fp, "started_ts": started, "through_ts": through}],
            "decisions": decisions, "runs": runs or []}


def run(last_bar: int, sha: str) -> dict:
    return {"id": f"r{last_bar}", "status": "ok", "last_bar_ts": last_bar, "data_sha256": sha}


def test_audit_matches_identical_decisions_and_reports_each_difference():
    paper = [dec("SIGNAL", 3600, 3600), dec("ENTRY", 7200, 7200, 2.0), dec("TRAIL", 10800, 7200, 3.0),
             dec("ENTRY", 500, 500)]  # known before the epoch start: outside the window
    live = [dec("SIGNAL", 3600, 3600), dec("ENTRY", 7200, 7200, 2.0 + 1e-12), dec("TRAIL", 10800, 7200, 3.5),
            dec("STOP", 14400, 7200), dec("SIGNAL", 200_000, 200_000)]  # known after the paper's last bar
    res = audit(live, ledger(paper), baseline_fingerprint="fp", now=NOW)
    assert (res.window, res.compared, res.matched, res.as_of) == ((1000, 100_000), 4, 2, NOW)
    assert [(m["id"], m["type"], m["cause"]) for m in res.mismatches] == [
        ("STOP:14400:7200", "MISSING_IN_PAPER", "UNEXPLAINED"), ("TRAIL:10800:7200", "DIFFERENT", "UNEXPLAINED")]
    assert not res.ok and audit(paper, ledger(paper), now=NOW).ok
    other = audit(paper, ledger(paper, fp="new"), baseline_fingerprint="fp", now=NOW)
    assert (other.mismatches[0]["type"], other.mismatches[0]["cause"]) == ("PAPER_FINGERPRINT", "FINGERPRINT_CHANGE")
    assert audit([], ledger(paper), since=8000, now=NOW).compared == 1  # only TRAIL is after `since`
    assert audit([], ledger([]), now=NOW).ok is False  # nothing compared is not a pass


def test_the_window_ends_at_the_last_live_cycle_and_starts_at_the_epoch_floor():
    paper = [dec("ENTRY", 7200, 7200), dec("TRAIL", 10800, 7200, 3.0)]
    live = [dec("ENTRY", 7200, 7200)]  # the live cycle of bar 10800 was held (data late): TRAIL comes next cycle
    assert [m["type"] for m in audit(live, ledger(paper), now=NOW).mismatches] == ["MISSING_IN_LIVE"]
    res = audit(live, ledger(paper), until=7200, now=NOW)  # `until`: the last bar whose live cycle ended OK
    assert res.ok and res.window == (1000, 7200)
    # a later epoch records from the previous epoch's through_ts (paper.runner._recording_floor), and one back on
    # the baseline fingerprint is audited like epoch 1
    epochs = [{"id": 1, "fingerprint": "fp", "started_ts": 1000, "through_ts": 50_000},
              {"id": 2, "fingerprint": "other", "started_ts": 60_000, "through_ts": 70_000},
              {"id": 3, "fingerprint": "fp", "started_ts": 80_000, "through_ts": 90_000}]
    paper3 = [dec("TRAIL", 75_600, 3600, 3.0)]  # first known by the run that opened epoch 3
    res = audit([dec("TRAIL", 75_600, 3600, 4.0)], ledger(paper3, epochs=epochs), baseline_fingerprint="fp", now=NOW)
    assert res.window == (70_000, 90_000) and [m["type"] for m in res.mismatches] == ["DIFFERENT"]


def test_a_bar_revised_between_the_two_fetches_is_a_data_revision():
    t = 36_000
    runs = [run(t - 3600, "h-before"), run(t, "h-paper")]
    paper = [dec("TRAIL", t, 7200, 3.0, data_sha256="h-paper")]
    live = [dec("TRAIL", t, 7200, 3.5)]
    revised = {t - 3600: "h-before", t: "h-live"}  # the live candles: equal up to the paper's previous run, not at t
    res = audit(live, ledger(paper, runs=runs), now=NOW, data_hash=revised.get)
    assert [(m["type"], m["cause"]) for m in res.mismatches] == [("DIFFERENT", "DATA_REVISION")] and not res.ok
    alone = audit(live, ledger([], runs=runs), now=NOW, data_hash=revised.get)  # the paper's run at t has its hash
    assert [(m["type"], m["cause"]) for m in alone.mismatches] == [("MISSING_IN_PAPER", "DATA_REVISION")]
    same = {t - 3600: "h-before", t: "h-paper"}  # the same candles
    parted_before = {t - 3600: "x", t: "h-live"}  # different before the bar already: not located at this bar
    for hashes in (same, parted_before):
        assert audit(live, ledger(paper, runs=runs), now=NOW, data_hash=hashes.get).mismatches[0]["cause"] == "UNEXPLAINED"
    assert audit(live, ledger(paper, runs=runs), now=NOW).mismatches[0]["cause"] == "UNEXPLAINED"  # no live candles


def test_a_ledger_that_is_not_a_valid_paper_ledger_is_an_audit_error():
    good_ledger = ledger([dec("TRAIL", 10800, 7200, 3.0)])

    def broken(change) -> dict:
        led = copy.deepcopy(good_ledger)
        change(led)
        return led

    cases = {
        "not an object": [good_ledger],
        "metadata JSON": {"name": "ledger.json", "sha": "abc", "size": 10, "content": "e30="},
        "schema 2": broken(lambda d: d.update(schema_version=2)),
        "no epochs": broken(lambda d: d.pop("epochs")),
        "empty epochs": broken(lambda d: d.update(epochs=[])),
        "through None": broken(lambda d: d["epochs"][0].update(through_ts=None)),
        "decisions a dict": broken(lambda d: d.update(decisions={"a": 1})),
        "decision without levels": broken(lambda d: d["decisions"][0].pop("levels")),
    }
    live = [dec("TRAIL", 10800, 7200, 3.0)]
    for name, bad in cases.items():
        with pytest.raises(AuditError):
            audit(live, bad, now=NOW)
            pytest.fail(name)
    assert audit(live, good_ledger, now=NOW).ok


class Resp:
    def __init__(self, status: int, body: bytes = b"", chunks: list[bytes] | None = None) -> None:
        self.status_code, self.chunks, self.closed = status, chunks if chunks is not None else [body], False

    def iter_content(self, size):
        yield from self.chunks

    def close(self):
        self.closed = True


class Sess:
    def __init__(self, answer):
        self.answer, self.calls, self.closed = answer, [], False

    def get(self, url, params=None, headers=None, timeout=None, stream=False):
        self.calls.append((url, params, headers, timeout, stream))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def close(self):
        self.closed = True


def test_fetch_paper_ledger_uses_the_contents_api_bounds_its_time_and_hides_the_token(monkeypatch):
    body = json.dumps(ledger([])).encode()
    s = Sess(Resp(200, body))
    assert fetch_paper_ledger("me/repo", "ghp_SECRET", s)["epochs"][0]["id"] == 1
    url, params, headers, timeout, stream = s.calls[0]
    assert url == "https://api.github.com/repos/me/repo/contents/ledger.json" and params == {"ref": "paper"}
    assert headers["Accept"] == "application/vnd.github.raw+json" and headers["Authorization"] == "Bearer ghp_SECRET"
    assert timeout == (5.0, 20.0) and stream and s.answer.closed and not s.closed  # the caller's session stays open
    for answer in (Resp(404, b'{"message": "Not Found"}'), Resp(200, b"[1, 2]"), Resp(200, b"<html>"),
                   requests.exceptions.ConnectionError("ghp_SECRET")):
        with pytest.raises(AuditError) as err:
            fetch_paper_ledger("me/repo", "ghp_SECRET", Sess(answer))
        assert "ghp_SECRET" not in str(err.value) and err.value.__context__ is None and err.value.__cause__ is None
    made: list[Sess] = []
    monkeypatch.setattr(requests, "Session", lambda: made.append(Sess(Resp(200, body))) or made[-1])
    fetch_paper_ledger("me/repo", "t")
    assert made[0].closed  # a session made here is closed here
    ticks = iter(range(0, 1000, 10))
    trickle = Resp(200, chunks=[b" "] * 10 + [body])  # 10 s per chunk: past the 30 s deadline
    with pytest.raises(AuditError, match="TimeoutError"):
        fetch_paper_ledger("me/repo", "t", Sess(trickle), clock=lambda: float(next(ticks)))
    assert trickle.closed


def hourly(jr, since: float, now: float, skip: tuple[int, ...] = ()) -> int:
    """An OK cycle 75 s after the close for every bar that closed in the stage and is past its deadline."""
    first, n = int(since) // H1 * H1 - H1, 0
    for bar in range(first, int(now) - H1 - 120 + 1, H1):
        if bar + H1 + 120 > now or bar in skip:
            continue
        jr.start_cycle(bar, bar + H1 + 30, "SHADOW")
        jr.finish_cycle(bar, bar + H1 + 75, "OK")
        n += 1
    return n


def shadow_journal(tmp_path, days: float = 15.0):
    jr = journal(tmp_path)
    jr.set_stage("SHADOW", "initial", S0)
    now = S0 + days * DAY
    jr.add_event(S0 + 10, "RECOVERY", {"accepted": 0, "abandoned": 0, "unknown": 0, "rebuilt": 0})
    jr.add_event(S0 + 5 * DAY, "RECOVERY", {"accepted": 1, "abandoned": 0, "unknown": 0, "rebuilt": 0})
    hourly(jr, S0, now)
    jr.add_decisions([dec("SIGNAL", int(S0) + 7200, int(S0) + 7200)], now=S0 + 10_900)
    jr.add_event(S0 + 10_900, "PREVIEW", {"ok": True, "errors": []})
    return jr, now


def test_shadow_gates_all_met(tmp_path):
    jr, now = shadow_journal(tmp_path)
    gates = shadow_gates(jr, now, good(now))
    assert all(g.met for g in gates), gate_lines(gates)
    assert {g.name: g.detail for g in gates}[CYCLE_GATE] == "360/360봉"  # every hour of the 15 days
    jr.close()


def test_each_shadow_gate_can_fail(tmp_path):
    jr, now = shadow_journal(tmp_path, days=13.0)
    bar = int(S0) + 10 * H1
    jr.start_cycle(bar, bar + H1 + 30, "SHADOW")
    jr.finish_cycle(bar, bar + H1 + 121, "OK")  # 121 s after the close
    jr.add_event(S0 + 9000, "PREVIEW", {"ok": False, "errors": ["PREVIEW_INSUFFICIENT_FUND"]})
    jr.add_event(S0 + 9 * DAY, "RECOVERY", {"accepted": 0, "abandoned": 0, "unknown": 1, "rebuilt": 0})
    bad = AuditResult((0, int(now) - 2 * H1), 3, 2, ({"id": "x", "type": "DIFFERENT", "cause": "UNEXPLAINED"},), now)
    met = {g.name: g.met for g in shadow_gates(jr, now, bad)}
    assert met == {"기간 14일 이상": False, "신호 1건 이상": True, AUDIT_GATE: False, CYCLE_GATE: False,
                   "③ 냈을 주문 전부 preview 통과": False, "④ 장애·재시작 시험 통과": False}
    jr.close()


def test_the_signal_and_cycle_gates_count_the_stage_itself(tmp_path):
    jr = journal(tmp_path)
    jr.set_stage("SHADOW", "initial", S0)
    now = S0 + 15 * DAY

    def gate(name: str, at: float = now) -> Gate:
        return {g.name: g for g in shadow_gates(jr, at, good(at))}[name]

    # the first cycle records every decision since 2024: none of those signals was first known in the stage
    jr.add_decisions([dec("SIGNAL", 1_730_000_000 + k * H1, 1_730_000_000 + k * H1) for k in range(3)], now=S0 + 40)
    assert not gate("신호 1건 이상").met
    jr.add_decisions([dec("SIGNAL", int(S0) + 7200, int(S0) + 7200)], now=S0 + 10_900)
    assert (gate("신호 1건 이상").met, gate("신호 1건 이상").detail) == (True, "단계 중 알게 된 신호 1건")
    # a runtime that was down: every hour without a cycle is a miss
    for k in (0, 300):
        bar = int(S0) + k * H1
        jr.start_cycle(bar, bar + H1 + 30, "SHADOW")
        jr.finish_cycle(bar, bar + H1 + 60, "OK")
    assert (gate(CYCLE_GATE).met, gate(CYCLE_GATE).detail) == (False, "2/360봉")
    # a bar while the futures session was recorded closed is excused; any other missing bar is not
    closed_bar, missing_bar = int(S0) + 100 * H1, int(S0) + 200 * H1
    jr.add_event(closed_bar + H1 - 60, "SESSION", {"text": "장 운영 상태 OPEN → CLOSE", "state": "CLOSE"})
    jr.add_event(closed_bar + 2 * H1, "SESSION", {"text": "장 운영 상태 CLOSE → OPEN", "state": "OPEN"})
    hourly(jr, S0, now, skip=(closed_bar, missing_bar))
    assert (gate(CYCLE_GATE).met, gate(CYCLE_GATE).detail) == (False, "358/359봉 (장 휴장 제외 1봉)")
    jr.start_cycle(missing_bar, missing_bar + H1 + 30, "SHADOW")
    jr.finish_cycle(missing_bar, missing_bar + H1 + 90, "OK")
    assert gate(CYCLE_GATE).met
    # a cycle still running is due only once its deadline has passed
    last = int(now) - H1  # closes at `now`
    jr.start_cycle(last, now + 30, "SHADOW")
    assert gate(CYCLE_GATE, now + 60).met and not gate(CYCLE_GATE, now + 200).met
    jr.close()


def test_the_audit_gate_needs_a_passing_audit_that_covers_the_stage_up_to_now(tmp_path):
    jr, now = shadow_journal(tmp_path)

    def audit_gate(res: AuditResult | None) -> tuple[bool, str]:
        g = {g.name: g for g in shadow_gates(jr, now, res)}[AUDIT_GATE]
        return g.met, g.detail

    assert audit_gate(good(now))[0] and audit_gate(AuditResult.from_json(json.loads(json.dumps(good(now).to_json()))))[0]
    assert audit_gate(None) == (False, "미실행")
    stale = AuditResult((0, 1_700_000_000), 1, 1, (), 1_700_010_000.0)  # an audit whose window ended in 2023
    assert not audit_gate(stale)[0] and audit_gate(stale)[1].startswith("오래된 감사")
    late = AuditResult((int(S0) + DAY, int(now) - 2 * H1), 5, 5, (), now)  # the paper epoch began during SHADOW
    assert not audit_gate(late)[0] and audit_gate(late)[1].startswith("감사 구간이 단계 시작 뒤부터")
    failed = AuditResult.failed("paper ledger malformed: KeyError: 'levels'", now)  # never the last good one
    assert audit_gate(failed) == (False, "감사 실패: paper ledger malformed: KeyError: 'levels'")
    assert not AuditResult.from_json(failed.to_json()).ok
    jr.close()


def test_canary_needs_shadow_paper_and_a_trade_key(tmp_path):
    jr, now = shadow_journal(tmp_path)
    assert all(g.met for g in canary_entry_gates(jr, now, good(now), PAPER_OK, KEY))
    no_c4 = {**PAPER_OK, "c4": {**PAPER_OK["c4"], "met": False}}
    assert not all(g.met for g in canary_entry_gates(jr, now, good(now), no_c4, KEY))
    assert not all(g.met for g in canary_entry_gates(jr, now, good(now), PAPER_OK, KeyPermissions(True, True, True)))
    assert not all(g.met for g in canary_entry_gates(jr, now, good(now), None, KEY))
    jr.close()


def test_transitions_need_confirm_flat_book_and_met_gates(tmp_path):
    jr, now = shadow_journal(tmp_path)
    gates = canary_entry_gates(jr, now, good(now), PAPER_OK, KEY)
    with pytest.raises(StageError, match="--confirm"):
        transition(jr, "CANARY", now, gates, confirm=False, flat=True)
    with pytest.raises(StageError, match="flat"):
        transition(jr, "CANARY", now, gates, confirm=True, flat=False)
    with pytest.raises(StageError, match="one stage at a time"):
        transition(jr, "FULL", now, gates, confirm=True, flat=True)
    with pytest.raises(StageError, match="gates not met"):
        transition(jr, "CANARY", now, canary_entry_gates(jr, now, None, PAPER_OK, KEY), confirm=True, flat=True)
    for wrong in ([], [Gate("anything", True, "")], shadow_gates(jr, now, good(now))):  # not this promotion's gates
        with pytest.raises(StageError, match="확인 안 됨"):
            transition(jr, "CANARY", now, wrong, confirm=True, flat=True)
    transition(jr, "CANARY", now, gates, confirm=True, flat=True)
    assert jr.stage() == ("CANARY", now)
    jr.set_stage("FULL", "test", now + 1)
    with pytest.raises(StageError, match="flat"):  # a demotion needs a flat book too
        transition(jr, "CANARY", now + 2, [], confirm=True, flat=False)
    transition(jr, "CANARY", now + 2, [], confirm=True, flat=True)  # a demotion needs no gates
    transition(jr, "SHADOW", now + 3, [], confirm=True, flat=True)
    assert [(e["data"]["from"], e["data"]["to"]) for e in jr.events(kind="STAGE")] == [
        ("SHADOW", "CANARY"), ("FULL", "CANARY"), ("CANARY", "SHADOW")]
    with pytest.raises(StageError):
        transition(jr, "SHADOW", now + 4, [], confirm=True, flat=True)
    jr.close()


def canary_journal(tmp_path, closed: int = 3, name: str = "journal.sqlite"):
    jr = journal(tmp_path, name)
    jr.set_stage("CANARY", "promotion", S0)
    for k in range(closed):
        jr.add_event(S0 + k * DAY, "PROTECTED_FIRST", {"trade_id": k + 1, "after_entry_s": 2.5})
        jr.add_event(S0 + k * DAY + 3600, "CLOSED", {"trade_id": k + 1, "reason": "BRACKET"})
    jr.add_event(S0 + 3 * DAY, "COSTS", {"fills": 6})
    return jr


def test_canary_pass_and_full_entry(tmp_path):
    jr = canary_journal(tmp_path)
    now = S0 + 20 * DAY
    assert all(g.met for g in canary_gates(jr, now, good(now))), gate_lines(canary_gates(jr, now, good(now)))
    assert not all(g.met for g in full_entry_gates(jr, now, good(now), PAPER_OK, KEY))  # 37/50
    assert all(g.met for g in full_entry_gates(jr, now, good(now), PAPER_ALL, KEY))
    jr.add_event(now, "UNPROTECTED_60S", {"trade_id": 9})
    jr.add_event(now, "ADOPTED", {"trade_id": 9})
    jr.add_event(now, "ORDER_UNRESOLVED", {"trade_id": 9, "kind": "CLOSE_FAILED"})
    met = {g.name: g.met for g in canary_gates(jr, now, good(now))}
    assert not met["② 손절 이동과 청산이 목표와 일치"] and not met["③ 보호되지 않은 상태 60초 초과 없음"]
    assert not met["④ 수동 개입 없음"]
    jr.close()


def test_every_canary_failure_pauses_canary_and_keeps_full_closed(tmp_path):
    now = S0 + 20 * DAY
    for kind in CANARY_FAIL:
        jr = canary_journal(tmp_path, name=f"{kind}.sqlite")
        assert all(g.met for g in full_entry_gates(jr, now, good(now), PAPER_ALL, KEY))
        assert on_incident(jr, kind, now) == ["paused"] and paused(jr)["reason"] == f"incident {kind}"
        resume(jr, now + 1)  # the user's /resume does not undo a CANARY failure of this stint
        gates = full_entry_gates(jr, now + 2, good(now), PAPER_ALL, KEY)
        assert not all(g.met for g in gates), kind
        with pytest.raises(StageError, match="gates not met"):
            transition(jr, "FULL", now + 3, gates, confirm=True, flat=True)
        jr.close()
    # an order failure on its own is none: the runtime resolves it or records ORDER_UNRESOLVED
    jr = canary_journal(tmp_path, name="orders.sqlite")
    for k in ORDER_FAILURES:
        jr.add_event(now, k, {"trade_id": 9})
    assert paused(jr) is None and all(g.met for g in canary_gates(jr, now + 400 * DAY, good(now + 400 * DAY)))
    pause(jr, "user /pause", now)  # paused for any reason, or a missing or failed audit: not passed
    assert [g.name for g in canary_gates(jr, now, good(now)) if not g.met] == ["일시 정지 아님"]
    resume(jr, now)
    assert [g.name for g in canary_gates(jr, now, None) if not g.met] == ["모의매매 대조 (최근 감사 통과)"]
    failed = AuditResult.failed("paper ledger fetch failed: HTTP 502", now)
    assert [g.name for g in canary_gates(jr, now, failed) if not g.met] == ["모의매매 대조 (최근 감사 통과)"]
    jr.close()


def test_canary_needs_three_trades_or_ninety_days(tmp_path):
    jr = canary_journal(tmp_path, closed=1)
    early, late = S0 + 30 * DAY, S0 + 91 * DAY
    assert not canary_gates(jr, early, good(early))[0].met and canary_gates(jr, late, good(late))[0].met
    jr.close()


def test_the_cap_is_the_journal_stages_and_lifts_only_after_30_clean_days_of_full(tmp_path):
    jr = journal(tmp_path)
    assert full_cap(jr, S0) == 0  # no stage yet: SHADOW, no real order
    jr.set_stage("CANARY", "promotion", S0)
    assert full_cap(jr, S0 + 1) == 1
    jr.set_stage("FULL", "promotion", S0 + 5)
    assert full_cap(jr, S0 + 6) == 3 and full_cap(jr, S0 + 5 + 30 * DAY) is None
    assert on_incident(jr, "FOREIGN_ORDERS", S0 + 20 * DAY) == ["3-contract cap for 30 more days"]  # not major
    assert full_cap(jr, S0 + 5 + 30 * DAY) == 3 and full_cap(jr, S0 + 50 * DAY) is None
    jr.add_event(S0 + 55 * DAY, "ORDER_UNRESOLVED", {"trade_id": 7, "kind": "CLOSE_FAILED"})  # even without its incident
    assert full_cap(jr, S0 + 84 * DAY) == 3 and full_cap(jr, S0 + 85 * DAY) is None
    kw = dict(equity=200_000.0, entry=60_000.0, stop=59_400.0, direction=1, risk_mult=1.0, cfg=BotConfig())
    assert stage_contracts("FULL", full_cap=full_cap(jr, S0 + 86 * DAY), **kw)[0] == 303  # released: uncapped
    # a major incident demotes in the journal while the FULL process still runs: the cap is CANARY's at once
    assert on_incident(jr, "UNPROTECTED_60S", S0 + 86 * DAY) == ["paused", "demoted to CANARY"]
    resume(jr, S0 + 86 * DAY + 1)
    assert full_cap(jr, S0 + 86 * DAY + 2) == 1
    assert stage_contracts("FULL", full_cap=full_cap(jr, S0 + 86 * DAY + 2), **kw)[0] == 1
    assert jr.events(kind="STAGE")[-1]["data"] == {"from": "FULL", "to": "CANARY",
                                                   "reason": "major incident UNPROTECTED_60S"}
    jr.close()


def test_pause_resume_and_incidents_by_stage(tmp_path):
    jr = journal(tmp_path)
    jr.set_stage("SHADOW", "initial", S0)
    assert on_incident(jr, "UNPROTECTED_60S", S0 + 1) == [] and paused(jr) is None  # SHADOW: recorded only
    jr.set_stage("CANARY", "promotion", S0 + 2)
    assert on_incident(jr, "FOREIGN_ORDERS", S0 + 3) == ["paused"] and paused(jr)["reason"] == "incident FOREIGN_ORDERS"
    resume(jr, S0 + 4)
    assert paused(jr) is None
    jr.set_stage("FULL", "promotion", S0 + 5)
    assert on_incident(jr, "BAD_EQUITY", S0 + 6) == []  # no CANARY failure (the runtime pauses entries itself)
    assert on_incident(jr, "BREAKER", S0 + 8) == ["paused", "demoted to CANARY"] and jr.stage()[0] == "CANARY"
    pause(jr, "user", S0 + 9)
    assert [e["data"]["kind"] for e in jr.events(kind="INCIDENT")] == [
        "UNPROTECTED_60S", "FOREIGN_ORDERS", "BAD_EQUITY", "BREAKER"]
    jr.close()
