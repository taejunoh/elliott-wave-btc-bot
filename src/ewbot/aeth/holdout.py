"""Stage 2 of A-ETH (Plan 5a spec 7): holdout-aeth.lock and the one-shot ETH holdout.

create_lock writes <project root>/holdout-aeth.lock BEFORE any ETH bar from 2024-01-01 on can be fetched or read
(data.py unlocks them only for a valid lock), from the committed stage-1 record (aeth-stage1.json): only a record
that passed (its criteria are stage1_verdict of its own headline) and was made with the params and sha256 of
strategy A's holdout.lock, this A-ETH code (aeth_sha256), strategy A's frozen fingerprint and the same pre-2024 ETH
bars. The lock fixes the product and contract, the params and A's lock sha256, the full config (ETH costs), the
holdout start and its last bar (the last complete hour at lock time), the 100-run random control, A's
fingerprint, the A-ETH fingerprint, the A-ETH decision fingerprint (config.py and data.py: what the paper
epoch is bound to, spec 8), the code state like A's lock (code_sha256: A's code_fingerprint over every
src/ewbot *.py), the pre-2024 bars (data_record) and the stage-1 record's sha256, and their digest. It refuses a
dirty tracked tree. The user commits it at once.

run_holdout_aeth runs the holdout ONCE, after the user's approval (confirm=True), over [2024-01-01, last_bar_ts]
from the first cached bar, like strategy A's holdout. It refuses unless the lock is valid and equals the committed
one, first_run is empty there and in the file, strategy A's fingerprint, the A-ETH fingerprint and the code
fingerprint are the lock's, the lock's fields are what this code writes for A's params now (lock_mismatch), no
tracked file but the lock has uncommitted changes, the pre-2024 bars are the lock's, the data reaches last_bar_ts
and an unfinished earlier attempt saw the same holdout bars. Each attempt is recorded in the lock before it runs.

Verdict (spec 7.3): strategy A's evaluate_criteria, unchanged, on the holdout's compare(). A-ETH passes when "A" is
one of its winners (A's Sharpe and Calmar above C's, buy & hold's and the random control's mean; the random control
evaluated) and A's max drawdown is at most 15% - strategy A's go-live items 1 and 2. The trade count is recorded."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

from ewbot.aeth.config import (
    A_LOCK_PATH,
    FROZEN_A_FINGERPRINT,
    FUTURE,
    LOCK_PATH,
    PRODUCT,
    RANDOM_RUNS,
    STAGE1_PATH,
    AethError,
    aeth_config,
    config_json,
    frozen_a_params,
)
from ewbot.aeth.data import cache_path, load_eth
from ewbot.aeth.fingerprint import aeth_decision_fingerprint, aeth_fingerprint
from ewbot.aeth.lockfile import (
    LOCK_KIND,
    LOCK_VERSION,
    committed_text,
    lock_digest,
    read_lock,
    refuse_dirty,
    write_json,
)
from ewbot.aeth.stage1 import MAX_DD, STAGE1_KIND, data_record, headline, stage1_verdict
from ewbot.backtest.holdout import code_fingerprint, data_fingerprint, evaluate_criteria, git_state
from ewbot.backtest.report import to_jsonable
from ewbot.backtest.runner import compare
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.data.candles import fill_gaps
from ewbot.paper.fingerprint import current_fingerprint
from ewbot.types import H1


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def stage2_verdict(criteria: Mapping[str, Any], comparison: Mapping[str, Any]) -> dict[str, Any]:
    """Spec 7.3 from strategy A's evaluate_criteria output and the comparison it was made from."""
    c1 = "A" in criteria["winners"]
    c2 = float(comparison["A"]["max_dd"]) <= MAX_DD
    return {"c1_a_beats_c_bh_random_mean": bool(c1), "c2_max_dd_le_15pct": bool(c2),
            "n_trades": int(comparison["A"]["n_trades"]), "passed": bool(c1 and c2)}


