"""Static checks of deploy/live and stubbed runs of deploy.sh and backup.sh (fake git / docker / date / sleep / age /
rclone / curl on PATH: no daemon, no network)."""

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
COMPOSE = "docker compose -f deploy/live/docker-compose.yml"
PREV_SHA = "1111111111111111111111111111111111111111"


def test_dockerfile_pins_the_runtime_and_ships_no_secret():
    text = (LIVE / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^FROM python:3\.12\.13-slim-bookworm@sha256:[0-9a-f]{64}$", text, re.M)
    assert re.search(r"^COPY --from=ghcr\.io/astral-sh/uv:0\.12\.13@sha256:[0-9a-f]{64} /uv /bin/uv$", text, re.M)
    assert text.count("uv sync --frozen --no-dev --group live") == 2
    assert "UV_NO_CACHE=1" in text  # uv's download cache never stays in a layer
    assert "python -m compileall -q /app/src" in text  # the read-only root: compiled once, at build time
    copies = re.findall(r"^COPY (?!--from)(.+)$", text, re.M)
    assert copies == ["pyproject.toml uv.lock ./", "src ./src", "holdout.lock ./"]
    # the cheap health check: the journal's loop heartbeat only, no gates
    assert "USER ewbot" in text and 'CMD ["ewbot", "live", "status", "--json", "--max-age", "180"]' in text
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
    # the journal keeps the log of the containers a deploy or a restart replaced (json-file logs go with them)
    assert svc["logging"] == {"driver": "journald", "options": {"tag": "ewbot-live"}}


def test_the_vps_settings_file_is_never_committed():
    assert "/.env" in (LIVE / ".gitignore").read_text().splitlines()  # deploy/live/.env


@pytest.mark.parametrize("script", ["deploy.sh", "backup.sh"])
def test_scripts_parse_and_fail_fast(script):
    path = LIVE / script
    assert path.read_text().splitlines()[0] == "#!/usr/bin/env bash" and "set -euo pipefail" in path.read_text()
    assert os.access(path, os.X_OK)
    assert subprocess.run(["bash", "-n", str(path)], capture_output=True).returncode == 0


@pytest.mark.parametrize("script", ["deploy.sh", "backup.sh"])
def test_scripts_pass_shellcheck(script):
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck is not installed (apt-get install shellcheck / brew install shellcheck)")
    r = subprocess.run(["shellcheck", "--enable=check-extra-masked-returns", str(LIVE / script)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_backup_encrypts_before_upload_and_the_timer_runs_it_daily():
    text = (LIVE / "backup.sh").read_text()
    assert text.index("ewbot live backup") < text.index("age -r") < text.index("rclone copy")
    service = (LIVE / "ewbot-backup.service").read_text()
    assert "EnvironmentFile=/etc/ewbot/backup.env" in service and "deploy/live/backup.sh" in service
    assert "TimeoutStartSec=30min" in service  # a hung run never holds back the later ones
    assert "OnCalendar=*-*-* 06:15:00 UTC" in (LIVE / "ewbot-backup.timer").read_text()


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


def run_deploy(tmp_path: Path, *args: str, on_master: bool = True, has_tag: bool = True, changes: str = "",
               untracked: str = "", status_rc: int = 0, build_rc: int = 0, preflight_rc: int = 0, flat_rc: int = 0,
               minute: str = "30", current: bool = True, health: tuple[str, ...] = ("ok",), answer: str | None = None):
    """deploy.sh with stubs. `health`: per `up -d` (the swap, then a rollback) whether `status --max-age` succeeds.
    The clock starts at 1 000 000 s and moves only with `sleep`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log, clock, ups = tmp_path / "calls.log", tmp_path / "clock", tmp_path / "ups"
    clock.write_text("1000000")
    ups.write_text("0")
    _stub(bin_dir, "git", f"""echo "git $*" >> "{log}"
case "$*" in
  "rev-parse --show-toplevel") echo "{ROOT}" ;;
  "rev-parse --verify --quiet refs/tags/"*) exit {0 if has_tag else 1} ;;
  "rev-parse --verify HEAD") echo "{PREV_SHA}" ;;
  "symbolic-ref --quiet --short HEAD") exit 1 ;;
  "merge-base "*) exit {0 if on_master else 1} ;;
  "status --porcelain --untracked-files=no") printf '%s' "{changes}"; exit {status_rc} ;;
  "status --porcelain --untracked-files=all -- "*) printf '%s' "{untracked}" ;;
