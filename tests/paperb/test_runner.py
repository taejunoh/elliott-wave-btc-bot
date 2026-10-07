from __future__ import annotations

import json
from datetime import datetime

import pytest

from ewbot.paper.decisions import known_at
from ewbot.paper.ledger import load_ledger
from ewbot.paper.notify import NY
from ewbot.paperb.notify import HEAD_B
from ewbot.paperb.runner import PaperBRunError, precheck_lock_b, run_paper_b
from ewbot.stratb.holdout import lock_digest_b
from tests.paper.helpers import git
from tests.paperb.helpers import LAST_BAR, FakeFetchB, commit_lock_b, make_env, world

H1 = 3600


def _ny(y, m, d, h, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=NY).timestamp())


def test_the_precheck_needs_the_committed_passed_lock_of_this_code(tmp_path, lock_text):
    with pytest.raises(PaperBRunError, match="missing"):
        precheck_lock_b(tmp_path / "x" / "holdout-b.lock")
    p = commit_lock_b(tmp_path / "a", lock_text)
    assert precheck_lock_b(p)["first_run"]["criteria"]["passed"] is True
    lock = json.loads(lock_text)
    p.write_text(json.dumps({**lock, "note": "edited"}), encoding="utf-8")
    with pytest.raises(PaperBRunError, match="differs from the committed"):
        precheck_lock_b(p)
    (tmp_path / "b").mkdir()
    git(tmp_path / "b", "init", "-q")
    (tmp_path / "b" / "holdout-b.lock").write_text(lock_text, encoding="utf-8")
    with pytest.raises(PaperBRunError, match="not committed"):
        precheck_lock_b(tmp_path / "b" / "holdout-b.lock")
    with pytest.raises(PaperBRunError, match="has not run"):
        precheck_lock_b(commit_lock_b(tmp_path / "c", json.dumps({**lock, "first_run": None})))
    failed = json.loads(lock_text)
    failed["first_run"]["criteria"]["passed"] = False
    with pytest.raises(PaperBRunError, match="strategy B ended"):
        precheck_lock_b(commit_lock_b(tmp_path / "d", json.dumps(failed)))
    other = json.loads(lock_text)
    other["config"]["initial_equity"] = 20_000.0
    other["sha256"] = lock_digest_b(other)
    with pytest.raises(PaperBRunError, match="not this code's config"):
        precheck_lock_b(commit_lock_b(tmp_path / "e", json.dumps(other)))


def test_the_first_run_reproduces_the_holdout_and_writes_only_the_b_ledger(tmp_path, lock_text):
    env, _, _ = make_env(tmp_path, lock_text, LAST_BAR + 900 + H1 + 60)
    out = run_paper_b(env)
    led = load_ledger(env.ledger_dir)
    assert (out.epoch_id, out.last_bar_ts, out.notes) == (1, LAST_BAR + H1, [])
    assert led.runs[0]["holdout_trades_recomputed"] == led.holdout["n_trades"]["B"] == 124
    assert led.runs[0]["warnings"] == [] and all(known_at(d) > LAST_BAR for d in led.decisions)
    assert sorted(p.name for p in env.ledger_dir.iterdir()) == ["ledger.json", "runs.log"]
    assert "paper=0/30" in (env.ledger_dir / "runs.log").read_text()


def test_hourly_runs_count_paper_trades_and_notify_with_the_b_prefix(tmp_path, lock_text):
    env, clock, sender = make_env(tmp_path, lock_text, LAST_BAR + 900 + 60)
    for k in range(25):
        clock.now = LAST_BAR + 900 + 60 + k * 6 * H1
        out = run_paper_b(env)
        assert out.new_discrepancies == 0
    led = load_ledger(env.ledger_dir)
    assert out.count["paper"] >= 4 and all(t["entry_ts"] > LAST_BAR for t in led.trades)
    assert {t["setup"] for t in led.trades} == {"B-BTC", "B-ETH"}
    for asset in ("B-BTC 롱", "B-ETH 숏"):
        assert any(x.startswith(f"{HEAD_B} 신규 진입") and asset in x for x in sender.texts)
        assert any(x.startswith(f"{HEAD_B} 청산") and asset in x for x in sender.texts)
    assert all(x.startswith(HEAD_B) for x in sender.texts) and led.discrepancies == []
    fresh, _, _ = make_env(tmp_path / "fresh", lock_text, clock.now)
    run_paper_b(fresh)
    assert {d["id"] for d in load_ledger(fresh.ledger_dir).decisions} == {d["id"] for d in led.decisions}


