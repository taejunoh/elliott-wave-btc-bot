from __future__ import annotations

import json
import os
import shutil

import pytest

from ewbot.aeth.fingerprint import aeth_fingerprint, current_epoch_fingerprint
from ewbot.aeth.paper import (
    HEAD_AETH,
    AethPaperEnv,
    AethPaperError,
    eth_head,
    holdout_summary_aeth,
    run_paper_aeth,
    status_aeth,
    status_lines_aeth,
)
from ewbot.paper.environment import environment_digest
from ewbot.paper.fingerprint import PROJECT_ROOT
from ewbot.paper.ledger import LEDGER_FILE, RUNS_LOG, load_ledger
from ewbot.types import H1
from tests.aeth.helpers import FakeCacheFetch, commit_file, eth_frame, write_a_lock
from tests.aeth.records import aeth_lock
from tests.paper.helpers import BOUNDARY, Clock, FakeSender, git, ts

FIRST = ts("2024-02-10 12:07")  # 07:07 New York: no daily summary
ENV = {"python": "3.12.13", "numpy": "2.1.0", "pandas": "2.2.3", "pyarrow": "18.0.0", "uv_lock_sha256": "a" * 64,
       "image_os": "ubuntu24", "image_version": "20261001.1.0"}


@pytest.fixture(scope="module")
def df():
    return eth_frame()


@pytest.fixture
def make_env(tmp_path, df):
    a_lock = write_a_lock(tmp_path / "a")

    def make(lock: dict | None = None, now: int = FIRST, **kw) -> AethPaperEnv:
        text = json.dumps(aeth_lock(df, a_lock) if lock is None else lock, indent=2)
        lock_path = commit_file(tmp_path / "repo", "holdout-aeth.lock", text)
        clock = Clock(now)
        defaults = dict(ledger_dir=tmp_path / "paper-aeth", lock_path=lock_path, a_lock_path=a_lock,
                        cache_dir=tmp_path / "cache", fetch=FakeCacheFetch(df, clock), sender=FakeSender(), clock=clock,
                        sleep=lambda s: None, environment=lambda: dict(ENV))
        defaults.update(kw)
        return AethPaperEnv(**defaults)

    make.a_lock = a_lock
    return make


def test_first_run_reproduces_the_holdout_and_records_the_eth_paper_trades(make_env):
    env = make_env()
    out = run_paper_aeth(env)
    led = load_ledger(env.ledger_dir)
    assert sorted(os.listdir(env.ledger_dir)) == [LEDGER_FILE, RUNS_LOG]  # no report.html
    assert [e["id"] for e in led.epochs] == [1] and led.epochs[0]["started_ts"] == BOUNDARY
    assert led.baseline["fingerprint"] == current_epoch_fingerprint()  # A's + A-ETH decision at the lock = now
    assert [t["entry_ts"] for t in led.trades] == [ts("2024-01-12 15:00"), ts("2024-02-07 21:00")]
    run = led.runs[0]
    assert run["holdout_trades_recomputed"] == 2 and run["holdout_data_ok"] is True and run["warnings"] == []
    assert run["count_total"] == 4 and out.status["count"]["required"] == 50  # 2 holdout + 2 paper, of 50
    assert run["env_sha256"] == environment_digest(ENV) and all(d["epoch_id"] == 1 for d in led.decisions)
    texts = env.sender.texts
    assert texts and all(t.startswith(HEAD_AETH) for t in texts)
    assert not any(t.startswith("[ewbot paper]") for t in texts)
    assert sum("신규 진입" in t for t in texts) == 2 and sum("청산" in t for t in texts) == 2
    assert led.holdout == holdout_summary_aeth(json.loads(env.lock_path.read_text(encoding="utf-8")))
    assert (env.ledger_dir / RUNS_LOG).read_text().count("\n") == 1


