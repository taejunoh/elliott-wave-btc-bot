"""Runtime environment of a paper run (Plan 2 spec 5.3, ENVIRONMENT_CHANGE; spec 8 freezes it).

The environment is what can change a recompute without touching the strategy code: the Python
version, the numpy / pandas / pyarrow versions, the sha256 of uv.lock and the GitHub Actions runner
image (the ImageOS / ImageVersion variables the hosted runners set; None elsewhere). Every run
records it; every decision carries the digest of the environment of the run that recorded it."""

from __future__ import annotations

import hashlib
import json
import os
import platform
from importlib import metadata
from pathlib import Path
from typing import Any, Mapping

from ewbot.paper.fingerprint import PROJECT_ROOT

PACKAGES = ("numpy", "pandas", "pyarrow")


def _version(dist: str) -> str | None:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def _sha256_of(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def runtime_environment(repo_root: Path | None = None, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """{"python", "numpy", "pandas", "pyarrow", "uv_lock_sha256", "image_os", "image_version"}."""
    root = PROJECT_ROOT if repo_root is None else Path(repo_root)
    environ = os.environ if environ is None else environ
    return {
        "python": platform.python_version(),
        **{name: _version(name) for name in PACKAGES},
        "uv_lock_sha256": _sha256_of(root / "uv.lock"),
        "image_os": environ.get("ImageOS"),
        "image_version": environ.get("ImageVersion"),
    }


def environment_digest(env: Mapping[str, Any]) -> str:
    """sha256 of the sorted, compact JSON of an environment record."""
    blob = json.dumps(dict(env), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