def lock_mismatch(lock: Mapping[str, Any], a_lock_path: Path = A_LOCK_PATH) -> list[str]:
    """The lock fields that are not what this code writes for strategy A's params now (AethError when A's
    holdout.lock itself is not valid)."""
    params, a_sha = frozen_a_params(a_lock_path)
    want = {"kind": LOCK_KIND, "version": LOCK_VERSION, "product": PRODUCT, "future": FUTURE,
            "params": list(params), "a_lock_sha256": a_sha, "config": config_json(params),
            "holdout_start": HOLDOUT_START}
    return [k for k, v in want.items() if lock.get(k) != v]


def create_lock(
    raw_pre2024: pd.DataFrame,
    *,
    now_ts: int,
    lock_path: Path = LOCK_PATH,
    stage1_path: Path = STAGE1_PATH,
    a_lock_path: Path = A_LOCK_PATH,
    random_runs: int = RANDOM_RUNS,
) -> dict[str, Any]:
    """Write the lock (once) from the committed, passed stage-1 record. raw_pre2024: the cached ETH bars, all
    before 2024-01-01 (the record must have been made on them)."""
    path, s1 = Path(lock_path), Path(stage1_path)
    if path.exists():
        raise AethError(f"{path} exists: the A-ETH lock is written once")
    text = committed_text(s1)
    if text is None or not s1.is_file() or s1.read_text(encoding="utf-8") != text:
        raise AethError(f"{s1} must be committed unchanged before the lock (it is the permanent stage-1 record)")
    try:
        rec = json.loads(text)
        head, crit, prov = rec["headline"], rec["criteria"], rec["provenance"]
        ok = rec.get("kind") == STAGE1_KIND and crit == stage1_verdict(head) and bool(crit["passed"])
    except (ValueError, KeyError, TypeError) as exc:
        raise AethError(f"{s1} is not an A-ETH stage-1 record: {exc!r}") from exc
    if not ok:
        raise AethError(f"{s1}: stage 1 did not pass (spec 6): A-ETH ends without a holdout")
    if len(raw_pre2024) and int(raw_pre2024["ts"].max()) >= HOLDOUT_START:
        raise AethError("the lock is made from the ETH bars before 2024-01-01 only")
    params, a_sha = frozen_a_params(a_lock_path)
    a_fp, aeth_sha = current_fingerprint(), aeth_fingerprint()
    pre = data_record(raw_pre2024)
    stale = [name for name, same in (
        ("params", rec.get("params") == list(params) and prov.get("a_lock_sha256") == a_sha),
        ("config", rec.get("config") == config_json(params)),
        ("a_fingerprint", a_fp == FROZEN_A_FINGERPRINT and prov.get("a_fingerprint") == a_fp),
        ("aeth_sha256", prov.get("aeth_sha256") == aeth_sha),
        ("data", prov.get("data") == pre),
    ) if not same]
    if stale:
        raise AethError(f"{s1} was made with other {stale} than now: the lock must bind what stage 1 ran on")
    last_bar = int(now_ts) // H1 * H1 - H1  # the last complete hour
    if last_bar < HOLDOUT_START:
        raise AethError("the lock must be written after 2024-01-01")
    if int(random_runs) < 1:
        raise AethError("random_runs must be >= 1 (strategy A's criteria compare with the random control)")
    refuse_dirty(path, path.name)
    lock: dict[str, Any] = {
        "kind": LOCK_KIND,
        "version": LOCK_VERSION,
        "product": PRODUCT,
        "future": FUTURE,
        "params": list(params),
        "a_lock_sha256": a_sha,
        "config": config_json(params),
        "holdout_start": HOLDOUT_START,
        "last_bar_ts": last_bar,
        "random_runs": int(random_runs),
        "a_fingerprint": a_fp,
        "aeth_sha256": aeth_sha,
        "aeth_decision_sha256": aeth_decision_fingerprint(),
        "code_sha256": code_fingerprint(),
        "pre2024": pre,
        "stage1_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    lock.update(sha256=lock_digest(lock), locked_at=_now(), **git_state(), attempts=[], first_run=None)
    write_json(path, lock)
    return lock


def run_holdout_aeth(
    *,
    confirm: bool,
    cache_dir: Path,
    lock_path: Path = LOCK_PATH,
    a_lock_path: Path = A_LOCK_PATH,
    workers: int = 1,
    a_fingerprint: Callable[[], str] = current_fingerprint,
    aeth_fp: Callable[[], str] = aeth_fingerprint,
    code_fp: Callable[[], str] = code_fingerprint,
) -> dict[str, Any]:
    """The one-shot ETH holdout over [2024-01-01, last_bar_ts]; the result goes to the lock's first_run."""
    if not confirm:
        raise AethError("the ETH holdout is single-use: pass confirm=True (CLI: --confirm) after the user's approval")
    path = Path(lock_path)
    lock = read_lock(path)
    text = committed_text(path)
    head = json.loads(text) if text else None
    if not isinstance(head, dict) or head.get("sha256") != lock["sha256"]:
        raise AethError(f"{path} differs from the committed lock: commit the lock as written by `aeth holdout lock`")
    if lock.get("first_run") is not None or head.get("first_run") is not None:
        raise AethError("the ETH holdout has already run once: no rerun (spec 7.2)")
    changed = [name for name, now, locked in (
        ("strategy A fingerprint", a_fingerprint(), lock["a_fingerprint"]),
        ("A-ETH fingerprint", aeth_fp(), lock["aeth_sha256"]),
        ("code fingerprint", code_fp(), lock["code_sha256"]),
    ) if now != locked]
    if changed:
        raise AethError(f"the {changed} changed after the lock: refusing")
    stale = lock_mismatch(lock, a_lock_path)
    if stale:
        raise AethError(f"the lock's {stale} are not this code's for strategy A's params: refusing")
    refuse_dirty(path, path.name)
    raw = load_eth(cache_path(cache_dir), unlock_2024=True, lock_path=path)
    if data_record(raw) != lock["pre2024"]:
        raise AethError("the cached ETH bars before 2024-01-01 differ from the ones stage 1 and the lock used")
    last = int(lock["last_bar_ts"])
    if raw.empty or int(raw["ts"].iloc[-1]) < last:
        raise AethError(f"the ETH cache ends before the lock's last bar {last}: run `ewbot aeth fetch` first")
    filled = fill_gaps(raw)  # like the paper runtime: fill all, then cut (a missing last bar: flat at the prior close)
    df = filled[filled["ts"] <= last].reset_index(drop=True)
    data = {"start_ts": HOLDOUT_START, "last_bar_ts": last,
            "n_bars": int(((df["ts"] >= HOLDOUT_START) & (df["ts"] <= last)).sum()),
            "sha256": data_fingerprint(df, HOLDOUT_START, last)}
    if any(a.get("data_sha256") != data["sha256"] for a in lock["attempts"]):
        raise AethError("an earlier unfinished attempt saw other holdout bars: refusing (inspect by hand)")
    attempt: dict[str, Any] = {"started": _now(), "data_sha256": data["sha256"], **git_state()}
    lock["attempts"].append(attempt)
    write_json(path, lock)

    cfg = aeth_config(tuple(lock["params"]))
    end = last + H1
    cmp = compare(cfg, df, HOLDOUT_START, end, random_runs=int(lock["random_runs"]), workers=workers,
                  allow_holdout=True)
    criteria = evaluate_criteria(cmp)
    verdict = stage2_verdict(criteria, cmp["comparison"])
    attempt["completed"] = _now()
    lock["first_run"] = to_jsonable({
        "started": attempt["started"], "completed": attempt["completed"], "start_ts": HOLDOUT_START, "end_ts": end,
        "last_bar_ts": last, "data": data, "headline": headline(cmp), "criteria": criteria, "verdict": verdict,
    })
    write_json(path, lock)
    return {"lock_path": str(path), "compare": cmp, "first_run": lock["first_run"]}