def test_a_run_ends_at_the_last_bar_both_assets_have(tmp_path, lock_text):
    now = LAST_BAR + 900 + 6 * H1 + 60
    newest = now // 900 * 900 - 900  # the newest closed bar (hh:45)
    full = world()
    late = {"BTC": full["BTC"], "ETH": full["ETH"][full["ETH"]["ts"] < newest]}  # ETH's newest bar is not out yet
    env, clock, _ = make_env(tmp_path, lock_text, now, source=late)
    out = run_paper_b(env)
    led = load_ledger(env.ledger_dir)
    assert out.last_bar_ts == led.runs[0]["last_bar_ts"] == newest - 900  # BTC's newest bar waits for ETH's
    assert all(d["ts"] <= newest - 900 for d in led.decisions) and "ETH" in out.notes[0]
    env.fetch = FakeFetchB(full, clock)
    clock.now += H1
    out = run_paper_b(env)
    assert out.last_bar_ts == newest + H1 and out.notes == [] and out.new_discrepancies == 0


def test_a_changed_recorded_decision_is_an_unexplained_discrepancy(tmp_path, lock_text):
    env, clock, sender = make_env(tmp_path, lock_text, LAST_BAR + 900 + 3 * 86_400)
    run_paper_b(env)
    raw = json.loads((env.ledger_dir / "ledger.json").read_text())
    assert raw["decisions"]
    raw["decisions"][0]["price"] += 1.0
    (env.ledger_dir / "ledger.json").write_text(json.dumps(raw))
    clock.now += H1
    out = run_paper_b(env)
    (disc,) = load_ledger(env.ledger_dir).discrepancies
    assert out.new_discrepancies == 1 and (disc["type"], disc["cause"]) == ("CHANGED", "UNEXPLAINED")
    assert sender.texts[-1].startswith(f"{HEAD_B} 경고: 결정 불일치 1건")


def test_a_new_b_fingerprint_opens_a_new_epoch_and_resets_the_count(tmp_path, lock_text):
    env, clock, sender = make_env(tmp_path, lock_text, LAST_BAR + 900 + 4 * 86_400)
    first = run_paper_b(env)
    assert first.count["paper"] >= 1
    env.fingerprint = lambda: "9" * 64
    clock.now += H1
    out = run_paper_b(env)
    assert out.epoch_id == 2 and out.count == {"epoch_id": 2, "eligible": False, "paper": 0, "required": 30}
    assert any(x.startswith(f"{HEAD_B} 경고: 전략 지문 변경 · 구간 2") for x in sender.texts)


def test_revised_holdout_bars_warn_once_and_changed_decisions_are_data_revisions(tmp_path, lock_text):
    env, clock, sender = make_env(tmp_path, lock_text, LAST_BAR + 900 + 3 * 86_400)
    run_paper_b(env)
    raw = json.loads((env.ledger_dir / "ledger.json").read_text())
    raw["decisions"][0]["price"] += 1.0  # a recorded decision that will not reappear unchanged
    (env.ledger_dir / "ledger.json").write_text(json.dumps(raw))
    dfs = world()
    eth = dfs["ETH"]
    eth.loc[(eth["ts"] - LAST_BAR).abs().idxmin() - 500, "close"] += 0.5  # an ETH bar inside the holdout window
    env.fetch = FakeFetchB(dfs, clock)
    for _ in range(2):
        clock.now += H1
        out = run_paper_b(env)
    led = load_ledger(env.ledger_dir)
    assert [r["holdout_data_ok"] for r in led.runs] == [True, False, False]
    assert out.notes and out.notes[0].startswith("holdout data hash ")
    assert raw["decisions"][0]["id"] in {d["decision_id"] for d in led.discrepancies}
    assert {d["cause"] for d in led.discrepancies} == {"DATA_REVISION"}
    warned = [x for x in sender.texts if "홀드아웃 구간 데이터 해시 불일치" in x]
    assert len(warned) == 1 and warned[0].startswith(f"{HEAD_B} 경고")


def test_candle_updates_are_retried_three_times(tmp_path, lock_text):
    env, clock, _ = make_env(tmp_path, lock_text, LAST_BAR + 900 + 60)
    env.fetch = FakeFetchB(world(), clock, fail=2)
    run_paper_b(env)
    env.fetch = FakeFetchB(world(), clock, fail=3)
    with pytest.raises(PaperBRunError) as exc:
        run_paper_b(env)
    assert exc.value.stage == "data" and env.fetch.calls == 3


def test_one_daily_summary_per_new_york_date_from_9_am(tmp_path, lock_text):
    env, clock, sender = make_env(tmp_path, lock_text, _ny(2024, 6, 3, 8, 30))
    run_paper_b(env)
    assert not any("일일 요약" in x for x in sender.texts)
    clock.now = _ny(2024, 6, 3, 9, 5)
    run_paper_b(env)
    (daily,) = [x for x in sender.texts if "일일 요약" in x]
    assert daily.startswith(f"{HEAD_B} 일일 요약 2024-06-03 (뉴욕)") and "모의 " in daily and "/30" in daily
    assert "1. 홀드아웃 합격: 충족" in daily and "3. 원인 불명 불일치 0건" in daily
    clock.now = _ny(2024, 6, 3, 10, 5)
    run_paper_b(env)
    assert len([x for x in sender.texts if "일일 요약" in x]) == 1