def test_a_rerun_in_the_same_hour_adds_nothing_and_new_bars_add_only_their_decisions(make_env):
    env = make_env()
    run_paper_aeth(env)
    before = load_ledger(env.ledger_dir)
    env.clock.now += 60
    out = run_paper_aeth(env)
    assert out.new_decisions == 0 and out.events == [] and load_ledger(env.ledger_dir).decisions == before.decisions
    n = len(env.sender.texts)
    env.clock.now = ts("2024-02-16 03:07")  # the 02-16 02:00 short has filled; 22:07 New York
    out = run_paper_aeth(env)
    new = env.sender.texts[n:]
    assert out.new_decisions == 2 and "신규 진입" in new[0] and "W3 숏" in new[0]
    assert "실행 공백" in new[1]
    assert "일일 요약 2024-02-15 (뉴욕)" in new[2] and "ETH 표본 외 누적 4/50" in new[2]
    assert "A-ETH 실거래 검토 조건" in new[2] and "A-BTC CANARY" in new[2]


def test_a_changed_strategy_a_or_aeth_decision_fingerprint_opens_a_new_epoch_and_restarts_the_count(make_env):
    env = make_env()
    run_paper_aeth(env)
    env.fingerprint = lambda: "edited"
    env.clock.now += H1
    out = run_paper_aeth(env)
    led = load_ledger(env.ledger_dir)
    assert [e["id"] for e in led.epochs] == [1, 2]
    assert out.status["count"] == {"epoch_id": 2, "eligible": False, "holdout": 0, "paper": 0, "total": 0,
                                   "required": 50}
    assert out.status["auto_met"] is False and "판정 대상 아님" in status_lines_aeth(out.status)[1]
    assert env.sender.texts[-1].startswith(HEAD_AETH) and "구간 2" in env.sender.texts[-1]


def test_a_runtime_fix_during_paper_trading_keeps_the_epoch_and_a_config_edit_opens_one(make_env, tmp_path):
    """Spec 8 (amended 2026-10-08): the epoch is bound to strategy A's fingerprint and the A-ETH decision
    fingerprint (aeth/config.py, aeth/data.py) - a fix to paper.py or sync.py while paper trading keeps the count."""
    root = tmp_path / "tree"
    shutil.copytree(PROJECT_ROOT / "src" / "ewbot", root / "src" / "ewbot")
    aeth = root / "src" / "ewbot" / "aeth"
    env = make_env(fingerprint=lambda: current_epoch_fingerprint(root))
    run_paper_aeth(env)
    for name in ("paper.py", "sync.py"):  # sync.py is created while the plan has not added it yet (Task 7)
        f = aeth / name
        f.write_bytes((f.read_bytes() if f.exists() else b"") + b"# a runtime fix: messages, retries\n")
    assert aeth_fingerprint(root) != aeth_fingerprint()  # the lock's A-ETH fingerprint no longer holds
    env.clock.now += H1
    out = run_paper_aeth(env)
    assert [e["id"] for e in load_ledger(env.ledger_dir).epochs] == [1]
    assert out.status["eligible"] is True and out.status["count"]["total"] == 4
    (aeth / "config.py").write_bytes((aeth / "config.py").read_bytes() + b"# a decision file edit\n")
    env.clock.now += H1
    out = run_paper_aeth(env)
    assert [e["id"] for e in load_ledger(env.ledger_dir).epochs] == [1, 2]
    assert out.status["eligible"] is False and out.status["count"]["total"] == 0


