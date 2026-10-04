# Live-trading runtime (Plan 3) — operations

Spec: `docs/superpowers/specs/2026-10-03-ewbot-plan3-live-trading-design.md` (parents: the Plan 1 and Plan 2 specs).
Plan: `docs/superpowers/plans/2026-10-03-ewbot-plan3-live-runtime.md`.

## What runs

- One Docker container on a US-East VPS runs `ewbot live run` (`deploy/live/docker-compose.yml`, `restart: unless-stopped`). It trades the frozen strategy (params from the committed `holdout.lock`, conservative risk) on the Coinbase CFM future `BIP-20DEC30-CDE` (0.01 BTC per contract, $5 tick). Signals come from the same spot BTC-USD hourly candles as the paper runtime.
- Every hour at hh:00:30 it replays the whole engine (as the paper runtime does) and reads the target after the bar that just closed: the position the engine holds, its stop and TPs, an exit or an entry queued at that close. It then makes the Coinbase account match that target (`src/ewbot/live/reconcile.py`): market entries, two `trigger_bracket_gtc` orders as protection (TP1 size + stop, the rest + TP2 + stop; one bracket for 1 contract), stop moves, TP1 break-even, closes. If the bar has not arrived 2 minutes after the close - or the candles could not be downloaded by then (tried again every 15 s) - the decision is held and the resting brackets keep protecting the position. The replay runs on the runtime's one loop: about 2 s on a laptop, 5–10 s on a 1-vCPU VPS, while the watchdog and the Telegram commands wait (a stop-gap close at hh:00:30 can come that much later than its 10 s grace). A start recovers and protects first (against the hour's decision stored before the restart, when it was taken) and only then downloads the candles.
- Between cycles: every second the stop-gap watchdog (the bracket's stop fills as a limit 5% beyond the stop, Coinbase's trigger_bracket rule; if the price is past that limit for 10 s, the position is closed at market); every 10 s while a trade is open the position and orders are re-read and protection is repaired; every minute the session state, the account breakers (−3% from the New York day's opening, −15% from the peak - each reference the median of three balance reads, each breaker tripping on two breaching reads in a row: flatten and pause - the flatten is recorded first and its close sent at every poll until the account is flat, at the reopen while the market is closed), the overnight-margin projection (80% warns, 90% reduces to 70%), the WebSocket price feed's health, the Telegram command thread's health, the heartbeat to healthchecks.io and the stage-change check; at 15:30 New York an overnight-margin report; at 09:00 New York the daily summary; from minute 50 of every hour the comparison of the live engine decisions with the paper ledger (up to the last bar whose cycle ended OK).
- Coinbase requests that are rate-limited or fail for a passing reason are retried, but one request's waits add up to at most 15 s (plus the requests' own 10 s timeouts) and while an HTTP 429's `Retry-After` runs a new request fails at once without being sent: in a 429 storm only the first request of a pass waits, the next pass tries again. The public market data (quotes) and the private account and order requests are paced and rate-limited apart: a 429 on a quote never holds back a close. A futures/spot quote more than 2% from 1 (a stale or one-sided book) or a crossed book (bid above ask) is no price: no stop move and no entry while it lasts (`[호가 이상]`, the resting brackets stay).
- Everything is recorded in the SQLite journal `/var/lib/ewbot/journal.sqlite`: every order intent before it is sent (deterministic `client_order_id`, so a restart never doubles an order), events, the engine decisions, the cycles and the stage history.
- The GitHub Actions paper runtime keeps running as the independent audit. Telegram messages of the live runtime start with `[LIVE]`, the paper runtime's with `[ewbot paper]`.
- Telegram never holds up trading: alerts are queued and sent by a background thread (every protective action - a stop-gap close, a breaker flatten, a margin reduction, a pause - is taken first, its alert queued after it). A message that fails for a passing reason (network, HTTP 429 or 5xx) is retried (2 s doubling to 60 s) for up to 30 minutes and its full text is written to the container log at once (`docker compose ... logs live | grep '\[LIVE\]'`); a message Telegram refuses outright (any other HTTP 4xx) goes to the log and is dropped at once, so it never holds up the messages behind it; messages still waiting when the runtime stops go to the log too. A message delivered a minute or more late ends with `(N분 지연)`, one longer than Telegram allows is cut with `…(잘림)`. The queue holds 100 messages (the oldest waiting one is dropped, into the log, when it is full).

## Stages (spec 6)

| Stage | What trades | Size | Pass gate (checked by `ewbot live status` / `ewbot live stage`) |
|---|---|---|---|
| SHADOW | nothing: every order is checked with Coinbase's `orders/preview` and executed on a virtual book that follows the real price | 1 contract (CANARY sizing) | ≥ 14 days and ≥ 1 signal first known during SHADOW; ① decisions 100% equal to the paper ledger (a comparison that covers the whole stage, reaches a bar closed at most 3 hours ago and has no mismatch the data do not explain - a bar Coinbase revised is shown, not counted); ② a cycle for every hourly bar of the stage, each finished ≤ 2 min after the close (an hour without a cycle - the runtime was down - fails it, unless the futures session was recorded closed then, for at most 4 hours; a cycle that ran and failed always counts; a restart never rewrites an hour already decided); ③ every would-be order passed preview (a preview Coinbase could not answer - 429, 5xx, a timeout - is no failure: `[SHADOW preview 미확인]`, asked again after every cycle, and a failure only when still unanswered after 24 cycles; only a refusal fails ③); ④ a restart recovered cleanly |
| CANARY | real orders | always 1 contract; a signal whose 1-contract risk exceeds 2% of the account is skipped | ≥ 3 closed trades or 90 days; ① protection ≤ 30 s after every fill; ② no order failure left unresolved (a bracket not replaced within 30 s, a close or a reduction not done by the next poll); ③ never unprotected > 60 s; ④ no manual intervention; ⑤ costs reported; and no other CANARY failure in this CANARY stint (a decision mismatch, a breaker, a transfer key), not paused, a comparison at most 3 hours old without an unexplained mismatch of a decision known in this stint |
| FULL | real orders | `risk.sizing.size_contracts` (real equity, conservative 1%), capped at 3 contracts until FULL has run 30 days without a CANARY failure (each one restarts the 30 days) and with no manual intervention still in force (a foreign order left resting keeps the cap; the 30 days then count from its end); counted on the VPS clock, kept synchronised - a clock stepped forward would lift the cap early | — |

