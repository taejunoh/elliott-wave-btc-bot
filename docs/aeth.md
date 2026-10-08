# A-ETH (Plan 5a) — strategy A, frozen, on ETH: stage 1, one-shot holdout, paper trading

Spec: `docs/superpowers/specs/2026-10-07-ewbot-plan5a-aeth-design.md`. Plan: `docs/superpowers/plans/2026-10-07-ewbot-plan5a-aeth.md`.

A-ETH runs strategy A exactly as frozen — the params of the committed `holdout.lock` (K4 = 2.0, Q_MIN = 0.7, S_MIN = 0.5), the conservative profile, A's breakers, A's code — on Coinbase `ETH-USD` 1h candles, with A's costs and the contract size of `ETP-20DEC30-CDE` (0.1 ETH). Nothing is tuned and no other asset is tried. It is a separate research and paper track: strategy A's fingerprint (`e0435223…`), `holdout.lock`, BTC 50-trade count, `paper.yml` and live runtime are never touched, and ETH trades are never pooled into A-BTC's count.

Three stages, one attempt each; a failure ends A-ETH:

1. **Stage 1** (2017-01-01 .. 2023-12-31, no 2024 data): A's `compare()` on ETH. Pass = A's net total return > 0, A's max drawdown ≤ 15%, A's Sharpe and Calmar each above C's and above the random-entry control's mean. The 2019–2023 sub-period and fees and slippage × 2 are reported for information only.
2. **Stage 2** (only after a passed stage 1): `holdout-aeth.lock`, then the ETH holdout from 2024-01-01 to the lock's last bar, run once after your approval. Pass = strategy A's `evaluate_criteria`, unchanged: A is a winner (Sharpe and Calmar above C, buy & hold and the random control's mean) and A's max drawdown ≤ 15% (A's go-live items 1 and 2). The trade count is recorded.
3. **Stage 3** (only after a passed stage 2): hourly paper trading of A on ETH (`paper-aeth.yml`, ledger on the orphan branch `paper-aeth`).

## Isolation from strategy A (and B)

- A-ETH's code is `src/ewbot/aeth/` (its own fingerprint: every `*.py` there). A's fingerprinted folders, `backtest/`, `data/coinbase_history.py`, `paper/`, `live/`, `stratb/`, `paperb/`, `holdout.lock`, `paper.yml`, `ci.yml`, `paper-b.yml`, `uv.lock` and `pyproject.toml` are never changed; A-ETH only imports A's modules.
- `ewbot.aeth` is imported only inside the `ewbot aeth …` handlers of `cli.py`: `ewbot live …` and `ewbot paper …` load the same modules as before.
- A-ETH's own artifacts: `data/cache/ETH-USD_1h.parquet`, `aeth-stage1.json`, `holdout-aeth.lock`, the `paper-aeth` branch, `.github/workflows/paper-aeth.yml`, the Actions cache key `eth-usd-1h-` and the Telegram prefix `[ewbot paper-A-ETH]`.
- `tests/aeth/test_isolation.py` checks A's fingerprint, that `aeth/` is outside A's fingerprinted folders, that a branch touching A-ETH touches no strategy-A or strategy-B path, and that only the holdout and the paper runtime read 2024+ candles; `tests/paper/test_static.py` (run by `ci.yml`) carries the CI part.
- **Never run strategy A's `ewbot fetch --product ETH-USD`**: it writes the same cache file up to now. Before the lock, A-ETH refuses a cache that holds 2024+ bars (delete the file and run `ewbot aeth fetch` again).

## Commands

```bash
uv run ewbot aeth fetch                    # ETH-USD 1h cache from 2017-01-01; stops at 2024-01-01 until holdout-aeth.lock exists
uv run ewbot aeth stage1                   # stage 1, ONCE -> aeth-stage1.json (commit it at once)
uv run ewbot aeth holdout lock             # holdout-aeth.lock from the committed, passed aeth-stage1.json (commit it at once)
uv run ewbot aeth holdout run --confirm    # ONCE, after your approval (commit the lock at once)
uv run ewbot aeth paper status --ledger-dir <paper-aeth worktree>   # read-only
```

There is no option for params, product, costs, windows or the random-control size: they are A's, fixed in code and in the lock.

## Stage 1 (controller; network: ETH before 2024 only)

1. On a clean working tree of `master` (`git status --porcelain --untracked-files=no` prints nothing): `uv run ewbot aeth fetch` (about 205 public Coinbase requests, a minute or two; an interrupted download resumes). Check the printed bar count (about 61,000) and `fetch window: stops at 2024-01-01 (no lock)`.
2. `uv run ewbot aeth stage1` (a few minutes). It refuses when `aeth-stage1.json` exists (one attempt), when tracked files have uncommitted changes, when A's fingerprint is not `e0435223…`, and when the bars do not cover 2017-01-01 .. 2023-12-31. Commit `aeth-stage1.json` right away, whatever the result.
3. If stage 1 failed, A-ETH ends here: no lock, no 2024 data, A stays BTC-only, no other asset is tried. Record the result in the spec's end record.

## Stage 2 (controller with your approval)

1. On a clean tree: `uv run ewbot aeth holdout lock`, then commit `holdout-aeth.lock` right away. The lock is refused unless `aeth-stage1.json` is committed unchanged, passed, and was made with A's params (and `holdout.lock` sha256), this A-ETH code, A's frozen fingerprint and the same pre-2024 bars. The lock fixes the product and contract, params, config, holdout start, its last bar (the last complete hour now), the 100-run random control, A's fingerprint, the A-ETH fingerprint, `code_sha256` (every `src/ewbot` `*.py`, like A's lock), the pre-2024 bars and the stage-1 record's sha256.
2. Approval: the user sees the lock and the pass rule and approves running the holdout once.
3. Before the run: the tree is clean, `HEAD` is the lock commit, and the protection tests pass (`uv run --group live pytest tests/paper/test_fingerprint.py tests/paper/test_static.py tests/aeth/test_isolation.py -q`). Then `uv run ewbot aeth holdout run --confirm` (it first extends the cache to now: 2024+ bars are fetched only now) and commit `holdout-aeth.lock` **immediately** (it holds `first_run`). A second run, a changed A or A-ETH or `src/ewbot` file, a lock that is not this code's, other pre-2024 bars or uncommitted changes are refused. Keep `data/cache/ETH-USD_1h.parquet` until paper trading has reproduced the holdout (`holdout data ok` in `ewbot aeth paper status`).
4. If `first_run.verdict.passed` is false, A-ETH ends: no paper trading.

## Paper trading (only after a passed ETH holdout; you)

What runs: `.github/workflows/paper-aeth.yml` recomputes ETH from 2017 with the committed `holdout-aeth.lock`, trading from 2024-01-01, so it reproduces the holdout and continues it. Decisions go to `ledger.json` and `runs.log` on the orphan branch `paper-aeth` (only the workflow writes it). Paper trades are the trades entering after the lock's last bar. The ETH count is the holdout's A trades plus the paper trades, 50 needed, on this ledger only. A change of strategy A's fingerprint or of the A-ETH fingerprint opens a new epoch and restarts the count. There is no `report.html` and no daily criteria 1–2 recompute (Plan 5c judges them once on the whole ETH out-of-sample window).

One-time setup:

1. `master` has the committed, passed `holdout-aeth.lock`. Until step 2 the workflow is dormant (its job only runs when the repository variable `PAPER_AETH_ENABLED` is `true`).
2. `gh variable set PAPER_AETH_ENABLED --body true` (to stop it: `gh variable delete PAPER_AETH_ENABLED`).
3. Hourly trigger: a second Cloudflare Worker (A's worker always dispatches `paper.yml`). **Workers & Pages** → **Create Worker**, name `ewbot-paper-aeth-trigger`, paste the code below, deploy; secret `GITHUB_TOKEN` (the same fine-grained token as A's worker: this repository, Actions read and write); cron trigger `42 * * * *`.

   ```js
   const DISPATCH_URL =
     "https://api.github.com/repos/taejunoh/elliott-wave-btc-bot/actions/workflows/paper-aeth.yml/dispatches";

   export default {
     async scheduled(event, env, ctx) {
       const response = await fetch(DISPATCH_URL, {
         method: "POST",
         headers: {
           Authorization: `Bearer ${env.GITHUB_TOKEN}`,
           Accept: "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28",
           "User-Agent": "ewbot-paper-aeth-trigger",
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

   The workflow's own `schedule` (`42 */3 * * *`) is only a backup.
4. First run by hand: `gh workflow run paper-aeth.yml --ref master`, then `gh run watch`. Check `[ewbot paper-A-ETH]` messages and, in a `paper-aeth` worktree, `uv run ewbot aeth paper status --ledger-dir <dir>`: `holdout data ok` and `holdout trades recomputed` equal to the lock's.
5. Actions usage: A-ETH adds one or two billed minutes per run, 24 hourly and 8 backup runs a day (roughly 1,000–2,000 minutes a month), on top of strategy A's paper runs, which are already near the 2,000 free minutes. Keep the small Actions budget of `docs/paper-trading.md`; do not thin the hourly trigger (criterion 4 counts hourly coverage).

## Failures

- `[ewbot paper-A-ETH] 실패 · 단계 <stage>`: `precheck` (lock missing, not committed, edited, holdout not run or failed, or not this code's lock for A's params), `data` (the candle update failed three times, or the data ends before the lock's last bar), or another stage. Nothing is committed after a failed run.
- `경고: 홀드아웃 구간 데이터 해시 불일치` / `홀드아웃 재현 불일치`: the holdout window's bars changed (a Coinbase revision or a re-download); warned once, never refused; affected decisions are `DATA_REVISION`.
- Discrepancies are classified like A's: `DATA_REVISION`, `FINGERPRINT_CHANGE`, `ENVIRONMENT_CHANGE`, `UNEXPLAINED`.

## Toward live ETH (Plan 5c, never automatic)

Plan 5c starts only after A-BTC's CANARY has passed and A-ETH's stage 3 is met: the passed ETH holdout, at least 50 ETH out-of-sample trades (holdout + paper), at least 28 days of paper with 0 `UNEXPLAINED` discrepancies and ≥ 95% hourly coverage, A's go-live items 1–2 re-judged on the whole ETH out-of-sample window, and your approval. The live runtime trades one product today; account-level breakers and margin are shared with A-BTC; any live code change restarts SHADOW. Confirm ETP liquidity and slippage, the per-contract minimum fee and ETH funding first.
