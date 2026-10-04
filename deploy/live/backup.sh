#!/usr/bin/env bash
# Daily encrypted journal backup (Plan 3 spec 5.2), run by ewbot-backup.timer (docs/live-trading.md).
# A consistent SQLite copy is made inside the running container (ewbot live backup), encrypted with age
# to EWBOT_BACKUP_AGE_RECIPIENT (the private key stays off the VPS) and copied with rclone to
# EWBOT_BACKUP_REMOTE (default r2:ewbot-backups, a Cloudflare R2 bucket). Settings: /etc/ewbot/backup.env.
set -euo pipefail

: "${EWBOT_BACKUP_AGE_RECIPIENT:?set EWBOT_BACKUP_AGE_RECIPIENT (an age1... public key) in /etc/ewbot/backup.env}"
REMOTE="${EWBOT_BACKUP_REMOTE:-r2:ewbot-backups}"
DATA="${EWBOT_DATA_DIR:-/var/lib/ewbot}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="journal-$STAMP.sqlite"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; rm -f "$DATA/backup/$NAME"' EXIT

docker compose -f "$(dirname "$0")/docker-compose.yml" exec -T live ewbot live backup --out "/data/backup/$NAME"
age -r "$EWBOT_BACKUP_AGE_RECIPIENT" -o "$WORK/$NAME.age" "$DATA/backup/$NAME"
rclone copy "$WORK/$NAME.age" "$REMOTE/"
echo "backup uploaded: $REMOTE/$NAME.age"
