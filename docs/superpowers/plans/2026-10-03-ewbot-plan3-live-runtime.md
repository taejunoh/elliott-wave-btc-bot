# ewbot Plan 3 — Live-Trading Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Trade the frozen holdout strategy (A, K4 = 2.0, Q_MIN = 0.7, S_MIN = 0.5 from `holdout.lock`, conservative risk) on the Coinbase CFM nano Bitcoin perp-style future `BIP-20DEC30-CDE` from a US-East VPS, through the stages SHADOW (virtual, previewed orders) → CANARY (always 1 contract) → FULL (frozen sizing, 3-contract cap for 30 days), so that the live decisions provably equal the paper runtime's, every position stays protected through restarts, outages and gaps, and the user can pause or flatten from Telegram at any time.

**Architecture:** Every hour at hh:00:30 the runtime replays the whole frozen engine exactly like the paper runtime (`run_backtest(cfg, df, HOLDOUT_START, None, allow_holdout=True)` inside the paper trace) on the frame plus one flat placeholder bar, and reads the target after the bar that just closed — open position, its stop and TPs, the exit or entry queued at that close (`live/target.py`). A pure planner (`live/reconcile.py`) turns target × exchange state into actions; an executor (`live/executor.py`) sends them through an `Exchange` protocol with intent-before-send idempotency (deterministic `client_order_id`, SQLite journal) — `CoinbaseExchange` (SDK 1.8.4) in CANARY/FULL, `ShadowExchange` (real previews, virtual book) in SHADOW, `SimExchange` in every test. A one-second main loop adds the watchdog (stop gap, margin, account breakers, session), Telegram commands, the external heartbeat, the daily summary and the hourly audit against the paper ledger. No strategy-fingerprinted file is edited: new code lives in `src/ewbot/live/`, plus `live` sub-commands in `cli.py`, `deploy/live/` and an ops runbook.

**Tech Stack:** Python 3.12.13 (uv), the existing dependencies (numpy, pandas, pyarrow, requests, PyYAML) plus `coinbase-advanced-py==1.8.4` in a new `live` dependency group (it brings cryptography, PyJWT, websockets 13.1, backoff); SQLite (stdlib `sqlite3`, WAL); pytest; Docker (`python:3.12.13-slim-bookworm`, `ghcr.io/astral-sh/uv:0.12.13`, both pinned by digest) with Docker Compose on Ubuntu 24.04; Telegram Bot API (`sendMessage`, `getUpdates`); healthchecks.io; age + rclone (Cloudflare R2) for backups.

**Spec:** `docs/superpowers/specs/2026-10-03-ewbot-plan3-live-trading-design.md` (parents: `docs/superpowers/specs/2026-10-01-elliott-wave-btc-bot-design.md`, `docs/superpowers/specs/2026-10-02-ewbot-plan2-paper-trading-design.md`). The spec's §2 Coinbase API facts were researched on 2026-10-03, the day this plan was written, so they stand in for the re-check required by spec §6.1; this plan additionally verified the SDK 1.8.4 source (generic `RESTClient.get/post`, `WSClient`, JWT signing) and recorded the real public `GET /market/products/BIP-20DEC30-CDE` and `/market/product_book` responses used as fixtures.

## Global Constraints

- Work on branch `feat/plan3-live` (from `docs/live-design`, or from `master` once PR #5 with the spec and this plan is merged), from the repository root; the venv must be Python 3.12.13 (`uv run python --version`; if not, `uv venv --python 3.12.13 && uv sync`).
- Tests: `uv run pytest …` in Tasks 1–2, `uv run --group live pytest …` from Task 3 on (it keeps the `live` group installed; a plain `uv sync` later would remove it).
- Never modify a strategy-fingerprinted file (Plan 2 spec §5.1, Plan 3 spec §3.1): `src/ewbot/types.py`, `config.py`, `indicators.py`, `data/candles.py`, `waves/**`, `strategy/**`, `engine/**`, `execution/**`, `risk/**`. Never modify `holdout.lock`, `backtest/**`, `data/coinbase_history.py`, `src/ewbot/paper/**` or `.github/workflows/**`. Allowed: create `src/ewbot/live/**`, `tests/live/**`, `deploy/live/**`, `docs/live-trading.md`; modify `src/ewbot/cli.py`, the one `ALLOWED_TRUE` line of `tests/paper/test_static.py` (Decision D2) and `pyproject.toml` + `uv.lock` only through `uv add --group live coinbase-advanced-py==1.8.4` (Decision D1).
- Fingerprint `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1` over 25 files and lock `sha256` `d1be04baa06c58dd483c88e053841d6eda56c1a560fd3196746bab311ece3c45` are pinned in `live/preflight.py`; the runtime refuses to start when either differs, or when the API key can transfer, or when the product is not 0.01 BTC / $5 tick / whole contracts.
- Frozen inputs: params only from `holdout.lock`; config = `holdout_config(BotConfig(), params)`; replay from the first cached bar with `start_ts = HOLDOUT_START`; signals from the spot `BTC-USD` hourly candles of the public Coinbase Exchange API (`data.coinbase_history.update_cache`, as the paper runtime); no CLI option changes any of it.
- Product `BIP-20DEC30-CDE`: `contract_size 0.01`, `price_increment 5`, `base_increment 1`, margin rates intraday ≈ 0.10, overnight 0.21025 long / 0.256625 short (recorded 2026-10-03). Orders: `market_market_ioc` entries/closes, `trigger_bracket_gtc` protection, `close_position` exits, `orders/preview` in SHADOW.
- Stages: SHADOW = no real order, CANARY sizing (1 contract, skipped above 2% risk) on virtual fills with real previews; CANARY = 1 contract, skip when the 1-contract risk > 2% of equity; FULL = `risk.sizing.size_contracts(real equity, entry, stop, direction, CONSERVATIVE, risk_mult, costs)` capped at 3 for the first 30 days of FULL.
- Timing: cycle at hh:00:30 for the bar that closed at hh:00, retried every 15 s, held at +120 s (protection kept); an entry is never sent later than +300 s after its bar's close; poll every 10 s while a trade is open or an intent is pending (60 s otherwise); stop gap = price beyond the stop's 5% limit for 10 s (Coinbase places a triggered bracket stop as a limit 5% beyond it, Create Order reference; spec §4.2 assumed 1%); unprotected > 60 s = major incident; WebSocket silent > 60 s → REST price every 10 s; heartbeat every 60 s (healthchecks.io period 1 min, grace 2 min); daily summary 09:00 New York; overnight-margin report 15:30 New York; paper audit from minute 50 of each hour.
- Safety limits: one position; margin projected at the overnight rate: entry refused above 70%, warning at 80%, reduce back to ≤ 70% at 90%; real-account breakers −3% from the New York day's first equity and −15% from the peak → flatten and pause.
- Secrets only in `/etc/ewbot/cdp_api_key.json` and `/etc/ewbot/live.env` (mode 600, refused otherwise), mounted read-only; never in the repository, the image or a log; HTTP exceptions that may carry a token are reduced to their type name.
- Tests never touch the network, a real account, the real `data/cache` or `<repo>/holdout.lock`: `SimExchange`, recorded JSON fixtures, fake sessions, synthetic candles (`tests.paper.helpers.synth_frame`) and temp-dir locks. Exception: `check_fingerprint()` hashes the working tree's strategy files (read-only), as the paper fingerprint test does.
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Decisions for the controller (sign off before Task 3 / before merging)

**D1 — the SDK dependency changes `uv.lock`, whichever way it is added; use a `live` dependency group (recommended).** Verified 2026-10-03 with uv 0.12.13 in a scratch copy: `uv add coinbase-advanced-py==1.8.4` (main dependency), `uv add --optional live …` (extra) and `uv add --group live …` (PEP 735 group) all rewrite `uv.lock` the same way — additively (+226 lines: `coinbase-advanced-py`, `cryptography`, `cffi`, `pycparser`, `pyjwt`, `websockets 13.1`, `backoff`), no existing package version changed, no line removed. The paper runtime's environment digest includes `uv_lock_sha256` (`paper/environment.py`), so after the merge the first paper run sees a new environment: a discrepancy found in that one run (none is expected) would be classified `ENVIRONMENT_CHANGE` instead of `UNEXPLAINED`; every later run is classified normally, the epoch and the criterion-4 28-day clock are not touched (only a fingerprint change opens an epoch). The group is the better of the three: `uv sync --frozen` in `paper.yml` and `ci.yml` installs the default groups only (`dev`), so the paper and CI runners keep exactly today's packages (no SDK, no `cryptography` on a job holding `contents: write`), while the Docker image installs `--group live` from the same lock. The zero-change alternative — leave `uv.lock` alone and install the SDK in the image from a hash-pinned `deploy/live/requirements-live.txt` — removes even the one-run window but creates a second lock that can drift from `uv.lock`, and the SDK compatibility test would only run where someone installed the SDK by hand; choose it only if the one-run `ENVIRONMENT_CHANGE` window is unacceptable. Merge timing: right after a paper run with 0 new discrepancies, then check the next run (Task 16 Step 2).

**D2 — `live/target.py` joins the `allow_holdout=True` allowlist.** `tests/paper/test_static.py` asserts the exact set of modules that may replay into the holdout period (`{"backtest/holdout.py", "paper/runner.py"}`). The live runtime has to replay exactly like the paper runtime, so the set grows by exactly this one module (one line in Task 4). Calling `TradingEngine` directly instead would bypass the guard silently. No file under `src/ewbot/paper/` changes.

**D3 — the paper messages keep their prefix.** Spec §3.5 names the prefixes `[LIVE]` and `[PAPER]`; the paper runtime's messages start with `[ewbot paper]` and changing that would edit paper behaviour. The live runtime uses `[LIVE]`; the two stay distinguishable.

## Spec Interpretations (decided here, binding for the tasks)

1. **Target at the latest bar.** The engine never evaluates entries on a run's last bar, so the replay gets one placeholder bar T+1h (flat at T's close, volume 0, synthetic). Everything decided at or before T's close is computed before the engine reaches T+1h, so it equals a replay with the real next bar; Task 4's tests prove it at every event bar of the synthetic series and on a sampled sweep of the other holdout bars (every 23rd, every CFM halt bar and the bar before it). Queued exits are recorded by swapping the engine's per-run class `_Run` (looked up at run time, like the paper trace swaps `PaperBroker` / `WaveEngine`) for a subclass that logs each fresh `_request_exit(reason)` with its bar. Nothing fills during the CFM halt (Fri 17–18 New York), so the target reports the exit still pending for the open position, not only one queued at T: an exit queued at the Fri 16:00 close is still `exit_reason` at the halt bar T = 17:00 (`exit_queued_ts` = 16:00 tells a carried exit from a new one), and a stop the halt bar's spot range crossed is flagged (`stop_pending_reopen`: the paper broker stops the position out at the next tradable open). Reconcile closes on both (`STOP_HALT` for the second) at the first poll that sees the session OPEN. The configuration is not a parameter: `compute_target(lock, df, T)` builds `holdout_config(BotConfig(), params)` from the holdout lock (checked like the paper precheck, without its git checks) and always replays from `HOLDOUT_START`. Nothing at T+1h is used.
2. **Trade identity.** A live trade is identified by its signal bar (`trade_id = signal ts`; the paper trade key is `trade_id + 1h`). Client order ids: entry `ewb-e-<signal ts>-<L|S>`, brackets `ewb-b-<trade>-<tp1|tp2>-<generation>`, closes `ewb-x-<trade>-<n>` (`ewb-x-0-<n>` for a position the journal does not know), reductions `ewb-r-<trade>-<n>`. Within the hour after an entry the replay still shows it as queued (`target.entry`): that queued entry is the engine position of the live trade (`reconcile.pending_position`), never a reason to close.
3. **Never chase.** Only an entry queued at the latest bar can open a trade, and only within 5 minutes of that bar's close; an entry that was skipped (pause, session, size, margin, breaker, missing TP2), refused or unfilled is recorded once and never sent again; a live trade closed early by its bracket is not re-entered. A position the journal does not know is adopted when the engine holds the same direction, else closed (spec §4.4: the actual position follows the target).
4. **Levels.** Futures level = `to_tick(spot level × futures mid / spot mid, $5)`, rounded down for a long and up for a short — for every level: the spec's stop rule, and for a TP it moves the limit toward the entry so the live TP fills no later than the engine's strict-cross TP. The ratio is taken when the order is sent; resting legs are re-placed only when the engine's stop (spot) changes, never for basis drift.
5. **Protection and TP1** (spec §4.2, §4.4). Legs: 1 contract → one bracket (TP1 + stop); more → TP1 size (`engine.position_mgr._tp1_split`) + stop, and the rest + TP2 + stop; after the live TP1 one bracket (rest + TP2) with the stop tightened to at least the live entry price (break-even). A 1-contract CANARY trade therefore exits whole at TP1 while the engine keeps its remainder. When the engine's TP1 has filled but the live TP1 has not, the TP1 size is closed at market (`Reduce TP1_SYNC`, only while the session is OPEN; until then both legs stay). A signal without TP2 is skipped (strategy A always sets one).
6. **Stop moves.** Replace first (new bracket, then cancel the old one; brackets only reduce, spec §4.2); if the venue refuses the overlap, cancel first and track the gap as unprotected time. `orders/edit` on a bracket is used only with `EWBOT_LIVE_EDIT_STOPS=1`, after CANARY confirms it (spec §6.2). With no resting stop and the price through the stop, the watchdog closes at once; with a resting stop gapped past its 5% limit (`STOP_LIMIT_OFFSET`, the Create Order reference; spec §4.2 assumed 1%) for 10 s, it closes at market (spec §4.2).
7. **Idempotency and retries** (spec §4.2, §5.1). Intent before send; a PENDING intent (ambiguous error, killed process) is looked up by `client_order_id` (orders created up to 5 min before it) before the same id is sent again; a duplicate-id answer resolves to the existing order; a PENDING intent not on the exchange after 10 min is ABANDONED (checked every minute). Reads are retried 3 times in the adapter; an ambiguous entry is looked up and re-sent with the same id on the next polls only until 5 minutes after its bar's close, then skipped (`진입이 실패하면 건너뛰고`); a failed close is retried on every poll with a new id, with an alert each time (`청산이 실패하면 계속 재시도하면서 경보`); `close_position` can only reduce, so a late duplicate close cannot open a position. Restart recovery also rebuilds a trade whose entry was accepted but not yet recorded and records a close accepted right before a crash; an accepted entry whose fill could not be read in time is rebuilt from its intent on the next pass (not treated as a foreign position).
8. **Exchange state vs. prices.** Positions and orders are read over REST (every 10 s while a trade is open or an intent is pending): one source of truth for reconcile. Prices for the watchdog come from the WebSocket `ticker` + `heartbeats` channels (SDK `WSClient`), with the spec's fallback: silent for 60 s → REST every 10 s; a reconnect or a `sequence_num` gap forces an immediate REST poll. The WebSocket `user` channel is not used.
9. **SHADOW** runs the complete runtime against `ShadowExchange`: reads (product, session, quotes, balance, key permissions) are real; every order is first checked with the real `POST /orders/preview` (journaled for gate ③) and then executed on a virtual book (`SimExchange`) that follows the real futures price. Sizing is CANARY's, on the real balance (so the account must be funded during SHADOW). After a restart the open virtual position is restored from the journal (`ShadowExchange.restore`) and its brackets are re-placed, so the SHADOW restart test exercises the real recovery path.
10. **Stages** change only through `ewbot live stage <to> --confirm` with a flat book (journal and Coinbase); promotions need every gate (one stage at a time). A running runtime notices the new stage within a minute and exits once flat; Docker (`restart: unless-stopped`) starts it in the new stage. Incidents: in CANARY the CANARY failure events (unprotected > 60 s, audit mismatch, breaker, transfer key, protection slower than 30 s, a foreign order or position) pause entries (spec §6.2 `자동으로 일시 정지`); in FULL the major incidents pause and demote to CANARY. Pause never stops protection or exits.
11. **Gates operationalised.** SHADOW ④ (`장애·재시작 시험`) = at least two `RECOVERY` events in the stage (first start + ≥ 1 restart) and none with an unresolved intent. CANARY ② (`손절 이동과 청산이 목표와 일치`) = no `BRACKET_FAILED`, `CLOSE_FAILED`, `CLOSE_UNCONFIRMED` or `REDUCE_FAILED` event; CANARY ⑤ = at least one `COSTS` event (a daily summary with fills). The paper criteria come from the paper ledger (`paper.criteria.golive_status`).
12. **Audit** (spec §3.5): the journal stores the engine decisions in the paper ledger's format; hourly (from minute 50) they are compared with the paper ledger's current epoch (known_at in (epoch start, epoch through_ts]); any mismatch is alerted once and, in CANARY/FULL, is a major incident.
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
| `src/ewbot/live/feed.py` | 8 | WebSocket ticker feed (SDK `WSClient`), gaps, reconnect backoff |
| `src/ewbot/live/notify.py` | 9 | `[LIVE]` Telegram notifier (paper `TelegramSender`, dedupe) |
| `src/ewbot/live/control.py` | 9 | Telegram commands (long poll, allowlisted chat, persisted offset) |
| `src/ewbot/live/health.py` | 9 | heartbeat pings (healthchecks.io) |
| `src/ewbot/live/audit.py` | 10 | paper-ledger fetch (GitHub contents API) and decision comparison |
| `src/ewbot/live/stage.py` | 10 | stages, gates, pause, incidents, transitions |
| `src/ewbot/live/settings.py` | 11 | settings from the environment, secrets files (mode 600) |
| `src/ewbot/live/preflight.py` | 11 | fingerprint, lock, key, product checks |
| `src/ewbot/live/shadow.py` | 12 | `ShadowExchange` (real reads and previews, virtual book) |
| `src/ewbot/live/runtime.py` | 12 | `LiveRuntime` main loop, `status_lines` |
| `src/ewbot/live/app.py` | 13 | production wiring (exchange, notifier, feed, control, heartbeat, audit) |
| `src/ewbot/cli.py` (modify) | 13 | `ewbot live` sub-commands: run, preflight, status, stage, backup |
| `pyproject.toml`, `uv.lock` (via `uv add`) | 3 | `live` dependency group: `coinbase-advanced-py==1.8.4` |
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
| 3 | Coinbase adapter, `live` dependency group, recorded fixtures | 76 |
| 4 | Target state from the engine replay (+ allowlist line) | 12 |
| 5 | Price mapping, stage sizing, margin projection | 15 |
| 6 | Reconcile planner | 16 |
| 7 | Executor: intent-before-send, protection, closes, recovery | 20 |
| 8 | Watchdog and WebSocket feed | 7 |
| 9 | `[LIVE]` notifier, Telegram control, heartbeat | 6 |
| 10 | Paper audit, stages and gates | 9 |
| 11 | Settings and preflight | 7 |
| 12 | `ShadowExchange` and the runtime loop | 17 |
| 13 | CLI `ewbot live …` and production wiring | 7 |
| 14 | Parity and chaos integration tests | 20 |
| 15 | Docker image, compose, deploy and backup scripts, runbook | 11 |
| 16 | Manual checklist: merge, VPS, SHADOW, CANARY gate (controller and user only) | — |

