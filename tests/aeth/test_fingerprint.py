from __future__ import annotations

import ast
import shutil

import pytest

from ewbot.aeth.fingerprint import (
    AETH_DECISION_FILES,
    AETH_PREFIX,
    aeth_decision_files,
    aeth_decision_fingerprint,
    aeth_files,
    aeth_fingerprint,
    current_epoch_fingerprint,
    epoch_fingerprint,
)
from ewbot.paper.fingerprint import PROJECT_ROOT, current_fingerprint, fingerprint_of

AETH = "src/ewbot/aeth"
# Every A-ETH module that is not a decision file: runtime (paper, sync), the lock checks (lockfile, holdout),
# stage 1, the fingerprints themselves and the package marker.
RUNTIME = ("paper.py", "sync.py", "holdout.py", "lockfile.py", "stage1.py", "fingerprint.py", "__init__.py")


def _copy_src(tmp_path):
    """A copy of src/ewbot with both decision files (a stand-in data.py while the plan has not added it: Task 2)."""
    shutil.copytree(PROJECT_ROOT / "src" / "ewbot", tmp_path / "src" / "ewbot")
    for name in AETH_DECISION_FILES:
        p = tmp_path / AETH / name
        if not p.exists():
            p.write_text("# stand-in\n", encoding="utf-8")
    return tmp_path


def _touch(root, rel):
    """Append one comment line to <root>/src/ewbot/<rel> (the file is created when it does not exist yet)."""
    p = root / "src/ewbot" / rel
    p.write_bytes((p.read_bytes() if p.exists() else b"") + b"# one comment line\n")


def _all(root):
    """(strategy A's, A-ETH's, A-ETH's decision and the epoch fingerprint) of the tree at root."""
    return (current_fingerprint(root), aeth_fingerprint(root), aeth_decision_fingerprint(root),
            current_epoch_fingerprint(root))


def test_aeth_files_are_every_py_file_below_aeth():
    files = aeth_files()
    pkg = PROJECT_ROOT / "src" / "ewbot" / "aeth"
    assert set(files) == {AETH_PREFIX + f.relative_to(pkg).as_posix() for f in pkg.rglob("*.py")}
    assert aeth_fingerprint() == fingerprint_of(files)


def test_the_decision_files_are_config_and_data(tmp_path):
    root = _copy_src(tmp_path)
    assert AETH_DECISION_FILES == ("config.py", "data.py")
    files = aeth_decision_files(root)
    assert set(files) == {f"{AETH}/config.py", f"{AETH}/data.py"}
    assert files.items() <= aeth_files(root).items()
    assert aeth_decision_fingerprint(root) == fingerprint_of(files)


@pytest.mark.parametrize("name", AETH_DECISION_FILES)
def test_a_decision_file_edit_changes_both_aeth_fingerprints_and_the_epoch_but_not_strategy_a_s(tmp_path, name):
    root = _copy_src(tmp_path)
    a0, e0, d0, ep0 = _all(root)
    _touch(root, f"aeth/{name}")
    a1, e1, d1, ep1 = _all(root)
    assert a1 == a0 and e1 != e0 and d1 != d0 and ep1 != ep0


@pytest.mark.parametrize("name", RUNTIME)
def test_a_runtime_edit_changes_the_aeth_fingerprint_but_not_the_epoch(tmp_path, name):
    """Spec 8 (amended 2026-10-08): a runtime fix during paper trading keeps the paper epoch and the ETH count;
    stage 1, the lock and the holdout still bind every A-ETH file."""
    root = _copy_src(tmp_path)
    a0, e0, d0, ep0 = _all(root)
    _touch(root, f"aeth/{name}")
    a1, e1, d1, ep1 = _all(root)
    assert a1 == a0 and e1 != e0 and d1 == d0 and ep1 == ep0


def test_a_strategy_a_edit_changes_the_epoch_fingerprint_but_not_aeth_s(tmp_path):
    root = _copy_src(tmp_path)
    _, e0, d0, ep0 = _all(root)
    _touch(root, "engine/loop.py")
    _, e1, d1, ep1 = _all(root)
    assert e1 == e0 and d1 == d0 and ep1 != ep0


def test_files_outside_both_change_neither(tmp_path):
    root = _copy_src(tmp_path)
    ep0 = current_epoch_fingerprint(root)
    for rel in ("cli.py", "live/app.py", "backtest/runner.py", "paper/runner.py"):
        (root / "src/ewbot" / rel).write_text("# tweak\n", encoding="utf-8")
    assert current_epoch_fingerprint(root) == ep0


def test_epoch_fingerprint_is_the_ordered_pair_of_a_s_and_the_decision_fingerprint(tmp_path):
    root = _copy_src(tmp_path)
    assert epoch_fingerprint("a", "b") != epoch_fingerprint("b", "a")
    assert current_epoch_fingerprint(root) == epoch_fingerprint(current_fingerprint(root),
                                                                aeth_decision_fingerprint(root))


def test_no_aeth_files_or_a_missing_decision_file_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        aeth_fingerprint(tmp_path)
    root = _copy_src(tmp_path / "copy")
    (root / AETH / "data.py").unlink()
    with pytest.raises(FileNotFoundError, match="data.py"):
        aeth_decision_fingerprint(root)


def test_the_decision_files_take_from_a_eth_only_decision_files_and_the_lock_check():
    """No decision logic can sit in an A-ETH module outside the decision fingerprint: config.py and data.py import
    from ewbot.aeth only each other and lockfile (lock_exists lets the bars be read or refuses; it never changes
    them)."""
    allowed = {f"ewbot.aeth.{name[:-3]}" for name in AETH_DECISION_FILES} | {"ewbot.aeth.lockfile"}
    for name in AETH_DECISION_FILES:
        p = PROJECT_ROOT / AETH / name
        if not p.exists():  # data.py arrives in Task 2
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level], name
        assert {m for m in mods if m.startswith("ewbot.aeth")} <= allowed, name
