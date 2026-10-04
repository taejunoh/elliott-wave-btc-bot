#!/usr/bin/env bash
# Deploy a tagged master commit of ewbot's live runtime to this VPS (Plan 3 spec 5.2; docs/live-trading.md,
# "Deploying a new version"). From the repository checkout on the VPS, as the user ewbot:
#   bash deploy/live/deploy.sh <tag>              the deploy: only with a flat book
#   bash deploy/live/deploy.sh --recover <tag>    the same without the flat-book check, only to bring a runtime back
#                                                 while a position is open - a release that crash-loops, a new VPS
#                                                 after a VPS loss (runbook); asks to type "recover <tag>"
#                                                 (--i-understand instead: no question)
# Before the running runtime is touched, the new image runs its offline preflight with the service's real
# configuration (the journal's stage, the secrets and key files, the read-only root, no capabilities). The swap
# happens only outside the minutes hh:54-hh:05 UTC (a swap, its wait and a rollback with its own wait end before the
# hour's cycle at hh:00:30; entries go out until hh:05), keeps the running image as ewbot-live:previous and waits for a
# loop heartbeat from a container of the new image: 3 minutes (10 on the VPS's first deployment, which downloads the
# candle history first). A failed `up -d` or no such heartbeat is a failed start: the previous image is started
# again - only when it still finds the book flat.
# Exit codes:
#   0  deployed, the new runtime is up
#   2  refused, nothing changed: no such tag, a tag off origin/master, local changes or untracked files (src, deploy,
#      pyproject.toml, uv.lock, holdout.lock), an image that does not build or fails its preflight, the minutes
#      hh:54-hh:05 UTC, a book that could not be checked, --recover not confirmed
#   3  refused, nothing changed: not flat (an open trade, an order of unknown outcome, a standing flatten, a position or
#      a resting order)
#   4  the new runtime did not come up: rolled back, the previous image runs again and is up
#   5  the new runtime did not come up and nothing was rolled back (--recover, a first deployment, or a book the
#      previous image did not find flat), or the rolled-back previous image did not come up either: act now (runbook,
#      "Deploying a new version")
# The checkout is moved to <tag> for the build and moved back on every refusal and on a rollback.
set -euo pipefail

USAGE="usage: deploy.sh [--recover [--i-understand]] <tag>"
COMPOSE=(docker compose -f deploy/live/docker-compose.yml)
TREE=(src deploy pyproject.toml uv.lock holdout.lock)  # what the image or the deploy is made of
UP_WAIT_S=180
FIRST_UP_WAIT_S=600
MOVED=0
PREV_BRANCH=""
PREV_HEAD=""

refuse() {  # $1: why, $2: exit code (2)
  echo "refusing: $1" >&2
  exit "${2:-2}"
}

restore_checkout() {
  if [ "$MOVED" -ne 1 ]; then
    return 0
  fi
  if [ -n "$PREV_BRANCH" ]; then
    git checkout --quiet "$PREV_BRANCH" || echo "warning: could not check out $PREV_BRANCH again" >&2
  else
    git checkout --quiet --detach "$PREV_HEAD" || echo "warning: could not check out $PREV_HEAD again" >&2
  fi
  MOVED=0
}

on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    restore_checkout
  fi
}

runs_image() {  # $1: an image id. 0 when the service has a container and every one runs that image
  local want="$1" cids cid have
  cids="$("${COMPOSE[@]}" ps -q live </dev/null 2>/dev/null)" || return 1
  if [ -z "$cids" ]; then
    return 1
  fi
  for cid in $cids; do
    have="$(docker inspect -f '{{.Image}}' "$cid" 2>/dev/null)" || return 1
    if [ "$have" != "$want" ]; then
      return 1
    fi
  done
}

wait_up() {  # $1: seconds, $2: the image id. 0 once a container of that image journaled a loop heartbeat after this call
  local limit="$1" want="$2" since now elapsed
  since="$(date +%s)"
  while :; do
    sleep 10
    now="$(date +%s)"
    elapsed=$((now - since))
    # the container must be the new image's (a failed recreate leaves the old one running - and heartbeating), and
    # --max-age below the time since the swap: only a heartbeat journaled after it counts
    if runs_image "$want" \
      && "${COMPOSE[@]}" exec -T live ewbot live status --json --max-age "$((elapsed - 1))" </dev/null >/dev/null 2>&1; then
      return 0
    fi
    if [ "$elapsed" -ge "$limit" ]; then
      return 1
    fi
  done
}