Baseline before Task 1: `uv run pytest -q` → `620 passed, 1 skipped` without a candle cache (`621 passed` when `data/cache/BTC-USD_1h.parquet` exists). After Task 15: 252 more tests, `872 passed, 1 skipped` (`873 passed` with the cache), about 80 s.

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
- Produces (`ewbot.live.coinbase`): `API = "/api/v3/brokerage"`, `SESSION_PREFIX`, `RETRY_AFTER_MAX_S = 10.0`, `CANCEL_DONE`, `class DuplicateClientOrderId(ExchangeError)`, `fmt_decimal(x) -> str`, `bad_size(size) -> bool`, `bad_price(price) -> bool`, `iso_ts(text) -> int | None`, `retry_after_s(value, now=None) -> float` (delta-seconds or HTTP-date, clamped to [0, 10], garbage → 1), `map_error(exc) -> ExchangeError` (429 → `RateLimited`, 5xx and every other `requests` exception → `Unavailable` with the type name only, a 4xx naming `DUPLICATE_CLIENT_ORDER_ID` → `DuplicateClientOrderId`, anything else → `ExchangeError(type name)`), `parse_order(dict) -> Order` (sides `BUY`/`SELL` only), `parse_place(dict, client_order_id) -> PlaceResult` (reason: `new_order_failure_reason`, then the deprecated `error` and `preview_failure_reason`, then `failure_reason`, skipping `UNKNOWN_*`; `duplicate=True` when any of them is `DUPLICATE_CLIENT_ORDER_ID`; accepted without an order id → `Unavailable`), `class CoinbaseExchange(client, futures_id=FUTURES_PRODUCT, spot_id=SPOT_PRODUCT, *, sleep=time.sleep, clock=time.time, attempts=3, backoff_s=1.0, min_remaining=2)` implementing `Exchange` with the SDK's generic `client.get(path, params=..., public=...)` / `client.post(path, data=...)`, and `CoinbaseExchange.from_key_file(key_file, futures_id, spot_id, timeout=10)`. Reads and `cancel` retry 3 times on 429 / 5xx / network errors; `place_market`, `place_bracket`, `close_position` and `edit_bracket` are sent once. Only `ExchangeError` leaves the adapter (or `ValueError` for a side other than ±1): a reply of the wrong shape is an `ExchangeError`, one that is not a JSON object `Unavailable`. Refused locally without a request: sizes that are not whole contracts ≥ 1 (`INVALID_SIZE_PRECISION`), non-finite or ≤ 0 limit / stop prices (`INVALID_PRICE_PRECISION` / `INVALID_STOP_PRICE_PRECISION`). `position()` maps `LONG`/`SHORT` only and takes `abs()` of the contract count; `open_orders()` asks for every resting status (`order_status` repeated); `balance()` requires `total_usd_balance`; a missing `can_transfer` reads `True`; `cancel()` raises `ExchangeError` for every order whose `results[].success` is false (except `DUPLICATE_CANCEL_REQUEST` / `ORDER_IS_FULLY_FILLED`) or that has no result.

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
    ws = pytest.importorskip("coinbase.websocket")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    assert metadata.version("coinbase-advanced-py") == "1.8.4"
    init = inspect.signature(rest.RESTClient.__init__).parameters
    assert {"key_file", "timeout", "rate_limit_headers"} <= set(init)
    assert {"url_path", "params", "public"} <= set(inspect.signature(rest.RESTClient.get).parameters)
    assert {"url_path", "data"} <= set(inspect.signature(rest.RESTClient.post).parameters)
    for name in ("open", "subscribe", "close"):
        assert callable(getattr(ws.WSClient, name))

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
            return Quote(product_id, _num(bids[0]["price"]), _num(asks[0]["price"]), self.clock())

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

Expected: `76 passed` (with `uv run pytest` and without the group the two SDK tests are skipped: `74 passed, 2 skipped`).

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
- Produces (`ewbot.live.pricing`): `CANARY_MAX_RISK = 0.02`, `FULL_CAP_CONTRACTS = 3`, `FULL_CAP_DAYS = 30`, `MARGIN_ENTRY_MAX = 0.70`, `MARGIN_WARN = 0.80`, `MARGIN_REDUCE = 0.90`; `to_tick(price, tick, direction) -> float` (long floor, short ceil), `basis_ratio(fut_mid, spot_mid) -> float`, `map_level(spot_level, ratio, tick, direction) -> float`, frozen `Levels(stop, tp1, tp2)`, `map_levels(direction, stop, tp1, tp2, ratio, tick) -> Levels`, `stop_limit(stop, direction, offset=0.01) -> float`, `stop_gapped(price, stop, direction, offset=0.01) -> bool`, `risk_per_contract(entry, stop, costs) -> float`, `margin_ratio(contracts, price, side, product, equity, overnight=True) -> float`, `max_contracts_for_margin(price, side, product, equity, limit=0.70) -> int`, `stage_contracts(stage, *, equity, entry, stop, direction, risk_mult, cfg, full_cap=None) -> tuple[int, str]` (0 = skip, with the reason).

- [ ] **Step 1: Write the failing test**

**File: `tests/live/test_pricing.py`**

```python
from __future__ import annotations

import math

import pytest

from ewbot.config import BotConfig
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
    assert stage_contracts("SHADOW", equity=1_000.0, **kw)[0] == 1
    n, why = stage_contracts("CANARY", equity=500.0, **kw)  # risk 10.6 > 10.0
    assert n == 0 and "2%" in why
    assert stage_contracts("CANARY", equity=1_000.0, entry=60_000.0, stop=61_000.0, direction=1,
                           risk_mult=1.0, cfg=CFG)[0] == 0


def test_full_uses_the_frozen_sizing_with_the_conservative_profile_and_the_cap():
    kw = dict(entry=60_000.0, stop=59_400.0, direction=1, risk_mult=1.0, cfg=CFG)
    expected = size_contracts(20_000.0, 60_000.0, 59_400.0, 1, CFG.risk, 1.0, CFG.costs)
    assert expected > 3
    assert stage_contracts("FULL", equity=20_000.0, **kw) == (expected, f"full: {expected}")
    assert stage_contracts("FULL", equity=20_000.0, full_cap=3, **kw)[0] == 3
    assert stage_contracts("FULL", equity=10.0, **kw)[0] == 0
    with pytest.raises(ValueError):
        stage_contracts("PAPER", equity=1.0, **kw)


def test_margin_projection_uses_the_overnight_rate():
    assert margin_ratio(1, 84_000.0, 1, BIP_SPEC, 1_000.0) == pytest.approx(840.0 * 0.21025 / 1_000.0)
    assert margin_ratio(1, 84_000.0, -1, BIP_SPEC, 1_000.0) == pytest.approx(840.0 * 0.256625 / 1_000.0)
    assert margin_ratio(1, 84_000.0, 1, BIP_SPEC, 1_000.0, overnight=False) < margin_ratio(1, 84_000.0, 1, BIP_SPEC, 1_000.0)
    assert margin_ratio(0, 84_000.0, 1, BIP_SPEC, 0.0) == 0.0 and math.isinf(margin_ratio(1, 1.0, 1, BIP_SPEC, 0.0))
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
    if fut_mid <= 0 or spot_mid <= 0:
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
    if equity <= 0:
        return math.inf
    return contracts * product.contract_size * price * product.margin_rate(side, overnight) / equity


def max_contracts_for_margin(price: float, side: int, product: ProductSpec, equity: float,
                             limit: float = MARGIN_ENTRY_MAX) -> int:
    per = product.contract_size * price * product.margin_rate(side, True)
    if per <= 0 or equity <= 0:
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
    """(contracts, reason). 0 contracts = skip the signal (recorded, not traded)."""
    if direction * (entry - stop) <= 0:
        return 0, "stop on the wrong side of the entry"
    if stage in ("SHADOW", "CANARY"):
        risk = risk_per_contract(entry, stop, cfg.costs)
        if risk > CANARY_MAX_RISK * equity:
            return 0, f"1-contract risk {risk:.2f} USD > {CANARY_MAX_RISK:.0%} of equity {equity:.2f}"
        return 1, "canary: 1 contract"
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

Expected: `15 passed`.

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
- Produces (`ewbot.live.reconcile`): `PREFIX = "ewb-"`, `UNPROTECTED_MAX_S = 60.0`; ids `entry_cid(signal_ts, direction)`, `bracket_cid(trade_id, leg, gen)`, `close_cid(trade_id, n)`, `reduce_cid(trade_id, n)`, `is_ours(order) -> bool`; dataclasses `Leg(name, size, limit_price, stop_price, client_order_id="", order_id=None)` and `LiveTrade(trade_id, setup, direction, contracts, tp1_n, entry_price, tp1_price, tp2_price, stop_spot, stop_price, opened_ts, tp1_filled=False, legs=[], gen=0, closes=0, protected_ts=None, unprotected_since=None, incident_unprotected=False, status="OPEN", exit_reason=None)` with `to_json()` / `from_json(dict)`; frozen actions `Close(reason)`, `Reduce(size, reason)`, `Protect(legs: tuple[Leg, ...], stop_spot, stop_price)`, `Enter(entry: TargetEntry, contracts)`, `Skip(signal_ts, reason)`, `Cancel(order_ids: tuple, reason)`, `Adopt(target: TargetPosition)`, `Alert(kind, detail)` and `Action` (their union); frozen `PlanContext(now, ratio, tick, session_open=True, entry_contracts=0, entry_block=None)`; `observe(trade, position, orders, now) -> (LiveTrade | None, list[(event kind, data)])` (events `CLOSED`, `TP1_FILLED`, `UNPROTECTED`, `UNPROTECTED_60S`, `PROTECTED`), `desired_legs(trade, contracts, stop_spot, ratio, tick) -> (list[Leg], futures stop)`, `legs_match(trade, orders, desired) -> bool`, `pending_position(entry) -> TargetPosition`, `engine_position(target) -> TargetPosition | None` (none while an exit is queued or the halt bar crossed the stop), `plan(target, trade, position, orders, ctx) -> list[Action]` (pure; never mutates its inputs; an exit carried across the CFM halt closes like one queued at the last close, `stop_pending_reopen` closes with reason `STOP_HALT`; the executor sends a close only while the session is OPEN).

- [ ] **Step 1: Write the failing test**

**File: `tests/live/test_reconcile.py`**

```python
from __future__ import annotations

import dataclasses

