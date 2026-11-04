# ewbot Plan 6 — 전략 D 1단계(횟수 점검) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 전략 A와 지금 돌아가는 모의매매·SHADOW를 전혀 바꾸지 않고, A의 규칙을 시간만 4분의 1로 줄인 전략 D(1시간 파동 + 15분 돌파)의 엔진을 `src/ewbot/stratd/`에 만들고, 명세 5.1의 횟수 점검을 실행하는 `ewbot stratd count`까지 완성한다. 이 계획은 1단계(①)만 다루며, 성과 값은 계산하지도 출력하지도 않는다.

**Architecture:** A의 파동 엔진, 브로커, 거래 루프를 하위 클래스로 재사용하고, 봉 길이를 가정한 부분만 덮어쓴다. 타임스탬프는 실제 시각 그대로다. A의 파일이 지문으로 고정돼 있으므로 덮어쓴 메서드의 복사본은 원본과 어긋날 수 없고, 어긋남의 나머지 위험은 축소 동등성 테스트가 잡는다.

- `src/ewbot/stratd/config.py`: 봉 길이 상수, `base_config()`(= A의 `BotConfig()`).
- `src/ewbot/stratd/clock.py`: `data/candles.py`의 금요일 달력을 봉 길이 인자로 복사.
- `src/ewbot/stratd/waves.py`: `WaveEngineD(WaveEngine)`. `on_1h_close`의 봉 경계 검사만 15분/1시간으로.
- `src/ewbot/stratd/broker.py`: `BrokerD(PaperBroker)`. `process_bar`의 달력과 `_charge_funding`의 1/4 부과.
- `src/ewbot/stratd/engine.py`: `RunD(_Run)`(`execute`, `_may_enter`), `TradingEngineD(TradingEngine)`(`run`). 진행 곡선은 시간당 점.
- `src/ewbot/stratd/data.py`: `stratb.data15.load_15m`만으로 읽는 2024 이전 BTC 15분봉, 2024 이후 거부, 구간 확인.
- `src/ewbot/stratd/count.py`: 작업자(횟수만 반환), 게이트, 보고서.
- `src/ewbot/cli.py`(수정): `ewbot stratd count`. `ewbot.stratd`는 처리 함수 안에서만 import한다.
- `tests/stratd/`: 축소 동등성, 펀딩, 금요일, 15분봉 보존, 성과 값 차단, 격리 테스트.

**Tech Stack:** Python 3.12(uv), 기존 의존성만(numpy, pandas, pyarrow), 표준 라이브러리 `concurrent.futures.ProcessPoolExecutor`(spawn), pytest. 테스트 명령은 `uv run --group live pytest`.

## Global Constraints

**범위**
- 저장소 루트에서 실행한다. 브랜치는 `docs/strategy-d`(이미 체크아웃됨). 커밋만 하고 push, PR, 워크플로 실행, 시크릿 접근은 하지 않는다.
- 어떤 작업도 실제 캐시로 `ewbot stratd count`를 실행하지 않고, 어떤 백테스트도 돌리지 않으며, 성과 값을 계산하지 않고, 2024-01-01 이후 시장 데이터를 읽지 않는다. 테스트는 합성 봉과 `tmp_path`만 쓴다. 실제 횟수 점검은 이 계획이 끝나고 검토를 통과한 뒤 사용자의 지시로 `uv run ewbot stratd count`로 따로 실행한다.
- 범위 밖: `stratd/fingerprint.py`, `baselines.py`, `research.py`, `holdout.py`, 워크포워드, `holdout-d.lock`, 잠금 다이제스트에 묶인 2024 가드, 모의매매. 이것들은 **게이트를 통과했을 때만 별도 계획으로 만든다.**

**변경 금지 (명세 1절 그대로)**
- A의 지문 대상: `types.py`, `config.py`, `indicators.py`, `data/candles.py`, `waves/`, `strategy/`, `engine/`, `execution/`, `risk/` (`src/ewbot/` 아래, 폴더에 새 파일 추가 포함). `paper/`, `live/`, `backtest/`, `holdout.lock`, `paper.yml`, `ci.yml`, `stratb/`, `paperb/`.
- A 지문은 `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1`로 유지된다.
- 기존 파일 중 수정하는 것은 `src/ewbot/cli.py` 하나다. 이 파일은 금지 목록 밖이다(`is_fingerprinted("cli.py")`는 거짓, B의 deps와 A-ETH의 지문에도 없음, 수정 4곳은 Task 6에 정확히 적었다). 새 파일은 `src/ewbot/stratd/**`와 `tests/stratd/**`에만 만든다. `pyproject.toml`, `uv.lock`, 다른 테스트 파일은 고치지 않는다. 새 워크플로는 만들지 않는다.
- 시간표를 속이는 방식(타임스탬프 ×4)은 금지한다. 펀딩이 4배로 붙고 금요일 판단과 손실 한도 날짜가 깨진다.

**기존 가드 준수**
- `tests/paper/test_static.py`, `tests/stratb/test_isolation.py`, `tests/aeth/test_isolation.py`가 `src/ewbot` 전체를 훑는다. `stratd/`는 `allow_holdout`, `include_holdout`, `unlock_2024`를 쓰지 않고, `check_before_holdout`, `_map_tasks`, `_init_worker`를 이름, 속성, 문자열 어느 형태로도 쓰지 않는다. 병렬 실행은 자체 `ProcessPoolExecutor`와 자체 초기화 함수 `_setup_worker`로 한다.
- 새 최상위 패키지가 깨뜨리는 기존 테스트는 없다. 패키지를 열거하는 테스트는 없고, `tests/paper/test_fingerprint.py`의 파일 목록은 지문 대상만 본다. `backtest.holdout.code_fingerprint`(`src/ewbot` 전체 해시)는 값이 바뀌지만, `paper/runner.py:198`, `live/target.py:122`, `live/preflight.py:96`, `aeth/config.py:65`는 잠금에 저장된 값을 읽을 뿐 다시 계산하지 않고, 다시 계산하는 두 1회성 홀드아웃은 이미 `first_run`이 기록돼 있다.
- `count.py`의 실제 실행 진입점은 `ewbot` 명령뿐이다. `cli.py:1315`와 `.venv/bin/ewbot`에 main 가드가 있어 spawn에서 안전하다. 작업자 함수는 `ewbot.stratd.count`의 최상위 함수다.

**봉 길이**

| 항목 | A | D |
|---|---|---|
| 파동 봉 | 4시간 | 1시간 |
| 돌파 확인 봉 | 1시간 | 15분 |
| 자산 | BTC | BTC (같은 Coinbase 15분봉 캐시) |

**봉 개수로 같게 두는 값(숫자를 바꾸지 않는다):** `K4` 그리드 (2.0, 2.5, 3.0, 4.0), `atr_len` 14, `M` 12, `choch_bars` 3, 스윙 조회 4, ATR 버퍼(이제 1시간 ATR 기준), 윅 클립 6(이제 15분 ATR 기준), `warmup_4h` 200(33일 → 8.3일), 시간 손절 12/60봉(48~240시간 → 12~60시간).

**A와 그대로 같은 값:** 네 가지 진입 자리(W3, W5, W5_REV, C_END), `r_gate` 1.5, TP1 50% 후 손절 본전 이동, 추적 손절, W5_REV 위험 절반, 보수적 위험 1%·최대 3배, 비용(`CostParams` 기본값).

**실제 시간과 돈 기준이라 줄이지 않는 값:** 시간당 펀딩, 금요일 휴장·블랙아웃 시각, 뉴욕 날짜 기준 손실 한도와 24시간 정지, 체결당 수수료와 슬리피지, 연율화 8760시간, 비교용 4시간 EMA.

**데이터:** `data/cache/BTC-USD_15m.parquet` (git 제외). 2017-01-01 00:00 ~ 2023-12-31 23:45 UTC, 245,123행. 파일을 고치지 않고 `stratb/data15.py`의 `load_15m`을 import해 읽는다.

**게이트 (명세 5.1):** 구간 2017-01-01 ~ 2023-12-31, 84개월. 손실 한도는 끈다. 작업자는 진입 시각 기준 연도별·자리별 건수만 반환한다.
- 1차: 60개 조합(K4 4 × Q_MIN 5 × S_MIN 3)의 월평균 건수 중앙값 ≥ 4.0이면 그리드 그대로 ②로 간다.
- 예비: 미달이면 Q_MIN 0.5, S_MIN 0.0으로 고정하고 K4 4개를 센다. 중앙값 ≥ 4.0이면 이 4개 조합을 그리드로 삼아 ②로 간다.
- 중단: 그래도 미달이면 수익을 보지 않고 끝낸다.
- 합격선은 결과를 본 뒤 바꾸지 않는다. CLI에 합격선, 구간, 조합을 바꾸는 옵션을 두지 않는다.

**보고서:** `reports/stratd-count.json`(`--out`으로 변경 가능, `reports/`는 git 제외)에 쓰고 조합별 월평균 건수, 중앙값, 판정을 출력한다.

**이 계획이 명세를 해석하거나 바로잡은 점 (규칙은 바꾸지 않는다)**
1. **명세 3절의 사실 오류.** "시간봉 기본값의 `fill_gaps`가 봉 4분의 3을 버린다"는 틀렸다. 15분봉에 `fill_gaps(df)`를 부르면 `synthetic[~missing] = flags`에서 `ValueError`가 난다(합성 프레임으로 확인: "cannot assign 40 input values to the 10 output values"). 결론("15분봉을 `TradingEngine.run`에 넣지 않는다")은 그대로이고, D는 `fill_gaps(df15, tf=M15)`를 쓴다. Task 4가 이 동작을 테스트로 고정한다.
2. **축소 동등성의 조건 하나 추가.** 명세 6절은 "펀딩 0과 금요일 구간 제외"만 적었지만, 손실 한도도 꺼야 한다. 뉴욕 날짜와 24시간 정지는 실제 시각 규칙이라 시간을 압축하면 달라진다. 테스트는 `breakers.enabled=False`를 더한다. 금요일 제외는 경로를 고르는 대신 A와 D의 달력을 테스트 안에서 "항상 아님"으로 바꿔 구현한다. A의 경로는 일주일을 넘으므로 금요일을 피할 수 없고, 달력 자체는 Task 1, 3, 4가 따로 검사한다.
3. **`metrics` import 금지의 범위.** `count.py`가 직접 import하지 않는 것은 지킨다. 그러나 명세가 지시한 `backtest.walkforward`(`GRID`, `config_for`)와 `stratb.data15`(`load_15m`)가 모듈 수준에서 `backtest.metrics`를 불러오므로, 이 모듈이 프로세스에 올라오는 것 자체는 금지 파일을 고치지 않고는 막을 수 없다. 그래서 테스트는 ① `count.py`의 AST에 성과 모듈 import, 성과 함수 이름, 거래 결과 속성이 없음, ② 실제 실행에서 `compute_metrics`가 호출되지 않음을 본다. `sys.modules` 검사는 하지 않는다.
4. **예비안은 시뮬레이션을 더 하지 않는다.** (K4, 0.5, 0.0) 4개는 60개 안에 있으므로 같은 실행의 건수로 판단한다. 테스트로 고정한다.
5. **중앙값 계산.** 건수 합계의 중앙값을 먼저 구하고 84로 나눈다. 335건과 337건의 중앙값은 정확히 4.0이 되어, 부동소수점 오차로 판정이 뒤집히지 않는다.
6. **건수의 정의.** 체결된 진입 하나가 1건이다. 자기 진입 시각의 UTC 연도로 묶고, 데이터 끝에서 닫힌(END) 거래도 진입했으므로 센다. 대기 중 사라진 신호는 세지 않는다.
7. **명세에 없는 안전장치 하나.** 캐시가 2017-01-01 00:00 ~ 2023-12-31 23:45를 덮지 않으면 `count`는 거부한다(종료 코드 2). 분모가 84개월로 고정이고 떨어지면 D가 영구히 끝나므로, 잘린 캐시가 거짓 중단을 만들면 안 된다.
8. **손실 한도 관측 시점.** 명세에 없다. A가 돌파 봉마다 관측하므로 D도 15분 봉 마감마다(`bar.ts + M15`) 관측한다. 1단계에서는 꺼져 있어 횟수에 영향이 없고, ③에서 켤 때 이 선택을 다시 검토한다.