main() {
  local recover=0 confirmed=0 tag="" arg root changes untracked image minute rc answer first wait_s why want up
  for arg in "$@"; do
    case "$arg" in
      --recover) recover=1 ;;
      --i-understand) confirmed=1 ;;
      -*) refuse "unknown option $arg ($USAGE)" ;;
      *) if [ -n "$tag" ]; then refuse "$USAGE"; fi; tag="$arg" ;;
    esac
  done
  if [ -z "$tag" ] || { [ "$confirmed" -eq 1 ] && [ "$recover" -eq 0 ]; }; then
    refuse "$USAGE"
  fi

  root="$(git rev-parse --show-toplevel)"
  cd "$root"
  git fetch --quiet --tags origin master || refuse "git fetch failed"
  git rev-parse --verify --quiet "refs/tags/$tag" >/dev/null || refuse "no tag $tag"
  git merge-base --is-ancestor "$tag^{commit}" origin/master || refuse "$tag is not on origin/master"
  # a git status that fails counts as a dirty checkout
  changes="$(git status --porcelain --untracked-files=no)" || changes="git status failed"
  if [ -n "$changes" ]; then
    refuse "the checkout has local changes"
  fi
  untracked="$(git status --porcelain --untracked-files=all -- "${TREE[@]}")" || untracked="git status failed"
  if [ -n "$untracked" ]; then
    refuse "untracked files under ${TREE[*]} (they would enter the image or the deploy): remove them - git status"
  fi

  PREV_BRANCH="$(git symbolic-ref --quiet --short HEAD || true)"
  PREV_HEAD="$(git rev-parse --verify HEAD)"
  trap on_exit EXIT
  MOVED=1
  git checkout --quiet --detach "$tag"

  image="ewbot-live:$tag"
  docker build --pull -f deploy/live/Dockerfile -t "$image" . || refuse "the image $image did not build"
  # The new image's preflight with the service's real configuration, before anything runs on it.
  EWBOT_IMAGE="$image" "${COMPOSE[@]}" run --rm --no-deps -T live preflight --offline </dev/null \
    || refuse "$image failed its preflight with the service's configuration (see above)"

  if [ "$recover" -eq 1 ]; then
    echo "--recover: no flat-book check - the new runtime takes over what the journal and Coinbase hold:" >&2
    EWBOT_IMAGE="$image" "${COMPOSE[@]}" run --rm --no-deps -T live status --json --exchange </dev/null \
      || echo "(the book could not be read)" >&2
    if [ "$confirmed" -eq 0 ]; then
      printf 'type "recover %s" to deploy it over the open book: ' "$tag" >&2
      answer=""
      read -r answer || true
      if [ "$answer" != "recover $tag" ]; then
        refuse "--recover not confirmed"
      fi
    fi
  fi

  # Last check before the swap: the swap, its wait and a rollback with its own wait end before the hour's cycle
  # (hh:00:30), and nothing is swapped while that cycle's entries may go out (until hh:05:00).
  minute="$(date -u +%M)"
  if [ "$((10#$minute))" -ge 54 ] || [ "$((10#$minute))" -le 5 ]; then
    refuse "it is hh:$minute UTC: no swap from hh:54 to hh:05 (the hour's cycle and its entries) - run it again from hh:06"
  fi

  if [ "$recover" -eq 0 ]; then
    # Flat book: the new image reads the live journal and asks Coinbase (the service's mounts).
    rc=0
    EWBOT_IMAGE="$image" "${COMPOSE[@]}" run --rm --no-deps -T live status --json --exchange --require-flat \
      </dev/null || rc=$?
    if [ "$rc" -eq 3 ]; then
      refuse "not flat - send /flatten in Telegram (or wait for the trade to close), then deploy again" 3
    elif [ "$rc" -ne 0 ]; then
      refuse "could not check the book (exit $rc: Coinbase or the journal unreadable, see above) - deploy again later"
    fi
  fi

  first=1
  if docker image inspect ewbot-live:current >/dev/null 2>&1; then
    docker image tag ewbot-live:current ewbot-live:previous
    first=0
  fi
  docker image tag "$image" ewbot-live:current
  want="$(docker image inspect -f '{{.Id}}' ewbot-live:current)"
  # EWBOT_IMAGE pinned: an EWBOT_IMAGE in deploy/live/.env never decides what runs
  up=0
  EWBOT_IMAGE=ewbot-live:current "${COMPOSE[@]}" up -d --force-recreate || up=$?
  wait_s="$UP_WAIT_S"
  if [ "$first" -eq 1 ]; then
    wait_s="$FIRST_UP_WAIT_S"
  fi
  if [ "$up" -eq 0 ] && wait_up "$wait_s" "$want"; then
    MOVED=0
    echo "deployed $image (tagged ewbot-live:current; the one before is ewbot-live:previous); follow it with: ${COMPOSE[*]} logs -f live"
    return 0
  fi
  if [ "$up" -ne 0 ]; then
    echo "$image: up -d failed (exit $up) - a failed start" >&2
  else
    echo "$image: no loop heartbeat from a container of it ${wait_s}s after the start - read ${COMPOSE[*]} logs live" >&2
  fi
  if [ "$recover" -eq 1 ] || [ "$first" -eq 1 ]; then
    MOVED=0
    why="no previous image"
    if [ "$recover" -eq 1 ]; then
      why="--recover"
    fi
    echo "not rolled back ($why): $image keeps starting - fix it now (runbook, Deploying a new version)" >&2
    exit 5
  fi
  restore_checkout
  # The new runtime may have traded meanwhile: roll back only to a book the previous image still finds flat.
  rc=0
  EWBOT_IMAGE=ewbot-live:previous "${COMPOSE[@]}" run --rm --no-deps -T live status --json --exchange --require-flat \
    </dev/null || rc=$?
  if [ "$rc" -ne 0 ]; then
    git checkout --quiet --detach "$tag" || echo "warning: could not check out $tag again" >&2
    why="the book is not flat any more"
    if [ "$rc" -ne 3 ]; then
      why="the book could not be checked (exit $rc)"
    fi
    echo "not rolled back: $why - $image keeps starting: /status, the logs, then act now (runbook, Deploying a new version)" >&2
    exit 5
  fi
  docker image tag ewbot-live:previous ewbot-live:current
  want="$(docker image inspect -f '{{.Id}}' ewbot-live:current)"
  up=0
  EWBOT_IMAGE=ewbot-live:current "${COMPOSE[@]}" up -d --force-recreate || up=$?
  if [ "$up" -eq 0 ] && wait_up "$UP_WAIT_S" "$want"; then
    echo "rolled back: ewbot-live:current is the previous image again and it is up ($image kept for a look)" >&2
    exit 4
  fi
  echo "the previous image is not up either - act now (runbook, Deploying a new version)" >&2
  exit 5
}

main "$@"