esac
exit 0
""")
    health_case = "\n".join(f"    {i + 1}) exit {0 if h == 'ok' else 1} ;;" for i, h in enumerate(health))
    _stub(bin_dir, "docker", f"""echo "docker $*${{EWBOT_IMAGE+ [EWBOT_IMAGE=$EWBOT_IMAGE]}}" >> "{log}"
case "$*" in
  build*) exit {build_rc} ;;
  *"preflight --offline"*) exit {preflight_rc} ;;
  *"--require-flat"*) exit {flat_rc} ;;
  "image inspect ewbot-live:current") exit {0 if current else 1} ;;
  *"up -d"*) echo $(( $(cat "{ups}") + 1 )) > "{ups}" ;;
  *"exec -T live ewbot live status"*)
    case "$(cat "{ups}")" in
{health_case}
    esac
    exit 1 ;;
esac
exit 0
""")
    _stub(bin_dir, "date", f"""case "$*" in
  "-u +%M") echo "{minute}" ;;
  "+%s") cat "{clock}" ;;
  *) exec /bin/date "$@" ;;
esac
""")
    _stub(bin_dir, "sleep", f"""echo $(( $(cat "{clock}") + $1 )) > "{clock}"
""")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    env.pop("EWBOT_IMAGE", None)
    stdin = {"input": answer} if answer is not None else {"stdin": subprocess.DEVNULL}  # never the terminal
    r = subprocess.run(["bash", str(LIVE / "deploy.sh"), *(args or ("v3.0.0",))], capture_output=True, text=True,
                       env=env, timeout=60, **stdin)
    calls = log.read_text().splitlines() if log.exists() else []
    return r, calls


def docker_calls(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith("docker")]


def health_polls(calls: list[str]) -> list[str]:
    return [c for c in calls if "exec -T live ewbot live status" in c]


SWAP = [
    "docker image inspect ewbot-live:current",
    "docker image tag ewbot-live:current ewbot-live:previous",
    "docker image tag ewbot-live:v3.0.0 ewbot-live:current",
    f"{COMPOSE} up -d --force-recreate [EWBOT_IMAGE=ewbot-live:current]",
]


def test_deploy_happy_path_checks_the_image_with_the_service_config_then_swaps_and_waits_for_the_heartbeat(tmp_path):
    r, calls = run_deploy(tmp_path)
    assert r.returncode == 0, r.stderr
    assert docker_calls(calls) == [
        "docker build --pull -f deploy/live/Dockerfile -t ewbot-live:v3.0.0 .",
        # the new image's preflight with the service's mounts, secrets, user and read-only root
        f"{COMPOSE} run --rm --no-deps -T live preflight --offline [EWBOT_IMAGE=ewbot-live:v3.0.0]",
        f"{COMPOSE} run --rm --no-deps -T live status --json --exchange --require-flat [EWBOT_IMAGE=ewbot-live:v3.0.0]",
        *SWAP,
        # only a heartbeat younger than the time since the swap counts: never the old runtime's
        f"{COMPOSE} exec -T live ewbot live status --json --max-age 9",
    ]
    assert "git checkout --quiet --detach v3.0.0" in calls
    assert f"git checkout --quiet --detach {PREV_SHA}" not in calls  # the checkout stays at the deployed tag


def test_deploy_refuses_a_tag_off_master(tmp_path):
    r, calls = run_deploy(tmp_path, on_master=False)
    assert r.returncode == 2 and "not on origin/master" in r.stderr and not docker_calls(calls)
    assert not any(c.startswith("git checkout") for c in calls)


def test_deploy_refuses_a_missing_tag(tmp_path):
    r, calls = run_deploy(tmp_path, has_tag=False)
    assert r.returncode == 2 and "no tag v3.0.0" in r.stderr and not docker_calls(calls)


@pytest.mark.parametrize("changes, status_rc", [(" M src/ewbot/live/runtime.py\n", 0), ("", 128)])
def test_deploy_refuses_local_changes_and_a_git_status_that_fails(tmp_path, changes, status_rc):
    r, calls = run_deploy(tmp_path, changes=changes, status_rc=status_rc)
    assert r.returncode == 2 and "local changes" in r.stderr and not docker_calls(calls)
    assert not any(c.startswith("git checkout") for c in calls)


def test_deploy_refuses_untracked_files_in_the_build_context(tmp_path):
    r, calls = run_deploy(tmp_path, untracked="?? src/ewbot/live/extra.py\n")
    assert r.returncode == 2 and "untracked files" in r.stderr and not docker_calls(calls)
    assert "git status --porcelain --untracked-files=all -- src deploy pyproject.toml uv.lock holdout.lock" in calls


def test_deploy_refuses_while_not_flat_and_moves_the_checkout_back(tmp_path):
    r, calls = run_deploy(tmp_path, flat_rc=3)
    assert r.returncode == 3 and "/flatten" in r.stderr and not any("up -d" in c for c in calls)
    assert calls[-1] == f"git checkout --quiet --detach {PREV_SHA}"


def test_deploy_refuses_when_the_flat_check_cannot_read_the_account(tmp_path):
    r, calls = run_deploy(tmp_path, flat_rc=2)  # Coinbase or the journal unreadable: not "send /flatten"
    assert r.returncode == 2 and "could not check" in r.stderr and "/flatten" not in r.stderr
    assert not any("up -d" in c for c in calls) and calls[-1] == f"git checkout --quiet --detach {PREV_SHA}"


@pytest.mark.parametrize("build_rc, preflight_rc", [(1, 0), (0, 2)])
def test_deploy_refuses_an_image_that_does_not_build_or_fails_its_preflight(tmp_path, build_rc, preflight_rc):
    r, calls = run_deploy(tmp_path, build_rc=build_rc, preflight_rc=preflight_rc)
    assert r.returncode == 2 and not any("--require-flat" in c or "image tag" in c for c in calls)
    assert calls[-1] == f"git checkout --quiet --detach {PREV_SHA}"


@pytest.mark.parametrize("minute, refused", [("57", False), ("58", True), ("59", True), ("00", True), ("05", True),
                                             ("06", False)])
def test_deploy_never_swaps_from_hh58_to_hh05(tmp_path, minute, refused):
    r, calls = run_deploy(tmp_path, minute=minute)
    if refused:
        assert r.returncode == 2 and "hh:58 to hh:05" in r.stderr
        assert not any("--require-flat" in c or "up -d" in c for c in calls)
    else:
        assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("answer", [None, "", "yes\n", "recover v2.9.0\n"])
def test_deploy_recover_needs_the_typed_confirmation(tmp_path, answer):
    r, calls = run_deploy(tmp_path, "--recover", "v3.0.0", answer=answer)
    assert r.returncode == 2 and "--recover not confirmed" in r.stderr and not any("up -d" in c for c in calls)
    assert calls[-1] == f"git checkout --quiet --detach {PREV_SHA}"


@pytest.mark.parametrize("args, answer", [(("--recover", "v3.0.0"), "recover v3.0.0\n"),
                                          (("--recover", "--i-understand", "v3.0.0"), None)])
def test_deploy_recover_skips_only_the_flat_check(tmp_path, args, answer):
    r, calls = run_deploy(tmp_path, *args, answer=answer, flat_rc=3)  # an open position
    assert r.returncode == 0, r.stderr
    assert docker_calls(calls) == [
        "docker build --pull -f deploy/live/Dockerfile -t ewbot-live:v3.0.0 .",
        f"{COMPOSE} run --rm --no-deps -T live preflight --offline [EWBOT_IMAGE=ewbot-live:v3.0.0]",
        f"{COMPOSE} run --rm --no-deps -T live status --json --exchange [EWBOT_IMAGE=ewbot-live:v3.0.0]",  # shown only
        *SWAP,
        f"{COMPOSE} exec -T live ewbot live status --json --max-age 9",
    ]


def test_deploy_recover_still_keeps_the_time_window_and_the_preflight(tmp_path):
    r, calls = run_deploy(tmp_path, "--recover", "--i-understand", "v3.0.0", minute="00")
    assert r.returncode == 2 and "hh:58 to hh:05" in r.stderr and not any("up -d" in c for c in calls)
    r, calls = run_deploy(tmp_path, "--recover", "--i-understand", "v3.0.0", preflight_rc=2)
    assert r.returncode == 2 and not any("up -d" in c for c in calls)


@pytest.mark.parametrize("args", [(), ("--i-understand", "v3.0.0"), ("v3.0.0", "v3.0.1"), ("--force", "v3.0.0")])
def test_deploy_refuses_bad_arguments(tmp_path, args):
    r, calls = run_deploy(tmp_path, *(args or ("",)))
    assert r.returncode == 2 and "usage: deploy.sh" in r.stderr and not docker_calls(calls)


def test_deploy_rolls_back_when_the_new_runtime_writes_no_heartbeat(tmp_path):
    r, calls = run_deploy(tmp_path, health=("fail", "ok"))
    assert r.returncode == 4 and "rolled back" in r.stderr
    polls = health_polls(calls)
    assert len(polls) == 19 and polls[17].endswith("--max-age 179")  # 3 minutes of polls, then the rollback's first
    after = calls[calls.index(polls[17]) + 1:]
    assert after == [
        f"git checkout --quiet --detach {PREV_SHA}",  # the previous release's compose file and scripts first
        "docker image tag ewbot-live:previous ewbot-live:current",
        f"{COMPOSE} up -d --force-recreate [EWBOT_IMAGE=ewbot-live:current]",
        f"{COMPOSE} exec -T live ewbot live status --json --max-age 9",
    ]


def test_deploy_reports_a_rollback_that_does_not_come_up_either(tmp_path):
    r, calls = run_deploy(tmp_path, health=("fail", "fail"))
    assert r.returncode == 5 and "not up either" in r.stderr and len(health_polls(calls)) == 36


@pytest.mark.parametrize("args, current, polls", [(("--recover", "--i-understand", "v3.0.0"), True, 18),
                                                  (("v3.0.0",), False, 60)])  # a first deployment waits 10 minutes
def test_deploy_never_rolls_back_a_recovery_or_a_first_deployment(tmp_path, args, current, polls):
    r, calls = run_deploy(tmp_path, *args, current=current, health=("fail",))
    assert r.returncode == 5 and "not rolled back" in r.stderr
    assert len(health_polls(calls)) == polls and not any("ewbot-live:previous" in c for c in calls[-polls - 2:])
    assert sum("up -d" in c for c in calls) == 1 and f"git checkout --quiet --detach {PREV_SHA}" not in calls
    if not current:
        assert "docker image tag ewbot-live:current ewbot-live:previous" not in calls


def run_backup(tmp_path: Path, *, running: bool = True, rclone_rc: int = 0, ping: bool = True,
               recipient: bool = True):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    _stub(bin_dir, "docker", f"""echo "docker $*" >> "{log}"