from ewbot.live.exchange import Order, Position
from ewbot.live.reconcile import (
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
    bracket_cid,
    close_cid,
    desired_legs,
    engine_position,
    entry_cid,
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
    assert plan(target(position=p), None, pos, [], CTX) == [Adopt(p)]
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
    assert observe(None, None, [], 1.0) == (None, [])
    assert tr.status == "OPEN" and not tr.tp1_filled  # the input is not modified
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

  1. `observe` folds exchange facts into the trade: flat -> CLOSED; fewer contracts than entered ->
     the TP1 leg (or our TP1 sync) has filled; open legs that do not cover the position -> unprotected
     since now (a major incident after 60 s, spec 6.2).
  2. `plan` returns the actions, in order:
       - a position the journal does not know: adopt it when the engine holds the same direction,
         else close it (spec 4.4: the actual position always follows the target);
       - within the hour after an entry the replay still shows it as queued (`target.entry`): the live
         trade of that signal is the engine position (`pending_position`), never a reason to close;
       - flat: cancel our leftover orders; enter when the engine queued an entry at the last close;
       - engine flat / another trade / exit queued (TIME, LINEAGE, BREAKER, FLIP, ...): close, then
         enter if the engine also queued an entry (the executor enters only after the close is
         confirmed, spec 4.2 `반대 신호 전환`). An exit queued before the CFM halt is still queued at
         the halt bar (`exit_queued_ts` < bar) and a stop the halt bar crossed stops the engine out at
         the reopen (`stop_pending_reopen`, reason STOP_HALT): both close as well - the executor sends
         the close at the first poll that sees the session OPEN, whichever target is current then;
       - engine TP1 done but the live TP1 leg not filled: reduce by the TP1 size at market (spec 4.4);
       - protection: the brackets must equal the desired legs (spec 4.2): two legs (TP1 size + stop,
         rest + TP2 + stop) before TP1, one leg (rest + TP2 + stop) after it, one leg (TP1 + stop) for
         1 contract; after the live TP1 the stop is at least break-even (live entry price).
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


# ----------------------------------------------------------------------------- observe


def _open_legs(trade: LiveTrade, orders: list[Order]) -> list[tuple[Leg, Order]]:
    by_id = {o.order_id: o for o in orders if o.is_open}
    return [(leg, by_id[leg.order_id]) for leg in trade.legs if leg.order_id in by_id]


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
    if not t.tp1_filled and t.contracts > 1 and position.contracts < t.contracts:
        t.tp1_filled = True
        events.append(("TP1_FILLED", {"trade_id": t.trade_id, "contracts": position.contracts}))
    covered = sum(o.size - o.filled for leg, o in _open_legs(t, orders) if o.stop_price is not None)
    if covered >= position.contracts:
        if t.unprotected_since is not None:
            events.append(("PROTECTED", {"trade_id": t.trade_id, "after_s": round(now - t.unprotected_since, 3)}))
        t.unprotected_since = None
    else:
        if t.unprotected_since is None:
            t.unprotected_since = now
            events.append(("UNPROTECTED", {"trade_id": t.trade_id, "covered": covered, "position": position.contracts}))
        elif now - t.unprotected_since > UNPROTECTED_MAX_S and not t.incident_unprotected:
            t.incident_unprotected = True
            events.append(("UNPROTECTED_60S", {"trade_id": t.trade_id, "since": t.unprotected_since}))
    return t, events


# ----------------------------------------------------------------------------- desired legs


def desired_legs(trade: LiveTrade, contracts: int, stop_spot: float, ratio: float | None, tick: float
                 ) -> tuple[list[Leg], float]:
    """The brackets that must rest for `contracts` contracts, and their futures stop. The stored futures
    stop is kept while the engine stop is unchanged (no churn when only the basis moves)."""
    d = trade.direction
    if stop_spot == trade.stop_spot or ratio is None:
        stop = trade.stop_price
    else:
        stop = map_level(stop_spot, ratio, tick, d)
    if trade.tp1_filled:
        be = to_tick(trade.entry_price, tick, d)
        stop = max(stop, be) if d > 0 else min(stop, be)
    if trade.contracts == 1:
        return [Leg("tp1", contracts, trade.tp1_price, stop)], stop
    if not trade.tp1_filled and contracts == trade.contracts:
        assert trade.tp2_price is not None
        return [Leg("tp1", trade.tp1_n, trade.tp1_price, stop),
                Leg("tp2", contracts - trade.tp1_n, trade.tp2_price, stop)], stop
    assert trade.tp2_price is not None
    return [Leg("tp2", contracts, trade.tp2_price, stop)], stop


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
        if ctx.entry_block is not None:
            return [Skip(e.signal_ts, ctx.entry_block)]
        if e.tp2 is None:
            return [Skip(e.signal_ts, "the engine signal has no TP2 (a second bracket needs one)")]
        return [Enter(e, ctx.entry_contracts)]

    if position is not None and open_trade is None:  # a position the journal does not know
        if target is None:
            return acts + [Alert("UNKNOWN_POSITION", f"{position.side:+d} x{position.contracts}: waiting for a target")]
        tp = engine_position(target)
        if tp is not None and tp.direction == position.side:
            return acts + [Adopt(tp)]
        return acts + [Close("UNKNOWN_POSITION")]

    if position is None:
        if ours:
            acts.append(Cancel(tuple(o.order_id for o in ours), "FLAT"))
        return acts + entry_actions()

    assert open_trade is not None
    if position.side != open_trade.direction:
        return acts + [Close("DIRECTION_MISMATCH")]

    contracts = position.contracts
    stop_spot = open_trade.stop_spot
    if target is not None:
        tp = target.position
        queued = target.entry is not None and target.entry.signal_ts == open_trade.trade_id
        if queued:  # entered at this hour's open; the replay still shows the queued entry
            tp = pending_position(target.entry)  # type: ignore[arg-type]
        elif (tp is None or tp.signal_ts != open_trade.trade_id or target.exit_reason is not None
              or target.stop_pending_reopen):
            if target.exit_reason is not None:  # queued at the last close, or before the halt (carried)
                reason = target.exit_reason
            elif tp is None:
                reason = "ENGINE_FLAT"
            elif tp.signal_ts != open_trade.trade_id:
                reason = "ENGINE_OTHER_TRADE"
            else:
                reason = "STOP_HALT"
            return acts + [Close(reason)] + entry_actions()
        assert tp is not None
        if (tp.tp1_done and not open_trade.tp1_filled and open_trade.contracts > 1
                and contracts == open_trade.contracts and ctx.session_open):  # market orders need an OPEN session
            acts.append(Reduce(open_trade.tp1_n, "TP1_SYNC"))
            contracts -= open_trade.tp1_n
            open_trade = LiveTrade.from_json({**open_trade.to_json(), "tp1_filled": True})
        stop_spot = tp.stop

    desired, stop = desired_legs(open_trade, contracts, stop_spot, ctx.ratio, ctx.tick)
    if not legs_match(open_trade, orders, desired) or stop_spot != open_trade.stop_spot:
        acts.append(Protect(tuple(desired), stop_spot, stop))
    return acts
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run --group live pytest tests/live/test_reconcile.py -q
```

Expected: `16 passed`.

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
- Create: `tests/live/helpers.py` (shared test builders: `T0`, `Clock`, `Alerts`, `sim`, `journal`, `entry`, `position`, `target`, `ctx`)
- Test: `tests/live/test_executor.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 5, 6 (`Exchange`, `ExchangeError`, `Order`, `PlaceResult`, `SimExchange`, `SimCrash`, `Journal`, `map_levels`, every `reconcile` name), `ewbot.live.target.TargetPosition`, `ewbot.engine.position_mgr._tp1_split` (read-only import of the engine's TP1 split).
- Produces (`ewbot.live.executor`): `FILL_TIMEOUT_S = 10.0`, `FLAT_TIMEOUT_S = 30.0`, `POLL_S = 0.5`, `LOOKUP_WINDOW_S = 300.0`, `ABANDON_AFTER_S = 600.0`, journal keys `TRADE_KEY = "trade"`, `SKIPS_KEY`, `ORPHAN_KEY`; frozen `Sent(status: "ACCEPTED"|"REJECTED"|"PENDING", order_id=None, error=None)`; `class Executor(exchange, journal, *, clock=time.time, sleep=time.sleep, alert=None, tp1_frac=0.5, edit_stops=False, fill_timeout=10.0, flat_timeout=30.0, poll_s=0.5)` with `trade() -> LiveTrade | None`, `save(trade)`, `event(kind, data, text=None)` (journal + alert when `text`), `send(cid, kind, trade_id, payload, call) -> Sent`, `execute(actions, ctx)`, `enter(Enter, ctx) -> LiveTrade | None`, `adopt(TargetPosition, ctx)` (rebuilds our own accepted entry from its intent, else adopts a foreign position), `protect(Protect, ctx)`, `close(reason, ctx) -> bool` (flat confirmed; a refused bracket cancel is recorded as `CANCEL_FAILED` and never blocks the close), `reduce(size, reason, ctx)`, `flatten(reason, ctx) -> bool`, `resolve_pending() -> (counts, CLOSE intents found)` (no event; the runtime calls it every minute while intents are pending), `recover() -> {"accepted", "abandoned", "unknown", "rebuilt"}` (startup; writes `RECOVERY`). Journal events written: `ENTRY` (`trade_id, side, contracts, price, ref, fee, order_id`), `PROTECTED_FIRST` (`after_entry_s`), `STOP_MOVED` (`stop, stop_spot`), `STOP_EDITED`, `LEG_GAP`, `BRACKET_FAILED`, `CLOSED` (`trade_id, reason, price, contracts, side, fee`), `CLOSE_FAILED`, `CLOSE_UNCONFIRMED`, `REDUCED`, `REDUCE_FAILED`, `ENTRY_FAILED`, `ENTRY_UNFILLED`, `ENTRY_UNCONFIRMED`, `CANCEL_FAILED`, `SKIP`, `ADOPTED`, `RECOVERED_ENTRY`, `RECOVERY`, `CANCELLED`, `ACTION_ERROR`, and once-only alert events (`FOREIGN_ORDERS`, `UNKNOWN_POSITION`, `CLOSE_DEFERRED`).

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
        session_open: bool = True) -> PlanContext:
    return PlanContext(now=clock(), ratio=ratio, tick=5.0, session_open=session_open, entry_contracts=contracts,
                       entry_block=block)
```

**File: `tests/live/test_executor.py`**

```python
from __future__ import annotations

import pytest

from ewbot.live.exchange import ExchangeError, RateLimited, Unavailable
from ewbot.live.executor import Executor
from ewbot.live.reconcile import Alert, Close, Enter, Protect, Reduce, Skip, desired_legs, plan
from ewbot.live.sim import SimCrash
from tests.live.helpers import T0, Alerts, Clock, ctx, entry, journal, position, sim, target


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


def test_a_refused_overlap_falls_back_to_cancel_then_place(tmp_path):
    clock = Clock()
    ex = sim(clock, reject_overlap=True)
    jr = journal(tmp_path)
    exe = Executor(ex, jr, clock=clock, sleep=clock.sleep)
    tr = exe.enter(Enter(entry(), 3), ctx(clock))
    legs, stop = desired_legs(tr, 3, 59_500.0, 1.0, 5.0)
    exe.protect(Protect(tuple(legs), 59_500.0, stop), ctx(clock))
    assert sorted((o.size, o.stop_price) for o in ex.open_orders()) == [(1, 59_500.0), (2, 59_500.0)]
    assert len(jr.events(kind="LEG_GAP")) == 2
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


def test_a_refused_bracket_cancel_does_not_block_the_close(env):
    clock, ex, jr, _, exe = env
    exe.enter(Enter(entry(), 3), ctx(clock))
    ex.fail_next("cancel", ExchangeError("cancel refused: sim-2: NOT_ALLOWED_TO_CANCEL"))
    assert exe.close("TIME", ctx(clock)) is True  # brackets only reduce: the close goes out anyway
    assert ex.position() is None and ex.open_orders() == []
    assert jr.events(kind="CANCEL_FAILED")[0]["data"]["error"].startswith("cancel refused")


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
trigger_bracket_gtc legs. Stop moves edit the resting bracket when `edit_stops` is on (unconfirmed for
CFM brackets: off until CANARY confirms it), otherwise the new bracket is placed first and the old
one cancelled (brackets only reduce, so a short overlap cannot over-close); if the exchange refuses the
overlap, the old one is cancelled first and the gap is tracked as unprotected time. Closes cancel our
brackets (a refused cancel is recorded, CANCEL_FAILED, and never blocks the close) and call
close_position; the flat position is awaited (30 s). Market orders wait for an OPEN
session; a deferred close is retried on every pass (spec 4.5: `휴장 중에 필요해진 청산은 재개장 직후`).

The same code runs every stage: in SHADOW the exchange is a ShadowExchange (virtual fills, real
previews)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from ewbot.engine.position_mgr import _tp1_split
from ewbot.live.exchange import Exchange, ExchangeError, Order, PlaceResult
from ewbot.live.journal import Journal
from ewbot.live.pricing import map_levels
from ewbot.live.reconcile import (
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
    bracket_cid,
    close_cid,
    desired_legs,
    entry_cid,
    is_ours,
    reduce_cid,
)
from ewbot.live.target import TargetPosition

FILL_TIMEOUT_S = 10.0
FLAT_TIMEOUT_S = 30.0
POLL_S = 0.5
LOOKUP_WINDOW_S = 300.0  # find_order searches orders created up to 5 min before the intent
ABANDON_AFTER_S = 600.0  # a PENDING intent not found on the exchange after 10 min was never sent
TRADE_KEY = "trade"
SKIPS_KEY = "skipped_signals"
ORPHAN_KEY = "orphan_closes"

Alerter = Callable[[str, str], None]


@dataclass(frozen=True)
class Sent:
    status: str  # ACCEPTED | REJECTED | PENDING
    order_id: str | None = None
    error: str | None = None


def _side(d: int) -> str:
    return "롱" if d > 0 else "숏"


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

    # ------------------------------------------------------------------ state
    def trade(self) -> LiveTrade | None:
        data = self.journal.get(TRADE_KEY)
        return None if data is None else LiveTrade.from_json(data)

    def save(self, trade: LiveTrade | None) -> None:
        self.journal.put(TRADE_KEY, None if trade is None else trade.to_json())

    def event(self, kind: str, data: dict[str, Any], text: str | None = None) -> None:
        self.journal.add_event(self.clock(), kind, data)
        if text is not None:
            self.alert(kind, text)

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

    def _await_flat(self) -> bool:
        deadline = self.clock() + self.flat_timeout
        while True:
            if self.ex.position() is None:
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(self.poll_s)

    # ------------------------------------------------------------------ actions
    def execute(self, actions: Sequence[Action], ctx: PlanContext) -> None:
        closed = True
        for a in actions:
            try:
                if isinstance(a, Alert):
                    self._alert_once(a.kind, a.detail)
                elif isinstance(a, Skip):
                    self._skip(a)
                elif isinstance(a, Cancel):
                    self.ex.cancel(list(a.order_ids))
                    self.event("CANCELLED", {"order_ids": list(a.order_ids), "reason": a.reason})
                elif isinstance(a, Close):
                    closed = self.close(a.reason, ctx)
                elif isinstance(a, Reduce):
                    self.reduce(a.size, a.reason, ctx)
                elif isinstance(a, Protect):
                    self.protect(a, ctx)
                elif isinstance(a, Adopt):
                    self.adopt(a.target, ctx)
                elif isinstance(a, Enter):
                    if closed:
                        self.enter(a, ctx)
                    else:
                        self._skip(Skip(a.entry.signal_ts, "the previous position is not closed yet (flip)"))
            except ExchangeError as exc:
                self.event("ACTION_ERROR", {"action": type(a).__name__, "error": str(exc)[:200]},
                           f"[주문 오류] {type(a).__name__}: {str(exc)[:200]}")
                if isinstance(a, Close):
                    closed = False

    def _alert_once(self, kind: str, detail: str) -> None:
        key = f"alerted:{kind}:{detail}"
        if not self.journal.get(key, False):
            self.journal.put(key, True)
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
            self.event("ENTRY_UNCONFIRMED", {"signal_ts": e.signal_ts, "error": str(exc)[:200]})
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
        n = int(order.filled)
        return LiveTrade(
            trade_id=int(payload["signal_ts"]), setup=str(payload["setup"]), direction=int(payload["direction"]),
            contracts=n, tp1_n=_tp1_split(n, self.tp1_frac), entry_price=float(order.avg_fill_price or 0.0),
            tp1_price=float(payload["tp1"]), tp2_price=None if payload["tp2"] is None else float(payload["tp2"]),
            stop_spot=float(payload["stop_spot"]), stop_price=float(payload["stop"]), opened_ts=self.clock(),
        )

    def adopt(self, tp: TargetPosition, ctx: PlanContext) -> None:
        """Take over a position the trade state does not know. Our own accepted entry for that signal (its
        fill could not be read in time) is rebuilt from its intent; anything else is a foreign position
        (event ADOPTED: manual intervention)."""
        pos = self.ex.position()
        if pos is None or ctx.ratio is None:
            return
        own = self.journal.intent(entry_cid(tp.signal_ts, tp.direction))
        if own is not None and own["status"] == "ACCEPTED" and own["order_id"]:
            order = self.ex.get_order(str(own["order_id"]))
            if order.filled > 0:
                trade = self._trade_from(own["payload"], order)
                self.save(trade)
                self.event("RECOVERED_ENTRY", {"trade_id": trade.trade_id},
                           f"[복구] 진입 {trade.trade_id} {_side(trade.direction)} {trade.contracts}계약 복원")
                legs, stop = desired_legs(trade, pos.contracts, trade.stop_spot, ctx.ratio, ctx.tick)
                self.protect(Protect(tuple(legs), trade.stop_spot, stop), ctx)
                return
        lv = map_levels(tp.direction, tp.stop, tp.tp1, tp.tp2, ctx.ratio, ctx.tick)
        trade = LiveTrade(
            trade_id=tp.signal_ts, setup=tp.setup, direction=tp.direction, contracts=pos.contracts,
            tp1_n=_tp1_split(pos.contracts, self.tp1_frac), entry_price=pos.avg_entry, tp1_price=lv.tp1,
            tp2_price=lv.tp2, stop_spot=tp.stop, stop_price=lv.stop, opened_ts=self.clock(),
            tp1_filled=tp.tp1_done and pos.contracts > 1,
        )
        self.save(trade)
        self.event("ADOPTED", {"trade_id": trade.trade_id, "contracts": pos.contracts},
                   f"[경고] 저널에 없던 포지션 {_side(pos.side)} {pos.contracts}계약을 엔진 거래 {tp.signal_ts}로 인수")
        legs, stop = desired_legs(trade, pos.contracts, trade.stop_spot, ctx.ratio, ctx.tick)
        self.protect(Protect(tuple(legs), trade.stop_spot, stop), ctx)

    # protection --------------------------------------------------------------
    def _place_leg(self, trade: LiveTrade, want: Leg) -> Leg | None:
        trade.gen += 1
        cid = bracket_cid(trade.trade_id, want.name, trade.gen)
        leg = Leg(want.name, want.size, want.limit_price, want.stop_price, cid, None)
        self.save(trade)  # the generation is persisted before the request
        sent = self.send(cid, "BRACKET", trade.trade_id,
                         {"leg": want.name, "size": want.size, "limit": want.limit_price, "stop": want.stop_price},
                         lambda: self.ex.place_bracket(cid, -trade.direction, want.size, want.limit_price, want.stop_price))
        if sent.status == "ACCEPTED":
            leg.order_id = sent.order_id
            return leg
        self.event("BRACKET_FAILED", {"trade_id": trade.trade_id, "leg": want.name, "status": sent.status,
                                      "error": sent.error},
                   f"[보호 주문 실패] {want.name} {want.size}계약 손절 {want.stop_price:,.0f}: {sent.status} {sent.error}")
        return None

    def protect(self, a: Protect, ctx: PlanContext) -> None:
        trade = self.trade()
        if trade is None or trade.status != "OPEN":
            return
        orders = {o.order_id: o for o in self.ex.open_orders()}
        kept: list[Leg] = []
        for want in a.legs:
            cur = next((leg for leg in trade.legs if leg.name == want.name and leg.order_id in orders), None)
            if cur is not None:
                o = orders[str(cur.order_id)]
                if (o.size - o.filled, o.limit_price, o.stop_price) == (want.size, want.limit_price, want.stop_price):
                    kept.append(cur)
                    continue
                if (self.edit_stops and (o.size - o.filled, o.limit_price) == (want.size, want.limit_price)
                        and self.ex.edit_bracket(str(cur.order_id), want.size, want.limit_price, want.stop_price)):
                    kept.append(Leg(cur.name, want.size, want.limit_price, want.stop_price, cur.client_order_id, cur.order_id))
                    self.event("STOP_EDITED", {"trade_id": trade.trade_id, "leg": want.name, "stop": want.stop_price})
                    continue
            placed = self._place_leg(trade, want)
            if placed is None and cur is not None:  # the venue refused the overlap: cancel first, then place
                self.ex.cancel([str(cur.order_id)])
                self.event("LEG_GAP", {"trade_id": trade.trade_id, "leg": want.name})
                cur = None
                placed = self._place_leg(trade, want)
            if placed is not None:
                if cur is not None:
                    self._cancel_quietly([str(cur.order_id)], trade.trade_id)
                kept.append(placed)
            elif cur is not None:
                kept.append(cur)
        keep = {leg.order_id for leg in kept}
        stale = [oid for oid, o in orders.items() if is_ours(o) and o.kind == "BRACKET" and oid not in keep]
        if stale:
            self._cancel_quietly(stale, trade.trade_id)
        moved = a.stop_price != trade.stop_price
        trade.legs, trade.stop_spot, trade.stop_price = kept, a.stop_spot, a.stop_price
        if trade.protected_ts is None and kept and len(kept) == len(a.legs):
            trade.protected_ts = self.clock()
            self.event("PROTECTED_FIRST", {"trade_id": trade.trade_id,
                                           "after_entry_s": round(trade.protected_ts - trade.opened_ts, 3)})
        elif moved:
            self.event("STOP_MOVED", {"trade_id": trade.trade_id, "stop": a.stop_price, "stop_spot": a.stop_spot},
                       f"[손절 이동] {trade.setup} {_side(trade.direction)} → {a.stop_price:,.0f}")
        self.save(trade)

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
        ours = [o.order_id for o in self.ex.open_orders() if is_ours(o)]
        if ours:  # a refused cancel never blocks the close: brackets only reduce
            self._cancel_quietly(ours, trade.trade_id if trade is not None else 0)
        open_trade = trade if trade is not None and trade.status == "OPEN" else None
        if open_trade is not None:
            open_trade.closes += 1
            n, tid = open_trade.closes, open_trade.trade_id
            self.save(open_trade)
        else:
            n, tid = int(self.journal.get(ORPHAN_KEY, 0)) + 1, 0
            self.journal.put(ORPHAN_KEY, n)
        cid = close_cid(tid, n)
        sent = self.send(cid, "CLOSE", tid, {"reason": reason}, lambda: self.ex.close_position(cid))
        if sent.status != "ACCEPTED":
            self.event("CLOSE_FAILED", {"trade_id": tid, "reason": reason, "status": sent.status, "error": sent.error},
                       f"[청산 실패 - 재시도 중] {reason}: {sent.status} {sent.error}")
            return False
        if not self._await_flat():
            self.event("CLOSE_UNCONFIRMED", {"trade_id": tid, "reason": reason}, f"[청산 미확인 - 재시도 중] {reason}")
            return False
        if open_trade is not None:
            open_trade.status, open_trade.exit_reason = "CLOSED", reason
            self.save(open_trade)
        fill = self._fill_of(sent.order_id)
        self.event("CLOSED", {"trade_id": tid, "reason": reason, "order_id": sent.order_id, **fill},
                   f"[청산] {reason}" + (f" @ {fill['price']:,.1f}" if fill.get("price") else ""))
        return True

    def _fill_of(self, order_id: str | None) -> dict[str, Any]:
        """Fill price, size and fee of a market order, for the cost report (best effort)."""
        if not order_id:
            return {}
        try:
            o = self.ex.get_order(order_id)
        except ExchangeError:
            return {}
        return {"price": o.avg_fill_price, "contracts": o.filled, "side": o.side, "fee": o.fee}

    def reduce(self, size: int, reason: str, ctx: PlanContext) -> None:
        trade = self.trade()
        if trade is None or trade.status != "OPEN" or not ctx.session_open:
            return
        trade.closes += 1
        self.save(trade)
        cid = reduce_cid(trade.trade_id, trade.closes)
        sent = self.send(cid, "REDUCE", trade.trade_id, {"size": size, "reason": reason},
                         lambda: self.ex.close_position(cid, size))
        if sent.status == "ACCEPTED":
            if reason == "TP1_SYNC":
                trade.tp1_filled = True
            else:  # e.g. MARGIN: the trade itself is smaller now (not a TP1: the stop stays where it is)
                trade.contracts = max(trade.contracts - size, 0)
                if not trade.tp1_filled:
                    trade.tp1_n = _tp1_split(trade.contracts, self.tp1_frac)
            self.save(trade)
            self.event("REDUCED", {"trade_id": trade.trade_id, "size": size, "reason": reason,
                                   **self._fill_of(sent.order_id)},
                       f"[수량 축소] {size}계약 · {reason}")
        else:
            self.event("REDUCE_FAILED", {"trade_id": trade.trade_id, "error": sent.error},
                       f"[수량 축소 실패] {reason}: {sent.error}")

    def flatten(self, reason: str, ctx: PlanContext) -> bool:
        """Cancel every order of ours and close the whole position (/flatten, breakers, margin)."""
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

    def recover(self) -> dict[str, int]:
        """At startup: resolve PENDING intents, rebuild the trade from an accepted entry the journal did not
        get to record, record a close accepted right before a crash. A bracket accepted right before a crash
        is not in the trade's legs: the next protect() cancels it as a stale order of ours."""
        out, closes = self.resolve_pending()
        trade = self.trade()
        if trade is not None and trade.status == "OPEN" and self.ex.position() is None:
            mine = [r for r in closes if r["trade_id"] == trade.trade_id]
            if mine:  # killed after our close was accepted: record it with its reason
                trade.status, trade.exit_reason = "CLOSED", str(mine[-1]["payload"].get("reason"))
                self.save(trade)
                self.event("CLOSED", {"trade_id": trade.trade_id, "reason": trade.exit_reason, "recovered": True})
        entries = [r for r in self.journal.intents(status="ACCEPTED", kind="ENTRY")]
        if entries and (trade is None or trade.trade_id != entries[-1]["trade_id"]):
            row = entries[-1]
            order = self.ex.get_order(str(row["order_id"]))
            pos = self.ex.position()
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

Expected: `20 passed`.

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
- Test: `tests/live/test_watchdog.py`

**Interfaces:**
- Consumes: `ewbot.config.BreakerParams` (daily 3%, total 15%), `ewbot.live.exchange`, `ewbot.live.journal.Journal`, `ewbot.live.pricing` (margin constants, `margin_ratio`, `max_contracts_for_margin`, `stop_gapped`), `tests.live.helpers` (`Clock`, `journal`, `sim`).
- Produces (`ewbot.live.watchdog`): `NY`, `GAP_GRACE_S = 10.0`, `WS_STALE_S = 60.0`, `REST_POLL_S = 10.0`, `MAINTENANCE_NOTICE_S`; `class StopGapWatch(grace_s=10.0)` with `check(direction, stop_price, price, now) -> bool` and `reset()`; frozen `MarginStatus(ratio, level: "OK"|"WARN"|"REDUCE", reduce_by=0)`, `margin_status(contracts, price, side, product, equity) -> MarginStatus`; `ny_date(ts) -> str`; `class AccountGuard(journal, params=None)` (journal key `account_guard`) with `state()`, `update(equity, now) -> "DAILY_LOSS" | "TOTAL_DD" | None` (newly tripped only), `reset(equity, now)`; `class SessionWatch` with `update(info, now) -> list[("SESSION" | "MAINTENANCE", text)]`; `class PriceSource(exchange, feed=None, *, clock, stale_s=60.0, poll_s=10.0)` with `futures_price() -> float | None` and `.mode` (`"ws"` / `"rest"`).
- Produces (`ewbot.live.feed`): `CHANNELS = ("ticker", "heartbeats")`, frozen `Tick(price, ts)`, `class TickerFeed(products, ws_factory, *, clock=time.time, sleep=time.sleep, max_backoff=60.0)` with `on_message(raw)`, `latest(product_id) -> Tick | None`, `age() -> float | None`, `take_resync() -> bool`, `run()`, `start() -> Thread`, `stop()`, attributes `gaps`, `connects`; `sdk_ws_factory(key_file) -> factory(on_message=...)` (SDK `WSClient`, imported lazily).

- [ ] **Step 1: Write the failing test**

**File: `tests/live/test_watchdog.py`**

```python
from __future__ import annotations

import json

import pytest

from ewbot.live.exchange import SessionInfo, Unavailable
from ewbot.live.feed import TickerFeed
from ewbot.live.sim import BIP_SPEC
from ewbot.live.watchdog import AccountGuard, PriceSource, SessionWatch, StopGapWatch, margin_status
from tests.live.helpers import Clock, journal, sim

NY_NOON = 1_791_043_200  # 2026-10-03 12:00 New York (16:00 UTC)


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


def test_margin_levels_use_the_overnight_rate():
    assert margin_status(1, 84_000.0, 1, BIP_SPEC, 1_000.0).level == "OK"  # 17.7%
    assert margin_status(5, 84_000.0, 1, BIP_SPEC, 1_000.0).level == "WARN"  # 88.3%
    st = margin_status(6, 84_000.0, 1, BIP_SPEC, 1_000.0)  # 106%
    assert st.level == "REDUCE" and st.reduce_by == 3  # back to 3 contracts (<= 70%)
    assert margin_status(1, 84_000.0, 1, BIP_SPEC, 100.0).reduce_by == 1  # nothing fits: close it all


def test_account_guard_trips_daily_and_total_once_and_survives_a_restart(tmp_path):
    jr = journal(tmp_path)
    g = AccountGuard(jr)
    assert g.update(10_000.0, NY_NOON) is None
    assert g.update(9_701.0, NY_NOON + 60) is None
    assert g.update(9_700.0, NY_NOON + 120) == "DAILY_LOSS"
    assert g.update(9_600.0, NY_NOON + 180) is None  # once per New York day
    g2 = AccountGuard(jr)  # restart
    assert g2.state()["daily_tripped"] and g2.state()["peak"] == 10_000.0
    assert g2.update(9_600.0, NY_NOON + 86_400) is None  # next day: new reference 9_600
    assert g2.update(8_500.0, NY_NOON + 86_460) == "TOTAL_DD"  # 15% below the 10_000 peak
    assert g2.update(8_000.0, NY_NOON + 86_520) is None
    g2.reset(8_000.0, NY_NOON + 86_580)
    assert g2.state()["peak"] == 8_000.0 and not g2.state()["total_tripped"]
    jr.close()


def test_session_watch_reports_changes_and_maintenance_once():
    w = SessionWatch()
    start = NY_NOON + 3_600
    assert w.update(SessionInfo("OPEN", start, start + 3 * 3600), NY_NOON) == [
        ("MAINTENANCE", "정기 점검 예정 2026-10-03 13:00 ~ 16:00 (뉴욕)")]
    assert w.update(SessionInfo("OPEN", start, start + 3 * 3600), NY_NOON + 60) == []
    assert w.update(SessionInfo("CLOSE"), NY_NOON + 120) == [("SESSION", "장 운영 상태 OPEN → CLOSE")]
    assert w.update(SessionInfo("OPEN", NY_NOON + 3 * 86_400), NY_NOON + 180)[0][0] == "SESSION"  # too far ahead


def ticker(product: str, price: float, seq: int) -> str:
    return json.dumps({"channel": "ticker", "sequence_num": seq,
                       "events": [{"type": "update", "tickers": [{"type": "ticker", "product_id": product, "price": str(price)}]}]})


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


def test_feed_parses_ticks_and_flags_sequence_gaps():
    clock = Clock(5.0)
    feed = TickerFeed(["BIP-20DEC30-CDE"], ws_factory=lambda **kw: None, clock=clock)
    feed.on_message(ticker("BIP-20DEC30-CDE", 60_000.0, 7))
    feed.on_message(json.dumps({"channel": "heartbeats", "sequence_num": 8}))
    assert feed.latest("BIP-20DEC30-CDE").price == 60_000.0 and not feed.take_resync()
    feed.on_message(ticker("BIP-20DEC30-CDE", 60_010.0, 11))  # 9 and 10 missing
    assert feed.gaps == 1 and feed.take_resync() and not feed.take_resync()
    feed.on_message("not json")
    clock.now = 9.0
    assert feed.age() == pytest.approx(4.0)


class FakeWS:
    def __init__(self, feed: TickerFeed, plan: list[int], log: list[str], **kw) -> None:
        self.feed, self.log = feed, log
        self.on_message = kw["on_message"]
        self.beats = plan.pop(0) if plan else 0

    def open(self):
        self.log.append("open")

    def subscribe(self, products, channels):
        self.log.append(f"subscribe {','.join(products)} {','.join(channels)}")

    def sleep_with_exception_check(self, s):
        if self.beats <= 0:
            if not self.feed.connects < 3:
                self.feed.stop()
                return
            raise ConnectionError("dropped")
        self.beats -= 1
        self.on_message(ticker("BIP-20DEC30-CDE", 60_000.0, 0))

    def close(self):
        self.log.append("close")


def test_feed_reconnects_with_backoff_and_requests_a_resync():
    log: list[str] = []
    waits: list[float] = []
    plan = [2, 0, 1]
    feed = TickerFeed(["BIP-20DEC30-CDE", "BTC-USD"], ws_factory=None, sleep=waits.append)  # type: ignore[arg-type]
    feed.ws_factory = lambda **kw: FakeWS(feed, plan, log, **kw)
    feed.run()
    assert feed.connects == 3 and waits == [1.0, 1.0]  # reset to 1 s after each successful connect
    assert log.count("subscribe BIP-20DEC30-CDE,BTC-USD ticker,heartbeats") == 3 and log.count("close") == 3
    assert feed.take_resync()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --group live pytest tests/live/test_watchdog.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'ewbot.live.feed'`.

- [ ] **Step 3: Write minimal implementation**

**File: `src/ewbot/live/watchdog.py`**

```python
"""Always-on safety checks between decision cycles (Plan 3 spec 3.2 `watchdog`, 4.2, 4.5, 5.1).

  StopGapWatch   a bracket stop fills as a limit 5% beyond the stop; when the futures price has passed
                 that limit and the position is still open 10 s later, the runtime closes at market.
  margin_status  overnight-margin projection of the open position: >= 80% warn, >= 90% reduce back to
                 the 70% entry limit (entries above 70% are refused before sending, live/pricing.py).
  AccountGuard   real-account breakers: equity <= the New York day's first equity x 0.97, or <= the peak
                 x 0.85 -> flatten and pause (spec 4.5). State lives in the journal (survives restarts).
  SessionWatch   session state changes and the next scheduled maintenance (noticed 24 h ahead).
  PriceSource    the futures price for the checks: the WebSocket ticker while it is fresh (a message in
                 the last 60 s), otherwise REST polled every 10 s (spec 5.1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from ewbot.config import BreakerParams
from ewbot.live.exchange import Exchange, ExchangeError, ProductSpec, SessionInfo
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


class StopGapWatch:
    def __init__(self, grace_s: float = GAP_GRACE_S) -> None:
        self.grace_s = grace_s
        self.since: float | None = None

    def check(self, direction: int, stop_price: float, price: float, now: float) -> bool:
        """True when the price has been beyond the stop's limit for `grace_s` seconds."""
        if not stop_gapped(price, stop_price, direction):
            self.since = None
            return False
        if self.since is None:
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


class AccountGuard:
    KEY = "account_guard"

    def __init__(self, journal: Journal, params: BreakerParams | None = None) -> None:
        self.journal = journal
        self.params = BreakerParams() if params is None else params  # daily 3%, total 15% (spec 4.5)

    def state(self) -> dict[str, Any] | None:
        return self.journal.get(self.KEY)

    def update(self, equity: float, now: float) -> str | None:
        """The breaker newly tripped by this equity observation (DAILY_LOSS / TOTAL_DD), else None."""
        st = self.state() or {"peak": equity, "day": None, "day_ref": equity, "daily_tripped": False,
                              "total_tripped": False}
        day = ny_date(now)
        if st["day"] != day:
            st.update(day=day, day_ref=equity, daily_tripped=False)
        st["peak"] = max(float(st["peak"]), equity)
        trip = None
        if not st["total_tripped"] and equity <= st["peak"] * (1.0 - self.params.total_dd):
            st["total_tripped"] = st["daily_tripped"] = True  # one flatten covers both
            trip = "TOTAL_DD"
        elif not st["daily_tripped"] and equity <= st["day_ref"] * (1.0 - self.params.daily_loss):
            st["daily_tripped"] = True
            trip = "DAILY_LOSS"
        st["equity"] = equity
        self.journal.put(self.KEY, st)
        return trip

    def reset(self, equity: float, now: float) -> None:
        """After the user resumes: the current equity is the new peak and day reference."""
        self.journal.put(self.KEY, {"peak": equity, "day": ny_date(now), "day_ref": equity, "daily_tripped": False,
                                    "total_tripped": False, "equity": equity})


class SessionWatch:
    def __init__(self) -> None:
        self.state: str | None = None
        self.noticed: set[int] = set()

    def update(self, info: SessionInfo, now: float) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if self.state is not None and info.state != self.state:
            out.append(("SESSION", f"장 운영 상태 {self.state} → {info.state}"))
        self.state = info.state
        start = info.maintenance_start
        if start is not None and 0 < start - now <= MAINTENANCE_NOTICE_S and start not in self.noticed:
            self.noticed.add(start)
            end = info.maintenance_end
            out.append(("MAINTENANCE", f"정기 점검 예정 {datetime.fromtimestamp(start, NY):%Y-%m-%d %H:%M} ~ "
                                       f"{'' if end is None else f'{datetime.fromtimestamp(end, NY):%H:%M}'} (뉴욕)"))
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
        self._rest_ts: float | None = None

    def futures_price(self) -> float | None:
        now = self.clock()
        if self.feed is not None:
            tick = self.feed.latest(self.ex.futures_id)
            if tick is not None and now - tick.ts <= self.stale_s:
                self.mode = "ws"
                return float(tick.price)
        self.mode = "rest"
        if self._rest_ts is None or now - self._rest_ts >= self.poll_s:
            try:
                self._rest = self.ex.quote(self.ex.futures_id).mid
                self._rest_ts = now
            except ExchangeError:
                pass
        return self._rest
```

**File: `src/ewbot/live/feed.py`**

```python
"""WebSocket ticker feed (Plan 3 spec 2 `WebSocket`, 5.1).

The SDK's WSClient (coinbase-advanced-py 1.8.4, dependency group `live`) runs the connection in its own
thread; this class subscribes to the `ticker` and `heartbeats` channels of the futures and spot
products, keeps the last trade price per product and the time of the last message, and watches the
per-connection `sequence_num` for gaps. When the SDK gives up on a connection, `run` reconnects with
exponential backoff (1 s doubling to 60 s). Every (re)connect and every sequence gap sets `resync`, so
the runtime re-reads positions and orders over REST. The runtime reads prices through
watchdog.PriceSource, which uses REST polling once the feed has been silent for 60 s."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

CHANNELS = ("ticker", "heartbeats")
MAX_BACKOFF_S = 60.0


@dataclass(frozen=True, slots=True)
class Tick:
    price: float
    ts: float


class TickerFeed:
    def __init__(self, products: Sequence[str], ws_factory: Callable[..., Any], *,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 max_backoff: float = MAX_BACKOFF_S) -> None:
        self.products = list(products)
        self.ws_factory = ws_factory
        self.clock = clock
        self.sleep = sleep
        self.max_backoff = max_backoff
        self.last_msg: float | None = None
        self.last_seq: int | None = None
        self.gaps = 0
        self.connects = 0
        self._ticks: dict[str, Tick] = {}
        self._resync = False
        self._stop = False
        self._lock = threading.Lock()

    def on_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        now = self.clock()
        with self._lock:
            self.last_msg = now
            seq = msg.get("sequence_num")
            if isinstance(seq, int):
                if self.last_seq is not None and seq != self.last_seq + 1:
                    self.gaps += 1
                    self._resync = True
                self.last_seq = seq
            if msg.get("channel") == "ticker":
                for ev in msg.get("events") or []:
                    for t in ev.get("tickers") or []:
                        if t.get("product_id") and t.get("price"):
                            self._ticks[t["product_id"]] = Tick(float(t["price"]), now)

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
        self._stop = True

    def run(self) -> None:
        backoff = 1.0
        while not self._stop:
            ws = None
            try:
                ws = self.ws_factory(on_message=self.on_message)
                ws.open()
                ws.subscribe(self.products, list(CHANNELS))
                with self._lock:
                    self.connects += 1
                    self.last_seq = None
                    self._resync = True
                backoff = 1.0
                while not self._stop:
                    ws.sleep_with_exception_check(1)
            except Exception:  # noqa: BLE001 - any connection failure: back off and reconnect
                pass
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:  # noqa: BLE001 - closing a dead socket may fail too
                        pass
            if self._stop:
                return
            self.sleep(backoff)
            backoff = min(backoff * 2.0, self.max_backoff)

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.run, name="ewbot-ticker", daemon=True)
        th.start()
        return th


def sdk_ws_factory(key_file: Path) -> Callable[..., Any]:
    """WSClient factory for production (the CDP key signs the subscription JWTs)."""

    def make(on_message: Callable[[str], None]) -> Any:
        from coinbase.websocket import WSClient  # dependency group `live`

        return WSClient(key_file=str(key_file), on_message=on_message, retry=True)

    return make
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run --group live pytest tests/live/test_watchdog.py -q
```

Expected: `7 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/live/watchdog.py src/ewbot/live/feed.py tests/live/test_watchdog.py
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
- Produces (`ewbot.live.notify`): `PREFIX = "[LIVE]"`, `class LiveNotifier(sender, *, clock=time.time, sleep=time.sleep, dedupe_s=600.0)`, callable `(kind, text) -> bool` (never raises; the same `(kind, text)` at most once per 10 minutes; prints to stderr without a sender).
- Produces (`ewbot.live.control`): `COMMANDS = ("/status", "/pause", "/resume", "/flatten", "/flatten_confirm", "/help")`, `OFFSET_KEY = "telegram_offset"`, `class ControlError(RuntimeError)`, `command_word(text) -> str | None`, `class TelegramControl(token, chat_id, journal, *, session=None, timeout=25, sleep=time.sleep)` with `fetch() -> list[str]`, `run(queue)`, `start(queue) -> Thread`, `stop()`, attribute `ignored`.
- Produces (`ewbot.live.health`): `class Heartbeat(url, *, session=None, clock=time.time, interval=60.0, timeout=10.0)` with `beat() -> bool`, `fail(message)`, attributes `url`, `failures`.

- [ ] **Step 1: Write the failing test**

**File: `tests/live/test_control.py`**

```python
from __future__ import annotations

import queue

import pytest
import requests

from ewbot.live.control import ControlError, TelegramControl, command_word
from ewbot.live.health import Heartbeat
from ewbot.live.notify import LiveNotifier
from tests.live.helpers import Clock, journal
from tests.paper.helpers import FakeSender

TOKEN = "123456:SECRET-TOKEN"


class Resp:
    def __init__(self, status: int, body) -> None:
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Session:
    def __init__(self, answers: list) -> None:
        self.answers = answers
        self.calls: list[tuple] = []

    def _next(self):
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params))
        return self._next()

    def post(self, url, data=None, timeout=None, json=None):
        self.calls.append(("POST", url, data))
        return self._next()


