"""Strategy-B fingerprint (Plan 4 spec 3, 7): sha256 over every *.py file below src/ewbot/stratb.

Same format as strategy A's (ewbot.paper.fingerprint.fingerprint_of: sorted repo-relative paths, each
path NUL bytes NUL), over a disjoint file set: stratb/ is outside A's fingerprinted folders and no A file
is in this one, so either fingerprint can change without the other. Any byte change, addition, removal or
rename below stratb/ changes it; paperb/ and cli.py are not part of it."""

from __future__ import annotations

from pathlib import Path

from ewbot.paper.fingerprint import PROJECT_ROOT, fingerprint_of

STRATB_PREFIX = "src/ewbot/stratb/"


def stratb_files(repo_root: Path | None = None) -> dict[str, bytes]:
    """{"src/ewbot/stratb/<rel>": bytes} for every *.py file below src/ewbot/stratb."""
    pkg = (PROJECT_ROOT if repo_root is None else Path(repo_root)) / "src" / "ewbot" / "stratb"
    return {STRATB_PREFIX + f.relative_to(pkg).as_posix(): f.read_bytes() for f in pkg.rglob("*.py") if f.is_file()}


def stratb_fingerprint(repo_root: Path | None = None) -> str:
    files = stratb_files(repo_root)
    if not files:
        raise FileNotFoundError(f"no strategy-B source files below {repo_root or PROJECT_ROOT}/src/ewbot/stratb")
    return fingerprint_of(files)
