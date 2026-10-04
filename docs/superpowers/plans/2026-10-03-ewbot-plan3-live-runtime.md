# ewbot Plan 3 — Live-Trading Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Trade the frozen holdout strategy (A, K4 = 2.0, Q_MIN = 0.7, S_MIN = 0.5 from `holdout.lock`, conservative risk) on the Coinbase CFM nano Bitcoin perp-style future `BIP-20DEC30-CDE` from a US-East VPS, through the stages SHADOW (virtual, previewed orders) → CANARY (always 1 contract) → FULL (frozen sizing, 3-contract cap for 30 days), so that the live decisions provably equal the paper runtime's, every position stays protected through restarts, outages and gaps, and the user can pause or flatten from Telegram at any time.

**Architecture:** Every hour at hh:00:30 the runtime replays the whole frozen engine exactly like the paper runtime (`run_backtest(cfg, df, HOLDOUT_START, None, allow_holdout=True)` inside the paper trace) on the frame plus one flat placeholder bar, and reads the target after the bar that just closed — open position, its stop and TPs, the exit or entry queued at that close (`live/target.py`). A pure planner (`live/reconcile.py`) turns target × exchange state into actions; an executor (`live/executor.py`) sends them through an `Exchange` protocol with intent-before-send idempotency (deterministic `client_order_id`, SQLite journal) — `CoinbaseExchange` (SDK 1.8.4) in CANARY/FULL, `ShadowExchange` (real previews, virtual book) in SHADOW, `SimExchange` in every test. A one-second main loop adds the watchdog (stop gap, margin, account breakers, session), Telegram commands, the external heartbeat, the daily summary and the hourly audit against the paper ledger. No strategy-fingerprinted file is edited: new code lives in `src/ewbot/live/`, plus `live` sub-commands in `cli.py`, `deploy/live/` and an ops runbook.

**Tech Stack:** Python 3.12.13 (uv), the existing dependencies (numpy, pandas, pyarrow, requests, PyYAML) plus `coinbase-advanced-py==1.8.4` (REST) and `websockets==13.1` (the price feed's client, already the SDK's own dependency) in a new `live` dependency group (the SDK brings cryptography, PyJWT, websockets 13.1, backoff); SQLite (stdlib `sqlite3`, WAL); pytest; Docker (`python:3.12.13-slim-bookworm`, `ghcr.io/astral-sh/uv:0.12.13`, both pinned by digest) with Docker Compose on Ubuntu 24.04; Telegram Bot API (`sendMessage`, `getUpdates`); healthchecks.io; age + rclone (Cloudflare R2) for backups.

**Spec:** `docs/superpowers/specs/2026-10-03-ewbot-plan3-live-trading-design.md` (parents: `docs/superpowers/specs/2026-10-01-elliott-wave-btc-bot-design.md`, `docs/superpowers/specs/2026-10-02-ewbot-plan2-paper-trading-design.md`). The spec's §2 Coinbase API facts were researched on 2026-10-03, the day this plan was written, so they stand in for the re-check required by spec §6.1; this plan additionally verified the SDK 1.8.4 source (generic `RESTClient.get/post`, `WSClient` - not used: it opens wss:// without verifying the certificate or the host name -, JWT signing) and recorded the real public `GET /market/products/BIP-20DEC30-CDE` and `/market/product_book` responses used as fixtures.

## Global Constraints

- Work on branch `feat/plan3-live` (from `docs/live-design`, or from `master` once PR #5 with the spec and this plan is merged), from the repository root; the venv must be Python 3.12.13 (`uv run python --version`; if not, `uv venv --python 3.12.13 && uv sync`).
- Tests: `uv run pytest …` in Tasks 1–2, `uv run --group live pytest …` from Task 3 on (it keeps the `live` group installed; a plain `uv sync` later would remove it).
- Never modify a strategy-fingerprinted file (Plan 2 spec §5.1, Plan 3 spec §3.1): `src/ewbot/types.py`, `config.py`, `indicators.py`, `data/candles.py`, `waves/**`, `strategy/**`, `engine/**`, `execution/**`, `risk/**`. Never modify `holdout.lock`, `backtest/**`, `data/coinbase_history.py`, `src/ewbot/paper/**` or `.github/workflows/**`. Allowed: create `src/ewbot/live/**`, `tests/live/**`, `deploy/live/**`, `docs/live-trading.md`; modify `src/ewbot/cli.py`, the one `ALLOWED_TRUE` line of `tests/paper/test_static.py` (Decision D2) and `pyproject.toml` + `uv.lock` only through `uv add --group live coinbase-advanced-py==1.8.4` (Task 3) and `uv add --group live websockets==13.1` (Task 8; Decision D1).
- Fingerprint `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1` over 25 files and lock `sha256` `d1be04baa06c58dd483c88e053841d6eda56c1a560fd3196746bab311ece3c45` are pinned in `live/preflight.py`; the runtime refuses to start when either differs, or when the API key can transfer, or when the product is not 0.01 BTC / $5 tick / whole contracts.
- Frozen inputs: params only from `holdout.lock`; config = `holdout_config(BotConfig(), params)`; replay from the first cached bar with `start_ts = HOLDOUT_START`; signals from the spot `BTC-USD` hourly candles of the public Coinbase Exchange API (`data.coinbase_history.update_cache`, as the paper runtime); no CLI option changes any of it.
- Product `BIP-20DEC30-CDE`: `contract_size 0.01`, `price_increment 5`, `base_increment 1`, margin rates intraday ≈ 0.10, overnight 0.21025 long / 0.256625 short (recorded 2026-10-03). Orders: `market_market_ioc` entries/closes, `trigger_bracket_gtc` protection, `close_position` exits, `orders/preview` in SHADOW.
- Stages: SHADOW = no real order, CANARY sizing (1 contract, skipped above 2% risk) on virtual fills with real previews; CANARY = 1 contract, skip when the 1-contract risk > 2% of equity; FULL = `risk.sizing.size_contracts(real equity, entry, stop, direction, CONSERVATIVE, risk_mult, costs)` capped at 3 until FULL has run 30 days without a CANARY failure (each one restarts the 30 days; spec §6.2 `이상이 없으면`); every entry and adoption is sized by the more conservative of the running stage and the journal's (a demotion applies to the next order at once).
- Timing: cycle at hh:00:30 for the bar that closed at hh:00, retried every 15 s, held at +120 s (protection kept); an entry is never sent later than +300 s after its bar's close; poll every 10 s while a trade is open or an intent is pending (60 s otherwise); stop gap = price beyond the stop's 5% limit for 10 s (Coinbase places a triggered bracket stop as a limit 5% beyond it, Create Order reference; spec §4.2 assumed 1%); unprotected > 60 s = major incident; WebSocket silent > 60 s → REST price every 10 s (no price once the last good one is older than 60 s; a connection without a message for 15 s is reopened, a feed without a futures price for 5 minutes while the market is open is alerted); heartbeat every 60 s (healthchecks.io period 1 min, grace 2 min; Telegram unusable - 10 minutes of failures, a stuck sender or 3 refused messages in a row → a `/fail`, success pings withheld at most 30 minutes, the `/fail` repeated every 3 hours while it lasts); Telegram alerts are queued and sent by a background thread (never awaited; a protective action goes before its alert), commands come only from the user's private chat, a command sent more than 120 s before the loop reads it is ignored and `/flatten_confirm` counts within 60 s of `/flatten` by the messages' dates; daily summary 09:00 New York; overnight-margin report 15:30 New York; paper audit from minute 50 of each hour (up to the last bar whose cycle ended OK).
- Safety limits: one position; margin projected at the overnight rate: entry refused above 70%, warning at 80%, reduce back to ≤ 70% at 90% (only on a real futures price, never on a stand-in); real-account breakers −3% from the New York day's opening equity and −15% from the peak → flatten and pause, each reference the median of three balance reads (the day's first three; after a `/resume` that re-based it, that read and the next two) and each breaker tripping on two breaching reads in a row (a balance that is not a finite amount above 0 is no reading, and three in a row are an incident that pauses entries; `/resume` re-bases only the breaker that tripped).
- Secrets only in `/etc/ewbot/cdp_api_key.json` and `/etc/ewbot/live.env` (mode 600, refused otherwise), mounted read-only; never in the repository, the image or a log; HTTP exceptions that may carry a token are reduced to their type name (healthchecks.io gets an exception's type name only; Telegram texts, loop errors and tracebacks go through the settings' redaction; a `LiveSettings` repr shows no secret).
- Tests never touch the network, a real account, the real `data/cache` or `<repo>/holdout.lock`: `SimExchange`, recorded JSON fixtures, fake sessions, synthetic candles (`tests.paper.helpers.synth_frame`) and temp-dir locks. Exception: `check_fingerprint()` hashes the working tree's strategy files (read-only), as the paper fingerprint test does.
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Decisions for the controller (sign off before Task 3 / before merging)

**D1 — the SDK dependency changes `uv.lock`, whichever way it is added; use a `live` dependency group (recommended).** Verified 2026-10-03 with uv 0.12.13 in a scratch copy: `uv add coinbase-advanced-py==1.8.4` (main dependency), `uv add --optional live …` (extra) and `uv add --group live …` (PEP 735 group) all rewrite `uv.lock` the same way — additively (+226 lines: `coinbase-advanced-py`, `cryptography`, `cffi`, `pycparser`, `pyjwt`, `websockets 13.1`, `backoff`), no existing package version changed, no line removed. The paper runtime's environment digest includes `uv_lock_sha256` (`paper/environment.py`), so after the merge the first paper run sees a new environment: a discrepancy found in that one run (none is expected) would be classified `ENVIRONMENT_CHANGE` instead of `UNEXPLAINED`; every later run is classified normally, the epoch and the criterion-4 28-day clock are not touched (only a fingerprint change opens an epoch). The group is the better of the three: `uv sync --frozen` in `paper.yml` and `ci.yml` installs the default groups only (`dev`), so the paper and CI runners keep exactly today's packages (no SDK, no `cryptography` on a job holding `contents: write`), while the Docker image installs `--group live` from the same lock. The zero-change alternative — leave `uv.lock` alone and install the SDK in the image from a hash-pinned `deploy/live/requirements-live.txt` — removes even the one-run window but creates a second lock that can drift from `uv.lock`, and the SDK compatibility test would only run where someone installed the SDK by hand; choose it only if the one-run `ENVIRONMENT_CHANGE` window is unacceptable. Merge timing: right after a paper run with 0 new discrepancies, then check the next run (Task 16 Step 2). Task 8 declares `websockets==13.1` in the same group (the price feed's client; already locked as the SDK's dependency): `uv.lock` changes only in the project's own group metadata, no package or version.

**D2 — `live/target.py` joins the `allow_holdout=True` allowlist.** `tests/paper/test_static.py` asserts the exact set of modules that may replay into the holdout period (`{"backtest/holdout.py", "paper/runner.py"}`). The live runtime has to replay exactly like the paper runtime, so the set grows by exactly this one module (one line in Task 4). Calling `TradingEngine` directly instead would bypass the guard silently. No file under `src/ewbot/paper/` changes.

**D3 — the paper messages keep their prefix.** Spec §3.5 names the prefixes `[LIVE]` and `[PAPER]`; the paper runtime's messages start with `[ewbot paper]` and changing that would edit paper behaviour. The live runtime uses `[LIVE]`; the two stay distinguishable.

## Spec Interpretations (decided here, binding for the tasks)

1. **Target at the latest bar.** The engine never evaluates entries on a run's last bar, so the replay gets one placeholder bar T+1h (flat at T's close, volume 0, synthetic). Everything decided at or before T's close is computed before the engine reaches T+1h, so it equals a replay with the real next bar; Task 4's tests prove it at every event bar of the synthetic series and on a sampled sweep of the other holdout bars (every 23rd, every CFM halt bar and the bar before it). Queued exits are recorded by swapping the engine's per-run class `_Run` (looked up at run time, like the paper trace swaps `PaperBroker` / `WaveEngine`) for a subclass that logs each fresh `_request_exit(reason)` with its bar. Nothing fills during the CFM halt (Fri 17–18 New York), so the target reports the exit still pending for the open position, not only one queued at T: an exit queued at the Fri 16:00 close is still `exit_reason` at the halt bar T = 17:00 (`exit_queued_ts` = 16:00 tells a carried exit from a new one), and a stop the halt bar's spot range crossed is flagged (`stop_pending_reopen`: the paper broker stops the position out at the next tradable open). Reconcile closes on both (`STOP_HALT` for the second) at the first poll that sees the session OPEN. The configuration is not a parameter: `compute_target(lock, df, T)` builds `holdout_config(BotConfig(), params)` from the holdout lock (checked like the paper precheck, without its git checks) and always replays from `HOLDOUT_START`. Nothing at T+1h is used.
2. **Trade identity.** A live trade is identified by its signal bar (`trade_id = signal ts`; the paper trade key is `trade_id + 1h`). Client order ids: entry `ewb-e-<signal ts>-<L|S>`, brackets `ewb-b-<trade>-<tp1|tp2>-<generation>`, closes `ewb-x-<trade>-<n>` (`ewb-x-0-<n>` for a position the journal does not know), reductions `ewb-r-<trade>-<n>`. Within the hour after an entry the replay still shows it as queued (`target.entry`): that queued entry is the engine position of the live trade (`reconcile.pending_position`), never a reason to close. Client order ids are never reused: a trade rebuilt or taken over under the id of an earlier trade goes on from the journal's highest bracket generation and close number for that id (a reused id would resolve to the earlier, filled order).
3. **Never chase.** Only an entry queued at the latest bar can open a trade, and only within 5 minutes of that bar's close; an entry that was skipped (pause, session, size, margin, breaker, missing TP2, a flip whose close was not confirmed, no quotes), refused or unfilled is recorded once and never sent again - the executor refuses a skipped signal even when a later pass plans it again; an entry after a close (a flip) or a cancel of the same pass goes out only once no bracket of ours rests (else it is skipped); a live trade closed early by its bracket is not re-entered. A position the journal does not know is adopted when the engine holds the same direction, else closed (spec §4.4: the actual position follows the target).
4. **Levels.** Futures level = `to_tick(spot level × futures mid / spot mid, $5)`, rounded down for a long and up for a short — for every level: the spec's stop rule, and for a TP it moves the limit toward the entry so the live TP fills no later than the engine's strict-cross TP. The ratio is taken when the order is sent; resting legs are re-placed only when the engine's stop (spot) changes, never for basis drift. Without quotes the stop in force stays (the new engine stop is not recorded) and the move is sent at the first poll with quotes; a foreign position taken over without quotes is protected at the spot levels (ratio 1.0, alerted as approximate) and re-mapped at the first poll with quotes.
5. **Protection and TP1** (spec §4.2, §4.4). Legs: 1 contract → one bracket (TP1 + stop); more → TP1 size (`engine.position_mgr._tp1_split`) + stop, and the rest + TP2 + stop; after the live TP1 one bracket (rest + TP2) with the stop tightened to at least the live entry price (break-even). A 1-contract CANARY trade therefore exits whole at TP1 while the engine keeps its remainder. When the engine's TP1 has filled but the live TP1 has not, what the resting TP1 leg has left is closed at market (`Reduce TP1_SYNC`, only while the session is OPEN and the TP1 leg rests - a vanished one may already have filled; until then both legs stay). A signal without TP2 is skipped (strategy A always sets one). A smaller live position counts as a bracket fill only when our resting legs still cover exactly the position: the TP1 leg used up is the live TP1 (break-even); a partial TP1 keeps the TP1 leg for the rest; any other reduction is unexplained (`UNEXPLAINED_REDUCTION`: alert, the stop stays, the rest is protected). More contracts than the trade holds (a manual order, a reduction that did not fill) are reduced at market while the session is OPEN (`Reduce EXCESS`, spec §4.4) - until then, or while another reduction is pending, the legs cover the whole position (brackets only reduce); a position taken over is capped at the stage maximum (SHADOW/CANARY 1, FULL 3 until the cap is released) and the rest reduced. A reduction the venue refuses because our brackets already cover the position (`CLOSE_CONFLICTS`) makes room first - the TP1 leg is cancelled for `TP1_SYNC`, the legs shrink to the size after it for `EXCESS` / `MARGIN` (placed first) - and is resent under a new id; when it does not fill the legs go back for the whole position. Legs never exceed the position, and a close waiting for the session keeps missing legs placed at the stop in force. A TP1 leg emptied by its own stop (both legs share it) reads as the live TP1: the rest's break-even stop is then through the market and fills at once; if the venue refuses it, the old leg (its stop triggered already) stays, with the watchdog as the backstop (runbook).
6. **Stop moves.** Working protection is never removed before its replacement rests. Replace first (new bracket, then cancel the old one; brackets only reduce, spec §4.2); only when the venue refuses the replacement with a known overlap refusal code (`OVERLAP_REFUSALS`, to be confirmed in CANARY) while our brackets plus the new leg really exceed the position is the old one cancelled first - the gap is tracked as unprotected time (`LEG_GAP`, the designed path on such a venue, not an order failure) and a replacement that then fails puts the old terms back at once (`LEG_RESTORED`; a known narrow limit: if the old leg's cancel is only queued and the venue still counts it, the replacement and the restore can both be refused as overlaps and that leg is missing until the next pass, tracked as unprotected time). A replacement whose outcome is unknown (429, 5xx, timeout) or that is refused for any other reason (order entry disabled, a price, an unknown code) keeps the old leg (`BRACKET_FAILED`) and the move is tried again on the next pass; an unconfirmed one found among the open orders replaces the old leg at once. A resting bracket of ours the trade does not list (accepted right before a crash, an unconfirmed replacement that reached the venue) is taken over by its client order id and terms (`LEG_ADOPTED`; never one whose cancel is queued); other brackets of ours are cancelled only once every wanted leg rests; the trade records the new stop only once every leg rests at it. `orders/edit` on a bracket (the order's total size: filled + left) is used only with `EWBOT_LIVE_EDIT_STOPS=1`, after CANARY confirms it (spec §6.2). With no resting stop and the price through the stop, the watchdog closes at once; with a resting stop gapped past its 5% limit (`STOP_LIMIT_OFFSET`, the Create Order reference; spec §4.2 assumed 1%) for 10 s, it closes at market (spec §4.2).
7. **Idempotency and retries** (spec §4.2, §5.1). Intent before send; a PENDING intent (ambiguous error, killed process) is looked up by `client_order_id` (orders created up to 5 min before it) before the same id is sent again; a duplicate-id answer resolves to the existing order; a PENDING intent not on the exchange after 10 min is ABANDONED (checked every minute). Reads are retried 3 times in the adapter; an ambiguous entry is looked up and re-sent with the same id on the next polls only until 5 minutes after its bar's close, then skipped (`진입이 실패하면 건너뛰고`); a failed close is retried on every poll with a new id, with an alert each time (`청산이 실패하면 계속 재시도하면서 경보`) - a close goes out with the brackets resting (both only reduce; the brackets go with the position) and they are cancelled only when the venue refuses the close because they rest (`CLOSE_CONFLICTS`, after a fresh session read; the close is then resent at once), and after every failed or unconfirmed close the legs are made the ones for the position left (placed again when none rests, on later passes too), so a close that keeps failing never leaves the position unprotected; `close_position` can only reduce, so a late duplicate close cannot open a position. Restart recovery also rebuilds a trade whose entry was accepted but not yet recorded (only when no trade with that id was recorded: a position appearing after our trade closed is a foreign one, adopted at its own entry price) and records a close accepted right before a crash with its reason; a failed read during recovery is recorded and counted unresolved, never raised; the open trade's resting brackets are its legs again at restart (no false unprotected time). An accepted entry whose fill could not be read in time (or one still PENDING: looked up first; while that lookup fails - or while the fill of an accepted one cannot be read - the position is protected at once with a provisional trade from the entry's intent, settled as ours or as a foreign position once the lookup answers) is rebuilt from its intent on the next pass (not treated as a foreign position); a rebuilt trade opens at its entry order's creation time, so the protection latency (`PROTECTED_FIRST`, `PROTECT_SLOW`) includes an outage. A reduction is recorded on the trade before it is sent (`pending_reduce`: id, size, reason, the position it was sent against, the trade once done): while its outcome is unknown no other reduction is planned and it is resolved by its id first (looked up; re-sent under the same id only when the venue does not have it and the position shows no reduction); the position showing it settles it as ours (`TP1_SYNC` → the live TP1) - never an unexplained reduction or a second sale. Protection planned together with a reduction waits until the reduction is known to have filled.
8. **Exchange state vs. prices.** Positions and orders are read over REST (every 10 s while a trade is open or an intent is pending): one source of truth for reconcile, read as one state - the position is read again after the orders and the pair is used only when both reads agree (3 tries, else the poll is skipped), so a fill between the reads never pairs a stale position with a vanished leg. Because Coinbase may show a fill in the orders before the position, a leg that left the book is looked up before it is placed again: one with fills is not placed again for 30 s (the next read folds the fill). Prices for the watchdog come from the WebSocket `ticker` + `heartbeats` channels (Coinbase's public market-data endpoint `wss://advanced-trade-ws.coinbase.com`, which serves spot and US futures without a key: no JWT is sent, and the `websockets` sync client verifies TLS - SDK 1.8.4's `WSClient` verifies neither the certificate nor the host name; the mid of the ticker's best bid and ask - the REST quote's measure - and only finite prices above 0), with the spec's fallback: silent for 60 s → REST every 10 s, and a REST price older than 60 s is no price (the stop-gap check waits, the brackets keep protecting). The feed reopens a lost or closed connection, and one that brought no message for 15 s, itself with a 1-60 s backoff, and a feed without a futures price for 5 minutes while the session is OPEN (a dead connection, or a refused subscription while the heartbeats still flow) is alerted; a reconnect or a `sequence_num` gap forces an immediate REST poll. The WebSocket `user` channel is not used.
9. **SHADOW** runs the complete runtime against `ShadowExchange`: reads (product, session, quotes, balance, key permissions) are real; every order is first checked with the real `POST /orders/preview` (journaled for gate ③) and then executed on a virtual book (`SimExchange`) that follows the real futures price. Sizing is CANARY's, on the real balance (so the account must be funded during SHADOW). After a restart the open virtual position is restored from the journal (`ShadowExchange.restore`) and its brackets are re-placed, so the SHADOW restart test exercises the real recovery path.
10. **Stages** change only through `ewbot live stage <to> --confirm` with a flat book (journal and Coinbase); promotions need every gate (one stage at a time). A running runtime notices the new stage within a minute and exits once flat; Docker (`restart: unless-stopped`) starts it in the new stage; until then it sizes every entry and adoption by the more conservative of its stage and the journal's (`stage.full_cap` is the journal stage's maximum: SHADOW 0 real orders, CANARY 1, FULL 3 until released - never "no cap" after a demotion). One CANARY failure set (`stage.CANARY_FAIL`) serves the pause and the gates: unprotected > 60 s, an unexplained audit mismatch, a breaker, a transfer key, protection slower than 30 s, a foreign order or position, more contracts than the trade holds or fewer than its legs explain, the trade's position flipped to the other side, and an order failure the executor did not resolve in time (`ORDER_UNRESOLVED`: a bracket not replaced within 30 s, a close or a reduction not done by the next pass; one resolved in time is none). In CANARY each pauses entries (spec §6.2 `자동으로 일시 정지`) and keeps the CANARY pass gate unmet for the rest of that CANARY stint; in every stage three balance reads in a row that are not a finite amount above 0 pause entries (`BAD_EQUITY`, never a flatten); in FULL the major incidents pause and demote to CANARY (with a `STAGE` event), and every other CANARY failure keeps the 3-contract cap for 30 more days. Pause never stops protection or exits.
11. **Gates operationalised.** SHADOW `신호 1건 이상` = a signal first known in the stage (`known_at` after its start: the first cycle records every decision since 2024). SHADOW ① = a passing audit (no mismatch of any cause) whose window starts at or before the stage's start and reaches a bar that closed at most 3 hours ago; a failed audit is stored and unmet, never the last good one. SHADOW ② (`모든 사이클`) = every hourly bar that closed in the stage has a cycle that ended OK within 2 minutes of the close - a runtime that was down misses bars; a bar whose deadline is still ahead is not due, and one while the futures session was recorded closed (a `SESSION` event's `state`) is excused when it has none. SHADOW ④ (`장애·재시작 시험`) = at least two `RECOVERY` events in the stage (first start + ≥ 1 restart) and none with an unresolved intent. CANARY ② (`손절 이동과 청산이 목표와 일치`) = no `ORDER_UNRESOLVED`: the runtime watches every `BRACKET_FAILED`, `CLOSE_FAILED`, `CLOSE_UNCONFIRMED`, `REDUCE_FAILED` and `ACTION_ERROR` of a Protect, Close or Reduce until it is resolved - its trade closed, or a later pass that saw a target and quotes had no order failure for that trade (a close needs the close itself) - within 30 s for a bracket and by the next pass for a close or a reduction (the cancel-first fallback on a venue that refuses overlapping brackets writes `LEG_GAP` only: the designed path, not a failure; so do `CLOSE_CONFLICT` / `REDUCE_CONFLICT`, refusals answered by making room); CANARY ⑤ = at least one `COSTS` event (a daily summary with fills). The CANARY pass and FULL entry gates also need no other CANARY failure in the stint (audit mismatch, breaker, transfer key), the runtime not paused and a current passing audit. `transition` refuses a promotion unless every gate of that promotion (`stage.REQUIRED_GATES`) is among those given and all are met. The paper criteria come from the paper ledger (`paper.criteria.golive_status`).
12. **Audit** (spec §3.5): the journal stores the engine decisions in the paper ledger's format; hourly (from minute 50) they are compared with the paper ledger's current epoch (known_at in (its recording floor - the previous epoch's through_ts for a later epoch -, min(epoch through_ts, the last bar whose live cycle ended OK)]: a held or failed cycle is never a mismatch). Every mismatch has a cause like the paper runtime's: `DATA_REVISION` (the candles the two runtimes decided on differ at the decision's bar although they were equal at the paper's previous run) is alerted only; a decision missing on one side is `AUDIT_PENDING` and a major incident (`AUDIT_MISMATCH`, in CANARY/FULL) only if the next audit still finds it; a different decision or a paper fingerprint change is one at once. A ledger that is not a valid schema-1 paper ledger, like a failed fetch, is a failed audit: stored as failed, alerted once per episode.
13. **Costs in the daily summary**: entry slippage against the engine's reference (spot `entry_ref` × ratio), stop slippage of bracket stop fills against the stop price (every filled leg is read back with `get_order` and journaled as `BRACKET_FILL`), measured fees vs. the paper cost model (`execution.costs.fee`); funding is not measured separately (Coinbase books it into the balance) — the summary says so.
14. **Deployment**: only tags on `origin/master`, only with a flat book; the new image's offline preflight runs before anything else; the running image is `ewbot-live:current` (rollback = deploy the previous tag).

## File Map

| File | Task | Responsibility |
|---|---|---|
| `src/ewbot/live/__init__.py` | 1 | package marker |
| `src/ewbot/live/exchange.py` | 1 | value types, errors, the `Exchange` protocol |
| `src/ewbot/live/sim.py` | 1 | `SimExchange`: in-memory CFM venue (venue intake checks, reduce-only brackets, partial fills, gaps, sessions, faults, crashes) |
| `src/ewbot/live/journal.py` | 2 | SQLite journal: intents, events, decisions, cycles, stages, key/value |
| `src/ewbot/live/coinbase.py` | 3 | `CoinbaseExchange` over the SDK's `RESTClient` (raw JSON parsers, retries, pacing) |
| `src/ewbot/live/target.py` | 4 | engine replay of the holdout lock's config → `Target` (position, pending exit, halt stop, queued entry, decisions) |
| `src/ewbot/live/pricing.py` | 5 | basis mapping, tick rounding, stage sizing, margin projection |
| `src/ewbot/live/reconcile.py` | 6 | `LiveTrade` state, `observe`, `desired_legs`, `plan` (pure) |
| `src/ewbot/live/executor.py` | 7 | intent-before-send, entries, brackets, closes, reductions, restart recovery |
| `src/ewbot/live/watchdog.py` | 8 | stop gap, margin status, account breakers, session watch, price source |
| `src/ewbot/live/feed.py` | 8 | WebSocket ticker feed (Coinbase's public endpoint, no key, `websockets` client with verified TLS), gaps, reconnect backoff |
| `src/ewbot/live/notify.py` | 9 | `[LIVE]` Telegram notifier (paper `TelegramSender`; queued, sent by a background thread with retries; dedupe) |
| `src/ewbot/live/control.py` | 9 | Telegram commands (long poll, the user's private chat only, dated commands, persisted offset; the thread never dies) |
| `src/ewbot/live/health.py` | 9 | heartbeat pings and `/fail` (healthchecks.io) |
| `src/ewbot/live/audit.py` | 10 | paper-ledger fetch (GitHub contents API) and decision comparison |
| `src/ewbot/live/stage.py` | 10 | stages, gates, pause, incidents, transitions |
| `src/ewbot/live/settings.py` | 11 | settings from the environment, secrets files (mode 600) |
| `src/ewbot/live/preflight.py` | 11 | fingerprint, lock, key, product checks |
| `src/ewbot/live/shadow.py` | 12 | `ShadowExchange` (real reads and previews, virtual book) |
| `src/ewbot/live/runtime.py` | 12 | `LiveRuntime` main loop, `status_lines` |
| `src/ewbot/live/app.py` | 13 | production wiring (exchange, notifier, feed, control, heartbeat, audit) |
| `src/ewbot/cli.py` (modify) | 13 | `ewbot live` sub-commands: run, preflight, status, stage, backup |
| `pyproject.toml`, `uv.lock` (via `uv add`) | 3, 8 | `live` dependency group: `coinbase-advanced-py==1.8.4`, `websockets==13.1` |
| `tests/paper/test_static.py` (one line) | 4 | `live/target.py` in `ALLOWED_TRUE` |
| `tests/live/*` | 1–15 | unit, fixture, parity, chaos, CLI and deploy tests; `helpers.py` (7), `harness.py` (12), `fixtures/coinbase.json` (3) |
| `deploy/live/Dockerfile`, `Dockerfile.dockerignore`, `docker-compose.yml` | 15 | image and service |
| `deploy/live/deploy.sh`, `backup.sh`, `ewbot-backup.service`, `ewbot-backup.timer` | 15 | deployment (tag on master, flat book) and daily encrypted backups |
| `docs/live-trading.md` | 15 | ops runbook |

## Task Overview

| Task | Component | New tests |
|---|---|---|
| 1 | Exchange boundary and `SimExchange` | 23 |
| 2 | SQLite journal | 6 |
| 3 | Coinbase adapter, `live` dependency group, recorded fixtures | 81 |
| 4 | Target state from the engine replay (+ allowlist line) | 12 |
| 5 | Price mapping, stage sizing, margin projection | 22 |
| 6 | Reconcile planner | 29 |
| 7 | Executor: intent-before-send, protection, closes, recovery | 65 |
| 8 | Watchdog and WebSocket feed | 27 |
| 9 | `[LIVE]` notifier, Telegram control, heartbeat | 13 |
| 10 | Paper audit, stages and gates | 16 |
| 11 | Settings and preflight | 8 |
| 12 | `ShadowExchange` and the runtime loop | 34 |
| 13 | CLI `ewbot live …` and production wiring | 9 |
| 14 | Parity and chaos integration tests | 26 |
| 15 | Docker image, compose, deploy and backup scripts, runbook | 11 |
| 16 | Manual checklist: merge, VPS, SHADOW, CANARY gate (controller and user only) | — |

Baseline before Task 1: `uv run pytest -q` → `620 passed, 1 skipped` without a candle cache (`621 passed` when `data/cache/BTC-USD_1h.parquet` exists). After Task 15: 382 more tests, `1002 passed, 1 skipped` (`1003 passed` with the cache), about 80 s.

---

### Task 1: Exchange boundary and the `SimExchange` venue simulator

**Files:**
- Create: `src/ewbot/live/__init__.py`, `src/ewbot/live/exchange.py`, `src/ewbot/live/sim.py`
- Create: `tests/live/__init__.py`
- Test: `tests/live/test_sim.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (stdlib only).
- Produces (`ewbot.live.exchange`): constants `FUTURES_PRODUCT = "BIP-20DEC30-CDE"`, `SPOT_PRODUCT = "BTC-USD"`, `SESSION_OPEN = "OPEN"`, `OPEN_STATUSES` (`OPEN`, `PENDING`, `QUEUED`, `EDIT_QUEUED`, `CANCEL_QUEUED`: an order with a queued edit or cancel still rests and can fill), `TERMINAL_STATUSES`, `STOP_LIMIT_OFFSET = 0.05` (a triggered bracket stop is placed as a limit 5% beyond it, Create Order reference); frozen dataclasses `ProductSpec(product_id, contract_size, price_increment, base_increment, intraday_margin: tuple[float, float], overnight_margin: tuple[float, float])` with `margin_rate(side, overnight=True) -> float`, `SessionInfo(state, maintenance_start=None, maintenance_end=None)` with `.is_open`, `Quote(product_id, bid, ask, ts)` with `.mid`, `Position(side, contracts, avg_entry)`, `Order(order_id, client_order_id, side, kind, size, filled, status, limit_price=None, stop_price=None, avg_fill_price=None, created_ts=None, fee=None)` with `.is_open` / `.is_terminal`, `Balance(equity, available_margin=None)`, `KeyPermissions(can_view, can_trade, can_transfer, portfolio_type=None)`, `PlaceResult(client_order_id, order_id, ok, error=None, duplicate=False)` (`duplicate`: the client_order_id already exists), `PreviewResult(ok, errors=(), warnings=())`; errors `ExchangeError` (`.retryable = False`), `RateLimited(message, retry_after=1.0)` and `Unavailable` (both retryable); `class Exchange(Protocol)` with attributes `futures_id`, `spot_id` and methods `product()`, `session()`, `quote(product_id)`, `position()`, `open_orders()`, `get_order(order_id)`, `find_order(client_order_id, since_ts)`, `balance()`, `key_permissions()`, `place_market(client_order_id, side, size)`, `place_bracket(client_order_id, side, size, limit_price, stop_price)`, `edit_bracket(order_id, size, limit_price, stop_price) -> bool`, `cancel(order_ids)` (raises `ExchangeError` naming each order that may still rest; an order that already filled or whose cancel is already queued is not an error), `close_position(client_order_id, size=None)`, `preview_market(side, size)`, `preview_bracket(side, size, limit_price, stop_price)`.
- Produces (`ewbot.live.sim`): `BIP_SPEC`, `VIEW_TRADE`, `class SimCrash(BaseException)`, `class SimExchange(product=BIP_SPEC, *, equity=10_000.0, clock=time.time, spot_id="BTC-USD", spread=5.0, duplicate="existing"|"error", reject_overlap=False, edit_supported=False, fee_per_contract=0.0, permissions=VIEW_TRADE)` implementing `Exchange`, plus the test/shadow drivers `set_prices(fut, spot=None) -> list[dict]` (fills it caused), `set_session(state)`, `fail_next(method, exc, times=1)`, `crash_after_accept(method, exc=None)` (both raise `ValueError` for a method outside the class attributes `FAULT_METHODS` / `CRASH_METHODS`, the methods that honour them), and the public attributes `pos`, `orders`, `fills`, `calls`, `cash`, `spread`, `session_state`, `market_fill_ratio`, `bracket_fill_cap`, `preview_errors`, `reject_overlap`, `edit_supported`. Order intake is never more forgiving than the venue: a size that is not a whole number of contracts (at least 1), a limit or stop price off the $5 grid, or a bracket whose take-profit is not beyond its stop in the profit direction is refused — `PlaceResult(ok=False, error="INVALID_SIZE_PRECISION" | "INVALID_PRICE_PRECISION" | "INVALID_STOP_PRICE_PRECISION" | "INVALID_BRACKET_PRICES")`, `edit_bracket` → `False`, previews → `PREVIEW_<reason>`. `close_position` is IOC like `place_market` (`market_fill_ratio`), and both re-match the book, so a bracket left with nothing to reduce is cancelled at once. `cancel` cancels every OPEN order given, then raises `ExchangeError` for the unknown or no longer cancellable ones (a FILLED order is not an error); a duplicate client_order_id in `duplicate="error"` mode answers `PlaceResult(..., "DUPLICATE_CLIENT_ORDER_ID", duplicate=True)`.

- [ ] **Step 1: Write the failing test**

```bash
mkdir -p src/ewbot/live tests/live
```

**File: `tests/live/__init__.py`**

```python
"""Tests for ewbot.live (Plan 3). No test touches the network or a real Coinbase account."""
```

**File: `tests/live/test_sim.py`**

```python
from __future__ import annotations

import pytest

from ewbot.live.exchange import (
    STOP_LIMIT_OFFSET,
    ExchangeError,
    PlaceResult,
    Position,
    RateLimited,
    Unavailable,
)
from ewbot.live.sim import BIP_SPEC, SimCrash, SimExchange


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def ex():
    sim = SimExchange(equity=5_000.0, clock=Clock())
    sim.set_prices(60_000.0, 59_990.0)
    return sim


def test_product_spec_is_the_bip_contract():
    assert (BIP_SPEC.contract_size, BIP_SPEC.price_increment, BIP_SPEC.base_increment) == (0.01, 5.0, 1.0)
    assert BIP_SPEC.margin_rate(1) == 0.21025 and BIP_SPEC.margin_rate(-1) == 0.256625
    assert BIP_SPEC.margin_rate(1, overnight=False) == 0.100079


def test_market_buy_fills_at_the_ask_and_marks_equity(ex):
    r = ex.place_market("e1", 1, 2)
    assert r.ok and ex.get_order(r.order_id).status == "FILLED" and ex.get_order(r.order_id).fee == 0.0
    assert ex.position() == Position(1, 2, 60_002.5)
    ex.set_prices(60_102.5)
    assert ex.balance().equity == pytest.approx(5_000.0 + 100.0 * 2 * 0.01)
    assert ex.quote("BTC-USD").mid == pytest.approx(59_990.0)


def test_brackets_only_reduce_and_take_profit_at_the_limit(ex):
    ex.place_market("e1", 1, 3)
    ex.place_bracket("b1", -1, 1, 60_500.0, 59_000.0)
    ex.place_bracket("b2", -1, 2, 61_000.0, 59_000.0)
    fills = ex.set_prices(60_600.0)
    assert [(f["client_order_id"], f["size"], f["price"]) for f in fills] == [("b1", 1, 60_500.0)]
    assert ex.position().contracts == 2
    ex.set_prices(61_200.0)
    assert ex.position() is None
    assert [o.client_order_id for o in ex.open_orders()] == []
    ex.place_market("e2", -1, 1)  # a later bracket for a long cannot increase the new short
    r = ex.place_bracket("b3", -1, 1, 61_000.0, 59_000.0)
    assert not r.ok and r.error == "NO_POSITION_TO_REDUCE"


def test_a_triggered_stop_rests_five_percent_beyond_the_stop():
    """Coinbase create-order reference, trigger_bracket_gtc.stop_trigger_price: a stop limit order is placed
    "with a limit price 5% higher for BUYS and 5% lower for SELLS"."""
    assert STOP_LIMIT_OFFSET == 0.05


def test_stop_fills_inside_the_five_percent_limit(ex):
    ex.place_market("e1", 1, 2)
    ex.place_bracket("b1", -1, 1, 62_000.0, 59_500.0)
    ex.place_bracket("b2", -1, 1, 62_000.0, 59_500.0)
    ex.bracket_fill_cap = 1
    fills = ex.set_prices(59_400.0)
    assert [(f["size"], f["price"]) for f in fills] == [(1, 59_400.0), (1, 59_400.0)] and ex.position() is None
    ex.set_prices(60_000.0)
    ex.place_market("e2", 1, 1)
    ex.place_bracket("b3", -1, 1, 62_000.0, 59_500.0)
    fills = ex.set_prices(56_600.0)  # 4.9% through the stop: still inside its 56_525 limit
    assert [f["price"] for f in fills] == [56_600.0] and ex.position() is None


def test_stop_gap_beyond_the_limit_rests_until_the_price_comes_back(ex):
    ex.place_market("e1", 1, 1)
    ex.place_bracket("b1", -1, 1, 62_000.0, 59_500.0)
    assert ex.set_prices(56_000.0) == []  # limit 56_525: gapped through, nothing fills
    assert ex.position() is not None and ex.open_orders()[0].client_order_id == "b1"
    fills = ex.set_prices(56_550.0)
    assert [f["price"] for f in fills] == [56_550.0] and ex.position() is None


def test_short_bracket_mirrors_the_long(ex):
    ex.place_market("s1", -1, 2)
    ex.place_bracket("b1", 1, 2, 59_000.0, 60_500.0)
    fills = ex.set_prices(60_600.0)
    assert [(f["side"], f["size"], f["price"]) for f in fills] == [(1, 2, 60_600.0)] and ex.position() is None
    ex.set_prices(60_000.0)
    ex.place_market("s2", -1, 1)
    ex.place_bracket("b2", 1, 1, 59_000.0, 60_500.0)  # buy limit 5% above the stop: 63_525
    assert ex.set_prices(64_000.0) == [] and ex.position() is not None
    assert [f["price"] for f in ex.set_prices(63_500.0)] == [63_500.0] and ex.position() is None


def test_partial_fills(ex):
    ex.market_fill_ratio = 0.5
    r = ex.place_market("e1", 1, 3)
    o = ex.get_order(r.order_id)
    assert (o.filled, o.status) == (1, "CANCELLED") and ex.position().contracts == 1
    ex.market_fill_ratio = 1.0
    ex.place_market("e2", 1, 2)
    ex.bracket_fill_cap = 1
    ex.place_bracket("b1", -1, 3, 60_100.0, 59_000.0)
    ex.set_prices(60_200.0)
    assert ex.position().contracts == 2 and ex.open_orders()[0].filled == 1


def test_nothing_fills_while_the_session_is_closed(ex):
    ex.place_market("e1", 1, 1)
    ex.place_bracket("b1", -1, 1, 62_000.0, 59_500.0)
    ex.set_session("CLOSE")
    assert ex.set_prices(56_000.0) == []
    assert not ex.place_market("e2", 1, 1).ok and not ex.close_position("x1").ok
    assert ex.session().state == "CLOSE" and not ex.session().is_open
    fills = ex.set_session("OPEN")  # reopens far below the stop limit: the stop rests
    assert fills == [] and ex.position() is not None


def test_injected_faults_raise_once(ex):
    ex.fail_next("place_market", RateLimited("429", retry_after=2.0))
    ex.fail_next("position", Unavailable("503"), times=2)
    with pytest.raises(RateLimited) as err:
        ex.place_market("e1", 1, 1)
    assert err.value.retry_after == 2.0 and ex.orders == {}
    assert ex.place_market("e1", 1, 1).ok
    for _ in range(2):
        with pytest.raises(Unavailable):
            ex.position()
    assert ex.position().contracts == 1


def test_crash_after_accept_leaves_the_order_on_the_exchange(ex):
    ex.crash_after_accept("place_market")
    with pytest.raises(SimCrash):
        ex.place_market("e1", 1, 1)
    found = ex.find_order("e1", since_ts=0.0)
    assert found is not None and found.status == "FILLED" and ex.position().contracts == 1
    assert ex.find_order("e1", since_ts=10_000.0) is None
    ex.crash_after_accept("place_bracket", Unavailable("timeout"))  # the reply is lost, the order exists
    with pytest.raises(Unavailable):
        ex.place_bracket("b1", -1, 1, 62_000.0, 59_000.0)
    assert [o.client_order_id for o in ex.open_orders()] == ["b1"]


def test_duplicate_client_order_id_modes():
    existing = SimExchange(clock=Clock())
    existing.set_prices(60_000.0)
    first = existing.place_market("e1", 1, 1)
    again = existing.place_market("e1", 1, 1)
    assert again.ok and again.order_id == first.order_id and existing.position().contracts == 1
    strict = SimExchange(clock=Clock(), duplicate="error")
    strict.set_prices(60_000.0)
    strict.place_market("e1", 1, 1)
    dup = strict.place_market("e1", 1, 1)
    assert not dup.ok and dup.duplicate and strict.position().contracts == 1


def test_overlapping_brackets_can_be_rejected(ex):
    ex.reject_overlap = True
    ex.place_market("e1", 1, 2)
    assert ex.place_bracket("b1", -1, 2, 61_000.0, 59_000.0).ok
    r = ex.place_bracket("b2", -1, 2, 61_000.0, 59_500.0)
    assert not r.ok and r.error == "INSUFFICIENT_POSITION_SIZE"


def test_fees_are_charged_per_contract():
    sim = SimExchange(clock=Clock(), fee_per_contract=0.15, equity=1_000.0)
    sim.set_prices(60_000.0)
    r = sim.place_market("e1", 1, 2)
    assert sim.get_order(r.order_id).fee == pytest.approx(0.30) and sim.cash == pytest.approx(999.70)


def test_edit_and_close(ex):
    ex.place_market("e1", 1, 2)
    b = ex.place_bracket("b1", -1, 2, 61_000.0, 59_000.0)
    assert not ex.edit_bracket(b.order_id, 2, 61_000.0, 59_500.0)  # unsupported by default
    ex.edit_supported = True
    assert ex.edit_bracket(b.order_id, 2, 61_000.0, 59_500.0)
    assert ex.get_order(b.order_id).stop_price == 59_500.0
    assert ex.close_position("x1", size=1).ok and ex.position().contracts == 1
    assert ex.close_position("x2").ok and ex.position() is None
    assert ex.close_position("x3").error == "NO_OPEN_POSITION"
    assert ex.open_orders() == []  # the orphaned bracket is cancelled by the close itself


def test_prices_off_the_tick_grid_are_rejected(ex):
    ex.place_market("e1", 1, 1)
    r = ex.place_bracket("b1", -1, 1, 60_502.5, 59_000.0)  # $5 tick
    assert not r.ok and r.error == "INVALID_PRICE_PRECISION"
    r = ex.place_bracket("b2", -1, 1, 60_500.0, 59_001.0)
    assert not r.ok and r.error == "INVALID_STOP_PRICE_PRECISION"
    assert not ex.place_bracket("b3", -1, 1, float("nan"), 59_000.0).ok
    assert ex.open_orders() == [] and not ex.preview_bracket(-1, 1, 60_500.0, 59_001.0).ok
    b = ex.place_bracket("b4", -1, 1, 60_500.0, 59_000.0)
    ex.edit_supported = True
    assert not ex.edit_bracket(b.order_id, 1, 60_500.0, 59_002.5)
    assert ex.get_order(b.order_id).stop_price == 59_000.0


def test_sizes_must_be_whole_contracts(ex):
    for size in (1.5, 0, -1):
        r = ex.place_market(f"e{size}", 1, size)
        assert not r.ok and r.error == "INVALID_SIZE_PRECISION"
    assert ex.orders == {} and ex.position() is None and not ex.preview_market(1, 1.5).ok
    assert ex.place_market("e2", 1, 2.0).ok and ex.position().contracts == 2  # a whole number is fine
    assert ex.place_bracket("b1", -1, 0.5, 61_000.0, 59_000.0).error == "INVALID_SIZE_PRECISION"
    assert ex.close_position("x1", size=1.5).error == "INVALID_SIZE_PRECISION"
    assert ex.position().contracts == 2 and ex.open_orders() == []


def test_bracket_take_profit_and_stop_must_be_on_their_sides(ex):
    ex.place_market("e1", 1, 1)  # long: the sell bracket's take-profit must be above its stop
    for tp, stop in ((59_000.0, 61_000.0), (60_000.0, 60_000.0)):
        r = ex.place_bracket(f"b{tp}", -1, 1, tp, stop)
        assert not r.ok and r.error == "INVALID_BRACKET_PRICES"
    b = ex.place_bracket("b1", -1, 1, 61_000.0, 59_000.0)
    ex.edit_supported = True
    assert not ex.edit_bracket(b.order_id, 1, 59_000.0, 61_000.0)
    ex.close_position("x1")
    ex.place_market("s1", -1, 1)  # short: the buy bracket's take-profit must be below its stop
    assert ex.place_bracket("b2", 1, 1, 61_000.0, 59_000.0).error == "INVALID_BRACKET_PRICES"
    assert ex.place_bracket("b3", 1, 1, 59_000.0, 61_000.0).ok


def test_close_position_honours_the_market_fill_ratio(ex):
    ex.place_market("e1", 1, 3)
    ex.market_fill_ratio = 0.5
    r = ex.close_position("x1")
    o = ex.get_order(r.order_id)
    assert r.ok and (o.size, o.filled, o.status) == (3, 1, "CANCELLED") and ex.position().contracts == 2
    r = ex.close_position("x2", size=1)  # floor(0.5): nothing fills, the IOC rest is cancelled
    assert r.ok and ex.get_order(r.order_id).status == "CANCELLED" and ex.position().contracts == 2


def test_a_market_order_cancels_orphaned_brackets_at_once(ex):
    ex.place_market("e1", 1, 1)
    b = ex.place_bracket("b1", -1, 1, 62_000.0, 59_500.0)
    assert ex.place_market("x1", -1, 1).ok and ex.position() is None
    assert ex.open_orders() == [] and ex.get_order(b.order_id).status == "CANCELLED"


def test_fault_injection_only_accepts_methods_that_honour_it(ex):
    for bad in ("place_order", "set_prices"):
        with pytest.raises(ValueError):
            ex.fail_next(bad, RateLimited("429"))
    for bad in ("cancel", "edit_bracket", "quote", "place_order"):
        with pytest.raises(ValueError):
            ex.crash_after_accept(bad)
    ex.fail_next("cancel", Unavailable("503"))
    ex.crash_after_accept("close_position")
    with pytest.raises(Unavailable):
        ex.cancel([])


def test_cancel_reports_orders_it_could_not_cancel(ex):
    """Like the adapter honouring batch_cancel's results[].success: an unknown or no longer cancellable
    order raises after the others were cancelled; an order that already filled no longer rests (the venue's
    ORDER_IS_FULLY_FILLED) and is not an error."""
    ex.place_market("e1", 1, 2)
    b1 = ex.place_bracket("b1", -1, 1, 62_000.0, 59_000.0)
    b2 = ex.place_bracket("b2", -1, 1, 61_000.0, 59_000.0)
    ex.cancel([b1.order_id])
    with pytest.raises(ExchangeError) as err:
        ex.cancel(["nope", b1.order_id, b2.order_id])
    assert not err.value.retryable and "nope" in str(err.value) and b1.order_id in str(err.value)
    assert b2.order_id not in str(err.value) and ex.get_order(b2.order_id).status == "CANCELLED"
    b3 = ex.place_bracket("b3", -1, 2, 60_500.0, 59_000.0)
    ex.set_prices(60_600.0)
    assert ex.get_order(b3.order_id).status == "FILLED"
    ex.cancel([b3.order_id])


def test_a_duplicate_result_is_flagged_explicitly():
    strict = SimExchange(clock=Clock(), duplicate="error")
    strict.set_prices(60_000.0)
    strict.place_market("e1", 1, 1)
    dup = strict.place_market("e1", 1, 1)
    assert dup == PlaceResult("e1", None, False, "DUPLICATE_CLIENT_ORDER_ID", duplicate=True)
    assert not PlaceResult("x", None, False, "INVALID_REQUEST: duplicate field").duplicate
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/live/test_sim.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'ewbot.live.exchange'` (`src/ewbot/live/` is still an empty directory).

- [ ] **Step 3: Write minimal implementation**

**File: `src/ewbot/live/__init__.py`**

```python
"""Live-trading runtime (Plan 3): target-state replay, reconcile against Coinbase CFM, watchdog, control."""
```

**File: `src/ewbot/live/exchange.py`**

```python
"""Exchange boundary of the live runtime (Plan 3 spec 3.2 `exchange`): value types, errors and the
Exchange protocol implemented by CoinbaseExchange (live/coinbase.py), SimExchange (live/sim.py) and
ShadowExchange (live/shadow.py).

Conventions: side +1 = buy / long, -1 = sell / short; sizes are whole contracts; prices are USD floats
already on the product's tick grid when they are sent (live/pricing.py does the rounding)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

FUTURES_PRODUCT = "BIP-20DEC30-CDE"  # nano Bitcoin perp-style future, 0.01 BTC per contract
SPOT_PRODUCT = "BTC-USD"  # the signal source (same candles as the paper runtime)
SESSION_OPEN = "OPEN"
# Statuses of an order that still rests and can fill. EDIT_QUEUED and CANCEL_QUEUED are open until the
# venue confirms the edit / cancel: a queued cancel can still fill, so it still counts as protection and as
# an order of ours (List Orders reference, OrderExecutionStatus).
OPEN_STATUSES = ("OPEN", "PENDING", "QUEUED", "EDIT_QUEUED", "CANCEL_QUEUED")
TERMINAL_STATUSES = ("FILLED", "CANCELLED", "EXPIRED", "FAILED")
# A triggered trigger_bracket stop rests as a limit this far beyond the stop. Coinbase Advanced Trade API
# reference, Create Order, trigger_bracket_gtc.stop_trigger_price (checked 2026-10-03): "When triggered, a
# stop limit order is automatically placed with a limit price 5% higher for BUYS and 5% lower for SELLS."
# (Spec 4.2 assumed 1%; the reference wins. Confirm on the first stop fill in CANARY.)
STOP_LIMIT_OFFSET = 0.05


@dataclass(frozen=True, slots=True)
class ProductSpec:
    product_id: str
    contract_size: float
    price_increment: float
    base_increment: float
    intraday_margin: tuple[float, float]  # (long, short) initial margin rates
    overnight_margin: tuple[float, float]  # (long, short): from 16:00 New York

    def margin_rate(self, side: int, overnight: bool = True) -> float:
        rates = self.overnight_margin if overnight else self.intraday_margin
        return rates[0] if side > 0 else rates[1]


@dataclass(frozen=True, slots=True)
class SessionInfo:
    state: str  # OPEN, CLOSE, PRE_OPEN, HALTED, ... (the FCM_TRADING_SESSION_STATE_ prefix stripped)
    maintenance_start: int | None = None  # next scheduled maintenance window (epoch s)
    maintenance_end: int | None = None

    @property
    def is_open(self) -> bool:
        return self.state == SESSION_OPEN


@dataclass(frozen=True, slots=True)
class Quote:
    product_id: str
    bid: float
    ask: float
    ts: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True, slots=True)
class Position:
    side: int
    contracts: int
    avg_entry: float


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    client_order_id: str
    side: int
    kind: str  # "MARKET" | "BRACKET" | "OTHER"
    size: int
    filled: int
    status: str  # OPEN_STATUSES, TERMINAL_STATUSES or UNKNOWN
    limit_price: float | None = None  # bracket take-profit
    stop_price: float | None = None  # bracket stop trigger
    avg_fill_price: float | None = None
    created_ts: float | None = None
    fee: float | None = None  # USD paid on the fills so far (None: not reported)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class Balance:
    equity: float  # USD: cash balances + unrealized PnL
    available_margin: float | None = None


@dataclass(frozen=True, slots=True)
class KeyPermissions:
    can_view: bool
    can_trade: bool
    can_transfer: bool
    portfolio_type: str | None = None


@dataclass(frozen=True, slots=True)
class PlaceResult:
    client_order_id: str
    order_id: str | None
    ok: bool
    error: str | None = None
    duplicate: bool = False  # refused because the client_order_id exists: look the order up, never re-send blind


@dataclass(frozen=True, slots=True)
class PreviewResult:
    ok: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class ExchangeError(RuntimeError):
    """A request failed. `retryable` errors (rate limit, 5xx, network) may succeed later; for an order
    placement the outcome is then unknown and the order must be looked up by client_order_id first."""

    retryable = False


class RateLimited(ExchangeError):
    retryable = True

    def __init__(self, message: str, retry_after: float = 1.0) -> None:
        super().__init__(message)
        self.retry_after = float(retry_after)


class Unavailable(ExchangeError):
    """HTTP 5xx, connection error or timeout."""

    retryable = True


class Exchange(Protocol):
    futures_id: str
    spot_id: str

    def product(self) -> ProductSpec: ...
    def session(self) -> SessionInfo: ...
    def quote(self, product_id: str) -> Quote: ...
    def position(self) -> Position | None: ...
    def open_orders(self) -> list[Order]: ...
    def get_order(self, order_id: str) -> Order: ...
    def find_order(self, client_order_id: str, since_ts: float) -> Order | None: ...
    def balance(self) -> Balance: ...
    def key_permissions(self) -> KeyPermissions: ...
    def place_market(self, client_order_id: str, side: int, size: int) -> PlaceResult: ...
    def place_bracket(
        self, client_order_id: str, side: int, size: int, limit_price: float, stop_price: float
    ) -> PlaceResult: ...
    def edit_bracket(self, order_id: str, size: int, limit_price: float, stop_price: float) -> bool: ...
    def cancel(self, order_ids: Sequence[str]) -> None:
        """Cancel resting orders. Raises ExchangeError naming every order that may still rest (refused or
        unknown); an order that already filled, or whose cancel is already queued, is not an error."""
        ...
    def close_position(self, client_order_id: str, size: int | None = None) -> PlaceResult: ...
    def preview_market(self, side: int, size: int) -> PreviewResult: ...
    def preview_bracket(self, side: int, size: int, limit_price: float, stop_price: float) -> PreviewResult: ...
```

**File: `src/ewbot/live/sim.py`**

```python
"""SimExchange: an in-memory CFM futures venue (Plan 3 spec 7 `가짜 거래소`).

Used by every live test (no network) and, wrapped by ShadowExchange (live/shadow.py), as the virtual
book of the SHADOW stage. It models what the runtime relies on:
  - order intake is never more forgiving than the venue: a size that is not a whole number of
    base_increment (min one contract), a price off the price_increment grid ($5) or a bracket whose
    take-profit is not beyond its stop in the profit direction is rejected (PlaceResult not ok,
    edit_bracket False, a preview with errors);
  - market orders and close_position are IOC: they fill at the ask (buy) / bid (sell) while the session
    is OPEN, possibly only partly (`market_fill_ratio`), the rest is cancelled; a bracket left with
    nothing to reduce is cancelled at once;
  - trigger_bracket_gtc orders only ever REDUCE the position (help-center behaviour; the API has no
    reduce_only field): a bracket whose position is gone, or would be increased, is cancelled; a fill
    never exceeds the contracts left; `bracket_fill_cap` makes fills partial;
  - a bracket's take-profit fills at its limit when the futures price reaches it; its stop triggers when
    the price reaches the stop and then rests as a limit STOP_LIMIT_OFFSET (5%, the Create Order
    reference) beyond the stop: it fills at the market while the price is inside that limit and does not
    fill while the price has gapped past it (the case the stop-gap watchdog closes);
  - cancel() cancels every OPEN order it is given and then raises ExchangeError naming each order it could
    not cancel (unknown, or already cancelled / expired: the venue's batch_cancel results[].success is
    false); an order that already FILLED no longer rests and is not an error (ORDER_IS_FULLY_FILLED);
  - nothing fills while the session is not OPEN; market orders are rejected then;
  - fault injection: `fail_next(method, exc)` raises on the next call(s) of a method (429 / 5xx /
    outages) and `crash_after_accept(method)` raises SimCrash right AFTER the order was accepted (a
    process killed between send and journal update); a method that does not honour them (FAULT_METHODS,
    CRASH_METHODS) is refused with ValueError instead of being silently ignored;
  - a repeated client_order_id returns the existing order (`duplicate="existing"`) or an error
    (`duplicate="error"`): Coinbase documents both, the real behaviour is confirmed in CANARY."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from ewbot.live.exchange import (
    FUTURES_PRODUCT,
    SESSION_OPEN,
    SPOT_PRODUCT,
    STOP_LIMIT_OFFSET,
    Balance,
    ExchangeError,
    KeyPermissions,
    Order,
    PlaceResult,
    Position,
    PreviewResult,
    ProductSpec,
    Quote,
    SessionInfo,
)

BIP_SPEC = ProductSpec(FUTURES_PRODUCT, 0.01, 5.0, 1.0, (0.100079, 0.0999811), (0.21025, 0.256625))
VIEW_TRADE = KeyPermissions(True, True, False, "DEFAULT")  # what the live key must look like


class SimCrash(BaseException):
    """Simulated process death right after the exchange accepted an order (BaseException: nothing
    in the runtime may catch it, like a SIGKILL)."""


@dataclass
class SimOrder:
    order_id: str
    client_order_id: str
    side: int
    kind: str
    size: int
    created_ts: float
    limit_price: float | None = None
    stop_price: float | None = None
    filled: int = 0
    status: str = "OPEN"
    fill_value: float = 0.0
    fees: float = 0.0
    triggered: bool = False

    def view(self) -> Order:
        avg = self.fill_value / self.filled if self.filled else None
        return Order(self.order_id, self.client_order_id, self.side, self.kind, self.size, self.filled,
                     self.status, self.limit_price, self.stop_price, avg, self.created_ts, self.fees)


class SimExchange:
    # fail_next() works on every method that calls _fault(); crash_after_accept() on those that call
    # _accepted() after accepting an order. A subclass overriding one without that call narrows these.
    FAULT_METHODS: frozenset[str] = frozenset({
        "product", "session", "quote", "position", "open_orders", "get_order", "find_order", "balance",
        "key_permissions", "place_market", "place_bracket", "edit_bracket", "cancel", "close_position",
        "preview_market", "preview_bracket",
    })
    CRASH_METHODS: frozenset[str] = frozenset({"place_market", "place_bracket", "close_position"})

    def __init__(
        self,
        product: ProductSpec = BIP_SPEC,
        *,
        equity: float = 10_000.0,
        clock: Callable[[], float] = time.time,
        spot_id: str = SPOT_PRODUCT,
        spread: float = 5.0,
        duplicate: str = "existing",
        reject_overlap: bool = False,
        edit_supported: bool = False,
        fee_per_contract: float = 0.0,
        permissions: KeyPermissions = VIEW_TRADE,
    ) -> None:
        if duplicate not in ("existing", "error"):
            raise ValueError("duplicate must be 'existing' or 'error'")
        self.spec = product
        self.futures_id = product.product_id
        self.spot_id = spot_id
        self.clock = clock
        self.cash = float(equity)
        self.pos: Position | None = None
        self.orders: dict[str, SimOrder] = {}
        self.fut_price = 0.0
        self.spot_price = 0.0
        self.spread = float(spread)
        self.session_state = SESSION_OPEN
        self.maintenance: tuple[int | None, int | None] = (None, None)
        self.duplicate = duplicate
        self.reject_overlap = reject_overlap
        self.edit_supported = edit_supported
        self.fee_per_contract = float(fee_per_contract)
        self.permissions = permissions
        self.market_fill_ratio = 1.0
        self.bracket_fill_cap: int | None = None
        self.preview_errors: tuple[str, ...] = ()
        self.fills: list[dict] = []
        self.calls: list[str] = []
        self._faults: dict[str, list[BaseException]] = {}
        self._crash_after: dict[str, BaseException] = {}
        self._seq = 0

    # ------------------------------------------------------------------ driving (tests, shadow)
    def set_prices(self, fut: float, spot: float | None = None) -> list[dict]:
        """Move the futures last price (and the spot mid); returns the bracket fills it caused."""
        self.fut_price = float(fut)
        if spot is not None:
            self.spot_price = float(spot)
        return self._match()

    def set_session(self, state: str) -> list[dict]:
        self.session_state = state
        return self._match()

    def fail_next(self, method: str, exc: BaseException, times: int = 1) -> None:
        if method not in self.FAULT_METHODS:
            raise ValueError(f"fail_next: {method!r} never raises injected faults")
        self._faults.setdefault(method, []).extend([exc] * times)

    def crash_after_accept(self, method: str, exc: BaseException | None = None) -> None:
        """The next `method` call is accepted, then raises `exc` (default SimCrash: the process dies;
        an Unavailable: the reply was lost, e.g. a timeout after the venue created the order)."""
        if method not in self.CRASH_METHODS:
            raise ValueError(f"crash_after_accept: {method!r} does not accept orders")
        self._crash_after[method] = exc if exc is not None else SimCrash(method)

    def _fault(self, method: str) -> None:
        self.calls.append(method)
        queue = self._faults.get(method)
        if queue:
            raise queue.pop(0)

    def _accepted(self, method: str) -> None:
        exc = self._crash_after.pop(method, None)
        if exc is not None:
            raise exc

    # ------------------------------------------------------------------ reads
    def product(self) -> ProductSpec:
        self._fault("product")
        return self.spec

    def session(self) -> SessionInfo:
        self._fault("session")
        return SessionInfo(self.session_state, *self.maintenance)

    def quote(self, product_id: str) -> Quote:
        self._fault("quote")
        if product_id == self.futures_id:
            half = self.spread / 2.0
            return Quote(product_id, self.fut_price - half, self.fut_price + half, self.clock())
        if product_id == self.spot_id:
            return Quote(product_id, self.spot_price - 0.005, self.spot_price + 0.005, self.clock())
        raise ExchangeError(f"unknown product {product_id}")

    def position(self) -> Position | None:
        self._fault("position")
        return self.pos

    def open_orders(self) -> list[Order]:
        self._fault("open_orders")
        return [o.view() for o in self.orders.values() if o.status == "OPEN"]

    def get_order(self, order_id: str) -> Order:
        self._fault("get_order")
        if order_id not in self.orders:
            raise ExchangeError(f"unknown order {order_id}")
        return self.orders[order_id].view()

    def find_order(self, client_order_id: str, since_ts: float) -> Order | None:
        self._fault("find_order")
        for o in self.orders.values():
            if o.client_order_id == client_order_id and o.created_ts >= since_ts:
                return o.view()
        return None

    def balance(self) -> Balance:
        self._fault("balance")
        upnl = 0.0
        if self.pos is not None:
            upnl = self.pos.side * (self.fut_price - self.pos.avg_entry) * self.pos.contracts * self.spec.contract_size
        return Balance(self.cash + upnl, None)

    def key_permissions(self) -> KeyPermissions:
        self._fault("key_permissions")
        return self.permissions

    # ------------------------------------------------------------------ order intake (the venue's checks)
    @staticmethod
    def _on_grid(x: float, step: float) -> bool:
        """x is a positive whole multiple of step."""
        x = float(x)
        if not math.isfinite(x) or x <= 0:
            return False
        n = x / step
        return abs(n - round(n)) < 1e-6

    def _rejected(self, size: float, side: int = 0, limit: float | None = None,
                  stop: float | None = None) -> str | None:
        """The venue's failure reason for this order, or None: the size in whole base increments (at least
        one), prices on the tick grid, a bracket's take-profit beyond its stop in the profit direction."""
        if not self._on_grid(size, self.spec.base_increment):
            return "INVALID_SIZE_PRECISION"
        if limit is not None and not self._on_grid(limit, self.spec.price_increment):
            return "INVALID_PRICE_PRECISION"
        if stop is not None and not self._on_grid(stop, self.spec.price_increment):
            return "INVALID_STOP_PRICE_PRECISION"
        if limit is not None and stop is not None and side * (stop - limit) <= 0:
            return "INVALID_BRACKET_PRICES"
        return None

    # ------------------------------------------------------------------ writes
    def _duplicate(self, client_order_id: str) -> PlaceResult | None:
        for o in self.orders.values():
            if o.client_order_id == client_order_id:
                if self.duplicate == "existing":
                    return PlaceResult(client_order_id, o.order_id, True)
                return PlaceResult(client_order_id, None, False, "DUPLICATE_CLIENT_ORDER_ID", duplicate=True)
        return None

    def _new(self, client_order_id: str, side: int, kind: str, size: int, limit: float | None = None,
             stop: float | None = None) -> SimOrder:
        if side not in (1, -1) or size < 1:
            raise ExchangeError(f"invalid order side={side} size={size}")
        self._seq += 1
        o = SimOrder(f"sim-{self._seq}", client_order_id, side, kind, int(size), self.clock(), limit, stop)
        self.orders[o.order_id] = o
        return o

    def place_market(self, client_order_id: str, side: int, size: int) -> PlaceResult:
        self._fault("place_market")
        dup = self._duplicate(client_order_id)
        if dup is not None:
            return dup
        reason = self._rejected(size)
        if reason is not None:
            return PlaceResult(client_order_id, None, False, reason)
        if self.session_state != SESSION_OPEN:
            return PlaceResult(client_order_id, None, False, "SESSION_NOT_OPEN")
        o = self._new(client_order_id, side, "MARKET", size)
        self._execute_market(o)
        self._accepted("place_market")
        return PlaceResult(client_order_id, o.order_id, True)

    def place_bracket(
        self, client_order_id: str, side: int, size: int, limit_price: float, stop_price: float
    ) -> PlaceResult:
        self._fault("place_bracket")
        dup = self._duplicate(client_order_id)
        if dup is not None:
            return dup
        reason = self._rejected(size, side, limit_price, stop_price)
        if reason is not None:
            return PlaceResult(client_order_id, None, False, reason)
        if self.pos is None or self.pos.side != -side:
            return PlaceResult(client_order_id, None, False, "NO_POSITION_TO_REDUCE")
        if self.reject_overlap:
            resting = sum(o.size - o.filled for o in self.orders.values() if o.kind == "BRACKET" and o.status == "OPEN")
            if resting + size > self.pos.contracts:
                return PlaceResult(client_order_id, None, False, "INSUFFICIENT_POSITION_SIZE")
        self._new(client_order_id, side, "BRACKET", size, float(limit_price), float(stop_price))
        self._match()
        self._accepted("place_bracket")
        o = next(x for x in self.orders.values() if x.client_order_id == client_order_id)
        return PlaceResult(client_order_id, o.order_id, True)

    def edit_bracket(self, order_id: str, size: int, limit_price: float, stop_price: float) -> bool:
        self._fault("edit_bracket")
        o = self.orders.get(order_id)
        if not self.edit_supported or o is None or o.status != "OPEN" or o.triggered:
            return False
        if self._rejected(size, o.side, limit_price, stop_price) is not None:
            return False
        o.size, o.limit_price, o.stop_price = int(size), float(limit_price), float(stop_price)
        self._match()
        return True

    def cancel(self, order_ids: Sequence[str]) -> None:
        self._fault("cancel")
        refused: list[str] = []
        for oid in order_ids:
            o = self.orders.get(oid)
            if o is not None and o.status == "OPEN":
                o.status = "CANCELLED"
            elif o is None or o.status != "FILLED":
                refused.append(f"{oid}: UNKNOWN_CANCEL_ORDER")
        if refused:
            raise ExchangeError("cancel refused: " + ", ".join(refused))

    def close_position(self, client_order_id: str, size: int | None = None) -> PlaceResult:
        self._fault("close_position")
        dup = self._duplicate(client_order_id)
        if dup is not None:
            return dup
        reason = None if size is None else self._rejected(size)
        if reason is not None:
            return PlaceResult(client_order_id, None, False, reason)
        if self.session_state != SESSION_OPEN:
            return PlaceResult(client_order_id, None, False, "SESSION_NOT_OPEN")
        if self.pos is None:
            return PlaceResult(client_order_id, None, False, "NO_OPEN_POSITION")
        qty = self.pos.contracts if size is None else min(int(size), self.pos.contracts)
        o = self._new(client_order_id, -self.pos.side, "MARKET", qty)
        self._execute_market(o)
        self._accepted("close_position")
        return PlaceResult(client_order_id, o.order_id, True)

    def preview_market(self, side: int, size: int) -> PreviewResult:
        self._fault("preview_market")
        return self._preview_result(self._rejected(size))

    def preview_bracket(self, side: int, size: int, limit_price: float, stop_price: float) -> PreviewResult:
        self._fault("preview_bracket")
        return self._preview_result(self._rejected(size, side, limit_price, stop_price))

    def _preview_result(self, reason: str | None) -> PreviewResult:
        errors = self.preview_errors + ((f"PREVIEW_{reason}",) if reason else ())
        return PreviewResult(not errors, errors)

    # ------------------------------------------------------------------ matching
    def _execute_market(self, o: SimOrder) -> None:
        """IOC at the touch: market_fill_ratio of the size fills, the unfilled rest is cancelled; a bracket
        left with nothing to reduce is cancelled at once."""
        qty = int(math.floor(o.size * self.market_fill_ratio + 1e-9))
        if qty > 0:
            self._fill(o, qty, self.fut_price + o.side * self.spread / 2.0)
        o.status = "FILLED" if o.filled == o.size else "CANCELLED"
        self._match()

    def _fill(self, o: SimOrder, qty: int, price: float) -> None:
        cs = self.spec.contract_size
        pos = self.pos
        if pos is None:
            self.pos = Position(o.side, qty, price)
        elif pos.side == o.side:
            n = pos.contracts + qty
            self.pos = Position(pos.side, n, (pos.avg_entry * pos.contracts + price * qty) / n)
        else:
            closed = min(qty, pos.contracts)
            self.cash += pos.side * (price - pos.avg_entry) * closed * cs
            left = pos.contracts - closed
            rest = qty - closed
            if left > 0:
                self.pos = Position(pos.side, left, pos.avg_entry)
            elif rest > 0:
                self.pos = Position(o.side, rest, price)
            else:
                self.pos = None
        fee = self.fee_per_contract * qty
        self.cash -= fee
        o.fees += fee
        o.filled += qty
        o.fill_value += price * qty
        self.fills.append({"order_id": o.order_id, "client_order_id": o.client_order_id, "kind": o.kind,
                           "side": o.side, "size": qty, "price": price, "fee": fee, "ts": self.clock()})

    def _match(self) -> list[dict]:
        if self.session_state != SESSION_OPEN:
            return []
        start = len(self.fills)
        last = self.fut_price
        for o in list(self.orders.values()):
            if o.kind != "BRACKET" or o.status != "OPEN":
                continue
            pos = self.pos
            if pos is None or pos.side == o.side:  # reduce-only: nothing (left) to reduce
                o.status = "CANCELLED"
                continue
            d = pos.side
            qty = min(o.size - o.filled, pos.contracts)
            if self.bracket_fill_cap is not None:
                qty = min(qty, self.bracket_fill_cap)
            if not o.triggered and o.limit_price is not None and d * (last - o.limit_price) >= 0:
                self._fill(o, qty, o.limit_price)
            else:
                if not o.triggered and o.stop_price is not None and d * (last - o.stop_price) <= 0:
                    o.triggered = True
                if o.triggered and o.stop_price is not None:
                    limit = o.stop_price * (1.0 - d * STOP_LIMIT_OFFSET)
                    if d * (last - limit) >= 0:
                        self._fill(o, qty, last)
            if o.filled >= o.size:
                o.status = "FILLED"
        return self.fills[start:]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/live/test_sim.py -q
```

Expected: `23 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/live/__init__.py src/ewbot/live/exchange.py src/ewbot/live/sim.py tests/live/__init__.py tests/live/test_sim.py
git commit -m "$(cat <<'EOF'
feat(live): exchange boundary and the SimExchange venue simulator

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: SQLite journal (intent-before-send)

**Files:**
- Create: `src/ewbot/live/journal.py`
- Test: `tests/live/test_journal.py`

**Interfaces:**
- Consumes: stdlib `sqlite3` only.
- Produces (`ewbot.live.journal`): `INTENT_STATUSES = ("PENDING", "ACCEPTED", "REJECTED", "ABANDONED")`, `DEFAULT_STAGE = "SHADOW"`, `class Journal(path: Path)` with `close()`; intents `record_intent(client_order_id, kind, trade_id, payload: dict, now) -> tuple[dict, bool]` (row, created; an existing id is never overwritten), `intent(client_order_id) -> dict | None`, `update_intent(client_order_id, status, now, order_id=None, note=None)`, `intents(status=None, trade_id=None, kind=None) -> list[dict]` (row dicts with `payload` decoded); events `add_event(ts, kind, data=None) -> int`, `events(kind=None, since=None) -> list[{"id","ts","kind","data"}]`; engine decisions `add_decisions(decisions, now) -> list[str]` (new ids), `decisions() -> list[dict]` (each with `recorded_ts`); cycles `start_cycle(bar_ts, now, stage)`, `finish_cycle(bar_ts, now, status, note=None)`, `cycles(since_bar=None) -> list[dict]` (`bar_ts, started_ts, finished_ts, status, stage, note`); stages `set_stage(stage, reason, now)`, `stage() -> tuple[str, float | None]` (`("SHADOW", None)` before the first record), `stage_history()`; key/value `get(key, default=None)`, `put(key, value)` (strict JSON); `backup(dest: Path) -> Path`.

- [ ] **Step 1: Write the failing test**

**File: `tests/live/test_journal.py`**

```python
from __future__ import annotations

import sqlite3

import pytest

from ewbot.live.journal import Journal


@pytest.fixture
def jr(tmp_path):
    j = Journal(tmp_path / "live" / "journal.sqlite")
    yield j
    j.close()


def test_intent_is_recorded_once_and_never_overwritten(jr):
    row, created = jr.record_intent("ewb-e-100-L", "ENTRY", 100, {"size": 1}, now=5.0)
    assert created and row["status"] == "PENDING" and row["payload"] == {"size": 1} and row["order_id"] is None
    again, created2 = jr.record_intent("ewb-e-100-L", "ENTRY", 100, {"size": 9}, now=6.0)
    assert not created2 and again["payload"] == {"size": 1} and again["created_ts"] == 5.0
    jr.update_intent("ewb-e-100-L", "ACCEPTED", now=7.0, order_id="o-1")
    jr.update_intent("ewb-e-100-L", "ACCEPTED", now=8.0, note="filled 1")
    got = jr.intent("ewb-e-100-L")
    assert (got["status"], got["order_id"], got["note"], got["updated_ts"]) == ("ACCEPTED", "o-1", "filled 1", 8.0)
    with pytest.raises(ValueError):
        jr.update_intent("ewb-e-100-L", "SENT", now=9.0)


def test_intents_filter_and_survive_a_reopen(tmp_path):
    path = tmp_path / "j.sqlite"
    j = Journal(path)
    j.record_intent("a", "ENTRY", 1, {}, 1.0)
    j.record_intent("b", "BRACKET", 1, {}, 2.0)
    j.record_intent("c", "BRACKET", 2, {}, 3.0)
    j.update_intent("b", "ACCEPTED", 4.0, order_id="x")
    j.close()  # a kill right after the commit: the rows are on disk
    k = Journal(path)
    assert [i["client_order_id"] for i in k.intents(status="PENDING")] == ["a", "c"]
    assert [i["client_order_id"] for i in k.intents(trade_id=1, kind="BRACKET")] == ["b"]
    k.close()


def test_events_decisions_and_kv(jr):
    jr.add_event(10.0, "ENTRY", {"trade_id": 100})
    jr.add_event(20.0, "CLOSE", {"trade_id": 100, "reason": "STOP"})
    assert [e["kind"] for e in jr.events()] == ["ENTRY", "CLOSE"]
    assert jr.events(kind="CLOSE")[0]["data"] == {"trade_id": 100, "reason": "STOP"}
    assert [e["ts"] for e in jr.events(since=15.0)] == [20.0]
    d1 = {"id": "SIGNAL:100:100", "ts": 100, "kind": "SIGNAL", "price": 1.5}
    d2 = {"id": "ENTRY:3700:3700", "ts": 3700, "kind": "ENTRY", "price": 2.5}
    assert jr.add_decisions([d1, d2], now=50.0) == ["SIGNAL:100:100", "ENTRY:3700:3700"]
    assert jr.add_decisions([d1], now=60.0) == []
    assert [d["id"] for d in jr.decisions()] == ["SIGNAL:100:100", "ENTRY:3700:3700"]
    assert jr.decisions()[0]["recorded_ts"] == 50.0
    assert jr.get("trade") is None and jr.get("paused", False) is False
    jr.put("trade", {"trade_id": 100, "legs": [{"size": 1}]})
    assert jr.get("trade") == {"trade_id": 100, "legs": [{"size": 1}]}
    with pytest.raises(ValueError):
        jr.put("bad", float("nan"))


def test_cycles_and_stages(jr):
    assert jr.stage() == ("SHADOW", None)
    jr.set_stage("SHADOW", "initial", now=1.0)
    jr.set_stage("CANARY", "user approved", now=2.0)
    assert jr.stage() == ("CANARY", 2.0) and [s["stage"] for s in jr.stage_history()] == ["SHADOW", "CANARY"]
    jr.start_cycle(3600, now=7230.0, stage="SHADOW")
    jr.finish_cycle(3600, now=7260.0, status="OK", note="flat")
    jr.start_cycle(7200, now=10830.0, stage="SHADOW")
    rows = jr.cycles()
    assert [(c["bar_ts"], c["status"], c["finished_ts"]) for c in rows] == [(3600, "OK", 7260.0), (7200, "RUNNING", None)]
    assert [c["bar_ts"] for c in jr.cycles(since_bar=7200)] == [7200]


def test_a_second_connection_reads_while_the_first_writes(tmp_path):
    path = tmp_path / "j.sqlite"
    writer, reader = Journal(path), Journal(path)
    writer.put("heartbeat", 123.0)
    assert reader.get("heartbeat") == 123.0
    writer.close()
    reader.close()


def test_backup_is_a_consistent_copy(jr, tmp_path):
    jr.record_intent("a", "ENTRY", 1, {"k": 1}, 1.0)
    out = jr.backup(tmp_path / "bk" / "copy.sqlite")
    con = sqlite3.connect(str(out))
    assert con.execute("SELECT client_order_id FROM intents").fetchall() == [("a",)]
    con.close()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/live/test_journal.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'ewbot.live.journal'`.

- [ ] **Step 3: Write minimal implementation**

**File: `src/ewbot/live/journal.py`**

```python
"""Live journal (Plan 3 spec 3.2 `journal`): one SQLite file with order intents, events, the engine's
decisions, decision cycles, stage history and a small JSON key/value store (trade state, pause flag,
account guard, Telegram offset).

Intent-before-send: every order is first recorded as an intent keyed by its deterministic
client_order_id (status PENDING) and committed; only then is it sent. After a crash, a PENDING intent
means "outcome unknown": it is looked up on the exchange by client_order_id before anything is sent
again (live/executor.py). The connection is in autocommit mode with WAL and synchronous=FULL, so a
committed row survives a kill or power loss, and `ewbot live status` can read while the runtime writes."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

INTENT_STATUSES = ("PENDING", "ACCEPTED", "REJECTED", "ABANDONED")
DEFAULT_STAGE = "SHADOW"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    client_order_id TEXT PRIMARY KEY, kind TEXT NOT NULL, trade_id INTEGER, payload TEXT NOT NULL,
    status TEXT NOT NULL, order_id TEXT, note TEXT, created_ts REAL NOT NULL, updated_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_kind ON events (kind, ts);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY, ts INTEGER NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL, recorded_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cycles (
    bar_ts INTEGER PRIMARY KEY, started_ts REAL NOT NULL, finished_ts REAL, status TEXT NOT NULL,
    stage TEXT NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, stage TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False, timeout=30.0)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, tuple(args))

    # ------------------------------------------------------------------ intents
    @staticmethod
    def _intent(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        out = dict(row)
        out["payload"] = json.loads(out["payload"])
        return out

    def record_intent(
        self, client_order_id: str, kind: str, trade_id: int | None, payload: dict[str, Any], now: float
    ) -> tuple[dict[str, Any], bool]:
        """(intent row, created). An existing client_order_id is never overwritten: the caller gets the
        recorded row back and must not send blindly (look it up first if it is still PENDING)."""
        cur = self._exec(
            "INSERT OR IGNORE INTO intents VALUES (?, ?, ?, ?, 'PENDING', NULL, NULL, ?, ?)",
            (client_order_id, kind, trade_id, _dumps(payload), float(now), float(now)),
        )
        row = self.intent(client_order_id)
        assert row is not None
        return row, cur.rowcount == 1

    def intent(self, client_order_id: str) -> dict[str, Any] | None:
        return self._intent(self._exec("SELECT * FROM intents WHERE client_order_id = ?", (client_order_id,)).fetchone())

    def update_intent(
        self, client_order_id: str, status: str, now: float, order_id: str | None = None, note: str | None = None
    ) -> None:
        if status not in INTENT_STATUSES:
            raise ValueError(f"unknown intent status {status}")
        self._exec(
            "UPDATE intents SET status = ?, order_id = COALESCE(?, order_id), note = COALESCE(?, note), "
            "updated_ts = ? WHERE client_order_id = ?",
            (status, order_id, note, float(now), client_order_id),
        )

    def intents(self, status: str | None = None, trade_id: int | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM intents WHERE 1 = 1", []
        for col, val in (("status", status), ("trade_id", trade_id), ("kind", kind)):
            if val is not None:
                sql += f" AND {col} = ?"
                args.append(val)
        rows = self._exec(sql + " ORDER BY created_ts, client_order_id", args).fetchall()
        return [self._intent(r) for r in rows]  # type: ignore[misc]

    # ------------------------------------------------------------------ events
    def add_event(self, ts: float, kind: str, data: dict[str, Any] | None = None) -> int:
        cur = self._exec("INSERT INTO events (ts, kind, data) VALUES (?, ?, ?)", (float(ts), kind, _dumps(data or {})))
        return int(cur.lastrowid or 0)

    def events(self, kind: str | None = None, since: float | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM events WHERE 1 = 1", []
        if kind is not None:
            sql += " AND kind = ?"
            args.append(kind)
        if since is not None:
            sql += " AND ts >= ?"
            args.append(float(since))
        rows = self._exec(sql + " ORDER BY id", args).fetchall()
        return [{"id": r["id"], "ts": r["ts"], "kind": r["kind"], "data": json.loads(r["data"])} for r in rows]

    # ------------------------------------------------------------------ engine decisions (paper format)
    def add_decisions(self, decisions: Iterable[dict[str, Any]], now: float) -> list[str]:
        new: list[str] = []
        for d in decisions:
            cur = self._exec(
                "INSERT OR IGNORE INTO decisions VALUES (?, ?, ?, ?, ?)",
                (d["id"], int(d["ts"]), d["kind"], _dumps(d), float(now)),
            )
            if cur.rowcount == 1:
                new.append(d["id"])
        return new

    def decisions(self) -> list[dict[str, Any]]:
        rows = self._exec("SELECT data, recorded_ts FROM decisions ORDER BY ts, id").fetchall()
        return [{**json.loads(r["data"]), "recorded_ts": r["recorded_ts"]} for r in rows]

    # ------------------------------------------------------------------ cycles
    def start_cycle(self, bar_ts: int, now: float, stage: str) -> None:
        self._exec("INSERT OR REPLACE INTO cycles VALUES (?, ?, NULL, 'RUNNING', ?, NULL)", (int(bar_ts), float(now), stage))

    def finish_cycle(self, bar_ts: int, now: float, status: str, note: str | None = None) -> None:
        self._exec("UPDATE cycles SET finished_ts = ?, status = ?, note = ? WHERE bar_ts = ?",
                   (float(now), status, note, int(bar_ts)))

    def cycles(self, since_bar: int | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM cycles", []
        if since_bar is not None:
            sql += " WHERE bar_ts >= ?"
            args.append(int(since_bar))
        return [dict(r) for r in self._exec(sql + " ORDER BY bar_ts", args).fetchall()]

    # ------------------------------------------------------------------ stages
    def set_stage(self, stage: str, reason: str, now: float) -> None:
        self._exec("INSERT INTO stages (ts, stage, reason) VALUES (?, ?, ?)", (float(now), stage, reason))

    def stage(self) -> tuple[str, float | None]:
        """(current stage, since). SHADOW with since None until the first set_stage."""
        row = self._exec("SELECT stage, ts FROM stages ORDER BY id DESC LIMIT 1").fetchone()
        return (DEFAULT_STAGE, None) if row is None else (row["stage"], row["ts"])

    def stage_history(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._exec("SELECT * FROM stages ORDER BY id").fetchall()]

    # ------------------------------------------------------------------ key / value
    def get(self, key: str, default: Any = None) -> Any:
        row = self._exec("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    def put(self, key: str, value: Any) -> None:
        self._exec("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, _dumps(value)))

    # ------------------------------------------------------------------ backup
    def backup(self, dest: Path) -> Path:
        """A consistent copy of the whole journal (SQLite online backup API), safe while running."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(dest))
        try:
            with self._lock:
                self._db.backup(target)
        finally:
            target.close()
        return dest
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/live/test_journal.py -q
```

Expected: `6 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/live/journal.py tests/live/test_journal.py
git commit -m "$(cat <<'EOF'
feat(live): SQLite journal with intent-before-send, events, decisions, cycles and stages

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Coinbase adapter, the `live` dependency group and recorded fixtures

**Files:**
- Modify (through `uv add` only): `pyproject.toml`, `uv.lock` — Decision D1
- Create: `src/ewbot/live/coinbase.py`
- Create: `tests/live/fixtures/coinbase.json` (the product and order-book entries are trimmed real public responses recorded 2026-10-03; the private-endpoint entries follow the Advanced Trade API reference and are re-recorded in SHADOW, Task 16 Step 5)
- Test: `tests/live/test_coinbase.py`

**Interfaces:**
- Consumes: `ewbot.live.exchange` (Task 1); `requests` (existing dependency); the SDK `coinbase.rest.RESTClient` (only in `CoinbaseExchange.from_key_file` and the SDK test; imported lazily, so `ewbot` still imports without the `live` group).
- Produces (`ewbot.live.coinbase`): `API = "/api/v3/brokerage"`, `SESSION_PREFIX`, `RETRY_AFTER_MAX_S = 10.0`, `CANCEL_DONE`, `class DuplicateClientOrderId(ExchangeError)`, `fmt_decimal(x) -> str`, `bad_size(size) -> bool`, `bad_price(price) -> bool`, `iso_ts(text) -> int | None`, `retry_after_s(value, now=None) -> float` (delta-seconds or HTTP-date, clamped to [0, 10], garbage → 1), `map_error(exc) -> ExchangeError` (429 → `RateLimited`, 5xx and every other `requests` exception → `Unavailable` with the type name only, a 4xx naming `DUPLICATE_CLIENT_ORDER_ID` → `DuplicateClientOrderId`, anything else → `ExchangeError(type name)`), `parse_order(dict) -> Order` (sides `BUY`/`SELL` only), `parse_place(dict, client_order_id) -> PlaceResult` (reason: `new_order_failure_reason`, then the deprecated `error` and `preview_failure_reason`, then `failure_reason`, skipping `UNKNOWN_*`; `duplicate=True` when any of them is `DUPLICATE_CLIENT_ORDER_ID`; accepted without an order id → `Unavailable`), `class CoinbaseExchange(client, futures_id=FUTURES_PRODUCT, spot_id=SPOT_PRODUCT, *, sleep=time.sleep, clock=time.time, attempts=3, backoff_s=1.0, min_remaining=2)` implementing `Exchange` with the SDK's generic `client.get(path, params=..., public=...)` / `client.post(path, data=...)`, and `CoinbaseExchange.from_key_file(key_file, futures_id, spot_id, timeout=10)`. Reads and `cancel` retry 3 times on 429 / 5xx / network errors; `place_market`, `place_bracket`, `close_position` and `edit_bracket` are sent once. Only `ExchangeError` leaves the adapter (or `ValueError` for a side other than ±1): a reply of the wrong shape is an `ExchangeError`, one that is not a JSON object `Unavailable`. Refused locally without a request: sizes that are not whole contracts ≥ 1 (`INVALID_SIZE_PRECISION`), non-finite or ≤ 0 limit / stop prices (`INVALID_PRICE_PRECISION` / `INVALID_STOP_PRICE_PRECISION`). `position()` maps `LONG`/`SHORT` only and takes `abs()` of the contract count; `open_orders()` asks for every resting status (`order_status` repeated); `quote()` raises `Unavailable` for an empty book or a top of book without a finite bid and ask above 0; `balance()` requires `total_usd_balance`; a missing `can_transfer` reads `True`; `cancel()` raises `ExchangeError` for every order whose `results[].success` is false (except `DUPLICATE_CANCEL_REQUEST` / `ORDER_IS_FULLY_FILLED`) or that has no result.

- [ ] **Step 1: Write the failing test**

```bash
mkdir -p tests/live/fixtures
```

**File: `tests/live/fixtures/coinbase.json`**

```json
{
 "_comment": "product_bip, book_bip and book_btc are trimmed real public responses recorded 2026-10-03 (GET /market/products/BIP-20DEC30-CDE, /market/product_book?limit=1). The private-endpoint entries follow the Advanced Trade API reference shapes; re-record them in SHADOW (docs/live-trading.md). positions_bip_long, create_failed_new_reason, create_duplicate_new_reason, http400_duplicate_*_body and cancel_done_or_refused (Fix round 1) follow the reference's enums: FCMPositionSide LONG/SHORT, NewOrderErrorResponse.new_order_failure_reason (error and preview_failure_reason are deprecated), CancelOrderFailureReason.",
 "product_bip": {
  "product_id": "BIP-20DEC30-CDE",
  "price": "84675",
  "base_increment": "1",
  "quote_increment": "0.01",
  "base_min_size": "1",
  "base_max_size": "5000",
  "product_type": "FUTURE",
  "fcm_trading_session_details": {
   "is_session_open": true,
   "open_time": "2026-10-02T22:00:00Z",
   "close_time": "2026-10-05T21:00:00Z",
   "session_state": "FCM_TRADING_SESSION_STATE_OPEN",
   "after_hours_order_entry_disabled": false,
   "closed_reason": "FCM_TRADING_SESSION_CLOSED_REASON_UNDEFINED",
   "maintenance": {
    "start_time": "2026-10-24T10:00:00Z",
    "end_time": "2026-10-24T19:00:00Z"
   }
  },
  "mid_market_price": "84665",
  "price_increment": "5",
  "display_name": "BTC PERP",
  "product_venue": "FCM",
  "future_product_details": {
   "venue": "cde",
   "contract_code": "BIP",
   "contract_expiry": "2089-12-30T16:00:00Z",
   "contract_size": "0.01",
   "contract_root_unit": "BTC",
   "group_description": "nano BTC Perp Futures",
   "contract_expiry_type": "EXPIRING",
   "twenty_four_by_seven": true,
   "funding_interval": "3600s",
   "funding_rate": "0.000003",
   "funding_time": "2026-10-03T12:00:00Z",
   "display_name": "Bitcoin Perpetual",
   "intraday_margin_rate": {
    "long_margin_rate": "0.100079",
    "short_margin_rate": "0.0999811"
   },
   "overnight_margin_rate": {
    "long_margin_rate": "0.21025",
    "short_margin_rate": "0.256625"
   },
   "index_price": "84671.649272"
  }
 },
 "book_bip": {
  "pricebook": {
   "product_id": "BIP-20DEC30-CDE",
   "bids": [
    {
     "price": "84655",
     "size": "549"
    }
   ],
   "asks": [
    {
     "price": "84680",
     "size": "170"
    }
   ],
   "time": "2026-10-03T12:10:36.533304Z"
  },
  "last": "84667.5",
  "mid_market": "84667.5",
  "spread_bps": "2.952290977799",
  "spread_absolute": "25"
 },
 "book_btc": {
  "pricebook": {
   "product_id": "BTC-USD",
   "bids": [
    {
     "price": "84671.26",
     "size": "0.64791945"
    }
   ],
   "asks": [
    {
     "price": "84671.27",
     "size": "0.05143921"
    }
   ],
   "time": "2026-10-03T12:10:36.869175Z"
  },
  "last": "84671.265",
  "mid_market": "84671.265",
  "spread_bps": "0.001181038149",
  "spread_absolute": "0.01"
 },
 "positions": {
  "positions": [
   {
    "product_id": "BIT-31OCT26-CDE",
    "expiration_time": "2026-10-31T16:00:00Z",
    "side": "LONG",
    "number_of_contracts": "2",
    "current_price": "84700",
    "avg_entry_price": "84000",
    "unrealized_pnl": "14",
    "daily_realized_pnl": "0"
   },
   {
    "product_id": "BIP-20DEC30-CDE",
    "expiration_time": "2030-12-20T16:00:00Z",
    "side": "SHORT",
    "number_of_contracts": "3",
    "current_price": "84665",
    "avg_entry_price": "85100",
    "unrealized_pnl": "13.05",
    "daily_realized_pnl": "0"
   }
  ]
 },
 "positions_flat": {
  "positions": []
 },
 "positions_bip_long": {
  "positions": [
   {
    "product_id": "BIP-20DEC30-CDE",
    "expiration_time": "2030-12-20T16:00:00Z",
    "side": "LONG",
    "number_of_contracts": "2",
    "current_price": "84665",
    "avg_entry_price": "84000",
    "unrealized_pnl": "13.30",
    "daily_realized_pnl": "0"
   }
  ]
 },
 "balance_summary": {
  "balance_summary": {
   "futures_buying_power": {
    "value": "4210.55",
    "currency": "USD"
   },
   "total_usd_balance": {
    "value": "5000.00",
    "currency": "USD"
   },
   "cbi_usd_balance": {
    "value": "3500.00",
    "currency": "USD"
   },
   "cfm_usd_balance": {
    "value": "1500.00",
    "currency": "USD"
   },
   "total_open_orders_hold_amount": {
    "value": "0",
    "currency": "USD"
   },
   "unrealized_pnl": {
    "value": "13.05",
    "currency": "USD"
   },
   "daily_realized_pnl": {
    "value": "0",
    "currency": "USD"
   },
   "initial_margin": {
    "value": "651.80",
    "currency": "USD"
   },
   "available_margin": {
    "value": "4361.25",
    "currency": "USD"
   },
   "liquidation_threshold": {
    "value": "325.90",
    "currency": "USD"
   },
   "liquidation_buffer_amount": {
    "value": "4687.15",
    "currency": "USD"
   },
   "liquidation_buffer_percentage": "1438"
  }
 },
 "key_permissions": {
  "can_view": true,
  "can_trade": true,
  "can_transfer": false,
  "portfolio_uuid": "8e1b3f9a-0000-4000-8000-000000000001",
  "portfolio_type": "DEFAULT"
 },
 "orders_open_page1": {
  "orders": [
   {
    "order_id": "11111111-aaaa-4000-8000-000000000001",
    "product_id": "BIP-20DEC30-CDE",
    "side": "BUY",
    "client_order_id": "ewb-b-1790942400-tp1-1",
    "status": "OPEN",
    "time_in_force": "GOOD_UNTIL_CANCELLED",
    "created_time": "2026-10-03T12:00:41.120000Z",
    "completion_percentage": "0",
    "filled_size": "0",
    "average_filled_price": "0",
    "order_type": "BRACKET",
    "product_type": "FUTURE",
    "order_configuration": {
     "trigger_bracket_gtc": {
      "base_size": "1",
      "limit_price": "83500",
      "stop_trigger_price": "85900"
     }
    }
   }
  ],
  "sequence": "0",
  "has_next": true,
  "cursor": "c-2"
 },
 "orders_open_page2": {
  "orders": [
   {
    "order_id": "11111111-aaaa-4000-8000-000000000002",
    "product_id": "BIP-20DEC30-CDE",
    "side": "BUY",
    "client_order_id": "ewb-b-1790942400-tp2-1",
    "status": "CANCEL_QUEUED",
    "time_in_force": "GOOD_UNTIL_CANCELLED",
    "created_time": "2026-10-03T12:00:41.320000Z",
    "completion_percentage": "0",
    "filled_size": "0",
    "average_filled_price": "0",
    "order_type": "BRACKET",
    "product_type": "FUTURE",
    "order_configuration": {
     "trigger_bracket_gtc": {
      "base_size": "2",
      "limit_price": "82000",
      "stop_trigger_price": "85900"
     }
    }
   }
  ],
  "sequence": "0",
  "has_next": false,
  "cursor": ""
 },
 "orders_history": {
  "orders": [
   {
    "order_id": "22222222-bbbb-4000-8000-000000000001",
    "product_id": "BIP-20DEC30-CDE",
    "side": "SELL",
    "client_order_id": "ewb-e-1790942400-S",
    "status": "FILLED",
    "time_in_force": "IMMEDIATE_OR_CANCEL",
    "created_time": "2026-10-03T12:00:31.004000Z",
    "completion_percentage": "100",
    "filled_size": "3",
    "average_filled_price": "85100",
    "order_type": "MARKET",
    "product_type": "FUTURE",
    "order_configuration": {
     "market_market_ioc": {
      "base_size": "3"
     }
    },
    "total_fees": "0.45"
   }
  ],
  "has_next": false,
  "cursor": ""
 },
 "order_get": {
  "order": {
   "order_id": "22222222-bbbb-4000-8000-000000000001",
   "product_id": "BIP-20DEC30-CDE",
   "side": "SELL",
   "client_order_id": "ewb-e-1790942400-S",
   "status": "FILLED",
   "created_time": "2026-10-03T12:00:31.004000Z",
   "filled_size": "3",
   "average_filled_price": "85100",
   "order_type": "MARKET",
   "order_configuration": {
    "market_market_ioc": {
     "base_size": "3"
    }
   },
   "total_fees": "0.45"
  }
 },
 "create_ok": {
  "success": true,
  "success_response": {
   "order_id": "33333333-cccc-4000-8000-000000000001",
   "product_id": "BIP-20DEC30-CDE",
   "side": "SELL",
   "client_order_id": "ewb-e-1790942400-S"
  },
  "order_configuration": {
   "market_market_ioc": {
    "base_size": "3"
   }
  }
 },
 "create_insufficient": {
  "success": false,
  "failure_reason": "UNKNOWN_FAILURE_REASON",
  "error_response": {
   "error": "INSUFFICIENT_FUND",
   "message": "Insufficient balance in source account",
   "error_details": "",
   "preview_failure_reason": "PREVIEW_INSUFFICIENT_FUND"
  }
 },
 "create_duplicate": {
  "success": false,
  "error_response": {
   "error": "DUPLICATE_CLIENT_ORDER_ID",
   "message": "client_order_id has already been used",
   "error_details": ""
  }
 },
 "create_failed_new_reason": {
  "success": false,
  "failure_reason": "UNKNOWN_FAILURE_REASON",
  "error_response": {
   "error": "UNKNOWN_FAILURE_REASON",
   "message": "Invalid bracket prices",
   "error_details": "stop_trigger_price must be below limit_price for a sell bracket",
   "preview_failure_reason": "UNKNOWN_PREVIEW_FAILURE_REASON",
   "new_order_failure_reason": "INVALID_BRACKET_PRICES"
  }
 },
 "create_duplicate_new_reason": {
  "success": false,
  "failure_reason": "UNKNOWN_FAILURE_REASON",
  "error_response": {
   "error": "UNKNOWN_FAILURE_REASON",
   "message": "client_order_id has already been used",
   "error_details": "",
   "preview_failure_reason": "UNKNOWN_PREVIEW_FAILURE_REASON",
   "new_order_failure_reason": "DUPLICATE_CLIENT_ORDER_ID"
  }
 },
 "http400_duplicate_order_body": {
  "success": false,
  "error_response": {
   "error": "UNKNOWN_FAILURE_REASON",
   "message": "client_order_id has already been used",
   "error_details": "",
   "new_order_failure_reason": "DUPLICATE_CLIENT_ORDER_ID"
  }
 },
 "http400_duplicate_gateway_body": {
  "error": "INVALID_ARGUMENT",
  "code": 3,
  "message": "DUPLICATE_CLIENT_ORDER_ID",
  "details": []
 },
 "close_ok": {
  "success": true,
  "success_response": {
   "order_id": "44444444-dddd-4000-8000-000000000001",
   "product_id": "BIP-20DEC30-CDE",
   "side": "BUY",
   "client_order_id": "ewb-x-1790942400-1"
  }
 },
 "preview_ok": {
  "order_total": "851.65",
  "commission_total": "0.15",
  "errs": [],
  "warning": [],
  "preview_id": "p-1",
  "current_liquidation_buffer": "1438",
  "projected_liquidation_buffer": "1201",
  "base_size": "1",
  "quote_size": "846.65"
 },
 "preview_err": {
  "order_total": "0",
  "commission_total": "0",
  "errs": [
   "PREVIEW_INSUFFICIENT_FUNDS_FOR_FUTURES"
  ],
  "warning": [
   "UNKNOWN"
  ],
  "preview_id": ""
 },
 "cancel_ok": {
  "results": [
   {
    "success": true,
    "failure_reason": "UNKNOWN_CANCEL_FAILURE_REASON",
    "order_id": "11111111-aaaa-4000-8000-000000000001"
   }
  ]
 },
 "cancel_done_or_refused": {
  "results": [
   {
    "success": true,
    "failure_reason": "UNKNOWN_CANCEL_FAILURE_REASON",
    "order_id": "o-ok"
   },
   {
    "success": false,
    "failure_reason": "DUPLICATE_CANCEL_REQUEST",
    "order_id": "o-queued"
   },
   {
    "success": false,
    "failure_reason": "ORDER_IS_FULLY_FILLED",
    "order_id": "o-filled"
   },
   {
    "success": false,
    "failure_reason": "UNKNOWN_CANCEL_ORDER",
    "order_id": "o-refused"
   }
  ]
 },
 "edit_ok": {
  "success": true,
  "errors": []
 }
}
```

**File: `tests/live/test_coinbase.py`**

```python
from __future__ import annotations

import copy
import inspect
import json
from importlib import metadata
from pathlib import Path

import pytest
import requests

from ewbot.live.coinbase import (
    API,
    RETRY_AFTER_MAX_S,
    CoinbaseExchange,
    fmt_decimal,
    map_error,
    parse_order,
    parse_place,
    retry_after_s,
)
from ewbot.live.exchange import OPEN_STATUSES, ExchangeError, Position, RateLimited, Unavailable

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "coinbase.json").read_text(encoding="utf-8"))
FUT = "BIP-20DEC30-CDE"


def http_error(status: int, text: str = "", headers: dict | None = None) -> requests.exceptions.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    resp._content = text.encode()
    resp.headers.update(headers or {})
    return requests.exceptions.HTTPError(f"{status}", response=resp)


class RecordedClient:
    """Stands in for coinbase.rest.RESTClient: answers get/post from recorded JSON by (method, path).
    An answer may be a fixture name, a list (consumed in order; exceptions are raised) or a callable."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[tuple] = []

    def _answer(self, key, arg):
        ans = self.routes[key]
        if isinstance(ans, list):
            ans = ans.pop(0)
        if callable(ans) and not isinstance(ans, BaseException):
            ans = ans(arg)
        if isinstance(ans, BaseException):
            raise ans
        return copy.deepcopy(FIXTURES[ans] if isinstance(ans, str) else ans)

    def get(self, url_path, params=None, public=False, **kwargs):
        self.calls.append(("GET", url_path, params, public))
        return self._answer(("GET", url_path), params)

    def post(self, url_path, params=None, data=None, **kwargs):
        self.calls.append(("POST", url_path, data))
        return self._answer(("POST", url_path), data)


def make(routes: dict, **kw) -> tuple[CoinbaseExchange, RecordedClient, list[float]]:
    client = RecordedClient({(m, API + p): v for (m, p), v in routes.items()})
    slept: list[float] = []
    ex = CoinbaseExchange(client, FUT, "BTC-USD", sleep=slept.append, clock=lambda: 1_790_000_000.0, **kw)
    return ex, client, slept


def test_product_spec_and_session_from_the_recorded_product():
    ex, client, _ = make({("GET", f"/products/{FUT}"): "product_bip"})
    spec = ex.product()
    assert (spec.product_id, spec.contract_size, spec.price_increment, spec.base_increment) == (FUT, 0.01, 5.0, 1.0)
    assert spec.overnight_margin == (0.21025, 0.256625) and spec.intraday_margin == (0.100079, 0.0999811)
    s = ex.session()
    assert s.state == "OPEN" and s.is_open
    assert (s.maintenance_start, s.maintenance_end) == (1792836000, 1792868400)  # 2026-10-24 10:00-19:00 UTC


def test_quotes_come_from_the_public_order_book():
    ex, client, _ = make({("GET", "/market/product_book"): lambda p: "book_bip" if p["product_id"] == FUT else "book_btc"})
    q, s = ex.quote(FUT), ex.quote("BTC-USD")
    assert (q.bid, q.ask, q.mid) == (84655.0, 84680.0, 84667.5)
    assert s.mid == pytest.approx(84671.265)
    assert client.calls[0] == ("GET", API + "/market/product_book", {"product_id": FUT, "limit": 1}, True)


@pytest.mark.parametrize(("bid", "ask"), [("", "84680"), ("0", "84680"), ("-5", "84680"), ("NaN", "84680"),
                                          ("84655", "inf")])
def test_an_unusable_top_of_book_is_unavailable_not_a_price(bid, ask):
    book = {"pricebook": {"bids": [{"price": bid, "size": "1"}], "asks": [{"price": ask, "size": "1"}]}}
    ex, _, _ = make({("GET", "/market/product_book"): book})
    with pytest.raises(Unavailable):  # a bid read as 0 would make a mid of half the price (and a basis ratio of 0.5)
        ex.quote(FUT)


def test_position_picks_the_bip_product():
    ex, _, _ = make({("GET", "/cfm/positions"): ["positions", "positions_flat"]})
    assert ex.position() == Position(-1, 3, 85100.0)
    assert ex.position() is None


def test_balance_is_cash_plus_unrealized_pnl_and_permissions():
    ex, _, _ = make({("GET", "/cfm/balance_summary"): "balance_summary", ("GET", "/key_permissions"): "key_permissions"})
    b = ex.balance()
    assert b.equity == pytest.approx(5013.05) and b.available_margin == pytest.approx(4361.25)
    k = ex.key_permissions()
    assert (k.can_view, k.can_trade, k.can_transfer, k.portfolio_type) == (True, True, False, "DEFAULT")


def test_open_orders_follow_the_cursor_and_parse_brackets():
    ex, client, _ = make({("GET", "/orders/historical/batch"): ["orders_open_page1", "orders_open_page2"]})
    orders = ex.open_orders()
    assert [(o.client_order_id, o.kind, o.size, o.limit_price, o.stop_price, o.status) for o in orders] == [
        ("ewb-b-1790942400-tp1-1", "BRACKET", 1, 83500.0, 85900.0, "OPEN"),
        ("ewb-b-1790942400-tp2-1", "BRACKET", 2, 82000.0, 85900.0, "CANCEL_QUEUED"),  # may still fill
    ]
    assert all(o.is_open for o in orders)
    assert orders[0].side == 1 and orders[0].created_ts == 1791028841
    assert client.calls[0][2]["product_ids"] == [FUT]
    assert client.calls[0][2]["order_status"] == ["OPEN", "PENDING", "QUEUED", "EDIT_QUEUED", "CANCEL_QUEUED"]
    assert client.calls[1][2]["cursor"] == "c-2"


def test_find_and_get_order():
    ex, client, _ = make({("GET", "/orders/historical/batch"): "orders_history",
                          ("GET", "/orders/historical/22222222-bbbb-4000-8000-000000000001"): "order_get"})
    o = ex.find_order("ewb-e-1790942400-S", since_ts=1_791_028_000)
    assert o is not None and (o.kind, o.filled, o.avg_fill_price, o.status, o.side) == ("MARKET", 3, 85100.0, "FILLED", -1)
    assert client.calls[0][2]["start_date"] == "2026-10-03T11:46:40Z"
    assert ex.find_order("nope", since_ts=0) is None
    got = ex.get_order("22222222-bbbb-4000-8000-000000000001")
    assert (got.filled, got.fee) == (3, 0.45)


def test_market_and_bracket_payloads():
    ex, client, _ = make({("POST", "/orders"): ["create_ok", "create_ok"]})
    r = ex.place_market("ewb-e-1790942400-S", -1, 3)
    assert r.ok and r.order_id == "33333333-cccc-4000-8000-000000000001"
    assert client.calls[0][2] == {"client_order_id": "ewb-e-1790942400-S", "product_id": FUT, "side": "SELL",
                                  "order_configuration": {"market_market_ioc": {"base_size": "3"}}}
    ex.place_bracket("ewb-b-1790942400-tp1-1", 1, 1, 83500.0, 85900.0)
    assert client.calls[1][2]["order_configuration"] == {
        "trigger_bracket_gtc": {"base_size": "1", "limit_price": "83500", "stop_trigger_price": "85900"}}
    with pytest.raises(ValueError):
        ex.place_market("x", 0, 1)


def test_rejections_and_duplicates_are_results_not_exceptions():
    ex, _, _ = make({("POST", "/orders"): ["create_insufficient", "create_duplicate"]})
    r = ex.place_market("a", 1, 1)
    assert not r.ok and r.error.startswith("INSUFFICIENT_FUND") and not r.duplicate
    d = ex.place_market("a", 1, 1)
    assert not d.ok and d.duplicate


def test_close_position_edit_cancel_and_preview():
    ex, client, _ = make({
        ("POST", "/orders/close_position"): "close_ok",
        ("POST", "/orders/edit"): ["edit_ok", http_error(400, "edit not supported")],
        ("POST", "/orders/batch_cancel"): "cancel_ok",
        ("POST", "/orders/preview"): ["preview_ok", "preview_err"],
    })
    assert ex.close_position("ewb-x-1790942400-1", size=2).ok
    assert client.calls[0][2] == {"client_order_id": "ewb-x-1790942400-1", "product_id": FUT, "size": "2"}
    assert ex.edit_bracket("o1", 2, 82000.0, 85000.0) is True
    assert client.calls[1][2] == {"order_id": "o1", "size": "2", "price": "82000", "stop_price": "85000"}
    assert ex.edit_bracket("o1", 2, 82000.0, 85000.0) is False  # unsupported -> the executor replaces
    ex.cancel(["11111111-aaaa-4000-8000-000000000001"])
    ex.cancel([])
    assert [c[1] for c in client.calls].count(API + "/orders/batch_cancel") == 1
    ok = ex.preview_market(1, 1)
    bad = ex.preview_bracket(-1, 1, 83000.0, 86000.0)
    assert ok.ok and not bad.ok and bad.errors == ("PREVIEW_INSUFFICIENT_FUNDS_FOR_FUTURES",)


def test_reads_retry_429_and_5xx_then_give_up():
    ex, client, slept = make({("GET", "/cfm/positions"): [http_error(429, "slow down", {"Retry-After": "2"}),
                                                            http_error(503, "busy"), "positions"]})
    assert ex.position().contracts == 3 and slept == [2.0, 2.0]
    ex2, _, _ = make({("GET", "/cfm/positions"): [requests.exceptions.ConnectionError("x")] * 3})
    with pytest.raises(Unavailable):
        ex2.position()
    ex3, c3, _ = make({("GET", "/cfm/positions"): [http_error(401, "unauthorized"), "positions"]})
    with pytest.raises(ExchangeError, match="HTTP 401"):
        ex3.position()
    assert len(c3.calls) == 1  # a 4xx is final


def test_order_placement_is_sent_once():
    ex, client, _ = make({("POST", "/orders"): [http_error(502, "bad gateway"), "create_ok"]})
    with pytest.raises(Unavailable):
        ex.place_market("ewb-e-1-L", 1, 1)
    assert len(client.calls) == 1  # ambiguous: the executor looks the order up before resending


def test_rate_limit_headers_pace_the_next_request():
    resp = dict(FIXTURES["positions"], **{"x-ratelimit-remaining": "1", "x-ratelimit-reset": "3"})
    ex, _, slept = make({("GET", "/cfm/positions"): [resp, "positions"]})
    ex.position()
    ex.position()
    assert slept == [3.0]


def test_helpers():
    assert fmt_decimal(84665.0) == "84665" and fmt_decimal(0.5) == "0.5" and fmt_decimal(85000) == "85000"
    assert isinstance(map_error(http_error(429)), RateLimited)
    assert isinstance(map_error(requests.exceptions.Timeout()), Unavailable)
    o = parse_order({"order_id": "x", "side": "SELL", "status": "OPEN", "order_configuration": {}})
    assert (o.kind, o.size, o.client_order_id) == ("OTHER", 0, "")


def test_the_pinned_sdk_matches_the_calls_this_adapter_makes(monkeypatch):
    """Runs only with the `live` dependency group installed (uv run --group live pytest)."""
    rest = pytest.importorskip("coinbase.rest")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    assert metadata.version("coinbase-advanced-py") == "1.8.4"
    init = inspect.signature(rest.RESTClient.__init__).parameters
    assert {"key_file", "timeout", "rate_limit_headers"} <= set(init)
    assert {"url_path", "params", "public"} <= set(inspect.signature(rest.RESTClient.get).parameters)
    assert {"url_path", "data"} <= set(inspect.signature(rest.RESTClient.post).parameters)

    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode()
    client = rest.RESTClient(api_key="organizations/x/apiKeys/y", api_secret=pem, rate_limit_headers=True)
    seen: list[tuple] = []

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        seen.append((method, url, params, json, headers))
        r = requests.Response()
        r.status_code = 200
        r._content = b'{"positions": []}'
        r.headers.update({"x-ratelimit-remaining": "29", "x-ratelimit-reset": "1"})
        return r

    monkeypatch.setattr(client.session, "request", fake_request)
    assert CoinbaseExchange(client, FUT).position() is None
    method, url, _, _, headers = seen[0]
    assert (method, url) == ("GET", "https://api.coinbase.com/api/v3/brokerage/cfm/positions")
    assert headers["Authorization"].startswith("Bearer ") and len(headers["Authorization"]) > 100  # SDK-signed JWT


@pytest.mark.parametrize("size", [0, 0.5, 1.5, 2.9999, -1, float("nan"), float("inf")])
def test_non_integer_or_sub_one_sizes_are_rejected_not_truncated(size):
    """Controller amendment: str(int(size)) would silently send fewer contracts; the adapter refuses like
    SimExchange (INVALID_SIZE_PRECISION) and never reaches the network."""
    ex, client, _ = make({})
    for r in (ex.place_market("a", 1, size), ex.place_bracket("b", 1, size, 83500.0, 85900.0),
              ex.close_position("c", size=size)):
        assert not r.ok and r.order_id is None and r.error == "INVALID_SIZE_PRECISION"
    assert ex.edit_bracket("o1", size, 82000.0, 85000.0) is False
    for p in (ex.preview_market(1, size), ex.preview_bracket(-1, size, 83000.0, 86000.0)):
        assert not p.ok and p.errors == ("PREVIEW_INVALID_SIZE_PRECISION",)
    assert client.calls == []


def test_whole_number_float_sizes_are_still_sent():
    ex, client, _ = make({("POST", "/orders"): "create_ok", ("POST", "/orders/close_position"): "close_ok"})
    assert ex.place_market("a", 1, 3.0).ok
    assert ex.close_position("c", size=2.0).ok
    assert client.calls[0][2]["order_configuration"] == {"market_market_ioc": {"base_size": "3"}}
    assert client.calls[1][2]["size"] == "2"


# ---------------------------------------------------------------------------- Fix round 1
SECRET = "Bearer eyJhbGciOiJFUzI1NiJ9.secret"


@pytest.mark.parametrize("exc", [
    requests.exceptions.ChunkedEncodingError(SECRET),
    requests.exceptions.ContentDecodingError(SECRET),
    requests.exceptions.JSONDecodeError("Expecting value", SECRET, 0),
    requests.exceptions.InvalidHeader(f"Invalid return character or leading space in header: Authorization: {SECRET}"),
    requests.exceptions.ReadTimeout(SECRET),
    requests.exceptions.SSLError(SECRET),
    requests.exceptions.HTTPError(SECRET),  # no response attached
    requests.exceptions.RequestException(SECRET),
])
def test_ambiguous_post_send_failures_are_unavailable_and_sent_once(exc):
    """The order may exist: any requests failure is ambiguous (Unavailable -> lookup by client_order_id),
    never a final rejection; the message is the type name only (an exception text may carry the JWT)."""
    ex, client, _ = make({("POST", "/orders"): [exc, "create_ok"]})
    with pytest.raises(Unavailable) as err:
        ex.place_market("ewb-e-1-L", 1, 1)
    assert str(err.value) == type(exc).__name__ and "secret" not in str(err.value)
    assert len(client.calls) == 1


def test_unknown_exceptions_keep_only_their_type_name():
    err = map_error(ValueError(SECRET))
    assert type(err) is ExchangeError and not err.retryable and str(err) == "ValueError"


def test_failure_reason_prefers_new_order_failure_reason():
    ex, _, _ = make({("POST", "/orders"): ["create_failed_new_reason", "create_duplicate_new_reason"]})
    r = ex.place_bracket("b", -1, 1, 86000.0, 83500.0)
    assert not r.ok and r.error.startswith("INVALID_BRACKET_PRICES") and not r.duplicate
    d = ex.place_market("a", 1, 1)  # error and failure_reason say UNKNOWN_*; new_order_failure_reason decides
    assert not d.ok and d.duplicate and d.error.startswith("DUPLICATE_CLIENT_ORDER_ID")
    assert parse_place({"success": False, "error_response": {"preview_failure_reason": "DUPLICATE_CLIENT_ORDER_ID"}},
                       "c").duplicate
    assert parse_place({"success": False, "failure_reason": "UNKNOWN_FAILURE_REASON"}, "c").error == "UNKNOWN_FAILURE_REASON"
    assert not parse_place(FIXTURES["create_insufficient"], "c").duplicate


@pytest.mark.parametrize("body", ["http400_duplicate_order_body", "http400_duplicate_gateway_body"])
def test_an_http_4xx_naming_the_duplicate_id_takes_the_duplicate_path(body):
    text = json.dumps(FIXTURES[body])
    ex, client, _ = make({("POST", "/orders"): [http_error(400, text)],
                          ("POST", "/orders/close_position"): [http_error(409, text)]})
    for r in (ex.place_market("ewb-e-1-L", 1, 1), ex.close_position("ewb-x-1-1")):
        assert not r.ok and r.duplicate and r.order_id is None
    assert len(client.calls) == 2
    ex2, _, _ = make({("POST", "/orders"): [http_error(400, json.dumps(FIXTURES["create_insufficient"]))]})
    with pytest.raises(ExchangeError) as err:
        ex2.place_market("a", 1, 1)
    assert not err.value.retryable


def test_position_sides_are_mapped_explicitly():
    ex, _, _ = make({("GET", "/cfm/positions"): ["positions_bip_long", {"positions": [
        {"product_id": FUT, "side": "SHORT", "number_of_contracts": "-3", "avg_entry_price": "85100"}]},
        {"positions": [{"product_id": FUT, "side": "UNKNOWN", "number_of_contracts": "2", "avg_entry_price": "1"}]},
        {"positions": [{"product_id": FUT, "side": "FUTURES_POSITION_SIDE_LONG", "number_of_contracts": "2"}]}]})
    assert ex.position() == Position(1, 2, 84000.0)
    assert ex.position() == Position(-1, 3, 85100.0)  # abs() of a signed count
    for _ in range(2):
        with pytest.raises(ExchangeError) as err:
            ex.position()
        assert not err.value.retryable


def test_order_sides_are_mapped_explicitly():
    base = {"order_id": "x", "status": "OPEN", "order_configuration": {}}
    assert parse_order({**base, "side": "BUY"}).side == 1 and parse_order({**base, "side": "SELL"}).side == -1
    for side in ("", "UNKNOWN_ORDER_SIDE", None, "buy"):
        with pytest.raises(ExchangeError):
            parse_order({**base, "side": side})


def test_sell_bracket_protects_a_long():
    ex, client, _ = make({("POST", "/orders"): "create_ok"})
    assert ex.place_bracket("ewb-b-1790942400-tp1-1", -1, 2, 86000.0, 83500.0).ok
    assert client.calls[0][2] == {
        "client_order_id": "ewb-b-1790942400-tp1-1", "product_id": FUT, "side": "SELL",
        "order_configuration": {"trigger_bracket_gtc": {"base_size": "2", "limit_price": "86000",
                                                        "stop_trigger_price": "83500"}}}


@pytest.mark.parametrize("status", ["OPEN", "PENDING", "QUEUED", "EDIT_QUEUED", "CANCEL_QUEUED"])
def test_resting_statuses_are_open(status):
    o = parse_order({"order_id": "x", "side": "BUY", "status": status, "order_configuration": {}})
    assert o.status == status and o.is_open and not o.is_terminal
    assert status in OPEN_STATUSES


def test_unknown_order_status_is_neither_open_nor_terminal():
    o = parse_order({"order_id": "x", "side": "BUY", "status": "UNKNOWN_ORDER_STATUS", "order_configuration": {}})
    assert o.status == "UNKNOWN" and not o.is_open and not o.is_terminal


@pytest.mark.parametrize(("value", "expected"), [
    (None, 1.0), ("2", 2.0), ("0.5", 0.5), ("120", RETRY_AFTER_MAX_S), ("-5", 0.0), ("soon", 1.0), ("nan", 1.0),
    ("inf", 1.0), ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0), ("Fri, 31 Dec 2099 23:59:59 GMT", RETRY_AFTER_MAX_S),
])
def test_retry_after_is_parsed_leniently_and_capped(value, expected):
    assert retry_after_s(value) == pytest.approx(expected)
    headers = {} if value is None else {"Retry-After": value}
    err = map_error(http_error(429, "slow down", headers))
    assert isinstance(err, RateLimited) and err.retry_after == pytest.approx(expected)


def test_a_long_retry_after_waits_at_most_the_cap():
    ex, _, slept = make({("GET", "/cfm/positions"): [http_error(429, "", {"Retry-After": "3600"}), "positions"]})
    assert ex.position().contracts == 3 and slept == [RETRY_AFTER_MAX_S]
    resp = dict(FIXTURES["positions"], **{"x-ratelimit-remaining": "lots", "x-ratelimit-reset": "soon"})
    ex2, _, slept2 = make({("GET", "/cfm/positions"): [resp, "positions"]})
    ex2.position()
    ex2.position()
    assert slept2 == []  # unreadable pacing headers are ignored


@pytest.mark.parametrize(("route", "reply", "call"), [
    (("GET", f"/products/{FUT}"), {"price_increment": "5"}, lambda ex: ex.product()),
    (("GET", f"/products/{FUT}"), {"fcm_trading_session_details": {"maintenance": {"start_time": "soon"}}},
     lambda ex: ex.session()),
    (("GET", "/market/product_book"), {"pricebook": {"bids": [{"px": "1"}], "asks": [{"px": "2"}]}},
     lambda ex: ex.quote(FUT)),
    (("GET", "/cfm/positions"), {"positions": [{"product_id": FUT, "side": "LONG", "number_of_contracts": "two"}]},
     lambda ex: ex.position()),
    (("GET", "/orders/historical/batch"), {"orders": [{"side": "BUY", "status": "OPEN"}]}, lambda ex: ex.open_orders()),
    (("GET", "/orders/historical/o1"), {"not_order": {}}, lambda ex: ex.get_order("o1")),
    (("GET", "/cfm/balance_summary"), {"balance_summary": {"total_usd_balance": {"value": "n/a"}}},
     lambda ex: ex.balance()),
    (("POST", "/orders/preview"), {"errs": 5}, lambda ex: ex.preview_market(1, 1)),
    (("POST", "/orders/batch_cancel"), {"results": [7]}, lambda ex: ex.cancel(["o1"])),
])
def test_malformed_replies_raise_exchange_errors_only(route, reply, call):
    ex, _, _ = make({route: reply})
    with pytest.raises(ExchangeError) as err:
        call(ex)
    assert not err.value.retryable


def test_a_reply_that_is_not_an_object_or_an_accepted_order_without_id_is_ambiguous():
    ex, client, _ = make({("POST", "/orders"): [["not", "an", "object"], {"success": True, "success_response": {}}]})
    for _ in range(2):
        with pytest.raises(Unavailable):
            ex.place_market("ewb-e-1-L", 1, 1)
    assert len(client.calls) == 2


@pytest.mark.parametrize("bad", [0.0, -5.0, float("nan"), float("inf")])
def test_non_finite_or_non_positive_prices_are_refused_locally(bad):
    ex, client, _ = make({})
    r = ex.place_bracket("b", -1, 1, bad, 83500.0)
    assert not r.ok and r.error == "INVALID_PRICE_PRECISION"
    r = ex.place_bracket("b", -1, 1, 86000.0, bad)
    assert not r.ok and r.error == "INVALID_STOP_PRICE_PRECISION"
    assert ex.edit_bracket("o1", 1, bad, 83500.0) is False and ex.edit_bracket("o1", 1, 86000.0, bad) is False
    assert ex.preview_bracket(-1, 1, bad, 83500.0).errors == ("PREVIEW_INVALID_PRICE_PRECISION",)
    assert ex.preview_bracket(-1, 1, 86000.0, bad).errors == ("PREVIEW_INVALID_STOP_PRICE_PRECISION",)
    assert client.calls == []


def test_key_permissions_fail_closed():
    ex, _, _ = make({("GET", "/key_permissions"): [{"can_view": True, "can_trade": True},
                                                    {"can_view": True, "can_trade": True, "can_transfer": None}, {}]})
    k = ex.key_permissions()
    assert (k.can_view, k.can_trade, k.can_transfer) == (True, True, True)  # can_transfer missing: assume it can
    assert ex.key_permissions().can_transfer is True  # null: the same
    k = ex.key_permissions()
    assert (k.can_view, k.can_trade, k.can_transfer) == (False, False, True)


@pytest.mark.parametrize("summary", [
    {"balance_summary": {"unrealized_pnl": {"value": "13.05", "currency": "USD"}}},
    {"balance_summary": {"total_usd_balance": {"currency": "USD"}, "unrealized_pnl": {"value": "13.05"}}},
    {"balance_summary": {"total_usd_balance": {"value": "", "currency": "USD"}}},
    {"balance_summary": {"total_usd_balance": {"value": "inf", "currency": "USD"}}},
    {},
])
def test_balance_without_a_cash_total_is_an_error(summary):
    ex, _, _ = make({("GET", "/cfm/balance_summary"): summary})
    with pytest.raises(ExchangeError):
        ex.balance()


def test_cancel_honours_each_result():
    ex, client, _ = make({("POST", "/orders/batch_cancel"): ["cancel_done_or_refused", "cancel_done_or_refused",
                                                             "cancel_ok"]})
    ex.cancel(["o-ok", "o-queued", "o-filled"])  # cancelled, already queued, already filled: nothing rests
    with pytest.raises(ExchangeError) as err:
        ex.cancel(["o-ok", "o-refused"])
    assert "o-refused: UNKNOWN_CANCEL_ORDER" in str(err.value) and "o-ok" not in str(err.value)
    assert not err.value.retryable
    with pytest.raises(ExchangeError, match="o-missing: NO_RESULT"):
        ex.cancel(["11111111-aaaa-4000-8000-000000000001", "o-missing"])
    assert len(client.calls) == 3


def test_the_pinned_sdk_sends_every_open_status_as_a_repeated_parameter(monkeypatch):
    """List Orders takes order_status as an exploded array; SDK 1.8.4 passes the list to requests unchanged
    (list_orders(order_status: Optional[List[str]])), which repeats the key. Needs the `live` group."""
    rest = pytest.importorskip("coinbase.rest")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    assert "List" in str(inspect.signature(rest.RESTClient.list_orders).parameters["order_status"].annotation)
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode()
    client = rest.RESTClient(api_key="organizations/x/apiKeys/y", api_secret=pem)
    urls: list[str] = []

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        urls.append(requests.Request(method, url, params=params).prepare().url)
        r = requests.Response()
        r.status_code = 200
        r._content = b'{"orders": [], "has_next": false, "cursor": ""}'
        return r

    monkeypatch.setattr(client.session, "request", fake_request)
    assert CoinbaseExchange(client, FUT).open_orders() == []
    query = urls[0].split("?", 1)[1]
    assert [p.split("=")[1] for p in query.split("&") if p.startswith("order_status=")] == list(OPEN_STATUSES)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/live/test_coinbase.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'ewbot.live.coinbase'`.

- [ ] **Step 3: Write minimal implementation**

Add the SDK as the `live` dependency group (Decision D1; this rewrites `uv.lock` additively and installs the group into the venv):

```bash
uv add --group live coinbase-advanced-py==1.8.4
git diff --stat pyproject.toml uv.lock
```

Expected: `pyproject.toml | 3 +` (a `live = ["coinbase-advanced-py==1.8.4"]` entry under `[dependency-groups]`) and `uv.lock | 226 +` with no removed line.

**File: `src/ewbot/live/coinbase.py`**

```python
"""CoinbaseExchange: the Exchange protocol over the Coinbase Advanced Trade REST API (Plan 3 spec 2).

The SDK `coinbase-advanced-py==1.8.4` (dependency group `live`) signs every request with a fresh JWT
from the CDP key (ECDSA or Ed25519; 2-minute expiry) - this module never touches the key. Only the
SDK's generic `client.get(path, params, public=...)` / `client.post(path, data=...)` are used, so the
parsers below read the raw JSON of the API (the recorded fixtures in tests/live/fixtures/coinbase.json)
and do not depend on the SDK's response classes.

Retries (spec 5.1): reads and cancels are retried up to 3 times on HTTP 429 / 5xx / network errors
(429 waits for the server's Retry-After, at most RETRY_AFTER_MAX_S); order placements are sent ONCE -
their failure is ambiguous (the order may exist) and the executor looks the order up by client_order_id
before sending it again. Every requests failure other than an HTTP status (a broken or undecodable
reply after the send, a bad header, ...) is ambiguous too: Unavailable, never a final rejection. When
`x-ratelimit-remaining` drops to `min_remaining`, the next request waits for the reset.

Only ExchangeError (or ValueError for a side that is not +1/-1, a programming error) leaves this module:
an unexpected reply shape is an ExchangeError, a reply that is not a JSON object is Unavailable. Error
messages never carry an exception's text (it may hold the JWT): the HTTP status and the start of the
reply body, or the exception's type name."""

from __future__ import annotations

import math
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

import requests

from ewbot.live.exchange import (
    FUTURES_PRODUCT,
    OPEN_STATUSES,
    SPOT_PRODUCT,
    Balance,
    ExchangeError,
    KeyPermissions,
    Order,
    PlaceResult,
    Position,
    PreviewResult,
    ProductSpec,
    Quote,
    RateLimited,
    SessionInfo,
    Unavailable,
)

API = "/api/v3/brokerage"
SESSION_PREFIX = "FCM_TRADING_SESSION_STATE_"
_STATUS = {"UNKNOWN_ORDER_STATUS": "UNKNOWN"}  # EDIT_QUEUED / CANCEL_QUEUED keep their name: open (exchange.py)
_BRACKETS = ("trigger_bracket_gtc", "trigger_bracket_gtd")
_ORDER_SIDES = {"BUY": 1, "SELL": -1}  # OrderSide
_POSITION_SIDES = {"LONG": 1, "SHORT": -1}  # FCMPositionSide (UNKNOWN is refused)
INVALID_SIZE = "INVALID_SIZE_PRECISION"  # the reasons SimExchange gives, too
INVALID_PRICE = "INVALID_PRICE_PRECISION"
INVALID_STOP = "INVALID_STOP_PRICE_PRECISION"
DUPLICATE_ID = "DUPLICATE_CLIENT_ORDER_ID"
_REASON_FIELDS = ("new_order_failure_reason", "error", "preview_failure_reason")  # most specific first
CANCEL_DONE = frozenset({"DUPLICATE_CANCEL_REQUEST", "ORDER_IS_FULLY_FILLED"})  # nothing rests any more
RETRY_AFTER_MAX_S = 10.0  # never wait longer than the pacing cap on one 429
_PARSE_ERRORS = (KeyError, ValueError, TypeError, AttributeError, IndexError)


class DuplicateClientOrderId(ExchangeError):
    """An HTTP 4xx whose body names DUPLICATE_CLIENT_ORDER_ID: an order with this id exists. The order
    calls turn it into PlaceResult(duplicate=True), so the executor looks the order up."""


@contextmanager
def _parsing(what: str) -> Iterator[None]:
    """A reply that does not have the expected shape -> ExchangeError (type name only)."""
    try:
        yield
    except _PARSE_ERRORS as exc:
        raise ExchangeError(f"unexpected {what} reply ({type(exc).__name__})") from None


def fmt_decimal(x: float | int) -> str:
    """84665.0 -> "84665", 0.5 -> "0.5" (the API takes decimal strings)."""
    d = Decimal(repr(float(x))).normalize()
    return format(d, "f")


def bad_size(size: float) -> bool:
    """True unless size is a whole number of contracts, at least 1. The adapter never truncates
    (str(int(2.9)) would silently send fewer contracts than asked): it refuses, like the venue."""
    try:
        x = float(size)
    except (TypeError, ValueError):
        return True
    return not math.isfinite(x) or x != int(x) or x < 1


def bad_price(price: float) -> bool:
    """True unless price is a finite number above zero (fmt_decimal would send "NaN" or "-5")."""
    try:
        x = float(price)
    except (TypeError, ValueError):
        return True
    return not math.isfinite(x) or x <= 0


def _bracket_problem(size: float, limit_price: float, stop_price: float) -> str | None:
    if bad_size(size):
        return INVALID_SIZE
    if bad_price(limit_price):
        return INVALID_PRICE
    if bad_price(stop_price):
        return INVALID_STOP
    return None


def iso_ts(text: str | None) -> int | None:
    if not text:
        return None
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(x: Any, default: float = 0.0) -> float:
    if x is None or x == "":
        return default
    if isinstance(x, dict):  # {"value": "12.3", "currency": "USD"}
        return _num(x.get("value"), default)
    return float(x)


def retry_after_s(value: Any, now: float | None = None) -> float:
    """Retry-After in seconds - delta-seconds or an HTTP-date - clamped to [0, RETRY_AFTER_MAX_S];
    missing or unreadable -> 1 s."""
    if value is None:
        return 1.0
    text = str(value).strip()
    try:
        secs = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return 1.0
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        secs = when.timestamp() - (time.time() if now is None else now)
    if not math.isfinite(secs):
        return 1.0
    return min(max(secs, 0.0), RETRY_AFTER_MAX_S)


def map_error(exc: BaseException) -> ExchangeError:
    """SDK / requests exception -> ExchangeError (RateLimited, Unavailable or a final error). Messages
    keep the status and the start of the reply only, or the exception's type name - never its text or
    headers (they may carry the JWT)."""
    if isinstance(exc, ExchangeError):
        return exc
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        resp = exc.response
        status = int(resp.status_code)
        body = str(getattr(resp, "text", "") or "")
        text = body[:200]
        if status == 429:
            return RateLimited(f"HTTP 429: {text}", retry_after=retry_after_s((resp.headers or {}).get("Retry-After")))
        if status >= 500:
            return Unavailable(f"HTTP {status}: {text}")
        if DUPLICATE_ID in body:
            return DuplicateClientOrderId(f"HTTP {status}: {text}")
        return ExchangeError(f"HTTP {status}: {text}")
    if isinstance(exc, requests.exceptions.RequestException):
        # connection, timeout, a reply cut short or undecodable after the send, an invalid header, ...:
        # the request may have reached the venue -> ambiguous (the executor looks the order up first)
        return Unavailable(type(exc).__name__)
    return ExchangeError(type(exc).__name__)


def _side_of(text: Any, sides: dict[str, int], what: str) -> int:
    side = sides.get(str(text)) if text is not None else None
    if side is None:
        raise ExchangeError(f"unexpected {what} side {str(text)[:40]!r}")
    return side


def parse_order(o: dict[str, Any]) -> Order:
    with _parsing("order"):
        cfg = o.get("order_configuration") or {}
        kind, limit, stop, size = "OTHER", None, None, None
        if "market_market_ioc" in cfg:
            kind, size = "MARKET", cfg["market_market_ioc"].get("base_size")
        for key in _BRACKETS:
            if key in cfg:
                b = cfg[key]
                kind, size = "BRACKET", b.get("base_size")
                limit, stop = _num(b.get("limit_price"), 0.0) or None, _num(b.get("stop_trigger_price"), 0.0) or None
        filled = int(_num(o.get("filled_size")))
        avg = _num(o.get("average_filled_price")) or None
        status = str(o.get("status") or "UNKNOWN")
        return Order(
            order_id=str(o["order_id"]),
            client_order_id=str(o.get("client_order_id") or ""),
            side=_side_of(o.get("side"), _ORDER_SIDES, "order"),
            kind=kind,
            size=int(_num(size, filled)),
            filled=filled,
            status=_STATUS.get(status, status),
            limit_price=limit,
            stop_price=stop,
            avg_fill_price=avg,
            created_ts=iso_ts(o.get("created_time")),
            fee=None if o.get("total_fees") in (None, "") else _num(o.get("total_fees")),
        )


def _reasons(resp: dict[str, Any]) -> list[str]:
    """The failure reasons of a create / close reply, most specific first: error_response's
    new_order_failure_reason, then its deprecated error and preview_failure_reason, then failure_reason."""
    err = resp.get("error_response") or {}
    found = [err.get(k) for k in _REASON_FIELDS] + [resp.get("failure_reason")]
    return [str(r) for r in found if r]


def parse_place(resp: dict[str, Any], client_order_id: str) -> PlaceResult:
    """A create / close_position reply -> PlaceResult. Accepted without an order id is ambiguous
    (Unavailable: the executor looks the order up by client_order_id)."""
    with _parsing("order placement"):
        if resp.get("success"):
            order_id = str((resp.get("success_response") or {}).get("order_id") or "")
            if not order_id:
                raise Unavailable("order accepted without an order_id")
            return PlaceResult(client_order_id, order_id, True)
        reasons = _reasons(resp)
        reason = next((r for r in reasons if not r.startswith("UNKNOWN_")), "UNKNOWN_FAILURE_REASON")
        err = resp.get("error_response") or {}
        message = str(err.get("message") or err.get("error_details") or "")
        error = f"{reason}: {message}"[:200] if message else reason
        return PlaceResult(client_order_id, None, False, error, duplicate=DUPLICATE_ID in reasons)


class CoinbaseExchange:
    def __init__(
        self,
        client: Any,
        futures_id: str = FUTURES_PRODUCT,
        spot_id: str = SPOT_PRODUCT,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        attempts: int = 3,
        backoff_s: float = 1.0,
        min_remaining: int = 2,
    ) -> None:
        self.client = client
        self.futures_id = futures_id
        self.spot_id = spot_id
        self.sleep = sleep
        self.clock = clock
        self.attempts = attempts
        self.backoff_s = backoff_s
        self.min_remaining = min_remaining
        self._wait_until = 0.0

    @classmethod
    def from_key_file(cls, key_file: Path, futures_id: str = FUTURES_PRODUCT, spot_id: str = SPOT_PRODUCT,
                      timeout: int = 10, **kw: Any) -> CoinbaseExchange:
        """The CDP key JSON as downloaded ({"name": ..., "privateKey": ...}); read by the SDK only."""
        from coinbase.rest import RESTClient  # dependency group `live`

        return cls(RESTClient(key_file=str(key_file), timeout=timeout, rate_limit_headers=True), futures_id, spot_id, **kw)

    # ------------------------------------------------------------------ transport
    def _pace(self, resp: dict[str, Any]) -> None:
        """Rate-limit headers (merged into the reply by the SDK); unreadable values are ignored."""
        remaining = resp.pop("x-ratelimit-remaining", None)
        reset = resp.pop("x-ratelimit-reset", None)
        resp.pop("x-ratelimit-limit", None)
        try:
            if remaining is None or int(_num(remaining)) > self.min_remaining:
                return
            r = _num(reset, 1.0)
        except _PARSE_ERRORS:
            return
        if not math.isfinite(r):
            return
        wait = r - self.clock() if r > 1e9 else r  # epoch seconds or seconds-to-reset
        self._wait_until = self.clock() + min(max(wait, 0.0), RETRY_AFTER_MAX_S)

    def _call(self, method: str, path: str, *, params: dict[str, Any] | None = None,
              data: dict[str, Any] | None = None, public: bool = False, retry: bool = True) -> dict[str, Any]:
        tries = self.attempts if retry else 1
        for attempt in range(tries):
            delay = self._wait_until - self.clock()
            if delay > 0:
                self.sleep(delay)
            try:
                if method == "GET":
                    resp = self.client.get(API + path, params=params, public=public)
                else:
                    resp = self.client.post(API + path, data=data)
                if not isinstance(resp, dict):  # after the send: the request may have been executed
                    raise Unavailable(f"reply is not a JSON object ({type(resp).__name__})")
            except Exception as exc:  # noqa: BLE001 - mapped below
                err = map_error(exc)
                if not err.retryable or attempt == tries - 1:
                    raise err from None
                wait = min(err.retry_after, RETRY_AFTER_MAX_S) if isinstance(err, RateLimited) else self.backoff_s * 2**attempt
                self.sleep(wait)
                continue
            resp = dict(resp)
            self._pace(resp)
            return resp
        raise ExchangeError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------ reads
    def _product_json(self) -> dict[str, Any]:
        return self._call("GET", f"/products/{self.futures_id}")

    def product(self) -> ProductSpec:
        p = self._product_json()
        with _parsing("product"):
            fpd = p.get("future_product_details") or {}
            im, om = fpd.get("intraday_margin_rate") or {}, fpd.get("overnight_margin_rate") or {}
            return ProductSpec(
                product_id=str(p["product_id"]),
                contract_size=_num(fpd.get("contract_size")),
                price_increment=_num(p.get("price_increment")),
                base_increment=_num(p.get("base_increment")),
                intraday_margin=(_num(im.get("long_margin_rate")), _num(im.get("short_margin_rate"))),
                overnight_margin=(_num(om.get("long_margin_rate")), _num(om.get("short_margin_rate"))),
            )

    def session(self) -> SessionInfo:
        p = self._product_json()
        with _parsing("session"):
            s = p.get("fcm_trading_session_details") or {}
            state = str(s.get("session_state") or "UNKNOWN").removeprefix(SESSION_PREFIX)
            m = s.get("maintenance") or {}
            return SessionInfo(state, iso_ts(m.get("start_time")), iso_ts(m.get("end_time")))

    def quote(self, product_id: str) -> Quote:
        book = self._call("GET", "/market/product_book", params={"product_id": product_id, "limit": 1}, public=True)
        with _parsing("product_book"):
            pb = book.get("pricebook") or {}
            bids, asks = pb.get("bids") or [], pb.get("asks") or []
            if not bids or not asks:
                raise Unavailable(f"empty order book for {product_id}")
            bid, ask = _num(bids[0]["price"], math.nan), _num(asks[0]["price"], math.nan)
            if not (math.isfinite(bid) and math.isfinite(ask) and bid > 0 and ask > 0):
                raise Unavailable(f"unusable top of book for {product_id}")  # never a price of 0, NaN or inf
            return Quote(product_id, bid, ask, self.clock())

    def position(self) -> Position | None:
        reply = self._call("GET", "/cfm/positions")
        with _parsing("positions"):
            for p in reply.get("positions") or []:
                if p.get("product_id") != self.futures_id:
                    continue
                n = abs(int(_num(p.get("number_of_contracts"))))
                if n == 0:
                    return None
                return Position(_side_of(p.get("side"), _POSITION_SIDES, "position"), n, _num(p.get("avg_entry_price")))
            return None

    def _orders(self, params: dict[str, Any]) -> list[Order]:
        out: list[Order] = []
        cursor: str | None = None
        while True:
            page = self._call("GET", "/orders/historical/batch",
                              params={**params, "product_ids": [self.futures_id], "limit": 250, "cursor": cursor})
            with _parsing("orders"):
                out += [parse_order(o) for o in page.get("orders") or []]
                cursor = page.get("cursor") or None
                more = bool(page.get("has_next"))
            if not more or not cursor:
                return out

    def open_orders(self) -> list[Order]:
        """Every resting order: order_status is a repeated query parameter (List Orders reference,
        `explode: true`; SDK 1.8.4 list_orders takes `order_status: List[str]`)."""
        return [o for o in self._orders({"order_status": list(OPEN_STATUSES)}) if o.is_open]

    def get_order(self, order_id: str) -> Order:
        reply = self._call("GET", f"/orders/historical/{order_id}")
        with _parsing("order"):
            return parse_order(reply["order"])

    def find_order(self, client_order_id: str, since_ts: float) -> Order | None:
        for o in self._orders({"start_date": _iso(since_ts)}):
            if o.client_order_id == client_order_id:
                return o
        return None

    def balance(self) -> Balance:
        """Equity = total_usd_balance (cash) + unrealized PnL. Without a readable, finite cash total there
        is no equity: an error, never PnL alone (the breakers and sizing read it)."""
        reply = self._call("GET", "/cfm/balance_summary")
        with _parsing("balance_summary"):
            bs = reply.get("balance_summary") or {}
            cash = _num(bs.get("total_usd_balance"), math.nan)
            if not math.isfinite(cash):
                raise ExchangeError("balance_summary without a total_usd_balance")
            equity = cash + _num(bs.get("unrealized_pnl"))
            avail = bs.get("available_margin")
            return Balance(equity, None if avail is None else _num(avail))

    def key_permissions(self) -> KeyPermissions:
        """Fail closed: a missing can_view / can_trade reads False, a missing can_transfer reads True
        (the preflight refuses a key that can transfer)."""
        k = self._call("GET", "/key_permissions")
        with _parsing("key_permissions"):
            transfer = k.get("can_transfer")
            return KeyPermissions(bool(k.get("can_view")), bool(k.get("can_trade")),
                                  True if transfer is None else bool(transfer), k.get("portfolio_type"))

    # ------------------------------------------------------------------ writes
    @staticmethod
    def _side(side: int) -> str:
        if side not in (1, -1):
            raise ValueError(f"side must be +1 or -1, got {side}")
        return "BUY" if side > 0 else "SELL"

    def _market_cfg(self, size: int) -> dict[str, Any]:
        return {"market_market_ioc": {"base_size": str(int(size))}}

    def _bracket_cfg(self, size: int, limit_price: float, stop_price: float) -> dict[str, Any]:
        return {"trigger_bracket_gtc": {"base_size": str(int(size)), "limit_price": fmt_decimal(limit_price),
                                        "stop_trigger_price": fmt_decimal(stop_price)}}

    def _send_order(self, path: str, data: dict[str, Any], client_order_id: str) -> PlaceResult:
        """POST once; a 4xx naming DUPLICATE_CLIENT_ORDER_ID is the duplicate path, not a final error."""
        try:
            resp = self._call("POST", path, data=data, retry=False)
        except DuplicateClientOrderId as exc:
            return PlaceResult(client_order_id, None, False, f"{DUPLICATE_ID}: {exc}"[:200], duplicate=True)
        return parse_place(resp, client_order_id)

    def _create(self, client_order_id: str, side: int, cfg: dict[str, Any]) -> PlaceResult:
        data = {"client_order_id": client_order_id, "product_id": self.futures_id, "side": self._side(side),
                "order_configuration": cfg}
        return self._send_order("/orders", data, client_order_id)

    def place_market(self, client_order_id: str, side: int, size: int) -> PlaceResult:
        self._side(side)
        if bad_size(size):
            return PlaceResult(client_order_id, None, False, INVALID_SIZE)
        return self._create(client_order_id, side, self._market_cfg(size))

    def place_bracket(
        self, client_order_id: str, side: int, size: int, limit_price: float, stop_price: float
    ) -> PlaceResult:
        self._side(side)
        problem = _bracket_problem(size, limit_price, stop_price)
        if problem is not None:
            return PlaceResult(client_order_id, None, False, problem)
        return self._create(client_order_id, side, self._bracket_cfg(size, limit_price, stop_price))

    def edit_bracket(self, order_id: str, size: int, limit_price: float, stop_price: float) -> bool:
        """Edit a resting bracket's stop (spec 4.2). Whether CFM brackets accept stop_price edits is
        confirmed in CANARY; any failure returns False and the executor replaces the order instead."""
        if _bracket_problem(size, limit_price, stop_price) is not None:
            return False
        data = {"order_id": order_id, "size": str(int(size)), "price": fmt_decimal(limit_price),
                "stop_price": fmt_decimal(stop_price)}
        try:
            return bool(self._call("POST", "/orders/edit", data=data, retry=False).get("success"))
        except ExchangeError:
            return False

    def cancel(self, order_ids: Sequence[str]) -> None:
        """batch_cancel, retried like a read (a repeated cancel is answered DUPLICATE_CANCEL_REQUEST).
        Raises ExchangeError naming each order that may still rest: refused, or missing from results."""
        if not order_ids:
            return
        ids = [str(i) for i in order_ids]
        reply = self._call("POST", "/orders/batch_cancel", data={"order_ids": ids})
        with _parsing("batch_cancel"):
            results = {str(r.get("order_id")): r for r in reply.get("results") or []}
            refused = []
            for oid in ids:
                r = results.get(oid)
                if r is None:
                    refused.append(f"{oid}: NO_RESULT")
                elif not r.get("success") and str(r.get("failure_reason")) not in CANCEL_DONE:
                    refused.append(f"{oid}: {r.get('failure_reason') or 'UNKNOWN_CANCEL_FAILURE_REASON'}")
        if refused:
            raise ExchangeError("cancel refused: " + ", ".join(refused))

    def close_position(self, client_order_id: str, size: int | None = None) -> PlaceResult:
        if size is not None and bad_size(size):
            return PlaceResult(client_order_id, None, False, INVALID_SIZE)
        data: dict[str, Any] = {"client_order_id": client_order_id, "product_id": self.futures_id}
        if size is not None:
            data["size"] = str(int(size))
        return self._send_order("/orders/close_position", data, client_order_id)

    def _preview(self, side: int, cfg: dict[str, Any]) -> PreviewResult:
        resp = self._call("POST", "/orders/preview",
                          data={"product_id": self.futures_id, "side": self._side(side), "order_configuration": cfg})
        with _parsing("preview"):
            errs = tuple(str(e) for e in resp.get("errs") or ())
            warns = tuple(str(w) for w in resp.get("warning") or ())
            return PreviewResult(not errs, errs, warns)

    def preview_market(self, side: int, size: int) -> PreviewResult:
        self._side(side)
        if bad_size(size):
            return PreviewResult(False, (f"PREVIEW_{INVALID_SIZE}",))
        return self._preview(side, self._market_cfg(size))

    def preview_bracket(self, side: int, size: int, limit_price: float, stop_price: float) -> PreviewResult:
        self._side(side)
        problem = _bracket_problem(size, limit_price, stop_price)
        if problem is not None:
            return PreviewResult(False, (f"PREVIEW_{problem}",))
        return self._preview(side, self._bracket_cfg(size, limit_price, stop_price))
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run --group live pytest tests/live/test_coinbase.py -q
```

Expected: `81 passed` (with `uv run pytest` and without the group the two SDK tests are skipped: `79 passed, 2 skipped`).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock src/ewbot/live/coinbase.py tests/live/fixtures/coinbase.json tests/live/test_coinbase.py
git commit -m "$(cat <<'EOF'
feat(live): Coinbase Advanced Trade adapter on coinbase-advanced-py 1.8.4 (live dependency group)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Target state from the engine replay

**Files:**
- Create: `src/ewbot/live/target.py`
- Modify: `tests/paper/test_static.py` (one line, Decision D2)
- Test: `tests/live/test_target.py`

**Interfaces:**
- Consumes: `ewbot.backtest.runner.run_backtest`, `ewbot.backtest.holdout` (`holdout_config`, `lock_digest`), `ewbot.backtest.walkforward` (`HOLDOUT_START`, `grid_combos`), `ewbot.config` (`BotConfig`, `CONSERVATIVE`), `ewbot.paper.trace.traced_engine` / `EngineTrace`, `ewbot.paper.decisions.build_decisions`, the engine module `ewbot.engine.loop` (its `_Run` name, read-only swap; the paper broker's `_stop_triggered_while_closed` flag, read only), `tests.paper.helpers` (`PARAMS`, `make_lock`, `synth_frame`, `ts`).
- Produces (`ewbot.live.target`): `class TargetError(RuntimeError)`; frozen dataclasses `TargetPosition(trade_key, signal_ts, setup, direction, entry_price, stop, stop0, tp1, tp2, tp1_done, contracts, remaining)`, `TargetEntry(signal_ts, setup, direction, entry_ref, stop, tp1, tp2, risk_mult, contracts, reason)`, `Target(bar_ts, close, position, exit_reason, entry, decisions: tuple[dict, ...], exit_queued_ts=None, stop_pending_reopen=False)` with `to_json() -> dict` — `exit_reason` is the exit queued for the open position and not executed by T (queued at T, or before T and carried across the CFM halt; `exit_queued_ts` is the bar whose close queued it), `stop_pending_reopen` means the halt bar T crossed the open position's stop (the engine stops it out at the reopen); `lock_params(lock) -> tuple[float, ...]` and `frozen_config(lock) -> BotConfig` (the paper precheck's lock checks without its git checks: a grid combo, `HOLDOUT_START`, `sha256 == lock_digest(BotConfig(), params, random_runs, code_sha256)`; then `holdout_config(BotConfig(), params)`, conservative with the breakers on; `TargetError` otherwise); `with_placeholder(df1h, bar_ts) -> DataFrame`; context manager `traced_with_exits() -> (EngineTrace, exits: list[(bar ts, reason)], halt_stops: list[bar ts])`; `compute_target(lock, df1h, bar_ts) -> Target` (no configuration or start parameter; `TargetError` for a lock that is not the holdout's, a missing bar or an inconsistent replay). All prices are spot prices; `contracts` are the engine's own sizes (informational).

- [ ] **Step 1: Write the failing test**

**File: `tests/live/test_target.py`**

```python
from __future__ import annotations

import dataclasses
import inspect
from dataclasses import dataclass
from typing import Any

import pandas as pd
import pytest

import ewbot.engine.loop as loop_mod
import ewbot.live.target as target_mod
from ewbot.backtest.holdout import holdout_config
from ewbot.backtest.runner import run_backtest
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.config import BotConfig
from ewbot.data.candles import futures_closed
from ewbot.engine.position_mgr import EngineResult
from ewbot.live.target import TargetError, compute_target, frozen_config, traced_with_exits, with_placeholder
from ewbot.paper.decisions import build_decisions
from ewbot.paper.trace import EngineTrace
from ewbot.strategy.base import Signal
from ewbot.types import H1
from tests.paper.helpers import PARAMS, make_lock, synth_frame, ts

SWEEP_EVERY = 23  # coprime with 24: the sampled sweep visits every hour of the day and every weekday
LONG_ENTRY = ts("2024-01-12 15:00")  # the synthetic long open across two CFM halts (trade key)
HALT = ts("2024-01-12 22:00")  # Fri 17:00 New York (EST): the CFM halt bar while that long is open
FLIP_AT = ts("2024-01-16 10:00")  # a Tuesday bar while that long is open, with no exit queued


@dataclass
class Ref:
    df: pd.DataFrame
    lock: dict[str, Any]
    res: EngineResult
    trace: EngineTrace
    exits: list[tuple[int, str]]
    halt_stops: list[int]
    decisions: list[dict[str, Any]]


def replay(df: pd.DataFrame) -> tuple[EngineResult, EngineTrace, list[tuple[int, str]], list[int]]:
    with traced_with_exits() as (trace, exits, halt_stops):
        res = run_backtest(holdout_config(BotConfig(), PARAMS), df, HOLDOUT_START, None, allow_holdout=True)
    return res, trace, exits, halt_stops


@pytest.fixture(scope="module")
def lock() -> dict[str, Any]:
    return make_lock(synth_frame())


@pytest.fixture(scope="module")
def ref(lock) -> Ref:
    """One replay over every synthetic bar: the reference the hourly targets must agree with."""
    df = synth_frame()
    res, trace, exits, halt_stops = replay(df)
    return Ref(df, lock, res, trace, exits, halt_stops, build_decisions(res, trace, list(res.trades)))


def _event_bars(ref: Ref) -> list[int]:
    """Every bar where something happens in the reference, with its neighbours."""
    marks = {o.ts for o in ref.trace.orders} | {f.ts for f in ref.trace.fills} | {s.ts for s in ref.trace.stops}
    marks |= {t.exit_ts - H1 for t in ref.res.trades}
    last = int(ref.res.equity[-1].ts)
    return sorted({m + k * H1 for m in marks for k in (-1, 0, 1) if HOLDOUT_START <= m + k * H1 < last})


def _assert_matches(ref: Ref, t: int) -> None:
    """compute_target at bar t (frame cut at t + the placeholder) equals the full replay's state after t."""
    tg = compute_target(ref.lock, ref.df, t)
    # entry queued at T's close
    queued = [o for o in ref.trace.orders if o.ts == t]
    if queued:
        o = queued[0]
        assert tg.entry is not None, t
        assert (tg.entry.direction, tg.entry.stop, tg.entry.tp1, tg.entry.tp2, tg.entry.contracts) == (
            o.direction, o.stop, o.tp1, o.tp2, o.contracts)
    else:
        assert tg.entry is None, t
    # position still open after T (a trade exiting at T + 1h's open is still open after T)
    live = [x for x in ref.res.trades if x.entry_ts <= t < x.exit_ts]
    if live:
        x = live[0]
        stops = [s.new for s in ref.trace.stops if s.trade_key == x.entry_ts and s.ts <= t]
        tp1 = any(f.kind == "TP1" and f.trade_key == x.entry_ts and f.ts <= t for f in ref.trace.fills)
        p = tg.position
        assert p is not None, t
        assert (p.trade_key, p.direction, p.setup, p.entry_price, p.tp1, p.tp2) == (
            x.entry_ts, x.direction, x.setup, x.entry_price, x.tp1, x.tp2)
        assert p.stop == (stops[-1] if stops else x.stop0) and p.tp1_done == tp1, t
        assert p.signal_ts == x.entry_ts - H1
        # the exit queued for this trade and not executed by T (queued at T, or before T across the halt)
        pending = [(e, r) for e, r in ref.exits if x.entry_ts <= e <= t]
        assert (tg.exit_queued_ts, tg.exit_reason) == (pending[-1] if pending else (None, None)), t
        assert tg.stop_pending_reopen == (t in ref.halt_stops), t
    else:
        assert tg.position is None, t
        assert (tg.exit_reason, tg.exit_queued_ts, tg.stop_pending_reopen) == (None, None, False), t
    # decisions up to T
    assert list(tg.decisions) == [d for d in ref.decisions if d["ts"] <= t], t


def _force_exit(monkeypatch, at: int, reason: str) -> None:
    """The engine requests `reason` at the close of bar `at` and again at the next close (BREAKER and LINEAGE
    repeat while their condition holds; only a trade's first request counts): a constructed breaker or
    broken lineage on a chosen bar."""

    class Forced(loop_mod._Run):  # type: ignore[misc, valid-type]
        def _may_enter(self, bar, t_close):  # type: ignore[no-untyped-def]
            if bar.ts in (at, at + H1):
                self._request_exit(reason)
            return super()._may_enter(bar, t_close)

    monkeypatch.setattr(loop_mod, "_Run", Forced)


def test_the_placeholder_bar_is_flat_and_refuses_a_missing_bar(ref):
    df = ref.df
    t = ts("2024-03-13 22:00")
    out = with_placeholder(df, t)
    assert int(out["ts"].iloc[-1]) == t + H1 and int(out["ts"].iloc[-2]) == t
    last = out.iloc[-1]
    assert last["open"] == last["high"] == last["low"] == last["close"] == df[df["ts"] == t]["close"].iloc[0]
    assert last["volume"] == 0.0
    with pytest.raises(TargetError):
        with_placeholder(df[df["ts"] < t], t)


def test_the_hourly_target_equals_the_full_replay_at_every_event_bar(ref):
    bars = _event_bars(ref)
    assert len(bars) > 60
    for t in bars:
        _assert_matches(ref, t)


def test_a_sampled_sweep_of_the_holdout_bars_equals_the_full_replay(ref):
    """Bar by bar beyond the event bars: every SWEEP_EVERY-th holdout bar, every CFM halt bar and the bar
    before it (the event bars are covered above)."""
    last = int(ref.res.equity[-1].ts)
    hours = [int(x) for x in ref.df["ts"] if HOLDOUT_START <= int(x) < last]
    halts = [h for h in hours if futures_closed(h)]
    bars = sorted((set(hours[::SWEEP_EVERY]) | set(halts) | {h - H1 for h in halts}) - set(_event_bars(ref)))
    assert len(bars) >= 80 and len(halts) >= 10
    assert any(x.entry_ts <= h < x.exit_ts for h in halts for x in ref.res.trades)  # a position across a halt
    for t in bars:
        _assert_matches(ref, t)


def test_queued_exits_carry_the_engine_reason(ref):
    timed = [x for x in ref.res.trades if x.exit_reason == "TIME"]
    assert timed
    for x in timed:
        tg = compute_target(ref.lock, ref.df, x.exit_ts - H1)
        assert tg.exit_reason == "TIME" and tg.exit_queued_ts == x.exit_ts - H1
        assert tg.position is not None and tg.position.trade_key == x.entry_ts


def test_a_signal_bar_gives_an_entry_with_the_engine_levels(ref):
    t = ts("2024-03-13 22:00")
    tg = compute_target(ref.lock, ref.df, t)
    sig = next(s for s in ref.res.signals if s.ts == t)
    assert tg.entry is not None and tg.position is None
    assert (tg.entry.setup, tg.entry.direction, tg.entry.entry_ref, tg.entry.risk_mult, tg.entry.reason) == (
        sig.setup, sig.direction, sig.entry_ref, sig.risk_mult, sig.reason)
    assert tg.decisions[-1]["id"] == f"SIGNAL:{t}:{t}"
    js = tg.to_json()
    assert js["entry"]["signal_ts"] == t and js["position"] is None
    assert (js["exit_reason"], js["exit_queued_ts"], js["stop_pending_reopen"]) == (None, None, False)


def test_the_engine_class_is_restored_after_an_error():
    original = loop_mod._Run
    with pytest.raises(RuntimeError):
        with traced_with_exits():
            assert loop_mod._Run is not original
            raise RuntimeError("boom")
    assert loop_mod._Run is original


def test_the_target_is_bound_to_the_holdout_lock(lock, ref):
    """No caller passes a config or a start: both come from the lock, checked like the paper precheck."""
    assert list(inspect.signature(compute_target).parameters) == ["lock", "df1h", "bar_ts"]
    assert frozen_config(lock) == holdout_config(BotConfig(), PARAMS)
    bad = {
        "sha256": dict(lock, sha256="0" * 64),  # not lock_digest(BotConfig(), params, ...): another configuration
        "code": dict(lock, code_sha256="f" * 64),  # bound through lock_digest as well
        "start": dict(lock, holdout_start=HOLDOUT_START + H1),
        "grid": make_lock(ref.df, params=(9.0, 9.0, 9.0)),  # self-consistent digest, but not a grid combo
        "missing": {k: v for k, v in lock.items() if k != "random_runs"},
        "garbage": dict(lock, params="2.0,0.5,0.0x"),
    }
    for b in bad.values():
        with pytest.raises(TargetError):
            frozen_config(b)
        with pytest.raises(TargetError):
            compute_target(b, ref.df, ts("2024-03-13 22:00"))


@pytest.mark.parametrize("reason", ["BREAKER", "LINEAGE"])
def test_an_exit_queued_before_the_cfm_halt_is_still_pending_at_the_halt_bar(lock, monkeypatch, reason):
    fri16 = HALT - H1  # Fri 16:00 New York
    assert futures_closed(HALT) and not futures_closed(fri16) and not futures_closed(HALT + H1)
    _force_exit(monkeypatch, fri16, reason)
    df = synth_frame()
    before = compute_target(lock, df, fri16)
    assert before.position is not None and before.position.trade_key == LONG_ENTRY
    assert (before.exit_reason, before.exit_queued_ts) == (reason, fri16)
    halt = compute_target(lock, df, HALT)  # nothing fills while the futures are closed: the exit is still queued
    assert halt.position is not None and halt.position.trade_key == LONG_ENTRY
    assert (halt.exit_reason, halt.exit_queued_ts, halt.stop_pending_reopen) == (reason, fri16, False)
    assert halt.to_json()["exit_queued_ts"] == fri16
    after = compute_target(lock, df, HALT + H1)  # the engine closed it at the reopen's open
    assert after.position is None and (after.exit_reason, after.exit_queued_ts) == (None, None)
    res, _, exits, _ = replay(df)
    x = next(t for t in res.trades if t.entry_ts == LONG_ENTRY)
    assert (x.exit_ts, x.exit_reason) == (HALT + H1, reason) and (fri16, reason) in exits


def test_a_stop_touched_during_the_halt_bar_is_flagged(lock, ref):
    x = next(t for t in ref.res.trades if t.entry_ts == LONG_ENTRY)
    assert x.direction == 1 and x.exit_ts > HALT + H1
    stops = [s.new for s in ref.trace.stops if s.trade_key == LONG_ENTRY and s.ts < HALT]
    stop = stops[-1] if stops else x.stop0  # the stop in force during the halt bar
    df = ref.df.copy()
    i = df.index[df["ts"] == HALT][0]
    assert df.loc[i, "low"] > stop
    df.loc[i, "low"] = stop - 10.0  # the halt bar's range crosses the stop (spot trades through the CFM halt)
    tg = compute_target(lock, df, HALT)
    assert tg.position is not None and tg.position.trade_key == LONG_ENTRY
    assert tg.stop_pending_reopen and tg.exit_reason is None and tg.to_json()["stop_pending_reopen"] is True
    assert not compute_target(lock, df, HALT - H1).stop_pending_reopen
    after = compute_target(lock, df, HALT + H1)
    assert after.position is None or after.position.trade_key != LONG_ENTRY
    assert not after.stop_pending_reopen
    res, _, _, halt_stops = replay(df)
    y = next(t for t in res.trades if t.entry_ts == LONG_ENTRY)
    assert (y.exit_ts, y.exit_reason) == (HALT + H1, "STOP") and halt_stops == [HALT]


def test_a_flip_target_holds_the_position_its_queued_exit_and_the_opposite_entry(lock, monkeypatch):
    """The engine flips at FLIP_AT: the long is still open after that bar, its FLIP exit and the short entry are
    both queued at the close; one hour later the short is the position."""

    class Flipping(loop_mod._Run):  # type: ignore[misc, valid-type]
        def __init__(self, cfg, strategy, wave, choch, start_ts) -> None:  # type: ignore[no-untyped-def]
            super().__init__(cfg, strategy, wave, choch, start_ts)
            inner = self.strategy

            class Opposite:
                def on_bar(self, cbar, ws, ch):  # type: ignore[no-untyped-def]
                    sig = inner.on_bar(cbar, ws, ch)
                    if cbar.ts != FLIP_AT:
                        return sig
                    c = cbar.close
                    return Signal(FLIP_AT, "C_END", -1, c, c + 300.0, c - 600.0, c - 1200.0, 1.0, None, None, None,
                                  "constructed flip")

            self.strategy = Opposite()

    monkeypatch.setattr(loop_mod, "_Run", Flipping)
    df = synth_frame()
    tg = compute_target(lock, df, FLIP_AT)
    assert tg.position is not None and (tg.position.trade_key, tg.position.direction) == (LONG_ENTRY, 1)
    assert (tg.exit_reason, tg.exit_queued_ts) == ("FLIP", FLIP_AT)
    assert tg.entry is not None and (tg.entry.signal_ts, tg.entry.direction, tg.entry.reason) == (
        FLIP_AT, -1, "constructed flip")
    assert tg.decisions[-1]["id"] == f"SIGNAL:{FLIP_AT}:{FLIP_AT}"
    nxt = compute_target(lock, df, FLIP_AT + H1)
    assert nxt.position is not None and (nxt.position.trade_key, nxt.position.signal_ts, nxt.position.direction) == (
        FLIP_AT + H1, FLIP_AT, -1)
    assert (nxt.exit_reason, nxt.exit_queued_ts, nxt.entry) == (None, None, None)
    res, _, _, _ = replay(df)
    assert next(t for t in res.trades if t.entry_ts == LONG_ENTRY).exit_reason == "FLIP"


def test_an_inconsistent_replay_raises_a_target_error(lock, ref, monkeypatch):
    real = target_mod.run_backtest
    signal_bar = ts("2024-03-13 22:00")
    monkeypatch.setattr(target_mod, "run_backtest", lambda *a, **k: dataclasses.replace(real(*a, **k), signals=()))
    with pytest.raises(TargetError, match="signal"):
        compute_target(lock, ref.df, signal_bar)
    monkeypatch.setattr(target_mod, "run_backtest", lambda *a, **k: dataclasses.replace(real(*a, **k), trades=()))
    with pytest.raises(TargetError, match="trade"):
        compute_target(lock, ref.df, signal_bar + 5 * H1)  # the long entered at signal_bar + 1h is open
```

Then add the live target to the static allowlist (Decision D2):

**Edit `tests/paper/test_static.py` — replace:**

```python
ALLOWED_TRUE = {"backtest/holdout.py", "paper/runner.py"}  # Plan 2 spec 7.1
```

**with:**

```python
ALLOWED_TRUE = {"backtest/holdout.py", "paper/runner.py", "live/target.py"}  # Plan 2 spec 7.1, Plan 3 spec 3.1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --group live pytest tests/live/test_target.py tests/paper/test_static.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'ewbot.live.target'` (and, run alone, `tests/paper/test_static.py` fails `test_only_the_holdout_and_the_paper_runner_pass_allow_holdout_true`: the allowlist names a module that does not exist yet).

- [ ] **Step 3: Write minimal implementation**

**File: `src/ewbot/live/target.py`**

```python
"""Target state from the frozen engine (Plan 3 spec 3.1, 4.1 step 2).

Every cycle replays the whole engine - `run_backtest(cfg, df, HOLDOUT_START, None, allow_holdout=True)`
over every cached bar, exactly like the paper runtime - and reads what the engine wants after the
latest closed bar T:
  - `position`: the engine position still open after bar T (its current stop, TPs, TP1 state);
  - `exit_reason` / `exit_queued_ts`: the exit the engine has queued for that position and not executed
    yet (TIME, LINEAGE, BREAKER, FLIP) and the bar at whose close it was queued. Usually that bar is T
    (the engine fills the exit at T+1's open). Nothing fills during the CFM halt (Fri 17-18 New York,
    `futures_closed`), so an exit queued at the Fri 16:00 close is still pending after the halt bar
    T = 17:00 (exit_queued_ts = 16:00 < T) and fills at the 18:00 open;
  - `stop_pending_reopen`: the halt bar T crossed the position's stop - the paper broker marks the stop
    triggered while closed and the engine stops the position out at the next tradable open;
  - `entry`: an entry the engine queued at T's close (it fills at T+1's open in the engine);
  - `decisions`: the engine's decisions with ts <= T in the paper ledger's format (audit, spec 3.5).

Frozen inputs (Plan 3 global constraints): the configuration is built here from the holdout lock -
`holdout_config(BotConfig(), params)` with the lock checked like the paper precheck (`lock_params`) -
and the replay starts at HOLDOUT_START. No caller passes a configuration or a start.

The engine never evaluates entries on the LAST bar of a run (it has no next bar to fill on), so the
frame gets one placeholder bar T+1 (flat at T's close, volume 0, synthetic). Everything decided at or
before T's close is computed before the engine reaches T+1, so it is identical to a replay that has
the real bar T+1 (tests/live/test_target.py proves it bar by bar); nothing at T+1 is used.

Queued exits and halt stops are not in EngineResult or in the paper trace, so the engine's per-run
`_Run` class is swapped (the paper trace's technique: the name is looked up at run time, no file is
edited) for a subclass that records each fresh `_request_exit(reason)` with the bar it happened on and
every bar after which the broker holds a stop triggered during the halt. The swap is process-global
inside the `with` block: never run two replays concurrently.

This module is the live runtime's only caller of allow_holdout=True (tests/paper/test_static.py)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pandas as pd

import ewbot.engine.loop as loop_mod
from ewbot.backtest.holdout import holdout_config, lock_digest
from ewbot.backtest.runner import run_backtest
from ewbot.backtest.walkforward import HOLDOUT_START, grid_combos
from ewbot.config import CONSERVATIVE, BotConfig
from ewbot.paper.decisions import build_decisions
from ewbot.paper.trace import EngineTrace, traced_engine
from ewbot.types import H1
from ewbot.waves.engine import WaveState


class TargetError(RuntimeError):
    """The lock is not the holdout's, the candles do not end at the requested bar, or the replay is
    inconsistent."""


@dataclass(frozen=True, slots=True)
class TargetPosition:
    trade_key: int  # engine entry_ts (the paper ledger's trade key)
    signal_ts: int  # bar whose close queued the entry (= the live trade id)
    setup: str
    direction: int
    entry_price: float  # engine (spot) entry fill
    stop: float  # engine stop after bar T (spot)
    stop0: float
    tp1: float
    tp2: float | None
    tp1_done: bool
    contracts: int  # engine size on the paper equity (informational)
    remaining: int


@dataclass(frozen=True, slots=True)
class TargetEntry:
    signal_ts: int  # = T
    setup: str
    direction: int
    entry_ref: float  # spot close of T
    stop: float
    tp1: float
    tp2: float | None
    risk_mult: float
    contracts: int  # engine size on the paper equity (informational)
    reason: str


@dataclass(frozen=True, slots=True)
class Target:
    bar_ts: int
    close: float
    position: TargetPosition | None
    exit_reason: str | None  # exit queued for `position`, not executed by T (None when flat)
    entry: TargetEntry | None
    decisions: tuple[dict[str, Any], ...]
    exit_queued_ts: int | None = None  # bar whose close queued `exit_reason` (< bar_ts: carried across the halt)
    stop_pending_reopen: bool = False  # the halt bar crossed the stop: the engine stops out at the reopen

    def to_json(self) -> dict[str, Any]:
        def pos(p: Any) -> dict[str, Any] | None:
            return None if p is None else {f: getattr(p, f) for f in p.__slots__}

        return {"bar_ts": self.bar_ts, "close": self.close, "position": pos(self.position),
                "exit_reason": self.exit_reason, "exit_queued_ts": self.exit_queued_ts,
                "stop_pending_reopen": self.stop_pending_reopen, "entry": pos(self.entry)}


def lock_params(lock: Mapping[str, Any]) -> tuple[float, ...]:
    """The params of a completed holdout lock, checked with the paper precheck's semantics
    (paper.runner.precheck_lock) minus its git checks - the image has no git history; preflight pins the
    committed lock's sha256 instead: the params are a walk-forward grid combo, the holdout start is
    HOLDOUT_START and sha256 == lock_digest(BotConfig(), params, random_runs, code_sha256), i.e. the
    configuration built from them is the one the holdout ran with (conservative, breakers on)."""
    try:
        params = tuple(float(x) for x in lock["params"])
        random_runs = int(lock["random_runs"])
        code_sha = str(lock["code_sha256"])
        holdout_start = int(lock["holdout_start"])
        sha = str(lock["sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TargetError(f"not a completed holdout lock: {exc!r}") from exc
    if params not in grid_combos():
        raise TargetError(f"lock params {list(params)} are not a walk-forward grid combo")
    if holdout_start != HOLDOUT_START:
        raise TargetError(f"lock holdout_start {holdout_start} != {HOLDOUT_START}")
    digest = lock_digest(BotConfig(), params, random_runs, code_sha)
    if digest != sha:
        raise TargetError(f"lock sha256 {sha[:12]} != lock_digest(BotConfig(), params, random_runs, code_sha256) "
                          f"{digest[:12]}: not the configuration the holdout ran with")
    return params


def frozen_config(lock: Mapping[str, Any]) -> BotConfig:
    """The holdout's configuration: BotConfig defaults with the lock's params (strategy A, conservative risk,
    circuit breakers on)."""
    cfg = holdout_config(BotConfig(), lock_params(lock))
    if cfg.risk != CONSERVATIVE or not cfg.breakers.enabled:
        raise TargetError("the frozen configuration must be conservative with the circuit breakers on")
    return cfg


def with_placeholder(df1h: pd.DataFrame, bar_ts: int) -> pd.DataFrame:
    """Bars up to and including bar_ts plus the flat placeholder bar bar_ts + 1h."""
    d = df1h[df1h["ts"] <= bar_ts]
    if d.empty or int(d["ts"].iloc[-1]) != int(bar_ts):
        raise TargetError(f"the candles do not contain the bar {bar_ts}")
    close = float(d["close"].iloc[-1])
    row: dict[str, Any] = {"ts": int(bar_ts) + H1, "open": close, "high": close, "low": close, "close": close,
                           "volume": 0.0}
    if "synthetic" in d.columns:
        row["synthetic"] = True
    out = pd.concat([d, pd.DataFrame([row])], ignore_index=True)
    return out.astype({"ts": "int64"})


@contextmanager
def traced_with_exits() -> Iterator[tuple[EngineTrace, list[tuple[int, str]], list[int]]]:
    """paper.trace.traced_engine() plus what the engine queues: (trace, exits [(bar ts, reason)] - each
    fresh exit request with the bar whose close made it -, halt_stops [bar ts] - every bar after which
    the open position's stop has been triggered during the futures halt, PaperBroker's
    `_stop_triggered_while_closed`: it stops out at the next tradable open)."""
    exits: list[tuple[int, str]] = []
    halt_stops: list[int] = []
    with traced_engine() as trace:
        original = loop_mod._Run

        class _ExitRecordingRun(original):  # type: ignore[misc, valid-type]
            def _manage(self, ws: WaveState, closed_4h: bool) -> None:
                b = self.broker  # step 5 runs right after this bar's fills (process_bar)
                if b.position is not None and b._stop_triggered_while_closed and trace.last_ts is not None:
                    halt_stops.append(int(trace.last_ts))
                super()._manage(ws, closed_4h)

            def _request_exit(self, reason: str) -> None:
                ot = self.open
                fresh = ot is not None and self.broker.position is not None and ot.exit_reason is None
                super()._request_exit(reason)
                if fresh and trace.last_ts is not None:
                    exits.append((int(trace.last_ts), reason))

        loop_mod._Run = _ExitRecordingRun  # type: ignore[misc]
        try:
            yield trace, exits, halt_stops
        finally:
            loop_mod._Run = original  # type: ignore[misc]


def compute_target(lock: Mapping[str, Any], df1h: pd.DataFrame, bar_ts: int) -> Target:
    """The engine's target after bar `bar_ts` (module docstring) for the holdout `lock`. Raises TargetError
    when the lock is not the holdout's, the candles do not end at bar_ts or the replay is inconsistent."""
    cfg = frozen_config(lock)
    bar_ts = int(bar_ts)
    frame = with_placeholder(df1h, bar_ts)
    with traced_with_exits() as (trace, exits, halt_stops):
        res = run_backtest(cfg, frame, HOLDOUT_START, None, allow_holdout=True)

    def inconsistent(what: str) -> TargetError:
        return TargetError(f"inconsistent replay at bar {bar_ts}: {what}")

    fills = [f for f in trace.fills if f.ts <= bar_ts]
    left: dict[int, int] = {}
    open_key: int | None = None
    for f in fills:
        if f.kind == "ENTRY":
            left[f.trade_key] = f.contracts
            open_key = f.trade_key
        elif f.trade_key not in left:
            raise inconsistent(f"a {f.kind} fill at {f.ts} for the unknown position {f.trade_key}")
        else:
            left[f.trade_key] -= f.contracts
            if left[f.trade_key] == 0 and f.trade_key == open_key:
                open_key = None

    position: TargetPosition | None = None
    exit_reason: str | None = None
    exit_queued_ts: int | None = None
    if open_key is not None:
        t = next((x for x in res.trades if x.entry_ts == open_key), None)
        if t is None:
            raise inconsistent(f"no engine trade for the position entered at {open_key}")
        orders = [o for o in trace.orders if o.ts < open_key]
        if not orders:
            raise inconsistent(f"no queued entry before the position entered at {open_key}")
        stops = [s for s in trace.stops if s.trade_key == open_key and s.ts <= bar_ts]
        tp1 = sum(f.contracts for f in fills if f.trade_key == open_key and f.kind == "TP1")
        position = TargetPosition(
            trade_key=open_key, signal_ts=orders[-1].ts, setup=t.setup, direction=t.direction,
            entry_price=t.entry_price, stop=stops[-1].new if stops else t.stop0, stop0=t.stop0, tp1=t.tp1,
            tp2=t.tp2, tp1_done=tp1 > 0, contracts=t.contracts, remaining=t.contracts - tp1,
        )
        # the position is still open after T, so an exit queued for it since its entry has not filled yet
        pending = [(ts, r) for ts, r in exits if open_key <= ts <= bar_ts]
        if pending:
            exit_queued_ts, exit_reason = pending[-1]
    stop_pending_reopen = position is not None and bar_ts in halt_stops

    entry: TargetEntry | None = None
    queued = [o for o in trace.orders if o.ts == bar_ts]
    if queued:
        sig = next((s for s in res.signals if s.ts == bar_ts), None)
        if sig is None:
            raise inconsistent(f"an entry queued at {bar_ts} without its engine signal")
        o = queued[-1]
        entry = TargetEntry(
            signal_ts=bar_ts, setup=sig.setup, direction=sig.direction, entry_ref=sig.entry_ref, stop=o.stop,
            tp1=o.tp1, tp2=o.tp2, risk_mult=sig.risk_mult, contracts=o.contracts, reason=sig.reason,
        )

    try:
        decisions = tuple(d for d in build_decisions(res, trace, list(res.trades)) if d["ts"] <= bar_ts)
    except (KeyError, StopIteration) as exc:
        raise inconsistent(f"the decisions cannot be built: {exc!r}") from exc
    close = float(frame["close"].iloc[-1])
    return Target(bar_ts, close, position, exit_reason, entry, decisions, exit_queued_ts, stop_pending_reopen)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run --group live pytest tests/live/test_target.py tests/paper/test_static.py -q
```

Expected: `37 passed` (12 target tests, about 10 s: the equivalence tests replay the synthetic series at every event bar and at a sampled sweep of the other holdout bars; 25 static tests).

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/live/target.py tests/live/test_target.py tests/paper/test_static.py
git commit -m "$(cat <<'EOF'
feat(live): target state from the frozen engine replay (placeholder bar, queued exits)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Price mapping, stage sizing and margin projection

**Files:**
- Create: `src/ewbot/live/pricing.py`
- Test: `tests/live/test_pricing.py`

**Interfaces:**
- Consumes: `ewbot.config` (`CONSERVATIVE`, `BotConfig`, `CostParams`), `ewbot.risk.sizing.size_contracts` (called, not changed), `ewbot.live.exchange` (`STOP_LIMIT_OFFSET`, `ProductSpec`), `ewbot.live.sim.BIP_SPEC` (test).
- Produces (`ewbot.live.pricing`): `CANARY_MAX_RISK = 0.02`, `FULL_CAP_CONTRACTS = 3`, `FULL_CAP_DAYS = 30`, `MARGIN_ENTRY_MAX = 0.70`, `MARGIN_WARN = 0.80`, `MARGIN_REDUCE = 0.90`; `to_tick(price, tick, direction) -> float` (long floor, short ceil), `basis_ratio(fut_mid, spot_mid) -> float`, `map_level(spot_level, ratio, tick, direction) -> float`, frozen `Levels(stop, tp1, tp2)`, `map_levels(direction, stop, tp1, tp2, ratio, tick) -> Levels`, `stop_limit(stop, direction, offset=STOP_LIMIT_OFFSET) -> float`, `stop_gapped(price, stop, direction, offset=STOP_LIMIT_OFFSET) -> bool`, `risk_per_contract(entry, stop, costs) -> float`, `margin_ratio(contracts, price, side, product, equity, overnight=True) -> float`, `max_contracts_for_margin(price, side, product, equity, limit=0.70) -> int`, `stage_contracts(stage, *, equity, entry, stop, direction, risk_mult, cfg, full_cap=None) -> tuple[int, str]` (0 = skip, with the reason; `"bad sizing inputs"` for a direction other than ±1, a non-finite entry/stop/equity/risk_mult or equity <= 0; the reason of an accepted SHADOW/CANARY entry is `"<stage>: 1 contract"`). Fail-closed: `basis_ratio` raises `ValueError` for a non-positive or non-finite mid, `margin_ratio` returns `inf` for a non-finite or non-positive price or a non-finite or non-positive equity, `max_contracts_for_margin` returns 0 for a non-finite or non-positive price/margin or equity, `stage_contracts` raises `ValueError` for a negative `full_cap`.

- [ ] **Step 1: Write the failing test**

**File: `tests/live/test_pricing.py`**

```python
from __future__ import annotations

import dataclasses
import math

import pytest

from ewbot.config import AGGRESSIVE, BotConfig
from ewbot.live.pricing import (
    basis_ratio,
    map_level,
    map_levels,
    margin_ratio,
    max_contracts_for_margin,
    risk_per_contract,
    stage_contracts,
    stop_gapped,
    stop_limit,
    to_tick,
)
from ewbot.live.sim import BIP_SPEC
from ewbot.risk.sizing import size_contracts

CFG = BotConfig()


@pytest.mark.parametrize(
    "price,direction,expected",
    [(84667.3, 1, 84665.0), (84667.3, -1, 84670.0), (84665.0, 1, 84665.0), (84665.0, -1, 84665.0),
     (84669.99, 1, 84665.0), (84665.01, -1, 84670.0),
     (84669.9999999999, 1, 84670.0), (84665.0000000001, -1, 84665.0)],  # float noise stays on the grid
)
def test_to_tick_rounds_longs_down_and_shorts_up(price, direction, expected):
    assert to_tick(price, 5.0, direction) == expected


def test_to_tick_rejects_bad_inputs():
    with pytest.raises(ValueError):
        to_tick(100.0, 0.0, 1)
    with pytest.raises(ValueError):
        to_tick(100.0, 5.0, 0)


def test_mapping_uses_the_basis_ratio_conservatively():
    ratio = basis_ratio(84667.5, 84671.265)
    assert ratio == pytest.approx(0.99995553, rel=1e-8)
    long_ = map_levels(1, stop=82_000.0, tp1=86_000.0, tp2=88_000.0, ratio=ratio, tick=5.0)
    assert (long_.stop, long_.tp1, long_.tp2) == (81995.0, 85995.0, 87995.0)  # all rounded down
    short = map_levels(-1, stop=86_000.0, tp1=82_000.0, tp2=None, ratio=ratio, tick=5.0)
    assert (short.stop, short.tp1, short.tp2) == (86000.0, 82000.0, None)  # rounded up
    assert map_level(100.0, 1.0, 5.0, -1) == 100.0
    with pytest.raises(ValueError):
        basis_ratio(0.0, 1.0)


@pytest.mark.parametrize("fut,spot", [(math.nan, 1.0), (1.0, math.nan), (math.inf, 1.0), (1.0, math.inf), (-math.inf, 1.0)])
def test_basis_ratio_fails_closed_on_non_finite_mids(fut, spot):
    with pytest.raises(ValueError):
        basis_ratio(fut, spot)


def test_stop_limit_and_gap():
    assert stop_limit(80_000.0, 1) == pytest.approx(76_000.0)  # STOP_LIMIT_OFFSET 5%
    assert stop_limit(80_000.0, -1) == pytest.approx(84_000.0)
    assert not stop_gapped(76_100.0, 80_000.0, 1) and stop_gapped(75_900.0, 80_000.0, 1)
    assert not stop_gapped(83_900.0, 80_000.0, -1) and stop_gapped(84_100.0, 80_000.0, -1)


def test_risk_per_contract_matches_the_sizing_distance():
    r = risk_per_contract(60_000.0, 59_000.0, CFG.costs)
    assert r == pytest.approx((1000.0 + 0.0005 * 119_000.0) * 0.01)


def test_canary_and_shadow_trade_one_contract_unless_the_risk_exceeds_two_percent():
    kw = dict(entry=60_000.0, stop=59_000.0, direction=1, risk_mult=1.0, cfg=CFG)
    assert stage_contracts("CANARY", equity=1_000.0, **kw) == (1, "canary: 1 contract")
    assert stage_contracts("SHADOW", equity=1_000.0, **kw) == (1, "shadow: 1 contract")
    n, why = stage_contracts("CANARY", equity=500.0, **kw)  # risk 10.6 > 10.0
    assert n == 0 and "2%" in why
    assert stage_contracts("CANARY", equity=1_000.0, entry=60_000.0, stop=61_000.0, direction=1,
                           risk_mult=1.0, cfg=CFG)[0] == 0


def test_canary_short_side_and_the_exact_two_percent_boundary():
    short = dict(entry=60_000.0, stop=61_000.0, direction=-1, risk_mult=1.0, cfg=CFG)
    assert stage_contracts("CANARY", equity=1_000.0, **short) == (1, "canary: 1 contract")
    n, why = stage_contracts("CANARY", equity=500.0, **short)  # risk 10.605 > 10.0
    assert n == 0 and "2%" in why
    assert stage_contracts("CANARY", equity=1_000.0, entry=60_000.0, stop=59_000.0, direction=-1,
                           risk_mult=1.0, cfg=CFG)[0] == 0  # stop on the wrong side for a short
    exact = dataclasses.replace(CFG, costs=dataclasses.replace(CFG.costs, slippage=0.0))
    kw = dict(entry=60_000.0, stop=59_000.0, direction=1, risk_mult=1.0, cfg=exact)
    assert risk_per_contract(60_000.0, 59_000.0, exact.costs) == 10.0
    assert stage_contracts("CANARY", equity=500.0, **kw)[0] == 1  # exactly 2%: allowed (strictly greater skips)
    assert stage_contracts("CANARY", equity=499.99, **kw)[0] == 0


def test_stage_contracts_fails_closed_on_bad_sizing_inputs():
    good = dict(equity=1_000.0, entry=60_000.0, stop=59_000.0, direction=1, risk_mult=1.0, cfg=CFG)
    nan, inf = math.nan, math.inf
    bad = [dict(direction=2), dict(direction=0), dict(entry=nan), dict(entry=inf), dict(stop=nan), dict(stop=-inf),
           dict(equity=nan), dict(equity=inf), dict(equity=0.0), dict(equity=-5.0),
           dict(risk_mult=nan), dict(risk_mult=inf)]
    for stage in ("SHADOW", "CANARY", "FULL"):
        for override in bad:
            assert stage_contracts(stage, **{**good, **override}) == (0, "bad sizing inputs"), (stage, override)
    assert stage_contracts("CANARY", **good)[0] == 1


def test_full_uses_the_frozen_sizing_with_the_conservative_profile_and_the_cap():
    kw = dict(entry=60_000.0, stop=59_400.0, direction=1, risk_mult=1.0, cfg=CFG)
    expected = size_contracts(20_000.0, 60_000.0, 59_400.0, 1, CFG.risk, 1.0, CFG.costs)
    assert expected > 3
    assert stage_contracts("FULL", equity=20_000.0, **kw) == (expected, f"full: {expected}")
    assert stage_contracts("FULL", equity=20_000.0, full_cap=3, **kw)[0] == 3
    assert stage_contracts("FULL", equity=10.0, **kw)[0] == 0
    aggressive = dataclasses.replace(CFG, risk=AGGRESSIVE)
    assert aggressive.risk != CFG.risk
    assert stage_contracts("FULL", equity=20_000.0, **{**kw, "cfg": aggressive}) == (expected, f"full: {expected}")
    with pytest.raises(ValueError):
        stage_contracts("FULL", equity=20_000.0, full_cap=-1, **kw)
    with pytest.raises(ValueError):
        stage_contracts("PAPER", equity=1.0, **kw)


def test_margin_projection_uses_the_overnight_rate():
    assert margin_ratio(1, 84_000.0, 1, BIP_SPEC, 1_000.0) == pytest.approx(840.0 * 0.21025 / 1_000.0)
    assert margin_ratio(1, 84_000.0, -1, BIP_SPEC, 1_000.0) == pytest.approx(840.0 * 0.256625 / 1_000.0)
    assert margin_ratio(1, 84_000.0, 1, BIP_SPEC, 1_000.0, overnight=False) < margin_ratio(1, 84_000.0, 1, BIP_SPEC, 1_000.0)
    assert margin_ratio(0, 84_000.0, 1, BIP_SPEC, 0.0) == 0.0 and math.isinf(margin_ratio(1, 1.0, 1, BIP_SPEC, 0.0))
    for price, equity in [(math.nan, 1_000.0), (math.inf, 1_000.0), (84_000.0, math.nan), (84_000.0, math.inf),
                          (0.0, 1_000.0), (-84_000.0, 1_000.0)]:
        assert math.isinf(margin_ratio(1, price, 1, BIP_SPEC, equity)), (price, equity)
    for price, equity in [(math.nan, 1_000.0), (84_000.0, math.nan), (84_000.0, math.inf), (math.inf, 1_000.0),
                          (0.0, 1_000.0), (-84_000.0, 1_000.0), (84_000.0, 0.0), (84_000.0, -1.0)]:
        assert max_contracts_for_margin(price, 1, BIP_SPEC, equity) == 0, (price, equity)
    n = max_contracts_for_margin(84_000.0, 1, BIP_SPEC, 1_000.0)
    assert n == 3 and margin_ratio(n, 84_000.0, 1, BIP_SPEC, 1_000.0) <= 0.70 < margin_ratio(n + 1, 84_000.0, 1, BIP_SPEC, 1_000.0)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --group live pytest tests/live/test_pricing.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'ewbot.live.pricing'`.

- [ ] **Step 3: Write minimal implementation**

**File: `src/ewbot/live/pricing.py`**

```python
"""Spot -> futures price mapping, tick rounding, per-stage sizing and margin projection
(Plan 3 spec 3.3, 4.3, 4.5).

Signals, stops and targets are computed on spot BTC-USD candles (the paper runtime's input); orders go
to the BIP future. A level is mapped with the basis ratio at order time (futures mid / spot mid) and
put on the $5 tick conservatively: long levels round DOWN, short levels round UP. For a stop that is
the spec's rule (never triggered earlier than the engine's stop); for a take-profit it moves the
level toward the entry, so the live limit is reached no later than the engine's strict-cross TP.

Sizing: SHADOW and CANARY trade 1 contract and skip a signal whose 1-contract risk exceeds 2% of the
account; FULL calls the frozen risk.sizing.size_contracts with the real equity and the conservative
profile (no strategy file is changed), capped at 3 contracts for the first 30 days of FULL. Margin is
always projected at the OVERNIGHT rate (the stricter one; it applies from 16:00 New York)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ewbot.config import CONSERVATIVE, BotConfig, CostParams
from ewbot.live.exchange import STOP_LIMIT_OFFSET, ProductSpec
from ewbot.risk.sizing import size_contracts

CANARY_MAX_RISK = 0.02  # spec 4.3
FULL_CAP_CONTRACTS = 3  # spec 4.3 / 6.2: first 30 days of FULL
FULL_CAP_DAYS = 30
MARGIN_ENTRY_MAX = 0.70  # spec 4.5: refuse an entry whose overnight projection exceeds 70%
MARGIN_WARN = 0.80
MARGIN_REDUCE = 0.90
_EPS = 1e-9


def to_tick(price: float, tick: float, direction: int) -> float:
    """Long (+1): floor to the tick grid; short (-1): ceil."""
    if tick <= 0 or direction not in (1, -1):
        raise ValueError(f"bad tick {tick} / direction {direction}")
    q = price / tick
    n = math.floor(q + _EPS) if direction > 0 else math.ceil(q - _EPS)
    return float(n) * tick


def basis_ratio(fut_mid: float, spot_mid: float) -> float:
    if not (math.isfinite(fut_mid) and math.isfinite(spot_mid)) or fut_mid <= 0 or spot_mid <= 0:
        raise ValueError(f"bad mids {fut_mid} / {spot_mid}")
    return fut_mid / spot_mid


def map_level(spot_level: float, ratio: float, tick: float, direction: int) -> float:
    return to_tick(spot_level * ratio, tick, direction)


@dataclass(frozen=True, slots=True)
class Levels:
    stop: float
    tp1: float
    tp2: float | None


def map_levels(direction: int, stop: float, tp1: float, tp2: float | None, ratio: float, tick: float) -> Levels:
    return Levels(
        map_level(stop, ratio, tick, direction),
        map_level(tp1, ratio, tick, direction),
        None if tp2 is None else map_level(tp2, ratio, tick, direction),
    )


def stop_limit(stop: float, direction: int, offset: float = STOP_LIMIT_OFFSET) -> float:
    """The limit a triggered stop of a `direction` position rests at (a long sells 5% below)."""
    return stop * (1.0 - direction * offset)


def stop_gapped(price: float, stop: float, direction: int, offset: float = STOP_LIMIT_OFFSET) -> bool:
    """The price is beyond the stop's limit: the triggered stop cannot fill (spec 4.2 stop gap)."""
    return direction * (price - stop_limit(stop, direction, offset)) < 0


def risk_per_contract(entry: float, stop: float, costs: CostParams) -> float:
    """USD lost on one contract stopped out, with both fills slipping (risk.sizing's distance)."""
    return (abs(entry - stop) + costs.slippage * (entry + stop)) * costs.contract_size


def margin_ratio(contracts: int, price: float, side: int, product: ProductSpec, equity: float,
                 overnight: bool = True) -> float:
    if contracts <= 0:
        return 0.0
    if not (math.isfinite(price) and math.isfinite(equity)) or price <= 0 or equity <= 0:
        return math.inf
    return contracts * product.contract_size * price * product.margin_rate(side, overnight) / equity


def max_contracts_for_margin(price: float, side: int, product: ProductSpec, equity: float,
                             limit: float = MARGIN_ENTRY_MAX) -> int:
    per = product.contract_size * price * product.margin_rate(side, True)
    if not (math.isfinite(per) and math.isfinite(equity)) or per <= 0 or equity <= 0:
        return 0
    return max(0, math.floor(limit * equity / per + _EPS))


def stage_contracts(
    stage: str,
    *,
    equity: float,
    entry: float,
    stop: float,
    direction: int,
    risk_mult: float,
    cfg: BotConfig,
    full_cap: int | None = None,
) -> tuple[int, str]:
    """(contracts, reason). 0 contracts = skip the signal (recorded, not traded). Fails closed on a
    non-finite price, equity or `risk_mult`, a bad direction, or a negative `full_cap`."""
    if full_cap is not None and full_cap < 0:
        raise ValueError(f"bad full_cap {full_cap}")
    if direction not in (1, -1) or not all(map(math.isfinite, (entry, stop, equity, risk_mult))) or equity <= 0:
        return 0, "bad sizing inputs"
    if direction * (entry - stop) <= 0:
        return 0, "stop on the wrong side of the entry"
    if stage in ("SHADOW", "CANARY"):
        risk = risk_per_contract(entry, stop, cfg.costs)
        if risk > CANARY_MAX_RISK * equity:
            return 0, f"1-contract risk {risk:.2f} USD > {CANARY_MAX_RISK:.0%} of equity {equity:.2f}"
        return 1, f"{stage.lower()}: 1 contract"
    if stage == "FULL":
        n = size_contracts(equity, entry, stop, direction, CONSERVATIVE, risk_mult, cfg.costs)
        if n == 0:
            return 0, "size_contracts returned 0"
        if full_cap is not None and n > full_cap:
            return full_cap, f"full: {n} capped at {full_cap}"
        return n, f"full: {n}"
    raise ValueError(f"unknown stage {stage}")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run --group live pytest tests/live/test_pricing.py -q
```

Expected: `22 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/live/pricing.py tests/live/test_pricing.py
git commit -m "$(cat <<'EOF'
feat(live): spot-to-futures level mapping, stage sizing and overnight margin projection

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Reconcile planner (target × exchange state → actions)

**Files:**
- Create: `src/ewbot/live/reconcile.py`
- Test: `tests/live/test_reconcile.py`

**Interfaces:**
- Consumes: `ewbot.live.exchange` (`Order`, `Position`), `ewbot.live.pricing` (`map_level`, `to_tick`), `ewbot.live.target` (`Target`, `TargetEntry`, `TargetPosition`).
- Produces (`ewbot.live.reconcile`): `PREFIX = "ewb-"`, `UNPROTECTED_MAX_S = 60.0`, `REDUCE_SETTLE_S = 60.0`; ids `entry_cid(signal_ts, direction)`, `bracket_cid(trade_id, leg, gen)`, `close_cid(trade_id, n)`, `reduce_cid(trade_id, n)`, `is_ours(order) -> bool`; dataclasses `Leg(name, size, limit_price, stop_price, client_order_id="", order_id=None)` and `LiveTrade(trade_id, setup, direction, contracts, tp1_n, entry_price, tp1_price, tp2_price, stop_spot, stop_price, opened_ts, tp1_filled=False, legs=[], gen=0, closes=0, protected_ts=None, unprotected_since=None, incident_unprotected=False, status="OPEN", exit_reason=None, pending_reduce=None)` with `to_json()` / `from_json(dict)` (`contracts` / `tp1_n` are the trade's size and its TP1 portion: `observe` folds a partial fill or an unexplained reduction into them; `pending_reduce` is a reduce the executor recorded before sending it: `{"cid", "size", "reason", "before" (position contracts when sent), "ts", "after" (the trade's `contracts`, `tp1_n`, `tp1_filled` once done), "filled" (None while the outcome is unknown)}`), `expected_contracts(trade) -> int` (`contracts` before the live TP1, `contracts - tp1_n` after it), `after_reduce(trade, size, reason, tp1_n=None) -> dict` (the `after` fields: `TP1_SYNC` marks the live TP1 done, `EXCESS` changes nothing, another reason (`MARGIN`) makes the trade smaller, `tp1_n` its new TP1 portion); frozen actions `Close(reason)`, `Reduce(size, reason)` (`TP1_SYNC`, `EXCESS`; the runtime adds `MARGIN`), `Protect(legs: tuple[Leg, ...], stop_spot, stop_price)`, `Enter(entry: TargetEntry, contracts)`, `Skip(signal_ts, reason)`, `Cancel(order_ids: tuple, reason)`, `Adopt(target: TargetPosition, contracts)`, `Alert(kind, detail)` (`FOREIGN_ORDERS`, `UNKNOWN_POSITION`, `EXCESS_POSITION`, `DIRECTION_MISMATCH`) and `Action` (their union); frozen `PlanContext(now, ratio, tick, session_open=True, entry_contracts=0, entry_block=None, max_contracts=None)` (`max_contracts`: the stage maximum for a position taken over, None = no cap, < 1 raises ValueError); `observe(trade, position, orders, now) -> (LiveTrade | None, list[(event kind, data)])` (events `CLOSED`, `REDUCE_SETTLED` (`trade_id, reason, size, position`: a recorded reduce the position now shows - ours whatever its answer was, its `after` applied), `REDUCE_NOT_SEEN` (`trade_id, reason, filled, position`: reported filled but not in the position after `REDUCE_SETTLE_S` - the position wins), `TP1_FILLED`, `PARTIAL_FILL`, `UNEXPLAINED_REDUCTION` (`trade_id, expected, position`), `UNPROTECTED`, `UNPROTECTED_60S`, `PROTECTED`; a reduce reported filled that the position does not show yet counts as done for folds and protection; a reduction is ours only when our resting legs still cover exactly the position - `TP1_FILLED` when the TP1 leg is used up, else `PARTIAL_FILL` (the legs keep their remainders); anything else is `UNEXPLAINED_REDUCTION`: the trade shrinks to the position, the stop stays (no break-even); `PROTECTED` clears `incident_unprotected`), `desired_legs(trade, contracts, stop_spot, ratio, tick) -> (list[Leg], futures stop)` (ValueError when a TP2 leg is needed and the trade has no TP2), `legs_match(trade, orders, desired) -> bool`, `pending_position(entry) -> TargetPosition`, `engine_position(target) -> TargetPosition | None` (none while an exit is queued or the halt bar crossed the stop), `plan(target, trade, position, orders, ctx) -> list[Action]` (pure; never mutates its inputs; an exit carried across the CFM halt closes like one queued at the last close, `stop_pending_reopen` closes with reason `STOP_HALT`; the executor sends a close only while the session is OPEN, and until then missing legs are placed at the stop in force - a position on the other side of the trade cannot be protected by its legs (`Alert DIRECTION_MISMATCH`); more contracts than `expected_contracts` -> `Alert EXCESS_POSITION` and, while OPEN and no reduce is pending, `Reduce(excess, "EXCESS")` with legs for the expected size - otherwise the legs cover the whole position (brackets only reduce); legs never cover more than the position; no reduce at all while `pending_reduce` is set (until its outcome is known the trade is planned as it was, once reported filled as it will be - the executor resolves it by its id); `Reduce TP1_SYNC` only against a resting TP1 leg, sized `min(tp1_n, what that leg has left)`, at most one reduce per pass; an unknown position is adopted up to `ctx.max_contracts` (`Adopt(target, n)` then `Reduce EXCESS` for the rest); without quotes a moved engine stop is not recorded, so the move is planned again once quotes are back; the signal of the journal's trade (open or closed) is never entered again).

- [ ] **Step 1: Write the failing test**

**File: `tests/live/test_reconcile.py`**

```python
from __future__ import annotations

import dataclasses

import pytest

from ewbot.live.exchange import Order, Position
from ewbot.live.reconcile import (
    REDUCE_SETTLE_S,
    Adopt,
    Alert,
    Cancel,
    Close,
    Enter,
    Leg,
    LiveTrade,
    PlanContext,
    Protect,
    Reduce,
    Skip,
    after_reduce,
    bracket_cid,
    close_cid,
    desired_legs,
    engine_position,
    entry_cid,
    expected_contracts,
    observe,
    plan,
    reduce_cid,
)
from ewbot.live.target import Target, TargetEntry, TargetPosition

T = 1_790_942_400  # a bar open (signal bar of the trade below)
CTX = PlanContext(now=T + 3630.0, ratio=1.0, tick=5.0, entry_contracts=1)


def tpos(**kw) -> TargetPosition:
    base = dict(trade_key=T + 3600, signal_ts=T, setup="W3", direction=1, entry_price=60_010.0, stop=59_000.0,
                stop0=59_000.0, tp1=61_000.0, tp2=62_000.0, tp1_done=False, contracts=5, remaining=5)
    return TargetPosition(**{**base, **kw})


def tentry(**kw) -> TargetEntry:
    base = dict(signal_ts=T + 7200, setup="C_END", direction=-1, entry_ref=60_000.0, stop=61_000.0, tp1=58_000.0,
                tp2=57_000.0, risk_mult=1.0, contracts=4, reason="r")
    return TargetEntry(**{**base, **kw})


def target(position=None, entry=None, exit_reason=None, bar=T + 3600, **kw) -> Target:
    return Target(bar, 60_000.0, position, exit_reason, entry, (), **kw)


def trade(**kw) -> LiveTrade:
    base = dict(trade_id=T, setup="W3", direction=1, contracts=3, tp1_n=1, entry_price=60_020.0,
                tp1_price=61_000.0, tp2_price=62_000.0, stop_spot=59_000.0, stop_price=59_000.0, opened_ts=T + 3605.0,
                legs=[Leg("tp1", 1, 61_000.0, 59_000.0, bracket_cid(T, "tp1", 1), "o1"),
                      Leg("tp2", 2, 62_000.0, 59_000.0, bracket_cid(T, "tp2", 2), "o2")], gen=2)
    return LiveTrade(**{**base, **kw})


def resting(tr: LiveTrade, **over) -> list[Order]:
    out = []
    for leg in tr.legs:
        o = Order(leg.order_id, leg.client_order_id, -tr.direction, "BRACKET", leg.size, 0, "OPEN",
                  leg.limit_price, leg.stop_price)
        out.append(dataclasses.replace(o, **over.get(leg.name, {})))
    return out


def test_client_order_ids_are_deterministic():
    assert entry_cid(T, 1) == f"ewb-e-{T}-L" and entry_cid(T, -1) == f"ewb-e-{T}-S"
    assert bracket_cid(T, "tp2", 3) == f"ewb-b-{T}-tp2-3"
    assert close_cid(T, 1) == f"ewb-x-{T}-1" and reduce_cid(T, 2) == f"ewb-r-{T}-2"


def test_trade_state_round_trips_through_json():
    tr = trade()
    assert LiveTrade.from_json(tr.to_json()) == tr


def test_flat_and_idle_needs_nothing_but_cleans_leftover_orders():
    assert plan(target(), None, None, [], CTX) == []
    leftover = resting(trade())
    assert plan(target(), None, None, leftover, CTX) == [Cancel(("o1", "o2"), "FLAT")]


def test_flat_with_a_queued_entry_enters_or_records_why_not():
    e = tentry()
    assert plan(target(entry=e), None, None, [], CTX) == [Enter(e, 1)]
    blocked = dataclasses.replace(CTX, entry_block="paused")
    assert plan(target(entry=e), None, None, [], blocked) == [Skip(e.signal_ts, "paused")]
    no_tp2 = tentry(tp2=None)
    assert isinstance(plan(target(entry=no_tp2), None, None, [], CTX)[0], Skip)


def test_an_unknown_position_is_adopted_or_closed():
    pos = Position(1, 2, 60_000.0)
    assert isinstance(plan(None, None, pos, [], CTX)[0], Alert)
    p = tpos()
    assert plan(target(position=p), None, pos, [], CTX) == [Adopt(p, 2)]
    assert plan(target(position=tpos(direction=-1)), None, pos, [], CTX) == [Close("UNKNOWN_POSITION")]
    assert plan(target(), None, pos, [], CTX) == [Close("UNKNOWN_POSITION")]


def test_matching_brackets_need_nothing():
    tr = trade()
    assert plan(target(position=tpos()), tr, Position(1, 3, 60_020.0), resting(tr), CTX) == []
    assert plan(None, tr, Position(1, 3, 60_020.0), resting(tr), CTX) == []  # no target: keep protecting


def test_a_moved_engine_stop_replaces_the_legs_with_the_mapped_stop():
    tr = trade()
    ctx = dataclasses.replace(CTX, ratio=1.0005)
    acts = plan(target(position=tpos(stop=59_500.0)), tr, Position(1, 3, 60_020.0), resting(tr), ctx)
    assert len(acts) == 1 and isinstance(acts[0], Protect)
    p = acts[0]
    assert p.stop_spot == 59_500.0 and p.stop_price == 59_525.0  # 59_529.75 floored to the $5 tick
    assert [(leg.name, leg.size, leg.limit_price, leg.stop_price) for leg in p.legs] == [
        ("tp1", 1, 61_000.0, 59_525.0), ("tp2", 2, 62_000.0, 59_525.0)]


def test_missing_or_partly_filled_legs_are_replaced():
    tr = trade()
    only_tp2 = [o for o in resting(tr) if o.order_id == "o2"]
    assert isinstance(plan(target(position=tpos()), tr, Position(1, 3, 60_020.0), only_tp2, CTX)[0], Protect)
    part = resting(tr, tp2={"filled": 1})
    assert isinstance(plan(target(position=tpos()), tr, Position(1, 3, 60_020.0), part, CTX)[0], Protect)


def test_within_the_entry_hour_the_queued_engine_entry_is_the_position():
    tr = trade()
    e = tentry(signal_ts=T, direction=1, stop=59_000.0, tp1=61_000.0, tp2=62_000.0)
    pos = Position(1, 3, 60_020.0)
    assert plan(target(entry=e), tr, pos, resting(tr), CTX) == []  # no close: the engine fills it at this open
    moved = tentry(signal_ts=T, direction=1, stop=59_500.0, tp1=61_000.0, tp2=62_000.0)
    assert isinstance(plan(target(entry=moved), tr, pos, resting(tr), CTX)[0], Protect)
    adopted = plan(target(entry=e), None, pos, [], CTX)
    assert len(adopted) == 1 and isinstance(adopted[0], Adopt) and adopted[0].target.signal_ts == T


def test_engine_exit_closes_and_a_flip_enters_after_the_close():
    tr = trade()
    pos = Position(1, 3, 60_020.0)
    assert plan(target(), tr, pos, resting(tr), CTX) == [Close("ENGINE_FLAT")]
    assert plan(target(position=tpos(), exit_reason="TIME"), tr, pos, resting(tr), CTX) == [Close("TIME")]
    e = tentry()
    assert plan(target(position=tpos(), exit_reason="FLIP", entry=e), tr, pos, resting(tr), CTX) == [Close("FLIP"), Enter(e, 1)]
    other = tpos(signal_ts=T + 7200, trade_key=T + 10800)
    assert plan(target(position=other), tr, pos, resting(tr), CTX) == [Close("ENGINE_OTHER_TRADE")]
    assert plan(target(position=tpos()), tr, Position(-1, 3, 60_020.0), [], CTX) == [Close("DIRECTION_MISMATCH")]


def test_an_exit_carried_across_the_halt_or_a_stop_crossed_in_it_closes():
    """At the CFM halt bar the engine still holds the position: an exit queued at the bar before the halt
    is still queued (exit_queued_ts < bar), and a stop the halt bar crossed stops it out at the reopen. Both
    close the live trade (the executor waits for an OPEN session); neither position is adopted."""
    tr = trade()
    pos = Position(1, 3, 60_020.0)
    halt = T + 7200
    carried = target(position=tpos(), exit_reason="LINEAGE", bar=halt, exit_queued_ts=halt - 3600)
    assert plan(carried, tr, pos, resting(tr), CTX) == [Close("LINEAGE")]
    assert plan(carried, tr, pos, resting(tr), dataclasses.replace(CTX, session_open=False)) == [Close("LINEAGE")]
    stopped = target(position=tpos(), bar=halt, stop_pending_reopen=True)
    assert plan(stopped, tr, pos, resting(tr), CTX) == [Close("STOP_HALT")]
    both = target(position=tpos(), exit_reason="BREAKER", bar=halt, exit_queued_ts=halt - 3600, stop_pending_reopen=True)
    assert plan(both, tr, pos, resting(tr), CTX) == [Close("BREAKER")]  # the queued exit fills first in the engine
    assert engine_position(carried) is None and engine_position(stopped) is None
    assert plan(stopped, None, pos, [], CTX) == [Close("UNKNOWN_POSITION")]
    other = tpos(signal_ts=T + 7200, trade_key=T + 10800)
    assert plan(target(position=other, bar=halt, stop_pending_reopen=True), tr, pos, resting(tr), CTX) == [
        Close("ENGINE_OTHER_TRADE")]


def test_engine_tp1_without_the_live_tp1_reduces_at_market_then_protects_the_rest_at_break_even():
    tr = trade()
    acts = plan(target(position=tpos(tp1_done=True, stop=60_010.0)), tr, Position(1, 3, 60_020.0), resting(tr), CTX)
    assert acts[0] == Reduce(1, "TP1_SYNC")
    p = acts[1]
    assert isinstance(p, Protect) and [(leg.name, leg.size) for leg in p.legs] == [("tp2", 2)]
    assert p.stop_price == 60_020.0  # max(engine break-even 60_010, live entry 60_020)
    closed = dataclasses.replace(CTX, session_open=False)  # no market order now: keep both legs until the reopen
    assert plan(target(position=tpos(tp1_done=True, stop=59_000.0)), tr, Position(1, 3, 60_020.0), resting(tr), closed) == []


def test_after_the_live_tp1_the_single_leg_stops_at_break_even():
    tr = trade(tp1_filled=True)
    legs, stop = desired_legs(tr, 2, 59_000.0, 1.0, 5.0)
    assert stop == 60_020.0 and [(leg.name, leg.size, leg.limit_price) for leg in legs] == [("tp2", 2, 62_000.0)]
    short = trade(direction=-1, entry_price=60_002.0, stop_price=61_000.0, stop_spot=61_000.0, tp1_filled=True)
    assert desired_legs(short, 2, 61_000.0, 1.0, 5.0)[1] == 60_005.0  # short break-even rounds up


def test_one_contract_trades_use_one_tp1_bracket():
    tr = trade(contracts=1, tp1_n=1, legs=[Leg("tp1", 1, 61_000.0, 59_000.0, bracket_cid(T, "tp1", 1), "o1")])
    legs, _ = desired_legs(tr, 1, 59_000.0, 1.0, 5.0)
    assert [(leg.name, leg.size, leg.limit_price) for leg in legs] == [("tp1", 1, 61_000.0)]
    assert plan(target(position=tpos()), tr, Position(1, 1, 60_020.0), resting(tr), CTX) == []


def test_foreign_orders_raise_an_alert():
    tr = trade()
    foreign = Order("f1", "manual-1", -1, "OTHER", 1, 0, "OPEN")
    acts = plan(target(position=tpos()), tr, Position(1, 3, 60_020.0), resting(tr) + [foreign], CTX)
    assert acts == [Alert("FOREIGN_ORDERS", "f1")]


def test_observe_detects_close_tp1_and_protection_gaps():
    tr = trade()
    t2, ev = observe(tr, None, [], now=100.0)
    assert t2.status == "CLOSED" and t2.exit_reason == "BRACKET" and ev[0][0] == "CLOSED"
    t3, ev = observe(tr, Position(1, 2, 60_020.0), resting(tr)[1:], now=100.0)
    assert t3.tp1_filled and [k for k, _ in ev] == ["TP1_FILLED"]
    t4, ev = observe(tr, Position(1, 3, 60_020.0), resting(tr)[1:], now=100.0)
    assert t4.unprotected_since == 100.0 and [k for k, _ in ev] == ["UNPROTECTED"]
    t5, ev = observe(t4, Position(1, 3, 60_020.0), resting(tr)[1:], now=161.0)
    assert t5.incident_unprotected and [k for k, _ in ev] == ["UNPROTECTED_60S"]
    t6, ev = observe(t5, Position(1, 3, 60_020.0), resting(tr), now=170.0)
    assert t6.unprotected_since is None and ev == [("PROTECTED", {"trade_id": T, "after_s": 70.0})]
    assert not t6.incident_unprotected  # PROTECTED ends the incident: a later gap is a new one
    t7, _ = observe(t6, Position(1, 3, 60_020.0), resting(tr)[1:], now=200.0)
    t8, ev = observe(t7, Position(1, 3, 60_020.0), resting(tr)[1:], now=261.0)
    assert t8.incident_unprotected and [k for k, _ in ev] == ["UNPROTECTED_60S"]
    assert observe(None, None, [], 1.0) == (None, [])
    assert tr.status == "OPEN" and not tr.tp1_filled  # the input is not modified


def five() -> LiveTrade:
    """A 5-contract trade: a 2-contract TP1 leg (o1) and a 3-contract TP2 leg (o2)."""
    return trade(contracts=5, tp1_n=2, legs=[Leg("tp1", 2, 61_000.0, 59_000.0, bracket_cid(T, "tp1", 1), "o1"),
                                             Leg("tp2", 3, 62_000.0, 59_000.0, bracket_cid(T, "tp2", 2), "o2")])


def after_tp1() -> LiveTrade:
    """trade() after its live TP1: the 2-contract TP2 leg (o3) at break-even."""
    return trade(tp1_filled=True, stop_price=60_020.0,
                 legs=[Leg("tp2", 2, 62_000.0, 60_020.0, bracket_cid(T, "tp2", 3), "o3")])


def test_tp1_is_filled_only_when_the_tp1_leg_explains_the_reduction():
    tr = trade()
    done, ev = observe(tr, Position(1, 2, 60_020.0), resting(tr, tp1={"filled": 1}), now=100.0)  # filled, not yet gone
    assert done.tp1_filled and [k for k, _ in ev] == ["TP1_FILLED"] and expected_contracts(done) == 2
    # one contract less while the TP1 leg still rests unfilled: not the TP1 (no break-even)
    t2, ev = observe(tr, Position(1, 2, 60_020.0), resting(tr), now=100.0)
    assert not t2.tp1_filled and ev == [("UNEXPLAINED_REDUCTION", {"trade_id": T, "expected": 3, "position": 2})]
    assert (t2.contracts, t2.tp1_n, t2.stop_price) == (2, 1, 59_000.0) and expected_contracts(t2) == 2
    acts = plan(target(position=tpos()), t2, Position(1, 2, 60_020.0), resting(tr), CTX)  # protect what is left
    assert len(acts) == 1 and isinstance(acts[0], Protect)
    assert [(leg.name, leg.size, leg.stop_price) for leg in acts[0].legs] == [
        ("tp1", 1, 59_000.0), ("tp2", 1, 59_000.0)]
    assert observe(t2, Position(1, 2, 60_020.0), resting(tr), now=110.0)[1] == []  # reported once
    # down to one contract: a one-contract trade (TP1 + stop)
    t3, ev = observe(tr, Position(1, 1, 60_020.0), resting(tr), now=100.0)
    assert (t3.contracts, t3.tp1_n) == (1, 1) and ev[0][0] == "UNEXPLAINED_REDUCTION"
    assert [(leg.name, leg.size) for leg in desired_legs(t3, 1, 59_000.0, 1.0, 5.0)[0]] == [("tp1", 1)]


def test_a_partial_tp1_keeps_the_tp1_leg_for_the_rest():
    tr = five()
    part = resting(tr, tp1={"filled": 1})
    t2, ev = observe(tr, Position(1, 4, 60_020.0), part, now=100.0)
    assert not t2.tp1_filled and (t2.contracts, t2.tp1_n) == (4, 1)
    assert ev == [("PARTIAL_FILL", {"trade_id": T, "contracts": 4})]
    assert plan(target(position=tpos()), t2, Position(1, 4, 60_020.0), part, CTX) == []  # the TP1 leg keeps 1
    moved = plan(target(position=tpos(stop=59_500.0)), t2, Position(1, 4, 60_020.0), part, CTX)
    assert [(leg.name, leg.size) for leg in moved[0].legs] == [("tp1", 1), ("tp2", 3)]  # replaced for the rest only
    t3, ev = observe(t2, Position(1, 3, 60_020.0), part[1:], now=110.0)  # the rest of the TP1 leg filled
    assert t3.tp1_filled and [k for k, _ in ev] == ["TP1_FILLED"] and expected_contracts(t3) == 3
    sync = plan(target(position=tpos(tp1_done=True, stop=60_010.0)), t2, Position(1, 4, 60_020.0), part, CTX)
    assert sync[0] == Reduce(1, "TP1_SYNC")  # the engine's TP1: only what is left of the live TP1 leg


def test_tp1_and_a_tp2_fill_in_one_interval_and_reductions_after_tp1():
    tr = five()
    t2, ev = observe(tr, Position(1, 2, 60_020.0), resting(tr, tp2={"filled": 1})[1:], now=100.0)
    assert t2.tp1_filled and [k for k, _ in ev] == ["TP1_FILLED"] and expected_contracts(t2) == 2
    after = after_tp1()
    t3, ev = observe(after, Position(1, 1, 60_020.0), resting(after, tp2={"filled": 1}), now=100.0)
    assert ev == [("PARTIAL_FILL", {"trade_id": T, "contracts": 1})] and expected_contracts(t3) == 1
    t4, ev = observe(after, Position(1, 1, 60_020.0), resting(after), now=100.0)
    assert ev[0] == ("UNEXPLAINED_REDUCTION", {"trade_id": T, "expected": 2, "position": 1})
    assert t4.tp1_filled and expected_contracts(t4) == 1 and t4.stop_price == 60_020.0


def test_excess_contracts_are_reduced_or_protected_until_they_can_be():
    tr = trade()  # 3 contracts
    pos = Position(1, 5, 60_020.0)
    alert = Alert("EXCESS_POSITION", f"trade {T}: position 5 > 3 expected")
    assert plan(target(position=tpos()), tr, pos, resting(tr), CTX) == [alert, Reduce(2, "EXCESS")]
    closed = dataclasses.replace(CTX, session_open=False)  # no market order now: the legs cover all 5 until then
    acts = plan(target(position=tpos()), tr, pos, resting(tr), closed)
    assert acts[0] == alert and len(acts) == 2 and isinstance(acts[1], Protect)
    assert [(leg.name, leg.size, leg.stop_price) for leg in acts[1].legs] == [("tp1", 1, 59_000.0), ("tp2", 4, 59_000.0)]
    acts = plan(None, tr, pos, [], CTX)  # without a target too; the missing legs are placed for 3, never 5
    assert acts[:2] == [alert, Reduce(2, "EXCESS")] and isinstance(acts[2], Protect)
    assert [(leg.name, leg.size) for leg in acts[2].legs] == [("tp1", 1), ("tp2", 2)]
    after = after_tp1()
    acts = plan(target(position=tpos(tp1_done=True)), after, Position(1, 3, 60_020.0), resting(after), CTX)
    assert acts == [Alert("EXCESS_POSITION", f"trade {T}: position 3 > 2 expected"), Reduce(1, "EXCESS")]


def test_an_adopted_position_is_capped_at_the_stage_maximum():
    pos = Position(1, 3, 60_000.0)
    p = tpos()
    canary = dataclasses.replace(CTX, max_contracts=1)
    alert = Alert("EXCESS_POSITION", "position 3 > stage maximum 1: adopting 1")
    assert plan(target(position=p), None, pos, [], canary) == [alert, Adopt(p, 1), Reduce(2, "EXCESS")]
    closed = dataclasses.replace(canary, session_open=False)
    assert plan(target(position=p), None, pos, [], closed) == [alert, Adopt(p, 1)]
    assert plan(target(position=p), None, pos, [], dataclasses.replace(CTX, max_contracts=3)) == [Adopt(p, 3)]
    with pytest.raises(ValueError):
        dataclasses.replace(CTX, max_contracts=0)


def test_a_stop_move_without_quotes_waits_for_them():
    tr = trade()
    pos = Position(1, 3, 60_020.0)
    noq = dataclasses.replace(CTX, ratio=None)
    moved = target(position=tpos(stop=59_500.0))
    assert plan(moved, tr, pos, resting(tr), noq) == []  # the engine stop (spot) is not recorded as moved
    acts = plan(moved, tr, pos, resting(tr)[1:], noq)  # a missing leg is re-placed at the stop in force
    assert len(acts) == 1 and (acts[0].stop_spot, acts[0].stop_price) == (59_000.0, 59_000.0)
    acts = plan(moved, tr, pos, resting(tr), dataclasses.replace(CTX, ratio=1.0005))  # quotes are back: the move
    assert len(acts) == 1 and (acts[0].stop_spot, acts[0].stop_price) == (59_500.0, 59_525.0)


def test_a_signal_already_traded_is_never_entered_again():
    e = tentry(signal_ts=T, direction=1, stop=59_000.0, tp1=61_000.0, tp2=62_000.0)
    closed_early = trade(status="CLOSED", exit_reason="BRACKET")  # stopped out within its entry hour
    assert plan(target(entry=e), closed_early, None, [], CTX) == []
    assert plan(target(entry=e), trade(), None, [], CTX) == []
    assert plan(target(entry=tentry()), closed_early, None, [], CTX) == [Enter(tentry(), 1)]  # another signal


def test_a_close_waiting_for_the_session_keeps_the_position_protected():
    tr = trade()
    pos = Position(1, 3, 60_020.0)
    closed = dataclasses.replace(CTX, session_open=False)
    halt = T + 7200
    carried = target(position=tpos(stop=59_500.0), exit_reason="LINEAGE", bar=halt, exit_queued_ts=halt - 3600)
    assert plan(carried, tr, pos, resting(tr), closed) == [Close("LINEAGE")]  # the legs rest: kept
    acts = plan(carried, tr, pos, resting(tr)[1:], closed)
    assert acts[0] == Close("LINEAGE") and isinstance(acts[1], Protect) and len(acts) == 2
    assert (acts[1].stop_spot, acts[1].stop_price) == (59_000.0, 59_000.0)  # the stop in force, not moved
    assert [(leg.name, leg.size) for leg in acts[1].legs] == [("tp1", 1), ("tp2", 2)]
    assert plan(carried, tr, pos, resting(tr)[1:], CTX) == [Close("LINEAGE")]  # open: the close goes out now
    assert plan(target(), tr, pos, [], closed)[1:] == [acts[1]]  # ENGINE_FLAT as well
    wrong = plan(target(position=tpos()), tr, Position(-1, 3, 60_020.0), [], closed)
    assert wrong[0] == Close("DIRECTION_MISMATCH") and isinstance(wrong[1], Alert) and len(wrong) == 2


def test_a_tp2_leg_without_a_tp2_price_is_refused():
    with pytest.raises(ValueError, match="TP2"):
        desired_legs(trade(tp2_price=None), 3, 59_000.0, 1.0, 5.0)
    assert desired_legs(trade(tp2_price=None, contracts=1, tp1_n=1), 1, 59_000.0, 1.0, 5.0)[0][0].name == "tp1"


def short(**kw) -> LiveTrade:
    """trade() mirrored: a 3-contract short at 60_002, stop 61_000, TP1 59_000 (o1, 1) and TP2 58_000 (o2, 2)."""
    base = dict(direction=-1, entry_price=60_002.0, tp1_price=59_000.0, tp2_price=58_000.0, stop_spot=61_000.0,
                stop_price=61_000.0, legs=[Leg("tp1", 1, 59_000.0, 61_000.0, bracket_cid(T, "tp1", 1), "o1"),
                                          Leg("tp2", 2, 58_000.0, 61_000.0, bracket_cid(T, "tp2", 2), "o2")])
    return trade(**{**base, **kw})


def spos(**kw) -> TargetPosition:
    base = dict(direction=-1, entry_price=60_000.0, stop=61_000.0, stop0=61_000.0, tp1=59_000.0, tp2=58_000.0)
    return tpos(**{**base, **kw})


def test_short_trades_protect_reduce_and_fold_like_longs():
    tr = short()
    pos = Position(-1, 3, 60_002.0)
    assert plan(target(position=spos()), tr, pos, resting(tr), CTX) == []  # buy-side brackets above / below
    moved = plan(target(position=spos(stop=60_500.0)), tr, pos, resting(tr), dataclasses.replace(CTX, ratio=1.0005))
    assert [(leg.name, leg.size, leg.limit_price, leg.stop_price) for leg in moved[0].legs] == [
        ("tp1", 1, 59_000.0, 60_535.0), ("tp2", 2, 58_000.0, 60_535.0)]  # 60_530.25 rounded up for a short
    big = Position(-1, 5, 60_002.0)
    alert = Alert("EXCESS_POSITION", f"trade {T}: position 5 > 3 expected")
    assert plan(target(position=spos()), tr, big, resting(tr), CTX) == [alert, Reduce(2, "EXCESS")]
    closed = plan(target(position=spos()), tr, big, resting(tr), dataclasses.replace(CTX, session_open=False))
    assert closed[0] == alert and [(leg.name, leg.size, leg.stop_price) for leg in closed[1].legs] == [
        ("tp1", 1, 61_000.0), ("tp2", 4, 61_000.0)]
    sync = plan(target(position=spos(tp1_done=True)), tr, pos, resting(tr), CTX)
    assert sync[0] == Reduce(1, "TP1_SYNC") and sync[1].stop_price == 60_005.0  # break-even rounds up for a short
    t2, ev = observe(tr, Position(-1, 2, 60_002.0), resting(tr)[1:], now=100.0)  # the TP1 leg bought back 1
    assert t2.tp1_filled and [k for k, _ in ev] == ["TP1_FILLED"]
    acts = plan(target(position=spos(tp1_done=True)), t2, Position(-1, 2, 60_002.0), resting(tr)[1:], CTX)
    assert [(leg.name, leg.size, leg.limit_price, leg.stop_price) for leg in acts[0].legs] == [
        ("tp2", 2, 58_000.0, 60_005.0)]
    t3, ev = observe(tr, Position(-1, 2, 60_002.0), resting(tr), now=100.0)  # bought back by hand: not ours
    assert ev[0] == ("UNEXPLAINED_REDUCTION", {"trade_id": T, "expected": 3, "position": 2})
    assert not t3.tp1_filled and (t3.contracts, t3.tp1_n, t3.stop_price) == (2, 1, 61_000.0)


def tp1_sync_pending(tr: LiveTrade, filled: int | None = None) -> LiveTrade:
    """`tr` once the executor has recorded Reduce(1, TP1_SYNC) against a 3-contract position (before sending it)."""
    pr = {"cid": reduce_cid(T, 1), "size": 1, "reason": "TP1_SYNC", "before": 3, "ts": 100.0,
          "after": after_reduce(tr, 1, "TP1_SYNC"), "filled": filled}
    return dataclasses.replace(tr, pending_reduce=pr)


def test_a_reduce_whose_answer_was_lost_is_ours_once_the_position_shows_it():
    """Probe (a): the TP1_SYNC reduce came back ambiguous (PENDING) but filled. It is ours - never an unexplained
    reduction - and no other reduce is planned while it is pending (the executor resolves it by its id first)."""
    tr = tp1_sync_pending(trade())
    eng = target(position=tpos(tp1_done=True))
    assert plan(eng, tr, Position(1, 3, 60_020.0), resting(tr), CTX) == []  # not in the position yet: both legs stay
    acts = plan(eng, tr, Position(1, 4, 60_020.0), resting(tr), CTX)
    assert acts[0].kind == "EXCESS_POSITION" and not [a for a in acts if isinstance(a, Reduce)]
    seen, ev = observe(tr, Position(1, 2, 60_020.0), resting(tr), now=110.0)  # it filled
    assert ev == [("REDUCE_SETTLED", {"trade_id": T, "reason": "TP1_SYNC", "size": 1, "position": 2})]
    assert seen.tp1_filled and seen.pending_reduce is None and expected_contracts(seen) == 2
    acts = plan(eng, seen, Position(1, 2, 60_020.0), resting(tr), CTX)
    assert len(acts) == 1 and isinstance(acts[0], Protect)  # the rest at break-even, never a second TP1 sale
    assert [(leg.name, leg.size, leg.stop_price) for leg in acts[0].legs] == [("tp2", 2, 60_020.0)]
    # the reduce and a TP2 fill in one interval: the reduce first, then the legs explain the rest
    seen, ev = observe(tr, Position(1, 1, 60_020.0), resting(tr, tp2={"filled": 1}), now=110.0)
    assert [k for k, _ in ev] == ["REDUCE_SETTLED", "PARTIAL_FILL"] and expected_contracts(seen) == 1
    assert observe(tr, Position(1, 3, 60_020.0), resting(tr), now=900.0)[0].pending_reduce is not None  # unknown: kept


def test_a_filled_reduce_the_position_does_not_show_yet_is_not_an_excess():
    """The venue reported the reduce filled (the executor applied it) but the position read lags: no EXCESS, no
    gap in protection. A minute later the position wins (e.g. contracts added by hand meanwhile)."""
    tr = tp1_sync_pending(trade(tp1_filled=True, stop_price=60_020.0,
                                legs=[Leg("tp2", 2, 62_000.0, 60_020.0, bracket_cid(T, "tp2", 3), "o3")]), filled=1)
    lagging = Position(1, 3, 60_020.0)
    eng = target(position=tpos(tp1_done=True))
    assert plan(eng, tr, lagging, resting(tr), CTX) == []
    assert observe(tr, lagging, resting(tr), now=100.0 + REDUCE_SETTLE_S)[1] == []
    seen, ev = observe(tr, lagging, resting(tr), now=101.0 + REDUCE_SETTLE_S)
    assert [k for k, _ in ev] == ["REDUCE_NOT_SEEN", "UNPROTECTED"] and seen.pending_reduce is None
    assert plan(eng, seen, lagging, resting(tr), CTX) == [
        Alert("EXCESS_POSITION", f"trade {T}: position 3 > 2 expected"), Reduce(1, "EXCESS")]
    assert [k for k, _ in observe(tr, Position(1, 2, 60_020.0), resting(tr), now=110.0)[1]] == ["REDUCE_SETTLED"]


def test_a_vanished_tp1_leg_is_never_synced_at_market():
    """Probe (b): the TP1 leg filled between the position read and the order read (or the order list is ahead of
    the position): the position still shows 5 while the TP1 leg is gone. The engine's TP1 must not be synced by a
    market sale of what the leg already sold (the executor looks a vanished leg up before re-placing it); the next
    read folds the fill as the live TP1."""
    tr = five()
    stale, gone = Position(1, 5, 60_020.0), resting(tr)[1:]
    seen, ev = observe(tr, stale, gone, now=100.0)
    assert [k for k, _ in ev] == ["UNPROTECTED"] and not seen.tp1_filled
    eng = target(position=tpos(tp1_done=True))
    assert not [a for a in plan(eng, seen, stale, gone, CTX) if isinstance(a, Reduce)]
    half = resting(tr, tp1={"filled": 1})  # half the TP1 leg filled, the position does not show it yet
    assert plan(eng, tr, stale, half, CTX)[0] == Reduce(1, "TP1_SYNC")  # only what the leg has left
    later, ev = observe(seen, Position(1, 3, 60_020.0), gone, now=110.0)  # the fill shows: the live TP1
    assert [k for k, _ in ev] == ["TP1_FILLED", "PROTECTED"] and expected_contracts(later) == 3
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --group live pytest tests/live/test_reconcile.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'ewbot.live.reconcile'`.

- [ ] **Step 3: Write minimal implementation**

**File: `src/ewbot/live/reconcile.py`**

```python
"""Target vs. actual -> actions (Plan 3 spec 3.2 `reconcile`, 4.2, 4.4). Pure functions: no I/O.

State of the one live trade (`LiveTrade`, persisted by the executor in the journal) is compared with
the engine's target (live/target.py) and the exchange's position and open orders:

  1. `observe` folds exchange facts into the trade: flat -> CLOSED; a reduce of ours that the executor
     recorded before sending it (`pending_reduce`, intent before state) and that now shows in the position
     is ours whatever its answer was (REDUCE_SETTLED: TP1_SYNC -> the live TP1, MARGIN -> a smaller trade;
     a filled reduce the position still does not show after REDUCE_SETTLE_S yields to the position,
     REDUCE_NOT_SEEN); fewer contracts than expected (`expected_contracts`: all before the live TP1, the
     rest after it) are otherwise explained only by our resting legs - a reduction is a bracket fill when
     the legs still cover exactly the position: the TP1 leg used up -> TP1_FILLED (break-even), else
     PARTIAL_FILL (the legs keep their remainders); any other reduction is UNEXPLAINED_REDUCTION (the trade
     shrinks to the position, the stop stays where it is, no break-even). Open legs that do not cover the
     position -> unprotected since now (a major incident after 60 s, spec 6.2; PROTECTED ends it, a later
     gap is a new incident).
  2. `plan` returns the actions, in order:
       - a position the journal does not know: adopt it when the engine holds the same direction (at
         most the stage maximum `ctx.max_contracts`, the rest is reduced: EXCESS), else close it
         (spec 4.4: the actual position always follows the target);
       - within the hour after an entry the replay still shows it as queued (`target.entry`): the live
         trade of that signal is the engine position (`pending_position`), never a reason to close;
       - flat: cancel our leftover orders; enter when the engine queued an entry at the last close
         (never a signal the live trade already traded: closed early by its bracket, it is not
         re-entered);
       - engine flat / another trade / exit queued (TIME, LINEAGE, BREAKER, FLIP, ...): close, then
         enter if the engine also queued an entry (the executor enters only after the close is
         confirmed, spec 4.2 `반대 신호 전환`). An exit queued before the CFM halt is still queued at
         the halt bar (`exit_queued_ts` < bar) and a stop the halt bar crossed stops the engine out at
         the reopen (`stop_pending_reopen`, reason STOP_HALT): both close as well - the executor sends
         the close at the first poll that sees the session OPEN, whichever target is current then -
         until then the position stays protected (missing legs are placed at the stop in force);
       - more contracts than expected (a manual order, a reduction that did not fill): reduce the excess
         at market while the session is OPEN (spec 4.4) and alert; the legs cover the expected size, or the
         whole position while the excess cannot be reduced (session closed, a reduce pending: brackets
         only reduce, so the excess is never left unprotected);
       - engine TP1 done but the live TP1 leg not filled: reduce by what the resting TP1 leg has left at
         market (spec 4.4) - never while that leg is missing (a vanished leg may have filled with the
         position read before it: the executor looks it up before re-placing it);
       - never a second reduce while one is pending: until its outcome is known it is resolved by its
         client order id (executor), and the trade is planned as it was; once the venue reported it
         filled, as it will be (the position may still lag behind it);
       - protection: the brackets must equal the desired legs (spec 4.2): two legs (TP1 size + stop,
         rest + TP2 + stop) before TP1, one leg (rest + TP2 + stop) after it, one leg (TP1 + stop) for
         1 contract; after the live TP1 the stop is at least break-even (live entry price). Legs never
         exceed the position. A moved engine stop is mapped with the basis ratio at the time; without
         quotes the stop in force is kept and the move is planned again once quotes are back.
     Without a target (data late, spec 4.1 step 1) only protection is maintained.

Client order ids are deterministic (spec 4.2): entry `ewb-e-<signal ts>-<L|S>`, brackets
`ewb-b-<trade>-<leg>-<generation>`, closes `ewb-x-<trade>-<n>`, reductions `ewb-r-<trade>-<n>`."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from ewbot.live.exchange import Order, Position
from ewbot.live.pricing import map_level, to_tick
from ewbot.live.target import Target, TargetEntry, TargetPosition

PREFIX = "ewb-"
UNPROTECTED_MAX_S = 60.0  # spec 6.2: a position unprotected for longer is a major incident
REDUCE_SETTLE_S = 60.0  # a reduce the venue reported filled but the position does not show by then: the position wins


def entry_cid(signal_ts: int, direction: int) -> str:
    return f"{PREFIX}e-{int(signal_ts)}-{'L' if direction > 0 else 'S'}"


def bracket_cid(trade_id: int, leg: str, gen: int) -> str:
    return f"{PREFIX}b-{int(trade_id)}-{leg}-{int(gen)}"


def close_cid(trade_id: int, n: int) -> str:
    return f"{PREFIX}x-{int(trade_id)}-{int(n)}"


def reduce_cid(trade_id: int, n: int) -> str:
    return f"{PREFIX}r-{int(trade_id)}-{int(n)}"


def is_ours(order: Order) -> bool:
    return order.client_order_id.startswith(PREFIX)


@dataclass
class Leg:
    name: str  # "tp1" | "tp2"
    size: int
    limit_price: float  # futures take-profit
    stop_price: float  # futures stop trigger
    client_order_id: str = ""
    order_id: str | None = None


@dataclass
class LiveTrade:
    trade_id: int  # signal ts of the engine trade (its paper trade key is trade_id + 1h)
    setup: str
    direction: int
    contracts: int  # filled at entry
    tp1_n: int
    entry_price: float  # futures average fill
    tp1_price: float  # futures, mapped at entry
    tp2_price: float | None
    stop_spot: float  # engine stop (spot) the legs protect
    stop_price: float  # futures stop of the legs
    opened_ts: float
    tp1_filled: bool = False
    legs: list[Leg] = field(default_factory=list)
    gen: int = 0
    closes: int = 0
    protected_ts: float | None = None
    unprotected_since: float | None = None
    incident_unprotected: bool = False
    status: str = "OPEN"  # OPEN | CLOSED
    exit_reason: str | None = None
    # a reduce sent and not yet seen in the position, recorded by the executor before the request: {"cid", "size",
    # "reason", "before" (position contracts when sent), "ts", "after" (`after_reduce`: the trade's size fields
    # once done), "filled" (contracts the venue reported filled; None while the outcome is unknown)}
    pending_reduce: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> LiveTrade:
        d = dict(data)
        d["legs"] = [Leg(**leg) for leg in d.get("legs", [])]
        return cls(**d)


# ----------------------------------------------------------------------------- actions


@dataclass(frozen=True)
class Close:
    reason: str


@dataclass(frozen=True)
class Reduce:
    size: int
    reason: str


@dataclass(frozen=True)
class Protect:
    legs: tuple[Leg, ...]
    stop_spot: float
    stop_price: float


@dataclass(frozen=True)
class Enter:
    entry: TargetEntry
    contracts: int


@dataclass(frozen=True)
class Skip:
    signal_ts: int
    reason: str


@dataclass(frozen=True)
class Cancel:
    order_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Adopt:
    target: TargetPosition
    contracts: int  # contracts to take over (at most the stage maximum); the rest is reduced (EXCESS)


@dataclass(frozen=True)
class Alert:
    kind: str
    detail: str


Action = Close | Reduce | Protect | Enter | Skip | Cancel | Adopt | Alert


@dataclass(frozen=True)
class PlanContext:
    now: float
    ratio: float | None  # futures mid / spot mid now (None: quotes unavailable)
    tick: float
    session_open: bool = True  # market orders (entry, close, reduce) only while the session is OPEN
    entry_contracts: int = 0  # stage size for target.entry (0: skip)
    entry_block: str | None = None  # why no entry may be sent now (paused, session, margin, stale, size)
    max_contracts: int | None = None  # stage maximum for a position taken over (None: no cap)

    def __post_init__(self) -> None:
        if self.max_contracts is not None and self.max_contracts < 1:
            raise ValueError(f"bad max_contracts {self.max_contracts}")


# ----------------------------------------------------------------------------- observe


def _open_legs(trade: LiveTrade, orders: list[Order]) -> list[tuple[Leg, Order]]:
    by_id = {o.order_id: o for o in orders if o.is_open}
    return [(leg, by_id[leg.order_id]) for leg in trade.legs if leg.order_id in by_id]


def expected_contracts(trade: LiveTrade) -> int:
    """Contracts the live trade holds: all of them before the live TP1, the rest after it."""
    return trade.contracts - trade.tp1_n if trade.tp1_filled else trade.contracts


def after_reduce(trade: LiveTrade, size: int, reason: str, tp1_n: int | None = None) -> dict[str, Any]:
    """The trade's size fields once a reduce of `size` is done: TP1_SYNC marks the live TP1 done, EXCESS changes
    nothing (it closes contracts above the trade), any other reason (MARGIN) makes the trade smaller - `tp1_n`
    is then its new TP1 portion before the live TP1 (the executor splits it like the engine)."""
    after = {"contracts": trade.contracts, "tp1_n": trade.tp1_n, "tp1_filled": trade.tp1_filled}
    if reason == "TP1_SYNC":
        after["tp1_filled"] = True
    elif reason != "EXCESS":
        after["contracts"] = max(trade.contracts - size, 0)
        if not trade.tp1_filled and tp1_n is not None:
            after["tp1_n"] = tp1_n
    return after


def _settled(trade: LiveTrade) -> LiveTrade:
    """A copy of the trade with its pending reduce done (its `after` fields; nothing pending)."""
    d = trade.to_json()
    if trade.pending_reduce is not None:
        d.update(trade.pending_reduce["after"])
    d["pending_reduce"] = None
    return LiveTrade.from_json(d)


def _held(trade: LiveTrade, n: int) -> int:
    """The contracts the trade holds once a reduce the venue reported filled shows in a position of `n` that
    still lags behind it; `n` otherwise (nothing pending, the outcome unknown, or already shown)."""
    pr = trade.pending_reduce
    if pr is not None and pr.get("filled") and n >= pr["before"]:
        return max(n - int(pr["filled"]), 0)
    return n


def _leg_rest(trade: LiveTrade, orders: list[Order], name: str) -> int | None:
    """Contracts our `name` leg still has resting (0 once it left the book); None: the trade has no such leg."""
    leg = next((x for x in trade.legs if x.name == name), None)
    if leg is None:
        return None
    o = next((o for x, o in _open_legs(trade, orders) if x is leg), None)
    return 0 if o is None else max(o.size - o.filled, 0)


def _fold_reduction(t: LiveTrade, n: int, orders: list[Order]) -> tuple[str, dict[str, Any]]:
    """`t` (a copy) holds fewer contracts (`n`) than expected: fold the reduction into it and name it.

    A bracket fill reduces its leg and the position alike, so a reduction is ours when the legs still
    cover exactly the position. Before the live TP1 (two legs): the TP1 leg used up -> TP1_FILLED (a TP2
    fill in the same interval is folded in), else PARTIAL_FILL (the TP1 leg keeps its remainder). After it
    (one leg): PARTIAL_FILL. Anything else is UNEXPLAINED_REDUCTION: the trade shrinks to the position,
    the TP1 portion kept as far as possible, the stop unchanged (no break-even)."""
    expected = expected_contracts(t)
    if t.tp1_filled:
        rest2 = _leg_rest(t, orders, "tp2")
        t.contracts = n + t.tp1_n
        if rest2 == n:
            return "PARTIAL_FILL", {"trade_id": t.trade_id, "contracts": n}
    else:
        rest1, rest2 = _leg_rest(t, orders, "tp1"), _leg_rest(t, orders, "tp2")
        if t.contracts > 1 and rest1 is not None and rest2 is not None and rest1 + rest2 == n:
            if rest1 == 0:
                t.tp1_filled, t.contracts = True, n + t.tp1_n
                return "TP1_FILLED", {"trade_id": t.trade_id, "contracts": n}
            t.contracts, t.tp1_n = n, rest1
            return "PARTIAL_FILL", {"trade_id": t.trade_id, "contracts": n}
        t.contracts, t.tp1_n = n, max(1, min(t.tp1_n, n - 1))
    return "UNEXPLAINED_REDUCTION", {"trade_id": t.trade_id, "expected": expected, "position": n}


def observe(trade: LiveTrade | None, position: Position | None, orders: list[Order], now: float
            ) -> tuple[LiveTrade | None, list[tuple[str, dict[str, Any]]]]:
    """Fold exchange facts into the trade (a modified copy) and return the events they raise."""
    if trade is None or trade.status != "OPEN":
        return trade, []
    t = LiveTrade.from_json(trade.to_json())
    events: list[tuple[str, dict[str, Any]]] = []
    if position is None:
        t.status = "CLOSED"
        t.exit_reason = t.exit_reason or "BRACKET"
        t.unprotected_since = None
        events.append(("CLOSED", {"trade_id": t.trade_id, "reason": t.exit_reason}))
        return t, events
    if position.side != t.direction:
        return t, events  # plan() closes a position on the wrong side
    pr = t.pending_reduce
    if pr is not None and position.contracts < pr["before"]:  # our reduce shows in the position: ours
        t = _settled(t)
        events.append(("REDUCE_SETTLED", {"trade_id": t.trade_id, "reason": pr["reason"], "size": pr["size"],
                                          "position": position.contracts}))
    elif pr is not None and pr.get("filled") and now - float(pr["ts"]) > REDUCE_SETTLE_S:
        t.pending_reduce = None  # reported filled, still not in the position: the position wins (plan: EXCESS)
        events.append(("REDUCE_NOT_SEEN", {"trade_id": t.trade_id, "reason": pr["reason"], "filled": pr["filled"],
                                           "position": position.contracts}))
    held = _held(t, position.contracts)
    if held < expected_contracts(t):
        events.append(_fold_reduction(t, held, orders))
    covered = sum(o.size - o.filled for leg, o in _open_legs(t, orders) if o.stop_price is not None)
    if covered >= held:
        if t.unprotected_since is not None:
            events.append(("PROTECTED", {"trade_id": t.trade_id, "after_s": round(now - t.unprotected_since, 3)}))
        t.unprotected_since = None
        t.incident_unprotected = False  # the incident ends here: a later gap is a new one
    else:
        if t.unprotected_since is None:
            t.unprotected_since = now
            events.append(("UNPROTECTED", {"trade_id": t.trade_id, "covered": covered, "position": held}))
        elif now - t.unprotected_since > UNPROTECTED_MAX_S and not t.incident_unprotected:
            t.incident_unprotected = True
            events.append(("UNPROTECTED_60S", {"trade_id": t.trade_id, "since": t.unprotected_since}))
    return t, events


# ----------------------------------------------------------------------------- desired legs


def _leg_sizes(trade: LiveTrade, contracts: int) -> list[tuple[str, int]]:
    """Leg names and sizes covering `contracts` (normally `expected_contracts`; never more than held)."""
    if contracts <= 0:
        return []
    if trade.tp1_filled:
        return [("tp2", contracts)]
    if trade.contracts == 1 or contracts == 1:
        return [("tp1", contracts)]
    k = min(trade.tp1_n, contracts - 1)
    return [("tp1", k), ("tp2", contracts - k)]


def desired_legs(trade: LiveTrade, contracts: int, stop_spot: float, ratio: float | None, tick: float
                 ) -> tuple[list[Leg], float]:
    """The brackets that must rest for `contracts` contracts, and their futures stop. The stored futures
    stop is kept while the engine stop is unchanged (no churn when only the basis moves) or without
    quotes. Raises ValueError when a TP2 leg is needed and the trade has no TP2."""
    d = trade.direction
    if stop_spot == trade.stop_spot or ratio is None:
        stop = trade.stop_price
    else:
        stop = map_level(stop_spot, ratio, tick, d)
    if trade.tp1_filled:
        be = to_tick(trade.entry_price, tick, d)
        stop = max(stop, be) if d > 0 else min(stop, be)
    legs: list[Leg] = []
    for name, size in _leg_sizes(trade, contracts):
        if name == "tp1":
            legs.append(Leg("tp1", size, trade.tp1_price, stop))
        elif trade.tp2_price is None:
            raise ValueError(f"trade {trade.trade_id}: a TP2 leg of {size} needs a TP2 price")
        else:
            legs.append(Leg("tp2", size, trade.tp2_price, stop))
    return legs, stop


def legs_match(trade: LiveTrade, orders: list[Order], desired: list[Leg]) -> bool:
    """Every desired leg rests with exactly its terms, and no other leg of ours rests."""
    resting = _open_legs(trade, orders)
    if len(resting) != len(desired):
        return False
    have = {leg.name: (leg, o) for leg, o in resting}
    for want in desired:
        got = have.get(want.name)
        if got is None:
            return False
        leg, o = got
        if (o.size - o.filled, o.limit_price, o.stop_price) != (want.size, want.limit_price, want.stop_price):
            return False
    return True


# ----------------------------------------------------------------------------- plan


def pending_position(e: TargetEntry) -> TargetPosition:
    """The engine position an entry queued at the last close becomes at the next open: within that hour
    the live position already exists while the engine replay still shows it as a queued entry."""
    return TargetPosition(trade_key=e.signal_ts + 3600, signal_ts=e.signal_ts, setup=e.setup, direction=e.direction,
                          entry_price=e.entry_ref, stop=e.stop, stop0=e.stop, tp1=e.tp1, tp2=e.tp2, tp1_done=False,
                          contracts=e.contracts, remaining=e.contracts)


def engine_position(target: Target) -> TargetPosition | None:
    """The engine's position for the hour after the target bar: the open one, unless an exit is queued or
    the halt bar crossed its stop, else the one a queued entry opens."""
    if target.position is not None and target.exit_reason is None and not target.stop_pending_reopen:
        return target.position
    if target.entry is not None:
        return pending_position(target.entry)
    return None


def _engine_view(target: Target, trade: LiveTrade) -> TargetPosition | str:
    """The engine position of the live trade, or the reason the live trade must close."""
    e = target.entry
    if e is not None and e.signal_ts == trade.trade_id:
        return pending_position(e)  # entered at this hour's open; the replay still shows the queued entry
    if target.exit_reason is not None:  # queued at the last close, or before the halt (carried)
        return target.exit_reason
    tp = target.position
    if tp is None:
        return "ENGINE_FLAT"
    if tp.signal_ts != trade.trade_id:
        return "ENGINE_OTHER_TRADE"
    if target.stop_pending_reopen:
        return "STOP_HALT"
    return tp


def _protection(trade: LiveTrade, orders: list[Order], contracts: int, stop_spot: float,
                ctx: PlanContext) -> list[Action]:
    desired, stop = desired_legs(trade, contracts, stop_spot, ctx.ratio, ctx.tick)
    if not legs_match(trade, orders, desired) or stop_spot != trade.stop_spot:
        return [Protect(tuple(desired), stop_spot, stop)]
    return []


def plan(target: Target | None, trade: LiveTrade | None, position: Position | None, orders: list[Order],
         ctx: PlanContext) -> list[Action]:
    acts: list[Action] = []
    ours = [o for o in orders if o.is_open and is_ours(o)]
    foreign = [o for o in orders if o.is_open and not is_ours(o)]
    if foreign:
        acts.append(Alert("FOREIGN_ORDERS", ",".join(sorted(o.order_id for o in foreign))))
    open_trade = trade if trade is not None and trade.status == "OPEN" else None

    def entry_actions() -> list[Action]:
        if target is None or target.entry is None:
            return []
        e = target.entry
        if trade is not None and trade.trade_id == e.signal_ts:
            return []  # this signal was traded (open, or closed early by its bracket): never entered twice
        if ctx.entry_block is not None:
            return [Skip(e.signal_ts, ctx.entry_block)]
        if e.tp2 is None:
            return [Skip(e.signal_ts, "the engine signal has no TP2 (a second bracket needs one)")]
        return [Enter(e, ctx.entry_contracts)]

    if position is None:
        if ours:
            acts.append(Cancel(tuple(o.order_id for o in ours), "FLAT"))
        return acts + entry_actions()

    n = position.contracts
    if open_trade is None:  # a position the journal does not know
        if target is None:
            return acts + [Alert("UNKNOWN_POSITION", f"{position.side:+d} x{n}: waiting for a target")]
        held = engine_position(target)
        if held is None or held.direction != position.side:
            return acts + [Close("UNKNOWN_POSITION")]
        take = n if ctx.max_contracts is None else min(n, ctx.max_contracts)
        if take == n:
            return acts + [Adopt(held, n)]
        acts += [Alert("EXCESS_POSITION", f"position {n} > stage maximum {ctx.max_contracts}: adopting {take}"),
                 Adopt(held, take)]
        return acts + ([Reduce(n - take, "EXCESS")] if ctx.session_open else [])

    if position.side != open_trade.direction:
        acts.append(Close("DIRECTION_MISMATCH"))
        if not ctx.session_open:  # the trade's legs only reduce its own side: nothing can protect this
            acts.append(Alert("DIRECTION_MISMATCH", f"trade {open_trade.trade_id} {open_trade.direction:+d}: "
                                                    f"position {position.side:+d} x{n} unprotected until the reopen"))
        return acts

    pr = open_trade.pending_reduce
    if pr is not None and n < pr["before"]:  # our reduce shows in the position (observe settles it as well)
        open_trade, pr = _settled(open_trade), None
    have = _held(open_trade, n)  # a filled reduce the position does not show yet counts as done
    expected = expected_contracts(open_trade)
    excess = have - expected
    reducible = ctx.session_open and pr is None  # market orders need an OPEN session; one reduce at a time
    # the legs cover the expected size, never more than the position; an excess that cannot be reduced now
    # is covered as well (brackets only reduce: protected until the reduce at the reopen resizes them)
    size = have if excess > 0 and not reducible else min(have, expected)
    stop_spot = open_trade.stop_spot
    tp: TargetPosition | None = None
    if target is not None:
        view = _engine_view(target, open_trade)
        if isinstance(view, str):
            acts.append(Close(view))
            if not ctx.session_open:  # the close waits for the reopen: keep the position protected meanwhile
                acts += _protection(open_trade, orders, size, stop_spot, ctx)
            return acts + entry_actions()
        tp = view
        if ctx.ratio is not None:  # without quotes the stop in force stays; the move is planned again later
            stop_spot = tp.stop

    if excess > 0:  # spec 4.4: the excess is closed at market (a manual order, an unfilled reduction)
        acts.append(Alert("EXCESS_POSITION", f"trade {open_trade.trade_id}: position {n} > {expected} expected"))
        if reducible:
            acts.append(Reduce(excess, "EXCESS"))
    elif (excess == 0 and tp is not None and tp.tp1_done and not open_trade.tp1_filled and open_trade.contracts > 1
          and reducible):
        rest1 = _leg_rest(open_trade, orders, "tp1")
        if rest1:  # only against a resting TP1 leg, by what it has left: a vanished one may have filled already
            k = min(open_trade.tp1_n, rest1)
            acts.append(Reduce(k, "TP1_SYNC"))
            open_trade = LiveTrade.from_json({**open_trade.to_json(), "tp1_filled": True})
            size = min(have - k, expected_contracts(open_trade))
    return acts + _protection(open_trade, orders, size, stop_spot, ctx)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run --group live pytest tests/live/test_reconcile.py -q
```

Expected: `29 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/live/reconcile.py tests/live/test_reconcile.py
git commit -m "$(cat <<'EOF'
feat(live): reconcile planner - observe fills, desired bracket legs, entries, exits, TP1 sync

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Executor — intent-before-send, protection, closes and restart recovery

**Files:**
- Create: `src/ewbot/live/executor.py`
- Create: `tests/live/helpers.py` (shared test builders: `T0`, `Clock`, `Alerts`, `sim`, `journal`, `entry`, `position`, `target`, `ctx(clock, ratio=1.0, contracts=1, block=None, session_open=True, max_contracts=None)`)
- Test: `tests/live/test_executor.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 5, 6 (`Exchange`, `ExchangeError`, `Order`, `PlaceResult`, `SimExchange`, `SimCrash`, `Journal`, `map_levels`, every `reconcile` name), `ewbot.live.target.TargetPosition`, `ewbot.engine.position_mgr._tp1_split` (read-only import of the engine's TP1 split).
- Produces (`ewbot.live.executor`): `FILL_TIMEOUT_S = 10.0`, `FLAT_TIMEOUT_S = 30.0`, `POLL_S = 0.5`, `LOOKUP_WINDOW_S = 300.0`, `ABANDON_AFTER_S = 600.0`, `LEG_SETTLE_S = 30.0`, `CLEAR_TIMEOUT_S = 5.0`, journal keys `TRADE_KEY = "trade"`, `SKIPS_KEY`, `ORPHAN_KEY`, `SCRATCH_KEY` (one trade's once-alerts, leg-fill marks and a provisional or approximate takeover: a single key, dropped with the trade), `CANCEL_QUEUED` (never a leg), refusal reason codes `OVERLAP_REFUSALS` (a bracket refused because our brackets would exceed the position: SimExchange `INSUFFICIENT_POSITION_SIZE` and the documented Coinbase candidates) and `CLOSE_CONFLICTS` (a close refused while brackets rest), both matched on the code (the first token of `PlaceResult.error`) and to be confirmed in CANARY; frozen `Sent(status: "ACCEPTED"|"REJECTED"|"PENDING", order_id=None, error=None)`; `class Executor(exchange, journal, *, clock=time.time, sleep=time.sleep, alert=None, tp1_frac=0.5, edit_stops=False, fill_timeout=10.0, flat_timeout=30.0, poll_s=0.5, overlap_refusals=OVERLAP_REFUSALS, close_conflicts=CLOSE_CONFLICTS)` with `trade() -> LiveTrade | None`, `save(trade)`, `event(kind, data, text=None)` (journal + alert when `text`), `send(cid, kind, trade_id, payload, call) -> Sent`, `execute(actions, ctx)` (first settles a provisional trade - `RECOVERED_ENTRY` or `ADOPTED` - and maps levels taken over without quotes - `LEVELS_MAPPED`; an `Enter` after a `Close` or `Cancel` of the same pass waits up to `CLEAR_TIMEOUT_S` for our brackets to leave the book, else it is skipped; a `Protect` planned with a `Reduce` is skipped when that reduce is not known filled: the resting legs stay), `enter(Enter, ctx) -> LiveTrade | None` (never a signal skipped once; a trade opens at its entry order's `created_ts`), `adopt(TargetPosition, ctx, contracts=None)` (rebuilds our own accepted entry from its intent - one still PENDING is looked up first, and while that lookup fails - or while the fill of an accepted one cannot be read (`get_order` failing) - the position is protected at once with a provisional trade from the intent: its levels, the position's size and price, `ADOPTED_PROVISIONAL` - unless a trade with that id was recorded already; else adopts a foreign position at its own entry price (`ADOPTED`): at most `contracts` and `ctx.max_contracts` - the planner's `Reduce EXCESS` closes the rest; a position the engine took TP1 on is adopted as the rest after TP1: `tp1_filled`, `tp1_n = 0`; without quotes a foreign position is protected at the spot levels (ratio 1.0, `LEVELS_APPROX` alerted once) until they are back; a rebuilt or adopted trade goes on from the journal's highest bracket generation and close number for its id - client order ids are never reused), `protect(Protect, ctx)` (never removes a working leg before its replacement rests: replace = place first, then cancel; cancel first (`LEG_GAP`, no `BRACKET_FAILED`) only on a refusal code in `overlap_refusals` while our brackets plus the new leg exceed the position (fresh reads), and if the replacement then fails the old terms are placed again (`LEG_RESTORED`); an unknown outcome (PENDING) or any other refusal keeps the old leg (`BRACKET_FAILED`) - a PENDING leg found among the open orders is accepted at once (`found resting`); a resting bracket of ours named for the trade and leg but not listed (accepted before a crash, an unconfirmed replacement) is taken over when its terms match (`LEG_ADOPTED`), else replaced; an order whose cancel is queued is never a leg; other brackets of ours are cancelled only once every wanted leg is in place; the new stop is recorded (`STOP_MOVED`) only once every leg rests at it; with `edit_stops` the edit sends the order's total size, filled + left; a leg that left the book is looked up first: one with fills is not placed again for `LEG_SETTLE_S` - event `LEG_FILLED` - so the next observe folds it; unreadable or unfilled: placed again; a replaced leg is cancelled once), `close(reason, ctx) -> bool` (flat confirmed; close_position is sent with the brackets resting - only a refusal code in `close_conflicts` cancels them, after a fresh session read (not OPEN: deferred, still protected), event `CLOSE_CONFLICT`, and the close is resent at once; the reason is on the trade once the close is accepted; the flat wait ends at once when the IOC close ended short; every failed or unconfirmed close makes the legs the ones for the position left (placed again when none of the trade's legs rests, else kept or resized); brackets left on the flat position are cancelled), `reduce(size, reason, ctx) -> bool` (records `pending_reduce` - the fresh position as `before`, `reconcile.after_reduce` as `after` - before sending; while one is pending it resolves that one instead of sending another; True when the venue reported it filled: its `after` is applied at once (`TP1_SYNC` marks the live TP1 done, `EXCESS` leaves the trade unchanged, `MARGIN` makes the trade smaller) and the record stays until the position shows it; refused, abandoned or unfilled: the record is dropped; refused with a `close_conflicts` code (session read first): makes room - `TP1_SYNC` cancels the TP1 leg, `EXCESS` / `MARGIN` resize the legs to the trade after it, placed first - and resends under a new reduce id (`REDUCE_CONFLICT`, no `REDUCE_FAILED`), the legs going back for the whole position when it does not fill), `resolve_reduce()` (a pending reduce with an unknown outcome: nothing when the position already shows a reduction, else `send` with its id - looked up first, re-sent under the same id only when the venue does not have it; a `close_conflicts` refusal drops the record, `REDUCE_CONFLICT`, so the next pass makes room), `flatten(reason, ctx) -> bool`, `resolve_pending() -> (counts, CLOSE intents found)` (no event; the runtime calls it every minute while intents are pending), `recover() -> {"accepted", "abandoned", "unknown", "rebuilt"}` (startup; records a close ACCEPTED before a crash with its reason and fill; rebuilds an accepted entry only when no trade with its id was recorded; the open trade's resting brackets (`ewb-b-<trade>-<leg>-*`, not cancel-queued) are its legs again (`LEG_ADOPTED`; `PROTECTED_FIRST` from their creation when they cover the position); a failed read is `RECOVERY_ERROR`, counted `unknown`, never raised; writes `RECOVERY`). Journal events written: `ENTRY` (`trade_id, side, contracts, price, ref, fee, order_id`), `PROTECTED_FIRST` (`after_entry_s`), `STOP_MOVED` (`stop, stop_spot`), `STOP_EDITED`, `LEG_GAP`, `BRACKET_FAILED`, `CLOSED` (`trade_id, reason, price, contracts, side, fee`), `CLOSE_FAILED`, `CLOSE_UNCONFIRMED`, `REDUCED` (`trade_id, size, reason, price, contracts, side, fee`), `REDUCE_FAILED` (`trade_id, reason, status` `PENDING` / `REJECTED` / `UNFILLED`, `error`), `LEG_FILLED` (`trade_id, leg, order_id, filled, status`), `ENTRY_FAILED`, `ENTRY_UNFILLED`, `ENTRY_UNCONFIRMED`, `CANCEL_FAILED`, `SKIP`, `ADOPTED` (`trade_id, contracts, position`), `RECOVERED_ENTRY`, `RECOVERY`, `RECOVERY_ERROR`, `LEG_ADOPTED`, `LEG_RESTORED`, `ADOPTED_PROVISIONAL` (`trade_id, contracts, position`), `LEVELS_MAPPED`, `CLOSE_CONFLICT`, `REDUCE_CONFLICT`, `CANCELLED`, `ACTION_ERROR`, and alert events sent once per trade (the planner's `Alert` kinds such as `FOREIGN_ORDERS`, `UNKNOWN_POSITION`; `CLOSE_DEFERRED`, `LEVELS_APPROX`).

- [ ] **Step 1: Write the failing test**

**File: `tests/live/helpers.py`**

```python
"""Shared builders for tests/live: a settable clock, a recording alerter, engine targets and a
SimExchange with prices already set. Synthetic only - no network, no real account."""

from __future__ import annotations

from ewbot.live.journal import Journal
from ewbot.live.reconcile import PlanContext
from ewbot.live.sim import SimExchange
from ewbot.live.target import Target, TargetEntry, TargetPosition

T0 = 1_790_942_400  # 2026-10-02 11:00 UTC (a bar open)


class Clock:
    def __init__(self, now: float = T0 + 3630.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += float(s)


class Alerts:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []

    def __call__(self, kind: str, text: str) -> None:
        self.items.append((kind, text))

    def kinds(self) -> list[str]:
        return [k for k, _ in self.items]


def sim(clock: Clock, fut: float = 60_000.0, spot: float = 60_000.0, **kw) -> SimExchange:
    ex = SimExchange(clock=clock, **kw)
    ex.set_prices(fut, spot)
    return ex


def journal(tmp_path, name: str = "journal.sqlite") -> Journal:
    return Journal(tmp_path / name)


def entry(signal_ts: int = T0, direction: int = 1, stop: float = 59_000.0, tp1: float = 61_000.0,
          tp2: float | None = 62_000.0, setup: str = "W3") -> TargetEntry:
    return TargetEntry(signal_ts, setup, direction, 60_000.0, stop, tp1, tp2, 1.0, 5, "test signal")


def position(signal_ts: int = T0, direction: int = 1, stop: float = 59_000.0, tp1_done: bool = False,
             tp1: float = 61_000.0, tp2: float | None = 62_000.0) -> TargetPosition:
    return TargetPosition(signal_ts + 3600, signal_ts, "W3", direction, 60_000.0, stop, 59_000.0, tp1, tp2,
                          tp1_done, 5, 5)


def target(bar_ts: int, pos: TargetPosition | None = None, ent: TargetEntry | None = None,
           exit_reason: str | None = None) -> Target:
    return Target(bar_ts, 60_000.0, pos, exit_reason, ent, ())


def ctx(clock: Clock, ratio: float | None = 1.0, contracts: int = 1, block: str | None = None,
        session_open: bool = True, max_contracts: int | None = None) -> PlanContext:
    return PlanContext(now=clock(), ratio=ratio, tick=5.0, session_open=session_open, entry_contracts=contracts,
                       entry_block=block, max_contracts=max_contracts)
```

**File: `tests/live/test_executor.py`**

```python
from __future__ import annotations

import dataclasses

import pytest

from ewbot.live.exchange import Order, PlaceResult, RateLimited, Unavailable
from ewbot.live.executor import Executor
from ewbot.live.reconcile import (
    Adopt,
    Alert,
    Close,
    Enter,
    Leg,
    Protect,
    Reduce,
    Skip,
    desired_legs,
    expected_contracts,
    observe,
    plan,
)
from ewbot.live.sim import SimCrash
from tests.live.helpers import T0, Alerts, Clock, ctx, entry, journal, position, sim, target


def poll(exe: Executor, ex, clock: Clock, tgt) -> list[str]:
    """One runtime pass: a pending reduce resolved by its id first, then observe -> plan -> execute."""
    exe.resolve_reduce()
    seen, events = observe(exe.trade(), ex.position(), ex.open_orders(), clock())
    exe.save(seen)
    c = ctx(clock)
    exe.execute(plan(tgt, seen, ex.position(), ex.open_orders(), c), c)
    return [k for k, _ in events]


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    ex = sim(clock)
    jr = journal(tmp_path)
    alerts = Alerts()
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep, alert=alerts)
    yield clock, ex, jr, alerts, exe
    jr.close()


def test_entry_sends_one_market_order_then_two_brackets(env):
    clock, ex, jr, alerts, exe = env
    tr = exe.enter(Enter(entry(), 3), ctx(clock))
    assert tr is not None and (tr.contracts, tr.tp1_n, tr.entry_price) == (3, 1, 60_002.5)
    assert ex.position().contracts == 3
    rest = sorted((o.client_order_id, o.size, o.limit_price, o.stop_price) for o in ex.open_orders())
    assert rest == [(f"ewb-b-{T0}-tp1-1", 1, 61_000.0, 59_000.0), (f"ewb-b-{T0}-tp2-2", 2, 62_000.0, 59_000.0)]
    assert jr.intent(f"ewb-e-{T0}-L")["status"] == "ACCEPTED"
    assert [e["kind"] for e in jr.events()] == ["ENTRY", "PROTECTED_FIRST"]
    assert jr.events(kind="PROTECTED_FIRST")[0]["data"]["after_entry_s"] <= 30.0
    assert alerts.kinds() == ["ENTRY"]
    assert exe.enter(Enter(entry(), 3), ctx(clock)) is None  # an entry is never sent twice
    assert ex.calls.count("place_market") == 1


def test_one_contract_uses_one_bracket_and_levels_are_mapped(env):
    clock, ex, jr, _, exe = env
    tr = exe.enter(Enter(entry(stop=59_001.0, tp1=61_003.0), 1), ctx(clock, ratio=1.001))
    assert (tr.stop_price, tr.tp1_price, tr.tp2_price) == (59_060.0, 61_060.0, 62_060.0)  # x1.001, floored
    assert [(o.size, o.limit_price, o.stop_price) for o in ex.open_orders()] == [(1, 61_060.0, 59_060.0)]


def test_a_rejected_entry_is_recorded_and_never_retried(env):
    clock, ex, jr, alerts, exe = env
    ex.set_session("CLOSE")
    assert exe.enter(Enter(entry(), 1), ctx(clock)) is None
    assert jr.intent(f"ewb-e-{T0}-L")["status"] == "REJECTED" and "ENTRY_FAILED" in alerts.kinds()
    ex.set_session("OPEN")
    assert exe.enter(Enter(entry(), 1), ctx(clock)) is None and ex.position() is None


def test_an_ambiguous_entry_is_looked_up_before_it_is_sent_again(env):
    clock, ex, jr, _, exe = env
    ex.fail_next("place_market", Unavailable("503"))  # failed before reaching the venue
    assert exe.enter(Enter(entry(), 1), ctx(clock)) is None
    assert jr.intent(f"ewb-e-{T0}-L")["status"] == "PENDING" and ex.position() is None
    tr = exe.enter(Enter(entry(), 1), ctx(clock))  # lookup: not there -> sent again with the same id
    assert tr is not None and ex.calls.count("place_market") == 2 and ex.position().contracts == 1
    assert [o.client_order_id for o in ex.orders.values()].count(f"ewb-e-{T0}-L") == 1


def test_a_lost_reply_after_the_venue_accepted_is_found_not_resent(env):
    clock, ex, jr, _, exe = env
    ex.crash_after_accept("place_market", Unavailable("read timeout"))
    tr = exe.enter(Enter(entry(), 1), ctx(clock))
    assert tr is not None and ex.calls.count("place_market") == 1 and ex.position().contracts == 1
    assert jr.intent(f"ewb-e-{T0}-L")["note"] == "found after error"


def test_an_entry_whose_fill_cannot_be_read_is_rebuilt_not_adopted(env):
    clock, ex, jr, alerts, exe = env
    ex.fail_next("get_order", Unavailable("503"), times=25)  # longer than the 10 s fill wait
    assert exe.enter(Enter(entry(), 1), ctx(clock)) is None
    assert ex.position().contracts == 1 and exe.trade() is None and jr.events(kind="ENTRY_UNCONFIRMED")
    ex._faults.clear()
    acts = plan(target(T0 + 3600, ent=entry()), None, ex.position(), ex.open_orders(), ctx(clock))
    exe.execute(acts, ctx(clock))
    assert exe.trade().trade_id == T0 and len(ex.open_orders()) == 1
    assert "RECOVERED_ENTRY" in alerts.kinds() and "ADOPTED" not in alerts.kinds()


def test_a_duplicate_id_error_resolves_to_the_existing_order(tmp_path):
    clock = Clock()
    ex = sim(clock, duplicate="error")
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    ex.place_market(f"ewb-e-{T0}-L", 1, 1)  # e.g. sent by a run killed before it could record the reply
    jr.record_intent(f"ewb-e-{T0}-L", "ENTRY", T0, {}, clock())
    jr.update_intent(f"ewb-e-{T0}-L", "PENDING", clock())
    sent = exe.send(f"ewb-e-{T0}-L", "ENTRY", T0, {}, lambda: ex.place_market(f"ewb-e-{T0}-L", 1, 1))
    assert sent.status == "ACCEPTED" and ex.position().contracts == 1
    jr.close()


def test_stop_moves_replace_place_first_then_cancel(env):
    clock, ex, jr, _, exe = env
    tr = exe.enter(Enter(entry(), 3), ctx(clock))
    legs, stop = desired_legs(tr, 3, 59_500.0, 1.0, 5.0)
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    rest = sorted((o.client_order_id, o.stop_price) for o in ex.open_orders())
    assert rest == [(f"ewb-b-{T0}-tp1-3", 59_500.0), (f"ewb-b-{T0}-tp2-4", 59_500.0)]
    assert exe.trade().stop_spot == 59_500.0 and "STOP_MOVED" in [e["kind"] for e in jr.events()]
    assert not jr.events(kind="CANCEL_FAILED")  # each replaced leg is cancelled once


def test_a_refused_overlap_falls_back_to_cancel_then_place(tmp_path):
    """Probe B: on a venue that refuses overlapping brackets the fallback is the designed path - a LEG_GAP each,
    no BRACKET_FAILED (a CANARY gate 2 failure) and no alert."""
    clock = Clock()
    ex = sim(clock, reject_overlap=True)
    jr = journal(tmp_path)
    alerts = Alerts()
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep, alert=alerts)
    tr = exe.enter(Enter(entry(), 3), ctx(clock))
    legs, stop = desired_legs(tr, 3, 59_500.0, 1.0, 5.0)
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    assert sorted((o.size, o.stop_price) for o in ex.open_orders()) == [(1, 59_500.0), (2, 59_500.0)]
    assert len(jr.events(kind="LEG_GAP")) == 2 and not jr.events(kind="BRACKET_FAILED")
    assert alerts.kinds() == ["ENTRY", "STOP_MOVED"]
    jr.close()


def test_edit_in_place_when_enabled(tmp_path):
    clock = Clock()
    ex = sim(clock, edit_supported=True)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep, edit_stops=True)
    tr = exe.enter(Enter(entry(), 1), ctx(clock))
    legs, stop = desired_legs(tr, 1, 59_500.0, 1.0, 5.0)
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    assert [(o.client_order_id, o.stop_price) for o in ex.open_orders()] == [(f"ewb-b-{T0}-tp1-1", 59_500.0)]
    assert ex.calls.count("place_bracket") == 1
    jr.close()


def test_close_cancels_brackets_and_waits_for_flat(env):
    clock, ex, jr, alerts, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    assert exe.close("TIME", ctx(clock)) is True
    assert ex.position() is None and ex.open_orders() == []
    tr = exe.trade()
    assert (tr.status, tr.exit_reason, tr.closes) == ("CLOSED", "TIME", 1)
    assert jr.intent(f"ewb-x-{T0}-1")["status"] == "ACCEPTED" and "CLOSED" in alerts.kinds()


def test_a_close_is_sent_with_the_brackets_resting(env):
    """Protection is never cancelled before the venue took the close: close_position goes out with the brackets
    resting (both only reduce); the ones left on the flat position go with it."""
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    first = len(ex.calls)
    assert exe.close("TIME", ctx(clock)) is True
    assert "cancel" not in ex.calls[first:ex.calls.index("close_position", first)]
    assert ex.position() is None and ex.open_orders() == [] and not jr.events(kind="CANCEL_FAILED")


def test_close_waits_for_an_open_session_and_alerts_once(env):
    clock, ex, jr, alerts, exe = env
    exe.enter(Enter(entry(), 1), ctx(clock))
    ex.set_session("CLOSE")
    assert exe.close("LINEAGE", ctx(clock, session_open=False)) is False
    assert exe.close("LINEAGE", ctx(clock, session_open=False)) is False
    assert alerts.kinds().count("CLOSE_DEFERRED") == 1 and ex.position() is not None
    ex.set_session("OPEN")
    assert exe.close("LINEAGE", ctx(clock)) is True


def test_close_failure_is_retried_with_a_new_id(env):
    clock, ex, jr, alerts, exe = env
    exe.enter(Enter(entry(), 1), ctx(clock))
    ex.fail_next("close_position", RateLimited("429"))
    assert exe.close("TIME", ctx(clock)) is False and "CLOSE_FAILED" in alerts.kinds()
    assert exe.close("TIME", ctx(clock)) is True
    assert jr.intent(f"ewb-x-{T0}-1")["status"] == "PENDING" and jr.intent(f"ewb-x-{T0}-2")["status"] == "ACCEPTED"


def test_tp1_sync_reduces_then_the_rest_is_protected_at_break_even(env):
    clock, ex, jr, _, exe = env
    tr = exe.enter(Enter(entry(), 3), ctx(clock))
    ex.set_prices(60_900.0)  # the engine's TP1 (spot) filled, the live TP1 limit 61_000 did not
    acts = plan(target(T0 + 3600, position(tp1_done=True, stop=60_000.0)), tr, ex.position(), ex.open_orders(), ctx(clock))
    assert isinstance(acts[0], Reduce) and isinstance(acts[1], Protect)
    exe.execute(acts, ctx(clock))
    assert ex.position().contracts == 2 and exe.trade().tp1_filled
    assert [(o.size, o.limit_price, o.stop_price) for o in ex.open_orders()] == [(2, 62_000.0, 60_000.0)]


def test_an_unknown_position_matching_the_engine_is_adopted_and_protected(env):
    clock, ex, jr, alerts, exe = env
    ex.place_market("manual-1", 1, 2)
    acts = plan(target(T0 + 3600, position()), None, ex.position(), ex.open_orders(), ctx(clock))
    exe.execute(acts, ctx(clock))
    tr = exe.trade()
    assert tr.trade_id == T0 and tr.contracts == 2 and "ADOPTED" in alerts.kinds()
    assert sorted(o.size for o in ex.open_orders()) == [1, 1]


def test_an_adopted_position_is_capped_at_the_stage_maximum(env):
    clock, ex, jr, alerts, exe = env
    ex.place_market("manual-1", 1, 3)
    c = ctx(clock, max_contracts=1)  # CANARY
    exe.execute(plan(target(T0 + 3600, position()), None, ex.position(), ex.open_orders(), c), c)
    assert (exe.trade().contracts, ex.position().contracts) == (1, 1)
    assert [(o.size, o.limit_price) for o in ex.open_orders()] == [(1, 61_000.0)]
    assert jr.events(kind="REDUCED")[0]["data"]["reason"] == "EXCESS" and "EXCESS_POSITION" in alerts.kinds()
    ex.place_market("manual-2", 1, 2)  # the executor never adopts more than the stage maximum either
    exe.save(None)
    exe.execute([Adopt(position(), 3)], ctx(clock, max_contracts=2))
    assert exe.trade().contracts == 2 and sum(o.size for o in ex.open_orders()) == 2


def test_a_position_adopted_after_the_engine_tp1_is_the_rest(env):
    clock, ex, jr, _, exe = env
    ex.place_market("manual-1", 1, 2)  # @ 60_002.5
    ex.set_prices(61_200.0)  # past the engine's TP1
    tgt = target(T0 + 3600, position(tp1_done=True, stop=60_000.0))
    exe.execute(plan(tgt, None, ex.position(), ex.open_orders(), ctx(clock)), ctx(clock))
    tr = exe.trade()
    assert tr.tp1_filled and expected_contracts(tr) == 2  # no phantom excess to reduce
    assert [(o.size, o.limit_price, o.stop_price) for o in ex.open_orders()] == [(2, 62_000.0, 60_000.0)]
    assert plan(tgt, tr, ex.position(), ex.open_orders(), ctx(clock)) == []


def test_contracts_added_by_hand_are_reduced_and_the_trade_keeps_its_size(env):
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    ex.place_market("manual-1", 1, 2)
    acts = plan(target(T0 + 3600, position()), exe.trade(), ex.position(), ex.open_orders(), ctx(clock))
    assert acts[0] == Alert("EXCESS_POSITION", f"trade {T0}: position 5 > 3 expected")
    exe.execute(acts, ctx(clock))
    assert ex.position().contracts == 3 and exe.trade().contracts == 3
    assert sorted(o.size for o in ex.open_orders()) == [1, 2]


def test_a_stop_move_without_quotes_is_sent_once_they_are_back(env):
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    moved = target(T0 + 3600, position(stop=59_500.0))
    exe.execute(plan(moved, exe.trade(), ex.position(), ex.open_orders(), ctx(clock, ratio=None)), ctx(clock, ratio=None))
    assert exe.trade().stop_spot == 59_000.0 and not jr.events(kind="STOP_MOVED")
    exe.execute(plan(moved, exe.trade(), ex.position(), ex.open_orders(), ctx(clock)), ctx(clock))
    assert exe.trade().stop_spot == 59_500.0 and jr.events(kind="STOP_MOVED")[0]["data"]["stop"] == 59_500.0
    assert [o.stop_price for o in ex.open_orders()] == [59_500.0, 59_500.0]


def test_a_partial_tp1_keeps_its_leg_until_the_rest_fills(env):
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 5), ctx(clock))  # TP1 leg 2, TP2 leg 3
    ex.bracket_fill_cap = 1  # the TP1 limit fills one contract at a time

    def poll() -> list[str]:
        seen, events = observe(exe.trade(), ex.position(), ex.open_orders(), clock())
        exe.save(seen)
        exe.execute(plan(target(T0 + 3600, position()), seen, ex.position(), ex.open_orders(), ctx(clock)), ctx(clock))
        return [k for k, _ in events]

    ex.set_prices(61_000.0)
    assert poll() == ["PARTIAL_FILL"] and not exe.trade().tp1_filled
    assert sorted((o.size - o.filled, o.limit_price, o.stop_price) for o in ex.open_orders()) == [
        (1, 61_000.0, 59_000.0), (3, 62_000.0, 59_000.0)]  # nothing replaced
    ex.set_prices(61_000.0)
    assert poll() == ["TP1_FILLED"] and exe.trade().tp1_filled and ex.position().contracts == 3
    assert [(o.size, o.limit_price, o.stop_price) for o in ex.open_orders()] == [(3, 62_000.0, 60_000.0)]


def test_flip_enters_only_after_the_close_is_confirmed(env):
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 1), ctx(clock))
    short = entry(signal_ts=T0 + 3600, direction=-1, stop=61_000.0, tp1=59_000.0, tp2=58_000.0)
    ex.fail_next("close_position", RateLimited("429"))
    exe.execute([Close("FLIP"), Enter(short, 1)], ctx(clock))
    assert ex.position().side == 1 and jr.events(kind="SKIP")[0]["data"]["signal_ts"] == T0 + 3600
    exe2 = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    short2 = entry(signal_ts=T0 + 7200, direction=-1, stop=61_000.0, tp1=59_000.0, tp2=58_000.0)
    exe2.execute([Close("FLIP"), Enter(short2, 1)], ctx(clock))
    assert ex.position().side == -1 and exe2.trade().trade_id == T0 + 7200


def test_skips_and_alerts_are_recorded_once(env):
    clock, ex, jr, alerts, exe = env
    exe.execute([Skip(T0, "paused"), Skip(T0, "paused"), Alert("FOREIGN_ORDERS", "f1"), Alert("FOREIGN_ORDERS", "f1")],
                ctx(clock))
    assert alerts.kinds() == ["SKIP", "FOREIGN_ORDERS"] and len(jr.events(kind="SKIP")) == 1


def test_restart_recovery_rebuilds_an_entry_the_crash_did_not_record(tmp_path):
    clock = Clock()
    ex = sim(clock)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    ex.crash_after_accept("place_market")
    with pytest.raises(SimCrash):
        exe.enter(Enter(entry(), 3), ctx(clock))
    assert jr.intent(f"ewb-e-{T0}-L")["status"] == "PENDING" and exe.trade() is None
    jr.close()
    jr2 = journal(tmp_path)  # restart
    exe2 = Executor(ex, jr2, clock=clock, sleep=clock.sleep)
    out = exe2.recover()
    assert out == {"accepted": 1, "abandoned": 0, "unknown": 0, "rebuilt": 1}
    tr = exe2.trade()
    assert tr.trade_id == T0 and tr.contracts == 3 and ex.open_orders() == []
    acts = plan(None, tr, ex.position(), ex.open_orders(), ctx(clock))
    exe2.execute(acts, ctx(clock))
    assert sorted(o.size for o in ex.open_orders()) == [1, 2] and ex.calls.count("place_market") == 1
    jr2.close()


def test_recovery_abandons_old_intents_that_never_reached_the_venue(env):
    clock, ex, jr, _, exe = env
    jr.record_intent("ewb-x-1-1", "CLOSE", 1, {}, clock() - 700.0)
    jr.record_intent("ewb-x-1-2", "CLOSE", 1, {}, clock() - 10.0)
    ex.fail_next("find_order", Unavailable("503"))
    assert exe.recover() == {"accepted": 0, "abandoned": 0, "unknown": 1, "rebuilt": 0}
    assert exe.recover() == {"accepted": 0, "abandoned": 1, "unknown": 0, "rebuilt": 0}
    assert jr.intent("ewb-x-1-2")["status"] == "PENDING"
    clock.now += 601.0  # while running, the minute job resolves it without a RECOVERY event
    assert exe.resolve_pending()[0]["abandoned"] == 1 and len(jr.events(kind="RECOVERY")) == 2


def test_an_ambiguous_tp1_sync_that_filled_is_never_sold_twice(env):
    """Probe (a): the venue filled the TP1_SYNC reduce, but its answer was lost and the lookup failed. The reduce
    was recorded on the trade before it was sent, so the next poll reads the smaller position as ours: no
    UNEXPLAINED_REDUCTION, no second TP1 sale; the protection planned with the reduce waited for it."""
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    ex.set_prices(60_900.0)  # the engine's TP1 (spot) filled, the live TP1 limit 61_000 did not
    tgt = target(T0 + 3600, position(tp1_done=True))
    ex.crash_after_accept("close_position", Unavailable("read timeout"))  # filled, the answer lost
    ex.fail_next("find_order", Unavailable("503"))  # and the lookup failed: PENDING
    assert poll(exe, ex, clock, tgt) == []
    assert ex.position().contracts == 2 and exe.trade().pending_reduce["cid"] == f"ewb-r-{T0}-1"
    assert sorted(o.size for o in ex.open_orders()) == [1, 2]  # both legs stay until the reduce is known
    clock.now += 10
    assert poll(exe, ex, clock, tgt) == ["REDUCE_SETTLED"]
    tr = exe.trade()
    assert ex.position().contracts == 2 and tr.tp1_filled and tr.pending_reduce is None
    assert ex.calls.count("close_position") == 1 and not jr.events(kind="UNEXPLAINED_REDUCTION")
    assert [(o.size, o.limit_price, o.stop_price) for o in ex.open_orders()] == [(2, 62_000.0, 60_000.0)]


def test_an_ambiguous_reduce_is_resolved_by_its_id_never_sent_under_a_new_one(env):
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    ex.set_prices(60_900.0)
    tgt = target(T0 + 3600, position(tp1_done=True))
    ex.fail_next("close_position", Unavailable("503"))  # failed before it reached the venue
    poll(exe, ex, clock, tgt)
    assert ex.position().contracts == 3 and jr.intent(f"ewb-r-{T0}-1")["status"] == "PENDING"
    assert jr.events(kind="REDUCE_FAILED")[0]["data"]["status"] == "PENDING"
    clock.now += 10
    assert poll(exe, ex, clock, tgt) == ["REDUCE_SETTLED"]  # looked up (not there), sent again under its id
    assert ex.position().contracts == 2 and exe.trade().tp1_filled
    assert jr.intent(f"ewb-r-{T0}-1")["status"] == "ACCEPTED" and jr.intent(f"ewb-r-{T0}-2") is None
    assert [o.client_order_id for o in ex.orders.values()].count(f"ewb-r-{T0}-1") == 1
    assert [(o.size, o.limit_price, o.stop_price) for o in ex.open_orders()] == [(2, 62_000.0, 60_000.0)]


def test_a_vanished_leg_that_filled_is_looked_up_not_placed_again(env):
    """Probe (b): the TP1 leg fills between the position read and the order read. The stale pair plans the TP1 leg
    again; the executor looks the vanished leg up first and does not place a filled one again (a TP1 limit below
    the market would sell TP2 contracts at once); the next poll folds the fill. A leg that left the book unfilled
    (cancelled by hand) is placed again."""
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 5), ctx(clock))  # TP1 leg 2 @ 61_000, TP2 leg 3 @ 62_000
    tgt = target(T0 + 3600, position())
    stale = ex.position()
    ex.set_prices(61_000.0)  # the TP1 leg fills right after the position was read
    orders = ex.open_orders()
    seen, _ = observe(exe.trade(), stale, orders, clock())
    exe.save(seen)
    exe.execute(plan(tgt, seen, stale, orders, ctx(clock)), ctx(clock))
    assert ex.position().contracts == 3 and ex.calls.count("place_bracket") == 2  # not placed again
    assert jr.events(kind="LEG_FILLED")[0]["data"]["leg"] == "tp1"
    clock.now += 10
    assert poll(exe, ex, clock, tgt) == ["TP1_FILLED", "PROTECTED"]
    assert ex.position().contracts == 3
    assert [(o.size, o.limit_price, o.stop_price) for o in ex.open_orders()] == [(3, 62_000.0, 60_000.0)]
    ex.cancel([o.order_id for o in ex.open_orders()])  # cancelled by hand: unfilled
    clock.now += 10
    poll(exe, ex, clock, tgt)
    assert [(o.size, o.limit_price, o.stop_price) for o in ex.open_orders()] == [(3, 62_000.0, 60_000.0)]


def resting(ex) -> list[tuple[int, float, float]]:
    """(contracts left, take-profit, stop) of every resting order, sorted."""
    return sorted((o.size - o.filled, o.limit_price, o.stop_price) for o in ex.open_orders())


@pytest.mark.parametrize("failures", [1, 2])
def test_a_stop_move_never_removes_a_leg_whose_replacement_is_unconfirmed(env, failures):
    """Probe A: a 503 leaves the replacement's outcome unknown (PENDING, not a refusal): the old leg keeps
    protecting - nothing is cancelled first - and the trade keeps the stop in force until every leg moved."""
    clock, ex, jr, _, exe = env
    tr = exe.enter(Enter(entry(), 3), ctx(clock))
    ex.fail_next("place_bracket", Unavailable("503"), times=failures)
    legs, stop = desired_legs(tr, 3, 59_500.0, 1.0, 5.0)
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    moved_tp2 = 59_500.0 if failures == 1 else 59_000.0
    assert resting(ex) == [(1, 61_000.0, 59_000.0), (2, 62_000.0, moved_tp2)]
    assert not jr.events(kind="LEG_GAP") and len(jr.events(kind="BRACKET_FAILED")) == failures
    assert exe.trade().stop_spot == 59_000.0 and not jr.events(kind="STOP_MOVED")
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    assert resting(ex) == [(1, 61_000.0, 59_500.0), (2, 62_000.0, 59_500.0)]
    assert exe.trade().stop_spot == 59_500.0 and len(jr.events(kind="STOP_MOVED")) == 1


def test_an_unconfirmed_replacement_resting_on_the_book_replaces_the_old_leg_at_once(env):
    """Probe P3: the new TP1 leg rests but its answer and the lookup were lost. It is found among the open orders
    right away, so the old TP1 leg is cancelled in the same pass - two TP1 legs resting would sell TP2 contracts
    at the TP1 price."""
    clock, ex, jr, _, exe = env
    tr = exe.enter(Enter(entry(), 3), ctx(clock))
    ex.crash_after_accept("place_bracket", Unavailable("read timeout"))
    ex.fail_next("find_order", Unavailable("503"))
    legs, stop = desired_legs(tr, 3, 59_500.0, 1.0, 5.0)
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    assert resting(ex) == [(1, 61_000.0, 59_500.0), (2, 62_000.0, 59_500.0)]
    assert jr.intent(f"ewb-b-{T0}-tp1-3")["status"] == "ACCEPTED" and not jr.events(kind="BRACKET_FAILED")
    ex.set_prices(61_000.0)
    assert ex.position().contracts == 2  # only the TP1 portion left at TP1


def test_an_unconfirmed_replacement_is_taken_over_on_the_next_pass(env):
    """When the open orders cannot be read either, both TP1 legs rest until the next pass takes the new one over
    by its client order id and terms (LEG_ADOPTED) and cancels the old one - nothing is placed a third time."""
    clock, ex, jr, _, exe = env
    tr = exe.enter(Enter(entry(), 3), ctx(clock))
    ex.crash_after_accept("place_bracket", Unavailable("read timeout"))
    ex.fail_next("find_order", Unavailable("503"))
    real, reads = ex.open_orders, []

    def flaky_reads():
        reads.append(1)
        if len(reads) == 2:  # the check for the unconfirmed leg
            raise Unavailable("503")
        return real()

    ex.open_orders = flaky_reads
    legs, stop = desired_legs(tr, 3, 59_500.0, 1.0, 5.0)
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    ex.open_orders = real
    assert not jr.events(kind="LEG_GAP") and sum(n for n, _, _ in resting(ex)) == 4
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    assert resting(ex) == [(1, 61_000.0, 59_500.0), (2, 62_000.0, 59_500.0)]
    assert ex.calls.count("place_bracket") == 4 and jr.events(kind="LEG_ADOPTED")[0]["data"]["leg"] == "tp1"


def test_a_replacement_that_fails_after_the_cancel_puts_the_old_leg_back(tmp_path):
    """The venue refuses the overlap, the old leg is cancelled first, then the replacement fails (503): the old
    terms are placed again at once, and the next pass completes the move."""
    clock = Clock()
    ex = sim(clock, reject_overlap=True)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    tr = exe.enter(Enter(entry(), 3), ctx(clock))
    real, calls = ex.place_bracket, []

    def flaky(*args, **kw):
        calls.append(args[0])
        if len(calls) == 2:  # the replacement sent after the cancel
            raise Unavailable("503")
        return real(*args, **kw)

    ex.place_bracket = flaky
    legs, stop = desired_legs(tr, 3, 59_500.0, 1.0, 5.0)
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    assert resting(ex) == [(1, 61_000.0, 59_000.0), (2, 62_000.0, 59_500.0)]  # TP1 restored, TP2 moved
    assert len(jr.events(kind="LEG_RESTORED")) == 1 and exe.trade().stop_spot == 59_000.0
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    assert resting(ex) == [(1, 61_000.0, 59_500.0), (2, 62_000.0, 59_500.0)] and exe.trade().stop_spot == 59_500.0
    jr.close()


def test_a_replacement_refused_for_its_prices_keeps_the_old_leg(tmp_path):
    """A refusal that names a price (a stop through the market, an off-grid level) would refuse the replacement
    again: the old leg - maybe already triggered and filling - is never cancelled first, even on a venue that
    refuses overlapping brackets."""
    clock = Clock()
    ex = sim(clock, reject_overlap=True)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    tr = exe.enter(Enter(entry(), 3), ctx(clock))
    legs, _ = desired_legs(tr, 3, 59_500.0, 1.0, 5.0)
    bad = tuple(Leg(leg.name, leg.size, leg.limit_price, 59_501.0) for leg in legs)  # off the $5 grid
    exe.protect(Protect(bad, 59_500.0, 59_501.0), ctx(clock))
    assert resting(ex) == [(1, 61_000.0, 59_000.0), (2, 62_000.0, 59_000.0)]
    assert not jr.events(kind="LEG_GAP") and len(jr.events(kind="BRACKET_FAILED")) == 2
    assert exe.trade().stop_price == 59_000.0
    jr.close()


@pytest.mark.parametrize("reject_overlap", [False, True])
def test_brackets_accepted_before_a_crash_are_taken_over_after_the_restart(tmp_path, reject_overlap):
    """Probe C: killed right after the second bracket was accepted - both legs rest, the trade does not know
    them. After the restart they are taken over by their client order id and terms (on a venue that refuses
    overlapping brackets, placing new ones first would fail and cancelling them would leave it unprotected)."""
    clock = Clock()
    ex = sim(clock, reject_overlap=reject_overlap)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    real, calls = ex.place_bracket, []

    def crash_on_second(*args, **kw):
        calls.append(args[0])
        if len(calls) == 2:
            ex.crash_after_accept("place_bracket")
        return real(*args, **kw)

    ex.place_bracket = crash_on_second
    with pytest.raises(SimCrash):
        exe.enter(Enter(entry(), 3), ctx(clock))
    ex.place_bracket = real
    jr.close()
    jr2 = journal(tmp_path)
    exe2 = Executor(ex, jr2, clock=clock, sleep=clock.sleep)
    exe2.recover()  # probe N6: the trade's resting brackets are its legs again - never read as unprotected
    assert sorted(e["data"]["leg"] for e in jr2.events(kind="LEG_ADOPTED")) == ["tp1", "tp2"]
    assert jr2.events(kind="PROTECTED_FIRST")
    assert poll(exe2, ex, clock, target(T0 + 3600, position())) == []
    assert resting(ex) == [(1, 61_000.0, 59_000.0), (2, 62_000.0, 59_000.0)]
    assert ex.calls.count("place_bracket") == 2 and not jr2.events(kind="BRACKET_FAILED")
    assert [leg.client_order_id for leg in exe2.trade().legs] == [f"ewb-b-{T0}-tp1-1", f"ewb-b-{T0}-tp2-2"]
    jr2.close()


def test_a_failed_close_keeps_the_protection(env):
    """Probe D: a close that fails (429: outcome unknown) never cancelled the brackets, so a close that keeps
    failing never leaves the position unprotected."""
    clock, ex, jr, alerts, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    ex.fail_next("close_position", RateLimited("429"))
    assert exe.close("TIME", ctx(clock)) is False and "CLOSE_FAILED" in alerts.kinds()
    assert sorted(o.client_order_id for o in ex.open_orders()) == [f"ewb-b-{T0}-tp1-1", f"ewb-b-{T0}-tp2-2"]
    assert "cancel" not in ex.calls
    assert poll(exe, ex, clock, target(T0 + 3600, position(), exit_reason="TIME")) == []  # never seen unprotected
    assert ex.position() is None and exe.trade().exit_reason == "TIME"


def test_a_partly_filled_close_returns_at_once_and_protects_the_rest(env):
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    ex.market_fill_ratio = 0.5  # the IOC close fills 1 of 3, its rest is cancelled
    start = clock()
    assert exe.close("TIME", ctx(clock)) is False and jr.events(kind="CLOSE_UNCONFIRMED")
    assert clock() - start < 5.0  # the close order ended: no 30 s wait for a flat position that cannot come
    assert ex.position().contracts == 2 and resting(ex) == [(1, 61_000.0, 59_000.0), (1, 62_000.0, 59_000.0)]


def test_an_edit_keeps_what_a_partly_filled_leg_has_left(tmp_path):
    """Probe E: orders/edit takes the order's total size: a TP1 leg of 2 with 1 filled is edited to 2 (1 left),
    never to 1 (which would end it and leave a contract unprotected)."""
    clock = Clock()
    ex = sim(clock, edit_supported=True)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep, edit_stops=True)
    exe.enter(Enter(entry(), 5), ctx(clock))  # TP1 leg 2, TP2 leg 3
    ex.bracket_fill_cap = 1
    ex.set_prices(61_000.0)
    ex.set_prices(60_500.0)
    ex.bracket_fill_cap = None
    seen, events = observe(exe.trade(), ex.position(), ex.open_orders(), clock())
    exe.save(seen)
    assert [k for k, _ in events] == ["PARTIAL_FILL"] and ex.position().contracts == 4
    legs, stop = desired_legs(exe.trade(), 4, 59_500.0, 1.0, 5.0)
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    assert resting(ex) == [(1, 61_000.0, 59_500.0), (3, 62_000.0, 59_500.0)]
    assert ex.calls.count("place_bracket") == 2 and len(jr.events(kind="STOP_EDITED")) == 2
    jr.close()


def test_a_signal_skipped_once_is_never_entered_later(env):
    """Probe F: the flip's close failed, so the new signal was skipped (alerted); on the next pass the close goes
    through, and the skipped entry is not sent late (never chase)."""
    clock, ex, jr, alerts, exe = env
    exe.enter(Enter(entry(), 1), ctx(clock))
    short = entry(signal_ts=T0 + 3600, direction=-1, stop=61_000.0, tp1=59_000.0, tp2=58_000.0)
    ex.fail_next("close_position", RateLimited("429"))
    exe.execute([Close("FLIP"), Enter(short, 1)], ctx(clock))
    clock.now += 10
    poll(exe, ex, clock, target(T0 + 3600, position(), exit_reason="FLIP", ent=short))
    assert ex.position() is None and ex.calls.count("place_market") == 1
    assert jr.intent(f"ewb-e-{T0 + 3600}-S") is None and alerts.kinds().count("SKIP") == 1


def test_recovery_records_a_close_accepted_before_the_crash(tmp_path):
    """Probe G: killed while waiting for the flat position after the close was accepted: the restart records
    the close with its reason (not as a bracket exit)."""
    clock = Clock()
    ex = sim(clock)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    exe.enter(Enter(entry(), 1), ctx(clock))

    def killed(*_args):
        raise SimCrash("killed during the flat wait")

    exe._await_flat = killed
    with pytest.raises(SimCrash):
        exe.close("TIME", ctx(clock))
    assert jr.intent(f"ewb-x-{T0}-1")["status"] == "ACCEPTED" and ex.position() is None
    jr.close()
    jr2 = journal(tmp_path)
    exe2 = Executor(ex, jr2, clock=clock, sleep=clock.sleep)
    exe2.recover()
    tr = exe2.trade()
    assert (tr.status, tr.exit_reason) == ("CLOSED", "TIME")
    closed = jr2.events(kind="CLOSED")[0]["data"]
    assert closed["recovered"] and closed["reason"] == "TIME" and closed["price"] == 59_997.5
    jr2.close()


def test_a_rebuilt_trade_measures_protection_from_its_fill(tmp_path):
    """Probe H: an entry rebuilt after a 5-minute outage was unprotected for 5 minutes: PROTECTED_FIRST says so
    (PROTECT_SLOW, CANARY gate 1), it does not start the clock at the restart."""
    clock = Clock()
    ex = sim(clock)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    ex.crash_after_accept("place_market")
    with pytest.raises(SimCrash):
        exe.enter(Enter(entry(), 3), ctx(clock))
    jr.close()
    clock.now += 300
    jr2 = journal(tmp_path)
    exe2 = Executor(ex, jr2, clock=clock, sleep=clock.sleep)
    exe2.recover()
    exe2.execute(plan(None, exe2.trade(), ex.position(), ex.open_orders(), ctx(clock)), ctx(clock))
    assert jr2.events(kind="PROTECTED_FIRST")[0]["data"]["after_entry_s"] == 300.0
    jr2.close()


def test_a_foreign_position_after_our_trade_closed_is_adopted_under_fresh_ids(env):
    """probe3: our 1-contract trade left at its TP1; a manual position in the engine's direction follows while
    the engine still holds that trade. It is a foreign position (ADOPTED, its own entry price), not our entry
    rebuilt from its intent, and its brackets go on from the journal's ids (a reused id would resolve to the
    filled TP1 leg and leave the position unprotected)."""
    clock, ex, jr, alerts, exe = env
    exe.enter(Enter(entry(), 1), ctx(clock))
    ex.set_prices(61_000.0)
    assert poll(exe, ex, clock, target(T0 + 3600, position())) == ["CLOSED"]
    ex.set_prices(60_500.0)
    ex.place_market("manual-1", 1, 1)
    clock.now += 60
    poll(exe, ex, clock, target(T0 + 3600, position()))
    tr = exe.trade()
    assert (tr.status, tr.entry_price, tr.gen) == ("OPEN", 60_502.5, 2)
    assert [o.client_order_id for o in ex.open_orders()] == [f"ewb-b-{T0}-tp1-2"]
    assert "ADOPTED" in alerts.kinds() and "RECOVERED_ENTRY" not in alerts.kinds()


def test_an_own_entry_still_pending_is_looked_up_before_a_position_is_called_foreign(env):
    clock, ex, jr, alerts, exe = env
    ex.crash_after_accept("place_market", Unavailable("read timeout"))  # filled, the answer lost
    ex.fail_next("find_order", Unavailable("503"))  # and the lookup failed: PENDING
    assert exe.enter(Enter(entry(), 1), ctx(clock)) is None and jr.intent(f"ewb-e-{T0}-L")["status"] == "PENDING"
    poll(exe, ex, clock, target(T0 + 3600, ent=entry()))
    assert exe.trade().trade_id == T0 and len(ex.open_orders()) == 1
    assert "RECOVERED_ENTRY" in alerts.kinds() and "ADOPTED" not in alerts.kinds()


def test_a_foreign_position_without_quotes_is_covered_at_the_spot_levels_until_they_are_back(env):
    """Probe N7: no quotes to map the engine levels: the position is protected at once at the spot levels (ratio
    1.0, alerted as approximate) and re-mapped at the first pass with quotes."""
    clock, ex, jr, alerts, exe = env
    ex.place_market("manual-1", 1, 2)
    exe.execute([Adopt(position(), 2)], ctx(clock, ratio=None))
    assert resting(ex) == [(1, 61_000.0, 59_000.0), (1, 62_000.0, 59_000.0)]
    assert "ADOPTED" in alerts.kinds() and "LEVELS_APPROX" in alerts.kinds()
    c = ctx(clock, ratio=1.001)
    for _ in range(2):
        exe.execute(plan(target(T0 + 3600, position()), exe.trade(), ex.position(), ex.open_orders(), c), c)
    assert resting(ex) == [(1, 61_060.0, 59_055.0), (1, 62_060.0, 59_055.0)]
    assert len(jr.events(kind="LEVELS_MAPPED")) == 1


def test_alerts_are_once_per_trade_and_the_journal_keys_stay_bounded(env):
    clock, ex, jr, alerts, exe = env
    for signal in (T0, T0 + 7200):
        exe.enter(Enter(entry(signal_ts=signal), 1), ctx(clock))
        for _ in range(2):
            exe.close("LINEAGE", ctx(clock, session_open=False))
        assert exe.close("LINEAGE", ctx(clock)) is True
    assert alerts.kinds().count("CLOSE_DEFERRED") == 2  # once for each trade
    for k in range(50):
        exe.execute([Alert("FOREIGN_ORDERS", f"f{k}")], ctx(clock))
    keys = [r[0] for r in jr._exec("SELECT key FROM kv").fetchall()]
    assert len(keys) <= 4 and not [k for k in keys if k.startswith(("alerted:", "leg_filled:"))]


def test_recovery_survives_an_unreadable_exchange(tmp_path):
    clock = Clock()
    ex = sim(clock)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    ex.crash_after_accept("place_market")
    with pytest.raises(SimCrash):
        exe.enter(Enter(entry(), 1), ctx(clock))
    ex.fail_next("get_order", Unavailable("503"))
    assert exe.recover() == {"accepted": 1, "abandoned": 0, "unknown": 1, "rebuilt": 0}
    assert jr.events(kind="RECOVERY_ERROR") and exe.trade() is None
    assert exe.recover()["rebuilt"] == 1 and exe.trade().trade_id == T0
    jr.close()


def entry_disabled(ex, reason: str = "ORDER_ENTRY_DISABLED") -> None:
    """The venue refuses every new order with `reason` (cancel-only / order entry disabled); cancels still work."""
    def refuse(name):
        def call(cid, *args, **kw):
            ex.calls.append(name)
            return PlaceResult(cid, None, False, f"{reason}: new orders are not accepted")
        return call
    ex.place_bracket = refuse("place_bracket")
    ex.close_position = refuse("close_position")


@pytest.mark.parametrize("reject_overlap", [False, True])
@pytest.mark.parametrize("reason", ["ORDER_ENTRY_DISABLED", "UNKNOWN_FAILURE_REASON"])
def test_a_venue_refusing_every_new_order_never_loses_the_old_legs(tmp_path, reason, reject_overlap):
    """Probes P1, P1b: a refusal that is not a known overlap refusal never cancels the old leg first."""
    clock = Clock()
    ex = sim(clock, reject_overlap=reject_overlap)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    tr = exe.enter(Enter(entry(), 3), ctx(clock))
    entry_disabled(ex, reason)
    legs, stop = desired_legs(tr, 3, 59_500.0, 1.0, 5.0)
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    assert resting(ex) == [(1, 61_000.0, 59_000.0), (2, 62_000.0, 59_000.0)]
    assert not jr.events(kind="LEG_GAP") and len(jr.events(kind="BRACKET_FAILED")) == 2
    clock.now += 10
    assert observe(exe.trade(), ex.position(), ex.open_orders(), clock())[1] == []
    jr.close()


def test_an_overlap_refusal_without_an_overlap_keeps_the_old_leg(env):
    """A known overlap refusal while our brackets plus the new leg would not exceed the position is not an overlap:
    the old leg is not cancelled first."""
    clock, ex, jr, _, exe = env
    tr = exe.enter(Enter(entry(), 3), ctx(clock))
    ex.cancel([o.order_id for o in ex.open_orders() if "-tp2-" in o.client_order_id])  # by hand: 1 of 3 covered
    entry_disabled(ex, "INSUFFICIENT_POSITION_SIZE")
    legs, stop = desired_legs(tr, 3, 59_500.0, 1.0, 5.0)
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    assert resting(ex) == [(1, 61_000.0, 59_000.0)] and not jr.events(kind="LEG_GAP")


def test_a_close_refused_by_a_venue_taking_no_new_orders_keeps_the_brackets(env):
    """Probe P2: cancels work but new orders are refused - the close fails and the brackets were never touched."""
    clock, ex, jr, alerts, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    entry_disabled(ex)
    assert exe.close("TIME", ctx(clock)) is False and "CLOSE_FAILED" in alerts.kinds()
    assert resting(ex) == [(1, 61_000.0, 59_000.0), (2, 62_000.0, 59_000.0)] and "cancel" not in ex.calls
    assert not jr.events(kind="BRACKET_FAILED")


def bracket_conflict(ex, closing_session: bool = False) -> None:
    """The venue refuses close_position while brackets of ours rest (a known close-conflict refusal)."""
    real = ex.close_position

    def call(cid, size=None):
        if any(o.kind == "BRACKET" for o in ex.open_orders()):
            ex.calls.append("close_position")
            if closing_session:
                ex.set_session("CLOSE")
            return PlaceResult(cid, None, False, "OPEN_BRACKET_ORDERS")
        return real(cid, size)

    ex.close_position = call


def test_a_close_refused_for_resting_brackets_cancels_them_then_closes(env):
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    bracket_conflict(ex)
    assert exe.close("TIME", ctx(clock)) is True and ex.position() is None
    assert ex.calls.count("close_position") == 2 and not jr.events(kind="CLOSE_FAILED")
    assert jr.events(kind="CLOSED")[0]["data"]["reason"] == "TIME"


def test_a_close_conflict_while_the_session_closes_keeps_the_brackets(env):
    """The session is read right before the brackets would be cancelled: not OPEN -> the close waits, protected."""
    clock, ex, jr, alerts, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    bracket_conflict(ex, closing_session=True)
    assert exe.close("TIME", ctx(clock)) is False and "CLOSE_DEFERRED" in alerts.kinds()
    assert resting(ex) == [(1, 61_000.0, 59_000.0), (2, 62_000.0, 59_000.0)] and "cancel" not in ex.calls


def failing_lookups(ex, clock: Clock, until: float) -> None:
    """find_order raises (the list-orders query is degraded) until `until`."""
    real = ex.find_order

    def call(cid, since):
        if clock() < until:
            ex.calls.append("find_order")
            raise Unavailable("503")
        return real(cid, since)

    ex.find_order = call


def test_an_unconfirmed_own_entry_is_protected_at_once_and_settled_as_ours(env):
    """Probe N1: our entry filled, its answer was lost and the lookups fail for a minute. The position is protected
    from the first pass with a provisional trade built from the entry's intent (its levels, the position's size and
    price); once the lookup answers, it is settled as our entry - never a foreign position."""
    clock, ex, jr, alerts, exe = env
    ex.crash_after_accept("place_market", Unavailable("read timeout"))
    failing_lookups(ex, clock, clock() + 65)
    assert exe.enter(Enter(entry(), 3), ctx(clock)) is None and jr.intent(f"ewb-e-{T0}-L")["status"] == "PENDING"
    tgt = target(T0 + 3600, ent=entry())
    for _ in range(6):
        clock.now += 10
        poll(exe, ex, clock, tgt)
        assert resting(ex) == [(1, 61_000.0, 59_000.0), (2, 62_000.0, 59_000.0)]
        assert exe.trade().trade_id == T0 and "RECOVERED_ENTRY" not in alerts.kinds()
    assert jr.events(kind="ADOPTED_PROVISIONAL")[0]["data"]["contracts"] == 3
    assert jr.events(kind="PROTECTED_FIRST")[0]["data"]["after_entry_s"] == 10.0
    clock.now += 10
    poll(exe, ex, clock, tgt)
    assert "RECOVERED_ENTRY" in alerts.kinds() and "ADOPTED" not in alerts.kinds()
    assert jr.intent(f"ewb-e-{T0}-L")["status"] == "ACCEPTED" and ex.calls.count("place_bracket") == 2


def test_a_provisional_trade_whose_entry_never_reached_the_venue_is_settled_as_foreign(env):
    clock, ex, jr, alerts, exe = env
    ex.fail_next("place_market", Unavailable("503"))  # never reached the venue
    failing_lookups(ex, clock, clock() + 30)
    assert exe.enter(Enter(entry(), 1), ctx(clock)) is None and jr.intent(f"ewb-e-{T0}-L")["status"] == "PENDING"
    ex.place_market("manual-1", 1, 1)  # a manual position in the engine's direction
    tgt = target(T0 + 3600, ent=entry())
    clock.now += 10
    poll(exe, ex, clock, tgt)
    assert len(ex.open_orders()) == 1 and "ADOPTED" not in alerts.kinds()
    clock.now += 30
    poll(exe, ex, clock, tgt)
    assert "ADOPTED" in alerts.kinds() and "RECOVERED_ENTRY" not in alerts.kinds() and len(ex.open_orders()) == 1


def test_a_bracket_whose_cancel_is_queued_is_not_taken_over(tmp_path):
    """Probe N5: a resting bracket of ours with a queued cancel is about to leave the book - never a leg."""
    clock = Clock()
    ex = sim(clock)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    real, calls = ex.place_bracket, []

    def crash_on_second(*args, **kw):
        calls.append(args[0])
        if len(calls) == 2:
            ex.crash_after_accept("place_bracket")
        return real(*args, **kw)

    ex.place_bracket = crash_on_second
    with pytest.raises(SimCrash):
        exe.enter(Enter(entry(), 3), ctx(clock))
    ex.place_bracket = real
    queued = f"ewb-b-{T0}-tp1-1"
    real_open = ex.open_orders
    ex.open_orders = lambda: [dataclasses.replace(o, status="CANCEL_QUEUED") if o.client_order_id == queued else o
                              for o in real_open()]
    exe.recover()
    poll(exe, ex, clock, target(T0 + 3600, position()))
    assert [e["data"]["leg"] for e in jr.events(kind="LEG_ADOPTED")] == ["tp2"]
    assert queued not in [leg.client_order_id for leg in exe.trade().legs]
    jr.close()


def test_an_accepted_entry_whose_fill_stays_unreadable_is_protected_at_once(env):
    """Probe R1: get_order is down, so the fill of our accepted entry cannot be read - neither in the fill wait nor on
    the next passes. The position is protected from the first pass with a provisional trade from the entry's intent
    (alerted), never left without a trade or treated as foreign."""
    clock, ex, jr, alerts, exe = env
    ex.fail_next("get_order", Unavailable("503"), times=200)
    assert exe.enter(Enter(entry(), 3), ctx(clock)) is None and "ENTRY_UNCONFIRMED" in alerts.kinds()
    for _ in range(4):
        clock.now += 10
        poll(exe, ex, clock, target(T0 + 3600, ent=entry()))
        assert exe.trade().trade_id == T0
        assert resting(ex) == [(1, 61_000.0, 59_000.0), (2, 62_000.0, 59_000.0)]
    assert "ADOPTED" not in alerts.kinds() and "ACTION_ERROR" not in alerts.kinds()
    assert "RECOVERED_ENTRY" in alerts.kinds() and ex.calls.count("place_market") == 1


def test_a_close_that_fails_after_a_conflict_cancel_is_protected_again_on_later_passes(env):
    """Probe R2 (Q1): the close is refused while the brackets rest, they are cancelled, the resent close and the
    re-protection both hit a 429 storm. Every later failed close puts the legs back - not only the one that cancelled."""
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    real, calls = ex.close_position, []

    def conflict_then_429(cid, size=None):
        calls.append(cid)
        ex.calls.append("close_position")
        if len(calls) == 1:
            return PlaceResult(cid, None, False, "OPEN_BRACKET_ORDERS")
        raise RateLimited("429")

    ex.close_position = conflict_then_429
    ex.fail_next("place_bracket", RateLimited("429"), times=2)
    tgt = target(T0 + 3600, position(), exit_reason="TIME")
    clock.now += 10
    poll(exe, ex, clock, tgt)
    assert ex.open_orders() == [] and len(jr.events(kind="BRACKET_FAILED")) == 2
    clock.now += 10
    poll(exe, ex, clock, tgt)  # the close fails again: the legs go back although this call cancelled nothing
    assert resting(ex) == [(1, 61_000.0, 59_000.0), (2, 62_000.0, 59_000.0)]
    ex.close_position = real
    clock.now += 10
    poll(exe, ex, clock, tgt)
    assert ex.position() is None and exe.trade().exit_reason == "TIME"


def covered_refusal(ex) -> None:
    """Coinbase-style: a close or reduce is refused while our brackets already cover what it would close."""
    real = ex.close_position

    def call(cid, size=None):
        pos = ex.pos
        cover = sum(o.size - o.filled for o in ex.orders.values() if o.kind == "BRACKET" and o.status == "OPEN")
        if pos is not None and cover + (pos.contracts if size is None else size) > pos.contracts:
            ex.calls.append("close_position")
            return PlaceResult(cid, None, False, "ORDER_SIZE_EXCEEDS_BRACKETED_POSITION")
        return real(cid, size)

    ex.close_position = call


def test_a_tp1_sync_refused_while_the_brackets_cover_the_position_cancels_the_tp1_leg_first(env):
    """Probe R3 (Q3): the venue refuses the TP1_SYNC reduce because the legs cover the position. The TP1 leg (the
    contracts the reduce sells) is cancelled, the reduce resent under a new id, and the rest goes to break-even."""
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    covered_refusal(ex)
    ex.set_prices(60_900.0)
    clock.now += 10
    poll(exe, ex, clock, target(T0 + 3600, position(tp1_done=True, stop=60_000.0)))
    assert ex.position().contracts == 2 and exe.trade().tp1_filled
    assert resting(ex) == [(2, 62_000.0, 60_000.0)]
    assert not jr.events(kind="REDUCE_FAILED") and jr.events(kind="REDUCE_CONFLICT")
    assert jr.intent(f"ewb-r-{T0}-1")["status"] == "REJECTED" and jr.intent(f"ewb-r-{T0}-2")["status"] == "ACCEPTED"


def test_an_excess_refused_while_the_brackets_cover_it_shrinks_the_legs_first(env):
    """Probe R3 (Q4): contracts added by hand while the session was closed are covered by the legs; at the reopen the
    EXCESS reduce is refused for it. The legs shrink to the trade's size (placed first), then the reduce goes out."""
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    ex.place_market("manual-1", 1, 2)
    closed = ctx(clock, session_open=False)
    seen, _ = observe(exe.trade(), ex.position(), ex.open_orders(), clock())
    exe.save(seen)
    exe.execute(plan(target(T0 + 3600, position()), seen, ex.position(), ex.open_orders(), closed), closed)
    assert sum(n for n, _, _ in resting(ex)) == 5
    covered_refusal(ex)
    clock.now += 10
    poll(exe, ex, clock, target(T0 + 3600, position()))
    assert ex.position().contracts == 3 and resting(ex) == [(1, 61_000.0, 59_000.0), (2, 62_000.0, 59_000.0)]
    assert not jr.events(kind="REDUCE_FAILED") and exe.trade().contracts == 3


def test_a_margin_reduce_refused_while_the_brackets_cover_it_resizes_the_legs_first(env):
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    covered_refusal(ex)
    assert exe.reduce(1, "MARGIN", ctx(clock)) is True
    assert ex.position().contracts == 2 and exe.trade().contracts == 2
    assert resting(ex) == [(1, 61_000.0, 59_000.0), (1, 62_000.0, 59_000.0)] and not jr.events(kind="REDUCE_FAILED")


def test_a_resent_reduce_that_fails_puts_the_shrunk_legs_back(env):
    """Never a gap: the TP1 leg was cancelled to make room, the resent reduce's outcome is unknown (429) - the leg goes
    back at once so the whole position stays covered (a reduce that did fill only leaves an extra reduce-only leg)."""
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    covered_refusal(ex)
    ex.set_prices(60_900.0)
    refusing = ex.close_position

    def refuse_then_429(cid, size=None):
        if cid.endswith("-2"):
            ex.calls.append("close_position")
            raise RateLimited("429")
        return refusing(cid, size)

    ex.close_position = refuse_then_429
    clock.now += 10
    poll(exe, ex, clock, target(T0 + 3600, position(tp1_done=True, stop=60_000.0)))
    assert ex.position().contracts == 3 and sum(n for n, _, _ in resting(ex)) == 3
    assert exe.trade().pending_reduce["cid"] == f"ewb-r-{T0}-2"


def test_a_flip_waits_while_a_bracket_of_the_closed_position_still_rests(env):
    """Probe R4: the close is confirmed but a bracket of ours could not be cancelled (or not read): the opposite entry is
    skipped (recorded once), never sent next to a bracket of the old side."""
    clock, ex, jr, alerts, exe = env
    exe.enter(Enter(entry(), 1), ctx(clock))
    real_open = ex.open_orders
    stray = Order("sim-x", f"ewb-b-{T0}-tp1-9", -1, "BRACKET", 1, 0, "OPEN", 61_000.0, 59_000.0)
    ex.open_orders = lambda: real_open() + ([stray] if ex.pos is None else [])
    short = entry(signal_ts=T0 + 3600, direction=-1, stop=61_000.0, tp1=59_000.0, tp2=58_000.0)
    exe.execute([Close("FLIP"), Enter(short, 1)], ctx(clock))
    assert ex.position() is None and jr.intent(f"ewb-e-{T0 + 3600}-S") is None
    assert jr.events(kind="SKIP")[0]["data"]["signal_ts"] == T0 + 3600 and alerts.kinds().count("SKIP") == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --group live pytest tests/live/test_executor.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'ewbot.live.executor'`.

- [ ] **Step 3: Write minimal implementation**

**File: `src/ewbot/live/executor.py`**

```python
"""Executes reconcile actions against the exchange with intent-before-send (Plan 3 spec 4.2, 5.1).

Every order goes through `send`: the intent (deterministic client_order_id, kind, payload) is committed
to the journal as PENDING before the request; the answer marks it ACCEPTED or REJECTED. An ambiguous
failure (429 / 5xx / timeout / process killed) leaves it PENDING, and a PENDING intent is looked up on
the exchange by client_order_id before it is ever sent again - so a restart never doubles an order.

Entries are market IOC orders; the fill is awaited (10 s) and the trade is protected at once with
trigger_bracket_gtc legs. Working protection is never removed before its replacement is in place. Stop
moves edit the resting bracket when `edit_stops` is on (unconfirmed for CFM brackets: off until CANARY
confirms it; the edit sends the order's total size, filled + left), otherwise the new bracket is placed
first and the old one cancelled (brackets only reduce, so a short overlap cannot over-close). Only when
the venue refuses the replacement with a known overlap refusal (`OVERLAP_REFUSALS`, reason codes to be
confirmed in CANARY) and our brackets plus the new leg really exceed the position is the old one
cancelled first - LEG_GAP, the designed path on such a venue and not an order failure - and when the
replacement then fails as well, the old terms are placed again at once (LEG_RESTORED). Any other refusal
(order entry disabled, a price refused, an unknown reason) or an unknown outcome (PENDING: 429 / 5xx /
timeout) keeps the old leg; an unconfirmed new leg that rests on the book (its answer lost) is found
among the open orders at once, so two legs of one name never rest for long. A resting bracket of ours
the trade does not list (accepted right before a crash, an unconfirmed replacement) is taken over when
its client order id names the trade and leg and its terms are the wanted ones (LEG_ADOPTED), else
replaced like a leg of the trade - never one whose cancel is queued; other brackets of ours are cancelled
only once every wanted leg is in place, and the trade records a new stop only once every leg rests at it
(STOP_MOVED). A leg that left the book is looked up before it is placed again: one with fills is not
placed again for LEG_SETTLE_S (the position read may not show the fill yet, and a take-profit placed
again below the market would sell at once what the leg already sold) - the next observe folds the fill.

Closes never cancel protection before the venue took the close: close_position goes out with the brackets
resting (both only reduce); only a known close-conflict refusal (`CLOSE_CONFLICTS`: the venue refuses a
close while brackets rest) cancels them - after a session read, deferring while it is not OPEN - and the
close is sent again at once. The flat position is awaited (30 s; at once over when the IOC close ended
without closing everything). After every close that did not go flat - failed or unconfirmed, this pass or a
later one - the legs are made the ones for the position left (resized, or placed again when none rests), so
a failing close never leaves the position unprotected; an entry after a close (a flip) or a cancel waits
until no bracket of ours rests, else it is skipped. Reductions (close_position
with a size) are recorded on the trade before they are sent (`LiveTrade.pending_reduce`): a reduce with
an unknown outcome is resolved by its client order id (`resolve_reduce`, before every read of the exchange
state) and never followed by a second one, and a Protect planned with a reduce waits until the reduce is
known to have filled. A reduce the venue refuses while our brackets cover what it would close
(`CLOSE_CONFLICTS`) first makes room - TP1_SYNC cancels the TP1 leg (the contracts it sells), EXCESS / MARGIN
resize the legs to the size after it (placed first) - and goes out again under a new reduce id; when that does
not fill, the legs go back for the whole position. Market orders wait for an OPEN session; a deferred close is retried on every pass
(spec 4.5: `휴장 중에 필요해진 청산은 재개장 직후`). An entry skipped once is never sent later (never chase).
Client order ids are never reused: a trade rebuilt or taken over under the id of an earlier one goes on
from the journal's highest bracket generation and close number. A position whose entry outcome is still
unknown (our entry PENDING and its lookup failing) or whose fill cannot be read (our entry ACCEPTED, get_order
failing) is protected at once with a provisional trade built from the entry's intent, settled as ours or
foreign once the outcome is known; a foreign position without quotes
is protected at the spot levels (ratio 1.0, approximate) and re-mapped at the first pass with quotes. At
restart the trade's resting brackets are its legs again (no false UNPROTECTED).

The same code runs every stage: in SHADOW the exchange is a ShadowExchange (virtual fills, real
previews)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from ewbot.engine.position_mgr import _tp1_split
from ewbot.live.exchange import Exchange, ExchangeError, Order, PlaceResult, Position
from ewbot.live.journal import Journal
from ewbot.live.pricing import map_levels
from ewbot.live.reconcile import (
    PREFIX,
    Action,
    Adopt,
    Alert,
    Cancel,
    Close,
    Enter,
    Leg,
    LiveTrade,
    PlanContext,
    Protect,
    Reduce,
    Skip,
    after_reduce,
    bracket_cid,
    close_cid,
    desired_legs,
    entry_cid,
    expected_contracts,
    is_ours,
    reduce_cid,
)
from ewbot.live.target import TargetPosition

FILL_TIMEOUT_S = 10.0
FLAT_TIMEOUT_S = 30.0
POLL_S = 0.5
LOOKUP_WINDOW_S = 300.0  # find_order searches orders created up to 5 min before the intent
ABANDON_AFTER_S = 600.0  # a PENDING intent not found on the exchange after 10 min was never sent
LEG_SETTLE_S = 30.0  # a leg that left the book with fills is not placed again for this long (the position catches up)
CLEAR_TIMEOUT_S = 5.0  # an entry after a close or a cancel waits this long for our brackets to leave the book
TRADE_KEY = "trade"
SKIPS_KEY = "skipped_signals"
ORPHAN_KEY = "orphan_closes"
SCRATCH_KEY = "trade_scratch"  # one trade's bookkeeping (alerts sent once, legs seen filled): replaced with the trade
CANCEL_QUEUED = "CANCEL_QUEUED"  # still on the book, about to leave it: never a leg
# Refusal reason codes (the first token of PlaceResult.error), to be confirmed in CANARY (Task 16 step 8):
# a bracket refused because our brackets would exceed the position (SimExchange: INSUFFICIENT_POSITION_SIZE)...
OVERLAP_REFUSALS = frozenset({"INSUFFICIENT_POSITION_SIZE", "BRACKET_ORDER_SIZE_EXCEEDS_POSITION",
                              "PREVIEW_BRACKET_ORDER_SIZE_EXCEEDS_POSITION"})
# ...and a close refused because brackets rest on the position
CLOSE_CONFLICTS = frozenset({"OPEN_BRACKET_ORDERS", "ORDER_SIZE_EXCEEDS_BRACKETED_POSITION",
                             "PREVIEW_ORDER_SIZE_EXCEEDS_BRACKETED_POSITION"})

Alerter = Callable[[str, str], None]


@dataclass(frozen=True)
class Sent:
    status: str  # ACCEPTED | REJECTED | PENDING
    order_id: str | None = None
    error: str | None = None


def _side(d: int) -> str:
    return "롱" if d > 0 else "숏"


def _terms(o: Order) -> tuple[int, float | None, float | None]:
    """What a resting bracket still covers, at which take-profit and stop."""
    return o.size - o.filled, o.limit_price, o.stop_price


def _code(sent: Sent) -> str | None:
    """The venue's reason code of a refusal (PlaceResult.error is `CODE` or `CODE: message`); None otherwise."""
    if sent.status != "REJECTED" or not sent.error:
        return None
    return str(sent.error).split(":", 1)[0].strip()


def _leg_of(name: str, o: Order) -> Leg:
    """The trade's record of a resting bracket of ours, with the order's own terms."""
    return Leg(name, o.size - o.filled, float(o.limit_price or 0.0), float(o.stop_price or 0.0), o.client_order_id,
               o.order_id)


class Executor:
    def __init__(
        self,
        exchange: Exchange,
        journal: Journal,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        alert: Alerter | None = None,
        tp1_frac: float = 0.5,
        edit_stops: bool = False,
        fill_timeout: float = FILL_TIMEOUT_S,
        flat_timeout: float = FLAT_TIMEOUT_S,
        poll_s: float = POLL_S,
        overlap_refusals: Iterable[str] = OVERLAP_REFUSALS,
        close_conflicts: Iterable[str] = CLOSE_CONFLICTS,
    ) -> None:
        self.ex = exchange
        self.journal = journal
        self.clock = clock
        self.sleep = sleep
        self.alert = alert or (lambda kind, text: None)
        self.tp1_frac = tp1_frac
        self.edit_stops = edit_stops
        self.fill_timeout = fill_timeout
        self.flat_timeout = flat_timeout
        self.poll_s = poll_s
        self.overlap_refusals = frozenset(overlap_refusals)
        self.close_conflicts = frozenset(close_conflicts)

    # ------------------------------------------------------------------ state
    def trade(self) -> LiveTrade | None:
        data = self.journal.get(TRADE_KEY)
        return None if data is None else LiveTrade.from_json(data)

    def save(self, trade: LiveTrade | None) -> None:
        self.journal.put(TRADE_KEY, None if trade is None else trade.to_json())
        if trade is None or trade.status != "OPEN":
            self.journal.put(SCRATCH_KEY, None)  # the trade's bookkeeping ends with it

    def event(self, kind: str, data: dict[str, Any], text: str | None = None) -> None:
        self.journal.add_event(self.clock(), kind, data)
        if text is not None:
            self.alert(kind, text)

    def _scratch(self, trade_id: int) -> dict[str, Any]:
        """The bookkeeping of one trade (0: no open trade): `alerted` (alerts sent once) and `leg_filled` (order id
        -> when a leg was first found gone with fills). One journal key, replaced when the trade changes."""
        data = self.journal.get(SCRATCH_KEY)
        if not isinstance(data, dict) or data.get("trade_id") != trade_id:
            data = {"trade_id": trade_id, "alerted": [], "leg_filled": {}}
        return data

    def _counters(self, trade_id: int) -> tuple[int, int]:
        """The highest bracket generation and close / reduce number the journal holds for `trade_id`: a trade
        rebuilt or taken over under the id of an earlier one goes on from there (a reused client order id would
        resolve to the earlier, filled order)."""
        gen = closes = 0
        for row in self.journal.intents(trade_id=trade_id):
            tail = str(row["client_order_id"]).rsplit("-", 1)[-1]
            if not tail.isdigit():
                continue
            if row["kind"] == "BRACKET":
                gen = max(gen, int(tail))
            elif row["kind"] in ("CLOSE", "REDUCE"):
                closes = max(closes, int(tail))
        return gen, closes

    def _recorded(self, trade_id: int) -> bool:
        """A trade with this id was recorded once: it is the journal's trade, or it sent orders of its own."""
        t = self.trade()
        if t is not None and t.trade_id == trade_id:
            return True
        return any(row["kind"] != "ENTRY" for row in self.journal.intents(trade_id=trade_id))

    # ------------------------------------------------------------------ sending
    def _lookup(self, client_order_id: str, created_ts: float) -> tuple[Order | None, bool]:
        """(order, known): known False when the lookup itself failed (outcome still unknown)."""
        try:
            return self.ex.find_order(client_order_id, created_ts - LOOKUP_WINDOW_S), True
        except ExchangeError:
            return None, False

    def send(self, cid: str, kind: str, trade_id: int | None, payload: dict[str, Any],
             call: Callable[[], PlaceResult]) -> Sent:
        now = self.clock()
        row, created = self.journal.record_intent(cid, kind, trade_id, payload, now)
        if not created:
            if row["status"] == "ACCEPTED":
                return Sent("ACCEPTED", row["order_id"])
            if row["status"] in ("REJECTED", "ABANDONED"):
                return Sent("REJECTED", None, row["note"])
            found, known = self._lookup(cid, row["created_ts"])
            if not known:
                return Sent("PENDING", None, "lookup failed")
            if found is not None:
                self.journal.update_intent(cid, "ACCEPTED", self.clock(), order_id=found.order_id, note="found by lookup")
                return Sent("ACCEPTED", found.order_id)
        try:
            res = call()
        except ExchangeError as exc:
            if not exc.retryable:
                self.journal.update_intent(cid, "REJECTED", self.clock(), note=str(exc)[:200])
                return Sent("REJECTED", None, str(exc))
            found, known = self._lookup(cid, row["created_ts"])
            if found is not None:
                self.journal.update_intent(cid, "ACCEPTED", self.clock(), order_id=found.order_id, note="found after error")
                return Sent("ACCEPTED", found.order_id)
            return Sent("PENDING", None, f"{type(exc).__name__}: {exc}")
        if res.ok:
            self.journal.update_intent(cid, "ACCEPTED", self.clock(), order_id=res.order_id)
            return Sent("ACCEPTED", res.order_id)
        if res.duplicate:
            found, _ = self._lookup(cid, row["created_ts"])
            if found is not None:
                self.journal.update_intent(cid, "ACCEPTED", self.clock(), order_id=found.order_id, note="duplicate id")
                return Sent("ACCEPTED", found.order_id)
            return Sent("PENDING", None, res.error)
        self.journal.update_intent(cid, "REJECTED", self.clock(), note=res.error)
        return Sent("REJECTED", None, res.error)

    def _await_fill(self, order_id: str) -> Order:
        """The order once terminal (or at the deadline); read errors are retried until the deadline."""
        deadline = self.clock() + self.fill_timeout
        while True:
            try:
                o = self.ex.get_order(order_id)
                if o.is_terminal or self.clock() >= deadline:
                    return o
            except ExchangeError:
                if self.clock() >= deadline:
                    raise
            self.sleep(self.poll_s)

    def _cancel_quietly(self, order_ids: list[str], trade_id: int) -> None:
        """A failed cancel of a replaced leg is harmless (brackets only reduce): the next protect() cancels
        it as a stale order of ours."""
        try:
            self.ex.cancel(order_ids)
        except ExchangeError as exc:
            self.event("CANCEL_FAILED", {"trade_id": trade_id, "order_ids": order_ids, "error": str(exc)[:200]})

    def _await_flat(self, order_id: str | None) -> bool:
        """True once the position is flat. False at the deadline - or at once when the IOC close ended without
        filling all it was sent for (the venue cancelled the rest: the position stays). Read errors are retried
        until the deadline."""
        deadline = self.clock() + self.flat_timeout
        while True:
            try:
                if self.ex.position() is None:
                    return True
                o = self.ex.get_order(order_id) if order_id else None
                if o is not None and o.is_terminal and o.filled < o.size:
                    return False
            except ExchangeError:
                pass
            if self.clock() >= deadline:
                return False
            self.sleep(self.poll_s)

    # ------------------------------------------------------------------ actions
    def execute(self, actions: Sequence[Action], ctx: PlanContext) -> None:
        self._settle(ctx)
        closed = True
        reduced = True  # a Protect planned with a Reduce assumes it: kept back while the reduce is not known filled
        cleared = False  # a Close or Cancel ran: an entry waits until no bracket of ours rests
        for a in actions:
            try:
                if isinstance(a, Alert):
                    self._alert_once(a.kind, a.detail)
                elif isinstance(a, Skip):
                    self._skip(a)
                elif isinstance(a, Cancel):
                    cleared = True
                    self.ex.cancel(list(a.order_ids))
                    self.event("CANCELLED", {"order_ids": list(a.order_ids), "reason": a.reason})
                elif isinstance(a, Close):
                    cleared = True
                    closed = self.close(a.reason, ctx)
                elif isinstance(a, Reduce):
                    reduced = self.reduce(a.size, a.reason, ctx)
                elif isinstance(a, Protect):
                    if reduced:
                        self.protect(a, ctx)
                elif isinstance(a, Adopt):
                    self.adopt(a.target, ctx, a.contracts)
                elif isinstance(a, Enter):
                    if not closed:
                        self._skip(Skip(a.entry.signal_ts, "the previous position is not closed yet (flip)"))
                    elif cleared and not self._brackets_clear():
                        self._skip(Skip(a.entry.signal_ts, "a bracket of the closed position still rests (or the "
                                                           "orders cannot be read)"))
                    else:
                        self.enter(a, ctx)
            except ExchangeError as exc:
                self.event("ACTION_ERROR", {"action": type(a).__name__, "error": str(exc)[:200]},
                           f"[주문 오류] {type(a).__name__}: {str(exc)[:200]}")
                if isinstance(a, Close):
                    closed = False
                elif isinstance(a, Reduce):
                    reduced = False

    def _brackets_clear(self) -> bool:
        """No bracket of ours rests (one whose cancel is queued still does), waiting up to CLEAR_TIMEOUT_S; an
        unreadable book is not clear. An entry never goes out next to a bracket of the old side."""
        deadline = self.clock() + CLEAR_TIMEOUT_S
        while True:
            try:
                if not any(is_ours(o) and o.kind == "BRACKET" for o in self.ex.open_orders()):
                    return True
            except ExchangeError:
                pass
            if self.clock() >= deadline:
                return False
            self.sleep(self.poll_s)

    def _alert_once(self, kind: str, detail: str) -> None:
        """Once per open trade (or once while there is none): the record goes with the trade."""
        t = self.trade()
        scratch = self._scratch(t.trade_id if t is not None and t.status == "OPEN" else 0)
        key = f"{kind}:{detail}"
        if key in scratch["alerted"]:
            return
        scratch["alerted"] = (scratch["alerted"] + [key])[-100:]
        self.journal.put(SCRATCH_KEY, scratch)
        self.event(kind, {"detail": detail}, f"[경고] {kind}: {detail}")

    def _skip(self, a: Skip) -> None:
        done = self.journal.get(SKIPS_KEY, [])
        if a.signal_ts in done:
            return
        self.journal.put(SKIPS_KEY, (done + [a.signal_ts])[-200:])
        self.event("SKIP", {"signal_ts": a.signal_ts, "reason": a.reason}, f"[진입 건너뜀] 신호 {a.signal_ts}: {a.reason}")

    # entry -----------------------------------------------------------------
    def enter(self, a: Enter, ctx: PlanContext) -> LiveTrade | None:
        e = a.entry
        cid = entry_cid(e.signal_ts, e.direction)
        row = self.journal.intent(cid)
        if row is not None and row["status"] != "PENDING":
            return None  # already entered or refused once: an entry is never sent twice
        if e.signal_ts in self.journal.get(SKIPS_KEY, []):
            return None  # skipped once (paused, a flip's close not confirmed, no quotes...): never sent later
        if ctx.ratio is None:
            self._skip(Skip(e.signal_ts, "no futures/spot quotes"))
            return None
        lv = map_levels(e.direction, e.stop, e.tp1, e.tp2, ctx.ratio, ctx.tick)
        payload = {"signal_ts": e.signal_ts, "setup": e.setup, "direction": e.direction, "contracts": a.contracts,
                   "stop_spot": e.stop, "tp1_spot": e.tp1, "tp2_spot": e.tp2, "stop": lv.stop, "tp1": lv.tp1,
                   "tp2": lv.tp2, "ratio": ctx.ratio}
        sent = self.send(cid, "ENTRY", e.signal_ts, payload, lambda: self.ex.place_market(cid, e.direction, a.contracts))
        if sent.status != "ACCEPTED":
            self.event("ENTRY_FAILED", {"signal_ts": e.signal_ts, "status": sent.status, "error": sent.error},
                       f"[진입 실패] {e.setup} {_side(e.direction)} {a.contracts}계약: {sent.status} {sent.error}")
            return None
        try:
            order = self._await_fill(str(sent.order_id))
        except ExchangeError as exc:  # accepted but unreadable: the next pass rebuilds it from the intent
            self.event("ENTRY_UNCONFIRMED", {"signal_ts": e.signal_ts, "error": str(exc)[:200]},
                       f"[진입 미확인] {e.setup} {_side(e.direction)} {a.contracts}계약: 체결 조회 실패 - 다음 확인에서 보호")
            return None
        if order.filled == 0:
            self.event("ENTRY_UNFILLED", {"signal_ts": e.signal_ts}, f"[진입 미체결] {e.setup} {_side(e.direction)}")
            return None
        trade = self._trade_from(payload, order)
        self.save(trade)
        self.event("ENTRY", {"trade_id": trade.trade_id, "side": e.direction, "contracts": trade.contracts,
                             "price": trade.entry_price, "ref": e.entry_ref * ctx.ratio, "fee": order.fee,
                             "order_id": order.order_id},
                   f"[진입] {e.setup} {_side(e.direction)} {trade.contracts}계약 @ {trade.entry_price:,.1f} · 손절 "
                   f"{trade.stop_price:,.0f} · TP1 {trade.tp1_price:,.0f} · TP2 {trade.tp2_price or 0:,.0f}")
        legs, stop = desired_legs(trade, trade.contracts, trade.stop_spot, ctx.ratio, ctx.tick)
        self.protect(Protect(tuple(legs), trade.stop_spot, stop), ctx)
        return self.trade()

    def _trade_from(self, payload: dict[str, Any], order: Order) -> LiveTrade:
        """The trade of our filled entry. It opened when the order was created (a trade rebuilt after an outage
        was unprotected since then: PROTECTED_FIRST measures it); its ids go on from the journal's."""
        n = int(order.filled)
        trade_id = int(payload["signal_ts"])
        gen, closes = self._counters(trade_id)
        return LiveTrade(
            trade_id=trade_id, setup=str(payload["setup"]), direction=int(payload["direction"]),
            contracts=n, tp1_n=_tp1_split(n, self.tp1_frac), entry_price=float(order.avg_fill_price or 0.0),
            tp1_price=float(payload["tp1"]), tp2_price=None if payload["tp2"] is None else float(payload["tp2"]),
            stop_spot=float(payload["stop_spot"]), stop_price=float(payload["stop"]),
            opened_ts=float(order.created_ts or self.clock()), gen=gen, closes=closes,
        )

    def adopt(self, tp: TargetPosition, ctx: PlanContext, contracts: int | None = None) -> None:
        """Take over a position the trade state does not know: at most `contracts` (default: the whole
        position) and never above the stage maximum `ctx.max_contracts`; the planner reduces the rest
        (Reduce EXCESS). Our own entry for that signal (its fill could not be read in time; one still PENDING
        is looked up first) is rebuilt from its intent - unless a trade with that id was recorded already: a
        position after our trade closed is a foreign one. A foreign position (event ADOPTED: manual
        intervention) is taken over at its own entry price, its ids going on from the journal's; without quotes
        the engine's levels cannot be mapped (alerted once while it is not covered). A position the engine
        already took TP1 on is the rest after TP1 (break-even, no TP1 portion)."""
        pos = self.ex.position()
        if pos is None:
            return
        cid = entry_cid(tp.signal_ts, tp.direction)
        own = self.journal.intent(cid)
        if own is not None and own["status"] == "PENDING":  # our entry's outcome is unknown: look it up first
            found, known = self._lookup(cid, float(own["created_ts"]))
            if found is not None:
                self.journal.update_intent(cid, "ACCEPTED", self.clock(), order_id=found.order_id, note="found by lookup")
                own = self.journal.intent(cid)
            elif not known and not self._recorded(tp.signal_ts):  # protected now, settled once the lookup answers
                self._provisional(own, pos, self._take(pos, ctx, contracts), ctx)
                return
        if (own is not None and own["status"] == "ACCEPTED" and own["order_id"]
                and not self._recorded(tp.signal_ts)):
            try:
                order = self.ex.get_order(str(own["order_id"]))
            except ExchangeError:  # ours, its fill unreadable: protected now from its intent
                self._provisional(own, pos, self._take(pos, ctx, contracts), ctx, fill_unread=True)
                return
            if order.filled > 0:
                trade = self._trade_from(own["payload"], order)
                self.save(trade)
                self.event("RECOVERED_ENTRY", {"trade_id": trade.trade_id},
                           f"[복구] 진입 {trade.trade_id} {_side(trade.direction)} {trade.contracts}계약 복원")
                size = min(expected_contracts(trade), pos.contracts)
                legs, stop = desired_legs(trade, size, trade.stop_spot, ctx.ratio, ctx.tick)
                self.protect(Protect(tuple(legs), trade.stop_spot, stop), ctx)
                return
        n = self._take(pos, ctx, contracts)
        after_tp1 = tp.tp1_done and n > 1
        approx = ctx.ratio is None  # no quotes: the spot levels as they are (ratio 1.0) until the quotes are back
        ratio = 1.0 if ctx.ratio is None else ctx.ratio
        lv = map_levels(tp.direction, tp.stop, tp.tp1, tp.tp2, ratio, ctx.tick)
        gen, closes = self._counters(tp.signal_ts)
        trade = LiveTrade(
            trade_id=tp.signal_ts, setup=tp.setup, direction=tp.direction, contracts=n,
            tp1_n=0 if after_tp1 else _tp1_split(n, self.tp1_frac), entry_price=pos.avg_entry, tp1_price=lv.tp1,
            tp2_price=lv.tp2, stop_spot=tp.stop, stop_price=lv.stop, opened_ts=self.clock(), tp1_filled=after_tp1,
            gen=gen, closes=closes,
        )
        self.save(trade)
        if approx:
            scratch = self._scratch(trade.trade_id)
            scratch["approx"] = {"stop": tp.stop, "tp1": tp.tp1, "tp2": tp.tp2}
            self.journal.put(SCRATCH_KEY, scratch)
        self.event("ADOPTED", {"trade_id": trade.trade_id, "contracts": n, "position": pos.contracts},
                   f"[경고] 저널에 없던 포지션 {_side(pos.side)} {pos.contracts}계약 중 {n}계약을 엔진 거래 "
                   f"{tp.signal_ts}로 인수")
        if approx:
            self._alert_once("LEVELS_APPROX", f"trade {trade.trade_id}: no quotes - protected at the spot levels "
                                              f"(ratio 1.0, approximate) until the quotes are back")
        legs, stop = desired_legs(trade, n, trade.stop_spot, ratio, ctx.tick)
        self.protect(Protect(tuple(legs), trade.stop_spot, stop), ctx)

    @staticmethod
    def _take(pos: Position, ctx: PlanContext, contracts: int | None) -> int:
        """Contracts to take over: at most `contracts` (default the whole position) and the stage maximum."""
        n = pos.contracts if contracts is None else min(contracts, pos.contracts)
        return n if ctx.max_contracts is None else min(n, ctx.max_contracts)

    def _provisional(self, own: dict[str, Any], pos: Position, n: int, ctx: PlanContext,
                     fill_unread: bool = False) -> None:
        """Our entry's outcome is unknown and its lookup fails - or it was accepted and its fill cannot be read -
        yet a position is there: protect it at once with a provisional trade from the entry's intent (its levels;
        the position's size and price; opened when the entry was sent). `_settle` decides later whether it was ours
        (RECOVERED_ENTRY) or foreign (ADOPTED)."""
        p = own["payload"]
        trade_id = int(p["signal_ts"])
        gen, closes = self._counters(trade_id)
        trade = LiveTrade(
            trade_id=trade_id, setup=str(p["setup"]), direction=int(p["direction"]), contracts=n,
            tp1_n=_tp1_split(n, self.tp1_frac), entry_price=pos.avg_entry, tp1_price=float(p["tp1"]),
            tp2_price=None if p["tp2"] is None else float(p["tp2"]), stop_spot=float(p["stop_spot"]),
            stop_price=float(p["stop"]), opened_ts=float(own["created_ts"]), gen=gen, closes=closes,
        )
        self.save(trade)
        scratch = self._scratch(trade_id)
        scratch["provisional"] = own["client_order_id"]
        self.journal.put(SCRATCH_KEY, scratch)
        why = "체결 조회 실패" if fill_unread else "조회 실패"
        self.event("ADOPTED_PROVISIONAL", {"trade_id": trade_id, "contracts": n, "position": pos.contracts,
                                           "why": "FILL_UNREAD" if fill_unread else "LOOKUP_FAILED"},
                   f"[경고] 진입 {trade_id} 결과 미확인({why}): 포지션 {_side(pos.side)} {pos.contracts}계약 중 "
                   f"{n}계약을 진입 주문의 손절·TP로 임시 보호 - 확인되면 정리")
        legs, stop = desired_legs(trade, n, trade.stop_spot, ctx.ratio, ctx.tick)
        self.protect(Protect(tuple(legs), trade.stop_spot, stop), ctx)

    def _entry_outcome(self, cid: str) -> bool | None:
        """Our entry `cid` reached the venue (True), never did (False), or is still unknown (None)."""
        row = self.journal.intent(cid)
        if row is None or row["status"] in ("REJECTED", "ABANDONED"):
            return False
        if row["status"] == "ACCEPTED":
            return True
        found, known = self._lookup(cid, float(row["created_ts"]))
        if not known:
            return None
        if found is None:
            return False
        self.journal.update_intent(cid, "ACCEPTED", self.clock(), order_id=found.order_id, note="found by lookup")
        return True

    def _settle(self, ctx: PlanContext) -> None:
        """Before every pass: a provisional trade is settled once its entry's outcome is known (ours:
        RECOVERED_ENTRY; not on the venue: a foreign position, ADOPTED); levels taken over without quotes are mapped
        at the first pass with quotes (LEVELS_MAPPED: the next plan re-places the legs)."""
        t = self.trade()
        if t is None or t.status != "OPEN":
            return
        scratch = self._scratch(t.trade_id)
        cid, approx = scratch.get("provisional"), scratch.get("approx")
        if cid:
            own = self._entry_outcome(str(cid))
            if own is not None:
                scratch["provisional"] = None
                self.journal.put(SCRATCH_KEY, scratch)
                if own:
                    self.event("RECOVERED_ENTRY", {"trade_id": t.trade_id, "provisional": True},
                               f"[복구] 진입 {t.trade_id} 확인: 임시 보호한 {t.contracts}계약은 우리 진입")
                else:
                    self.event("ADOPTED", {"trade_id": t.trade_id, "contracts": t.contracts, "provisional": True},
                               f"[경고] 진입 {t.trade_id}는 거래소에 없음: 임시 보호한 {t.contracts}계약은 저널에 없던 "
                               f"포지션 (엔진 거래 {t.trade_id}로 인수)")
        if approx and ctx.ratio is not None:
            lv = map_levels(t.direction, approx["stop"], approx["tp1"], approx["tp2"], ctx.ratio, ctx.tick)
            if t.stop_spot == approx["stop"]:  # a stop moved since was mapped with quotes already
                t.stop_price = lv.stop
            t.tp1_price, t.tp2_price = lv.tp1, lv.tp2
            self.save(t)
            scratch["approx"] = None
            self.journal.put(SCRATCH_KEY, scratch)
            self.event("LEVELS_MAPPED", {"trade_id": t.trade_id, "stop": t.stop_price, "tp1": lv.tp1, "tp2": lv.tp2,
                                         "ratio": ctx.ratio})

    # protection --------------------------------------------------------------
    def _place_leg(self, trade: LiveTrade, want: Leg) -> tuple[Leg | None, Sent]:
        trade.gen += 1
        cid = bracket_cid(trade.trade_id, want.name, trade.gen)
        self.save(trade)  # the generation is persisted before the request
        sent = self.send(cid, "BRACKET", trade.trade_id,
                         {"leg": want.name, "size": want.size, "limit": want.limit_price, "stop": want.stop_price},
                         lambda: self.ex.place_bracket(cid, -trade.direction, want.size, want.limit_price, want.stop_price))
        if sent.status == "PENDING":  # answer and lookup lost: an order resting on the book is ours, found at once
            found = self._resting(cid)
            if found is not None:
                self.journal.update_intent(cid, "ACCEPTED", self.clock(), order_id=found.order_id, note="found resting")
                sent = Sent("ACCEPTED", found.order_id)
        if sent.status != "ACCEPTED":
            return None, sent
        return Leg(want.name, want.size, want.limit_price, want.stop_price, cid, sent.order_id), sent

    def _resting(self, cid: str) -> Order | None:
        """Our order `cid` among the open orders (best effort: None when they cannot be read)."""
        try:
            return next((o for o in self.ex.open_orders() if o.client_order_id == cid and o.status != CANCEL_QUEUED),
                        None)
        except ExchangeError:
            return None

    def _overlap(self, trade: LiveTrade, want: Leg) -> bool:
        """Our resting brackets plus `want` exceed the position (fresh reads): the overlap a refusal names exists."""
        try:
            pos = self.ex.position()
            orders = self.ex.open_orders()
        except ExchangeError:
            return False
        if pos is None:
            return False
        resting = sum(o.size - o.filled for o in orders
                      if is_ours(o) and o.kind == "BRACKET" and o.side == -trade.direction)
        return resting + want.size > pos.contracts

    def _bracket_failed(self, trade: LiveTrade, want: Leg, sent: Sent) -> None:
        self.event("BRACKET_FAILED", {"trade_id": trade.trade_id, "leg": want.name, "status": sent.status,
                                      "error": sent.error},
                   f"[보호 주문 실패] {want.name} {want.size}계약 손절 {want.stop_price:,.0f}: {sent.status} {sent.error}")

    def _edit(self, o: Order, want: Leg) -> bool:
        """orders/edit takes the order's total size: what already filled plus what is to rest. A failed edit
        (refused or unanswered) is replaced like any other leg."""
        try:
            return self.ex.edit_bracket(o.order_id, o.filled + want.size, want.limit_price, want.stop_price)
        except ExchangeError:
            return False

    @staticmethod
    def _orphan(trade: LiveTrade, want: Leg, orders: dict[str, Order], claimed: set[str]) -> Order | None:
        """A resting bracket of ours for this trade's `want.name` leg that the trade does not list (accepted right
        before a crash, or a replacement whose answer was lost) - one with the wanted terms first."""
        prefix = f"{PREFIX}b-{trade.trade_id}-{want.name}-"
        found = [o for oid, o in orders.items() if oid not in claimed and o.kind == "BRACKET"
                 and o.client_order_id.startswith(prefix) and o.side == -trade.direction]
        wanted = (want.size, want.limit_price, want.stop_price)
        return next((o for o in found if _terms(o) == wanted), found[0] if found else None)

    def protect(self, a: Protect, ctx: PlanContext) -> None:
        """Make the resting brackets the wanted legs without ever removing a working leg before its replacement
        rests (module docstring): kept, taken over, edited or replaced (place first, then cancel; cancel first
        only when the venue refuses the overlap)."""
        trade = self.trade()
        if trade is None or trade.status != "OPEN":
            return
        orders = {o.order_id: o for o in self.ex.open_orders() if o.status != CANCEL_QUEUED}  # queued: leaving
        claimed = {str(leg.order_id) for leg in trade.legs if leg.order_id in orders}
        kept: list[Leg] = []
        exact = 0  # wanted legs resting with exactly the wanted terms
        replaced: set[str] = set()  # cancelled below already: not cancelled again as stale
        for want in a.legs:
            terms = (want.size, want.limit_price, want.stop_price)
            cur = next((leg for leg in trade.legs if leg.name == want.name and leg.order_id in orders), None)
            if cur is None:
                gone = next((leg for leg in trade.legs if leg.name == want.name and leg.order_id), None)
                if gone is not None and self._left_with_fills(trade, gone):
                    kept.append(gone)  # not placed again: the next observe folds its fill (or it is placed later)
                    continue
            if cur is not None and _terms(orders[str(cur.order_id)]) == terms:
                kept.append(cur)
                exact += 1
                continue
            orphan = self._orphan(trade, want, orders, claimed)
            if orphan is not None:
                claimed.add(orphan.order_id)
                if _terms(orphan) == terms:  # ours for this leg, its answer lost: taken over, not placed again
                    kept.append(_leg_of(want.name, orphan))
                    exact += 1
                    self.event("LEG_ADOPTED", {"trade_id": trade.trade_id, "leg": want.name, "order_id": orphan.order_id,
                                               "client_order_id": orphan.client_order_id})
                    if cur is not None:
                        self._cancel_quietly([str(cur.order_id)], trade.trade_id)
                        replaced.add(str(cur.order_id))
                    continue
                if cur is None:
                    cur = _leg_of(want.name, orphan)  # replaced like a leg of the trade
            if cur is not None and self.edit_stops:
                o = orders[str(cur.order_id)]
                if (o.size - o.filled, o.limit_price) == (want.size, want.limit_price) and self._edit(o, want):
                    kept.append(Leg(cur.name, want.size, want.limit_price, want.stop_price, cur.client_order_id,
                                    cur.order_id))
                    exact += 1
                    self.event("STOP_EDITED", {"trade_id": trade.trade_id, "leg": want.name, "stop": want.stop_price})
                    continue
            placed, sent = self._place_leg(trade, want)
            moved = placed is not None
            if (placed is None and cur is not None and _code(sent) in self.overlap_refusals
                    and self._overlap(trade, want)):  # the venue refuses overlapping brackets: cancel first
                placed, moved = self._swap(trade, cur, orders[str(cur.order_id)], want, replaced)
            elif placed is None:  # unknown (PENDING), any other refusal or nothing to replace: the old leg stays
                self._bracket_failed(trade, want, sent)
            if placed is not None:
                if cur is not None and str(cur.order_id) not in replaced:
                    self._cancel_quietly([str(cur.order_id)], trade.trade_id)
                    replaced.add(str(cur.order_id))
                kept.append(placed)
                exact += int(moved)
            elif cur is not None and str(cur.order_id) not in replaced:
                kept.append(cur)  # its replacement is not in place: the old leg keeps protecting
        complete = len(kept) == len(a.legs)
        keep = {leg.order_id for leg in kept}
        stale = [oid for oid, o in orders.items()
                 if is_ours(o) and o.kind == "BRACKET" and oid not in keep and oid not in replaced]
        if stale and complete:  # extra brackets only reduce: removed once every wanted leg is in place
            self._cancel_quietly(stale, trade.trade_id)
        trade.legs = kept
        in_place = exact == len(a.legs)  # every leg rests at the new terms: their stop is the stop in force
        moved_stop = in_place and a.stop_price != trade.stop_price
        if in_place:
            trade.stop_spot, trade.stop_price = a.stop_spot, a.stop_price
        if trade.protected_ts is None and kept and complete:
            trade.protected_ts = self.clock()
            self.event("PROTECTED_FIRST", {"trade_id": trade.trade_id,
                                           "after_entry_s": round(trade.protected_ts - trade.opened_ts, 3)})
        elif moved_stop:
            self.event("STOP_MOVED", {"trade_id": trade.trade_id, "stop": a.stop_price, "stop_spot": a.stop_spot},
                       f"[손절 이동] {trade.setup} {_side(trade.direction)} → {a.stop_price:,.0f}")
        self.save(trade)

    def _swap(self, trade: LiveTrade, cur: Leg, o: Order, want: Leg, replaced: set[str]) -> tuple[Leg | None, bool]:
        """The venue refused `want` as an overlap while `cur` rests: cancel `cur` first (LEG_GAP:
        the designed path on such a venue, not an order failure), then place `want`; when that fails as well,
        `cur`'s terms are placed again at once (LEG_RESTORED). Returns the leg now resting (None: none) and whether
        it has the wanted terms."""
        oid = str(cur.order_id)
        try:
            self.ex.cancel([oid])
        except ExchangeError as exc:  # it may still rest: kept, the move is tried again on the next pass
            self.event("CANCEL_FAILED", {"trade_id": trade.trade_id, "order_ids": [oid], "error": str(exc)[:200]})
            self._bracket_failed(trade, want, Sent("REJECTED", None, "overlap refused, the old leg not cancelled"))
            return None, False
        replaced.add(oid)
        self.event("LEG_GAP", {"trade_id": trade.trade_id, "leg": want.name})
        placed, sent = self._place_leg(trade, want)
        if placed is not None:
            return placed, True
        self._bracket_failed(trade, want, sent)
        back = Leg(cur.name, o.size - o.filled, float(o.limit_price or cur.limit_price),
                   float(o.stop_price or cur.stop_price))
        restored, sent = self._place_leg(trade, back)
        if restored is None:
            self._bracket_failed(trade, back, sent)
            return None, False
        self.event("LEG_RESTORED", {"trade_id": trade.trade_id, "leg": cur.name, "stop": back.stop_price})
        return restored, False

    def _left_with_fills(self, trade: LiveTrade, leg: Leg) -> bool:
        """A leg of ours that left the book: True while it has fills the position read may not show yet - for
        LEG_SETTLE_S from when it was first found so (then it is placed again if still wanted). A leg that cannot
        be read is placed again (protection first)."""
        scratch = self._scratch(trade.trade_id)
        first = scratch["leg_filled"].get(str(leg.order_id))
        if first is not None:
            return self.clock() - float(first) < LEG_SETTLE_S
        try:
            o = self.ex.get_order(str(leg.order_id))
        except ExchangeError:
            return False
        if o.filled <= 0:
            return False
        scratch["leg_filled"][str(leg.order_id)] = self.clock()
        self.journal.put(SCRATCH_KEY, scratch)
        self.event("LEG_FILLED", {"trade_id": trade.trade_id, "leg": leg.name, "order_id": leg.order_id,
                                  "filled": o.filled, "status": o.status})
        return True

    def _protect_left(self, ctx: PlanContext) -> None:
        """After every close that did not go flat (failed or unconfirmed) and every reduce that did not fill after
        its legs were shrunk: the legs become the ones for the position left, at the stop in force. When none of the
        trade's legs rests (cancelled for a close conflict, now or on an earlier pass), they are placed again from
        scratch - a leg still resting is taken back by its terms; otherwise protect() keeps the matching ones and
        resizes the rest. Brackets only reduce: an excess is covered as well."""
        trade = self.trade()
        if trade is None or trade.status != "OPEN":
            return
        try:
            pos = self.ex.position()
            if pos is None or pos.side != trade.direction:
                return
            resting = {o.order_id for o in self.ex.open_orders() if o.status != CANCEL_QUEUED}
            if trade.legs and not any(leg.order_id in resting for leg in trade.legs):
                trade.legs = []  # none rests: nothing to look up, placed again
                self.save(trade)
            legs, stop = desired_legs(trade, pos.contracts, trade.stop_spot, ctx.ratio, ctx.tick)
            self.protect(Protect(tuple(legs), trade.stop_spot, stop), ctx)
        except ExchangeError as exc:  # observe sees the gap (UNPROTECTED); the next pass places them
            self.event("ACTION_ERROR", {"action": "Protect", "error": str(exc)[:200]},
                       f"[주문 오류] Protect: {str(exc)[:200]}")

    # exits -------------------------------------------------------------------
    def close(self, reason: str, ctx: PlanContext) -> bool:
        trade = self.trade()
        if self.ex.position() is None:
            if trade is not None and trade.status == "OPEN":
                trade.status, trade.exit_reason = "CLOSED", trade.exit_reason or reason
                self.save(trade)
            return True
        if not ctx.session_open:
            self._alert_once("CLOSE_DEFERRED", f"{reason}: session not open")
            return False
        open_trade = trade if trade is not None and trade.status == "OPEN" else None
        tid = open_trade.trade_id if open_trade is not None else 0
        sent = self._send_close(open_trade, reason)  # the brackets keep protecting until the venue took the close
        if _code(sent) in self.close_conflicts:  # refused while brackets rest: cancel them, then close at once
            if not self._session_open():
                self._alert_once("CLOSE_DEFERRED", f"{reason}: session not open")
                return False
            ours = [o.order_id for o in self.ex.open_orders() if is_ours(o) and o.status != CANCEL_QUEUED]
            self.event("CLOSE_CONFLICT", {"trade_id": tid, "reason": reason, "error": sent.error, "cancel": ours})
            if ours:
                self._cancel_quietly(ours, tid)
            sent = self._send_close(open_trade, reason)
        if sent.status != "ACCEPTED":
            self.event("CLOSE_FAILED", {"trade_id": tid, "reason": reason, "status": sent.status, "error": sent.error},
                       f"[청산 실패 - 재시도 중] {reason}: {sent.status} {sent.error}")
            self._protect_left(ctx)  # every failed close: the legs (back) for the position, whoever cancelled them
            return False
        if open_trade is not None:  # the position going flat from now on is this close (also after a crash)
            open_trade = self.trade() or open_trade
            open_trade.exit_reason = reason
            self.save(open_trade)
        if not self._await_flat(sent.order_id):
            self.event("CLOSE_UNCONFIRMED", {"trade_id": tid, "reason": reason}, f"[청산 미확인 - 재시도 중] {reason}")
            self._protect_left(ctx)
            return False
        if open_trade is not None:
            open_trade.status, open_trade.exit_reason = "CLOSED", reason
            self.save(open_trade)
        fill = self._fill_of(sent.order_id)
        self.event("CLOSED", {"trade_id": tid, "reason": reason, "order_id": sent.order_id, **fill},
                   f"[청산] {reason}" + (f" @ {fill['price']:,.1f}" if fill.get("price") else ""))
        try:  # brackets left on the flat position (the venue cancels them as well)
            left = [o.order_id for o in self.ex.open_orders() if is_ours(o) and o.status != CANCEL_QUEUED]
        except ExchangeError:
            left = []
        if left:
            self._cancel_quietly(left, tid)
        return True

    def _send_close(self, open_trade: LiveTrade | None, reason: str) -> Sent:
        """close_position under a new client order id (the trade's next close number; `ewb-x-0-<n>` without one)."""
        if open_trade is not None:
            current = self.trade() or open_trade
            current.closes += 1
            n, tid = current.closes, current.trade_id
            self.save(current)
        else:
            n, tid = int(self.journal.get(ORPHAN_KEY, 0)) + 1, 0
            self.journal.put(ORPHAN_KEY, n)
        cid = close_cid(tid, n)
        return self.send(cid, "CLOSE", tid, {"reason": reason}, lambda: self.ex.close_position(cid))

    def _session_open(self) -> bool:
        """A fresh session read (a failed read counts as not OPEN: protection stays)."""
        try:
            return self.ex.session().is_open
        except ExchangeError:
            return False

    def _fill_of(self, order_id: str | None) -> dict[str, Any]:
        """Fill price, size and fee of a market order, for the cost report (best effort)."""
        if not order_id:
            return {}
        try:
            o = self.ex.get_order(order_id)
        except ExchangeError:
            return {}
        return {"price": o.avg_fill_price, "contracts": o.filled, "side": o.side, "fee": o.fee}

    def reduce(self, size: int, reason: str, ctx: PlanContext) -> bool:
        """Close `size` contracts at market (close_position with a size: it only reduces). Intent before state: the
        reduce is recorded on the trade (`pending_reduce`, with the position it was sent against and the trade's
        size fields once done, `reconcile.after_reduce`) before the request, so an ambiguous answer can never lead
        to a second reduce - while one is pending, `reduce` resolves that one instead. True when the venue reported
        it filled: its after-state is applied at once (TP1_SYNC marks the live TP1 done, EXCESS leaves the trade
        unchanged, any other reason - MARGIN - makes the trade smaller) and the record stays until the position
        shows it (`reconcile.observe`)."""
        trade = self.trade()
        if trade is None or trade.status != "OPEN" or not ctx.session_open:
            return False
        if trade.pending_reduce is not None:  # never a second reduce while one is pending
            self.resolve_reduce()
            return False
        pos = self.ex.position()
        if pos is None or pos.side != trade.direction:
            return False
        trade.closes += 1
        cid = reduce_cid(trade.trade_id, trade.closes)
        left = max(trade.contracts - size, 0)
        tp1_n = None if reason in ("TP1_SYNC", "EXCESS") or trade.tp1_filled else _tp1_split(left, self.tp1_frac)
        trade.pending_reduce = {"cid": cid, "size": size, "reason": reason, "before": pos.contracts,
                                "ts": self.clock(), "after": after_reduce(trade, size, reason, tp1_n), "filled": None}
        self.save(trade)  # recorded before the request
        sent = self.send(cid, "REDUCE", trade.trade_id, {"size": size, "reason": reason},
                         lambda: self.ex.close_position(cid, size))
        if _code(sent) in self.close_conflicts and self._session_open():
            return self._reduce_with_room(size, reason, sent, ctx)
        return self._reduce_outcome(trade, sent, first=True)

    def _reduce_with_room(self, size: int, reason: str, refused: Sent, ctx: PlanContext) -> bool:
        """The venue refused the reduce because our brackets cover what it would close (its id sold nothing): make
        room, then send it again under a new reduce id. If no room could be made or the resent reduce does not fill,
        the legs go back for the whole position (never a gap; brackets only reduce, so after a reduce that did fill
        an extra leg only over-covers until the next pass resizes it)."""
        current = self.trade()
        self.event("REDUCE_CONFLICT", {"trade_id": current.trade_id if current is not None else 0, "reason": reason,
                                       "size": size, "error": refused.error})
        if not self._make_room(size, reason, ctx):
            self._protect_left(ctx)
            trade = self.trade()
            return trade is not None and self._reduce_outcome(trade, refused, first=True)
        trade = self.trade()
        try:
            pos = self.ex.position()
        except ExchangeError:
            pos = None
        if trade is None or trade.pending_reduce is None or pos is None or pos.side != trade.direction:
            self._protect_left(ctx)
            return trade is not None and self._reduce_outcome(trade, refused, first=True)
        trade.closes += 1
        cid = reduce_cid(trade.trade_id, trade.closes)
        trade.pending_reduce = {**trade.pending_reduce, "cid": cid, "before": pos.contracts, "ts": self.clock(),
                                "filled": None}
        self.save(trade)  # the new id is recorded before the request
        sent = self.send(cid, "REDUCE", trade.trade_id, {"size": size, "reason": reason},
                         lambda: self.ex.close_position(cid, size))
        filled = self._reduce_outcome(trade, sent, first=True)
        if not filled:
            self._protect_left(ctx)
        return filled

    def _make_room(self, size: int, reason: str, ctx: PlanContext) -> bool:
        """Shrink our brackets so that a reduce of `size` fits under them: TP1_SYNC cancels the TP1 leg (the contracts
        the reduce sells); EXCESS / MARGIN resize the legs to the trade after the reduce (`pending_reduce.after`, the
        position less `size`), placed first. True when our resting brackets then leave room for the reduce."""
        trade = self.trade()
        if trade is None or trade.pending_reduce is None:
            return False
        try:
            pos = self.ex.position()
            if pos is None or pos.side != trade.direction or pos.contracts < size:
                return False
            if reason == "TP1_SYNC":
                leg = next((x for x in trade.legs if x.name == "tp1" and x.order_id), None)
                if leg is None:
                    return False
                self.ex.cancel([str(leg.order_id)])
                trade.legs = [x for x in trade.legs if x is not leg]  # cancelled by us: nothing to look up
                self.save(trade)
            else:
                after = LiveTrade.from_json({**trade.to_json(), **trade.pending_reduce["after"]})
                legs, stop = desired_legs(after, pos.contracts - size, trade.stop_spot, ctx.ratio, ctx.tick)
                self.protect(Protect(tuple(legs), trade.stop_spot, stop), ctx)
            cover = sum(o.size - o.filled for o in self.ex.open_orders()
                        if is_ours(o) and o.kind == "BRACKET" and o.side == -trade.direction)
            return cover + size <= pos.contracts
        except ExchangeError as exc:
            self.event("ACTION_ERROR", {"action": "Reduce", "error": str(exc)[:200]})
            return False

    def resolve_reduce(self) -> None:
        """A reduce whose outcome is still unknown (an ambiguous answer, a killed process, a fill not read yet) is
        resolved by its client order id: looked up first and sent again under the same id only when the venue
        does not have it and the position does not show a reduction yet (then observe attributes it) - never a
        second reduce. The runtime calls this before every read of the exchange state."""
        trade = self.trade()
        if trade is None or trade.status != "OPEN" or trade.pending_reduce is None:
            return
        pr = trade.pending_reduce
        if pr.get("filled"):
            return  # reported filled: the position will show it (observe settles it)
        pos = self.ex.position()
        if pos is None or pos.contracts < int(pr["before"]):
            return  # the position already shows a reduction: observe attributes it; the minute job resolves the id
        cid, size = str(pr["cid"]), int(pr["size"])
        sent = self.send(cid, "REDUCE", trade.trade_id, {"size": size, "reason": pr["reason"]},
                         lambda: self.ex.close_position(cid, size))
        if _code(sent) in self.close_conflicts:  # refused for our brackets: nothing sold; the next pass makes room
            trade.pending_reduce = None
            self.save(trade)
            self.event("REDUCE_CONFLICT", {"trade_id": trade.trade_id, "reason": pr["reason"], "size": size,
                                           "error": sent.error})
            return
        self._reduce_outcome(trade, sent)

    def _reduce_outcome(self, trade: LiveTrade, sent: Sent, first: bool = False) -> bool:
        pr = dict(trade.pending_reduce or {})
        tid, reason, size = trade.trade_id, pr.get("reason"), pr.get("size")
        if sent.status == "PENDING":  # unknown: kept, resolved by its id on the next pass
            if first:
                self.event("REDUCE_FAILED", {"trade_id": tid, "reason": reason, "status": "PENDING", "error": sent.error},
                           f"[수량 축소 미확인] {size}계약 · {reason}: 주문 번호로 확인 중 ({sent.error})")
            return False
        order: Order | None = None
        if sent.status == "ACCEPTED":
            try:
                order = self._await_fill(str(sent.order_id))
            except ExchangeError:
                return False  # accepted, the fill not readable yet: read again on the next pass
            if not order.is_terminal:
                return False
        if order is None or order.filled == 0:  # refused, abandoned or nothing filled: nothing was sold
            status = sent.status if order is None else "UNFILLED"
            trade.pending_reduce = None
            self.save(trade)
            self.event("REDUCE_FAILED", {"trade_id": tid, "reason": reason, "status": status, "error": sent.error},
                       f"[수량 축소 실패] {size}계약 · {reason}: {status} {sent.error or ''}".rstrip())
            return False
        for k, v in pr["after"].items():
            setattr(trade, k, v)
        pr["filled"] = int(order.filled)
        trade.pending_reduce = pr  # kept until the position shows it (reconcile.observe settles it)
        self.save(trade)
        self.event("REDUCED", {"trade_id": tid, "size": size, "reason": reason, "price": order.avg_fill_price,
                               "contracts": order.filled, "side": order.side, "fee": order.fee},
                   f"[수량 축소] {order.filled}계약 · {reason}")
        return True

    def flatten(self, reason: str, ctx: PlanContext) -> bool:
        """Close the whole position (/flatten, breakers, margin); our brackets go with it (see `close`)."""
        return self.close(reason, ctx)

    # ------------------------------------------------------------------ restart recovery (spec 5.1)
    def resolve_pending(self) -> tuple[dict[str, int], list[dict[str, Any]]]:
        """Look every PENDING intent up by client_order_id: found -> ACCEPTED; not found and older than 10
        minutes -> ABANDONED (it never reached the venue); lookup failed -> still unknown. Returns the counts
        and the CLOSE intents found. The runtime also calls this every minute while intents are pending."""
        now = self.clock()
        out = {"accepted": 0, "abandoned": 0, "unknown": 0, "rebuilt": 0}
        closes: list[dict[str, Any]] = []
        for row in self.journal.intents(status="PENDING"):
            found, known = self._lookup(row["client_order_id"], row["created_ts"])
            if not known:
                out["unknown"] += 1
            elif found is not None:
                self.journal.update_intent(row["client_order_id"], "ACCEPTED", now, order_id=found.order_id,
                                           note="found by lookup")
                out["accepted"] += 1
                if row["kind"] == "CLOSE":
                    closes.append(row)
            elif now - row["created_ts"] > ABANDON_AFTER_S:
                self.journal.update_intent(row["client_order_id"], "ABANDONED", now, note="not on the exchange")
                out["abandoned"] += 1
        return out, closes

    def _recovery_error(self, out: dict[str, int], what: str, exc: ExchangeError) -> None:
        """A read the recovery needs failed: unresolved (counted unknown), the regular passes pick it up."""
        out["unknown"] += 1
        self.event("RECOVERY_ERROR", {"what": what, "error": str(exc)[:200]})

    def _claim_legs(self, trade: LiveTrade, pos: Position, orders: list[Order]) -> None:
        """At restart: a resting bracket of ours named for the trade (`ewb-b-<trade>-<leg>-<gen>`) that the trade does
        not list - accepted right before the crash - is that leg again (LEG_ADOPTED; the newest per leg, never one
        whose cancel is queued), so the first observe does not read the position as unprotected. When they cover the
        position and the trade was not protected yet, it was protected when they were placed (PROTECTED_FIRST)."""
        resting = {o.order_id: o for o in orders if o.status != CANCEL_QUEUED}
        names = {leg.name for leg in trade.legs if leg.order_id in resting}
        listed = {leg.order_id for leg in trade.legs}
        prefix = f"{PREFIX}b-{trade.trade_id}-"
        claimed: list[Order] = []
        for o in sorted(resting.values(), key=lambda o: (o.created_ts or 0.0, o.client_order_id), reverse=True):
            name, _, gen = o.client_order_id[len(prefix):].partition("-")
            if (not o.client_order_id.startswith(prefix) or name not in ("tp1", "tp2") or not gen.isdigit()
                    or o.kind != "BRACKET" or o.side != -trade.direction or o.order_id in listed or name in names):
                continue
            trade.legs = [leg for leg in trade.legs if leg.name != name] + [_leg_of(name, o)]
            names.add(name)
            claimed.append(o)
            self.event("LEG_ADOPTED", {"trade_id": trade.trade_id, "leg": name, "order_id": o.order_id,
                                       "client_order_id": o.client_order_id, "recovered": True})
        if not claimed:
            return
        trade.legs.sort(key=lambda leg: leg.name)
        covered = sum(resting[str(leg.order_id)].size - resting[str(leg.order_id)].filled
                      for leg in trade.legs if leg.order_id in resting)
        if trade.protected_ts is None and covered >= pos.contracts:
            trade.protected_ts = max(float(o.created_ts or self.clock()) for o in claimed)
            self.event("PROTECTED_FIRST", {"trade_id": trade.trade_id,
                                           "after_entry_s": round(trade.protected_ts - trade.opened_ts, 3)})
        self.save(trade)

    def recover(self) -> dict[str, int]:
        """At startup: resolve PENDING intents, record a close accepted right before a crash (the journal's open
        trade, a flat position and an ACCEPTED close of that trade - its reason, not a bracket exit), rebuild the
        trade from an accepted entry the journal did not get to record (only when no trade with that id was
        recorded), and take the open trade's resting brackets back as its legs (`_claim_legs`: a bracket accepted
        right before a crash). A read that fails is recorded (RECOVERY_ERROR, counted unknown) and the recovery
        goes on."""
        out, _ = self.resolve_pending()
        trade = self.trade()
        if trade is not None and trade.status == "OPEN":
            try:
                pos = self.ex.position()
                orders = [] if pos is None else self.ex.open_orders()
            except ExchangeError as exc:
                self._recovery_error(out, "position", exc)
            else:
                mine = self.journal.intents(status="ACCEPTED", kind="CLOSE", trade_id=trade.trade_id)
                if pos is None and mine:  # killed after our close was accepted: record it with its reason
                    row = mine[-1]
                    trade.status, trade.exit_reason = "CLOSED", str(row["payload"].get("reason"))
                    self.save(trade)
                    self.event("CLOSED", {"trade_id": trade.trade_id, "reason": trade.exit_reason, "recovered": True,
                                          "order_id": row["order_id"], **self._fill_of(row["order_id"])})
                elif pos is not None:
                    self._claim_legs(trade, pos, orders)
        trade = self.trade()
        entries = self.journal.intents(status="ACCEPTED", kind="ENTRY")
        if entries and (trade is None or trade.status != "OPEN"):
            row = entries[-1]
            if row["order_id"] and not self._recorded(int(row["trade_id"])):
                try:
                    order = self.ex.get_order(str(row["order_id"]))
                    pos = self.ex.position()
                except ExchangeError as exc:
                    self._recovery_error(out, str(row["client_order_id"]), exc)
                else:
                    if order.filled > 0 and pos is not None and pos.side == int(row["payload"]["direction"]):
                        trade = self._trade_from(row["payload"], order)
                        self.save(trade)
                        out["rebuilt"] += 1
                        self.event("RECOVERED_ENTRY", {"trade_id": trade.trade_id},
                                   f"[재시작 복구] 진입 {trade.trade_id} {_side(trade.direction)} {trade.contracts}계약 복원")
        self.event("RECOVERY", out)
        return out
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run --group live pytest tests/live/test_executor.py -q
```

Expected: `65 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/live/executor.py tests/live/helpers.py tests/live/test_executor.py
git commit -m "$(cat <<'EOF'
feat(live): executor with intent-before-send, bracket protection, closes and restart recovery

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Watchdog (stop gap, margin, account breakers, session) and the WebSocket feed

**Files:**
- Create: `src/ewbot/live/watchdog.py`, `src/ewbot/live/feed.py`
- Modify (through `uv add` only): `pyproject.toml`, `uv.lock` - `websockets==13.1` in the `live` group (Decision D1)
- Test: `tests/live/test_watchdog.py`

**Interfaces:**
- Consumes: `ewbot.config.BreakerParams` (daily 3%, total 15%), `ewbot.live.exchange`, `ewbot.live.journal.Journal`, `ewbot.live.pricing` (margin constants, `margin_ratio`, `max_contracts_for_margin`, `stop_gapped`), `tests.live.helpers` (`Clock`, `journal`, `sim`); `websockets.sync.client.connect` (websockets 13.1, dependency group `live`; imported lazily in `PublicSocket.open`, and in one test that is skipped without the group).
- Produces (`ewbot.live.watchdog`): `NY`, `GAP_GRACE_S = 10.0`, `WS_STALE_S = 60.0`, `REST_POLL_S = 10.0`, `MAINTENANCE_NOTICE_S`, `BASING_READS = 3`; `class StopGapWatch(grace_s=10.0)` with `check(direction, stop_price, price, now) -> bool` (a wall clock stepping back re-arms it at the new time) and `reset()`; frozen `MarginStatus(ratio, level: "OK"|"WARN"|"REDUCE", reduce_by=0)`, `margin_status(contracts, price, side, product, equity) -> MarginStatus`; `ny_date(ts) -> str`; `class BadEquity(ValueError)`; `class AccountGuard(journal, params=None)` (journal key `account_guard`) with `state()`, `update(equity, now) -> "DAILY_LOSS" | "TOTAL_DD" | None` (newly tripped only, on the second breaching observation in a row, each judged against the reference as it then stands; references are based on three reads - the day's on the New York day's first three, the peak on the first three ever, and after a re-base on the reset's read and the next two - as the highest read so far until the third, then their median; after basing the peak only rises to the lower of two observations in a row; so one wrong read, high or low, at any position neither trips a breaker nor leaves a reference looser than the truth for more than that read - two wrong basing reads, a real move within the basing reads or two wrong reads in a row can, the accepted limits; an equity that is not finite and above 0 raises `BadEquity` and writes nothing), `reset(equity, now) -> list[str]` (re-bases only the breakers that tripped - the peak after `TOTAL_DD`, the day's reference after `DAILY_LOSS`, which a `TOTAL_DD` trip marks too - with this read as the first of their three basing reads, and names them; `[]` and no change without a trip; `BadEquity` likewise); `class SessionWatch` with `update(info, now) -> list[("SESSION" | "MAINTENANCE", text)]` (a window already under way is announced too; an unknown end reads `미정`); `class PriceSource(exchange, feed=None, *, clock, stale_s=60.0, poll_s=10.0)` with `futures_price() -> float | None` (a usable WebSocket price at most `stale_s` old, else the REST mid read at most every `poll_s` - an error or a book without a finite bid and ask above 0 keeps the last good one - and `None` once that is older than `stale_s`) and `.mode` (`"ws"` / `"rest"`).
- Produces (`ewbot.live.feed`): `CHANNELS = ("ticker", "heartbeats")`, `MAX_BACKOFF_S = 60.0`, `SILENT_S = 15.0`, `HEALTHY_S = 60.0`, `OPEN_TIMEOUT_S = 10`, `PUBLIC_WS_URL = "wss://advanced-trade-ws.coinbase.com"`, frozen `Tick(price, ts)`, `usable_price(x) -> float | None` (finite and above 0), `ticker_price(ticker) -> float | None` (the best bid / ask mid, else the trade price); `class TickerFeed(products, ws_factory, *, clock=time.time, sleep=None, max_backoff=60.0, silent_s=15.0, healthy_s=60.0)` (`sleep=None`: the backoff waits on the stop event, so `stop()` ends it) with `on_message(raw)` (never raises: an unreadable message counts in `unreadable_messages`, a ticker entry without a usable price in `unpriced_tickers`; a server `error` message sets `last_error` and is no liveness), `latest(product_id) -> Tick | None`, `age() -> float | None`, `take_resync() -> bool`, `run()` (drives `ws_factory(on_message=...)` sockets through `open()`, `subscribe(products, channels)`, `pump(seconds)`, `close()`; reconnects on a failure, a lost or closed connection or `silent_s` without a message; backoff 1 s doubling to `max_backoff`, reset after `healthy_s` of messages), `start() -> Thread`, `stop()`, attributes `gaps`, `connects`, `failures`, `messages`, `unreadable_messages`, `unpriced_tickers`, `last_error` (an exception type name, `silent for 15 s` or a server error message); `class PublicSocket(url, on_message, *, connect=None, open_timeout=OPEN_TIMEOUT_S)` (`open()` calls `connect(url, ssl=ssl.create_default_context(), open_timeout=...)` - certificate and host name verified - `subscribe()` sends one keyless `{"type": "subscribe", "product_ids", "channel"}` per channel, `pump(seconds)` hands over what arrives and raises on a lost or closed connection, `close()`); `public_ws_factory(url=PUBLIC_WS_URL, *, connect=None) -> factory(on_message=...) -> PublicSocket` (no credential).

- [ ] **Step 1: Write the failing test**

**File: `tests/live/test_watchdog.py`**

```python
from __future__ import annotations

import inspect
import json
import math
import ssl
import time
from importlib import metadata

import pytest

from ewbot.live.exchange import Quote, SessionInfo, Unavailable
from ewbot.live.feed import HEALTHY_S, PUBLIC_WS_URL, SILENT_S, Tick, TickerFeed, public_ws_factory
from ewbot.live.sim import BIP_SPEC
from ewbot.live.watchdog import AccountGuard, BadEquity, PriceSource, SessionWatch, StopGapWatch, margin_status
from tests.live.helpers import Clock, journal, sim

NY_NOON = 1_791_043_200  # 2026-10-03 12:00 New York (16:00 UTC)
FUT = "BIP-20DEC30-CDE"


def test_stop_gap_closes_only_after_ten_seconds_beyond_the_limit():
    w = StopGapWatch()
    assert not w.check(1, 60_000.0, 57_500.0, now=0.0)  # inside the 5% limit (57_000)
    assert not w.check(1, 60_000.0, 56_900.0, now=1.0)
    assert not w.check(1, 60_000.0, 56_900.0, now=10.9)
    assert w.check(1, 60_000.0, 56_900.0, now=11.0)
    assert not w.check(1, 60_000.0, 57_100.0, now=12.0) and w.since is None  # back inside: reset
    assert not w.check(-1, 60_000.0, 63_100.0, now=20.0)  # a short's limit is 63_000
    assert not w.check(-1, 60_000.0, 62_900.0, now=25.0) and w.since is None
    assert not w.check(-1, 60_000.0, 63_100.0, now=26.0)
    assert w.check(-1, 60_000.0, 63_100.0, now=36.0)


def test_stop_gap_fires_at_most_one_grace_period_late_when_the_clock_steps_back():
    w = StopGapWatch()
    assert not w.check(1, 60_000.0, 56_900.0, now=10_000.0)
    assert not w.check(1, 60_000.0, 56_900.0, now=6_405.0)  # the wall clock stepped back an hour: re-armed
    assert not w.check(1, 60_000.0, 56_900.0, now=6_414.0)
    assert w.check(1, 60_000.0, 56_900.0, now=6_415.0)  # 10 s after the step, not an hour


def test_margin_levels_use_the_overnight_rate():
    assert margin_status(1, 84_000.0, 1, BIP_SPEC, 1_000.0).level == "OK"  # 17.7%
    assert margin_status(5, 84_000.0, 1, BIP_SPEC, 1_000.0).level == "WARN"  # 88.3%
    st = margin_status(6, 84_000.0, 1, BIP_SPEC, 1_000.0)  # 106%
    assert st.level == "REDUCE" and st.reduce_by == 3  # back to 3 contracts (<= 70%)
    assert margin_status(1, 84_000.0, 1, BIP_SPEC, 100.0).reduce_by == 1  # nothing fits: close it all


def test_account_guard_trips_on_two_breaching_reads_once_a_day_and_survives_a_restart(tmp_path):
    jr = journal(tmp_path)
    g = AccountGuard(jr)
    for k in range(3):  # the day's first three reads set its reference (their median)
        assert g.update(10_000.0, NY_NOON + 60 * k) is None
    assert g.update(9_701.0, NY_NOON + 180) is None
    assert g.update(9_700.0, NY_NOON + 240) is None  # -3%: one breaching read is not enough
    assert g.update(9_700.0, NY_NOON + 300) == "DAILY_LOSS"  # the second in a row trips (one poll later)
    assert g.update(9_600.0, NY_NOON + 360) is None  # once per New York day
    g2 = AccountGuard(jr)  # restart
    assert g2.state()["daily_tripped"] and g2.state()["peak"] == 10_000.0
    for k in range(3):  # next day: new reference 9_600
        assert g2.update(9_600.0, NY_NOON + 86_400 + 60 * k) is None
    assert g2.update(8_500.0, NY_NOON + 86_580) is None
    assert g2.update(8_500.0, NY_NOON + 86_640) == "TOTAL_DD"  # 15% below the 10_000 peak, twice
    assert g2.update(8_000.0, NY_NOON + 86_700) is None
    jr.close()


def test_one_bad_balance_read_never_trips_a_breaker_nor_raises_the_peak(tmp_path):
    jr = journal(tmp_path)
    g = AccountGuard(jr)
    assert g.update(10_000.0, NY_NOON) is None
    assert g.update(9_000.0, NY_NOON + 60) is None  # one low read (-10%) between normal ones
    assert g.update(10_000.0, NY_NOON + 120) is None
    assert g.update(20_000.0, NY_NOON + 180) is None  # one high read
    assert [g.update(10_000.0, NY_NOON + 240 + 60 * k) for k in range(3)] == [None, None, None]
    assert g.state()["peak"] == 10_000.0  # the peak only rises to what two reads in a row reached
    assert g.update(10_400.0, NY_NOON + 480) is None and g.state()["peak"] == 10_000.0
    assert g.update(10_500.0, NY_NOON + 540) is None and g.state()["peak"] == 10_400.0
    for bad in (math.nan, math.inf, 0.0, -5.0):
        with pytest.raises(BadEquity):
            g.update(bad, NY_NOON + 600)  # a read failure, not an observation
    assert g.state()["equity"] == 10_500.0 and g.update(10_500.0, NY_NOON + 660) is None
    jr.close()


@pytest.mark.parametrize("wrong", [12_000.0, 8_000.0], ids=["high", "low"])
@pytest.mark.parametrize("position", [0, 1, 2])
def test_one_wrong_basing_read_neither_trips_nor_loosens_a_breaker(tmp_path, wrong, position):
    """The first reads base both the peak and the day's reference: one of them 20% off, either way, at any of the
    three positions. Neither breaker trips, both references end at the true 10_000 (looser for at most that one
    read), and a real -3.5% move afterwards still trips DAILY_LOSS on its second read."""
    g = AccountGuard(journal(tmp_path))
    reads = [10_000.0] * 3
    reads[position] = wrong
    refs = []
    for k, equity in enumerate(reads + [10_000.0] * 3):
        assert g.update(equity, NY_NOON + 60 * k) is None
        refs.append((g.state()["day_ref"], g.state()["peak"]))
    assert refs[3:] == [(10_000.0, 10_000.0)] * 3  # the median of three: the wrong read set neither reference
    assert sum(1 for day_ref, peak in refs if min(day_ref, peak) < 10_000.0) <= 1
    assert g.update(9_650.0, NY_NOON + 360) is None
    assert g.update(9_650.0, NY_NOON + 420) == "DAILY_LOSS"


@pytest.mark.parametrize("wrong", [12_000.0, 8_000.0], ids=["high", "low"])
def test_one_wrong_read_at_resume_neither_trips_nor_loosens_a_breaker(tmp_path, wrong):
    g = AccountGuard(journal(tmp_path))
    for k in range(3):
        g.update(10_000.0, NY_NOON + 60 * k)
    g.update(8_400.0, NY_NOON + 180)
    assert g.update(8_400.0, NY_NOON + 240) == "TOTAL_DD"  # it marks the day too
    assert g.reset(wrong, NY_NOON + 300) == ["TOTAL_DD", "DAILY_LOSS"]  # /resume read a wrong balance (truth: 8_400)
    refs = []
    for k in range(4):
        assert g.update(8_400.0, NY_NOON + 360 + 60 * k) is None
        refs.append((g.state()["day_ref"], g.state()["peak"]))
    assert refs[1:] == [(8_400.0, 8_400.0)] * 3  # the median of the reset's read and the next two
    assert min(refs[0]) >= 8_400.0  # after the reset's own read, never looser than a read seen
    assert g.update(8_100.0, NY_NOON + 600) is None  # a real -3.5% from 8_400
    assert g.update(8_100.0, NY_NOON + 660) == "DAILY_LOSS"


def test_resume_rebases_only_the_breaker_that_tripped(tmp_path):
    jr = journal(tmp_path)
    g = AccountGuard(jr)
    for k in range(3):
        g.update(10_000.0, NY_NOON + 60 * k)
    g.update(9_800.0, NY_NOON + 180)
    before = g.state()
    assert g.reset(9_800.0, NY_NOON + 240) == [] and g.state() == before  # /pause then /resume: nothing changes
    g.update(9_700.0, NY_NOON + 300)
    assert g.update(9_650.0, NY_NOON + 360) == "DAILY_LOSS"
    assert g.reset(9_650.0, NY_NOON + 420) == ["DAILY_LOSS"]
    st = g.state()
    assert (st["day_ref"], st["peak"], st["daily_tripped"]) == (9_650.0, 10_000.0, False)  # the peak stays
    g.update(8_400.0, NY_NOON + 480)
    assert g.update(8_400.0, NY_NOON + 540) == "TOTAL_DD"  # 16% below the peak (it marks the day too)
    assert g.reset(8_400.0, NY_NOON + 600) == ["TOTAL_DD", "DAILY_LOSS"]
    st = g.state()
    assert (st["peak"], st["day_ref"], st["total_tripped"], st["daily_tripped"]) == (8_400.0, 8_400.0, False, False)
    with pytest.raises(BadEquity):
        g.reset(math.nan, NY_NOON + 660)
    jr.close()


def test_session_watch_reports_changes_and_maintenance_once():
    w = SessionWatch()
    start = NY_NOON + 3_600
    assert w.update(SessionInfo("OPEN", start, start + 3 * 3600), NY_NOON) == [
        ("MAINTENANCE", "정기 점검 예정 2026-10-03 13:00 ~ 16:00 (뉴욕)")]
    assert w.update(SessionInfo("OPEN", start, start + 3 * 3600), NY_NOON + 60) == []
    assert w.update(SessionInfo("CLOSE"), NY_NOON + 120) == [("SESSION", "장 운영 상태 OPEN → CLOSE")]
    assert w.update(SessionInfo("OPEN", NY_NOON + 3 * 86_400), NY_NOON + 180)[0][0] == "SESSION"  # too far ahead


def test_session_watch_announces_a_window_in_progress_and_an_open_end():
    w = SessionWatch()  # a (re)start during maintenance
    assert w.update(SessionInfo("OPEN", NY_NOON - 600, NY_NOON + 3_000), NY_NOON) == [
        ("MAINTENANCE", "정기 점검 진행 중 2026-10-03 11:50 ~ 12:50 (뉴욕)")]
    assert w.update(SessionInfo("OPEN", NY_NOON - 600, NY_NOON + 3_000), NY_NOON + 60) == []
    assert SessionWatch().update(SessionInfo("OPEN", NY_NOON + 3_600, None), NY_NOON) == [
        ("MAINTENANCE", "정기 점검 예정 2026-10-03 13:00 ~ 미정 (뉴욕)")]
    assert SessionWatch().update(SessionInfo("OPEN", NY_NOON - 7_200, NY_NOON - 3_600), NY_NOON) == []  # over


def ticker(product: str, price: float | str, seq: int, **book: str) -> str:
    return json.dumps({"channel": "ticker", "sequence_num": seq, "events": [
        {"type": "update", "tickers": [{"type": "ticker", "product_id": product, "price": str(price), **book}]}]})


def test_price_source_uses_a_fresh_feed_then_rest_every_ten_seconds():
    clock = Clock(1_000.0)
    ex = sim(clock, fut=60_000.0)
    feed = TickerFeed([ex.futures_id], ws_factory=lambda **kw: None, clock=clock)
    src = PriceSource(ex, feed, clock=clock)
    feed.on_message(ticker(ex.futures_id, 60_100.0, 1))
    assert src.futures_price() == 60_100.0 and src.mode == "ws" and "quote" not in ex.calls
    clock.now += 60.5  # silent for more than 60 s
    assert src.futures_price() == 60_000.0 and src.mode == "rest" and ex.calls.count("quote") == 1
    clock.now += 5.0
    src.futures_price()
    assert ex.calls.count("quote") == 1  # polled at most every 10 s
    clock.now += 5.0
    ex.fail_next("quote", Unavailable("503"))
    assert src.futures_price() == 60_000.0  # the last REST price is kept on an error
    assert PriceSource(ex, None, clock=clock).futures_price() == 60_000.0


class Book:
    """A futures quote source whose book can break (a price read as 0, NaN, ...) or fail."""

    futures_id = FUT

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.bid: float | None = 60_000.0
        self.ask = 60_010.0
        self.calls = 0

    def quote(self, product_id: str) -> Quote:
        self.calls += 1
        if self.bid is None:
            raise Unavailable("503")
        return Quote(product_id, self.bid, self.ask, self.clock())


class StuckFeed:
    def __init__(self, tick: Tick) -> None:
        self.tick = tick

    def latest(self, product_id: str) -> Tick:
        return self.tick


def test_price_source_never_returns_an_unusable_or_stale_price():
    clock = Clock(1_000.0)
    book = Book(clock)
    src = PriceSource(book, StuckFeed(Tick(math.nan, 1_000.0)), clock=clock)  # type: ignore[arg-type]
    assert src.futures_price() == 60_005.0 and src.mode == "rest"  # a NaN tick is never fresh
    for bid, ask in ((0.0, 60_010.0), (math.nan, 60_010.0), (-1.0, -1.0), (math.inf, math.inf)):
        clock.now += 10.0
        book.bid, book.ask = bid, ask
        assert src.futures_price() == 60_005.0  # a broken book is a failed read: the last good price (<= 40 s old)
    clock.now += 10.0
    book.bid = None  # outage
    assert src.futures_price() == 60_005.0 and book.calls == 6  # 50 s old
    clock.now += 5.0
    assert src.futures_price() == 60_005.0 and book.calls == 6  # one attempt per 10 s, failed or not
    clock.now += 6.0
    assert src.futures_price() is None and book.calls == 7  # the last good price is 61 s old: no price
    clock.now += 10.0
    book.bid, book.ask = 60_100.0, 60_110.0
    assert src.futures_price() == 60_105.0


def test_feed_parses_ticks_and_flags_sequence_gaps():
    clock = Clock(5.0)
    feed = TickerFeed([FUT], ws_factory=lambda **kw: None, clock=clock)
    feed.on_message(ticker(FUT, 60_000.0, 7))
    feed.on_message(json.dumps({"channel": "heartbeats", "sequence_num": 8}))
    assert feed.latest(FUT).price == 60_000.0 and not feed.take_resync()
    feed.on_message(ticker(FUT, 60_010.0, 11))  # 9 and 10 missing
    assert feed.gaps == 1 and feed.take_resync() and not feed.take_resync()
    feed.on_message("not json")
    clock.now = 9.0
    assert feed.age() == pytest.approx(4.0)


def test_feed_takes_the_bid_ask_mid_and_ignores_unusable_prices():
    clock = Clock(5.0)
    feed = TickerFeed([FUT], ws_factory=lambda **kw: None, clock=clock)
    feed.on_message(ticker(FUT, 60_100.0, 1, best_bid="60000", best_ask="60010"))
    assert feed.latest(FUT).price == 60_005.0  # the REST quote's measure, not the last trade
    feed.on_message(ticker(FUT, 60_020.0, 2, best_bid="", best_ask="60030"))
    assert feed.latest(FUT).price == 60_020.0  # no usable bid: the last trade price
    clock.now = 6.0
    for seq, bad in enumerate(("NaN", "inf", "0", "-5", "", "abc"), start=3):
        feed.on_message(ticker(FUT, bad, seq, best_bid=bad, best_ask="60030"))
    assert feed.latest(FUT) == Tick(60_020.0, 5.0) and (feed.unpriced_tickers, feed.unreadable_messages) == (6, 0)


def test_a_message_the_feed_cannot_read_is_counted_never_raised():
    feed = TickerFeed([FUT], ws_factory=lambda **kw: None, clock=Clock(5.0))
    for raw in ("not json", "[]", "5", json.dumps({"channel": "ticker", "events": [None]}),
                json.dumps({"channel": "ticker", "events": [{"tickers": "x"}]})):
        feed.on_message(raw)  # an exception here would end the connection's read loop (PublicSocket.pump)
    assert (feed.unreadable_messages, feed.unpriced_tickers) == (5, 0) and feed.latest(FUT) is None
    feed.on_message(json.dumps({"type": "error", "message": "failure to subscribe"}))  # a refused subscription
    assert feed.last_error == "server error: failure to subscribe"
    assert feed.messages == 0 and feed.age() is None  # a server error is no sign of a working feed
    feed.on_message(ticker(FUT, 60_000.0, 1))
    assert feed.latest(FUT).price == 60_000.0 and feed.messages == 1


class ScriptedWS:
    """One connection. `script` is "refuse" (open fails) or steps consumed one per pump(1): "msg" delivers a
    heartbeat, "quiet" returns without one (a connection that fell silent), "drop" raises like a lost
    connection, "stop" stops the feed. An exhausted script stays quiet; after 500 opens and steps in
    all the feed is stopped (a feed that never notices silence must not hang the test)."""

    steps = 0

    def __init__(self, feed: TickerFeed, scripts: list, log: list[str], **kw) -> None:
        self.feed, self.log = feed, log
        self.on_message = kw["on_message"]
        self.script = scripts.pop(0) if scripts else ["stop"]
        self.used = 0

    def open(self):
        ScriptedWS.steps += 1
        if ScriptedWS.steps > 500:
            self.feed.stop()
        self.log.append("open")
        if self.script == "refuse":
            raise ConnectionRefusedError("refused")

    def subscribe(self, products, channels):
        self.log.append(f"subscribe {','.join(products)} {','.join(channels)}")

    def pump(self, s):
        ScriptedWS.steps += 1
        self.used += 1
        step = self.script.pop(0) if self.script else "quiet"
        if ScriptedWS.steps > 500:
            step = "stop"
        if step == "msg":
            self.on_message(json.dumps({"channel": "heartbeats", "sequence_num": self.used}))
        elif step == "drop":
            raise ConnectionError("dropped")
        elif step == "stop":
            self.feed.stop()

    def close(self):
        self.log.append(f"close {self.used}")


def scripted(scripts: list, **kw) -> tuple[TickerFeed, list[str], list[float]]:
    ScriptedWS.steps = 0
    log: list[str] = []
    waits: list[float] = []
    feed = TickerFeed([FUT, "BTC-USD"], ws_factory=None, sleep=waits.append, **kw)  # type: ignore[arg-type]
    feed.ws_factory = lambda **k: ScriptedWS(feed, scripts, log, **k)
    return feed, log, waits


def test_feed_reconnects_with_backoff_and_requests_a_resync():
    feed, log, waits = scripted([["msg", "msg", "drop"], ["drop"], ["msg", "stop"]])
    feed.run()
    assert feed.connects == 3 and waits == [1.0, 2.0]  # a short-lived connection does not reset the backoff
    assert log.count("subscribe BIP-20DEC30-CDE,BTC-USD ticker,heartbeats") == 3
    assert [x for x in log if x.startswith("close")] == ["close 3", "close 1", "close 2"]
    assert feed.failures == 2 and feed.last_error == "ConnectionError" and feed.take_resync()


def test_a_silent_connection_is_torn_down_and_opened_again():
    """A connection can stay open and bring nothing (a half-open TCP connection, a server that stopped
    sending): pump keeps returning and no message comes. The heartbeats channel sends one every second, so
    SILENT_S without one means the connection is gone."""
    feed, log, waits = scripted([["msg"], ["msg", "stop"]])
    feed.run()
    assert feed.connects == 2 and waits == [1.0]
    assert [x for x in log if x.startswith("close")] == [f"close {1 + int(SILENT_S)}", "close 2"]
    assert feed.failures == 1 and feed.last_error == f"silent for {SILENT_S:.0f} s"


def test_backoff_doubles_to_the_cap_and_resets_only_after_a_healthy_minute():
    healthy = ["msg"] * int(HEALTHY_S) + ["drop"]
    feed, log, waits = scripted(["refuse"] * 8 + [["msg"] * 5 + ["drop"], "refuse", healthy, "refuse", ["stop"]])
    feed.run()
    assert waits == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0, 60.0, 1.0, 2.0]
    assert feed.connects == 3 and feed.failures == 12 and feed.last_error == "ConnectionRefusedError"


class Refused:
    def __init__(self, **kw) -> None:
        pass

    def open(self):
        raise ConnectionRefusedError("refused")

    def close(self):
        pass


def test_stop_ends_a_backoff_wait_at_once():
    feed = TickerFeed([FUT], ws_factory=lambda **kw: Refused(**kw))
    th = feed.start()
    deadline = time.monotonic() + 2.0
    while feed.failures == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    feed.stop()  # the feed is waiting 1 s before the next attempt
    th.join(0.5)
    assert feed.failures >= 1 and not th.is_alive()


class FakeConnection:
    """Stands in for a websockets ClientConnection: recv() hands out `inbox`, then raises `end`."""

    def __init__(self, inbox: list[str], end: BaseException) -> None:
        self.inbox, self.end = inbox, end
        self.sent: list[dict] = []
        self.closed = False

    def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def recv(self, timeout: float | None = None) -> str:
        if self.inbox:
            return self.inbox.pop(0)
        raise self.end

    def close(self) -> None:
        self.closed = True


def test_the_public_socket_subscribes_without_a_key_over_verified_tls():
    beat = json.dumps({"channel": "heartbeats", "sequence_num": 1})
    conn = FakeConnection([beat, ticker(FUT, 60_000.0, 2)], ConnectionResetError("lost"))
    opened: list[tuple[str, dict]] = []

    def connect(url, **kw):  # no network: what the socket asks of websockets.sync.client.connect
        opened.append((url, kw))
        return conn

    got: list[str] = []
    ws = public_ws_factory(connect=connect)(on_message=got.append)
    ws.open()
    [(url, kw)] = opened
    assert url == PUBLIC_WS_URL == "wss://advanced-trade-ws.coinbase.com" and kw["open_timeout"] == 10
    ctx = kw["ssl"]  # the default context: the certificate chain and the host name are verified
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    ws.subscribe([FUT, "BTC-USD"], ["ticker", "heartbeats"])
    assert conn.sent == [{"type": "subscribe", "product_ids": [FUT, "BTC-USD"], "channel": "ticker"},
                         {"type": "subscribe", "product_ids": [FUT, "BTC-USD"], "channel": "heartbeats"}]  # no jwt
    with pytest.raises(ConnectionResetError):  # a lost connection reaches TickerFeed.run
        ws.pump(1)
    assert got == [beat, ticker(FUT, 60_000.0, 2)]
    quiet = FakeConnection([], TimeoutError())
    ws2 = public_ws_factory(connect=lambda url, **kw: quiet)(on_message=got.append)
    ws2.open()
    ws2.pump(0.01)  # nothing within the time: returns (TickerFeed counts the silence)
    ws2.close()
    assert quiet.closed and len(got) == 2


def test_the_pinned_websockets_client_takes_the_arguments_the_socket_passes():
    """Runs only with the `live` dependency group installed (websockets 13.1, declared there)."""
    client = pytest.importorskip("websockets.sync.client")
    assert metadata.version("websockets") == "13.1"
    assert {"ssl", "open_timeout"} <= set(inspect.signature(client.connect).parameters)
    for name in ("send", "recv", "close"):
        assert callable(getattr(client.ClientConnection, name))
    assert "timeout" in inspect.signature(client.ClientConnection.recv).parameters
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --group live pytest tests/live/test_watchdog.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'ewbot.live.feed'`.

- [ ] **Step 3: Write minimal implementation**

Declare the feed's WebSocket client in the `live` group (Decision D1: already locked as the SDK's dependency, so `uv.lock` changes only in the project's group metadata):

```bash
uv add --group live websockets==13.1
```

**File: `src/ewbot/live/watchdog.py`**

```python
"""Always-on safety checks between decision cycles (Plan 3 spec 3.2 `watchdog`, 4.2, 4.5, 5.1).

  StopGapWatch   a bracket stop fills as a limit 5% beyond the stop; when the futures price has passed
                 that limit and the position is still open 10 s later, the runtime closes at market.
  margin_status  overnight-margin projection of the open position: >= 80% warn, >= 90% reduce back to
                 the 70% entry limit (entries above 70% are refused before sending, live/pricing.py).
  AccountGuard   real-account breakers: equity <= the New York day's opening equity x 0.97, or <= the peak
                 x 0.85 -> flatten and pause (spec 4.5), on the second observation in a row that breaches.
                 State lives in the journal (survives restarts).
  SessionWatch   session state changes and the next scheduled maintenance (noticed 24 h ahead, or at once
                 when it is already under way).
  PriceSource    the futures price for the checks: the WebSocket ticker while it is fresh (a usable price in
                 the last 60 s), otherwise REST polled every 10 s; no price once the last good one is older
                 than 60 s (spec 5.1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from ewbot.config import BreakerParams
from ewbot.live.exchange import Exchange, ExchangeError, ProductSpec, SessionInfo
from ewbot.live.feed import usable_price
from ewbot.live.journal import Journal
from ewbot.live.pricing import (
    MARGIN_ENTRY_MAX,
    MARGIN_REDUCE,
    MARGIN_WARN,
    margin_ratio,
    max_contracts_for_margin,
    stop_gapped,
)

NY = ZoneInfo("America/New_York")
GAP_GRACE_S = 10.0
WS_STALE_S = 60.0
REST_POLL_S = 10.0
MAINTENANCE_NOTICE_S = 24 * 3600
BASING_READS = 3  # a reference (the day's, a re-based peak) is the median of its first three valid reads


class StopGapWatch:
    """`now` is the runtime's wall clock: when it steps backwards the timer is armed again at the new time,
    so a close fires at most one grace period late (never as late as the size of the step)."""

    def __init__(self, grace_s: float = GAP_GRACE_S) -> None:
        self.grace_s = grace_s
        self.since: float | None = None

    def check(self, direction: int, stop_price: float, price: float, now: float) -> bool:
        """True when the price has been beyond the stop's limit for `grace_s` seconds."""
        if not stop_gapped(price, stop_price, direction):
            self.since = None
            return False
        if self.since is None or now < self.since:
            self.since = now
        return now - self.since >= self.grace_s

    def reset(self) -> None:
        self.since = None


@dataclass(frozen=True, slots=True)
class MarginStatus:
    ratio: float
    level: str  # OK | WARN | REDUCE
    reduce_by: int = 0


def margin_status(contracts: int, price: float, side: int, product: ProductSpec, equity: float) -> MarginStatus:
    ratio = margin_ratio(contracts, price, side, product, equity)
    if ratio >= MARGIN_REDUCE:
        keep = max_contracts_for_margin(price, side, product, equity, MARGIN_ENTRY_MAX)
        return MarginStatus(ratio, "REDUCE", max(contracts - keep, 0))
    if ratio >= MARGIN_WARN:
        return MarginStatus(ratio, "WARN")
    return MarginStatus(ratio, "OK")


def ny_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, NY).date().isoformat()


class BadEquity(ValueError):
    """An equity that is not a finite amount above 0: a failed balance read, never an observation."""


def _observed(equity: float) -> float:
    e = usable_price(equity)
    if e is None:
        raise BadEquity(f"equity {equity!r} is not a finite amount above 0")
    return e


def _basing(reads: list[float], e: float) -> tuple[list[float] | None, float]:
    """A reference being based on its first BASING_READS reads: the highest read so far until the last one
    arrives (never looser than a read seen), then their median - and basing is over (None)."""
    reads = [*reads, e]
    if len(reads) < BASING_READS:
        return reads, max(reads)
    return None, sorted(reads)[len(reads) // 2]


class AccountGuard:
    """Real-account breakers (spec 4.5). Only a finite equity above 0 is an observation; anything else raises
    BadEquity and changes nothing (the runtime alerts it).

    References are based on three reads. The day's reference is the median of the New York day's first three
    observations; the peak is the median of the first three ever and, after a reset re-based either, the
    reference is the median of the reset's read and the next two. Until its third read arrives a reference is
    the highest read so far (never looser than any read seen). After basing, the peak only rises to a level
    two observations in a row reached (the lower of the two). A breaker trips on the second observation in a
    row that breaches it, each judged against the reference as it then stands (one poll - a minute in the
    runtime - after the first). So one wrong balance read, high or low, at any position - a basing read
    (the midnight funding settlement, say), the /resume read or any later one - neither trips a breaker nor
    leaves a reference looser than the truth for more than that one read.

    Accepted limits: two wrong reads among a reference's three basing reads set it - two wrong high ones can
    trip that breaker two reads later; two wrong low ones loosen it, the day's reference until the next New
    York day, a re-based peak until two true reads in a row lift it - and so does a real move within those
    reads (the first ~2 minutes of the day); two wrong high reads in a row raise the peak; two wrong low reads
    in a row trip a breaker."""

    KEY = "account_guard"

    def __init__(self, journal: Journal, params: BreakerParams | None = None) -> None:
        self.journal = journal
        self.params = BreakerParams() if params is None else params  # daily 3%, total 15% (spec 4.5)

    def state(self) -> dict[str, Any] | None:
        return self.journal.get(self.KEY)

    def update(self, equity: float, now: float) -> str | None:
        """The breaker newly tripped by this equity observation (DAILY_LOSS / TOTAL_DD), else None."""
        e = _observed(equity)
        st = self.state() or {"peak": e, "peak_reads": [], "day": None, "day_ref": e, "daily_tripped": False,
                              "total_tripped": False}
        prev = st.get("equity")
        day = ny_date(now)
        if st["day"] != day:
            st.update(day=day, day_reads=[], daily_tripped=False, daily_breach=False)
        if st.get("day_reads") is not None:
            st["day_reads"], st["day_ref"] = _basing(st["day_reads"], e)
        if st.get("peak_reads") is not None:
            st["peak_reads"], st["peak"] = _basing(st["peak_reads"], e)
        else:
            st["peak"] = max(float(st["peak"]), e if prev is None else min(e, float(prev)))
        total = e <= st["peak"] * (1.0 - self.params.total_dd)
        daily = e <= st["day_ref"] * (1.0 - self.params.daily_loss)
        trip = None
        if total and st.get("total_breach") and not st["total_tripped"]:
            st["total_tripped"] = st["daily_tripped"] = True  # one flatten covers both
            trip = "TOTAL_DD"
        elif daily and st.get("daily_breach") and not st["daily_tripped"]:
            st["daily_tripped"] = True
            trip = "DAILY_LOSS"
        st.update(equity=e, total_breach=total, daily_breach=daily)
        self.journal.put(self.KEY, st)
        return trip

    def reset(self, equity: float, now: float) -> list[str]:
        """After the user resumes: only a breaker that tripped is re-based - the peak after TOTAL_DD, the day's
        reference after DAILY_LOSS (a TOTAL_DD trip marks the day too) - starting from this read as the first of
        its three basing reads, and its name is returned. A resume without a trip changes nothing."""
        e = _observed(equity)
        st = self.state()
        done: list[str] = []
        if not st:
            return done
        if st.get("total_tripped"):
            st.update(peak=e, peak_reads=[e], total_tripped=False, total_breach=False)
            done.append("TOTAL_DD")
        if st.get("daily_tripped"):
            st.update(day=ny_date(now), day_ref=e, day_reads=[e], daily_tripped=False, daily_breach=False)
            done.append("DAILY_LOSS")
        if done:
            st["equity"] = e
            self.journal.put(self.KEY, st)
        return done


class SessionWatch:
    def __init__(self) -> None:
        self.state: str | None = None
        self.noticed: set[int] = set()

    def update(self, info: SessionInfo, now: float) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if self.state is not None and info.state != self.state:
            out.append(("SESSION", f"장 운영 상태 {self.state} → {info.state}"))
        self.state = info.state
        start, end = info.maintenance_start, info.maintenance_end
        if (start is not None and start - now <= MAINTENANCE_NOTICE_S and (end is None or end > now)
                and start not in self.noticed):
            self.noticed.add(start)
            when = "진행 중" if start <= now else "예정"
            until = "미정" if end is None else f"{datetime.fromtimestamp(end, NY):%H:%M}"
            out.append(("MAINTENANCE", f"정기 점검 {when} {datetime.fromtimestamp(start, NY):%Y-%m-%d %H:%M} ~ "
                                       f"{until} (뉴욕)"))
        return out


class Feed(Protocol):
    def latest(self, product_id: str) -> Any: ...


class PriceSource:
    def __init__(self, exchange: Exchange, feed: Feed | None = None, *, clock: Callable[[], float],
                 stale_s: float = WS_STALE_S, poll_s: float = REST_POLL_S) -> None:
        self.ex = exchange
        self.feed = feed
        self.clock = clock
        self.stale_s = stale_s
        self.poll_s = poll_s
        self.mode = "rest"
        self._rest: float | None = None
        self._rest_ts: float | None = None  # when the last good REST price was read
        self._tried: float | None = None  # the last REST read, good or not

    def futures_price(self) -> float | None:
        """The WebSocket price while a usable one arrived in the last `stale_s`; otherwise the REST quote's
        mid, read at most every `poll_s` (a failed read or a book without a finite bid and ask above 0 keeps
        the last good price) and returned only while it is at most `stale_s` old; else None."""
        now = self.clock()
        if self.feed is not None:
            tick = self.feed.latest(self.ex.futures_id)
            if tick is not None and now - tick.ts <= self.stale_s and usable_price(tick.price) is not None:
                self.mode = "ws"
                return float(tick.price)
        self.mode = "rest"
        if self._tried is None or not 0 <= now - self._tried < self.poll_s:
            self._tried = now
            try:
                q = self.ex.quote(self.ex.futures_id)
                if usable_price(q.bid) is not None and usable_price(q.ask) is not None:
                    self._rest, self._rest_ts = q.mid, now
            except ExchangeError:
                pass
        if self._rest_ts is None or now - self._rest_ts > self.stale_s:
            return None
        return self._rest
```

**File: `src/ewbot/live/feed.py`**

```python
"""WebSocket ticker feed (Plan 3 spec 2 `WebSocket`, 5.1).

Coinbase's public market-data endpoint (PUBLIC_WS_URL: spot and US futures) serves the `ticker` and
`heartbeats` channels without a key or JWT (WebSocket Endpoints reference, checked 2026-10-03), so the feed
holds no credential. It connects with the `websockets` sync client (dependency group `live`) and the
default TLS context, which verifies the certificate chain and the host name; the SDK's WSClient is not used
(SDK 1.8.4 opens wss:// with an SSLContext that verifies neither). This class subscribes to both channels
for the futures and spot products, keeps the price per product - the mid of the ticker's best bid and ask
(what the REST quote measures), its last trade price only when those are missing; only a finite price above
0 - and the time of the last message, and watches the per-connection `sequence_num` for gaps.

`run` owns every reconnect. A connection that fails, closes (a clean 1000/1001 close included), raises, or
brings no message for SILENT_S (the heartbeats channel sends one every second, so silence means the
connection is gone) is torn down and opened again after an exponential backoff: 1 s doubling to 60 s, back
to 1 s only once a connection has carried messages for HEALTHY_S. `stop()` also ends a backoff wait at once.
Every (re)connect and every sequence gap sets `resync`, so the runtime re-reads positions and orders over
REST. A message that cannot be read is counted (`unreadable_messages`), a ticker entry without a usable
price too (`unpriced_tickers`); neither is ever raised. A server `error` message (a refused subscription,
say) is kept in `last_error` and is no sign of life: only other messages count as liveness. `failures` and
`last_error` (an exception's type name only, "silent for 15 s", or the server's error message) tell the
runtime why the feed is down. The runtime reads prices through watchdog.PriceSource, which uses REST polling once
the feed has been silent for 60 s."""

from __future__ import annotations

import json
import math
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

CHANNELS = ("ticker", "heartbeats")
MAX_BACKOFF_S = 60.0
SILENT_S = 15.0  # no message for this long (heartbeats come every second): the connection is gone
HEALTHY_S = 60.0  # a connection that carried messages this long resets the backoff to 1 s
OPEN_TIMEOUT_S = 10  # the opening handshake (TCP, TLS, HTTP upgrade) times out after this
PUBLIC_WS_URL = "wss://advanced-trade-ws.coinbase.com"  # public market data: no key, no JWT


@dataclass(frozen=True, slots=True)
class Tick:
    price: float
    ts: float


def usable_price(x: Any) -> float | None:
    """A finite price above 0, else None."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def ticker_price(t: dict[str, Any]) -> float | None:
    """The mid of the ticker's best bid and ask; its last trade price when either is missing or unusable."""
    bid, ask = usable_price(t.get("best_bid")), usable_price(t.get("best_ask"))
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return usable_price(t.get("price"))


class _Silent(Exception):
    pass


class TickerFeed:
    def __init__(self, products: Sequence[str], ws_factory: Callable[..., Any], *,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], Any] | None = None,
                 max_backoff: float = MAX_BACKOFF_S, silent_s: float = SILENT_S,
                 healthy_s: float = HEALTHY_S) -> None:
        self.products = list(products)
        self.ws_factory = ws_factory
        self.clock = clock
        self._stop = threading.Event()
        self.sleep = self._stop.wait if sleep is None else sleep  # the backoff wait: stop() ends the default one
        self.max_backoff = max_backoff
        self.silent_s = silent_s
        self.healthy_s = healthy_s
        self.last_msg: float | None = None
        self.last_seq: int | None = None
        self.gaps = 0
        self.connects = 0
        self.failures = 0
        self.messages = 0
        self.unreadable_messages = 0
        self.unpriced_tickers = 0
        self.last_error: str | None = None
        self._ticks: dict[str, Tick] = {}
        self._resync = False
        self._lock = threading.Lock()

    def on_message(self, raw: str) -> None:
        """Called from PublicSocket.pump on the feed's thread: never raises (that would end the connection)."""
        try:
            self._read(raw)
        except Exception:  # noqa: BLE001 - a message we cannot read is counted, never raised
            with self._lock:
                self.unreadable_messages += 1

    def _read(self, raw: str) -> None:
        msg = json.loads(raw)
        if not isinstance(msg, dict):
            raise ValueError("a message that is not a JSON object")
        ticks: dict[str, float] = {}
        unusable = 0
        if msg.get("channel") == "ticker":
            for ev in msg.get("events") or []:
                for t in ev.get("tickers") or []:
                    if not t.get("product_id"):
                        continue
                    price = ticker_price(t)
                    if price is None:
                        unusable += 1
                    else:
                        ticks[t["product_id"]] = price
        now = self.clock()
        with self._lock:
            self.unpriced_tickers += unusable
            if msg.get("type") == "error":  # e.g. a refused subscription: recorded, but no sign of life
                self.last_error = f"server error: {msg.get('message')}"[:160]
            else:
                self.last_msg = now
                self.messages += 1
            seq = msg.get("sequence_num")
            if isinstance(seq, int):
                if self.last_seq is not None and seq != self.last_seq + 1:
                    self.gaps += 1
                    self._resync = True
                self.last_seq = seq
            for product, price in ticks.items():
                self._ticks[product] = Tick(price, now)

    def latest(self, product_id: str) -> Tick | None:
        with self._lock:
            return self._ticks.get(product_id)

    def age(self) -> float | None:
        with self._lock:
            return None if self.last_msg is None else self.clock() - self.last_msg

    def take_resync(self) -> bool:
        with self._lock:
            flag, self._resync = self._resync, False
            return flag

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            ws = None
            try:
                ws = self.ws_factory(on_message=self.on_message)
                ws.open()
                ws.subscribe(self.products, list(CHANNELS))
                with self._lock:
                    self.connects += 1
                    self.last_seq = None
                    self._resync = True
                    seen = self.messages
                up = silent = 0
                while not self._stop.is_set():
                    ws.pump(1)  # hands over what arrives within 1 s; raises when the connection is lost
                    up += 1
                    with self._lock:
                        count = self.messages
                    silent, seen = (0 if count != seen else silent + 1), count
                    if silent >= self.silent_s:
                        raise _Silent(f"silent for {silent} s")
                    if up >= self.healthy_s:
                        backoff = 1.0  # a healthy connection: the next outage starts from 1 s again
            except Exception as exc:  # noqa: BLE001 - any connection failure: back off and reconnect
                with self._lock:
                    self.failures += 1
                    self.last_error = str(exc) if isinstance(exc, _Silent) else type(exc).__name__
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:  # noqa: BLE001 - closing a dead socket may fail too
                        pass
            if self._stop.is_set():
                return
            self.sleep(backoff)
            backoff = min(backoff * 2.0, self.max_backoff)

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.run, name="ewbot-ticker", daemon=True)
        th.start()
        return th


class PublicSocket:
    """One connection to Coinbase's public market-data WebSocket, as TickerFeed drives it. `connect` is
    `websockets.sync.client.connect` (tests pass a fake); a wss:// URL gets the default TLS context."""

    def __init__(self, url: str, on_message: Callable[[str], None], *,
                 connect: Callable[..., Any] | None = None, open_timeout: float = OPEN_TIMEOUT_S) -> None:
        self.url = url
        self.on_message = on_message
        self.connect = connect
        self.open_timeout = open_timeout
        self.conn: Any = None

    def open(self) -> None:
        if self.connect is None:
            from websockets.sync.client import connect  # dependency group `live`

            self.connect = connect
        ctx = ssl.create_default_context() if self.url.startswith("wss://") else None
        self.conn = self.connect(self.url, ssl=ctx, open_timeout=self.open_timeout)

    def subscribe(self, products: list[str], channels: list[str]) -> None:
        for channel in channels:  # one subscribe message per channel; public channels take no jwt
            self.conn.send(json.dumps({"type": "subscribe", "product_ids": list(products), "channel": channel}))

    def pump(self, seconds: float) -> None:
        """Hand every message that arrives within `seconds` to on_message; a lost or closed connection raises
        (websockets' ConnectionClosed, a clean close included)."""
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            try:
                raw = self.conn.recv(timeout=left)
            except TimeoutError:
                return
            self.on_message(raw if isinstance(raw, str) else bytes(raw).decode("utf-8", "replace"))

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()


def public_ws_factory(url: str = PUBLIC_WS_URL, *,
                      connect: Callable[..., Any] | None = None) -> Callable[..., PublicSocket]:
    """TickerFeed's ws_factory for production: a PublicSocket on Coinbase's public endpoint (no credential)."""

    def make(on_message: Callable[[str], None]) -> PublicSocket:
        return PublicSocket(url, on_message, connect=connect)

    return make
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run --group live pytest tests/live/test_watchdog.py -q
```

Expected: `27 passed` (without the `live` group the `websockets` test is skipped: `26 passed, 1 skipped`).

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/live/watchdog.py src/ewbot/live/feed.py tests/live/test_watchdog.py pyproject.toml uv.lock
git commit -m "$(cat <<'EOF'
feat(live): watchdog (stop gap, margin, account breakers, session) and WebSocket ticker feed

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: `[LIVE]` notifier, Telegram commands and the heartbeat

**Files:**
- Create: `src/ewbot/live/notify.py`, `src/ewbot/live/control.py`, `src/ewbot/live/health.py`
- Test: `tests/live/test_control.py`

**Interfaces:**
- Consumes: `ewbot.paper.notify` (`Sender`, `deliver`, `make_event`; import only), `ewbot.live.journal.Journal`, `requests`, `tests.paper.helpers.FakeSender`, `tests.live.helpers` (`Clock`, `journal`).
- Produces (`ewbot.live.notify`): `PREFIX = "[LIVE]"`, `DEDUPE_S = 600.0`, `MAX_CHARS = 4000`, `MAX_PENDING = 100`, `MAX_AGE_S = 1800.0`, `fit(text, limit=MAX_CHARS) -> str` (cut in UTF-16 code units, Telegram's measure, ending `…(잘림)`), `refused(error) -> bool` (a send error Telegram gives again for the same message: HTTP 4xx other than 429); `class LiveNotifier(sender, *, clock=time.time, sleep=None, dedupe_s=600.0, max_pending=100)`, callable `(kind, text) -> bool` (only queues: never blocks or raises; False for a duplicate - queued, or delivered within `dedupe_s`, counted from the delivery - or without a sender: then stderr; a full queue drops its oldest waiting message), `pump() -> float | None` (one delivery attempt of the oldest message: None when idle, 0.0 after a delivery or a drop, else the pause before the retry - 2 s doubling to 60 s, for a network error, HTTP 429 or 5xx; a `refused` message is dropped at once, never holding the queue, and counted in `refusals`; a message is kept `MAX_AGE_S`; its full text goes to stderr on its first failure and when dropped; one delivered 60 s or more late ends with `(N분 지연)`), `run()`, `start() -> Thread | None` (the sending thread; none without a sender), `flush(timeout) -> bool`, `stop()` (at a flush timeout and at stop every undelivered text goes to stderr; `sleep=None`: the retry pause waits on the stop event), attributes `pending`, `oldest_pending` (when the oldest waiting message was queued), `failing_since` (when the oldest waiting message that has failed was queued; None when none has - it clears on delivery or drop), `last_attempt`, `refusals` (in a row; a delivery resets it), `sent` (the latest 50 texts), `sent_count`, `dropped`, `failures`, `last_error`.
- Produces (`ewbot.live.control`): `COMMANDS = ("/status", "/pause", "/resume", "/flatten", "/flatten_confirm", "/help")`, `OFFSET_KEY = "telegram_offset"` (stored as `{"offset", "ts"}`), `OFFSET_MAX_AGE_S = 86_400.0`, `ERROR_BACKOFF_S = 5.0`, `MAX_BACKOFF_S = 60.0`, `ME_RETRY_S = 60.0`, `ME_RETRY_MAX_S = 1800.0`, `class ControlError(RuntimeError)` (`status`, `retry_after`; its message is a type name or the HTTP status with Telegram's `description`, never the token, and it is raised outside any `except` block, so neither `__cause__` nor `__context__` holds the original), NamedTuple `Command(word, sent)` (`sent`: the message's Telegram date), `command_word(text, bot=None) -> str | None` (total; `/cmd@<bot>` only for this bot's username), `class TelegramControl(token, chat_id, journal, *, session=None, timeout=25, sleep=None, clock=time.time)` with `fetch() -> list[Command]` (messages of the private chat `chat_id` from that user only - no group, channel, edited, forwarded or via-bot message, no command addressed to another bot; the offset stored once per batch, before the batch is returned: at most once; the first start - no stored offset - skips the backlog with getUpdates offset -1 and acts on nothing; an offset stored more than `OFFSET_MAX_AGE_S` ago is not sent), `poll_once(queue)` (getMe first while `bot` is unknown and due - again after 60 s doubling to 30 min until it answers, never a failed poll; never raises: a failure is counted, printed to stderr once per change of state and waited out - 5 s doubling to 60 s, HTTP 429's `retry_after`), `overdue(now) -> float` (seconds the thread is behind its own schedule: the latest poll's end or `start()` + its back-off + a getMe and one long poll; grows while a request hangs), `run(queue)` (polls until `stop()`), `start(queue) -> Thread`, `stop()` (`sleep=None`: the back-off waits on the stop event, so `stop()` ends it), property `alive`, attributes `bot`, `ignored`, `skipped`, `failures` (in a row), `conflicts` (HTTP 409 answers), `last_error`, `last_ok`, `started`, `last_poll_end`, `wait_s`.
- Produces (`ewbot.live.health`): `fail_url(url) -> str` (`<url>/fail`, a query string kept); `class Heartbeat(url, *, session=None, clock=time.time, interval=60.0, timeout=5.0)` with `beat() -> bool` (an exception or an HTTP status of 400 or more is a failure; stderr once per change of state), `fail(message) -> bool`, attributes `url`, `failures` (in a row), `last_error`.

- [ ] **Step 1: Write the failing test**

**File: `tests/live/test_control.py`**

```python
from __future__ import annotations

import queue
import sqlite3
import threading
import time
import traceback
from typing import Any

import pytest
import requests

from ewbot.live.control import (
    OFFSET_KEY,
    Command,
    ControlError,
    TelegramControl,
    command_word,
)
from ewbot.live.health import Heartbeat, fail_url
from ewbot.live.notify import LiveNotifier, fit
from ewbot.paper.notify import SendError
from tests.live.helpers import Clock, journal
from tests.paper.helpers import FakeSender

TOKEN = "123456:SECRET-TOKEN"


class Resp:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Session:
    """getUpdates (and heartbeat GETs) answer from `answers` in order, then with empty batches; getMe answers
    with the bot `ewbot_bot` (after `me_failures` connection errors); POSTs answer from `posts`."""

    def __init__(self, answers: list, posts: list | None = None, me_failures: int = 0) -> None:
        self.answers = answers
        self.posts = posts or []
        self.me_failures = me_failures
        self.calls: list[tuple] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, dict(params or {}), timeout))
        if url.endswith("/getMe") and self.me_failures:
            self.me_failures -= 1
            a: Any = requests.exceptions.ConnectionError("getMe down")
        elif url.endswith("/getMe"):
            a = Resp(200, {"ok": True, "result": {"id": 5, "is_bot": True, "username": "ewbot_bot"}})
        elif self.answers:
            a = self.answers.pop(0)
        else:
            time.sleep(0.01)  # a long poll that brought nothing
            a = ok()
        if isinstance(a, BaseException):
            raise a
        return a

    def post(self, url, data=None, timeout=None, json=None):
        self.calls.append(("POST", url, data, timeout))
        a = self.posts.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a


def ok(*updates: dict) -> Resp:
    return Resp(200, {"ok": True, "result": list(updates)})


def upd(uid: int, text: str, *, chat: int = 42, sender: int = 42, date: int = 1_000, kind: str = "private",
        **extra: Any) -> dict:
    msg = {"message_id": uid, "date": date, "chat": {"id": chat, "type": kind},
           "from": {"id": sender, "is_bot": False}, "text": text, **extra}
    return {"update_id": uid, "message": msg}


def test_command_words():
    assert command_word("/pause") == "/pause" and command_word("/Flatten_Confirm") == "/flatten_confirm"
    assert command_word("/status please") == "/status" and command_word("  /help\n") == "/help"
    assert command_word("/Flatten_Confirm@EwBot_Bot", "ewbot_bot") == "/flatten_confirm"
    assert command_word("/pause@other_bot", "ewbot_bot") is None  # addressed to another bot
    assert command_word("/pause@ewbot_bot") is None  # this bot's name unknown (getMe failed): bare commands only
    assert command_word("pause") is None and command_word("/rm -rf") is None and command_word("/flatten_confirmed") is None
    for blank in ("", "   ", "　", "\xa0", "\n"):
        assert command_word(blank) is None


def test_only_the_users_private_chat_is_obeyed_and_the_offset_is_stored_once_per_batch(tmp_path):
    jr = journal(tmp_path)
    jr.put(OFFSET_KEY, {"offset": 10, "ts": 4_000.0})  # a running bot: updates up to 9 were handled
    s = Session([ok(upd(10, "/pause", date=4_990), upd(11, "/flatten_confirm", chat=999, sender=999),
                    upd(12, "hello"), upd(13, "/status@ewbot_bot", date=4_995),
                    upd(14, "/resume", sender=777),  # someone else
                    upd(15, "/resume", chat=-100, kind="group"),
                    upd(16, "/pause", forward_origin={"type": "user"}), upd(17, "/pause", forward_date=4_000),
                    upd(18, "/pause", via_bot={"id": 5}), upd(19, "/pause@other_bot"),
                    {"update_id": 20, "edited_message": upd(20, "/flatten_confirm")["message"]},
                    {"update_id": 21, "channel_post": {"chat": {"id": 42, "type": "channel"}, "text": "/flatten"}})])
    writes: list[str] = []
    put = jr.put
    jr.put = lambda key, value: (writes.append(key), put(key, value))  # type: ignore[method-assign]
    c = TelegramControl(TOKEN, "42", jr, session=s, clock=Clock(5_000.0))
    c.bot = "ewbot_bot"  # getMe's answer (run() asks once)
    assert c.fetch() == [Command("/pause", 4_990.0), Command("/status", 4_995.0)] and c.ignored == 8
    assert writes == [OFFSET_KEY] and jr.get(OFFSET_KEY) == {"offset": 22, "ts": 5_000.0}  # once, before the batch
    TelegramControl(TOKEN, "42", jr, session=s, clock=Clock(5_010.0)).fetch()  # a restart resumes after update 21
    params = s.calls[-1][2]
    assert s.calls[-1][1].endswith("/getUpdates") and params["offset"] == 22 and params["allowed_updates"] == '["message"]'
    jr.close()


def test_the_first_start_skips_the_backlog_and_a_day_old_offset_is_not_sent(tmp_path):
    jr = journal(tmp_path)
    clock = Clock(100_000.0)
    s = Session([ok(upd(7, "/flatten", date=20_000)), ok(upd(8, "/status", date=99_990))])
    c = TelegramControl(TOKEN, "42", jr, session=s, clock=clock)
    assert c.fetch() == [] and c.skipped == 1  # yesterday's /flatten is never acted on
    assert (s.calls[0][2]["offset"], s.calls[0][2]["timeout"]) == (-1, 0)  # Telegram drops all but the newest
    assert jr.get(OFFSET_KEY) == {"offset": 8, "ts": 100_000.0}
    assert c.fetch() == [Command("/status", 99_990.0)] and s.calls[1][2]["offset"] == 8
    clock.now += 86_401  # a day without any update: the stored offset is not sent (updates are kept 24 h at most)
    assert c.fetch() == [] and "offset" not in s.calls[2][2]
    empty = TelegramControl(TOKEN, "42", journal(tmp_path, "j2.sqlite"), session=Session([]), clock=clock)
    assert empty.fetch() == [] and empty.skipped == 0 and empty.journal.get(OFFSET_KEY)["offset"] is None
    empty.fetch()  # primed: an ordinary long poll from now on
    assert "offset" not in empty.session.calls[1][2] and empty.session.calls[1][2]["timeout"] == 25
    jr.close()
    empty.journal.close()


def _chain(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    todo: list[BaseException | None] = [exc]
    while todo:
        e = todo.pop()
        if e is not None and e not in out:
            out.append(e)
            todo += [e.__cause__, e.__context__]
    return out


def test_errors_never_carry_the_token(tmp_path):
    jr = journal(tmp_path)
    jr.put(OFFSET_KEY, {"offset": 1, "ts": 0.0})
    leak = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    s = Session([requests.exceptions.ConnectionError(leak), Resp(200, ValueError(leak)),
                 Resp(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})])
    c = TelegramControl(TOKEN, "42", jr, session=s, clock=Clock(0.0))
    for _ in range(3):
        with pytest.raises(ControlError) as err:
            c.fetch()
        assert _chain(err.value) == [err.value]  # neither __cause__ nor __context__ keeps the original
        text = str(err.value) + repr(err.value) + "".join(traceback.format_exception(err.value))
        assert TOKEN not in text and "SECRET" not in text
    jr.close()


def test_poll_failures_are_counted_reported_once_per_change_and_backed_off(tmp_path, capsys):
    jr = journal(tmp_path)
    jr.put(OFFSET_KEY, {"offset": 1, "ts": 0.0})
    conflict = {"ok": False, "error_code": 409,
                "description": "Conflict: terminated by other getUpdates request; make sure that only one bot instance is running"}
    s = Session([Resp(409, conflict), Resp(409, conflict), Resp(401, {"ok": False, "description": "Unauthorized"}),
                 Resp(429, {"ok": False, "description": "Too Many Requests: retry after 30", "parameters": {"retry_after": 30}}),
                 Resp(502, requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)),
                 ok(upd(1, "/status", date=0))])
    waits: list[float] = []
    c = TelegramControl(TOKEN, "42", jr, session=s, clock=Clock(0.0), sleep=waits.append)
    q: queue.Queue[Command] = queue.Queue()
    c.poll_once(q)
    assert c.last_error is not None and c.last_error.startswith("getUpdates failed: HTTP 409: Conflict: terminated by other")
    assert c.last_error.endswith("(another getUpdates poller or a webhook uses this bot)")
    c.poll_once(q)
    c.poll_once(q)
    assert c.last_error == "getUpdates failed: HTTP 401: Unauthorized (the bot token was refused)"
    c.poll_once(q)
    c.poll_once(q)
    assert (c.failures, c.conflicts, c.last_error) == (5, 2, "getUpdates failed: HTTP 502")  # the status, not JSONDecodeError
    assert waits == [5.0, 10.0, 20.0, 30.0, 60.0]  # doubling to 60 s; HTTP 429: Telegram's retry_after
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 4 and "HTTP 409" in lines[0] and "HTTP 401" in lines[1] and "HTTP 429" in lines[2]
    assert "HTTP 502" in lines[3]  # the second 409 in a row is not printed again
    c.poll_once(q)
    assert c.failures == 0 and c.last_ok == 0.0 and q.get_nowait() == Command("/status", 0.0)
    assert capsys.readouterr().err == "[LIVE] telegram commands ok again after 5 failed polls\n"
    jr.close()


def test_the_thread_survives_any_error_and_stop_ends_its_wait(tmp_path, monkeypatch):
    monkeypatch.setattr("ewbot.live.control.ERROR_BACKOFF_S", 60.0)  # a back-off far longer than the test waits
    jr = journal(tmp_path)
    jr.put(OFFSET_KEY, {"offset": 1, "ts": time.time()})
    s = Session([ok(upd(1, "　")), ok({"message": {"chat": {"id": 42}, "text": "/pause"}}),  # no update_id
                 ok(upd(2, "/flatten", date=int(time.time())))])
    c = TelegramControl(TOKEN, "42", jr, session=s)
    q: queue.Queue[Command] = queue.Queue()
    th = c.start(q)
    assert q.get(timeout=5).word == "/flatten" and c.alive and c.bot == "ewbot_bot"

    def broken(key, value):
        raise sqlite3.OperationalError("database is locked")

    jr.put = broken  # type: ignore[method-assign]
    s.answers.append(ok(upd(3, "/status")))
    deadline = time.monotonic() + 10
    while c.failures == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert c.failures == 1 and c.last_error == "OperationalError" and c.alive and q.empty()
    c.stop()  # ends the 60 s back-off: the thread is gone long before it would have ended
    th.join(15)
    assert not th.is_alive() and not c.alive
    jr.close()


def test_the_notifier_queues_and_never_blocks_its_caller(capsys):
    entered, gate = threading.Event(), threading.Event()
    texts: list[str] = []

    def hanging(text: str) -> None:  # Telegram not answering
        entered.set()
        gate.wait(30)
        texts.append(text)

    n = LiveNotifier(hanging, max_pending=3)
    n.start()
    assert n("STOP_GAP", "a")
    assert entered.wait(10)
    assert n("BREAKER", "b") and n("X", "c") and n("X", "d") and n("X", "e")  # full: the oldest waiting ones go
    assert texts == [] and n.dropped == 2 and n.pending == 3  # every call returned while the send still hangs
    assert "dropped (queue full): [LIVE] b" in capsys.readouterr().err  # the container log keeps it
    gate.set()
    assert n.flush(30) and texts == ["[LIVE] a", "[LIVE] d", "[LIVE] e"]
    n.stop()


def test_undelivered_messages_go_to_the_log_on_shutdown(capsys):
    gate = threading.Event()
    n = LiveNotifier(lambda text: gate.wait(30))  # Telegram hangs
    n.start()
    n("A", "first (in flight)")
    n("FATAL", "[중단] RuntimeError: boom")
    assert not n.flush(0.2)
    n.stop()
    err = capsys.readouterr().err
    assert err.count("[LIVE] first (in flight)") == 1 and err.count("[LIVE] [중단] RuntimeError: boom") == 1
    gate.set()


def test_a_failed_send_is_kept_retried_and_printed_never_deduplicated_away(capsys):
    clock = Clock(1_000.0)
    sender = FakeSender(fail=2)
    n = LiveNotifier(sender, clock=clock)
    alert = "[중대 사건] 거래 1이 60초 넘게 보호되지 않음"
    assert n("INCIDENT", alert) and not n("INCIDENT", alert)  # queued once
    assert (n.failing_since, n.oldest_pending, n.last_attempt) == (None, 1_000.0, None)  # queued, not tried yet
    assert n.pump() == 2.0 and (n.failing_since, n.pending, n.last_attempt) == (1_000.0, 1, 1_000.0)
    assert f"[LIVE] {alert}" in capsys.readouterr().err  # the full text is in the container log
    clock.now += 2
    assert n.pump() == 4.0 and capsys.readouterr().err == ""  # printed once per message
    clock.now += 64
    assert n.pump() == 0.0 and sender.texts == [f"[LIVE] {alert} (1분 지연)"]
    assert n.failing_since is None and n.oldest_pending is None and n.pump() is None
    assert not n("INCIDENT", alert)  # delivered: deduplicated now
    clock.now += 601
    assert n("INCIDENT", alert)
    stale = LiveNotifier(FakeSender(fail=99), clock=clock)
    stale("X", "y")
    stale.pump()
    assert stale.failing_since == clock.now
    clock.now += 1_801  # kept for 30 minutes at most
    assert stale.pump() == 0.0 and (stale.pending, stale.dropped, stale.failing_since) == (0, 1, None)  # cleared


def test_a_refused_message_is_dropped_at_once_and_never_holds_the_queue(capsys):
    clock = Clock(0.0)
    texts: list[str] = []
    answers = {"bad": "telegram HTTP 400: {\"ok\":false,\"description\":\"Bad Request: message text is empty\"}",
               "blocked": "telegram HTTP 403: {\"ok\":false,\"description\":\"Forbidden: bot was blocked by the user\"}",
               "flood": "telegram HTTP 429: {\"ok\":false,\"parameters\":{\"retry_after\":5}}",
               "gateway": "telegram HTTP 502: <html>"}

    def sender(text: str) -> None:
        for word, error in answers.items():
            if word in text:
                raise SendError(error)
        texts.append(text)

    n = LiveNotifier(sender, clock=clock)
    n("AUDIT_MISMATCH", "bad text")
    n("STOP_GAP", "[손절 갭] 시장가 청산")
    assert n.pump() == 0.0 and n.pump() == 0.0 and texts == ["[LIVE] [손절 갭] 시장가 청산"]  # at once, not in 30 min
    assert (n.dropped, n.failing_since, n.refusals, n.pending) == (1, None, 0, 0)  # a delivery resets refusals
    err = capsys.readouterr().err
    assert "HTTP 400" in err and "[LIVE] bad text" in err  # the refused text is in the container log
    for word in ("flood", "gateway"):  # 429 and 5xx are retried
        n("X", word)
        assert n.pump() > 0 and n.pending == 1 and n.failing_since == clock.now
        answers.pop(word)
        assert n.pump() == 0.0 and n.pending == 0
    for i in range(3):
        n("X", f"blocked {i}")
        assert n.pump() == 0.0
    assert n.refusals == 3 and n.pending == 0  # the runtime counts this as Telegram unusable


def test_get_me_is_retried_and_a_thread_that_stops_answering_is_overdue(tmp_path, capsys):
    jr = journal(tmp_path)
    jr.put(OFFSET_KEY, {"offset": 1, "ts": 0.0})
    clock = Clock(0.0)
    s = Session([], me_failures=2)
    c = TelegramControl(TOKEN, "42", jr, session=s, clock=clock, sleep=lambda wait: None)
    q: queue.Queue[Command] = queue.Queue()

    def asked() -> int:
        return sum(1 for call in s.calls if call[1].endswith("/getMe"))

    for t, calls in ((0.0, 1), (10.0, 1), (70.0, 2), (150.0, 2), (200.0, 3)):  # again after 60 s, then 120 s
        clock.now = t
        c.poll_once(q)
        assert asked() == calls
    assert c.bot == "ewbot_bot" and c.failures == 0  # getMe failing never counts as a failed poll
    clock.now = 300.0
    c.poll_once(q)
    assert asked() == 3  # known: not asked again
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 2 and "getMe failed" in err[0] and "getMe ok" in err[1]
    assert c.overdue(300.0 + 45) == 0.0 and c.overdue(300.0 + 45 + 120) == 120.0  # a long poll takes up to 35 s
    s.answers.append(Resp(429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 200}}))
    c.poll_once(q)  # waits 200 s: no lateness while it waits
    assert c.overdue(300.0 + 200 + 45) == 0.0 and c.overdue(300.0 + 200 + 45 + 60) == 60.0
    jr.close()


def test_live_notifier_prefixes_truncates_dedupes_and_prunes():
    clock = Clock(1_000.0)
    sender = FakeSender()
    n = LiveNotifier(sender, clock=clock)
    assert n("ENTRY", "진입 W3 롱") and n.pump() == 0.0 and sender.texts == ["[LIVE] 진입 W3 롱"]
    assert not n("ENTRY", "진입 W3 롱")  # within 10 minutes
    clock.now += 601
    assert n("ENTRY", "진입 W3 롱") and n.pump() == 0.0 and len(sender.texts) == 2
    assert n("DAILY", "x" * 5_000) and n.pump() == 0.0
    assert len(sender.texts[-1]) == 4_000 and sender.texts[-1].endswith("…(잘림)")  # Telegram's limit is 4096
    assert len(fit("😀" * 3_000).encode("utf-16-le")) // 2 <= 4_000  # counted in UTF-16 units, as Telegram does
    for i in range(1_000):
        n("COMMAND", f"reply {i}")
        n.pump()
    clock.now += 601
    assert n("COMMAND", "one more") and len(n._last) == 0  # entries older than 10 minutes are pruned
    assert n.pump() == 0.0 and n.sent_count == 1_004 and len(n.sent) == 50
    assert LiveNotifier(None, clock=clock)("X", "y") is False  # no sender: stderr only


def test_heartbeat_pings_once_a_minute_and_logs_state_changes(capsys):
    clock = Clock(0.0)
    s = Session([Resp(200, {}), Resp(404, {}), requests.exceptions.ConnectionError("down"), Resp(200, {})],
                posts=[Resp(200, {})])
    hb = Heartbeat("https://hc-ping.com/abc?create=1", session=s, clock=clock)
    assert hb.beat() and not hb.beat() and s.calls[0][3] == 5.0
    clock.now = 60.0
    assert not hb.beat() and (hb.failures, hb.last_error) == (1, "HTTP 404")  # e.g. a wrong check UUID
    clock.now = 120.0
    assert not hb.beat() and (hb.failures, hb.last_error) == (2, "ConnectionError")
    clock.now = 180.0
    assert hb.beat() and hb.failures == 0
    assert capsys.readouterr().err.splitlines() == ["[LIVE] heartbeat failed: HTTP 404",
                                                    "[LIVE] heartbeat ok again after 2 failed pings"]
    assert hb.fail("fatal: boom")
    assert s.calls[-1][:3] == ("POST", "https://hc-ping.com/abc/fail?create=1", b"fatal: boom")
    assert fail_url("https://hc-ping.com/abc/") == "https://hc-ping.com/abc/fail"
    assert Heartbeat(None).beat() is False and Heartbeat("  ").url is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --group live pytest tests/live/test_control.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'ewbot.live.control'`.

- [ ] **Step 3: Write minimal implementation**

**File: `src/ewbot/live/notify.py`**

```python
"""[LIVE] Telegram notifications (Plan 3 spec 3.5): the paper runtime's TelegramSender, messages prefixed
"[LIVE]" (the paper runtime's start with "[ewbot paper]").

Never on the trading loop: a call only queues the message and returns at once (it never blocks or raises); a
background thread (`start()`) sends the queue in order, one attempt at a time. A send that fails for a reason
that may pass - a transport error, HTTP 429 or 5xx - keeps the message at the head of the queue and retries it
after a pause that doubles from 2 s to 60 s, for up to 30 minutes (MAX_AGE_S) after it was queued. A message
Telegram refuses outright (any other HTTP 4xx) is dropped at once, so it never holds the messages behind it;
`refusals` counts such refusals in a row (a delivery resets it). Every message's full text reaches stderr when
its send first fails, when it is dropped and when it is still undelivered at flush timeout or stop(), so the
container log always has it. The queue holds MAX_PENDING messages: when it is full the oldest waiting one is
dropped (`dropped` counts them). The same (kind, text) is never queued twice and is sent at most once per 10
minutes, counted from its delivery (a failed send is never deduplicated away). A message is cut to MAX_CHARS
with "…(잘림)" (Telegram refuses more than 4096); one delivered a minute or more late says so.

For the runtime's view of Telegram: `failing_since` is when the oldest waiting message that has failed at least
once was queued (None when no waiting message has failed: it clears when that message is delivered or dropped),
`oldest_pending` when the oldest waiting message was queued and `last_attempt` when the worker last tried one -
waiting messages with no attempt for minutes mean a stuck or never-started worker. Without a sender every
message goes to stderr."""

from __future__ import annotations

import collections
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

from ewbot.paper.notify import Sender, deliver, make_event

PREFIX = "[LIVE]"
DEDUPE_S = 600.0
MAX_CHARS = 4000  # Telegram's limit is 4096 UTF-16 code units: room for the delay note
TRUNCATED = "…(잘림)"
MAX_PENDING = 100
MAX_AGE_S = 1800.0  # a message is retried for 30 minutes, then dropped (it is in the container log)
RETRY_BASE_S = 2.0
RETRY_MAX_S = 60.0
LATE_NOTE_S = 60.0
RECENT = 50  # delivered texts kept in `sent`
_HTTP = re.compile(r"telegram HTTP (\d{3})")  # paper TelegramSender's SendError for a reply that is not ok


def refused(error: str) -> bool:
    """A send error Telegram will give again for the same message: HTTP 4xx other than 429 (flood control)."""
    m = _HTTP.search(error)
    return m is not None and 400 <= int(m.group(1)) < 500 and int(m.group(1)) != 429


def _units(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def fit(text: str, limit: int = MAX_CHARS) -> str:
    """`text`, or its start with TRUNCATED, within `limit` UTF-16 code units (Telegram's measure)."""
    if _units(text) <= limit:
        return text
    room, used, out = limit - _units(TRUNCATED), 0, []
    for ch in text:
        used += 2 if ord(ch) > 0xFFFF else 1
        if used > room:
            break
        out.append(ch)
    return "".join(out) + TRUNCATED


@dataclass(eq=False)
class _Message:
    kind: str
    key: tuple[str, str]
    body: str
    created: float
    seq: int
    failed: bool = False  # a send of it has failed (retried)
    logged: bool = False  # its text is in stderr
    final: bool = False  # reported undelivered at flush timeout or stop()


class LiveNotifier:
    def __init__(self, sender: Sender | None, *, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], object] | None = None, dedupe_s: float = DEDUPE_S,
                 max_pending: int = MAX_PENDING) -> None:
        self.sender = sender
        self.clock = clock
        self.dedupe_s = dedupe_s
        self.max_pending = max_pending
        self._stop = threading.Event()
        self.sleep = sleep if sleep is not None else self._stop.wait  # the worker's pause (stop() ends it)
        self._cond = threading.Condition()
        self._pending: collections.deque[_Message] = collections.deque()
        self._queued: set[tuple[str, str]] = set()
        self._inflight: _Message | None = None
        self._last: dict[tuple[str, str], float] = {}  # delivery time per (kind, text), kept dedupe_s
        self._seq = 0
        self._thread: threading.Thread | None = None
        self.sent: collections.deque[str] = collections.deque(maxlen=RECENT)
        self.sent_count = 0
        self.dropped = 0
        self.failures = 0  # failed sends in a row (the retry pause)
        self.refusals = 0  # messages refused (HTTP 4xx but 429) in a row
        self.last_attempt: float | None = None
        self.last_error: str | None = None

    @property
    def pending(self) -> int:
        with self._cond:
            return len(self._pending)

    @property
    def oldest_pending(self) -> float | None:
        with self._cond:
            return self._pending[0].created if self._pending else None

    @property
    def failing_since(self) -> float | None:
        with self._cond:
            return next((m.created for m in self._pending if m.failed), None)

    def __call__(self, kind: str, text: str) -> bool:
        """Queue a message. False when it is already queued or was delivered within dedupe_s, or without a
        sender (then it is printed to stderr)."""
        now = self.clock()
        key = (kind, text)
        body = fit(f"{PREFIX} {text}")
        with self._cond:
            self._last = {k: t for k, t in self._last.items() if now - t < self.dedupe_s}
            if key in self._queued or key in self._last:
                return False
            if self.sender is None:
                self._last[key] = now
                print(body, file=sys.stderr)
                return False
            if len(self._pending) >= self.max_pending:
                self._drop(next((m for m in self._pending if m is not self._inflight), None), "queue full")
            self._seq += 1
            self._pending.append(_Message(kind, key, body, now, self._seq))
            self._queued.add(key)
            self._cond.notify_all()
        return True

    def _drop(self, m: _Message | None, why: str) -> None:  # with self._cond held
        if m is None or m not in self._pending:
            return
        self._pending.remove(m)
        self._queued.discard(m.key)
        self.dropped += 1
        print(f"[LIVE] telegram message dropped ({why}): {m.body}", file=sys.stderr)
        self._cond.notify_all()

    def pump(self) -> float | None:
        """One delivery attempt for the oldest queued message (the worker thread's step; tests call it directly).
        None when nothing is queued, 0.0 after a delivery or a drop, else the pause before the next attempt."""
        with self._cond:
            if not self._pending or self.sender is None:
                return None
            m = self._inflight = self._pending[0]
        now = self.clock()
        late = now - m.created
        if late > MAX_AGE_S:
            with self._cond:
                self._inflight = None
                self._drop(m, f"not delivered in {MAX_AGE_S / 60:.0f} min")
            return 0.0
        self.last_attempt = now
        text = m.body + (f" ({late / 60:.0f}분 지연)" if late >= LATE_NOTE_S else "")
        _, sent, errors = deliver([make_event(m.kind, int(m.created), m.seq, text)], [], self.sender, attempts=1)
        with self._cond:
            self._inflight = None
            if sent:
                if m in self._pending:
                    self._pending.remove(m)
                self._queued.discard(m.key)
                self._last[m.key] = now
                self.sent.append(text)
                self.sent_count += 1
                self.failures = self.refusals = 0
                self._cond.notify_all()
                return 0.0
            self.last_error = errors[0] if errors else "send failed"
            if refused(self.last_error):  # never accepted on a retry: out of the way at once
                self.refusals += 1
                self._drop(m, f"refused: {self.last_error}")
                return 0.0
            self.failures += 1
            m.failed = True
            if not m.logged:
                m.logged = True
                print(f"[LIVE] telegram send failed, retrying ({self.last_error}): {m.body}", file=sys.stderr)
            return min(RETRY_BASE_S * 2.0 ** min(self.failures - 1, 10), RETRY_MAX_S)

    def run(self) -> None:
        """The worker: sends while anything is queued, until stop(). Never raises."""
        while not self._stop.is_set():
            with self._cond:
                if not self._pending:
                    self._cond.wait(1.0)
                    continue
            try:
                pause = self.pump()
            except Exception as exc:  # noqa: BLE001 - the worker must outlive any error
                self.last_error = type(exc).__name__
                pause = RETRY_MAX_S
            if pause:
                self.sleep(pause)

    def start(self) -> threading.Thread | None:
        if self.sender is None:
            return None
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self.run, name="ewbot-notify", daemon=True)
            self._thread.start()
        return self._thread

    def _report_undelivered(self, why: str) -> None:  # with self._cond held
        for m in self._pending:
            if not m.final:
                m.final = True
                print(f"[LIVE] telegram message not delivered ({why}): {m.body}", file=sys.stderr)

    def flush(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for the queue to empty (the worker sends it); True when it did, else every
        message still waiting goes to stderr."""
        end = time.monotonic() + timeout
        with self._cond:
            while self._pending:
                left = end - time.monotonic()
                if left <= 0:
                    self._report_undelivered("flush timed out")
                    return False
                self._cond.wait(left)
        return True

    def stop(self) -> None:
        """Stop the worker; every message still waiting goes to stderr."""
        self._stop.set()
        with self._cond:
            self._report_undelivered("stopped")
            self._cond.notify_all()
```

**File: `src/ewbot/live/control.py`**

```python
"""Telegram commands (Plan 3 spec 3.2 `control`): /status, /pause, /resume, /flatten (confirmed with
/flatten_confirm sent within 60 s of it) and /help.

Accepted only from a private chat with the bot whose chat id is the configured TELEGRAM_CHAT_ID (the user's own
id) and whose sender is that user: group, channel and edited messages, forwarded messages, messages via a bot
and commands addressed to another bot (`/pause@other_bot`; `/pause@<this bot>` once getMe has named it - asked
before the polls until it answers, again after 60 s doubling to 30 min) are ignored. A background thread long-polls getUpdates and queues `Command(word, sent)`; the runtime executes them on
its main loop, so no trading state is shared across threads. `sent` is the message's Telegram date: the runtime
ignores a command that is old by the time it would run (live/runtime.py COMMAND_MAX_AGE_S) and times the
/flatten confirmation on the messages, not on when it handled them.

The update offset is stored in the journal with the time it was stored, once per batch and before the batch is
handed over: a crash can lose a command (at most once), never repeat one. On the first start (no stored offset)
the backlog is skipped without acting on it. A stored offset older than 24 h is not sent (Telegram keeps
updates 24 h at most, and may number the next update at random after a week without any): getUpdates then
returns what is unconfirmed, and the runtime's age check still applies.

The thread never dies of an error: every exception is counted (`failures` in a row, `last_error`; `conflicts`
counts HTTP 409 - another getUpdates poller or a webhook on this bot), waited out (5 s doubling to 60 s, or
HTTP 429's retry_after; stop() ends the wait) and printed to stderr once per change of state; `alive` tells the
runtime whether the thread runs and `overdue(now)` how far it is behind its own schedule (a hung request). The HTTP layer's exceptions may contain the bot token (it is part of the URL),
so only their type name, the HTTP status and Telegram's own `description` are ever reported."""

from __future__ import annotations

import queue
import sys
import threading
import time
from typing import Any, Callable, NamedTuple

import requests

from ewbot.live.journal import Journal

API = "https://api.telegram.org/bot{token}/{method}"
COMMANDS = ("/status", "/pause", "/resume", "/flatten", "/flatten_confirm", "/help")
OFFSET_KEY = "telegram_offset"
OFFSET_MAX_AGE_S = 86_400.0
POLL_TIMEOUT_S = 25
ERROR_BACKOFF_S = 5.0
MAX_BACKOFF_S = 60.0
MAX_RETRY_AFTER_S = 300.0
ME_RETRY_S = 60.0  # getMe asked again after this, doubling up to ME_RETRY_MAX_S, until it answers
ME_RETRY_MAX_S = 1800.0
ME_TIMEOUT_S = 10.0
FORWARDED = ("forward_origin", "forward_from", "forward_from_chat", "forward_sender_name", "forward_date")
HINTS = {401: "the bot token was refused", 409: "another getUpdates poller or a webhook uses this bot"}


class ControlError(RuntimeError):
    """A Telegram call failed. The message never contains the token: an exception's type name, or the HTTP
    status with Telegram's description. `status` and `retry_after` (HTTP 429) when Telegram answered."""

    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class Command(NamedTuple):
    word: str  # one of COMMANDS
    sent: float  # the message's Telegram date (Unix seconds)


def command_word(text: str, bot: str | None = None) -> str | None:
    """'/pause now' -> '/pause'; '/pause@<bot>' only when `bot` is this bot's username (unknown: bare commands
    only); anything that is not a known command -> None."""
    parts = (text or "").split()
    if not parts:
        return None
    word, _, target = parts[0].partition("@")
    if target and (bot is None or target.lower() != bot.lower()):
        return None
    word = word.lower()
    return word if word in COMMANDS else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _stored_offset(stored: Any, now: float) -> int | None:
    """The stored offset when it is an update id stored at most OFFSET_MAX_AGE_S ago, else None (not sent)."""
    s = _dict(stored)
    off, ts = s.get("offset"), s.get("ts")
    if not isinstance(off, int) or isinstance(off, bool) or not isinstance(ts, (int, float)):
        return None
    return off if now - float(ts) <= OFFSET_MAX_AGE_S else None


class TelegramControl:
    def __init__(self, token: str, chat_id: str, journal: Journal, *, session: Any | None = None,
                 timeout: int = POLL_TIMEOUT_S, sleep: Callable[[float], object] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._token = token
        self.chat_id = str(chat_id).strip()
        self.journal = journal
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout
        self.clock = clock
        self._stop = threading.Event()
        self.sleep = sleep if sleep is not None else self._stop.wait  # the back-off pause (stop() ends it)
        self.bot: str | None = None  # this bot's username, from getMe (asked until it answers)
        self.ignored = 0  # updates not from the user's private chat (or forwarded, edited, via a bot)
        self.skipped = 0  # backlog updates skipped on the first start
        self.failures = 0  # failed polls in a row
        self.conflicts = 0  # HTTP 409 answers so far
        self.last_error: str | None = None
        self.last_ok: float | None = None
        self.started: float | None = None  # start() time
        self.last_poll_end: float | None = None  # the latest poll's end, ok or failed
        self.wait_s = 0.0  # the back-off after that poll
        self._me_failures = 0
        self._me_next = 0.0
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ Telegram calls
    def _call(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        """GET a Bot API method -> its result. Raises ControlError outside any except block, so neither its
        __cause__ nor its __context__ holds the token-bearing exception."""
        failed: str | None = None
        status: int | None = None
        resp: Any = None
        data: Any = None
        try:
            resp = self.session.get(API.format(token=self._token, method=method), params=params, timeout=timeout)
            status = int(resp.status_code)
        except Exception as exc:  # noqa: BLE001 - the URL holds the token: type name only
            failed = type(exc).__name__
        if failed is None:
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001 - not JSON (an HTML error page): the status tells
                data = None
        if failed is not None:
            raise ControlError(f"{method} failed: {failed}")
        body = _dict(data)
        if body.get("ok") is True:
            return body.get("result")
        desc = str(body.get("description") or "")[:120]
        if self._token:
            desc = desc.replace(self._token, "<token>")
        retry = _dict(body.get("parameters")).get("retry_after")
        hint = HINTS.get(status or 0)
        raise ControlError(f"{method} failed: HTTP {status}" + (f": {desc}" if desc else "") + (f" ({hint})" if hint else ""),
                           status=status, retry_after=float(retry) if isinstance(retry, (int, float)) else None)

    def _ids(self, result: Any) -> tuple[list[dict[str, Any]], list[int]]:
        updates = [u for u in (result if isinstance(result, list) else []) if isinstance(u, dict)]
        ids = [u["update_id"] for u in updates if isinstance(u.get("update_id"), int) and not isinstance(u["update_id"], bool)]
        return updates, ids

    def _accept(self, upd: dict[str, Any]) -> Command | None:
        msg = upd.get("message")
        chat, sender = _dict(_dict(msg).get("chat")), _dict(_dict(msg).get("from"))
        if (not isinstance(msg, dict) or chat.get("type") != "private" or str(chat.get("id")) != self.chat_id
                or str(sender.get("id")) != self.chat_id or any(f in msg for f in FORWARDED)
                or "via_bot" in msg or not isinstance(msg.get("date"), (int, float))):
            self.ignored += 1
            return None
        text = msg.get("text")
        word = command_word(text if isinstance(text, str) else "", self.bot)
        return None if word is None else Command(word, float(msg["date"]))

    def fetch(self) -> list[Command]:
        """One getUpdates long poll -> the user's commands, in order. Raises on any failure."""
        stored = self.journal.get(OFFSET_KEY)
        if stored is None:
            return self._skip_backlog()
        params: dict[str, Any] = {"timeout": self.timeout, "allowed_updates": '["message"]'}
        offset = _stored_offset(stored, self.clock())
        if offset is not None:
            params["offset"] = offset
        updates, ids = self._ids(self._call("getUpdates", params, self.timeout + 10))
        if ids:  # once per batch, before anything is handed over: a crash loses a command, never repeats one
            self.journal.put(OFFSET_KEY, {"offset": max(ids) + 1, "ts": self.clock()})
        out: list[Command] = []
        for upd in updates:
            cmd = self._accept(upd)
            if cmd is not None:
                out.append(cmd)
        return out

    def _skip_backlog(self) -> list[Command]:
        """First start: offset -1 makes Telegram drop every waiting update but the newest; the offset after it is
        stored and nothing is acted on (an empty backlog stores no update id: from now on ordinary polls)."""
        _, ids = self._ids(self._call("getUpdates", {"offset": -1, "timeout": 0, "allowed_updates": '["message"]'}, 15))
        self.skipped += len(ids)
        self.journal.put(OFFSET_KEY, {"offset": max(ids) + 1 if ids else None, "ts": self.clock()})
        return []

    def _learn_name(self) -> None:
        """getMe -> `bot`. A failure (bare /commands only meanwhile) is asked again after ME_RETRY_S doubling."""
        name: Any = None
        why = "no username"
        try:
            name = _dict(self._call("getMe", {}, ME_TIMEOUT_S)).get("username")
        except Exception as exc:  # noqa: BLE001 - never a failed poll: the commands go on, bare only
            why = self._why(exc)
        if isinstance(name, str) and name:
            if self._me_failures:
                print(f"[LIVE] telegram getMe ok after {self._me_failures} failures", file=sys.stderr)
            self.bot, self._me_failures = name, 0
            return
        if self._me_failures == 0:
            print(f"[LIVE] telegram getMe failed ({why}): only bare /commands until it answers", file=sys.stderr)
        self._me_failures += 1
        self._me_next = self.clock() + min(ME_RETRY_S * 2.0 ** min(self._me_failures - 1, 10), ME_RETRY_MAX_S)

    # ------------------------------------------------------------------ the thread
    @staticmethod
    def _why(exc: Exception) -> str:
        return str(exc) if isinstance(exc, ControlError) else type(exc).__name__

    def poll_once(self, commands: queue.Queue[Command]) -> None:
        """One long poll (getMe first while the bot's name is unknown and due); its commands go to `commands`. Any
        error is counted and waited out, never raised."""
        if self.bot is None and self.clock() >= self._me_next:
            self._learn_name()
        try:
            batch = self.fetch()
        except Exception as exc:  # noqa: BLE001 - the thread must outlive any error (type name only: the token)
            why = self._why(exc)
            if self.failures == 0 or why != self.last_error:
                print(f"[LIVE] telegram commands: {why} (retrying)", file=sys.stderr)
            self.last_error = why  # before the count: the runtime reads them from another thread
            if isinstance(exc, ControlError) and exc.status == 409:
                self.conflicts += 1
            self.failures += 1
            wait = min(ERROR_BACKOFF_S * 2.0 ** min(self.failures - 1, 10), MAX_BACKOFF_S)
            if isinstance(exc, ControlError) and exc.retry_after:
                wait = min(max(exc.retry_after, 1.0), MAX_RETRY_AFTER_S)
            self.wait_s, self.last_poll_end = wait, self.clock()
            self.sleep(wait)
            return
        if self.failures:
            print(f"[LIVE] telegram commands ok again after {self.failures} failed polls", file=sys.stderr)
        self.failures = 0
        self.wait_s = 0.0
        self.last_ok = self.last_poll_end = self.clock()
        for cmd in batch:
            commands.put(cmd)

    def overdue(self, now: float) -> float:
        """Seconds the running thread is behind its own schedule: the next poll ends at the latest one's end (or
        start()) plus its back-off, a getMe and one long poll (timeout + 10 s each way). 0 while it keeps time;
        growing when a request hangs - the thread cannot be replaced then (the hung request would keep polling)."""
        ref = self.last_poll_end if self.last_poll_end is not None else self.started
        if ref is None:
            return 0.0
        return max(0.0, now - (ref + self.wait_s + ME_TIMEOUT_S + self.timeout + 10))

    def run(self, commands: queue.Queue[Command]) -> None:
        while not self._stop.is_set():
            self.poll_once(commands)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, commands: queue.Queue[Command]) -> threading.Thread:
        self._stop.clear()
        self.started, self.last_poll_end, self.wait_s = self.clock(), None, 0.0
        th = threading.Thread(target=self.run, args=(commands,), name="ewbot-telegram", daemon=True)
        th.start()
        self._thread = th
        return th

    def stop(self) -> None:
        self._stop.set()
```

**File: `src/ewbot/live/health.py`**

```python
"""Heartbeat to an external monitor (Plan 3 spec 3.2 `health`, 5.1 `VPS 다운`).

Every minute the runtime's main loop GETs EWBOT_HEALTHCHECK_URL (for example a healthchecks.io check
with period 1 min and grace 2 min, so 3 silent minutes alert through its integrations). The ping is sent
from the main loop only, so a hung loop stops the pings as surely as a dead VPS; it times out after 5 s.
A failed ping - an exception (its type name) or an HTTP status of 400 or more (the status, e.g. 404 for a
wrong check UUID) - never raises: it counts in `failures` (in a row, alerted by the runtime) and
`last_error`, and stderr gets one line when the pings start failing and one when they work again.
`fail(message)` POSTs `<url>/fail` (a query string kept) with a short message that names no secret: on a
fatal exit, and when Telegram has been unusable for 10 minutes (live/runtime.py)."""

from __future__ import annotations

import sys
import time
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import requests

INTERVAL_S = 60.0
TIMEOUT_S = 5.0


def fail_url(url: str) -> str:
    """https://hc-ping.com/<uuid>?create=1 -> https://hc-ping.com/<uuid>/fail?create=1."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=parts.path.rstrip("/") + "/fail"))


class Heartbeat:
    def __init__(self, url: str | None, *, session: Any | None = None, clock: Callable[[], float] = time.time,
                 interval: float = INTERVAL_S, timeout: float = TIMEOUT_S) -> None:
        self.url = (url or "").strip() or None
        self.session = session if session is not None else requests.Session()
        self.clock = clock
        self.interval = interval
        self.timeout = timeout
        self.last: float | None = None
        self.failures = 0
        self.last_error: str | None = None

    def beat(self) -> bool:
        if self.url is None:
            return False
        now = self.clock()
        if self.last is not None and now - self.last < self.interval:
            return False
        self.last = now
        error: str | None = None
        try:
            status = int(self.session.get(self.url, timeout=self.timeout).status_code)
            if status >= 400:
                error = f"HTTP {status}"
        except Exception as exc:  # noqa: BLE001 - monitoring must never stop trading (the URL is a secret)
            error = type(exc).__name__
        if error is None:
            if self.failures:
                print(f"[LIVE] heartbeat ok again after {self.failures} failed pings", file=sys.stderr)
            self.failures = 0
            return True
        if self.failures == 0:
            print(f"[LIVE] heartbeat failed: {error}", file=sys.stderr)
        self.failures += 1
        self.last_error = error
        return False

    def fail(self, message: str) -> bool:
        if self.url is None:
            return False
        try:
            r = self.session.post(fail_url(self.url), data=message[:500].encode("utf-8"), timeout=self.timeout)
            return int(r.status_code) < 400
        except Exception:  # noqa: BLE001 - best effort
            return False
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run --group live pytest tests/live/test_control.py -q
```

Expected: `13 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/live/notify.py src/ewbot/live/control.py src/ewbot/live/health.py tests/live/test_control.py
git commit -m "$(cat <<'EOF'
feat(live): [LIVE] Telegram notifier, allowlisted Telegram commands and heartbeat pings

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Paper audit, stages and gates

**Files:**
- Create: `src/ewbot/live/audit.py`, `src/ewbot/live/stage.py`
- Test: `tests/live/test_stage.py`

**Interfaces:**
- Consumes: `ewbot.paper.decisions` (`known_at`, `same_decision`), `ewbot.paper.ledger` (`Ledger.from_dict`, `LedgerError`: the schema-1 check), `ewbot.live.exchange` (`KeyPermissions`, `SESSION_OPEN`), `ewbot.live.journal.Journal`, `ewbot.live.pricing` (`FULL_CAP_CONTRACTS`, `FULL_CAP_DAYS`), `ewbot.types.H1`, `requests`.
- Produces (`ewbot.live.audit`): `CONTENTS_API`, `FETCH_TIMEOUT = (5.0, 20.0)`, `FETCH_DEADLINE_S = 30.0`, `class AuditError(RuntimeError)`, `fetch_paper_ledger(repo, token, session=None, ref="paper", *, clock=time.monotonic) -> dict` (raw media type, streamed within a total deadline; any failure is an `AuditError` naming only the HTTP status or the exception type, raised outside the handler so that no request exception - its headers carry the token - is chained to it; a session it creates it closes), `recording_floor(epochs) -> int` (as `paper.runner._recording_floor`), frozen `AuditResult(window: (lo, hi), compared, matched, mismatches: tuple[dict, ...], as_of, error=None)` with `.ok` (no error, something compared, nothing mismatched), `failed(error, as_of)`, `to_json()` / `from_json(data)` (the journal's format: each mismatch as id, type, cause), `audit(live_decisions, ledger, baseline_fingerprint=None, since=None, until=None, *, now, data_hash=None) -> AuditResult` (known_at in (max(recording floor, since), min(epoch through_ts, until)]; mismatch types `MISSING_IN_LIVE`, `MISSING_IN_PAPER`, `DIFFERENT`, `PAPER_FINGERPRINT`, each with a `cause`: `DATA_REVISION` when `data_hash(ts)` - the live candles' `data_fingerprint(df, 0, ts)` - differs from the paper's hash at the decision's bar (its decision's `data_sha256`, else its run at that bar) and equals it at the paper's previous run, `FINGERPRINT_CHANGE`, else `UNEXPLAINED`; a ledger that is not a valid schema-1 paper ledger raises `AuditError`).
- Produces (`ewbot.live.stage`): `STAGES = ("SHADOW", "CANARY", "FULL")`, `SHADOW_MIN_DAYS = 14`, `CYCLE_DEADLINE_S = 120`, `CANARY_MIN_TRADES = 3`, `CANARY_MAX_DAYS = 90`, `PROTECT_MAX_S = 30.0`, `AUDIT_MAX_AGE_S = 3 * 3600` (a gate's audit must reach a bar that closed at most this long ago), `PAUSE_KEY = "paused"`, `MAJOR`, `MANUAL` (`FOREIGN_ORDERS`, `UNKNOWN_POSITION`, `ADOPTED`, `EXCESS_POSITION`, `UNEXPLAINED_REDUCTION`, `DIRECTION_MISMATCH`: the position changed outside our orders - brackets and closes only reduce, so only someone else's order can flip it), `ORDER_UNRESOLVED = "ORDER_UNRESOLVED"` (an order failure the runtime did not resolve in time), `CANARY_FAIL` (`MAJOR` + `PROTECT_SLOW` + `MANUAL` + `ORDER_UNRESOLVED`: the one CANARY failure set of the pause and the gates), `ORDER_FAILURES` (the order failures the runtime watches: `BRACKET_FAILED`, `CLOSE_FAILED`, `CLOSE_UNCONFIRMED`, `REDUCE_FAILED` - not `LEG_GAP`, the executor's designed cancel-first fallback on a venue that refuses overlapping brackets); the gate names `G_*`, `SHADOW_PASS`, `CANARY_PASS`, `REQUIRED_GATES` (per promotion); `class StageError(RuntimeError)`, frozen `Gate(name, met, detail)`, `gate_lines(gates) -> list[str]`, `paused(journal) -> dict | None`, `pause(journal, reason, now)`, `resume(journal, now)`, `canary_failures(journal, since) -> {kind: (count, latest ts)}` (INCIDENT events of a `CANARY_FAIL` kind and events of those kinds themselves), `full_cap(journal, now) -> int | None` (the journal's current stage maximum, never more: SHADOW 0, CANARY 1, FULL `FULL_CAP_CONTRACTS` until it has run `FULL_CAP_DAYS` without a CANARY failure - each one restarts the window, on the journal's times -, then None), `on_incident(journal, kind, now) -> list[str]` (`"paused"`, `"demoted to CANARY"` with a `STAGE` event; in FULL another CANARY failure `"3-contract cap for 30 more days"`), `audit_gate(name, audit_result, since, now) -> Gate` (a passing audit from at or before `since` to a bar closed at most `AUDIT_MAX_AGE_S` ago; a failed one unmet), `shadow_gates(journal, now, audit_result)`, `canary_entry_gates(journal, now, audit_result, paper_status, perms)`, `canary_gates(journal, now, audit_result)`, `full_entry_gates(journal, now, audit_result, paper_status, perms)` (all `-> list[Gate]`), `transition(journal, to, now, gates, *, confirm, flat)` (raises `StageError`; a promotion needs every gate of `REQUIRED_GATES[(current, to)]` among `gates` and every gate met; writes a `STAGE` event). `paper_status` is the dict of `paper.criteria.golive_status`.

- [ ] **Step 1: Write the failing test**

**File: `tests/live/test_stage.py`**

```python
from __future__ import annotations

import copy
import json

import pytest
import requests

from ewbot.config import BotConfig
from ewbot.live.audit import AuditError, AuditResult, audit, fetch_paper_ledger
from ewbot.live.exchange import KeyPermissions
from ewbot.live.pricing import stage_contracts
from ewbot.live.stage import (
    CANARY_FAIL,
    ORDER_FAILURES,
    Gate,
    StageError,
    canary_entry_gates,
    canary_gates,
    full_cap,
    full_entry_gates,
    gate_lines,
    on_incident,
    pause,
    paused,
    resume,
    shadow_gates,
    transition,
)
from ewbot.types import H1
from tests.live.helpers import journal

DAY = 86_400
S0 = 1_791_000_000.0  # SHADOW start (on the hour)
NOW = 1_791_500_000.0
KEY = KeyPermissions(True, True, False)
PAPER_OK = {"c1": True, "c2": True, "c3": False, "count": {"total": 37, "required": 50},
            "c4": {"met": True, "days": 28.5, "coverage": 0.99, "unexplained": 0}}
PAPER_ALL = {**PAPER_OK, "c3": True, "count": {"total": 50, "required": 50}}
AUDIT_GATE = "① 판단이 모의매매와 100% 일치"
CYCLE_GATE = "② 모든 사이클이 마감 후 2분 안에 끝남"


def good(now: float) -> AuditResult:
    """A passing audit from before any stage start to the bar that closed an hour before `now`."""
    return AuditResult((0, int(now) // H1 * H1 - 2 * H1), 12, 12, (), now - 600)


def dec(kind: str, ts: int, key: int, price: float = 1.0, **kw) -> dict:
    return {"id": f"{kind}:{ts}:{key}", "kind": kind, "ts": ts, "price": price, "contracts": 1, "reason": kind,
            "trade_key": key, "levels": [], "setup": "W3", "direction": 1, **kw}


def ledger(decisions: list[dict], started: int = 1000, through: int = 100_000, fp: str = "fp",
           epochs: list[dict] | None = None, runs: list[dict] | None = None) -> dict:
    """A schema-1 paper ledger (paper.ledger.Ledger)."""
    return {"schema_version": 1, "baseline": {"fingerprint": "fp", "commit": "abc1234"}, "holdout": {},
            "epochs": epochs or [{"id": 1, "fingerprint": fp, "started_ts": started, "through_ts": through}],
            "decisions": decisions, "runs": runs or []}


def run(last_bar: int, sha: str) -> dict:
    return {"id": f"r{last_bar}", "status": "ok", "last_bar_ts": last_bar, "data_sha256": sha}


def test_audit_matches_identical_decisions_and_reports_each_difference():
    paper = [dec("SIGNAL", 3600, 3600), dec("ENTRY", 7200, 7200, 2.0), dec("TRAIL", 10800, 7200, 3.0),
             dec("ENTRY", 500, 500)]  # known before the epoch start: outside the window
    live = [dec("SIGNAL", 3600, 3600), dec("ENTRY", 7200, 7200, 2.0 + 1e-12), dec("TRAIL", 10800, 7200, 3.5),
            dec("STOP", 14400, 7200), dec("SIGNAL", 200_000, 200_000)]  # known after the paper's last bar
    res = audit(live, ledger(paper), baseline_fingerprint="fp", now=NOW)
    assert (res.window, res.compared, res.matched, res.as_of) == ((1000, 100_000), 4, 2, NOW)
    assert [(m["id"], m["type"], m["cause"]) for m in res.mismatches] == [
        ("STOP:14400:7200", "MISSING_IN_PAPER", "UNEXPLAINED"), ("TRAIL:10800:7200", "DIFFERENT", "UNEXPLAINED")]
    assert not res.ok and audit(paper, ledger(paper), now=NOW).ok
    other = audit(paper, ledger(paper, fp="new"), baseline_fingerprint="fp", now=NOW)
    assert (other.mismatches[0]["type"], other.mismatches[0]["cause"]) == ("PAPER_FINGERPRINT", "FINGERPRINT_CHANGE")
    assert audit([], ledger(paper), since=8000, now=NOW).compared == 1  # only TRAIL is after `since`
    assert audit([], ledger([]), now=NOW).ok is False  # nothing compared is not a pass


def test_the_window_ends_at_the_last_live_cycle_and_starts_at_the_epoch_floor():
    paper = [dec("ENTRY", 7200, 7200), dec("TRAIL", 10800, 7200, 3.0)]
    live = [dec("ENTRY", 7200, 7200)]  # the live cycle of bar 10800 was held (data late): TRAIL comes next cycle
    assert [m["type"] for m in audit(live, ledger(paper), now=NOW).mismatches] == ["MISSING_IN_LIVE"]
    res = audit(live, ledger(paper), until=7200, now=NOW)  # `until`: the last bar whose live cycle ended OK
    assert res.ok and res.window == (1000, 7200)
    # a later epoch records from the previous epoch's through_ts (paper.runner._recording_floor), and one back on
    # the baseline fingerprint is audited like epoch 1
    epochs = [{"id": 1, "fingerprint": "fp", "started_ts": 1000, "through_ts": 50_000},
              {"id": 2, "fingerprint": "other", "started_ts": 60_000, "through_ts": 70_000},
              {"id": 3, "fingerprint": "fp", "started_ts": 80_000, "through_ts": 90_000}]
    paper3 = [dec("TRAIL", 75_600, 3600, 3.0)]  # first known by the run that opened epoch 3
    res = audit([dec("TRAIL", 75_600, 3600, 4.0)], ledger(paper3, epochs=epochs), baseline_fingerprint="fp", now=NOW)
    assert res.window == (70_000, 90_000) and [m["type"] for m in res.mismatches] == ["DIFFERENT"]


def test_a_bar_revised_between_the_two_fetches_is_a_data_revision():
    t = 36_000
    runs = [run(t - 3600, "h-before"), run(t, "h-paper")]
    paper = [dec("TRAIL", t, 7200, 3.0, data_sha256="h-paper")]
    live = [dec("TRAIL", t, 7200, 3.5)]
    revised = {t - 3600: "h-before", t: "h-live"}  # the live candles: equal up to the paper's previous run, not at t
    res = audit(live, ledger(paper, runs=runs), now=NOW, data_hash=revised.get)
    assert [(m["type"], m["cause"]) for m in res.mismatches] == [("DIFFERENT", "DATA_REVISION")] and not res.ok
    alone = audit(live, ledger([], runs=runs), now=NOW, data_hash=revised.get)  # the paper's run at t has its hash
    assert [(m["type"], m["cause"]) for m in alone.mismatches] == [("MISSING_IN_PAPER", "DATA_REVISION")]
    same = {t - 3600: "h-before", t: "h-paper"}  # the same candles
    parted_before = {t - 3600: "x", t: "h-live"}  # different before the bar already: not located at this bar
    for hashes in (same, parted_before):
        assert audit(live, ledger(paper, runs=runs), now=NOW, data_hash=hashes.get).mismatches[0]["cause"] == "UNEXPLAINED"
    assert audit(live, ledger(paper, runs=runs), now=NOW).mismatches[0]["cause"] == "UNEXPLAINED"  # no live candles


def test_a_ledger_that_is_not_a_valid_paper_ledger_is_an_audit_error():
    good_ledger = ledger([dec("TRAIL", 10800, 7200, 3.0)])

    def broken(change) -> dict:
        led = copy.deepcopy(good_ledger)
        change(led)
        return led

    cases = {
        "not an object": [good_ledger],
        "metadata JSON": {"name": "ledger.json", "sha": "abc", "size": 10, "content": "e30="},
        "schema 2": broken(lambda d: d.update(schema_version=2)),
        "no epochs": broken(lambda d: d.pop("epochs")),
        "empty epochs": broken(lambda d: d.update(epochs=[])),
        "through None": broken(lambda d: d["epochs"][0].update(through_ts=None)),
        "decisions a dict": broken(lambda d: d.update(decisions={"a": 1})),
        "decision without levels": broken(lambda d: d["decisions"][0].pop("levels")),
    }
    live = [dec("TRAIL", 10800, 7200, 3.0)]
    for name, bad in cases.items():
        with pytest.raises(AuditError):
            audit(live, bad, now=NOW)
            pytest.fail(name)
    assert audit(live, good_ledger, now=NOW).ok


class Resp:
    def __init__(self, status: int, body: bytes = b"", chunks: list[bytes] | None = None) -> None:
        self.status_code, self.chunks, self.closed = status, chunks if chunks is not None else [body], False

    def iter_content(self, size):
        yield from self.chunks

    def close(self):
        self.closed = True


class Sess:
    def __init__(self, answer):
        self.answer, self.calls, self.closed = answer, [], False

    def get(self, url, params=None, headers=None, timeout=None, stream=False):
        self.calls.append((url, params, headers, timeout, stream))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def close(self):
        self.closed = True


def test_fetch_paper_ledger_uses_the_contents_api_bounds_its_time_and_hides_the_token(monkeypatch):
    body = json.dumps(ledger([])).encode()
    s = Sess(Resp(200, body))
    assert fetch_paper_ledger("me/repo", "ghp_SECRET", s)["epochs"][0]["id"] == 1
    url, params, headers, timeout, stream = s.calls[0]
    assert url == "https://api.github.com/repos/me/repo/contents/ledger.json" and params == {"ref": "paper"}
    assert headers["Accept"] == "application/vnd.github.raw+json" and headers["Authorization"] == "Bearer ghp_SECRET"
    assert timeout == (5.0, 20.0) and stream and s.answer.closed and not s.closed  # the caller's session stays open
    for answer in (Resp(404, b'{"message": "Not Found"}'), Resp(200, b"[1, 2]"), Resp(200, b"<html>"),
                   requests.exceptions.ConnectionError("ghp_SECRET")):
        with pytest.raises(AuditError) as err:
            fetch_paper_ledger("me/repo", "ghp_SECRET", Sess(answer))
        assert "ghp_SECRET" not in str(err.value) and err.value.__context__ is None and err.value.__cause__ is None
    made: list[Sess] = []
    monkeypatch.setattr(requests, "Session", lambda: made.append(Sess(Resp(200, body))) or made[-1])
    fetch_paper_ledger("me/repo", "t")
    assert made[0].closed  # a session made here is closed here
    ticks = iter(range(0, 1000, 10))
    trickle = Resp(200, chunks=[b" "] * 10 + [body])  # 10 s per chunk: past the 30 s deadline
    with pytest.raises(AuditError, match="TimeoutError"):
        fetch_paper_ledger("me/repo", "t", Sess(trickle), clock=lambda: float(next(ticks)))
    assert trickle.closed


def hourly(jr, since: float, now: float, skip: tuple[int, ...] = ()) -> int:
    """An OK cycle 75 s after the close for every bar that closed in the stage and is past its deadline."""
    first, n = int(since) // H1 * H1 - H1, 0
    for bar in range(first, int(now) - H1 - 120 + 1, H1):
        if bar + H1 + 120 > now or bar in skip:
            continue
        jr.start_cycle(bar, bar + H1 + 30, "SHADOW")
        jr.finish_cycle(bar, bar + H1 + 75, "OK")
        n += 1
    return n


def shadow_journal(tmp_path, days: float = 15.0):
    jr = journal(tmp_path)
    jr.set_stage("SHADOW", "initial", S0)
    now = S0 + days * DAY
    jr.add_event(S0 + 10, "RECOVERY", {"accepted": 0, "abandoned": 0, "unknown": 0, "rebuilt": 0})
    jr.add_event(S0 + 5 * DAY, "RECOVERY", {"accepted": 1, "abandoned": 0, "unknown": 0, "rebuilt": 0})
    hourly(jr, S0, now)
    jr.add_decisions([dec("SIGNAL", int(S0) + 7200, int(S0) + 7200)], now=S0 + 10_900)
    jr.add_event(S0 + 10_900, "PREVIEW", {"ok": True, "errors": []})
    return jr, now


def test_shadow_gates_all_met(tmp_path):
    jr, now = shadow_journal(tmp_path)
    gates = shadow_gates(jr, now, good(now))
    assert all(g.met for g in gates), gate_lines(gates)
    assert {g.name: g.detail for g in gates}[CYCLE_GATE] == "360/360봉"  # every hour of the 15 days
    jr.close()


def test_each_shadow_gate_can_fail(tmp_path):
    jr, now = shadow_journal(tmp_path, days=13.0)
    bar = int(S0) + 10 * H1
    jr.start_cycle(bar, bar + H1 + 30, "SHADOW")
    jr.finish_cycle(bar, bar + H1 + 121, "OK")  # 121 s after the close
    jr.add_event(S0 + 9000, "PREVIEW", {"ok": False, "errors": ["PREVIEW_INSUFFICIENT_FUND"]})
    jr.add_event(S0 + 9 * DAY, "RECOVERY", {"accepted": 0, "abandoned": 0, "unknown": 1, "rebuilt": 0})
    bad = AuditResult((0, int(now) - 2 * H1), 3, 2, ({"id": "x", "type": "DIFFERENT", "cause": "UNEXPLAINED"},), now)
    met = {g.name: g.met for g in shadow_gates(jr, now, bad)}
    assert met == {"기간 14일 이상": False, "신호 1건 이상": True, AUDIT_GATE: False, CYCLE_GATE: False,
                   "③ 냈을 주문 전부 preview 통과": False, "④ 장애·재시작 시험 통과": False}
    jr.close()


def test_the_signal_and_cycle_gates_count_the_stage_itself(tmp_path):
    jr = journal(tmp_path)
    jr.set_stage("SHADOW", "initial", S0)
    now = S0 + 15 * DAY

    def gate(name: str, at: float = now) -> Gate:
        return {g.name: g for g in shadow_gates(jr, at, good(at))}[name]

    # the first cycle records every decision since 2024: none of those signals was first known in the stage
    jr.add_decisions([dec("SIGNAL", 1_730_000_000 + k * H1, 1_730_000_000 + k * H1) for k in range(3)], now=S0 + 40)
    assert not gate("신호 1건 이상").met
    jr.add_decisions([dec("SIGNAL", int(S0) + 7200, int(S0) + 7200)], now=S0 + 10_900)
    assert (gate("신호 1건 이상").met, gate("신호 1건 이상").detail) == (True, "단계 중 알게 된 신호 1건")
    # a runtime that was down: every hour without a cycle is a miss
    for k in (0, 300):
        bar = int(S0) + k * H1
        jr.start_cycle(bar, bar + H1 + 30, "SHADOW")
        jr.finish_cycle(bar, bar + H1 + 60, "OK")
    assert (gate(CYCLE_GATE).met, gate(CYCLE_GATE).detail) == (False, "2/360봉")
    # a bar while the futures session was recorded closed is excused; any other missing bar is not
    closed_bar, missing_bar = int(S0) + 100 * H1, int(S0) + 200 * H1
    jr.add_event(closed_bar + H1 - 60, "SESSION", {"text": "장 운영 상태 OPEN → CLOSE", "state": "CLOSE"})
    jr.add_event(closed_bar + 2 * H1, "SESSION", {"text": "장 운영 상태 CLOSE → OPEN", "state": "OPEN"})
    hourly(jr, S0, now, skip=(closed_bar, missing_bar))
    assert (gate(CYCLE_GATE).met, gate(CYCLE_GATE).detail) == (False, "358/359봉 (장 휴장 제외 1봉)")
    jr.start_cycle(missing_bar, missing_bar + H1 + 30, "SHADOW")
    jr.finish_cycle(missing_bar, missing_bar + H1 + 90, "OK")
    assert gate(CYCLE_GATE).met
    # a cycle still running is due only once its deadline has passed
    last = int(now) - H1  # closes at `now`
    jr.start_cycle(last, now + 30, "SHADOW")
    assert gate(CYCLE_GATE, now + 60).met and not gate(CYCLE_GATE, now + 200).met
    jr.close()


def test_the_audit_gate_needs_a_passing_audit_that_covers_the_stage_up_to_now(tmp_path):
    jr, now = shadow_journal(tmp_path)

    def audit_gate(res: AuditResult | None) -> tuple[bool, str]:
        g = {g.name: g for g in shadow_gates(jr, now, res)}[AUDIT_GATE]
        return g.met, g.detail

    assert audit_gate(good(now))[0] and audit_gate(AuditResult.from_json(json.loads(json.dumps(good(now).to_json()))))[0]
    assert audit_gate(None) == (False, "미실행")
    stale = AuditResult((0, 1_700_000_000), 1, 1, (), 1_700_010_000.0)  # an audit whose window ended in 2023
    assert not audit_gate(stale)[0] and audit_gate(stale)[1].startswith("오래된 감사")
    late = AuditResult((int(S0) + DAY, int(now) - 2 * H1), 5, 5, (), now)  # the paper epoch began during SHADOW
    assert not audit_gate(late)[0] and audit_gate(late)[1].startswith("감사 구간이 단계 시작 뒤부터")
    failed = AuditResult.failed("paper ledger malformed: KeyError: 'levels'", now)  # never the last good one
    assert audit_gate(failed) == (False, "감사 실패: paper ledger malformed: KeyError: 'levels'")
    assert not AuditResult.from_json(failed.to_json()).ok
    jr.close()


def test_canary_needs_shadow_paper_and_a_trade_key(tmp_path):
    jr, now = shadow_journal(tmp_path)
    assert all(g.met for g in canary_entry_gates(jr, now, good(now), PAPER_OK, KEY))
    no_c4 = {**PAPER_OK, "c4": {**PAPER_OK["c4"], "met": False}}
    assert not all(g.met for g in canary_entry_gates(jr, now, good(now), no_c4, KEY))
    assert not all(g.met for g in canary_entry_gates(jr, now, good(now), PAPER_OK, KeyPermissions(True, True, True)))
    assert not all(g.met for g in canary_entry_gates(jr, now, good(now), None, KEY))
    jr.close()


def test_transitions_need_confirm_flat_book_and_met_gates(tmp_path):
    jr, now = shadow_journal(tmp_path)
    gates = canary_entry_gates(jr, now, good(now), PAPER_OK, KEY)
    with pytest.raises(StageError, match="--confirm"):
        transition(jr, "CANARY", now, gates, confirm=False, flat=True)
    with pytest.raises(StageError, match="flat"):
        transition(jr, "CANARY", now, gates, confirm=True, flat=False)
    with pytest.raises(StageError, match="one stage at a time"):
        transition(jr, "FULL", now, gates, confirm=True, flat=True)
    with pytest.raises(StageError, match="gates not met"):
        transition(jr, "CANARY", now, canary_entry_gates(jr, now, None, PAPER_OK, KEY), confirm=True, flat=True)
    for wrong in ([], [Gate("anything", True, "")], shadow_gates(jr, now, good(now))):  # not this promotion's gates
        with pytest.raises(StageError, match="확인 안 됨"):
            transition(jr, "CANARY", now, wrong, confirm=True, flat=True)
    transition(jr, "CANARY", now, gates, confirm=True, flat=True)
    assert jr.stage() == ("CANARY", now)
    jr.set_stage("FULL", "test", now + 1)
    with pytest.raises(StageError, match="flat"):  # a demotion needs a flat book too
        transition(jr, "CANARY", now + 2, [], confirm=True, flat=False)
    transition(jr, "CANARY", now + 2, [], confirm=True, flat=True)  # a demotion needs no gates
    transition(jr, "SHADOW", now + 3, [], confirm=True, flat=True)
    assert [(e["data"]["from"], e["data"]["to"]) for e in jr.events(kind="STAGE")] == [
        ("SHADOW", "CANARY"), ("FULL", "CANARY"), ("CANARY", "SHADOW")]
    with pytest.raises(StageError):
        transition(jr, "SHADOW", now + 4, [], confirm=True, flat=True)
    jr.close()


def canary_journal(tmp_path, closed: int = 3, name: str = "journal.sqlite"):
    jr = journal(tmp_path, name)
    jr.set_stage("CANARY", "promotion", S0)
    for k in range(closed):
        jr.add_event(S0 + k * DAY, "PROTECTED_FIRST", {"trade_id": k + 1, "after_entry_s": 2.5})
        jr.add_event(S0 + k * DAY + 3600, "CLOSED", {"trade_id": k + 1, "reason": "BRACKET"})
    jr.add_event(S0 + 3 * DAY, "COSTS", {"fills": 6})
    return jr


def test_canary_pass_and_full_entry(tmp_path):
    jr = canary_journal(tmp_path)
    now = S0 + 20 * DAY
    assert all(g.met for g in canary_gates(jr, now, good(now))), gate_lines(canary_gates(jr, now, good(now)))
    assert not all(g.met for g in full_entry_gates(jr, now, good(now), PAPER_OK, KEY))  # 37/50
    assert all(g.met for g in full_entry_gates(jr, now, good(now), PAPER_ALL, KEY))
    jr.add_event(now, "UNPROTECTED_60S", {"trade_id": 9})
    jr.add_event(now, "ADOPTED", {"trade_id": 9})
    jr.add_event(now, "ORDER_UNRESOLVED", {"trade_id": 9, "kind": "CLOSE_FAILED"})
    met = {g.name: g.met for g in canary_gates(jr, now, good(now))}
    assert not met["② 손절 이동과 청산이 목표와 일치"] and not met["③ 보호되지 않은 상태 60초 초과 없음"]
    assert not met["④ 수동 개입 없음"]
    jr.close()


def test_every_canary_failure_pauses_canary_and_keeps_full_closed(tmp_path):
    now = S0 + 20 * DAY
    for kind in CANARY_FAIL:
        jr = canary_journal(tmp_path, name=f"{kind}.sqlite")
        assert all(g.met for g in full_entry_gates(jr, now, good(now), PAPER_ALL, KEY))
        assert on_incident(jr, kind, now) == ["paused"] and paused(jr)["reason"] == f"incident {kind}"
        resume(jr, now + 1)  # the user's /resume does not undo a CANARY failure of this stint
        gates = full_entry_gates(jr, now + 2, good(now), PAPER_ALL, KEY)
        assert not all(g.met for g in gates), kind
        with pytest.raises(StageError, match="gates not met"):
            transition(jr, "FULL", now + 3, gates, confirm=True, flat=True)
        jr.close()
    # an order failure on its own is none: the runtime resolves it or records ORDER_UNRESOLVED
    jr = canary_journal(tmp_path, name="orders.sqlite")
    for k in ORDER_FAILURES:
        jr.add_event(now, k, {"trade_id": 9})
    assert paused(jr) is None and all(g.met for g in canary_gates(jr, now + 400 * DAY, good(now + 400 * DAY)))
    pause(jr, "user /pause", now)  # paused for any reason, or a missing or failed audit: not passed
    assert [g.name for g in canary_gates(jr, now, good(now)) if not g.met] == ["일시 정지 아님"]
    resume(jr, now)
    assert [g.name for g in canary_gates(jr, now, None) if not g.met] == ["모의매매 대조 (최근 감사 통과)"]
    failed = AuditResult.failed("paper ledger fetch failed: HTTP 502", now)
    assert [g.name for g in canary_gates(jr, now, failed) if not g.met] == ["모의매매 대조 (최근 감사 통과)"]
    jr.close()


def test_canary_needs_three_trades_or_ninety_days(tmp_path):
    jr = canary_journal(tmp_path, closed=1)
    early, late = S0 + 30 * DAY, S0 + 91 * DAY
    assert not canary_gates(jr, early, good(early))[0].met and canary_gates(jr, late, good(late))[0].met
    jr.close()


def test_the_cap_is_the_journal_stages_and_lifts_only_after_30_clean_days_of_full(tmp_path):
    jr = journal(tmp_path)
    assert full_cap(jr, S0) == 0  # no stage yet: SHADOW, no real order
    jr.set_stage("CANARY", "promotion", S0)
    assert full_cap(jr, S0 + 1) == 1
    jr.set_stage("FULL", "promotion", S0 + 5)
    assert full_cap(jr, S0 + 6) == 3 and full_cap(jr, S0 + 5 + 30 * DAY) is None
    assert on_incident(jr, "FOREIGN_ORDERS", S0 + 20 * DAY) == ["3-contract cap for 30 more days"]  # not major
    assert full_cap(jr, S0 + 5 + 30 * DAY) == 3 and full_cap(jr, S0 + 50 * DAY) is None
    jr.add_event(S0 + 55 * DAY, "ORDER_UNRESOLVED", {"trade_id": 7, "kind": "CLOSE_FAILED"})  # even without its incident
    assert full_cap(jr, S0 + 84 * DAY) == 3 and full_cap(jr, S0 + 85 * DAY) is None
    kw = dict(equity=200_000.0, entry=60_000.0, stop=59_400.0, direction=1, risk_mult=1.0, cfg=BotConfig())
    assert stage_contracts("FULL", full_cap=full_cap(jr, S0 + 86 * DAY), **kw)[0] == 303  # released: uncapped
    # a major incident demotes in the journal while the FULL process still runs: the cap is CANARY's at once
    assert on_incident(jr, "UNPROTECTED_60S", S0 + 86 * DAY) == ["paused", "demoted to CANARY"]
    resume(jr, S0 + 86 * DAY + 1)
    assert full_cap(jr, S0 + 86 * DAY + 2) == 1
    assert stage_contracts("FULL", full_cap=full_cap(jr, S0 + 86 * DAY + 2), **kw)[0] == 1
    assert jr.events(kind="STAGE")[-1]["data"] == {"from": "FULL", "to": "CANARY",
                                                   "reason": "major incident UNPROTECTED_60S"}
    jr.close()


def test_pause_resume_and_incidents_by_stage(tmp_path):
    jr = journal(tmp_path)
    jr.set_stage("SHADOW", "initial", S0)
    assert on_incident(jr, "UNPROTECTED_60S", S0 + 1) == [] and paused(jr) is None  # SHADOW: recorded only
    jr.set_stage("CANARY", "promotion", S0 + 2)
    assert on_incident(jr, "FOREIGN_ORDERS", S0 + 3) == ["paused"] and paused(jr)["reason"] == "incident FOREIGN_ORDERS"
    resume(jr, S0 + 4)
    assert paused(jr) is None
    jr.set_stage("FULL", "promotion", S0 + 5)
    assert on_incident(jr, "BAD_EQUITY", S0 + 6) == []  # no CANARY failure (the runtime pauses entries itself)
    assert on_incident(jr, "BREAKER", S0 + 8) == ["paused", "demoted to CANARY"] and jr.stage()[0] == "CANARY"
    pause(jr, "user", S0 + 9)
    assert [e["data"]["kind"] for e in jr.events(kind="INCIDENT")] == [
        "UNPROTECTED_60S", "FOREIGN_ORDERS", "BAD_EQUITY", "BREAKER"]
    jr.close()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --group live pytest tests/live/test_stage.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'ewbot.live.audit'`.

- [ ] **Step 3: Write minimal implementation**

**File: `src/ewbot/live/audit.py`**

```python
"""Live vs. paper decision audit (Plan 3 spec 3.5; SHADOW pass criterion ① of spec 6.2).

The live runtime records the engine's decisions (live/target.py) in the paper ledger's format; the
paper runtime (GitHub Actions, the independent audit) records the same decisions on its `paper` branch.
Both replay the same frozen engine on the same Coinbase spot candles, so every decision both runtimes
can know must be identical: same id (kind:ts:trade_key) and `paper.decisions.same_decision`.

The window is the paper ledger's current epoch, from its recording floor - the epoch's start for epoch 1,
the previous epoch's through_ts for a later one (as `paper.runner._recording_floor`) - to the last bar both
runtimes have decided: known_at in (floor, min(epoch through_ts, until)], `until` being the last bar whose
live cycle ended OK (a cycle held for late data or failed decides that bar at the next cycle, while the
paper runtime may know it already). The live runtime records every decision since 2024 on its first cycle,
so it has no gap. An epoch whose fingerprint is not the baseline is a mismatch of its own
(PAPER_FINGERPRINT): the two engines differ.

Every mismatch has a cause, like the paper runtime's replay check (`paper.decisions.classify`):
DATA_REVISION when the candles the two runtimes decided on differ up to the decision's bar although they
were equal up to the paper's previous run (a bar Coinbase changed between the live fetch at hh:00:30 and the
paper's at :37); FINGERPRINT_CHANGE for PAPER_FINGERPRINT; else UNEXPLAINED - also whenever the data cannot
be compared (no live candles, no paper hash), and once the two candle histories differ before the decision
(a revision is only recognised at the bar where they part). `data_hash(ts)` is the live candles'
`data_fingerprint(df, 0, ts)`; the paper ledger holds the same hash per decision and per run.

A ledger that is not a schema-1 paper ledger (`paper.ledger.Ledger.from_dict`) or whose epochs or decisions
are malformed is an AuditError, like a failed fetch: never a passing audit.

The ledger is read with the GitHub contents API (`EWBOT_PAPER_REPO`, fine-grained token with
Contents: read on that repository only, `EWBOT_GITHUB_TOKEN`), raw media type (up to 100 MB)."""

from __future__ import annotations

import bisect
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import requests

from ewbot.paper.decisions import known_at, same_decision
from ewbot.paper.ledger import Ledger, LedgerError

CONTENTS_API = "https://api.github.com/repos/{repo}/contents/ledger.json"
FETCH_TIMEOUT = (5.0, 20.0)  # connect, each read
FETCH_DEADLINE_S = 30.0  # the whole fetch, body included: it runs on the protective main loop
CHUNK = 1 << 16


class AuditError(RuntimeError):
    """The paper ledger could not be read or is not a valid paper ledger (the message never contains the token)."""


def fetch_paper_ledger(repo: str, token: str, session: Any | None = None, ref: str = "paper", *,
                       clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """The ledger as a JSON object. Each request step is bounded by FETCH_TIMEOUT and the whole fetch by
    FETCH_DEADLINE_S. Any failure is an AuditError naming only the HTTP status or the exception type; it is
    raised outside the handler, so no exception carrying the request (and its token) is chained to it. A
    session created here is closed here."""
    own = session is None
    sess: Any = requests.Session() if own else session
    headers = {"Accept": "application/vnd.github.raw+json", "Authorization": f"Bearer {token}",
               "X-GitHub-Api-Version": "2022-11-28"}
    start = clock()
    failure: str | None = None
    status = 0
    data: Any = None
    try:
        resp = sess.get(CONTENTS_API.format(repo=repo), params={"ref": ref}, headers=headers, timeout=FETCH_TIMEOUT,
                        stream=True)
        try:
            status = int(resp.status_code)
            if status == 200:
                body = bytearray()
                for chunk in resp.iter_content(CHUNK):
                    body += chunk
                    if clock() - start > FETCH_DEADLINE_S:
                        raise TimeoutError
                data = json.loads(bytes(body))
        finally:
            resp.close()
    except Exception as exc:  # noqa: BLE001 - never echo request details (they carry the token)
        failure = type(exc).__name__
    finally:
        if own:
            sess.close()
    if failure is not None:
        raise AuditError(f"paper ledger fetch failed: {failure}")
    if status != 200:
        raise AuditError(f"paper ledger fetch failed: HTTP {status}")
    if not isinstance(data, dict):
        raise AuditError(f"paper ledger fetch failed: HTTP 200 but not a JSON object ({type(data).__name__})")
    return data


@dataclass(frozen=True)
class AuditResult:
    window: tuple[int, int]  # (exclusive, inclusive) known_at bounds
    compared: int
    matched: int
    mismatches: tuple[dict[str, Any], ...]  # {"id", "type", "cause", "paper", "live"}
    as_of: float  # when the audit ran
    error: str | None = None  # the audit failed (fetch, invalid ledger): never a pass

    @property
    def ok(self) -> bool:
        return self.error is None and self.compared > 0 and not self.mismatches

    @classmethod
    def failed(cls, error: str, as_of: float) -> AuditResult:
        return cls((0, 0), 0, 0, (), float(as_of), error)

    def to_json(self) -> dict[str, Any]:
        """For the journal: every mismatch as id, type and cause (the decisions go to the alert's event)."""
        return {"as_of": self.as_of, "window": list(self.window), "compared": self.compared, "matched": self.matched,
                "mismatches": [{"id": m.get("id"), "type": m.get("type"), "cause": m.get("cause")}
                               for m in self.mismatches], "error": self.error}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AuditResult:
        return cls((int(data["window"][0]), int(data["window"][1])), int(data["compared"]), int(data["matched"]),
                   tuple(data["mismatches"]), float(data["as_of"]), data.get("error"))


def recording_floor(epochs: Sequence[dict[str, Any]]) -> int:
    """known_at bound (exclusive) of the current epoch's decisions (`paper.runner._recording_floor`)."""
    if len(epochs) == 1:
        return int(epochs[0]["started_ts"])
    return int(epochs[-2]["through_ts"])


def audit(live: Sequence[dict[str, Any]], ledger: dict[str, Any], baseline_fingerprint: str | None = None,
          since: int | None = None, until: int | None = None, *, now: float,
          data_hash: Callable[[int], str | None] | None = None) -> AuditResult:
    """Compare the live decisions with the paper ledger's current epoch (module docstring). Raises AuditError
    for a ledger that is not a valid paper ledger."""
    try:
        led = Ledger.from_dict(ledger)
    except LedgerError as exc:
        raise AuditError(f"paper ledger invalid: {exc}"[:300]) from None
    try:
        return _audit(live, led, baseline_fingerprint, since, until, now, data_hash)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise AuditError(f"paper ledger malformed: {type(exc).__name__}: {exc}"[:300]) from None


def _audit(live: Sequence[dict[str, Any]], led: Ledger, baseline: str | None, since: int | None, until: int | None,
           now: float, data_hash: Callable[[int], str | None] | None) -> AuditResult:
    if not isinstance(led.epochs, list) or not led.epochs or not isinstance(led.decisions, list):
        raise AuditError("paper ledger malformed: no epoch, or decisions not a list")
    epoch = led.epochs[-1]
    floor = recording_floor(led.epochs)
    lo = floor if since is None else max(floor, int(since))
    hi = int(epoch["through_ts"]) if until is None else min(int(epoch["through_ts"]), int(until))

    def inside(d: dict[str, Any]) -> bool:
        return lo < known_at(d) <= hi

    paper = {d["id"]: d for d in led.decisions if inside(d)}
    mine = {d["id"]: d for d in live if inside(d)}
    runs = sorted((int(r["last_bar_ts"]), str(r["data_sha256"])) for r in led.runs
                  if isinstance(r, dict) and r.get("status") == "ok" and r.get("data_sha256"))
    run_bars = [b for b, _ in runs]

    def revised(ts: int, recorded: dict[str, Any] | None) -> bool:
        """The live candles up to `ts` differ from the paper's (its decision's hash, else its run at that bar)
        while both were equal at the paper's previous run."""
        if data_hash is None:
            return False
        k = bisect.bisect_left(run_bars, ts)
        theirs = recorded.get("data_sha256") if recorded is not None else None
        if theirs is None and k < len(runs) and runs[k][0] == ts:
            theirs = runs[k][1]
        if theirs is None or k == 0:
            return False
        anchor_bar, anchor_hash = runs[k - 1]
        mine_now = data_hash(ts)
        return mine_now is not None and mine_now != theirs and data_hash(anchor_bar) == anchor_hash

    mismatches: list[dict[str, Any]] = []
    if baseline is not None and epoch.get("fingerprint") != baseline:
        mismatches.append({"id": f"EPOCH:{epoch['id']}", "type": "PAPER_FINGERPRINT", "cause": "FINGERPRINT_CHANGE",
                           "paper": epoch.get("fingerprint"), "live": baseline})
    matched = 0
    for did in sorted(paper.keys() | mine.keys()):
        a, b = mine.get(did), paper.get(did)
        if a is not None and b is not None and same_decision(a, b):
            matched += 1
            continue
        kind = "MISSING_IN_LIVE" if a is None else "MISSING_IN_PAPER" if b is None else "DIFFERENT"
        ref = b if b is not None else a
        assert ref is not None
        cause = "DATA_REVISION" if revised(int(ref["ts"]), b) else "UNEXPLAINED"
        mismatches.append({"id": did, "type": kind, "cause": cause, "paper": b, "live": a})
    return AuditResult((lo, hi), len(paper.keys() | mine.keys()), matched, tuple(mismatches), float(now))
```

**File: `src/ewbot/live/stage.py`**

```python
"""Operating stages SHADOW -> CANARY -> FULL, their gates, pause and incidents (Plan 3 spec 4.3, 6).

The journal holds the stage history and the pause flag. Promotions need every gate met, a flat book and an
explicit --confirm (the user's approval, spec 6.1); demotions need a flat book and --confirm. A running runtime
keeps running until it is flat and then exits so Docker restarts it in the new stage (live/runtime.py), but it
sizes every entry and adoption by the more conservative of its own stage and the journal's (`full_cap` reads the
journal's current stage), so a demotion applies to the next order at once.

One CANARY failure definition for the pause and the gates (spec 6.2 CANARY `실패 시 자동으로 일시 정지`):
`CANARY_FAIL` = the major incidents (`MAJOR`: unprotected > 60 s, an unexplained audit mismatch, a breaker, a
transfer-permission key), protection slower than 30 s, the position changed outside our orders (`MANUAL`) and
an order failure the executor did not resolve (`ORDER_UNRESOLVED`, recorded by live/runtime.py: a failed
bracket not replaced within PROTECT_MAX_S, a failed close or reduction not done by the next pass; a failure it
resolved in time is no CANARY failure). In CANARY each one pauses entries (`on_incident`), and the CANARY pass and
FULL entry gates are unmet after any of them in the current CANARY stint, while the runtime is paused and
without a current passing audit. In FULL the major incidents pause and demote to CANARY, and every CANARY
failure restarts the 30-day clean window of the 3-contract cap (spec 6.2 FULL `이상이 없으면 상한을 해제`).

Gates (spec 6.2), all computed from the journal. Times are the journal's (the VPS clock, NTP-synchronised): a
clock step moves every window with it.
  SHADOW pass   >= 14 days and >= 1 signal first known in the stage; (1) decisions equal the paper ledger's (an
                audit from at or before the stage's start to at most AUDIT_MAX_AGE_S ago, with no mismatch);
                (2) every hourly bar that closed in the stage has a cycle that ended OK within 2 min of the
                close (a bar while the futures session was recorded closed is excused when it has none; a
                cycle not yet past its deadline is not due); (3) every would-be order passed preview; (4)
                restart test: at least one restart recovered with no unresolved intent.
  CANARY start  SHADOW pass + paper criteria 1, 2 and 4 met + a trade key without transfer permission.
  CANARY pass   >= 3 closed trades or 90 days; (1) protection within 30 s of every fill; (2) stop moves and
                closes executed as targeted (no unresolved order failure); (3) never unprotected > 60 s; (4) no
                manual intervention; (5) costs measured (a daily summary with fills); no other CANARY failure
                (audit mismatch, breaker, transfer key); not paused; a current passing audit.
  FULL start    CANARY pass + paper criteria 1-4 all met (incl. 50 out-of-sample trades)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Sequence

from ewbot.live.audit import AuditResult
from ewbot.live.exchange import SESSION_OPEN, KeyPermissions
from ewbot.live.journal import Journal
from ewbot.live.pricing import FULL_CAP_CONTRACTS, FULL_CAP_DAYS
from ewbot.paper.decisions import known_at
from ewbot.types import H1

STAGES = ("SHADOW", "CANARY", "FULL")
DAY = 86_400
SHADOW_MIN_DAYS = 14
CYCLE_DEADLINE_S = 120
CANARY_MIN_TRADES = 3
CANARY_MAX_DAYS = 90
PROTECT_MAX_S = 30.0
AUDIT_MAX_AGE_S = 3 * 3600  # a gate's audit must reach a bar that closed at most this long ago
PAUSE_KEY = "paused"
MAJOR = ("UNPROTECTED_60S", "AUDIT_MISMATCH", "BREAKER", "TRANSFER_KEY")  # spec 6.2 중대 사건
# the position changed outside our orders: a foreign order or position, more contracts than the trade holds,
# fewer than its legs explain (reconcile.observe), the trade's position on the other side (brackets and closes only
# reduce: only an order of someone else's can flip it)
MANUAL = ("FOREIGN_ORDERS", "UNKNOWN_POSITION", "ADOPTED", "EXCESS_POSITION", "UNEXPLAINED_REDUCTION", "DIRECTION_MISMATCH")
# an order failure the executor did not resolve in time (live/runtime.py watches ORDER_FAILURES)
ORDER_UNRESOLVED = "ORDER_UNRESOLVED"
CANARY_FAIL = MAJOR + ("PROTECT_SLOW",) + MANUAL + (ORDER_UNRESOLVED,)
# the order failures the runtime watches for CANARY gate 2: an order that did not do what was targeted. Not LEG_GAP:
# on a venue that refuses overlapping brackets the executor cancels the old leg first by design (a replacement that
# then fails is BRACKET_FAILED). Each one is resolved or becomes ORDER_UNRESOLVED.
ORDER_FAILURES = ("BRACKET_FAILED", "CLOSE_FAILED", "CLOSE_UNCONFIRMED", "REDUCE_FAILED")

# gate names: transition() checks that a promotion got every one of its gates, met
G_DAYS = "기간 14일 이상"
G_SIGNAL = "신호 1건 이상"
G_AUDIT = "① 판단이 모의매매와 100% 일치"
G_CYCLES = "② 모든 사이클이 마감 후 2분 안에 끝남"
G_PREVIEW = "③ 냈을 주문 전부 preview 통과"
G_RESTART = "④ 장애·재시작 시험 통과"
G_TRADES = "완결 거래 3건 이상 또는 90일"
G_PROTECT = "① 보호 주문이 체결 후 30초 안에 걸림"
G_ORDERS = "② 손절 이동과 청산이 목표와 일치"
G_UNPROTECTED = "③ 보호되지 않은 상태 60초 초과 없음"
G_MANUAL = "④ 수동 개입 없음"
G_COSTS = "⑤ 비용 실측치 비교"
G_MAJOR = "중대 사건 없음 (판단 불일치, 손실 한도, 송금 권한 키)"
G_RUNNING = "일시 정지 아님"
G_AUDIT_NOW = "모의매매 대조 (최근 감사 통과)"
G_C1 = "모의매매 1번 (위험 대비 수익)"
G_C2 = "모의매매 2번 (최대 낙폭)"
G_C4 = "모의매매 4번 (28일, 원인 불명 0건, 커버리지 95%)"
G_C3 = "모의매매 3번 (표본 외 50건)"
G_KEY = "거래 키 권한 (view+trade, 송금 불가)"
SHADOW_PASS = (G_DAYS, G_SIGNAL, G_AUDIT, G_CYCLES, G_PREVIEW, G_RESTART)
CANARY_PASS = (G_TRADES, G_PROTECT, G_ORDERS, G_UNPROTECTED, G_MANUAL, G_COSTS, G_MAJOR, G_RUNNING, G_AUDIT_NOW)
REQUIRED_GATES = {("SHADOW", "CANARY"): SHADOW_PASS + (G_C1, G_C2, G_C4, G_KEY),
                  ("CANARY", "FULL"): CANARY_PASS + (G_C1, G_C2, G_C4, G_C3, G_KEY)}


class StageError(RuntimeError):
    """A stage transition was refused."""


@dataclass(frozen=True)
class Gate:
    name: str
    met: bool
    detail: str


def gate_lines(gates: Sequence[Gate]) -> list[str]:
    return [f"[{'충족' if g.met else '미충족'}] {g.name}: {g.detail}" for g in gates]


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


def paused(journal: Journal) -> dict[str, Any] | None:
    return journal.get(PAUSE_KEY)


def pause(journal: Journal, reason: str, now: float) -> None:
    journal.put(PAUSE_KEY, {"reason": reason, "ts": now})
    journal.add_event(now, "PAUSED", {"reason": reason})


def resume(journal: Journal, now: float) -> None:
    journal.put(PAUSE_KEY, None)
    journal.add_event(now, "RESUMED", {})


def canary_failures(journal: Journal, since: float) -> dict[str, tuple[int, float]]:
    """The CANARY failures since `since`: kind -> (count, latest time). Read from the INCIDENT events
    (`on_incident`) and from the events of those kinds themselves, so a failure journaled but never handed to
    `on_incident` (a crash in between) still counts."""
    found: dict[str, tuple[int, float]] = {}
    incidents: dict[str, list[float]] = {}
    for e in journal.events(kind="INCIDENT", since=since):
        k = e["data"].get("kind")
        if k in CANARY_FAIL:
            incidents.setdefault(k, []).append(float(e["ts"]))
    for k in CANARY_FAIL:
        times = incidents.get(k, []) + [float(e["ts"]) for e in journal.events(kind=k, since=since)]
        if times:
            found[k] = (max(len(incidents.get(k, [])), len(times) - len(incidents.get(k, []))), max(times))
    return found


def full_cap(journal: Journal, now: float) -> int | None:
    """The most contracts the journal's current stage allows - never more than the stage that is in force:
    SHADOW 0 (no real order), CANARY 1, FULL FULL_CAP_CONTRACTS until it has run FULL_CAP_DAYS without a CANARY
    failure (each one restarts the window, counted from the journal's event times), None (uncapped) only then."""
    stage, since = journal.stage()
    if stage == "CANARY":
        return 1
    if stage != "FULL" or since is None:
        return 0
    clean_from = max([float(since)] + [last for _, last in canary_failures(journal, float(since)).values()])
    return None if now - clean_from >= FULL_CAP_DAYS * DAY else FULL_CAP_CONTRACTS


def on_incident(journal: Journal, kind: str, now: float) -> list[str]:
    """Pause in CANARY for a CANARY failure; in FULL pause and demote to CANARY for a major incident (another
    CANARY failure keeps the 3-contract cap for 30 more days). Returns what was done."""
    stage, _ = journal.stage()
    done: list[str] = []
    if (stage == "CANARY" and kind in CANARY_FAIL) or (stage == "FULL" and kind in MAJOR):
        if paused(journal) is None:
            pause(journal, f"incident {kind}", now)
            done.append("paused")
        if stage == "FULL":
            reason = f"major incident {kind}"
            journal.set_stage("CANARY", reason, now)
            journal.add_event(now, "STAGE", {"from": "FULL", "to": "CANARY", "reason": reason})
            done.append("demoted to CANARY")
    elif stage == "FULL" and kind in CANARY_FAIL:
        done.append(f"{FULL_CAP_CONTRACTS}-contract cap for {FULL_CAP_DAYS} more days")
    journal.add_event(now, "INCIDENT", {"kind": kind, "stage": stage, "done": done})
    return done


def _since(journal: Journal, stage: str) -> float | None:
    hist = journal.stage_history()
    if not hist or hist[-1]["stage"] != stage:
        return None
    return float(hist[-1]["ts"])


def audit_gate(name: str, res: AuditResult | None, since: float, now: float) -> Gate:
    """A passing audit that covers the stage: its window starts at or before `since` and reaches a bar that
    closed at most AUDIT_MAX_AGE_S ago. A failed audit (fetch, invalid ledger) is unmet, never the last good one."""
    if res is None:
        return Gate(name, False, "미실행")
    if res.error is not None:
        return Gate(name, False, f"감사 실패: {res.error}"[:200])
    lo, hi = res.window
    if now - (hi + H1) > AUDIT_MAX_AGE_S:
        return Gate(name, False, f"오래된 감사: {_utc(hi)} 봉까지 ({_utc(res.as_of)} 실행)")
    if lo > since:
        return Gate(name, False, f"감사 구간이 단계 시작 뒤부터 ({_utc(lo)} 이후)")
    return Gate(name, res.ok, f"비교 {res.compared}건, 불일치 {len(res.mismatches)}건 ({_utc(hi)} 봉까지)")


def _closed_spans(journal: Journal, since: float, now: float) -> list[tuple[float, float]]:
    """Spans in which the futures session was recorded not OPEN: from a SESSION event whose `state` is not OPEN to
    the next SESSION event that is OPEN, or to now (live/runtime.py records the state with each SESSION event)."""
    spans: list[tuple[float, float]] = []
    start: float | None = None
    for e in journal.events(kind="SESSION", since=since):
        state = e["data"].get("state")
        if state is None:
            continue
        if state != SESSION_OPEN and start is None:
            start = float(e["ts"])
        elif state == SESSION_OPEN and start is not None:
            spans.append((start, float(e["ts"])))
            start = None
    if start is not None:
        spans.append((start, now))
    return spans


def _cycle_gate(journal: Journal, since: float, now: float) -> Gate:
    first = math.ceil((since - H1) / H1) * H1  # the first bar that closes in the stage
    last = math.floor((now - H1 - CYCLE_DEADLINE_S) / H1) * H1  # the last bar whose deadline has passed
    cycles = {int(c["bar_ts"]): c for c in journal.cycles(since_bar=first)}
    closed = _closed_spans(journal, since, now)
    on_time = excused = due = 0
    for bar in range(first, last + 1, H1):
        close = bar + H1
        c = cycles.get(bar)
        if (c is not None and c["status"] == "OK" and c["finished_ts"] is not None
                and c["finished_ts"] - close <= CYCLE_DEADLINE_S):
            on_time += 1
        elif any(s <= close + CYCLE_DEADLINE_S and close <= e for s, e in closed):
            excused += 1
            continue
        due += 1
    detail = f"{on_time}/{due}봉" + (f" (장 휴장 제외 {excused}봉)" if excused else "")
    return Gate(G_CYCLES, due > 0 and on_time == due, detail)


def shadow_gates(journal: Journal, now: float, audit_result: AuditResult | None) -> list[Gate]:
    since = _since(journal, "SHADOW")
    if since is None:
        return [Gate("SHADOW 단계", False, "현재 단계가 SHADOW가 아님")]
    days = (now - since) / DAY
    signals = [d for d in journal.decisions() if d["kind"] == "SIGNAL" and known_at(d) > since]
    previews = journal.events(kind="PREVIEW", since=since)
    failed = [p for p in previews if not p["data"].get("ok")]
    recoveries = journal.events(kind="RECOVERY", since=since)
    clean = [r for r in recoveries if not r["data"].get("unknown")]
    return [
        Gate(G_DAYS, days >= SHADOW_MIN_DAYS, f"{days:.1f}/{SHADOW_MIN_DAYS}일"),
        Gate(G_SIGNAL, len(signals) >= 1, f"단계 중 알게 된 신호 {len(signals)}건"),
        audit_gate(G_AUDIT, audit_result, since, now),
        _cycle_gate(journal, since, now),
        Gate(G_PREVIEW, bool(previews) and not failed, f"{len(previews) - len(failed)}/{len(previews)}"),
        Gate(G_RESTART, len(recoveries) >= 2 and len(clean) == len(recoveries),
             f"재시작 복구 {len(recoveries)}회 (첫 시작 포함), 미해결 {len(recoveries) - len(clean)}회"),
    ]


def _paper_gates(paper_status: dict[str, Any] | None, full: bool) -> list[Gate]:
    if paper_status is None:
        return [Gate("모의매매 판정", False, "paper ledger를 읽지 못함")]
    c4 = paper_status.get("c4") or {}
    gates = [
        Gate(G_C1, bool(paper_status.get("c1")), str(paper_status.get("c1"))),
        Gate(G_C2, bool(paper_status.get("c2")), str(paper_status.get("c2"))),
        Gate(G_C4, bool(c4.get("met")),
             f"{float(c4.get('days', 0.0)):.1f}일, 커버리지 {float(c4.get('coverage', 0.0)) * 100:.1f}%, "
             f"원인 불명 {c4.get('unexplained')}건"),
    ]
    if full:
        count = paper_status.get("count") or {}
        gates.append(Gate(G_C3, bool(paper_status.get("c3")), f"{count.get('total')}/{count.get('required')}"))
    return gates


def _key_gate(perms: KeyPermissions | None) -> Gate:
    if perms is None:
        return Gate("거래 키 권한", False, "확인 못 함")
    ok = perms.can_view and perms.can_trade and not perms.can_transfer
    return Gate(G_KEY, ok, f"view={perms.can_view} trade={perms.can_trade} transfer={perms.can_transfer}")


def canary_entry_gates(journal: Journal, now: float, audit_result: AuditResult | None,
                       paper_status: dict[str, Any] | None, perms: KeyPermissions | None) -> list[Gate]:
    return shadow_gates(journal, now, audit_result) + _paper_gates(paper_status, full=False) + [_key_gate(perms)]


def _counted(found: dict[str, tuple[int, float]], kinds: Sequence[str]) -> tuple[bool, str]:
    hits = [f"{k} {found[k][0]}건" for k in kinds if k in found]
    return not hits, ", ".join(hits) or "없음"


def canary_gates(journal: Journal, now: float, audit_result: AuditResult | None) -> list[Gate]:
    since = _since(journal, "CANARY")
    if since is None:
        return [Gate("CANARY 단계", False, "현재 단계가 CANARY가 아님")]
    days = (now - since) / DAY
    n_closed = len({e["data"]["trade_id"] for e in journal.events(kind="CLOSED", since=since) if e["data"].get("trade_id")})
    protected = journal.events(kind="PROTECTED_FIRST", since=since)
    slow = [p for p in protected if p["data"]["after_entry_s"] > PROTECT_MAX_S]
    found = canary_failures(journal, since)
    costs = journal.events(kind="COSTS", since=since)
    p = paused(journal)
    orders_ok, orders = _counted(found, (ORDER_UNRESOLVED,))
    unprotected_ok, unprotected = _counted(found, ("UNPROTECTED_60S",))
    manual_ok, manual = _counted(found, MANUAL)
    major_ok, major = _counted(found, ("AUDIT_MISMATCH", "BREAKER", "TRANSFER_KEY"))
    slow_ok = not slow and "PROTECT_SLOW" not in found
    return [
        Gate(G_TRADES, n_closed >= CANARY_MIN_TRADES or days >= CANARY_MAX_DAYS, f"{n_closed}건, {days:.1f}일"),
        Gate(G_PROTECT, bool(protected) and slow_ok, f"{len(protected) - len(slow)}/{len(protected)}"),
        Gate(G_ORDERS, orders_ok, f"해결되지 않은 주문 실패: {orders}"),
        Gate(G_UNPROTECTED, unprotected_ok, unprotected),
        Gate(G_MANUAL, manual_ok, manual),
        Gate(G_COSTS, bool(costs), f"비용 보고 {len(costs)}회"),
        Gate(G_MAJOR, major_ok, major),
        Gate(G_RUNNING, p is None, "운영 중" if p is None else f"일시 정지: {p['reason']}"),
        audit_gate(G_AUDIT_NOW, audit_result, since, now),
    ]


def full_entry_gates(journal: Journal, now: float, audit_result: AuditResult | None,
                     paper_status: dict[str, Any] | None, perms: KeyPermissions | None) -> list[Gate]:
    return canary_gates(journal, now, audit_result) + _paper_gates(paper_status, full=True) + [_key_gate(perms)]


def transition(journal: Journal, to: str, now: float, gates: Sequence[Gate], *, confirm: bool, flat: bool) -> None:
    """Record a stage change, or raise StageError with the reason. A promotion needs every gate of
    REQUIRED_GATES[(current, to)] among `gates` and every gate given met (a second guard on the caller's set)."""
    if to not in STAGES:
        raise StageError(f"unknown stage {to}")
    cur, _ = journal.stage()
    if to == cur:
        raise StageError(f"already in {cur}")
    if not confirm:
        raise StageError("a stage change needs --confirm (the user's explicit approval)")
    if not flat:
        raise StageError("a stage change needs a flat book (no live or virtual position, no resting order)")
    promotion = STAGES.index(to) > STAGES.index(cur)
    if promotion and STAGES.index(to) - STAGES.index(cur) != 1:
        raise StageError(f"{cur} -> {to}: promote one stage at a time")
    if promotion:
        given = {g.name for g in gates}
        unmet = [f"{g.name} ({g.detail})" for g in gates if not g.met]
        missing = [f"{n} (확인 안 됨)" for n in REQUIRED_GATES[(cur, to)] if n not in given]
        if unmet or missing:
            raise StageError("gates not met: " + "; ".join(unmet + missing))
    reason = "promotion" if promotion else "demotion"
    journal.set_stage(to, reason, now)
    journal.add_event(now, "STAGE", {"from": cur, "to": to, "reason": reason})
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run --group live pytest tests/live/test_stage.py -q
```

Expected: `16 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/live/audit.py src/ewbot/live/stage.py tests/live/test_stage.py
git commit -m "$(cat <<'EOF'
feat(live): paper-ledger audit, stage gates, pause and incident handling

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: Settings and the startup preflight

**Files:**
- Create: `src/ewbot/live/settings.py`, `src/ewbot/live/preflight.py`
- Test: `tests/live/test_preflight.py`

**Interfaces:**
- Consumes: `ewbot.backtest.holdout` (`DEFAULT_LOCK_PATH`, `lock_digest`), `ewbot.backtest.walkforward` (`HOLDOUT_START`, `grid_combos`), `ewbot.config.BotConfig`, `ewbot.paper.fingerprint` (`fingerprint_of`, `tree_files`), `ewbot.live.exchange`, `tests.paper.helpers` (`make_lock`, `synth_frame`), `tests.live.helpers` (`Clock`, `sim`).
- Produces (`ewbot.live.settings`): `class SettingsError(RuntimeError)`, frozen `LiveSettings(data_dir=Path("/data"), key_file=None, futures_id=FUTURES_PRODUCT, spot_id=SPOT_PRODUCT, telegram_token=None, telegram_chat=None, healthcheck_url=None, github_token=None, paper_repo=None, edit_stops=False, expiry={})` (its repr leaves out `key_file`, `telegram_token`, `healthcheck_url` and `github_token`) with `.journal_path` (`data_dir/journal.sqlite`) and `.cache_dir` (`data_dir/cache`); `check_private(path)`, `read_env_file(path) -> dict`, `parse_expiry(text) -> dict[name, ISO date]`, `private_chat(value) -> str | None` (`TELEGRAM_CHAT_ID` must be a positive number - the user's private chat with the bot, the only chat commands come from - and is returned canonical; a group or channel id or an @name is a `SettingsError`), `load_settings(environ=None) -> LiveSettings` (environment: `EWBOT_LIVE_DIR`, `EWBOT_COINBASE_KEY_FILE`, `EWBOT_SECRETS_FILE`, `EWBOT_FUTURES_PRODUCT`, `EWBOT_SPOT_PRODUCT`, `EWBOT_PAPER_REPO`, `EWBOT_LIVE_EDIT_STOPS`, `EWBOT_EXPIRY`; secrets file keys `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `EWBOT_GITHUB_TOKEN`, `EWBOT_HEALTHCHECK_URL`).
- Produces (`ewbot.live.preflight`): `BASELINE_FINGERPRINT`, `LOCK_SHA256`, `FINGERPRINT_FILES = 25`, `CONTRACT_SIZE = 0.01`, `PRICE_INCREMENT = 5.0`, `BASE_INCREMENT = 1.0`; frozen `Check(name, ok, detail)`; `class PreflightError(RuntimeError)` (`.checks`); `check_fingerprint(files=None, expected=BASELINE_FINGERPRINT) -> Check`, `check_lock(path=DEFAULT_LOCK_PATH, expected=LOCK_SHA256) -> (Check, lock dict | None)`, `check_key(perms, stage) -> Check`, `check_product(spec, expected_id) -> Check`, `run_preflight(exchange | None, stage, lock_path=DEFAULT_LOCK_PATH, files=None, expected_lock=LOCK_SHA256) -> (list[Check], lock | None)` (names in order `fingerprint`, `lock`, `key`, `product`; `exchange=None` runs the first two), `require(checks)`.

- [ ] **Step 1: Write the failing test**

**File: `tests/live/test_preflight.py`**

```python
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from ewbot.live.exchange import KeyPermissions, Unavailable
from ewbot.live.preflight import (
    BASELINE_FINGERPRINT,
    PreflightError,
    check_fingerprint,
    check_key,
    check_lock,
    check_product,
    require,
    run_preflight,
)
from ewbot.live.settings import SettingsError, load_settings, parse_expiry
from ewbot.live.sim import BIP_SPEC
from ewbot.paper.fingerprint import tree_files
from tests.live.helpers import Clock, sim
from tests.paper.helpers import make_lock, synth_frame


@pytest.fixture(scope="module")
def lock_file(tmp_path_factory) -> tuple[Path, dict]:
    lock = make_lock(synth_frame())
    path = tmp_path_factory.mktemp("lock") / "holdout.lock"
    path.write_text(json.dumps(lock), encoding="utf-8")
    return path, lock


def test_the_working_tree_fingerprint_is_the_pinned_baseline():
    c = check_fingerprint()
    assert c.ok and BASELINE_FINGERPRINT[:12] in c.detail and "25 files" in c.detail


def test_a_changed_or_missing_strategy_file_is_refused():
    files = tree_files()
    changed = dict(files, **{"src/ewbot/risk/sizing.py": files["src/ewbot/risk/sizing.py"] + b"\n"})
    assert not check_fingerprint(changed).ok
    assert not check_fingerprint({}).ok  # e.g. a non-editable install: src/ewbot not found


def test_lock_checks(lock_file, tmp_path):
    path, lock = lock_file
    ok, parsed = check_lock(path, expected=lock["sha256"])
    assert ok.ok and parsed["params"] == list(lock["params"])
    wrong, none = check_lock(path, expected="0" * 64)
    assert not wrong.ok and none is None and "committed" in wrong.detail
    bad = dict(lock, params=[9.0, 9.0, 9.0])
    (tmp_path / "bad.lock").write_text(json.dumps(bad), encoding="utf-8")
    c, _ = check_lock(tmp_path / "bad.lock", expected=lock["sha256"])
    assert not c.ok and "grid combo" in c.detail and "lock_digest" in c.detail
    assert not check_lock(tmp_path / "missing.lock")[0].ok


def test_key_permissions():
    assert check_key(KeyPermissions(True, False, False), "SHADOW").ok
    assert not check_key(KeyPermissions(True, False, False), "CANARY").ok
    assert check_key(KeyPermissions(True, True, False), "FULL").ok
    c = check_key(KeyPermissions(True, True, True), "SHADOW")
    assert not c.ok and "TRANSFER" in c.detail
    assert not check_key(KeyPermissions(False, True, False), "SHADOW").ok


def test_product_spec():
    assert check_product(BIP_SPEC, "BIP-20DEC30-CDE").ok
    assert not check_product(dataclasses.replace(BIP_SPEC, contract_size=0.1), "BIP-20DEC30-CDE").ok
    assert not check_product(dataclasses.replace(BIP_SPEC, price_increment=1.0), "BIP-20DEC30-CDE").ok
    assert not check_product(BIP_SPEC, "BIT-31OCT26-CDE").ok


def test_run_preflight_online_offline_and_exchange_errors(lock_file):
    path, lock = lock_file
    ex = sim(Clock())
    checks, parsed = run_preflight(ex, "CANARY", path, expected_lock=lock["sha256"])
    assert [c.name for c in checks] == ["fingerprint", "lock", "key", "product"] and all(c.ok for c in checks)
    require(checks)
    offline, _ = run_preflight(None, "SHADOW", path, expected_lock=lock["sha256"])
    assert [c.name for c in offline] == ["fingerprint", "lock"]
    ex.fail_next("key_permissions", Unavailable("503"))
    failing, _ = run_preflight(ex, "SHADOW", path, expected_lock=lock["sha256"])
    with pytest.raises(PreflightError, match="key_permissions failed"):
        require(failing)
    transfer = sim(Clock(), permissions=KeyPermissions(True, True, True))
    with pytest.raises(PreflightError, match="TRANSFER"):
        require(run_preflight(transfer, "SHADOW", path, expected_lock=lock["sha256"])[0])


def test_settings_read_secrets_only_from_private_files(tmp_path):
    secrets = tmp_path / "live.env"
    secrets.write_text("# ewbot\nTELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID='42'\nEWBOT_GITHUB_TOKEN=\"ghp_x\"\n",
                       encoding="utf-8")
    key = tmp_path / "cdp_api_key.json"
    key.write_text('{"name": "organizations/o/apiKeys/k", "privateKey": "-----BEGIN EC PRIVATE KEY-----"}')
    secrets.chmod(0o600)
    key.chmod(0o600)
    env = {"EWBOT_SECRETS_FILE": str(secrets), "EWBOT_COINBASE_KEY_FILE": str(key), "EWBOT_LIVE_DIR": str(tmp_path),
           "TELEGRAM_CHAT_ID": "7", "EWBOT_PAPER_REPO": "me/repo", "EWBOT_LIVE_EDIT_STOPS": "1",
           "EWBOT_EXPIRY": "coinbase=2027-10-01, github=2027-09-30", "EWBOT_HEALTHCHECK_URL": "https://hc-ping.com/u-1"}
    s = load_settings(env)
    assert (s.telegram_token, s.telegram_chat, s.github_token) == ("123:abc", "42", "ghp_x")  # the file wins
    assert s.healthcheck_url == "https://hc-ping.com/u-1"
    assert not any(secret in repr(s) for secret in ("123:abc", "ghp_x", "hc-ping.com/u-1", str(key)))
    assert s.key_file == key and s.journal_path == tmp_path / "journal.sqlite" and s.cache_dir == tmp_path / "cache"
    assert s.paper_repo == "me/repo" and s.edit_stops and s.futures_id == "BIP-20DEC30-CDE"
    assert s.expiry == {"coinbase": "2027-10-01", "github": "2027-09-30"}
    secrets.chmod(0o644)
    with pytest.raises(SettingsError, match="mode 600"):
        load_settings(env)
    secrets.chmod(0o600)
    key.chmod(0o640)
    with pytest.raises(SettingsError, match="mode 600"):
        load_settings(env)
    defaults = load_settings({})
    assert defaults.data_dir == Path("/data") and defaults.key_file is None and not defaults.edit_stops
    with pytest.raises(SettingsError):
        parse_expiry("coinbase:2027-10-01")


def test_telegram_commands_come_only_from_a_private_chat(tmp_path):
    secrets = tmp_path / "live.env"
    env = {"EWBOT_SECRETS_FILE": str(secrets)}
    for chat in ("-1001234567890", "-42", "@ewbot_alerts", "0", "42a"):  # a channel or group id, a name
        secrets.write_text(f"TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID={chat}\n", encoding="utf-8")
        secrets.chmod(0o600)
        with pytest.raises(SettingsError, match="private chat"):
            load_settings(env)
    secrets.write_text("TELEGRAM_CHAT_ID=0042\n", encoding="utf-8")
    assert load_settings(env).telegram_chat == "42"  # as Telegram writes the chat id
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --group live pytest tests/live/test_preflight.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'ewbot.live.preflight'`.

- [ ] **Step 3: Write minimal implementation**

**File: `src/ewbot/live/settings.py`**

```python
"""Settings of `ewbot live` (Plan 3 spec 3.4).

Non-secret values come from the environment (deploy/live/docker-compose.yml). Secrets live in two files
on the VPS that only their owner may read (mode 600, refused otherwise), mounted read-only into the
container and never stored in the repository, the image or a log:
  EWBOT_COINBASE_KEY_FILE  the CDP API key JSON as downloaded ({"name": ..., "privateKey": ...})
  EWBOT_SECRETS_FILE       KEY=VALUE lines: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, EWBOT_GITHUB_TOKEN,
                           EWBOT_HEALTHCHECK_URL (values in the secrets file win over the environment).
TELEGRAM_CHAT_ID must be a positive number: the user's private chat with the bot (its id is the user's own id),
the only chat Telegram commands are taken from (live/control.py); a group or channel id (negative) or an @name
is refused. A LiveSettings repr never shows a secret or the key file's path."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Mapping

from ewbot.live.exchange import FUTURES_PRODUCT, SPOT_PRODUCT


class SettingsError(RuntimeError):
    """A setting or a secrets file is missing, malformed or readable by others."""


@dataclass(frozen=True)
class LiveSettings:
    data_dir: Path = Path("/data")
    key_file: Path | None = field(default=None, repr=False)  # secrets and where they are: never in a repr
    futures_id: str = FUTURES_PRODUCT
    spot_id: str = SPOT_PRODUCT
    telegram_token: str | None = field(default=None, repr=False)
    telegram_chat: str | None = None
    healthcheck_url: str | None = field(default=None, repr=False)
    github_token: str | None = field(default=None, repr=False)
    paper_repo: str | None = None
    edit_stops: bool = False
    expiry: dict[str, str] = field(default_factory=dict)

    @property
    def journal_path(self) -> Path:
        return self.data_dir / "journal.sqlite"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"


def check_private(path: Path) -> None:
    """The file exists and neither its group nor others have any permission on it."""
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise SettingsError(f"{path} is not readable: {type(exc).__name__}") from None
    if mode & 0o077:
        raise SettingsError(f"{path} must be mode 600 (chmod 600), is {oct(mode & 0o777)}")


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            raise SettingsError(f"{path}:{n} is not KEY=VALUE")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def parse_expiry(text: str) -> dict[str, str]:
    """'coinbase=2027-10-01,github=2027-09-30' -> {name: ISO date}."""
    out: dict[str, str] = {}
    for item in filter(None, (x.strip() for x in text.split(","))):
        name, sep, day = item.partition("=")
        try:
            out[name.strip()] = date.fromisoformat(day.strip()).isoformat()
        except ValueError:
            raise SettingsError(f"EWBOT_EXPIRY item {item!r} is not name=YYYY-MM-DD") from None
        if not sep or not name.strip():
            raise SettingsError(f"EWBOT_EXPIRY item {item!r} is not name=YYYY-MM-DD")
    return out


def private_chat(value: str | None) -> str | None:
    """TELEGRAM_CHAT_ID as the canonical positive number of a private chat (None when unset)."""
    if value is None:
        return None
    if not (value.isascii() and value.isdigit() and int(value) > 0):
        raise SettingsError("TELEGRAM_CHAT_ID must be the positive id of your private chat with the bot (your Telegram "
                            "user id): commands are taken only from a private chat - not a group or channel id "
                            "(negative) or an @name")
    return str(int(value))


def load_settings(environ: Mapping[str, str] | None = None) -> LiveSettings:
    env = os.environ if environ is None else environ
    secrets: dict[str, str] = {}
    sf = env.get("EWBOT_SECRETS_FILE", "").strip()
    if sf:
        check_private(Path(sf))
        secrets = read_env_file(Path(sf))

    def get(name: str) -> str | None:
        return (secrets.get(name) or env.get(name) or "").strip() or None

    kf = env.get("EWBOT_COINBASE_KEY_FILE", "").strip()
    if kf:
        check_private(Path(kf))
    return LiveSettings(
        data_dir=Path(env.get("EWBOT_LIVE_DIR", "/data")),
        key_file=Path(kf) if kf else None,
        futures_id=env.get("EWBOT_FUTURES_PRODUCT", FUTURES_PRODUCT).strip() or FUTURES_PRODUCT,
        spot_id=env.get("EWBOT_SPOT_PRODUCT", SPOT_PRODUCT).strip() or SPOT_PRODUCT,
        telegram_token=get("TELEGRAM_BOT_TOKEN"),
        telegram_chat=private_chat(get("TELEGRAM_CHAT_ID")),
        healthcheck_url=get("EWBOT_HEALTHCHECK_URL"),
        github_token=get("EWBOT_GITHUB_TOKEN"),
        paper_repo=(env.get("EWBOT_PAPER_REPO", "").strip() or None),
        edit_stops=env.get("EWBOT_LIVE_EDIT_STOPS", "0").strip() == "1",
        expiry=parse_expiry(env.get("EWBOT_EXPIRY", "")),
    )
```

**File: `src/ewbot/live/preflight.py`**

```python
"""Startup checks (Plan 3 spec 3.4, 4.5 `시작 시 점검`, 7 `안전 검사`). Any failure refuses to run.

  fingerprint  the 25 strategy-fingerprinted files hash to the frozen baseline e0435223... (the value is
               pinned here: the image has no git history). An empty file set (a non-editable install,
               where src/ewbot is not below the project root) fails as well.
  lock         holdout.lock is the committed one (sha256 d1be04baa06c...), its params are a grid combo,
               its holdout start is HOLDOUT_START and its sha256 equals lock_digest(BotConfig(), params,
               random_runs, code_sha256): the configuration the runtime uses is the one the holdout ran.
  key          GET /key_permissions: can_view, can_trade for CANARY/FULL, and never can_transfer.
  product      the futures product is BIP: contract_size 0.01, price_increment 5, base_increment 1."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ewbot.backtest.holdout import DEFAULT_LOCK_PATH, lock_digest
from ewbot.backtest.walkforward import HOLDOUT_START, grid_combos
from ewbot.config import BotConfig
from ewbot.live.exchange import Exchange, ExchangeError, KeyPermissions, ProductSpec
from ewbot.paper.fingerprint import fingerprint_of, tree_files

BASELINE_FINGERPRINT = "e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1"
LOCK_SHA256 = "d1be04baa06c58dd483c88e053841d6eda56c1a560fd3196746bab311ece3c45"
FINGERPRINT_FILES = 25
CONTRACT_SIZE = 0.01
PRICE_INCREMENT = 5.0
BASE_INCREMENT = 1.0


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


class PreflightError(RuntimeError):
    def __init__(self, checks: list[Check]) -> None:
        bad = [c for c in checks if not c.ok]
        super().__init__("preflight failed: " + "; ".join(f"{c.name}: {c.detail}" for c in bad))
        self.checks = checks


def check_fingerprint(files: Mapping[str, bytes] | None = None, expected: str = BASELINE_FINGERPRINT) -> Check:
    files = tree_files() if files is None else files
    fp = fingerprint_of(files)
    ok = len(files) == FINGERPRINT_FILES and fp == expected
    return Check("fingerprint", ok, f"{fp[:12]} over {len(files)} files (baseline {expected[:12]}, {FINGERPRINT_FILES} files)")


def check_lock(path: Path = DEFAULT_LOCK_PATH, expected: str = LOCK_SHA256) -> tuple[Check, dict[str, Any] | None]:
    try:
        lock = json.loads(Path(path).read_text(encoding="utf-8"))
        params = tuple(float(x) for x in lock["params"])
        random_runs = int(lock["random_runs"])
        code_sha = str(lock["code_sha256"])
        holdout_start = int(lock["holdout_start"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return Check("lock", False, f"{path} unreadable: {type(exc).__name__}"), None
    problems = []
    if params not in grid_combos():
        problems.append(f"params {list(params)} not a grid combo")
    if holdout_start != HOLDOUT_START:
        problems.append(f"holdout_start {holdout_start}")
    digest = lock_digest(BotConfig(), params, random_runs, code_sha)
    if digest != lock.get("sha256"):
        problems.append(f"lock_digest {digest[:12]} != lock sha256 {str(lock.get('sha256'))[:12]}")
    if lock.get("sha256") != expected:
        problems.append(f"lock sha256 {str(lock.get('sha256'))[:12]} != committed {expected[:12]}")
    detail = "; ".join(problems) or f"sha256 {expected[:12]}, params {list(params)}"
    return Check("lock", not problems, detail), (lock if not problems else None)


def check_key(perms: KeyPermissions, stage: str) -> Check:
    problems = []
    if perms.can_transfer:
        problems.append("the key can TRANSFER: create a view+trade key only")
    if not perms.can_view:
        problems.append("the key cannot view")
    if stage in ("CANARY", "FULL") and not perms.can_trade:
        problems.append(f"{stage} needs trade permission")
    detail = "; ".join(problems) or f"view={perms.can_view} trade={perms.can_trade} transfer=False"
    return Check("key", not problems, detail)


def check_product(spec: ProductSpec, expected_id: str) -> Check:
    problems = []
    if spec.product_id != expected_id:
        problems.append(f"product {spec.product_id} != {expected_id}")
    if spec.contract_size != CONTRACT_SIZE:
        problems.append(f"contract_size {spec.contract_size} != {CONTRACT_SIZE}")
    if spec.price_increment != PRICE_INCREMENT:
        problems.append(f"price_increment {spec.price_increment} != {PRICE_INCREMENT}")
    if spec.base_increment != BASE_INCREMENT:
        problems.append(f"base_increment {spec.base_increment} != {BASE_INCREMENT}")
    detail = "; ".join(problems) or f"{spec.product_id}: 0.01 BTC/contract, $5 tick, whole contracts"
    return Check("product", not problems, detail)


def run_preflight(exchange: Exchange | None, stage: str, lock_path: Path = DEFAULT_LOCK_PATH,
                  files: Mapping[str, bytes] | None = None, expected_lock: str = LOCK_SHA256
                  ) -> tuple[list[Check], dict[str, Any] | None]:
    """All checks; `exchange=None` runs the offline ones only (fingerprint, lock)."""
    lock_check, lock = check_lock(lock_path, expected_lock)
    checks = [check_fingerprint(files), lock_check]
    if exchange is not None:
        try:
            checks.append(check_key(exchange.key_permissions(), stage))
        except ExchangeError as exc:
            checks.append(Check("key", False, f"key_permissions failed: {exc}"))
        try:
            checks.append(check_product(exchange.product(), exchange.futures_id))
        except ExchangeError as exc:
            checks.append(Check("product", False, f"product failed: {exc}"))
    return checks, lock


def require(checks: list[Check]) -> None:
    if any(not c.ok for c in checks):
        raise PreflightError(checks)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run --group live pytest tests/live/test_preflight.py -q
```

Expected: `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/live/settings.py src/ewbot/live/preflight.py tests/live/test_preflight.py
git commit -m "$(cat <<'EOF'
feat(live): settings with mode-600 secrets files and the startup preflight (fingerprint, lock, key, product)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: `ShadowExchange` and the runtime main loop

**Files:**
- Create: `src/ewbot/live/shadow.py`, `src/ewbot/live/runtime.py`
- Create: `tests/live/harness.py` (a `LiveRuntime` on synthetic candles with a `SimExchange` that follows them; reused by Tasks 13–14)
- Test: `tests/live/test_runtime.py`

**Interfaces:**
- Consumes: every module of Tasks 1–11; `ewbot.backtest.holdout.data_fingerprint` (the audit's DATA_REVISION check), `ewbot.data.candles` (`fill_gaps`, `load_1h`), `ewbot.execution.costs.fee` (the paper cost model, for the cost report), `tests.paper.helpers` (`FakeFetch`, `make_lock`, `synth_frame`, `ts`), `tests.live.helpers`.
- Produces (`ewbot.live.shadow`): `class ShadowExchange(SimExchange)(real: Exchange, journal, *, clock=time.time, alert=None)`: real `product/session/quote/balance/key_permissions` (so its `FAULT_METHODS` leaves them out: their faults are injected on `real`); `restore(trade)` puts an open virtual position (`reconcile.expected_contracts`) back after a restart; `place_market`, `place_bracket`, `close_position` journal a `PREVIEW` event (`kind, client_order_id, ok, errors, warnings, ...`) from the real preview, alert `PREVIEW_FAILED` on errors, then fill virtually.
- Produces (`ewbot.live.runtime`): constants `CYCLE_OFFSET_S = 30`, `CYCLE_RETRY_S = 15`, `DATA_DEADLINE_S = 120`, `ENTRY_DEADLINE_S = 300`, `POLL_ACTIVE_S = 10.0`, `POLL_IDLE_S = 60.0`, `AUDIT_MINUTE = 50`, `DAILY_HOUR_NY = 9`, `MARGIN_CHECK_NY = (15, 30)`, `FLATTEN_CONFIRM_S = 60.0` (between the messages' Telegram dates), `COMMAND_MAX_AGE_S = 120.0`, `CONTROL_ALERT_FAILURES = 5`, `CONTROL_STUCK_S = 120.0`, `TELEGRAM_DOWN_S = 600.0`, `NOTIFY_STALL_S = 180.0`, `NOTIFY_REFUSALS = 3`, `HC_WITHHOLD_S = 1800.0`, `HC_REFAIL_S = 10_800.0`, `HEARTBEAT_ALERT_FAILURES = 3`, `EXPIRY_WARN_DAYS = 30`, `SNAPSHOT_TRIES = 3`, `FEED_ALERT_S = 300.0`, `BAD_EQUITY_PAUSE_READS = 3`, journal keys `HEARTBEAT_KEY = "heartbeat"`, `AUDIT_KEY = "audit"` (`AuditResult.to_json`), `AUDIT_PENDING_KEY = "audit_pending"`, `ORDER_FAIL_KEY = "order_failures"`; `FAILURE_CLASS` / `ACTION_CLASS` (an order failure is about a bracket, a close or a reduction); dataclass `LiveDeps(journal, exchange, lock, fetch: Callable[[Path], Path], cache_dir, notify=no-op, clock=time.time, sleep=time.sleep, feed=None, heartbeat=None, commands: Queue[Command] | None = None, control=None, paper_ledger: Callable[[], dict] | None = None, edit_stops=False, expiry={}, redact=identity)` (`redact` takes the settings' secrets out of error texts: loop, cycle and fetch errors and the loop's tracebacks on stderr) (`lock` is the preflight-checked holdout lock: the runtime builds its configuration with `target.frozen_config(lock)` and passes the lock to `compute_target`; there is no configuration or start field); `class LiveRuntime(deps)` with `startup() -> recovery dict` (`executor.recover()` never raises on a failed exchange read: `RECOVERY_ERROR`, counted `unknown`, so SHADOW gate ④ sees that restart as unresolved; a trade it rebuilds opens at its entry order's creation, so a long outage shows as `PROTECT_SLOW`), `cycle(bar_ts, now) -> "OK"|"LATE"|"ERROR"`, `reconcile(now)` (first `executor.resolve_reduce()`, then position and open orders as one state - the position read again after the orders, the pair used only when both reads agree, `SNAPSHOT_TRIES` times, else `ExchangeError` and the poll is skipped; `REDUCE_NOT_SEEN` is alerted; its `PlanContext` carries the stage maximum `max_contracts` - 1 below FULL, `stage.full_cap` in FULL - and the entry size, both from `_limits(now)`: the more conservative of the stage this process runs in and the journal's, re-read every time, so a demotion applies to the next order at once (a real runtime whose journal is back in SHADOW sends no entry); after every completed pass `_order_failures` resolves each order failure or records it `ORDER_UNRESOLVED`, an incident before its alert - a bracket not replaced within `PROTECT_MAX_S`, a close not done (a `CLOSED` event) or a reduction not done by the next pass; `UNEXPLAINED_REDUCTION` is alerted, and it, `UNPROTECTED_60S` and the `FOREIGN_ORDERS` / `UNKNOWN_POSITION` / `EXCESS_POSITION` alerts go to `on_incident`, as does a planned `Close` of a foreign position (`UNKNOWN_POSITION`) or of the trade's position on the other side (`DIRECTION_MISMATCH`) and an `ADOPTED` event of the pass: a foreign position taken over, or a provisional trade settled as foreign), `poll(now)`, `watch(now)`, `minute(now)` (first - in a guard that records a failure as `LOOP_ERROR` with section `telegram` and the type name, never skipping the rest of the minute - the Telegram command thread: started again when it died; a dead thread, one `CONTROL_STUCK_S` behind its own schedule (`overdue`; a hung request: reported, not replaced), `CONTROL_ALERT_FAILURES` failed polls in a row or a new HTTP 409 is alerted once per episode, `TELEGRAM_DOWN` / `TELEGRAM_UP`; and the external heartbeat: Telegram is unusable while such a problem has lasted `TELEGRAM_DOWN_S` or the notifier cannot deliver - its oldest failing message `TELEGRAM_DOWN_S` old, messages waiting `TELEGRAM_DOWN_S` with no attempt for `NOTIFY_STALL_S`, or `NOTIFY_REFUSALS` refusals in a row -, and then `heartbeat.fail` (`TELEGRAM_UNUSABLE`), the success pings withheld at most `HC_WITHHOLD_S` and the `/fail` repeated every `HC_REFAIL_S` while it lasts; otherwise `heartbeat.beat()`, and `HEARTBEAT_ALERT_FAILURES` failed pings in a row alerted once, `HEARTBEAT_FAILING`; then the account breakers - a balance that is not a finite amount above 0 is one `BAD_EQUITY` alert and event per episode, never a breaker sample nor a margin check, and `BAD_EQUITY_PAUSE_READS` of them in a row are a `BAD_EQUITY` incident that pauses entries (no flatten; protection stays) -, the margin (never acted on without a futures price: one `MARGIN` alert and `MARGIN_NO_PRICE` event per episode; the 15:30 report and the daily summary show the entry price instead only when it is usable), and the WebSocket feed: no futures tick for `FEED_ALERT_S` while the session is OPEN → one `FEED` alert per outage with its reconnect failures and last error, `FEED_DOWN` / `FEED_UP` events, and an alert when it is back; a session change is journaled as `SESSION` with the new `state` (the SHADOW cycle gate excuses hours the session was recorded closed); the 15:30 report's reads are retried every minute until 16:00, the first failure alerted, and the day is marked done only once they succeed), `run_audit(now) -> AuditResult | None` (up to the last bar whose cycle ended OK, with the live candles' hashes: a `DATA_REVISION` is alerted only - `AUDIT_REVISION` -, a decision missing on one side is `AUDIT_PENDING` until the next audit confirms it, a different decision or a paper fingerprint change is `AUDIT_MISMATCH` at once; a failed audit - any exception - is stored as `AuditResult.failed` and alerted once per episode, `AUDIT_ERROR`), `costs(since) -> {fills, slippage_bps, stop_slippage_bps, fees, model_fees}` (journals `BRACKET_FILL` for every leg that left the book), `daily(now)`, `handle(cmd, now, sent=None) -> reply` (`sent`: the message's Telegram date, `now` when not given; `/flatten_confirm` counts only when sent within `FLATTEN_CONFIRM_S` of its `/flatten`; the loop handles the queue's `Command`s and ignores one sent more than `COMMAND_MAX_AGE_S` before it reads it: `COMMAND_STALE` event and the reply `오래된 명령 무시: /…`; `/resume` re-bases only a breaker that tripped, `AccountGuard.reset`, from the median of that read and the next two; the reply names it), `tick()`, `run_forever(max_ticks=None) -> int` (alerts are only queued - `deps.notify` is the background `LiveNotifier` - and every protective action, a stop-gap or unprotected close, a breaker's pause and flatten, a margin reduction, an incident's pause - an audit mismatch's and a transfer-permission key's included -, is taken before its alert), `flat() -> bool`, attributes `cfg`, `stage`, `target`, `frame` (the last cycle's candles), `executor`, `prices`, `guard`, `active`, `exit_code`, `done_bar`, `next_try`; module functions `status_lines(journal, now) -> list[str]` (journal only; the FULL cap, the last audit or its failure) and `audit_from_journal(journal) -> AuditResult | None` (the last stored audit, a failed one included).

- [ ] **Step 1: Write the failing test**

**File: `tests/live/harness.py`**

```python
"""A LiveRuntime on the synthetic candles of tests/paper/helpers with a SimExchange whose futures and spot
prices follow those candles (basis ratio `ratio`). Used by the runtime, parity and chaos tests.

`walk(start, end)` replays the hours [start, end): at hh:00:30 the decision cycle runs with the price at
the bar's open (the engine fills market orders at the next open), then the bar's path is played in
four steps - open, the adverse extreme first (stop before TP, as the engine assumes), the favourable
extreme, the close - with a runtime tick after each step (poll -> observe -> reconcile)."""

from __future__ import annotations

import queue
from pathlib import Path

import pandas as pd

from ewbot.live.control import Command
from ewbot.live.journal import Journal
from ewbot.live.runtime import LiveDeps, LiveRuntime
from ewbot.live.sim import SimExchange
from ewbot.types import H1
from tests.live.helpers import Alerts, Clock
from tests.paper.helpers import FakeFetch, make_lock, synth_frame


class Harness:
    def __init__(self, tmp_path: Path, start: int, *, stage: str | None = "CANARY", stage_days: float = 60.0,
                 equity: float = 10_000.0, ratio: float = 1.0, df: pd.DataFrame | None = None,
                 exchange: SimExchange | None = None, journal_name: str = "journal.sqlite", **sim_kw) -> None:
        self.tmp = tmp_path
        self.df = synth_frame() if df is None else df
        self.lock = make_lock(self.df)  # a temp holdout lock (PARAMS) for these candles
        self.ratio = ratio
        self.clock = Clock(start)
        self.ex = exchange if exchange is not None else SimExchange(clock=self.clock, equity=equity, **sim_kw)
        self.journal = Journal(tmp_path / journal_name)
        if stage is not None and self.journal.stage()[1] is None:  # None: a fresh journal without a stage
            self.journal.set_stage(stage, "test", start - stage_days * 86_400)
        self.alerts = Alerts()
        self.commands: queue.Queue[Command] = queue.Queue()
        self.fetch = FakeFetch(self.df, self.clock)
        self.by_ts = self.df.set_index("ts")
        self.rt = self.make_runtime()

    def make_runtime(self, **kw) -> LiveRuntime:
        deps = LiveDeps(journal=self.journal, exchange=self.ex, lock=self.lock, fetch=self.fetch,
                        cache_dir=self.tmp / "cache", notify=self.alerts, clock=self.clock, sleep=self.clock.sleep,
                        commands=self.commands, **kw)
        return LiveRuntime(deps)

    def price(self, spot: float) -> None:
        self.ex.set_prices(spot * self.ratio, spot)

    def bar(self, ts: int) -> pd.Series:
        return self.by_ts.loc[ts]

    def at(self, now: float) -> None:
        self.clock.now = float(now)
        self.rt.tick()

    def hour(self, h: int) -> None:
        """Bar h (open time) runs: cycle for bar h - 1 at h + 30 s, then the bar's path."""
        b = self.bar(h)
        self.price(float(b["open"]))
        self.at(h + 30)
        pos = self.ex.position()
        down_first = pos is None or pos.side > 0
        path = [b["low"], b["high"]] if down_first else [b["high"], b["low"]]
        for k, p in enumerate(path + [b["close"]]):
            self.price(float(p))
            self.at(h + 600 * (k + 1) + 30)

    def walk(self, start: int, end: int) -> None:
        for h in range(int(start), int(end), H1):
            self.hour(h)
```

**File: `tests/live/test_runtime.py`**

```python
from __future__ import annotations

import dataclasses
import json
import math
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from ewbot.backtest.holdout import data_fingerprint
from ewbot.live.audit import AuditError, audit
from ewbot.live.control import Command
from ewbot.live.exchange import Balance, ExchangeError, KeyPermissions, Order, Position, RateLimited, Unavailable
from ewbot.live.feed import TickerFeed
from ewbot.live.health import Heartbeat
from ewbot.live.notify import LiveNotifier
from ewbot.live.preflight import BASELINE_FINGERPRINT
from ewbot.live.reconcile import pending_position
from ewbot.live.runtime import audit_from_journal, status_lines
from ewbot.live.shadow import ShadowExchange
from ewbot.live.sim import SimExchange
from ewbot.live.stage import canary_gates, on_incident, paused, resume
from ewbot.live.target import compute_target
from ewbot.paper.decisions import known_at
from ewbot.paper.notify import SendError
from tests.live.harness import Harness
from tests.live.helpers import Clock, journal, position
from tests.paper.helpers import synth_frame, ts

SIGNAL = ts("2024-03-13 22:00")  # C_END long: entry 23:00, trail 03-14 07:00, TP1 + break-even 03-14 23:00
ENTRY_BAR = SIGNAL + 3600


@pytest.fixture
def h(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    yield hs
    hs.journal.close()


def entered(h: Harness) -> None:
    h.at(ENTRY_BAR + 30)
    assert h.ex.position() is not None


def noting(h: Harness, probe: Callable[[], Any]) -> list[tuple[str, str, Any]]:
    """From now on every alert is also recorded with probe() at the moment it was queued."""
    out: list[tuple[str, str, Any]] = []
    alerts = h.rt.d.notify

    def note(kind: str, text: str) -> None:
        out.append((kind, text, probe()))
        alerts(kind, text)

    h.rt.d.notify = note
    return out


def alerts_with_positions(h: Harness) -> list[tuple[str, str, Position | None]]:
    """From now on every alert is also recorded with the exchange position at the moment it was queued."""
    return noting(h, h.ex.position)


def paper_ledger(through: int, decisions: list[dict[str, Any]], runs: list[dict[str, Any]] | None = None) -> dict:
    """A schema-1 paper ledger whose one epoch (the baseline fingerprint) records known_at in (2024-01-10, through]."""
    return {"schema_version": 1, "baseline": {"fingerprint": BASELINE_FINGERPRINT, "commit": "abc1234"}, "holdout": {},
            "epochs": [{"id": 1, "fingerprint": BASELINE_FINGERPRINT, "started_ts": ts("2024-01-10 00:00"),
                        "through_ts": through}], "decisions": decisions, "runs": runs or []}


def test_startup_records_the_initial_stage_and_the_recovery(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR, stage=None)
    hs.price(26_700.0)
    out = hs.rt.startup()
    assert hs.journal.stage()[0] == "SHADOW" and hs.rt.stage == "SHADOW"
    assert out == {"accepted": 0, "abandoned": 0, "unknown": 0, "rebuilt": 0}
    assert [e["kind"] for e in hs.journal.events()][:2] == ["RECOVERY", "STARTUP"] and "STARTUP" in hs.alerts.kinds()
    assert hs.fetch.calls == 1  # the candle history is fetched before the first timed cycle
    hs.journal.close()


def test_the_cycle_enters_one_contract_in_canary_and_protects_it(h):
    h.at(ENTRY_BAR + 30)
    pos = h.ex.position()
    assert (pos.side, pos.contracts, pos.avg_entry) == (1, 1, 26_747.0)
    assert [(o.size, o.limit_price, o.stop_price) for o in h.ex.open_orders()] == [(1, 27_245.0, 26_585.0)]
    c = h.journal.cycles()[-1]
    assert (c["bar_ts"], c["status"]) == (SIGNAL, "OK") and c["finished_ts"] - (SIGNAL + 3600) <= 120
    ids = {d["id"] for d in h.journal.decisions()}
    assert f"SIGNAL:{SIGNAL}:{SIGNAL}" in ids and "ENTRY" in h.alerts.kinds()
    assert h.journal.get("target")["entry"]["signal_ts"] == SIGNAL
    h.at(ENTRY_BAR + 45)  # the same hour again: nothing is sent twice
    assert h.ex.calls.count("place_market") == 1


def test_a_late_bar_is_retried_then_the_decision_is_held(tmp_path):
    df = synth_frame()
    hs = Harness(tmp_path, ENTRY_BAR + 30, df=df[df["ts"] < SIGNAL])
    hs.price(26_700.0)
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    assert hs.rt.next_try == ENTRY_BAR + 45 and hs.journal.cycles()[-1]["status"] == "RUNNING"
    hs.at(ENTRY_BAR + 60)
    hs.at(ENTRY_BAR + 121)
    c = hs.journal.cycles()[-1]
    assert (c["bar_ts"], c["status"]) == (SIGNAL, "HOLD") and "DATA_LATE" in hs.alerts.kinds()
    assert hs.rt.target is None and hs.ex.position() is None and hs.rt.done_bar == SIGNAL
    hs.journal.close()


def test_a_paused_runtime_records_the_skipped_entry(h):
    h.commands.put(Command("/pause", ENTRY_BAR + 25))
    h.at(ENTRY_BAR + 30)
    assert h.ex.position() is None and paused(h.journal)["reason"] == "user /pause"
    skip = h.journal.events(kind="SKIP")[0]["data"]
    assert skip["signal_ts"] == SIGNAL and "일시 정지" in skip["reason"]
    guard = h.rt.guard.state()
    h.commands.put(Command("/resume", ENTRY_BAR + 39))
    h.at(ENTRY_BAR + 40)
    assert paused(h.journal) is None and h.rt.guard.state() == guard  # no breaker tripped: nothing re-based


def test_flatten_needs_a_confirmation_within_sixty_seconds(h):
    entered(h)
    assert h.rt.handle("/flatten_confirm", ENTRY_BAR + 40).startswith("확인할 /flatten 요청이 없음")
    h.rt.handle("/flatten", ENTRY_BAR + 50)
    assert h.rt.handle("/flatten_confirm", ENTRY_BAR + 111).startswith("확인할")  # 61 s later: expired
    h.rt.handle("/flatten", ENTRY_BAR + 120)
    assert h.rt.handle("/flatten_confirm", ENTRY_BAR + 130) == "전량 청산 완료, 일시 정지"
    assert h.ex.position() is None and h.ex.open_orders() == [] and paused(h.journal)["reason"] == "user /flatten"
    assert "단계 CANARY" in h.rt.handle("/status", ENTRY_BAR + 140)
    assert h.rt.handle("/help", ENTRY_BAR + 150).startswith("/status")


def test_a_failing_command_is_reported_and_the_loop_goes_on(h):
    entered(h)
    h.ex.fail_next("position", RateLimited("429"))
    h.commands.put(Command("/flatten", ENTRY_BAR + 45))
    h.commands.put(Command("/flatten_confirm", ENTRY_BAR + 48))
    h.at(ENTRY_BAR + 50)
    assert any("/flatten_confirm 실패" in t for k, t in h.alerts.items if k == "COMMAND")
    h.commands.put(Command("/flatten", ENTRY_BAR + 55))
    h.commands.put(Command("/flatten_confirm", ENTRY_BAR + 58))
    h.at(ENTRY_BAR + 60)
    assert h.ex.position() is None


def test_old_commands_are_ignored_and_the_confirmation_is_timed_on_the_messages(h):
    entered(h)
    h.commands.put(Command("/flatten", ENTRY_BAR - 570))  # sent while the runtime was down, read 10 minutes later
    h.commands.put(Command("/flatten_confirm", ENTRY_BAR + 30))
    h.at(ENTRY_BAR + 40)
    replies = [t for k, t in h.alerts.items if k == "COMMAND"]
    assert replies[0].startswith("오래된 명령 무시: /flatten (610초 전에 보냄")
    assert replies[1].startswith("확인할 /flatten 요청이 없음") and h.ex.position() is not None
    h.commands.put(Command("/flatten", ENTRY_BAR + 45))  # sent 61 s apart, read together
    h.commands.put(Command("/flatten_confirm", ENTRY_BAR + 106))
    h.at(ENTRY_BAR + 110)
    assert h.ex.position() is not None and paused(h.journal) is None
    h.commands.put(Command("/flatten", ENTRY_BAR + 120))  # sent 50 s apart, read 100 s after the /flatten
    h.commands.put(Command("/flatten_confirm", ENTRY_BAR + 170))
    h.at(ENTRY_BAR + 220)
    assert h.ex.position() is None and paused(h.journal)["reason"] == "user /flatten"
    assert [e["data"]["cmd"] for e in h.journal.events(kind="COMMAND_STALE")] == ["/flatten"]


def test_the_stop_gap_watchdog_closes_after_ten_seconds(h):
    entered(h)
    seen = alerts_with_positions(h)
    h.price(25_000.0)  # far below the stop's 5% limit (25_255.75): the bracket cannot fill
    assert h.ex.position() is not None
    for k in range(1, 12):
        h.at(ENTRY_BAR + 40 + k)
    assert h.ex.position() is None and [(k, p) for k, _, p in seen if k == "STOP_GAP"] == [("STOP_GAP", None)]  # closed first
    assert h.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "STOP_GAP"


def test_an_unprotected_position_through_its_stop_is_closed_at_once(h):
    h.ex.fail_next("place_bracket", ExchangeError("INVALID_STOP_PRICE"), times=5)  # the venue refuses the stop
    h.at(ENTRY_BAR + 30)
    assert h.ex.position() is not None and h.ex.open_orders() == []
    h.at(ENTRY_BAR + 40)  # the poll observes the gap in protection
    assert h.rt.executor.trade().unprotected_since == ENTRY_BAR + 40
    seen = alerts_with_positions(h)
    h.price(26_580.0)  # through the 26_585 stop, inside its 5% limit: a resting stop would have filled
    h.at(ENTRY_BAR + 51)  # the next REST price (no WebSocket feed here: polled every 10 s)
    assert h.ex.position() is None and [p for k, _, p in seen if k == "STOP_UNPROTECTED"] == [None]  # closed first
    assert h.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "STOP_UNPROTECTED"


def test_bracket_fills_are_recorded_for_the_cost_report(h):
    entered(h)
    h.price(26_570.0)  # through the 26_585 stop, inside its 5% limit: the bracket's stop fills at 26_570
    h.at(ENTRY_BAR + 45)
    assert h.ex.position() is None
    fill = h.journal.events(kind="BRACKET_FILL")[0]["data"]
    assert (fill["leg"], fill["side"], fill["contracts"], fill["price"], fill["stop"]) == ("tp1", -1, 1, 26_570.0, 26_585.0)
    c = h.rt.costs(ENTRY_BAR)
    assert c["fills"] == 2 and c["stop_slippage_bps"] == pytest.approx(15.0 / 26_585.0 * 1e4)
    assert c["slippage_bps"] == pytest.approx((26_747.0 - 26_744.5) / 26_744.5 * 1e4)


def test_the_account_breaker_flattens_and_pauses(h):
    entered(h)  # the day's first balance read
    h.at(ENTRY_BAR + 90)
    h.at(ENTRY_BAR + 150)  # three reads: the day's reference (their median)
    h.ex.cash -= 400.0  # equity 10_000 -> ~9_600: the daily -3% breaker
    seen = alerts_with_positions(h)
    h.at(ENTRY_BAR + 210)
    assert h.ex.position() is not None and paused(h.journal) is None  # one breaching balance read is not enough
    h.at(ENTRY_BAR + 270)  # the next minute's read breaches again
    assert h.ex.position() is None and paused(h.journal)["reason"] == "breaker DAILY_LOSS"
    assert [p for k, _, p in seen if k == "BREAKER"] == [None]  # flattened before the alert
    assert h.journal.events(kind="INCIDENT")[-1]["data"]["kind"] == "BREAKER"
    peak = h.rt.guard.state()["peak"]
    assert h.rt.handle("/resume", ENTRY_BAR + 280).endswith("(지금과 다음 두 번 조회의 중앙값): DAILY_LOSS")
    g = h.rt.guard.state()
    assert not g["daily_tripped"] and g["day_ref"] == h.ex.balance().equity and g["peak"] == peak  # the peak stays


def test_a_balance_that_is_not_a_finite_amount_never_trips_and_pauses_entries_after_three_reads(h):
    entered(h)
    guard = h.rt.guard.state()
    h.ex.balance = lambda: Balance(math.nan, None)  # a broken balance read
    h.at(ENTRY_BAR + 95)
    h.at(ENTRY_BAR + 155)
    assert h.alerts.kinds().count("BAD_EQUITY") == 1 and paused(h.journal) is None  # alerted once, not an incident yet
    h.at(ENTRY_BAR + 215)  # the third in a row: an incident that pauses entries - never a flatten
    assert paused(h.journal)["reason"] == "incident BAD_EQUITY"
    assert h.journal.events(kind="INCIDENT")[-1]["data"]["kind"] == "BAD_EQUITY"
    assert h.ex.position() is not None and len(h.ex.open_orders()) == 1  # still protected
    assert "BREAKER" not in h.alerts.kinds() and h.rt.guard.state() == guard
    h.at(ENTRY_BAR + 275)
    assert h.alerts.kinds().count("INCIDENT") == 1  # once per episode
    del h.ex.balance  # good reads again
    h.at(ENTRY_BAR + 335)
    assert h.alerts.kinds().count("BAD_EQUITY") == 1 and h.rt.guard.state()["equity"] > 0
    assert paused(h.journal) is not None  # resuming is the user's decision (/resume)


def test_contracts_added_by_hand_are_reduced_and_pause_canary(h):
    entered(h)
    h.ex.place_market("manual-1", 1, 1)  # a manual buy: 2 contracts, the CANARY trade holds 1
    h.at(ENTRY_BAR + 45)  # the next poll
    assert h.ex.position().contracts == 1 and h.rt.executor.trade().contracts == 1
    assert [e["data"]["reason"] for e in h.journal.events(kind="REDUCED")] == ["EXCESS"]
    assert "EXCESS_POSITION" in h.alerts.kinds() and paused(h.journal)["reason"] == "incident EXCESS_POSITION"


def test_a_position_flipped_or_opened_by_hand_is_closed_and_pauses_canary(h):
    entered(h)  # long 1
    h.ex.place_market("manual-1", -1, 2)  # sold 2 by hand: the trade's position is now short
    h.at(ENTRY_BAR + 45)
    assert h.ex.position() is None and paused(h.journal)["reason"] == "incident DIRECTION_MISMATCH"
    resume(h.journal, ENTRY_BAR + 50)
    h.ex.place_market("manual-2", -1, 1)  # a short the engine does not hold: closed, not adopted
    h.at(ENTRY_BAR + 110)
    assert h.ex.position() is None and paused(h.journal)["reason"] == "incident UNKNOWN_POSITION"
    assert [e["data"]["kind"] for e in h.journal.events(kind="INCIDENT")] == ["DIRECTION_MISMATCH", "UNKNOWN_POSITION"]


def test_an_unexplained_reduction_keeps_the_stop_and_protects_the_rest(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=10)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    assert hs.ex.position().contracts == 3
    hs.ex.close_position("manual-x", 1)  # a manual partial close: the TP1 leg still rests
    hs.at(ENTRY_BAR + 45)
    tr = hs.rt.executor.trade()
    assert not tr.tp1_filled and (tr.contracts, tr.stop_price) == (2, 26_585.0)
    assert sorted((o.size, o.limit_price, o.stop_price) for o in hs.ex.open_orders()) == [
        (1, 27_245.0, 26_585.0), (1, 27_610.0, 26_585.0)]
    assert "UNEXPLAINED_REDUCTION" in hs.alerts.kinds()
    hs.journal.close()


def test_margin_warns_then_reduces(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", equity=400.0)
    hs.price(26_744.5)
    hs.rt.startup()
    hs.rt.guard.params = dataclasses.replace(hs.rt.guard.params, daily_loss=1.0, total_dd=1.0)  # tested on their own
    hs.ex.place_market("ewb-e-1-L", 1, 3)  # 3 contracts: overnight margin 168.7 USD, 42% of 400
    hs.rt.executor.adopt(position(signal_ts=SIGNAL, stop=26_000.0, tp1=27_500.0, tp2=28_000.0), hs.rt._ctx(hs.clock()))
    hs.ex.cash = 200.0  # equity ~200: 84% -> warn
    hs.at(ENTRY_BAR + 100)
    assert "MARGIN" in hs.alerts.kinds() and hs.ex.position().contracts == 3
    hs.ex.cash = 170.0  # ~99% -> reduce to <= 70%
    seen = alerts_with_positions(hs)
    hs.at(ENTRY_BAR + 170)
    assert hs.ex.position().contracts == 2 and hs.rt.executor.trade().contracts == 2
    assert [e["data"]["reason"] for e in hs.journal.events(kind="REDUCED")] == ["MARGIN"]
    assert [p.contracts for k, t, p in seen if t.startswith("[증거금 축소]")] == [2]  # reduced before the alert
    hs.journal.close()


def test_a_feed_without_futures_prices_is_alerted_once_per_outage(h):
    feed = TickerFeed([h.ex.futures_id], ws_factory=lambda **kw: None, clock=h.clock)
    h.rt = h.make_runtime(feed=feed)
    h.rt.startup()
    beat = json.dumps({"channel": "heartbeats"})
    tick = json.dumps({"channel": "ticker", "events": [{"tickers": [{"product_id": h.ex.futures_id, "price": "26744.5"}]}]})
    feed.on_message(tick)  # the last futures price, at +30
    feed.failures = 3  # TickerFeed.run's count of failed connections so far
    h.ex.set_session("CLOSE")
    for k in range(8):  # a closed venue has no trades: eight minutes without a futures price are no outage
        feed.on_message(beat)
        h.at(ENTRY_BAR + 30 + 60 * k)
    assert "FEED" not in h.alerts.kinds()
    h.ex.set_session("OPEN")
    for k in range(6):  # open, heartbeats fresh, no futures price: counted from +510, alerted at +810
        if k == 1:
            feed.on_message(json.dumps({"type": "error", "message": "failure to subscribe"}))  # the ticker refused
        if k == 3:
            feed.failures += 2
        feed.on_message(beat)
        h.at(ENTRY_BAR + 510 + 60 * k)
    texts = [t for kind, t in h.alerts.items if kind == "FEED"]
    assert len(texts) == 1 and "REST" in texts[0] and "재연결 실패 2회" in texts[0]
    assert "server error: failure to subscribe" in texts[0]
    h.ex.set_session("CLOSE")  # the venue closes during the outage: still the same outage
    h.at(ENTRY_BAR + 870)
    h.at(ENTRY_BAR + 930)
    feed.on_message(tick)  # futures prices again
    h.at(ENTRY_BAR + 990)
    assert [t for kind, t in h.alerts.items if kind == "FEED"][-1] == "[가격 피드 복구] 웹소켓 선물 가격 수신 재개"
    assert h.alerts.kinds().count("FEED") == 2 and [e["kind"] for e in h.journal.events()
                                                    if e["kind"].startswith("FEED")] == ["FEED_DOWN", "FEED_UP"]
    assert h.journal.events(kind="FEED_UP")[-1]["data"]["down_s"] == 480.0  # since +510, the close included


class FakeControl:
    """The attributes of live.control.TelegramControl the runtime watches."""

    def __init__(self) -> None:
        self.alive, self.failures, self.conflicts, self.last_error, self.starts = True, 0, 0, None, 0
        self.late = 0.0

    def start(self, commands: Any) -> None:
        self.starts += 1
        self.alive = True

    def overdue(self, now: float) -> float:
        return self.late


class Pings:
    """healthchecks.io: GET pings answer `status`; POSTs (/fail) are recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.status = 200

    def get(self, url: str, timeout: float | None = None) -> Any:
        self.calls.append(("GET", url, None))
        return SimpleNamespace(status_code=self.status)

    def post(self, url: str, data: Any = None, timeout: float | None = None) -> Any:
        self.calls.append(("POST", url, data))
        return SimpleNamespace(status_code=200)


def test_the_telegram_thread_is_watched_and_reported_out_of_band(h):
    control, pings = FakeControl(), Pings()
    h.rt = h.make_runtime(control=control, heartbeat=Heartbeat("https://hc-ping.com/check", session=pings, clock=h.clock))
    t0 = ENTRY_BAR + 100

    def minute(k: int) -> list[str]:
        h.clock.now = t0 + 60 * k
        h.rt.minute(h.clock.now)
        return [x for kind, x in h.alerts.items if kind == "TELEGRAM"]

    assert minute(0) == [] and pings.calls == [("GET", "https://hc-ping.com/check", None)]
    control.alive = False  # the thread died
    assert minute(1)[-1].startswith("[텔레그램 명령 수신 이상] 명령 수신 스레드가 멈춰 다시 시작함") and control.starts == 1
    assert minute(2)[-1] == "[텔레그램 복구] 명령 수신 정상"  # it runs again
    control.failures, control.last_error = 5, "getUpdates failed: HTTP 401: Unauthorized (the bot token was refused)"
    for k in range(3, 15):  # twelve minutes of failed polls: one alert; at 10 minutes one /fail, then no success ping
        texts = minute(k)
    assert len(texts) == 3 and "getUpdates 5회 연속 실패" in texts[-1] and "HTTP 401" in texts[-1]
    posts = [c for c in pings.calls if c[0] == "POST"]
    assert len(posts) == 1 and posts[0][1] == "https://hc-ping.com/check/fail" and "HTTP 401" in posts[0][2].decode()
    assert pings.calls[-1][0] == "POST" and len(pings.calls) == 14  # pings at 0-12, the /fail at 13, nothing at 14
    control.failures = 0
    assert minute(15)[-1] == "[텔레그램 복구] 명령 수신 정상" and pings.calls[-1][0] == "GET"
    control.conflicts = 2  # another getUpdates poller on this bot token
    assert "HTTP 409" in minute(16)[-1]
    pings.status = 404  # the check's URL is wrong: three failed pings are alerted once (on Telegram)
    for k in range(17, 21):
        minute(k)
    assert [x for kind, x in h.alerts.items if kind == "HEARTBEAT"] == [
        "[외부 감시 핑 실패] healthchecks.io 핑이 3회 연속 실패 (HTTP 404): VPS가 멈춰도 경보가 오지 않을 수 있음"]
    pings.status = 200
    minute(21)
    assert h.alerts.items[-1] == ("HEARTBEAT", "[외부 감시 핑 복구] healthchecks.io 핑 정상")
    control.late = 130.0  # a request hangs: reported, not replaced (a second thread would poll beside it)
    assert minute(22)[-1].startswith("[텔레그램 명령 수신 이상] 명령 수신 스레드가 2분 넘게 응답 없음") and control.starts == 1
    assert [e["kind"] for e in h.journal.events() if e["kind"].startswith("TELEGRAM")] == [
        "TELEGRAM_DOWN", "TELEGRAM_UP", "TELEGRAM_DOWN", "TELEGRAM_UNUSABLE", "TELEGRAM_UP", "TELEGRAM_DOWN", "TELEGRAM_UP",
        "TELEGRAM_DOWN"]


def test_unusable_telegram_withholds_pings_at_most_30_minutes_and_fails_again_every_3_hours(h):
    control, pings = FakeControl(), Pings()
    h.rt = h.make_runtime(control=control, heartbeat=Heartbeat("https://hc-ping.com/check", session=pings, clock=h.clock))
    control.failures, control.last_error = 5, "getUpdates failed: ConnectionError"  # Telegram out for four hours
    t0 = ENTRY_BAR + 100
    calls: dict[int, str] = {}
    for k in range(240):
        h.clock.now = t0 + 60 * k
        before = len(pings.calls)
        h.rt.minute(h.clock.now)
        calls[k] = "".join(c[0][0] for c in pings.calls[before:])  # G: a ping, P: a /fail
    assert [k for k, c in calls.items() if "P" in c] == [10, 190]  # after 10 minutes, then every 3 hours
    assert [k for k, c in calls.items() if "G" not in c] == list(range(10, 40)) + list(range(190, 220))  # 30 min each
    assert len([x for kind, x in h.alerts.items if kind == "TELEGRAM"]) == 1  # one episode


def test_a_notifier_that_cannot_deliver_counts_as_telegram_unusable(h):
    pings = Pings()
    h.rt = h.make_runtime(heartbeat=Heartbeat("https://hc-ping.com/check", session=pings, clock=h.clock))
    state: dict[str, Exception | None] = {"error": ConnectionError("telegram down")}

    def sender(text: str) -> None:
        if state["error"] is not None:
            raise state["error"]

    t0 = ENTRY_BAR + 100
    h.clock.now = t0 - 60
    h.rt.minute(h.clock.now)  # the day's summary and the first ping, before the notifier under test
    n = LiveNotifier(sender, clock=h.clock)  # its worker's steps are taken here (pump)
    h.rt.d.notify = n

    def minute(k: int, pump: bool = True) -> str:
        h.clock.now = t0 + 60 * k
        if pump:
            n.pump()
        before = len(pings.calls)
        h.rt.minute(h.clock.now)
        return "".join(c[0][0] for c in pings.calls[before:])

    h.clock.now = t0
    n("X", "queued while Telegram is down")
    assert [minute(k) for k in range(12)] == ["G"] * 10 + ["P", ""]  # failing for 10 minutes: /fail, pings withheld
    state["error"] = None  # Telegram is back: the message goes out, and so do the pings
    assert minute(12) == "G" and n.pending == 0
    n("Y", "first alert after the outage")  # waiting, not tried yet: no /fail
    assert minute(13, pump=False) == "G"
    assert [minute(k, pump=False) for k in range(14, 24)] == ["G"] * 8 + ["P", ""]  # 10 min waiting, no attempt: stuck
    assert minute(24) == "G" and n.pending == 0  # the sender runs again
    state["error"] = SendError('telegram HTTP 403: {"ok":false,"description":"Forbidden: bot was blocked by the user"}')
    for i in range(3):
        n("X", f"refused {i}")
        n.pump()
    assert minute(25, pump=False) == "P"  # three refused in a row: unusable at once
    reasons = [e["data"]["reasons"][0] for e in h.journal.events(kind="TELEGRAM_UNUSABLE")]
    assert "알림 전송이 10분째 실패" in reasons[0] and "멈춤" in reasons[1] and "3건을 연속 거부" in reasons[2]


def test_a_broken_telegram_check_never_skips_the_minute_and_loop_errors_are_redacted(h, capsys):
    class Broken(FakeControl):
        @property  # type: ignore[override]
        def alive(self) -> bool:
            raise RuntimeError("control state unreadable")

        @alive.setter
        def alive(self, value: bool) -> None:
            pass

    h.rt = h.make_runtime(control=Broken(), redact=lambda text: text.replace("SECRET", "<secret>"))
    h.ex.cash -= 100.0
    h.rt.minute(ENTRY_BAR + 100)
    assert h.journal.events(kind="LOOP_ERROR")[-1]["data"] == {"section": "telegram", "error": "RuntimeError"}
    assert h.rt.guard.state()["equity"] == h.ex.balance().equity  # the breakers' read still ran
    capsys.readouterr()

    def failing_poll(now: float) -> None:
        raise RuntimeError("https://api.telegram.org/botSECRET/getUpdates refused")

    h.rt.poll = failing_poll  # type: ignore[method-assign]
    h.rt.last_poll = None
    h.rt.tick()
    err = capsys.readouterr().err
    assert "Traceback" in err and "bot<secret>" in err and "SECRET" not in err
    assert h.journal.events(kind="LOOP_ERROR")[-1]["data"]["error"] == (
        "RuntimeError: https://api.telegram.org/bot<secret>/getUpdates refused")
    assert "SECRET" not in [t for k, t in h.alerts.items if k == "LOOP_ERROR"][-1]


def test_a_failed_read_at_the_overnight_margin_check_is_retried_not_lost(h):
    entered(h)
    t = ts("2024-03-14 19:31")  # 15:31 New York (EDT)
    h.ex.fail_next("position", RateLimited("429"), times=2)  # the margin check's position read, then the report's
    h.rt.minute(t)
    assert [x for k, x in h.alerts.items if k == "MARGIN_1530"] == [
        "[15:30 증거금 점검] 조회 실패: 429 (16시 전까지 매분 다시 시도)"]
    h.rt.minute(t + 60)  # still before 16:00: read again, reported
    h.rt.minute(t + 120)
    texts = [x for k, x in h.alerts.items if k == "MARGIN_1530"]
    assert len(texts) == 2 and texts[1].startswith("[15:30 증거금 점검] 16시 오버나이트 기준 예상")
    h.ex.fail_next("balance", RateLimited("429"), times=2)  # the next day: the breaker's balance read, the report's
    h.rt.minute(t + 86_400)
    h.rt.minute(t + 86_460)
    texts = [x for k, x in h.alerts.items if k == "MARGIN_1530"]
    assert texts[2:] == ["[15:30 증거금 점검] 조회 실패: 429 (16시 전까지 매분 다시 시도)", texts[1]]


def test_margin_is_never_acted_on_without_a_futures_price(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", equity=400.0)
    hs.price(26_744.5)
    hs.rt.startup()
    hs.rt.guard.params = dataclasses.replace(hs.rt.guard.params, daily_loss=1.0, total_dd=1.0)  # tested on their own
    hs.ex.place_market("ewb-e-1-L", 1, 3)
    hs.rt.executor.adopt(position(signal_ts=SIGNAL, stop=26_000.0, tp1=27_500.0, tp2=28_000.0), hs.rt._ctx(hs.clock()))
    hs.ex.cash = 170.0  # ~99% of the account at the overnight rate: a reduce, if there were a price
    hs.rt.prices.futures_price = lambda: None  # neither a WebSocket nor a REST price within 60 s
    hs.at(ENTRY_BAR + 100)
    hs.at(ENTRY_BAR + 160)
    assert hs.ex.position().contracts == 3 and not hs.journal.events(kind="REDUCED")  # never on a stand-in price
    assert [t for k, t in hs.alerts.items if k == "MARGIN"] == [
        "[증거금 점검: 가격 없음] 선물 가격을 읽지 못해 증거금 점검을 건너뜀 (가격이 돌아오면 다시 점검)"]
    hs.ex.position = lambda: Position(1, 3, 0.0)  # an entry price the adapter read as 0: reports must not use it
    hs.rt._overnight_report(ENTRY_BAR + 170)
    hs.rt.daily(ENTRY_BAR + 175)
    assert hs.journal.events(kind="MARGIN_1530")[-1]["data"]["ratio"] is None
    assert "오버나이트 증거금: 가격 또는 잔고 없음" in [t for k, t in hs.alerts.items if k == "DAILY"][-1]
    del hs.ex.position
    del hs.rt.prices.futures_price  # prices are back
    hs.at(ENTRY_BAR + 220)
    assert hs.ex.position().contracts == 2 and [e["data"]["reason"] for e in hs.journal.events(kind="REDUCED")] == ["MARGIN"]
    hs.journal.close()


def test_a_stage_change_restarts_the_runtime_once_flat(h):
    entered(h)
    h.journal.set_stage("SHADOW", "demotion", ENTRY_BAR + 35)
    h.at(ENTRY_BAR + 100)
    assert h.rt.exit_code is None  # a position is open
    h.rt.handle("/flatten", ENTRY_BAR + 110)
    h.rt.handle("/flatten_confirm", ENTRY_BAR + 111)
    h.at(ENTRY_BAR + 170)
    assert h.rt.exit_code == 0 and "STAGE" in h.alerts.kinds()


def test_daily_summary_audit_and_status(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    led = paper_ledger(SIGNAL, [d for d in hs.journal.decisions() if d["ts"] < SIGNAL])
    hs.rt.d.paper_ledger = lambda: led
    res = hs.rt.run_audit(ENTRY_BAR + 40)
    assert res.ok and res.compared > 0 and audit_from_journal(hs.journal).ok
    bad = dict(led, decisions=led["decisions"][:-1])  # the paper ledger misses a decision the live runtime made
    hs.rt.d.paper_ledger = lambda: bad
    seen = noting(hs, lambda: paused(hs.journal))
    assert not hs.rt.run_audit(ENTRY_BAR + 50).ok
    assert "AUDIT_PENDING" in hs.alerts.kinds() and paused(hs.journal) is None  # a lag might explain it: pending
    hs.rt.run_audit(ENTRY_BAR + 3650)  # the next audit still finds it: a major incident
    assert "AUDIT_MISMATCH" in hs.alerts.kinds() and paused(hs.journal)["reason"] == "incident AUDIT_MISMATCH"
    assert [p is not None for k, _, p in seen if k == "AUDIT_MISMATCH"] == [True]  # paused before the alert
    hs.rt.daily(ENTRY_BAR + 60)
    daily = [t for k, t in hs.alerts.items if k == "DAILY"][-1]
    assert "일일 요약" in daily and "포지션: W3" not in daily and "체결 24시간: 1건" in daily
    assert hs.journal.events(kind="COSTS")[-1]["data"]["fills"] == 1
    lines = status_lines(hs.journal, ENTRY_BAR + 70)
    assert lines[0].startswith("단계 CANARY") and lines[1].startswith("포지션: C_END 롱 1계약")
    assert any(x.startswith("모의매매 대조: 비교 ") and "불일치 1건" in x for x in lines)
    hs.journal.close()


def test_the_audit_stops_at_the_last_decided_bar_and_never_passes_on_a_failed_one(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)  # the cycle of bar SIGNAL
    hs.fetch.fail = 10
    hs.at(ENTRY_BAR + 3630)  # the cycle of bar ENTRY_BAR fails (no candles): not decided yet
    assert [c["status"] for c in hs.journal.cycles()][-2:] == ["OK", "ERROR"]
    target = compute_target(hs.lock, hs.df[hs.df["ts"] <= ENTRY_BAR], ENTRY_BAR)  # the paper runtime has the bar
    led = paper_ledger(ENTRY_BAR, [d for d in target.decisions if known_at(d) <= ENTRY_BAR])
    lagging = audit(hs.journal.decisions(), led, now=ENTRY_BAR + 3640)  # without the live bound
    assert [m["type"] for m in lagging.mismatches] == ["MISSING_IN_LIVE"]
    hs.rt.d.paper_ledger = lambda: led
    res = hs.rt.run_audit(ENTRY_BAR + 3650)
    assert res.ok and res.window[1] == SIGNAL and "AUDIT_PENDING" not in hs.alerts.kinds()
    # a bar Coinbase revised between the two fetches is alerted, never an incident; a different decision on the
    # same candles is a major incident at once (no lag explains it)
    d = [x for x in hs.journal.decisions() if known_at(x) <= SIGNAL][-1]
    before = data_fingerprint(hs.rt.frame, 0, d["ts"] - 3600)
    runs = [{"id": "r1", "status": "ok", "last_bar_ts": d["ts"] - 3600, "data_sha256": before}]
    revised = {k: v for k, v in d.items() if k != "recorded_ts"} | {"price": d["price"] + 5.0, "data_sha256": "other"}
    hs.rt.d.paper_ledger = lambda: paper_ledger(SIGNAL, [x for x in led["decisions"] if x["id"] != d["id"]] + [revised],
                                                runs)
    res = hs.rt.run_audit(ENTRY_BAR + 3660)
    assert [(m["type"], m["cause"]) for m in res.mismatches] == [("DIFFERENT", "DATA_REVISION")]
    assert "AUDIT_REVISION" in hs.alerts.kinds() and paused(hs.journal) is None
    same = revised | {"data_sha256": data_fingerprint(hs.rt.frame, 0, d["ts"])}
    hs.rt.d.paper_ledger = lambda: paper_ledger(SIGNAL, [x for x in led["decisions"] if x["id"] != d["id"]] + [same],
                                                runs)
    hs.rt.run_audit(ENTRY_BAR + 3670)
    assert paused(hs.journal)["reason"] == "incident AUDIT_MISMATCH"
    # a failed audit is stored as failed (the gates never pass on the last good one) and alerted once per episode
    hs.rt.d.paper_ledger = lambda: {"schema_version": 2}
    assert hs.rt.run_audit(ENTRY_BAR + 3680).error.startswith("paper ledger invalid")

    def down() -> dict[str, Any]:
        raise AuditError("paper ledger fetch failed: HTTP 502")

    hs.rt.d.paper_ledger = down
    stored = hs.rt.run_audit(ENTRY_BAR + 3690)
    assert audit_from_journal(hs.journal) == stored and not stored.ok
    assert any(x.startswith("모의매매 대조: 실패 - paper ledger fetch failed: HTTP 502")
               for x in status_lines(hs.journal, ENTRY_BAR + 3700))
    hs.rt.d.paper_ledger = lambda: led
    hs.rt.run_audit(ENTRY_BAR + 3700)
    assert [t.split("]")[0] for k, t in hs.alerts.items if k == "AUDIT"] == ["[모의매매 대조 실패", "[모의매매 대조 복구"]
    hs.journal.close()


def test_a_demotion_applies_to_the_next_entry_while_the_process_still_runs(tmp_path):
    sizes = {}
    for demoted in (False, True):
        hs = Harness(tmp_path / str(demoted), ENTRY_BAR + 30, stage="FULL", stage_days=40)  # the cap is released
        hs.price(float(hs.bar(ENTRY_BAR)["open"]))
        hs.rt.startup()
        if demoted:  # a major incident in FULL, then the user's /resume: this process still runs FULL
            assert on_incident(hs.journal, "UNPROTECTED_60S", ENTRY_BAR + 1) == ["paused", "demoted to CANARY"]
            resume(hs.journal, ENTRY_BAR + 2)
            assert hs.rt.stage == "FULL" and hs.rt._ctx(ENTRY_BAR + 3).max_contracts == 1
        hs.at(ENTRY_BAR + 30)
        sizes[demoted] = hs.ex.position().contracts
        hs.journal.close()
    assert sizes[False] > 3 and sizes[True] == 1  # CANARY's one contract at once, not FULL's uncapped size


def test_an_order_failure_is_a_canary_failure_only_when_not_resolved_in_time(h):
    trail = ts("2024-03-14 07:00")  # the engine trails the long's stop at this close
    h.walk(ENTRY_BAR, trail + 3600)
    h.price(float(h.bar(trail + 3600)["open"]))
    h.ex.fail_next("place_bracket", RateLimited("429"))  # the stop move's replacement: one 429
    t0 = trail + 3600 + 30
    h.at(t0)
    h.at(t0 + 10)  # the next poll replaces it
    assert len(h.journal.events(kind="BRACKET_FAILED", since=t0)) == 1 and h.journal.events(kind="STOP_MOVED", since=t0)
    assert [e["data"]["kind"] for e in h.journal.events(kind="ORDER_RESOLVED")] == ["BRACKET_FAILED"]
    assert paused(h.journal) is None and not h.journal.events(kind="ORDER_UNRESOLVED")
    assert {g.name: g.met for g in canary_gates(h.journal, t0 + 20, None)}["② 손절 이동과 청산이 목표와 일치"]
    seen = noting(h, lambda: paused(h.journal))
    h.ex.fail_next("place_bracket", RateLimited("429"), times=50)
    h.ex.cancel([o.order_id for o in h.ex.open_orders()])  # the bracket cancelled by hand, and no new one goes out
    for k in range(2, 6):
        h.at(t0 + 10 * k)
        assert paused(h.journal) is None  # within 30 s of the failure: still being retried
    h.at(t0 + 60)
    assert [e["data"]["kind"] for e in h.journal.events(kind="ORDER_UNRESOLVED")] == ["BRACKET_FAILED"]
    assert paused(h.journal)["reason"] == "incident ORDER_UNRESOLVED"
    assert [p is not None for k, _, p in seen if k == "ORDER_UNRESOLVED"] == [True]  # paused before the alert
    assert not {g.name: g.met for g in canary_gates(h.journal, t0 + 70, None)}["② 손절 이동과 청산이 목표와 일치"]


def test_expiry_reminders_and_a_transfer_key_incident(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, permissions=KeyPermissions(True, True, True))
    hs.price(26_700.0)
    hs.rt.startup()
    hs.rt.d.expiry = {"coinbase": "2024-04-01", "github": "2025-01-01"}
    seen = noting(hs, lambda: paused(hs.journal))
    hs.rt.daily(ENTRY_BAR + 60)
    texts = [t for k, t in hs.alerts.items if k == "EXPIRY"]
    assert len(texts) == 1 and "coinbase" in texts[0]
    assert [p is not None for k, _, p in seen if k == "TRANSFER_KEY"] == [True]  # paused before the alert
    hs.journal.close()


def test_the_shadow_exchange_previews_every_order_and_trades_virtually(tmp_path):
    clock = Clock(1_000.0)
    real = SimExchange(clock=clock)
    real.set_prices(60_000.0, 60_010.0)
    jr = journal(tmp_path)
    alerts = []
    sh = ShadowExchange(real, jr, clock=clock, alert=lambda k, t: alerts.append(k))
    with pytest.raises(ValueError):
        sh.fail_next("quote", RateLimited("429"))  # a real read: inject it on `real`
    assert sh.place_market("ewb-e-1-L", 1, 1).ok
    assert sh.place_bracket("ewb-b-1-tp1-1", -1, 1, 61_000.0, 59_000.0).ok
    real.preview_errors = ("PREVIEW_INSUFFICIENT_FUND",)
    real.set_prices(60_500.0, 60_510.0)
    assert sh.close_position("ewb-x-1-1").ok
    previews = [e["data"] for e in jr.events(kind="PREVIEW")]
    assert [(p["kind"], p["ok"]) for p in previews] == [("MARKET", True), ("BRACKET", True), ("CLOSE", False)]
    assert alerts == ["PREVIEW_FAILED"] and real.orders == {} and real.position() is None
    assert sh.fills[0]["price"] == 60_002.5 and sh.fills[-1]["price"] == 60_497.5  # real bid / ask
    assert sh.balance().equity == real.balance().equity and sh.session().is_open
    jr.close()


def test_shadow_runs_the_whole_loop_without_a_real_order(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="SHADOW")
    real = hs.ex  # stands in for the Coinbase account
    hs.ex = ShadowExchange(real, hs.journal, clock=hs.clock)
    hs.rt = hs.make_runtime()
    opening = float(hs.bar(ENTRY_BAR)["open"])
    real.set_prices(opening, opening)
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    assert real.orders == {} and real.position() is None
    assert hs.ex.position().contracts == 1 and len(hs.ex.open_orders()) == 1
    assert [e["data"]["kind"] for e in hs.journal.events(kind="PREVIEW")] == ["MARKET", "BRACKET"]
    real.set_prices(27_300.0, 27_300.0)  # through the virtual TP1 (27_245)
    hs.at(ENTRY_BAR + 45)
    assert hs.ex.position() is None and hs.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "BRACKET"
    hs.journal.close()


def test_a_shadow_restart_keeps_the_virtual_position(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="SHADOW")
    real = hs.ex
    hs.ex = ShadowExchange(real, hs.journal, clock=hs.clock)
    hs.rt = hs.make_runtime()
    opening = float(hs.bar(ENTRY_BAR)["open"])
    real.set_prices(opening, opening)
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    assert hs.ex.position().contracts == 1
    hs.ex = ShadowExchange(real, hs.journal, clock=hs.clock)  # restart: a new, empty virtual book
    hs.rt = hs.make_runtime()
    hs.clock.now = ENTRY_BAR + 60
    hs.rt.startup()
    assert hs.ex.position().contracts == 1 and len(hs.ex.open_orders()) == 1
    assert not hs.journal.events(kind="CLOSED") and len(hs.journal.events(kind="RECOVERY")) == 2
    hs.journal.close()


class RacyExchange(SimExchange):
    """A fill lands between the runtime's position read and its order read (once, when armed)."""

    race: Callable[[], Any] | None = None

    def open_orders(self) -> list[Order]:
        race, self.race = self.race, None
        if race is not None:
            race()
        return super().open_orders()


def test_a_fill_between_the_position_and_the_order_read_is_read_again(tmp_path):
    """Probe (b): the TP1 leg fills after the poll read the position and before it read the orders. The runtime
    reads the position again after the orders and repeats the pair until both agree, so a stale position is
    never paired with a vanished leg (that would place the TP1 limit again below the market, then sell the TP2
    contracts too): the fill is the live TP1 at once."""
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=10)
    hs.ex = RacyExchange(clock=hs.clock)
    hs.rt = hs.make_runtime()
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    assert hs.ex.position().contracts == 3  # TP1 leg 1 @ 27_245, TP2 leg 2 @ 27_610
    hs.ex.race = lambda: hs.ex.set_prices(27_300.0)  # through the TP1 limit only
    hs.at(ENTRY_BAR + 45)  # the next poll
    tr = hs.rt.executor.trade()
    assert hs.ex.position().contracts == 2 and tr.tp1_filled and hs.ex.calls.count("place_bracket") == 3
    assert [(o.size, o.limit_price, o.stop_price) for o in hs.ex.open_orders()] == [(2, 27_610.0, 26_745.0)]
    assert "UNEXPLAINED_REDUCTION" not in hs.alerts.kinds() and "TP1_FILLED" in hs.alerts.kinds()
    hs.journal.close()


def test_an_ambiguous_tp1_sync_is_read_from_the_position_not_sold_again(tmp_path):
    """Probe (a) end to end: the engine's TP1 (spot) filled, the live TP1 limit did not; the market reduce that
    syncs it fills, but its answer is lost and the lookup fails. The next poll reads the smaller position as that
    reduce: one sale, no UNEXPLAINED_REDUCTION incident, the rest at break-even."""
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", stage_days=10)
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    hs.price(27_200.0)  # between the live entry (26_747) and the TP1 limit (27_245); the engine's TP1 has filled:
    t = hs.rt.target
    engine_tp1 = dataclasses.replace(pending_position(t.entry), tp1_done=True)
    hs.rt.target = dataclasses.replace(t, entry=None, position=engine_tp1)
    hs.ex.crash_after_accept("close_position", Unavailable("read timeout"))
    hs.ex.fail_next("find_order", Unavailable("503"))
    hs.at(ENTRY_BAR + 45)
    assert hs.ex.position().contracts == 2 and hs.rt.executor.trade().pending_reduce is not None
    hs.at(ENTRY_BAR + 55)
    tr = hs.rt.executor.trade()
    assert hs.ex.position().contracts == 2 and tr.tp1_filled and tr.pending_reduce is None
    assert [e["data"]["reason"] for e in hs.journal.events(kind="REDUCE_SETTLED")] == ["TP1_SYNC"]
    assert hs.ex.calls.count("close_position") == 1 and "UNEXPLAINED_REDUCTION" not in hs.alerts.kinds()
    assert [(o.size, o.limit_price, o.stop_price) for o in hs.ex.open_orders()] == [(2, 27_610.0, 26_745.0)]
    assert paused(hs.journal) is None
    hs.journal.close()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --group live pytest tests/live/test_runtime.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'ewbot.live.runtime'` (raised by `tests/live/harness.py`).

- [ ] **Step 3: Write minimal implementation**

**File: `src/ewbot/live/shadow.py`**

```python
"""SHADOW stage exchange (Plan 3 spec 4.3 `SHADOW`): no order ever reaches Coinbase.

Reads - product, session, quotes, balance, key permissions - come from the real exchange. Every order
the runtime would place is first checked with the real POST /orders/preview (journaled as a PREVIEW
event: SHADOW pass criterion ③) and then executed on the embedded SimExchange, whose book follows the
real futures price: market orders fill at the real bid/ask, brackets rest virtually and fill when the
real price reaches them. Position, open orders and order lookups come from that virtual book, so the
whole runtime - entries, protection, stop moves, closes, the watchdog - runs exactly as in CANARY. On a
restart `restore` puts the open virtual position back from the journal."""

from __future__ import annotations

import time
from typing import Any, Callable

from ewbot.live.exchange import (
    Balance,
    Exchange,
    ExchangeError,
    KeyPermissions,
    PlaceResult,
    Position,
    PreviewResult,
    ProductSpec,
    Quote,
    SessionInfo,
)
from ewbot.live.journal import Journal
from ewbot.live.reconcile import LiveTrade, expected_contracts
from ewbot.live.sim import SimExchange


class ShadowExchange(SimExchange):
    # these reads go to the real exchange (no _fault): inject their faults there, `real.fail_next(...)`
    FAULT_METHODS = SimExchange.FAULT_METHODS - {"product", "session", "quote", "balance", "key_permissions"}

    def __init__(self, real: Exchange, journal: Journal, *, clock: Callable[[], float] = time.time,
                 alert: Callable[[str, str], Any] | None = None) -> None:
        super().__init__(real.product(), equity=0.0, clock=clock, spot_id=real.spot_id)
        self.real = real
        self.journal = journal
        self.alert = alert or (lambda kind, text: None)

    def restore(self, trade: LiveTrade | None) -> None:
        """After a restart the virtual book is empty: put back the open virtual position from the journal's
        trade (its brackets are re-placed by the next reconcile), so a restart does not close it."""
        if trade is None or trade.status != "OPEN":
            return
        left = expected_contracts(trade)
        if left > 0:
            self.pos = Position(trade.direction, left, trade.entry_price)

    # ------------------------------------------------------------------ real reads
    def product(self) -> ProductSpec:
        return self.real.product()

    def session(self) -> SessionInfo:
        info = self.real.session()
        self.set_session(info.state)
        return info

    def quote(self, product_id: str) -> Quote:
        q = self.real.quote(product_id)
        if product_id == self.futures_id:
            self.spread = max(q.ask - q.bid, 0.0)
            self.set_prices(q.mid)
        elif product_id == self.spot_id:
            self.spot_price = q.mid
        return q

    def balance(self) -> Balance:
        return self.real.balance()  # sizing and the 2% check use the real account

    def key_permissions(self) -> KeyPermissions:
        return self.real.key_permissions()

    # ------------------------------------------------------------------ previewed virtual orders
    def _preview(self, kind: str, client_order_id: str, call: Callable[[], PreviewResult], **order: Any) -> None:
        try:
            res = call()
        except ExchangeError as exc:
            res = PreviewResult(False, (f"preview request failed: {exc}"[:200],))
        self.journal.add_event(self.clock(), "PREVIEW", {"kind": kind, "client_order_id": client_order_id,
                                                         "ok": res.ok, "errors": list(res.errors),
                                                         "warnings": list(res.warnings), **order})
        if not res.ok:
            self.alert("PREVIEW_FAILED", f"[SHADOW preview 실패] {kind} {client_order_id}: {', '.join(res.errors)}")

    def _refresh(self) -> None:
        self.quote(self.futures_id)

    def place_market(self, client_order_id: str, side: int, size: int) -> PlaceResult:
        self._preview("MARKET", client_order_id, lambda: self.real.preview_market(side, size), side=side, size=size)
        self._refresh()
        return super().place_market(client_order_id, side, size)

    def place_bracket(self, client_order_id: str, side: int, size: int, limit_price: float,
                      stop_price: float) -> PlaceResult:
        self._preview("BRACKET", client_order_id,
                      lambda: self.real.preview_bracket(side, size, limit_price, stop_price),
                      side=side, size=size, limit_price=limit_price, stop_price=stop_price)
        return super().place_bracket(client_order_id, side, size, limit_price, stop_price)

    def close_position(self, client_order_id: str, size: int | None = None) -> PlaceResult:
        pos = self.pos
        if pos is not None:
            qty = pos.contracts if size is None else min(int(size), pos.contracts)
            self._preview("CLOSE", client_order_id, lambda: self.real.preview_market(-pos.side, qty),
                          side=-pos.side, size=qty)
            self._refresh()
        return super().close_position(client_order_id, size)
```

**File: `src/ewbot/live/runtime.py`**

```python
"""The live runtime (Plan 3 spec 3.2 `scheduler`, 4.1, 5.1, 5.2): one process, one main loop, ticking
every second. Only the WebSocket feed, the Telegram long-poll and the Telegram sender run in their own
threads; they hand data over (prices, queued commands, queued alerts), and every trading decision is taken
on the main loop. An alert is only queued (live/notify.py sends it), so Telegram never delays the loop, and
every protective action - a stop-gap or unprotected close, a breaker flatten, a margin reduction, a pause -
is taken before its alert.

  commands  Telegram /status /pause /resume /flatten /flatten_confirm /help (live/control.py); a command sent
            more than 2 minutes before the loop reaches it is ignored with a reply (sent again if wanted),
            and /flatten_confirm counts only when sent within 60 s of its /flatten (the messages' dates)
  watch     every second while a trade is open: the stop-gap check on the futures price, and an
            immediate close when no stop rests (refused bracket) and the price is through the stop
  poll      every 10 s while a trade is open (60 s otherwise, at once after a WebSocket resync): a reduce
            with an unknown outcome is resolved by its client order id first; then position and open
            orders are re-read as one state (the position read again after the orders, repeated until
            both reads agree: a fill between the reads never pairs a stale position with a vanished
            leg), observed and reconciled against the last target (protection right after fills, the
            TP1 break-even, deferred closes). Every entry and adoption is sized by the more conservative of
            the stage this process runs in and the journal's (a demotion applies at once). After each pass an
            order failure is resolved or recorded unresolved (CANARY gate 2: a bracket not replaced within
            30 s, a close or reduction not done by the next pass - a CANARY failure that pauses)
  minute    journal heartbeat; the Telegram command thread (started again when it died; a dead thread, a
            hung one, 5 failed polls in a row or a conflict with another poller alerted once per episode)
            and the external heartbeat: a success ping while Telegram works - Telegram unusable (10 minutes
            of failures, a stuck sender, 3 refused messages in a row) sends healthchecks.io a /fail (its
            email reaches the user without Telegram) and withholds the pings for at most 30 minutes, the
            /fail repeated every 3 hours while it lasts; 3 failed pings in a row are alerted. A failure of
            this check never skips the rest of the minute. Then session state, account breakers (a balance
            that is not
            a finite amount above 0 is alerted, never a sample; three such reads in a row are an incident
            that pauses entries), margin (80% warn, 90% reduce; never acted on without a futures price), the
            WebSocket feed's health (no futures price for 5 minutes while the session is OPEN: alerted once
            per outage, and when it is back),
            pending intents resolved (or abandoned after 10 min), the 15:30 New York
            overnight-margin report (its reads retried every minute until 16:00), the daily summary (09:00 New York)
            with expiry reminders and the daily key-permission check, the paper audit (from minute 50, up
            to the last bar whose cycle ended OK: a revised bar is alerted only, a decision missing on one side
            is pending until the next audit confirms it, a different decision is a major incident at once; a
            failed audit is stored as failed and alerted), and the stage-change restart
  cycle     at hh:00:30 for the bar that just closed: candles, engine replay (target), decisions,
            reconcile. Retried every 15 s while the bar is missing; 2 minutes after the close the
            decision is held and only protection is kept (spec 4.1 step 1)."""

from __future__ import annotations

import queue
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ewbot.backtest.holdout import data_fingerprint
from ewbot.data.candles import fill_gaps, load_1h
from ewbot.execution.costs import fee as model_fee
from ewbot.live.audit import AuditError, AuditResult, audit
from ewbot.live.control import Command
from ewbot.live.exchange import STOP_LIMIT_OFFSET, Exchange, ExchangeError, Order, Position, ProductSpec, SessionInfo
from ewbot.live.executor import Executor
from ewbot.live.feed import usable_price
from ewbot.live.journal import Journal
from ewbot.live.preflight import BASELINE_FINGERPRINT
from ewbot.live.pricing import MARGIN_ENTRY_MAX, basis_ratio, map_level, margin_ratio, stage_contracts
from ewbot.live.reconcile import Alert, Close, PlanContext, is_ours, observe, plan
from ewbot.live.shadow import ShadowExchange
from ewbot.live.stage import (
    MAJOR,
    ORDER_FAILURES,
    ORDER_UNRESOLVED,
    PROTECT_MAX_S,
    STAGES,
    full_cap,
    on_incident,
    pause,
    paused,
    resume,
)
from ewbot.live.target import Target, TargetError, compute_target, frozen_config
from ewbot.live.watchdog import (
    NY,
    WS_STALE_S,
    AccountGuard,
    BadEquity,
    PriceSource,
    SessionWatch,
    StopGapWatch,
    margin_status,
    ny_date,
)
from ewbot.types import H1

CYCLE_OFFSET_S = 30
CYCLE_RETRY_S = 15
DATA_DEADLINE_S = 120
ENTRY_DEADLINE_S = 300
POLL_ACTIVE_S = 10.0
POLL_IDLE_S = 60.0
AUDIT_MINUTE = 50
DAILY_HOUR_NY = 9
MARGIN_CHECK_NY = (15, 30)
FLATTEN_CONFIRM_S = 60.0  # /flatten_confirm must be sent within this of its /flatten (Telegram message dates)
COMMAND_MAX_AGE_S = 120.0  # a command sent longer ago than this when the loop reaches it is ignored, with a reply
CONTROL_ALERT_FAILURES = 5  # failed getUpdates polls in a row that are alerted
CONTROL_STUCK_S = 120.0  # the command thread this far behind its own schedule (a hung request) is alerted
TELEGRAM_DOWN_S = 600.0  # Telegram failing this long is unusable: healthchecks.io /fail
NOTIFY_STALL_S = 180.0  # messages waiting TELEGRAM_DOWN_S with no send attempt for this long: the sender is stuck
NOTIFY_REFUSALS = 3  # messages Telegram refused in a row (HTTP 4xx, e.g. the bot blocked): Telegram unusable
HC_WITHHOLD_S = 1800.0  # after a Telegram /fail the success pings are withheld at most this long
HC_REFAIL_S = 10_800.0  # and the /fail is repeated this often while Telegram stays unusable
HEARTBEAT_ALERT_FAILURES = 3  # failed healthchecks.io pings in a row that are alerted (on Telegram)
EXPIRY_WARN_DAYS = 30
SNAPSHOT_TRIES = 3  # position / orders / position reads until both position reads agree
FEED_ALERT_S = 300.0  # no WebSocket futures price this long while OPEN is alerted (the watchdog's prices come from REST)
BAD_EQUITY_PAUSE_READS = 3  # balance reads in a row that are not a finite amount above 0: incident, entries paused
HEARTBEAT_KEY = "heartbeat"
AUDIT_KEY = "audit"
AUDIT_PENDING_KEY = "audit_pending"  # mismatches a lag could explain, seen in the last audit (id:type)
ORDER_FAIL_KEY = "order_failures"  # order failures not resolved yet, and the last event id looked at
# what an order failure is about: a bracket (protection), a close or a reduction
FAILURE_CLASS = {"BRACKET_FAILED": "bracket", "CLOSE_FAILED": "close", "CLOSE_UNCONFIRMED": "close",
                 "REDUCE_FAILED": "reduce"}
ACTION_CLASS = {"Protect": "bracket", "Close": "close", "Reduce": "reduce"}  # an ACTION_ERROR (a read failed)


@dataclass
class LiveDeps:
    journal: Journal
    exchange: Exchange
    lock: dict[str, Any]  # the preflight-checked holdout lock: the only source of the strategy configuration
    fetch: Callable[[Path], Path]  # candle cache update -> parquet path (data.coinbase_history.update_cache)
    cache_dir: Path
    notify: Callable[[str, str], Any] = lambda kind, text: None
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    feed: Any | None = None  # live.feed.TickerFeed
    heartbeat: Any | None = None  # live.health.Heartbeat
    commands: queue.Queue[Command] | None = None
    control: Any | None = None  # live.control.TelegramControl: watched every minute, started again when it died
    paper_ledger: Callable[[], dict[str, Any]] | None = None
    edit_stops: bool = False
    expiry: dict[str, str] = field(default_factory=dict)
    redact: Callable[[str], str] = lambda text: text  # removes the settings' secrets from error texts (live.app.redact)


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


def _side(d: int) -> str:
    return "롱" if d > 0 else "숏"


class LiveRuntime:
    def __init__(self, deps: LiveDeps) -> None:
        self.d = deps
        self.journal = deps.journal
        self.ex = deps.exchange
        self.clock = deps.clock
        self.cfg = frozen_config(deps.lock)  # holdout_config(BotConfig(), lock params), checked
        self.stage, _ = self.journal.stage()
        self.executor = Executor(self.ex, self.journal, clock=deps.clock, sleep=deps.sleep, alert=self.alert,
                                 tp1_frac=self.cfg.strategy.tp1_frac, edit_stops=deps.edit_stops)
        self.prices = PriceSource(self.ex, deps.feed, clock=deps.clock)
        self.gap = StopGapWatch()
        self.guard = AccountGuard(self.journal, self.cfg.breakers)
        self.sessions = SessionWatch()
        self.target: Target | None = None
        self.frame: pd.DataFrame | None = None  # the candles of the last cycle (the audit's DATA_REVISION check)
        self.product: ProductSpec | None = None
        self.session: SessionInfo | None = None
        self.done_bar: int | None = None
        self.next_try: float | None = None
        self.last_poll: float | None = None
        self.last_minute: float | None = None
        self.active = False
        self.flatten_sent: float | None = None  # the Telegram date of a /flatten awaiting its /flatten_confirm
        self.exit_code: int | None = None
        self.bad_equity_reads = 0  # balance reads in a row that were not an observation
        self.feed_down_since: float | None = None
        self.feed_failures = 0  # the feed's failure count when this outage began
        self.feed_alerted = False
        self.margin_no_price = False  # the margin check was skipped for want of a futures price (alerted once)
        self.tg_down_since: float | None = None  # a Telegram command-thread problem (alerted) since then
        self.tg_conflicts = 0  # the control's HTTP 409 count at the last minute
        self.hc_failed_at: float | None = None  # the latest /fail for Telegram of this episode
        self.hb_alerted = False  # failing healthchecks.io pings were alerted

    # ------------------------------------------------------------------ helpers
    def alert(self, kind: str, text: str) -> None:
        self.d.notify(kind, text)  # queued (live/notify.py sends it from its own thread): never waits on Telegram

    def _once(self, key: str, value: Any = True) -> bool:
        """True when the journal's `key` is not `value` yet (and records it): periodic jobs keep one row
        per key (e.g. the last New York date of the daily summary), alerts one row per item."""
        if self.journal.get(f"once:{key}") == value:
            return False
        self.journal.put(f"once:{key}", value)
        return True

    def _incident(self, kind: str, now: float) -> None:
        done = on_incident(self.journal, kind, now)
        if done:
            label = "중대 사건" if kind in MAJOR else "CANARY 실패"
            again = " (재개는 /resume)" if "paused" in done else ""
            self.alert("INCIDENT", f"[{label}] {kind}: {', '.join(done)}{again}")

    def _limits(self, now: float) -> tuple[str, int | None]:
        """(stage to size with, contract cap), read from the journal before every entry and adoption: the more
        conservative of the stage this process runs in and the journal's - an incident's or the CLI's demotion
        applies to the next order at once, not only after the restart - and `stage.full_cap`, the journal stage's
        maximum (0 in SHADOW: no real order; 1 in CANARY; 3 in FULL until released)."""
        cur, _ = self.journal.stage()
        stage = min(self.stage, cur if cur in STAGES else "SHADOW", key=STAGES.index)
        return stage, full_cap(self.journal, now)

    def _ctx(self, now: float) -> PlanContext:
        ratio = None
        try:
            ratio = basis_ratio(self.ex.quote(self.ex.futures_id).mid, self.ex.quote(self.ex.spot_id).mid)
        except (ExchangeError, ValueError):
            pass
        session_open = self.session is not None and self.session.is_open
        n, block = self._entry_size(now, ratio, session_open)
        tick = self.product.price_increment if self.product is not None else 5.0
        stage, cap = self._limits(now)
        most = cap if stage == "FULL" else 1  # the stage maximum for a position taken over (a real one in SHADOW too)
        return PlanContext(now=now, ratio=ratio, tick=tick, session_open=session_open, entry_contracts=n,
                           entry_block=block, max_contracts=most)

    def _entry_size(self, now: float, ratio: float | None, session_open: bool) -> tuple[int, str | None]:
        t = self.target
        if t is None or t.entry is None:
            return 0, None
        e = t.entry
        p = paused(self.journal)
        if p is not None:
            return 0, f"일시 정지 중 ({p['reason']})"
        if not session_open:
            return 0, f"장 운영 상태 {self.session.state if self.session else 'UNKNOWN'}"
        if now > e.signal_ts + H1 + ENTRY_DEADLINE_S:
            return 0, "봉 마감 후 5분이 지나 진입하지 않음"
        if ratio is None or self.product is None:
            return 0, "호가를 읽지 못함"
        try:
            equity = self.ex.balance().equity
        except ExchangeError as exc:
            return 0, f"잔고를 읽지 못함: {exc}"
        g = self.guard.state() or {}
        if g.get("total_tripped") or (g.get("daily_tripped") and g.get("day") == ny_date(now)):
            return 0, "실계좌 손실 한도 발동 중"
        stage, cap = self._limits(now)
        if cap == 0 and self.stage != "SHADOW":
            return 0, "단계가 SHADOW로 바뀜: 실주문을 내지 않음 (포지션이 없으면 재시작)"
        tick = self.product.price_increment
        entry = e.entry_ref * ratio
        stop = map_level(e.stop, ratio, tick, e.direction)
        n, why = stage_contracts(stage, equity=equity, entry=entry, stop=stop, direction=e.direction,
                                 risk_mult=e.risk_mult, cfg=self.cfg, full_cap=cap if stage == "FULL" else None)
        if n == 0:
            return 0, why
        projected = margin_ratio(n, entry, e.direction, self.product, equity)
        if projected > MARGIN_ENTRY_MAX:
            return 0, f"오버나이트 증거금 예상 {projected:.0%} > {MARGIN_ENTRY_MAX:.0%}"
        return n, None

    def flat(self) -> bool:
        t = self.executor.trade()
        if t is not None and t.status == "OPEN":
            return False
        return self.ex.position() is None and not [o for o in self.ex.open_orders() if is_ours(o)]

    # ------------------------------------------------------------------ startup
    def startup(self) -> dict[str, int]:
        now = self.clock()
        if self.journal.stage()[1] is None:
            self.journal.set_stage("SHADOW", "initial", now)
            self.stage = "SHADOW"
        self.product = self.ex.product()
        self.session = self.ex.session()
        self.sessions.update(self.session, now)
        try:  # the first download of the candle history takes minutes: never inside a timed cycle
            self._frame()
        except Exception as exc:  # noqa: BLE001 - the cycle retries the fetch
            self.journal.add_event(now, "FETCH_ERROR", {"error": self.d.redact(f"{type(exc).__name__}: {exc}")[:200]})
        if isinstance(self.ex, ShadowExchange):
            self.ex.restore(self.executor.trade())
        out = self.executor.recover()
        self.journal.add_event(now, "STARTUP", {"stage": self.stage})
        self.alert("STARTUP", f"[시작] 단계 {self.stage} · 재시작 복구 {out} · {_utc(now)}")
        self.poll(now)
        return out

    # ------------------------------------------------------------------ decision cycle
    def _frame(self) -> pd.DataFrame:
        raw = load_1h(Path(self.d.fetch(Path(self.d.cache_dir))))
        if raw.empty:
            raise TargetError("the candle cache is empty")
        self.frame = fill_gaps(raw)
        return self.frame

    def cycle(self, bar_ts: int, now: float) -> str:
        """OK | LATE (bar not there yet) | ERROR."""
        if self.next_try is None:
            self.journal.start_cycle(bar_ts, now, self.stage)
        try:
            df = self._frame()
            if int(df["ts"].iloc[-1]) < bar_ts:
                return "LATE"
            target = compute_target(self.d.lock, df, bar_ts)
        except Exception as exc:  # noqa: BLE001 - the cycle fails, protection goes on
            why = self.d.redact(f"{type(exc).__name__}: {exc}")
            self.journal.finish_cycle(bar_ts, self.clock(), "ERROR", why[:300])
            self.alert("CYCLE_ERROR", f"[사이클 오류] {_utc(bar_ts)} 봉: {why[:200]}")
            self.target = None
            return "ERROR"
        new = self.journal.add_decisions(target.decisions, self.clock())
        self.target = target
        self.journal.put("target", target.to_json())
        note = f"{len(new)} new decisions"
        try:
            self.reconcile(self.clock())
        except ExchangeError as exc:  # the next poll reconciles against this target
            note += f"; reconcile deferred: {exc}"[:200]
        self.journal.finish_cycle(bar_ts, self.clock(), "OK", note)
        return "OK"

    def _cycle_due(self, now: float) -> None:
        bar = int(now) // H1 * H1 - H1
        if self.done_bar is not None and bar <= self.done_bar:
            return
        if now < bar + H1 + CYCLE_OFFSET_S or (self.next_try is not None and now < self.next_try):
            return
        status = self.cycle(bar, now)
        if status == "LATE" and now < bar + H1 + DATA_DEADLINE_S:
            self.next_try = now + CYCLE_RETRY_S
            return
        if status == "LATE":
            self.journal.finish_cycle(bar, self.clock(), "HOLD", "bar missing 2 min after the close")
            self.target = None
            self.alert("DATA_LATE", f"[판단 보류] {_utc(bar)} 봉이 마감 후 2분 안에 오지 않음 (보호 주문 유지)")
        self.done_bar, self.next_try = bar, None

    # ------------------------------------------------------------------ reconcile
    def _snapshot(self) -> tuple[Position | None, list[Order]]:
        """Position and open orders as one state: the position is read again after the orders and the pair is
        used only when both position reads agree (a leg filling between the reads would pair a stale position
        with orders that already miss it); otherwise read again, and after SNAPSHOT_TRIES the poll is skipped."""
        for _ in range(SNAPSHOT_TRIES):
            pos = self.ex.position()
            orders = self.ex.open_orders()
            if self.ex.position() == pos:
                return pos, orders
        raise ExchangeError(f"the position changed during {SNAPSHOT_TRIES} reads of the orders")

    def reconcile(self, now: float) -> None:
        self.executor.resolve_reduce()  # a reduce with an unknown outcome: looked up by its id, never a second one
        pos, orders = self._snapshot()
        trade = self.executor.trade()
        seen, events = observe(trade, pos, orders, now)
        if seen is not None and seen != trade:
            self.executor.save(seen)
        if trade is not None and any(kind in ("CLOSED", "TP1_FILLED") for kind, _ in events):
            self._bracket_fills(trade, {o.order_id for o in orders if o.is_open}, now)
        for kind, data in events:
            self.journal.add_event(now, kind, data)
            if kind == "CLOSED":
                self.alert(kind, f"[청산 확인] 거래 {data['trade_id']} · {data['reason']}")
            elif kind == "TP1_FILLED":
                self.alert(kind, f"[TP1 체결] 거래 {data['trade_id']} · 잔여 {data['contracts']}계약 · 손절 본전으로")
            elif kind == "UNPROTECTED_60S":
                self._incident(kind, now)
                self.alert(kind, f"[중대 사건] 거래 {data['trade_id']}가 60초 넘게 보호되지 않음")
            elif kind == "REDUCE_NOT_SEEN":
                self.alert(kind, f"[경고] 거래 {data['trade_id']}: 체결된 수량 축소({data['reason']} {data['filled']}계약)가 "
                                 f"1분 넘게 포지션에 반영되지 않음 - 포지션 {data['position']}계약 기준으로 정리")
            elif kind == "UNEXPLAINED_REDUCTION":
                self._incident(kind, now)
                self.alert(kind, f"[경고] 거래 {data['trade_id']}: 주문 없이 {data['expected']}계약 → "
                                 f"{data['position']}계약 (손절 유지, 남은 수량 보호)")
        ctx = self._ctx(now)
        actions = plan(self.target, seen, pos, orders, ctx)
        self.executor.execute(actions, ctx)
        for a in actions:
            if isinstance(a, Alert) and a.kind in ("FOREIGN_ORDERS", "UNKNOWN_POSITION", "EXCESS_POSITION"):
                self._incident(a.kind, now)
            elif isinstance(a, Close) and a.reason in ("UNKNOWN_POSITION", "DIRECTION_MISMATCH"):
                self._incident(a.reason, now)  # a position opened, or flipped, outside our orders: closed, and manual
        if self.journal.events(kind="ADOPTED", since=now):  # a foreign position: taken over now, or a provisional
            self._incident("ADOPTED", now)  # trade settled as foreign (never our own entry rebuilt from its intent)
        t = self.executor.trade()
        # poll fast while a trade is open or an order's outcome is still unknown (looked up, then resent)
        self.active = (t is not None and t.status == "OPEN") or bool(self.journal.intents(status="PENDING"))
        if (t is not None and t.protected_ts is not None and t.protected_ts - t.opened_ts > PROTECT_MAX_S
                and self._once(f"slow:{t.trade_id}")):
            self._incident("PROTECT_SLOW", now)
        self._order_failures(now, ctx)

    def _order_failures(self, start: float, ctx: PlanContext) -> None:
        """CANARY gate 2 and its pause (spec 6.2), after every completed reconcile pass (begun at `start`). Each order
        failure - stage.ORDER_FAILURES, or an ACTION_ERROR of a Protect, Close or Reduce - is watched from the pass
        after it: it is resolved when its trade has closed since (a CLOSED event), or, for a bracket or a reduction,
        by a pass that saw a target and quotes (a reduction also an OPEN session and no reduction pending) and had
        no order failure for that trade. A bracket must be resolved within PROTECT_MAX_S of its failure, a close or a
        reduction by the next pass; otherwise it is ORDER_UNRESOLVED, a CANARY failure (incident: it pauses CANARY,
        fails the CANARY gate and in FULL keeps the 3-contract cap), recorded before its alert."""
        st = self.journal.get(ORDER_FAIL_KEY) or {"seen": 0, "ts": 0.0, "open": []}
        t = self.executor.trade()
        open_id = t.trade_id if t is not None and t.status == "OPEN" else 0
        new: list[dict[str, Any]] = []
        for kind in ORDER_FAILURES + ("ACTION_ERROR",):
            for e in self.journal.events(kind=kind, since=float(st["ts"]) - 600.0):
                cls = FAILURE_CLASS.get(kind) or ACTION_CLASS.get(str(e["data"].get("action")))
                if e["id"] > int(st["seen"]) and cls is not None:
                    tid = int(e["data"].get("trade_id") or open_id)
                    new.append({"id": e["id"], "kind": kind, "cls": cls, "trade_id": tid, "ts": float(e["ts"])})
        failing = {f["trade_id"] for f in new if f["ts"] >= start}  # trades with an order failure in this pass
        whole = self.target is not None and ctx.ratio is not None  # the pass saw what the trade should look like
        reducible = whole and ctx.session_open and (t is None or t.pending_reduce is None)

        def after(f: dict[str, Any], kinds: tuple[str, ...]) -> float | None:
            """When the first event of `kinds` for f's trade after f was recorded (None: none yet)."""
            return min((e["ts"] for k in kinds for e in self.journal.events(kind=k, since=f["ts"])
                        if e["id"] > f["id"] and int(e["data"].get("trade_id") or 0) == f["trade_id"]), default=None)

        kept: list[dict[str, Any]] = []
        done: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        for f in st["open"]:
            closed = after(f, ("CLOSED",))
            clean = f["trade_id"] not in failing and (reducible if f["cls"] == "reduce" else whole)
            if f["cls"] == "bracket":
                deadline = f["ts"] + PROTECT_MAX_S
                if (closed is not None and closed <= deadline) or (clean and start <= deadline):
                    done.append(f)
                else:
                    (kept if start <= deadline else unresolved).append(f)
            elif f["cls"] == "close":
                (done if closed is not None else unresolved).append(f)
            else:
                ok = closed is not None or clean or after(f, ("REDUCED", "REDUCE_SETTLED")) is not None
                (done if ok else unresolved).append(f)
        if new or done or unresolved:  # written only when something changed (one row, not one per pass)
            seen = max([int(st["seen"])] + [f["id"] for f in new])
            self.journal.put(ORDER_FAIL_KEY, {"seen": seen, "ts": start, "open": kept + new})
        for f in done:
            self.journal.add_event(start, "ORDER_RESOLVED", {"kind": f["kind"], "trade_id": f["trade_id"],
                                                             "failed_ts": f["ts"], "after_s": round(start - f["ts"], 3)})
        if not unresolved:
            return
        for f in unresolved:
            self.journal.add_event(start, ORDER_UNRESOLVED, {"kind": f["kind"], "trade_id": f["trade_id"],
                                                            "failed_ts": f["ts"], "event_id": f["id"]})
        self._incident(ORDER_UNRESOLVED, start)  # the pause first, the alert second
        for tid in sorted({f["trade_id"] for f in unresolved}):
            if self._once(f"unresolved:{tid}"):
                kinds = ", ".join(sorted({f["kind"] for f in unresolved if f["trade_id"] == tid}))
                self.alert(ORDER_UNRESOLVED, f"[주문 실패 미해결] 거래 {tid}: {kinds} - 제시간에 해결되지 않음 "
                                             "(CANARY 기준 ② 실패)")

    def _bracket_fills(self, trade: Any, still_open: set[str], now: float) -> None:
        """Record the fills of the legs that left the book (TP or stop), for the cost report."""
        for leg in trade.legs:
            if not leg.order_id or leg.order_id in still_open or not self._once(f"fill:{leg.order_id}"):
                continue
            try:
                o = self.ex.get_order(leg.order_id)
            except ExchangeError:
                continue
            if o.filled > 0 and o.avg_fill_price:
                self.journal.add_event(now, "BRACKET_FILL", {
                    "trade_id": trade.trade_id, "leg": leg.name, "side": o.side, "contracts": o.filled,
                    "price": o.avg_fill_price, "fee": o.fee, "stop": leg.stop_price, "limit": leg.limit_price})

    def poll(self, now: float) -> None:
        self.last_poll = now
        try:
            self.reconcile(now)
        except ExchangeError as exc:
            self.journal.add_event(now, "POLL_ERROR", {"error": str(exc)[:200]})

    def watch(self, now: float) -> None:
        t = self.executor.trade()
        if t is None or t.status != "OPEN":
            self.gap.reset()
            return
        price = self.prices.futures_price()
        if price is None:
            return
        if isinstance(self.ex, ShadowExchange):
            self.ex.set_prices(price)  # the virtual brackets see every price the watchdog sees
        if t.unprotected_since is not None and t.direction * (price - t.stop_price) <= 0:
            # no resting stop (e.g. the venue refused the bracket) and the price is through the stop: close, then alert
            self.journal.add_event(now, "STOP_UNPROTECTED", {"trade_id": t.trade_id, "price": price, "stop": t.stop_price})
            try:
                self.executor.close("STOP_UNPROTECTED", self._ctx(now))
            finally:
                self.alert("STOP_UNPROTECTED", f"[손절] 보호 주문 없이 가격 {price:,.1f}이 손절 {t.stop_price:,.0f}을 지남: 시장가 청산")
            return
        if not self.gap.check(t.direction, t.stop_price, price, now):
            return
        self.journal.add_event(now, "STOP_GAP", {"trade_id": t.trade_id, "price": price, "stop": t.stop_price})
        try:
            self.executor.close("STOP_GAP", self._ctx(now))
            self.gap.reset()
        finally:
            self.alert("STOP_GAP", f"[손절 갭] 가격 {price:,.1f}이 손절 {t.stop_price:,.0f}의 {STOP_LIMIT_OFFSET:.0%} 지정가를 10초 넘게 지남: 시장가 청산")

    # ------------------------------------------------------------------ every minute
    def minute(self, now: float) -> None:
        self.last_minute = now
        self.journal.put(HEARTBEAT_KEY, now)
        try:
            self._telegram_health(now)
        except Exception as exc:  # noqa: BLE001 - never at the cost of the session, breaker and margin checks
            self.journal.add_event(now, "LOOP_ERROR", {"section": "telegram", "error": type(exc).__name__})
            print(f"[LIVE] telegram and heartbeat check failed: {type(exc).__name__}", file=sys.stderr)
        try:
            was_open = self.session is not None and self.session.is_open
            self.session = self.ex.session()
            for kind, text in self.sessions.update(self.session, now):
                self.journal.add_event(now, kind, {"text": text, "state": self.session.state} if kind == "SESSION"
                                       else {"text": text})
                self.alert(kind, f"[장 상태] {text}")
            if self.session.is_open and not was_open:
                self.poll(now)  # deferred closes run right after the reopen (spec 4.5)
        except ExchangeError as exc:
            self.journal.add_event(now, "SESSION_ERROR", {"error": str(exc)[:200]})
        self._money(now)
        self._feed_health(now)
        if self.journal.intents(status="PENDING"):
            try:
                self.executor.resolve_pending()
            except ExchangeError:
                pass
        local = datetime.fromtimestamp(now, NY)
        today = local.date().isoformat()
        if (local.hour, local.minute) >= MARGIN_CHECK_NY and local.hour < 16 and self.journal.get("once:margin1530") != today:
            self._overnight_report(now)  # marks the day done once its reads succeed
        if local.hour >= DAILY_HOUR_NY and self._once("daily", today):
            self.daily(now)
        if datetime.fromtimestamp(now, UTC).minute >= AUDIT_MINUTE and self._once("audit", int(now) // H1):
            self.run_audit(now)
        stage, _ = self.journal.stage()
        if stage != self.stage and self.flat():
            self.alert("STAGE", f"[단계 변경] {self.stage} → {stage}: 재시작해 적용")
            self.exit_code = 0

    def _control_problem(self, now: float) -> str | None:
        """What is wrong with the Telegram command thread this minute, or None. A dead thread is started again. A
        thread CONTROL_STUCK_S behind its own schedule (a hung request) is only reported: a second thread would poll
        the same bot beside the hung one (HTTP 409) and leak; restarting the container ends both."""
        c = self.d.control
        if c is None:
            return None
        conflicts, self.tg_conflicts = c.conflicts - self.tg_conflicts, c.conflicts
        if not c.alive:
            if self.d.commands is not None:
                c.start(self.d.commands)
            return f"명령 수신 스레드가 멈춰 다시 시작함 (마지막 오류 {c.last_error or '-'})"
        overdue = getattr(c, "overdue", None)  # TelegramControl.overdue; a control without it is never late
        late = float(overdue(now)) if callable(overdue) else 0.0
        if late >= CONTROL_STUCK_S:
            return f"명령 수신 스레드가 {late / 60:.0f}분 넘게 응답 없음 (걸린 요청: 컨테이너를 다시 시작해야 함)"
        if c.failures >= CONTROL_ALERT_FAILURES:
            return f"getUpdates {c.failures}회 연속 실패 ({c.last_error or '-'})"
        if conflicts > 0:
            return f"getUpdates 충돌 {conflicts}회 (HTTP 409: 같은 봇 토큰으로 다른 프로세스나 웹훅이 명령을 받음)"
        return None

    def _notify_problem(self, now: float) -> str | None:
        """Why the notifier (live.notify.LiveNotifier) cannot deliver, or None: its oldest failing message has waited
        TELEGRAM_DOWN_S; messages have waited that long with no send attempt for NOTIFY_STALL_S (a stuck or stopped
        sender thread); or Telegram refused NOTIFY_REFUSALS messages in a row. A recorder without these attributes
        (tests) never has a problem."""
        n: Any = self.d.notify
        failing, oldest = getattr(n, "failing_since", None), getattr(n, "oldest_pending", None)
        attempt, refusals = getattr(n, "last_attempt", None), getattr(n, "refusals", 0)
        why = getattr(n, "last_error", None) or "-"
        if failing is not None and now - failing >= TELEGRAM_DOWN_S:
            return f"알림 전송이 {(now - failing) / 60:.0f}분째 실패 ({why})"
        if (oldest is not None and now - oldest >= TELEGRAM_DOWN_S
                and (attempt is None or now - attempt >= NOTIFY_STALL_S)):
            return f"알림 전송 스레드가 멈춤 (대기 {getattr(n, 'pending', 0)}건, {(now - oldest) / 60:.0f}분째 시도 없음)"
        if refusals >= NOTIFY_REFUSALS:
            return f"텔레그램이 알림 {refusals}건을 연속 거부 ({why})"
        return None

    def _telegram_health(self, now: float) -> None:
        """The Telegram command thread and the external heartbeat (spec 3.2 `control`, `health`). A command-thread
        problem - the thread died (it is started again) or hangs, CONTROL_ALERT_FAILURES failed polls in a row (e.g.
        HTTP 401: the token refused) or a new HTTP 409 (another poller or a webhook on this bot: commands would be
        split) - is alerted once per episode and once more when it is over. Telegram is unusable while such a problem
        has lasted TELEGRAM_DOWN_S or the notifier cannot deliver (`_notify_problem`): healthchecks.io then gets a
        /fail naming it (the check's email reaches the user without Telegram) and the success pings are withheld
        for at most HC_WITHHOLD_S, so the check stays down; then the pings resume - a hung loop or a dead VPS
        still stops them - and the /fail is repeated every HC_REFAIL_S while Telegram stays unusable. Otherwise the
        minute's success ping goes out (from this loop: a hung loop stops it), and HEARTBEAT_ALERT_FAILURES failed
        pings in a row are alerted once."""
        problem = self._control_problem(now)
        if problem is None and self.tg_down_since is not None:
            self.journal.add_event(now, "TELEGRAM_UP", {"down_s": now - self.tg_down_since})
            self.alert("TELEGRAM", "[텔레그램 복구] 명령 수신 정상")
            self.tg_down_since = None
        elif problem is not None and self.tg_down_since is None:
            self.tg_down_since = now
            self.journal.add_event(now, "TELEGRAM_DOWN", {"problem": problem})
            self.alert("TELEGRAM", f"[텔레그램 명령 수신 이상] {problem} - 명령이 듣지 않으면 VPS에서 ewbot live status")
        down: list[str] = []
        if problem is not None and self.tg_down_since is not None and now - self.tg_down_since >= TELEGRAM_DOWN_S:
            down.append(f"명령 수신: {problem}")
        sending = self._notify_problem(now)
        if sending is not None:
            down.append(sending)
        hb = self.d.heartbeat
        if down:
            if self.hc_failed_at is None or now - self.hc_failed_at >= HC_REFAIL_S:
                self.hc_failed_at = now
                self.journal.add_event(now, "TELEGRAM_UNUSABLE", {"reasons": down})
                if hb is not None:
                    hb.fail("[LIVE] 텔레그램 사용 불가 - " + "; ".join(down))
            if now - self.hc_failed_at < HC_WITHHOLD_S:
                return  # no success ping yet: the check stays down (a failed /fail brings it down after its grace)
        else:
            self.hc_failed_at = None
        if hb is None:
            return
        hb.beat()
        if hb.failures >= HEARTBEAT_ALERT_FAILURES and not self.hb_alerted:
            self.hb_alerted = True
            self.journal.add_event(now, "HEARTBEAT_FAILING", {"failures": hb.failures, "error": hb.last_error})
            self.alert("HEARTBEAT", f"[외부 감시 핑 실패] healthchecks.io 핑이 {hb.failures}회 연속 실패 "
                                    f"({hb.last_error}): VPS가 멈춰도 경보가 오지 않을 수 있음")
        elif hb.failures == 0 and self.hb_alerted:
            self.hb_alerted = False
            self.alert("HEARTBEAT", "[외부 감시 핑 복구] healthchecks.io 핑 정상")

    def _money(self, now: float) -> None:
        try:
            equity = self.ex.balance().equity
            trip = self.guard.update(equity, now)  # trips on the second breaching read in a row
        except ExchangeError:
            return
        except BadEquity:  # not an observation: no breaker sample and no margin check this minute
            self.bad_equity_reads += 1
            if self.bad_equity_reads == 1:
                self.journal.add_event(now, "BAD_EQUITY", {"equity": repr(equity)})
                self.alert("BAD_EQUITY", f"[잔고 이상] 자산 값 {equity!r}: 손실 한도와 증거금 점검을 건너뜀 "
                                         "(정상 값이 읽히면 다시 점검)")
            if self.bad_equity_reads == BAD_EQUITY_PAUSE_READS:  # persistent: no new risk without a balance
                if paused(self.journal) is None:
                    pause(self.journal, "incident BAD_EQUITY", now)
                self._incident("BAD_EQUITY", now)
                self.alert("INCIDENT", f"[중대 사건] 잔고 이상이 {BAD_EQUITY_PAUSE_READS}회 연속: 신규 진입 일시 정지 "
                                       "(보호 주문과 청산은 계속, 재개는 /resume)")
            return
        self.bad_equity_reads = 0
        if trip is not None:  # pause and flatten first, alert second
            self.journal.add_event(now, "BREAKER", {"kind": trip, "equity": equity})
            if paused(self.journal) is None:
                pause(self.journal, f"breaker {trip}", now)
            try:
                self.executor.flatten(f"BREAKER_{trip}", self._ctx(now))
            finally:
                self._incident("BREAKER", now)
                self.alert("BREAKER", f"[실계좌 손실 한도] {trip}: 전량 청산하고 일시 정지 (자산 {equity:,.2f} USD)")
            return
        t = self.executor.trade()
        try:
            pos = self.ex.position() if t is not None and t.status == "OPEN" else None
        except ExchangeError:
            return  # the next minute checks the margin again (the rest of the minute still runs)
        if pos is None or self.product is None:
            return
        price = self.prices.futures_price()
        if price is None:  # never reduce or close on a stand-in (the entry price may read 0): skip, alert once
            if not self.margin_no_price:
                self.journal.add_event(now, "MARGIN_NO_PRICE", {"contracts": pos.contracts})
                self.alert("MARGIN", "[증거금 점검: 가격 없음] 선물 가격을 읽지 못해 증거금 점검을 건너뜀 "
                                     "(가격이 돌아오면 다시 점검)")
            self.margin_no_price = True
            return
        self.margin_no_price = False
        st = margin_status(pos.contracts, price, pos.side, self.product, equity)
        if st.level == "WARN" and self._once("marginwarn", int(now) // H1):
            self.alert("MARGIN", f"[증거금 경고] 오버나이트 기준 {st.ratio:.0%} (80% 이상)")
        elif st.level == "REDUCE" and st.reduce_by > 0:
            try:
                if st.reduce_by >= pos.contracts:
                    self.executor.close("MARGIN", self._ctx(now))
                else:
                    self.executor.reduce(st.reduce_by, "MARGIN", self._ctx(now))
            finally:
                self.alert("MARGIN", f"[증거금 축소] 오버나이트 기준 {st.ratio:.0%}: {st.reduce_by}계약 축소")

    def _feed_health(self, now: float) -> None:
        """The watchdog's WebSocket price: no futures tick for FEED_ALERT_S while the session is OPEN - a dead
        connection, or a refused subscription while the heartbeats still flow - is alerted once per outage
        (prices come from REST meanwhile), and once more when futures ticks are back. A closed or halted venue
        has no trades: that is no outage."""
        feed = self.d.feed
        if feed is None:
            return
        tick = feed.latest(self.ex.futures_id)
        if tick is not None and now - tick.ts <= WS_STALE_S:
            if self.feed_alerted:
                self.journal.add_event(now, "FEED_UP", {"down_s": now - (self.feed_down_since or now)})
                self.alert("FEED", "[가격 피드 복구] 웹소켓 선물 가격 수신 재개")
            self.feed_down_since, self.feed_alerted = None, False
            return
        if self.session is None or not self.session.is_open:
            if not self.feed_alerted:  # no trades while the venue is not OPEN: only OPEN time counts to an alert
                self.feed_down_since = None
            return
        if self.feed_down_since is None:
            self.feed_down_since = now
            self.feed_failures = feed.failures
        down = now - self.feed_down_since
        if self.feed_alerted or down < FEED_ALERT_S:
            return
        self.feed_alerted = True
        failures = feed.failures - self.feed_failures
        self.journal.add_event(now, "FEED_DOWN", {"down_s": down, "failures": failures, "last_error": feed.last_error,
                                                  "mode": self.prices.mode})
        self.alert("FEED", f"[가격 피드 끊김] 웹소켓 선물 가격이 {down / 60:.0f}분 넘게 없음: 감시 가격은 REST 10초 폴링 "
                           f"(재연결 실패 {failures}회, 마지막 오류 {feed.last_error or '-'})")

    def _overnight_report(self, now: float) -> None:
        """The 15:30 New York check before the 16:00 overnight margin. Failed reads are retried every minute until
        16:00 (the first failure of the day is alerted); the day is marked done once the reads succeed."""
        today = datetime.fromtimestamp(now, NY).date().isoformat()
        try:
            pos = self.ex.position()
            balance = self.ex.balance() if pos is not None else None
        except ExchangeError as exc:
            if self._once("margin1530fail", today):
                self.journal.add_event(now, "MARGIN_1530", {"ratio": None, "error": str(exc)[:200]})
                self.alert("MARGIN_1530", f"[15:30 증거금 점검] 조회 실패: {str(exc)[:120]} (16시 전까지 매분 다시 시도)")
            return
        self._once("margin1530", today)
        if pos is None or balance is None or self.product is None:
            return
        equity = usable_price(balance.equity)
        price = self.prices.futures_price() or usable_price(pos.avg_entry)  # a report: the entry price may stand in
        if price is None or equity is None:
            self.journal.add_event(now, "MARGIN_1530", {"ratio": None, "contracts": pos.contracts})
            self.alert("MARGIN_1530", f"[15:30 증거금 점검] 가격 또는 잔고를 읽지 못해 계산하지 못함 ({pos.contracts}계약)")
            return
        r = margin_ratio(pos.contracts, price, pos.side, self.product, equity)
        self.journal.add_event(now, "MARGIN_1530", {"ratio": r})
        self.alert("MARGIN_1530", f"[15:30 증거금 점검] 16시 오버나이트 기준 예상 {r:.0%} ({pos.contracts}계약)")

    # ------------------------------------------------------------------ audit, summary, reminders
    def _data_hash(self) -> Callable[[int], str | None] | None:
        """The live candles' `data_fingerprint(df, 0, ts)` (the paper ledger records the same hash per decision and per
        run) for the audit's DATA_REVISION check; None before candles were loaded, and for a bar after them."""
        df = self.frame
        if df is None or df.empty:
            return None
        last = int(df["ts"].iloc[-1])
        memo: dict[int, str] = {}

        def digest(ts: int) -> str | None:
            if ts > last:
                return None
            if ts not in memo:
                memo[ts] = data_fingerprint(df, 0, ts)
            return memo[ts]

        return digest

    def _decided_until(self) -> int:
        """The last bar whose cycle ended OK (0: none): the live runtime has decided every bar up to it."""
        return max((int(c["bar_ts"]) for c in self.journal.cycles() if c["status"] == "OK"), default=0)

    def run_audit(self, now: float) -> AuditResult | None:
        """The journal's decisions against the paper ledger (spec 3.5), up to the last bar whose cycle ended OK. A
        mismatch the candles explain (DATA_REVISION: the bar changed between the two fetches) is alerted once and is
        no incident; a decision missing on one side is AUDIT_PENDING first (alerted once) and AUDIT_MISMATCH - a
        major incident, its pause before its alert - only when the next audit still finds it; a different decision
        or a paper fingerprint change, which no lag causes, is one at once. A failed audit (the fetch, an invalid
        ledger, anything else) is stored as failed - the gates never pass on the last good one - and alerted once
        per failing episode."""
        if self.d.paper_ledger is None:
            return None
        try:
            res = audit(self.journal.decisions(), self.d.paper_ledger(), BASELINE_FINGERPRINT,
                        until=self._decided_until(), now=now, data_hash=self._data_hash())
        except Exception as exc:  # noqa: BLE001 - every failure is a failed audit, never the last good one
            why = self.d.redact(str(exc) if isinstance(exc, AuditError) else f"{type(exc).__name__}: {exc}")[:300]
            res = AuditResult.failed(why, now)
            self.journal.put(AUDIT_KEY, res.to_json())
            self.journal.add_event(now, "AUDIT_ERROR", {"error": why})
            if self._once("auditfail", True):
                self.alert("AUDIT", f"[모의매매 대조 실패] {why[:200]} (대조가 다시 성공할 때까지 단계 기준 미충족)")
            return res
        self.journal.put(AUDIT_KEY, res.to_json())
        if self.journal.get("once:auditfail") is True:
            self.journal.put("once:auditfail", False)
            self.alert("AUDIT", "[모의매매 대조 복구] 대조가 다시 성공함")

        def key(m: dict[str, Any]) -> str:
            return f"{m['id']}:{m['type']}"

        before = set(self.journal.get(AUDIT_PENDING_KEY) or [])
        laggable = {key(m) for m in res.mismatches
                    if m["cause"] != "DATA_REVISION" and m["type"] in ("MISSING_IN_LIVE", "MISSING_IN_PAPER")}
        self.journal.put(AUDIT_PENDING_KEY, sorted(laggable))
        revised = [m for m in res.mismatches if m["cause"] == "DATA_REVISION" and self._once(f"revision:{key(m)}")]
        pending = [m for m in res.mismatches if key(m) in laggable - before and self._once(f"pending:{key(m)}")]
        major = [m for m in res.mismatches if m["cause"] != "DATA_REVISION" and (key(m) not in laggable or key(m) in before)
                 and self._once(f"mismatch:{key(m)}")]

        def lines(items: list[dict[str, Any]]) -> str:
            return "\n".join(f"{m['type']} {m['id']}" for m in items[:10])

        if revised:
            self.journal.add_event(now, "AUDIT_REVISION", {"items": revised[:20]})
            self.alert("AUDIT_REVISION", "[판단 불일치: 봉 수정] 두 런타임이 받은 캔들이 그 봉에서 다름 "
                                         "(중대 사건 아님, 확인 필요)\n" + lines(revised))
        if pending:
            self.journal.add_event(now, "AUDIT_PENDING", {"items": pending[:20]})
            self.alert("AUDIT_PENDING", "[판단 불일치 확인 중] 다음 대조에서도 남으면 중대 사건\n" + lines(pending))
        if major:
            self.journal.add_event(now, "AUDIT_MISMATCH", {"items": major[:20]})
            self._incident("AUDIT_MISMATCH", now)
            self.alert("AUDIT_MISMATCH", "[판단 불일치] 실거래 vs 모의매매\n" + lines(major))
        return res

    def costs(self, since: float) -> dict[str, Any]:
        """Measured costs since `since`: fills, entry slippage vs. the engine reference, stop slippage vs. the
        stop (positive = worse), fees vs. the paper cost model. Funding is in the balance, not measured here."""
        kinds = ("ENTRY", "CLOSED", "REDUCED", "BRACKET_FILL")
        fills = [e["data"] for k in kinds for e in self.journal.events(kind=k, since=since) if e["data"].get("price")]
        entries = [e["data"] for e in self.journal.events(kind="ENTRY", since=since) if e["data"].get("ref")]
        entry_bps = [d["side"] * (d["price"] - d["ref"]) / d["ref"] * 1e4 for d in entries]
        stops = [d for d in (e["data"] for e in self.journal.events(kind="BRACKET_FILL", since=since))
                 if abs(d["price"] - d["stop"]) < abs(d["price"] - d["limit"])]
        stop_bps = [d["side"] * (d["price"] - d["stop"]) / d["stop"] * 1e4 for d in stops]
        fees = sum(float(f.get("fee") or 0.0) for f in fills)
        model = sum(model_fee(float(f["price"]), int(f.get("contracts") or 0), self.cfg.costs) for f in fills)
        return {"fills": len(fills), "slippage_bps": (sum(entry_bps) / len(entry_bps)) if entry_bps else None,
                "stop_slippage_bps": (sum(stop_bps) / len(stop_bps)) if stop_bps else None, "fees": fees,
                "model_fees": model}

    def daily(self, now: float) -> None:
        local = datetime.fromtimestamp(now, NY).date()
        c = self.costs(now - 86_400)
        if c["fills"]:
            self.journal.add_event(now, "COSTS", c)
        lines = [f"[일일 요약] {local} (뉴욕)"] + status_lines(self.journal, now)
        slip = "-" if c["slippage_bps"] is None else f"{c['slippage_bps']:+.1f} bps"
        stop_slip = "-" if c["stop_slippage_bps"] is None else f"{c['stop_slippage_bps']:+.1f} bps"
        lines.append(f"체결 24시간: {c['fills']}건 · 진입 슬리피지 {slip} · 손절 슬리피지 {stop_slip} · 수수료 실측 "
                     f"{c['fees']:.2f} USD (모의 가정 {c['model_fees']:.2f} USD) · 펀딩은 잔고 변화에 포함")
        try:
            equity = self.ex.balance().equity
            lines.append(f"자산 {equity:,.2f} USD")
            pos = self.ex.position()
            if pos is not None and self.product is not None:
                price = self.prices.futures_price() or usable_price(pos.avg_entry)  # display only
                if price is None or usable_price(equity) is None:
                    lines.append("오버나이트 증거금: 가격 또는 잔고 없음")
                else:
                    lines.append(f"오버나이트 증거금 {margin_ratio(pos.contracts, price, pos.side, self.product, equity):.0%}")
        except ExchangeError:
            lines.append("잔고를 읽지 못함")
        self.alert("DAILY", "\n".join(lines))
        for name, day in sorted(self.d.expiry.items()):
            left = (datetime.fromisoformat(day).date() - local).days
            if left <= EXPIRY_WARN_DAYS:
                self.alert("EXPIRY", f"[만료 예정] {name}: {day} ({left}일 남음) - 갱신 필요")
        try:
            if self.ex.key_permissions().can_transfer:
                self._incident("TRANSFER_KEY", now)
                self.alert("TRANSFER_KEY", "[중대 사건] API 키에 송금 권한이 있음: 즉시 키를 교체")
        except ExchangeError:
            pass

    # ------------------------------------------------------------------ Telegram commands
    def handle(self, cmd: str, now: float, sent: float | None = None) -> str:
        """One Telegram command. `sent` is the message's Telegram date (`now` when not given): /flatten_confirm
        counts only when sent within FLATTEN_CONFIRM_S of the /flatten it confirms."""
        sent = now if sent is None else float(sent)
        stamp = datetime.fromtimestamp(now, NY).strftime("%H:%M:%S")
        if cmd == "/status":
            reply = "\n".join(status_lines(self.journal, now))
        elif cmd == "/pause":
            pause(self.journal, "user /pause", now)
            reply = "일시 정지: 신규 진입 중단 (보호 주문과 청산은 계속)"
        elif cmd == "/resume":
            resume(self.journal, now)
            reply = "재개: 다음 신호부터 진입"
            try:  # only a breaker that tripped is re-based at the current equity
                rebased: list[str] | None = self.guard.reset(self.ex.balance().equity, now)
            except (ExchangeError, BadEquity):
                rebased = None
            if rebased:
                reply += f" · 손실 한도 기준을 다시 잡음 (지금과 다음 두 번 조회의 중앙값): {', '.join(rebased)}"
            elif rebased is None:
                reply += " · 잔고를 읽지 못해 손실 한도 기준은 그대로 (발동 중이면 다시 /resume)"
        elif cmd == "/flatten":
            self.flatten_sent = sent
            reply = "60초 안에 /flatten_confirm 을 보내면 전량 청산하고 일시 정지합니다"
        elif cmd == "/flatten_confirm":
            if self.flatten_sent is None or not 0.0 <= sent - self.flatten_sent <= FLATTEN_CONFIRM_S:
                reply = "확인할 /flatten 요청이 없음 (먼저 /flatten)"
            else:
                self.flatten_sent = None
                pause(self.journal, "user /flatten", now)
                ok = self.executor.flatten("USER_FLATTEN", self._ctx(now))
                reply = "전량 청산 완료, 일시 정지" if ok else "청산 요청을 보냈으나 아직 확인되지 않음 (계속 재시도)"
        else:
            reply = "/status · /pause · /resume · /flatten (+ /flatten_confirm 60초 안)"
        self.journal.add_event(now, "COMMAND", {"cmd": cmd, "reply": reply[:200]})
        self.alert("COMMAND", f"{reply}\n({stamp} 뉴욕)")
        return reply

    # ------------------------------------------------------------------ main loop
    def _commands(self, now: float) -> None:
        q = self.d.commands
        while q is not None and not q.empty():
            cmd = q.get_nowait()
            if now - cmd.sent > COMMAND_MAX_AGE_S:  # e.g. sent while the runtime restarted: never acted on late
                self.journal.add_event(now, "COMMAND_STALE", {"cmd": cmd.word, "sent": cmd.sent})
                self.alert("COMMAND", f"오래된 명령 무시: {cmd.word} ({now - cmd.sent:.0f}초 전에 보냄 - 필요하면 다시 보내 주세요)")
                continue
            try:
                self.handle(cmd.word, now, cmd.sent)
            except ExchangeError as exc:
                self.alert("COMMAND", f"{cmd.word} 실패: {str(exc)[:200]} (다시 보내 주세요)")

    def tick(self) -> None:
        now = self.clock()
        sections: list[tuple[str, Callable[[float], None], bool]] = [
            ("commands", self._commands, self.d.commands is not None),
            ("watch", self.watch, self.active),
            ("poll", self.poll, self.last_poll is None or now - self.last_poll >= (POLL_ACTIVE_S if self.active else POLL_IDLE_S)
             or (self.d.feed is not None and self.d.feed.take_resync())),
            ("minute", self.minute, self.last_minute is None or now - self.last_minute >= 60.0),
            ("cycle", self._cycle_due, True),
        ]
        for name, fn, due in sections:
            if not due:
                continue
            try:
                fn(now)
            except Exception as exc:  # noqa: BLE001 - one failing section must not stop the others
                why = self.d.redact(f"{type(exc).__name__}: {exc}")
                self.journal.add_event(now, "LOOP_ERROR", {"section": name, "error": why[:300]})
                self.alert("LOOP_ERROR", f"[루프 오류] {name}: {why[:200]}")
                print(self.d.redact(traceback.format_exc()), file=sys.stderr, end="")

    def run_forever(self, max_ticks: int | None = None) -> int:
        self.startup()
        n = 0
        while self.exit_code is None and (max_ticks is None or n < max_ticks):
            self.tick()
            n += 1
            self.d.sleep(1.0)
        return 0 if self.exit_code is None else self.exit_code


def status_lines(journal: Journal, now: float) -> list[str]:
    """Journal-only status (no exchange call): `ewbot live status`, /status and the daily summary."""
    stage, since = journal.stage()
    days = "" if since is None else f" (D+{(now - since) / 86_400:.1f})"
    cap = ""
    if stage == "FULL":
        most = full_cap(journal, now)
        cap = " · 상한 해제" if most is None else f" · 상한 {most}계약"
    p = paused(journal)
    lines = [f"단계 {stage}{days}{cap} · {'일시 정지: ' + p['reason'] if p else '운영 중'}"]
    t = journal.get("trade")
    if t and t.get("status") == "OPEN":
        legs = ", ".join(f"{leg['name']} {leg['size']}@{leg['limit_price']:,.0f}" for leg in t.get("legs", []))
        lines.append(f"포지션: {t['setup']} {_side(t['direction'])} {t['contracts']}계약 @ {t['entry_price']:,.1f} · "
                     f"손절 {t['stop_price']:,.0f} · TP1 {'체결' if t.get('tp1_filled') else '대기'} · 묶음 [{legs}]")
    else:
        lines.append("포지션: 없음")
    cycles = journal.cycles()
    if cycles:
        c = cycles[-1]
        took = "-" if c["finished_ts"] is None else f"마감 후 {c['finished_ts'] - (c['bar_ts'] + H1):.0f}초"
        lines.append(f"마지막 사이클: {_utc(c['bar_ts'])} 봉 · {c['status']} · {took}")
    a = audit_from_journal(journal)
    if a is not None and a.error is not None:
        lines.append(f"모의매매 대조: 실패 - {a.error[:120]} ({_utc(a.as_of)})")
    elif a is not None:
        lines.append(f"모의매매 대조: 비교 {a.compared}건, 일치 {a.matched}건, 불일치 {len(a.mismatches)}건 "
                     f"({_utc(a.window[1])} 봉까지, {_utc(a.as_of)})")
    hb = journal.get(HEARTBEAT_KEY)
    if hb is not None:
        lines.append(f"루프 하트비트: {now - float(hb):.0f}초 전")
    return lines


def audit_from_journal(journal: Journal) -> AuditResult | None:
    """The last audit the runtime stored - a failed one included (`AuditResult.error`)."""
    a = journal.get(AUDIT_KEY)
    return None if not a else AuditResult.from_json(a)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run --group live pytest tests/live/test_runtime.py -q
```

Expected: `34 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/live/shadow.py src/ewbot/live/runtime.py tests/live/harness.py tests/live/test_runtime.py
git commit -m "$(cat <<'EOF'
feat(live): SHADOW exchange and the runtime loop (cycle, poll, watchdog, commands, daily summary, audit)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 13: CLI `ewbot live …` and the production wiring

**Files:**
- Create: `src/ewbot/live/app.py`
- Modify: `src/ewbot/cli.py` (six replacements below)
- Test: `tests/live/test_cli_live.py`

**Interfaces:**
- Consumes: every module of Tasks 1–12; `ewbot.data.coinbase_history.update_cache`, `ewbot.paper.criteria.golive_status`, `ewbot.paper.ledger` (`Ledger`, `LedgerError`), `ewbot.paper.notify.TelegramSender`.
- Produces (`ewbot.live.app`): `real_exchange(settings) -> Exchange` (raises `SettingsError` without a key file), `stage_exchange(real, journal, stage, notify)` (`ShadowExchange` in SHADOW), `notifier(settings) -> LiveNotifier` (its sending thread started), `redact(text, settings) -> str` (the bot token, the GitHub token and the health-check URL replaced by `<secret>`), `fatal_report(exc, settings) -> str` (the traceback, `redact`ed), `paper_fetcher(settings) -> Callable | None`, `paper_status(settings, now) -> dict | None`, `price_feed(settings) -> TickerFeed | None` (the ticker feed on Coinbase's public endpoint, `feed.public_ws_factory()`: no key), dataclass `Wired(runtime, feed, control, heartbeat, notify)` with `close(flush_s=10.0)` (stops the Telegram poller and the feed, then gives the queued messages up to `flush_s` to go out), `build(settings, lock, real, journal, notify=None) -> Wired` (hands the preflight-checked lock to `LiveDeps`, the Telegram control so the runtime watches it and `redact` for its error texts; starts the feed and the Telegram threads). Tests replace `real_exchange`, `price_feed` (no WebSocket) and the module-level `update_cache`; the fatal-exit test also `Heartbeat`, `notifier` and `TelegramControl`.
- Produces (`ewbot.cli`): sub-command `live` with `run`, `preflight [--offline]`, `status [--json] [--max-age S] [--exchange] [--require-flat]`, `stage {shadow,canary,full} [--confirm]`, `backup --out PATH` (the SHADOW and CANARY gates of `status` and the promotion gates of `stage` take the runtime's last stored audit, `audit_from_journal` - a failed one is unmet); `LIVE_REFUSAL_WAIT_S = 600.0` (a refused start waits before exiting so Docker's restarts stay sparse), test seams `_live_settings() -> LiveSettings` and `_live_lock() -> (Path, sha256)`, `cmd_live(args) -> int`; `live run` closes the wiring on every exit; on a fatal exception it sends healthchecks.io `/fail` with the type name only (a third party), Telegram the type and `redact`ed text and stderr the `fatal_report`, and returns 1 (KeyboardInterrupt and SystemExit are re-raised). Exit codes: 0 ok; 1 no journal / heartbeat older than `--max-age`, or `live run` ended by a fatal exception; 2 refused (settings, preflight, stage gate); 3 `--require-flat` found a trade, position or resting order.

- [ ] **Step 1: Write the failing test**

**File: `tests/live/test_cli_live.py`**

```python
from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from typing import Any

import pytest

from ewbot import cli
from ewbot.cli import build_parser, main
from ewbot.live import app as live_app
from ewbot.live.audit import AuditResult
from ewbot.live.exchange import KeyPermissions
from ewbot.live.health import Heartbeat
from ewbot.live.journal import Journal
from ewbot.live.notify import LiveNotifier
from ewbot.live.runtime import LiveRuntime
from ewbot.live.settings import LiveSettings, SettingsError
from ewbot.live.sim import SimExchange
from tests.live.helpers import Clock
from tests.paper.helpers import FakeFetch, FakeSender, make_lock, synth_frame, ts

NOW = ts("2024-03-13 23:00") + 30


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    """Settings in a temp dir, a temp holdout lock, a SimExchange instead of Coinbase, synthetic candles."""
    df = synth_frame()
    lock = make_lock(df)
    lock_path = tmp_path / "holdout.lock"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    settings = LiveSettings(data_dir=tmp_path / "data")
    clock = Clock(NOW)
    ex = SimExchange(clock=clock)
    ex.set_prices(26_744.5, 26_744.5)
    monkeypatch.setattr(cli, "_live_settings", lambda: settings)
    monkeypatch.setattr(cli, "_live_lock", lambda: (lock_path, lock["sha256"]))
    monkeypatch.setattr(live_app, "real_exchange", lambda s: ex)
    monkeypatch.setattr(live_app, "price_feed", lambda s: None)  # no WebSocket in tests
    monkeypatch.setattr(live_app, "update_cache", lambda product, cache_dir: FakeFetch(df, clock)(cache_dir))
    monkeypatch.setattr(cli, "LIVE_REFUSAL_WAIT_S", 0.0)
    return settings, ex, lock


def test_live_parser_has_no_strategy_options():
    ns = build_parser().parse_args(["live", "stage", "canary", "--confirm"])
    assert (ns.cmd, ns.live_cmd, ns.to, ns.confirm) == ("live", "stage", "canary", True)
    for argv in (["live", "run", "--params", "1"], ["live", "run", "--profile", "aggressive"],
                 ["live", "stage", "paper"], ["live", "backup"]):
        with pytest.raises(SystemExit):
            build_parser().parse_args(argv)


def test_the_price_feed_uses_the_public_endpoint_without_a_key():
    feed = live_app.price_feed(LiveSettings())  # no key file: the public ticker and heartbeats need none
    sock = feed.ws_factory(on_message=feed.on_message)  # built, never opened (no network)
    assert feed.products == ["BIP-20DEC30-CDE", "BTC-USD"] and sock.url == "wss://advanced-trade-ws.coinbase.com"


def test_preflight_offline_and_online(live_env, capsys, monkeypatch):
    assert main(["live", "preflight", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "preflight fingerprint: ok" in out and "preflight lock: ok" in out and "key" not in out
    assert main(["live", "preflight"]) == 0
    assert "preflight product: ok" in capsys.readouterr().out
    monkeypatch.setattr(cli, "_live_lock", lambda: (live_env[0].data_dir / "missing.lock", "0" * 64))
    assert main(["live", "preflight", "--offline"]) == 2


def test_run_refuses_without_a_key_file_or_a_passing_preflight(live_env, monkeypatch, capsys):
    def no_key(s):
        raise SettingsError("EWBOT_COINBASE_KEY_FILE is not set")

    monkeypatch.setattr(live_app, "real_exchange", no_key)
    assert main(["live", "run"]) == 2 and "refusing" in capsys.readouterr().err
    settings, ex, lock = live_env
    transfer = SimExchange(permissions=KeyPermissions(True, True, True))
    monkeypatch.setattr(live_app, "real_exchange", lambda s: transfer)
    assert main(["live", "run"]) == 2 and "preflight key: FAILED" in capsys.readouterr().out


def test_run_status_and_backup(live_env, monkeypatch, capsys, tmp_path):
    settings, ex, lock = live_env
    original = LiveRuntime.run_forever
    monkeypatch.setattr(LiveRuntime, "run_forever", lambda self, max_ticks=None: original(self, 2))
    monkeypatch.setattr("ewbot.live.runtime.time.time", lambda: NOW)
    assert main(["live", "run"]) == 0
    jr = Journal(settings.journal_path)
    assert jr.stage()[0] == "SHADOW" and [e["kind"] for e in jr.events(kind="STARTUP")] == ["STARTUP"]
    assert jr.cycles()[-1]["status"] == "OK"
    jr.close()
    capsys.readouterr()
    monkeypatch.setattr("ewbot.cli.time.time", lambda: NOW + 30)
    assert main(["live", "status"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("단계 SHADOW") and "[미충족] 기간 14일 이상" in out
    assert main(["live", "status", "--json", "--exchange", "--max-age", "180"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["stage"] == "SHADOW" and data["exchange_position"] is None and data["heartbeat_age"] == 30.0
    monkeypatch.setattr("ewbot.cli.time.time", lambda: NOW + 400)
    assert main(["live", "status", "--max-age", "180"]) == 1  # the Docker health check fails
    assert main(["live", "backup", "--out", str(tmp_path / "bk" / "j.sqlite")]) == 0
    assert (tmp_path / "bk" / "j.sqlite").exists()


def test_a_fatal_exit_tells_healthchecks_only_the_exception_type_and_stops_the_threads(live_env, monkeypatch, capsys):
    settings, _, _ = live_env
    token = "123456:SECRET-TOKEN"
    s = dataclasses.replace(settings, telegram_token=token, telegram_chat="42", healthcheck_url="https://hc-ping.com/u1")
    monkeypatch.setattr(cli, "_live_settings", lambda: s)
    posts: list[tuple[str, Any]] = []
    pings = SimpleNamespace(get=lambda url, timeout=None: SimpleNamespace(status_code=200),
                            post=lambda url, data=None, timeout=None: posts.append((url, data)) or SimpleNamespace(status_code=200))
    monkeypatch.setattr(live_app, "Heartbeat", lambda url: Heartbeat(url, session=pings))
    sender = FakeSender()

    def started(_s: LiveSettings) -> LiveNotifier:  # the real notifier on a fake Telegram
        n = LiveNotifier(sender)
        n.start()
        return n

    monkeypatch.setattr(live_app, "notifier", started)

    class Control:  # no Telegram long poll in tests
        made: list[Any] = []

        def __init__(self, token: str, chat: str, journal: Journal) -> None:
            self.running = False
            Control.made.append(self)

        def start(self, commands: Any) -> None:
            self.running = True

        def stop(self) -> None:
            self.running = False

    monkeypatch.setattr(live_app, "TelegramControl", Control)
    watched: list[Any] = []

    def crash(self: LiveRuntime, max_ticks: int | None = None) -> int:
        watched.append(self.d.control)
        assert self.d.redact(f"bot{token} at https://hc-ping.com/u1") == "bot<secret> at <secret>"  # loop errors too
        raise RuntimeError(f"connect https://api.telegram.org/bot{token}/getUpdates failed")

    monkeypatch.setattr(LiveRuntime, "run_forever", crash)
    capsys.readouterr()
    assert main(["live", "run"]) == 1  # Docker starts it again
    err = capsys.readouterr().err
    assert "Traceback" in err and "bot<secret>/getUpdates" in err and token not in err  # the traceback, redacted
    assert posts == [("https://hc-ping.com/u1/fail", "[LIVE] 중단: RuntimeError".encode())]  # never the exception's text
    assert watched == Control.made and not Control.made[0].running  # the runtime watches it; stopped on the way out
    assert sender.texts == ["[LIVE] [중단] RuntimeError: connect https://api.telegram.org/bot<secret>/getUpdates failed"]


def test_status_without_a_journal(live_env, capsys):
    assert main(["live", "status"]) == 1 and "no journal" in capsys.readouterr().err
    assert main(["live", "status", "--json", "--exchange", "--require-flat"]) == 0  # first deployment
    assert json.loads(capsys.readouterr().out)["stage"] is None


def test_require_flat_sees_the_journal_trade_and_the_account(live_env, capsys):
    settings, ex, _ = live_env
    jr = Journal(settings.journal_path)
    jr.put("trade", {"status": "OPEN", "trade_id": 1})
    jr.close()
    assert main(["live", "status", "--json", "--require-flat"]) == 3
    jr = Journal(settings.journal_path)
    jr.put("trade", {"status": "CLOSED", "trade_id": 1})
    jr.close()
    assert main(["live", "status", "--json", "--require-flat"]) == 0
    ex.place_market("manual", 1, 1)
    assert main(["live", "status", "--json", "--exchange", "--require-flat"]) == 3
    assert "not flat" in capsys.readouterr().err


def test_stage_changes_need_confirm_gates_and_a_flat_book(live_env, capsys):
    settings, ex, _ = live_env
    jr = Journal(settings.journal_path)
    jr.set_stage("SHADOW", "initial", NOW - 86_400)
    jr.close()
    assert main(["live", "stage", "canary"]) == 2
    res = capsys.readouterr()
    assert "[미충족] ① 판단이 모의매매와 100% 일치" in res.out and "--confirm" in res.err
    assert main(["live", "stage", "canary", "--confirm"]) == 2 and "gates not met" in capsys.readouterr().err
    jr = Journal(settings.journal_path)
    jr.set_stage("CANARY", "test", NOW)
    jr.put("audit", AuditResult.failed("paper ledger fetch failed: HTTP 502", NOW).to_json())
    jr.close()
    assert main(["live", "status"]) == 0  # the CANARY gates read the runtime's last audit: a failed one is unmet
    assert "[미충족] 모의매매 대조 (최근 감사 통과): 감사 실패: paper ledger fetch failed: HTTP 502" in capsys.readouterr().out
    assert main(["live", "stage", "full", "--confirm"]) == 2 and "gates not met" in capsys.readouterr().err
    ex.place_market("manual", 1, 1)
    assert main(["live", "stage", "shadow", "--confirm"]) == 2 and "flat" in capsys.readouterr().err
    ex.close_position("manual-x")
    assert main(["live", "stage", "shadow", "--confirm"]) == 0
    jr = Journal(settings.journal_path)
    assert jr.stage()[0] == "SHADOW"
    jr.close()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --group live pytest tests/live/test_cli_live.py -q
```

Expected: collection error `ImportError: cannot import name 'app' from 'ewbot.live'`.

- [ ] **Step 3: Write minimal implementation**

**File: `src/ewbot/live/app.py`**

```python
"""Production wiring of `ewbot live` (Plan 3): settings -> Coinbase exchange (ShadowExchange in SHADOW),
journal, [LIVE] notifier, WebSocket price feed (Coinbase's public endpoint: no key), Telegram control,
heartbeat and paper audit -> LiveRuntime. The CLI calls these factories; tests replace `real_exchange`,
`price_feed` and `paper_status` (no network)."""

from __future__ import annotations

import queue
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ewbot.data.coinbase_history import update_cache
from ewbot.live.audit import AuditError, fetch_paper_ledger
from ewbot.live.coinbase import CoinbaseExchange
from ewbot.live.control import Command, TelegramControl
from ewbot.live.exchange import Exchange
from ewbot.live.feed import TickerFeed, public_ws_factory
from ewbot.live.health import Heartbeat
from ewbot.live.journal import Journal
from ewbot.live.notify import LiveNotifier
from ewbot.live.runtime import LiveDeps, LiveRuntime
from ewbot.live.settings import LiveSettings, SettingsError
from ewbot.live.shadow import ShadowExchange
from ewbot.paper.criteria import golive_status
from ewbot.paper.ledger import Ledger, LedgerError
from ewbot.paper.notify import TelegramSender


def real_exchange(s: LiveSettings) -> Exchange:
    if s.key_file is None:
        raise SettingsError("EWBOT_COINBASE_KEY_FILE is not set")
    return CoinbaseExchange.from_key_file(s.key_file, s.futures_id, s.spot_id)


def stage_exchange(real: Exchange, journal: Journal, stage: str, notify: Callable[[str, str], Any]) -> Exchange:
    """SHADOW trades virtually on real prices (previews only); CANARY and FULL trade for real."""
    return ShadowExchange(real, journal, alert=notify) if stage == "SHADOW" else real


def notifier(s: LiveSettings) -> LiveNotifier:
    """The [LIVE] notifier with its sending thread started: a call only queues, the loop never waits on Telegram."""
    sender = TelegramSender(s.telegram_token, s.telegram_chat) if s.telegram_token and s.telegram_chat else None
    notify = LiveNotifier(sender)
    notify.start()
    return notify


def redact(text: str, s: LiveSettings) -> str:
    """`text` with the settings' secrets (bot token, GitHub token, health-check URL) replaced by <secret>."""
    for secret in (s.telegram_token, s.github_token, s.healthcheck_url):
        if secret:
            text = text.replace(secret, "<secret>")
    return text


def fatal_report(exc: BaseException, s: LiveSettings) -> str:
    """A fatal exception's traceback for stderr, `redact`ed."""
    return redact("".join(traceback.format_exception(exc)), s)


def paper_fetcher(s: LiveSettings) -> Callable[[], dict[str, Any]] | None:
    if not (s.paper_repo and s.github_token):
        return None
    repo, token = s.paper_repo, s.github_token
    return lambda: fetch_paper_ledger(repo, token)


def paper_status(s: LiveSettings, now: float) -> dict[str, Any] | None:
    """The paper runtime's go-live status (criteria 1-4) from its ledger, or None when unreadable."""
    fetch = paper_fetcher(s)
    if fetch is None:
        return None
    try:
        return golive_status(Ledger.from_dict(fetch()), int(now))
    except (AuditError, LedgerError, KeyError, TypeError, ValueError) as exc:
        print(f"paper ledger unreadable: {exc}", file=sys.stderr)
        return None


def price_feed(s: LiveSettings) -> TickerFeed | None:
    """The WebSocket ticker feed for the watchdog (public channels: no key, verified TLS)."""
    return TickerFeed([s.futures_id, s.spot_id], public_ws_factory())


@dataclass
class Wired:
    runtime: LiveRuntime
    feed: TickerFeed | None
    control: TelegramControl | None
    heartbeat: Heartbeat
    notify: LiveNotifier

    def close(self, flush_s: float = 10.0) -> None:
        """Shutdown: stop the Telegram poller (its long poll ends with the process: a command fetched but not yet
        handled is lost - at most once) and the price feed, then give the queued messages up to flush_s to go out."""
        if self.control is not None:
            self.control.stop()
        if self.feed is not None:
            self.feed.stop()
        self.notify.flush(flush_s)
        self.notify.stop()


def build(s: LiveSettings, lock: dict[str, Any], real: Exchange, journal: Journal,
          notify: LiveNotifier | None = None) -> Wired:
    notify = notifier(s) if notify is None else notify
    stage, _ = journal.stage()
    ex = stage_exchange(real, journal, stage, notify)
    feed = price_feed(s)
    commands: queue.Queue[Command] = queue.Queue()
    control = (TelegramControl(s.telegram_token, s.telegram_chat, journal)
               if s.telegram_token and s.telegram_chat else None)
    heartbeat = Heartbeat(s.healthcheck_url)
    deps = LiveDeps(
        journal=journal, exchange=ex, lock=lock,
        fetch=lambda cache_dir: update_cache(s.spot_id, Path(cache_dir)), cache_dir=s.cache_dir, notify=notify,
        clock=time.time, sleep=time.sleep, feed=feed, heartbeat=heartbeat, commands=commands, control=control,
        paper_ledger=paper_fetcher(s), edit_stops=s.edit_stops, expiry=s.expiry,
        redact=lambda text: redact(text, s),
    )
    rt = LiveRuntime(deps)
    if feed is not None:
        feed.start()
    if control is not None:
        control.start(commands)
    return Wired(rt, feed, control, heartbeat, notify)
```

Apply these six replacements to `src/ewbot/cli.py` (each `replace` text occurs exactly once):

**Edit `src/ewbot/cli.py` (1/6) — replace:**

```python
"""`ewbot` command line: fetch | backtest | compare | walkforward | holdout | paper.

Holdout isolation: every research command (backtest, compare, walkforward) cuts the candle
frame to ts < HOLDOUT_START (2024-01-01) right after loading it, before any engine run, benchmark or
report candle sees it, and an explicit --start at/after HOLDOUT_START or --end after it is refused
(exit 2). runner.run_backtest and runner.compare refuse the holdout on their own as well (defence in
depth). `holdout` is the one-shot evaluation. `paper run` (Plan 2) works on the period AFTER the
holdout: it reads the params from the committed holdout.lock only and has no strategy or config
options (ewbot.paper.runner)."""

```

**with:**

```python
"""`ewbot` command line: fetch | backtest | compare | walkforward | holdout | paper | live.

Holdout isolation: every research command (backtest, compare, walkforward) cuts the candle
frame to ts < HOLDOUT_START (2024-01-01) right after loading it, before any engine run, benchmark or
report candle sees it, and an explicit --start at/after HOLDOUT_START or --end after it is refused
(exit 2). runner.run_backtest and runner.compare refuse the holdout on their own as well (defence in
depth). `holdout` is the one-shot evaluation. `paper run` (Plan 2) works on the period AFTER the
holdout: it reads the params from the committed holdout.lock only and has no strategy or config
options (ewbot.paper.runner). `live run | preflight | status | stage | backup` (Plan 3) runs and inspects
the live-trading runtime (ewbot.live); like `paper` it has no strategy or config options."""

```

**Edit `src/ewbot/cli.py` (2/6) — replace:**

```python
from ewbot.backtest.holdout import HoldoutError, run_holdout
```

**with:**

```python
from ewbot.backtest.holdout import DEFAULT_LOCK_PATH, HoldoutError, run_holdout
```

**Edit `src/ewbot/cli.py` (3/6) — replace:**

```python
from ewbot.data.coinbase_history import update_cache
```

**with:**

```python
from ewbot.data.coinbase_history import update_cache
from ewbot.live import app as live_app
from ewbot.live import runtime as live_runtime
from ewbot.live.exchange import ExchangeError
from ewbot.live.journal import Journal
from ewbot.live.preflight import LOCK_SHA256, run_preflight
from ewbot.live.settings import LiveSettings, SettingsError, load_settings
from ewbot.live.stage import (
    Gate,
    StageError,
    canary_entry_gates,
    canary_gates,
    full_entry_gates,
    gate_lines,
    shadow_gates,
    transition,
)
```

**Edit `src/ewbot/cli.py` (4/6) — replace:**

```python
    p_status.add_argument("--ledger-dir", type=Path, required=True)
    return parser
```

**with:**

```python
    p_status.add_argument("--ledger-dir", type=Path, required=True)

    p_live = sub.add_parser("live", help="live-trading runtime (Plan 3): Coinbase CFM BIP, SHADOW -> CANARY -> FULL")
    live_sub = p_live.add_subparsers(dest="live_cmd", required=True)
    live_sub.add_parser("run", help="run the live runtime (settings and secrets from the environment)")
    p_pre = live_sub.add_parser("preflight", help="startup checks: fingerprint, holdout.lock, key permissions, product")
    p_pre.add_argument("--offline", action="store_true", help="fingerprint and lock only (no Coinbase request)")
    p_ls = live_sub.add_parser("status", help="journal status and the current stage's gates (read-only)")
    p_ls.add_argument("--json", action="store_true", help="machine-readable (deploy/live/deploy.sh)")
    p_ls.add_argument("--max-age", type=float, default=None, help="exit 1 when the loop heartbeat is older (seconds)")
    p_ls.add_argument("--exchange", action="store_true", help="also read the Coinbase position and open orders")
    p_ls.add_argument("--require-flat", action="store_true",
                      help="exit 3 unless no trade is open and (with --exchange) the account is flat (deploy.sh)")
    p_stage = live_sub.add_parser("stage", help="change the operating stage (promotions need every gate met)")
    p_stage.add_argument("to", choices=("shadow", "canary", "full"))
    p_stage.add_argument("--confirm", action="store_true", help="required: the user's explicit approval")
    p_bk = live_sub.add_parser("backup", help="consistent copy of the journal (SQLite online backup)")
    p_bk.add_argument("--out", type=Path, required=True)
    return parser
```

**Edit `src/ewbot/cli.py` (5/6) — replace:**

```python
_COMMANDS = {
```

**with:**

```python
LIVE_REFUSAL_WAIT_S = 600.0  # a refused start waits before exiting: Docker restarts it, alerts stay sparse


def _live_settings() -> LiveSettings:
    """Production settings from the environment and the secrets files (tests replace this function)."""
    return load_settings()


def _live_lock() -> tuple[Path, str]:
    """The committed holdout lock and its pinned sha256 (tests replace this function)."""
    return DEFAULT_LOCK_PATH, LOCK_SHA256


def _print_checks(checks: list) -> bool:
    for c in checks:
        print(f"preflight {c.name}: {'ok' if c.ok else 'FAILED'} - {c.detail}")
    return all(c.ok for c in checks)


def _live_run(s: LiveSettings) -> int:
    journal = Journal(s.journal_path)
    stage, _ = journal.stage()
    notify = live_app.notifier(s)
    try:
        real = live_app.real_exchange(s)
    except SettingsError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        notify("PREFLIGHT", f"[시작 거부] {exc}")
        time.sleep(LIVE_REFUSAL_WAIT_S)
        return 2
    path, sha = _live_lock()
    checks, lock = run_preflight(real, stage, path, expected_lock=sha)
    if not _print_checks(checks) or lock is None:
        notify("PREFLIGHT", "[시작 거부] 시작 점검 실패: " + "; ".join(f"{c.name}: {c.detail}" for c in checks if not c.ok))
        time.sleep(LIVE_REFUSAL_WAIT_S)
        return 2
    wired = live_app.build(s, lock, real, journal, notify)
    try:
        return wired.runtime.run_forever()
    except BaseException as exc:
        wired.heartbeat.fail(f"[LIVE] 중단: {type(exc).__name__}")  # a third party: the type name only, never the text
        notify("FATAL", f"[중단] {type(exc).__name__}: {live_app.redact(str(exc), s)[:300]}")
        if not isinstance(exc, Exception):
            raise  # KeyboardInterrupt, SystemExit
        print(live_app.fatal_report(exc, s), file=sys.stderr, end="")  # re-raised, Python would print it unredacted
        return 1
    finally:
        wired.close()  # stops the Telegram poller and the feed, lets the queued messages go out


def _live_preflight(s: LiveSettings, args: argparse.Namespace) -> int:
    stage = "SHADOW"
    if s.journal_path.exists():
        journal = Journal(s.journal_path)
        stage = journal.stage()[0]
        journal.close()
    real = None
    if not args.offline:
        try:
            real = live_app.real_exchange(s)
        except SettingsError as exc:
            print(f"refusing: {exc}", file=sys.stderr)
            return 2
    path, sha = _live_lock()
    checks, _ = run_preflight(real, stage, path, expected_lock=sha)
    return 0 if _print_checks(checks) else 2


def _live_gates(journal: Journal, stage: str, now: float) -> list[Gate]:
    audit = live_runtime.audit_from_journal(journal)  # the runtime's last audit, a failed one included
    if stage == "SHADOW":
        return shadow_gates(journal, now, audit)
    if stage == "CANARY":
        return canary_gates(journal, now, audit)
    return []


def _live_status(s: LiveSettings, args: argparse.Namespace) -> int:
    """Journal status (no journal yet: --json still answers, stage null). Exit 1 when --max-age is exceeded
    (Docker HEALTHCHECK), 3 when --require-flat finds an open trade, position or resting order."""
    if not s.journal_path.exists() and not args.json:
        print(f"no journal at {s.journal_path}", file=sys.stderr)
        return 1
    journal = Journal(s.journal_path) if s.journal_path.exists() else None
    try:
        now = time.time()
        stage, since = journal.stage() if journal is not None else (None, None)
        hb = journal.get("heartbeat") if journal is not None else None
        age = None if hb is None else now - float(hb)
        trade = journal.get("trade") if journal is not None else None
        out: dict[str, Any] = {"stage": stage, "since": since, "heartbeat_age": age,
                               "paused": journal.get("paused") if journal is not None else None,
                               "trade": trade if trade and trade.get("status") == "OPEN" else None}
        if args.exchange:
            real = live_app.real_exchange(s)
            pos = real.position()
            out["exchange_position"] = None if pos is None else {"side": pos.side, "contracts": pos.contracts}
            out["exchange_open_orders"] = len(real.open_orders())
        if args.json:
            print(json.dumps(out, sort_keys=True))
        elif journal is not None:
            assert stage is not None
            for line in live_runtime.status_lines(journal, now) + gate_lines(_live_gates(journal, stage, now)):
                print(line)
            if args.exchange:
                print(f"exchange position: {out['exchange_position']}, open orders: {out['exchange_open_orders']}")
        if args.require_flat and (out["trade"] is not None or out.get("exchange_position") is not None
                                  or out.get("exchange_open_orders", 0) > 0):
            print("not flat: an open trade, position or resting order (flatten first: /flatten)", file=sys.stderr)
            return 3
        return 1 if args.max_age is not None and (age is None or age > args.max_age) else 0
    finally:
        if journal is not None:
            journal.close()


def _live_stage(s: LiveSettings, args: argparse.Namespace) -> int:
    journal = Journal(s.journal_path)
    try:
        now = time.time()
        to = args.to.upper()
        cur, _ = journal.stage()
        trade = journal.get("trade")
        flat = not (trade and trade.get("status") == "OPEN")
        try:  # the real account must be flat too, and its key is a CANARY / FULL gate
            real = live_app.real_exchange(s)
            perms = real.key_permissions()
            flat = flat and real.position() is None and not real.open_orders()
        except (SettingsError, ExchangeError) as exc:
            print(f"refusing: {exc}", file=sys.stderr)
            return 2
        gates: list[Gate] = []
        audit = live_runtime.audit_from_journal(journal)
        if (cur, to) == ("SHADOW", "CANARY"):
            gates = canary_entry_gates(journal, now, audit, live_app.paper_status(s, now), perms)
        elif (cur, to) == ("CANARY", "FULL"):
            gates = full_entry_gates(journal, now, audit, live_app.paper_status(s, now), perms)
        for line in gate_lines(gates):
            print(line)
        try:
            transition(journal, to, now, gates, confirm=args.confirm, flat=flat)
        except StageError as exc:
            print(f"refusing: {exc}", file=sys.stderr)
            return 2
        print(f"stage {cur} -> {to} recorded; the running runtime restarts into it once flat")
        return 0
    finally:
        journal.close()


def _live_backup(s: LiveSettings, args: argparse.Namespace) -> int:
    journal = Journal(s.journal_path)
    try:
        print(f"journal backup: {journal.backup(args.out)}")
    finally:
        journal.close()
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    try:
        s = _live_settings()
    except SettingsError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    if args.live_cmd == "run":
        return _live_run(s)
    handlers = {"preflight": _live_preflight, "status": _live_status, "stage": _live_stage, "backup": _live_backup}
    return handlers[args.live_cmd](s, args)


_COMMANDS = {
```

**Edit `src/ewbot/cli.py` (6/6) — replace:**

```python
    "paper": cmd_paper,
```

**with:**

```python
    "paper": cmd_paper,
    "live": cmd_live,
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run --group live pytest tests/live/test_cli_live.py tests/paper/test_cli_paper.py tests/backtest/test_cli.py -q
uv run ewbot live --help
```

Expected: `35 passed` (9 new, 6 paper CLI, 20 research CLI); the help lists `run, preflight, status, stage, backup`.

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/live/app.py src/ewbot/cli.py tests/live/test_cli_live.py
git commit -m "$(cat <<'EOF'
feat(cli): ewbot live run | preflight | status | stage | backup with production wiring

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 14: Parity and chaos integration tests

**Files:**
- Test: `tests/live/test_parity.py`, `tests/live/test_chaos.py`

**Interfaces:**
- Consumes: `tests.live.harness.Harness` (Task 12), `LiveRuntime`, `SimExchange` / `SimCrash`, `TickerFeed`, `Journal`, `traced_with_exits`, `compute_target`, `build_decisions`, `same_decision`, `map_level`, `tests.paper.helpers.make_lock`, `ewbot.engine.loop._Run` (swapped by one test to queue an exit on a chosen bar).
- Produces: no production code. This task is the spec §7 `일치 테스트` and `장애 주입` coverage over Tasks 1–13: three hour-by-hour walks of the synthetic series in the first 30 days of FULL (3 contracts, basis ratio 1.0005) — a TIME exit, a trailed stop-out and TP1 → break-even → TP2 — must record the paper replay's decisions exactly, enter on the engine's signals, place the mapped engine levels, move the stop with the engine and exit in the engine's exit hour; and a process killed right after an accepted entry, bracket or close, a WebSocket drop and sequence gap, 429s and outages, a stop move while the quotes cannot be read (sent at the first poll with quotes, never lost), a 429 storm during a stop move, a close that keeps failing and a venue that takes cancels but refuses every new order (the resting legs protect the position at every poll: never cancelled before the venue took the replacement or the close), an entry whose answer and lookups are lost (protected at once by a provisional trade from its intent, settled as ours or as a foreign position: a CANARY incident), an exit waiting for the session to reopen, an exit queued before the weekly CFM halt (still the target at the halt bar's cycle) and a stop the spot price crossed during the halt must all end protected, flat or reconciled without a duplicate order — the last two closed by the first poll that sees the session OPEN, not an hour later.

- [ ] **Step 1: Write the failing test**

**File: `tests/live/test_parity.py`**

```python
"""Plan 3 spec 7 `일치 테스트`: synthetic hourly bars through the live loop and the SimExchange; the entry
decisions, stops, TPs and exits must match the paper replay of the same bars.

Three walks cover every exit path of the strategy's trades on the synthetic series: a short closed by
its TIME stop (exit queued at a close, executed by the runtime), a short with a trailing stop that is
stopped out (bracket), and a long with a trailing stop, TP1 + break-even and TP2 (brackets). The basis
ratio is 1.0005, so every futures level is the spot level x 1.0005 on the $5 tick. The walks run in the
first 30 days of FULL (3 contracts: a 1-contract TP1 leg and a 2-contract TP2 leg), so the live trade
follows the engine's TP1 / break-even / TP2 path; a 1-contract CANARY trade exits whole at TP1."""

from __future__ import annotations

import pytest

from ewbot.backtest.holdout import holdout_config
from ewbot.backtest.runner import run_backtest
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.config import BotConfig
from ewbot.live.pricing import map_level
from ewbot.live.target import traced_with_exits
from ewbot.paper.decisions import build_decisions, same_decision
from ewbot.types import H1
from tests.live.harness import Harness
from tests.paper.helpers import PARAMS, synth_frame, ts

RATIO = 1.0005
WALKS = {
    "time_exit": (ts("2024-01-01 18:00"), ts("2024-01-04 19:00")),
    "trailed_stop": (ts("2024-02-15 23:00"), ts("2024-02-18 20:00")),
    "tp1_tp2": (ts("2024-03-13 20:00"), ts("2024-03-16 21:00")),
}


@pytest.fixture(scope="module")
def reference():
    """The paper runtime's view: one replay over every synthetic bar."""
    df = synth_frame()
    cfg = holdout_config(BotConfig(), PARAMS)
    with traced_with_exits() as (trace, _, _):
        res = run_backtest(cfg, df, HOLDOUT_START, None, allow_holdout=True)
    return res, trace, build_decisions(res, trace, list(res.trades))


@pytest.fixture(scope="module", params=sorted(WALKS))
def walked(request, tmp_path_factory, reference):
    start, end = WALKS[request.param]
    h = Harness(tmp_path_factory.mktemp(request.param), start + 30, stage="FULL", stage_days=10, ratio=RATIO)
    h.price(float(h.bar(start)["open"]))
    h.rt.startup()
    h.walk(start, end)
    yield request.param, h, start, end, reference
    h.journal.close()


def test_recorded_decisions_equal_the_paper_replay(walked):
    _, h, start, end, (res, trace, decisions) = walked
    last_bar = end - 2 * H1  # the last cycle ran at end - 1h + 30 s for the bar end - 2h
    want = {d["id"]: d for d in decisions if d["ts"] <= last_bar}
    got = {d["id"]: d for d in h.journal.decisions()}
    assert sorted(got) == sorted(want) and len(want) >= 3  # at least SIGNAL, ENTRY and the exit
    assert all(same_decision(got[k], want[k]) for k in want)


def test_entries_follow_the_engine_signals(walked):
    _, h, start, end, (res, trace, _) = walked
    signals = [o for o in trace.orders if start - H1 <= o.ts <= end - 2 * H1]
    entries = [e["data"] for e in h.journal.events(kind="ENTRY")]
    assert [(e["trade_id"], e["side"], e["contracts"]) for e in entries] == [(o.ts, o.direction, 3) for o in signals]
    assert len(entries) == 1  # one trade per walk


def test_stops_and_take_profits_are_the_mapped_engine_levels(walked):
    name, h, start, end, (res, trace, _) = walked
    tr = h.rt.executor.trade()
    engine = next(t for t in res.trades if t.entry_ts == tr.trade_id + H1)
    d = engine.direction
    assert tr.tp1_price == map_level(engine.tp1, RATIO, 5.0, d) and tr.tp2_price == map_level(engine.tp2, RATIO, 5.0, d)
    moves = [s for s in trace.stops if s.trade_key == engine.entry_ts and s.ts <= end - 2 * H1]
    live_moves: list[float] = []
    for e in h.journal.events(kind="STOP_MOVED"):  # the live TP1 moves the stop before the engine's close does
        if not live_moves or live_moves[-1] != e["data"]["stop_spot"]:
            live_moves.append(e["data"]["stop_spot"])
    assert live_moves == [s.new for s in moves]
    first = h.journal.events(kind="PROTECTED_FIRST")[0]["data"]
    assert first["after_entry_s"] <= 30.0
    if name == "tp1_tp2":
        assert [s.kind for s in moves] == ["TRAIL", "BREAKEVEN"] and tr.tp1_filled


def test_exits_happen_in_the_engine_exit_hour(walked):
    name, h, start, end, (res, trace, _) = walked
    tr = h.rt.executor.trade()
    engine = next(t for t in res.trades if t.entry_ts == tr.trade_id + H1)
    closed = [e for e in h.journal.events(kind="CLOSED") if e["data"]["trade_id"] == tr.trade_id]
    assert tr.status == "CLOSED" and len(closed) == 1 and h.ex.position() is None and h.ex.open_orders() == []
    when = closed[0]["ts"]
    assert engine.exit_ts <= when < engine.exit_ts + H1, (name, engine.exit_reason)
    expected = {"time_exit": "TIME", "trailed_stop": "BRACKET", "tp1_tp2": "BRACKET"}[name]
    assert closed[0]["data"]["reason"] == expected
    assert engine.exit_reason == {"time_exit": "TIME", "trailed_stop": "STOP", "tp1_tp2": "TP2"}[name]
```

**File: `tests/live/test_chaos.py`**

```python
"""Plan 3 spec 7 `장애 주입`: a process killed in the middle of an order, restart reconciliation, WebSocket
drop and sequence gaps (REST fallback and resync), 429s and outages, a session closure with an exit
waiting for the reopen, and the weekly CFM halt (an exit queued before it and a stop the spot price
crossed during it close at the reopen, not an hour later). The process is "killed" by SimCrash (a
BaseException nothing in the runtime catches) right after the venue accepted an order; the restart is a
new LiveRuntime on the same journal file and the same exchange."""

from __future__ import annotations

import json

import pytest

import ewbot.engine.loop as loop_mod
from ewbot.live.exchange import PlaceResult, RateLimited, Unavailable
from ewbot.live.feed import TickerFeed
from ewbot.live.journal import Journal
from ewbot.live.sim import SimCrash
from ewbot.live.stage import paused
from ewbot.live.target import compute_target
from tests.live.harness import Harness
from tests.paper.helpers import make_lock, synth_frame, ts

SIGNAL = ts("2024-03-13 22:00")
ENTRY_BAR = SIGNAL + 3600
TIME_SIGNAL = ts("2024-01-01 19:00")  # W5_REV short whose TIME exit is queued at 2024-01-04 15:00
LONG_ENTRY = ts("2024-01-12 15:00")  # a W3 long still open at the CFM halt below
HALT = ts("2024-01-12 22:00")  # Fri 17:00 New York (EST): the weekly CFM halt bar


def started(tmp_path, start: float = ENTRY_BAR + 30, **kw) -> Harness:
    h = Harness(tmp_path, int(start), **kw)
    h.price(float(h.bar(int(start) // 3600 * 3600)["open"]))
    h.rt.startup()
    return h


def restart(h: Harness, now: float) -> None:
    h.journal.close()
    h.journal = Journal(h.tmp / "journal.sqlite")
    h.clock.now = now
    h.rt = h.make_runtime()
    h.rt.startup()


@pytest.mark.parametrize("method", ["place_market", "place_bracket"])
def test_killed_right_after_an_order_was_accepted(tmp_path, method):
    h = started(tmp_path, stage="FULL", stage_days=10)
    h.ex.crash_after_accept(method)
    with pytest.raises(SimCrash):
        h.at(ENTRY_BAR + 30)
    assert h.journal.intents(status="PENDING")  # the journal never heard back
    restart(h, ENTRY_BAR + 50)
    h.at(ENTRY_BAR + 51)  # the interrupted cycle runs again
    h.at(ENTRY_BAR + 70)
    assert h.ex.calls.count("place_market") == 1 and h.ex.position().contracts == 3
    rest = sorted((o.size, o.stop_price) for o in h.ex.open_orders())
    assert rest == [(1, 26_585.0), (2, 26_585.0)]  # exactly the two legs: a stray bracket is cancelled
    assert not h.journal.intents(status="PENDING")
    assert h.rt.executor.trade().trade_id == SIGNAL


def test_killed_right_after_the_close_was_accepted(tmp_path):
    h = started(tmp_path)
    h.at(ENTRY_BAR + 30)
    h.ex.crash_after_accept("close_position")
    h.rt.handle("/flatten", ENTRY_BAR + 40)
    with pytest.raises(SimCrash):
        h.rt.handle("/flatten_confirm", ENTRY_BAR + 41)
    assert h.ex.position() is None and h.rt.executor.trade().status == "OPEN"  # the journal never heard back
    restart(h, ENTRY_BAR + 60)
    tr = h.rt.executor.trade()
    assert (tr.status, tr.exit_reason) == ("CLOSED", "USER_FLATTEN") and h.ex.calls.count("close_position") == 1
    assert h.journal.intent(f"ewb-x-{SIGNAL}-1")["status"] == "ACCEPTED"


def ticker(product: str, price: float, seq: int) -> str:
    return json.dumps({"channel": "ticker", "sequence_num": seq,
                       "events": [{"tickers": [{"product_id": product, "price": str(price)}]}]})


def test_websocket_drop_falls_back_to_rest_polling_and_a_gap_forces_a_resync(tmp_path):
    h = started(tmp_path)
    feed = TickerFeed([h.ex.futures_id], ws_factory=lambda **kw: None, clock=h.clock)
    h.rt = h.make_runtime(feed=feed)
    h.rt.startup()
    feed.on_message(ticker(h.ex.futures_id, 26_744.5, 1))
    h.at(ENTRY_BAR + 30)
    assert h.rt.active
    quotes = h.ex.calls.count("quote")
    h.at(ENTRY_BAR + 31)
    assert h.rt.prices.mode == "ws" and h.ex.calls.count("quote") == quotes  # the fresh feed is used
    h.at(ENTRY_BAR + 95)  # 65 s without a message: REST
    assert h.rt.prices.mode == "rest"
    polls = len([c for c in h.ex.calls if c == "position"])
    feed.on_message(ticker(h.ex.futures_id, 26_744.5, 2))
    feed.on_message(ticker(h.ex.futures_id, 26_744.5, 9))  # 3..8 lost
    h.at(ENTRY_BAR + 96)  # a resync request polls at once, before the 10 s poll interval
    assert len([c for c in h.ex.calls if c == "position"]) > polls and feed.gaps == 1


def test_rate_limits_and_outages_never_stop_the_loop(tmp_path):
    h = started(tmp_path)
    h.ex.fail_next("place_market", RateLimited("429"))
    h.at(ENTRY_BAR + 30)
    assert h.ex.position() is None and h.journal.intent(f"ewb-e-{SIGNAL}-L")["status"] == "PENDING"
    h.at(ENTRY_BAR + 45)  # next poll: looked up (not there), sent again with the same id
    assert h.ex.position().contracts == 1 and h.ex.calls.count("place_market") == 2
    h.ex.fail_next("position", Unavailable("503"), times=2)
    h.at(ENTRY_BAR + 60)
    assert h.journal.events(kind="POLL_ERROR")
    h.at(ENTRY_BAR + 75)
    assert h.ex.position().contracts == 1 and h.rt.executor.trade().status == "OPEN"


def test_a_stop_move_during_a_quote_outage_goes_out_with_the_quotes(tmp_path):
    """The engine trails the stop at a close while the quotes cannot be read: the stop in force stays (no
    ratio to map the new one) and the move goes out at the first poll with quotes - never lost."""
    trail = ts("2024-03-14 07:00")  # the engine trails the long's stop at this close
    h = started(tmp_path)
    h.walk(ENTRY_BAR, trail + 3600)
    before = h.rt.executor.trade().stop_spot
    h.price(float(h.bar(trail + 3600)["open"]))
    h.ex.fail_next("quote", Unavailable("503"), times=50)
    h.at(trail + 3600 + 30)  # the cycle sees the new engine stop; every quote fails
    h.ex._faults.clear()
    assert h.rt.target.position.stop != before and h.rt.executor.trade().stop_spot == before
    h.at(trail + 3600 + 45)  # the next poll has quotes
    tr = h.rt.executor.trade()
    assert tr.stop_spot == h.rt.target.position.stop
    assert h.journal.events(kind="STOP_MOVED")[-1]["data"]["stop_spot"] == tr.stop_spot


def covered(h: Harness) -> bool:
    """Our resting brackets cover the whole position."""
    pos = h.ex.position()
    return pos is not None and sum(o.size - o.filled for o in h.ex.open_orders() if o.stop_price) >= pos.contracts


def test_a_429_storm_during_a_stop_move_keeps_the_position_protected(tmp_path):
    """Every bracket request is rate-limited while the engine trails the stop: the replacement's outcome is
    unknown (never a refusal), so the resting leg keeps protecting - it is never cancelled first - and the stop in
    force stays recorded; the move goes out at the first poll the venue answers."""
    trail = ts("2024-03-14 07:00")  # the engine trails the long's stop at this close
    h = started(tmp_path)
    h.walk(ENTRY_BAR, trail + 3600)
    before = h.rt.executor.trade().stop_spot
    h.price(float(h.bar(trail + 3600)["open"]))
    h.ex.fail_next("place_bracket", RateLimited("429"), times=50)
    t0 = trail + 3600 + 30
    for k in range(4):  # the cycle sees the new engine stop, then three polls into the storm
        h.at(t0 + 10 * k)
        assert covered(h) and h.rt.executor.trade().stop_spot == before
    assert h.journal.events(kind="BRACKET_FAILED", since=t0) and not h.journal.events(kind="LEG_GAP")
    h.ex._faults.clear()
    h.at(t0 + 40)
    tr = h.rt.executor.trade()
    assert tr.stop_spot == h.rt.target.position.stop != before
    assert [o.stop_price for o in h.ex.open_orders()] == [tr.stop_price] and covered(h)
    assert not h.journal.events(kind="UNPROTECTED", since=t0)
    # the first failed move was not replaced within 30 s: a CANARY failure (spec 6.2), entries paused
    assert [e["data"]["kind"] for e in h.journal.events(kind="ORDER_UNRESOLVED")] == ["BRACKET_FAILED"]
    assert paused(h.journal)["reason"] == "incident ORDER_UNRESOLVED"


def test_a_close_that_keeps_failing_keeps_the_position_protected(tmp_path):
    """The engine queues the TIME exit and the venue rate-limits the close three times. Each close cancelled the
    brackets first; each failure puts them back at the stop in force, so no poll ever finds the position
    unprotected, and the fourth close goes through with the engine's reason."""
    exit_bar = ts("2024-01-04 15:00")  # the engine queues the TIME exit at this close
    h = started(tmp_path, start=TIME_SIGNAL + 3600 + 30)
    h.at(TIME_SIGNAL + 3600 + 30)
    assert h.ex.position().side == -1
    h.price(float(h.bar(exit_bar + 3600)["open"]))
    h.ex.fail_next("close_position", RateLimited("429"), times=3)
    t0 = exit_bar + 3600 + 30
    for k in range(3):
        h.at(t0 + 10 * k)
        assert covered(h)
    h.at(t0 + 30)
    assert h.ex.position() is None and h.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "TIME"
    assert len(h.journal.events(kind="CLOSE_FAILED")) == 3 and not h.journal.events(kind="UNPROTECTED", since=t0)
    # a close not done by the next pass is a CANARY failure; the last failed one was done by its next pass
    assert [e["data"]["kind"] for e in h.journal.events(kind="ORDER_UNRESOLVED")] == ["CLOSE_FAILED"] * 2
    assert paused(h.journal)["reason"] == "incident ORDER_UNRESOLVED"


@pytest.mark.parametrize("ours", [True, False])
def test_an_entry_whose_answer_and_lookups_are_lost_is_protected_at_once(tmp_path, ours):
    """The entry's answer is lost and the order lookups fail for a minute while a position is there: it is protected
    from the first poll with a provisional trade from the entry's intent. Once the lookups answer it is settled -
    our entry (no incident), or, when the entry never reached the venue and the position was opened by hand, a
    foreign position (ADOPTED: a CANARY incident, entries paused)."""
    h = started(tmp_path)
    until = ENTRY_BAR + 30 + 65
    real = h.ex.find_order

    def degraded(cid, since):
        if h.clock() < until:
            h.ex.calls.append("find_order")
            raise Unavailable("503")
        return real(cid, since)

    h.ex.find_order = degraded
    if ours:
        h.ex.crash_after_accept("place_market", Unavailable("read timeout"))  # filled, the answer lost
    else:
        h.ex.fail_next("place_market", Unavailable("503"))  # never reached the venue
    h.at(ENTRY_BAR + 30)
    assert h.journal.intent(f"ewb-e-{SIGNAL}-L")["status"] == "PENDING"
    if not ours:
        h.ex.place_market("manual-1", 1, 1)  # the engine's long, opened by hand
    for k in range(1, 7):
        h.at(ENTRY_BAR + 30 + 10 * k)
        assert covered(h) and paused(h.journal) is None
    h.at(ENTRY_BAR + 30 + 70)  # the lookups answer again
    kinds = h.alerts.kinds()
    assert ("RECOVERED_ENTRY" in kinds) is ours and ("ADOPTED" in kinds) is not ours
    assert (paused(h.journal) is None) is ours and covered(h) and h.ex.calls.count("place_market") == 1 + (not ours)


def test_a_venue_refusing_new_orders_keeps_the_brackets_while_the_close_waits(tmp_path):
    """The engine queues the TIME exit while the venue takes cancels but refuses every new order (cancel-only): the
    close is refused at every poll and the brackets are never touched; it goes through once orders are taken."""
    exit_bar = ts("2024-01-04 15:00")  # the engine queues the TIME exit at this close
    h = started(tmp_path, start=TIME_SIGNAL + 3600 + 30)
    h.at(TIME_SIGNAL + 3600 + 30)
    before = sorted(o.order_id for o in h.ex.open_orders())
    h.price(float(h.bar(exit_bar + 3600)["open"]))

    def refuse(cid, *args, **kw):
        return PlaceResult(cid, None, False, "ORDER_ENTRY_DISABLED")

    h.ex.close_position = refuse
    h.ex.place_bracket = refuse
    t0 = exit_bar + 3600 + 30
    for k in range(3):
        h.at(t0 + 10 * k)
        assert sorted(o.order_id for o in h.ex.open_orders()) == before and covered(h)
    assert len(h.journal.events(kind="CLOSE_FAILED")) == 3 and "cancel" not in h.ex.calls
    del h.ex.close_position, h.ex.place_bracket
    h.at(t0 + 30)
    assert h.ex.position() is None and h.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "TIME"


def test_an_exit_waits_for_the_reopen(tmp_path):
    exit_bar = ts("2024-01-04 15:00")  # the engine queues the TIME exit at this close
    h = started(tmp_path, start=TIME_SIGNAL + 3600 + 30)
    h.at(TIME_SIGNAL + 3600 + 30)
    assert h.ex.position().side == -1
    h.ex.set_session("CLOSE")
    h.price(float(h.bar(exit_bar + 3600)["open"]))
    h.at(exit_bar + 3600 + 30)  # the cycle sees the TIME exit while the session is closed
    assert h.ex.position() is not None and "CLOSE_DEFERRED" in h.alerts.kinds()
    h.ex.set_session("OPEN")
    h.at(exit_bar + 3600 + 100)  # the minute job sees OPEN and polls at once
    assert h.ex.position() is None
    assert h.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "TIME"
    assert [e["data"]["state"] for e in h.journal.events(kind="SESSION")] == ["CLOSE", "OPEN"]  # the SHADOW gate 2


def test_an_exit_queued_before_the_cfm_halt_closes_at_the_first_open_poll(tmp_path, monkeypatch):
    """The engine queues LINEAGE at the Fri 16:00 New York close; the venue halts, so the close waits. The
    venue still reports the halt at the 18:00:05 minute check, so the halt bar's cycle (18:00:30) replaces
    the target before any poll sees OPEN: that target must still carry the exit, and the first poll that
    sees OPEN closes - not the next cycle an hour later (the engine's exit fills at the 18:00 open)."""
    fri16 = HALT - 3600

    class Forced(loop_mod._Run):  # type: ignore[misc, valid-type]
        def _may_enter(self, bar, t_close):  # type: ignore[no-untyped-def]
            if bar.ts in (fri16, HALT):  # a broken lineage (the engine repeats the request while it holds)
                self._request_exit("LINEAGE")
            return super()._may_enter(bar, t_close)

    monkeypatch.setattr(loop_mod, "_Run", Forced)
    h = started(tmp_path, start=LONG_ENTRY + 30)
    h.at(LONG_ENTRY + 30)
    assert h.ex.position().side == 1
    h.ex.set_session("CLOSE")  # the CFM halt
    h.price(float(h.bar(HALT)["open"]))
    h.at(HALT + 30)  # the cycle for the Fri 16:00 bar sees the exit while the venue is halted
    assert h.rt.target.exit_queued_ts == fri16 and "CLOSE_DEFERRED" in h.alerts.kinds()
    h.price(float(h.bar(HALT + 3600)["open"]))
    h.at(HALT + 3600 + 5)  # still halted at this minute check
    assert h.ex.position() is not None
    h.ex.set_session("OPEN")
    h.at(HALT + 3600 + 30)  # the halt bar's cycle: the exit queued at 16:00 is still the target
    assert (h.rt.target.bar_ts, h.rt.target.exit_reason, h.rt.target.exit_queued_ts) == (HALT, "LINEAGE", fri16)
    h.at(HALT + 3600 + 65)  # the minute job sees OPEN and polls at once
    assert h.ex.position() is None
    assert h.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "LINEAGE"


def test_a_stop_crossed_during_the_cfm_halt_closes_at_the_reopen(tmp_path):
    """Spot trades through the long's stop during the halt hour while the futures venue is closed: the engine
    stops out at the 18:00 open; the live futures reopen above the resting stop, so the bracket does not
    trigger - the halt bar's cycle closes the trade (STOP_HALT) instead of an hour later (ENGINE_FLAT)."""
    df = synth_frame()
    stop = compute_target(make_lock(df), df, HALT - 3600).position.stop  # the stop in force during the halt bar
    df.loc[df["ts"] == HALT, "low"] = stop - 10.0
    h = started(tmp_path, start=LONG_ENTRY + 30, df=df)
    h.at(LONG_ENTRY + 30)
    assert h.ex.position().side == 1
    h.ex.set_session("CLOSE")
    h.price(float(h.bar(HALT)["open"]))
    h.at(HALT + 30)
    h.price(stop - 10.0)  # nothing fills while the venue is halted
    h.at(HALT + 1800)
    assert h.ex.position() is not None and h.rt.executor.trade().status == "OPEN"
    h.price(float(h.bar(HALT + 3600)["open"]))  # the reopen, above the stop: the bracket stays
    h.ex.set_session("OPEN")
    h.at(HALT + 3600 + 30)  # minute (OPEN, nothing to close yet), then the halt bar's cycle
    assert h.rt.target.stop_pending_reopen and h.ex.position() is None
    assert h.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "STOP_HALT"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --group live pytest tests/live/test_parity.py tests/live/test_chaos.py -q
```

Expected: `26 passed` already — this task only adds integration coverage of code written in Tasks 1–13, so there is no red phase. If anything fails here, the bug is in Tasks 1–13: fix the component (and its unit test), never relax this test.

- [ ] **Step 3: Write minimal implementation**

No production code. (If Step 2 failed, fix the responsible module from Tasks 1–13 and re-run Step 2.)

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run --group live pytest tests/live -q
```

Expected: `371 passed` (all live tests so far, about 30 s; the three parity walks take about 14 s).

- [ ] **Step 5: Commit**

```bash
git add tests/live/test_parity.py tests/live/test_chaos.py
git commit -m "$(cat <<'EOF'
test(live): parity with the paper replay and chaos (crash mid-order, WebSocket drop, 429, session close, CFM halt)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 15: Docker image, compose service, deploy and backup scripts, ops runbook

**Files:**
- Create: `deploy/live/Dockerfile`, `deploy/live/Dockerfile.dockerignore`, `deploy/live/docker-compose.yml`
- Create: `deploy/live/deploy.sh`, `deploy/live/backup.sh` (both executable), `deploy/live/ewbot-backup.service`, `deploy/live/ewbot-backup.timer`
- Create: `docs/live-trading.md`
- Test: `tests/live/test_deploy.py`

**Interfaces:**
- Consumes: `ewbot live run | preflight --offline | status --json --exchange --require-flat | status --max-age 180 | backup --out` (Task 13).
- Produces: the image `ewbot-live:<tag>` (Python 3.12.13 slim + uv 0.12.13, both pinned by digest; editable install at `/app` with `--no-dev --group live`; user uid 1000; `HEALTHCHECK ewbot live status --max-age 180`; `ENTRYPOINT ["ewbot", "live"]`, `CMD ["run"]`); the compose service `live` (`restart: unless-stopped`, read-only root, `/var/lib/ewbot:/data`, `/etc/ewbot:/run/ewbot:ro`, no secret value in the file); `deploy.sh <tag>` (exit 2 on a tag off `origin/master`, a dirty checkout or a failing offline preflight, exit 3 when not flat); `backup.sh` + systemd timer (daily, age-encrypted, rclone to R2); the runbook.

- [ ] **Step 1: Write the failing test**

**File: `tests/live/test_deploy.py`**

```python
"""Static checks of deploy/live and a stubbed run of deploy.sh (fake git / docker on PATH, no daemon)."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

import ewbot

ROOT = Path(ewbot.__file__).resolve().parents[2]
LIVE = ROOT / "deploy" / "live"
DOC = ROOT / "docs" / "live-trading.md"


def test_dockerfile_pins_the_runtime_and_ships_no_secret():
    text = (LIVE / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^FROM python:3\.12\.13-slim-bookworm@sha256:[0-9a-f]{64}$", text, re.M)
    assert re.search(r"^COPY --from=ghcr\.io/astral-sh/uv:0\.12\.13@sha256:[0-9a-f]{64} /uv /bin/uv$", text, re.M)
    assert text.count("uv sync --frozen --no-dev --group live") == 2
    copies = re.findall(r"^COPY (?!--from)(.+)$", text, re.M)
    assert copies == ["pyproject.toml uv.lock ./", "src ./src", "holdout.lock ./"]
    assert "USER ewbot" in text and '"live", "status", "--max-age", "180"' in text
    assert "RUN test -s /etc/ssl/certs/ca-certificates.crt" in text  # verified TLS needs the CA store
    assert 'ENTRYPOINT ["ewbot", "live"]' in text and 'CMD ["run"]' in text
    assert not re.search(r"^ADD ", text, re.M)
    assert not any(secret in c for c in copies for secret in ("cdp_api_key", "live.env", ".env", "/etc/ewbot"))


def test_the_build_context_is_an_allowlist():
    lines = [x for x in (LIVE / "Dockerfile.dockerignore").read_text().splitlines() if x and not x.startswith("#")]
    assert lines[0] == "*" and set(lines[1:5]) == {"!pyproject.toml", "!uv.lock", "!holdout.lock", "!src/"}


def test_compose_service_restarts_mounts_secrets_read_only_and_holds_no_secret_value():
    svc = yaml.safe_load((LIVE / "docker-compose.yml").read_text())["services"]["live"]
    assert svc["restart"] == "unless-stopped" and svc["read_only"] is True and svc["user"] == "1000:1000"
    assert svc["image"] == "${EWBOT_IMAGE:-ewbot-live:current}"
    assert set(svc["volumes"]) == {"/var/lib/ewbot:/data", "/etc/ewbot:/run/ewbot:ro"}
    env = svc["environment"]
    assert env["EWBOT_SECRETS_FILE"] == "/run/ewbot/live.env" and env["EWBOT_COINBASE_KEY_FILE"] == "/run/ewbot/cdp_api_key.json"
    assert not any(k in env for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "EWBOT_GITHUB_TOKEN", "EWBOT_HEALTHCHECK_URL"))
    assert svc["cap_drop"] == ["ALL"] and "no-new-privileges:true" in svc["security_opt"]
    assert svc["logging"]["options"] == {"max-size": "10m", "max-file": "5"}


@pytest.mark.parametrize("script", ["deploy.sh", "backup.sh"])
def test_scripts_parse_and_fail_fast(script):
    path = LIVE / script
    assert path.read_text().splitlines()[0] == "#!/usr/bin/env bash" and "set -euo pipefail" in path.read_text()
    assert subprocess.run(["bash", "-n", str(path)], capture_output=True).returncode == 0
    if shutil.which("shellcheck"):
        assert subprocess.run(["shellcheck", str(path)], capture_output=True).returncode == 0


def test_backup_encrypts_before_upload_and_the_timer_runs_it_daily():
    text = (LIVE / "backup.sh").read_text()
    assert text.index("ewbot live backup") < text.index("age -r") < text.index("rclone copy")
    service = (LIVE / "ewbot-backup.service").read_text()
    assert "EnvironmentFile=/etc/ewbot/backup.env" in service and "deploy/live/backup.sh" in service
    assert "OnCalendar=*-*-* 06:15:00 UTC" in (LIVE / "ewbot-backup.timer").read_text()


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


def run_deploy(tmp_path: Path, *, on_master: bool = True, flat_rc: int = 0, preflight_rc: int = 0):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    _stub(bin_dir, "git", f"""echo "git $*" >> "{log}"
case "$1" in
  rev-parse) if [ "$2" = "--show-toplevel" ]; then echo "{ROOT}"; fi; exit 0 ;;
  merge-base) exit {0 if on_master else 1} ;;
  status) exit 0 ;;
  *) exit 0 ;;
esac
""")
    _stub(bin_dir, "docker", f"""echo "docker $*" >> "{log}"
case "$*" in
  *"preflight --offline"*) exit {preflight_rc} ;;
  *"--require-flat"*) exit {flat_rc} ;;
  *) exit 0 ;;
esac
""")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    r = subprocess.run(["bash", str(LIVE / "deploy.sh"), "v3.0.0"], capture_output=True, text=True, env=env)
    calls = log.read_text().splitlines() if log.exists() else []
    return r, calls


def test_deploy_happy_path_builds_checks_and_restarts(tmp_path):
    r, calls = run_deploy(tmp_path)
    assert r.returncode == 0, r.stderr
    docker = [c for c in calls if c.startswith("docker")]
    assert docker == [
        "docker build --pull -f deploy/live/Dockerfile -t ewbot-live:v3.0.0 .",
        "docker run --rm ewbot-live:v3.0.0 preflight --offline",
        "docker compose -f deploy/live/docker-compose.yml run --rm --no-deps -T live status --json --exchange --require-flat",
        "docker image tag ewbot-live:v3.0.0 ewbot-live:current",
        "docker compose -f deploy/live/docker-compose.yml up -d --force-recreate",
    ]
    assert "git checkout --quiet --detach v3.0.0" in calls


def test_deploy_refuses_a_tag_off_master(tmp_path):
    r, calls = run_deploy(tmp_path, on_master=False)
    assert r.returncode == 2 and "not on origin/master" in r.stderr and not [c for c in calls if c.startswith("docker")]


def test_deploy_refuses_while_not_flat(tmp_path):
    r, calls = run_deploy(tmp_path, flat_rc=3)
    assert r.returncode == 3 and "/flatten" in r.stderr and not any("up -d" in c for c in calls)


def test_deploy_refuses_an_image_that_fails_its_offline_preflight(tmp_path):
    r, calls = run_deploy(tmp_path, preflight_rc=2)
    assert r.returncode == 2 and not any("--require-flat" in c for c in calls)


def test_the_runbook_covers_setup_stages_and_emergencies():
    text = DOC.read_text(encoding="utf-8")
    for needle in ("Ubuntu 24.04", "unattended-upgrades", "chmod 600", "view", "trade", "IP allowlist",
                   "healthchecks.io", "ewbot live stage canary --confirm", "/flatten", "deploy.sh",
                   "ewbot-backup.timer", "SHADOW", "CANARY", "FULL", "EWBOT_LIVE_EDIT_STOPS",
                   "spec §2 Coinbase facts", "ORDER_UNRESOLVED", "AUDIT_PENDING"):
        assert needle in text, needle
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --group live pytest tests/live/test_deploy.py -q
```

Expected: `11 failed`, each with `FileNotFoundError` (`deploy/live/…` and `docs/live-trading.md` do not exist yet).

- [ ] **Step 3: Write minimal implementation**

```bash
mkdir -p deploy/live
```

**File: `deploy/live/Dockerfile`**

```dockerfile
# ewbot live runtime (Plan 3). Built on the VPS from a tag on master by deploy/live/deploy.sh:
#   docker build -f deploy/live/Dockerfile -t ewbot-live:<tag> .
# Only pyproject.toml, uv.lock, src/ and holdout.lock enter the build (Dockerfile.dockerignore); no
# secret is ever copied in: the CDP key and the secrets file are mounted read-only at run time.
# The project is installed editable at /app, so the fingerprint finds /app/src/ewbot and the
# committed /app/holdout.lock (ewbot live preflight checks both).
FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
COPY --from=ghcr.io/astral-sh/uv:0.12.13@sha256:b485bd65cc2cf1c9a93b3554012c9c3778cf7b1b5fd3d3096ce9e1226c97e1e6 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    EWBOT_LIVE_DIR=/data \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --group live --no-install-project
COPY src ./src
COPY holdout.lock ./
RUN uv sync --frozen --no-dev --group live \
    && useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin ewbot \
    && mkdir -p /data \
    && chown ewbot:ewbot /data

# Verified TLS (the price feed's WebSocket checks Coinbase's certificate against the system CA store) needs
# the base image's ca-certificates: fail the build, not the feed, if a new base image lacks them.
RUN test -s /etc/ssl/certs/ca-certificates.crt

USER ewbot
HEALTHCHECK --interval=60s --timeout=30s --start-period=300s --retries=3 \
    CMD ["ewbot", "live", "status", "--max-age", "180"]
ENTRYPOINT ["ewbot", "live"]
CMD ["run"]
```

**File: `deploy/live/Dockerfile.dockerignore`**

```text
# Build context of deploy/live/Dockerfile (BuildKit reads <Dockerfile>.dockerignore): everything is
# excluded except what the image needs - never .git, .venv, data/, reports/ or any secret.
*
!pyproject.toml
!uv.lock
!holdout.lock
!src/
**/__pycache__
**/*.pyc
```

**File: `deploy/live/docker-compose.yml`**

```yaml
# ewbot live runtime on the VPS (docs/live-trading.md). deploy/live/deploy.sh builds ewbot-live:<tag>,
# checks it, tags it ewbot-live:current and runs `docker compose -f deploy/live/docker-compose.yml up -d`.
# Host paths: /var/lib/ewbot (journal, candle cache, backups; owned by uid 1000) and /etc/ewbot (secrets,
# mode 700 dir / 600 files, owned by uid 1000), mounted read-only. Non-secret settings may be overridden
# in deploy/live/.env (EWBOT_EXPIRY, EWBOT_LIVE_EDIT_STOPS, EWBOT_FUTURES_PRODUCT, EWBOT_PAPER_REPO).
name: ewbot-live
services:
  live:
    image: ${EWBOT_IMAGE:-ewbot-live:current}
    restart: unless-stopped
    user: "1000:1000"
    environment:
      EWBOT_LIVE_DIR: /data
      EWBOT_SECRETS_FILE: /run/ewbot/live.env
      EWBOT_COINBASE_KEY_FILE: /run/ewbot/cdp_api_key.json
      EWBOT_FUTURES_PRODUCT: ${EWBOT_FUTURES_PRODUCT:-BIP-20DEC30-CDE}
      EWBOT_PAPER_REPO: ${EWBOT_PAPER_REPO:-taejunoh/elliott-wave-btc-bot}
      EWBOT_LIVE_EDIT_STOPS: ${EWBOT_LIVE_EDIT_STOPS:-0}
      EWBOT_EXPIRY: ${EWBOT_EXPIRY:-}
      TZ: UTC
    volumes:
      - /var/lib/ewbot:/data
      - /etc/ewbot:/run/ewbot:ro
    read_only: true
    tmpfs:
      - /tmp
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    stop_grace_period: 30s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
```

**File: `deploy/live/deploy.sh`**

```bash
#!/usr/bin/env bash
# Deploy a tagged master commit of ewbot's live runtime to this VPS (Plan 3 spec 5.2).
# Usage (from the repository checkout on the VPS): bash deploy/live/deploy.sh <tag>
# Refuses (exit 2) a tag that is not on origin/master, a checkout with local changes or an image whose
# offline preflight fails (fingerprint, holdout.lock); refuses (exit 3) while a trade is open in the
# journal (SHADOW's virtual one included) or the Coinbase account has a position or a resting order.
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
if ! EWBOT_IMAGE="$IMAGE" "${COMPOSE[@]}" run --rm --no-deps -T live status --json --exchange --require-flat; then
  echo "refusing: not flat - send /flatten in Telegram (or wait for the trade to close), then deploy again" >&2
  exit 3
fi

docker image tag "$IMAGE" ewbot-live:current
"${COMPOSE[@]}" up -d --force-recreate
echo "deployed $IMAGE (tagged ewbot-live:current); follow the start with: ${COMPOSE[*]} logs -f live"
```

**File: `deploy/live/backup.sh`**

```bash
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
```

**File: `deploy/live/ewbot-backup.service`**

```ini
# /etc/systemd/system/ewbot-backup.service (docs/live-trading.md, "Backups")
[Unit]
Description=ewbot live journal backup (encrypted, off-site)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=ewbot
EnvironmentFile=/etc/ewbot/backup.env
ExecStart=/usr/bin/bash /home/ewbot/elliott-wave-btc-bot/deploy/live/backup.sh
```

**File: `deploy/live/ewbot-backup.timer`**

```ini
# /etc/systemd/system/ewbot-backup.timer (docs/live-trading.md, "Backups")
[Unit]
Description=Daily ewbot live journal backup

[Timer]
OnCalendar=*-*-* 06:15:00 UTC
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
```

```bash
chmod +x deploy/live/deploy.sh deploy/live/backup.sh
```

**File: `docs/live-trading.md`**

````markdown
# Live-trading runtime (Plan 3) — operations

Spec: `docs/superpowers/specs/2026-10-03-ewbot-plan3-live-trading-design.md` (parents: the Plan 1 and Plan 2 specs).
Plan: `docs/superpowers/plans/2026-10-03-ewbot-plan3-live-runtime.md`.

## What runs

- One Docker container on a US-East VPS runs `ewbot live run` (`deploy/live/docker-compose.yml`, `restart: unless-stopped`). It trades the frozen strategy (params from the committed `holdout.lock`, conservative risk) on the Coinbase CFM future `BIP-20DEC30-CDE` (0.01 BTC per contract, $5 tick). Signals come from the same spot BTC-USD hourly candles as the paper runtime.
- Every hour at hh:00:30 it replays the whole engine (as the paper runtime does) and reads the target after the bar that just closed: the position the engine holds, its stop and TPs, an exit or an entry queued at that close. It then makes the Coinbase account match that target (`src/ewbot/live/reconcile.py`): market entries, two `trigger_bracket_gtc` orders as protection (TP1 size + stop, the rest + TP2 + stop; one bracket for 1 contract), stop moves, TP1 break-even, closes. If the bar has not arrived 2 minutes after the close, the decision is held and the resting brackets keep protecting the position.
- Between cycles: every second the stop-gap watchdog (the bracket's stop fills as a limit 5% beyond the stop, Coinbase's trigger_bracket rule; if the price is past that limit for 10 s, the position is closed at market); every 10 s while a trade is open the position and orders are re-read and protection is repaired; every minute the session state, the account breakers (−3% from the New York day's opening, −15% from the peak - each reference the median of three balance reads, each breaker tripping on two breaching reads in a row: flatten and pause), the overnight-margin projection (80% warns, 90% reduces to 70%), the WebSocket price feed's health, the Telegram command thread's health, the heartbeat to healthchecks.io and the stage-change check; at 15:30 New York an overnight-margin report; at 09:00 New York the daily summary; from minute 50 of every hour the comparison of the live engine decisions with the paper ledger (up to the last bar whose cycle ended OK).
- Everything is recorded in the SQLite journal `/var/lib/ewbot/journal.sqlite`: every order intent before it is sent (deterministic `client_order_id`, so a restart never doubles an order), events, the engine decisions, the cycles and the stage history.
- The GitHub Actions paper runtime keeps running as the independent audit. Telegram messages of the live runtime start with `[LIVE]`, the paper runtime's with `[ewbot paper]`.
- Telegram never holds up trading: alerts are queued and sent by a background thread (every protective action - a stop-gap close, a breaker flatten, a margin reduction, a pause - is taken first, its alert queued after it). A message that fails for a passing reason (network, HTTP 429 or 5xx) is retried (2 s doubling to 60 s) for up to 30 minutes and its full text is written to the container log at once (`docker compose ... logs live | grep '\[LIVE\]'`); a message Telegram refuses outright (any other HTTP 4xx) goes to the log and is dropped at once, so it never holds up the messages behind it; messages still waiting when the runtime stops go to the log too. A message delivered a minute or more late ends with `(N분 지연)`, one longer than Telegram allows is cut with `…(잘림)`. The queue holds 100 messages (the oldest waiting one is dropped, into the log, when it is full).

## Stages (spec 6)

| Stage | What trades | Size | Pass gate (checked by `ewbot live status` / `ewbot live stage`) |
|---|---|---|---|
| SHADOW | nothing: every order is checked with Coinbase's `orders/preview` and executed on a virtual book that follows the real price | 1 contract (CANARY sizing) | ≥ 14 days and ≥ 1 signal first known during SHADOW; ① decisions 100% equal to the paper ledger (a passing comparison that covers the whole stage and reaches a bar closed at most 3 hours ago); ② a cycle for every hourly bar of the stage, each finished ≤ 2 min after the close (an hour the runtime was down fails it; an hour the futures session was recorded closed is excused); ③ every would-be order passed preview; ④ a restart recovered cleanly |
| CANARY | real orders | always 1 contract; a signal whose 1-contract risk exceeds 2% of the account is skipped | ≥ 3 closed trades or 90 days; ① protection ≤ 30 s after every fill; ② no order failure left unresolved (a bracket not replaced within 30 s, a close or a reduction not done by the next poll); ③ never unprotected > 60 s; ④ no manual intervention; ⑤ costs reported; and no other CANARY failure in this CANARY stint (a decision mismatch, a breaker, a transfer key), not paused, a passing comparison at most 3 hours old |
| FULL | real orders | `risk.sizing.size_contracts` (real equity, conservative 1%), capped at 3 contracts until FULL has run 30 days without a CANARY failure (each one restarts the 30 days) | — |

- CANARY may start only when SHADOW passed, the paper runtime's criteria 1, 2 and 4 are met, the trade key is in place and you approve (`--confirm`). FULL needs CANARY passed and **all four** paper criteria (incl. 50 out-of-sample trades) and your approval. Promotions go one stage at a time and need a flat book.
- In CANARY a CANARY failure (unprotected > 60 s, an unexplained decision mismatch with the paper ledger, a breaker, a transfer-permission key, a foreign order or position - contracts added or removed by hand included -, slow protection, an order failure not resolved in time) pauses entries (`[CANARY 실패]` / `[중대 사건]`) and keeps the CANARY pass gate unmet for the rest of this CANARY stint: `/resume` resumes trading, but passing CANARY then needs a new stint (a demotion to SHADOW and the SHADOW and CANARY gates again). In FULL the major incidents pause and demote to CANARY - the next entry is sized as CANARY at once, even before the runtime restarts - and any other CANARY failure keeps the 3-contract cap for 30 more days. Pause never stops protection or exits; `/resume` is your decision.

## One-time setup (you)

1. **VPS.** Ubuntu 24.04 LTS in a US-East region (New York / New Jersey / Virginia), 1 vCPU, 1–2 GB RAM, about $5–7 a month. Log in with an SSH key only:

   ```bash
   sudo sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config && sudo systemctl restart ssh
   sudo ufw allow OpenSSH && sudo ufw --force enable
   ```

   Note the VPS's public IPv4 (`curl -4 -s https://ifconfig.me`): the API key is bound to it.
2. **Automatic security updates.**

   ```bash
   sudo apt-get update && sudo apt-get install -y unattended-upgrades
   sudo dpkg-reconfigure -f noninteractive unattended-upgrades
   cat /etc/apt/apt.conf.d/20auto-upgrades   # both lines must end in "1"
   ```

   Do not enable automatic reboots: reboot by hand when the book is flat (see Weekly checks).
3. **Docker Engine and the compose plugin** from Docker's apt repository (<https://docs.docker.com/engine/install/ubuntu/>), then a user `ewbot` with uid 1000 that runs everything:

   ```bash
   sudo useradd --uid 1000 --create-home --shell /bin/bash ewbot   # if uid 1000 is taken, use that user instead
   sudo usermod -aG docker ewbot
   sudo install -d -m 700 -o 1000 -g 1000 /var/lib/ewbot /etc/ewbot
   ```
4. **Coinbase CDP API key** (spec 3.4). In <https://portal.cdp.coinbase.com> → API Keys → Secret API Keys → Create:
   - signature algorithm **ECDSA**;
   - permissions **View** and **Trade** only, never **Transfer** (the runtime refuses to start with a transfer key and treats one found later as a major incident). For SHADOW a **View**-only key is enough; create the View + Trade key when CANARY is approved;
   - **IP allowlist**: the VPS's public IPv4 only;
   - download the key JSON and copy it to the VPS, then lock it down (`chmod 600`):

   ```bash
   scp cdp_api_key.json ewbot@<vps>:/tmp/cdp_api_key.json
   sudo install -m 600 -o 1000 -g 1000 /tmp/cdp_api_key.json /etc/ewbot/cdp_api_key.json && rm /tmp/cdp_api_key.json
   ```
   Delete the downloaded copy from your laptop once the VPS has it. Note the key's creation date for the expiry reminder.
5. **Health check** (spec 5.1 `VPS 다운`). At <https://healthchecks.io> create a check `ewbot-live` with period **1 minute** and grace **2 minutes** (3 silent minutes alert), add the **Telegram** integration to it and keep the **email** integration (your account address) on it too, and copy its ping URL. The email is the channel that works when Telegram does not. Telegram counts as unusable when the command thread has been failing, dead or hung for 10 minutes, a message has failed to go out for 10 minutes, messages have waited 10 minutes without a send attempt (a stuck sender), or Telegram refused 3 messages in a row (e.g. the bot was blocked). The runtime then sends the check a `/fail` with the reason (`[LIVE] 텔레그램 사용 불가 - ...`) and withholds its success pings for at most 30 minutes, so the check stays down; after that the pings resume - so a hung runtime or a dead VPS still trips the check - and the `/fail` is repeated every 3 hours while Telegram stays unusable. A healthchecks.io alert therefore means the VPS or the runtime is down, or Telegram is unusable: only the latter comes with a `/fail` body.
6. **GitHub token for the audit** (spec 3.5). A fine-grained personal access token at <https://github.com/settings/personal-access-tokens/new>: resource owner `taejunoh`, only the repository `elliott-wave-btc-bot`, permission **Contents: Read-only**, expiry 1 year.
7. **Secrets file** `/etc/ewbot/live.env` (mode 600; the runtime refuses any other mode). Create it empty with the right mode, then edit it (nothing goes to the shell history):

   ```bash
   sudo install -m 600 -o 1000 -g 1000 /dev/null /etc/ewbot/live.env
   sudo -u ewbot nano /etc/ewbot/live.env
   ```

   ```
   TELEGRAM_BOT_TOKEN=<the paper runtime's bot token>
   TELEGRAM_CHAT_ID=<the same chat id: your private chat with the bot>
   EWBOT_GITHUB_TOKEN=<the fine-grained token of step 6>
   EWBOT_HEALTHCHECK_URL=<the ping URL of step 5>
   ```

   Commands are taken only from a **private chat** with the bot: `TELEGRAM_CHAT_ID` must be its id, a positive number (your own Telegram user id); a group or channel id (negative) or an `@name` is refused at start. If the paper runtime reports to a group, use your private chat with the bot here. Only this runtime may read the bot's updates: no webhook on the bot, no second runtime with the same token, and do not run a `getUpdates` request by hand while it runs (Telegram answers the runtime with HTTP 409, alerted as `[텔레그램 명령 수신 이상] … 충돌`). Sending (`sendMessage`, as the paper runtime does) is not affected.
8. **Repository checkout** on the VPS (a read-only deploy key: <https://github.com/taejunoh/elliott-wave-btc-bot/settings/keys>):

   ```bash
   sudo -iu ewbot
   ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519 && cat ~/.ssh/id_ed25519.pub   # add as a read-only deploy key
   git clone git@github.com:taejunoh/elliott-wave-btc-bot.git ~/elliott-wave-btc-bot
   ```

   Non-secret settings go to `~/elliott-wave-btc-bot/deploy/live/.env` (read by compose; not committed), for example the expiry reminders (30 days ahead, in the daily summary):

   ```
   EWBOT_EXPIRY=coinbase=2027-10-03,github=2027-10-03
   ```
9. **Fund the futures account** with enough for one contract's margin plus buffer (about $500). SHADOW's previews run against the real account: an unfunded account fails gate ③.
10. **First deployment**: tag the merged commit on `master` (for example `live-v1`, `git tag live-v1 origin/master && git push origin live-v1` on your laptop), then on the VPS:

    ```bash
    cd ~/elliott-wave-btc-bot && bash deploy/live/deploy.sh live-v1
    docker compose -f deploy/live/docker-compose.yml logs -f live
    ```

    The first start downloads the candle history (a few minutes), records stage SHADOW and sends `[LIVE] [시작]` to Telegram. Check `docker compose -f deploy/live/docker-compose.yml exec live ewbot live preflight` (all four checks `ok`).
11. **Backups** (spec 5.2): daily, encrypted with [age](https://github.com/FiloSottile/age) and copied with rclone to a Cloudflare R2 bucket.
    - On your laptop: `age-keygen -o ewbot-backup.key` — keep this private key offline; note the `age1...` public key it prints.
    - In Cloudflare: an R2 bucket `ewbot-backups` and an R2 API token with Object Read & Write on that bucket only.
    - On the VPS: `sudo apt-get install -y age rclone`, then `sudo -u ewbot rclone config` → new remote `r2`, type S3, provider Cloudflare, the token's access key / secret and the account endpoint.
    - `/etc/ewbot/backup.env` (mode 600): `EWBOT_BACKUP_AGE_RECIPIENT=age1...` and optionally `EWBOT_BACKUP_REMOTE=r2:ewbot-backups`.
    - Install and start the timer:

    ```bash
    sudo cp ~ewbot/elliott-wave-btc-bot/deploy/live/ewbot-backup.service ~ewbot/elliott-wave-btc-bot/deploy/live/ewbot-backup.timer /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now ewbot-backup.timer
    sudo systemctl start ewbot-backup.service && journalctl -u ewbot-backup.service -n 20
    ```

    Restore: download the newest `journal-*.sqlite.age`, `age -d -i ewbot-backup.key -o journal.sqlite journal-...sqlite.age`, stop the service, copy it to `/var/lib/ewbot/journal.sqlite` (owner 1000), start the service: the restart recovery reconciles it with Coinbase.

## Daily operation

- Telegram commands (only from your private chat with the bot, `TELEGRAM_CHAT_ID`; messages from anyone else, forwarded messages, edits and commands addressed to another bot are ignored): `/status`, `/pause` (no new entries; protection and exits continue), `/resume`, `/flatten` then `/flatten_confirm` sent within 60 s of it (close at market - the brackets go with the position - and pause), `/help`. Every command is answered. A command sent more than 2 minutes before the runtime reads it (for example while it restarted) is not acted on: the answer is `오래된 명령 무시: /…` - send it again if you still want it (the ages come from Telegram's message dates and the VPS clock, which Ubuntu keeps synchronised: `timedatectl` shows `System clock synchronized: yes`). The first start ignores whatever was sent to the bot before. Delivery is at most once: a command the runtime fetched right before a crash or a restart is lost without an answer - if no answer comes within a minute, check `/status` and send it again.
- **Telegram and health-check alerts.** `[텔레그램 명령 수신 이상]` means the command thread failed 5 polls in a row (the reason: `HTTP 401` - the bot token was refused; `HTTP 409` - another process or a webhook reads this bot's updates; a network error), was in conflict with another poller (`충돌`, HTTP 409), had stopped and was started again, or stopped answering (`응답 없음`: a hung request; it is not replaced automatically, since the hung thread would keep polling beside a new one - restart the container: `docker compose -f deploy/live/docker-compose.yml restart live`); `[텔레그램 복구]` that commands work again. While it lasts the commands may not arrive: use the VPS (`ewbot live status`, and the emergency procedures). `[외부 감시 핑 실패]` means three healthchecks.io pings in a row failed (`HTTP 404`: a wrong ping URL - fix `EWBOT_HEALTHCHECK_URL`): a dead VPS would then go unreported; `[외부 감시 핑 복구]` that they work again.
- Status on the VPS: `docker compose -f deploy/live/docker-compose.yml exec live ewbot live status` (stage, position, last cycle, audit, heartbeat and the current stage's gates). `docker compose ... ps` shows the container's health (the Docker health check fails when the loop heartbeat is older than 3 minutes).
- **Price feed and balance alerts.** The watchdog's prices come from the Coinbase WebSocket ticker, on Coinbase's public market-data endpoint (`wss://advanced-trade-ws.coinbase.com`: no API key, TLS certificate and host name verified); the feed reconnects by itself (a connection silent for 15 s is reopened, backoff 1 s to 60 s) and falls back to REST quotes every 10 s while it is silent. `[가격 피드 끊김]` means no WebSocket futures price for 5 minutes while the market is open (with the reconnect failures and the last error; `server error: ...` means Coinbase refused the subscription), `[가격 피드 복구]` that futures prices are back; nothing to do unless it persists (check the VPS network and `docker compose ... logs live`). With neither a WebSocket nor a REST price for 60 s the stop-gap check waits (the resting brackets still protect) and the margin check is skipped - never reduced on a stand-in price - with one `[증거금 점검: 가격 없음]` alert until a price is back. `[잔고 이상]` means Coinbase returned a balance that is not a finite amount above 0: that read is ignored (no breaker sample, no margin check) until a normal one arrives; three such reads in a row are an incident (`[중대 사건] 잔고 이상이 3회 연속`) that pauses new entries - the position and its brackets stay, nothing is closed. Check the balance in the Coinbase app, then `/resume`. `[15:30 증거금 점검] 조회 실패` means the 15:30 check could not read the position or the balance; it is retried every minute until 16:00.
- **Paper comparison alerts** (from minute 50, against the paper ledger up to the last bar whose cycle ended OK, so a held or failed cycle is never a mismatch). `[판단 불일치 확인 중]` (`AUDIT_PENDING`): a decision one runtime recorded and the other not; it becomes `[판단 불일치]` (`AUDIT_MISMATCH`, a major incident) only if the next hourly comparison still finds it. A decision both recorded differently, or a paper ledger on another strategy fingerprint, is `[판단 불일치]` at once. `[판단 불일치: 봉 수정]` (`AUDIT_REVISION`): the two runtimes decided on different candles for that bar - equal up to the paper's previous run, different at it (Coinbase changed the bar between the live fetch at hh:00:30 and the paper run at :37); no incident, but the comparison is no longer 100% equal, so the SHADOW ① and CANARY comparison gates stay unmet while it is in the compared window: compare that bar in both candle caches (the live cache on the VPS, the paper runtime's in its workflow) before going on. A revision is only recognised at the bar where the two candle histories part: a later decision it changed counts as unexplained. `[모의매매 대조 실패]`: the paper ledger could not be read or is not a valid paper ledger; the gates are unmet until a comparison succeeds again (`[모의매매 대조 복구]`).
- The daily summary (09:00 New York) shows the stage, the position, the cycle, the paper comparison, the fills of the last 24 h (entry slippage vs. the engine's reference, stop slippage vs. the stop price, measured fees vs. the paper cost model; funding is part of the balance change) and the margin.
- **A stop that empties the TP1 leg first** (FULL only: a 1-contract CANARY trade has one bracket). Both legs share the stop. When the price falls through it, the TP1 leg's stop can fill a poll before the TP2 leg's: the runtime then reads the smaller position as the live TP1 (`TP1_FILLED`, Telegram `[TP1 체결]`) and re-places the rest with a break-even stop that is already through the market, so it fills at once - or, if Coinbase refuses a stop through the market (`BRACKET_FAILED`), the old leg stays: its stop has triggered already and fills, and if the price gapped past its 5% limit the watchdog closes at market (`STOP_GAP`). Either way the rest leaves at about the stop; for that trade `TP1_FILLED` is not a take-profit (its `BRACKET_FILL` events show the stop prices).
- **A close or a stop move that keeps failing.** Protection is never cancelled before the venue took its replacement. A close goes out with the brackets resting; a failed close (`CLOSE_FAILED`, Telegram `[청산 실패 - 재시도 중]`) is retried on every poll with a new id and the brackets stay. Only if Coinbase refuses a close because brackets rest (`CLOSE_CONFLICT`) are they cancelled - after a session check - and the close is sent again at once; after every failed close the legs are placed again if none rests, so the position is protected while the close is retried. A flip waits (up to 5 s) until no bracket of the old side rests, else the new entry is skipped (`[진입 건너뜀]`). A stop move keeps the old leg whenever the new one is not confirmed (429, 5xx, timeout) or refused for any reason other than a known overlap refusal (`BRACKET_FAILED`, `[보호 주문 실패]`; e.g. order entry disabled); only a known overlap refusal while our brackets would exceed the position cancels the old leg first (`LEG_GAP`, no alert: not an order failure) and puts it back at once if the new one fails (`LEG_RESTORED`). A bracket Coinbase accepted while the answer was lost is found among the open orders or taken over by its client order id (`LEG_ADOPTED`).
- **An entry whose answer and lookups were lost, or whose fill cannot be read** (`ENTRY_UNCONFIRMED` `[진입 미확인]`, then `ADOPTED_PROVISIONAL`, Telegram `[경고] 진입 ... 결과 미확인`): the position is protected at once with the entry's stop and TPs; when the lookup answers it is either our entry (`RECOVERED_ENTRY`) or, if the entry never reached Coinbase, a foreign position (`ADOPTED`: a CANARY incident, entries paused - check the Coinbase order history). A foreign position taken over without quotes is protected at the spot levels (`LEVELS_APPROX`) and re-mapped once quotes are back (`LEVELS_MAPPED`). A provisional trade keeps the position's size: a contract added by hand at the same moment is not reduced as excess (close it yourself). A provisional trade closed by its bracket before it was settled is never classified - no `ADOPTED` incident even if the position was not ours - so after an `ADOPTED_PROVISIONAL` alert check the Coinbase order history.
- **A reduction refused because the brackets cover it** (`REDUCE_CONFLICT`: TP1 sync, excess, margin): the legs make room first - the TP1 leg is cancelled for a TP1 sync, the legs shrink to the size after an excess or margin reduction (the new ones placed first) - and the reduction is sent again under a new id; if it does not fill the legs go back for the whole position. Nothing to do; `REDUCE_FAILED` after it means the venue refused again.
- **A reduction whose answer was lost** (`REDUCE_FAILED` with status `PENDING`, Telegram `[수량 축소 미확인]`): it was recorded before it was sent, so it is never sent under a new id. It is looked up by its client order id (sent again under the same id only if Coinbase does not have it), and the smaller position is read as that reduction (`REDUCE_SETTLED`), not as a manual one. Nothing to do unless `[경고] … 1분 넘게 포지션에 반영되지 않음` follows.

## Stage transitions

1. **SHADOW** starts with the first deployment (a View key suffices).
2. **SHADOW → CANARY** (expected around 2026-10-30): check `ewbot live status` (all SHADOW gates `충족`) and the paper runtime's status (criteria 1, 2 and 4 met: `uv run ewbot paper status --ledger-dir <a worktree of the paper branch>` on your laptop, docs/paper-trading.md). Restart the container once during SHADOW (`docker compose -f deploy/live/docker-compose.yml restart live`) for gate ④, between hh:05 and hh:55: a restart across the top of the hour can make that hour's cycle late (gate ②). Then create the View + Trade key (step 4), replace `/etc/ewbot/cdp_api_key.json`, check what no gate can see - in the CDP portal the key's **IP allowlist** holds only the VPS's IPv4 (spec §6.1) - and with the book flat:

   ```bash
   docker compose -f deploy/live/docker-compose.yml exec live ewbot live stage canary --confirm
   ```

   The command prints every gate and refuses while one is unmet or the book is not flat. The running container restarts itself into CANARY within a minute; Telegram shows `[단계 변경]` and a new `[시작] 단계 CANARY`.
3. **CANARY → FULL**: only after all four Plan 1 §9.6 criteria (incl. 50 out-of-sample trades) and the CANARY gates are met, with your approval. Before it, check by hand what no gate can see: re-check the spec §2 Coinbase facts against Coinbase's current documentation (spec §8: the product - 0.01 BTC, $5 tick, whole contracts -, the intraday and overnight margin rates, the order types `market_market_ioc`, `trigger_bracket_gtc`, `close_position` and `orders/preview`, the triggered bracket stop's 5% limit, the rate limits, the fee tier, the session hours and maintenance) and record the result, and that the trade key's IP allowlist still holds only the VPS's IPv4. Then `... exec live ewbot live stage full --confirm`.
4. **Demotion** (any time, flat book): `... exec live ewbot live stage shadow --confirm` (or `canary`).

## Confirm during CANARY (spec 6.2)

- Duplicate `client_order_id` behaviour (the runtime handles both documented answers; note which one Coinbase gives).
- Whether brackets survive the Friday 17:00–18:00 New York close.
- Whether `orders/edit` changes a bracket's stop. If it does, set `EWBOT_LIVE_EDIT_STOPS=1` in `deploy/live/.env` and redeploy; until then stop moves place the new bracket first and cancel the old one.
- The reason codes Coinbase gives when it refuses a bracket that would exceed the position and a close while brackets rest (`OVERLAP_REFUSALS`, `CLOSE_CONFLICTS` in `src/ewbot/live/executor.py`; journal events `BRACKET_FAILED` / `CLOSE_FAILED` carry the code). If they differ, correct the sets and redeploy with a flat book; until then an unknown refusal keeps the old bracket and the move or close is retried.
- That brackets - including a triggered stop resting as a limit - only reduce at fill time: a bracket left after a close cannot open or flip a position (closes are sent with the brackets resting).
- Whether a sized `close_position` (a reduction) is refused while our brackets cover the position, and with which reason code (add it to `CLOSE_CONFLICTS`).
- The actual fee tier (`GET /api/v3/brokerage/transaction_summary`).
- The limit of the first triggered bracket stop: the Create Order reference says 5% beyond the stop (`STOP_LIMIT_OFFSET`, also the stop-gap watchdog's threshold). If Coinbase uses another offset, change the constant and redeploy with a flat book.
- Re-record the private-endpoint fixtures in `tests/live/fixtures/coinbase.json` (positions, balance summary, key permissions, orders, the open-orders query with every resting `order_status`, preview) from real responses with personal data removed, and rerun the tests.

## Emergency procedures

- **Close everything now**: `/flatten`, then `/flatten_confirm`. If Telegram does not answer: `docker compose -f deploy/live/docker-compose.yml exec live ewbot live status` and, if the runtime is down, close the position and cancel the orders in the Coinbase app (a manual intervention: the runtime alerts it and CANARY gate ④ records it).
- **Stop trading but keep the protection**: `/pause`, or `docker compose -f deploy/live/docker-compose.yml stop live` (the brackets stay on Coinbase and keep protecting).
- **An order failure not resolved in time** (`[주문 실패 미해결]`, `ORDER_UNRESOLVED`; in CANARY `[CANARY 실패] ORDER_UNRESOLVED: paused`): a bracket that could not be replaced within 30 s, or a close or a reduction that did not go through by the next poll. Read the order events (`BRACKET_FAILED`, `CLOSE_FAILED`, `CLOSE_UNCONFIRMED`, `REDUCE_FAILED`, `ACTION_ERROR`) and check the position's brackets in the Coinbase app before `/resume`. A failure the runtime resolved in time (`ORDER_RESOLVED`, e.g. one 429 on a stop move replaced at the next poll) is no CANARY failure.
- **Heartbeat alert from healthchecks.io**: SSH in, `docker compose ... ps` and `... logs --tail 200 live`. If the VPS is gone, the brackets still protect the position: check it in the Coinbase app. If the alert came with a `/fail` body `텔레그램 사용 불가 - ...`, the runtime was running when it sent it but cannot be reached on Telegram - check that it still runs (`docker compose ... ps`; its pings resume within 30 minutes of the `/fail`, so a check that stays down longer means it stopped) - and then check `... logs live | grep 'telegram'` (`HTTP 401`: replace the bot token; `HTTP 409`: stop the other poller or delete the webhook; network errors: Telegram or the VPS network). Meanwhile: `ewbot live status` on the VPS, `docker compose -f deploy/live/docker-compose.yml stop live` to stop trading (the brackets stay on Coinbase) and the Coinbase app to close a position.
- **API key leaked or a transfer-permission alert**: delete the key in the CDP portal at once (open brackets stay), create a new View + Trade key bound to the VPS IP, replace the file and restart.
- **A start refused by the preflight** (fingerprint, lock, key, product): the message names the failing check. Never edit a strategy-fingerprinted file to fix it; redeploy the previous tag.
- **Breaker or incident pause**: read `/status` and the latest journal events, fix the cause, then `/resume`. `/resume` re-bases only the breaker that tripped (the day's reference after −3%, the peak after −15% - which also marks the day), as the median of the `/resume` balance read and the next two; a `/pause` → `/resume` without a trip changes neither. If the balance cannot be read at that moment, the reply says so and a tripped breaker still blocks entries: send `/resume` again. The latest journal events:

  ```bash
  docker compose -f deploy/live/docker-compose.yml exec live python -c "from ewbot.live.journal import Journal; [print(e['ts'], e['kind'], e['data']) for e in Journal('/data/journal.sqlite').events()[-40:]]"
  ```

## Deploying a new version

- The image verifies Coinbase's TLS certificates with the base image's CA store (`ca-certificates`, part of `python:3.12.13-slim-bookworm`); the build stops if `/etc/ssl/certs/ca-certificates.crt` is missing (a new base image without it): keep the pinned base, or install `ca-certificates` in the Dockerfile.
- Only tags on `master`, only with a flat book: `bash deploy/live/deploy.sh <tag>` refuses a tag off `master`, a dirty checkout, an image whose offline preflight fails (fingerprint `e0435223…`, lock `d1be04baa06c…`) and any open trade, position or resting order. Rollback = deploy the previous tag.
- A change to any strategy-fingerprinted file (`types.py`, `config.py`, `indicators.py`, `data/candles.py`, `waves/`, `strategy/`, `engine/`, `execution/`, `risk/`) is refused by the preflight and would reset the paper runtime's out-of-sample counter: never deploy one.

## Weekly checks

- `df -h /var/lib/ewbot`, `docker system df` (prune old images: `docker image prune`), `journalctl -u ewbot-backup.service -n 5`, the newest backup in R2.
- Pending updates are installed by unattended-upgrades; if `/var/run/reboot-required` exists, reboot only with a flat book (`/flatten` not needed if no trade is open), then check `[시작]` in Telegram.
- Expiry reminders (`EWBOT_EXPIRY`) arrive 30 days ahead in the daily summary: the Coinbase API key, the GitHub audit token and the Cloudflare Workers trigger token of the paper runtime.

## Cost

VPS about $5–7 a month; healthchecks.io, R2 (a few MB) and Telegram are free at this size. Exchange fees: $0.10 per BIP contract per side plus the clearing and NFA fees (at least $0.15 per contract per side in total, spec 2).
````

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run --group live pytest tests/live/test_deploy.py -q
command -v shellcheck >/dev/null && shellcheck deploy/live/deploy.sh deploy/live/backup.sh && echo shellcheck-ok || echo 'shellcheck not installed (optional)'
if command -v docker >/dev/null; then docker build -q -f deploy/live/Dockerfile -t ewbot-live:plan3-check . && docker run --rm ewbot-live:plan3-check preflight --offline; else echo 'docker not available (optional)'; fi
uv run --group live pytest -q
uv run python -c "from ewbot.paper.fingerprint import current_fingerprint; print(current_fingerprint())"
git diff --stat docs/live-design -- src/ewbot/types.py src/ewbot/config.py src/ewbot/indicators.py src/ewbot/data src/ewbot/waves src/ewbot/strategy src/ewbot/engine src/ewbot/execution src/ewbot/risk src/ewbot/backtest src/ewbot/paper holdout.lock .github
```

Expected: `11 passed`; `shellcheck-ok` when installed; with Docker, the image builds (about 20 s on a warm cache) and its offline preflight prints `preflight fingerprint: ok - e0435223a3f9 over 25 files (baseline e0435223a3f9, 25 files)` and `preflight lock: ok - sha256 d1be04baa06c, params [2.0, 0.7, 0.5]`; the full suite `1002 passed, 1 skipped` without a candle cache (`1003 passed` with `data/cache/BTC-USD_1h.parquet`); the fingerprint `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1`; the final `git diff --stat` prints nothing (every fingerprinted file, `backtest/`, `paper/`, the lock and the workflows are byte-identical; use `master` instead of `docs/live-design` when branching from `master`).

- [ ] **Step 5: Commit**

```bash
git add deploy/live docs/live-trading.md tests/live/test_deploy.py
git commit -m "$(cat <<'EOF'
ops(live): Docker image, compose service, deploy and backup scripts, live-trading runbook

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 16: Manual checklist — merge, VPS, SHADOW start, CANARY gate (controller and user only — not for subagents)

Spec §6.1, §6.2, §7 `실제 환경`. These steps touch GitHub, a VPS, the real Coinbase account and real money; nothing here is automated, no code changes, and every promotion needs the user's explicit approval. The user's CFM futures account was reported approved on 2026-10-03 (project notes), which settles the spec §8 New York approval risk.

**Files:** none.

**Interfaces:**
- Consumes: everything above, `docs/live-trading.md`, the user's accounts (GitHub, Coinbase CDP, VPS provider, healthchecks.io, Cloudflare R2, Telegram).
- Produces: the SHADOW start and, later, the go/no-go for CANARY.

- [ ] **Step 1: Confirm the build**

```bash
git status --short
uv run --group live pytest -q
uv run pytest tests/paper/test_fingerprint.py -q -k baseline
git diff --stat master -- src/ewbot/types.py src/ewbot/config.py src/ewbot/indicators.py src/ewbot/data src/ewbot/waves src/ewbot/strategy src/ewbot/engine src/ewbot/execution src/ewbot/risk src/ewbot/backtest src/ewbot/paper holdout.lock .github
```

Expected: clean tree; `1003 passed` with the real cache (`1002 passed, 1 skipped` without); both fingerprint tripwires pass; the diff prints nothing.

- [ ] **Step 2: Decisions, pull request, merge (controller, user approves)**

Get the user's sign-off on D1–D3 (top of this plan). Open a PR from `feat/plan3-live` to `master`; its `ci` run must be green (the fingerprint and static guards, incl. the one-line allowlist change of D2). Merge right after a paper run that reported 0 new discrepancies, then verify the next paper run in a worktree of `paper`:

```bash
git fetch origin paper && git worktree add /tmp/ewbot-paper-check origin/paper
uv run ewbot paper status --ledger-dir /tmp/ewbot-paper-check
git worktree remove /tmp/ewbot-paper-check
```

Expected: `epoch 1 of 1` with the baseline fingerprint, `discrepancies … (unexplained 0, environment change 0)`; the latest run's recorded environment differs from the previous one only in `uv_lock_sha256` (Decision D1). Tag the merge commit for the first deployment: `git tag live-v1 origin/master && git push origin live-v1`.

- [ ] **Step 3: VPS, keys and secrets (user, with the controller reading along)**

Follow `docs/live-trading.md` → One-time setup, steps 1–9: Ubuntu 24.04 US-East VPS (SSH keys only, ufw), unattended-upgrades, Docker, user `ewbot` (uid 1000), `/var/lib/ewbot` and `/etc/ewbot` (mode 700); a **View-only** CDP key bound to the VPS IP for SHADOW (`/etc/ewbot/cdp_api_key.json`, mode 600); healthchecks.io check (period 1 min, grace 2 min, Telegram and email integrations: the email reaches the user when Telegram is unusable); the fine-grained GitHub token (Contents: read on this repository); `/etc/ewbot/live.env` (mode 600; `TELEGRAM_CHAT_ID` = the user's private chat with the bot, a positive id; no webhook or second poller on the bot); `timedatectl` shows the clock synchronised (command ages use it); the repository checkout with a read-only deploy key; `deploy/live/.env` with `EWBOT_EXPIRY`; about $500 in the futures account. The user types every secret; the controller never sees one.

- [ ] **Step 4: Deploy and start SHADOW**

```bash
cd ~/elliott-wave-btc-bot && bash deploy/live/deploy.sh live-v1
docker compose -f deploy/live/docker-compose.yml exec live ewbot live preflight
docker compose -f deploy/live/docker-compose.yml logs --tail 50 live
docker compose -f deploy/live/docker-compose.yml exec live ewbot live status
```

Expected: the preflight prints four `ok` lines (fingerprint `e0435223a3f9`, lock `d1be04baa06c`, key `view=True trade=False transfer=False`, product `BIP-20DEC30-CDE: 0.01 BTC/contract, $5 tick, whole contracts`); Telegram receives `[LIVE] [시작] 단계 SHADOW`; within the next hour the status shows a `마지막 사이클 … OK … 마감 후 N초` with N ≤ 120; healthchecks.io shows the check up; `/status` sent in the private chat is answered within seconds, and `docker compose -f deploy/live/docker-compose.yml logs live | grep -i telegram` shows no `HTTP 401` / `HTTP 409`. Then set up the backups (runbook step 11) and run the backup service once.

- [ ] **Step 5: Re-record the private-endpoint fixtures (first SHADOW week, controller)**

Capture real responses with the View key inside the container and compare them field by field with `tests/live/fixtures/coinbase.json` (positions, balance summary, key permissions, orders batch, product, preview):

```bash
docker compose -f deploy/live/docker-compose.yml exec live python - <<'EOF'
import json
from coinbase.rest import RESTClient
c = RESTClient(key_file="/run/ewbot/cdp_api_key.json", timeout=10)
p = "/api/v3/brokerage"
out = {
    "positions": c.get(p + "/cfm/positions"),
    "balance_summary": c.get(p + "/cfm/balance_summary"),
    "key_permissions": c.get(p + "/key_permissions"),
    "orders_history": c.get(p + "/orders/historical/batch", params={"product_ids": ["BIP-20DEC30-CDE"], "limit": 5}),
    "orders_open": c.get(p + "/orders/historical/batch", params={
        "product_ids": ["BIP-20DEC30-CDE"], "limit": 5,
        "order_status": ["OPEN", "PENDING", "QUEUED", "EDIT_QUEUED", "CANCEL_QUEUED"]}),  # open_orders()' filter
    "preview_ok": c.post(p + "/orders/preview", data={"product_id": "BIP-20DEC30-CDE", "side": "BUY",
                                                      "order_configuration": {"market_market_ioc": {"base_size": "1"}}}),
}
for k in ("portfolio_uuid",):
    out["key_permissions"].pop(k, None)
json.dump(out, open("/data/recorded-private.json", "w"), indent=1)
print(sorted(out))
EOF
```

Copy `/var/lib/ewbot/recorded-private.json` to your laptop, remove any account identifier, and diff its keys and value formats against the fixture entries. A difference in a field the parsers read (`side`, `number_of_contracts`, `avg_entry_price`, `total_usd_balance`, `unrealized_pnl`, `available_margin`, `can_*`, `order_configuration`, `status`, `filled_size`, `average_filled_price`, `total_fees`, `errs`) is a bug: update the fixture with the real shape, adapt `live/coinbase.py`, rerun the tests, redeploy (flat book). If `orders_open` is refused (HTTP 4xx), Coinbase does not combine `order_status` values: make `open_orders()` send one request per resting status before CANARY.

- [ ] **Step 6: Run SHADOW for at least 14 days (controller watches, user restarts once)**

Daily: the `[LIVE] [일일 요약]` (stage, cycle times, the paper comparison `불일치 0건`, previews), `ewbot live status` (the SHADOW gates). After a day or more without any message to the bot, `/status` must still be answered (the stored update offset is then older than 24 h and not sent; Telegram may renumber updates after a quiet week): if it is not, report it. Once during SHADOW, restart for gate ④ - between hh:05 and hh:55, so no cycle is late (gate ② counts every hour of the stage; a runtime down across a bar close fails it) -: `docker compose -f deploy/live/docker-compose.yml restart live`, then check a second `RECOVERY` event without unresolved intents in the status. Any `[판단 불일치]`, `[판단 불일치: 봉 수정]`, `[판단 불일치 확인 중]` that the next hour does not clear, `[모의매매 대조 실패]`, `[사이클 오류]`, `[SHADOW preview 실패]` or `[루프 오류]` message is investigated before CANARY; a fix means a new tag, a flat-book deploy and, per spec §6.2, SHADOW starts over. The latency of the public candle after the close is not documented: if cycles end `HOLD` (`[판단 보류]`) because the bar arrives later than 2 minutes after the close, report it as a finding against gate ② and spec §4.1 — do not move the deadline in code.

- [ ] **Step 7: CANARY gate (around 2026-10-30; user approves explicitly)**

Preconditions (spec §6.1): every SHADOW gate `충족` in `ewbot live status`; the paper runtime's criteria 1, 2 and 4 met (`ewbot paper status` on a worktree of `paper`); the user's explicit approval in the conversation. Then the user creates a **View + Trade** CDP key (never Transfer) bound to the VPS IP and replaces `/etc/ewbot/cdp_api_key.json` (mode 600). No gate can check the IP binding (spec §6.1 `거래 권한 키가 VPS IP에 묶여 있다`): the user confirms in the CDP portal that the key's IP allowlist holds only the VPS's IPv4 and says so in the conversation (the controller records it). Then, with the book flat:

```bash
docker compose -f deploy/live/docker-compose.yml restart live
docker compose -f deploy/live/docker-compose.yml exec live ewbot live preflight
docker compose -f deploy/live/docker-compose.yml exec live ewbot live stage canary --confirm
```

Expected: the preflight's key line reads `view=True trade=True transfer=False`; the stage command prints every gate `[충족]` and `stage SHADOW -> CANARY recorded`; within a minute Telegram shows `[단계 변경] SHADOW → CANARY` and a new `[시작] 단계 CANARY`. If any gate is `[미충족]`, the command refuses: do not work around it.

- [ ] **Step 8: CANARY confirmations (controller records the answers in the project notes)**

During CANARY, confirm the spec §6.2 unknowns from the journal and the Coinbase order history: duplicate `client_order_id` behaviour; whether the brackets survive the Friday 17:00–18:00 New York close; whether `orders/edit` moves a bracket stop (then set `EWBOT_LIVE_EDIT_STOPS=1` in `deploy/live/.env` and redeploy with a flat book); the limit of the first triggered bracket stop (the reference says 5% beyond the stop: `STOP_LIMIT_OFFSET`, also the stop-gap threshold); the reason codes Coinbase gives for a bracket refused because it would exceed the position and for a close refused while brackets rest (`OVERLAP_REFUSALS`, `CLOSE_CONFLICTS` in `live/executor.py`; the `BRACKET_FAILED` / `CLOSE_FAILED` events carry them: correct the sets and redeploy with a flat book); that brackets - a triggered stop resting as a limit included - only reduce at fill time, so a bracket left after a close cannot open or flip a position (closes are sent with the brackets resting); whether a sized `close_position` (a reduction) is refused while our brackets cover the position, and the code it gives (add it to `CLOSE_CONFLICTS`); the actual fee tier (`GET /api/v3/brokerage/transaction_summary`); the measured costs in the daily summaries vs. the paper cost model. CANARY passes with ≥ 3 closed trades (or 90 days) and every CANARY gate `충족` (`ewbot live status`). FULL is not part of this month's schedule: it needs all four Plan 1 §9.6 criteria (incl. 50 out-of-sample trades), the CANARY gates and the user's approval (`ewbot live stage full --confirm`). Two FULL preconditions are not machine-checkable and are recorded in the project notes before it: the spec §2 Coinbase facts re-checked against Coinbase's current documentation (spec §8 `FULL 전에 §2를 다시 확인한다`: product, margin rates, order types, the triggered stop's limit, rate limits, fees, session hours) and the trade key's IP allowlist still holding only the VPS's IPv4 (runbook, Stage transitions).
