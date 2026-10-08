"""A-ETH fingerprints (Plan 5a spec 3, 8; amended 2026-10-08), in strategy A's format
(ewbot.paper.fingerprint.fingerprint_of: sorted repo-relative paths, each path NUL bytes NUL).

aeth_fingerprint: every *.py file below src/ewbot/aeth - all of A-ETH's code. Stage 1, holdout-aeth.lock and the
holdout run bind it: from stage 1 to the holdout no A-ETH file can change.
aeth_decision_fingerprint: the A-ETH files that decide what a paper run trades (AETH_DECISION_FILES) - config.py
(the params read from A's lock, the run config with the ETH costs, the product, the first bar) and data.py (which
ETH bars are fetched and how they are read). A change there changes the bars or the config the paper run recomputes
from, which the replay check alone would take for a data revision. Every other A-ETH file is runtime or a check:
paper.py (the runner, like strategy A's paper/runner.py, which A's fingerprint leaves out too), sync.py, the lock
checks of lockfile.py and holdout.py (they allow a run or refuse it), stage1.py (not used by the paper run) and
this module.
epoch_fingerprint: what a paper-A-ETH epoch is bound to - strategy A's fingerprint and the A-ETH decision
fingerprint. A change of either opens a new epoch and restarts the ETH count; a runtime fix during paper trading
does not (before the holdout run, the A-ETH files are frozen from stage 1 on and every src/ewbot *.py from the
lock on).
None of A-ETH's files is in strategy A's fingerprinted set, so either side can change without the other."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ewbot.paper.fingerprint import PROJECT_ROOT, current_fingerprint, fingerprint_of

AETH_PREFIX = "src/ewbot/aeth/"
AETH_DECISION_FILES = ("config.py", "data.py")


def _pkg(repo_root: Path | None) -> Path:
    return (PROJECT_ROOT if repo_root is None else Path(repo_root)) / "src" / "ewbot" / "aeth"


def aeth_files(repo_root: Path | None = None) -> dict[str, bytes]:
    """{"src/ewbot/aeth/<rel>": bytes} for every *.py file below src/ewbot/aeth."""
    pkg = _pkg(repo_root)
    return {AETH_PREFIX + f.relative_to(pkg).as_posix(): f.read_bytes() for f in pkg.rglob("*.py") if f.is_file()}


def aeth_fingerprint(repo_root: Path | None = None) -> str:
    files = aeth_files(repo_root)
    if not files:
        raise FileNotFoundError(f"no A-ETH source files below {repo_root or PROJECT_ROOT}/src/ewbot/aeth")
    return fingerprint_of(files)


def aeth_decision_files(repo_root: Path | None = None) -> dict[str, bytes]:
    """{"src/ewbot/aeth/<name>": bytes} for the AETH_DECISION_FILES (FileNotFoundError when one is missing)."""
    pkg = _pkg(repo_root)
    return {AETH_PREFIX + name: (pkg / name).read_bytes() for name in AETH_DECISION_FILES}


def aeth_decision_fingerprint(repo_root: Path | None = None) -> str:
    return fingerprint_of(aeth_decision_files(repo_root))


def epoch_fingerprint(a_fingerprint: str, aeth_decision_sha256: str) -> str:
    """sha256 of the pair (strategy A's fingerprint, the A-ETH decision fingerprint)."""
    pair = f"strategy-a:{a_fingerprint}\naeth-decision:{aeth_decision_sha256}\n"
    return hashlib.sha256(pair.encode("utf-8")).hexdigest()


def current_epoch_fingerprint(repo_root: Path | None = None) -> str:
    return epoch_fingerprint(current_fingerprint(repo_root), aeth_decision_fingerprint(repo_root))
