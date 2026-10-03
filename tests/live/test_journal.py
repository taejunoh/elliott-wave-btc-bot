from __future__ import annotations

import sqlite3

import pytest

from ewbot.live.journal import Journal


@pytest.fixture
def jr(tmp_path):
    j = Journal(tmp_path / "live" / "journal.sqlite")
    yield j
    j.close()


def test_intent_is_recorded_once_and_never_overwritten(jr):
    row, created = jr.record_intent("ewb-e-100-L", "ENTRY", 100, {"size": 1}, now=5.0)
    assert created and row["status"] == "PENDING" and row["payload"] == {"size": 1} and row["order_id"] is None
    again, created2 = jr.record_intent("ewb-e-100-L", "ENTRY", 100, {"size": 9}, now=6.0)
    assert not created2 and again["payload"] == {"size": 1} and again["created_ts"] == 5.0
    jr.update_intent("ewb-e-100-L", "ACCEPTED", now=7.0, order_id="o-1")
    jr.update_intent("ewb-e-100-L", "ACCEPTED", now=8.0, note="filled 1")
    got = jr.intent("ewb-e-100-L")
    assert (got["status"], got["order_id"], got["note"], got["updated_ts"]) == ("ACCEPTED", "o-1", "filled 1", 8.0)
    with pytest.raises(ValueError):
        jr.update_intent("ewb-e-100-L", "SENT", now=9.0)


def test_intents_filter_and_survive_a_reopen(tmp_path):
    path = tmp_path / "j.sqlite"
    j = Journal(path)
    j.record_intent("a", "ENTRY", 1, {}, 1.0)
    j.record_intent("b", "BRACKET", 1, {}, 2.0)
    j.record_intent("c", "BRACKET", 2, {}, 3.0)
    j.update_intent("b", "ACCEPTED", 4.0, order_id="x")
    j.close()  # a kill right after the commit: the rows are on disk
    k = Journal(path)
    assert [i["client_order_id"] for i in k.intents(status="PENDING")] == ["a", "c"]
    assert [i["client_order_id"] for i in k.intents(trade_id=1, kind="BRACKET")] == ["b"]
    k.close()


def test_events_decisions_and_kv(jr):
    jr.add_event(10.0, "ENTRY", {"trade_id": 100})
    jr.add_event(20.0, "CLOSE", {"trade_id": 100, "reason": "STOP"})
    assert [e["kind"] for e in jr.events()] == ["ENTRY", "CLOSE"]
    assert jr.events(kind="CLOSE")[0]["data"] == {"trade_id": 100, "reason": "STOP"}
    assert [e["ts"] for e in jr.events(since=15.0)] == [20.0]
    d1 = {"id": "SIGNAL:100:100", "ts": 100, "kind": "SIGNAL", "price": 1.5}
    d2 = {"id": "ENTRY:3700:3700", "ts": 3700, "kind": "ENTRY", "price": 2.5}
    assert jr.add_decisions([d1, d2], now=50.0) == ["SIGNAL:100:100", "ENTRY:3700:3700"]
    assert jr.add_decisions([d1], now=60.0) == []
    assert [d["id"] for d in jr.decisions()] == ["SIGNAL:100:100", "ENTRY:3700:3700"]
    assert jr.decisions()[0]["recorded_ts"] == 50.0
    assert jr.get("trade") is None and jr.get("paused", False) is False
    jr.put("trade", {"trade_id": 100, "legs": [{"size": 1}]})
    assert jr.get("trade") == {"trade_id": 100, "legs": [{"size": 1}]}
    with pytest.raises(ValueError):
        jr.put("bad", float("nan"))


def test_cycles_and_stages(jr):
    assert jr.stage() == ("SHADOW", None)
    jr.set_stage("SHADOW", "initial", now=1.0)
    jr.set_stage("CANARY", "user approved", now=2.0)
    assert jr.stage() == ("CANARY", 2.0) and [s["stage"] for s in jr.stage_history()] == ["SHADOW", "CANARY"]
    jr.start_cycle(3600, now=7230.0, stage="SHADOW")
    jr.finish_cycle(3600, now=7260.0, status="OK", note="flat")
    jr.start_cycle(7200, now=10830.0, stage="SHADOW")
    rows = jr.cycles()
    assert [(c["bar_ts"], c["status"], c["finished_ts"]) for c in rows] == [(3600, "OK", 7260.0), (7200, "RUNNING", None)]
    assert [c["bar_ts"] for c in jr.cycles(since_bar=7200)] == [7200]


def test_a_second_connection_reads_while_the_first_writes(tmp_path):
    path = tmp_path / "j.sqlite"
    writer, reader = Journal(path), Journal(path)
    writer.put("heartbeat", 123.0)
    assert reader.get("heartbeat") == 123.0
    writer.close()
    reader.close()


def test_backup_is_a_consistent_copy(jr, tmp_path):
    jr.record_intent("a", "ENTRY", 1, {"k": 1}, 1.0)
    out = jr.backup(tmp_path / "bk" / "copy.sqlite")
    con = sqlite3.connect(str(out))
    assert con.execute("SELECT client_order_id FROM intents").fetchall() == [("a",)]
    con.close()
