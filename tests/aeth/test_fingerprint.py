from __future__ import annotations

import shutil

import pytest

from ewbot.aeth.fingerprint import (
    AETH_PREFIX,
    aeth_files,
    aeth_fingerprint,
    current_epoch_fingerprint,
    epoch_fingerprint,
)
from ewbot.paper.fingerprint import PROJECT_ROOT, current_fingerprint, fingerprint_of


def _copy_src(tmp_path):
    shutil.copytree(PROJECT_ROOT / "src" / "ewbot", tmp_path / "src" / "ewbot")
    return tmp_path


def test_aeth_files_are_every_py_file_below_aeth():
    files = aeth_files()
    pkg = PROJECT_ROOT / "src" / "ewbot" / "aeth"
    assert set(files) == {AETH_PREFIX + f.relative_to(pkg).as_posix() for f in pkg.rglob("*.py")}
    assert aeth_fingerprint() == fingerprint_of(files)


def test_an_aeth_edit_changes_the_aeth_and_epoch_fingerprints_but_not_strategy_a_s(tmp_path):
    root = _copy_src(tmp_path)
    a0, e0, ep0 = current_fingerprint(root), aeth_fingerprint(root), current_epoch_fingerprint(root)
    cfg = root / "src/ewbot/aeth/config.py"
    cfg.write_bytes(cfg.read_bytes() + b"# one comment line\n")
    assert current_fingerprint(root) == a0
    assert aeth_fingerprint(root) != e0 and current_epoch_fingerprint(root) != ep0


def test_a_strategy_a_edit_changes_the_epoch_fingerprint_but_not_aeth_s(tmp_path):
    root = _copy_src(tmp_path)
    e0, ep0 = aeth_fingerprint(root), current_epoch_fingerprint(root)
    loop = root / "src/ewbot/engine/loop.py"
    loop.write_bytes(loop.read_bytes() + b"# one comment line\n")
    assert aeth_fingerprint(root) == e0 and current_epoch_fingerprint(root) != ep0


def test_files_outside_both_change_neither(tmp_path):
    root = _copy_src(tmp_path)
    ep0 = current_epoch_fingerprint(root)
    for rel in ("cli.py", "live/app.py", "backtest/runner.py", "paper/runner.py"):
        (root / "src/ewbot" / rel).write_text("# tweak\n", encoding="utf-8")
    assert current_epoch_fingerprint(root) == ep0


def test_epoch_fingerprint_is_the_ordered_pair():
    assert epoch_fingerprint("a", "b") != epoch_fingerprint("b", "a")
    assert current_epoch_fingerprint() == epoch_fingerprint(current_fingerprint(), aeth_fingerprint())


def test_no_aeth_files_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        aeth_fingerprint(tmp_path)
