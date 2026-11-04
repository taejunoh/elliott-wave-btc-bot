# ewbot Plan 7 ① — 전략 E(일봉 채널 돌파 추세 추종) 횟수 점검 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 전략 A와 B를 전혀 바꾸지 않고 `src/ewbot/strate/`에 E의 규칙, 1시간봉 엔진, 데이터 가드, 횟수 점검을 만들고 `ewbot strate count`로 묶는다. 이 명령은 후보 4개의 체결된 진입을 2017-01-01 ~ 2023-12-31 구간에서 세어, 건수 중앙값 ≥ 84(월 1.0)인지만 판정한다. 성과 값은 계산해 내보내지 않고, 2024-01-01 이후 봉은 메모리에 올리지 않는다.

**Architecture:** 순수 함수(`rules.py`)가 1시간봉에서 UTC 일봉, 신호 봉을 뺀 채널, Wilder ATR, 일봉 판단을 만든다. `engine.py`는 A의 `PaperBroker`와 같은 순서로 1시간봉을 돌지만, 체결가에서 손절을 정하고 그 자리에서 크기를 계산해야 해서 브로커를 쓰지 않고 `execution.costs`의 `fee`·`slip`·`funding_per_hour`와 `risk.sizing.size_contracts`, `data.candles`의 달력과 `fill_gaps`만 import한다. 엔진은 시간당 자산 곡선과 거래 기록도 내보내지만(②용) `count.py`는 `entry_ts`만 읽고, 그 import 사슬에는 성과·파동·전략 모듈이 없다. `data.py`는 parquet 필터로 구간 밖 봉을 거르고, 가격을 읽기 전에 `ts` 열만으로 캐시를 검사한다. 일꾼은 D와 같은 spawn 방식이고 연도별 건수만 돌려준다.

**Tech Stack:** Python 3.12(uv), 기존 의존성만(numpy, pandas, pyarrow의 parquet 필터), pytest. 새 의존성 없음.

**Spec:** `docs/superpowers/specs/2026-11-04-ewbot-plan7-strategy-e-design.md`. 이 계획은 그 명세의 ① 단계만 다룬다.

## Global Constraints