**커밋:** 모든 커밋 메시지는 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` 줄로 끝난다.

---

### Task 1: 패키지, 고정값, 거래 달력, 격리 검사

**Files:**
- Create: `src/ewbot/stratd/__init__.py`
- Create: `src/ewbot/stratd/config.py`
- Create: `src/ewbot/stratd/clock.py`
- Test: `tests/stratd/__init__.py`
- Test: `tests/stratd/helpers.py`
- Test: `tests/stratd/test_config.py`
- Test: `tests/stratd/test_clock.py`
- Test: `tests/stratd/test_isolation.py`

**Interfaces:**
- Consumes: `ewbot.config.BotConfig`; `ewbot.types.H1`; `ewbot.paper.fingerprint`: `PROJECT_ROOT`, `current_fingerprint() -> str`, `is_fingerprinted(rel: str) -> bool`, `tree_files() -> dict[str, bytes]`; `ewbot.data.candles`: `futures_closed(ts: int) -> bool`, `entry_blackout(ts: int) -> bool` (비교용); `tests.synth.random_bars(seed: int, n: int, vol: float = 6.0, start: float = 5000.0) -> list[Bar]`.
- Produces:
  - `ewbot.stratd.config`: `M15 = 900`, `WAVE_BAR = 3600`, `TRIGGER_BARS_PER_WAVE = 4`, `HOUR_LAST = 2700`, `base_config() -> BotConfig`.
  - `ewbot.stratd.clock`: `futures_closed(ts: int, tf: int) -> bool`, `entry_blackout(ts: int, tf: int) -> bool`.
  - `tests.stratd.helpers`: `T_A`, `T_D`, `N_BARS`, `GAPS`, `ny(year: int, month: int, day: int, hour: int, minute: int = 0) -> int`, `a_frame() -> pd.DataFrame`, `compress(df1h: pd.DataFrame) -> pd.DataFrame`, `idx_a(ts: int) -> int`, `idx_d(ts: int) -> int`.

- [ ] **Step 1: 실패하는 테스트 작성**

테스트 파일만 만든다. `src/ewbot/stratd/` 폴더는 아직 만들지 않는다.

**파일 `tests/stratd/__init__.py`**

```python
"""Tests for ewbot.stratd (Plan 6)."""
```

**파일 `tests/stratd/helpers.py`**

```python
"""Synthetic bars for the strategy-D tests (no network, no real cache).

a_frame() is the path tests/backtest/test_integration_synth.py runs strategy A on: two years of 1h bars, seed 0, with
its three missing hours, from T_A. compress() keeps every bar and gives it one 15m slot instead of one 1h slot, so bar k
is the same bar in both frames and only ts differs. T_A is a multiple of 4h and T_D a multiple of 1h, so A's 4h buckets
and D's 1h bars hold the same bars."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ewbot.stratd.config import M15
from ewbot.types import H1
from tests.synth import random_bars

T_A = 1_605_657_600  # 2020-11-18 00:00 UTC: tests/synth's T0 moved back 156 whole weeks
T_D = T_A
N_BARS = 2 * 8760
GAPS = [3_000, 3_001, 9_000]  # bar indices the path misses
_NY = ZoneInfo("America/New_York")


def ny(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    """Epoch seconds of a New York wall-clock time."""
    return int(datetime(year, month, day, hour, minute, tzinfo=_NY).timestamp())


def a_frame() -> pd.DataFrame:
    bars = random_bars(seed=0, n=N_BARS, vol=30.0, start=20_000.0)
    df = pd.DataFrame(
        {
            "ts": np.array([T_A + k * H1 for k in range(N_BARS)], dtype=np.int64),
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    )
    return df.drop(index=GAPS).reset_index(drop=True)


def compress(df1h: pd.DataFrame) -> pd.DataFrame:
    """The same bars at 15m spacing from T_D: nothing but ts changes."""
    return df1h.assign(ts=T_D + (df1h["ts"] - T_A) // (H1 // M15))


def idx_a(ts: int) -> int:
    return (ts - T_A) // H1


def idx_d(ts: int) -> int:
    return (ts - T_D) // M15
```

**파일 `tests/stratd/test_config.py`**

```python
from __future__ import annotations

from ewbot.backtest.walkforward import GRID, config_for
from ewbot.config import CONSERVATIVE, SETUPS, BotConfig, CostParams
from ewbot.strategy import elliott
from ewbot.stratd.config import HOUR_LAST, M15, TRIGGER_BARS_PER_WAVE, WAVE_BAR, base_config


def test_the_bar_lengths():
    assert (M15, WAVE_BAR, TRIGGER_BARS_PER_WAVE, HOUR_LAST) == (900, 3600, 4, 2700)


def test_the_bar_count_values_keep_a_s_numbers():
    w, s = base_config().wave, base_config().strategy
    assert GRID["wave.K4"] == (2.0, 2.5, 3.0, 4.0)
    assert (w.atr_len, w.M, w.wick_clip_atr1h, w.warmup_4h) == (14, 12, 6.0, 200)
    assert (s.choch_bars, s.time_stop_min_4h, s.time_stop_max_4h) == (3, 12, 60)
    assert elliott._SWING_LOOKBACK == 4


def test_the_unchanged_values_are_a_s():
    cfg = base_config()
    assert cfg == BotConfig()
    assert cfg.strategy.mode == "A" and cfg.strategy.setups_enabled == SETUPS == ("W3", "W5", "W5_REV", "C_END")
    assert (cfg.strategy.r_gate, cfg.strategy.tp1_frac, cfg.strategy.setup3_risk_mult) == (1.5, 0.5, 0.5)
    assert cfg.risk == CONSERVATIVE and (cfg.risk.risk_pct, cfg.risk.max_leverage) == (0.01, 3.0)
    assert cfg.costs == CostParams()


def test_a_grid_combo_sets_k4_q_min_and_s_min():
    cfg = config_for(base_config(), (2.5, 0.55, 0.25))
    assert (cfg.wave.K4, cfg.strategy.Q_MIN, cfg.strategy.S_MIN) == (2.5, 0.55, 0.25)
```

**파일 `tests/stratd/test_clock.py`**

```python
from __future__ import annotations

import pytest

from ewbot.backtest.runner import utc_ts
from ewbot.data import candles
from ewbot.stratd.clock import entry_blackout, futures_closed
from ewbot.stratd.config import M15
from ewbot.types import H1
from tests.stratd.helpers import ny

SLOTS = [(h, m) for h in range(24) for m in (0, 15, 30, 45)]


@pytest.mark.parametrize("day", [(2023, 11, 17), (2023, 7, 14)], ids=["EST", "EDT"])
def test_the_friday_windows_on_15m_bars_are_at_the_true_wall_clock_time(day):
    closed = [s for s in SLOTS if futures_closed(ny(*day, *s), M15)]
    blackout = [s for s in SLOTS if entry_blackout(ny(*day, *s), M15)]
    assert closed == [(17, 0), (17, 15), (17, 30), (17, 45)]
    assert blackout == [(16, 0), (16, 15), (16, 30), (16, 45), (17, 0), (17, 15), (17, 30), (17, 45)]


def test_other_days_are_never_closed():
    for day in ((2023, 11, 16), (2023, 11, 18)):  # Thursday, Saturday
        assert not any(entry_blackout(ny(*day, *s), M15) or futures_closed(ny(*day, *s), M15) for s in SLOTS)


def test_on_1h_bars_it_is_a_s_calendar_for_a_whole_year():
    t0 = utc_ts("2023-01-01")
    for ts in range(t0, t0 + 8760 * H1, H1):
        assert futures_closed(ts, H1) == candles.futures_closed(ts)
        assert entry_blackout(ts, H1) == candles.entry_blackout(ts)


def test_a_s_1h_calendar_would_close_a_15m_bar_too_early():
    bar = ny(2023, 11, 17, 16, 15)
    assert candles.futures_closed(bar) is True
    assert futures_closed(bar, M15) is False
```

**파일 `tests/stratd/test_isolation.py`**

```python
"""Strategy-A protection (Plan 6 spec 1, 3, 6): D must not change anything A's paper, live or go-live count depends on,
nor strategy B's ended code. These run in the full suite; tests/paper/test_static.py (run by ci.yml) scans stratd/ too."""

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
D_PATHS = ("src/ewbot/stratd/", "tests/stratd/")
FORBIDDEN = {"allow_holdout", "include_holdout", "unlock_2024", "check_before_holdout", "_map_tasks", "_init_worker"}
FOREIGN_PACKAGES = ("ewbot.paper", "ewbot.live", "ewbot.aeth", "ewbot.stratb")
ALLOWED_FOREIGN = {"ewbot.stratb.data15"}  # spec 4: D reads the 15m cache through B's load_15m


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], capture_output=True, text=True, timeout=60)


def _merge_base() -> str | None:
    for ref in ("master", "origin/master"):
        r = _git("merge-base", "HEAD", ref)
        if r.returncode == 0:
            return r.stdout.strip()
    return None


def _modules():
    for f in sorted((SRC / "stratd").rglob("*.py")):
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


def test_stratd_is_outside_strategy_a_fingerprinted_folders():
    assert not is_fingerprinted("stratd/x.py") and not is_fingerprinted("stratd/sub/y.py")
    assert [rel for rel, _ in _modules()]
    assert not [p for p in tree_files() if p.startswith("src/ewbot/stratd/")]


def test_stratd_work_changes_no_protected_path():
    """Since the merge-base with master (tracked changes and new untracked files): a branch that touches strategy D
    touches no protected path. A branch that does not touch D is not concerned."""
    base = _merge_base()
    if base is None:
        pytest.skip("no master branch in this clone (shallow CI checkout)")
    diff = _git("diff", "--name-only", base)
    new = _git("ls-files", "--others", "--exclude-standard")
    assert diff.returncode == 0 and new.returncode == 0
    changed = set(diff.stdout.split()) | set(new.stdout.split())
    if any(p.startswith(D_PATHS) for p in changed):
        assert sorted(p for p in changed if p.startswith(PROTECTED)) == []


@pytest.mark.parametrize(
    "src",
    [
        "run(cfg, allow_holdout=True)",
        "load_15m(path, include_holdout=True)",
        "def f(unlock_2024=False): ...",
        "from ewbot.backtest.runner import _map_tasks",
        "import ewbot.backtest.runner as r\nr._init_worker(1)",
        'x = "check_before_holdout"',
    ],
)
def test_the_name_guard_catches(src):
    assert _names(ast.parse(src)) & FORBIDDEN


def test_stratd_uses_none_of_the_forbidden_names():
    for rel, tree in _modules():
        assert not _names(tree) & FORBIDDEN, rel


def test_only_stratd_holdout_may_use_the_unlock_keyword():
    users = {rel for rel, tree in _modules() if "unlock_holdout_d" in _names(tree)}
    assert users <= {"stratd/holdout.py"}


def test_stratd_reads_candles_only_through_load_15m_in_data():
    for rel, tree in _modules():
        names = _names(tree)
        assert "read_parquet" not in names, rel
        assert "load_15m" not in names or rel == "stratd/data.py", rel


def test_stratd_imports_no_runtime_and_only_b_s_candle_reader():
    for rel, tree in _modules():
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not [m for m in mods if m.startswith(FOREIGN_PACKAGES) and m not in ALLOWED_FOREIGN], rel
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratd -q
```

예상: `test_config.py`, `test_clock.py`의 수집 오류 `ModuleNotFoundError: No module named 'ewbot.stratd'`. pytest가 수집 오류로 멈추므로 테스트는 실행되지 않는다.

- [ ] **Step 3: 구현**

**파일 `src/ewbot/stratd/__init__.py`**

```python
"""Strategy D (Plan 6): strategy A with every bar count unchanged on shorter bars - 1h waves, 15m trigger.

Stage 1 only so far: the trade count of spec 5.1. Everything lives in this folder; of the existing files only cli.py
gains the `stratd` command."""
```

**파일 `src/ewbot/stratd/config.py`**

```python
"""Strategy D's fixed values (Plan 6 spec 2): the bar lengths, and A's config.

Nothing here is a new rule value. D runs on A's BotConfig defaults, so K4, atr_len, M, choch_bars, the swing lookback,
the wick clip, warmup_4h (now 200 1h bars) and the time stops (now 12..60 1h bars) keep their numbers, and r_gate,
tp1_frac, the risk profile and the costs are A's. Only the bars are shorter."""

from __future__ import annotations

from ewbot.config import BotConfig
from ewbot.types import H1

M15 = 900  # one trigger bar, seconds
WAVE_BAR = H1  # one wave bar, seconds
TRIGGER_BARS_PER_WAVE = WAVE_BAR // M15
HOUR_LAST = H1 - M15  # ts % H1 of an hour's last 15m bar


def base_config() -> BotConfig:
    return BotConfig()
