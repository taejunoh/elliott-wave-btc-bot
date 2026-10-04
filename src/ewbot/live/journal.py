"""Live journal (Plan 3 spec 3.2 `journal`): one SQLite file with order intents, events, the engine's
decisions, decision cycles, stage history and a small JSON key/value store (trade state, pause flag,
account guard, Telegram offset).

Intent-before-send: every order is first recorded as an intent keyed by its deterministic
client_order_id (status PENDING) and committed; only then is it sent. After a crash, a PENDING intent
means "outcome unknown": it is looked up on the exchange by client_order_id before anything is sent
again (live/executor.py). The connection is in autocommit mode with WAL and synchronous=FULL, so a
committed row survives a kill or power loss, and `ewbot live status` can read while the runtime writes: a
database that does not take WAL is refused (JournalError), as is one whose schema version (`PRAGMA
user_version`, SCHEMA_VERSION) is newer than this runtime's - a rollback never writes into a journal it does not
know. Every statement and its fetch run under one lock (the Telegram thread writes too), and an update of an
intent or a cycle that does not exist raises KeyError (never a silent no-op). `read_only=True` (`live status`,
`live preflight`, the deploy script's probes from a candidate image) opens an existing journal for reading only: it
never creates, migrates or writes it (a write raises sqlite3.OperationalError), so a candidate image never changes
the journal the running image uses; a missing file or a newer schema is a JournalError."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

SCHEMA_VERSION = 1  # PRAGMA user_version of the journal this module writes
INTENT_STATUSES = ("PENDING", "ACCEPTED", "REJECTED", "ABANDONED")
DEFAULT_STAGE = "SHADOW"
KNOWN_STAGES = ("SHADOW", "CANARY", "FULL")  # = live.stage.STAGES (the stage module builds on this one)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    client_order_id TEXT PRIMARY KEY, kind TEXT NOT NULL, trade_id INTEGER, payload TEXT NOT NULL,
    status TEXT NOT NULL, order_id TEXT, note TEXT, created_ts REAL NOT NULL, updated_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_kind ON events (kind, ts);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY, ts INTEGER NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL, recorded_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cycles (
    bar_ts INTEGER PRIMARY KEY, started_ts REAL NOT NULL, finished_ts REAL, status TEXT NOT NULL,
    stage TEXT NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, stage TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class JournalError(RuntimeError):
    """The file cannot be this runtime's journal: no WAL, or a schema version newer than SCHEMA_VERSION."""