- 작업 위치: 브랜치 `docs/strategy-e`(명세 커밋 `acdad37` 위, 전략 D와 같은 방식), 저장소 루트. 테스트 명령은 `uv run --group live pytest …`.
- **범위는 ①뿐이다.** 만들지 않는 것: `research.py`, `holdout.py`, `holdout-e.lock`, 잠금, 워크포워드, 모의매매, 손실 한도 코드(②는 끄고 ③에서 켜므로 ③의 계획이 더한다), 새 워크플로.
- **수정 금지(폴더에 파일 추가 포함):** `src/ewbot/types.py`, `config.py`, `indicators.py`, `data/candles.py`, `waves/`, `strategy/`, `engine/`, `execution/`, `risk/`. 같은 금지: `src/ewbot/paper/`, `live/`, `backtest/`, `holdout.lock`, `.github/workflows/paper.yml`, `.github/workflows/ci.yml`, `src/ewbot/stratb/`, `src/ewbot/paperb/`, A의 캐시 파일 `data/cache/BTC-USD_1h.parquet`. A 지문은 `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1`로 유지된다. 고칠 수 있는 기존 파일은 `src/ewbot/cli.py`뿐이고 명령 하나만 더한다.
- **금지어:** `strate/`는 `allow_holdout`, `include_holdout`, `unlock_2024`, `check_before_holdout`, `_map_tasks`, `_init_worker`를 어떤 형태로도(주석과 문서 문자열 포함) 쓰지 않는다. `unlock_holdout_e`는 ①에서 어디에도 없다.
- **이름 충돌:** `ewbot.strategy`는 문자열 `ewbot.strate`로 시작한다. 모듈과 경로를 접두어로 비교할 때는 반드시 `ewbot.strate.`, `src/ewbot/strate/`처럼 구분자까지 쓴다.
- **명세 2절의 값(그대로):** 자산 BTC-USD 현물 시세, 비용은 BIP 기준(A와 같음). 판단 봉 UTC 일봉(1시간봉 24개, 00:00 UTC 마감). 진입: 포지션이 없을 때 일봉 종가 > 직전 N일 최고가면 롱, 종가 < 직전 N일 최저가면 숏, 다음 1시간봉 시가에 체결. 청산: 롱은 일봉 종가 < 직전 N/2일 최저가, 숏은 일봉 종가 > 직전 N/2일 최고가, 다음 1시간봉 시가에 체결. 초기 손절: 체결가에서 2 × ATR(20일), 이후 옮기지 않고 1시간봉으로 확인. 반대 전환: 청산 신호와 같은 종가가 반대쪽 N일 돌파이면 청산과 같은 시가에 반대로 진입. 후보 N: 20, 40, 55, 100 (N/2는 정수 내림: 10, 20, 27, 50). 위험: 거래당 자본의 2%, 최대 레버리지 3배, 시작 자본 10,000과 계약 크기 0.01은 A의 백테스트와 같음. 비용: `CostParams` 기본값(수수료 0.05%, 슬리피지 0.05%, 롱 펀딩 연 10%). 금요일 휴장·블랙아웃: A와 같음, 휴장 중 나온 신호는 다음 열리는 시가에 체결. 직전 N일에는 신호 봉을 넣지 않는다. 합성 봉은 `fill_gaps`로 만들고 일봉에 포함한다.
- **명세 5.1의 값(그대로):** 구간 2017-01-01 ~ 2023-12-31, 84개월. 체결된 진입을 진입 시각 기준으로 센다. 손실 한도는 끈다. 수익, R, 승률, 자산, 청산 사유를 계산하거나 출력하지 않는다. 후보 4개의 월평균 건수 중앙값 ≥ **1.0** (건수 중앙값 ≥ **84**)이면 ②로 가고, 미달이면 E는 영구히 끝난다.
- **데이터(명세 4절):** `data/cache/BTC-USD_1h.parquet`를 읽기만 하고, 읽을 때마다 pyarrow의 parquet 필터 `ts >= 2017-01-01 00:00 UTC`, `ts < 2024-01-01 00:00 UTC`를 건다. 필터를 푸는 인자는 없다. 값: `START_TS = 1_483_228_800`, `END_TS = 1_704_067_200`, 기대 시간 수 61,344(2,556일).
- **빠진 시간의 수를 정하는 방법:** 이 계획은 실제 수를 추측하지 않고 코드와 테스트도 어떤 실제 값을 가정하지 않는다. 실행할 때 `ts` 열만 읽어 `빠진 시간 = 61,344 − (구간 안의 서로 다른 ts 수)`로 잰다. 가격을 읽기 전에 재고, 보고서(`data.missing_hours`)와 출력에 남긴다. 거부 기준은 `MAX_MISSING_HOURS = 300`이다. 이 값은 A-ETH 1단계가 쓴 한도(`docs/aeth.md`)이고 실제 값의 추정이 아니다. 첫 봉이 2017-01-01 00:00, 마지막 봉이 2023-12-31 23:00 UTC가 아니어도 거부한다. 거부되면 한도나 캐시를 고치지 않고 수치를 사용자에게 보고한다. 재는 명령은 끝의 "실제 실행"에 있다.
- **`count.py`:** 일꾼은 연도별 진입 건수만 돌려준다. 엔진이 내보내는 `equity`와 `trades`를 읽지 않는다. 판정선, 구간, 후보를 바꾸는 CLI 옵션은 없다. 보고서는 `reports/strate-count.json`이다(`reports/`는 git에 올라가지 않는다).
- **테스트 원칙:** 모든 기댓값은 `tests/strate/helpers.py`의 경로에서 손으로 계산한 수이고, 엔진끼리 비교하지 않는다. 합성 프레임만 쓰고 `data/cache`를 읽는 테스트는 없다. 가드마다 그 가드가 실제로 잡는지 보는 비공허성 검사가 함께 있다.
- **이 계획의 검증 범위:** 코드 블록은 저장소 밖의 임시 사본(시장 데이터 없음)에서 이 문서만으로 다시 만들어 Task 순서대로 실행했고, 아래의 실패와 통과 수는 그 결과다. 규칙을 깨는 변이 18개(채널에 신호 봉 포함, 신호를 손절보다 먼저, 숏 펀딩 부호, 필터 제거, 판정선 등)는 모두 테스트에 잡혔다. 실제 캐시로는 어떤 것도 돌리지 않았고 `uv` 대신 저장소의 `.venv`로 pytest를 직접 실행했다.
- 커밋 메시지는 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`로 끝난다.

## 명세의 모호점과 이 계획의 해석

규칙을 바꾸지 않았다. 명세가 정하지 않았거나 실제로 작동하지 않는 곳과 이 계획이 택한 해석이다. **1, 4번은 손절 경로를 통해 건수를, 2번은 ②부터의 손익을 바꿀 수 있으므로 실제 실행 전에 사용자가 확인한다.**

1. **ATR의 종류.** 명세는 "ATR(20일)"만 적었다. A의 Wilder ATR(`ewbot.indicators.atr_series`, 처음 값은 TR 20개의 단순 평균)을 일봉에 쓰고 신호 봉까지 포함한다(종가에 이미 아는 값이다). 명세 3절의 import 목록에 `indicators`는 없지만 import만 하고 고치지 않는다.
2. **크기 계산 시점.** 명세가 손절을 "체결가에서" 재므로 크기도 체결 순간에 정한다(A는 신호 봉 종가에서 정한다). 그 순간 포지션이 없으므로 자본은 현금이다. `size_contracts`의 `entry`에는 미끄러짐 전 시가를 넘긴다. 롱에서 체결가 E = O(1+s), 손절 S = E − 2A, 손절 체결 S(1−s)이면 실제 손실 폭은 2A + sS이고, `size_contracts`의 `(O − S) + s(O + S)`도 2A + sS다.
3. **금요일 조항은 진입에 작동하지 않는다.** 금요일 16:00~18:00 뉴욕은 UTC로 늦어도 23:00에 끝나므로 신호 봉(23:00 UTC)과 체결 봉(00:00 UTC)은 2017~2023 전 구간에서 겹치지 않는다(확인함, `test_the_real_calendar_never_touches_a_daily_signal_or_its_fill`). "휴장 중 나온 신호"는 실제 달력에서 생기지 않고 달력은 손절 체결에만 작용한다. A는 블랙아웃의 신호를 버리지만 명세는 미룬다고 적었으므로 미루도록 구현했다(진입은 블랙아웃이 끝날 때까지, 청산은 휴장이 끝날 때까지). 이 경로의 테스트는 가짜 달력을 쓴다.
4. **심지 자르기(`clip_wicks`).** A는 전략 입력에 쓰지만 명세에 없고 파동 쪽 전처리라 E는 쓰지 않는다. 일봉 고가·저가는 원래 값이다.
5. **"자산을 계산하지 않는다"(5.1).** 크기 계산에 자본이 필요해 엔진 안에서는 계산한다. `count.py`가 읽거나 내보내지 않는 것으로 해석했다(D와 같다).
6. **"직전 N일 최고가·최저가"** 는 종가가 아니라 일봉 고가·저가다.
7. **쿨다운 없음.** 손절 뒤 포지션이 없으면 같은 봉의 종가로도 진입 신호가 날 수 있다(`test_the_stop_wins_when_it_shares_a_bar_with_the_signal`). 명세에 쿨다운이 없다.
8. **명세가 말하지 않는 끝 처리.** 데이터 끝의 포지션은 A처럼 마지막 종가에 닫는다(진입 건수에는 영향이 없다). 마지막 봉의 신호는 체결될 시가가 없어 버린다. 크기가 0이면 체결이 아니므로 세지 않고, 전환의 청산은 그대로 두며, 다음 일봉에서 다시 판단한다. 합성 봉에서도 A처럼 체결과 손절이 일어난다.
9. **빠진 시간 한도.** 명세 6절은 "빠진 시간이 많으면 거부"라고만 적었다. 위의 Global Constraints대로 300을 쓴다.
10. **잠금 관련 테스트.** "잠금 전 키워드 거부"는 ①에서 `unlock_holdout_e` 인자가 아예 없어 `TypeError`로 시험한다. "이름만 맞는 잠금 거부"는 잠금 코드가 생기는 뒤 계획으로 넘긴다.

## 명세 6절 대응표

| 명세 6절 | 테스트 |
|---|---|
| 채널: 신호 봉 미포함, 손으로 계산한 신호·체결 | `test_rules.py::test_a_channel_holds_the_days_before_the_bar_not_the_bar_itself`, `test_engine.py`의 진입·청산·전환 경로 |
| 손절: 고정, 간격, 손절 우선 | `test_the_stop_never_moves`, `test_an_open_beyond_the_stop_fills_at_the_open_with_slippage`, `test_the_stop_wins_when_it_shares_a_bar_with_the_signal` |
| 비용: 시간당 펀딩, 숏 | `test_funding_is_an_hourly_amount_on_the_close`, `test_a_short_receives_funding`, `test_a_whole_trade_with_a_s_costs` |
| 일봉: 00:00 UTC 경계, 합성 봉, 미완성 | `test_a_day_runs_from_the_00_utc_bar_to_the_23_utc_bar`, `test_filled_hours_belong_to_the_day_and_mark_it_synthetic`, `test_a_filled_last_hour_counts_in_the_day`, `test_incomplete_days_make_no_daily_bar`, `test_an_incomplete_day_gives_no_signal` |
| 2024 가드: parquet 거름, 키워드 거부 | `test_data.py::test_a_parquet_with_2024_bars_and_bars_before_2017_gives_only_the_window`, `test_every_read_is_filtered_by_the_parquet_reader_and_the_check_reads_ts_alone_first`, `test_the_unlock_keyword_is_refused_before_the_lock` (이름만 맞는 잠금은 뒤 계획) |
| 격리: 금지어, A 지문, 전체 테스트 | `test_isolation.py`, Task 4의 전체 실행 |
| `count.py`: 성과 모듈·값 없음 | `test_count.py`의 "no performance value" 묶음 |
| 캐시 거부 | `test_data.py`의 coverage 묶음, `test_cli_strate.py::test_a_short_cache_is_refused_and_writes_nothing` |

## Tasks

### Task 1: 고정값과 순수 규칙

**Files:**
- Create: `src/ewbot/strate/__init__.py`, `src/ewbot/strate/config.py`, `src/ewbot/strate/rules.py`
- Create: `tests/strate/__init__.py`(빈 파일), `tests/strate/helpers.py`, `tests/strate/test_rules.py`

**Interfaces:**
- Consumes:
  - `ewbot.config.CostParams`, `ewbot.config.RiskProfile(name: str, risk_pct: float, max_leverage: float)`
  - `ewbot.indicators.atr_series(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray`
  - `ewbot.types.H1`
  - 테스트만: `ewbot.backtest.runner.utc_ts(date: str) -> int`, `ewbot.backtest.walkforward.HOLDOUT_START`, `ewbot.config.BotConfig`, `ewbot.data.candles.fill_gaps(df: pd.DataFrame, tf: int = H1) -> pd.DataFrame`
- Produces:
  - `strate.config`: `CANDIDATES`, `ATR_DAYS`, `ATR_STOP_MULT`, `RISK`, `COSTS`, `INITIAL_EQUITY`, `DAY`, `START_TS`, `END_TS`, `MONTHS`, `exit_days(n: int) -> int`
  - `strate.rules`: `DayLevels(close, upper, lower, exit_upper, exit_lower, atr)`(모두 `float`), `daily_bars(df1h: pd.DataFrame) -> pd.DataFrame`, `channel_high(high: np.ndarray, days: int) -> np.ndarray`, `channel_low(low: np.ndarray, days: int) -> np.ndarray`, `daily_atr(daily: pd.DataFrame) -> np.ndarray`, `day_levels(df1h: pd.DataFrame, n: int) -> dict[int, DayLevels]`, `decide(direction: int, lv: DayLevels) -> tuple[bool, int]`
  - `tests.strate.helpers`: `T0`, `H`, `DAY`, `FREE`, `FUNDING_ONLY`, `day`, `quiet`, `with_hour`, `frame`, `base`, `long_setup`, `short_setup`, `flip_path`

- [ ] **Step 1: 실패하는 테스트를 쓴다.** 빈 파일 `tests/strate/__init__.py`를 만들고 아래 두 파일을 쓴다.

`tests/strate/helpers.py` (전체 내용):

```python
"""Hand-built hourly paths for strategy E's tests. Every expected number in the tests is computed by hand from these."""

from __future__ import annotations

import pandas as pd

from ewbot.config import CostParams

T0 = 1_483_228_800  # 2017-01-01 00:00 UTC, a Sunday; day 26 is Friday 2017-01-27
H = 3600
DAY = 86_400
FREE = CostParams(fee_rate=0.0, slippage=0.0, funding_apr_long=0.0)
FUNDING_ONLY = CostParams(fee_rate=0.0, slippage=0.0)


def day(i: int, o: float, hi: float, lo: float, c: float, *, low_hour: int = 12, high_hour: int = 11) -> list[dict]:
    """Day i's 24 bars: flat at o, the day's high in high_hour, its low in low_hour, the close in the last hour."""
    rows = []
    for k in range(24):
        close = c if k == 23 else o
        rows.append({
            "ts": T0 + i * DAY + k * H,
            "open": o,
            "high": max(o, close, hi if k == high_hour else o),
            "low": min(o, close, lo if k == low_hour else o),
            "close": close,
            "volume": 1.0,
        })
    return rows


def quiet(i: int, p: float) -> list[dict]:
    """A day at p with high p + 5 and low p - 5: a true range of 10 while the previous close is within 5 of p."""
    return day(i, p, p + 5.0, p - 5.0, p)


def with_hour(rows: list[dict], k: int, o: float, hi: float, lo: float, c: float) -> list[dict]:
    out = [dict(r) for r in rows]
    out[k].update(open=o, high=hi, low=lo, close=c)
    return out


def frame(*days: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([r for d in days for r in d]).astype({"ts": "int64"})


def base() -> list[list[dict]]:
    """Days 0-19 at 100: the 20-day channel is 105 / 95 and the 20-day ATR is exactly 10."""
    return [quiet(i, 100.0) for i in range(20)]


def long_setup() -> list[list[dict]]:
    """base + day 20 closing at 109 with a high of 110: long signal (109 > 105), and only because day 20 is not in its own
    channel. True range 10 (110 - 100), so ATR stays 10. The entry fills at day 21's open."""
    return [*base(), day(20, 100.0, 110.0, 100.0, 109.0)]


def short_setup() -> list[list[dict]]:
    """base + day 20 closing at 91 with a low of 90: short signal (91 < 95). True range 10, so ATR stays 10."""
    return [*base(), day(20, 100.0, 100.0, 90.0, 91.0)]


def flip_path() -> pd.DataFrame:
    """Long from day 21 at 110 (stop 90), then a close at 91 on day 25, below both channels (95): flip at day 26's open, 92.
    Without costs: 1000 contracts long, -180; 915 contracts short, closed at the end at 92. Two entries for N = 20."""
    return frame(*long_setup(), *[quiet(i, 110.0) for i in range(21, 25)], day(25, 110.0, 115.0, 90.5, 91.0), quiet(26, 92.0))
```

`tests/strate/test_rules.py` (전체 내용):

```python
from __future__ import annotations

import math

import numpy as np
import pytest

from ewbot.backtest.runner import utc_ts
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.config import BotConfig, CostParams
from ewbot.data.candles import fill_gaps
from ewbot.strate import config
from ewbot.strate.rules import DayLevels, channel_high, channel_low, daily_atr, daily_bars, day_levels, decide
from tests.strate.helpers import DAY, H, T0, base, day, frame, long_setup, quiet

NAN = float("nan")


def test_the_fixed_values():
    assert config.CANDIDATES == (20, 40, 55, 100)
    assert [config.exit_days(n) for n in config.CANDIDATES] == [10, 20, 27, 50]
    assert (config.ATR_DAYS, config.ATR_STOP_MULT) == (20, 2.0)
    assert (config.RISK.risk_pct, config.RISK.max_leverage) == (0.02, 3.0)
    assert config.COSTS == CostParams()
    assert (config.COSTS.fee_rate, config.COSTS.slippage, config.COSTS.funding_apr_long) == (0.0005, 0.0005, 0.10)
    assert (config.COSTS.contract_size, config.COSTS.apply_min_fee) == (0.01, False)
    assert config.INITIAL_EQUITY == BotConfig().initial_equity == 10_000.0
    assert config.START_TS == utc_ts("2017-01-01") == T0
    assert config.END_TS == utc_ts("2024-01-01") == HOLDOUT_START
    assert config.MONTHS == 84


# ----------------------------------------------------------------------------- daily bars


def test_a_day_runs_from_the_00_utc_bar_to_the_23_utc_bar():
    d = daily_bars(frame(day(0, 10.0, 12.0, 9.0, 11.0), day(1, 20.0, 22.0, 19.0, 21.0)))
    assert d["ts"].tolist() == [T0, T0 + DAY]
    assert d.loc[0, ["open", "high", "low", "close"]].tolist() == [10.0, 12.0, 9.0, 11.0]
    assert d.loc[1, ["open", "high", "low", "close"]].tolist() == [20.0, 22.0, 19.0, 21.0]


def test_incomplete_days_make_no_daily_bar():
    rows = day(0, 10.0, 12.0, 9.0, 11.0)[1:] + day(1, 20.0, 22.0, 19.0, 21.0) + day(2, 30.0, 32.0, 29.0, 31.0)[:23]
    assert daily_bars(frame(rows))["ts"].tolist() == [T0 + DAY]


def test_filled_hours_belong_to_the_day_and_mark_it_synthetic():
    raw = frame(day(0, 10.0, 12.0, 9.0, 11.0)[:23], day(1, 20.0, 22.0, 19.0, 21.0))  # day 0 lost its 23:00 bar
    assert daily_bars(raw)["ts"].tolist() == [T0 + DAY]  # before fill_gaps the day is incomplete
    d = daily_bars(fill_gaps(raw))
    assert d["ts"].tolist() == [T0, T0 + DAY]
    assert d["synthetic"].tolist() == [True, False]
    assert d.loc[0, "close"] == 10.0  # the filled bar is flat at the previous close


# ----------------------------------------------------------------------------- channels


def test_a_channel_holds_the_days_before_the_bar_not_the_bar_itself():
    up = channel_high(np.array([10.0, 11.0, 12.0, 50.0, 13.0]), 2)
    dn = channel_low(np.array([5.0, 4.0, 3.0, 1.0, 2.0]), 2)
    assert np.isnan(up[:2]).all() and up[2:].tolist() == [11.0, 12.0, 50.0]  # index 3: 12, not its own 50
    assert np.isnan(dn[:2]).all() and dn[2:].tolist() == [4.0, 3.0, 1.0]  # index 3: 3, not its own 1


def test_a_later_bar_never_changes_an_earlier_channel():
    high = np.array([10.0, 11.0, 12.0, 50.0, 13.0])
    changed = high.copy()
    changed[4] = 999.0
    np.testing.assert_array_equal(channel_high(high, 2), channel_high(changed, 2))
    changed[3] = 999.0  # non-vacuity: the channel of index 4 does read index 3
    assert channel_high(changed, 2)[4] == 999.0


def test_the_entry_and_exit_channels_cover_n_and_n_over_2_days():
    days = [quiet(i, 100.0) for i in range(21)]
    days[5] = day(5, 100.0, 140.0, 60.0, 100.0)  # in the 20 days before day 20, not in the 10 days before it
    lv = day_levels(fill_gaps(frame(*days)), 20)[T0 + 20 * DAY + 23 * H]
    assert (lv.upper, lv.lower, lv.exit_upper, lv.exit_lower) == (140.0, 60.0, 105.0, 95.0)


def test_there_is_no_entry_channel_before_n_days_and_levels_are_keyed_by_the_last_hour():
    levels = day_levels(fill_gaps(frame(*long_setup())), 20)
    assert set(levels) == {T0 + i * DAY + 23 * H for i in range(21)}
    assert math.isnan(levels[T0 + 19 * DAY + 23 * H].upper)  # 19 earlier days
    assert levels[T0 + 19 * DAY + 23 * H].exit_upper == 105.0  # 10 earlier days exist
    assert levels[T0 + 20 * DAY + 23 * H] == DayLevels(109.0, 105.0, 95.0, 105.0, 95.0, 10.0)


# ----------------------------------------------------------------------------- ATR


def test_atr_is_wilder_over_20_days_and_includes_the_day_it_is_read_on():
    atr = daily_atr(daily_bars(frame(*base(), day(20, 100.0, 111.0, 99.0, 110.0))))
    assert np.isnan(atr[:19]).all()
    assert atr[19] == 10.0  # every true range of days 0-19 is 10
    assert atr[20] == pytest.approx((10.0 * 19 + 12.0) / 20)  # day 20's true range: 111 - 99 = 12


# ----------------------------------------------------------------------------- the daily decision


def lv(close: float, upper: float = 105.0, lower: float = 95.0, xup: float = 103.0, xdn: float = 97.0) -> DayLevels:
    return DayLevels(close, upper, lower, xup, xdn, 10.0)


@pytest.mark.parametrize(
    ("direction", "level", "want"),
    [
        (0, lv(105.1), (False, 1)),
        (0, lv(105.0), (False, 0)),  # strict
        (0, lv(94.9), (False, -1)),
        (0, lv(95.0), (False, 0)),  # strict
        (0, lv(100.0), (False, 0)),
        (0, lv(200.0, upper=NAN, lower=NAN), (False, 0)),  # no channel, no signal
        (0, lv(1.0, upper=NAN, lower=NAN), (False, 0)),
        (1, lv(200.0), (False, 0)),  # no second entry
        (1, lv(97.0), (False, 0)),  # strict
        (1, lv(96.9), (True, 0)),  # exit only
        (1, lv(95.0), (True, 0)),  # exit only: the N-day channel is strict too
        (1, lv(94.9), (True, -1)),  # flip
        (-1, lv(1.0), (False, 0)),
        (-1, lv(103.0), (False, 0)),
        (-1, lv(103.1), (True, 0)),
        (-1, lv(105.0), (True, 0)),
        (-1, lv(105.1), (True, 1)),
    ],
)
def test_decide(direction, level, want):
    assert decide(direction, level) == want
```

- [ ] **Step 2: 실패를 확인한다.**

```bash
uv run --group live pytest tests/strate/test_rules.py -q
```

Expected: 수집 오류 `ModuleNotFoundError: No module named 'ewbot.strate'`.

- [ ] **Step 3: 구현한다.**

`src/ewbot/strate/__init__.py` (전체 내용):

```python
"""Strategy E (Plan 7): daily channel-breakout trend following. Research code, separate from strategy A."""
```

`src/ewbot/strate/config.py` (전체 내용):

```python
"""Strategy E's fixed values (Plan 7 spec 2, 5.1). None of them is an option of any command."""

from __future__ import annotations

from ewbot.config import CostParams, RiskProfile
from ewbot.types import H1

CANDIDATES = (20, 40, 55, 100)  # N, the entry channel in days
ATR_DAYS = 20
ATR_STOP_MULT = 2.0
RISK = RiskProfile("strategy-e", 0.02, 3.0)  # 2% per trade, leverage at most 3
COSTS = CostParams()  # A's defaults: fee 0.05%, slippage 0.05%, long funding 10% a year, contract 0.01 BTC
INITIAL_EQUITY = 10_000.0  # as BotConfig().initial_equity
DAY = 24 * H1
START_TS = 1_483_228_800  # 2017-01-01 00:00 UTC
END_TS = 1_704_067_200  # 2024-01-01 00:00 UTC, exclusive
MONTHS = 84  # 2017-01 .. 2023-12


def exit_days(n: int) -> int:
    """The exit channel: N/2 days, rounded down."""
    return n // 2
```

`src/ewbot/strate/rules.py` (전체 내용):

```python
"""Strategy E's rules as pure functions (Plan 7 spec 2): UTC daily bars from 1h bars, channels, ATR, the daily decision.

A channel holds the days BEFORE a bar, never the bar itself. ATR is A's Wilder ATR on daily bars and includes the day it is
read on, which is known at that day's close."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ewbot.indicators import atr_series
from ewbot.strate.config import ATR_DAYS, DAY, exit_days
from ewbot.types import H1


@dataclass(frozen=True, slots=True)
class DayLevels:
    """What is known at the close of one UTC day. Channels are NaN until enough earlier days exist."""

    close: float
    upper: float  # highest high of the N days before this one
    lower: float  # lowest low of the N days before this one
    exit_upper: float  # the same over N/2 days
    exit_lower: float
    atr: float


def daily_bars(df1h: pd.DataFrame) -> pd.DataFrame:
    """UTC days [00:00, 24:00) of a 1h frame with ts = the day's 00:00. Only days with all 24 hours: run fill_gaps first, so
    only the two ends of the data can be incomplete. ``synthetic`` is True if any hour of the day was filled."""
    d = df1h.drop_duplicates("ts").sort_values("ts")
    syn = d["synthetic"].astype(bool) if "synthetic" in d.columns else False
    d = d.assign(day=(d["ts"] // DAY) * DAY, synthetic=syn)
    agg = d.groupby("day", sort=True).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        synthetic=("synthetic", "any"),
        n=("ts", "size"),
    )
    agg = agg[agg["n"] == DAY // H1].drop(columns="n")
    return agg.rename_axis("ts").reset_index().astype({"ts": "int64", "synthetic": "bool"})


def channel_high(high: np.ndarray, days: int) -> np.ndarray:
    """Highest high of the ``days`` bars before each bar; NaN until that many earlier bars exist."""
    return pd.Series(high, dtype="float64").rolling(days).max().shift(1).to_numpy(dtype=np.float64)


def channel_low(low: np.ndarray, days: int) -> np.ndarray:
    """Lowest low of the ``days`` bars before each bar; NaN until that many earlier bars exist."""
    return pd.Series(low, dtype="float64").rolling(days).min().shift(1).to_numpy(dtype=np.float64)


def daily_atr(daily: pd.DataFrame) -> np.ndarray:
    """A's Wilder ATR over ATR_DAYS daily bars; NaN until ATR_DAYS bars have been seen."""
    return atr_series(
        daily["high"].to_numpy(dtype=np.float64),
        daily["low"].to_numpy(dtype=np.float64),
        daily["close"].to_numpy(dtype=np.float64),
        ATR_DAYS,
    )


def day_levels(df1h: pd.DataFrame, n: int) -> dict[int, DayLevels]:
    """{ts of the day's last 1h bar: that day's levels}, for complete days only. ``df1h`` is gap-filled."""
    d = daily_bars(df1h)
    high = d["high"].to_numpy(dtype=np.float64)
    low = d["low"].to_numpy(dtype=np.float64)
    x = exit_days(n)
    rows = zip(
        d["ts"].to_numpy(dtype=np.int64),
        d["close"].to_numpy(dtype=np.float64),
        channel_high(high, n),
        channel_low(low, n),
        channel_high(high, x),
        channel_low(low, x),
        daily_atr(d),
    )
    return {
        int(ts) + DAY - H1: DayLevels(float(c), float(up), float(dn), float(xup), float(xdn), float(atr))
        for ts, c, up, dn, xup, xdn, atr in rows
    }


def decide(direction: int, lv: DayLevels) -> tuple[bool, int]:
    """(exit the held position?, side to enter or 0) at one daily close. ``direction`` is 0 when flat. All comparisons are
    strict and a NaN channel compares False, so there is no signal before the channel exists. The N-day channel lies outside
    the N/2-day channel, so a close through the opposite N-day channel is always an exit too: that is the flip."""
    if direction == 0:
        if lv.close > lv.upper:
            return False, 1
        if lv.close < lv.lower:
            return False, -1
        return False, 0
    if direction == 1:
        if lv.close < lv.exit_lower:
            return True, -1 if lv.close < lv.lower else 0
        return False, 0
    if lv.close > lv.exit_upper:
        return True, 1 if lv.close > lv.upper else 0
    return False, 0
```

- [ ] **Step 4: 통과를 확인한다.**

```bash
uv run --group live pytest tests/strate/test_rules.py -q
```

Expected: `26 passed`.

- [ ] **Step 5: 커밋한다.**

```bash
git add src/ewbot/strate/__init__.py src/ewbot/strate/config.py src/ewbot/strate/rules.py tests/strate/__init__.py tests/strate/helpers.py tests/strate/test_rules.py
git commit -m "feat(strate): fixed values and pure rules: UTC daily bars, channels without the signal bar, Wilder ATR, the daily decision" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2: 1시간봉 엔진

**Files:**
- Create: `src/ewbot/strate/engine.py`, `tests/strate/test_engine.py`

**Interfaces:**
- Consumes:
  - Task 1: `strate.config`의 값, `strate.rules.DayLevels`, `day_levels`, `decide`, `tests.strate.helpers`
  - `ewbot.data.candles.fill_gaps`, `to_bars(df: pd.DataFrame) -> list[Bar]`, `futures_closed(ts: int) -> bool`, `entry_blackout(ts: int) -> bool`
  - `ewbot.execution.costs.fee(price: float, contracts: int, c: CostParams) -> float`, `slip(price: float, side: int, c: CostParams) -> float`, `funding_per_hour(mark: float, contracts: int, direction: int, c: CostParams) -> float`
  - `ewbot.risk.sizing.size_contracts(equity: float, entry: float, stop: float, direction: int, risk: RiskProfile, risk_mult: float, c: CostParams) -> int`
  - `ewbot.types.Bar(ts, open, high, low, close, volume, synthetic)`
- Produces:
  - `strate.engine.EquityPoint(ts: int, equity: float)`
  - `strate.engine.Trade(entry_ts: int, exit_ts: int, direction: int, contracts: int, entry_price: float, exit_price: float, stop: float, pnl: float, fees: float, funding: float, exit_reason: str)`
  - `strate.engine.SimResult(entry_ts: tuple[int, ...], trades: tuple[Trade, ...], equity: tuple[EquityPoint, ...])`
  - `strate.engine.simulate(df1h: pd.DataFrame, n: int, costs: CostParams = COSTS, risk: RiskProfile = RISK) -> SimResult`

`EquityPoint`와 `Trade`의 이름과 필드는 A의 것을 따르되 여기서 정의한다. A의 `EquityPoint`가 있는 `engine/position_mgr.py`를 import하면 파동과 전략 코드가 함께 올라오기 때문이다(명세 3절). `compute_metrics`에 넘길 때 필요한 `r_multiple`은 ②의 계획이 더한다.

- [ ] **Step 1: 실패하는 테스트를 쓴다.**

`tests/strate/test_engine.py` (전체 내용):

```python
"""Hand-computed paths. FREE = no fee, no slippage, no funding: 2% of 10,000 is 200, so a stop 20 away is 10 BTC = 1000 contracts."""

from __future__ import annotations

import pytest

from ewbot.config import CostParams, RiskProfile
from ewbot.data.candles import entry_blackout, futures_closed
from ewbot.strate import engine
from ewbot.strate.config import DAY as CONFIG_DAY
from ewbot.strate.config import END_TS, START_TS
from ewbot.strate.engine import simulate
from tests.strate.helpers import (
    DAY,
    FREE,
    FUNDING_ONLY,
    H,
    T0,
    base,
    day,
    flip_path,
    frame,
    long_setup,
    quiet,
    short_setup,
    with_hour,
)


def at(i: int, hour: int = 0) -> int:
    return T0 + i * DAY + hour * H


def check_consistent(res) -> None:
    assert res.entry_ts == tuple(t.entry_ts for t in res.trades)


# ----------------------------------------------------------------------------- entry, exit, flip


def test_a_long_fills_at_the_next_open_and_exits_at_the_open_after_the_short_channel_breaks():
    # Day 31 closes at 100, below the 10-day low of 105 and above the 20-day low of 95: exit only, at day 32's open, 101.
    df = frame(*long_setup(), *[quiet(i, 110.0) for i in range(21, 31)], day(31, 110.0, 110.0, 100.0, 100.0), quiet(32, 101.0))
    res = simulate(df, 20, FREE)
    assert res.entry_ts == (at(21),)
    (t,) = res.trades
    assert (t.entry_ts, t.direction, t.contracts, t.entry_price, t.stop) == (at(21), 1, 1000, 110.0, 90.0)
    assert (t.exit_ts, t.exit_price, t.exit_reason) == (at(32), 101.0, "EXIT")
    assert t.pnl == pytest.approx(-90.0)  # (101 - 110) x 10 BTC
    eq = {p.ts: p.equity for p in res.equity}
    assert eq[at(20, 23)] == 10_000.0  # the signal bar: not filled yet
    assert eq[at(31, 23)] == pytest.approx(9_900.0)  # marked at 100: -10 x 10 BTC
    assert res.equity[-1].equity == pytest.approx(9_910.0)
    check_consistent(res)


def test_a_short_mirrors_the_long():
    # Short at 90, stop 110. Day 31 closes at 100, above the 10-day high of 95 and below the 20-day high of 105: exit only.
    df = frame(*short_setup(), *[quiet(i, 90.0) for i in range(21, 31)], day(31, 90.0, 100.0, 90.0, 100.0), quiet(32, 99.0))
    res = simulate(df, 20, FREE)
    (t,) = res.trades
    assert (t.entry_ts, t.direction, t.contracts, t.entry_price, t.stop) == (at(21), -1, 1000, 90.0, 110.0)
    assert (t.exit_ts, t.exit_price, t.exit_reason) == (at(32), 99.0, "EXIT")
    assert t.pnl == pytest.approx(-90.0)  # (90 - 99) x 10 BTC


def test_a_flip_exits_and_enters_on_the_same_open():
    res = simulate(flip_path(), 20, FREE)
    assert res.entry_ts == (at(21), at(26))
    first, second = res.trades
    assert (first.exit_ts, first.exit_price, first.exit_reason) == (at(26), 92.0, "FLIP")
    assert first.pnl == pytest.approx(-180.0)  # (92 - 110) x 10 BTC; cash 9,820
    # Day 25's true range: 115 - 90.5 = 24.5, so ATR = (10 x 19 + 24.5) / 20 = 10.725 and the stop is 92 + 21.45 = 113.45.
    # 2% of 9,820 = 196.4; 196.4 / 21.45 = 9.156 BTC = 915 contracts.
    assert (second.entry_ts, second.direction, second.contracts, second.entry_price) == (at(26), -1, 915, 92.0)
    assert second.stop == pytest.approx(113.45)
    assert (second.exit_ts, second.exit_price, second.exit_reason) == (at(26, 23), 92.0, "END")
    assert res.equity[-1].equity == pytest.approx(9_820.0)
    check_consistent(res)


def test_a_signal_bar_s_own_high_does_not_hide_its_breakout():
    assert simulate(frame(*long_setup(), quiet(21, 110.0)), 20, FREE).entry_ts == (at(21),)  # high 110 > close 109 on day 20


def test_the_stop_distance_uses_the_atr_of_the_signal_day():
    # Day 20's true range is 30: ATR = (10 x 19 + 30) / 20 = 11, stop = 110 - 22 = 88; 200 / 22 = 9.09 BTC = 909 contracts.
    res = simulate(frame(*base(), day(20, 100.0, 130.0, 100.0, 110.0), quiet(21, 110.0)), 20, FREE)
    (t,) = res.trades
    assert (t.entry_price, t.stop, t.contracts, t.exit_reason) == (110.0, 88.0, 909, "END")


# ----------------------------------------------------------------------------- no signal


def test_no_entry_channel_no_entry():
    # Day 19 closes at 109 but only 19 days precede it; day 20's channel then holds day 19's high of 110, so 109 is no break.
    days = [quiet(i, 100.0) for i in range(19)]
    res = simulate(frame(*days, day(19, 100.0, 110.0, 100.0, 109.0), quiet(20, 109.0), quiet(21, 109.0)), 20, FREE)
    assert res.entry_ts == ()


def test_an_intraday_break_is_no_signal():
    res = simulate(frame(*base(), day(20, 100.0, 120.0, 80.0, 100.0), quiet(21, 100.0)), 20, FREE)
    assert res.entry_ts == ()


def test_an_incomplete_day_gives_no_signal():
    last = with_hour(day(20, 100.0, 110.0, 100.0, 109.0), 22, 100.0, 110.0, 100.0, 109.0)[:23]
    assert simulate(frame(*base(), last), 20, FREE).entry_ts == ()


def test_a_signal_on_the_last_bar_never_fills():
    res = simulate(frame(*long_setup()), 20, FREE)
    assert res.entry_ts == () and res.trades == ()
    assert {p.equity for p in res.equity} == {10_000.0}


def test_a_filled_last_hour_counts_in_the_day():
    # Day 20's 23:00 bar is missing: fill_gaps repeats hour 22's close, 109, which is the day's close and breaks 105.
    last = with_hour(day(20, 100.0, 110.0, 100.0, 109.0), 22, 100.0, 110.0, 100.0, 109.0)[:23]
    res = simulate(frame(*base(), last, quiet(21, 110.0)), 20, FREE)
    assert res.entry_ts == (at(21),)
    assert [p.ts for p in res.equity] == [T0 + k * H for k in range(22 * 24)]  # one point per hour, the filled one too


def test_a_size_of_zero_fills_no_entry():
    # 0.000001 x 10,000 = 0.01 of risk over a stop 20 away is far below one contract.
    res = simulate(frame(*long_setup(), quiet(21, 110.0)), 20, FREE, RiskProfile("tiny", 0.000001, 3.0))
    assert res.entry_ts == () and res.trades == ()


def test_the_leverage_cap_holds():
    # Quiet days with a range of 0.2 and a day 20 with a range of 1: ATR = (0.2 x 19 + 1) / 20 = 0.24, stop = 110 - 0.48.
    # The risk alone asks for 200 / 0.48 = 416 BTC. The cap is 10,000 x 3 / 110 = 272.72 BTC = 27,272 contracts.
    days = [day(i, 100.0, 100.1, 99.9, 100.0) for i in range(20)]
    res = simulate(frame(*days, day(20, 100.0, 101.0, 100.0, 100.9), day(21, 110.0, 110.0, 110.0, 110.0)), 20, FREE)
    (t,) = res.trades
    assert t.contracts == 27_272 and t.stop == pytest.approx(109.52)


def test_n_below_the_atr_length_is_refused():
    with pytest.raises(ValueError, match="at least 20"):
        simulate(frame(*long_setup()), 19, FREE)


# ----------------------------------------------------------------------------- the stop


def test_the_stop_never_moves():
    # Long at 110, stop 90. The price runs to 150 and ATR grows; a low of 92 does nothing, a low of 89 fills at 90.
    df = frame(
        *long_setup(),
        quiet(21, 110.0),
        day(22, 110.0, 150.0, 110.0, 150.0),
        quiet(23, 150.0),
        day(24, 150.0, 150.0, 92.0, 150.0),
        day(25, 150.0, 150.0, 89.0, 150.0),
    )
    res = simulate(df, 20, FREE)
    assert res.entry_ts == (at(21),)  # and no second entry during the run
    (t,) = res.trades
    assert (t.stop, t.exit_price, t.exit_ts, t.exit_reason) == (90.0, 90.0, at(25, 12), "STOP")
    assert t.pnl == pytest.approx(-200.0)


def test_a_short_stop_fills_on_the_high():
    # Short at 90, stop 110: a high of 111 in hour 11 of day 22.
    res = simulate(frame(*short_setup(), quiet(21, 90.0), day(22, 90.0, 111.0, 90.0, 90.0)), 20, FREE)
    (t,) = res.trades
    assert (t.exit_price, t.exit_ts, t.exit_reason) == (110.0, at(22, 11), "STOP")
    assert t.pnl == pytest.approx(-200.0)


def test_an_open_beyond_the_stop_fills_at_the_open_with_slippage():
    # With A's costs: fill 110 x 1.0005 = 110.055, stop 90.055. size_contracts: 110 - 90.055 + 0.0005 x (110 + 90.055)
    # = 20.0450275, 200 / 20.0450275 = 9.9775 BTC = 997 contracts. Hour 5 of day 22 opens at 80: 80 x 0.9995 = 79.96.
    gap = with_hour(quiet(22, 110.0), 5, 80.0, 80.0, 80.0, 80.0)
    res = simulate(frame(*long_setup(), quiet(21, 110.0), gap), 20, CostParams())
    t = res.trades[0]
    assert (t.contracts, t.exit_ts, t.exit_reason) == (997, at(22, 5), "STOP")
    assert t.stop == pytest.approx(90.055) and t.exit_price == pytest.approx(79.96)


def test_the_stop_wins_when_it_shares_a_bar_with_the_signal():
    # Day 25's last hour: low 89 (stop 90) and close 91, below both channels (95). The stop fills at 90 inside that hour, not
    # at the next open as a flip, and the close then enters short from flat. True range 115 - 89 = 26: ATR = 10.8, stop 92 + 21.6.
    # Cash 9,800; 196 / 21.6 = 9.07 BTC = 907 contracts.
    df = frame(*long_setup(), *[quiet(i, 110.0) for i in range(21, 25)],
               day(25, 110.0, 115.0, 89.0, 91.0, low_hour=23), quiet(26, 92.0))
    res = simulate(df, 20, FREE)
    first, second = res.trades
    assert (first.exit_price, first.exit_ts, first.exit_reason) == (90.0, at(25, 23), "STOP")
    assert (second.entry_ts, second.direction, second.entry_price, second.contracts) == (at(26), -1, 92.0, 907)
    assert second.stop == pytest.approx(113.6)
    check_consistent(res)


# ----------------------------------------------------------------------------- costs


def test_funding_is_an_hourly_amount_on_the_close():
    # 10 BTC long. Hour 0 closes at 110, hour 1 at 120, the other 22 at 110. Long funding: notional x 0.10 / 8760 an hour.
    day21 = with_hour(day(21, 110.0, 110.0, 110.0, 110.0), 1, 110.0, 120.0, 110.0, 120.0)
    res = simulate(frame(*long_setup(), day21), 20, FUNDING_ONLY)
    hour = {p.ts: p.equity for p in res.equity}
    assert hour[at(21, 0)] == pytest.approx(10_000.0 - 1100.0 * 0.10 / 8760)
    assert hour[at(21, 1)] == pytest.approx(10_000.0 + 100.0 - (1100.0 + 1200.0) * 0.10 / 8760)
    total = (23 * 1100.0 + 1200.0) * 0.10 / 8760
    (t,) = res.trades
    assert t.funding == pytest.approx(total) and t.pnl == pytest.approx(-total)
    assert res.equity[-1].equity == pytest.approx(10_000.0 - total)


def test_a_short_receives_funding():
    res = simulate(frame(*short_setup(), day(21, 90.0, 90.0, 90.0, 90.0)), 20, FUNDING_ONLY)
    (t,) = res.trades
    assert t.funding == pytest.approx(-24 * 900.0 * 0.10 / 8760)
    assert res.equity[-1].equity == pytest.approx(10_000.0 + 24 * 900.0 * 0.10 / 8760)


def test_a_whole_trade_with_a_s_costs():
    # Fill 110.055, stop 90.055, 997 contracts = 9.97 BTC. Exit at 101 x 0.9995 = 100.9495. Funding on 264 closes, days 21-31:
    # 263 at 110 and the last at 100. No funding in the hour of the exit.
    df = frame(*long_setup(), *[quiet(i, 110.0) for i in range(21, 31)], day(31, 110.0, 110.0, 100.0, 100.0), quiet(32, 101.0))
    res = simulate(df, 20, CostParams())
    (t,) = res.trades
    fee_in = 0.0005 * 110.055 * 9.97
    fee_out = 0.0005 * 100.9495 * 9.97
    funding = (263 * 110.0 + 100.0) * 9.97 * 0.10 / 8760
    pnl = (100.9495 - 110.055) * 9.97 - fee_in - fee_out - funding
    assert t.contracts == 997 and t.exit_ts == at(32)
    assert t.entry_price == pytest.approx(110.055) and t.exit_price == pytest.approx(100.9495)
    assert t.fees == pytest.approx(fee_in + fee_out) and t.funding == pytest.approx(funding)
    assert t.pnl == pytest.approx(pnl)
    assert res.equity[-1].equity == pytest.approx(10_000.0 + pnl)


def test_equity_has_one_point_per_bar():
    df = flip_path()
    res = simulate(df, 20, FREE)
    assert [p.ts for p in res.equity] == df["ts"].tolist()


# ----------------------------------------------------------------------------- the Friday calendar

FRIDAY_CLOSED = at(26, 22)  # Fri 2017-01-27 22:00 UTC = 17:00 in New York (EST)


def test_a_stop_crossed_in_the_halt_fills_at_the_next_tradable_open():
    assert futures_closed(FRIDAY_CLOSED) and not futures_closed(FRIDAY_CLOSED + H)
    friday = with_hour(with_hour(quiet(26, 110.0), 22, 110.0, 110.0, 85.0, 110.0), 23, 95.0, 95.0, 95.0, 95.0)
    res = simulate(frame(*long_setup(), *[quiet(i, 110.0) for i in range(21, 26)], friday), 20, FREE)
    (t,) = res.trades
    # The open, 95, is above the stop of 90, but the stop was crossed during the halt: it fills there, not at 90.
    assert (t.exit_ts, t.exit_price, t.exit_reason) == (FRIDAY_CLOSED + H, 95.0, "STOP")


def test_funding_runs_through_the_halt():
    df = frame(*long_setup(), *[day(i, 110.0, 110.0, 110.0, 110.0) for i in range(21, 27)])
    hour = {p.ts: p.equity for p in simulate(df, 20, FUNDING_ONLY).equity}
    assert hour[FRIDAY_CLOSED] - hour[FRIDAY_CLOSED - H] == pytest.approx(-1100.0 * 0.10 / 8760)


def test_the_real_calendar_never_touches_a_daily_signal_or_its_fill():
    """Fri 16:00-18:00 New York ends at 23:00 UTC at the latest, so the 23:00 UTC signal bar and the 00:00 UTC fill bar are
    always clear: the two tests below need a faked calendar. On real data the calendar only affects stops."""
    assert entry_blackout(FRIDAY_CLOSED)  # non-vacuity
    for midnight in range(START_TS, END_TS + 1, CONFIG_DAY):
        assert not entry_blackout(midnight) and not entry_blackout(midnight - H)


def test_an_entry_waits_for_the_next_open_after_a_halt(monkeypatch):
    halted = at(21)
    monkeypatch.setattr(engine, "futures_closed", lambda ts: ts == halted)
    monkeypatch.setattr(engine, "entry_blackout", lambda ts: ts == halted)
    day21 = with_hour(quiet(21, 110.0), 1, 111.0, 111.0, 111.0, 111.0)
    (t,) = simulate(frame(*long_setup(), day21), 20, FREE).trades
    assert (t.entry_ts, t.entry_price, t.stop) == (at(21, 1), 111.0, 91.0)


def test_a_blackout_delays_the_entry_of_a_flip_but_not_its_exit(monkeypatch):
    blackout = at(26)
    monkeypatch.setattr(engine, "entry_blackout", lambda ts: ts == blackout)
    first, second = simulate(flip_path(), 20, FREE).trades
    assert (first.exit_ts, first.exit_reason) == (blackout, "FLIP")
    assert second.entry_ts == blackout + H


def test_a_halt_delays_both_sides_of_a_flip(monkeypatch):
    halted = at(26)
    monkeypatch.setattr(engine, "futures_closed", lambda ts: ts == halted)
    monkeypatch.setattr(engine, "entry_blackout", lambda ts: ts == halted)
    first, second = simulate(flip_path(), 20, FREE).trades
    assert first.exit_ts == second.entry_ts == halted + H
```

- [ ] **Step 2: 실패를 확인한다.**

```bash
uv run --group live pytest tests/strate/test_engine.py -q
```

Expected: 수집 오류 `ImportError: cannot import name 'engine' from 'ewbot.strate'`.

- [ ] **Step 3: 구현한다.**

`src/ewbot/strate/engine.py` (전체 내용):

```python
"""Strategy E's 1h simulation (Plan 7 spec 2): one position at a time, decided at the UTC daily close, filled on 1h opens.

Order inside one 1h bar, the same as A's PaperBroker:
  0. Friday halt (futures_closed): nothing fills; a stop crossed during the halt fills at the next tradable open.
  1. Pending exit at the open, then the pending entry at the same open. An entry also waits out entry_blackout. The entry
     is sized here: its stop is 2 x ATR from the fill price and never moves.
  2. Gap: the open is at or beyond the stop (or the stop was crossed during the halt) -> fill at the open.
  3. Intrabar: the stop is touched -> fill at the stop. Prices slip against the position in both cases.
  4. Funding for the hour on the position still open at the close (mark = close), during the halt too.
  5. On a UTC day's last hour, unless it is the last bar: the daily decision, after the stop, so the stop wins on one bar.
  6. Last bar: any position closes at the close; a decision there could not fill.
  7. One equity point per bar, marked at the close.

There are no loss limits here: stage 1 and the walk-forward run without them (spec 2, 5.1). ``trades`` and ``equity`` are for
the tests and stage 2; the entry count reads ``entry_ts`` only."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ewbot.config import CostParams, RiskProfile
from ewbot.data.candles import entry_blackout, fill_gaps, futures_closed, to_bars
from ewbot.execution.costs import fee, funding_per_hour, slip
from ewbot.risk.sizing import size_contracts
from ewbot.strate.config import ATR_DAYS, ATR_STOP_MULT, COSTS, INITIAL_EQUITY, RISK
from ewbot.strate.rules import DayLevels, day_levels, decide
from ewbot.types import Bar


@dataclass(frozen=True, slots=True)
class EquityPoint:
    ts: int
    equity: float


@dataclass(frozen=True, slots=True)
class Trade:
    entry_ts: int
    exit_ts: int
    direction: int
    contracts: int
    entry_price: float
    exit_price: float
    stop: float
    pnl: float  # after fees and funding
    fees: float
    funding: float  # >0 = paid
    exit_reason: str  # "STOP" | "EXIT" | "FLIP" | "END"


@dataclass(frozen=True, slots=True)
class SimResult:
    entry_ts: tuple[int, ...]  # every filled entry; a size of 0 fills nothing
    trades: tuple[Trade, ...]
    equity: tuple[EquityPoint, ...]  # one per 1h bar


@dataclass(slots=True)
class _Position:
    direction: int
    contracts: int
    entry_ts: int
    entry_price: float
    stop: float
    fees: float
    funding: float = 0.0
    stop_crossed_in_halt: bool = False


class _Simulation:
    def __init__(self, levels: dict[int, DayLevels], costs: CostParams, risk: RiskProfile) -> None:
        self.levels = levels
        self.costs = costs
        self.risk = risk
        self.cash = INITIAL_EQUITY
        self.pos: _Position | None = None
        self.exit_reason: str | None = None  # a pending exit at the next open
        self.entry_order: tuple[int, float] | None = None  # (side, ATR of the signal day): a pending entry
        self.entry_ts: list[int] = []
        self.trades: list[Trade] = []
        self.equity: list[EquityPoint] = []

    def run(self, bars: list[Bar]) -> SimResult:
        last = len(bars) - 1
        for i, bar in enumerate(bars):
            self._fills(bar)
            self._funding(bar)
            if i == last:
                if self.pos is not None:
                    self._close(bar.ts, slip(bar.close, -self.pos.direction, self.costs), "END")
            else:
                self._decide(bar)
            self.equity.append(EquityPoint(bar.ts, self._mark(bar.close)))
        return SimResult(tuple(self.entry_ts), tuple(self.trades), tuple(self.equity))

    def _fills(self, bar: Bar) -> None:
        if futures_closed(bar.ts):
            if self.pos is not None and self._touched(self.pos, bar):
                self.pos.stop_crossed_in_halt = True
            return
        if self.exit_reason is not None:
            reason, self.exit_reason = self.exit_reason, None
            if self.pos is not None:
                self._close(bar.ts, slip(bar.open, -self.pos.direction, self.costs), reason)
        if self.entry_order is not None and self.pos is None and not entry_blackout(bar.ts):
            self._open(bar)
        pos = self.pos
        if pos is None:
            return
        d = pos.direction
        if pos.stop_crossed_in_halt or d * (bar.open - pos.stop) <= 0:
            self._close(bar.ts, slip(bar.open, -d, self.costs), "STOP")
        elif self._touched(pos, bar):
            self._close(bar.ts, slip(pos.stop, -d, self.costs), "STOP")

    @staticmethod
    def _touched(pos: _Position, bar: Bar) -> bool:
        return bar.low <= pos.stop if pos.direction == 1 else bar.high >= pos.stop

    def _open(self, bar: Bar) -> None:
        assert self.entry_order is not None
        d, atr = self.entry_order
        self.entry_order = None
        price = slip(bar.open, d, self.costs)
        stop = price - d * ATR_STOP_MULT * atr
        # flat here, so cash is the equity; size_contracts adds the slippage of both fills to bar.open - stop
        contracts = size_contracts(self.cash, bar.open, stop, d, self.risk, 1.0, self.costs)
        if contracts == 0:
            return
        f = fee(price, contracts, self.costs)
        self.cash -= f
        self.pos = _Position(d, contracts, bar.ts, price, stop, f)
        self.entry_ts.append(bar.ts)

    def _close(self, ts: int, price: float, reason: str) -> None:
        pos = self.pos
        assert pos is not None
        realized = pos.direction * (price - pos.entry_price) * pos.contracts * self.costs.contract_size
        f = fee(price, pos.contracts, self.costs)
        self.cash += realized - f
        fees = pos.fees + f
        self.trades.append(
            Trade(pos.entry_ts, ts, pos.direction, pos.contracts, pos.entry_price, price, pos.stop,
                  realized - fees - pos.funding, fees, pos.funding, reason)
        )
        self.pos = None

    def _funding(self, bar: Bar) -> None:
        if self.pos is not None:
            cost = funding_per_hour(bar.close, self.pos.contracts, self.pos.direction, self.costs)
            self.cash -= cost
            self.pos.funding += cost

    def _decide(self, bar: Bar) -> None:
        lv = self.levels.get(bar.ts)
        if lv is None or self.exit_reason is not None or self.entry_order is not None:
            return
        leave, side = decide(0 if self.pos is None else self.pos.direction, lv)
        if leave:
            self.exit_reason = "FLIP" if side else "EXIT"
        if side:
            self.entry_order = (side, lv.atr)

    def _mark(self, close: float) -> float:
        pos = self.pos
        if pos is None:
            return self.cash
        return self.cash + pos.direction * (close - pos.entry_price) * pos.contracts * self.costs.contract_size


def simulate(df1h: pd.DataFrame, n: int, costs: CostParams = COSTS, risk: RiskProfile = RISK) -> SimResult:
    """One run of candidate ``n`` over all of ``df1h``. Holes become flat synthetic bars (A's fill_gaps), in the days too."""
    if n < ATR_DAYS:
        raise ValueError(f"n must be at least {ATR_DAYS}, so that ATR exists when the channel does, got {n}")
    df = fill_gaps(df1h)
    return _Simulation(day_levels(df, n), costs, risk).run(to_bars(df))
```

- [ ] **Step 4: 통과를 확인한다.**

```bash
uv run --group live pytest tests/strate/test_engine.py -q
```

Expected: `27 passed`.

- [ ] **Step 5: 커밋한다.**

```bash
git add src/ewbot/strate/engine.py tests/strate/test_engine.py
git commit -m "feat(strate): 1h engine: next-open fills, fixed stop from the fill price, stop before signal, flips, A's costs, hourly equity" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 3: 데이터 가드와 횟수 점검

**Files:**
- Create: `src/ewbot/strate/data.py`, `src/ewbot/strate/count.py`, `tests/strate/test_data.py`, `tests/strate/test_count.py`

**Interfaces:**
- Consumes:
  - Task 1: `strate.config.CANDIDATES`, `START_TS`, `END_TS`, `MONTHS`, `exit_days`; Task 2: `strate.engine.simulate`, `SimResult.entry_ts`
  - `pandas.read_parquet(path, columns=…, filters=…)`
  - 테스트만: `ewbot.backtest.runner.utc_ts`
- Produces:
  - `strate.data`: `PRODUCT`, `EXPECTED_HOURS`, `LAST_TS`, `MAX_MISSING_HOURS`, `DataWindowError(ValueError)`, `Coverage(first_ts: int | None, last_ts: int | None, rows: int, missing_hours: int)`, `cache_file(cache_dir: Path) -> Path`, `read_ts(path: Path) -> np.ndarray`, `coverage(ts: np.ndarray) -> Coverage`, `check_coverage(ts: np.ndarray) -> None`, `refuse_2024(df1h: pd.DataFrame) -> None`, `load_btc_1h(cache_dir: Path) -> pd.DataFrame`
  - `strate.count`: `GATE_MEDIAN_ENTRIES`, `REPORT_KIND`, `PROCEED`, `STOP`, `ByYear = dict[int, int]`, `tally_entries(entry_ts: Iterable[int]) -> ByYear`, `run_count(df1h: pd.DataFrame, workers: int = 1) -> dict[int, ByYear]`, `decide(totals: Mapping[int, int]) -> dict[str, Any]`, `build_report(results: Mapping[int, ByYear], df1h: pd.DataFrame) -> dict[str, Any]`, `report_lines(rep: Mapping[str, Any]) -> list[str]`

- [ ] **Step 1: 실패하는 테스트를 쓴다.**

`tests/strate/test_data.py` (전체 내용):

```python
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ewbot.strate import data
from ewbot.strate.config import END_TS, START_TS
from ewbot.strate.data import (
    EXPECTED_HOURS,
    LAST_TS,
    MAX_MISSING_HOURS,
    DataWindowError,
    cache_file,
    check_coverage,
    coverage,
    load_btc_1h,
    refuse_2024,
)
from ewbot.types import H1

WINDOW = np.arange(START_TS, END_TS, H1, dtype=np.int64)


def frame(ts: np.ndarray, price: float = 1.0) -> pd.DataFrame:
    prices = {c: price for c in ("open", "high", "low", "close", "volume")}
    return pd.DataFrame({"ts": pd.Series(ts, dtype="int64"), **prices})


def holes(k: int) -> np.ndarray:
    """The window without k hours from its middle."""
    return np.delete(WINDOW, np.arange(1000, 1000 + k))


def write(tmp_path: Path, df: pd.DataFrame) -> Path:
    df.to_parquet(cache_file(tmp_path), index=False)
    return tmp_path


def test_the_cache_file_is_a_s_btc_1h_cache(tmp_path):
    assert cache_file(tmp_path) == tmp_path / "BTC-USD_1h.parquet"


def test_the_window_and_the_limit():
    assert EXPECTED_HOURS == 2_556 * 24 == 61_344 == WINDOW.size
    assert LAST_TS == WINDOW[-1]
    assert MAX_MISSING_HOURS == 300


# ----------------------------------------------------------------------------- coverage, from ts alone


def test_missing_hours_are_measured_from_the_distinct_ts_in_the_window():
    assert coverage(WINDOW).missing_hours == 0
    assert coverage(holes(3)).missing_hours == 3
    assert coverage(np.concatenate([holes(3), WINDOW[:5]])).missing_hours == 3  # duplicates hide no hole
    cov = coverage(holes(3))
    assert (cov.first_ts, cov.last_ts, cov.rows) == (START_TS, LAST_TS, EXPECTED_HOURS - 3)
    assert coverage(np.array([], dtype=np.int64)).missing_hours == EXPECTED_HOURS


def test_the_limit_of_missing_hours_is_inclusive():
    check_coverage(holes(MAX_MISSING_HOURS))
    with pytest.raises(DataWindowError, match="misses 301 of 61344 hours"):
        check_coverage(holes(MAX_MISSING_HOURS + 1))


@pytest.mark.parametrize("ts", [WINDOW[1:], WINDOW[:-1], np.array([], dtype=np.int64)], ids=["first", "last", "empty"])
def test_a_cache_that_does_not_cover_the_window_is_refused(ts):
    with pytest.raises(DataWindowError, match="2017-01-01"):
        check_coverage(ts)


# ----------------------------------------------------------------------------- the 2024 guard


def test_a_frame_with_a_2024_bar_is_refused():
    refuse_2024(frame(WINDOW[-2:]))
    refuse_2024(frame(np.array([], dtype=np.int64)))
    with pytest.raises(DataWindowError, match="2024"):
        refuse_2024(frame(np.array([LAST_TS, END_TS])))


def test_a_parquet_with_2024_bars_and_bars_before_2017_gives_only_the_window(tmp_path):
    outside = np.array([START_TS - 2 * H1, START_TS - H1, END_TS, END_TS + H1, END_TS + 2 * H1])
    write(tmp_path, pd.concat([frame(outside, 1e9), frame(WINDOW)]))
    raw = pd.read_parquet(cache_file(tmp_path), columns=["ts"])["ts"]
    assert raw.max() == END_TS + 2 * H1 and raw.min() == START_TS - 2 * H1  # non-vacuity: they are in the file
    df = load_btc_1h(tmp_path)
    assert df["ts"].tolist() == WINDOW.tolist()
    assert df["close"].max() == 1.0


def test_every_read_is_filtered_by_the_parquet_reader_and_the_check_reads_ts_alone_first(tmp_path, monkeypatch):
    write(tmp_path, frame(WINDOW))
    calls = []
    real = pd.read_parquet

    def spy(path, **kw):
        calls.append(kw)
        return real(path, **kw)

    monkeypatch.setattr(data.pd, "read_parquet", spy)
    load_btc_1h(tmp_path)
    assert [c["columns"] for c in calls] == [["ts"], ["ts", "open", "high", "low", "close", "volume"]]
    for c in calls:
        assert ("ts", "<", END_TS) in c["filters"] and ("ts", ">=", START_TS) in c["filters"]
        assert c["engine"] == "pyarrow"


def test_a_refused_cache_has_no_price_read(tmp_path, monkeypatch):
    write(tmp_path, frame(holes(MAX_MISSING_HOURS + 1)))
    calls = []
    real = pd.read_parquet

    def spy(path, **kw):
        calls.append(kw["columns"])
        return real(path, **kw)

    monkeypatch.setattr(data.pd, "read_parquet", spy)
    with pytest.raises(DataWindowError, match="misses 301"):
        load_btc_1h(tmp_path)
    assert calls == [["ts"]]


def test_the_unlock_keyword_is_refused_before_the_lock(tmp_path):
    write(tmp_path, frame(WINDOW))
    with pytest.raises(TypeError, match="unlock_holdout_e"):
        load_btc_1h(tmp_path, unlock_holdout_e=True)


def test_reading_leaves_the_cache_file_as_it_was(tmp_path):
    write(tmp_path, frame(WINDOW))
    path = cache_file(tmp_path)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    load_btc_1h(tmp_path)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert [p.name for p in tmp_path.iterdir()] == [path.name]
```

`tests/strate/test_count.py` (전체 내용):

```python
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from ewbot.backtest.runner import utc_ts
from ewbot.strate import count
from ewbot.strate.config import CANDIDATES, END_TS, START_TS
from ewbot.strate.data import LAST_TS, DataWindowError
from tests.strate.helpers import DAY, T0, flip_path

FRAME = pd.DataFrame({"ts": pd.Series([START_TS, LAST_TS], dtype="int64")})


def totals(a: int, b: int, c: int, d: int) -> dict[int, int]:
    return dict(zip(CANDIDATES, (a, b, c, d)))


def results(n: int) -> dict[int, dict[int, int]]:
    return {c: {2019: n} for c in CANDIDATES}


# ----------------------------------------------------------------------------- counting


def test_the_fixed_values():
    assert count.GATE_MEDIAN_ENTRIES == 84
    assert len(CANDIDATES) == 4


def test_tally_groups_by_the_utc_year_of_the_entry():
    boundary = utc_ts("2018-01-01")
    assert count.tally_entries([boundary - 3600, boundary, boundary + 3600]) == {2017: 1, 2018: 2}
    assert count.tally_entries([]) == {}


class OnlyEntries:
    """A run result that fails if anything but the entry times is read."""

    entry_ts = (utc_ts("2019-03-01"), utc_ts("2019-04-01"), utc_ts("2021-05-01"))

    @property
    def equity(self) -> Any:
        raise AssertionError("the count read the equity curve")

    @property
    def trades(self) -> Any:
        raise AssertionError("the count read a trade")


def test_the_worker_returns_counts_by_year_and_reads_entry_times_only(monkeypatch):
    seen = {}

    def fake_simulate(df1h, n):
        seen.update(df=df1h, n=n)
        return OnlyEntries()

    monkeypatch.setattr(count, "simulate", fake_simulate)
    marker = object()
    count._setup_worker(marker)
    assert count._count_candidate(55) == (55, {2019: 2, 2021: 1})
    assert seen == {"df": marker, "n": 55}


def test_counts_of_a_hand_computed_path():
    """flip_path has two entries for N = 20 (test_engine has the prices) and is too short for the other candidates."""
    assert count.run_count(flip_path()) == {20: {2017: 2}, 40: {}, 55: {}, 100: {}}


def test_two_worker_processes_match_one_process():
    assert count.run_count(flip_path(), workers=2) == count.run_count(flip_path(), workers=1) == {
        20: {2017: 2}, 40: {}, 55: {}, 100: {}}


def test_a_frame_reaching_2024_is_refused_before_any_run(monkeypatch):
    def boom(*a: Any, **kw: Any) -> None:
        raise AssertionError("a run started")

    monkeypatch.setattr(count, "simulate", boom)
    with pytest.raises(DataWindowError, match="2024"):
        count.run_count(pd.DataFrame({"ts": [END_TS]}))


# ----------------------------------------------------------------------------- the gate


def test_a_median_of_exactly_84_entries_proceeds():
    out = count.decide(totals(84, 84, 84, 84))
    assert out["verdict"] == count.PROCEED and out["passed"] is True
    assert out["median_entries"] == 84 and out["median_monthly"] == 1.0
    assert out["threshold_entries"] == 84 and out["threshold_monthly"] == 1.0


def test_the_median_of_four_is_the_mean_of_the_middle_two():
    assert count.decide(totals(0, 83, 85, 500))["median_entries"] == 84.0
    assert count.decide(totals(0, 83, 85, 500))["verdict"] == count.PROCEED
    assert count.decide(totals(500, 85, 83, 0))["verdict"] == count.PROCEED  # the order of the candidates does not matter
    assert count.decide(totals(0, 83, 84, 500))["median_entries"] == 83.5
    assert count.decide(totals(0, 83, 84, 500))["verdict"] == count.STOP


def test_one_large_candidate_does_not_carry_the_gate():
    assert count.decide(totals(10, 10, 10, 10_000))["verdict"] == count.STOP


def test_the_gate_takes_the_four_candidates_only():
    with pytest.raises(ValueError, match="candidates"):
        count.decide({20: 500})
    with pytest.raises(ValueError, match="candidates"):
        count.decide({**totals(1, 1, 1, 1), 30: 1})


# ----------------------------------------------------------------------------- the report


def test_the_report_has_per_candidate_counts_the_median_the_verdict_and_the_measured_hours():
    res = results(84)
    res[55] = {2023: 2, 2017: 100}
    rep = count.build_report(res, FRAME)
    row = next(r for r in rep["candidates"] if r["n"] == 55)
    assert row == {"n": 55, "exit_days": 27, "entries": 102, "monthly": 102 / 84, "by_year": {"2017": 100, "2023": 2}}
    assert list(row["by_year"]) == ["2017", "2023"]
    assert [r["n"] for r in rep["candidates"]] == [20, 40, 55, 100]
    assert rep["kind"] == count.REPORT_KIND
    assert rep["window"] == {"start": "2017-01-01", "end": "2024-01-01", "months": 84}
    assert rep["data"] == {"first_ts": START_TS, "last_ts": LAST_TS, "rows": 2, "missing_hours": 61_342,
                           "max_missing_hours": 300}
    assert rep["median_entries"] == 84 and rep["verdict"] == count.PROCEED
    json.dumps(rep)


def test_the_printed_lines_show_each_candidate_the_median_and_the_verdict():
    lines = count.report_lines(count.build_report(results(84), FRAME))
    assert "cache: 2 hours, 61342 missing (limit 300)" in lines
    assert "N=20 exit=10: 84 entries, 1.00 per month" in lines
    assert "N=100 exit=50: 84 entries, 1.00 per month" in lines
    assert sum(line.startswith("N=") for line in lines) == 4
    assert "median of 4 candidates = 84.0 entries = 1.000 per month (needs >= 84 entries)" in lines
    assert "verdict: PROCEED" in lines


def test_a_failed_gate_says_strategy_e_ends():
    lines = count.report_lines(count.build_report(results(83), FRAME))
    assert "verdict: STOP" in lines and any("strategy E ends here" in line for line in lines)


# ----------------------------------------------------------------------------- no performance value (spec 5.1, 6)

ALLOWED_KEYS = {"kind", "window", "start", "end", "months", "data", "first_ts", "last_ts", "rows", "missing_hours",
                "max_missing_hours", "candidates", "n", "exit_days", "entries", "monthly", "by_year", "threshold_entries",
                "threshold_monthly", "median_entries", "median_monthly", "passed", "verdict"}
PERFORMANCE_WORDS = re.compile(
    r"\b(pnl|profit|loss|returns?|sharpe|sortino|calmar|drawdown|max_dd|cagr|win_rate|avg_r|r_multiple|equity|exposure|"
    r"exit_reason|funding|fees?)\b", re.I)
PERFORMANCE_MODULES = {"metrics", "report", "benchmarks", "holdout", "walkforward"}
PERFORMANCE_NAMES = {"compute_metrics", "evaluate_criteria", "trade_stats", "bootstrap_ci", "breakdowns", "buy_hold", "folds",
                     "walk_forward"}
RESULT_ATTRS = {"trades", "equity", "pnl", "fees", "funding", "exit_reason", "exit_ts", "exit_price", "entry_price", "contracts",
                "direction", "stop", "cash"}


def _performance_refs(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[-1] in PERFORMANCE_MODULES]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[-1] in PERFORMANCE_MODULES:
                found.append(str(node.module))
            found += [a.name for a in node.names if a.name in PERFORMANCE_MODULES | PERFORMANCE_NAMES]
        elif isinstance(node, ast.Attribute) and node.attr in RESULT_ATTRS | PERFORMANCE_NAMES:
            found.append(node.attr)
        elif isinstance(node, ast.Name) and node.id in PERFORMANCE_NAMES:
            found.append(node.id)
    return found


@pytest.mark.parametrize(
    "src",
    [
        "import ewbot.backtest.metrics",
        "from ewbot.backtest.metrics import compute_metrics",
        "from ewbot.backtest import metrics",
        "compute_metrics(a, b)",
        "t.pnl",
        "res.equity",
        "res.trades",
    ],
)
def test_the_performance_guard_catches(src):
    assert _performance_refs(ast.parse(src))


def test_count_py_refers_to_no_performance_code_and_no_trade_result():
    assert _performance_refs(ast.parse(Path(count.__file__).read_text(encoding="utf-8"))) == []


def test_importing_the_count_loads_no_performance_wave_or_strategy_code():
    code = ("import sys, ewbot.strate.count\n"
            "bad = ('ewbot.backtest', 'ewbot.waves', 'ewbot.strategy', 'ewbot.engine', 'ewbot.paper', 'ewbot.live',\n"
            "       'ewbot.stratb', 'ewbot.paperb', 'ewbot.aeth', 'ewbot.execution.paper')\n"
            "print(sorted(m for m in sys.modules if m.startswith(bad)), 'ewbot.strate.engine' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[] True"  # True: the guard looked at a process that did load the engine


def _keys(o: Any) -> set[str]:
    if isinstance(o, dict):
        return set(o) | {k for v in o.values() for k in _keys(v)}
    if isinstance(o, (list, tuple)):
        return {k for v in o for k in _keys(v)}
    return set()


@pytest.mark.parametrize("n", [84, 83], ids=["proceed", "stop"])
def test_the_report_and_the_printed_lines_hold_no_performance_value(n):
    rep = count.build_report(results(n), FRAME)
    years = {k for k in _keys(rep) if re.fullmatch(r"\d{4}", k)}
    assert years and _keys(rep) - years <= ALLOWED_KEYS
    assert not PERFORMANCE_WORDS.search(json.dumps(rep))
    assert not PERFORMANCE_WORDS.search("\n".join(count.report_lines(rep)))


def test_the_word_guard_catches():
    assert PERFORMANCE_WORDS.search('{"sharpe": 1.0}') and PERFORMANCE_WORDS.search("equity 10000")


def test_a_real_run_s_report_holds_no_performance_value():
    df = flip_path()
    rep = count.build_report(count.run_count(df), df)
    assert rep["candidates"][0]["entries"] == 2  # non-vacuity: a run with entries
    assert not PERFORMANCE_WORDS.search(json.dumps(rep))
    assert df["ts"].iloc[0] == T0 and df["ts"].iloc[-1] == T0 + 27 * DAY - 3600
```

- [ ] **Step 2: 실패를 확인한다.**

```bash
uv run --group live pytest tests/strate/test_data.py tests/strate/test_count.py -q
```

Expected: 수집 오류 2개, `ImportError: cannot import name 'data' from 'ewbot.strate'`와 `ImportError: cannot import name 'count' from 'ewbot.strate'`.

- [ ] **Step 3: 구현한다.**

`src/ewbot/strate/data.py` (전체 내용):

```python
"""Strategy E's bars for stage 1 (Plan 7 spec 4): A's BTC 1h cache, read-only, [2017-01-01, 2024-01-01) only.

Every read carries a parquet filter, so a bar at or after 2024-01-01 never reaches memory, and there is no argument that lifts
it: the key bound to holdout-e.lock belongs to a later plan, written only if the gate passes.

The number of missing hours is measured at run time from the ts column alone: EXPECTED_HOURS minus the distinct ts in the
window. It is checked before any price is read, because a failed gate ends strategy E for good and a damaged cache must not
reach it. MAX_MISSING_HOURS is the limit A-ETH's stage 1 used (docs/aeth.md); it is a limit, not an estimate of the count."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from ewbot.strate.config import END_TS, START_TS
from ewbot.types import H1

PRODUCT = "BTC-USD"
EXPECTED_HOURS = (END_TS - START_TS) // H1  # 61,344: 2,556 days
LAST_TS = END_TS - H1  # 2023-12-31 23:00 UTC
MAX_MISSING_HOURS = 300
_WINDOW = [("ts", ">=", START_TS), ("ts", "<", END_TS)]
_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


class DataWindowError(ValueError):
    """The bars reach 2024, do not cover the count's window, or miss too many hours."""


@dataclass(frozen=True, slots=True)
class Coverage:
    first_ts: int | None
    last_ts: int | None
    rows: int  # distinct hours in the window
    missing_hours: int


def cache_file(cache_dir: Path) -> Path:
    return Path(cache_dir) / f"{PRODUCT}_1h.parquet"


def read_ts(path: Path) -> np.ndarray:
    """The ts column of the window. No price is read."""
    return pd.read_parquet(path, engine="pyarrow", columns=["ts"], filters=_WINDOW)["ts"].to_numpy(dtype=np.int64)


def coverage(ts: np.ndarray) -> Coverage:
    uniq = np.unique(ts[(ts >= START_TS) & (ts < END_TS)])
    first, last = (int(uniq[0]), int(uniq[-1])) if uniq.size else (None, None)
    return Coverage(first, last, int(uniq.size), EXPECTED_HOURS - int(uniq.size))


def check_coverage(ts: np.ndarray) -> None:
    cov = coverage(ts)
    if cov.first_ts != START_TS or cov.last_ts != LAST_TS:
        raise DataWindowError(
            f"the cache must run from 2017-01-01 00:00 to 2023-12-31 23:00 UTC, got ts {cov.first_ts} .. {cov.last_ts}: "
            "the count needs the whole window"
        )
    if cov.missing_hours > MAX_MISSING_HOURS:
        raise DataWindowError(
            f"the cache misses {cov.missing_hours} of {EXPECTED_HOURS} hours, the limit is {MAX_MISSING_HOURS}: "
            "too many flat filled bars for a gate that cannot be repeated"
        )


def refuse_2024(df1h: pd.DataFrame) -> None:
    if len(df1h) and int(df1h["ts"].max()) >= END_TS:
        raise DataWindowError("the bars reach 2024-01-01: strategy E's stage 1 reads nothing from 2024 on")


def load_btc_1h(cache_dir: Path) -> pd.DataFrame:
    """The window's bars, sorted. Refuses a cache that fails check_coverage before reading a price."""
    path = cache_file(cache_dir)
    check_coverage(read_ts(path))
    df = pd.read_parquet(path, engine="pyarrow", columns=_COLUMNS, filters=_WINDOW)
    df = df.astype({"ts": "int64", **{c: "float64" for c in _COLUMNS[1:]}})
    df = df.sort_values("ts", kind="stable").reset_index(drop=True)
    refuse_2024(df)
    return df
```

`src/ewbot/strate/count.py` (전체 내용):

```python
"""Stage 1 of strategy E (Plan 7 spec 5.1): how many entries, and nothing else.

Every candidate runs once through the engine over [2017-01-01, 2024-01-01), with no loss limits: one position at a time, and
stops and flips depend on the path, so counting signals would be wrong. A worker reads the entry times of the run and returns
counts by UTC year. It never reads a trade or the equity curve, and this module refers to no performance code.

The gate: a median over the 4 candidates of at least 84 entries, which is 1.0 per month, proceeds to stage 2. Otherwise
strategy E ends. The window, the candidates and the bar are constants, not options.

Run it through `ewbot strate count`: the workers are spawned, which needs the main guard of that entry point."""

from __future__ import annotations

import multiprocessing as mp
import statistics
import sys
from collections.abc import Iterable, Mapping
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from ewbot.strate.config import CANDIDATES, MONTHS, exit_days
from ewbot.strate.data import MAX_MISSING_HOURS, coverage, refuse_2024
from ewbot.strate.engine import simulate

GATE_MEDIAN_ENTRIES = 84  # 1.0 per month over 84 months
REPORT_KIND = "ewbot-strate-count"
PROCEED = "PROCEED"
STOP = "STOP"
_NEXT = {
    PROCEED: "next: report to the user; stage 2 gets a separate plan",
    STOP: "spec 5: strategy E ends here; no walk-forward, no other variant",
}

ByYear = dict[int, int]

_WORKER: dict[str, Any] = {}  # per-process, set by the pool initializer


def tally_entries(entry_ts: Iterable[int]) -> ByYear:
    """{UTC year of the entry: entries}."""
    out: ByYear = {}
    for ts in entry_ts:
        year = datetime.fromtimestamp(ts, UTC).year
        out[year] = out.get(year, 0) + 1
    return out


def _setup_worker(df1h: pd.DataFrame) -> None:
    _WORKER["df1h"] = df1h


def _count_candidate(n: int) -> tuple[int, ByYear]:
    """One engine run of one candidate. Returns the counts and nothing else."""
    return n, tally_entries(simulate(_WORKER["df1h"], n).entry_ts)


def run_count(df1h: pd.DataFrame, workers: int = 1) -> dict[int, ByYear]:
    """Every candidate once, in order. Refuses bars from 2024 on before any run."""
    refuse_2024(df1h)
    out: dict[int, ByYear] = {}
    if workers <= 1:
        _setup_worker(df1h)
        for n in CANDIDATES:
            out[n] = _count_candidate(n)[1]
            print(f"[count] N={n} done", file=sys.stderr, flush=True)
    else:
        with ProcessPoolExecutor(
            max_workers=min(workers, len(CANDIDATES)),
            mp_context=mp.get_context("spawn"),
            initializer=_setup_worker,
            initargs=(df1h,),
        ) as pool:
            for n, by_year in pool.map(_count_candidate, CANDIDATES):
                out[n] = by_year
                print(f"[count] N={n} done", file=sys.stderr, flush=True)
    return out


def decide(totals: Mapping[int, int]) -> dict[str, Any]:
    """Spec 5.1 on each candidate's entries over the window."""
    if set(totals) != set(CANDIDATES):
        raise ValueError(f"the gate needs the candidates {CANDIDATES}, got {sorted(totals)}")
    median = statistics.median(totals.values())
    passed = median >= GATE_MEDIAN_ENTRIES
    return {
        "threshold_entries": GATE_MEDIAN_ENTRIES,
        "threshold_monthly": GATE_MEDIAN_ENTRIES / MONTHS,
        "median_entries": median,
        "median_monthly": median / MONTHS,
        "passed": passed,
        "verdict": PROCEED if passed else STOP,
    }


def build_report(results: Mapping[int, ByYear], df1h: pd.DataFrame) -> dict[str, Any]:
    totals = {n: sum(by_year.values()) for n, by_year in results.items()}
    cov = coverage(df1h["ts"].to_numpy())
    return {
        "kind": REPORT_KIND,
        "window": {"start": "2017-01-01", "end": "2024-01-01", "months": MONTHS},
        "data": {"first_ts": cov.first_ts, "last_ts": cov.last_ts, "rows": cov.rows, "missing_hours": cov.missing_hours,
                 "max_missing_hours": MAX_MISSING_HOURS},
        "candidates": [
            {
                "n": n,
                "exit_days": exit_days(n),
                "entries": totals[n],
                "monthly": totals[n] / MONTHS,
                "by_year": {str(y): by_year[y] for y in sorted(by_year)},
            }
            for n, by_year in results.items()
        ],
        **decide(totals),
    }


def report_lines(rep: Mapping[str, Any]) -> list[str]:
    win, data = rep["window"], rep["data"]
    lines = [
        f"strategy E entries, {win['start']} .. {win['end']} (end exclusive), {win['months']} months",
        f"cache: {data['rows']} hours, {data['missing_hours']} missing (limit {data['max_missing_hours']})",
    ]
    for row in rep["candidates"]:
        lines.append(f"N={row['n']} exit={row['exit_days']}: {row['entries']} entries, {row['monthly']:.2f} per month")
    lines.append(f"median of {len(rep['candidates'])} candidates = {rep['median_entries']} entries = "
                 f"{rep['median_monthly']:.3f} per month (needs >= {rep['threshold_entries']} entries)")
    lines.append(f"verdict: {rep['verdict']}")
    lines.append(_NEXT[rep["verdict"]])
    return lines
```

- [ ] **Step 4: 통과를 확인한다.**

```bash
uv run --group live pytest tests/strate/test_data.py tests/strate/test_count.py -q
```

Expected: `39 passed`. 2024년 봉과 2017년 이전 봉이 든 parquet, 61,344시간짜리 프레임, spawn 일꾼 2개까지 모두 합성 데이터로 돈다.

- [ ] **Step 5: 커밋한다.**

```bash
git add src/ewbot/strate/data.py src/ewbot/strate/count.py tests/strate/test_data.py tests/strate/test_count.py
git commit -m "feat(strate): read-only cache guard from the ts column and the stage-1 count: entries by year only, the pre-registered gate, no performance value" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 4: `ewbot strate count`와 격리 가드

**Files:**
- Modify: `src/ewbot/cli.py`(아래 네 곳만)
- Create: `tests/strate/test_cli_strate.py`, `tests/strate/test_isolation.py`

**Interfaces:**
- Consumes:
  - Task 3: `strate.count.build_report`, `report_lines`, `run_count`, `strate.data.DataWindowError`, `cache_file`, `load_btc_1h`
  - `cli.py`의 `_refuse(msg: str) -> NoReturn`, `_b_write_json(path: Path, obj: Any) -> None`, `_COMMANDS`, `build_parser() -> argparse.ArgumentParser`, `main(argv: Sequence[str] | None = None) -> int`
  - `ewbot.paper.fingerprint.PROJECT_ROOT`, `current_fingerprint(repo_root: Path | None = None) -> str`, `is_fingerprinted(rel: str) -> bool`, `tree_files(repo_root: Path | None = None) -> dict[str, bytes]`
- Produces:
  - 명령 `ewbot strate count [--cache-dir PATH] [--workers N] [--out PATH]`, 기본값 `data/cache`, CPU 수 − 1, `reports/strate-count.json`. 종료 코드는 정상 0, 캐시 거부 2.
  - `cli.cmd_strate(args: argparse.Namespace) -> int`

- [ ] **Step 1: 실패하는 테스트를 쓴다.**

`tests/strate/test_cli_strate.py` (전체 내용):

```python
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ewbot.cli import build_parser, main
from ewbot.strate.config import CANDIDATES
from ewbot.strate.data import cache_file
from tests.strate.helpers import flip_path

# Run under spawn with a main guard, as the `ewbot` console script is; only the coverage check is off for the short cache.
DRIVER = '''\
import sys

import ewbot.strate.data as data
from ewbot.cli import main

if __name__ == "__main__":
    data.check_coverage = lambda ts: None
    sys.exit(main(sys.argv[1:]))
'''


def _cache(tmp_path: Path) -> Path:
    cache = tmp_path / "cache"
    cache.mkdir()
    flip_path().to_parquet(cache_file(cache), index=False)
    return cache


def _code(argv: list[str]) -> int:
    """main's exit code, also when a refusal exits through SystemExit (cli._refuse)."""
    try:
        return main(argv)
    except SystemExit as exc:
        return int(exc.code)


def test_the_parser_has_the_count_command_and_no_option_changes_the_bar_the_window_or_the_candidates():
    p = build_parser()
    ns = p.parse_args(["strate", "count"])
    assert (ns.cmd, ns.s_cmd) == ("strate", "count")
    assert (ns.cache_dir, ns.out) == (Path("data/cache"), Path("reports/strate-count.json"))
    assert ns.workers >= 1
    assert p.parse_args(["strate", "count", "--workers", "3"]).workers == 3
    assert set(vars(ns)) == {"cmd", "s_cmd", "cache_dir", "workers", "out"}
    for extra in (["--threshold", "3"], ["--gate", "60"], ["--start", "2018-01-01"], ["--end", "2023-01-01"], ["--n", "20"],
                  ["--candidates", "20,40"], ["--months", "60"], ["--max-missing", "1000"]):
        with pytest.raises(SystemExit):
            p.parse_args(["strate", "count", *extra])


def _strate_modules_after_importing(module: str) -> str:
    """The exact package, not a bare prefix: ewbot.strategy starts with "ewbot.strate" too."""
    code = (f"import sys, {module}\n"
            "print(sorted(m for m in sys.modules if m == 'ewbot.strate' or m.startswith('ewbot.strate.')))")
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()


def test_the_cli_loads_strate_only_for_strate_commands():
    """`ewbot live …` and `ewbot paper …` run with the same modules as before strategy E."""
    assert _strate_modules_after_importing("ewbot.cli") == "[]"
    assert "ewbot.strate.count" in _strate_modules_after_importing("ewbot.strate.count")  # non-vacuity


def test_the_command_writes_the_report_and_prints_the_verdict(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("ewbot.strate.data.check_coverage", lambda ts: None)
    seen = {}

    def fake_run(df1h, workers):
        seen["workers"] = workers
        return {n: {2019: 84} for n in CANDIDATES}

    monkeypatch.setattr("ewbot.strate.count.run_count", fake_run)
    out = tmp_path / "reports" / "count.json"
    assert main(["strate", "count", "--cache-dir", str(_cache(tmp_path)), "--workers", "3", "--out", str(out)]) == 0
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["verdict"] == "PROCEED" and [r["n"] for r in rep["candidates"]] == [20, 40, 55, 100]
    assert seen["workers"] == 3
    printed = capsys.readouterr().out
    assert "N=20 exit=10: 84 entries, 1.00 per month" in printed
    assert "verdict: PROCEED" in printed and f"json: {out}" in printed


def test_a_short_cache_is_refused_and_writes_nothing(tmp_path, capsys):
    out = tmp_path / "count.json"
    assert _code(["strate", "count", "--cache-dir", str(_cache(tmp_path)), "--out", str(out)]) == 2
    assert "2017-01-01" in capsys.readouterr().err and not out.exists()


def test_a_missing_cache_says_so_and_nothing_is_fetched(tmp_path):
    none = tmp_path / "none"
    with pytest.raises(SystemExit, match="1h cache not found"):
        main(["strate", "count", "--cache-dir", str(none), "--out", str(tmp_path / "count.json")])
    assert not none.exists()


def test_the_command_runs_engine_workers_under_spawn_with_a_main_guard(tmp_path):
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    out = tmp_path / "count.json"
    argv = [sys.executable, str(driver), "strate", "count", "--cache-dir", str(_cache(tmp_path)), "--workers", "2",
            "--out", str(out)]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert [(c["n"], c["entries"]) for c in rep["candidates"]] == [(20, 2), (40, 0), (55, 0), (100, 0)]  # hand-computed
    assert rep["median_entries"] == 0 and rep["verdict"] == "STOP"
```

`tests/strate/test_isolation.py` (전체 내용):

```python
"""Strategy-A protection (Plan 7 spec 1, 3, 6): E must not change anything A's paper, live or go-live count depends on, nor B's
ended code. These run in the full suite; tests/paper/test_static.py (run by ci.yml) scans strate/ too."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

import ewbot
from ewbot.paper.fingerprint import PROJECT_ROOT, current_fingerprint, is_fingerprinted, tree_files

SRC = Path(ewbot.__file__).resolve().parent
FROZEN_A = "e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1"
PROTECTED = (
    "src/ewbot/types.py", "src/ewbot/config.py", "src/ewbot/indicators.py", "src/ewbot/data/candles.py",
    "src/ewbot/waves/", "src/ewbot/strategy/", "src/ewbot/engine/", "src/ewbot/execution/", "src/ewbot/risk/",
    "src/ewbot/paper/", "src/ewbot/live/", "src/ewbot/backtest/", "src/ewbot/stratb/", "src/ewbot/paperb/",
    "holdout.lock", ".github/workflows/paper.yml", ".github/workflows/ci.yml",
)
E_PATHS = ("src/ewbot/strate/", "tests/strate/")
FORBIDDEN = {"allow_holdout", "include_holdout", "unlock_2024", "check_before_holdout", "_map_tasks", "_init_worker"}
FOREIGN_PACKAGES = ("ewbot.paper", "ewbot.paperb", "ewbot.live", "ewbot.aeth", "ewbot.stratb", "ewbot.stratc", "ewbot.stratd",
                    "ewbot.waves", "ewbot.strategy", "ewbot.engine")
A_ARTIFACTS = {"paper.yml", "ci.yml", "holdout.lock", "refs/heads/paper", "origin/paper"}
WRITERS = {"to_parquet", "to_csv", "to_pickle", "write_table", "write_bytes", "write_text", "update_cache", "unlink", "rename",
           "replace"}


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], capture_output=True, text=True, timeout=60)


def _merge_base() -> str | None:
    for ref in ("master", "origin/master"):
        r = _git("merge-base", "HEAD", ref)
        if r.returncode == 0:
            return r.stdout.strip()
    return None


def _files() -> list[Path]:
    return sorted((SRC / "strate").rglob("*.py"))


def _modules():
    for f in _files():
        yield f.relative_to(SRC).as_posix(), ast.parse(f.read_text(encoding="utf-8"), filename=str(f))


def _names(tree: ast.AST) -> set[str]:
    """Every identifier and whole string of a module: names, attributes, keywords, parameters, imports, definitions."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            out.add(node.arg)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, ast.alias):
            out.add(node.name.split(".")[-1])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.add(node.value)
    return out


def test_strategy_a_fingerprint_is_unchanged():
    assert current_fingerprint() == FROZEN_A


def test_strate_is_outside_strategy_a_fingerprinted_folders():
    assert not is_fingerprinted("strate/x.py") and not is_fingerprinted("strate/sub/y.py")
    assert {"strate/config.py", "strate/rules.py", "strate/engine.py", "strate/data.py", "strate/count.py"} <= {
        rel for rel, _ in _modules()}
    assert not [p for p in tree_files() if p.startswith("src/ewbot/strate/")]


def test_strate_work_changes_no_protected_path():
    """Since the merge-base with master (tracked changes and new untracked files): a branch that touches strategy E touches no
    protected path. A branch that does not touch E is not concerned."""
    base = _merge_base()
    if base is None:
        pytest.skip("no master branch in this clone (shallow CI checkout)")
    diff = _git("diff", "--name-only", base)
    new = _git("ls-files", "--others", "--exclude-standard")
    assert diff.returncode == 0 and new.returncode == 0
    changed = set(diff.stdout.split()) | set(new.stdout.split())
    if any(p.startswith(E_PATHS) for p in changed):
        assert sorted(p for p in changed if p.startswith(PROTECTED)) == []


@pytest.mark.parametrize(
    "src",
    [
        "run(cfg, allow_holdout=True)",
        "load_1h(path, include_holdout=True)",
        "def f(unlock_2024=False): ...",
        "from ewbot.backtest.runner import _map_tasks",
        "import ewbot.backtest.runner as r\nr._init_worker(1)",
        'x = "check_before_holdout"',
    ],
)
def test_the_name_guard_catches(src):
    assert _names(ast.parse(src)) & FORBIDDEN


def test_strate_uses_none_of_the_forbidden_names_in_any_form():
    for f in _files():
        text = f.read_text(encoding="utf-8")
        assert not _names(ast.parse(text)) & FORBIDDEN, f.name
        assert not [name for name in FORBIDDEN if name in text], f.name  # comments and docstrings too


def test_only_strate_holdout_may_use_the_unlock_keyword():
    users = {rel for rel, tree in _modules() if "unlock_holdout_e" in _names(tree)}
    assert users <= {"strate/holdout.py"}


def test_strate_reads_candles_only_in_data_py():
    readers = {rel for rel, tree in _modules() if "read_parquet" in _names(tree)}
    assert readers == {"strate/data.py"}  # == and not <=: the guard looks at a module that does read


@pytest.mark.parametrize("src", ["df.to_parquet(p)", "p.write_bytes(b)", "p.unlink()", "update_cache(p)"])
def test_the_writer_guard_catches(src):
    assert _names(ast.parse(src)) & WRITERS


def test_strate_writes_no_file():
    """A's cache stays as it is: strate/ only reads it. The report is written by the CLI."""
    for rel, tree in _modules():
        assert not _names(tree) & WRITERS, rel


def test_strate_imports_no_runtime_no_waves_and_no_strategy_code():
    seen: set[str] = set()
    for rel, tree in _modules():
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        seen |= mods
        assert not [m for m in mods if m.startswith(FOREIGN_PACKAGES)], rel
    assert {"ewbot.execution.costs", "ewbot.risk.sizing", "ewbot.data.candles"} <= seen  # A's parts, imported only


def test_strate_never_names_strategy_a_runtime_artifacts():
    for rel, tree in _modules():
        assert not _names(tree) & A_ARTIFACTS, rel
```

- [ ] **Step 2: 실패를 확인한다.**

```bash
uv run --group live pytest tests/strate/test_cli_strate.py tests/strate/test_isolation.py -q
```

Expected: `5 failed, 20 passed`. 실패는 모두 `test_cli_strate.py`(파서에 `strate`가 아직 없다)이고, `test_the_cli_loads_strate_only_for_strate_commands`와 `test_isolation.py` 19개는 이미 지켜지는 것을 고정하는 가드라 지금도 통과한다.

- [ ] **Step 3: `src/ewbot/cli.py`를 네 곳 고친다.** 각 "찾기"는 파일에 정확히 한 번 나온다. import 줄은 건드리지 않는다.

모듈 설명의 끝. 찾기:

```python
inside its handlers, so `ewbot live …` and `ewbot paper …` never load it."""
```

바꾸기:

```python
inside its handlers, so `ewbot live …` and `ewbot paper …` never load it.
`strate count` (Plan 7) is stage 1 of strategy E (ewbot.strate): it counts the entries of the 4 candidates on A's BTC 1h cache
before 2024 and prints no performance value; ewbot.strate is imported only inside its handler."""
```

파서의 끝. 찾기:

```python
    return parser
```

바꾸기:

```python

    p_s = sub.add_parser("strate", help="strategy E (Plan 7): daily channel-breakout trend following; stage 1 only")
    s_sub = p_s.add_subparsers(dest="s_cmd", required=True)
    p_sc = s_sub.add_parser("count", help="stage 1: entries per month of the 4 candidates, 2017-01..2023-12 (counts only)")
    p_sc.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    p_sc.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p_sc.add_argument("--out", type=Path, default=Path("reports/strate-count.json"))
    return parser
```

처리 함수. 찾기:

```python
_COMMANDS = {
```

바꾸기:

```python
# ----------------------------------------------------------------------------- strategy E (Plan 7)
# ewbot.strate is imported inside this handler only: `ewbot live …` and `ewbot paper …` never load it.


def _s_count(args: argparse.Namespace) -> int:
    from ewbot.strate.count import build_report, report_lines, run_count
    from ewbot.strate.data import DataWindowError, cache_file, load_btc_1h

    path = cache_file(args.cache_dir)
    if not path.exists():
        raise SystemExit(f"1h cache not found: {path} - strategy E reads A's BTC cache and never fetches")
    try:
        df = load_btc_1h(args.cache_dir)
        rep = build_report(run_count(df, args.workers), df)
    except DataWindowError as exc:
        _refuse(str(exc))
    _b_write_json(args.out, rep)
    for line in report_lines(rep):
        print(line)
    print(f"json: {args.out}")
    return 0


def cmd_strate(args: argparse.Namespace) -> int:
    return {"count": _s_count}[args.s_cmd](args)


_COMMANDS = {
```

명령 표. 찾기:

```python
    "aeth": cmd_aeth,
}
```

바꾸기:

```python
    "aeth": cmd_aeth,
    "strate": cmd_strate,
}
```

- [ ] **Step 4: 통과를 확인한다.**

```bash
uv run --group live pytest tests/strate -q
```

Expected: `117 passed`.

- [ ] **Step 5: 전체 테스트와 CI 가드를 돌린다.**

```bash
uv run --group live pytest -q
uv run pytest -q tests/paper/test_fingerprint.py tests/paper/test_static.py
git status --short
```

Expected: 두 실행 모두 실패 0. 전체 실행은 이 계획을 쓸 때 저장소 밖의 임시 사본(시장 데이터 없음, 커밋 1개)에서 `1643 passed, 2 skipped`였고, 실제 저장소에서는 캐시와 git 이력 때문에 통과와 건너뜀 수가 조금 다를 수 있다. `git status --short`에는 이 계획이 만들거나 고친 파일만 나오고 금지 경로는 없다.

- [ ] **Step 6: 커밋한다.**

```bash
git add src/ewbot/cli.py tests/strate/test_cli_strate.py tests/strate/test_isolation.py
git commit -m "feat(strate): ewbot strate count, spawned workers behind the CLI's main guard, no option on the bar, window or candidates; isolation guards" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

## 실제 실행 (Task 4가 끝난 뒤, 사용자에게 알리고 한 번만)

Task들은 코드만 만든다. 떨어지면 E가 영구히 끝나는 판정이므로 "명세의 모호점" 1, 2, 4번을 사용자가 확인한 뒤에 실행한다.

1. 빠진 시간을 `ts` 열만 읽어 잰다. 2024 이후 봉과 가격은 읽지 않는다.

   ```bash
   uv run python -c "
   from pathlib import Path
   from ewbot.strate.data import MAX_MISSING_HOURS, cache_file, coverage, read_ts
   cov = coverage(read_ts(cache_file(Path('data/cache'))))
   print(cov, 'limit', MAX_MISSING_HOURS)
   "
   ```

   `first_ts`가 `1483228800`, `last_ts`가 `1704063600`이고 `missing_hours`가 300 이하면 진행한다. 아니면 멈추고 수치를 사용자에게 보고한다. 한도를 올리거나 A의 캐시를 다시 받지 않는다. `docs/aeth.md`에는 A의 BTC 캐시가 43시간 빠졌다고 적혀 있으나, 이 계획은 그 수를 확인하지 않았고 의존하지도 않는다.

2. 점검을 돌린다. 옵션 없이 한 번이다.

   ```bash
   uv run ewbot strate count
   ```

   중간에 죽어 보고서가 안 나왔다면 같은 명령을 다시 돌려도 된다. 판정이 나온 뒤에는 어떤 값도 바꿔 다시 돌리지 않는다.

3. 출력의 건수, 중앙값, 판정을 사용자에게 보고한다. `STOP`이면 명세 7절대로 명세 끝에 결과와 날짜를 적고 README의 전략 표에 한 줄을 더한다. `PROCEED`이면 ②는 별도 계획으로 쓴다.
