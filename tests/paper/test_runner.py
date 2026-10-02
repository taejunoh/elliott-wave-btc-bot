from __future__ import annotations

import json
import os

import pytest

from ewbot.paper.ledger import LEDGER_FILE, REPORT_FILE, RUNS_LOG, load_ledger
from ewbot.paper.runner import PaperEnv, PaperRunError, run_paper
from ewbot.types import H1
from tests.paper.helpers import (
    BOUNDARY,
    Clock,
    FakeFetch,
    FakeSender,
    commit_lock,
    git,
    make_lock,
    synth_frame,
    ts,
)

FIRST = ts("2024-02-10 12:07")  # 07:07 New York: no daily summary


@pytest.fixture(scope="module")
def df():
    return synth_frame()


@pytest.fixture
def make_env(tmp_path, df):
    def make(lock: dict | None = None, now: int = FIRST, sender: FakeSender | None = None, **kw) -> PaperEnv:
        lock_path = commit_lock(tmp_path / "repo", make_lock(df) if lock is None else lock)
        clock = Clock(now)
        defaults = dict(
            ledger_dir=tmp_path / "paper", lock_path=lock_path, cache_dir=tmp_path / "cache",
            fetch=FakeFetch(df, clock), sender=FakeSender() if sender is None else sender, clock=clock,
            fingerprint=lambda: "fp-base", baseline=lambda: ("fp-base", "c" * 40), random_runs=2, workers=1,
            sleep=lambda s: None,
        )
        defaults.update(kw)
        return PaperEnv(**defaults)

    return make


def test_first_run_writes_ledger_report_log_and_notifies(make_env):
    env = make_env()
    out = run_paper(env)
    led = load_ledger(env.ledger_dir)
    assert sorted(os.listdir(env.ledger_dir)) == [LEDGER_FILE, REPORT_FILE, RUNS_LOG]
    assert [e["id"] for e in led.epochs] == [1] and led.epochs[0]["through_ts"] == ts("2024-02-10 11:00")
    assert [t["entry_ts"] for t in led.trades] == [ts("2024-01-12 15:00"), ts("2024-02-07 21:00")]
    assert all(t["entry_ts"] > BOUNDARY and t["epoch_id"] == 1 for t in led.trades)
    assert led.open_position is None
    assert {d["kind"] for d in led.decisions} == {"SIGNAL", "ENTRY", "TRAIL", "EXIT", "STOP"}
    assert all(d["run_id"] == out.run_id == "20240210T120700Z" and d["epoch_id"] == 1 for d in led.decisions)
    assert min(d["ts"] for d in led.decisions) == ts("2024-01-12 14:00")  # SIGNAL bar of the first paper trade
    run = led.runs[0]
    assert run["status"] == "ok" and run["holdout_data_ok"] is True and run["last_bar_ts"] == ts("2024-02-10 11:00")
    assert run["holdout_trades_recomputed"] == 2 and run["count_total"] == 4  # 2 holdout + 2 paper
    texts = env.sender.texts
    assert sum("신규 진입" in t for t in texts) == 2 and sum("청산" in t for t in texts) == 2
    assert sum("손절 이동 (추적)" in t for t in texts) == 3
    assert len(led.sent_event_ids) == len(texts) == len(set(led.sent_event_ids)) and led.pending_events == []
    assert (env.ledger_dir / RUNS_LOG).read_text().count("\n") == 1
    assert "실거래 전환 판정" in (env.ledger_dir / REPORT_FILE).read_text(encoding="utf-8")
    assert out.status["count"]["total"] == 4 and out.new_decisions == len(led.decisions)


def test_rerun_in_the_same_hour_changes_nothing_but_the_run_log(make_env):
    env = make_env()
    run_paper(env)
    before = load_ledger(env.ledger_dir)
    env.clock.now += 60
    out = run_paper(env)
    after = load_ledger(env.ledger_dir)
    assert out.new_decisions == 0 and out.events == [] and out.new_discrepancies == 0
    assert after.decisions == before.decisions and after.trades == before.trades
    assert after.sent_event_ids == before.sent_event_ids and len(after.runs) == 2


def test_new_bars_add_only_new_decisions_and_events(make_env):
    env = make_env()
    run_paper(env)
    n_texts = len(env.sender.texts)
    env.clock.now = ts("2024-02-16 03:07")  # the 02-16 02:00 short entry has filled; 22:07 New York
    out = run_paper(env)
    led = load_ledger(env.ledger_dir)
    assert out.new_decisions == 2  # its SIGNAL (01:00) and ENTRY (02:00)
    new = env.sender.texts[n_texts:]
    assert len(new) == 3 and "신규 진입" in new[0] and "W3 숏" in new[0]
    assert "실행 공백 135.0시간" in new[1]  # 5.6 days since the previous successful run
    assert "일일 요약 2024-02-15 (뉴욕)" in new[2]  # first run after 09:00 New York on 02-15
    assert led.open_position["entry_ts"] == ts("2024-02-16 02:00") and led.open_position["counted"] is True


def _modify(path):
    data = json.loads(path.read_text())
    data["runs"].append({"note": "edited"})
    path.write_text(json.dumps(data))