class Journal:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        self._lock = threading.RLock()
        if read_only:
            if not self.path.is_file():
                raise JournalError(f"{self.path}: no journal to read")
            uri = f"file:{quote(str(self.path.resolve()))}?mode=ro"
            self._db = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False, timeout=30.0)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False, timeout=30.0)
        self._db.row_factory = sqlite3.Row
        try:
            mode = str(self._db.execute("PRAGMA journal_mode" if read_only else "PRAGMA journal_mode=WAL").fetchone()[0])
            if mode.lower() != "wal":  # the pragma answers the mode in force: refused (e.g. no shared memory)
                raise JournalError(f"{self.path}: journal_mode is {mode!r}, not WAL - a committed row might not "
                                   "survive a crash")
            version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise JournalError(f"{self.path}: journal schema version {version} is newer than this runtime's "
                                   f"{SCHEMA_VERSION} (deploy the release that wrote it)")
            if read_only:  # never created, migrated or written
                return
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript(_SCHEMA)
            if version < SCHEMA_VERSION:  # a new journal, or one written before the version was recorded
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except BaseException:
            self._db.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, tuple(args))

    def _one(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:  # the fetch under the lock as well: another thread's statement never interleaves
            return self._db.execute(sql, tuple(args)).fetchone()

    def _all(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(args)).fetchall()

    # ------------------------------------------------------------------ intents
    @staticmethod
    def _intent(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        out = dict(row)
        out["payload"] = json.loads(out["payload"])
        return out

    def record_intent(
        self, client_order_id: str, kind: str, trade_id: int | None, payload: dict[str, Any], now: float
    ) -> tuple[dict[str, Any], bool]:
        """(intent row, created). An existing client_order_id is never overwritten: the caller gets the
        recorded row back and must not send blindly (look it up first if it is still PENDING)."""
        cur = self._exec(
            "INSERT OR IGNORE INTO intents VALUES (?, ?, ?, ?, 'PENDING', NULL, NULL, ?, ?)",
            (client_order_id, kind, trade_id, _dumps(payload), float(now), float(now)),
        )
        row = self.intent(client_order_id)
        assert row is not None
        return row, cur.rowcount == 1

    def intent(self, client_order_id: str) -> dict[str, Any] | None:
        return self._intent(self._one("SELECT * FROM intents WHERE client_order_id = ?", (client_order_id,)))

    def update_intent(
        self, client_order_id: str, status: str, now: float, order_id: str | None = None, note: str | None = None
    ) -> None:
        if status not in INTENT_STATUSES:
            raise ValueError(f"unknown intent status {status}")
        cur = self._exec(
            "UPDATE intents SET status = ?, order_id = COALESCE(?, order_id), note = COALESCE(?, note), "
            "updated_ts = ? WHERE client_order_id = ?",
            (status, order_id, note, float(now), client_order_id),
        )
        if cur.rowcount != 1:
            raise KeyError(f"no intent {client_order_id!r} to update")

    def intents(self, status: str | None = None, trade_id: int | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM intents WHERE 1 = 1", []
        for col, val in (("status", status), ("trade_id", trade_id), ("kind", kind)):
            if val is not None:
                sql += f" AND {col} = ?"
                args.append(val)
        rows = self._all(sql + " ORDER BY created_ts, client_order_id", args)
        return [self._intent(r) for r in rows]  # type: ignore[misc]

    # ------------------------------------------------------------------ events
    def add_event(self, ts: float, kind: str, data: dict[str, Any] | None = None) -> int:
        cur = self._exec("INSERT INTO events (ts, kind, data) VALUES (?, ?, ?)", (float(ts), kind, _dumps(data or {})))
        return int(cur.lastrowid or 0)

    def events(self, kind: str | None = None, since: float | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM events WHERE 1 = 1", []
        if kind is not None:
            sql += " AND kind = ?"
            args.append(kind)
        if since is not None:
            sql += " AND ts >= ?"
            args.append(float(since))
        rows = self._all(sql + " ORDER BY id", args)
        return [{"id": r["id"], "ts": r["ts"], "kind": r["kind"], "data": json.loads(r["data"])} for r in rows]

    # ------------------------------------------------------------------ engine decisions (paper format)
    def add_decisions(self, decisions: Iterable[dict[str, Any]], now: float) -> list[str]:
        new: list[str] = []
        for d in decisions:
            cur = self._exec(
                "INSERT OR IGNORE INTO decisions VALUES (?, ?, ?, ?, ?)",
                (d["id"], int(d["ts"]), d["kind"], _dumps(d), float(now)),
            )
            if cur.rowcount == 1:
                new.append(d["id"])
        return new

    def decisions(self) -> list[dict[str, Any]]:
        rows = self._all("SELECT data, recorded_ts FROM decisions ORDER BY ts, id")
        return [{**json.loads(r["data"]), "recorded_ts": r["recorded_ts"]} for r in rows]

    # ------------------------------------------------------------------ cycles
    def start_cycle(self, bar_ts: int, now: float, stage: str) -> None:
        self._exec("INSERT OR REPLACE INTO cycles VALUES (?, ?, NULL, 'RUNNING', ?, NULL)", (int(bar_ts), float(now), stage))

    def finish_cycle(self, bar_ts: int, now: float, status: str, note: str | None = None) -> None:
        cur = self._exec("UPDATE cycles SET finished_ts = ?, status = ?, note = ? WHERE bar_ts = ?",
                         (float(now), status, note, int(bar_ts)))
        if cur.rowcount != 1:
            raise KeyError(f"no cycle of bar {int(bar_ts)} to finish")

    def cycles(self, since_bar: int | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM cycles", []
        if since_bar is not None:
            sql += " WHERE bar_ts >= ?"
            args.append(int(since_bar))
        return [dict(r) for r in self._all(sql + " ORDER BY bar_ts", args)]

    # ------------------------------------------------------------------ stages
    def set_stage(self, stage: str, reason: str, now: float) -> None:
        """Record a stage; any other value than KNOWN_STAGES is a ValueError (never read back as a stage)."""
        if stage not in KNOWN_STAGES:
            raise ValueError(f"unknown stage {stage!r} (one of {', '.join(KNOWN_STAGES)})")
        self._exec("INSERT INTO stages (ts, stage, reason) VALUES (?, ?, ?)", (float(now), stage, reason))

    def stage(self) -> tuple[str, float | None]:
        """(current stage, since). SHADOW with since None until the first set_stage."""
        row = self._one("SELECT stage, ts FROM stages ORDER BY id DESC LIMIT 1")
        return (DEFAULT_STAGE, None) if row is None else (row["stage"], row["ts"])

    def stage_history(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._all("SELECT * FROM stages ORDER BY id")]

    # ------------------------------------------------------------------ key / value
    def get(self, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return default if row is None else json.loads(row["value"])

    def put(self, key: str, value: Any) -> None:
        self._exec("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, _dumps(value)))

    # ------------------------------------------------------------------ backup
    def backup(self, dest: Path) -> Path:
        """A consistent copy of the whole journal (SQLite online backup API), safe while running."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(dest))
        try:
            with self._lock:
                self._db.backup(target)
        finally:
            target.close()
        return dest
