# Paper-trading runtime (Plan 2) — operations

Spec: `docs/superpowers/specs/2026-10-02-ewbot-plan2-paper-trading-design.md`.

## What runs

- `.github/workflows/paper.yml` runs `uv run ewbot paper run --ledger-dir <paper worktree>` every hour at minute 7 (and on demand). `scripts/paper_sync.sh` does the git side: the `paper` worktree, the commit, the push and its retries.
- Each run recomputes the whole engine deterministically from 2017 with the params in the committed `holdout.lock` (K4 = 2.0, Q_MIN = 0.7, S_MIN = 0.5), conservative risk, breakers on and Plan 1 costs. Trading starts at the holdout start (2024-01-01), so the run reproduces the holdout's A run and continues it.
- Paper trades are the trades that **enter after the holdout's last bar** (`entry_ts > 1790938800`, 2026-10-02 11:00 UTC). A position already open at that bar belongs to the holdout and is never counted twice.
- Results go to the orphan branch `paper` (never to `master`). Only the workflow writes to `paper`: never push to it by hand (see Failures).
  - `ledger.json`: baseline fingerprint and commit, holdout summary, epochs, closed trades, open position, decisions, discrepancies, runs, sent and pending Telegram events, the last criteria 1–2 result.
  - `report.html`: go-live status at the top, out-of-sample equity and drawdown, trade tables with snapshot charts, current top count, discrepancy list, epochs.
  - `runs.log`: one line per successful run (time, status, last bar, data hash, N/50, epoch).
- The candle cache `data/cache/BTC-USD_1h.parquet` lives in the Actions cache (a new entry per run, restored from the newest one). A missing cache means a full download (about 70 s).

## One-time setup (you)

1. Merge `feat/plan2-paper` into `master`. Scheduled workflows only run from the default branch.
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
4. Start the first run by hand and watch it:

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
| 일일 요약 | first run at/after 09:00 New York time each day: position, equity, N/50, go-live status (criteria 1–2 are recomputed in this run: A/HYBRID vs C/BH/EMA/RANDOM x100; if that recompute fails the run still completes, a warning is sent once, and the summary comes from the next run that recomputes successfully) |
| 경고 | strategy fingerprint change (new epoch), decision discrepancy, holdout data hash mismatch, run gap over 3 hours |
| 실패 | the failed stage and the Actions log link (sent by `ewbot`; the workflow sends a fallback for checkout / setup / sync / push failures, step timeouts and cancellations) |

Each message has an id `kind:ts:trade_key`; an id is sent at most once. A failed send is retried 3 times and then kept pending for the next run.

## Strict fingerprint policy

The strategy fingerprint covers `types.py`, `config.py`, `indicators.py`, `data/candles.py`, `waves/`, `strategy/`, `engine/`, `execution/`, `risk/` (every byte, comments included). Its baseline is the same file set at the holdout commit `64e628a`. If any of these files changes, the next run opens a new epoch, sends a warning and resets the go-live counter to 0; the holdout's 36 trades no longer count. `tests/paper/test_fingerprint.py::test_baseline_recomputes_from_git_and_equals_the_working_tree` fails in that case, before anything is merged.

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

Do not lower the cost by changing what runs: the 100 RANDOM runs are part of criteria 1–2 (fewer runs change the criteria), and the hourly schedule is what criterion 4 counts. If a month would go over the free minutes, either allow paid Actions minutes (set a budget above $0: with the default $0, GitHub stops running the workflow once the free minutes are used up, and every missed hour counts against criterion 4), or move the job to a self-hosted runner (`runs-on: self-hosted`: no Actions minutes, but that machine has to be online every hour).