@pytest.mark.parametrize("case", ["missing", "untracked", "modified", "staged", "grid", "start", "incomplete"])
def test_precheck_refuses_and_writes_nothing(make_env, df, tmp_path, case):
    lock = make_lock(df)
    if case == "grid":
        lock["params"] = [2.2, 0.5, 0.0]
    if case == "start":
        lock["holdout_start"] = lock["holdout_start"] + H1
    if case == "incomplete":
        del lock["first_run"]
    env = make_env(lock=lock)
    path = env.lock_path
    if case == "missing":
        git(path.parent, "rm", "-q", "holdout.lock")
    elif case == "untracked":
        git(path.parent, "rm", "-q", "--cached", "holdout.lock")
        git(path.parent, "commit", "-q", "-m", "untrack")
    elif case == "modified":
        _modify(path)
    elif case == "staged":
        _modify(path)
        git(path.parent, "add", "holdout.lock")
    with pytest.raises(PaperRunError) as exc:
        run_paper(env)
    assert exc.value.stage == "precheck"
    assert not env.ledger_dir.exists() and env.fetch.calls == 0 and env.sender.texts == []


def test_fetch_is_retried_three_times(make_env, df):
    sleeps: list[float] = []
    env = make_env(sleep=sleeps.append)
    env.fetch.fail = 2
    run_paper(env)
    assert env.fetch.calls == 3 and sleeps == [10.0, 20.0]

    env2 = make_env(sleep=lambda s: None)
    env2.ledger_dir = env2.ledger_dir.parent / "paper2"
    env2.fetch.fail = 3
    with pytest.raises(PaperRunError) as exc:
        run_paper(env2)
    assert exc.value.stage == "data" and env2.fetch.calls == 3
    assert not (env2.ledger_dir / LEDGER_FILE).exists()


def test_ledger_of_another_holdout_is_refused(make_env, df):
    env = make_env()
    run_paper(env)
    other = make_env(lock=make_lock(df, sha256="e" * 64))
    with pytest.raises(PaperRunError, match="different holdout lock"):
        run_paper(other)


def test_corrupt_ledger_stops_at_the_ledger_stage(make_env):
    env = make_env()
    env.ledger_dir.mkdir(parents=True)
    (env.ledger_dir / LEDGER_FILE).write_text("{oops", encoding="utf-8")
    with pytest.raises(PaperRunError) as exc:
        run_paper(env)
    assert exc.value.stage == "ledger"


def test_fingerprint_change_opens_epoch_two_and_resets_the_count(make_env):
    env = make_env()
    run_paper(env)
    env.fingerprint = lambda: "fp-edited"
    env.clock.now += H1
    out = run_paper(env)
    led = load_ledger(env.ledger_dir)
    assert [e["id"] for e in led.epochs] == [1, 2] and led.epochs[1]["started_ts"] == ts("2024-02-10 12:00")
    assert out.status["count"] == {"epoch_id": 2, "eligible": False, "holdout": 0, "paper": 0, "total": 0, "required": 50}
    assert [t["epoch_id"] for t in led.trades] == [1, 1]  # epoch 1 trades kept
    assert "전략 지문 변경" in env.sender.texts[-1] and "구간 2" in env.sender.texts[-1]
    assert led.discrepancies == []  # same code, same data: every recorded decision reproduces


def test_telegram_failure_keeps_events_for_the_next_run(make_env):
    sender = FakeSender(fail=3)
    env = make_env(sender=sender)
    out = run_paper(env)
    led = load_ledger(env.ledger_dir)
    assert out.sent == [] and len(led.pending_events) == len(out.events) > 0
    assert led.runs[-1]["notify"]["pending"] == len(out.events)
    env.clock.now += 60
    out2 = run_paper(env)
    led = load_ledger(env.ledger_dir)
    assert out2.sent == out.events and led.pending_events == []
    assert len(sender.texts) == len(set(sender.texts)) == len(out.events)


def test_without_telegram_secrets_the_run_succeeds_and_sends_nothing(make_env):
    env = make_env(sender=None)
    env.sender = None
    out = run_paper(env)
    led = load_ledger(env.ledger_dir)
    assert "not configured" in out.notes[0] and led.pending_events == [] and led.sent_event_ids == []


def test_gap_over_three_hours_warns(make_env):
    env = make_env()
    run_paper(env)
    env.clock.now += 3 * H1
    run_paper(env)
    assert not any("실행 공백" in t for t in env.sender.texts)
    env.clock.now += 3 * H1 + 60
    run_paper(env)
    assert "실행 공백 3.0시간" in env.sender.texts[-1]


def test_daily_summary_once_after_nine_new_york_with_criteria(make_env):
    env = make_env(now=ts("2024-02-12 14:07"))  # 09:07 EST
    run_paper(env)
    led = load_ledger(env.ledger_dir)
    daily = [t for t in env.sender.texts if "일일 요약 2024-02-12" in t]
    assert len(daily) == 1 and "실거래 전환 판정" in daily[0] and "표본 외 누적 4/50" in daily[0]
    assert led.daily_summary_date == "2024-02-12" and led.criteria["epoch_id"] == 1
    assert led.criteria["random"]["runs"] == 2 and led.criteria["window"][1] == ts("2024-02-12 14:00")
    env.clock.now += H1
    run_paper(env)
    assert sum("일일 요약" in t for t in env.sender.texts) == 1


def test_holdout_data_hash_mismatch_warns_and_continues(make_env, df):
    lock = make_lock(df)
    lock["data"]["sha256"] = "0" * 64
    env = make_env(lock=lock)
    run_paper(env)
    led = load_ledger(env.ledger_dir)
    assert led.runs[-1]["holdout_data_ok"] is False
    assert any("홀드아웃 구간 데이터 해시 불일치" in t for t in env.sender.texts)
    env.clock.now += H1
    run_paper(env)
    assert sum("데이터 해시 불일치" in t for t in env.sender.texts) == 1  # one warning per distinct hash
