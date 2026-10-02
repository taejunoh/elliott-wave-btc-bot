"""Plan 2 spec 7.2: the hourly runtime replayed on synthetic candles with a moving clock.

The window covers the full life of a C_END long (signal 2024-03-13 22:00, entry 23:00, trailing stop
03-14 07:00, TP1 + breakeven 03-14 23:00, TP2 03-16 18:00) after a catch-up of the earlier paper
trades. Every run is the real `run_paper` (recompute, ledger, report, Telegram via a fake sender)."""

from __future__ import annotations

import pytest

from ewbot.paper.ledger import load_ledger
from ewbot.paper.runner import PaperEnv, run_paper
from ewbot.types import H1
from tests.paper.helpers import Clock, FakeFetch, FakeSender, commit_lock, make_lock, synth_frame, ts

START = ts("2024-03-13 21:07")
HOURLY = [START + k * H1 for k in range(28)]  # 03-13 21:07 .. 03-15 00:07, one run per hour
FINAL = ts("2024-03-16 19:07")
SPARSE = [HOURLY[0], HOURLY[5], HOURLY[6], HOURLY[13], HOURLY[27]]  # most runs skipped


def _walk(base, name: str, times: list[int], source=None) -> tuple[PaperEnv, FakeSender, list]:
    df = synth_frame()
    lock_path = commit_lock(base / "repo", make_lock(df))
    clock = Clock(times[0])
    sender = FakeSender()
    env = PaperEnv(
        ledger_dir=base / name, lock_path=lock_path, cache_dir=base / f"{name}-cache",
        fetch=FakeFetch(df if source is None else source, clock), sender=sender, clock=clock,
        fingerprint=lambda: "fp", baseline=lambda: ("fp", "c" * 40), random_runs=2, workers=1, sleep=lambda s: None,
    )
    outs = []
    for t in times:
        clock.now = t
        outs.append(run_paper(env))
    return env, sender, outs


def _core(decisions: list[dict]) -> list[dict]:
    return [{k: v for k, v in d.items() if k != "run_id"} for d in decisions]


@pytest.fixture(scope="module")
def walks(tmp_path_factory):
    base = tmp_path_factory.mktemp("walk")
    hourly = _walk(base, "hourly", HOURLY + [FINAL])
    sparse = _walk(base, "sparse", SPARSE + [FINAL])
    oneshot = _walk(base, "oneshot", [FINAL])
    return {k: (v[0], v[1], v[2], load_ledger(v[0].ledger_dir)) for k, v in
            {"hourly": hourly, "sparse": sparse, "oneshot": oneshot}.items()}


def test_final_ledger_equals_a_single_run_from_scratch(walks):
    led, one = walks["hourly"][3], walks["oneshot"][3]
    assert led.trades == one.trades and _core(led.decisions) == _core(one.decisions)
    assert led.open_position == one.open_position is None
    assert led.discrepancies == one.discrepancies == []
    kinds = {d["kind"] for d in led.decisions if d["trade_key"] == ts("2024-03-13 23:00")}
    assert kinds == {"ENTRY", "TRAIL", "TP1", "BREAKEVEN", "TP2"}


def test_skipped_runs_do_not_change_the_final_ledger(walks):
    led, one = walks["sparse"][3], walks["oneshot"][3]
    assert led.trades == one.trades and _core(led.decisions) == _core(one.decisions)
    assert len(led.runs) == len(SPARSE) + 1


def test_decisions_were_recorded_by_the_first_run_that_could_know_them(walks):
    env, _, outs, led = walks["hourly"]
    by_run = {o.run_id: o.last_bar_ts for o in outs}
    for d in led.decisions:
        known = d["ts"] + (H1 if d["kind"] == "SIGNAL" else 0)
        recorded_at = by_run[d["run_id"]]
        assert recorded_at >= known
        earlier = [b for b in by_run.values() if b < recorded_at]
        assert not earlier or max(earlier) < known  # no earlier run already knew it


