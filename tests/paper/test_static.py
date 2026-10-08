"""Static guards: who may run into the holdout, and the shape of the paper workflow."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

import ewbot

SRC = Path(ewbot.__file__).resolve().parent  # src/ewbot
ROOT = SRC.parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "paper.yml"
CI_WORKFLOW = WORKFLOWS / "ci.yml"
PYTHON_PATCH = "3.12.13"  # the exact patch both workflows resolve; part of the compared environment
RUNNER_IMAGE = "ubuntu-24.04"  # pinned while paper trading runs (spec 8): part of the recorded environment
ALLOWED_TRUE = {"backtest/holdout.py", "paper/runner.py", "live/target.py"}  # Plan 2 spec 7.1, Plan 3 spec 3.1
PASS_THROUGH = {"backtest/runner.py"}  # forwards its own allow_holdout argument to the engine run
# backtest/runner.py helpers that skip the holdout check or forward allow_holdout unchecked
GUARDED = {"check_before_holdout", "_map_tasks", "_init_worker"}
# the only allow_holdout parameters without a default: runner's private pool helpers, always passed explicitly
REQUIRED_ALLOW = {("backtest/runner.py", "_init_worker"), ("backtest/runner.py", "_map_tasks")}
SETUP_UV = "astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7"  # v10.2.0
SECRET_ENV = {
    "TELEGRAM_BOT_TOKEN": "${{ secrets.TELEGRAM_BOT_TOKEN }}",
    "TELEGRAM_CHAT_ID": "${{ secrets.TELEGRAM_CHAT_ID }}",
}
RUN = 'uv run ewbot paper run --ledger-dir "$PAPER_DIR"'


def _modules():
    for f in sorted(SRC.rglob("*.py")):
        yield f.relative_to(SRC).as_posix(), ast.parse(f.read_text(encoding="utf-8"), filename=str(f))


def _nodes():
    for rel, tree in _modules():
        for node in ast.walk(tree):
            yield rel, node


def test_only_the_holdout_and_the_paper_runner_pass_allow_holdout_true():
    literal_true, other = set(), set()
    for rel, node in _nodes():
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "allow_holdout":
                    if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        literal_true.add(rel)
                    elif not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                        other.add(rel)
    assert literal_true == ALLOWED_TRUE
    assert other <= PASS_THROUGH


def _allow_holdout_params(tree: ast.AST) -> tuple[list[int], set[str]]:
    """(lines of allow_holdout parameters whose default is not the literal False or that are *args /
    **kwargs, names of the functions taking allow_holdout without a default)."""
    bad, required = [], set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        a = fn.args
        positional = a.posonlyargs + a.args
        defaults = [None] * (len(positional) - len(a.defaults)) + list(a.defaults)
        pairs = list(zip(positional, defaults)) + list(zip(a.kwonlyargs, a.kw_defaults))
        bad += [s.lineno for s in (a.vararg, a.kwarg) if s is not None and s.arg == "allow_holdout"]
        for param, default in pairs:
            if param.arg != "allow_holdout":
                continue
            if default is None:
                required.add(getattr(fn, "name", "<lambda>"))
            elif not (isinstance(default, ast.Constant) and default.value is False):
                bad.append(param.lineno)
    return bad, required


@pytest.mark.parametrize(
    "src",
    [
        "def f(allow_holdout=True): ...",
        "def f(*, allow_holdout=None): ...",
        "def f(x, allow_holdout=1, /): ...",
        "async def f(*, allow_holdout=bool(1)): ...",
        "g = lambda allow_holdout=True: 0",
        "def f(**allow_holdout): ...",
    ],
)
def test_the_parameter_guard_catches_a_non_false_default(src):
    assert _allow_holdout_params(ast.parse(src))[0]


def test_allow_holdout_parameters_default_to_false():
    required = set()
    for rel, tree in _modules():
        bad, names = _allow_holdout_params(tree)
        assert not bad, (rel, bad)
        required |= {(rel, name) for name in names}
    assert required <= REQUIRED_ALLOW


def _holdout_back_doors(tree: ast.AST) -> list[tuple[int, str]]:
    """References outside backtest/runner.py that could reach the holdout without allow_holdout=True:
    an "allow_holdout" string (`**{"allow_holdout": True}`), and any import, attribute, name or getattr
    string of the GUARDED runner helpers. A module may define and call its own helper of the same name
    (walkforward.py has its own _init_worker, which takes no allow_holdout)."""
    own = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    found = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value == "allow_holdout" or node.value in GUARDED:
                found.append((line, f"string {node.value!r}"))
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[-1] == "runner" and any(a.name == "*" for a in node.names):
                found.append((line, "import * from a runner module"))
            found += [(line, f"import {a.name}") for a in node.names if a.name in GUARDED]
        elif isinstance(node, ast.Attribute) and node.attr in GUARDED:
            found.append((line, f"attribute .{node.attr}"))
        elif isinstance(node, ast.Name) and node.id in GUARDED and node.id not in own:
            found.append((line, f"name {node.id}"))
    return found


@pytest.mark.parametrize(
    "src",
    [
        'run_backtest(cfg, df, **{"allow_holdout": True})',
        "from ewbot.backtest.runner import check_before_holdout",
        "from .runner import _init_worker as init",
        "from ewbot.backtest.runner import *",
        "from ewbot.backtest import runner\nrunner._map_tasks([], 1, cfg, df, 0, 0, True)",
        "import ewbot.backtest.runner as r\ngetattr(r, '_init_worker')(cfg, df, 0, 0, True)",
        "check_before_holdout(df, None, True)",
    ],
)
def test_the_back_door_guard_catches(src):
    assert _holdout_back_doors(ast.parse(src))


def test_the_back_door_guard_allows_a_module_s_own_helper_of_the_same_name():
    assert _holdout_back_doors(ast.parse("def _init_worker(cfg): ...\n_init_worker(cfg)")) == []


def test_no_back_doors_into_the_holdout():
    for rel, tree in _modules():
        if rel not in PASS_THROUGH:
            assert _holdout_back_doors(tree) == [], rel


def _workflow():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return wf, wf["jobs"]["paper"]["steps"]


def _step(steps, step_id):
    return next(s for s in steps if s.get("id") == step_id)


def test_the_repository_has_exactly_the_paper_ci_and_paper_b_workflows():
    assert sorted(p.name for p in WORKFLOWS.iterdir()) == ["ci.yml", "paper-b.yml", "paper.yml"]


def test_strategy_b_code_is_outside_the_strategy_a_fingerprint():
    """Plan 4 spec 2, 9 (A protection, run by ci.yml): strategy B lives in stratb/ and paperb/ only, and
    none of its files is in A's fingerprinted set (A's fingerprint itself is pinned in test_fingerprint.py)."""
    from ewbot.paper.fingerprint import is_fingerprinted, tree_files

    b_files = [f.relative_to(SRC).as_posix() for d in ("stratb", "paperb") for f in (SRC / d).rglob("*.py")]
    assert b_files and not [r for r in b_files if is_fingerprinted(r)]
    assert not [p for p in tree_files() if p.startswith(("src/ewbot/stratb/", "src/ewbot/paperb/"))]


def test_aeth_code_is_outside_the_strategy_a_fingerprint():
    """Plan 5a spec 3, 11 (A protection, run by ci.yml): A-ETH lives in aeth/ only, and none of its files is in
    A's fingerprinted set (A's fingerprint itself is pinned in test_fingerprint.py)."""
    from ewbot.paper.fingerprint import is_fingerprinted, tree_files

    files = [f.relative_to(SRC).as_posix() for f in (SRC / "aeth").rglob("*.py")]
    assert files and not [r for r in files if is_fingerprinted(r)]
    assert not [p for p in tree_files() if p.startswith("src/ewbot/aeth/")]


def test_the_paper_b_workflow_never_touches_strategy_a():
    text = (WORKFLOWS / "paper-b.yml").read_text(encoding="utf-8")
    assert yaml.safe_load(text)["concurrency"]["group"] == "paper-b"
    for a_thing in ("paper_sync.sh", "btc-usd-1h", "ewbot paper run", "holdout.lock"):
        assert a_thing not in text


def test_workflow_triggers_concurrency_and_permissions():
    wf, _ = _workflow()
    on = wf[True]  # YAML 1.1 reads the bare key `on` as boolean True
    assert on["schedule"] == [{"cron": "52 */3 * * *"}] and "workflow_dispatch" in on
    assert wf["concurrency"] == {"group": "paper", "cancel-in-progress": False}
    assert wf["permissions"] == {"contents": "write"}
    assert "permissions" not in wf["jobs"]["paper"]
    assert wf["jobs"]["paper"]["runs-on"] == RUNNER_IMAGE  # never ubuntu-latest: no silent OS upgrade


def test_telegram_secrets_reach_only_the_steps_that_can_send():
    wf, steps = _workflow()
    assert "secrets." not in yaml.safe_dump(wf["jobs"]["paper"].get("env", {}))
    with_secrets = [s.get("id") for s in steps if "secrets." in yaml.safe_dump(s)]
    assert with_secrets == ["paper", "push", "alert"]  # the run, the push step's recompute, the alert
    for i in with_secrets:
        assert {k: v for k, v in _step(steps, i)["env"].items() if "secrets." in v} == SECRET_ENV, i
    assert WORKFLOW.read_text(encoding="utf-8").count("secrets.") == 2 * len(with_secrets)


def test_workflow_steps():
    wf, steps = _workflow()
    text = WORKFLOW.read_text(encoding="utf-8")
    uses = [s["uses"] for s in steps if "uses" in s]
    assert uses == ["actions/checkout@v7", SETUP_UV, "actions/cache@v6"]
    assert f"uses: {SETUP_UV} # v10.2.0" in text  # third-party action pinned to a commit
    assert steps[0]["with"] == {"ref": "master"}  # shallow: the paper history is never fetched in full
    runs = [s.get("run", "") for s in steps]
    checkout = _step(steps, "checkout-paper")["run"]
    assert "git fetch --no-tags --depth=1 origin 64e628ad4865b3b04571a7cba2886881eae19176" in checkout  # baseline
    assert 'PAPER_DIR="$RUNNER_TEMP/paper" bash scripts/paper_sync.sh checkout' in checkout
    assert "uv sync --frozen" in runs
    paper, push = _step(steps, "paper"), _step(steps, "push")
    assert paper["run"] == RUN
    assert push["run"] == f"bash scripts/paper_sync.sh push {RUN}"  # recomputes only when origin/paper moved
    # a hang becomes a step failure (and an alert) well before the job-level backstop
    assert paper["timeout-minutes"] == 35 and push["timeout-minutes"] == 35
    step_budget = sum(s.get("timeout-minutes", 0) for s in steps)
    assert wf["jobs"]["paper"]["timeout-minutes"] >= step_budget + 10
    cache = next(s for s in steps if s.get("uses") == "actions/cache@v6")["with"]
    assert cache["path"] == "data/cache" and cache["restore-keys"].strip() == "btc-usd-1h-"


def test_failure_alert_step():
    _, steps = _workflow()
    alert = steps[-1]
    assert alert["id"] == "alert" and alert["if"] == "failure() || cancelled()"  # timeouts cancel
    run = alert["run"]
    assert "EWBOT_FAILURE_MARKER" in run and "sendMessage" in run
    assert "curl" in run and "--fail-with-body" in run and "/dev/null" not in run  # keep Telegram's error
    assert "set -x" not in run and "echo \"${TELEGRAM_BOT_TOKEN" not in run and "echo $TELEGRAM" not in run


def test_failure_alert_curl_finishes_within_its_step_timeout():
    """curl --max-time bounds each attempt; --retry N adds N attempts after waits of 1, 2, 4, ... s.
    The worst case must end before the step timeout, or the alert step itself is killed silently."""
    _, steps = _workflow()
    alert = _step(steps, "alert")
    retries = int(re.search(r"--retry (\d+)", alert["run"]).group(1))
    max_time = int(re.search(r"--max-time (\d+)", alert["run"]).group(1))
    worst = (retries + 1) * max_time + sum(2**i for i in range(retries))
    assert (retries, max_time, alert["timeout-minutes"]) == (3, 20, 2)
    assert worst < alert["timeout-minutes"] * 60


def test_ci_workflow_runs_the_fingerprint_and_static_guards_without_secrets():
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    on = wf[True]
    assert on == {"pull_request": {"branches": ["master"]}, "push": {"branches": ["master"]}}
    assert wf["permissions"] == {"contents": "read"} and "secrets." not in text
    (job,) = wf["jobs"].values()
    assert job["runs-on"] == RUNNER_IMAGE and job["timeout-minutes"] <= 15
    uses = [s["uses"] for s in job["steps"] if "uses" in s]
    assert uses == ["actions/checkout@v7", SETUP_UV] and f"uses: {SETUP_UV} # v10.2.0" in text
    runs = [s["run"] for s in job["steps"] if "run" in s]
    assert runs == ["uv sync --frozen", "uv run pytest -q tests/paper/test_fingerprint.py tests/paper/test_static.py"]


def test_both_workflows_pin_the_exact_python_patch():
    """Python is part of the compared environment, so setup-uv gets the full patch, not "3.12"."""
    for path in (WORKFLOW, CI_WORKFLOW):
        wf = yaml.safe_load(path.read_text(encoding="utf-8"))
        pins = [s["with"]["python-version"] for job in wf["jobs"].values() for s in job["steps"]
                if str(s.get("uses", "")).startswith(SETUP_UV)]
        assert pins == [PYTHON_PATCH], path.name
