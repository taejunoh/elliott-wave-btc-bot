from __future__ import annotations

import shutil

import pytest

from ewbot.paper.fingerprint import PROJECT_ROOT, current_fingerprint, fingerprint_of
from ewbot.stratb.fingerprint import (
    STRATB_PREFIX,
    deps_files,
    deps_fingerprint,
    stratb_files,
    stratb_fingerprint,
)


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


def _tree(root, files):
    for rel, text in files.items():
        f = root / "src" / "ewbot" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return root


def test_deps_are_the_modules_b_imports_directly_or_through_others(tmp_path):
    root = _tree(tmp_path, {
        "__init__.py": "", "config.py": "", "cli.py": "from ewbot.stratb import x\n",
        "stratb/__init__.py": "", "stratb/y.py": "def f():\n    from ewbot.paper import ledger\n",
        "stratb/x.py": "from ewbot.backtest.metrics import m\nimport ewbot.risk.sizing\nfrom ewbot.stratb import y\n",
        "paperb/__init__.py": "from ewbot.config import C\n",
        "backtest/__init__.py": "", "backtest/metrics.py": "from ewbot.engine.loop import E\nfrom . import report\n",
        "backtest/report.py": "", "backtest/unused.py": "", "engine/__init__.py": "", "engine/loop.py": "",
        "risk/__init__.py": "", "risk/sizing.py": "", "paper/__init__.py": "", "paper/ledger.py": "",
        "paper/runner.py": "", "live/__init__.py": "", "live/app.py": "from ewbot.stratb import x\n",
    })
    files = deps_files(root)
    assert sorted(files) == ["src/ewbot/" + r for r in (
        "__init__.py", "backtest/__init__.py", "backtest/metrics.py", "backtest/report.py", "config.py",
        "engine/__init__.py", "engine/loop.py", "paper/__init__.py", "paper/ledger.py", "risk/__init__.py",
        "risk/sizing.py")]
    base = deps_fingerprint(root)
    assert base == fingerprint_of(files)
    for rel in ("cli.py", "live/app.py", "backtest/unused.py", "paper/runner.py", "stratb/y.py"):
        f = root / "src" / "ewbot" / rel
        f.write_text(f.read_text(encoding="utf-8") + "# not imported, or B's own\n", encoding="utf-8")
    assert deps_fingerprint(root) == base
    loop = root / "src/ewbot/engine/loop.py"
    loop.write_text("E = 2\n", encoding="utf-8")
    assert deps_fingerprint(root) != base


def test_the_real_deps_are_strategy_a_modules_only():
    files = deps_files()
    assert {"src/ewbot/config.py", "src/ewbot/paper/fingerprint.py"} <= set(files)
    assert not [p for p in files if p.startswith(("src/ewbot/stratb/", "src/ewbot/paperb/", "src/ewbot/live/"))]
    assert "src/ewbot/cli.py" not in files and deps_fingerprint() == fingerprint_of(files)