```

**파일 `src/ewbot/stratd/clock.py`**

```python
"""The trading calendar of data/candles.py with the bar length as a parameter (Plan 6 spec 3).

A's _overlaps_friday_window is written for a 1h bar: a 15m bar opening at 16:15 New York would count as overlapping the
17:00 close. The windows themselves are A's, at their real wall-clock times; they are not rescaled."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")


def _overlaps_friday_window(ts: int, tf: int, start_hour: int, end_hour: int) -> bool:
    """True if the bar [ts, ts+tf) overlaps Friday ``start_hour``:00-``end_hour``:00 New York time."""
    for probe in (ts, ts + tf - 1):  # local dates touched by the bar
        day = datetime.fromtimestamp(probe, _NY).date()
        if day.weekday() != 4:  # Friday
            continue
        w_start = int(datetime(day.year, day.month, day.day, start_hour, tzinfo=_NY).timestamp())
        w_end = int(datetime(day.year, day.month, day.day, end_hour, tzinfo=_NY).timestamp())
        if ts < w_end and ts + tf > w_start:
            return True
    return False


def futures_closed(ts: int, tf: int) -> bool:
    """CFM weekly halt: the bar [ts, ts+tf) overlaps Fri 17:00-18:00 America/New_York."""
    return _overlaps_friday_window(ts, tf, 17, 18)


def entry_blackout(ts: int, tf: int) -> bool:
    """No NEW entries decided on / filled in this bar: overlaps Fri 16:00-18:00 America/New_York."""
    return _overlaps_friday_window(ts, tf, 16, 18)
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratd tests/paper/test_static.py tests/paper/test_fingerprint.py tests/stratb/test_isolation.py tests/aeth/test_isolation.py -q
```

예상: 실패 0건. `tests/stratd`에서 22개가 통과한다.

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratd/__init__.py src/ewbot/stratd/config.py src/ewbot/stratd/clock.py tests/stratd/__init__.py tests/stratd/helpers.py tests/stratd/test_config.py tests/stratd/test_clock.py tests/stratd/test_isolation.py
git commit -m "feat(stratd): package, bar lengths, 15m trading calendar and isolation checks" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: 1시간 파동 + 15분 원자의 파동 엔진

**Files:**
- Create: `src/ewbot/stratd/waves.py`
- Test: `tests/stratd/test_waves.py`

**Interfaces:**
- Consumes: `ewbot.waves.engine.WaveEngine(params: WaveParams)`의 `on_1h_close(bar1h, bar4h, sub_bars)`(`waves/engine.py:142-168`)와 그 비공개 멤버 `_zz`, `_hist`, `_i4h`, `_pivots_t`, `_E_live`, `_E_live_ts`, `_prev_top`, `_on_new_pivot()`, `_rescore()`, `_kill(bar)`, `_state(ts, new_piv, closed)`; `ewbot.waves.engine.WaveState`; `ewbot.waves.zigzag.Pivot`; `ewbot.engine.loop._agg_4h(sub: Sequence[Bar]) -> Bar`; `tests.synth.feed(bars1h) -> Iterator[tuple[Bar, Bar | None, tuple[Bar, ...] | None]]`.
- Produces: `ewbot.stratd.waves.WaveEngineD(WaveEngine)`, `on_1h_close(self, bar15: Bar, bar1h: Bar | None, sub_bars: Sequence[Bar] | None) -> WaveState`.

`WaveEngine`에서 봉 길이를 아는 줄은 `waves/engine.py:146-149`의 검사뿐이다. `enumerate.py`, `scoring.py`, `patterns.py`, `zigzag.py`, `strategy/elliott.py`에는 `H1`, `H4`, 3600 같은 봉 길이가 없고, 타임스탬프는 비교와 정렬에만 쓰인다.

- [ ] **Step 1: 실패하는 테스트 작성**

**파일 `tests/stratd/test_waves.py`**

```python
from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence

import pytest

from ewbot.config import WaveParams
from ewbot.data.candles import to_bars
from ewbot.engine.loop import _agg_4h
from ewbot.stratd.waves import WaveEngineD
from ewbot.types import H1, Bar
from ewbot.waves.engine import WaveEngine, WaveState
from ewbot.waves.zigzag import Pivot
from tests.stratd.helpers import a_frame, compress, idx_a, idx_d
from tests.synth import feed

PATH_BARS = 2_900  # the path before its first missing bar


def feed_d(bars15: Sequence[Bar]) -> Iterator[tuple[Bar, Bar | None, tuple[Bar, ...] | None]]:
    """tests/synth's feed one size down: 15m -> (bar15, bar1h | None, sub_bars | None)."""
    buf: list[Bar] = []
    for b in bars15:
        if not buf or b.ts // H1 != buf[0].ts // H1:
            buf = []
        buf.append(b)
        if len(buf) == 4 and buf[0].ts % H1 == 0:
            yield b, _agg_4h(buf), tuple(buf)
        else:
            yield b, None, None


def _view(ws: WaveState, idx: Callable[[int], int]) -> tuple:
    """Everything in a WaveState, with each ts as a bar index."""

    def piv(p: Pivot | None) -> tuple | None:
        return None if p is None else (p.i, idx(p.sub_ts), p.price, p.kind, p.confirmed_at)

    counts = tuple((c.pattern, c.d, c.phase, c.q, tuple(piv(p) for p in c.points)) for c in ws.counts)
    e_ts = idx(ws.E_live_ts) if ws.E_live_ts >= 0 else ws.E_live_ts  # -1 before the first leg
    return (idx(ws.ts), ws.ready, counts, ws.probs, ws.q_top, ws.s_rev, ws.lineages, ws.E_live, e_ts, ws.leg_dir,
            piv(ws.leg_start), tuple(piv(p) for p in ws.pivots4h), piv(ws.new_pivot_4h), ws.atr4h, ws.closed_4h, ws.i4h)


def test_the_same_bars_give_a_s_wave_states_bar_for_bar():
    df = a_frame().iloc[:PATH_BARS]
    a, d = WaveEngine(WaveParams()), WaveEngineD(WaveParams())
    n_counts = 0
    ws = None
    for xa, xd in zip(feed(to_bars(df)), feed_d(to_bars(compress(df))), strict=True):
        ws = a.on_1h_close(*xa)
        assert _view(ws, idx_a) == _view(d.on_1h_close(*xd), idx_d)
        n_counts += len(ws.counts)
    assert ws is not None and ws.ready and ws.pivots4h and n_counts > 0  # the comparison saw real structure


def test_a_s_wave_engine_refuses_15m_bars_and_d_takes_them():
    bars = to_bars(compress(a_frame().iloc[:4]))
    bar1h = _agg_4h(bars)
    with pytest.raises(ValueError, match="UTC 4h bucket"):
        WaveEngine(WaveParams()).on_1h_close(bars[3], bar1h, tuple(bars))
    ws = WaveEngineD(WaveParams()).on_1h_close(bars[3], bar1h, tuple(bars))
    assert ws.closed_4h and ws.i4h == 0 and ws.ts == bars[3].ts


def test_d_refuses_a_1h_bar_this_15m_bar_does_not_close():
    bars = to_bars(compress(a_frame().iloc[:8]))
    hour0 = _agg_4h(bars[:4])
    with pytest.raises(ValueError, match="UTC 1h bar"):
        WaveEngineD(WaveParams()).on_1h_close(bars[2], hour0, tuple(bars[:4]))  # not the hour's last bar
    with pytest.raises(ValueError, match="UTC 1h bar"):
        WaveEngineD(WaveParams()).on_1h_close(bars[7], hour0, tuple(bars[4:8]))  # another hour's last bar
    with pytest.raises(ValueError, match="sub_bars"):
        WaveEngineD(WaveParams()).on_1h_close(bars[3], hour0, tuple(bars[:3]))


def test_only_the_hour_s_last_15m_bar_closes_a_wave_bar():
    w = WaveEngineD(WaveParams())
    states = [w.on_1h_close(*x) for x in feed_d(to_bars(compress(a_frame().iloc[:8])))]
    assert [s.closed_4h for s in states] == [False, False, False, True] * 2
    assert [s.i4h for s in states] == [-1, -1, -1, 0, 0, 0, 0, 1]
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratd/test_waves.py -q
```

예상: 수집 오류 `ModuleNotFoundError: No module named 'ewbot.stratd.waves'`.

- [ ] **Step 3: 구현**

**파일 `src/ewbot/stratd/waves.py`**

```python
"""WaveEngineD: A's WaveEngine with 1h structure bars and 15m atoms (Plan 6 spec 3).

The only line of WaveEngine that knows a bar length is the check at waves/engine.py:146-149, which refuses anything but
a 1h bar closing a 4h bucket; everything else compares prices and orders ts values. on_1h_close below is A's method with
that check moved one size down. A's name stays so the engine's call reads the same, and so do the names in WaveState:
atr4h, pivots4h, new_pivot_4h, closed_4h and i4h carry 1h values here, and warmup_4h counts 1h bars (200 = 8.3 days).
A's file is fingerprinted, so this copy cannot drift from it."""

from __future__ import annotations

from typing import Sequence

from ewbot.stratd.config import M15
from ewbot.types import H1, Bar
from ewbot.waves.engine import WaveEngine, WaveState
from ewbot.waves.zigzag import Pivot


class WaveEngineD(WaveEngine):
    def on_1h_close(self, bar15: Bar, bar1h: Bar | None, sub_bars: Sequence[Bar] | None) -> WaveState:
        zz = self._zz
        new_piv: Pivot | None = None
        if bar1h is not None:
            if (bar15.ts + M15) % H1 != 0 or bar1h.ts != bar15.ts + M15 - H1:
                raise ValueError("bar1h must be the UTC 1h bar that bar15 closes")
            if sub_bars is not None and (len(sub_bars) == 0 or sub_bars[-1].ts != bar15.ts):
                raise ValueError("sub_bars must end with the 15m bar that closes the 1h bar")
        self._hist.push(bar15)
        if bar1h is not None:
            self._i4h += 1
            new_piv = zz.update(self._i4h, bar1h, sub_bars)
            if new_piv is not None:
                self._pivots_t = tuple(zz.pivots)
                self._on_new_pivot()
            if zz.leg_dir != 0:
                self._E_live, self._E_live_ts = zz.E, zz.E_sub_ts
                self._rescore()
        elif zz.leg_dir != 0:
            ext = bar15.high if zz.leg_dir > 0 else bar15.low
            if zz.leg_dir * (ext - self._E_live) > 0:
                self._E_live, self._E_live_ts = ext, bar15.ts
        if zz.leg_dir != 0:
            self._kill(bar15)
        state = self._state(bar15.ts, new_piv, bar1h is not None)
        self._prev_top = state.top
        return state
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratd/test_waves.py tests/stratd/test_isolation.py -q
```

예상: `17 passed`(`test_waves.py` 4개, `test_isolation.py` 13개).

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratd/waves.py tests/stratd/test_waves.py
git commit -m "feat(stratd): wave engine on 1h bars with 15m atoms, A's states bar for bar" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: 15분 브로커 (펀딩 1/4, 금요일은 실제 시각)

**Files:**
- Create: `src/ewbot/stratd/broker.py`
- Test: `tests/stratd/test_broker.py`

**Interfaces:**
- Consumes: `ewbot.execution.paper`: `PaperBroker(costs: CostParams, equity: float)`, `Fill`, `PaperPosition`, 비공개 멤버 `position`, `cash`, `_pending_entry`, `_pending_exit`, `_stop_triggered_while_closed`, `_stop_touched(pos, bar)`, `_close(ts, kind, price, qty) -> Fill`; `ewbot.execution.costs`: `fee(price, contracts, c)`, `slip(price, side, c)`, `funding_per_hour(mark, contracts, direction, c)`; `ewbot.stratd.clock.futures_closed(ts, tf)`; `ewbot.stratd.config.M15`.
- Produces: `ewbot.stratd.broker.BrokerD(PaperBroker)`, `process_bar(self, bar: Bar) -> list[Fill]`, `_charge_funding(self, bar: Bar) -> None`.

- [ ] **Step 1: 실패하는 테스트 작성**

**파일 `tests/stratd/test_broker.py`**

```python
from __future__ import annotations

import pytest

from ewbot.config import CostParams
from ewbot.data import candles
from ewbot.execution.costs import funding_per_hour, slip
from ewbot.execution.paper import PaperBroker
from ewbot.stratd.broker import BrokerD
from ewbot.stratd.config import M15
from ewbot.types import H1, Bar
from tests.stratd.helpers import ny

C = CostParams()
NO_FUNDING = CostParams(funding_apr_long=0.0)
MONDAY = ny(2023, 11, 13, 0)
FRIDAY = (2023, 11, 17)
PX = 20_000.0
EQUITY = 10_000.0


def bar(ts: int, o: float = PX, h: float = PX + 5, lo: float = PX - 5, c: float = PX) -> Bar:
    return Bar(ts, o, h, lo, c, 1.0)


def long_10(broker: PaperBroker) -> PaperBroker:
    broker.queue_entry(1, 10, 19_000.0, 21_000.0, 22_000.0, 5)
    return broker


def kinds(fills) -> list[str]:
    return [f.kind for f in fills]


def fill_view(fills_per_bar) -> list:
    return [[(f.kind, f.side, f.contracts, f.price, f.fee) for f in fills] for fills in fills_per_bar]


def test_four_15m_bars_pay_one_wall_clock_hour_of_funding():
    d, a = long_10(BrokerD(C, EQUITY)), long_10(PaperBroker(C, EQUITY))
    for k in range(4):
        d.process_bar(bar(MONDAY + k * M15))
    a.process_bar(bar(MONDAY))
    hour = funding_per_hour(PX, 10, 1, C)
    assert a.position.funding == pytest.approx(hour)
    assert d.position.funding == pytest.approx(hour)  # not 4 x the hour, as relabelled timestamps would pay


def test_one_15m_bar_pays_a_quarter_and_a_short_receives_it():
    d = BrokerD(C, EQUITY)
    d.queue_entry(-1, 10, 21_000.0, 19_000.0, 18_000.0, 5)
    d.process_bar(bar(MONDAY))
    assert d.position.funding == pytest.approx(funding_per_hour(PX, 10, -1, C) / 4)
    assert d.position.funding < 0.0


