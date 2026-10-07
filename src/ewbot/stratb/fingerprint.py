"""Strategy-B fingerprints (Plan 4 spec 3, 6.4, 7), in strategy A's format (ewbot.paper.fingerprint.fingerprint_of:
sorted repo-relative paths, each path NUL bytes NUL).

stratb_fingerprint: every *.py file below src/ewbot/stratb. stratb/ is outside A's fingerprinted folders and no
A file is in this set, so either fingerprint can change without the other. Any byte change, addition, removal
or rename below stratb/ changes it; paperb/ and cli.py are not part of it.

deps_fingerprint: the strategy-A side of B's results - every module outside stratb/ and paperb/ that a module
of stratb/ or paperb/ imports, directly or through other modules, with the __init__.py of their packages. The
imports are found statically (ast): every `import ewbot...` and `from ewbot... import ...` (relative imports
too) anywhere in a file, function bodies and TYPE_CHECKING blocks included. It covers A's config defaults,
costs, sizing, breakers, ZigZag, indicators, metrics, buy & hold, walk-forward windows, data hashing and the
paper/ modules paper-B uses; modules B never reaches (live/, cli.py, A's paper runner) are not part of it.
holdout-b.lock binds it (deps_sha256): the holdout and paper-B refuse a change. After the lock, a new import
of another A module from paperb/ changes it too."""

from __future__ import annotations

import ast
from pathlib import Path

from ewbot.paper.fingerprint import PKG_PREFIX, PROJECT_ROOT, fingerprint_of

STRATB_PREFIX = "src/ewbot/stratb/"
B_PACKAGES = ("stratb", "paperb")  # strategy B's packages below src/ewbot: the roots of deps_files


def stratb_files(repo_root: Path | None = None) -> dict[str, bytes]:
    """{"src/ewbot/stratb/<rel>": bytes} for every *.py file below src/ewbot/stratb."""
    pkg = (PROJECT_ROOT if repo_root is None else Path(repo_root)) / "src" / "ewbot" / "stratb"
    return {STRATB_PREFIX + f.relative_to(pkg).as_posix(): f.read_bytes() for f in pkg.rglob("*.py") if f.is_file()}


def stratb_fingerprint(repo_root: Path | None = None) -> str:
    files = stratb_files(repo_root)
    if not files:
        raise FileNotFoundError(f"no strategy-B source files below {repo_root or PROJECT_ROOT}/src/ewbot/stratb")
    return fingerprint_of(files)


def _module_file(pkg: Path, parts: tuple[str, ...]) -> Path | None:
    """The file of module ewbot.<parts> (a .py file or a package's __init__.py), or None."""
    base = pkg.joinpath(*parts)
    for f in ((base.with_name(base.name + ".py"),) if parts else ()) + (base / "__init__.py",):
        if f.is_file():
            return f
    return None


def _imported(f: Path, pkg: Path) -> set[tuple[str, ...]]:
    """Every ewbot module a file may import (its parts below ewbot), wherever the import statement is."""
    here = f.relative_to(pkg).parts[:-1]  # the file's package below ewbot
    out: set[tuple[str, ...]] = set()
    for node in ast.walk(ast.parse(f.read_bytes(), filename=str(f))):
        if isinstance(node, ast.Import):
            out.update(tuple(a.name.split("."))[1:] for a in node.names if a.name.split(".")[0] == "ewbot")
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: from the file's package, one level up per extra dot
                if node.level - 1 > len(here):
                    continue
                mod = here[: len(here) - node.level + 1] + (tuple(node.module.split(".")) if node.module else ())
            elif node.module and node.module.split(".")[0] == "ewbot":
                mod = tuple(node.module.split("."))[1:]
            else:
                continue
            out.add(mod)
            out.update(mod + (a.name,) for a in node.names)  # `from ewbot.x import y` may name module x.y
    return out


def deps_files(repo_root: Path | None = None) -> dict[str, bytes]:
    """{"src/ewbot/<rel>": bytes} of the modules outside stratb/ and paperb/ that B imports (module docstring)."""
    pkg = (PROJECT_ROOT if repo_root is None else Path(repo_root)) / "src" / "ewbot"
    todo = [f for d in B_PACKAGES for f in sorted((pkg / d).rglob("*.py")) if f.is_file()]
    seen = set(todo)
    while todo:
        for parts in _imported(todo.pop(), pkg):
            for k in range(len(parts) + 1):  # the module and every package above it
                g = _module_file(pkg, parts[:k])
                if g is not None and g not in seen:
                    seen.add(g)
                    todo.append(g)
    rels = (f.relative_to(pkg).as_posix() for f in seen)
    return {PKG_PREFIX + r: (pkg / r).read_bytes() for r in rels if r.split("/")[0] not in B_PACKAGES}


def deps_fingerprint(repo_root: Path | None = None) -> str:
    files = deps_files(repo_root)
    if not files:
        raise FileNotFoundError(f"no module imported by strategy B below {repo_root or PROJECT_ROOT}/src/ewbot")
    return fingerprint_of(files)
