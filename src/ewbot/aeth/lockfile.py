"""holdout-aeth.lock primitives (Plan 5a spec 7.1) and the git checks of A-ETH's records.

The lock's sha256 is the sha256 of the sorted compact JSON of its LOCK_BOUND fields. lock_exists is True only for
a JSON object of kind LOCK_KIND whose sha256 is that digest - a file that merely has the name or the shape does
not unlock 2024 (data.py). committed_text is a file's content in git HEAD of its own working tree; dirty_files
lists the tracked files of that working tree with uncommitted changes (the stage-1 record, the lock and the
holdout are made on a clean tree)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping

from ewbot.aeth.config import AethError
from ewbot.backtest.report import to_jsonable

LOCK_KIND = "ewbot-aeth-holdout"
LOCK_VERSION = 1
LOCK_BOUND = ("kind", "version", "product", "future", "params", "a_lock_sha256", "config", "holdout_start",
              "last_bar_ts", "random_runs", "a_fingerprint", "aeth_sha256", "aeth_decision_sha256", "code_sha256",
              "pre2024", "stage1_sha256")


def lock_digest(lock: Mapping[str, Any]) -> str:
    """sha256 of the sorted compact JSON of the lock's LOCK_BOUND fields."""
    blob = json.dumps({k: lock.get(k) for k in LOCK_BOUND}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def lock_exists(lock_path: Path) -> bool:
    p = Path(lock_path)
    if not p.is_file():
        return False
    try:
        lock = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(lock, dict) and lock.get("kind") == LOCK_KIND and lock.get("sha256") == lock_digest(lock)


def read_lock(lock_path: Path) -> dict[str, Any]:
    if not lock_exists(lock_path):
        raise AethError(f"{lock_path} is missing or not a valid A-ETH lock (its sha256 must be the digest of its "
                        "fields)")
    return json.loads(Path(lock_path).read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> None:
    """Strict JSON, atomically (a temp file next to `path`, then os.replace)."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(to_jsonable(obj), indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def committed_text(path: Path) -> str | None:
    """`path`'s content in git HEAD of the working tree it is in; None when it is not committed there."""
    p = Path(path)
    try:
        r = subprocess.run(["git", "-C", str(p.parent), "show", f"HEAD:./{p.name}"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AethError(f"git is not available to check {p}: {exc}") from exc
    return r.stdout.decode("utf-8") if r.returncode == 0 else None


def dirty_files(anchor: Path, *ignore: str) -> list[str]:
    """Tracked files with uncommitted changes in the git working tree whose root holds `anchor`, the files named in
    `ignore` (next to `anchor`) excepted."""
    p = Path(anchor)
    cmd = ["git", "-C", str(p.parent), "status", "--porcelain", "--untracked-files=no", "--", ":/",
           *[f":!{name}" for name in ignore]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AethError(f"git is not available to check the working tree: {exc}") from exc
    if r.returncode != 0:
        raise AethError(f"{p.parent} is not a git working tree: {r.stderr.strip()}")
    return [line[3:] for line in r.stdout.splitlines() if line.strip()]


def refuse_dirty(anchor: Path, *ignore: str) -> None:
    dirty = dirty_files(anchor, *ignore)
    if dirty:
        raise AethError(f"uncommitted changes to tracked files {dirty[:5]}{' ...' if len(dirty) > 5 else ''}: "
                        "A-ETH's records are made on a clean tree (commit or discard them)")
