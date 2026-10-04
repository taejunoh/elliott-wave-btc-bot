#!/usr/bin/env bash
# Daily encrypted journal backup (Plan 3 spec 5.2), run by ewbot-backup.timer (docs/live-trading.md, "Backups").
# A consistent SQLite copy of the journal is made by the image (ewbot live backup: inside the running container, or
# in a one-off container of it when the service is stopped), encrypted with age to EWBOT_BACKUP_AGE_RECIPIENT (the
# private key stays off the VPS) and copied with rclone to EWBOT_BACKUP_REMOTE (default r2:ewbot-backups, a
# Cloudflare R2 bucket). Every run pings its own healthchecks.io check EWBOT_BACKUP_PING_URL - /start, then success
# or /fail with the exit code -, so a backup that fails, hangs or never runs is alerted, and first removes the
# plaintext copies an earlier run could not (killed). Settings: the root-only file /etc/ewbot/backup.env (systemd's
# EnvironmentFile; never in the repository).
set -euo pipefail

PING="${EWBOT_BACKUP_PING_URL:-}"
REMOTE="${EWBOT_BACKUP_REMOTE:-r2:ewbot-backups}"
DATA="${EWBOT_DATA_DIR:-/var/lib/ewbot}"
COMPOSE=(docker compose -f "$(dirname "$0")/docker-compose.yml")
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="journal-$STAMP.sqlite"
WORK="$(mktemp -d)"

ping_check() {  # $1: "" (success), /start or /fail; $2: the body (a failure's exit code). Never fails the backup.
  if [ -n "$PING" ]; then
    curl -fsS -m 10 --retry 3 -o /dev/null --data-raw "${2:-}" "$PING$1" || echo "warning: healthchecks.io ping $1 failed" >&2
  fi
}

on_exit() {
  local rc=$?
  rm -rf "$WORK"
  rm -f "$DATA/backup/$NAME"
  if [ "$rc" -ne 0 ]; then
    echo "backup failed (exit $rc)" >&2
    ping_check /fail "backup.sh exit $rc"
  fi
}
trap on_exit EXIT
trap 'exit 143' TERM  # systemd's TimeoutStartSec: the EXIT trap still reports the failure

if [ -z "$PING" ]; then
  echo "warning: EWBOT_BACKUP_PING_URL is not set in /etc/ewbot/backup.env - a failed backup alerts nobody" >&2
fi
ping_check /start
# plaintext copies a killed run (SIGKILL, power loss) left behind: never kept on the VPS
rm -f -- "$DATA"/backup/journal-*.sqlite
if [ -z "${EWBOT_BACKUP_AGE_RECIPIENT:-}" ]; then
  echo "set EWBOT_BACKUP_AGE_RECIPIENT (an age1... public key) in /etc/ewbot/backup.env" >&2
  exit 1
fi

running="$("${COMPOSE[@]}" ps --status running --quiet live)"
if [ -n "$running" ]; then
  "${COMPOSE[@]}" exec -T live ewbot live backup --out "/data/backup/$NAME" </dev/null
else  # the service is stopped (e.g. docker compose stop live): a one-off container of the same image and mounts
  "${COMPOSE[@]}" run --rm --no-deps -T live backup --out "/data/backup/$NAME" </dev/null
fi
age -r "$EWBOT_BACKUP_AGE_RECIPIENT" -o "$WORK/$NAME.age" "$DATA/backup/$NAME"
rclone copy "$WORK/$NAME.age" "$REMOTE/"
echo "backup uploaded: $REMOTE/$NAME.age"
ping_check ""