def _expected_event_ids(led) -> set[str]:
    trades = {t["entry_ts"]: t for t in led.trades}
    ids = set()
    for d in led.decisions:
        if d["kind"] == "ENTRY" or d["kind"] in ("BREAKEVEN", "TRAIL"):
            ids.add(d["id"])
        elif d["kind"] in ("STOP", "TP1", "TP2", "EXIT"):
            t = trades.get(d["trade_key"])
            if t is not None and t["exit_ts"] == d["ts"]:
                ids.add(f"CLOSE:{d['ts']}:{d['trade_key']}")
    return ids


@pytest.mark.parametrize("name", ["hourly", "sparse", "oneshot"])
def test_every_event_is_notified_exactly_once(walks, name):
    _, sender, _, led = walks[name]
    sent = led.sent_event_ids
    assert len(sent) == len(set(sent)) == len(sender.texts) and led.pending_events == []
    decision_ids = {i for i in sent if i.split(":")[0] in ("ENTRY", "BREAKEVEN", "TRAIL", "CLOSE")}
    assert decision_ids == _expected_event_ids(led)
    assert sum(i.startswith("DAILY:") for i in sent) == len({i for i in sent if i.startswith("DAILY:")})


def test_hourly_walk_notified_each_step_of_the_trade_when_it_happened(walks):
    _, _, outs, _ = walks["hourly"]
    key = ts("2024-03-13 23:00")
    at = {o.last_bar_ts: o.events for o in outs}  # a run at hh:07 has the bar of hh-1 as its last bar
    assert f"ENTRY:{key}:{key}" in at[key]  # the 03-14 00:07 run saw the 23:00 fill
    assert f"TRAIL:{ts('2024-03-14 07:00')}:{key}" in at[ts("2024-03-14 07:00")]
    assert f"BREAKEVEN:{ts('2024-03-14 23:00')}:{key}" in at[ts("2024-03-14 23:00")]
    assert f"CLOSE:{ts('2024-03-16 18:00')}:{key}" in at[ts("2024-03-16 18:00")]
    assert not any(e.startswith("ENTRY:") for o in outs[1:-1] for e in o.events if o.last_bar_ts != key)


def test_revised_past_bar_is_flagged_as_data_revision(tmp_path):
    df = synth_frame()
    revised = df.copy()
    i = int(revised.index[revised["ts"] == ts("2024-03-13 23:00")][0])
    revised.loc[i, "open"] += 7.0  # the C_END entry bar, as a re-download after cache loss might return it
    revised.loc[i, "high"] = max(revised.loc[i, "high"], revised.loc[i, "open"])
    clock_box: dict[str, int] = {}

    def source():
        return revised if clock_box["now"] >= ts("2024-03-14 03:00") else df

    times = [ts("2024-03-14 01:07"), ts("2024-03-14 02:07"), ts("2024-03-14 03:07"), ts("2024-03-14 04:07")]
    lock_path = commit_lock(tmp_path / "repo", make_lock(df))
    clock = Clock(times[0])
    sender = FakeSender()
    env = PaperEnv(
        ledger_dir=tmp_path / "paper", lock_path=lock_path, cache_dir=tmp_path / "cache",
        fetch=FakeFetch(source, clock), sender=sender, clock=clock, fingerprint=lambda: "fp",
        baseline=lambda: ("fp", "c" * 40), random_runs=2, workers=1, sleep=lambda s: None,
    )
    outs = []
    for t in times:
        clock.now = clock_box["now"] = t
        outs.append(run_paper(env))
    led = load_ledger(env.ledger_dir)
    assert [o.new_discrepancies for o in outs][:2] == [0, 0] and outs[2].new_discrepancies > 0
    assert outs[3].new_discrepancies == 0  # already recorded: no repeated warning
    assert led.discrepancies and all(d["cause"] == "DATA_REVISION" for d in led.discrepancies)
    key = ts("2024-03-13 23:00")
    assert f"CHANGED:ENTRY:{key}:{key}" in {d["id"] for d in led.discrepancies}
    warnings = [t for t in sender.texts if "결정 불일치" in t]
    assert len(warnings) == 1 and "DATA_REVISION" in warnings[0]
    recorded = next(d for d in led.decisions if d["id"] == f"ENTRY:{key}:{key}")
    assert led.open_position["entry_ts"] == key  # still open at 03-14 04:00
    assert led.open_position["entry_price"] != recorded["price"]  # state follows the recompute; the record stays
