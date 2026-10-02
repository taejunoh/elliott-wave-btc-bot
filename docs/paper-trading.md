# Paper-trading runtime (Plan 2) — operations

Spec: `docs/superpowers/specs/2026-10-02-ewbot-plan2-paper-trading-design.md`.

## What runs

- `.github/workflows/paper.yml` runs `uv run ewbot paper run --ledger-dir <paper worktree>` every hour at minute 7 (and on demand) on `ubuntu-24.04`. `scripts/paper_sync.sh` does the git side: the `paper` worktree, the commit, the push and its retries.
- GitHub can start a scheduled run late (minutes, sometimes much more when Actions is busy), so a run can slip into the next hour and leave an hour without a successful run. Criterion 4's 95% coverage target tolerates up to 33 missed hours in 28 days (5% of 672 hours is 33.6).
- `.github/workflows/ci.yml` runs the strategy-fingerprint and static guards on every pull request to `master` and every push to `master` (see Strict fingerprint policy).
- Each run recomputes the whole engine deterministically from 2017 with the params in the committed `holdout.lock` (K4 = 2.0, Q_MIN = 0.7, S_MIN = 0.5), conservative risk, breakers on and Plan 1 costs. Trading starts at the holdout start (2024-01-01), so the run reproduces the holdout's A run and continues it.
- Paper trades are the trades that **enter after the holdout's last bar** (`entry_ts > 1790938800`, 2026-10-02 11:00 UTC). A position already open at that bar belongs to the holdout and is never counted twice.
- Results go to the orphan branch `paper` (never to `master`). Only the workflow writes to `paper`: never push to it by hand (see Failures).
  - `ledger.json`: baseline fingerprint and commit, holdout summary, epochs, closed trades, open position, decisions, discrepancies, runs (each with its runtime environment), sent and pending Telegram events, the last criteria 1–2 result.
  - `report.html`: go-live status at the top, out-of-sample equity and drawdown, trade tables with snapshot charts, current top count, discrepancy list, epochs.
  - `runs.log`: one line per successful run (time, status, last bar, data hash, N/50, epoch).
- The candle cache `data/cache/BTC-USD_1h.parquet` lives in the Actions cache (a new entry per run, restored from the newest one). A missing cache means a full download (about 70 s).

## One-time setup (you)

1. Merge `feat/plan2-paper` into `master` through a pull request, after its `ci` check has passed. Scheduled workflows only run from the default branch.
2. Telegram bot:
   1. In Telegram, open **@BotFather**, send `/newbot`, choose a name and a username ending in `bot`. BotFather replies with the token (digits, a colon, then letters and digits). Keep it private.
   2. Open a chat with your new bot and send it any message (for example `hi`). A bot cannot write to you before you have written to it.
   3. Find your chat ID in a terminal. The token is typed at a hidden prompt, so it ends up neither in your shell history nor in a browser history:

      ```bash
      printf 'Bot token: '; read -rs TELEGRAM_BOT_TOKEN; echo
      curl -sS --fail-with-body "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" | grep -o '"chat":{"id":-*[0-9]*' | sort -u
      unset TELEGRAM_BOT_TOKEN
      ```

      The number after `"chat":{"id":` is your chat ID (a group chat ID starts with `-`). If nothing is printed, send the bot another message and run the three lines again.
3. Store both values as repository secrets. Run each command and paste the value at the prompt (so it does not end up in your shell history):

   ```bash
   gh secret set TELEGRAM_BOT_TOKEN
   gh secret set TELEGRAM_CHAT_ID
   ```

   Without these secrets the runtime still runs and records everything; it only skips Telegram.
4. Set a small Actions budget, for example $5, at <https://github.com/settings/billing> (budgets and alerts, product Actions). With the default $0 budget GitHub stops running the workflow once the month's free minutes are used up, and every missed hour counts against criterion 4; a small budget keeps the runs going and caps what they can cost (see Cost).
5. Start the first run by hand and watch it:

   ```bash
   gh workflow run paper.yml
   gh run watch "$(gh run list --workflow paper.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
   ```

## Reading the results

```bash
git fetch origin paper
git show origin/paper:runs.log | tail -n 5
git show origin/paper:report.html > /tmp/ewbot-paper-report.html && open /tmp/ewbot-paper-report.html
```

