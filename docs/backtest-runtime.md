# Backtest runtime log

| when (UTC) | label | seconds | budget | result | exit | machine |
|---|---|---|---|---|---|---|
| 2026-10-02 02:00 | fetch BTC-USD 1h | 67.0 | - | n/a | 0 | arm64 15 cpu Darwin |
| 2026-10-02 02:01 | backtest A 2017-2023 | 4.0 | 180 | PASS | 0 | arm64 15 cpu Darwin |
| 2026-10-02 02:01 | compare 2019-2023 random=5 | 17.9 | - | n/a | 0 | arm64 15 cpu Darwin |
| 2026-10-02 02:02 | compare 2019-2023 random=100 | 80.9 | - | n/a | 0 | arm64 15 cpu Darwin |

Walk-forward estimate: 60 combos x 4s / 14 workers = about 0 minutes (plus a few seconds of stitching).
