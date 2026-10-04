from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ewbot.live.journal import SCHEMA_VERSION, Journal, JournalError


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
    for bad in ("canary", "", "LIVE"):  # a typo is never written: the runtime would read it as a real-money stage
        with pytest.raises(ValueError, match="unknown stage"):
            jr.set_stage(bad, "typo", now=3.0)
    assert jr.stage() == ("CANARY", 2.0)
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


# ---------------------------------------------------------------------------- Final fix
def test_an_update_of_an_unknown_intent_or_cycle_raises(jr):
    """A silent no-op would lose an order's outcome (ACCEPTED, REJECTED) or a cycle's status without a trace."""
    with pytest.raises(KeyError, match="ewb-e-1-L"):
        jr.update_intent("ewb-e-1-L", "ACCEPTED", now=1.0, order_id="o-1")
    with pytest.raises(KeyError, match="3600"):
        jr.finish_cycle(3600, now=2.0, status="OK")
    assert jr.intents() == [] and jr.cycles() == []


def test_the_journal_is_wal_and_carries_its_schema_version(tmp_path):
    path = tmp_path / "j.sqlite"
    Journal(path).close()
    con = sqlite3.connect(str(path))
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 1
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")  # written by a newer runtime (a rollback)
    con.close()
    with pytest.raises(JournalError, match="schema version 2"):
        Journal(path)
    with pytest.raises(JournalError, match="WAL"):  # no WAL (here: an in-memory database): no crash-safe journal
        Journal(Path(":memory:"))


def test_committed_rows_survive_a_killed_process(tmp_path):
    """Intent-before-send relies on it: a row committed right before a SIGKILL (no close, no checkpoint) is there after."""
    path = tmp_path / "j.sqlite"
    code = (f"import os, signal; from pathlib import Path; from ewbot.live.journal import Journal\n"
            f"j = Journal(Path({str(path)!r}))\n"
            f"j.record_intent('ewb-e-7-L', 'ENTRY', 7, {{'size': 1}}, 1.0)\n"
            f"j.put('trade', {{'trade_id': 7}})\n"
            f"os.kill(os.getpid(), signal.SIGKILL)\n")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=60)
    assert done.returncode == -9, done.stderr
    j = Journal(path)
    assert j.intent("ewb-e-7-L")["status"] == "PENDING" and j.get("trade") == {"trade_id": 7}
    j.close()


def test_a_read_only_journal_reads_and_never_writes_nor_migrates(tmp_path):
    """Final review B minor 12: the deploy probes, `live preflight` and `live status` open the live journal read-only:
    a candidate image never migrates (or otherwise writes) the journal the running image uses - a later schema version
    would make the old image refuse it on a rollback."""
    p = tmp_path / "journal.sqlite"
    j = Journal(p)
    j.put("x", 1)
    j.set_stage("CANARY", "test", 1.0)
    j.close()
    raw = sqlite3.connect(p)
    raw.execute("PRAGMA user_version = 0")  # a journal written before the version was recorded
    raw.close()
    before = p.read_bytes()
    ro = Journal(p, read_only=True)
    assert ro.get("x") == 1 and ro.stage()[0] == "CANARY"
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ro.put("y", 2)
    ro.close()
    assert p.read_bytes() == before
    raw = sqlite3.connect(p)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 0  # not migrated
    raw.execute("PRAGMA user_version = 2")
    raw.close()
    with pytest.raises(JournalError, match="newer"):
        Journal(p, read_only=True)
    with pytest.raises(JournalError, match="no journal"):
        Journal(tmp_path / "missing.sqlite", read_only=True)
    assert not (tmp_path / "missing.sqlite").exists()
