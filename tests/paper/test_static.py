"""Static guards: who may run into the holdout, and the shape of the paper workflow."""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

import ewbot

SRC = Path(ewbot.__file__).resolve().parent  # src/ewbot
ROOT = SRC.parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "paper.yml"
ALLOWED_TRUE = {"backtest/holdout.py", "paper/runner.py"}  # Plan 2 spec 7.1
PASS_THROUGH = {"backtest/runner.py"}  # forwards its own allow_holdout argument to the engine run


def _calls():
    for f in sorted(SRC.rglob("*.py")):
        rel = f.relative_to(SRC).as_posix()
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            yield rel, node


def test_only_the_holdout_and_the_paper_runner_pass_allow_holdout_true():
    literal_true, other = set(), set()
    for rel, node in _calls():
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "allow_holdout":
                    if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        literal_true.add(rel)
                    elif not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                        other.add(rel)
    assert literal_true == ALLOWED_TRUE
    assert other <= PASS_THROUGH


def test_no_back_doors_into_the_holdout():
    """No `**{"allow_holdout": True}` and no direct check_before_holdout call outside backtest/runner.py."""
    for rel, node in _calls():
        if isinstance(node, ast.Constant) and node.value == "allow_holdout":
            assert rel in PASS_THROUGH, rel
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if name == "check_before_holdout":
                assert rel in PASS_THROUGH, rel


def _steps():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return wf, wf["jobs"]["paper"]["steps"]


def test_workflow_triggers_concurrency_and_permissions():
    wf, _ = _steps()
    on = wf[True]  # YAML 1.1 reads the bare key `on` as boolean True
    assert on["schedule"] == [{"cron": "7 * * * *"}] and "workflow_dispatch" in on
    assert wf["concurrency"] == {"group": "paper", "cancel-in-progress": False}
    assert wf["permissions"] == {"contents": "write"}
    env = wf["jobs"]["paper"]["env"]
    assert env["TELEGRAM_BOT_TOKEN"] == "${{ secrets.TELEGRAM_BOT_TOKEN }}"
    assert env["TELEGRAM_CHAT_ID"] == "${{ secrets.TELEGRAM_CHAT_ID }}"


def test_workflow_steps():
    _, steps = _steps()
    uses = [s["uses"] for s in steps if "uses" in s]
    assert uses == ["actions/checkout@v7", "astral-sh/setup-uv@v10.2.0", "actions/cache@v6"]
    assert steps[0]["with"] == {"ref": "master"}  # shallow: the paper history is never fetched in full
    runs = [s.get("run", "") for s in steps]
    joined = "\n".join(runs)
    assert "git fetch --no-tags --depth=1 origin 64e628ad4865b3b04571a7cba2886881eae19176" in joined  # baseline
    assert "git worktree add --orphan -b paper" in joined and "git ls-remote --exit-code --heads origin paper" in joined
    assert "uv sync --frozen" in runs
    paper = next(s for s in steps if s.get("id") == "paper")
    assert paper["run"] == 'uv run ewbot paper run --ledger-dir "$PAPER_DIR"'
    push = next(r for r in runs if "git -C \"$PAPER_DIR\" push" in r)
    assert "for attempt in 1 2 3" in push and "reset -q --hard origin/paper" in push and "ewbot paper run" in push
    cache = next(s for s in steps if s.get("uses") == "actions/cache@v6")["with"]
    assert cache["path"] == "data/cache" and cache["restore-keys"].strip() == "btc-usd-1h-"
    last = steps[-1]
    assert last["if"] == "failure()" and "EWBOT_FAILURE_MARKER" in last["run"] and "sendMessage" in last["run"]
