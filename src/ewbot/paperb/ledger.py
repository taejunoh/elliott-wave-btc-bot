"""Paper-B ledger: strategy A's ledger schema and IO (ewbot.paper.ledger) bound to holdout-b.lock.

Epoch 1 starts at the lock's last bar with the lock's B fingerprint; another B fingerprint opens a new
epoch (A's ensure_epoch) and the count starts again (spec 7). Paper trades are the current epoch's closed
trades of both assets that entered after its start (A's paper_trades); 30 are needed, BTC and ETH
together (spec 7.1). Holdout trades never count. open_position holds {asset: position} (or None): B may
hold one position per asset."""

from __future__ import annotations

from typing import Any

from ewbot.paper.ledger import Ledger, current_epoch

PAPER_B_REQUIRED = 30


def holdout_summary_b(lock: dict[str, Any]) -> dict[str, Any]:
    """The part of holdout-b.lock the ledger is bound to (the precheck validated the keys)."""
    fr = lock["first_run"]
    return {
        "lock_sha256": lock["sha256"],
        "params": lock["params"],
        "holdout_start": int(lock["holdout_start"]),
        "replay_anchor": int(lock["replay_anchor"]),
        "last_bar_ts": int(lock["last_bar_ts"]),
        "data_sha256": fr["data"]["sha256"],
        "n_trades": {"B": int(fr["metrics"]["B"]["n_trades"])},
        "passed": bool(fr["criteria"]["passed"]),
    }


def new_ledger_b(lock: dict[str, Any]) -> Ledger:
    hs = holdout_summary_b(lock)
    start = hs["last_bar_ts"]
    fp = lock["code_sha256"]
    epoch = {"id": 1, "fingerprint": fp, "started_ts": start, "reason": "baseline: holdout-b.lock", "through_ts": start}
    return Ledger(baseline={"fingerprint": fp, "commit": str(lock.get("git_head") or "holdout-b.lock")}, holdout=hs,
                  epochs=[epoch])


def paper_count_b(ledger: Ledger) -> dict[str, Any]:
    cur = current_epoch(ledger)
    n = sum(1 for t in ledger.trades if t["epoch_id"] == cur["id"])
    return {"epoch_id": int(cur["id"]), "eligible": cur["fingerprint"] == ledger.baseline["fingerprint"], "paper": n,
            "required": PAPER_B_REQUIRED}