def upd(uid: int, chat: int, text: str) -> dict:
    return {"update_id": uid, "message": {"message_id": uid, "chat": {"id": chat}, "text": text}}


def test_command_words():
    assert command_word("/pause") == "/pause" and command_word("/Flatten_Confirm@ewbot_bot") == "/flatten_confirm"
    assert command_word("/status please") == "/status"
    assert command_word("pause") is None and command_word("/rm -rf") is None and command_word("") is None


def test_only_the_allowlisted_chat_is_obeyed_and_the_offset_persists(tmp_path):
    jr = journal(tmp_path)
    s = Session([Resp(200, {"ok": True, "result": [upd(10, 42, "/pause"), upd(11, 999, "/flatten_confirm"),
                                                   upd(12, 42, "hello"), upd(13, 42, "/status@ewbot_bot")]}),
                 Resp(200, {"ok": True, "result": []})])
    c = TelegramControl(TOKEN, "42", jr, session=s)
    assert c.fetch() == ["/pause", "/status"] and c.ignored == 1
    assert jr.get("telegram_offset") == 14
    TelegramControl(TOKEN, "42", jr, session=s).fetch()  # a restart resumes after update 13
    assert s.calls[1][2]["offset"] == 14 and s.calls[0][1].endswith("/getUpdates")
    jr.close()


def test_errors_never_carry_the_token(tmp_path):
    jr = journal(tmp_path)
    s = Session([requests.exceptions.ConnectionError(f"https://api.telegram.org/bot{TOKEN}/getUpdates"),
                 Resp(401, {"ok": False, "description": "Unauthorized"})])
    c = TelegramControl(TOKEN, "42", jr, session=s)
    for _ in range(2):
        with pytest.raises(ControlError) as err:
            c.fetch()
        assert TOKEN not in str(err.value) and err.value.__cause__ is None
    jr.close()


def test_run_queues_commands_and_backs_off_on_errors(tmp_path):
    jr = journal(tmp_path)
    waits: list[float] = []
    c = TelegramControl(TOKEN, "42", jr, sleep=waits.append, session=None)
    answers = [ControlError("x"), ["/status"], ["/pause", "/resume"]]

    def fake_fetch():
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        if not answers:
            c.stop()
        return a

    c.fetch = fake_fetch  # type: ignore[method-assign]
    q: queue.Queue[str] = queue.Queue()
    c.run(q)
    assert [q.get_nowait() for _ in range(3)] == ["/status", "/pause", "/resume"] and waits == [5.0]
    jr.close()


def test_live_notifier_prefixes_dedupes_and_never_raises():
    clock = Clock(1_000.0)
    sender = FakeSender()
    n = LiveNotifier(sender, clock=clock, sleep=lambda s: None)
    assert n("ENTRY", "진입 W3 롱") and sender.texts == ["[LIVE] 진입 W3 롱"]
    assert not n("ENTRY", "진입 W3 롱")  # within 10 minutes
    clock.now += 601
    assert n("ENTRY", "진입 W3 롱") and len(sender.texts) == 2
    failing = LiveNotifier(FakeSender(fail=5), clock=clock, sleep=lambda s: None)
    assert failing("X", "y") is False
    assert LiveNotifier(None, clock=clock)("X", "y") is False


def test_heartbeat_pings_once_a_minute_and_survives_errors():
    clock = Clock(0.0)
    s = Session([Resp(200, {}), requests.exceptions.ConnectionError("down"), Resp(200, {}), Resp(200, {})])
    hb = Heartbeat("https://hc-ping.com/abc", session=s, clock=clock)
    assert hb.beat() and not hb.beat()
    clock.now = 60.0
    assert not hb.beat() and hb.failures == 1
    clock.now = 120.0
    assert hb.beat() and hb.failures == 0
    hb.fail("fatal: boom")
    assert s.calls[-1] == ("POST", "https://hc-ping.com/abc/fail", b"fatal: boom")
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
"""[LIVE] Telegram notifications (Plan 3 spec 3.5): the paper runtime's TelegramSender and delivery
with retries, prefixed "[LIVE]" (the paper runtime's messages start with "[ewbot paper]"). A failed
send never raises into the trading loop; the same (kind, text) is sent at most once per 10 minutes."""

from __future__ import annotations

import sys
import time
from typing import Callable

from ewbot.paper.notify import Sender, deliver, make_event

PREFIX = "[LIVE]"
DEDUPE_S = 600.0
SEND_ATTEMPTS = 2  # keeps a Telegram outage from stalling the loop for more than ~1 s per message


