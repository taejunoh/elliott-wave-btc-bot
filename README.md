# ewbot: Elliott Wave BTC bot

`ewbot` trades Bitcoin with a mechanical Elliott Wave strategy. It reads waves on 4-hour bars and times entries on 1-hour breakouts. It goes long and short, using four setups (W3, W5, W5 reversal, end of C), and risks 1% of equity per trade at up to 3x leverage.

The project was built in three layers. Each layer has to prove itself before the next one gets real money.

| Layer | What it does | Where it runs | Status |
|---|---|---|---|
| **Research** (Plan 1) | Backtest, walk-forward on 2017–2023, one-shot holdout from 2024-01-01 | Your laptop (`ewbot backtest / compare / walkforward / holdout`) | Done. The holdout was run once and is locked in `holdout.lock`. |
| **Paper trading** (Plan 2) | Every hour, replays the frozen strategy on fresh Coinbase candles and records each decision. Builds the out-of-sample trade count. | GitHub Actions, hourly (Cloudflare cron at :37). Ledger on the orphan `paper` branch. | Running since 2026-10-02 |
| **Live trading** (Plan 3) | Turns the same decisions into Coinbase CFM futures orders (`BIP-20DEC30-CDE`, nano BTC perp, 0.01 BTC per contract) with exchange-side stops | A US-East VPS, in Docker | **SHADOW** since 2026-10-05 (no real orders) |
| **Strategy B** (Plan 4) | A separate 15m strategy on BTC and ETH (1h swing structure, Fibonacci zone with an earlier swing, 15m RSI divergence and breakout). Its own walk-forward, one-shot holdout (`holdout-b.lock`) and paper trading (`paper-b` branch). Never touches A. | Your laptop, then GitHub Actions (`paper-b.yml`) | Research ([`docs/strategy-b.md`](docs/strategy-b.md)) |
| **A-ETH** (Plan 5a) | Strategy A, frozen (same params, code and costs except the 0.1 ETH contract), on ETH-USD: a pre-registered stage 1 on 2017–2023, a one-shot ETH holdout (`holdout-aeth.lock`) and paper trading (`paper-aeth` branch) with its own 50-trade count. Never touches A. | Your laptop, then GitHub Actions (`paper-aeth.yml`) | Stage 1 pending ([`docs/aeth.md`](docs/aeth.md)) |

## Live stages

| Stage | Orders | How it advances |
|---|---|---|
| **SHADOW** | None. Decisions are checked against paper trading, and orders are only previewed. | ≥ 14 days, every gate met, then your explicit `ewbot live stage canary --confirm` |
| **CANARY** | 1 contract | ≥ 3 trades or 90 days, with no incident |
| **FULL** | Sized by the strategy, capped at 3 contracts until 30 clean days | **50 out-of-sample trades** (holdout + paper), plus your explicit approval |

Any serious incident pauses trading or demotes the bot, and the Telegram commands `/status`, `/pause`, `/resume` and `/flatten` always work.

## Main commands

```bash
uv run ewbot paper status                    # paper ledger and the go-live criteria (read-only)
ewbot live status                            # on the VPS: stage, position, gates (read-only)
ewbot live stage shadow|canary|full --confirm  # change stage (promotions need every gate)
ewbot live rebase --confirm                  # after a deliberate deposit or withdrawal
bash deploy/live/deploy.sh <tag>             # deploy a tag on origin/master to the VPS
```

On the VPS, run `ewbot live …` inside the container: `docker compose -f deploy/live/docker-compose.yml exec live ewbot live status`.

Tests:

```bash
uv run --group live pytest
```

## Rules that must not be broken

- **Never edit a strategy file.** That covers `types.py`, `config.py`, `indicators.py`, `data/candles.py`, `waves/`, `strategy/`, `engine/`, `execution/` and `risk/`. Any change alters the strategy fingerprint (`e0435223…`), which resets the 50-trade count for good.
- **Never re-run or tune the holdout,** and never edit `holdout.lock`. The same holds for strategy B's `holdout-b.lock`.
- **Strategy B never touches A:** its code stays in `src/ewbot/stratb/` and `src/ewbot/paperb/`, and any change in `stratb/` restarts B's paper count (B fingerprint), never A's.
- **A-ETH never touches A:** its code stays in `src/ewbot/aeth/`; stage 1 and the ETH holdout run once each, and ETH trades are never pooled into A-BTC's 50. Never run `ewbot fetch --product ETH-USD` (it would download 2024+ ETH bars into A-ETH's cache).
- **The live Coinbase account belongs to the bot alone.** Never place a manual BIP order or hold a manual BIP position in it. Never deposit, withdraw, buy spot or set up recurring buys while the bot runs; use `ewbot live rebase --confirm` after a deliberate capital change.
- **Secrets live only on the VPS** (`/etc/ewbot/`, mode 600) **and in GitHub secrets.** Never put them in the repository.
- **Code changes after SHADOW starts restart its 14-day count.** Documentation changes don't.

## Documents

- Operations:
  - [`docs/live-trading.md`](docs/live-trading.md): live-trading runbook. Setup, deploy, backups and restore, emergencies, stage changes.
  - [`docs/paper-trading.md`](docs/paper-trading.md): paper-trading runtime.
  - [`docs/backtest-runtime.md`](docs/backtest-runtime.md): backtest runtime.
  - [`docs/strategy-b.md`](docs/strategy-b.md): strategy B research, holdout and paper trading.
  - [`docs/aeth.md`](docs/aeth.md): A-ETH stage 1, ETH holdout and paper trading.
- Design specs (`docs/superpowers/specs/`):
  - [Plan 1: research](docs/superpowers/specs/2026-10-01-elliott-wave-btc-bot-design.md)
  - [Plan 2: paper trading](docs/superpowers/specs/2026-10-02-ewbot-plan2-paper-trading-design.md)
  - [Plan 3: live trading](docs/superpowers/specs/2026-10-03-ewbot-plan3-live-trading-design.md)
  - [Plan 4: strategy B](docs/superpowers/specs/2026-10-07-ewbot-plan4-strategy-b-design.md)
  - [Plan 5a: A-ETH](docs/superpowers/specs/2026-10-07-ewbot-plan5a-aeth-design.md)
- Implementation plans: `docs/superpowers/plans/`.

## Layout

```
src/ewbot/            strategy (fingerprinted), backtest, data, cli.py
src/ewbot/paper/      paper-trading runtime (Plan 2)
src/ewbot/live/       live runtime (Plan 3): exchange adapter, reconcile, executor, watchdog, Telegram, stages
src/ewbot/stratb/     strategy B (Plan 4): BTC and ETH 15m data, rules, portfolio engine, walk-forward, holdout (B fingerprint)
src/ewbot/paperb/     strategy B paper trading (Plan 4): runner, ledger adapters, paper-b git sync
src/ewbot/aeth/       A-ETH (Plan 5a): strategy A on ETH - data guard, stage 1, ETH holdout, paper-aeth runtime and sync
deploy/live/          Dockerfile, compose service, deploy.sh, backup.sh and its systemd timer
tests/                research, paper/ and live/ suites
holdout.lock          the one-shot holdout result and its frozen parameters
```