SCENARIOS = {
    "tp1_then_gap_through_the_stop": (
        [(PX, PX + 5, PX - 5, PX), (PX, 21_100.0, PX - 5, 21_000.0), (18_900.0, 18_950.0, 18_800.0, 18_900.0)],
        None,
        [["ENTRY"], ["TP1"], ["STOP"]],
    ),
    "stop_beats_targets_in_one_bar": ([(PX, 21_100.0, 18_900.0, PX)], None, [["ENTRY", "STOP"]]),
    "tp1_and_tp2_in_one_bar": ([(PX, 22_100.0, PX - 5, 22_000.0)], None, [["ENTRY", "TP1", "TP2"]]),
    "queued_exit_at_the_open": ([(PX, PX + 5, PX - 5, PX), (PX, PX + 5, PX - 5, PX)], 0, [["ENTRY"], ["EXIT"]]),
}


def _replay(broker: PaperBroker, rows, exit_after, step: int) -> tuple[list, float]:
    long_10(broker)
    fills = []
    for k, (o, h, lo, c) in enumerate(rows):
        fills.append(broker.process_bar(bar(MONDAY + k * step, o, h, lo, c)))
        if k == exit_after:
            broker.queue_exit()
    return fills, broker.cash


@pytest.mark.parametrize("name", SCENARIOS)
def test_without_funding_the_fills_are_a_s(name):
    rows, exit_after, want = SCENARIOS[name]
    d_fills, d_cash = _replay(BrokerD(NO_FUNDING, EQUITY), rows, exit_after, M15)
    a_fills, a_cash = _replay(PaperBroker(NO_FUNDING, EQUITY), rows, exit_after, H1)
    assert [kinds(f) for f in d_fills] == want
    assert fill_view(d_fills) == fill_view(a_fills) and d_cash == a_cash


def test_friday_close_is_at_the_true_wall_clock_time():
    d = long_10(BrokerD(C, EQUITY))
    assert kinds(d.process_bar(bar(ny(*FRIDAY, 16, 30)))) == ["ENTRY"]
    at_1645 = ny(*FRIDAY, 16, 45)
    assert candles.futures_closed(at_1645) is True  # A's 1h calendar would stop here
    assert kinds(d.process_bar(bar(at_1645, h=21_100.0))) == ["TP1"]
    for minute in (0, 15, 30, 45):
        assert d.process_bar(bar(ny(*FRIDAY, 17, minute), lo=18_900.0)) == []  # the stop passes while closed
    fills = d.process_bar(bar(ny(*FRIDAY, 18)))
    assert kinds(fills) == ["STOP"] and fills[0].price == slip(PX, -1, C) and fills[0].contracts == 5


def test_a_queued_entry_waits_for_the_reopen():
    d = long_10(BrokerD(C, EQUITY))
    for minute in (0, 15, 30, 45):
        assert d.process_bar(bar(ny(*FRIDAY, 17, minute))) == []
    assert kinds(d.process_bar(bar(ny(*FRIDAY, 18)))) == ["ENTRY"]


def test_funding_keeps_running_while_the_market_is_closed():
    d = long_10(BrokerD(C, EQUITY))
    d.process_bar(bar(ny(*FRIDAY, 16, 45)))
    before = d.position.funding
    for minute in (0, 15, 30, 45):
        d.process_bar(bar(ny(*FRIDAY, 17, minute)))
    assert d.position.funding - before == pytest.approx(funding_per_hour(PX, 10, 1, C))
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratd/test_broker.py -q
```

예상: 수집 오류 `ModuleNotFoundError: No module named 'ewbot.stratd.broker'`.

- [ ] **Step 3: 구현**

**파일 `src/ewbot/stratd/broker.py`**

```python
"""BrokerD: A's PaperBroker on 15m bars (Plan 6 spec 3).

Two things in A's broker assume a 1h bar: the calendar check of process_bar (execution/paper.py:114; a 15m bar opening at
16:15 New York would count as closed) and one hour of funding per bar (187-192). process_bar below is A's with D's
calendar, and _charge_funding charges a quarter of A's hourly funding per bar, as stratb/engine.py:261 does, so the money
per wall-clock hour is A's. A's file is fingerprinted, so this copy cannot drift from it."""

from __future__ import annotations

from ewbot.execution.costs import fee, funding_per_hour, slip
from ewbot.execution.paper import Fill, PaperBroker, PaperPosition
from ewbot.stratd.clock import futures_closed
from ewbot.stratd.config import M15
from ewbot.types import H1, Bar


class BrokerD(PaperBroker):
    def process_bar(self, bar: Bar) -> list[Fill]:
        fills: list[Fill] = []
        if futures_closed(bar.ts, M15):
            pos = self.position
            if pos is not None and self._stop_touched(pos, bar):
                self._stop_triggered_while_closed = True
            self._charge_funding(bar)
            return fills

        # (1) pending market orders at the open
        if self._pending_exit:
            self._pending_exit = False
            if self.position is not None:
                pos = self.position
                price = slip(bar.open, -pos.direction, self.costs)
                fills.append(self._close(bar.ts, "EXIT", price, pos.contracts))
        if self._pending_entry is not None and self.position is None:
            pe = self._pending_entry
            self._pending_entry = None
            price = slip(bar.open, pe.direction, self.costs)
            f = fee(price, pe.contracts, self.costs)
            self.cash -= f
            self.position = PaperPosition(
                direction=pe.direction,
                contracts=pe.contracts,
                entry_price=price,
                entry_ts=bar.ts,
                stop=pe.stop,
                tp1=pe.tp1,
                tp2=pe.tp2,
                tp1_contracts=pe.tp1_contracts,
            )
            fills.append(Fill(bar.ts, "ENTRY", pe.direction, pe.contracts, price, f))

        pos = self.position
        if pos is not None:
            d = pos.direction
            if self._stop_triggered_while_closed or d * (bar.open - pos.stop) <= 0:
                # (2) gap through the stop (or stop passed during the futures close): fill at the open
                fills.append(self._close(bar.ts, "STOP", slip(bar.open, -d, self.costs), pos.contracts))
            elif self._stop_touched(pos, bar):
                # (3a) stop has priority over any TP in the same bar
                fills.append(self._close(bar.ts, "STOP", slip(pos.stop, -d, self.costs), pos.contracts))
            else:
                # (3b) limit TPs need a strict cross
                fav = bar.high if d == 1 else bar.low
                if pos.tp1 is not None and d * (fav - pos.tp1) > 0:
                    tp1 = pos.tp1
                    pos.tp1 = None
                    fills.append(self._close(bar.ts, "TP1", tp1, min(pos.tp1_contracts, pos.contracts)))
                if self.position is not None and pos.tp1 is None and pos.tp2 is not None:
                    if d * (fav - pos.tp2) > 0:
                        fills.append(self._close(bar.ts, "TP2", pos.tp2, pos.contracts))

        # (4) funding for the quarter hour
        self._charge_funding(bar)
        return fills

    def _charge_funding(self, bar: Bar) -> None:
        pos = self.position
        if pos is None:
            return
        cost = funding_per_hour(bar.close, pos.contracts, pos.direction, self.costs) * (M15 / H1)
        self.cash -= cost
        pos.funding += cost
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratd/test_broker.py tests/stratd/test_isolation.py -q
```

예상: `22 passed`(`test_broker.py` 9개, `test_isolation.py` 13개).

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratd/broker.py tests/stratd/test_broker.py
git commit -m "feat(stratd): 15m broker with a quarter of the hourly funding and the true Friday close" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: 15분 엔진과 축소 동등성

**Files:**
- Create: `src/ewbot/stratd/engine.py`
- Test: `tests/stratd/test_engine.py`

**Interfaces:**
- Consumes: `ewbot.engine.loop`: `TradingEngine(cfg, strategy, *, wave_engine=None, choch=None)`와 `_wave_in`, `_choch_in`, `cfg`, `strategy`; `_Run(cfg, strategy, wave, choch, start_ts)`와 `execute(bars, clipped, n_clipped) -> EngineResult`, `_may_enter(bar, t_close) -> bool`, `_on_fill`, `_manage(ws, closed_4h=...)`, `_request_exit`, `_release_cooldown`, `_consider`, `_close_end`; `_agg_4h(sub)`, `EngineResult`, `EquityPoint`, `Trade`. `ewbot.data.candles`: `fill_gaps(df, tf=H1)`, `clip_wicks(df, atr1h, k)`, `to_bars(df)`. `ewbot.indicators.atr_series(high, low, close, n=14)`. `ewbot.strategy.hybrid.make_strategy(sp)`. `ewbot.stratd.waves.WaveEngineD`, `ewbot.stratd.broker.BrokerD`, `ewbot.stratd.clock.entry_blackout(ts, tf)`, `ewbot.stratd.config`: `HOUR_LAST`, `M15`, `TRIGGER_BARS_PER_WAVE`, `base_config()`. `tests.stratd.helpers`: `T_A`, `T_D`, `a_frame()`, `compress()`, `idx_a()`, `idx_d()`, `ny()`.
- Produces:
  - `ewbot.stratd.engine.RunD(_Run)`: `__init__(self, cfg: BotConfig, strategy: Strategy, wave: WaveEngine, choch: ChochDetector, start_ts: int | None) -> None`, `execute(self, bars: list[Bar], clipped: list[Bar], n_clipped: int) -> EngineResult`, `_may_enter(self, bar: Bar, t_close: int) -> bool`.
  - `ewbot.stratd.engine.TradingEngineD(TradingEngine)`: `run(self, df15: pd.DataFrame, start_ts: int | None = None, end_ts: int | None = None) -> EngineResult`.

A의 `_Run`에서 봉 길이를 아는 줄은 `engine/loop.py:146-152`(4시간 묶음), `163`(마감 시각), `177`(진행 곡선), `265`(블랙아웃)이다. `RunD`는 앞의 셋이 있는 `execute`와 뒤의 하나가 있는 `_may_enter`를 덮어쓴다. 나머지(`_on_fill`, `_finish`, `_manage`, `_consider`, `_close_end`)는 봉 길이를 모르므로 물려받는다. `n4h`는 파동 봉 마감을 세므로 시간 손절이 12~60시간이 된다.

- [ ] **Step 1: 실패하는 테스트 작성**

**파일 `tests/stratd/test_engine.py`**

```python
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd
import pytest

from ewbot.backtest.walkforward import config_for
from ewbot.config import BotConfig, CostParams, with_overrides
from ewbot.engine.loop import TradingEngine
from ewbot.execution.costs import funding_per_hour
from ewbot.strategy.base import Signal
from ewbot.strategy.hybrid import make_strategy
from ewbot.stratd.broker import BrokerD
from ewbot.stratd.config import M15, base_config
from ewbot.stratd.engine import RunD, TradingEngineD
from ewbot.types import H1, Bar
from ewbot.waves.engine import WaveState
from tests.stratd.helpers import N_BARS, T_A, T_D, a_frame, compress, idx_a, idx_d, ny

T = ny(2023, 11, 13, 0)  # a Monday, on the hour
FLAT = (50_000.0, 50_100.0, 49_900.0, 50_000.0)
CFG = BotConfig(costs=CostParams(fee_rate=0.0, slippage=0.0, funding_apr_long=0.0))  # 1% of 10,000 over 1,000 = 10 contracts


def ts(i: int, t0: int = T) -> int:
    return t0 + i * M15


def frame15(n: int, rows: dict[int, tuple[float, float, float, float]] | None = None, t0: int = T) -> pd.DataFrame:
    rows = rows or {}
    data = [rows.get(i, FLAT) for i in range(n)]
    return pd.DataFrame(
        {
            "ts": pd.Series([ts(i, t0) for i in range(n)], dtype="int64"),
            "open": [r[0] for r in data],
            "high": [r[1] for r in data],
            "low": [r[2] for r in data],
            "close": [r[3] for r in data],
            "volume": [1.0] * n,
        }
    )


def sig(at: int, **kw: Any) -> Signal:
    base: dict[str, Any] = dict(ts=at, setup="W3", direction=1, entry_ref=50_000.0, stop=49_000.0, tp1=51_000.0,
                                tp2=52_000.0, risk_mult=1.0, time_stop_4h=None, instance=None, lineage=None, reason="fake")
    base.update(kw)
    return Signal(**base)


class StubWave:
    """Scripted wave engine: default WaveState, records what it is fed."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, Bar | None, tuple[Bar, ...] | None]] = []

    def on_1h_close(self, bar15: Bar, bar1h: Bar | None, sub_bars: Any) -> WaveState:
        self.calls.append((bar15.ts, bar1h, tuple(sub_bars) if sub_bars is not None else None))
        return WaveState(
            ts=bar15.ts, ready=True, counts=(), probs=(), top=None, q_top=0.0, s_rev=0.0, lineages=frozenset(),
            E_live=bar15.close, E_live_ts=bar15.ts, leg_dir=1, leg_start=None, pivots4h=(), new_pivot_4h=None,
            atr4h=400.0, closed_4h=bar1h is not None, i4h=0,
        )


class NullChoch:
    def update(self, bar: Bar) -> None:
        return None


class FakeStrategy:
    name = "FAKE"

    def __init__(self, plan: dict[int, Signal]) -> None:
        self.plan = plan
        self.calls: list[int] = []

    def on_bar(self, bar: Bar, ws: WaveState, choch: Any) -> Signal | None:
        self.calls.append(bar.ts)
        return self.plan.get(bar.ts)


def run(df: pd.DataFrame, plan: dict[int, Signal], cfg: BotConfig = CFG, start_ts: int | None = None):
    strat, wave = FakeStrategy(plan), StubWave()
    res = TradingEngineD(cfg, strat, wave_engine=wave, choch=NullChoch()).run(df, start_ts=start_ts)
    return res, strat, wave


# ----------------------------------------------------------------------------- 15m bars


def test_a_s_engine_raises_on_15m_bars():
    """Spec 3 says fill_gaps' hourly default drops 3/4 of them; it raises. Either way D must not go through it."""
    with pytest.raises(ValueError):
        TradingEngine(CFG, FakeStrategy({}), wave_engine=StubWave(), choch=NullChoch()).run(frame15(8))


