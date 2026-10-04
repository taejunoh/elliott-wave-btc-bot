"""Static checks of deploy/live and a stubbed run of deploy.sh (fake git / docker on PATH, no daemon)."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

import ewbot
from ewbot.live.settings import load_settings

ROOT = Path(ewbot.__file__).resolve().parents[2]
LIVE = ROOT / "deploy" / "live"
DOC = ROOT / "docs" / "live-trading.md"


def test_dockerfile_pins_the_runtime_and_ships_no_secret():
    text = (LIVE / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^FROM python:3\.12\.13-slim-bookworm@sha256:[0-9a-f]{64}$", text, re.M)
    assert re.search(r"^COPY --from=ghcr\.io/astral-sh/uv:0\.12\.13@sha256:[0-9a-f]{64} /uv /bin/uv$", text, re.M)
    assert text.count("uv sync --frozen --no-dev --group live") == 2
    copies = re.findall(r"^COPY (?!--from)(.+)$", text, re.M)
    assert copies == ["pyproject.toml uv.lock ./", "src ./src", "holdout.lock ./"]
    assert "USER ewbot" in text and '"live", "status", "--max-age", "180"' in text
    assert "RUN test -s /etc/ssl/certs/ca-certificates.crt" in text  # verified TLS needs the CA store
    assert 'ENTRYPOINT ["ewbot", "live"]' in text and 'CMD ["run"]' in text
    assert "STOPSIGNAL SIGTERM" in text  # `docker stop`: the runtime finishes its tick and exits 0
    assert not re.search(r"^ADD ", text, re.M)
    assert not any(secret in c for c in copies for secret in ("cdp_api_key", "live.env", ".env", "/etc/ewbot"))


def test_the_build_context_is_an_allowlist():
    lines = [x for x in (LIVE / "Dockerfile.dockerignore").read_text().splitlines() if x and not x.startswith("#")]
    assert lines[0] == "*" and set(lines[1:5]) == {"!pyproject.toml", "!uv.lock", "!holdout.lock", "!src/"}


def test_compose_service_restarts_mounts_secrets_read_only_and_holds_no_secret_value():
    svc = yaml.safe_load((LIVE / "docker-compose.yml").read_text())["services"]["live"]
    assert svc["restart"] == "unless-stopped" and svc["read_only"] is True and svc["user"] == "1000:1000"
    # an init as PID 1 hands SIGTERM to python, which then has time to finish its tick and flush Telegram
    assert svc["init"] is True and svc["stop_grace_period"] == "150s"
    assert svc["image"] == "${EWBOT_IMAGE:-ewbot-live:current}"
    assert set(svc["volumes"]) == {"/var/lib/ewbot:/data", "/etc/ewbot:/run/ewbot:ro"}
    env = svc["environment"]
    assert env["EWBOT_SECRETS_FILE"] == "/run/ewbot/live.env" and env["EWBOT_COINBASE_KEY_FILE"] == "/run/ewbot/cdp_api_key.json"
    assert not any(k in env for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "EWBOT_GITHUB_TOKEN", "EWBOT_HEALTHCHECK_URL",
                                      "EWBOT_FUTURES_PRODUCT", "EWBOT_SPOT_PRODUCT"))
    defaults = {k: re.sub(r"^\$\{\w+:-(.*)\}$", r"\1", str(v)) for k, v in env.items()
                if k not in ("EWBOT_SECRETS_FILE", "EWBOT_COINBASE_KEY_FILE")}  # the files exist only on the VPS
    assert load_settings(defaults).paper_repo == "taejunoh/elliott-wave-btc-bot"  # every default is a valid setting
    assert svc["cap_drop"] == ["ALL"] and "no-new-privileges:true" in svc["security_opt"]
    assert svc["logging"]["options"] == {"max-size": "10m", "max-file": "5"}


@pytest.mark.parametrize("script", ["deploy.sh", "backup.sh"])
def test_scripts_parse_and_fail_fast(script):
    path = LIVE / script
    assert path.read_text().splitlines()[0] == "#!/usr/bin/env bash" and "set -euo pipefail" in path.read_text()
    assert subprocess.run(["bash", "-n", str(path)], capture_output=True).returncode == 0
    if shutil.which("shellcheck"):
        assert subprocess.run(["shellcheck", str(path)], capture_output=True).returncode == 0


def test_backup_encrypts_before_upload_and_the_timer_runs_it_daily():
    text = (LIVE / "backup.sh").read_text()
    assert text.index("ewbot live backup") < text.index("age -r") < text.index("rclone copy")
    service = (LIVE / "ewbot-backup.service").read_text()
    assert "EnvironmentFile=/etc/ewbot/backup.env" in service and "deploy/live/backup.sh" in service
    assert "OnCalendar=*-*-* 06:15:00 UTC" in (LIVE / "ewbot-backup.timer").read_text()


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


def run_deploy(tmp_path: Path, *, on_master: bool = True, flat_rc: int = 0, preflight_rc: int = 0):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    _stub(bin_dir, "git", f"""echo "git $*" >> "{log}"
case "$1" in
  rev-parse) if [ "$2" = "--show-toplevel" ]; then echo "{ROOT}"; fi; exit 0 ;;
  merge-base) exit {0 if on_master else 1} ;;
  status) exit 0 ;;
  *) exit 0 ;;