Locally, with a worktree of the `paper` branch (`git worktree add ../paper-ledger paper`), `uv run ewbot paper status --ledger-dir ../paper-ledger` prints the go-live block, the open position, the epoch, the last run (data hash, holdout data check, holdout trades recomputed vs the lock's 36) and the discrepancy and pending-event counts.

## Telegram messages

| Message | When |
|---|---|
| 신규 진입 | a new entry fill (setup, side, entry, stop, TP1/TP2, blended R, top count q / S_rev) |
| 손절 이동 (본전 / 추적) | breakeven after TP1, or a structural trailing move |
| 청산 | the fill that closed a trade (reason, PnL, R, out-of-sample N/50) |
| 일일 요약 | first run at/after 09:00 New York time each day: position, equity, N/50, go-live status (criteria 1–2 are recomputed in this run: A/HYBRID vs C/BH/EMA/RANDOM x100). If that recompute fails, the run still completes, a warning is sent and the next run retries once; if the retry fails too, the summary is sent marked `기준 미갱신(stale)` with the previous criteria 1–2 result, and the next try is on the next New York date |
| 경고 | strategy fingerprint change (new epoch), decision discrepancy (with its cause, see Frozen runtime environment), holdout data hash mismatch, run gap over 3 hours |
| 실패 | the failed stage and the Actions log link (sent by `ewbot`; the workflow sends a fallback for checkout / setup / sync / push failures, step timeouts and cancellations) |

Each message has an id `kind:ts:trade_key`; an id is sent at most once per pushed ledger. A run killed after sending, or whose pushes all fail, may send it again in the next run, but a message is never lost. A failed send is retried 3 times and then kept pending for the next run.

## Strict fingerprint policy

The strategy fingerprint covers `types.py`, `config.py`, `indicators.py`, `data/candles.py`, `waves/`, `strategy/`, `engine/`, `execution/`, `risk/` (every byte, comments included). Its baseline is the same file set at the holdout commit `64e628a` (`e0435223a3f9…`). If any of these files changes, the next run opens a new epoch, sends a warning and resets the go-live counter to 0; the holdout's 36 trades no longer count.

Reverting the change does not bring the 36 back. The revert opens yet another epoch, with the baseline fingerprint, so it is eligible for go-live again, but its count starts at 0 paper trades and the holdout's 36 trades never count again.

The guard is `.github/workflows/ci.yml`. On every pull request to `master` and every push to `master` it runs `tests/paper/test_fingerprint.py`, which compares the working tree with the pinned baseline value (no git history needed), and `tests/paper/test_static.py` (the `allow_holdout` and workflow guards). A red `ci` check on a pull request means that merging it resets the counter; on a push to `master` it means the next hourly run will. The check only reports: GitHub merges a pull request with a red check unless you make the `guard` check required in the branch protection settings. On GitHub Free, branch protection rules and rulesets are not available for private repositories, so for a private repository there the `guard` check stays advisory: never merge a pull request whose `guard` check is red.

The precheck also binds the run to the lock's whole configuration: it refuses to run when the lock's `sha256` is not `lock_digest(BotConfig(), params, random_runs, code_sha256)`. A changed `BotConfig` default (in `config.py`) or walk-forward grid therefore stops the hourly runs with a precheck failure alert instead of opening a new epoch. Revert it to resume: no epoch was opened, so epoch 1 and the holdout's 36 trades still count, but the hours without a successful run count against criterion 4's coverage.

## Frozen runtime environment

Each run records its runtime environment in `ledger.json` (`runs[].env`): the Python version, the operating system and machine architecture (`platform.system()` / `platform.machine()`), the numpy, pandas and pyarrow versions, the sha256 of `uv.lock` and the runner image (`ImageOS` / `ImageVersion`, set by GitHub's hosted runners). `ImageVersion` is recorded but not compared: it is left out of the digest, because GitHub refreshes the image weekly and comparing it would hide real `UNEXPLAINED` discrepancies behind `ENVIRONMENT_CHANGE`. Each decision keeps the digest of the environment of the run that recorded it, and `report.html` shows the current one. A decision that no longer reproduces is classified in this order:

1. `DATA_REVISION`: a bar up to the decision changed.
2. `FINGERPRINT_CHANGE`: it was recorded in another epoch.
3. `ENVIRONMENT_CHANGE`: this is the first run since the runtime environment changed (the previous successful run had another one), and the run that recorded the decision (or should have) had another one too. Every run rechecks every recorded decision, so once a run in the new environment has reproduced a decision, a later difference is `UNEXPLAINED`. It is warned on Telegram and listed in the report and in the go-live block, but criterion 4 does not count it as `UNEXPLAINED`.
4. `UNEXPLAINED`: anything else. One is enough to fail criterion 4.

While paper trading runs, keep the environment frozen:

- Do not change `uv.lock`: no `uv lock --upgrade`, no dependency bumps, no merged dependency-update pull requests.
- Do not change the Python version: `python-version: "3.12.13"` in `paper.yml` and `ci.yml` and `requires-python` in `pyproject.toml`.
- Do not change the runner image: `runs-on: ubuntu-24.04` in `paper.yml`, never `ubuntu-latest`.

GitHub updates the `ubuntu-24.04` image itself (a new `ImageVersion`, often weekly). That is recorded in `runs[].env` but does not change the digest, so it never turns a discrepancy into `ENVIRONMENT_CHANGE`. The Python patch is pinned to `3.12.13`, so a different patch (or a new `ImageOS`, operating system, machine architecture, numpy, pandas, pyarrow or `uv.lock`) is what changes the digest, and a discrepancy found by the first run after such a change is `ENVIRONMENT_CHANGE`. Criterion 4 does not count those, so look at every `ENVIRONMENT_CHANGE` in the report yourself: it is a decision that did not reproduce.

## Failures

| Situation | What happens |
|---|---|
| Coinbase fetch fails | 3 tries, then the run stops without a commit and alerts; the next run recomputes the missing bars |
| Cache lost | full download; changed past bars show up as `DATA_REVISION` discrepancies |
| `paper` unreadable at checkout | `git ls-remote` failed (network, auth): the job fails and alerts. Only a definite "no such branch" (first run) starts an orphan `paper`, so an existing ledger is never replaced by a fresh one |
| `ewbot` hangs | `Paper run` and the push step time out after 35 minutes (the daily recompute takes 10–20); the step fails and the workflow's fallback alert goes out. The alert also runs when the job is cancelled |
| Push to `paper` fails | 3 pushes at most, then the job fails and alerts. If `origin/paper` did not move (network, a transient error), the same commit is pushed again after 5 s, then 10 s, without a recompute, so no Telegram message goes out twice. If `origin/paper` already holds the commit, the push landed. Only if `origin/paper` moved is the ledger reset to it, recomputed and pushed on top; that recompute may resend this run's Telegram messages |
| Someone pushes to `paper` | runs never overlap (`concurrency: paper`), so a moved `origin/paper` means a push by hand: the next push recomputes on top of it (row above). Don't push to `paper`; read it with `git fetch` / `git show` (Reading the results) |
| Actions itself stops | not detectable from inside a run: GitHub's failure e-mail and a missing daily summary are the alarm |

## Cost

Inputs: about 1–2 minutes per hourly run, plus the daily criteria 1–2 recompute (103 engine runs, roughly 10–20 minutes on a 2-vCPU runner) inside one run a day. GitHub rounds every job up to a whole minute (each run here is one job), so an hourly run of 1 min 10 s costs 2 minutes.

| Estimate | Per day | 30-day month | 31-day month |
|---|---|---|---|
| low: 23 × 1 + 1 × (1 + 10) | 34 min | 1,020 min | 1,054 min |
| high: 23 × 2 + 1 × (2 + 20) | 68 min | 2,040 min | 2,108 min |

The high end is above the 2,000 free minutes a month of a private repository on GitHub Free, and it is the likely end once an hourly run takes just over a minute. Check the real usage in your GitHub billing settings after the first week.

Do not lower the cost by changing what runs: the 100 RANDOM runs are part of criteria 1–2 (fewer runs change the criteria), and the hourly schedule is what criterion 4 counts. If a month would go over the free minutes, either allow paid Actions minutes (the small budget of One-time setup step 4: with the default $0, GitHub stops running the workflow once the free minutes are used up, and every missed hour counts against criterion 4), or move the job to a self-hosted runner (`runs-on: self-hosted`: no Actions minutes, but that machine has to be online every hour, and its own image becomes the recorded environment).