def test_every_15m_bar_reaches_the_wave_engine_and_four_make_a_1h_bar():
    rows = {0: (100.0, 110.0, 95.0, 105.0), 1: (105.0, 130.0, 100.0, 120.0), 2: (120.0, 125.0, 90.0, 92.0),
            3: (92.0, 99.0, 91.0, 98.0)}
    _, _, wave = run(frame15(8, rows), {})
    assert [c[0] for c in wave.calls] == [ts(i) for i in range(8)]
    assert [c[1] is not None for c in wave.calls] == [False, False, False, True] * 2
    assert wave.calls[3][1] == Bar(T, 100.0, 130.0, 90.0, 98.0, 4.0, False)
    assert [b.ts for b in wave.calls[3][2]] == [ts(i) for i in range(4)]


def test_a_missing_15m_bar_is_filled_not_dropped():
    _, _, wave = run(frame15(8).drop(index=[2]).reset_index(drop=True), {})
    assert [c[0] for c in wave.calls] == [ts(i) for i in range(8)]
    assert [b.synthetic for b in wave.calls[3][2]] == [False, False, True, False]
    assert wave.calls[3][1].synthetic is True


def test_an_hour_the_data_enters_midway_makes_no_1h_bar():
    _, _, wave = run(frame15(7, t0=T + M15), {})
    assert [c[1] is not None for c in wave.calls] == [False] * 6 + [True]
    assert wave.calls[6][1].ts == T + H1


def test_bars_off_the_15m_grid_are_refused():
    with pytest.raises(ValueError, match="15-minute"):
        run(frame15(8, t0=T + 60), {})


def test_the_run_uses_d_s_broker():
    assert isinstance(RunD(CFG, FakeStrategy({}), StubWave(), NullChoch(), None).broker, BrokerD)


# ----------------------------------------------------------------------------- equity curve


def test_equity_points_are_hourly_and_marked_at_the_hour_s_close():
    rows = {7: (50_000.0, 50_150.0, 49_900.0, 50_100.0), 11: (50_000.0, 50_250.0, 49_900.0, 50_200.0)}
    res, _, _ = run(frame15(12, rows), {ts(2): sig(ts(2))})
    assert [p.ts for p in res.equity] == [T, T + H1, T + 2 * H1]
    assert [p.equity for p in res.equity] == pytest.approx([10_000.0, 10_010.0, 10_020.0])


def test_start_ts_starts_the_curve_at_that_hour():
    res, _, _ = run(frame15(12), {}, start_ts=ts(4))
    assert [p.ts for p in res.equity] == [T + H1, T + 2 * H1]


def test_data_ending_mid_hour_still_books_the_last_close():
    res, _, _ = run(frame15(14, {13: (50_000.0, 50_250.0, 49_900.0, 50_200.0)}), {ts(2): sig(ts(2))})
    assert [p.ts for p in res.equity] == [T, T + H1, T + 2 * H1, T + 3 * H1]
    assert res.equity[-1].equity == pytest.approx(10_020.0) and res.trades[0].exit_reason == "END"


# ----------------------------------------------------------------------------- orders and wall-clock rules


def test_a_signal_fills_at_the_next_15m_open_and_ends_at_the_last_close():
    rows = {3: (50_050.0, 50_100.0, 49_900.0, 50_000.0), 11: (50_000.0, 50_250.0, 49_900.0, 50_200.0)}
    res, _, _ = run(frame15(12, rows), {ts(2): sig(ts(2))})
    [t] = res.trades
    assert (t.entry_ts, t.exit_ts, t.contracts, t.exit_reason) == (ts(3), ts(11), 10, "END")
    assert t.entry_price == 50_050.0 and t.exit_price_avg == 50_200.0
    assert t.pnl == pytest.approx(15.0)


def test_a_time_stop_of_12_bars_is_12_wall_clock_hours():
    res, _, _ = run(frame15(60), {ts(3): sig(ts(3), time_stop_4h=12)})
    [t] = res.trades
    assert (t.entry_ts, t.exit_ts, t.exit_reason) == (ts(4), ts(52), "TIME")
    assert t.exit_ts - t.entry_ts == 12 * H1


def test_a_trade_pays_funding_per_wall_clock_hour():
    costs = CostParams(fee_rate=0.0, slippage=0.0)
    res, _, _ = run(frame15(20), {ts(3): sig(ts(3))}, cfg=BotConfig(costs=costs))
    [t] = res.trades
    assert (t.entry_ts, t.exit_ts) == (ts(4), ts(19))  # 16 bars held = 4 hours
    assert t.funding == pytest.approx(4 * funding_per_hour(50_000.0, 10, 1, costs))


def test_entries_are_decided_by_the_true_friday_blackout():
    day = (2023, 11, 17)
    _, strat, _ = run(frame15(20, t0=ny(*day, 14)), {})  # 14:00 .. 18:45 New York
    allowed = [(14, 0), (14, 15), (14, 30), (14, 45), (15, 0), (15, 15), (15, 30), (18, 0), (18, 15), (18, 30)]
    assert strat.calls == [ny(*day, h, m) for h, m in allowed]  # 15:45 fills at 16:00; 18:45 is the last bar


# ----------------------------------------------------------------------------- rescale equivalence (spec 6)

COMBOS = [(3.0, 0.6, 0.25), (2.0, 0.5, 0.0)]
START_I = 1_200  # a multiple of 4: both runs start at the same bar, on an hour for D


@pytest.fixture
def no_friday(monkeypatch):
    """Spec 6's "Friday windows excluded". The path is longer than a week, so both calendars are switched off here;
    the calendar itself is tested above, in test_clock.py and in test_broker.py."""
    monkeypatch.setattr("ewbot.engine.loop.entry_blackout", lambda ts: False)
    monkeypatch.setattr("ewbot.execution.paper.futures_closed", lambda ts: False)
    monkeypatch.setattr("ewbot.stratd.engine.entry_blackout", lambda ts, tf: False)
    monkeypatch.setattr("ewbot.stratd.broker.futures_closed", lambda ts, tf: False)


def _config(combo: tuple[float, ...]) -> BotConfig:
    """Spec 6's zero funding, and the breakers off: their New York days and 24h pause are wall-clock rules too."""
    return config_for(with_overrides(base_config(), **{"costs.funding_apr_long": 0.0, "breakers.enabled": False}), combo)


def _signal(s: Signal, idx: Callable[[int], int]) -> tuple:
    return (idx(s.ts), s.setup, s.direction, s.entry_ref, s.stop, s.tp1, s.tp2, s.risk_mult, s.time_stop_4h, s.instance,
            s.lineage, s.reason)


def _trade(t: Any, idx: Callable[[int], int]) -> tuple:
    snap = t.snapshot
    counts = [(c["pattern"], c["d"], c["phase"], c["q"], c["p"], [(idx(sub_ts), px) for sub_ts, px in c["points"]])
              for c in snap["counts"]]
    return (idx(t.entry_ts), idx(t.exit_ts), t.setup, t.direction, t.contracts, t.entry_price, t.exit_price_avg, t.stop0,
            t.tp1, t.tp2, t.pnl, t.fees, t.funding, t.r_multiple, t.exit_reason, idx(snap["ts"]), snap["q_top"],
            snap["s_rev"], snap["E_live"], snap["reason"], counts)


@pytest.mark.parametrize("combo", COMBOS)
def test_a_s_path_compressed_in_time_gives_a_s_signals_bar_positions_and_prices(no_friday, combo):
    cfg = _config(combo)
    df = a_frame()
    a = TradingEngine(cfg, make_strategy(cfg.strategy)).run(df, T_A + START_I * H1)
    d = TradingEngineD(cfg, make_strategy(cfg.strategy)).run(compress(df), T_D + START_I * M15)
    assert len(a.signals) >= 3 and len(a.trades) >= 3  # the comparison below is not empty
    assert [_signal(s, idx_d) for s in d.signals] == [_signal(s, idx_a) for s in a.signals]
    assert [_trade(t, idx_d) for t in d.trades] == [_trade(t, idx_a) for t in a.trades]
    assert [(idx_d(t), e) for t, e in d.events] == [(idx_a(t), e) for t, e in a.events]
    # D's hourly points are A's points on every 4th bar; N_BARS is a multiple of 4, so the last bar closes an hour
    assert N_BARS % 4 == 0
    closes = [p for p in a.equity if idx_a(p.ts) % 4 == 3]
    assert [p.equity for p in d.equity] == [p.equity for p in closes]
    assert [idx_d(p.ts) for p in d.equity] == [idx_a(p.ts) - 3 for p in closes]
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratd/test_engine.py -q
```

예상: 수집 오류 `ModuleNotFoundError: No module named 'ewbot.stratd.engine'`.

- [ ] **Step 3: 구현**

**파일 `src/ewbot/stratd/engine.py`**

```python
"""Strategy D's engine (Plan 6 spec 3): A's loop on 15m trigger bars and 1h wave bars, at the real wall-clock times.

The timestamps are never relabelled: funding, the Friday windows and the breakers' New York days stay on the real clock.
Compared with engine/loop.py, TradingEngineD.run fills gaps on the 15m grid, and RunD changes the four places that know a
bar length: the wave-bar bucket (loop.py:146-152), the close instant (163), the equity point (177) and the blackout check
(265). The trigger bar is the 15m bar, so the ATR of the wick clip is the 15m ATR. Everything else is inherited.

Equity points are hourly, so A's metrics (annualised with 8760 hours) apply unchanged: EquityPoint(hour open ts, equity at
the close of the hour's last 15m bar). The last bar of the data also writes one, so an END close inside an hour is booked.
The breakers are observed at every 15m close, as A observes them at every trigger bar."""

from __future__ import annotations

import pandas as pd

from ewbot.config import BotConfig
from ewbot.data.candles import clip_wicks, fill_gaps, to_bars
from ewbot.engine.loop import EngineResult, EquityPoint, TradingEngine, _agg_4h, _Run
from ewbot.indicators import atr_series
from ewbot.strategy.base import Strategy
from ewbot.strategy.choch import ChochDetector
from ewbot.stratd.broker import BrokerD
from ewbot.stratd.clock import entry_blackout
from ewbot.stratd.config import HOUR_LAST, M15, TRIGGER_BARS_PER_WAVE
from ewbot.stratd.waves import WaveEngineD
from ewbot.types import H1, Bar
from ewbot.waves.engine import WaveEngine


class RunD(_Run):
    def __init__(
        self,
        cfg: BotConfig,
        strategy: Strategy,
        wave: WaveEngine,
        choch: ChochDetector,
        start_ts: int | None,
    ) -> None:
        super().__init__(cfg, strategy, wave, choch, start_ts)
        self.broker = BrokerD(cfg.costs, cfg.initial_equity)

    def execute(self, bars: list[Bar], clipped: list[Bar], n_clipped: int) -> EngineResult:
        """bars: raw 15m bars (broker, marks, END close); clipped: the same bars after clip_wicks
        (1h aggregation, wave engine, trigger, strategy). Same length and ts."""
        self.events.append((bars[0].ts, f"WICKS_CLIPPED:{n_clipped}"))
        bucket: list[Bar] = []
        last = len(bars) - 1
        for idx, (bar, cbar) in enumerate(zip(bars, clipped)):
            if bucket and bucket[0].ts // H1 != cbar.ts // H1:
                bucket = []
            bucket.append(cbar)
            bar1h: Bar | None = None
            sub: tuple[Bar, ...] | None = None
            closes_hour = cbar.ts % H1 == HOUR_LAST
            if closes_hour:
                if len(bucket) == TRIGGER_BARS_PER_WAVE:
                    sub = tuple(bucket)
                    bar1h = _agg_4h(sub)
                bucket = []

            for f in self.broker.process_bar(bar):  # 1-2 (raw prices)
                self._on_fill(f)
            ws = self.wave.on_1h_close(cbar, bar1h, sub)  # 3
            self.choch.update(cbar)  # 4
            self._manage(ws, closed_4h=bar1h is not None)  # 5

            t_close = bar.ts + M15  # 6
            for ev in self.breakers.on_equity(t_close, self.broker.equity(bar.close)):
                self.events.append((bar.ts, ev))
            if self.breakers.halted:
                self._request_exit("BREAKER")

            self._release_cooldown(ws)  # 7
            if idx < last and self._may_enter(bar, t_close):
                sig = self.strategy.on_bar(cbar, ws, self.choch)
                if sig is not None:
                    self._consider(sig, bar, ws)

            if idx == last:  # 8
                self._close_end(bar)
            hour_open = bar.ts - bar.ts % H1  # 9
            if (closes_hour or idx == last) and (self.start_ts is None or hour_open >= self.start_ts):
                self.equity.append(EquityPoint(hour_open, self.broker.equity(bar.close)))

        return EngineResult(tuple(self.trades), tuple(self.equity), tuple(self.signals), tuple(self.events))

    def _may_enter(self, bar: Bar, t_close: int) -> bool:
        if self.start_ts is not None and bar.ts < self.start_ts:
            return False
        if self.pending is not None:
            return False
        if not self.breakers.can_enter(t_close):
            return False
        return not (entry_blackout(bar.ts, M15) or entry_blackout(bar.ts + M15, M15))


