#!/usr/bin/env bash
# Git side of .github/workflows/paper.yml (Plan 2 spec 6.1, 6.4); run from the master checkout.
# Tested against local bare repositories in tests/paper/test_paper_sync.py.
#
#   paper_sync.sh checkout
#       Worktree of origin's `paper` branch at $PAPER_DIR (shallow), or an orphan `paper` branch
#       when origin has none (ls-remote exit 2). Any other ls-remote failure fails: a network or
#       auth error must not start a fresh ledger.
#   paper_sync.sh push RECOMPUTE...
#       Commit ledger.json / report.html / runs.log in $PAPER_DIR and push the commit to origin's
#       `paper` branch, 3 pushes at most. After a failed push:
#         origin still at our commit's parent (or unreadable) -> push the same commit again after a
#             short wait; no recompute, so no Telegram message is sent twice;
#         origin already at our commit -> the push landed, done;
#         origin moved (someone else wrote to `paper`) -> reset to it, run RECOMPUTE
#             (`uv run ewbot paper run ...`), commit and push on top of it.
#       Never recomputes after the last push; exits non-zero when every push failed.
#
# Environment: PAPER_DIR (required); PAPER_PUSH_BACKOFF = seconds, retry n waits n times this
# (default 5).
set -euo pipefail

ATTEMPTS=3
LEDGER_FILES=(ledger.json report.html runs.log)
NOTHING_TO_COMMIT=3

die() {
  echo "paper_sync: $*" >&2
  exit 1
}

# The sha of origin's paper branch, "" when origin has none; any other ls-remote failure is returned.
origin_paper() {
  local out rc=0
  out=$(git -C "$1" ls-remote --exit-code origin refs/heads/paper) || rc=$?
  case "$rc" in
    0) echo "${out%%[[:space:]]*}" ;;
    2) echo "" ;;
    *) return "$rc" ;;
  esac
}

checkout() {
  local remote rc=0
  remote=$(origin_paper .) || rc=$?
  if [ "$rc" -ne 0 ]; then
    die "git ls-remote origin failed (exit $rc): not starting a new paper branch"
  fi
  if [ -n "$remote" ]; then
    git fetch --no-tags --depth=1 origin +refs/heads/paper:refs/remotes/origin/paper
    git worktree add -B paper "$PAPER_DIR" origin/paper
  else
    echo "origin has no paper branch: starting an orphan one"
    git worktree add --orphan -b paper "$PAPER_DIR"
  fi
}

# 0 = committed, NOTHING_TO_COMMIT = no ledger change, 1 = git error. Every status is checked by hand:
# the caller tests this function's status, which switches `set -e` off inside it.
commit_ledger() {
  local rc=0
  git -C "$PAPER_DIR" add -- "${LEDGER_FILES[@]}" || return 1
  git -C "$PAPER_DIR" diff --cached --quiet || rc=$?
  case "$rc" in
    0) return "$NOTHING_TO_COMMIT" ;;
    1) ;;
    *) return 1 ;;
  esac
  git -C "$PAPER_DIR" commit -q -m "paper: run $(date -u +%Y-%m-%dT%H:%MZ)" || return 1
}

# Commit the ledger; stop the script (successfully) when nothing changed, fail on a git error.
commit_or_stop() {
  local rc=0
  commit_ledger || rc=$?
  case "$rc" in
    0) ;;
    "$NOTHING_TO_COMMIT")
      echo "no ledger change: nothing to push"
      exit 0
      ;;
    *) die "git add / commit of the ledger failed in $PAPER_DIR" ;;
  esac
}

push() {
  [ "$#" -gt 0 ] || die "usage: paper_sync.sh push RECOMPUTE..."
  local backoff="${PAPER_PUSH_BACKOFF:-5}" attempt head parent remote rc
  commit_or_stop
  for ((attempt = 1; ; attempt++)); do
    if git -C "$PAPER_DIR" push origin HEAD:refs/heads/paper; then
      echo "pushed $(git -C "$PAPER_DIR" rev-parse --short HEAD) to origin/paper (attempt $attempt)"
      return 0
    fi
    head=$(git -C "$PAPER_DIR" rev-parse HEAD)
    parent=$(git -C "$PAPER_DIR" rev-parse -q --verify 'HEAD^' || true) # "" for the first (orphan) commit
    rc=0
    remote=$(origin_paper "$PAPER_DIR") || rc=$?
    if [ "$rc" -eq 0 ] && [ "$remote" = "$head" ]; then
      echo "origin/paper is at our commit: the push landed despite the error"
      return 0
    fi
    if [ "$attempt" -ge "$ATTEMPTS" ]; then
      die "push failed $ATTEMPTS times"
    fi
    if [ "$rc" -ne 0 ] || [ "$remote" = "$parent" ]; then
      if [ "$rc" -ne 0 ]; then
        echo "push failed (attempt $attempt) and origin is unreadable (ls-remote exit $rc): push the same commit again"
      else
        echo "push failed (attempt $attempt), origin/paper did not move: push the same commit again"
      fi
      sleep "$((backoff * attempt))"
      continue
    fi
    [ -n "$remote" ] || die "origin's paper branch disappeared"
    echo "push rejected (attempt $attempt): origin/paper moved to ${remote:0:12}, recompute on top of it"
    git -C "$PAPER_DIR" fetch --no-tags --depth=1 origin +refs/heads/paper:refs/remotes/origin/paper
    git -C "$PAPER_DIR" reset -q --hard origin/paper
    "$@" || die "recompute failed (exit $?)"
    commit_or_stop
  done
}

: "${PAPER_DIR:?PAPER_DIR is not set}"
case "${1:-}" in
  checkout) checkout ;;
  push)
    shift
    push "$@"
    ;;
  *) die "usage: paper_sync.sh checkout | push RECOMPUTE..." ;;
esac
