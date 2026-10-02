from __future__ import annotations

import json
import os

import pytest

from ewbot.engine.loop import Trade
from ewbot.paper import ledger as lg
from ewbot.paper.ledger import (
    LEDGER_FILE,
    SCHEMA_VERSION,
    Ledger,
    LedgerError,
    append_log_line,
    atomic_write_text,
    ensure_epoch,
    golive_count,
    load_ledger,
    new_ledger,
    replace_epoch_trades,
    save_ledger,
    trade_from_record,
    trade_record,
)

LOCK = {
    "sha256": "a" * 64,
    "params": [2.0, 0.7, 0.5],
    "holdout_start": 1_704_067_200,
    "data": {"last_bar_ts": 1_790_938_800, "sha256": "b" * 64},
    "first_run": {"criteria": {"n_trades": {"A": 36, "HYBRID": 40}}},
}


def _trade(entry_ts: int, exit_ts: int, pnl: float = 5.0) -> Trade:
    return Trade(
        entry_ts=entry_ts, exit_ts=exit_ts, setup="W3", direction=1, contracts=2, entry_price=100.0,
        exit_price_avg=101.0, stop0=95.0, tp1=105.0, tp2=None, pnl=pnl, fees=0.1, funding=0.0, r_multiple=0.5,
        exit_reason="STOP", snapshot={"q_top": 0.8, "counts": [{"pattern": "IMP", "points": [(1, 2.0)]}]},
    )


def test_new_ledger_binds_baseline_and_holdout_summary():
    led = new_ledger("f" * 64, "64e628ad4865b3b04571a7cba2886881eae19176", LOCK)
    assert led.schema_version == SCHEMA_VERSION
    assert led.baseline == {"fingerprint": "f" * 64, "commit": "64e628ad4865b3b04571a7cba2886881eae19176"}
    assert led.holdout == {
        "lock_sha256": "a" * 64, "params": [2.0, 0.7, 0.5], "holdout_start": 1_704_067_200,
        "last_bar_ts": 1_790_938_800, "data_sha256": "b" * 64, "n_trades": {"A": 36, "HYBRID": 40},
    }
    assert led.epochs == [{"id": 1, "fingerprint": "f" * 64, "started_ts": 1_790_938_800,
                           "reason": "baseline: holdout commit 64e628a", "through_ts": 1_790_938_800}]


def test_schema_round_trip(tmp_path):
    led = new_ledger("f" * 64, "c" * 40, LOCK)
    replace_epoch_trades(led, 1, [_trade(1_790_942_400, 1_790_956_800)])
    led.decisions.append({"id": "ENTRY:1:1", "kind": "ENTRY", "levels": [None, 2.5]})
    led.sent_event_ids.append("ENTRY:1:1")
    led.daily_summary_date = "2026-10-03"
    save_ledger(tmp_path, led)
    again = load_ledger(tmp_path)
    assert again == led
    assert trade_from_record(again.trades[0]).entry_ts == 1_790_942_400
    assert load_ledger(tmp_path / "missing") is None


def test_trade_record_is_json_and_round_trips():
    t = _trade(10, 20)
    rec = trade_record(t, 3)
    assert rec["epoch_id"] == 3 and json.loads(json.dumps(rec)) == rec
    back = trade_from_record(rec)
    assert (back.entry_ts, back.pnl, back.tp2, back.exit_reason) == (10, 5.0, None, "STOP")


def test_schema_version_and_unknown_keys_are_refused(tmp_path):
    led = new_ledger("f" * 64, "c" * 40, LOCK)
    save_ledger(tmp_path, led)
    path = tmp_path / LEDGER_FILE
    data = json.loads(path.read_text())
    path.write_text(json.dumps({**data, "schema_version": 99}))
    with pytest.raises(LedgerError, match="schema_version"):
        load_ledger(tmp_path)
    path.write_text(json.dumps({**data, "surprise": 1}))
    with pytest.raises(LedgerError, match="unknown"):
        load_ledger(tmp_path)
    path.write_text("{not json")
    with pytest.raises(LedgerError, match="unreadable"):
        load_ledger(tmp_path)


def test_atomic_write_keeps_the_original_when_replace_fails(tmp_path, monkeypatch):
    led = new_ledger("f" * 64, "c" * 40, LOCK)
    save_ledger(tmp_path, led)
    before = (tmp_path / LEDGER_FILE).read_text()
    led.daily_summary_date = "2030-01-01"

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(lg.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        save_ledger(tmp_path, led)
    assert (tmp_path / LEDGER_FILE).read_text() == before
    assert sorted(os.listdir(tmp_path)) == [LEDGER_FILE]  # no temp file left behind


def test_unserialisable_ledger_never_touches_the_file(tmp_path):
    led = new_ledger("f" * 64, "c" * 40, LOCK)
    save_ledger(tmp_path, led)
    before = (tmp_path / LEDGER_FILE).read_text()
    led.criteria = {"sharpe": float("nan")}
    with pytest.raises(ValueError):
        save_ledger(tmp_path, led)
    assert (tmp_path / LEDGER_FILE).read_text() == before


def test_atomic_write_and_log_append(tmp_path):
    p = tmp_path / "sub" / "runs.log"
    append_log_line(p, "one")
    append_log_line(p, "two\n")
    assert p.read_text() == "one\ntwo\n"
    atomic_write_text(p, "x")
    assert p.read_text() == "x"


def test_new_fingerprint_opens_an_epoch_resets_the_count_and_keeps_old_trades():
    led = new_ledger("base", "c" * 40, LOCK)
    ep, created = ensure_epoch(led, "base", 1_791_000_000)
    assert not created and ep["id"] == 1
    replace_epoch_trades(led, 1, [_trade(1_790_942_400, 1_790_956_800), _trade(1_790_960_400, 1_790_964_000)])
    assert golive_count(led) == {"epoch_id": 1, "eligible": True, "holdout": 36, "paper": 2, "total": 38, "required": 50}

    ep2, created = ensure_epoch(led, "edited", 1_791_000_000)
    assert created and ep2 == {"id": 2, "fingerprint": "edited", "started_ts": 1_791_000_000,
                               "reason": "fingerprint changed: base -> edited", "through_ts": 1_791_000_000}
    replace_epoch_trades(led, 2, [])
    assert golive_count(led) == {"epoch_id": 2, "eligible": False, "holdout": 0, "paper": 0, "total": 0, "required": 50}
    assert [t["epoch_id"] for t in led.trades] == [1, 1]  # epoch 1 trades preserved

    replace_epoch_trades(led, 2, [_trade(1_791_003_600, 1_791_007_200)])
    assert golive_count(led)["paper"] == 1 and len(led.trades) == 3

    ep3, created = ensure_epoch(led, "base", 1_791_100_000)  # reverting the code: baseline fingerprint again
    assert created and ep3["id"] == 3
    count = golive_count(led)
    assert count["eligible"] and count["holdout"] == 0 and count["total"] == 0  # the holdout chain stays broken


def test_ledger_from_dict_requires_core_keys():
    with pytest.raises(LedgerError, match="missing"):
        Ledger.from_dict({"schema_version": SCHEMA_VERSION})
    with pytest.raises(LedgerError, match="not a JSON object"):
        Ledger.from_dict([1, 2])