@pytest.mark.parametrize("case, why", [
    ("missing", "not a valid A-ETH lock"), ("uncommitted", "differs from the committed"),
    ("modified", "differs from the committed"), ("not_run", "has not run"), ("failed", "did not pass"),
    ("a_params", "a_lock_sha256"), ("config", "config"),
])
def test_the_precheck_refuses_and_writes_nothing(make_env, df, tmp_path, case, why):
    lock = aeth_lock(df, make_env.a_lock, passed=case != "failed")
    if case == "not_run":
        lock["first_run"] = None
    if case == "config":
        lock = aeth_lock(df, make_env.a_lock, config={"costs": {"contract_size": 0.01}})
    kw = {"a_lock_path": write_a_lock(tmp_path / "other", random_runs=3)} if case == "a_params" else {}
    env = make_env(lock=lock, **kw)
    if case == "missing":
        git(env.lock_path.parent, "rm", "-q", env.lock_path.name)
    if case == "uncommitted":
        git(env.lock_path.parent, "rm", "-q", "--cached", env.lock_path.name)
        git(env.lock_path.parent, "commit", "-q", "-m", "untrack")
    if case == "modified":
        data = json.loads(env.lock_path.read_text(encoding="utf-8"))
        data["attempts"].append({"note": "edited"})
        env.lock_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AethPaperError, match=why) as exc:
        run_paper_aeth(env)
    assert exc.value.stage == "precheck"
    assert not env.ledger_dir.exists() and env.fetch.calls == 0 and env.sender.texts == []


def test_holdout_drift_warns_once_and_never_stops_the_run(make_env, df):
    lock = aeth_lock(df, make_env.a_lock, n_trades=3)
    lock["first_run"]["data"]["sha256"] = "0" * 64  # first_run is not bound by the digest
    env = make_env(lock=lock)
    out = run_paper_aeth(env)
    run = load_ledger(env.ledger_dir).runs[-1]
    assert run["status"] == "ok" and run["holdout_data_ok"] is False and run["holdout_trades_recomputed"] == 2
    assert out.notes[0].endswith("!= lock 000000000000") and out.notes[1] == "holdout trades recomputed 2 != lock 3"
    env.clock.now += H1
    run_paper_aeth(env)
    texts = env.sender.texts
    assert sum("데이터 해시 불일치" in t for t in texts) == 1 and sum("홀드아웃 재현 불일치" in t for t in texts) == 1


def test_fetch_is_retried_three_times_then_the_run_fails_at_data(make_env):
    sleeps: list[float] = []
    env = make_env(sleep=sleeps.append)
    env.fetch.fail = 2
    run_paper_aeth(env)
    assert env.fetch.calls == 3 and sleeps == [10.0, 20.0]
    env2 = make_env(ledger_dir=env.ledger_dir.parent / "other-ledger")
    env2.fetch.fail = 3
    with pytest.raises(AethPaperError) as exc:
        run_paper_aeth(env2)
    assert exc.value.stage == "data" and not (env2.ledger_dir / LEDGER_FILE).exists()


def test_a_ledger_of_another_lock_is_refused(make_env, df):
    env = make_env()
    run_paper_aeth(env)
    other = make_env(lock=aeth_lock(df, make_env.a_lock, n_trades=3))
    with pytest.raises(AethPaperError, match="another holdout-aeth.lock .*n_trades") as exc:
        run_paper_aeth(other)
    assert exc.value.stage == "precheck"


def test_status_needs_the_passed_holdout_fifty_trades_and_criterion_4(make_env):
    env = make_env()
    run_paper_aeth(env)
    led = load_ledger(env.ledger_dir)
    st = status_aeth(led, env.clock.now)
    assert st["holdout_passed"] and not st["count_met"] and not st["c4"]["met"] and not st["auto_met"]
    lines = status_lines_aeth(st)
    assert lines[1] == "1. 단계 2 홀드아웃 합격 (A의 9.6절 1·2번): 충족"
    assert lines[2] == "2. ETH 표본 외 거래 4/50 (홀드아웃 2 + 모의 2): 미충족"
    assert lines[-1] == "=> Plan 5c 검토 불가"


def test_eth_head_replaces_only_strategy_a_s_paper_prefix():
    assert eth_head("[ewbot paper] 신규 진입") == f"{HEAD_AETH} 신규 진입"
    with pytest.raises(ValueError):
        eth_head("[ewbot paper-B] 신규 진입")