case "$*" in
  *"ps --status running --quiet live") {"echo 0123abcd" if running else "true"} ;;
  *" backup --out /data/"*)
    last="${{@: -1}}"; out="$EWBOT_DATA_DIR/${{last#/data/}}"; mkdir -p "$(dirname "$out")"; echo plain > "$out" ;;
esac
""")
    _stub(bin_dir, "age", f"""echo "age $*" >> "{log}"
test "$(cat "${{@: -1}}")" = plain || exit 1
echo sealed > "$4"
""")
    _stub(bin_dir, "rclone", f"""echo "rclone $*" >> "{log}"
test "$(cat "$2")" = sealed || exit 1
exit {rclone_rc}
""")
    _stub(bin_dir, "curl", f"""echo "curl $*" >> "{log}"
""")
    env = {k: v for k, v in os.environ.items() if not k.startswith("EWBOT_")}
    env.update({"PATH": f"{bin_dir}:{os.environ['PATH']}", "EWBOT_DATA_DIR": str(data)})
    if recipient:
        env["EWBOT_BACKUP_AGE_RECIPIENT"] = "age1examplerecipient"
    if ping:
        env["EWBOT_BACKUP_PING_URL"] = "https://hc-ping.com/backup-uuid"
    r = subprocess.run(["bash", str(LIVE / "backup.sh")], capture_output=True, text=True, env=env, timeout=60)
    calls = log.read_text().splitlines() if log.exists() else []
    left = [p for p in data.rglob("*") if p.is_file()]
    return r, calls, left


def pings(calls: list[str]) -> list[str]:
    return [re.sub(r"^curl -fsS -m 10 --retry 3 -o /dev/null --data-raw ", "", c) for c in calls if c.startswith("curl")]


def test_backup_from_the_running_service_is_encrypted_uploaded_and_reported(tmp_path):
    r, calls, left = run_backup(tmp_path)
    assert r.returncode == 0, r.stderr
    names = [c for c in calls if c.startswith(("docker", "age", "rclone"))]
    assert names[1].startswith(f"docker compose -f {LIVE}/docker-compose.yml exec -T live ewbot live backup --out "
                               "/data/backup/journal-")
    assert names[2].startswith("age -r age1examplerecipient -o ") and names[3].startswith("rclone copy ")
    assert names[3].endswith(".sqlite.age r2:ewbot-backups/")
    assert pings(calls) == [" https://hc-ping.com/backup-uuid/start", " https://hc-ping.com/backup-uuid"]
    assert left == []  # the plaintext copy is gone


def test_backup_with_the_service_stopped_uses_a_one_off_container(tmp_path):
    r, calls, left = run_backup(tmp_path, running=False)
    assert r.returncode == 0, r.stderr
    assert any(c.startswith(f"docker compose -f {LIVE}/docker-compose.yml run --rm --no-deps -T live backup --out ")
               for c in calls)
    assert not any(" exec " in c for c in calls) and left == []


@pytest.mark.parametrize("kw", [{"rclone_rc": 1}, {"recipient": False}])
def test_a_failed_backup_pings_fail_and_leaves_no_plaintext(tmp_path, kw):
    r, calls, left = run_backup(tmp_path, **kw)
    assert r.returncode != 0 and left == []
    assert pings(calls)[0] == " https://hc-ping.com/backup-uuid/start"
    assert pings(calls)[-1] == f"backup.sh exit {r.returncode} https://hc-ping.com/backup-uuid/fail"


def test_a_backup_without_its_check_says_nobody_is_told(tmp_path):
    r, calls, _ = run_backup(tmp_path, ping=False)
    assert r.returncode == 0 and "alerts nobody" in r.stderr and not pings(calls)


def test_the_runbook_covers_setup_stages_and_emergencies():
    text = DOC.read_text(encoding="utf-8")
    for needle in ("Ubuntu 24.04", "unattended-upgrades", "chmod 600", "view", "trade", "IP allowlist",
                   "healthchecks.io", "ewbot live stage canary --confirm", "/flatten", "deploy.sh",
                   "ewbot-backup.timer", "SHADOW", "CANARY", "FULL", "EWBOT_LIVE_EDIT_STOPS",
                   "spec §2 Coinbase facts", "ORDER_UNRESOLVED", "AUDIT_PENDING", "--restart --confirm",
                   "book not flat", "stage shadow --restart --confirm", "except hh:00 to hh:03",
                   "outside hh:59 to hh:03", "the new stint's first start", "`[종료]`", "`[중단]`",
                   "last real stage", "15 minutes", "stop_grace_period",
                   # deploys, recovery and backups
                   "hh:58 to hh:05", "ewbot-live:previous", "deploy.sh --recover", "Recovering from VPS loss",
                   "journal.sqlite-wal", "install -m 600 -o 1000 -g 1000 /dev/stdin", "no_check_bucket = true",
                   "EWBOT_BACKUP_PING_URL", "restore drill", "journalctl -t ewbot-live", "unhealthy",
                   "docker builder prune", "never hold", "View-only key"):
        assert needle in text, needle
