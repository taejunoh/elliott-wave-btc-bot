from __future__ import annotations

import pytest

from ewbot.backtest.holdout import data_fingerprint, holdout_config
from ewbot.backtest.runner import run_backtest
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.config import BotConfig
from ewbot.engine.loop import Trade
from ewbot.paper.decisions import (
    build_decisions,
    classify,
    decision_id,
    decisions_to_record,
    known_at,
    open_position_record,
    paper_trades,
    same_decision,
    split_result,
    top_count_summary,
    verify_decisions,
)
from ewbot.paper.trace import traced_engine
from ewbot.types import H1
from tests.paper.helpers import BOUNDARY, PARAMS, synth_frame, ts

CFG = holdout_config(BotConfig(), PARAMS)


def _traced(df):
    with traced_engine() as trace:
        res = run_backtest(CFG, df, HOLDOUT_START, None, allow_holdout=True)
    closed, open_trade = split_result(res, trace)
    trades = closed + ([open_trade] if open_trade is not None else [])
    return res, trace, closed, open_trade, build_decisions(res, trace, trades)


@pytest.fixture(scope="module")
def full():
    return _traced(synth_frame())


@pytest.fixture(scope="module")
def cut():
    df = synth_frame()
    return _traced(df[df["ts"] <= ts("2024-03-15 06:00")])


def _t(entry_ts: int, exit_ts: int) -> Trade:
    return Trade(entry_ts, exit_ts, "W3", 1, 1, 100.0, 101.0, 95.0, 105.0, None, 1.0, 0.0, 0.0, 0.2, "STOP", {})


def test_out_of_sample_boundary():
    trades = [_t(BOUNDARY - H1, BOUNDARY + 5 * H1), _t(BOUNDARY, BOUNDARY + 2 * H1), _t(BOUNDARY + H1, BOUNDARY + 3 * H1)]
    # open at the boundary (entered before) and entered exactly on the boundary bar: holdout trades
    assert paper_trades(trades, BOUNDARY) == [trades[2]]


def test_split_result_separates_the_position_open_at_the_last_bar(full, cut):
    _, _, closed, open_trade, _ = full
    assert open_trade is None and all(t.exit_reason != "END" for t in closed)
    res, _, closed, open_trade, _ = cut
    assert open_trade is res.trades[-1] and open_trade.exit_reason == "END"
    assert closed == list(res.trades[:-1])


def test_decision_kinds_ids_and_fields(full):
    res, trace, closed, _, decs = full
    ids = [d["id"] for d in decs]
    assert len(ids) == len(set(ids))
    assert {d["kind"] for d in decs} == {"SIGNAL", "ENTRY", "TP1", "TP2", "STOP", "EXIT", "BREAKEVEN", "TRAIL"}
    assert [d["ts"] for d in decs] == sorted(d["ts"] for d in decs)
    by_entry = {t.entry_ts: t for t in closed}
    for d in decs:
        assert d["id"] == decision_id(d["kind"], d["ts"], d["trade_key"])
        if d["kind"] == "SIGNAL":
            entry = next(x for x in decs if x["kind"] == "ENTRY" and x["ts"] == d["ts"] + H1)
            assert d["contracts"] == entry["contracts"] and d["trade_key"] == d["ts"]
            assert len(d["levels"]) == 3
        elif d["kind"] == "ENTRY":
            assert d["reason"] == by_entry[d["trade_key"]].setup and d["price"] == by_entry[d["ts"]].entry_price
        elif d["kind"] == "EXIT":
            assert d["reason"] == by_entry[d["trade_key"]].exit_reason
    be = next(d for d in decs if d["kind"] == "BREAKEVEN")
    t = by_entry[be["trade_key"]]
    tp1 = next(d for d in decs if d["kind"] == "TP1" and d["trade_key"] == t.entry_ts)
    assert be["price"] == t.entry_price and be["contracts"] == t.contracts - tp1["contracts"]
    assert be["ts"] == tp1["ts"] and be["levels"][0] != be["price"]


def test_known_at_signals_lag_one_bar():
    assert known_at({"kind": "SIGNAL", "ts": 100 * H1}) == 101 * H1
    for kind in ("ENTRY", "TP1", "TP2", "STOP", "EXIT", "BREAKEVEN", "TRAIL"):
        assert known_at({"kind": kind, "ts": 100 * H1}) == 100 * H1


def test_a_prefix_run_knows_exactly_the_decisions_up_to_its_last_bar(full, cut):
    """Determinism of the recompute: the run cut at T knows every decision of the full run with
    known_at <= T, and nothing else (the END close is not a decision)."""
    full_decs, cut_decs = full[4], cut[4]
    last = ts("2024-03-15 06:00")
    expected = [d for d in full_decs if known_at(d) <= last]
    assert [d["id"] for d in cut_decs] == [d["id"] for d in expected]
    assert all(same_decision(a, b) for a, b in zip(cut_decs, expected, strict=True))