esac
""")
    _stub(bin_dir, "docker", f"""echo "docker $*" >> "{log}"
case "$*" in
  *"preflight --offline"*) exit {preflight_rc} ;;
  *"--require-flat"*) exit {flat_rc} ;;
  *) exit 0 ;;
esac
""")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    r = subprocess.run(["bash", str(LIVE / "deploy.sh"), "v3.0.0"], capture_output=True, text=True, env=env)
    calls = log.read_text().splitlines() if log.exists() else []
    return r, calls


def test_deploy_happy_path_builds_checks_and_restarts(tmp_path):
    r, calls = run_deploy(tmp_path)
    assert r.returncode == 0, r.stderr
    docker = [c for c in calls if c.startswith("docker")]
    assert docker == [
        "docker build --pull -f deploy/live/Dockerfile -t ewbot-live:v3.0.0 .",
        "docker run --rm ewbot-live:v3.0.0 preflight --offline",
        "docker compose -f deploy/live/docker-compose.yml run --rm --no-deps -T live status --json --exchange --require-flat",
        "docker image tag ewbot-live:v3.0.0 ewbot-live:current",
        "docker compose -f deploy/live/docker-compose.yml up -d --force-recreate",
    ]
    assert "git checkout --quiet --detach v3.0.0" in calls


def test_deploy_refuses_a_tag_off_master(tmp_path):
    r, calls = run_deploy(tmp_path, on_master=False)
    assert r.returncode == 2 and "not on origin/master" in r.stderr and not [c for c in calls if c.startswith("docker")]


def test_deploy_refuses_while_not_flat(tmp_path):
    r, calls = run_deploy(tmp_path, flat_rc=3)
    assert r.returncode == 3 and "/flatten" in r.stderr and not any("up -d" in c for c in calls)


def test_deploy_refuses_when_the_flat_check_cannot_read_the_account(tmp_path):
    r, calls = run_deploy(tmp_path, flat_rc=2)  # Coinbase or the journal unreadable: not "send /flatten"
    assert r.returncode == 2 and "could not check" in r.stderr and "/flatten" not in r.stderr
    assert not any("up -d" in c for c in calls)


def test_deploy_refuses_an_image_that_fails_its_offline_preflight(tmp_path):
    r, calls = run_deploy(tmp_path, preflight_rc=2)
    assert r.returncode == 2 and not any("--require-flat" in c for c in calls)


def test_the_runbook_covers_setup_stages_and_emergencies():
    text = DOC.read_text(encoding="utf-8")
    for needle in ("Ubuntu 24.04", "unattended-upgrades", "chmod 600", "view", "trade", "IP allowlist",
                   "healthchecks.io", "ewbot live stage canary --confirm", "/flatten", "deploy.sh",
                   "ewbot-backup.timer", "SHADOW", "CANARY", "FULL", "EWBOT_LIVE_EDIT_STOPS",
                   "spec §2 Coinbase facts", "ORDER_UNRESOLVED", "AUDIT_PENDING", "--restart --confirm",
                   "book not flat", "stage shadow --restart --confirm", "except hh:00 to hh:03",
                   "outside hh:59 to hh:03", "the new stint's first start", "`[종료]`", "`[중단]`",
                   "last real stage", "15 minutes", "stop_grace_period"):
        assert needle in text, needle
