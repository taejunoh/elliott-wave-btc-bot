"""A-ETH fingerprints (Plan 5a spec 3, 8), in strategy A's format (ewbot.paper.fingerprint.fingerprint_of: sorted
repo-relative paths, each path NUL bytes NUL).

aeth_fingerprint: every *.py file below src/ewbot/aeth - A-ETH's own code (ETH costs, data, stages, runtime). It
is outside strategy A's fingerprinted folders and no A file is in it, so either can change without the other.
epoch_fingerprint: what a paper-A-ETH epoch is bound to - strategy A's fingerprint (the strategy files) and the
A-ETH fingerprint together. A change of either opens a new epoch and restarts the ETH count."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ewbot.paper.fingerprint import PROJECT_ROOT, current_fingerprint, fingerprint_of

AETH_PREFIX = "src/ewbot/aeth/"


def aeth_files(repo_root: Path | None = None) -> dict[str, bytes]:
    """{"src/ewbot/aeth/<rel>": bytes} for every *.py file below src/ewbot/aeth."""
    pkg = (PROJECT_ROOT if repo_root is None else Path(repo_root)) / "src" / "ewbot" / "aeth"
    return {AETH_PREFIX + f.relative_to(pkg).as_posix(): f.read_bytes() for f in pkg.rglob("*.py") if f.is_file()}


def aeth_fingerprint(repo_root: Path | None = None) -> str:
    files = aeth_files(repo_root)
    if not files:
        raise FileNotFoundError(f"no A-ETH source files below {repo_root or PROJECT_ROOT}/src/ewbot/aeth")
    return fingerprint_of(files)


def epoch_fingerprint(a_fingerprint: str, aeth_sha256: str) -> str:
    """sha256 of the pair (strategy A's fingerprint, the A-ETH fingerprint)."""
    return hashlib.sha256(f"strategy-a:{a_fingerprint}\naeth:{aeth_sha256}\n".encode("utf-8")).hexdigest()


def current_epoch_fingerprint(repo_root: Path | None = None) -> str:
    return epoch_fingerprint(current_fingerprint(repo_root), aeth_fingerprint(repo_root))
