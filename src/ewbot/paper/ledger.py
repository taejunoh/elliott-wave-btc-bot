"""Paper-trading ledger (Plan 2 spec 6.2): JSON model, atomic file IO, epochs and the go-live counter.

ledger.json lives on the `paper` branch next to report.html and runs.log. Every write goes to a temp
file in the same directory that then replaces the target (os.replace), so a crash mid-write leaves
the previous version intact."""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ewbot.backtest.holdout import GO_LIVE_MIN_TRADES
from ewbot.backtest.report import to_jsonable
from ewbot.engine.loop import Trade

SCHEMA_VERSION = 1
LEDGER_FILE = "ledger.json"
REPORT_FILE = "report.html"
RUNS_LOG = "runs.log"
GO_LIVE_STRATEGY = "A"  # the paper runtime trades the locked config (strategy.mode "A")


class LedgerError(RuntimeError):
    """ledger.json is unreadable, has another schema version, or does not belong to this holdout."""


@dataclass
class Ledger:
    baseline: dict[str, Any]  # {"fingerprint", "commit"}
    holdout: dict[str, Any]  # holdout_summary(lock)
    epochs: list[dict[str, Any]] = field(default_factory=list)  # {"id","fingerprint","started_ts","reason","through_ts"}
    trades: list[dict[str, Any]] = field(default_factory=list)  # closed paper trades as trade_record dicts
    open_position: dict[str, Any] | None = None
    decisions: list[dict[str, Any]] = field(default_factory=list)
    discrepancies: list[dict[str, Any]] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    sent_event_ids: list[str] = field(default_factory=list)
    pending_events: list[dict[str, Any]] = field(default_factory=list)  # {"id","kind","text","attempts"}
    daily_summary_date: str | None = None  # New York date of the last daily summary
    criteria: dict[str, Any] | None = None  # criteria 1-2 from the last daily recompute
    schema_version: int = SCHEMA_VERSION

    def to_json(self) -> str:
        """Strict JSON (NaN/inf refused with ValueError)."""
        return json.dumps(dataclasses.asdict(self), indent=1, allow_nan=False)

    @classmethod
    def from_dict(cls, data: Any) -> Ledger:
        if not isinstance(data, dict):
            raise LedgerError("ledger root is not a JSON object")
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise LedgerError(f"ledger schema_version {version!r} is not {SCHEMA_VERSION}; refusing to touch it")
        names = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(data) - names)
        missing = sorted(n for n in ("baseline", "holdout", "epochs") if n not in data)
        if unknown or missing:
            raise LedgerError(f"ledger keys do not match schema {SCHEMA_VERSION}: unknown {unknown}, missing {missing}")
        return cls(**data)


def holdout_summary(lock: dict[str, Any]) -> dict[str, Any]:
    """The part of holdout.lock the paper ledger is bound to (precheck has validated the keys)."""
    return {
        "lock_sha256": lock["sha256"],
        "params": [float(p) for p in lock["params"]],
        "holdout_start": int(lock["holdout_start"]),
        "last_bar_ts": int(lock["data"]["last_bar_ts"]),
        "data_sha256": lock["data"]["sha256"],
        "n_trades": {k: int(v) for k, v in lock["first_run"]["criteria"]["n_trades"].items()},
    }


def new_ledger(baseline_fp: str, baseline_commit: str, lock: dict[str, Any]) -> Ledger:
    """Fresh ledger: epoch 1 carries the baseline fingerprint and starts at the holdout's last bar."""
    hs = holdout_summary(lock)
    start = hs["last_bar_ts"]
    epoch = {
        "id": 1,
        "fingerprint": baseline_fp,
        "started_ts": start,
        "reason": f"baseline: holdout commit {baseline_commit[:7]}",
        "through_ts": start,
    }
    return Ledger(baseline={"fingerprint": baseline_fp, "commit": baseline_commit}, holdout=hs, epochs=[epoch])


# ----------------------------------------------------------------------------- atomic IO


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to a temp file next to `path`, fsync it, then os.replace it over `path`.
    On any error the temp file is removed and `path` keeps its previous content."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_ledger(ledger_dir: Path) -> Ledger | None:
    """None if <ledger_dir>/ledger.json does not exist yet."""
    path = Path(ledger_dir) / LEDGER_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LedgerError(f"{path} is unreadable: {exc}") from exc
    return Ledger.from_dict(data)


def save_ledger(ledger_dir: Path, ledger: Ledger) -> Path:
    path = Path(ledger_dir) / LEDGER_FILE
    text = ledger.to_json()  # serialise fully before touching the file
    atomic_write_text(path, text + "\n")
    return path


def append_log_line(path: Path, line: str) -> None:
    """runs.log: append one line, atomically (read + rewrite through atomic_write_text)."""
    path = Path(path)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    atomic_write_text(path, old + line.rstrip("\n") + "\n")


# ----------------------------------------------------------------------------- epochs and trades


def current_epoch(ledger: Ledger) -> dict[str, Any]:
    return ledger.epochs[-1]


def ensure_epoch(ledger: Ledger, fingerprint: str, started_ts: int) -> tuple[dict[str, Any], bool]:
    """Return (current epoch, created). A fingerprint different from the last epoch's opens a new
    epoch starting at `started_ts` (the last closed bar of this run)."""
    last = ledger.epochs[-1]
    if last["fingerprint"] == fingerprint:
        return last, False
    epoch = {
        "id": int(last["id"]) + 1,
        "fingerprint": fingerprint,
        "started_ts": int(started_ts),
        "reason": f"fingerprint changed: {last['fingerprint'][:12]} -> {fingerprint[:12]}",
        "through_ts": int(started_ts),
    }
    ledger.epochs.append(epoch)
    return epoch, True


def trade_record(t: Trade, epoch_id: int) -> dict[str, Any]:
    rec = to_jsonable(dataclasses.asdict(t))
    rec["epoch_id"] = int(epoch_id)
    return rec


def trade_from_record(rec: dict[str, Any]) -> Trade:
    names = {f.name for f in dataclasses.fields(Trade)}
    return Trade(**{k: v for k, v in rec.items() if k in names})


def replace_epoch_trades(ledger: Ledger, epoch_id: int, trades: Sequence[Trade]) -> None:
    """The current epoch's closed trades are recomputed every run; earlier epochs stay frozen."""
    kept = [t for t in ledger.trades if t["epoch_id"] != epoch_id]
    ledger.trades = kept + [trade_record(t, epoch_id) for t in trades]


def golive_count(ledger: Ledger) -> dict[str, Any]:
    """Spec 9.6 criterion 3 counter. The holdout's A trades count only while the ledger has never
    left the baseline epoch; every fingerprint change resets the count to the new epoch's trades.
    `eligible` is False when the current epoch's fingerprint is not the baseline's."""
    cur = current_epoch(ledger)
    holdout_n = int(ledger.holdout["n_trades"][GO_LIVE_STRATEGY]) if len(ledger.epochs) == 1 else 0
    paper_n = sum(1 for t in ledger.trades if t["epoch_id"] == cur["id"])
    return {
        "epoch_id": int(cur["id"]),
        "eligible": cur["fingerprint"] == ledger.baseline["fingerprint"],
        "holdout": holdout_n,
        "paper": paper_n,
        "total": holdout_n + paper_n,
        "required": GO_LIVE_MIN_TRADES,
    }
