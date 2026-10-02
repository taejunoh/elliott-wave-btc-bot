# Paper-trading runtime (Plan 2) — operations

Spec: `docs/superpowers/specs/2026-10-02-ewbot-plan2-paper-trading-design.md`.

## What runs

- `.github/workflows/paper.yml` runs `uv run ewbot paper run --ledger-dir <paper worktree>` every hour at minute 7 (and on demand).
- Each run recomputes the whole engine deterministically from 2017 with the params in the committed `holdout.lock` (K4 = 2.0, Q_MIN = 0.7, S_MIN = 0.5), conservative risk, breakers on and Plan 1 costs. Trading starts at the holdout start (2024-01-01), so the run reproduces the holdout's A run and continues it.
- Paper trades are the trades that **enter after the holdout's last bar** (`entry_ts > 1790938800`, 2026-10-02 11:00 UTC). A position already open at that bar belongs to the holdout and is never counted twice.
- Results go to the orphan branch `paper` (never to `master`):
  - `ledger.json`: baseline fingerprint and commit, holdout summary, epochs, closed trades, open position, decisions, discrepancies, runs, sent and pending Telegram events, the last criteria 1–2 result.
  - `report.html`: go-live status at the top, out-of-sample equity and drawdown, trade tables with snapshot charts, current top count, discrepancy list, epochs.
  - `runs.log`: one line per successful run (time, status, last bar, data hash, N/50, epoch).
- The candle cache `data/cache/BTC-USD_1h.parquet` lives in the Actions cache (a new entry per run, restored from the newest one). A missing cache means a full download (about 70 s).

## One-time setup (you)

1. Merge `feat/plan2-paper` into `master`. Scheduled workflows only run from the default branch.
2. Telegram bot:
   1. In Telegram, open **@BotFather**, send `/newbot`, choose a name and a username ending in `bot`. BotFather replies with the token (digits, a colon, then letters and digits). Keep it private.
   2. Open a chat with your new bot and send it any message (for example `hi`). A bot cannot write to you before you have written to it.
   3. Find your chat ID: in a browser, open `https://api.telegram.org/bot<TOKEN>/getUpdates` (replace `<TOKEN>` with the token). In the JSON, the number right after `"chat":{"id":` is your chat ID (a group chat ID starts with `-`). If the result is empty, send the bot another message and reload.
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
| 실패 | the failed stage and the Actions log link (sent by `ewbot`; the workflow sends a fallback for checkout/setup/push failures) |

Each message has an id `kind:ts:trade_key`; an id is sent at most once. A failed send is retried 3 times and then kept pending for the next run.

## Strict fingerprint policy

The strategy fingerprint covers `types.py`, `config.py`, `indicators.py`, `data/candles.py`, `waves/`, `strategy/`, `engine/`, `execution/`, `risk/` (every byte, comments included). Its baseline is the same file set at the holdout commit `64e628a`. If any of these files changes, the next run opens a new epoch, sends a warning and resets the go-live counter to 0; the holdout's 36 trades no longer count. `tests/paper/test_fingerprint.py::test_baseline_recomputes_from_git_and_equals_the_working_tree` fails in that case, before anything is merged.

## Failures

| Situation | What happens |
|---|---|
| Coinbase fetch fails | 3 tries, then the run stops without a commit and alerts; the next run recomputes the missing bars |
| Cache lost | full download; changed past bars show up as `DATA_REVISION` discrepancies |
| Push conflict on `paper` | fetch the newest `paper`, recompute, push again (3 attempts) |
| Actions itself stops | not detectable from inside a run: GitHub's failure e-mail and a missing daily summary are the alarm |

## Cost

About 1–2 minutes per hourly run, plus the daily criteria 1–2 recompute (103 engine runs, roughly 10–20 minutes on a 2-vCPU runner): roughly 1,100–1,500 minutes a month, within the 2,000 free minutes of a private repository. If it gets close, lower the daily cost first (do not change the hourly schedule).