class TradingEngineD(TradingEngine):
    def run(self, df15: pd.DataFrame, start_ts: int | None = None, end_ts: int | None = None) -> EngineResult:
        if len(df15) and (df15["ts"] % M15 != 0).any():
            raise ValueError("every bar must open on a 15-minute boundary")
        df = fill_gaps(df15, tf=M15)  # A's default tf is 1h and raises on 15m bars
        if end_ts is not None:
            df = df[df["ts"] < end_ts]
        df = df.reset_index(drop=True)
        if df.empty:
            return EngineResult((), (), (), ())
        atr15 = atr_series(
            df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), self.cfg.wave.atr_len
        )
        clipped, n_clipped = clip_wicks(df, atr15, self.cfg.wave.wick_clip_atr1h)
        wave = self._wave_in if self._wave_in is not None else WaveEngineD(self.cfg.wave)
        choch = (
            self._choch_in
            if self._choch_in is not None
            else ChochDetector(self.cfg.strategy.choch_bars)
        )
        run = RunD(self.cfg, self.strategy, wave, choch, start_ts)
        return run.execute(to_bars(df), to_bars(clipped), n_clipped)
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratd/test_engine.py tests/stratd/test_isolation.py -q
```

예상: `28 passed`(`test_engine.py` 15개, `test_isolation.py` 13개).

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratd/engine.py tests/stratd/test_engine.py
git commit -m "feat(stratd): 15m engine with hourly equity points; A's test path compressed in time gives A's signals, bars and prices" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: 데이터 읽기, 횟수 작업자, 게이트, 보고서

**Files:**
- Create: `src/ewbot/stratd/data.py`
- Create: `src/ewbot/stratd/count.py`
- Test: `tests/stratd/test_data.py`
- Test: `tests/stratd/test_count.py`

**Interfaces:**
- Consumes: `ewbot.stratb.data15`: `cache_path(cache_dir: Path, product: str) -> Path`, `load_15m(path: Path, *, include_holdout: bool = False, lock_path: Path = ...) -> pd.DataFrame`(`include_holdout`는 넘기지 않는다). `ewbot.backtest.walkforward`: `GRID`, `WF_START = "2017-01-01"`, `WF_END = "2024-01-01"`, `HOLDOUT_START = 1704067200`, `Combo`, `grid_combos() -> list[Combo]`, `config_for(cfg, combo) -> BotConfig`. `ewbot.backtest.runner.utc_ts(date: str) -> int`. `ewbot.config.with_overrides`. `ewbot.strategy.hybrid.make_strategy`. `ewbot.stratd.engine.TradingEngineD`. `ewbot.stratd.config`: `M15`, `base_config()`. `ewbot.engine.loop`: `EngineResult`, `Trade`(테스트). `tests.stratd.helpers`: `a_frame()`, `compress()`.
- Produces:
  - `ewbot.stratd.data`: `PRODUCT = "BTC-USD"`, `class DataWindowError(ValueError)`, `cache_file(cache_dir: Path) -> Path`, `refuse_2024(df15: pd.DataFrame) -> None`, `check_coverage(df15: pd.DataFrame) -> None`, `load_btc_15m(cache_dir: Path) -> pd.DataFrame`.
  - `ewbot.stratd.count`: `MONTHS = 84`, `THRESHOLD = 4.0`, `FALLBACK_Q_MIN = 0.5`, `FALLBACK_S_MIN = 0.0`, `REPORT_KIND = "ewbot-stratd-count"`, `PROCEED_GRID = "PROCEED_GRID"`, `PROCEED_FALLBACK = "PROCEED_FALLBACK"`, `STOP = "STOP"`, `START_TS`, `END_TS`, `ByYear = dict[int, dict[str, int]]`, `tally_entries(trades: Iterable[Any]) -> ByYear`, `_setup_worker(df15: pd.DataFrame) -> None`, `_count_combo(combo: Combo) -> tuple[Combo, ByYear]`, `run_count(df15: pd.DataFrame, workers: int = 1, combos: Sequence[Combo] | None = None) -> dict[Combo, ByYear]`, `median_monthly(totals: Iterable[int]) -> float`, `fallback_combos() -> list[Combo]`, `decide(totals: Mapping[Combo, int]) -> dict[str, Any]`, `build_report(results: Mapping[Combo, ByYear], df15: pd.DataFrame) -> dict[str, Any]`, `report_lines(rep: Mapping[str, Any]) -> list[str]`, `count_stage(df15: pd.DataFrame, workers: int) -> dict[str, Any]`.

- [ ] **Step 1: 실패하는 테스트 작성**

**파일 `tests/stratd/test_data.py`**

```python
from __future__ import annotations

import pandas as pd
import pytest

from ewbot.backtest.walkforward import HOLDOUT_START as HS
from ewbot.stratd.config import M15
from ewbot.stratd.data import DataWindowError, cache_file, check_coverage, load_btc_15m, refuse_2024

START = 1_483_228_800  # 2017-01-01 00:00 UTC
LAST = HS - M15  # 2023-12-31 23:45 UTC


def frame(ts: list[int]) -> pd.DataFrame:
    prices = {c: 1.0 for c in ("open", "high", "low", "close", "volume")}
    return pd.DataFrame({"ts": pd.Series(ts, dtype="int64"), **prices})


def test_the_cache_file_is_the_shared_btc_15m_cache(tmp_path):
    assert cache_file(tmp_path) == tmp_path / "BTC-USD_15m.parquet"


def test_the_read_never_returns_a_bar_from_2024_on(tmp_path):
    frame([HS - 2 * M15, HS - M15, HS, HS + M15]).to_parquet(cache_file(tmp_path), index=False)
    assert load_btc_15m(tmp_path)["ts"].tolist() == [HS - 2 * M15, HS - M15]


def test_a_frame_with_a_2024_bar_is_refused():
    refuse_2024(frame([HS - M15]))
    refuse_2024(frame([]))
    with pytest.raises(DataWindowError, match="2024"):
        refuse_2024(frame([HS - M15, HS]))


def test_coverage_needs_the_whole_window():
    check_coverage(frame([START, LAST]))
    for ts in ([START + M15, LAST], [START, LAST - M15], []):
        with pytest.raises(DataWindowError, match="2017-01-01"):
            check_coverage(frame(ts))
```

**파일 `tests/stratd/test_count.py`**

```python
from __future__ import annotations

import ast
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from ewbot.backtest.runner import utc_ts
from ewbot.backtest.walkforward import GRID, HOLDOUT_START, config_for, grid_combos
from ewbot.config import with_overrides
from ewbot.engine.loop import EngineResult, Trade
from ewbot.strategy.hybrid import make_strategy
from ewbot.stratd import count
from ewbot.stratd.config import M15, base_config
from ewbot.stratd.data import DataWindowError
from ewbot.stratd.engine import TradingEngineD
from tests.stratd.helpers import a_frame, compress

FRAME = pd.DataFrame({"ts": pd.Series([utc_ts("2017-01-01"), HOLDOUT_START - M15], dtype="int64")})


def fake_trade(entry_ts: int, setup: str) -> Trade:
    return Trade(entry_ts=entry_ts, exit_ts=entry_ts + 900, setup=setup, direction=1, contracts=1, entry_price=100.0,
                 exit_price_avg=101.0, stop0=95.0, tp1=105.0, tp2=110.0, pnl=12.0, fees=0.1, funding=0.2, r_multiple=1.2,
                 exit_reason="STOP", snapshot={})


def totals(n: int) -> dict[tuple[float, ...], int]:
    return {c: n for c in grid_combos()}


def results(n: int, fallback: int | None = None) -> dict[tuple[float, ...], dict[int, dict[str, int]]]:
    out = {c: {2019: {"W3": n}} for c in grid_combos()}
    if fallback is not None:
        out.update({c: {2019: {"W3": fallback}} for c in count.fallback_combos()})
    return out


# ----------------------------------------------------------------------------- counting


def test_the_fixed_values():
    assert (count.MONTHS, count.THRESHOLD) == (84, 4.0)
    assert (count.START_TS, count.END_TS) == (utc_ts("2017-01-01"), utc_ts("2024-01-01"))
    assert len(grid_combos()) == 4 * 5 * 3 == 60


def test_tally_groups_by_the_utc_year_of_the_entry_and_the_setup():
    boundary = utc_ts("2018-01-01")
    trades = [fake_trade(boundary - M15, "W3"), fake_trade(boundary, "W3"), fake_trade(boundary, "C_END"),
              fake_trade(boundary + M15, "W3")]
    assert count.tally_entries(trades) == {2017: {"W3": 1}, 2018: {"W3": 2, "C_END": 1}}