- CANARY may start only when SHADOW passed, the paper runtime's criteria 1, 2 and 4 are met, the trade key is in place and you approve (`--confirm`). FULL needs CANARY passed and **all four** paper criteria (incl. 50 out-of-sample trades) and your approval. Promotions go one stage at a time and need a flat book.
- In CANARY a CANARY failure (unprotected > 60 s, an unexplained decision mismatch with the paper ledger, a breaker, a transfer-permission key, a foreign order or position - contracts added or removed by hand included -, slow protection, an order failure not resolved in time) pauses entries (`[CANARY 실패]` / `[중대 사건]`) and keeps the CANARY pass gate unmet for the rest of this CANARY stint: `/resume` resumes trading, and once the cause is fixed you may restart the stint with a flat book (`ewbot live stage canary --restart --confirm`, a `STAGE` event `restart`): every CANARY gate then counts from the restart - closed trades, days, failures and the comparison's mismatches - and a pause stays until `/resume`. Without a restart the failed stint keeps FULL closed; no SHADOW is needed again. In FULL the major incidents pause and demote to CANARY - the next entry is sized as CANARY at once, even before the runtime restarts - and any other CANARY failure keeps the 3-contract cap for 30 more days. Pause never stops protection or exits; `/resume` is your decision.

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
3. **Docker Engine and the compose plugin** from Docker's apt repository (<https://docs.docker.com/engine/install/ubuntu/>), then a user `ewbot` with uid 1000 that runs everything (the group `systemd-journal` lets it read the container log, which goes to the systemd journal):

   ```bash
   sudo useradd --uid 1000 --create-home --shell /bin/bash ewbot
   sudo usermod -aG docker,systemd-journal ewbot
   sudo install -d -m 700 -o 1000 -g 1000 /var/lib/ewbot /etc/ewbot
   ```

   If uid 1000 is taken (`getent passwd 1000`: some images give it to their default user), use that user instead of `ewbot` everywhere below, and in your copy of `deploy/live/ewbot-backup.service` (step 11) change `User=` and the home directory in `ExecStart=` to that user's. The container always runs as uid 1000.
4. **Coinbase CDP API key** (spec 3.4). In <https://portal.cdp.coinbase.com> → API Keys → Secret API Keys → Create:
   - signature algorithm **ECDSA**;
   - permissions **View** and **Trade** only, never **Transfer** (the runtime refuses to start with a transfer key and treats one found later as a major incident). For SHADOW a **View**-only key is enough; create the View + Trade key when CANARY is approved;
   - **IP allowlist**: the VPS's public IPv4 only;
   - download the key JSON and send it from your laptop straight into its place, owned by uid 1000 with mode 600 (the runtime refuses any other: `chmod 600`), through your admin login on the VPS (`ewbot` has no SSH key and no password):

   ```bash
   ssh <admin>@<vps> 'sudo install -m 600 -o 1000 -g 1000 /dev/stdin /etc/ewbot/cdp_api_key.json' < cdp_api_key.json
   ```

   If sudo asks for a password there (`a terminal is required`), log in (`ssh <admin>@<vps>`), run the same `sudo install …` command, paste the file's content and press Ctrl-D. The key never lands in `/tmp` or a home directory. Delete the downloaded copy from your laptop once the VPS has it. Note the key's creation date for the expiry reminder.
