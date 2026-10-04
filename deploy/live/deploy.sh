#!/usr/bin/env bash
# Deploy a tagged master commit of ewbot's live runtime to this VPS (Plan 3 spec 5.2).
# Usage (from the repository checkout on the VPS): bash deploy/live/deploy.sh <tag>
# Refuses (exit 2) a tag that is not on origin/master, a checkout with local changes or an image whose
# offline preflight fails (fingerprint, holdout.lock) or a flat-book check that cannot read Coinbase or the journal;
# refuses (exit 3) while a trade is open in the journal (SHADOW's virtual one included) or the Coinbase account has
# a position or a resting order.
set -euo pipefail

TAG="${1:?usage: deploy.sh <tag>}"
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
COMPOSE=(docker compose -f deploy/live/docker-compose.yml)

git fetch --quiet --tags origin master
if ! git rev-parse --verify --quiet "refs/tags/$TAG" >/dev/null; then
  echo "refusing: no tag $TAG" >&2
  exit 2
fi
if ! git merge-base --is-ancestor "$TAG^{commit}" origin/master; then
  echo "refusing: $TAG is not on origin/master" >&2
  exit 2
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "refusing: the checkout has local changes" >&2
  exit 2
fi
git checkout --quiet --detach "$TAG"

IMAGE="ewbot-live:$TAG"
docker build --pull -f deploy/live/Dockerfile -t "$IMAGE" .
docker run --rm "$IMAGE" preflight --offline

# Flat book: the new image reads the live journal and asks Coinbase (same mounts as the service).
rc=0
EWBOT_IMAGE="$IMAGE" "${COMPOSE[@]}" run --rm --no-deps -T live status --json --exchange --require-flat || rc=$?
if [ "$rc" -eq 3 ]; then
  echo "refusing: not flat - send /flatten in Telegram (or wait for the trade to close), then deploy again" >&2
  exit 3
elif [ "$rc" -ne 0 ]; then
  echo "refusing: could not check the book (exit $rc: Coinbase or the journal unreadable, see above) - deploy again later" >&2
  exit 2
fi

docker image tag "$IMAGE" ewbot-live:current
"${COMPOSE[@]}" up -d --force-recreate
echo "deployed $IMAGE (tagged ewbot-live:current); follow the start with: ${COMPOSE[*]} logs -f live"
