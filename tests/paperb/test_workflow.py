"""Static checks of .github/workflows/paper-b.yml: the shape of strategy A's paper.yml, and nothing of A's."""

from __future__ import annotations

from pathlib import Path

import yaml

import ewbot

ROOT = Path(ewbot.__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "paper-b.yml"
SETUP_UV = "astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7"
SECRETS = {"TELEGRAM_BOT_TOKEN": "${{ secrets.TELEGRAM_BOT_TOKEN }}", "TELEGRAM_CHAT_ID": "${{ secrets.TELEGRAM_CHAT_ID }}"}
RUN = 'uv run ewbot stratb paper run --ledger-dir "$PAPER_B_DIR"'


def _wf():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return wf, wf["jobs"]["paper-b"]["steps"]


def _step(steps, step_id):
    return next(s for s in steps if s.get("id") == step_id)


def test_triggers_concurrency_permissions_and_runner():
    wf, _ = _wf()
    assert wf[True] == {"schedule": [{"cron": "47 */3 * * *"}], "workflow_dispatch": None}
    assert wf["concurrency"] == {"group": "paper-b", "cancel-in-progress": False}
    assert wf["permissions"] == {"contents": "write"} and wf["jobs"]["paper-b"]["runs-on"] == "ubuntu-24.04"
    assert wf["jobs"]["paper-b"]["if"] == "vars.PAPER_B_ENABLED == 'true'"  # dormant until a passed holdout


def test_steps_run_the_b_runtime_and_its_sync():
    wf, steps = _wf()
    assert [s["uses"] for s in steps if "uses" in s] == ["actions/checkout@v7", SETUP_UV, "actions/cache@v6"]
    assert steps[0]["with"] == {"ref": "master"}
    assert _step(steps, "paper-b")["run"] == RUN
    assert _step(steps, "push")["run"] == f"uv run python -m ewbot.paperb.sync push {RUN}"
    assert "uv run python -m ewbot.paperb.sync checkout" in _step(steps, "checkout-paper-b")["run"]
    cache = next(s for s in steps if s.get("uses") == "actions/cache@v6")["with"]
    assert cache["path"] == "data/cache" and cache["restore-keys"].strip() == "stratb-15m-"
    budget = sum(s.get("timeout-minutes", 0) for s in steps)
    assert wf["jobs"]["paper-b"]["timeout-minutes"] >= budget + 10


def test_secrets_reach_only_the_steps_that_send_and_the_alert_has_the_b_prefix():
    _, steps = _wf()
    with_secrets = [s.get("id") for s in steps if "secrets." in yaml.safe_dump(s)]
    assert with_secrets == ["paper-b", "push", "alert"]
    for i in with_secrets:
        assert {k: v for k, v in _step(steps, i)["env"].items() if "secrets." in v} == SECRETS
    alert = _step(steps, "alert")
    assert alert["if"] == "failure() || cancelled()" and "[ewbot paper-B] 실패" in alert["run"]


def test_nothing_of_strategy_a_is_referenced():
    text = WORKFLOW.read_text(encoding="utf-8")
    for a_thing in ("paper_sync.sh", "btc-usd-1h", "ewbot paper run", "group: paper\n", "refs/heads/paper\n", "holdout.lock"):
        assert a_thing not in text