def test_the_worker_runs_breakers_off_over_the_window_and_returns_counts_only(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_run(self, df15, start_ts, end_ts):
        seen.update(cfg=self.cfg, start=start_ts, end=end_ts, df=df15)
        trades = (fake_trade(utc_ts("2019-03-01"), "W3"), fake_trade(utc_ts("2019-04-01"), "W3"),
                  fake_trade(utc_ts("2021-05-01"), "C_END"))
        return EngineResult(trades, (), (), ())

    monkeypatch.setattr(count.TradingEngineD, "run", fake_run)
    marker = object()
    count._setup_worker(marker)
    combo = (2.5, 0.55, 0.25)
    assert count._count_combo(combo) == (combo, {2019: {"W3": 2}, 2021: {"C_END": 1}})
    cfg = seen["cfg"]
    assert cfg.breakers.enabled is False and (cfg.wave.K4, cfg.strategy.Q_MIN, cfg.strategy.S_MIN) == combo
    assert (seen["start"], seen["end"]) == (utc_ts("2017-01-01"), utc_ts("2024-01-01")) and seen["df"] is marker


def test_a_frame_reaching_2024_is_refused_before_any_run():
    with pytest.raises(DataWindowError, match="2024"):
        count.run_count(pd.DataFrame({"ts": [HOLDOUT_START]}), combos=[(2.0, 0.5, 0.0)])


def test_a_cache_that_does_not_cover_the_window_is_refused_before_any_run():
    with pytest.raises(DataWindowError, match="2017-01-01"):
        count.count_stage(pd.DataFrame({"ts": [HOLDOUT_START - M15]}), workers=1)


def test_counts_are_the_entries_of_one_engine_run():
    df = compress(a_frame())
    combo = (3.0, 0.6, 0.25)
    got = count.run_count(df, combos=[combo])
    cfg = config_for(with_overrides(base_config(), **{"breakers.enabled": False}), combo)
    res = TradingEngineD(cfg, make_strategy(cfg.strategy)).run(df, count.START_TS, count.END_TS)
    want = Counter((datetime.fromtimestamp(t.entry_ts, UTC).year, t.setup) for t in res.trades)
    assert sum(want.values()) >= 3
    assert Counter({(y, s): n for y, by_setup in got[combo].items() for s, n in by_setup.items()}) == want


def test_two_worker_processes_engine_counts_match_one_process():
    df = compress(a_frame()).iloc[:2_000]
    combos = [(2.0, 0.5, 0.0), (3.0, 0.6, 0.25)]
    assert count.run_count(df, workers=2, combos=combos) == count.run_count(df, workers=1, combos=combos)


# ----------------------------------------------------------------------------- the gate


def test_the_fallback_combos_are_among_the_60():
    fb = count.fallback_combos()
    assert fb == [(2.0, 0.5, 0.0), (2.5, 0.5, 0.0), (3.0, 0.5, 0.0), (4.0, 0.5, 0.0)]
    assert set(fb) <= set(grid_combos())


def test_a_median_of_exactly_four_per_month_proceeds_with_the_grid():
    out = count.decide(totals(336))
    assert out["primary"] == {"n_combos": 60, "median_monthly": 4.0, "passed": True}
    assert out["verdict"] == count.PROCEED_GRID and out["grid"] == grid_combos() and out["fallback"] is None


def test_an_even_median_cannot_fall_a_hair_below_four():
    t = {c: (335 if k < 30 else 337) for k, c in enumerate(grid_combos())}
    assert count.median_monthly(t.values()) == 4.0
    assert count.decide(t)["verdict"] == count.PROCEED_GRID


def test_below_four_the_fallback_decides_and_becomes_the_grid():
    t = totals(335)
    t.update({c: 336 for c in count.fallback_combos()})
    out = count.decide(t)
    assert out["primary"]["passed"] is False and out["primary"]["median_monthly"] == 335 / 84
    assert out["fallback"]["passed"] is True and out["fallback"]["median_monthly"] == 4.0
    assert (out["fallback"]["q_min"], out["fallback"]["s_min"]) == (0.5, 0.0)
    assert out["verdict"] == count.PROCEED_FALLBACK and out["grid"] == count.fallback_combos()


def test_the_fallback_median_is_over_its_four_combos():
    t = totals(0)
    t.update(dict(zip(count.fallback_combos(), (300, 330, 342, 400))))
    assert count.decide(t)["fallback"]["median_monthly"] == 4.0


def test_both_below_four_stop():
    out = count.decide(totals(335))
    assert out["primary"]["passed"] is False and out["fallback"]["passed"] is False
    assert out["verdict"] == count.STOP and out["grid"] is None


def test_the_gate_takes_the_60_combos_only():
    with pytest.raises(ValueError, match="60"):
        count.decide({(2.0, 0.5, 0.0): 500})


# ----------------------------------------------------------------------------- the report


def test_the_report_has_per_combo_counts_the_median_and_the_verdict():
    res = results(84)
    res[(2.0, 0.5, 0.0)] = {2023: {"W5": 2}, 2017: {"W3": 100, "C_END": 8}}
    rep = count.build_report(res, FRAME)
    row = next(r for r in rep["combos"] if r["combo"] == [2.0, 0.5, 0.0])
    assert row == {"combo": [2.0, 0.5, 0.0], "entries": 110, "monthly": 110 / 84,
                   "by_year": {"2017": {"C_END": 8, "W3": 100}, "2023": {"W5": 2}}}
    assert list(row["by_year"]) == ["2017", "2023"]
    assert len(rep["combos"]) == 60 and rep["keys"] == list(GRID)
    assert rep["kind"] == count.REPORT_KIND
    assert rep["window"] == {"start": "2017-01-01", "end": "2024-01-01", "months": 84}
    assert rep["data"] == {"first_ts": int(FRAME["ts"].iloc[0]), "last_ts": HOLDOUT_START - M15, "rows": 2}
    assert rep["primary"]["median_monthly"] == 1.0 and rep["verdict"] == count.STOP
    json.dumps(rep)


def test_the_printed_lines_show_each_combo_the_median_and_the_verdict():
    lines = count.report_lines(count.build_report(results(336), FRAME))
    assert "K4=2.0 Q_MIN=0.5 S_MIN=0.0: 336 entries, 4.00 per month" in lines
    assert sum(line.startswith("K4=") for line in lines) == 60
    assert any("median of 60 combos = 4.000 per month" in line for line in lines)
    assert "verdict: PROCEED_GRID" in lines


# ----------------------------------------------------------------------------- no performance value (spec 5.1, 6)

ALLOWED_KEYS = {"kind", "window", "start", "end", "months", "data", "first_ts", "last_ts", "rows", "keys", "combos", "combo",
                "entries", "monthly", "by_year", "threshold", "primary", "n_combos", "median_monthly", "passed", "fallback",
                "q_min", "s_min", "verdict", "grid", "W3", "W5", "W5_REV", "C_END"}
PERFORMANCE_WORDS = re.compile(
    r"\b(pnl|profit|loss|returns?|sharpe|sortino|calmar|drawdown|max_dd|cagr|win_rate|avg_r|r_multiple|equity|exposure|"
    r"exit_reason)\b", re.I)
PERFORMANCE_MODULES = {"metrics", "report", "benchmarks", "holdout"}
PERFORMANCE_NAMES = {"compute_metrics", "evaluate_criteria", "trade_stats", "bootstrap_ci", "breakdowns", "reliability_by_q",
                     "Metrics", "run_backtest", "compare", "evaluate_folds", "walk_forward"}
RESULT_ATTRS = {"pnl", "r_multiple", "fees", "funding", "exit_reason", "exit_ts", "exit_price_avg", "entry_price", "contracts",
                "direction", "stop0", "tp1", "tp2", "snapshot", "equity", "cash", "signals", "events"}


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
        "t.r_multiple",
    ],
)
def test_the_performance_guard_catches(src):
    assert _performance_refs(ast.parse(src))


def test_count_py_refers_to_no_performance_code_and_no_trade_result():
    """Direct references only. backtest.walkforward and stratb.data15 load backtest.metrics as their own import, which
    cannot change without editing those files; the next test shows a run never calls it."""
    tree = ast.parse(Path(count.__file__).read_text(encoding="utf-8"))
    assert _performance_refs(tree) == []


def test_a_count_engine_run_never_calls_the_metrics(monkeypatch):
    def boom(*args: Any, **kw: Any) -> None:
        raise AssertionError("the count called a performance function")

    monkeypatch.setattr("ewbot.backtest.metrics.compute_metrics", boom)
    monkeypatch.setattr("ewbot.backtest.walkforward.compute_metrics", boom)
    count.run_count(compress(a_frame()).iloc[:2_000], combos=[(2.0, 0.5, 0.0)])


def _keys(o: Any) -> set[str]:
    if isinstance(o, dict):
        return set(o) | {k for v in o.values() for k in _keys(v)}
    if isinstance(o, (list, tuple)):
        return {k for v in o for k in _keys(v)}
    return set()


@pytest.mark.parametrize(
    "res",
    [results(336), results(335, fallback=336), results(335)],
    ids=["grid", "fallback", "stop"],
)
def test_the_report_and_the_printed_lines_hold_no_performance_value(res):
    rep = count.build_report(res, FRAME)
    years = {k for k in _keys(rep) if re.fullmatch(r"\d{4}", k)}
    assert _keys(rep) - years <= ALLOWED_KEYS
    assert not PERFORMANCE_WORDS.search(json.dumps(rep))
    assert not PERFORMANCE_WORDS.search("\n".join(count.report_lines(rep)))
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratd/test_data.py tests/stratd/test_count.py -q
```

예상: 수집 오류 `ModuleNotFoundError: No module named 'ewbot.stratd.data'`와 `No module named 'ewbot.stratd.count'`.

- [ ] **Step 3: 구현**

**파일 `src/ewbot/stratd/data.py`**

```python
"""Strategy D's bars for stage 1 (Plan 6 spec 4): the shared BTC 15m cache, before 2024 only.

The file is B's cache and is read through B's load_15m, whose parquet read filters out every bar at/after 2024-01-01.
Nothing here opens 2024: the guard bound to holdout-d.lock belongs to a later plan, written only if the gate passes."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ewbot.backtest.runner import utc_ts
from ewbot.backtest.walkforward import HOLDOUT_START, WF_START
from ewbot.stratb.data15 import cache_path, load_15m
from ewbot.stratd.config import M15

PRODUCT = "BTC-USD"
_FIRST_TS = utc_ts(WF_START)
_LAST_TS = HOLDOUT_START - M15


class DataWindowError(ValueError):
    """The bars reach 2024, or do not cover the count's window."""


def cache_file(cache_dir: Path) -> Path:
    return cache_path(Path(cache_dir), PRODUCT)


def refuse_2024(df15: pd.DataFrame) -> None:
    if len(df15) and int(df15["ts"].max()) >= HOLDOUT_START:
        raise DataWindowError("the bars reach 2024-01-01: strategy D's stage 1 reads nothing from 2024 on")


def check_coverage(df15: pd.DataFrame) -> None:
    """The gate divides by 84 months and a failed gate ends strategy D for good, so a short cache must not reach it."""
    first = int(df15["ts"].iloc[0]) if len(df15) else None
    last = int(df15["ts"].iloc[-1]) if len(df15) else None
    if first != _FIRST_TS or last != _LAST_TS:
        raise DataWindowError(
            f"the cache must run from 2017-01-01 00:00 to 2023-12-31 23:45 UTC, got ts {first} .. {last}: "
            "the count needs the whole window"
        )


def load_btc_15m(cache_dir: Path) -> pd.DataFrame:
    df = load_15m(cache_file(cache_dir))
    refuse_2024(df)
    return df
```

**파일 `src/ewbot/stratd/count.py`**

```python
"""Stage 1 of strategy D (Plan 6 spec 5.1): how many trades, and nothing else.

Every combo runs once through the engine over [2017-01-01, 2024-01-01) with the breakers off, as in the walk-forward: one
position at a time, flips and cooldowns depend on the path, so counting signals would be wrong. A worker reads the entry
time and the setup of each trade and returns counts by UTC year and setup. It never reads what a trade made or the equity
curve, and this module refers to no performance code (backtest.walkforward and stratb.data15 load backtest.metrics as
their own import; nothing here calls it).

The gate: a median over the 60 combos of at least 4.0 entries per month proceeds with the grid. Otherwise the four combos
with Q_MIN 0.5 and S_MIN 0.0, which are among the 60, decide: a median of at least 4.0 proceeds with those four. Otherwise
strategy D ends. The window and the threshold are constants, not options.

Run it through `ewbot stratd count`: the workers are spawned, which needs the main guard of that entry point."""

from __future__ import annotations

import multiprocessing as mp
import statistics
import sys
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from ewbot.backtest.runner import utc_ts
from ewbot.backtest.walkforward import GRID, WF_END, WF_START, Combo, config_for, grid_combos
from ewbot.config import with_overrides
from ewbot.strategy.hybrid import make_strategy
from ewbot.stratd.config import base_config
from ewbot.stratd.data import check_coverage, refuse_2024
from ewbot.stratd.engine import TradingEngineD

MONTHS = 84  # 2017-01 .. 2023-12
THRESHOLD = 4.0  # entries per month, median over the combos
FALLBACK_Q_MIN = 0.5
FALLBACK_S_MIN = 0.0
REPORT_KIND = "ewbot-stratd-count"
PROCEED_GRID = "PROCEED_GRID"
PROCEED_FALLBACK = "PROCEED_FALLBACK"
STOP = "STOP"
START_TS = utc_ts(WF_START)
END_TS = utc_ts(WF_END)
_NEXT = {
    PROCEED_GRID: "next: report to the user; stage 2 gets a separate plan, on the 60-combo grid",
    PROCEED_FALLBACK: "next: report to the user; stage 2 gets a separate plan, on these 4 combos",
    STOP: "spec 5.1: strategy D ends here; no walk-forward, no other variant",
}

ByYear = dict[int, dict[str, int]]

_WORKER: dict[str, Any] = {}  # per-process, set by the pool initializer


def tally_entries(trades: Iterable[Any]) -> ByYear:
    """{UTC year of the entry: {setup: entries}}. Reads entry_ts and setup only."""
    out: ByYear = {}
    for t in trades:
        by_setup = out.setdefault(datetime.fromtimestamp(t.entry_ts, UTC).year, {})
        by_setup[t.setup] = by_setup.get(t.setup, 0) + 1
    return out


def _setup_worker(df15: pd.DataFrame) -> None:
    _WORKER["df15"] = df15


def _count_combo(combo: Combo) -> tuple[Combo, ByYear]:
    """One engine run of one combo, breakers off. Returns the counts and nothing else."""
    cfg = config_for(with_overrides(base_config(), **{"breakers.enabled": False}), combo)
    res = TradingEngineD(cfg, make_strategy(cfg.strategy)).run(_WORKER["df15"], START_TS, END_TS)
    return combo, tally_entries(res.trades)


def run_count(df15: pd.DataFrame, workers: int = 1, combos: Sequence[Combo] | None = None) -> dict[Combo, ByYear]:
    """Every combo once (default: the 60 of GRID), in the given order. Refuses bars from 2024 on before any run."""
    refuse_2024(df15)
    todo = grid_combos() if combos is None else list(combos)
    out: dict[Combo, ByYear] = {}
    if workers <= 1:
        _setup_worker(df15)
        for n, c in enumerate(todo, 1):
            out[c] = _count_combo(c)[1]
            print(f"[count] {n}/{len(todo)} {c}", file=sys.stderr, flush=True)
    else:
        with ProcessPoolExecutor(
            max_workers=min(workers, len(todo)),
            mp_context=mp.get_context("spawn"),
            initializer=_setup_worker,
            initargs=(df15,),
        ) as pool:
            for n, (c, by_year) in enumerate(pool.map(_count_combo, todo), 1):
                out[c] = by_year
                print(f"[count] {n}/{len(todo)} {c}", file=sys.stderr, flush=True)
    return out


def median_monthly(totals: Iterable[int]) -> float:
    """The median of the totals, then per month: 335 and 337 give exactly 4.0, not a float a hair below it."""
    return statistics.median(totals) / MONTHS


def fallback_combos() -> list[Combo]:
    return [(k4, FALLBACK_Q_MIN, FALLBACK_S_MIN) for k4 in GRID["wave.K4"]]