def test_same_decision_tolerance():
    a = {"kind": "ENTRY", "ts": 1, "trade_key": 1, "contracts": 2, "reason": "W3", "setup": "W3", "direction": 1,
         "price": 20_000.0, "levels": [None, 1.0]}
    assert same_decision(a, {**a, "price": 20_000.0 * (1 + 5e-10)})
    assert not same_decision(a, {**a, "price": 20_000.0 * (1 + 5e-9)})
    assert not same_decision(a, {**a, "contracts": 3})
    assert not same_decision(a, {**a, "levels": [1.0, 1.0]})
    assert not same_decision(a, {**a, "reason": "W5"})


def test_classify_order():
    rec = {"epoch_id": 1}
    assert classify(rec, True, 2) == "DATA_REVISION"
    assert classify(rec, True, 2, env_changed=True) == "DATA_REVISION"
    assert classify(rec, False, 2) == "FINGERPRINT_CHANGE"
    assert classify(rec, False, 2, env_changed=True) == "FINGERPRINT_CHANGE"
    assert classify(rec, False, 1, env_changed=True) == "ENVIRONMENT_CHANGE"
    assert classify(None, False, 2, env_changed=True) == "ENVIRONMENT_CHANGE"
    assert classify(rec, False, 1) == "UNEXPLAINED"
    assert classify(None, False, 2) == "UNEXPLAINED"


def _record(decs, df, epoch_id=1, env_sha256="env-a"):
    return [{**d, "run_id": "r1", "epoch_id": epoch_id, "data_sha256": data_fingerprint(df, 0, d["ts"]),
             "env_sha256": env_sha256} for d in decs]


def test_replay_check_classifies_a_revised_bar_as_data_revision(full):
    df = synth_frame()
    decs = full[4]
    entry = next(d for d in decs if d["kind"] == "ENTRY" and d["ts"] > BOUNDARY)
    recorded = _record([d for d in decs if d["ts"] > BOUNDARY], df)
    epoch = {"id": 1, "started_ts": BOUNDARY, "through_ts": int(df["ts"].iloc[-1])}
    revised = df.copy()
    i = int(revised.index[revised["ts"] == entry["ts"]][0])
    revised.loc[i, "open"] += 7.0  # one past bar changes: the entry fills at a different open
    revised.loc[i, "high"] = max(revised.loc[i, "high"], revised.loc[i, "open"])
    recomputed = _traced(revised)[4]

    def revised_at(d):
        return data_fingerprint(revised, 0, d["ts"]) != d["data_sha256"]

    found = verify_decisions(recorded, recomputed, epoch, revised_at, lambda r: False)
    hit = next(f for f in found if f["decision_id"] == entry["id"])
    assert hit["type"] == "CHANGED" and hit["cause"] == "DATA_REVISION"
    assert all(f["cause"] == "DATA_REVISION" for f in found)
    assert all(f["recorded"]["ts"] >= entry["ts"] for f in found if f["recorded"])  # nothing before the bar


def test_replay_check_fingerprint_change_and_unexplained(full):
    df = synth_frame()
    decs = [d for d in full[4] if d["ts"] > BOUNDARY]
    epoch2 = {"id": 2, "started_ts": int(df["ts"].iloc[-1]), "through_ts": int(df["ts"].iloc[-1])}
    tampered = _record(decs, df, epoch_id=1)
    tampered[0] = {**tampered[0], "price": tampered[0]["price"] + 1.0}
    found = verify_decisions(tampered, full[4], epoch2, lambda d: False, lambda r: False)
    assert [(f["type"], f["cause"]) for f in found] == [("CHANGED", "FINGERPRINT_CHANGE")]

    epoch1 = {"id": 1, "started_ts": BOUNDARY, "through_ts": int(df["ts"].iloc[-1])}
    found = verify_decisions(tampered, full[4], epoch1, lambda d: False, lambda r: False)
    assert [(f["type"], f["cause"]) for f in found] == [("CHANGED", "UNEXPLAINED")]

    gone = _record(decs, df) + [{**_record(decs[:1], df)[0], "id": "ENTRY:1:1", "ts": 1, "trade_key": 1}]
    found = verify_decisions(gone, full[4], epoch1, lambda d: False, lambda r: False)
    assert [(f["type"], f["decision_id"], f["cause"]) for f in found] == [("VANISHED", "ENTRY:1:1", "UNEXPLAINED")]