class LiveNotifier:
    def __init__(self, sender: Sender | None, *, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, dedupe_s: float = DEDUPE_S) -> None:
        self.sender = sender
        self.clock = clock
        self.sleep = sleep
        self.dedupe_s = dedupe_s
        self._last: dict[tuple[str, str], float] = {}
        self.sent: list[str] = []

    def __call__(self, kind: str, text: str) -> bool:
        now = self.clock()
        key = (kind, text)
        last = self._last.get(key)
        if last is not None and now - last < self.dedupe_s:
            return False
        self._last[key] = now
        body = f"{PREFIX} {text}"
        if self.sender is None:
            print(body, file=sys.stderr)
            return False
        event = make_event(kind, int(now), len(self.sent), body)
        _, sent, errors = deliver([event], [], self.sender, attempts=SEND_ATTEMPTS, sleep=self.sleep)
        if sent:
            self.sent.append(body)
            return True
        print(f"[LIVE] telegram send failed: {errors}", file=sys.stderr)
        return False
```

**File: `src/ewbot/live/control.py`**

```python
"""Telegram commands (Plan 3 spec 3.2 `control`): /status, /pause, /resume, /flatten (confirmed with
/flatten_confirm within 60 s) and /help, accepted only from the configured chat id.

A background thread long-polls getUpdates and queues the command words; the runtime executes them on
its own thread, so no trading state is shared across threads. The update offset is stored in the
journal: a restart never replays an old command. Exceptions from the HTTP layer may contain the bot
token (it is part of the URL), so only their type name is ever reported."""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

import requests

from ewbot.live.journal import Journal

UPDATES_API = "https://api.telegram.org/bot{token}/getUpdates"
COMMANDS = ("/status", "/pause", "/resume", "/flatten", "/flatten_confirm", "/help")
OFFSET_KEY = "telegram_offset"
POLL_TIMEOUT_S = 25
ERROR_BACKOFF_S = 5.0


class ControlError(RuntimeError):
    """getUpdates failed (the message never contains the token)."""


def command_word(text: str) -> str | None:
    """'/pause@ewbot_bot now' -> '/pause'; anything that is not a known command -> None."""
    if not text:
        return None
    word = text.strip().split()[0].split("@")[0].lower()
    return word if word in COMMANDS else None


class TelegramControl:
    def __init__(self, token: str, chat_id: str, journal: Journal, *, session: Any | None = None,
                 timeout: int = POLL_TIMEOUT_S, sleep: Callable[[float], None] = time.sleep) -> None:
        self._url = UPDATES_API.format(token=token)
        self.chat_id = str(chat_id)
        self.journal = journal
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout
        self.sleep = sleep
        self.ignored = 0
        self._stop = threading.Event()

    def fetch(self) -> list[str]:
        params: dict[str, Any] = {"timeout": self.timeout, "allowed_updates": '["message"]'}
        offset = self.journal.get(OFFSET_KEY)
        if offset is not None:
            params["offset"] = int(offset)
        try:
            resp = self.session.get(self._url, params=params, timeout=self.timeout + 10)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - the URL holds the token: type name only
            raise ControlError(f"getUpdates failed: {type(exc).__name__}") from None
        if not isinstance(data, dict) or not data.get("ok"):
            raise ControlError(f"getUpdates failed: HTTP {getattr(resp, 'status_code', '?')}")
        out: list[str] = []
        for upd in data.get("result") or []:
            self.journal.put(OFFSET_KEY, int(upd["update_id"]) + 1)
            msg = upd.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                self.ignored += 1
                continue
            cmd = command_word(msg.get("text") or "")
            if cmd is not None:
                out.append(cmd)
        return out

    def run(self, commands: queue.Queue[str]) -> None:
        while not self._stop.is_set():
            try:
                for cmd in self.fetch():
                    commands.put(cmd)
            except ControlError:
                self.sleep(ERROR_BACKOFF_S)

    def start(self, commands: queue.Queue[str]) -> threading.Thread:
        th = threading.Thread(target=self.run, args=(commands,), name="ewbot-telegram", daemon=True)
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()
```

**File: `src/ewbot/live/health.py`**

```python
"""Heartbeat to an external monitor (Plan 3 spec 3.2 `health`, 5.1 `VPS 다운`).

Every minute the runtime's main loop GETs EWBOT_HEALTHCHECK_URL (for example a healthchecks.io check
with period 1 min and grace 2 min, so 3 silent minutes alert through its Telegram integration).
The ping is sent from the main loop only, so a hung loop stops the pings as surely as a dead VPS.
A failed ping is reported once to stderr and never raises; `fail()` pings `<url>/fail` on a fatal exit."""

from __future__ import annotations

import sys
import time
from typing import Any, Callable

import requests

INTERVAL_S = 60.0
TIMEOUT_S = 10.0


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

    def beat(self) -> bool:
        if self.url is None:
            return False
        now = self.clock()
        if self.last is not None and now - self.last < self.interval:
            return False
        self.last = now
        try:
            ok = int(self.session.get(self.url, timeout=self.timeout).status_code) < 400
        except Exception as exc:  # noqa: BLE001 - monitoring must never stop trading
            print(f"[LIVE] heartbeat failed: {type(exc).__name__}", file=sys.stderr)
            ok = False
        self.failures = 0 if ok else self.failures + 1
        return ok

    def fail(self, message: str) -> None:
        if self.url is None:
            return
        try:
            self.session.post(self.url.rstrip("/") + "/fail", data=message[:500].encode("utf-8"), timeout=self.timeout)
        except Exception:  # noqa: BLE001 - best effort on the way down
            pass
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run --group live pytest tests/live/test_control.py -q
```

Expected: `6 passed`.

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
- Consumes: `ewbot.paper.decisions` (`known_at`, `same_decision`), `ewbot.live.exchange.KeyPermissions`, `ewbot.live.journal.Journal`, `ewbot.live.pricing` (`FULL_CAP_CONTRACTS`, `FULL_CAP_DAYS`), `ewbot.types.H1`, `requests`.
- Produces (`ewbot.live.audit`): `CONTENTS_API`, `class AuditError(RuntimeError)`, `fetch_paper_ledger(repo, token, session=None, ref="paper") -> dict`, frozen `AuditResult(window: (lo, hi), compared, matched, mismatches: tuple[dict, ...])` with `.ok` (something compared, nothing mismatched), `audit(live_decisions, ledger, baseline_fingerprint=None, since=None) -> AuditResult` (mismatch types `MISSING_IN_LIVE`, `MISSING_IN_PAPER`, `DIFFERENT`, `PAPER_FINGERPRINT`).
- Produces (`ewbot.live.stage`): `STAGES = ("SHADOW", "CANARY", "FULL")`, `SHADOW_MIN_DAYS = 14`, `CYCLE_DEADLINE_S = 120`, `CANARY_MIN_TRADES = 3`, `CANARY_MAX_DAYS = 90`, `PROTECT_MAX_S = 30.0`, `PAUSE_KEY = "paused"`, `MAJOR`, `CANARY_FAIL`, `ORDER_FAILURES`, `MANUAL`; `class StageError(RuntimeError)`, frozen `Gate(name, met, detail)`, `gate_lines(gates) -> list[str]`, `paused(journal) -> dict | None`, `pause(journal, reason, now)`, `resume(journal, now)`, `full_cap(journal, now) -> int | None`, `on_incident(journal, kind, now) -> list[str]` (`"paused"`, `"demoted to CANARY"`), `shadow_gates(journal, now, audit_result)`, `canary_entry_gates(journal, now, audit_result, paper_status, perms)`, `canary_gates(journal, now)`, `full_entry_gates(journal, now, paper_status, perms)` (all `-> list[Gate]`), `transition(journal, to, now, gates, *, confirm, flat)` (raises `StageError`). `paper_status` is the dict of `paper.criteria.golive_status`.

- [ ] **Step 1: Write the failing test**

**File: `tests/live/test_stage.py`**

```python
from __future__ import annotations

import pytest
import requests

from ewbot.live.audit import AuditError, AuditResult, audit, fetch_paper_ledger
from ewbot.live.exchange import KeyPermissions
from ewbot.live.stage import (
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
S0 = 1_791_000_000.0  # SHADOW start
KEY = KeyPermissions(True, True, False)
PAPER_OK = {"c1": True, "c2": True, "c3": False, "count": {"total": 37, "required": 50},
            "c4": {"met": True, "days": 28.5, "coverage": 0.99, "unexplained": 0}}
GOOD_AUDIT = AuditResult((0, 1), 12, 12, ())


def dec(kind: str, ts: int, key: int, price: float = 1.0, **kw) -> dict:
    return {"id": f"{kind}:{ts}:{key}", "kind": kind, "ts": ts, "price": price, "contracts": 1, "reason": kind,
            "trade_key": key, "levels": [], "setup": "W3", "direction": 1, **kw}


def ledger(decisions: list[dict], started: int = 1000, through: int = 100_000, fp: str = "fp") -> dict:
    return {"epochs": [{"id": 1, "fingerprint": fp, "started_ts": started, "through_ts": through}],
            "decisions": decisions}


def test_audit_matches_identical_decisions_and_reports_each_difference():
    paper = [dec("SIGNAL", 3600, 3600), dec("ENTRY", 7200, 7200, 2.0), dec("TRAIL", 10800, 7200, 3.0),
             dec("ENTRY", 500, 500)]  # known before the epoch start: outside the window
    live = [dec("SIGNAL", 3600, 3600), dec("ENTRY", 7200, 7200, 2.0 + 1e-12), dec("TRAIL", 10800, 7200, 3.5),
            dec("STOP", 14400, 7200), dec("SIGNAL", 200_000, 200_000)]  # known after the paper's last bar
    res = audit(live, ledger(paper), baseline_fingerprint="fp")
    assert (res.window, res.compared, res.matched) == ((1000, 100_000), 4, 2)
    assert [(m["id"], m["type"]) for m in res.mismatches] == [("STOP:14400:7200", "MISSING_IN_PAPER"),
                                                             ("TRAIL:10800:7200", "DIFFERENT")]
    assert not res.ok and audit(paper, ledger(paper)).ok
    other = audit(paper, ledger(paper, fp="new"), baseline_fingerprint="fp")
    assert other.mismatches[0]["type"] == "PAPER_FINGERPRINT"
    assert audit([], ledger(paper), since=8000).compared == 1  # only TRAIL is after `since`
    assert audit([], ledger([])).ok is False  # nothing compared is not a pass


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class Sess:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_fetch_paper_ledger_uses_the_contents_api_and_hides_the_token():
    s = Sess(Resp(200, ledger([])))
    assert fetch_paper_ledger("me/repo", "ghp_SECRET", s)["epochs"][0]["id"] == 1
    url, params, headers = s.calls[0]
    assert url == "https://api.github.com/repos/me/repo/contents/ledger.json" and params == {"ref": "paper"}
    assert headers["Accept"] == "application/vnd.github.raw+json" and headers["Authorization"] == "Bearer ghp_SECRET"
    for answer in (Resp(404, {"message": "Not Found"}), requests.exceptions.ConnectionError("ghp_SECRET")):
        with pytest.raises(AuditError) as err:
            fetch_paper_ledger("me/repo", "ghp_SECRET", Sess(answer))
        assert "ghp_SECRET" not in str(err.value)


def shadow_journal(tmp_path, days: float = 15.0):
    jr = journal(tmp_path)
    jr.set_stage("SHADOW", "initial", S0)
    jr.add_event(S0 + 10, "RECOVERY", {"accepted": 0, "abandoned": 0, "unknown": 0, "rebuilt": 0})
    jr.add_event(S0 + 5 * DAY, "RECOVERY", {"accepted": 1, "abandoned": 0, "unknown": 0, "rebuilt": 0})
    for k in range(3):
        bar = int(S0) // H1 * H1 + k * H1
        jr.start_cycle(bar, bar + H1 + 30, "SHADOW")
        jr.finish_cycle(bar, bar + H1 + 75, "OK")
    jr.add_decisions([dec("SIGNAL", int(S0) + 7200, int(S0) + 7200)], now=S0 + 7300)
    jr.add_event(S0 + 7300, "PREVIEW", {"ok": True, "errors": []})
    return jr, S0 + days * DAY


def test_shadow_gates_all_met(tmp_path):
    jr, now = shadow_journal(tmp_path)
    gates = shadow_gates(jr, now, GOOD_AUDIT)
    assert all(g.met for g in gates), gate_lines(gates)
    jr.close()


def test_each_shadow_gate_can_fail(tmp_path):
    jr, now = shadow_journal(tmp_path, days=13.0)
    bar = int(S0) // H1 * H1 + 10 * H1
    jr.start_cycle(bar, bar + H1 + 30, "SHADOW")
    jr.finish_cycle(bar, bar + H1 + 121, "OK")  # 121 s after the close
    jr.add_event(S0 + 9000, "PREVIEW", {"ok": False, "errors": ["PREVIEW_INSUFFICIENT_FUND"]})
    jr.add_event(S0 + 9 * DAY, "RECOVERY", {"accepted": 0, "abandoned": 0, "unknown": 1, "rebuilt": 0})
    bad = AuditResult((0, 1), 3, 2, ({"id": "x", "type": "DIFFERENT"},))
    met = {g.name: g.met for g in shadow_gates(jr, now, bad)}
    assert met == {"기간 14일 이상": False, "신호 1건 이상": True, "① 판단이 모의매매와 100% 일치": False,
                   "② 모든 사이클이 마감 후 2분 안에 끝남": False, "③ 냈을 주문 전부 preview 통과": False,
                   "④ 장애·재시작 시험 통과": False}
    jr.close()


def test_canary_needs_shadow_paper_and_a_trade_key(tmp_path):
    jr, now = shadow_journal(tmp_path)
    assert all(g.met for g in canary_entry_gates(jr, now, GOOD_AUDIT, PAPER_OK, KEY))
    no_c4 = {**PAPER_OK, "c4": {**PAPER_OK["c4"], "met": False}}
    assert not all(g.met for g in canary_entry_gates(jr, now, GOOD_AUDIT, no_c4, KEY))
    assert not all(g.met for g in canary_entry_gates(jr, now, GOOD_AUDIT, PAPER_OK, KeyPermissions(True, True, True)))
    assert not all(g.met for g in canary_entry_gates(jr, now, GOOD_AUDIT, None, KEY))
    jr.close()


def test_transitions_need_confirm_flat_book_and_met_gates(tmp_path):
    jr, now = shadow_journal(tmp_path)
    gates = canary_entry_gates(jr, now, GOOD_AUDIT, PAPER_OK, KEY)
    with pytest.raises(StageError, match="--confirm"):
        transition(jr, "CANARY", now, gates, confirm=False, flat=True)
    with pytest.raises(StageError, match="flat"):
        transition(jr, "CANARY", now, gates, confirm=True, flat=False)
    with pytest.raises(StageError, match="one stage at a time"):
        transition(jr, "FULL", now, gates, confirm=True, flat=True)
    with pytest.raises(StageError, match="gates not met"):
        transition(jr, "CANARY", now, canary_entry_gates(jr, now, None, PAPER_OK, KEY), confirm=True, flat=True)
    transition(jr, "CANARY", now, gates, confirm=True, flat=True)
    assert jr.stage() == ("CANARY", now)
    transition(jr, "SHADOW", now + 1, [], confirm=True, flat=True)  # a demotion needs no gates
    assert jr.stage()[0] == "SHADOW"
    with pytest.raises(StageError):
        transition(jr, "SHADOW", now + 2, [], confirm=True, flat=True)
    jr.close()


def canary_journal(tmp_path, closed: int = 3):
    jr = journal(tmp_path)
    jr.set_stage("CANARY", "promotion", S0)
    for k in range(closed):
        jr.add_event(S0 + k * DAY, "PROTECTED_FIRST", {"trade_id": k + 1, "after_entry_s": 2.5})
        jr.add_event(S0 + k * DAY + 3600, "CLOSED", {"trade_id": k + 1, "reason": "BRACKET"})
    jr.add_event(S0 + 3 * DAY, "COSTS", {"fills": 6})
    return jr


def test_canary_pass_and_full_entry(tmp_path):
    jr = canary_journal(tmp_path)
    now = S0 + 20 * DAY
    assert all(g.met for g in canary_gates(jr, now)), gate_lines(canary_gates(jr, now))
    assert not all(g.met for g in full_entry_gates(jr, now, PAPER_OK, KEY))  # 37/50
    assert all(g.met for g in full_entry_gates(jr, now, {**PAPER_OK, "c3": True}, KEY))
    jr.add_event(now, "UNPROTECTED_60S", {"trade_id": 9})
    jr.add_event(now, "ADOPTED", {"trade_id": 9})
    jr.add_event(now, "CLOSE_FAILED", {"trade_id": 9})
    met = {g.name: g.met for g in canary_gates(jr, now)}
    assert not met["② 손절 이동과 청산이 목표와 일치"] and not met["③ 보호되지 않은 상태 60초 초과 없음"]
    assert not met["④ 수동 개입 없음"]
    jr.close()


def test_canary_needs_three_trades_or_ninety_days(tmp_path):
    jr = canary_journal(tmp_path, closed=1)
    assert not canary_gates(jr, S0 + 30 * DAY)[0].met and canary_gates(jr, S0 + 91 * DAY)[0].met
    jr.close()


def test_pause_resume_incidents_and_the_full_cap(tmp_path):
    jr = journal(tmp_path)
    jr.set_stage("SHADOW", "initial", S0)
    assert on_incident(jr, "UNPROTECTED_60S", S0 + 1) == [] and paused(jr) is None  # SHADOW: recorded only
    jr.set_stage("CANARY", "promotion", S0 + 2)
    assert on_incident(jr, "FOREIGN_ORDERS", S0 + 3) == ["paused"] and paused(jr)["reason"] == "incident FOREIGN_ORDERS"
    resume(jr, S0 + 4)
    assert paused(jr) is None
    jr.set_stage("FULL", "promotion", S0 + 5)
    assert full_cap(jr, S0 + 6) == 3 and full_cap(jr, S0 + 5 + 30 * DAY) is None
    assert on_incident(jr, "FOREIGN_ORDERS", S0 + 7) == []  # not a major incident in FULL
    assert on_incident(jr, "BREAKER", S0 + 8) == ["paused", "demoted to CANARY"] and jr.stage()[0] == "CANARY"
    pause(jr, "user", S0 + 9)
    assert [e["kind"] for e in jr.events(kind="INCIDENT")] == ["INCIDENT"] * 4
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

The window is the paper ledger's current epoch: known_at in (epoch started_ts, epoch through_ts] (the
live runtime records every decision since 2024 on its first cycle, so it has no gap). An epoch whose
fingerprint is not the baseline is reported as a mismatch of its own: the two engines differ.