5. **Health check** (spec 5.1 `VPS 다운`). At <https://healthchecks.io> create a check `ewbot-live` with period **1 minute** and grace **2 minutes** (3 silent minutes alert), add the **Telegram** integration to it and keep the **email** integration (your account address) on it too, and copy its ping URL. The email is the channel that works when Telegram does not. Telegram counts as unusable when the command thread has been failing, dead or hung for 10 minutes, a message has failed to go out for 10 minutes, messages have waited 10 minutes without a send attempt (a stuck sender), or Telegram refused 3 messages in a row (e.g. the bot was blocked). The runtime then sends the check a `/fail` with the reason (`[LIVE] 텔레그램 사용 불가 - ...`) and withholds its success pings for at most 30 minutes, so the check stays down; after that the pings resume - so a hung runtime or a dead VPS still trips the check - and the `/fail` is repeated every 3 hours while Telegram stays unusable. A healthchecks.io alert therefore means the VPS or the runtime is down, or Telegram is unusable: only the latter comes with a `/fail` body.
6. **GitHub token for the audit** (spec 3.5). A fine-grained personal access token at <https://github.com/settings/personal-access-tokens/new>: resource owner `taejunoh`, only the repository `elliott-wave-btc-bot`, permission **Contents: Read-only**, expiry 1 year.
7. **Secrets file** `/etc/ewbot/live.env` (owned by uid 1000, mode 600; the runtime refuses another owner, any other mode or anything but a regular file). Create it empty with the right mode, then edit it (nothing goes to the shell history):

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

   One `KEY=VALUE` per line, only these four keys; `export ` before a key and one pair of matching quotes around a value are accepted, comments go on lines of their own. An unknown key, a key twice or a value with a space or a quote (e.g. a trailing `# note`) is refused at start, named by line and key, never by value. These secrets are read only from this file: `TELEGRAM_BOT_TOKEN`, `EWBOT_GITHUB_TOKEN` or `EWBOT_HEALTHCHECK_URL` in the container's environment (an `environment:` entry of the compose file) is refused at start. `deploy/live/.env` (step 8) only fills the compose file's `${…}` settings: a secret written there never reaches the runtime - it is ignored, not refused - and sits in a plain file, so never put one there. `EWBOT_HEALTHCHECK_URL` must be the `https://` ping URL. SHADOW may start without Telegram and the health check (the preflight's `alerts` line warns); CANARY and FULL refuse to start without the bot token, the chat id and the health-check URL.

   Commands are taken only from a **private chat** with the bot: `TELEGRAM_CHAT_ID` must be its id, a positive number (your own Telegram user id); a group or channel id (negative) or an `@name` is refused at start. If the paper runtime reports to a group, use your private chat with the bot here. Only this runtime may read the bot's updates: no webhook on the bot, no second runtime with the same token, and do not run a `getUpdates` request by hand while it runs (Telegram answers the runtime with HTTP 409, alerted as `[텔레그램 명령 수신 이상] … 충돌`). Sending (`sendMessage`, as the paper runtime does) is not affected.
8. **Repository checkout** on the VPS (a read-only deploy key: <https://github.com/taejunoh/elliott-wave-btc-bot/settings/keys>):

   ```bash
   sudo -iu ewbot
   ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519 && cat ~/.ssh/id_ed25519.pub   # add as a read-only deploy key
   git clone git@github.com:taejunoh/elliott-wave-btc-bot.git ~/elliott-wave-btc-bot
   ```

   Non-secret settings go to `~/elliott-wave-btc-bot/deploy/live/.env` (read by compose for the `${…}` settings of `deploy/live/docker-compose.yml`; never committed: `deploy/live/.gitignore`): `EWBOT_EXPIRY`, `EWBOT_LIVE_EDIT_STOPS` (`0` or `1`), `EWBOT_PAPER_REPO` (`owner/repo`) and `EWBOT_CLOSE_CONFLICTS` / `EWBOT_OVERLAP_REFUSALS` (Coinbase refusal codes learned in CANARY, comma-separated, UPPER_SNAKE, never an `UNKNOWN_*` code - that is the stand-in for a refusal Coinbase did not explain: "Confirm during CANARY"). Never a secret (step 7), no `EWBOT_IMAGE` (deploy.sh starts `ewbot-live:current` whatever it says) and no product: the runtime trades `BIP-20DEC30-CDE` on signals from `BTC-USD`, both fixed (`EWBOT_FUTURES_PRODUCT` / `EWBOT_SPOT_PRODUCT` in the container's environment are refused). For example the expiry reminders (30 days ahead, in the daily summary; the contract's own two dates - its 2030-12-20 expiry and 2030-09-21, from which the preflight refuses to start on it, 90 days before - are reminded without a setting):

   ```
   EWBOT_EXPIRY=coinbase=2027-10-03,github=2027-10-03
   ```
9. **Fund the futures account** with enough for one contract's margin plus buffer (about $500). SHADOW's previews run against the real account: an unfunded account fails gate ③. The account is the bot's alone: never hold a `BIP-20DEC30-CDE` position or order of your own in it, and **no deposits, withdrawals, spot trades, conversions or recurring buys in this Coinbase account while the bot runs** - the account loss breakers read the account's whole USD balance (spot and futures cash plus unrealized P&L), so moving 3% of it out trips the daily breaker (flatten, pause, a major incident: in CANARY a failed stint, in FULL a demotion). Use a separate Coinbase account or portfolio for anything personal. To add or withdraw capital on purpose, follow "Adding or withdrawing capital". While the stage is CANARY or FULL the runtime takes a position it does not know for a foreign one - it closes it, or adopts it when the engine holds the same side (in CANARY a failure that pauses entries). A start in SHADOW (never real, or demoted) beside a position with no order of the bot's and nothing in its journal - your own - is refused (`[시작 거부] … 수동 포지션`): it is neither traded nor run beside; so is a journal that was never CANARY or FULL beside orders of the bot's (`[시작 거부] 실계좌에 …`) or beside an account that cannot be read.
10. **First deployment**: tag the merged commit on `master` (for example `live-v1`, `git tag live-v1 origin/master && git push origin live-v1` on your laptop), then on the VPS:

    ```bash
    cd ~/elliott-wave-btc-bot && bash deploy/live/deploy.sh live-v1
    docker compose -f deploy/live/docker-compose.yml logs -f live
    ```

    deploy.sh checks the tag, builds the image, runs its preflight with the service's configuration, starts it and waits for its loop heartbeat - up to 10 minutes this first time: the first start downloads the candle history (a few minutes) -; the runtime records stage SHADOW and sends `[LIVE] [시작]` to Telegram. Check `docker compose -f deploy/live/docker-compose.yml exec live ewbot live preflight` (all five checks `ok`: fingerprint, lock, alerts, key, product).
11. **Backups** (spec 5.2): daily, encrypted with [age](https://github.com/FiloSottile/age), copied with rclone to a Cloudflare R2 bucket and watched by a healthchecks.io check of their own.
    - On your laptop: `age-keygen -o ewbot-backup.key` — keep this private key offline (the VPS never has it: a restore is decrypted on the laptop); note the `age1...` public key it prints.
    - In Cloudflare: an R2 bucket `ewbot-backups` and an R2 API token with Object Read & Write on that bucket only. In the bucket's settings add an object lifecycle rule that deletes objects after 90 days and, where R2 offers it, a bucket lock (retention) rule of 30 days: a token stolen from the VPS can then neither delete nor overwrite the recent backups.
    - At healthchecks.io a second check `ewbot-backup`: period **1 day**, grace **2 hours**, the same Telegram and email integrations; copy its ping URL. Every run pings it at its start and at its end - `/fail` with the exit code when it failed -, so a backup that fails, hangs or never runs is alerted.
    - On the VPS: `sudo apt-get install -y age rclone`, then the rclone remote `r2` (type `s3`, provider `Cloudflare`, the token's access key and secret, the endpoint `https://<account id>.r2.cloudflarestorage.com`) with `no_check_bucket = true`: a token scoped to one bucket may not check for the bucket, and without that line every upload fails with AccessDenied.

    ```bash
    sudo -u ewbot rclone config                                  # n (new remote), name r2, s3, Cloudflare, the keys, the endpoint
    sudo -u ewbot rclone config update r2 no_check_bucket true   # the r2 section now holds: no_check_bucket = true
    sudo -u ewbot rclone lsf r2:ewbot-backups                    # answers (empty at first), no AccessDenied
    ```

    - `/etc/ewbot/backup.env`, owned by root with mode 600 (systemd reads it for the backup service; neither `ewbot` nor the container can), never in the repository:

    ```bash
    sudo install -m 600 -o root -g root /dev/null /etc/ewbot/backup.env
    sudo nano /etc/ewbot/backup.env
    ```

    ```
    EWBOT_BACKUP_AGE_RECIPIENT=age1...
    EWBOT_BACKUP_PING_URL=<the ping URL of the ewbot-backup check>
    ```

    and optionally `EWBOT_BACKUP_REMOTE=r2:ewbot-backups`.
    - Install and start the timer, then run one backup now:

    ```bash
    sudo cp ~ewbot/elliott-wave-btc-bot/deploy/live/ewbot-backup.service ~ewbot/elliott-wave-btc-bot/deploy/live/ewbot-backup.timer /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now ewbot-backup.timer
    sudo systemctl start ewbot-backup.service && journalctl -u ewbot-backup.service -n 20
    ```

    The log ends with `backup uploaded: r2:ewbot-backups/journal-….sqlite.age` and the `ewbot-backup` check turns up. A backup is made inside the running container, or in a one-off container of the same image while the service is stopped; a run that hangs is stopped after 30 minutes (`TimeoutStartSec`) and reported as failed, and every run first removes the plaintext copies a killed run left in `/var/lib/ewbot/backup`. Restoring: see "Restoring the journal" - try it once during SHADOW (the restore drill).

## Daily operation

- Telegram commands (only from your private chat with the bot, `TELEGRAM_CHAT_ID`; messages from anyone else, forwarded messages, edits and commands addressed to another bot are ignored): `/status`, `/pause` (no new entries; protection and exits continue), `/resume`, `/flatten` then `/flatten_confirm` sent within 60 s of it (entries pause and the close is recorded first, then sent at market at every poll until the position is gone - the brackets rest until Coinbase takes it, and while the market is closed it goes out at the reopen; the answer says what happened: `전량 청산 완료`, `장이 열려 있지 않아 청산 대기` or `청산 요청 - …`, still being sent, with `[전량 청산 완료]` when it is done; `/resume` does not cancel it), `/help`. Every command is answered. A command sent more than 2 minutes before the runtime reads it (for example while it restarted) is not acted on: the answer is `오래된 명령 무시: /…` - send it again if you still want it (the ages come from Telegram's message dates and the VPS clock, which Ubuntu keeps synchronised: `timedatectl` shows `System clock synchronized: yes`). The first start ignores whatever was sent to the bot before. Delivery is at most once: a command the runtime fetched right before a crash or a restart is lost without an answer - if no answer comes within a minute, check `/status` and send it again.
- **Telegram and health-check alerts.** `[텔레그램 명령 수신 이상]` means the command thread failed 5 polls in a row (the reason: `HTTP 401` - the bot token was refused; `HTTP 409` - another process or a webhook reads this bot's updates; a network error), was in conflict with another poller (`충돌`, HTTP 409), had stopped and was started again, or stopped answering (`응답 없음`: a hung request; it is not replaced automatically, since the hung thread would keep polling beside a new one - restart the container: `docker compose -f deploy/live/docker-compose.yml restart live`); `[텔레그램 복구]` that commands work again. While it lasts the commands may not arrive: use the VPS (`ewbot live status`, and the emergency procedures). `[외부 감시 핑 실패]` means three healthchecks.io pings in a row failed (`HTTP 404`: a wrong ping URL - fix `EWBOT_HEALTHCHECK_URL`): a dead VPS would then go unreported; `[외부 감시 핑 복구]` that they work again.
- Status on the VPS: `docker compose -f deploy/live/docker-compose.yml exec live ewbot live status` (stage, position, last cycle, audit, heartbeat and the current stage's gates). `docker compose ... ps` shows the container's health (the Docker health check fails when the loop heartbeat is older than 3 minutes). A container that runs but is `unhealthy` has a hung loop, and Docker never restarts an unhealthy container by itself: `docker compose -f deploy/live/docker-compose.yml restart live` at once (a hung loop runs no cycle anyway), then report it with the log.
- The container log goes to the systemd journal: `docker compose -f deploy/live/docker-compose.yml logs live` shows the running container's, `journalctl -t ewbot-live` every container's - also those a deploy, a rollback or a restart replaced (a crashed release's traceback, the messages logged at a stop).
- **Price feed and balance alerts.** The watchdog's prices come from the Coinbase WebSocket ticker, on Coinbase's public market-data endpoint (`wss://advanced-trade-ws.coinbase.com`: no API key, TLS certificate and host name verified); the feed reconnects by itself (a connection silent for 15 s is reopened, backoff 1 s to 60 s) and falls back to REST quotes every 10 s while it is silent. `[가격 피드 끊김]` means no WebSocket futures price for 5 minutes while the market is open (with the reconnect failures and the last error; `server error: ...` means Coinbase refused the subscription), `[가격 피드 복구]` that futures prices are back; nothing to do unless it persists (check the VPS network and `docker compose ... logs live`). With neither a WebSocket nor a REST price for 60 s the stop-gap check waits (the resting brackets still protect) and the margin check is skipped - never reduced on a stand-in price - with one `[증거금 점검: 가격 없음]` alert until a price is back. `[잔고 이상]` means Coinbase returned a balance that is not a finite amount above 0: that read is ignored (no breaker sample, no margin check) until a normal one arrives; three such reads in a row are an incident (`[중대 사건] 잔고 이상이 3회 연속`) that pauses new entries - the position and its brackets stay, nothing is closed. Check the balance in the Coinbase app, then `/resume`. `[15:30 증거금 점검] 조회 실패` means the 15:30 check could not read the position or the balance; it is retried every minute until 16:00.
- **Paper comparison alerts** (from minute 50, against the paper ledger up to the last bar whose cycle ended OK, so a held or failed cycle is never a mismatch). `[판단 불일치 확인 중]` (`AUDIT_PENDING`): a decision one runtime recorded and the other not; it becomes `[판단 불일치]` (`AUDIT_MISMATCH`, a major incident) only if the next hourly comparison still finds it. A decision both recorded differently, or a paper ledger on another strategy fingerprint, is `[판단 불일치]` at once. `[판단 불일치: 봉 수정]` (`AUDIT_REVISION`): the two runtimes decided on different candles for that bar - equal up to the paper's previous run, different at it (Coinbase changed the bar between the live fetch at hh:00:30 and the paper run at :37); no incident, and an explained cause (as in the paper runtime's criterion 4): the comparison gates do not count it - their detail shows it (`봉 수정 N건 (원인 설명됨, 미반영)`). Compare that bar in both candle caches (the live cache on the VPS, the paper runtime's in its workflow) anyway. A revision is only recognised at the bar where the two candle histories part: a later decision it changed counts as unexplained. `[모의매매 대조 실패]`: the paper ledger could not be read or is not a valid paper ledger; the gates are unmet until a comparison succeeds again (`[모의매매 대조 복구]`).
- The daily summary (09:00 New York) shows the stage, the position, the cycle, the paper comparison, the fills of the last 24 h (entry slippage vs. the engine's reference, stop slippage vs. the stop price, measured fees vs. the paper cost model; funding is part of the balance change), the margin and the price feed since the last summary (`순서 끊김`: WebSocket sequence gaps, reconnects and the REST polls they caused - a gap asks for a poll, at most one every 10 s).
- **A stop that empties the TP1 leg first** (FULL only: a 1-contract CANARY trade has one bracket). Both legs share the stop. When the price falls through it, the TP1 leg's stop can fill a poll before the TP2 leg's: the runtime then reads the smaller position as the live TP1 (`TP1_FILLED`, Telegram `[TP1 체결]`) and re-places the rest with a break-even stop that is already through the market, so it fills at once - or, if Coinbase refuses a stop through the market (`BRACKET_FAILED`), the old leg stays: its stop has triggered already and fills, and if the price gapped past its 5% limit the watchdog closes at market (`STOP_GAP`). Either way the rest leaves at about the stop; for that trade `TP1_FILLED` is not a take-profit (its `BRACKET_FILL` events show the stop prices).
- **A close or a stop move that keeps failing.** Protection is never cancelled before the venue took its replacement. A close goes out with the brackets resting; a failed close (`CLOSE_FAILED`, Telegram `[청산 실패 - 재시도 중]`) is retried on every poll with a new id and the brackets stay; while it keeps failing its alert repeats after 1, 2, 4 … up to 30 minutes (`(같은 경보 N건 생략)`), as do a failing stop move's and a failing watchdog close's (tried every 10 s). Only if Coinbase refuses a close because brackets rest (`CLOSE_CONFLICT`: a code in `CLOSE_CONFLICTS`, or any refusal of the watchdog's own close where the brackets protect nothing any more - a gapped stop, or a trade with no leg resting at all; a leg that still covers contracts is never cancelled for it) are they cancelled - after a session check - and the close is sent again once they have left the book (up to 5 s; a cancel Coinbase still queues then holds the close until the next poll - `CLOSE_FAILED` `WAITING`, `[청산 대기 - 재시도 중]` - and for up to 30 s no new legs are placed, so the next attempt is not refused for legs placed again in between); after every failed close the legs are placed again if none rests, so the position is protected while the close is retried. A known limit: if Coinbase disables order entry right between that cancel and the resent close, neither the close nor the legs can be placed until it is enabled again - the position has no resting stop meanwhile (`UNPROTECTED`, a major incident after 60 s; every poll tries the close and the legs again). A flip waits (up to 5 s) until no bracket of the old side rests, else the new entry is skipped (`[진입 건너뜀]`). A stop move keeps the old leg whenever the new one is not confirmed (429, 5xx, timeout) or refused for any reason other than a known overlap refusal (`BRACKET_FAILED`, `[보호 주문 실패]`; e.g. order entry disabled); only a known overlap refusal while our brackets would exceed the position cancels the old leg first (`LEG_GAP`, no alert: not an order failure) and puts it back at once if the new one fails (`LEG_RESTORED`). A bracket Coinbase accepted while the answer was lost is found among the open orders or taken over by its client order id (`LEG_ADOPTED`).
- **An entry whose answer and lookups were lost, or whose fill cannot be read** (`ENTRY_UNCONFIRMED` `[진입 미확인]`, then `ADOPTED_PROVISIONAL`, Telegram `[경고] 진입 ... 결과 미확인`): the runtime polls every 10 s until it is settled, and the position is protected at once with the entry's stop and TPs - also in an hour without a decision (the candles cannot be had after a restart): it is our own entry, so no manual-intervention incident; when the lookup answers it is either our entry (`RECOVERED_ENTRY`) or, if the entry never reached Coinbase, a foreign position (`ADOPTED`: a CANARY incident, entries paused - check the Coinbase order history). A foreign position taken over without quotes is protected at the spot levels (`LEVELS_APPROX`) and re-mapped once quotes are back (`LEVELS_MAPPED`). A provisional trade keeps the position's size: a contract added by hand at the same moment is not reduced as excess (close it yourself). A provisional trade closed by its bracket before it was settled is never classified - no `ADOPTED` incident even if the position was not ours - so after an `ADOPTED_PROVISIONAL` alert check the Coinbase order history.
- **A position read as flat while its brackets still rest whole** (`FLAT_UNCONFIRMED`): no bracket explains it, so one read is not believed - the trade stays open, nothing is cancelled or entered, and the next poll (10 s) decides. After a close only the reads showed (a bracket fill, or two flat reads in a row) the runtime polls every 10 s for 5 minutes, so a position that reappears is protected again at once (taken over as a foreign one: `ADOPTED`, check the order history). A positions reply of another shape than the documented one is an error (`POLL_ERROR`), never "flat".
- **A Coinbase account that cannot be read** (`[계좌 조회 불가]`, `ACCOUNT_UNREADABLE`): a position, session or balance read has failed for 3 minutes while a trade, an order of unknown outcome or a flatten is there (15 minutes otherwise; repeats after 1, 2, 4 … up to 30 minutes); the text says what stops meanwhile - the brackets on Coinbase still protect, but no stop move, close or stop-gap watch can happen. After 30 minutes with something to manage healthchecks.io also gets a `/fail` (`Coinbase 계정 조회 불가`). Check the API key (expired, deleted, its IP allowlist - did the VPS address change?) and Coinbase's status page; `[계좌 조회 복구]` says when it reads again.
- **A reduction refused because the brackets cover it** (`REDUCE_CONFLICT`: TP1 sync, excess, margin): the legs make room first - the TP1 leg is cancelled for a TP1 sync, the legs shrink to the size after an excess or margin reduction (the new ones placed first) - and the reduction is sent again under a new id; if it does not fill the legs go back for the whole position. Nothing to do; `REDUCE_FAILED` after it means the venue refused again.
- **A reduction whose answer was lost** (`REDUCE_FAILED` with status `PENDING`, Telegram `[수량 축소 미확인]`): it was recorded before it was sent, so it is never sent under a new id. It is looked up by its client order id (sent again under the same id only if Coinbase does not have it), and the smaller position is read as that reduction (`REDUCE_SETTLED`), not as a manual one. Nothing to do unless `[경고] … 1분 넘게 포지션에 반영되지 않음` follows.

## Stage transitions

1. **SHADOW** starts with the first deployment (a View key suffices).
2. **SHADOW → CANARY** (expected around 2026-10-30): check `ewbot live status` (all SHADOW gates `충족`) and the paper runtime's status (criteria 1, 2 and 4 met: `uv run ewbot paper status --ledger-dir <a worktree of the paper branch>` on your laptop, docs/paper-trading.md). Restart the container once during SHADOW (`docker compose -f deploy/live/docker-compose.yml restart live`) for gate ④, at any time except hh:00 to hh:03, while the hour's cycle runs: a restart never rewrites an hour already decided (it only takes up that hour's target again) and never decides again an hour that was held or failed, but one across the top of the hour can make the coming cycle late (gate ②). Then create the View + Trade key (step 4), replace `/etc/ewbot/cdp_api_key.json`, check what no gate can see - in the CDP portal the key's **IP allowlist** holds only the VPS's IPv4 (spec §6.1) - and with the book flat:

   ```bash
   docker compose -f deploy/live/docker-compose.yml exec live ewbot live stage canary --confirm
   ```

   The command prints every gate and refuses while one is unmet, while the book is not flat as the running runtime sees it (an open trade, an order of unknown outcome, a standing flatten, a position or a resting order) or from hh:59 to hh:06 UTC (the hour's cycle and its entries: run it again from hh:06). The running container restarts itself into CANARY within a minute; Telegram shows `[단계 변경]` and a new `[시작] 단계 CANARY`. Then delete the View-only key in the CDP portal: no process uses it any more.
3. **CANARY → FULL**: only after all four Plan 1 §9.6 criteria (incl. 50 out-of-sample trades) and the CANARY gates are met, with your approval. Before it, check by hand what no gate can see: re-check the spec §2 Coinbase facts against Coinbase's current documentation (spec §8: the product - 0.01 BTC, $5 tick, whole contracts -, the intraday and overnight margin rates, the order types `market_market_ioc`, `trigger_bracket_gtc`, `close_position` and `orders/preview`, the triggered bracket stop's 5% limit, the rate limits, the fee tier, the session hours and maintenance) and record the result, and that the trade key's IP allowlist still holds only the VPS's IPv4. Then `... exec live ewbot live stage full --confirm`.
4. **Demotion** (any time): `... exec live ewbot live stage shadow --confirm` (or `canary`). It is written first - the running runtime sizes new entries and adoptions for the lower stage from its next order - and then the command reports whether the book is flat: with a position open the runtime keeps protecting and managing it and restarts into the new stage once flat (`book not flat …`). A restart before the book is flat (a crash, a reboot, `docker compose restart`) never hides it behind SHADOW's virtual book: while the journal still holds something real - the open trade, an order of unknown outcome - or Coinbase shows a position or orders of ours, the process starts again on the real account in the last real stage, with that stage's preflight (the trade key, the alerts) and no new entry (`[시작] 단계 CANARY (기록된 단계 SHADOW: …)`); `/flatten` closes the real position, and once flat it restarts into SHADOW.
5. **Restarting a stint**, with the book flat: `... exec live ewbot live stage shadow --restart --confirm` - SHADOW after a fix (spec 6.2 `수정한 뒤 SHADOW를 다시 한다`: a gate that can no longer pass, such as a late cycle, a failed preview or an unresolved restart) - or `... stage canary --restart --confirm` - CANARY after a CANARY failure, once its cause is fixed. Every gate of the stage counts from the restart; `/resume` lifts a pause. The command refuses from hh:59 to hh:06 UTC: the running runtime restarts itself into the new stint within a minute, and a restart across the top of the hour can make that hour's cycle late (gate ②). That restart is the new stint's first start (`재시작 복구 1회 (첫 시작 포함)`): gate ④ still needs one manual restart after it.

## Confirm during CANARY (spec 6.2)

- Duplicate `client_order_id` behaviour (the runtime handles both documented answers; note which one Coinbase gives).
- Whether brackets survive the Friday 17:00–18:00 New York close.
- Whether `orders/edit` changes a bracket's stop. If it does, set `EWBOT_LIVE_EDIT_STOPS=1` in `deploy/live/.env` and redeploy; until then stop moves place the new bracket first and cancel the old one.
- The reason codes Coinbase gives when it refuses a bracket that would exceed the position and a close while brackets rest (`OVERLAP_REFUSALS`, `CLOSE_CONFLICTS` in `src/ewbot/live/executor.py`; journal events `BRACKET_FAILED` / `CLOSE_FAILED` carry the code). If they differ, add the codes to `EWBOT_OVERLAP_REFUSALS` / `EWBOT_CLOSE_CONFLICTS` in `deploy/live/.env` (comma-separated) and restart (`docker compose -f deploy/live/docker-compose.yml up -d`): no code change, no flat book needed; the `[시작]` alert lists the codes added. Until then an unknown refusal keeps the old bracket and the move or a planned close is retried (the watchdog's own close takes any refusal for a conflict).
- That brackets - including a triggered stop resting as a limit - only reduce at fill time: a bracket left after a close cannot open or flip a position (closes are sent with the brackets resting).
- Whether a sized `close_position` (a reduction) is refused while our brackets cover the position, and with which reason code (add it to `CLOSE_CONFLICTS`).
- The actual fee tier (`GET /api/v3/brokerage/transaction_summary`).
- Whether Coinbase accepts or refuses placing and cancelling brackets during the weekly Friday 17:00–18:00 New York halt and the quarterly maintenance, and with which code. Until it is known, a stop move refused while the session is not OPEN is judged by the protection still resting: no CANARY failure while the old bracket covers the position.
- The limit of the first triggered bracket stop: the Create Order reference says 5% beyond the stop (`STOP_LIMIT_OFFSET`, also the stop-gap watchdog's threshold). If Coinbase uses another offset, change the constant and redeploy with a flat book.
- Re-record the private-endpoint fixtures in `tests/live/fixtures/coinbase.json` (positions, balance summary, key permissions, orders, the open-orders query with every resting `order_status`, preview) from real responses with personal data removed, and rerun the tests. Right after the first fill, read the raw `GET /api/v3/brokerage/cfm/positions` reply and compare it with the fixture field by field (`positions`, `product_id`, `number_of_contracts`, `side`, `avg_entry_price`): a reply of another shape is refused, never read as flat.
- Whether the breakers could read the futures balance alone (`cfm/balance_summary`'s futures fields, and whether Coinbase's automatic transfers between the spot and futures balances move money on their own): until then the account's whole USD balance is the breakers' equity ("Adding or withdrawing capital").
- What Coinbase does with a bracket whose stop is already through the market when it is placed (a leg placed again after a gap, a break-even stop after a fast move): refused, with which code, or triggered at once (the simulator triggers it at once).
- How long Coinbase shows a bracket fill in the orders before the position (or the other way round): the planner reads a reduction the legs do not explain as `UNEXPLAINED_REDUCTION` at the first poll that sees it (see "Before FULL").

## Before FULL

CANARY trades one contract: none of these paths runs before FULL. Decide each before the promotion (a code change goes through SHADOW again):

- **TP1_SYNC races the TP1 leg.** When the engine's TP1 has filled but the live one has not, the reduce goes out with the TP1 leg resting; if that leg fills at the same moment, both sell, and the rest reads as an `UNEXPLAINED_REDUCTION` (the stop stays, the rest protected). Cancelling the TP1 leg before the reduce removes the race but leaves those contracts without a stop until the reduce sold them - as already happens when Coinbase refuses the reduce because the brackets cover it (`REDUCE_CONFLICT`): the TP1 leg is then cancelled first - only when that alone makes the room - and the reduce sent again one request later.
- **A reduction seen in the position before the order.** If Coinbase shows a bracket fill in the position before the order (the lag measured in CANARY), the first poll reads an `UNEXPLAINED_REDUCTION` - in FULL a CANARY failure that keeps the 3-contract cap for 30 more days. Consider counting it only after two polls in a row.
- **A resent reduce whose answer was lost** after the TP1 leg was cancelled: the legs are placed back for the whole position, so after it did fill they over-cover (reduce-only) until the next poll resizes them.
- **The cost report misses reductions settled from the position alone** (`REDUCE_SETTLED` without a `REDUCED`: its answer was lost, so no fill price is known).
- **The simulator is optimistic**: its stop fills have no half-spread (about $2.5 per contract less slippage than a real stop fill) and it has no margin model, so the parity and chaos tests never see a margin refusal.

## Emergency procedures

- **Close everything now**: `/flatten`, then `/flatten_confirm` (an answer `청산 요청 - …` means the runtime keeps sending the close at every poll: `[전량 청산 완료]` follows once the account is flat). If Telegram does not answer: `docker compose -f deploy/live/docker-compose.yml exec live ewbot live status` and, if the runtime is down, close the position and cancel the orders in the Coinbase app (a manual intervention: the runtime alerts it and CANARY gate ④ records it).
- **Stop trading but keep the protection**: `/pause`, or `docker compose -f deploy/live/docker-compose.yml stop live` (the brackets stay on Coinbase and keep protecting). A stop is graceful: the runtime finishes its current one-second tick, sends `[종료]` and exits 0 - no `[중단]`, no health-check `/fail` (healthchecks.io reports the silence after 3 minutes). While the open trade has no resting stop (a bracket just refused) it first goes on for up to 90 s of polls that place it (`STOP_DEFERRED`); `[종료] … 경고` says when it still had none. A stop while the start still retries its Coinbase reads ends those at once. Compose runs it under an init (`init: true`), so the signal reaches it, and gives it `stop_grace_period` 150 s before killing it.
- **`[중단]`** (the runtime stopped on an unexpected error; Docker starts it again): the container log has the traceback (secrets redacted), healthchecks.io a `/fail` with the error's type only. The same error type is alerted once an hour, across restarts, and an error that repeats within 15 minutes of the previous one's exit waits before exiting - 1, 2, 4 … up to 30 minutes - so a crash loop stays sparse; while the journal shows a real open trade, an order of unknown outcome or a standing flatten the wait stays at 2 minutes (only the brackets protect the position while it is down). Read the traceback, then fix it (a new tag) and deploy it - with a position open the plain deploy refuses (exit 3) and a crash-looping runtime cannot act on `/flatten`, so use `bash deploy/live/deploy.sh --recover <tag>` (the fixed tag, or the last one that ran well; "Deploying a new version") - or `docker compose -f deploy/live/docker-compose.yml stop live`; the brackets protect an open position meanwhile.
- **An order failure not resolved in time** (`[주문 실패 미해결]`, `ORDER_UNRESOLVED`; in CANARY `[CANARY 실패] ORDER_UNRESOLVED: paused`): a bracket that could not be replaced within 30 s, or a close or a reduction that did not go through by the next poll. Read the order events (`BRACKET_FAILED`, `CLOSE_FAILED`, `CLOSE_UNCONFIRMED`, `REDUCE_FAILED`, `ACTION_ERROR`) and check the position's brackets in the Coinbase app before `/resume`. A failure the runtime resolved in time (`ORDER_RESOLVED`, e.g. one 429 on a stop move replaced at the next poll, or - in an hour without a decision - a bracket seen resting over the whole position at the next poll) is no CANARY failure. An unresolved failure, like a manual intervention, is recorded once per episode, not at every poll: the episode ends at the first poll where the trade has no failure left inside its time (one whose fix awaits its confirmation does not count; a fix still not confirmed 2 polls and 10 minutes later is itself unresolved) - a later failure is a new incident, pauses again and in FULL restarts the cap's 30 days -, and with a restart or a stage change; while no trade is open every failure is its own episode.
- **Heartbeat alert from healthchecks.io**: SSH in, `docker compose ... ps` and `... logs --tail 200 live`. If the VPS is gone, the brackets still protect the position: check it in the Coinbase app and follow "Recovering from VPS loss". If the alert came with a `/fail` body `텔레그램 사용 불가 - ...`, the runtime was running when it sent it but cannot be reached on Telegram - check that it still runs (`docker compose ... ps`; its pings resume within 30 minutes of the `/fail`, so a check that stays down longer means it stopped) - and then check `... logs live | grep 'telegram'` (`HTTP 401`: replace the bot token; `HTTP 409`: stop the other poller or delete the webhook; network errors: Telegram or the VPS network). Meanwhile: `ewbot live status` on the VPS, `docker compose -f deploy/live/docker-compose.yml stop live` to stop trading (the brackets stay on Coinbase) and the Coinbase app to close a position.
- **API key leaked or a transfer-permission alert**: delete the key in the CDP portal at once (open brackets stay), create a new View + Trade key bound to the VPS IP, replace the file and restart.
- **A start refused by the preflight** (fingerprint, lock, alerts, key, product): the message names the failing check; the same reason's `[시작 거부]` is sent once an hour (a new reason at once), the start tried again every 10 minutes. A start that cannot read Coinbase - the preflight's key or product read failing with a 429, a 5xx or a timeout, or the product or the session unreadable for 5 minutes after the preflight passed (`[시작 거부] … 조회 실패`) - is an outage: it starts again after a minute, its alert sent once an hour (`… 60초마다 다시 시작`). Any other refusal waits 10 minutes before Docker starts it again. Never edit a strategy-fingerprinted file to fix it; redeploy the previous tag. A Coinbase key file the SDK cannot read is refused like a missing one. A start that finds orders of ours on Coinbase while the journal was never CANARY or FULL (a lost or replaced journal) is refused too (`[시작 거부] 실계좌에 …`): restore the journal from the backup, or close them in the Coinbase app; so is a SHADOW start beside a position with no order of ours and nothing in the journal (`… 수동 포지션`: close it in the Coinbase app). A start refused by the settings (`refusing: ...` in the log: a secrets-file line, a secret in the environment, a setting out of range - named by variable, file and line, never by value) waits 10 minutes before Docker starts it again: fix the file or `deploy/live/.env` and `docker compose -f deploy/live/docker-compose.yml restart live`.
- **A candle cache that cannot be read** (`[캔들 캐시 손상]`, `CACHE_SET_ASIDE`: a kill in the middle of the hourly cache write): the runtime moves it aside to `/var/lib/ewbot/cache/BTC-USD_1h.parquet.bad-<time>` and downloads the history again (a few minutes: that hour's decision is held). If the downloads keep failing (`[판단 보류]` every hour), stop the container, delete `/var/lib/ewbot/cache/BTC-USD_1h.parquet` and start it again; remove the `.bad-` files once the cycles run.
- **Breaker or incident pause**: read `/status` and the latest journal events, fix the cause, then `/resume`. `/resume` re-bases only the breaker that tripped (the day's reference after −3%, the peak after −15% - which also marks the day), as the median of the `/resume` balance read and the next two; a `/pause` → `/resume` without a trip changes neither. If the balance cannot be read at that moment, the reply says so and a tripped breaker still blocks entries: send `/resume` again. The latest journal events:

  ```bash
  docker compose -f deploy/live/docker-compose.yml exec live python -c "from ewbot.live.journal import Journal; [print(e['ts'], e['kind'], e['data']) for e in Journal('/data/journal.sqlite').events()[-40:]]"
  ```

## Adding or withdrawing capital

The breakers measure the account's whole USD balance: a deposit, a withdrawal or a spot purchase moves it like a profit or a loss. Do it only on purpose, this way:

1. With a flat book (no trade open: `/status`), stop the runtime: `docker compose -f deploy/live/docker-compose.yml stop live` (or act within a minute of the transfer showing in the balance: two breaching reads, a minute apart, trip a breaker).
2. Move the money in the Coinbase app.
3. Ask for the new references (the command prints the current ones and the equity now, and refuses unless the book is flat as the runtime sees it - an open trade, an order of unknown outcome, a standing flatten, a position or a resting order: an open trade's unrealized P&L would be folded into both references):

   ```bash
   docker compose -f deploy/live/docker-compose.yml run --rm --no-deps -T live rebase --confirm
   ```

4. `docker compose -f deploy/live/docker-compose.yml start live`. At its first minute the runtime re-bases the day's reference and the peak from its balance read and the next two (their median), journaled `REBASED` and alerted `[손실 한도 기준 재설정] 일 기준 … → …, 최고점 … → …`. A breaker that had tripped stays tripped until `/resume`. A rebase clears the breakers' breach flags (a loss already under way is absorbed), so it stays visible: the daily summary lists the last 24 hours' (`손실 한도 기준 재설정 24시간`) and the CANARY gate `중대 사건 없음` counts them in its detail.

## Restoring the journal

The journal holds the open trade, the order intents, the stage history and the gates' records; Coinbase holds the position and its brackets. A restore puts back the newest backup (up to a day old) and the start's recovery reconciles it with Coinbase.

1. On your laptop (the age key never leaves it): download the newest `journal-*.sqlite.age` from the R2 bucket and decrypt it:

   ```bash
   age -d -i ewbot-backup.key -o journal.sqlite journal-<stamp>.sqlite.age
   ```

2. Send it to the VPS through your admin login, owned by uid 1000 with mode 600 (as the key in setup step 4), then delete `journal.sqlite` on the laptop:

   ```bash
   ssh <admin>@<vps> 'sudo install -m 600 -o 1000 -g 1000 /dev/stdin /var/lib/ewbot/journal.restore' < journal.sqlite
   ```

3. On the VPS, as `ewbot`, exactly in this order. The runtime keeps the journal in SQLite's WAL mode: a `journal.sqlite-wal` or `journal.sqlite-shm` left from the old journal (after a kill, an out-of-memory or a power loss) would be replayed into the restored file and corrupt it silently.

   ```bash
   cd ~/elliott-wave-btc-bot
   docker compose -f deploy/live/docker-compose.yml stop live   # the container (not ewbot-backup.service)
   rm -f /var/lib/ewbot/journal.sqlite-wal /var/lib/ewbot/journal.sqlite-shm
   install -m 600 -o 1000 -g 1000 /var/lib/ewbot/journal.restore /var/lib/ewbot/journal.sqlite && rm /var/lib/ewbot/journal.restore
   docker compose -f deploy/live/docker-compose.yml start live
   ```

   On a new VPS there is no container yet: skip the stop and the start, `deploy.sh --recover` starts it ("Recovering from VPS loss").
4. Check `[시작]` in Telegram and `docker compose -f deploy/live/docker-compose.yml exec live ewbot live status`. What happened after the backup is not in the restored journal: a trade opened since is a position the journal does not know (adopted when the engine holds it - in CANARY a failure that pauses entries: check, then `/resume` - or closed), a trade that closed since is found closed, and the hours since the backup have no cycle (SHADOW ② fails: restart the stint, `ewbot live stage shadow --restart --confirm`).

**Restore drill** (once during SHADOW; it never touches the live journal or the running runtime): take a backup (`sudo systemctl start ewbot-backup.service`), download and decrypt it on the laptop (step 1) and send it into a drill directory - never over the live journal:

```bash
ssh <admin>@<vps> 'sudo install -d -m 700 -o 1000 -g 1000 /var/lib/ewbot/drill && sudo install -m 600 -o 1000 -g 1000 /dev/stdin /var/lib/ewbot/drill/journal.sqlite' < journal.sqlite
```

Then on the VPS, as `ewbot`, check it with the image - a sound SQLite file (`ok`) that reads like the live journal (the same stage, cycles and gates as the live status, up to the backup's time) - and remove it:

```bash
cd ~/elliott-wave-btc-bot
docker compose -f deploy/live/docker-compose.yml run --rm --no-deps -T --entrypoint python live -c "import sqlite3; print(sqlite3.connect('/data/drill/journal.sqlite').execute('PRAGMA integrity_check').fetchone()[0])"
docker compose -f deploy/live/docker-compose.yml run --rm --no-deps -T -e EWBOT_LIVE_DIR=/data/drill live status
docker compose -f deploy/live/docker-compose.yml exec live ewbot live status
rm -rf /var/lib/ewbot/drill
```

It proves the age key, the R2 copy and the transfer before they are needed. The restart for SHADOW ④ stays a step of its own (Stage transitions, step 2).

## Recovering from VPS loss

The brackets on Coinbase keep protecting an open position while the VPS is gone (spec 5.1), but nothing moves its stop, syncs its TP1 or closes it at the engine's exit, and nothing new is entered. Check the position and its brackets in the Coinbase app first; if it cannot wait for the steps below, close it there (a manual intervention: CANARY ④).

1. A new VPS: One-time setup steps 1–3 and 8, with its own public IPv4.
2. A new CDP key (setup step 4: View + Trade in CANARY and FULL, View only in SHADOW) with the **new** IPv4 in its IP allowlist: the old key's allowlist holds the old address, so Coinbase refuses it from the new VPS. Delete the old key in the CDP portal (it was on the lost disk) and send the new one to `/etc/ewbot/cdp_api_key.json` as in step 4.
3. `/etc/ewbot/live.env` (step 7: the same bot, chat and health-check URL) and `deploy/live/.env` (step 8). If the old VPS may be in someone else's hands rather than just gone, also replace the GitHub token (step 6) and the R2 token, and delete the old deploy key.
4. Backups on the new VPS (step 11: rclone, `/etc/ewbot/backup.env`, the timer).
5. Restore the newest backup: "Restoring the journal", steps 1–3.
6. Start the runtime on it without the flat-book check - the plain deploy refuses the open position (exit 3):

   ```bash
   cd ~/elliott-wave-btc-bot && bash deploy/live/deploy.sh --recover <the tag that ran before>
   ```

   Type `recover <tag>` when asked. It still checks the tag, the checkout and the preflight and keeps out of hh:54 to hh:05; the first start downloads the candle history, so it waits up to 10 minutes for the heartbeat.
7. Verify: `[시작]` in Telegram, `/status` answered, `docker compose -f deploy/live/docker-compose.yml exec live ewbot live status --exchange` (the position and the open orders as in the Coinbase app), the healthchecks.io checks up again; then "Restoring the journal", step 4.

## Deploying a new version

- The image verifies Coinbase's TLS certificates with the base image's CA store (`ca-certificates`, part of `python:3.12.13-slim-bookworm`); the build stops if `/etc/ssl/certs/ca-certificates.crt` is missing (a new base image without it): keep the pinned base, or install `ca-certificates` in the Dockerfile.
- Only tags on `master`, only with a flat book: `bash deploy/live/deploy.sh <tag>` (as `ewbot`, in the checkout). The new image's probes (`live preflight --offline`, `live status`) open the live journal read-only: a candidate never migrates the journal the running image uses. Nothing changes and the checkout goes back where it was when it refuses (exit 2) a missing tag or one off `master`, local changes or untracked files under `src/`, `deploy/`, `pyproject.toml`, `uv.lock` or `holdout.lock`, an image that does not build or fails its offline preflight with the service's own configuration (a one-off container of the new image with the service's mounts, secrets file, key file, user and read-only root: fingerprint `e0435223…`, lock file `6530175541c1…` with lock `d1be04baa06c…`, the settings, the alerts the journal's stage needs), the minutes hh:54 to hh:05 UTC (a swap, its wait and a rollback with its own wait end before the hour's cycle at hh:00:30, and nothing is swapped while its entries may go out, until hh:05: run it again from hh:06) or a book it could not check; or (exit 3) any open trade, order of unknown outcome, standing flatten, position or resting order.
- The swap keeps the running image as `ewbot-live:previous`, tags the new one `ewbot-live:current`, recreates the container and waits up to 3 minutes (10 on a first deployment) for a loop heartbeat journaled after the swap by a container of the new image (an old container still running after a failed recreate heartbeats too: it never counts). A failed `up -d` or no such heartbeat is a failed start: deploy.sh checks the book with `ewbot-live:previous` and the previous checkout - the new runtime may have traded meanwhile - and only if it is flat starts `ewbot-live:previous` again: exit 4 when that one is up again - read the failed release's log (`journalctl -t ewbot-live`) before the next try -, exit 5 when it is not either, or when the book was not flat (or could not be read) and nothing was rolled back (act now: `/status`, the logs, "Emergency procedures"). Rollback by hand = deploy the previous tag.
- `deploy.sh --recover <tag>` deploys while a position is open, for two cases only: a release that crash-loops while it holds a position (`[중단]` again and again, `/flatten` not acted on) and a new VPS after a VPS loss. It skips only the flat-book check (it prints the book instead), asks you to type `recover <tag>` (`--i-understand` skips the question) and never rolls back by itself (exit 5 when the new runtime does not come up); the new runtime takes over the position and its brackets through the restart recovery. Spec 5.2 allows deploys only with a flat book: this is the documented exception for bringing the runtime back.
- A change to any strategy-fingerprinted file (`types.py`, `config.py`, `indicators.py`, `data/candles.py`, `waves/`, `strategy/`, `engine/`, `execution/`, `risk/`) is refused by the preflight and would reset the paper runtime's out-of-sample counter: never deploy one.

## Weekly checks

- `df -h /var/lib/ewbot` and `docker system df`. Keep the images of `ewbot-live:current` and `ewbot-live:previous` (in `docker image ls ewbot-live` a tag with the same IMAGE ID as one of them stays), remove the older release tags (`docker image rm ewbot-live:<old tag>`), then `docker image prune -f` (dangling images) and `docker builder prune -f --filter until=720h` (build cache older than 30 days; the next build downloads what it needs again).
- `journalctl -u ewbot-backup.service -n 5`, the `ewbot-backup` check up, the newest backup in R2.
- Pending updates are installed by unattended-upgrades; if `/var/run/reboot-required` exists, reboot only with a flat book (`/flatten` not needed if no trade is open), then check `[시작]` in Telegram.
- Expiry reminders arrive 30 days ahead in the daily summary: from `EWBOT_EXPIRY` the Coinbase API key, the GitHub audit token and the Cloudflare Workers trigger token of the paper runtime; without a setting the contract's expiry (`BIP-20DEC30-CDE 만기`) and the day the preflight starts refusing it (`BIP-20DEC30-CDE 시작 점검 거부`).
- Coinbase's notices for `BIP-20DEC30-CDE` (a delisting, a new expiry): the preflight checks the contract's expiry (`contract_expiry` no earlier than 2030-12-20 and more than 90 days away) only when the runtime starts.

## Cost

VPS about $5–7 a month; healthchecks.io, R2 (a few MB) and Telegram are free at this size. Exchange fees: $0.10 per BIP contract per side plus the clearing and NFA fees (at least $0.15 per contract per side in total, spec 2).
