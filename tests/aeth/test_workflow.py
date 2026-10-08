"""Static checks of .github/workflows/paper-aeth.yml: the shape of strategy A's paper.yml, nothing of A's or B's."""

from __future__ import annotations

from pathlib import Path

import yaml

import ewbot

ROOT = Path(ewbot.__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "paper-aeth.yml"
SETUP_UV = "astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7"
SECRETS = {"TELEGRAM_BOT_TOKEN": "${{ secrets.TELEGRAM_BOT_TOKEN }}", "TELEGRAM_CHAT_ID": "${{ secrets.TELEGRAM_CHAT_ID }}"}
RUN = 'uv run ewbot aeth paper run --ledger-dir "$PAPER_AETH_DIR"'


def _wf():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return wf, wf["jobs"]["paper-aeth"]["steps"]


def _step(steps, step_id):
    return next(s for s in steps if s.get("id") == step_id)


def test_triggers_concurrency_permissions_runner_and_dormancy():
    wf, _ = _wf()
    assert wf[True] == {"schedule": [{"cron": "42 */3 * * *"}], "workflow_dispatch": None}
    assert wf["concurrency"] == {"group": "paper-aeth", "cancel-in-progress": False}
    job = wf["jobs"]["paper-aeth"]
    assert wf["permissions"] == {"contents": "write"} and job["runs-on"] == "ubuntu-24.04"
    assert job["if"] == "vars.PAPER_AETH_ENABLED == 'true'"  # dormant until a passed ETH holdout


def test_steps_run_the_aeth_runtime_and_its_sync_on_the_pinned_python():
    wf, steps = _wf()
    assert [s["uses"] for s in steps if "uses" in s] == ["actions/checkout@v7", SETUP_UV, "actions/cache@v6"]
    assert steps[0]["with"] == {"ref": "master"}
    assert next(s for s in steps if s.get("uses") == SETUP_UV)["with"]["python-version"] == "3.12.13"
    assert _step(steps, "paper-aeth")["run"] == RUN
    assert _step(steps, "push")["run"] == f"uv run python -m ewbot.aeth.sync push {RUN}"
    assert "uv run python -m ewbot.aeth.sync checkout" in _step(steps, "checkout-paper-aeth")["run"]
    cache = next(s for s in steps if s.get("uses") == "actions/cache@v6")["with"]
    assert cache["path"] == "data/cache" and cache["restore-keys"].strip() == "eth-usd-1h-"
    budget = sum(s.get("timeout-minutes", 0) for s in steps)
    assert wf["jobs"]["paper-aeth"]["timeout-minutes"] >= budget + 10


def test_secrets_reach_only_the_steps_that_send_and_the_alert_has_the_a_eth_prefix():
    _, steps = _wf()
    with_secrets = [s.get("id") for s in steps if "secrets." in yaml.safe_dump(s)]
    assert with_secrets == ["paper-aeth", "push", "alert"]
    for i in with_secrets:
        assert {k: v for k, v in _step(steps, i)["env"].items() if "secrets." in v} == SECRETS
    alert = _step(steps, "alert")
    assert alert["if"] == "failure() || cancelled()" and "[ewbot paper-A-ETH] 실패" in alert["run"]


def test_nothing_of_strategy_a_or_b_is_referenced():
    text = WORKFLOW.read_text(encoding="utf-8")
    for other in ("paper_sync.sh", "btc-usd-1h", "stratb-15m", "ewbot paper run", "ewbot stratb", "paperb",
                  "group: paper\n", "group: paper-b\n", "holdout.lock", "holdout-b.lock", "PAPER_B_ENABLED"):
        assert other not in text