The ledger is read with the GitHub contents API (`EWBOT_PAPER_REPO`, fine-grained token with
Contents: read on that repository only, `EWBOT_GITHUB_TOKEN`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import requests

from ewbot.paper.decisions import known_at, same_decision

CONTENTS_API = "https://api.github.com/repos/{repo}/contents/ledger.json"


class AuditError(RuntimeError):
    """The paper ledger could not be read (the message never contains the token)."""


def fetch_paper_ledger(repo: str, token: str, session: Any | None = None, ref: str = "paper") -> dict[str, Any]:
    sess = session if session is not None else requests.Session()
    headers = {"Accept": "application/vnd.github.raw+json", "Authorization": f"Bearer {token}",
               "X-GitHub-Api-Version": "2022-11-28"}
    try:
        resp = sess.get(CONTENTS_API.format(repo=repo), params={"ref": ref}, headers=headers, timeout=30)
        status = int(resp.status_code)
        data = resp.json() if status == 200 else None
    except Exception as exc:  # noqa: BLE001 - never echo request details (they carry the token)
        raise AuditError(f"paper ledger fetch failed: {type(exc).__name__}") from None
    if status != 200 or not isinstance(data, dict):
        raise AuditError(f"paper ledger fetch failed: HTTP {status}")
    return data


@dataclass(frozen=True)
class AuditResult:
    window: tuple[int, int]  # (exclusive, inclusive) known_at bounds
    compared: int
    matched: int
    mismatches: tuple[dict[str, Any], ...]

    @property
    def ok(self) -> bool:
        return self.compared > 0 and not self.mismatches


def audit(live: Sequence[dict[str, Any]], ledger: dict[str, Any], baseline_fingerprint: str | None = None,
          since: int | None = None) -> AuditResult:
    epoch = ledger["epochs"][-1]
    lo = int(epoch["started_ts"]) if since is None else max(int(epoch["started_ts"]), int(since))
    hi = int(epoch["through_ts"])

    def inside(d: dict[str, Any]) -> bool:
        return lo < known_at(d) <= hi

    paper = {d["id"]: d for d in ledger.get("decisions", []) if inside(d)}
    mine = {d["id"]: d for d in live if inside(d)}
    mismatches: list[dict[str, Any]] = []
    if baseline_fingerprint is not None and epoch.get("fingerprint") != baseline_fingerprint:
        mismatches.append({"id": f"EPOCH:{epoch['id']}", "type": "PAPER_FINGERPRINT",
                           "paper": epoch.get("fingerprint"), "live": baseline_fingerprint})
    matched = 0
    for did in sorted(paper.keys() | mine.keys()):
        a, b = mine.get(did), paper.get(did)
        if a is None:
            mismatches.append({"id": did, "type": "MISSING_IN_LIVE", "paper": b, "live": None})
        elif b is None:
            mismatches.append({"id": did, "type": "MISSING_IN_PAPER", "paper": None, "live": a})
        elif not same_decision(a, b):
            mismatches.append({"id": did, "type": "DIFFERENT", "paper": b, "live": a})
        else:
            matched += 1
    return AuditResult((lo, hi), len(paper.keys() | mine.keys()), matched, tuple(mismatches))
```

**File: `src/ewbot/live/stage.py`**

```python
"""Operating stages SHADOW -> CANARY -> FULL, their gates, pause and incidents (Plan 3 spec 4.3, 6).

The journal holds the stage history and the pause flag. Promotions need every gate met, a flat
book and an explicit --confirm (the user's approval, spec 6.1); demotions need a flat book and
--confirm. A running runtime keeps its stage until it is flat and then exits so Docker restarts it in
the new stage (live/runtime.py).

Gates (spec 6.2), all computed from the journal:
  SHADOW pass   >= 14 days and >= 1 signal; (1) decisions equal the paper ledger's (audit); (2) every
                cycle finished within 2 min of the bar close; (3) every would-be order passed preview;
                (4) restart test: at least one restart recovered with no unresolved intent.
  CANARY start  SHADOW pass + paper criteria 1, 2 and 4 met + a trade key without transfer permission.
  CANARY pass   >= 3 closed trades or 90 days; (1) protection within 30 s of every fill; (2) stop moves
                and closes executed as targeted (no failed / unconfirmed order); (3) never unprotected
                > 60 s; (4) no manual intervention; (5) costs measured (a daily summary with fills).
  FULL start    CANARY pass + paper criteria 1-4 all met (incl. 50 out-of-sample trades).
Incidents: in CANARY the CANARY failure events pause trading (spec 6.2 `자동으로 일시 정지`); in FULL
the major incidents pause and demote to CANARY."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ewbot.live.audit import AuditResult
from ewbot.live.exchange import KeyPermissions
from ewbot.live.journal import Journal
from ewbot.live.pricing import FULL_CAP_CONTRACTS, FULL_CAP_DAYS
from ewbot.types import H1

STAGES = ("SHADOW", "CANARY", "FULL")
DAY = 86_400
SHADOW_MIN_DAYS = 14
CYCLE_DEADLINE_S = 120
CANARY_MIN_TRADES = 3
CANARY_MAX_DAYS = 90
PROTECT_MAX_S = 30.0
PAUSE_KEY = "paused"
MAJOR = ("UNPROTECTED_60S", "AUDIT_MISMATCH", "BREAKER", "TRANSFER_KEY")  # spec 6.2 중대 사건
CANARY_FAIL = MAJOR + ("PROTECT_SLOW", "FOREIGN_ORDERS", "UNKNOWN_POSITION", "ADOPTED")
ORDER_FAILURES = ("BRACKET_FAILED", "CLOSE_FAILED", "CLOSE_UNCONFIRMED", "REDUCE_FAILED")
MANUAL = ("FOREIGN_ORDERS", "UNKNOWN_POSITION", "ADOPTED")


class StageError(RuntimeError):
    """A stage transition was refused."""


@dataclass(frozen=True)
class Gate:
    name: str
    met: bool
    detail: str


def gate_lines(gates: Sequence[Gate]) -> list[str]:
    return [f"[{'충족' if g.met else '미충족'}] {g.name}: {g.detail}" for g in gates]


def paused(journal: Journal) -> dict[str, Any] | None:
    return journal.get(PAUSE_KEY)


def pause(journal: Journal, reason: str, now: float) -> None:
    journal.put(PAUSE_KEY, {"reason": reason, "ts": now})
    journal.add_event(now, "PAUSED", {"reason": reason})


def resume(journal: Journal, now: float) -> None:
    journal.put(PAUSE_KEY, None)
    journal.add_event(now, "RESUMED", {})


def full_cap(journal: Journal, now: float) -> int | None:
    stage, since = journal.stage()
    if stage == "FULL" and since is not None and now - since < FULL_CAP_DAYS * DAY:
        return FULL_CAP_CONTRACTS
    return None


def on_incident(journal: Journal, kind: str, now: float) -> list[str]:
    """Pause (and in FULL demote to CANARY) for an incident of `kind`; returns what was done."""
    stage, _ = journal.stage()
    done: list[str] = []
    if (stage == "CANARY" and kind in CANARY_FAIL) or (stage == "FULL" and kind in MAJOR):
        if paused(journal) is None:
            pause(journal, f"incident {kind}", now)
            done.append("paused")
        if stage == "FULL":
            journal.set_stage("CANARY", f"major incident {kind}", now)
            done.append("demoted to CANARY")
    journal.add_event(now, "INCIDENT", {"kind": kind, "stage": stage, "done": done})
    return done


def _since(journal: Journal, stage: str) -> float | None:
    hist = journal.stage_history()
    if not hist or hist[-1]["stage"] != stage:
        return None
    return float(hist[-1]["ts"])


def shadow_gates(journal: Journal, now: float, audit_result: AuditResult | None) -> list[Gate]:
    since = _since(journal, "SHADOW")
    if since is None:
        return [Gate("SHADOW 단계", False, "현재 단계가 SHADOW가 아님")]
    days = (now - since) / DAY
    signals = [d for d in journal.decisions() if d["kind"] == "SIGNAL" and d["recorded_ts"] >= since]
    cycles = [c for c in journal.cycles() if c["started_ts"] >= since]
    late = [c for c in cycles if c["status"] != "OK" or c["finished_ts"] is None
            or c["finished_ts"] - (c["bar_ts"] + H1) > CYCLE_DEADLINE_S]
    previews = journal.events(kind="PREVIEW", since=since)
    failed = [p for p in previews if not p["data"].get("ok")]
    recoveries = journal.events(kind="RECOVERY", since=since)
    clean = [r for r in recoveries if not r["data"].get("unknown")]
    audit_ok = audit_result is not None and audit_result.ok
    audit_detail = ("미실행" if audit_result is None else
                    f"비교 {audit_result.compared}건, 불일치 {len(audit_result.mismatches)}건")
    return [
        Gate("기간 14일 이상", days >= SHADOW_MIN_DAYS, f"{days:.1f}/{SHADOW_MIN_DAYS}일"),
        Gate("신호 1건 이상", len(signals) >= 1, f"{len(signals)}건"),
        Gate("① 판단이 모의매매와 100% 일치", audit_ok, audit_detail),
        Gate("② 모든 사이클이 마감 후 2분 안에 끝남", bool(cycles) and not late, f"{len(cycles) - len(late)}/{len(cycles)}"),
        Gate("③ 냈을 주문 전부 preview 통과", bool(previews) and not failed, f"{len(previews) - len(failed)}/{len(previews)}"),
        Gate("④ 장애·재시작 시험 통과", len(recoveries) >= 2 and len(clean) == len(recoveries),
             f"재시작 복구 {len(recoveries)}회 (첫 시작 포함), 미해결 {len(recoveries) - len(clean)}회"),
    ]


def _paper_gates(paper_status: dict[str, Any] | None, full: bool) -> list[Gate]:
    if paper_status is None:
        return [Gate("모의매매 판정", False, "paper ledger를 읽지 못함")]
    c4 = paper_status.get("c4") or {}
    gates = [
        Gate("모의매매 1번 (위험 대비 수익)", bool(paper_status.get("c1")), str(paper_status.get("c1"))),
        Gate("모의매매 2번 (최대 낙폭)", bool(paper_status.get("c2")), str(paper_status.get("c2"))),
        Gate("모의매매 4번 (28일, 원인 불명 0건, 커버리지 95%)", bool(c4.get("met")),
             f"{float(c4.get('days', 0.0)):.1f}일, 커버리지 {float(c4.get('coverage', 0.0)) * 100:.1f}%, "
             f"원인 불명 {c4.get('unexplained')}건"),
    ]
    if full:
        count = paper_status.get("count") or {}
        gates.append(Gate("모의매매 3번 (표본 외 50건)", bool(paper_status.get("c3")),
                          f"{count.get('total')}/{count.get('required')}"))
    return gates


def _key_gate(perms: KeyPermissions | None) -> Gate:
    if perms is None:
        return Gate("거래 키 권한", False, "확인 못 함")
    ok = perms.can_view and perms.can_trade and not perms.can_transfer
    return Gate("거래 키 권한 (view+trade, 송금 불가)", ok,
                f"view={perms.can_view} trade={perms.can_trade} transfer={perms.can_transfer}")


def canary_entry_gates(journal: Journal, now: float, audit_result: AuditResult | None,
                       paper_status: dict[str, Any] | None, perms: KeyPermissions | None) -> list[Gate]:
    return shadow_gates(journal, now, audit_result) + _paper_gates(paper_status, full=False) + [_key_gate(perms)]


def canary_gates(journal: Journal, now: float) -> list[Gate]:
    since = _since(journal, "CANARY")
    if since is None:
        return [Gate("CANARY 단계", False, "현재 단계가 CANARY가 아님")]
    days = (now - since) / DAY
    n_closed = len({e["data"]["trade_id"] for e in journal.events(kind="CLOSED", since=since) if e["data"].get("trade_id")})
    protected = journal.events(kind="PROTECTED_FIRST", since=since)
    slow = [p for p in protected if p["data"]["after_entry_s"] > PROTECT_MAX_S]
    failures = [e for k in ORDER_FAILURES for e in journal.events(kind=k, since=since)]
    unprotected = journal.events(kind="UNPROTECTED_60S", since=since)
    manual = [e for k in MANUAL for e in journal.events(kind=k, since=since)]
    costs = journal.events(kind="COSTS", since=since)
    return [
        Gate("완결 거래 3건 이상 또는 90일", n_closed >= CANARY_MIN_TRADES or days >= CANARY_MAX_DAYS,
             f"{n_closed}건, {days:.1f}일"),
        Gate("① 보호 주문이 체결 후 30초 안에 걸림", bool(protected) and not slow, f"{len(protected) - len(slow)}/{len(protected)}"),
        Gate("② 손절 이동과 청산이 목표와 일치", not failures, f"실패·미확인 주문 {len(failures)}건"),
        Gate("③ 보호되지 않은 상태 60초 초과 없음", not unprotected, f"{len(unprotected)}건"),
        Gate("④ 수동 개입 없음", not manual, f"{len(manual)}건"),
        Gate("⑤ 비용 실측치 비교", bool(costs), f"비용 보고 {len(costs)}회"),
    ]


def full_entry_gates(journal: Journal, now: float, paper_status: dict[str, Any] | None,
                     perms: KeyPermissions | None) -> list[Gate]:
    return canary_gates(journal, now) + _paper_gates(paper_status, full=True) + [_key_gate(perms)]


def transition(journal: Journal, to: str, now: float, gates: Sequence[Gate], *, confirm: bool, flat: bool) -> None:
    """Record a stage change, or raise StageError with the reason."""
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
    unmet = [g for g in gates if not g.met]
    if promotion and (not gates or unmet):
        raise StageError("gates not met: " + "; ".join(f"{g.name} ({g.detail})" for g in unmet or gates))
    journal.set_stage(to, "promotion" if promotion else "demotion", now)
    journal.add_event(now, "STAGE", {"from": cur, "to": to})
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run --group live pytest tests/live/test_stage.py -q
```

Expected: `9 passed`.

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
- Produces (`ewbot.live.settings`): `class SettingsError(RuntimeError)`, frozen `LiveSettings(data_dir=Path("/data"), key_file=None, futures_id=FUTURES_PRODUCT, spot_id=SPOT_PRODUCT, telegram_token=None, telegram_chat=None, healthcheck_url=None, github_token=None, paper_repo=None, edit_stops=False, expiry={})` with `.journal_path` (`data_dir/journal.sqlite`) and `.cache_dir` (`data_dir/cache`); `check_private(path)`, `read_env_file(path) -> dict`, `parse_expiry(text) -> dict[name, ISO date]`, `load_settings(environ=None) -> LiveSettings` (environment: `EWBOT_LIVE_DIR`, `EWBOT_COINBASE_KEY_FILE`, `EWBOT_SECRETS_FILE`, `EWBOT_FUTURES_PRODUCT`, `EWBOT_SPOT_PRODUCT`, `EWBOT_PAPER_REPO`, `EWBOT_LIVE_EDIT_STOPS`, `EWBOT_EXPIRY`; secrets file keys `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `EWBOT_GITHUB_TOKEN`, `EWBOT_HEALTHCHECK_URL`).
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
           "EWBOT_EXPIRY": "coinbase=2027-10-01, github=2027-09-30"}
    s = load_settings(env)
    assert (s.telegram_token, s.telegram_chat, s.github_token) == ("123:abc", "42", "ghp_x")  # the file wins
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
                           EWBOT_HEALTHCHECK_URL (values in the secrets file win over the environment)."""

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
    key_file: Path | None = None
    futures_id: str = FUTURES_PRODUCT
    spot_id: str = SPOT_PRODUCT
    telegram_token: str | None = None
    telegram_chat: str | None = None
    healthcheck_url: str | None = None
    github_token: str | None = None
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
        telegram_chat=get("TELEGRAM_CHAT_ID"),
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

Expected: `7 passed`.

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
- Consumes: every module of Tasks 1–11; `ewbot.data.candles` (`fill_gaps`, `load_1h`), `ewbot.execution.costs.fee` (the paper cost model, for the cost report), `tests.paper.helpers` (`FakeFetch`, `make_lock`, `synth_frame`, `ts`), `tests.live.helpers`.
- Produces (`ewbot.live.shadow`): `class ShadowExchange(SimExchange)(real: Exchange, journal, *, clock=time.time, alert=None)`: real `product/session/quote/balance/key_permissions` (so its `FAULT_METHODS` leaves them out: their faults are injected on `real`); `restore(trade)` puts an open virtual position back after a restart; `place_market`, `place_bracket`, `close_position` journal a `PREVIEW` event (`kind, client_order_id, ok, errors, warnings, ...`) from the real preview, alert `PREVIEW_FAILED` on errors, then fill virtually.
- Produces (`ewbot.live.runtime`): constants `CYCLE_OFFSET_S = 30`, `CYCLE_RETRY_S = 15`, `DATA_DEADLINE_S = 120`, `ENTRY_DEADLINE_S = 300`, `POLL_ACTIVE_S = 10.0`, `POLL_IDLE_S = 60.0`, `AUDIT_MINUTE = 50`, `DAILY_HOUR_NY = 9`, `MARGIN_CHECK_NY = (15, 30)`, `FLATTEN_CONFIRM_S = 60.0`, `EXPIRY_WARN_DAYS = 30`, journal keys `HEARTBEAT_KEY = "heartbeat"`, `AUDIT_KEY = "audit"`; dataclass `LiveDeps(journal, exchange, lock, fetch: Callable[[Path], Path], cache_dir, notify=no-op, clock=time.time, sleep=time.sleep, feed=None, heartbeat=None, commands: Queue | None = None, paper_ledger: Callable[[], dict] | None = None, edit_stops=False, expiry={})` (`lock` is the preflight-checked holdout lock: the runtime builds its configuration with `target.frozen_config(lock)` and passes the lock to `compute_target`; there is no configuration or start field); `class LiveRuntime(deps)` with `startup() -> recovery dict`, `cycle(bar_ts, now) -> "OK"|"LATE"|"ERROR"`, `reconcile(now)`, `poll(now)`, `watch(now)`, `minute(now)`, `run_audit(now) -> AuditResult | None`, `costs(since) -> {fills, slippage_bps, stop_slippage_bps, fees, model_fees}` (journals `BRACKET_FILL` for every leg that left the book), `daily(now)`, `handle(cmd, now) -> reply`, `tick()`, `run_forever(max_ticks=None) -> int`, `flat() -> bool`, attributes `cfg`, `stage`, `target`, `executor`, `prices`, `guard`, `active`, `exit_code`, `done_bar`, `next_try`; module functions `status_lines(journal, now) -> list[str]` (journal only) and `audit_from_journal(journal) -> AuditResult | None`.

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
        self.commands: queue.Queue[str] = queue.Queue()
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

import pytest

from ewbot.live.exchange import ExchangeError, KeyPermissions, RateLimited
from ewbot.live.runtime import audit_from_journal, status_lines
from ewbot.live.shadow import ShadowExchange
from ewbot.live.sim import SimExchange
from ewbot.live.stage import paused
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
    h.commands.put("/pause")
    h.at(ENTRY_BAR + 30)
    assert h.ex.position() is None and paused(h.journal)["reason"] == "user /pause"
    skip = h.journal.events(kind="SKIP")[0]["data"]
    assert skip["signal_ts"] == SIGNAL and "일시 정지" in skip["reason"]
    h.commands.put("/resume")
    h.at(ENTRY_BAR + 40)
    assert paused(h.journal) is None


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
    h.commands.put("/flatten")
    h.commands.put("/flatten_confirm")
    h.at(ENTRY_BAR + 50)
    assert any("/flatten_confirm 실패" in t for k, t in h.alerts.items if k == "COMMAND")
    h.commands.put("/flatten")
    h.commands.put("/flatten_confirm")
    h.at(ENTRY_BAR + 60)
    assert h.ex.position() is None


def test_the_stop_gap_watchdog_closes_after_ten_seconds(h):
    entered(h)
    h.price(25_000.0)  # far below the stop's 5% limit (25_255.75): the bracket cannot fill
    assert h.ex.position() is not None
    for k in range(1, 12):
        h.at(ENTRY_BAR + 40 + k)
    assert h.ex.position() is None and "STOP_GAP" in h.alerts.kinds()
    assert h.journal.events(kind="CLOSED")[-1]["data"]["reason"] == "STOP_GAP"


def test_an_unprotected_position_through_its_stop_is_closed_at_once(h):
    h.ex.fail_next("place_bracket", ExchangeError("INVALID_STOP_PRICE"), times=5)  # the venue refuses the stop
    h.at(ENTRY_BAR + 30)
    assert h.ex.position() is not None and h.ex.open_orders() == []
    h.at(ENTRY_BAR + 40)  # the poll observes the gap in protection
    assert h.rt.executor.trade().unprotected_since == ENTRY_BAR + 40
    h.price(26_580.0)  # through the 26_585 stop, inside its 5% limit: a resting stop would have filled
    h.at(ENTRY_BAR + 51)  # the next REST price (no WebSocket feed here: polled every 10 s)
    assert h.ex.position() is None and "STOP_UNPROTECTED" in h.alerts.kinds()
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
    entered(h)
    h.ex.cash -= 400.0  # equity 10_000 -> ~9_600: the daily -3% breaker
    h.at(ENTRY_BAR + 95)
    assert h.ex.position() is None and paused(h.journal)["reason"] == "breaker DAILY_LOSS"
    assert "BREAKER" in h.alerts.kinds() and h.journal.events(kind="INCIDENT")[-1]["data"]["kind"] == "BREAKER"


def test_margin_warns_then_reduces(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="FULL", equity=400.0)
    hs.price(26_744.5)
    hs.rt.startup()
    hs.ex.place_market("ewb-e-1-L", 1, 3)  # 3 contracts: overnight margin 168.7 USD, 42% of 400
    hs.rt.executor.adopt(position(signal_ts=SIGNAL, stop=26_000.0, tp1=27_500.0, tp2=28_000.0), hs.rt._ctx(hs.clock()))
    hs.ex.cash = 200.0  # equity ~200: 84% -> warn
    hs.at(ENTRY_BAR + 100)
    assert "MARGIN" in hs.alerts.kinds() and hs.ex.position().contracts == 3
    hs.ex.cash = 170.0  # ~99% -> reduce to <= 70%
    hs.rt.guard.reset(170.0, hs.clock())  # the account breaker (-3% a day) is tested on its own
    hs.at(ENTRY_BAR + 170)
    assert hs.ex.position().contracts == 2 and hs.rt.executor.trade().contracts == 2
    assert [e["data"]["reason"] for e in hs.journal.events(kind="REDUCED")] == ["MARGIN"]
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
    led = {"epochs": [{"id": 1, "fingerprint": "e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1",
                       "started_ts": ts("2024-01-10 00:00"), "through_ts": SIGNAL}],
           "decisions": [d for d in hs.journal.decisions() if d["ts"] < SIGNAL]}
    hs.rt.d.paper_ledger = lambda: led
    res = hs.rt.run_audit(ENTRY_BAR + 40)
    assert res.ok and res.compared > 0 and audit_from_journal(hs.journal).ok
    bad = dict(led, decisions=led["decisions"][:-1])
    hs.rt.d.paper_ledger = lambda: bad
    assert not hs.rt.run_audit(ENTRY_BAR + 50).ok
    assert "AUDIT_MISMATCH" in hs.alerts.kinds() and paused(hs.journal)["reason"] == "incident AUDIT_MISMATCH"
    hs.rt.daily(ENTRY_BAR + 60)
    daily = [t for k, t in hs.alerts.items if k == "DAILY"][-1]
    assert "일일 요약" in daily and "포지션: W3" not in daily and "체결 24시간: 1건" in daily
    assert hs.journal.events(kind="COSTS")[-1]["data"]["fills"] == 1
    lines = status_lines(hs.journal, ENTRY_BAR + 70)
    assert lines[0].startswith("단계 CANARY") and lines[1].startswith("포지션: C_END 롱 1계약")
    hs.journal.close()


def test_expiry_reminders_and_a_transfer_key_incident(tmp_path):
    hs = Harness(tmp_path, ENTRY_BAR + 30, permissions=KeyPermissions(True, True, True))
    hs.price(26_700.0)
    hs.rt.startup()
    hs.rt.d.expiry = {"coinbase": "2024-04-01", "github": "2025-01-01"}
    hs.rt.daily(ENTRY_BAR + 60)
    texts = [t for k, t in hs.alerts.items if k == "EXPIRY"]
    assert len(texts) == 1 and "coinbase" in texts[0]
    assert "TRANSFER_KEY" in hs.alerts.kinds() and paused(hs.journal) is not None
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
from ewbot.live.reconcile import LiveTrade
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
        left = trade.contracts - (trade.tp1_n if trade.tp1_filled else 0)
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
every second. Only the WebSocket feed and the Telegram long-poll run in their own threads; they hand
data over (prices, queued commands), and every trading decision is taken on the main loop.

  commands  Telegram /status /pause /resume /flatten /flatten_confirm /help (live/control.py)
  watch     every second while a trade is open: the stop-gap check on the futures price, and an
            immediate close when no stop rests (refused bracket) and the price is through the stop
  poll      every 10 s while a trade is open (60 s otherwise, at once after a WebSocket resync):
            position and open orders are re-read, observed and reconciled against the last target
            (protection right after fills, the TP1 break-even, deferred closes)
  minute    journal heartbeat, external heartbeat, session state, account breakers, margin (80% warn,
            90% reduce), pending intents resolved (or abandoned after 10 min), the 15:30 New York
            overnight-margin report, the daily summary (09:00 New York)
            with expiry reminders and the daily key-permission check, the paper audit (from minute 50),
            and the stage-change restart
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

from ewbot.data.candles import fill_gaps, load_1h
from ewbot.execution.costs import fee as model_fee
from ewbot.live.audit import AuditError, AuditResult, audit
from ewbot.live.exchange import STOP_LIMIT_OFFSET, Exchange, ExchangeError, ProductSpec, SessionInfo
from ewbot.live.executor import Executor
from ewbot.live.journal import Journal
from ewbot.live.preflight import BASELINE_FINGERPRINT
from ewbot.live.pricing import MARGIN_ENTRY_MAX, basis_ratio, map_level, margin_ratio, stage_contracts
from ewbot.live.reconcile import Adopt, Alert, PlanContext, is_ours, observe, plan
from ewbot.live.shadow import ShadowExchange
from ewbot.live.stage import (
    PROTECT_MAX_S,
    full_cap,
    on_incident,
    pause,
    paused,
    resume,
)
from ewbot.live.target import Target, TargetError, compute_target, frozen_config
from ewbot.live.watchdog import NY, AccountGuard, PriceSource, SessionWatch, StopGapWatch, margin_status, ny_date
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
FLATTEN_CONFIRM_S = 60.0
EXPIRY_WARN_DAYS = 30
HEARTBEAT_KEY = "heartbeat"
AUDIT_KEY = "audit"


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
    commands: queue.Queue[str] | None = None
    paper_ledger: Callable[[], dict[str, Any]] | None = None
    edit_stops: bool = False
    expiry: dict[str, str] = field(default_factory=dict)


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
        self.product: ProductSpec | None = None
        self.session: SessionInfo | None = None
        self.done_bar: int | None = None
        self.next_try: float | None = None
        self.last_poll: float | None = None
        self.last_minute: float | None = None
        self.active = False
        self.flatten_until: float | None = None
        self.exit_code: int | None = None

    # ------------------------------------------------------------------ helpers
    def alert(self, kind: str, text: str) -> None:
        self.d.notify(kind, text)

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
            self.alert("INCIDENT", f"[중대 사건] {kind}: {', '.join(done)} (재개는 /resume)")

    def _ctx(self, now: float) -> PlanContext:
        ratio = None
        try:
            ratio = basis_ratio(self.ex.quote(self.ex.futures_id).mid, self.ex.quote(self.ex.spot_id).mid)
        except (ExchangeError, ValueError):
            pass
        session_open = self.session is not None and self.session.is_open
        n, block = self._entry_size(now, ratio, session_open)
        tick = self.product.price_increment if self.product is not None else 5.0
        return PlanContext(now=now, ratio=ratio, tick=tick, session_open=session_open, entry_contracts=n,
                           entry_block=block)

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
        tick = self.product.price_increment
        entry = e.entry_ref * ratio
        stop = map_level(e.stop, ratio, tick, e.direction)
        n, why = stage_contracts(self.stage, equity=equity, entry=entry, stop=stop, direction=e.direction,
                                 risk_mult=e.risk_mult, cfg=self.cfg, full_cap=full_cap(self.journal, now))
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
            self.d.fetch(Path(self.d.cache_dir))
        except Exception as exc:  # noqa: BLE001 - the cycle retries the fetch
            self.journal.add_event(now, "FETCH_ERROR", {"error": f"{type(exc).__name__}: {exc}"[:200]})
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
        return fill_gaps(raw)

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
            self.journal.finish_cycle(bar_ts, self.clock(), "ERROR", f"{type(exc).__name__}: {exc}"[:300])
            self.alert("CYCLE_ERROR", f"[사이클 오류] {_utc(bar_ts)} 봉: {type(exc).__name__}: {str(exc)[:200]}")
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
    def reconcile(self, now: float) -> None:
        pos = self.ex.position()
        orders = self.ex.open_orders()
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
                self.alert(kind, f"[중대 사건] 거래 {data['trade_id']}가 60초 넘게 보호되지 않음")
                self._incident(kind, now)
        ctx = self._ctx(now)
        actions = plan(self.target, seen, pos, orders, ctx)
        self.executor.execute(actions, ctx)
        for a in actions:
            if isinstance(a, Alert) and a.kind in ("FOREIGN_ORDERS", "UNKNOWN_POSITION"):
                self._incident(a.kind, now)
        if any(isinstance(a, Adopt) for a in actions) and self.journal.events(kind="ADOPTED", since=now):
            self._incident("ADOPTED", now)  # a foreign position, not our own entry rebuilt from its intent
        t = self.executor.trade()
        # poll fast while a trade is open or an order's outcome is still unknown (looked up, then resent)
        self.active = (t is not None and t.status == "OPEN") or bool(self.journal.intents(status="PENDING"))
        if (t is not None and t.protected_ts is not None and t.protected_ts - t.opened_ts > PROTECT_MAX_S
                and self._once(f"slow:{t.trade_id}")):
            self._incident("PROTECT_SLOW", now)

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
            # no resting stop (e.g. the venue refused the bracket) and the price is through the stop
            self.alert("STOP_UNPROTECTED", f"[손절] 보호 주문 없이 가격 {price:,.1f}이 손절 {t.stop_price:,.0f}을 지남: 시장가 청산")
            self.journal.add_event(now, "STOP_UNPROTECTED", {"trade_id": t.trade_id, "price": price, "stop": t.stop_price})
            self.executor.close("STOP_UNPROTECTED", self._ctx(now))
            return
        if not self.gap.check(t.direction, t.stop_price, price, now):
            return
        self.alert("STOP_GAP", f"[손절 갭] 가격 {price:,.1f}이 손절 {t.stop_price:,.0f}의 {STOP_LIMIT_OFFSET:.0%} 지정가를 10초 넘게 지남: 시장가 청산")
        self.journal.add_event(now, "STOP_GAP", {"trade_id": t.trade_id, "price": price, "stop": t.stop_price})
        self.executor.close("STOP_GAP", self._ctx(now))
        self.gap.reset()

    # ------------------------------------------------------------------ every minute
    def minute(self, now: float) -> None:
        self.last_minute = now
        self.journal.put(HEARTBEAT_KEY, now)
        if self.d.heartbeat is not None:
            self.d.heartbeat.beat()
        try:
            was_open = self.session is not None and self.session.is_open
            self.session = self.ex.session()
            for kind, text in self.sessions.update(self.session, now):
                self.journal.add_event(now, kind, {"text": text})
                self.alert(kind, f"[장 상태] {text}")
            if self.session.is_open and not was_open:
                self.poll(now)  # deferred closes run right after the reopen (spec 4.5)
        except ExchangeError as exc:
            self.journal.add_event(now, "SESSION_ERROR", {"error": str(exc)[:200]})
        self._money(now)
        if self.journal.intents(status="PENDING"):
            try:
                self.executor.resolve_pending()
            except ExchangeError:
                pass
        local = datetime.fromtimestamp(now, NY)
        today = local.date().isoformat()
        if (local.hour, local.minute) >= MARGIN_CHECK_NY and local.hour < 16 and self._once("margin1530", today):
            self._overnight_report(now)
        if local.hour >= DAILY_HOUR_NY and self._once("daily", today):
            self.daily(now)
        if datetime.fromtimestamp(now, UTC).minute >= AUDIT_MINUTE and self._once("audit", int(now) // H1):
            self.run_audit(now)
        stage, _ = self.journal.stage()
        if stage != self.stage and self.flat():
            self.alert("STAGE", f"[단계 변경] {self.stage} → {stage}: 재시작해 적용")
            self.exit_code = 0

    def _money(self, now: float) -> None:
        try:
            equity = self.ex.balance().equity
        except ExchangeError:
            return
        trip = self.guard.update(equity, now)
        if trip is not None:
            self.alert("BREAKER", f"[실계좌 손실 한도] {trip}: 전량 청산하고 일시 정지 (자산 {equity:,.2f} USD)")
            self.journal.add_event(now, "BREAKER", {"kind": trip, "equity": equity})
            if paused(self.journal) is None:
                pause(self.journal, f"breaker {trip}", now)
            self._incident("BREAKER", now)
            self.executor.flatten(f"BREAKER_{trip}", self._ctx(now))
            return
        t = self.executor.trade()
        pos = self.ex.position() if t is not None and t.status == "OPEN" else None
        if pos is None or self.product is None:
            return
        price = self.prices.futures_price() or pos.avg_entry
        st = margin_status(pos.contracts, price, pos.side, self.product, equity)
        if st.level == "WARN" and self._once("marginwarn", int(now) // H1):
            self.alert("MARGIN", f"[증거금 경고] 오버나이트 기준 {st.ratio:.0%} (80% 이상)")
        elif st.level == "REDUCE" and st.reduce_by > 0:
            self.alert("MARGIN", f"[증거금 축소] 오버나이트 기준 {st.ratio:.0%}: {st.reduce_by}계약 축소")
            if st.reduce_by >= pos.contracts:
                self.executor.close("MARGIN", self._ctx(now))
            else:
                self.executor.reduce(st.reduce_by, "MARGIN", self._ctx(now))

    def _overnight_report(self, now: float) -> None:
        pos = self.ex.position()
        if pos is None or self.product is None:
            return
        try:
            equity = self.ex.balance().equity
        except ExchangeError:
            return
        price = self.prices.futures_price() or pos.avg_entry
        r = margin_ratio(pos.contracts, price, pos.side, self.product, equity)
        self.journal.add_event(now, "MARGIN_1530", {"ratio": r})
        self.alert("MARGIN_1530", f"[15:30 증거금 점검] 16시 오버나이트 기준 예상 {r:.0%} ({pos.contracts}계약)")

    # ------------------------------------------------------------------ audit, summary, reminders
    def run_audit(self, now: float) -> AuditResult | None:
        if self.d.paper_ledger is None:
            return None
        try:
            ledger = self.d.paper_ledger()
        except AuditError as exc:
            self.journal.add_event(now, "AUDIT_ERROR", {"error": str(exc)})
            return None
        res = audit(self.journal.decisions(), ledger, BASELINE_FINGERPRINT)
        self.journal.put(AUDIT_KEY, {"ts": now, "window": list(res.window), "compared": res.compared,
                                     "matched": res.matched, "mismatches": list(res.mismatches[:50])})
        fresh = [m for m in res.mismatches if self._once(f"mismatch:{m['id']}:{m['type']}")]
        if fresh:
            self.journal.add_event(now, "AUDIT_MISMATCH", {"items": fresh[:20]})
            lines = [f"{m['type']} {m['id']}" for m in fresh[:10]]
            self.alert("AUDIT_MISMATCH", "[판단 불일치] 실거래 vs 모의매매\n" + "\n".join(lines))
            self._incident("AUDIT_MISMATCH", now)
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
                price = self.prices.futures_price() or pos.avg_entry
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
                self.alert("TRANSFER_KEY", "[중대 사건] API 키에 송금 권한이 있음: 즉시 키를 교체")
                self._incident("TRANSFER_KEY", now)
        except ExchangeError:
            pass

    # ------------------------------------------------------------------ Telegram commands
    def handle(self, cmd: str, now: float) -> str:
        stamp = datetime.fromtimestamp(now, NY).strftime("%H:%M:%S")
        if cmd == "/status":
            reply = "\n".join(status_lines(self.journal, now))
        elif cmd == "/pause":
            pause(self.journal, "user /pause", now)
            reply = "일시 정지: 신규 진입 중단 (보호 주문과 청산은 계속)"
        elif cmd == "/resume":
            resume(self.journal, now)
            try:
                self.guard.reset(self.ex.balance().equity, now)
            except ExchangeError:
                pass
            reply = "재개: 다음 신호부터 진입"
        elif cmd == "/flatten":
            self.flatten_until = now + FLATTEN_CONFIRM_S
            reply = "60초 안에 /flatten_confirm 을 보내면 전량 청산하고 일시 정지합니다"
        elif cmd == "/flatten_confirm":
            if self.flatten_until is None or now > self.flatten_until:
                reply = "확인할 /flatten 요청이 없음 (먼저 /flatten)"
            else:
                self.flatten_until = None
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
            try:
                self.handle(cmd, now)
            except ExchangeError as exc:
                self.alert("COMMAND", f"{cmd} 실패: {str(exc)[:200]} (다시 보내 주세요)")

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
                self.journal.add_event(now, "LOOP_ERROR", {"section": name, "error": f"{type(exc).__name__}: {exc}"[:300]})
                self.alert("LOOP_ERROR", f"[루프 오류] {name}: {type(exc).__name__}: {str(exc)[:200]}")
                traceback.print_exc(file=sys.stderr)

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
    p = paused(journal)
    lines = [f"단계 {stage}{days} · {'일시 정지: ' + p['reason'] if p else '운영 중'}"]
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
    a = journal.get(AUDIT_KEY)
    if a:
        lines.append(f"모의매매 대조: 비교 {a['compared']}건, 일치 {a['matched']}건, 불일치 {len(a['mismatches'])}건 "
                     f"({_utc(a['ts'])})")
    hb = journal.get(HEARTBEAT_KEY)
    if hb is not None:
        lines.append(f"루프 하트비트: {now - float(hb):.0f}초 전")
    return lines


def audit_from_journal(journal: Journal) -> AuditResult | None:
    a = journal.get(AUDIT_KEY)
    if not a:
        return None
    return AuditResult((int(a["window"][0]), int(a["window"][1])), int(a["compared"]), int(a["matched"]),
                       tuple(a["mismatches"]))
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run --group live pytest tests/live/test_runtime.py -q
```

Expected: `17 passed`.

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
- Produces (`ewbot.live.app`): `real_exchange(settings) -> Exchange` (raises `SettingsError` without a key file), `stage_exchange(real, journal, stage, notify)` (`ShadowExchange` in SHADOW), `notifier(settings) -> LiveNotifier`, `paper_fetcher(settings) -> Callable | None`, `paper_status(settings, now) -> dict | None`, dataclass `Wired(runtime, feed, control, heartbeat, notify)`, `build(settings, lock, real, journal) -> Wired` (hands the preflight-checked lock to `LiveDeps`; starts the feed and the Telegram threads). Tests replace `real_exchange` and the module-level `update_cache`.
- Produces (`ewbot.cli`): sub-command `live` with `run`, `preflight [--offline]`, `status [--json] [--max-age S] [--exchange] [--require-flat]`, `stage {shadow,canary,full} [--confirm]`, `backup --out PATH`; `LIVE_REFUSAL_WAIT_S = 600.0` (a refused start waits before exiting so Docker's restarts stay sparse), test seams `_live_settings() -> LiveSettings` and `_live_lock() -> (Path, sha256)`, `cmd_live(args) -> int`. Exit codes: 0 ok; 1 no journal / heartbeat older than `--max-age`; 2 refused (settings, preflight, stage gate); 3 `--require-flat` found a trade, position or resting order.

- [ ] **Step 1: Write the failing test**

**File: `tests/live/test_cli_live.py`**

```python
from __future__ import annotations

import json

import pytest

from ewbot import cli
from ewbot.cli import build_parser, main
from ewbot.live import app as live_app
from ewbot.live.exchange import KeyPermissions
from ewbot.live.journal import Journal
from ewbot.live.runtime import LiveRuntime
from ewbot.live.settings import LiveSettings, SettingsError
from ewbot.live.sim import SimExchange
from tests.live.helpers import Clock
from tests.paper.helpers import FakeFetch, make_lock, synth_frame, ts

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
    ex.place_market("manual", 1, 1)
    jr = Journal(settings.journal_path)
    jr.set_stage("CANARY", "test", NOW)
    jr.close()
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
journal, [LIVE] notifier, WebSocket feed, Telegram control, heartbeat and paper audit -> LiveRuntime.
The CLI calls these factories; tests replace `real_exchange` and `paper_status` (no network)."""

from __future__ import annotations

import queue
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ewbot.data.coinbase_history import update_cache
from ewbot.live.audit import AuditError, fetch_paper_ledger
from ewbot.live.coinbase import CoinbaseExchange
from ewbot.live.control import TelegramControl
from ewbot.live.exchange import Exchange
from ewbot.live.feed import TickerFeed, sdk_ws_factory
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
    sender = TelegramSender(s.telegram_token, s.telegram_chat) if s.telegram_token and s.telegram_chat else None
    return LiveNotifier(sender)


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


@dataclass
class Wired:
    runtime: LiveRuntime
    feed: TickerFeed | None
    control: TelegramControl | None
    heartbeat: Heartbeat
    notify: LiveNotifier


def build(s: LiveSettings, lock: dict[str, Any], real: Exchange, journal: Journal) -> Wired:
    notify = notifier(s)
    stage, _ = journal.stage()
    ex = stage_exchange(real, journal, stage, notify)
    feed = TickerFeed([s.futures_id, s.spot_id], sdk_ws_factory(s.key_file)) if s.key_file is not None else None
    commands: queue.Queue[str] = queue.Queue()
    control = (TelegramControl(s.telegram_token, s.telegram_chat, journal)
               if s.telegram_token and s.telegram_chat else None)
    heartbeat = Heartbeat(s.healthcheck_url)
    deps = LiveDeps(
        journal=journal, exchange=ex, lock=lock,
        fetch=lambda cache_dir: update_cache(s.spot_id, Path(cache_dir)), cache_dir=s.cache_dir, notify=notify,
        clock=time.time, sleep=time.sleep, feed=feed, heartbeat=heartbeat, commands=commands,
        paper_ledger=paper_fetcher(s), edit_stops=s.edit_stops, expiry=s.expiry,
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
    wired = live_app.build(s, lock, real, journal)
    try:
        return wired.runtime.run_forever()
    except BaseException as exc:
        wired.heartbeat.fail(f"{type(exc).__name__}: {exc}")
        notify("FATAL", f"[중단] {type(exc).__name__}: {str(exc)[:300]}")
        raise


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
    if stage == "SHADOW":
        return shadow_gates(journal, now, live_runtime.audit_from_journal(journal))
    if stage == "CANARY":
        return canary_gates(journal, now)
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
        if (cur, to) == ("SHADOW", "CANARY"):
            gates = canary_entry_gates(journal, now, live_runtime.audit_from_journal(journal),
                                       live_app.paper_status(s, now), perms)
        elif (cur, to) == ("CANARY", "FULL"):
            gates = full_entry_gates(journal, now, live_app.paper_status(s, now), perms)
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

Expected: `33 passed` (7 new, 6 paper CLI, 20 research CLI); the help lists `run, preflight, status, stage, backup`.

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
- Produces: no production code. This task is the spec §7 `일치 테스트` and `장애 주입` coverage over Tasks 1–13: three hour-by-hour walks of the synthetic series in the first 30 days of FULL (3 contracts, basis ratio 1.0005) — a TIME exit, a trailed stop-out and TP1 → break-even → TP2 — must record the paper replay's decisions exactly, enter on the engine's signals, place the mapped engine levels, move the stop with the engine and exit in the engine's exit hour; and a process killed right after an accepted entry, bracket or close, a WebSocket drop and sequence gap, 429s and outages, an exit waiting for the session to reopen, an exit queued before the weekly CFM halt (still the target at the halt bar's cycle) and a stop the spot price crossed during the halt must all end protected, flat or reconciled without a duplicate order — the last two closed by the first poll that sees the session OPEN, not an hour later.

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
from ewbot.live.exchange import RateLimited, Unavailable
from ewbot.live.feed import TickerFeed
from ewbot.live.journal import Journal
from ewbot.live.sim import SimCrash
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

Expected: `20 passed` already — this task only adds integration coverage of code written in Tasks 1–13, so there is no red phase. If anything fails here, the bug is in Tasks 1–13: fix the component (and its unit test), never relax this test.

- [ ] **Step 3: Write minimal implementation**

No production code. (If Step 2 failed, fix the responsible module from Tasks 1–13 and re-run Step 2.)

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run --group live pytest tests/live -q
```

Expected: `241 passed` (all live tests so far, about 30 s; the three parity walks take about 14 s).

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
                   "ewbot-backup.timer", "SHADOW", "CANARY", "FULL", "EWBOT_LIVE_EDIT_STOPS"):
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
- Between cycles: every second the stop-gap watchdog (the bracket's stop fills as a limit 5% beyond the stop, Coinbase's trigger_bracket rule; if the price is past that limit for 10 s, the position is closed at market); every 10 s while a trade is open the position and orders are re-read and protection is repaired; every minute the session state, the account breakers (−3% a New York day, −15% from the peak: flatten and pause), the overnight-margin projection (80% warns, 90% reduces to 70%), the heartbeat to healthchecks.io and the stage-change check; at 15:30 New York an overnight-margin report; at 09:00 New York the daily summary; from minute 50 of every hour the comparison of the live engine decisions with the paper ledger.
- Everything is recorded in the SQLite journal `/var/lib/ewbot/journal.sqlite`: every order intent before it is sent (deterministic `client_order_id`, so a restart never doubles an order), events, the engine decisions, the cycles and the stage history.
- The GitHub Actions paper runtime keeps running as the independent audit. Telegram messages of the live runtime start with `[LIVE]`, the paper runtime's with `[ewbot paper]`.

## Stages (spec 6)

| Stage | What trades | Size | Pass gate (checked by `ewbot live status` / `ewbot live stage`) |
|---|---|---|---|
| SHADOW | nothing: every order is checked with Coinbase's `orders/preview` and executed on a virtual book that follows the real price | 1 contract (CANARY sizing) | ≥ 14 days and ≥ 1 signal; ① decisions 100% equal to the paper ledger; ② every cycle finished ≤ 2 min after the close; ③ every would-be order passed preview; ④ a restart recovered cleanly |
| CANARY | real orders | always 1 contract; a signal whose 1-contract risk exceeds 2% of the account is skipped | ≥ 3 closed trades or 90 days; ① protection ≤ 30 s after every fill; ② no failed or unconfirmed order; ③ never unprotected > 60 s; ④ no manual intervention; ⑤ costs reported |
| FULL | real orders | `risk.sizing.size_contracts` (real equity, conservative 1%), capped at 3 contracts for the first 30 days | — |

- CANARY may start only when SHADOW passed, the paper runtime's criteria 1, 2 and 4 are met, the trade key is in place and you approve (`--confirm`). FULL needs CANARY passed and **all four** paper criteria (incl. 50 out-of-sample trades) and your approval. Promotions go one stage at a time and need a flat book.
- In CANARY an incident (unprotected > 60 s, a decision mismatch with the paper ledger, a breaker, a transfer-permission key, a foreign order or position, slow protection) pauses entries; in FULL the major incidents pause and demote to CANARY. Pause never stops protection or exits; `/resume` is your decision.

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
5. **Health check** (spec 5.1 `VPS 다운`). At <https://healthchecks.io> create a check `ewbot-live` with period **1 minute** and grace **2 minutes** (3 silent minutes alert), add the **Telegram** integration to it, and copy its ping URL.
6. **GitHub token for the audit** (spec 3.5). A fine-grained personal access token at <https://github.com/settings/personal-access-tokens/new>: resource owner `taejunoh`, only the repository `elliott-wave-btc-bot`, permission **Contents: Read-only**, expiry 1 year.
7. **Secrets file** `/etc/ewbot/live.env` (mode 600; the runtime refuses any other mode). Create it empty with the right mode, then edit it (nothing goes to the shell history):

   ```bash
   sudo install -m 600 -o 1000 -g 1000 /dev/null /etc/ewbot/live.env
   sudo -u ewbot nano /etc/ewbot/live.env
   ```

   ```
   TELEGRAM_BOT_TOKEN=<the paper runtime's bot token>
   TELEGRAM_CHAT_ID=<the same chat id>
   EWBOT_GITHUB_TOKEN=<the fine-grained token of step 6>
   EWBOT_HEALTHCHECK_URL=<the ping URL of step 5>
   ```
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

- Telegram commands (only from `TELEGRAM_CHAT_ID`): `/status`, `/pause` (no new entries; protection and exits continue), `/resume`, `/flatten` then `/flatten_confirm` within 60 s (cancel the brackets, close at market, pause), `/help`.
- Status on the VPS: `docker compose -f deploy/live/docker-compose.yml exec live ewbot live status` (stage, position, last cycle, audit, heartbeat and the current stage's gates). `docker compose ... ps` shows the container's health (the Docker health check fails when the loop heartbeat is older than 3 minutes).
- The daily summary (09:00 New York) shows the stage, the position, the cycle, the paper comparison, the fills of the last 24 h (entry slippage vs. the engine's reference, stop slippage vs. the stop price, measured fees vs. the paper cost model; funding is part of the balance change) and the margin.

## Stage transitions

1. **SHADOW** starts with the first deployment (a View key suffices).
2. **SHADOW → CANARY** (expected around 2026-10-30): check `ewbot live status` (all SHADOW gates `충족`) and the paper runtime's status (criteria 1, 2 and 4 met: `uv run ewbot paper status --ledger-dir <a worktree of the paper branch>` on your laptop, docs/paper-trading.md). Restart the container once during SHADOW (`docker compose -f deploy/live/docker-compose.yml restart live`) for gate ④. Then create the View + Trade key (step 4), replace `/etc/ewbot/cdp_api_key.json`, and with the book flat:

   ```bash
   docker compose -f deploy/live/docker-compose.yml exec live ewbot live stage canary --confirm
   ```

   The command prints every gate and refuses while one is unmet or the book is not flat. The running container restarts itself into CANARY within a minute; Telegram shows `[단계 변경]` and a new `[시작] 단계 CANARY`.
3. **CANARY → FULL**: only after all four Plan 1 §9.6 criteria (incl. 50 out-of-sample trades) and the CANARY gates are met, with your approval: `... exec live ewbot live stage full --confirm`.
4. **Demotion** (any time, flat book): `... exec live ewbot live stage shadow --confirm` (or `canary`).

## Confirm during CANARY (spec 6.2)

- Duplicate `client_order_id` behaviour (the runtime handles both documented answers; note which one Coinbase gives).
- Whether brackets survive the Friday 17:00–18:00 New York close.
- Whether `orders/edit` changes a bracket's stop. If it does, set `EWBOT_LIVE_EDIT_STOPS=1` in `deploy/live/.env` and redeploy; until then stop moves place the new bracket first and cancel the old one.
- The actual fee tier (`GET /api/v3/brokerage/transaction_summary`).
- The limit of the first triggered bracket stop: the Create Order reference says 5% beyond the stop (`STOP_LIMIT_OFFSET`, also the stop-gap watchdog's threshold). If Coinbase uses another offset, change the constant and redeploy with a flat book.
- Re-record the private-endpoint fixtures in `tests/live/fixtures/coinbase.json` (positions, balance summary, key permissions, orders, the open-orders query with every resting `order_status`, preview) from real responses with personal data removed, and rerun the tests.

## Emergency procedures

- **Close everything now**: `/flatten`, then `/flatten_confirm`. If Telegram does not answer: `docker compose -f deploy/live/docker-compose.yml exec live ewbot live status` and, if the runtime is down, close the position and cancel the orders in the Coinbase app (a manual intervention: the runtime alerts it and CANARY gate ④ records it).
- **Stop trading but keep the protection**: `/pause`, or `docker compose -f deploy/live/docker-compose.yml stop live` (the brackets stay on Coinbase and keep protecting).
- **Heartbeat alert from healthchecks.io**: SSH in, `docker compose ... ps` and `... logs --tail 200 live`. If the VPS is gone, the brackets still protect the position: check it in the Coinbase app.
- **API key leaked or a transfer-permission alert**: delete the key in the CDP portal at once (open brackets stay), create a new View + Trade key bound to the VPS IP, replace the file and restart.
- **A start refused by the preflight** (fingerprint, lock, key, product): the message names the failing check. Never edit a strategy-fingerprinted file to fix it; redeploy the previous tag.
- **Breaker or incident pause**: read `/status` and the latest journal events, fix the cause, then `/resume`:

  ```bash
  docker compose -f deploy/live/docker-compose.yml exec live python -c "from ewbot.live.journal import Journal; [print(e['ts'], e['kind'], e['data']) for e in Journal('/data/journal.sqlite').events()[-40:]]"
  ```

## Deploying a new version

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

Expected: `11 passed`; `shellcheck-ok` when installed; with Docker, the image builds (about 20 s on a warm cache) and its offline preflight prints `preflight fingerprint: ok - e0435223a3f9 over 25 files (baseline e0435223a3f9, 25 files)` and `preflight lock: ok - sha256 d1be04baa06c, params [2.0, 0.7, 0.5]`; the full suite `872 passed, 1 skipped` without a candle cache (`873 passed` with `data/cache/BTC-USD_1h.parquet`); the fingerprint `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1`; the final `git diff --stat` prints nothing (every fingerprinted file, `backtest/`, `paper/`, the lock and the workflows are byte-identical; use `master` instead of `docs/live-design` when branching from `master`).

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

Expected: clean tree; `873 passed` with the real cache (`872 passed, 1 skipped` without); both fingerprint tripwires pass; the diff prints nothing.

- [ ] **Step 2: Decisions, pull request, merge (controller, user approves)**

Get the user's sign-off on D1–D3 (top of this plan). Open a PR from `feat/plan3-live` to `master`; its `ci` run must be green (the fingerprint and static guards, incl. the one-line allowlist change of D2). Merge right after a paper run that reported 0 new discrepancies, then verify the next paper run in a worktree of `paper`:

```bash
git fetch origin paper && git worktree add /tmp/ewbot-paper-check origin/paper
uv run ewbot paper status --ledger-dir /tmp/ewbot-paper-check
git worktree remove /tmp/ewbot-paper-check
```

Expected: `epoch 1 of 1` with the baseline fingerprint, `discrepancies … (unexplained 0, environment change 0)`; the latest run's recorded environment differs from the previous one only in `uv_lock_sha256` (Decision D1). Tag the merge commit for the first deployment: `git tag live-v1 origin/master && git push origin live-v1`.

- [ ] **Step 3: VPS, keys and secrets (user, with the controller reading along)**

Follow `docs/live-trading.md` → One-time setup, steps 1–9: Ubuntu 24.04 US-East VPS (SSH keys only, ufw), unattended-upgrades, Docker, user `ewbot` (uid 1000), `/var/lib/ewbot` and `/etc/ewbot` (mode 700); a **View-only** CDP key bound to the VPS IP for SHADOW (`/etc/ewbot/cdp_api_key.json`, mode 600); healthchecks.io check (period 1 min, grace 2 min, Telegram integration); the fine-grained GitHub token (Contents: read on this repository); `/etc/ewbot/live.env` (mode 600); the repository checkout with a read-only deploy key; `deploy/live/.env` with `EWBOT_EXPIRY`; about $500 in the futures account. The user types every secret; the controller never sees one.

- [ ] **Step 4: Deploy and start SHADOW**

```bash
cd ~/elliott-wave-btc-bot && bash deploy/live/deploy.sh live-v1
docker compose -f deploy/live/docker-compose.yml exec live ewbot live preflight
docker compose -f deploy/live/docker-compose.yml logs --tail 50 live
docker compose -f deploy/live/docker-compose.yml exec live ewbot live status
```

Expected: the preflight prints four `ok` lines (fingerprint `e0435223a3f9`, lock `d1be04baa06c`, key `view=True trade=False transfer=False`, product `BIP-20DEC30-CDE: 0.01 BTC/contract, $5 tick, whole contracts`); Telegram receives `[LIVE] [시작] 단계 SHADOW`; within the next hour the status shows a `마지막 사이클 … OK … 마감 후 N초` with N ≤ 120; healthchecks.io shows the check up. Then set up the backups (runbook step 11) and run the backup service once.

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

Daily: the `[LIVE] [일일 요약]` (stage, cycle times, the paper comparison `불일치 0건`, previews), `ewbot live status` (the SHADOW gates). Once during SHADOW, restart for gate ④: `docker compose -f deploy/live/docker-compose.yml restart live`, then check a second `RECOVERY` event without unresolved intents in the status. Any `[판단 불일치]`, `[사이클 오류]`, `[SHADOW preview 실패]` or `[루프 오류]` message is investigated before CANARY; a fix means a new tag, a flat-book deploy and, per spec §6.2, SHADOW starts over. The latency of the public candle after the close is not documented: if cycles end `HOLD` (`[판단 보류]`) because the bar arrives later than 2 minutes after the close, report it as a finding against gate ② and spec §4.1 — do not move the deadline in code.

- [ ] **Step 7: CANARY gate (around 2026-10-30; user approves explicitly)**

Preconditions (spec §6.1): every SHADOW gate `충족` in `ewbot live status`; the paper runtime's criteria 1, 2 and 4 met (`ewbot paper status` on a worktree of `paper`); the user's explicit approval in the conversation. Then the user creates a **View + Trade** CDP key (never Transfer) bound to the VPS IP and replaces `/etc/ewbot/cdp_api_key.json` (mode 600), and with the book flat:

```bash
docker compose -f deploy/live/docker-compose.yml restart live
docker compose -f deploy/live/docker-compose.yml exec live ewbot live preflight
docker compose -f deploy/live/docker-compose.yml exec live ewbot live stage canary --confirm
```

Expected: the preflight's key line reads `view=True trade=True transfer=False`; the stage command prints every gate `[충족]` and `stage SHADOW -> CANARY recorded`; within a minute Telegram shows `[단계 변경] SHADOW → CANARY` and a new `[시작] 단계 CANARY`. If any gate is `[미충족]`, the command refuses: do not work around it.

- [ ] **Step 8: CANARY confirmations (controller records the answers in the project notes)**

During CANARY, confirm the spec §6.2 unknowns from the journal and the Coinbase order history: duplicate `client_order_id` behaviour; whether the brackets survive the Friday 17:00–18:00 New York close; whether `orders/edit` moves a bracket stop (then set `EWBOT_LIVE_EDIT_STOPS=1` in `deploy/live/.env` and redeploy with a flat book); the limit of the first triggered bracket stop (the reference says 5% beyond the stop: `STOP_LIMIT_OFFSET`, also the stop-gap threshold); the actual fee tier (`GET /api/v3/brokerage/transaction_summary`); the measured costs in the daily summaries vs. the paper cost model. CANARY passes with ≥ 3 closed trades (or 90 days) and every CANARY gate `충족` (`ewbot live status`). FULL is not part of this month's schedule: it needs all four Plan 1 §9.6 criteria (incl. 50 out-of-sample trades), the CANARY gates and the user's approval (`ewbot live stage full --confirm`).