def decide(totals: Mapping[Combo, int]) -> dict[str, Any]:
    """Spec 5.1 on each combo's entries over the window. The fallback combos are among the 60: no extra run."""
    combos = grid_combos()
    if set(totals) != set(combos):
        raise ValueError(f"the gate needs the {len(combos)} grid combos, got {len(totals)}")
    primary = median_monthly(totals.values())
    out: dict[str, Any] = {
        "threshold": THRESHOLD,
        "primary": {"n_combos": len(combos), "median_monthly": primary, "passed": primary >= THRESHOLD},
        "fallback": None,
    }
    if primary >= THRESHOLD:
        return {**out, "verdict": PROCEED_GRID, "grid": combos}
    narrow = fallback_combos()
    median = median_monthly(totals[c] for c in narrow)
    passed = median >= THRESHOLD
    out["fallback"] = {"q_min": FALLBACK_Q_MIN, "s_min": FALLBACK_S_MIN, "combos": narrow, "median_monthly": median,
                       "passed": passed}
    return {**out, "verdict": PROCEED_FALLBACK if passed else STOP, "grid": narrow if passed else None}


def build_report(results: Mapping[Combo, ByYear], df15: pd.DataFrame) -> dict[str, Any]:
    totals = {c: sum(n for by_setup in by_year.values() for n in by_setup.values()) for c, by_year in results.items()}
    rows = [
        {
            "combo": list(c),
            "entries": totals[c],
            "monthly": totals[c] / MONTHS,
            "by_year": {str(y): dict(sorted(by_year[y].items())) for y in sorted(by_year)},
        }
        for c, by_year in results.items()
    ]
    return {
        "kind": REPORT_KIND,
        "window": {"start": WF_START, "end": WF_END, "months": MONTHS},
        "data": {"first_ts": int(df15["ts"].iloc[0]), "last_ts": int(df15["ts"].iloc[-1]), "rows": len(df15)},
        "keys": list(GRID),
        "combos": rows,
        **decide(totals),
    }


def report_lines(rep: Mapping[str, Any]) -> list[str]:
    win = rep["window"]
    lines = [f"strategy D entries, {win['start']} .. {win['end']} (end exclusive), {win['months']} months"]
    for row in rep["combos"]:
        k4, q, s = row["combo"]
        lines.append(f"K4={k4} Q_MIN={q} S_MIN={s}: {row['entries']} entries, {row['monthly']:.2f} per month")
    p = rep["primary"]
    lines.append(f"primary: median of {p['n_combos']} combos = {p['median_monthly']:.3f} per month "
                 f"(needs >= {rep['threshold']})")
    fb = rep["fallback"]
    if fb is not None:
        lines.append(f"fallback: Q_MIN {fb['q_min']}, S_MIN {fb['s_min']}, median of {len(fb['combos'])} combos = "
                     f"{fb['median_monthly']:.3f} per month (needs >= {rep['threshold']})")
    lines.append(f"verdict: {rep['verdict']}")
    lines.append(_NEXT[rep["verdict"]])
    return lines


def count_stage(df15: pd.DataFrame, workers: int) -> dict[str, Any]:
    """The whole stage on the real cache: coverage check, the 60 runs, the report."""
    check_coverage(df15)
    return build_report(run_count(df15, workers), df15)
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratd/test_data.py tests/stratd/test_count.py tests/stratd/test_isolation.py -q
```

예상: `45 passed`(`test_data.py` 4개, `test_count.py` 28개, `test_isolation.py` 13개).

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratd/data.py src/ewbot/stratd/count.py tests/stratd/test_data.py tests/stratd/test_count.py
git commit -m "feat(stratd): stage-1 count: workers return counts only, the pre-registered gate, a report with no performance value" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: CLI `ewbot stratd count`와 전체 검증

**Files:**
- Modify: `src/ewbot/cli.py` (4곳: 모듈 설명, 파서, 처리 함수, `_COMMANDS`)
- Test: `tests/stratd/test_cli_stratd.py`

**Interfaces:**
- Consumes: `ewbot.cli`: `build_parser() -> argparse.ArgumentParser`, `main(argv=None) -> int`, `_refuse(msg) -> NoReturn`(종료 코드 2), `_b_write_json(path: Path, obj: Any) -> None`, `_COMMANDS`. `ewbot.stratd.count`: `count_stage(df15, workers)`, `report_lines(rep)`, `run_count`, `check_coverage`. `ewbot.stratd.data`: `cache_file(cache_dir)`, `load_btc_15m(cache_dir)`, `DataWindowError`.
- Produces: `ewbot stratd count [--cache-dir data/cache] [--workers N] [--out reports/stratd-count.json]`; `ewbot.cli._d_count(args: argparse.Namespace) -> int`, `ewbot.cli.cmd_stratd(args: argparse.Namespace) -> int`. 보고서가 정상으로 끝나면 판정과 상관없이 종료 코드 0, 데이터 구간 거부는 종료 코드 2, 캐시 파일이 없으면 메시지와 함께 종료 코드 1.

- [ ] **Step 1: 실패하는 테스트 작성**

**파일 `tests/stratd/test_cli_stratd.py`**

```python
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ewbot.backtest.walkforward import grid_combos
from ewbot.cli import build_parser, main
from ewbot.stratd.data import cache_file
from tests.stratd.helpers import a_frame, compress

# Run under spawn with a main guard, as the `ewbot` console script is; only the coverage check is off for the short cache.
DRIVER = '''\
import sys

import ewbot.stratd.count as count
from ewbot.cli import main

if __name__ == "__main__":
    count.check_coverage = lambda df: None
    sys.exit(main(sys.argv[1:]))
'''


def _cache(tmp_path: Path, rows: int = 8) -> Path:
    cache = tmp_path / "cache"
    cache.mkdir()
    compress(a_frame()).iloc[:rows].to_parquet(cache_file(cache), index=False)
    return cache


def _code(argv: list[str]) -> int:
    """main's exit code, also when a refusal exits through SystemExit (cli._refuse)."""
    try:
        return main(argv)
    except SystemExit as exc:
        return int(exc.code)


def test_the_parser_has_the_count_command_and_no_tuning_options():
    p = build_parser()
    ns = p.parse_args(["stratd", "count"])
    assert (ns.cmd, ns.d_cmd) == ("stratd", "count")
    assert (ns.cache_dir, ns.out) == (Path("data/cache"), Path("reports/stratd-count.json"))
    assert ns.workers >= 1
    assert p.parse_args(["stratd", "count", "--workers", "3"]).workers == 3
    for extra in (["--threshold", "3"], ["--params", "2,0.5,0"], ["--start", "2018-01-01"], ["--end", "2023-01-01"],
                  ["--q-min", "0.5"]):
        with pytest.raises(SystemExit):
            p.parse_args(["stratd", "count", *extra])


def test_the_cli_loads_stratd_only_for_stratd_commands():
    """`ewbot live …` and `ewbot paper …` run with the same modules as before strategy D."""
    code = "import sys, ewbot.cli; print(sorted(m for m in sys.modules if m.startswith('ewbot.stratd')))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


def test_the_command_writes_the_report_and_prints_the_verdict(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("ewbot.stratd.count.check_coverage", lambda df: None)
    seen = {}

    def fake_run(df15, workers):
        seen["workers"] = workers
        return {c: {2019: {"W3": 336}} for c in grid_combos()}

    monkeypatch.setattr("ewbot.stratd.count.run_count", fake_run)
    out = tmp_path / "reports" / "count.json"
    assert main(["stratd", "count", "--cache-dir", str(_cache(tmp_path)), "--workers", "3", "--out", str(out)]) == 0
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["verdict"] == "PROCEED_GRID" and len(rep["combos"]) == 60 and rep["combos"][0]["combo"] == [2.0, 0.5, 0.0]
    assert rep["grid"][0] == [2.0, 0.5, 0.0] and seen["workers"] == 3
    printed = capsys.readouterr().out
    assert "K4=2.0 Q_MIN=0.5 S_MIN=0.0: 336 entries, 4.00 per month" in printed
    assert "median of 60 combos = 4.000 per month" in printed
    assert "verdict: PROCEED_GRID" in printed and f"json: {out}" in printed


def test_a_short_cache_is_refused_and_writes_nothing(tmp_path, capsys):
    out = tmp_path / "count.json"
    assert _code(["stratd", "count", "--cache-dir", str(_cache(tmp_path)), "--out", str(out)]) == 2
    assert "2017-01-01" in capsys.readouterr().err and not out.exists()


def test_a_missing_cache_says_so(tmp_path):
    with pytest.raises(SystemExit, match="15m cache not found"):
        main(["stratd", "count", "--cache-dir", str(tmp_path / "none"), "--out", str(tmp_path / "count.json")])


def test_the_command_runs_engine_workers_under_spawn_with_a_main_guard(tmp_path):
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    out = tmp_path / "count.json"
    argv = [sys.executable, str(driver), "stratd", "count", "--cache-dir", str(_cache(tmp_path, rows=400)), "--workers", "2",
            "--out", str(out)]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert len(rep["combos"]) == 60 and rep["verdict"] == "STOP"  # 100 wave bars: still in the warm-up, no entry
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratd/test_cli_stratd.py -q
```

예상: 파서 오류 `invalid choice: 'stratd'`로 `test_the_parser_...`, `test_the_command_writes_...`, `test_a_short_cache_...`, `test_a_missing_cache_...`, `test_the_command_runs_...`가 실패하고, `test_the_cli_loads_stratd_only_for_stratd_commands`만 통과한다.

- [ ] **Step 3: 구현 (`src/ewbot/cli.py` 수정 4곳)**

찾을 부분은 각각 파일 안에 정확히 한 번 있다.

수정 1, 모듈 설명. 찾을 부분:

```python
inside its handlers, so `ewbot live …` and `ewbot paper …` never load it."""
```

바꿀 내용:

```python
inside its handlers, so `ewbot live …` and `ewbot paper …` never load it.
`stratd count` (Plan 6) is stage 1 of strategy D (ewbot.stratd): it counts the entries of the 60 combos on the BTC 15m
cache before 2024 and prints no performance value; ewbot.stratd is imported only inside its handler."""
```

수정 2, 파서. 찾을 부분:

```python
    p_eps.add_argument("--ledger-dir", type=Path, required=True)
    return parser
```

바꿀 내용:

```python
    p_eps.add_argument("--ledger-dir", type=Path, required=True)

    p_d = sub.add_parser("stratd", help="strategy D (Plan 6): A's rules on 1h waves and a 15m trigger; stage 1 only")
    d_sub = p_d.add_subparsers(dest="d_cmd", required=True)
    p_dc = d_sub.add_parser("count", help="stage 1: entries per month of the 60 combos, 2017-01..2023-12 (counts only)")
    p_dc.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    p_dc.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p_dc.add_argument("--out", type=Path, default=Path("reports/stratd-count.json"))
    return parser
```

수정 3, 처리 함수. 찾을 부분:

```python
_COMMANDS = {
```

바꿀 내용:

```python
# ----------------------------------------------------------------------------- strategy D (Plan 6)
# ewbot.stratd is imported inside this handler only: `ewbot live …` and `ewbot paper …` never load it.


def _d_count(args: argparse.Namespace) -> int:
    from ewbot.stratd.count import count_stage, report_lines
    from ewbot.stratd.data import DataWindowError, cache_file, load_btc_15m

    path = cache_file(args.cache_dir)
    if not path.exists():
        raise SystemExit(f"15m cache not found: {path} - strategy D reads the BTC cache of `ewbot stratb fetch`")
    try:
        rep = count_stage(load_btc_15m(args.cache_dir), args.workers)
    except DataWindowError as exc:
        _refuse(str(exc))
    _b_write_json(args.out, rep)
    for line in report_lines(rep):
        print(line)
    print(f"json: {args.out}")
    return 0


def cmd_stratd(args: argparse.Namespace) -> int:
    return {"count": _d_count}[args.d_cmd](args)


_COMMANDS = {
```

수정 4, `_COMMANDS`. 찾을 부분:

```python
    "aeth": cmd_aeth,
}
```

바꿀 내용:

```python
    "aeth": cmd_aeth,
    "stratd": cmd_stratd,
}
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratd/test_cli_stratd.py -q
```

예상: `6 passed`.

- [ ] **Step 5: 가드와 전체 테스트, A 지문, 바뀐 파일 확인**

```bash
uv run --group live pytest tests/stratd tests/paper/test_fingerprint.py tests/paper/test_static.py tests/stratb/test_isolation.py tests/aeth/test_isolation.py -q
uv run --group live pytest -q
uv run --group live python -c "from ewbot.paper.fingerprint import current_fingerprint as f; print(f())"
git diff --name-only "$(git merge-base HEAD master)"
git ls-files --others --exclude-standard
```

예상: 첫째 줄과 둘째 줄의 실패 0건. 전체 테스트 수는 기준선 1,528개(`pytest --collect-only`, 2026-11-04)에 `tests/stratd`의 88개를 더한 1,616개다. 셋째 줄은 `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1`을 출력한다. 넷째와 다섯째 줄의 경로는 `docs/superpowers/`, `src/ewbot/stratd/`, `tests/stratd/`, `src/ewbot/cli.py`뿐이다. 다른 경로가 보이면 커밋하지 말고 되돌린다.

- [ ] **Step 6: 커밋**

```bash
git add src/ewbot/cli.py tests/stratd/test_cli_stratd.py
git commit -m "feat(stratd): ewbot stratd count, spawned workers behind the CLI's main guard, no tuning options" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
