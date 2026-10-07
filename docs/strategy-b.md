> **Status (2026-10-07): ENDED.** Strategy B failed its walk-forward twice (B1, and a pre-registered B2 retry with a 4h trend filter); no holdout was run. See spec section 12. Do not enable `PAPER_B_ENABLED`.

# Strategy B (Plan 4) — research, one-shot holdout, paper trading

Spec: `docs/superpowers/specs/2026-10-07-ewbot-plan4-strategy-b-design.md`. Plan: `docs/superpowers/plans/2026-10-07-ewbot-plan4-strategy-b.md`.

Strategy B takes the swing structure from 1h bars (built from 15m bars, A's ATR ZigZag), waits for a retracement into a Fibonacci zone that overlaps an earlier swing, and enters on the next 15m open after a 15m RSI divergence and a breakout close. Half is taken at TP1, the rest runs to the leg's extension with a breakeven or original stop, everything is closed after 48 hours. It trades BTC and ETH with one shared parameter set (spec revised 2026-10-07, before any holdout run): at most one position per asset, 0.5% of the shared account equity per trade, leverage cap 3x per position and 3x for all positions together, A's breakers (-3% day, -15% total) on the account, and A's costs with each asset's contract size.

## Assets and contracts

| Asset | Signals (Coinbase spot, 15m) | Modelled contract (Plan 5 trades it) | Contract size | Tick |
|---|---|---|---|---|
| BTC | `BTC-USD` | `BIP-20DEC30-CDE` | 0.01 BTC | $5 |
| ETH | `ETH-USD` | `ETP-20DEC30-CDE` | 0.1 ETH | $0.5 (intraday margin about 10%) |

When both assets act on the same 15m bar, every step of the bar goes BTC first, then ETH, and queued exits fill before queued entries. The backtest, like A's, does not round prices to ticks (Plan 5's live orders will). To confirm before live trading (Plan 5): the ETP minimum fee per contract (A's value is kept; A's cost model does not apply a minimum fee) and ETH funding (A's 10% a year is assumed for both assets).

## Isolation from strategy A

- B's code lives in `src/ewbot/stratb/` (the B fingerprint hashes every `*.py` there) and `src/ewbot/paperb/` (the paper-B runtime). A's fingerprinted folders, `paper/`, `backtest/`, `live/`, `holdout.lock`, `paper.yml`, `ci.yml`, `scripts/` and `uv.lock` are never changed; B only imports from them.
- The strategy-A modules B imports, directly or through other modules (42 files today: A's config defaults, costs, sizing, breakers, ZigZag, metrics, buy & hold, walk-forward windows, data hashing and the `paper/` modules paper-B uses; not `live/`, `cli.py` or A's paper runner), are hashed into `deps_sha256` (`ewbot.stratb.fingerprint.deps_fingerprint`). `holdout-b.lock` binds it: after the lock, a change to any of them - an A maintenance change included - makes the holdout and paper-B refuse to run, and so does a new import of another A module from `paperb/`. Check with `uv run python -c "from ewbot.stratb.fingerprint import deps_files; print(*sorted(deps_files()), sep=chr(10))"`.
- B has its own cache files (`data/cache/BTC-USD_15m.parquet`, `data/cache/ETH-USD_15m.parquet`), lock (`holdout-b.lock`), ledger branch (`paper-b`), workflow (`.github/workflows/paper-b.yml`) and Actions cache key (`stratb-15m-`).
- `tests/stratb/test_isolation.py` checks A's fingerprint, that B is outside A's fingerprinted folders and that no A path changed since `master`; `tests/paper/test_static.py` (run by `ci.yml`) carries the CI part.

## Commands

```bash
uv run ewbot stratb fetch                                   # BTC and ETH 15m caches; stop at 2024-01-01 until holdout-b.lock exists
uv run ewbot stratb backtest --params 2.0,0.382,0.618,1.0,1.272,breakeven   # one BTC + ETH run, 2017..2023 only
uv run ewbot stratb walkforward                             # 32 combos on the BTC + ETH portfolio, A's windows and rules -> reports/stratb-walkforward.json
uv run ewbot stratb holdout lock --params-from reports/stratb-walkforward.json   # writes holdout-b.lock
uv run ewbot stratb holdout run --confirm                   # ONCE, after your approval
uv run ewbot stratb paper status --ledger-dir <paper-b worktree>   # read-only
```

`--params` is `zz_k,z_lo,z_hi,tp1_r,ext,remainder`. No research command can read a bar from 2024-01-01 on before the lock exists: the 15m loader filters them out while reading the parquet file and the fetcher does not download them.

## Research and holdout (in this order)

1. `uv run ewbot stratb fetch` (about 820 requests per asset to the public Coinbase API, a few minutes each; an interrupted download resumes from the last saved page). Check the printed missing-bar counts.
2. `uv run ewbot stratb walkforward`. It scores the 32 combos on the BTC + ETH portfolio and prints the stitched out-of-sample metrics, `chosen_final` and `proceed`. If `proceed` is false (negative out-of-sample return, or the last fold had no selectable combo), strategy B ends here (spec 6.3). The report also records what it ran on: each asset's bar hashes (`wf_data`), the B fingerprint (`code_sha256`) and `deps_sha256`.
3. On a clean working tree (`git status --porcelain --untracked-files=no` prints nothing): `uv run ewbot stratb holdout lock --params-from reports/stratb-walkforward.json`, then commit `holdout-b.lock` right away and note the commit (`git rev-parse HEAD`). The lock is refused when tracked files have uncommitted changes, or when the report does not proceed or was made on other bars, other B code or other A modules (re-run the walk-forward). The lock fixes the combo, the run config (assets included), the grid, the B fingerprint, `deps_sha256`, the hash of each asset's pre-2024 bars, the report's sha256 and the holdout's last bar (the last 15m bar of the last complete hour at lock time).
4. Ask the user for approval. Only then, before the run: the working tree is clean, `HEAD` is the lock commit, and the A-protection tests pass (`uv run --group live pytest tests/paper/test_fingerprint.py tests/paper/test_static.py tests/stratb/test_isolation.py -q`). Then `uv run ewbot stratb holdout run --confirm`, and commit `holdout-b.lock` **immediately** (it now holds `first_run`; until that commit, discarding the file would allow a second run - never do that). The holdout runs once: a second run, a changed B file, changed A modules (`deps_sha256`), a lock whose keys, grid, config, holdout start or replay anchor are not this code's, uncommitted changes to tracked files, or other pre-2024 bars of either asset are refused. B, the random control, the cost run and buy & hold are all measured on the same hours, from the account's value at 2024-01-01 00:00. The criteria are the portfolio's: Sharpe above the 50/50 BTC/ETH buy & hold and above the random control's 95th percentile (random entries per asset at each asset's own frequency), max drawdown at most 15%, at least 50 trades of both assets together, and a net profit with costs x 1.5. Keep `data/cache/` (the holdout window's 15m bars exist only there) until paper-B has reproduced the holdout (`ewbot stratb paper status`: `holdout data ok` and `holdout trades recomputed` equal to the lock's).
5. If `first_run.criteria.passed` is false, strategy B ends (spec 6.5): no paper trading, no retuning on the same data.

## Paper trading (only after a passed holdout)

What runs: `.github/workflows/paper-b.yml` recomputes both assets from 2023-10-01 (the replay anchor) with the params in the committed `holdout-b.lock`, records the decisions in `ledger.json` and `runs.log` on the orphan branch `paper-b` (only the workflow writes it: never push to it by hand), and sends Telegram messages prefixed `[ewbot paper-B]` with the same bot and secrets as strategy A; each trade message names its asset (`B-BTC`, `B-ETH`). Paper trades are the trades of either asset that enter after the lock's last bar; 30 are needed in all (spec 7.1), with no unexplained discrepancy. A change of any file in `src/ewbot/stratb/` opens a new epoch and restarts the count; a change of the strategy-A modules B imports (`deps_sha256`) stops paper-B at the precheck instead. Each run ends at the latest 15m bar both assets have: when one asset's newest bar is late, the other's newer bars wait for the next run (the run records a warning).

One-time setup (you):

1. Make sure `master` has the committed, passed `holdout-b.lock` (the workflow reads it from `master`). Until step 2 the workflow is dormant: its job only runs when the repository variable `PAPER_B_ENABLED` is `true`, so the backup schedule neither runs nor alerts before the holdout passed.
2. Switch it on: `gh variable set PAPER_B_ENABLED --body true` (to stop paper-B later: `gh variable delete PAPER_B_ENABLED`).
3. Hourly trigger: create a second Cloudflare Worker (A's worker stays as it is: its code always dispatches `paper.yml`, so a second cron line in it would start A's workflow twice). In **Workers & Pages** → **Create Worker**, name it `ewbot-paper-b-trigger`, paste the code below, deploy; add the secret `GITHUB_TOKEN` (the same fine-grained token as A's worker: this repository, Actions read and write); add the cron trigger `47 * * * *`.

   ```js
   const DISPATCH_URL =
     "https://api.github.com/repos/taejunoh/elliott-wave-btc-bot/actions/workflows/paper-b.yml/dispatches";

   export default {
     async scheduled(event, env, ctx) {
       const response = await fetch(DISPATCH_URL, {
         method: "POST",
         headers: {
           Authorization: `Bearer ${env.GITHUB_TOKEN}`,
           Accept: "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28",
           "User-Agent": "ewbot-paper-b-trigger",
           "Content-Type": "application/json",
         },
         body: JSON.stringify({ ref: "master" }),
       });
       if (!response.ok) {
         const text = await response.text();
         console.error(`workflow_dispatch failed: HTTP ${response.status} ${text}`);
         throw new Error(`workflow_dispatch failed: HTTP ${response.status}`);
       }
     },
     async fetch() {
       return new Response("Not found", { status: 404 });
     },
   };
   ```

   The workflow's own `schedule` (`47 */3 * * *`) is only a backup.
4. Start the first run by hand: `gh workflow run paper-b.yml --ref master`, then `gh run watch`.
5. Actions usage (spec 7): after the first week, compare the minutes used by `paper-b` with the free allowance at <https://github.com/settings/billing>. If the month would run over, change the worker's cron to `47 */2 * * *` (every 2 hours): decisions do not change (every run recomputes everything), only the messages come later.

## Failures

- A failed run sends `[ewbot paper-B] 실패 · 단계 <stage>` with the log link (the workflow's fallback alert covers checkout, setup and push failures). Nothing is committed after a failed run; the next run recomputes everything.
- Stage `precheck`: `holdout-b.lock` is missing, not committed, edited, not run, failed, its keys, grid, config, holdout start or replay anchor are not the code's, or the strategy-A modules B imports changed since the lock (`deps_sha256`: revert the A change; paper-B has no re-lock). Stage `data`: the candle update failed three times, or an asset's data ends before the lock's last bar.
- `[ewbot paper-B] 경고: 홀드아웃 구간 데이터 해시 불일치`: the 15m bars of the holdout window (either asset) no longer hash to the holdout run's (a Coinbase candle revision, or a full re-download after the Actions cache was evicted). The run is not refused (spec 8 as amended 2026-10-07): it warns once per distinct hash, and any recorded decision that changes because of it is classified `DATA_REVISION`. `ewbot stratb paper status` shows `holdout data MISMATCH` until the data is back.
- Discrepancies (`[ewbot paper-B] 경고: 결정 불일치`) are classified like A's: `DATA_REVISION`, `FINGERPRINT_CHANGE`, `ENVIRONMENT_CHANGE`, `UNEXPLAINED`. Any `UNEXPLAINED` one blocks the go-live condition.

## Go-live (Plan 5, never automatic)

Holdout passed, at least 30 paper trades of BTC and ETH together after the lock's last bar, zero unexplained discrepancies in the paper period, and your explicit approval. Live trading of B (BIP and ETP contracts) needs its own Coinbase account or portfolio (A and B cannot share one BIP position) and is Plan 5.
