from __future__ import annotations

import shutil

import pytest

from ewbot.paper.fingerprint import PROJECT_ROOT, current_fingerprint
from ewbot.stratb.fingerprint import STRATB_PREFIX, stratb_files, stratb_fingerprint


def _copy(tmp_path):
    shutil.copytree(PROJECT_ROOT / "src" / "ewbot", tmp_path / "src" / "ewbot",
                    ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


def test_the_file_set_is_every_python_file_below_stratb():
    files = stratb_files()
    pkg = PROJECT_ROOT / "src" / "ewbot" / "stratb"
    assert set(files) == {STRATB_PREFIX + f.relative_to(pkg).as_posix() for f in pkg.rglob("*.py")}
    assert "src/ewbot/stratb/config.py" in files and all(k.startswith(STRATB_PREFIX) for k in files)


def test_an_edit_an_addition_and_a_removal_change_it(tmp_path):
    root = _copy(tmp_path)
    base = stratb_fingerprint(root)
    assert base == stratb_fingerprint(PROJECT_ROOT)
    cfg = root / "src/ewbot/stratb/config.py"
    original = cfg.read_bytes()
    cfg.write_bytes(original + b"# comment\n")
    assert stratb_fingerprint(root) != base
    cfg.write_bytes(original)
    assert stratb_fingerprint(root) == base
    (root / "src/ewbot/stratb/extra.py").write_text("x = 1\n", encoding="utf-8")
    assert stratb_fingerprint(root) != base
    (root / "src/ewbot/stratb/extra.py").unlink()
    (root / "src/ewbot/stratb/fingerprint.py").unlink()
    assert stratb_fingerprint(root) != base


def test_strategy_a_and_b_fingerprints_are_independent(tmp_path):
    root = _copy(tmp_path)
    a0, b0 = current_fingerprint(root), stratb_fingerprint(root)
    (root / "src/ewbot/stratb/config.py").write_text("# changed\n", encoding="utf-8")
    b1 = stratb_fingerprint(root)
    assert current_fingerprint(root) == a0 and b1 != b0
    (root / "src/ewbot/paper/runner.py").write_text("# changed\n", encoding="utf-8")
    loop = root / "src/ewbot/engine/loop.py"
    loop.write_bytes(loop.read_bytes() + b"# one line\n")
    assert current_fingerprint(root) != a0 and stratb_fingerprint(root) == b1


def test_an_empty_tree_has_no_fingerprint(tmp_path):
    with pytest.raises(FileNotFoundError):
        stratb_fingerprint(tmp_path)