def test_replay_check_environment_change_against_the_recording_run(full):
    df = synth_frame()
    decs = [d for d in full[4] if known_at(d) > BOUNDARY]
    epoch1 = {"id": 1, "started_ts": BOUNDARY, "through_ts": int(df["ts"].iloc[-1])}
    tampered = _record(decs[1:], df)  # recorded under environment "env-a"; decs[0] was never recorded
    tampered[0] = {**tampered[0], "price": tampered[0]["price"] + 1.0}

    def check(current, appeared_env):  # the previous successful run ran in "env-a"
        found = verify_decisions(tampered, full[4], epoch1, lambda d: False, lambda r: False,
                                 env_sha256=current, appeared_env=appeared_env, previous_env_sha256="env-a")
        return [(f["type"], f["cause"]) for f in found]

    assert check("env-b", lambda r: "env-a") == [("CHANGED", "ENVIRONMENT_CHANGE"), ("APPEARED", "ENVIRONMENT_CHANGE")]
    assert check("env-a", lambda r: "env-a") == [("CHANGED", "UNEXPLAINED"), ("APPEARED", "UNEXPLAINED")]
    # unknown recording environment (no run should have known it yet / not recorded): not an environment change
    assert check("env-b", lambda r: None) == [("CHANGED", "ENVIRONMENT_CHANGE"), ("APPEARED", "UNEXPLAINED")]
    unstamped = [{k: v for k, v in d.items() if k != "env_sha256"} for d in tampered]
    found = verify_decisions(unstamped, full[4], epoch1, lambda d: False, lambda r: False, env_sha256="env-b",
                             previous_env_sha256="env-a")
    assert [(f["type"], f["cause"]) for f in found] == [("CHANGED", "UNEXPLAINED"), ("APPEARED", "UNEXPLAINED")]


def test_environment_change_explains_only_the_first_run_in_the_new_environment(full):
    df = synth_frame()
    decs = [d for d in full[4] if known_at(d) > BOUNDARY]
    epoch1 = {"id": 1, "started_ts": BOUNDARY, "through_ts": int(df["ts"].iloc[-1])}
    tampered = _record(decs[1:], df)  # recorded under "env-a"; decs[0] was never recorded (APPEARED)
    tampered[0] = {**tampered[0], "price": tampered[0]["price"] + 1.0}  # CHANGED
    tampered.append({**tampered[1], "id": "ENTRY:1:1", "ts": 1, "trade_key": 1})  # VANISHED

    def check(previous):  # the current run is in "env-b"
        found = verify_decisions(tampered, full[4], epoch1, lambda d: False, lambda r: False, env_sha256="env-b",
                                 appeared_env=lambda r: "env-a", previous_env_sha256=previous)
        return [(f["type"], f["cause"]) for f in found]

    types = ["CHANGED", "VANISHED", "APPEARED"]
    assert check("env-a") == [(t, "ENVIRONMENT_CHANGE") for t in types]  # the first run in env-b
    assert check("env-b") == [(t, "UNEXPLAINED") for t in types]  # an env-b run already reproduced them
    assert check("env-c") == [(t, "ENVIRONMENT_CHANGE") for t in types]  # the previous run had yet another one
    assert check(None) == [(t, "UNEXPLAINED") for t in types]  # no previous run, or one without a digest


def test_appeared_only_inside_the_epochs_recorded_window(full):
    df = synth_frame()
    decs = [d for d in full[4] if known_at(d) > BOUNDARY]
    recorded = _record(decs[1:], df)  # the first one was never recorded
    epoch = {"id": 1, "started_ts": BOUNDARY, "through_ts": int(df["ts"].iloc[-1])}
    found = verify_decisions(recorded, full[4], epoch, lambda d: False, lambda r: True)
    assert [(f["type"], f["decision_id"], f["cause"]) for f in found] == [("APPEARED", decs[0]["id"], "DATA_REVISION")]
    early = {**epoch, "through_ts": known_at(decs[0]) - H1}  # not yet known by any recording run
    assert verify_decisions(recorded, full[4], early, lambda d: False, lambda r: True) == []


def test_decisions_to_record_takes_only_newly_known_ones(full):
    decs = full[4]
    through = ts("2024-02-10 00:00")
    epoch = {"id": 1, "started_ts": BOUNDARY, "through_ts": through}
    fresh = decisions_to_record(decs, epoch, set())
    assert fresh and all(known_at(d) > through for d in fresh)
    assert decisions_to_record(decs, epoch, {d["id"] for d in fresh}) == []


def test_open_position_record_and_top_count(cut):
    _, trace, _, open_trade, _ = cut
    rec = open_position_record(open_trade, trace, BOUNDARY)
    assert rec["entry_ts"] == ts("2024-03-13 23:00") and rec["counted"] is True
    assert rec["tp1_done"] is True and rec["remaining"] == rec["contracts"] - 27
    assert rec["stop"] == open_trade.entry_price  # breakeven after TP1
    assert open_position_record(open_trade, trace, ts("2024-03-14 00:00"))["counted"] is False
    top = top_count_summary(trace.last_state)
    assert top is not None and top["ts"] == ts("2024-03-15 06:00") and top["pattern"] in ("IMP", "ABC")
    assert top_count_summary(None) is None
