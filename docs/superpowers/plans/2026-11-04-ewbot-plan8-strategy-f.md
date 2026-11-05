# ewbot Plan 8 — 전략 F(5분 파동 + 1분 돌파) 수집·횟수 점검·워크포워드 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 전략 A와 지금 돌아가는 모의매매·SHADOW를 전혀 바꾸지 않고, A의 규칙을 시간만 줄인 전략 F(5분 파동 + 1분 돌파)의 코드를 `src/ewbot/stratf/`에 만든다. `ewbot stratf fetch`(1분봉 캐시), `ewbot stratf count`(명세 5.1, 성과 값 없음), `ewbot stratf walkforward`(명세 5.2)까지 완성한다. 이 계획의 작업들은 코드와 합성 봉 테스트만 만들고, 실제 수집·횟수 점검·워크포워드는 실행하지 않는다.

**Architecture:** 전략 D(`docs/strategy-d`의 `bc212be`)의 구조를 복사해 봉 길이를 바꾼다. A의 파동 엔진, 브로커, 거래 루프를 하위 클래스로 재사용하고 봉 길이를 가정한 부분만 덮어쓴다. 타임스탬프는 실제 시각 그대로다. D와 달리 파동 봉(5분)과 진행 곡선 봉(1시간)의 경계가 다르므로 두 검사를 나눈다. 봉 비율이 5:1이라 D의 `compress` 동등성 테스트는 쓸 수 없고, 명세 6절대로 같은 파동 봉을 만드는 합성 경로에서 파동 상태 동등성을 보고, 진입·손절·시간 손절은 손으로 계산한 경로로 시험한다.

- `src/ewbot/stratf/config.py`: 봉 길이 상수, `base_config()`(= A의 `BotConfig()`), `default_workers()`.
- `src/ewbot/stratf/clock.py`: `data/candles.py`의 금요일 달력을 봉 길이 인자로 복사.
- `src/ewbot/stratf/waves.py`: `WaveEngineF(WaveEngine)`. `on_1h_close`의 봉 경계 검사만 1분/5분으로.
- `src/ewbot/stratf/broker.py`: `BrokerF(PaperBroker)`. `process_bar`의 달력과 `_charge_funding`의 1/60 부과.
- `src/ewbot/stratf/engine.py`: `RunF(_Run)`, `TradingEngineF(TradingEngine)`. 진행 곡선은 시간당 점. 메모리와 시간을 줄이는 동등한 구현 두 가지(아래 "자원 추정").
- `src/ewbot/stratf/data.py`: 1분봉 수집과 읽기, 2024 가드, `ts` 열만 읽는 빠진 수 계산.
- `src/ewbot/stratf/pool.py`: 두 단계가 함께 쓰는 프로세스 풀. 실패하면 단계 전체가 멈춘다.
- `src/ewbot/stratf/count.py`: 횟수 작업자, 조합별 저장과 이어 돌리기, 게이트, 보고서(성과 값 없음).
- `src/ewbot/stratf/research.py`: `run_grid_f`, A의 `folds`·`evaluate_folds`·`compute_metrics`로 하는 워크포워드, 진행 조건.
- `src/ewbot/cli.py`(수정): `ewbot stratf fetch | count | walkforward`. `ewbot.stratf`는 처리 함수 안에서만 import한다.
- `tests/stratf/`: 파동 동등성, 손 계산 엔진 경로, 펀딩, 금요일, 2024 가드, 빠진 수, 작업자 실패, 격리 테스트.

**Tech Stack:** Python 3.12(uv), 기존 의존성만(numpy, pandas, pyarrow, requests), 표준 라이브러리 `concurrent.futures.ProcessPoolExecutor`(spawn), pytest. 테스트 명령은 `uv run --group live pytest`.

## Global Constraints

**범위**
- 저장소 루트에서 실행한다. 브랜치는 `docs/strategy-f`(이미 체크아웃됨). 커밋만 하고 push, PR, 워크플로 실행, 시크릿 접근은 하지 않는다.
- 어떤 작업도 실제 네트워크로 수집하지 않고, 실제 캐시로 `ewbot stratf count`나 `walkforward`를 실행하지 않으며, 2024-01-01 이후 시장 데이터를 읽지 않는다. 테스트는 합성 봉, 가짜 세션, `tmp_path`만 쓴다. 실제 실행은 이 계획이 끝나고 검토를 통과한 뒤 사용자의 지시로 "실제 실행" 절의 순서로 따로 한다.
- 범위 밖: `stratf/holdout.py`, `holdout-f.lock`, 거부를 넘어서는 2024 잠금 해제, 모의매매. ③은 ②를 통과한 뒤 별도 계획으로 만든다.

**변경 금지 (명세 1절 그대로)**
- A의 지문 대상: `types.py`, `config.py`, `indicators.py`, `data/candles.py`, `waves/`, `strategy/`, `engine/`, `execution/`, `risk/` (`src/ewbot/` 아래, 폴더에 새 파일 추가 포함). `paper/`, `live/`, `backtest/`, `holdout.lock`, `paper.yml`, `ci.yml`, `stratb/`, `paperb/`, 기존 캐시 파일. 지금 돌아가는 A 모의매매와 SHADOW는 재시작하지 않는다.
- A 지문은 `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1`로 유지된다.
- 기존 파일 중 수정하는 것은 `src/ewbot/cli.py` 하나다(`is_fingerprinted("cli.py")`는 거짓, 수정 4곳은 Task 7에 정확히 적었다). 새 파일은 `src/ewbot/stratf/**`와 `tests/stratf/**`에만 만든다. `pyproject.toml`, `uv.lock`, 다른 테스트 파일은 고치지 않는다. 새 워크플로는 만들지 않는다.
- 시간표를 속이는 방식(타임스탬프 변경)은 금지한다. 실제 시각 그대로 하위 클래스로 돌린다.
- `stratd`와 `strate`는 이 브랜치에 없다. import하지 않고, 테스트에서 경로로도 가리키지 않는다. 이 계획의 코드는 필요한 것을 모두 담고 있다.

**기존 가드 준수**
- `tests/paper/test_static.py`, `tests/stratb/test_isolation.py`, `tests/aeth/test_isolation.py`가 `src/ewbot` 전체를 훑는다. `stratf/`는 `allow_holdout`, `include_holdout`, `unlock_2024`를 쓰지 않고, `check_before_holdout`, `_map_tasks`, `_init_worker`를 이름, 속성, 문자열 어느 형태로도 쓰지 않는다. 병렬 실행은 자체 `ProcessPoolExecutor`와 자체 초기화 함수 `_setup_worker`로 한다.
- 새 최상위 패키지가 깨뜨리는 기존 테스트는 없다. `backtest.holdout.code_fingerprint`(`src/ewbot` 전체 해시)의 값은 바뀌지만, 다시 계산하는 곳은 이미 실행이 끝난 1회성 홀드아웃과 A-ETH 기록뿐이다(D 계획이 같은 변경에 대해 확인한 내용이다).
- 실제 실행 진입점은 `ewbot` 명령뿐이다. `cli.py`와 `.venv/bin/ewbot`에 main 가드가 있어 spawn에서 안전하다(D에서 확인). 작업자 함수는 모듈 최상위 함수다.
- `GRID`, `config_for`, `folds`, `pick_plateau`(`evaluate_folds`가 부른다), `evaluate_folds`, `compute_metrics`는 import만 한다.

**봉 길이 (명세 2절)**

| 항목 | A | F |
|---|---|---|
| 파동 봉 | 4시간 | 5분 |
| 돌파 확인 봉 | 1시간 | 1분 |
| 봉 비율 | 4:1 | 5:1 |
| 자산 | BTC | BTC (Coinbase 1분봉) |

**봉 개수로 같게 두는 값(숫자를 바꾸지 않는다):** `K4` 그리드 (2.0, 2.5, 3.0, 4.0), `atr_len` 14, `M` 12, `choch_bars` 3, 스윙 조회 4, ATR 버퍼(이제 5분 ATR 기준), 윅 클립 6(이제 1분 ATR 기준), `warmup_4h` 200(약 16.7시간), 시간 손절 12/60봉(1~5시간).

**A와 그대로 같은 값:** 네 가지 진입 자리(W3, W5, W5_REV, C_END), `r_gate` 1.5, TP1 50% 후 손절 본전 이동, 추적 손절, W5_REV 위험 절반, 보수적 위험 1%·최대 3배, 비용(`CostParams` 기본값), Q_MIN 그리드 (0.5, 0.55, 0.6, 0.65, 0.7), S_MIN 그리드 (0.0, 0.25, 0.5).

**실제 시간과 돈 기준이라 줄이지 않는 값:** 시간당 펀딩(1분봉마다 1/60), 금요일 휴장·블랙아웃 시각, 뉴욕 날짜 기준 손실 한도와 24시간 정지, 체결당 수수료와 슬리피지, 연율화 8760시간.

**데이터 (명세 4절):** 새 캐시 `data/cache/BTC-USD_1m.parquet`(git 제외). 2017-01-01 00:00 ~ 2023-12-31 23:59 UTC. 거래소가 준 봉만 저장한다. 전체 격자는 3,680,640분(2,556일)이다. 2024-01-01에 닿는 구간은 요청 전에 거부하고, 읽을 때도 `ts < 2024-01-01`로 거른다. **빠진 봉이 격자의 5%(184,032분)를 넘으면 ①을 돌리지 않고 끝낸다.** 빠진 수는 `ts` 열만 읽어 계산하고 실행 전에 사용자에게 보고한다. 한도를 올리거나 다른 거래소 데이터로 바꾸지 않는다. 빠진 봉은 `fill_gaps(df, tf=M1)`로 채우고, 합성 봉 비율을 횟수 점검 보고서에 적는다.

**Coinbase 한도 (2026-11-04 공식 문서로 확인):** 요청당 최대 300봉, `granularity` 60 허용, 거래가 없던 구간은 봉을 주지 않는다. 공개 엔드포인트는 IP당 초당 10회, 순간 15회다. `stratb/data15.py`와 같이 요청 사이에 0.15초를 쉬므로 초당 7회 미만이고, `_get_json`이 429와 5xx를 1초, 2초 뒤에 다시 시도한다. 전체는 12,269페이지(3,680,640 / 300을 올림)다.

**① 횟수 점검 (명세 5.1):** 2017-01-01 ~ 2023-12-31, 84개월. 엔진으로 시뮬레이션해 체결된 진입을 진입 시각 기준으로 센다. 손실 한도는 끈다. 수익, R, 승률, 자산, 청산 사유를 계산하거나 출력하지 않는다. 60개 조합의 월평균 건수 중앙값 ≥ **4.0** (건수 중앙값 ≥ 336)이면 ②로 간다. 미달이면 끝낸다. 예비안은 없다. 조합별 결과를 저장해 중단 뒤 이어서 돌린다. CLI에 합격선, 구간, 조합을 바꾸는 옵션을 두지 않는다.

**② 워크포워드 (명세 5.2):** A의 틀 그대로: `folds()`, 학습 24개월·확인 6개월·6개월씩 이동, 학습 창 12건 이상·유한한 Calmar, 이웃 평균 Calmar로 선택, 이어 붙인 표본 외 곡선, 마지막으로 무언가를 고른 구간의 조합. 손실 한도는 끈다. 진행 조건: 이어 붙인 표본 외 수익 > 0, 최종 조합이 마지막 구간에서 뽑힘(fallback 아님). 하나라도 어기면 끝낸다. 통과하면 ③ 계획을 쓰기 전에 사용자에게 보고한다.

## 자원 추정 (3.68M봉)

측정은 합성 숫자로 루프 앞부분(`fill_gaps`, `atr_series`, `clip_wicks`, `to_bars`)만 돌리고 짧은 튜플 연산의 시간을 잰 결과다(이 기계: 15 CPU = 성능 5 + 효율 10, 48 GB). 거래 루프의 시간과 메모리는 코드를 읽고 어림한 값이며 측정하지 않았다.

**작업자당 메모리: 약 2.5 GB.** A처럼 `to_bars`를 두 번 부르면 루프 전에 이미 3.15 GB다(측정). 봉 객체 하나가 약 290바이트이므로 두 목록이 2.1 GB를 차지한다. `RunF`는 클립되지 않은 봉을 두 목록이 공유하고 데이터프레임을 루프 전에 버린다. 이렇게 하면 루프 전 최고치는 2.23 GB이고(측정, 작업자가 받은 프레임 포함), 버린 프레임의 0.33 GB를 다시 쓰면서 루프에서 `ChochDetector`의 네 목록 0.12 GB, 진행 곡선과 거래 0.05 GB, 이벤트 최대 0.24 GB가 더해진다(어림).

**작업자 기본값: 12** (`min(12, CPU 수)`). 12 × 2.5 GB = 30 GB로, D의 기본값 `CPU - 1` = 14보다 낮춰 어림이 틀려도 작업자당 3.5 GB까지 48 GB 안에 든다. 부모 프로세스는 프레임 0.2 GB, 워크포워드 결과 약 0.5 GB(조합당 61,344개의 시간당 점)다.

**시간(어림, 측정 아님):** 수집 1~1.5시간(12,269페이지). 조합당 5~20분, 12개 작업자로 단계당 30~100분. ①과 ②는 각각 60회 돌린다(①은 성과를 디스크에 남기지 않으므로 ②가 재사용할 수 없다).

**A의 코드에서 읽어 찾은 초선형 지점**

1. `engine/loop.py:255` `_release_cooldown`: 1분봉마다 쿨다운 집합 전체를 다시 만든다. 지나간 lineage의 항목은 빠지지 않으므로 집합은 손절 횟수만큼 자라고, 비용은 봉 수 × 손절 수다. 항목당 45 ns(측정)이므로 손절 2,500건이면 조합당 약 3.4분, 1만 건이면 약 14분으로, 가장 큰 초선형 비용이다. A의 파일은 고칠 수 없으므로 `RunF`가 lineage별 색인으로 같은 규칙을 구현한다(Task 4, 동등성 테스트 포함).
2. `waves/engine.py:155` `self._pivots_t = tuple(zz.pivots)`: 피벗이 생길 때마다 전체 피벗을 복사해 O(P²)이다. 6만 개 복사가 87 µs(측정)이므로 P가 6만이면 조합당 약 5초라 그대로 둔다.
3. `waves/zigzag.py`의 `_update_uninitialised`: 첫 피벗 전까지 원자 전체를 다시 훑는다. 데이터 시작의 수십 봉에서만 일어나므로 무시한다.
4. A의 코드는 아니지만 `stratb/data15.py:185-189`: 페이지마다 캐시 전체를 다시 쓴다. 1분봉에서는 12,269번 × 평균 184만 행으로 수백 GB를 디스크에 쓰게 된다(어림). 그래서 F는 페이지마다 파일 하나를 쓰고 마지막에 한 번 합친다.

선형이지만 무거운 곳: `WaveEngine._state`는 1분봉마다 살아 있는 모든 카운트를 돌고(`waves/engine.py:259`), 달력은 봉마다 시간대 변환을 6번 한다, `atr_series`는 파이썬 반복이다(1.2초, 측정).

나머지(`pick_plateau`, `evaluate_folds`, `compute_metrics`)는 선형이거나 조합 수에만 비례한다. 진행 곡선은 시간당 점이므로 61,344개로, A의 것과 크기가 같다.

## 명세의 모호점과 이 계획의 해석

규칙을 바꾸지 않았다. 명세가 정하지 않았거나 그대로는 작동하지 않는 곳과 이 계획이 택한 해석이다. **1, 2, 3번은 ①의 판정을, 4, 5, 6번은 ②의 판정이나 숫자를 바꿀 수 있으므로 실제 실행 전에 사용자가 확인한다.**

1. **양 끝 봉 (①에 영향, 거부 여부만).** 명세는 구간을 00:00 ~ 23:59로 적으면서 "거래소가 준 봉만 저장"한다고 한다. Coinbase는 거래가 없던 분의 봉을 주지 않으므로 양 끝 분이 정확히 있으리라는 보장이 없다. D처럼 양 끝이 정확히 같기를 요구하면 조용한 1분 때문에 거부될 수 있다. 첫 봉과 마지막 봉이 각각 구간 끝에서 60분 안에 있으면 받아들이고, 없는 양 끝 분은 빠진 수에 넣는다. 60분을 넘는 잘림은 5% 한도 안이어도 거부한다(잘린 캐시는 합격선의 84개월을 어긴다).
2. **중앙값 (①에 영향, 경계에서만).** 60개는 짝수이므로 `statistics.median`처럼 30번째와 31번째의 평균을 쓴다. 335와 337이면 336으로 통과하고, 335와 336이면 335.5로 탈락한다. D와 같다. 판정은 부동소수점을 피해 건수로 한다.
3. **한도 "넘으면" (①에 영향, 경계에서만).** 정확히 184,032분이면 통과하고 184,033분이면 끝난다.
4. **"B·C·D와 같음" (②에 영향, 수익이 정확히 0일 때만).** 명세 5.2는 수익 > 0이라 적고 B·C·D와 같다고 하지만, `stratb/research.py`의 B 코드는 `>= 0.0`이고 D는 ②에 가지 못했다. 명세의 글자대로 `> 0`을 쓴다.
5. **아무 구간도 고르지 못한 경우 (②의 판정, 결과는 같음).** A의 `chosen_final_fallback`은 이때 거짓이다. 진행 조건에 `chosen_final is not None`을 더한다. 곡선이 평평해 수익 > 0도 어차피 어긴다.
6. **평평한 봉의 한계 (①②의 숫자에 영향, 규칙 변경 아님).** 명세 4절은 1분 ATR과 윅 클립만 적었다. 긴 공백에서는 5분 ATR도 낮아져 ZigZag 임계값이 줄고, 공백 중에 피벗이 확정될 수 있다. 규칙은 그대로 두고 보고서에 합성 봉 비율과 함께 이 한계를 적는다.
7. **이웃 비율 50% (②에 영향, A와 같음).** 명세 5.2의 요약에는 없지만 `pick_plateau`에는 이웃의 50% 이상이 자격을 갖춰야 하는 규칙이 있다. "A의 틀 그대로"이므로 그대로 쓴다.
8. **명세 6절 괄호(영향 없음).** "(펀딩 0, 금요일 구간 제외, 손실 한도 끔)"은 D의 엔진 전체 비교에서 온 조건이다. 파동 상태는 체결과 상관없으므로 신호를 내지 않는 전략으로 돌리면 금요일 구간을 뺄 필요가 없다. 설정은 펀딩 0, 손실 한도 끔으로 두고 금요일 구간은 그대로 둔다.
9. **이어 받기 방식(영향 없음).** "페이지마다 저장"을 `BTC-USD_1m.parts/<시작 ts>.parquet`에 페이지당 파일 하나로 한다. 빈 페이지도 파일을 남겨 다시 요청하지 않는다. 다 받은 뒤에만 `BTC-USD_1m.parquet`를 만들므로 이 파일이 있으면 수집이 끝난 것이다.
10. **②의 이어 돌리기(영향 없음).** 명세는 ①에만 요구한다. ②는 조합별 성과를 디스크에 남기지 않고, 실패하면 멈추고 처음부터 다시 돌린다.
11. **②의 순서 가드(영향 없음).** "떨어지면 그 단계에서 끝낸다"를 코드로 지키기 위해 `walkforward`는 같은 캐시로 만든 PROCEED 횟수 보고서가 없으면 거부한다.
12. **"계산하지 않는다" (영향 없음).** 명세 5.1은 수익, R, 자산 등을 계산하지 말라고 한다. 크기 계산과 체결에 자본이 필요해 엔진 안에서는 계산된다. D, E와 같이 `count.py`가 읽거나 내보내거나 저장하지 않는 것으로 해석한다.
13. **파일 하나 추가, 동등한 구현 두 개(영향 없음).** 명세 3절 목록에 없는 `pool.py`를 더한다. `RunF`의 봉 공유와 쿨다운 색인은 규칙을 바꾸지 않으며 각각 테스트로 A의 결과와 같음을 확인한다.

## 명세 6절 대응표

| 명세 6절 | 테스트 |
|---|---|
| 파동 동등성 | `test_waves.py::test_the_same_wave_bars_give_a_s_wave_states_bar_for_bar`, `test_engine.py::test_the_engine_gives_f_a_s_wave_bars_and_wave_states` |
| 진입, 손절, 시간 손절의 손 계산 | `test_engine.py`의 `test_a_signal_fills_at_the_next_1m_open_and_ends_at_the_last_close`, `test_a_stop_fills_at_its_price_and_costs_one_r`, `test_a_time_stop_of_12_bars_is_one_wall_clock_hour` |
| 신호 봉 뒤 체결, 미완성 파동 봉 금지 | 위의 첫 테스트, `test_waves.py::test_only_the_last_minute_closes_a_wave_bar`, `test_engine.py::test_a_wave_state_does_not_depend_on_later_minutes` |
| 펀딩이 시간당 금액 그대로 | `test_broker.py::test_sixty_1m_bars_pay_one_wall_clock_hour_of_funding`, `test_engine.py::test_a_trade_pays_funding_per_wall_clock_hour` |
| 금요일 휴장이 실제 시각 | `test_clock.py`, `test_broker.py::test_friday_close_is_at_the_true_wall_clock_time`, `test_engine.py::test_entries_are_decided_by_the_true_friday_blackout` |
| 1분봉이 빠짐없이 처리 | `test_engine.py::test_every_1m_bar_reaches_the_wave_engine_and_five_make_a_5m_bar` |
| 빠진 시간을 낀 파동 봉 묶기 | `test_engine.py::test_a_missing_1m_bar_is_filled_not_dropped`, `test_a_5m_bar_the_data_enters_midway_makes_no_wave_bar` |
| 2024 가드 | `test_data.py`의 요청 전 거부, 응답의 2024 행, 읽기 필터 테스트 |
| 격리, A 지문 불변 | `test_isolation.py` |
| 전체 테스트 통과 | Task 7 Step 7 |
| `count.py`가 성과 모듈을 부르지 않고 출력에 성과 값 없음 | `test_count.py`의 성과 가드 네 개 |
| 작업자 실패가 0건으로 처리되지 않음 | `test_count.py::test_a_failing_worker_aborts_the_run`, `test_a_killed_worker_aborts_the_run`, `test_cli_stratf.py::test_a_failed_count_writes_no_report` |
| 캐시 구간과 빠진 수 거부(양 끝만 보지 않음) | `test_data.py::test_interior_holes_count_even_when_both_ends_are_there`, `test_a_cache_cut_short_is_refused_under_the_limit` |

## Tasks

### Task 1: 패키지, 고정값, 거래 달력, 격리 검사

**Files:**
- Create: `src/ewbot/stratf/__init__.py`, `src/ewbot/stratf/config.py`, `src/ewbot/stratf/clock.py`
- Create: `tests/stratf/__init__.py`(빈 파일), `tests/stratf/helpers.py`, `tests/stratf/test_config.py`, `tests/stratf/test_clock.py`, `tests/stratf/test_isolation.py`

**Interfaces:**
- Consumes: `ewbot.config.BotConfig`, `ewbot.types.H1`, `ewbot.data.candles.futures_closed(ts: int) -> bool`, `entry_blackout(ts: int) -> bool`(비교용), `ewbot.paper.fingerprint`의 `PROJECT_ROOT`, `current_fingerprint()`, `is_fingerprinted(rel: str) -> bool`, `tree_files()`, 테스트만: `tests.synth.random_bars(seed: int, n: int, vol: float = 6.0, start: float = 5000.0) -> list[Bar]`, `tests.synth.TICK`, `ewbot.waves.engine.WaveState`, `ewbot.waves.zigzag.Pivot`.
- Produces:
  - `stratf.config`: `M1 = 60`, `WAVE_BAR = 300`, `TRIGGER_BARS_PER_WAVE = 5`, `WAVE_LAST = 240`, `HOUR_LAST = 3540`, `MAX_DEFAULT_WORKERS = 12`, `base_config() -> BotConfig`, `default_workers() -> int`
  - `stratf.clock`: `futures_closed(ts: int, tf: int) -> bool`, `entry_blackout(ts: int, tf: int) -> bool`
  - `tests.stratf.helpers`: `T_A`, `T_F`, `N_BARS`, `GAPS`, `ny(year, month, day, hour, minute=0) -> int`, `a_frame() -> pd.DataFrame`, `stretch(df1h: pd.DataFrame) -> pd.DataFrame`, `idx_a(ts: int) -> int`, `idx_f(ts: int) -> int`, `is_flat_minute(ts: int) -> bool`, `view(ws: WaveState, idx: Callable[[int], int]) -> tuple`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/stratf/__init__.py`는 빈 파일로 만든다.

파일: `tests/stratf/helpers.py`
```python
"""Synthetic bars for the strategy-F tests (no network, no real cache).

a_frame() is the path tests/backtest/test_integration_synth.py runs strategy A on: two years of 1h bars, seed 0, with its
three missing hours, from T_A. Here every wick is one tick longer, so no bar's open is also its high or low.

stretch() makes a 1m path with the same wave bars as A's. A's ratio is 4 trigger bars per wave bar and F's is 5, so A's
path cannot be compressed in time. Each of A's 4h buckets becomes one 5m bar: minute 0 is a flat bar at the open of the
bucket's first 1h bar, and minutes 1-4 are the bucket's four 1h bars. The flat bar lies strictly inside the next bar, so the
wave bar's OHLC is A's and the flat bar is never an extreme: the next bar overrides whatever it did to E_live or a kill.
T_A is a multiple of 4h and T_F of 5m, so A's buckets and F's wave bars hold the same bars."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ewbot.stratf.config import M1, TRIGGER_BARS_PER_WAVE
from ewbot.types import H1
from ewbot.waves.engine import WaveState
from ewbot.waves.zigzag import Pivot
from tests.synth import TICK, random_bars

T_A = 1_605_657_600  # 2020-11-18 00:00 UTC: tests/synth's T0 moved back 156 whole weeks
T_F = T_A
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
            "high": [b.high + TICK for b in bars],
            "low": [b.low - TICK for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    )
    return df.drop(index=GAPS).reset_index(drop=True)


def stretch(df1h: pd.DataFrame) -> pd.DataFrame:
    """A's 1h bars at 1m spacing, one flat minute before each 4h bucket. A bucket that lost its first bar has no flat
    minute either: fill_gaps then fills both flat at the previous close, which is what A's fill_gaps gives its first bar."""
    k = ((df1h["ts"] - T_A) // H1).to_numpy()
    bucket, pos = k // 4, k % 4
    real = df1h.assign(ts=T_F + (TRIGGER_BARS_PER_WAVE * bucket + pos + 1) * M1)
    heads = df1h["open"].to_numpy()[pos == 0]
    flat = pd.DataFrame(
        {
            "ts": T_F + TRIGGER_BARS_PER_WAVE * bucket[pos == 0] * M1,
            "open": heads,
            "high": heads,
            "low": heads,
            "close": heads,
            "volume": 0.0,
        }
    )
    return pd.concat([flat, real], ignore_index=True).sort_values("ts", kind="stable").reset_index(drop=True)


def idx_a(ts: int) -> int:
    return (ts - T_A) // H1


def idx_f(ts: int) -> int:
    """The A bar a minute of stretch() holds. The flat minute counts as the first bar of its bucket."""
    q, r = divmod((ts - T_F) // M1, TRIGGER_BARS_PER_WAVE)
    return 4 * q + max(r - 1, 0)


def is_flat_minute(ts: int) -> bool:
    return (ts - T_F) // M1 % TRIGGER_BARS_PER_WAVE == 0


def view(ws: WaveState, idx: Callable[[int], int]) -> tuple:
    """Everything in a WaveState, with each ts as a bar index."""

    def piv(p: Pivot | None) -> tuple | None:
        return None if p is None else (p.i, idx(p.sub_ts), p.price, p.kind, p.confirmed_at)

    counts = tuple((c.pattern, c.d, c.phase, c.q, tuple(piv(p) for p in c.points)) for c in ws.counts)
    e_ts = idx(ws.E_live_ts) if ws.E_live_ts >= 0 else ws.E_live_ts  # -1 before the first leg
    return (idx(ws.ts), ws.ready, counts, ws.probs, ws.q_top, ws.s_rev, ws.lineages, ws.E_live, e_ts, ws.leg_dir,
            piv(ws.leg_start), tuple(piv(p) for p in ws.pivots4h), piv(ws.new_pivot_4h), ws.atr4h, ws.closed_4h, ws.i4h)
```

파일: `tests/stratf/test_config.py`
```python
from __future__ import annotations

from ewbot.backtest.walkforward import GRID, config_for
from ewbot.config import CONSERVATIVE, SETUPS, BotConfig, CostParams
from ewbot.strategy import elliott
from ewbot.stratf import config
from ewbot.stratf.config import HOUR_LAST, M1, TRIGGER_BARS_PER_WAVE, WAVE_BAR, WAVE_LAST, base_config


def test_the_bar_lengths():
    assert (M1, WAVE_BAR, TRIGGER_BARS_PER_WAVE, WAVE_LAST, HOUR_LAST) == (60, 300, 5, 240, 3540)


def test_the_bar_count_values_keep_a_s_numbers():
    w, s = base_config().wave, base_config().strategy
    assert GRID["wave.K4"] == (2.0, 2.5, 3.0, 4.0)
    assert (w.atr_len, w.M, w.wick_clip_atr1h, w.warmup_4h) == (14, 12, 6.0, 200)
    assert (s.choch_bars, s.time_stop_min_4h, s.time_stop_max_4h) == (3, 12, 60)
    assert elliott._SWING_LOOKBACK == 4
    assert 200 * WAVE_BAR / 3600 == 1000 / 60  # about 16.7 hours
    assert (12 * WAVE_BAR, 60 * WAVE_BAR) == (3600, 5 * 3600)  # 1 to 5 hours


def test_the_unchanged_values_are_a_s():
    cfg = base_config()
    assert cfg == BotConfig()
    assert cfg.strategy.mode == "A" and cfg.strategy.setups_enabled == SETUPS == ("W3", "W5", "W5_REV", "C_END")
    assert (cfg.strategy.r_gate, cfg.strategy.tp1_frac, cfg.strategy.setup3_risk_mult) == (1.5, 0.5, 0.5)
    assert cfg.risk == CONSERVATIVE and (cfg.risk.risk_pct, cfg.risk.max_leverage) == (0.01, 3.0)
    assert cfg.costs == CostParams()
    assert GRID["strategy.Q_MIN"] == (0.5, 0.55, 0.6, 0.65, 0.7) and GRID["strategy.S_MIN"] == (0.0, 0.25, 0.5)


def test_a_grid_combo_sets_k4_q_min_and_s_min():
    cfg = config_for(base_config(), (2.5, 0.55, 0.25))
    assert (cfg.wave.K4, cfg.strategy.Q_MIN, cfg.strategy.S_MIN) == (2.5, 0.55, 0.25)


def test_the_default_worker_count_fits_15_cpu_and_48_gb(monkeypatch):
    assert config.MAX_DEFAULT_WORKERS == 12
    for cpus, want in ((15, 12), (4, 4), (None, 1)):
        monkeypatch.setattr(config.os, "cpu_count", lambda c=cpus: c)
        assert config.default_workers() == want
```

파일: `tests/stratf/test_clock.py`
```python
from __future__ import annotations

import pytest

from ewbot.backtest.runner import utc_ts
from ewbot.data import candles
from ewbot.stratf.clock import entry_blackout, futures_closed
from ewbot.stratf.config import M1
from ewbot.types import H1
from tests.stratf.helpers import ny

SLOTS = [(h, m) for h in range(24) for m in range(60)]


@pytest.mark.parametrize("day", [(2023, 11, 17), (2023, 7, 14)], ids=["EST", "EDT"])
def test_the_friday_windows_on_1m_bars_are_at_the_true_wall_clock_time(day):
    closed = [s for s in SLOTS if futures_closed(ny(*day, *s), M1)]
    blackout = [s for s in SLOTS if entry_blackout(ny(*day, *s), M1)]
    assert closed == [(17, m) for m in range(60)]
    assert blackout == [(h, m) for h in (16, 17) for m in range(60)]


def test_other_days_are_never_closed():
    for day in ((2023, 11, 16), (2023, 11, 18)):  # Thursday, Saturday
        assert not any(entry_blackout(ny(*day, *s), M1) or futures_closed(ny(*day, *s), M1) for s in SLOTS)


def test_on_1h_bars_it_is_a_s_calendar_for_a_whole_year():
    t0 = utc_ts("2023-01-01")
    for ts in range(t0, t0 + 8760 * H1, H1):
        assert futures_closed(ts, H1) == candles.futures_closed(ts)
        assert entry_blackout(ts, H1) == candles.entry_blackout(ts)


def test_a_s_1h_calendar_would_close_a_1m_bar_too_early():
    bar = ny(2023, 11, 17, 16, 1)
    assert candles.futures_closed(bar) is True
    assert futures_closed(bar, M1) is False
```

파일: `tests/stratf/test_isolation.py`
```python
"""Strategy-A protection (Plan 8 spec 1, 3, 6): F must not change anything A's paper, live or go-live count depends on, nor
strategy B's ended code. tests/paper/test_static.py, tests/stratb/test_isolation.py and tests/aeth/test_isolation.py scan
src/ewbot as a whole, so they scan stratf/ too."""

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
    "holdout.lock", ".github/",
)
F_PATHS = ("src/ewbot/stratf/", "tests/stratf/")
FORBIDDEN = {"allow_holdout", "include_holdout", "unlock_2024", "check_before_holdout", "_map_tasks", "_init_worker"}
FOREIGN_PACKAGES = ("ewbot.paper", "ewbot.paperb", "ewbot.live", "ewbot.aeth", "ewbot.stratb", "ewbot.stratd", "ewbot.strate")
READERS = {"read_parquet", "to_parquet", "load_1h", "load_15m", "update_cache", "update_cache_15m"}


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], capture_output=True, text=True, timeout=60)


def _merge_base() -> str | None:
    for ref in ("master", "origin/master"):
        r = _git("merge-base", "HEAD", ref)
        if r.returncode == 0:
            return r.stdout.strip()
    return None


def _modules():
    for f in sorted((SRC / "stratf").rglob("*.py")):
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


def test_stratf_is_outside_strategy_a_fingerprinted_folders():
    assert not is_fingerprinted("stratf/x.py") and not is_fingerprinted("stratf/sub/y.py")
    assert [rel for rel, _ in _modules()]
    assert not [p for p in tree_files() if p.startswith("src/ewbot/stratf/")]


def test_stratf_work_changes_no_protected_path():
    """Since the merge-base with master (tracked changes and new untracked files): a branch that touches strategy F touches
    no protected path. A branch that does not touch F is not concerned."""
    base = _merge_base()
    if base is None:
        pytest.skip("no master branch in this clone (shallow CI checkout)")
    diff = _git("diff", "--name-only", base)
    new = _git("ls-files", "--others", "--exclude-standard")
    assert diff.returncode == 0 and new.returncode == 0
    changed = set(diff.stdout.split()) | set(new.stdout.split())
    if any(p.startswith(F_PATHS) for p in changed):
        assert sorted(p for p in changed if p.startswith(PROTECTED)) == []


def test_this_plan_stops_before_the_holdout():
    assert not (SRC / "stratf" / "holdout.py").exists()
    assert not (PROJECT_ROOT / "holdout-f.lock").exists()


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


def test_stratf_uses_none_of_the_forbidden_names():
    for rel, tree in _modules():
        assert not _names(tree) & FORBIDDEN, rel


def test_only_data_py_reads_or_writes_candles():
    for rel, tree in _modules():
        if rel != "stratf/data.py":
            assert not _names(tree) & READERS, rel


def test_stratf_imports_no_runtime_and_no_other_strategy():
    for rel, tree in _modules():
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not [m for m in mods if m in FOREIGN_PACKAGES or m.startswith(tuple(f"{p}." for p in FOREIGN_PACKAGES))], rel
```

- [ ] **Step 2: 실패 확인**

```bash
uv run --group live pytest tests/stratf -q
```

예상: 수집 오류 `ModuleNotFoundError: No module named 'ewbot.stratf'`.

- [ ] **Step 3: 구현**

파일: `src/ewbot/stratf/__init__.py`
```python
"""Strategy F (Plan 8): strategy A with every bar count unchanged on shorter bars - 5m waves, 1m trigger.

Stages 0-2 only so far: the 1m cache, the trade count of spec 5.1 and the walk-forward of spec 5.2. Everything lives in this
folder; of the existing files only cli.py gains the `stratf` command."""
```

파일: `src/ewbot/stratf/config.py`
```python
"""Strategy F's fixed values (Plan 8 spec 2, 3): the bar lengths, A's config, and the size of the pool.

Nothing here is a new rule value. F runs on A's BotConfig defaults, so K4, atr_len, M, choch_bars, the swing lookback, the
wick clip, warmup_4h (now 200 5m bars, about 16.7 hours) and the time stops (now 12..60 5m bars, 1..5 hours) keep their
numbers, and r_gate, tp1_frac, the risk profile and the costs are A's. Only the bars are shorter. A wave bar holds 5 trigger
bars here, not 4 as in A."""

from __future__ import annotations

import os

from ewbot.config import BotConfig
from ewbot.types import H1

M1 = 60  # one trigger bar, seconds
WAVE_BAR = 300  # one wave bar, seconds
TRIGGER_BARS_PER_WAVE = WAVE_BAR // M1
WAVE_LAST = WAVE_BAR - M1  # ts % WAVE_BAR of a wave bar's last 1m bar
HOUR_LAST = H1 - M1  # ts % H1 of an hour's last 1m bar
MAX_DEFAULT_WORKERS = 12  # about 2.5 GB per worker on 3.68M bars: 30 GB of the 48 GB, and 12 of the 15 CPUs


def base_config() -> BotConfig:
    return BotConfig()


def default_workers() -> int:
    return min(MAX_DEFAULT_WORKERS, os.cpu_count() or 1)
```

파일: `src/ewbot/stratf/clock.py`
```python
"""The trading calendar of data/candles.py with the bar length as a parameter (Plan 8 spec 2, 3).

A's _overlaps_friday_window is written for a 1h bar: a 1m bar opening at 16:01 New York would count as overlapping the 17:00
close. The windows themselves are A's, at their real wall-clock times; they are not rescaled."""

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

- [ ] **Step 4: 통과 확인**

```bash
uv run --group live pytest tests/stratf -q
```

예상: `23 passed`. `test_stratf_work_changes_no_protected_path`는 master가 있으면 통과하고, 없는 복제본에서는 건너뛴다.

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratf/__init__.py src/ewbot/stratf/config.py src/ewbot/stratf/clock.py tests/stratf/__init__.py tests/stratf/helpers.py tests/stratf/test_config.py tests/stratf/test_clock.py tests/stratf/test_isolation.py
git commit -m "feat(stratf): package, bar lengths, 1m trading calendar and isolation checks" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: 5분 파동 + 1분 원자의 파동 엔진

**Files:**
- Create: `src/ewbot/stratf/waves.py`
- Test: `tests/stratf/test_waves.py`

**Interfaces:**
- Consumes: `ewbot.waves.engine.WaveEngine(params: WaveParams)`, `WaveEngine.on_1h_close(bar1h: Bar, bar4h: Bar | None, sub_bars: Sequence[Bar] | None) -> WaveState`, `ewbot.engine.loop._agg_4h(sub: Sequence[Bar]) -> Bar`, `tests.synth.feed`, `ewbot.stratf.config.M1`, `WAVE_BAR`, `tests.stratf.helpers`.
- Produces: `stratf.waves.WaveEngineF(WaveEngine)` with `on_1h_close(self, bar1m: Bar, bar5m: Bar | None, sub_bars: Sequence[Bar] | None) -> WaveState`.

- [ ] **Step 1: 실패하는 테스트 작성**

파일: `tests/stratf/test_waves.py`
```python
from __future__ import annotations

from collections.abc import Iterator, Sequence

import pytest

from ewbot.config import WaveParams
from ewbot.data.candles import to_bars
from ewbot.engine.loop import _agg_4h
from ewbot.stratf.config import TRIGGER_BARS_PER_WAVE, WAVE_BAR
from ewbot.stratf.waves import WaveEngineF
from ewbot.types import Bar
from ewbot.waves.engine import WaveEngine
from tests.stratf.helpers import a_frame, idx_a, idx_f, stretch, view
from tests.synth import feed

PATH_BARS = 2_900  # the path before its first missing bar; a multiple of 4, so every wave bar is complete


def feed_f(bars1m: Sequence[Bar]) -> Iterator[tuple[Bar, Bar | None, tuple[Bar, ...] | None]]:
    """tests/synth's feed at F's sizes: 1m -> (bar1m, bar5m | None, sub_bars | None)."""
    buf: list[Bar] = []
    for b in bars1m:
        if not buf or b.ts // WAVE_BAR != buf[0].ts // WAVE_BAR:
            buf = []
        buf.append(b)
        if len(buf) == TRIGGER_BARS_PER_WAVE and buf[0].ts % WAVE_BAR == 0:
            yield b, _agg_4h(buf), tuple(buf)
        else:
            yield b, None, None


def _ohlc(b: Bar) -> tuple[float, float, float, float]:
    return b.open, b.high, b.low, b.close


def test_the_stretched_path_has_a_s_wave_bars():
    df = a_frame().iloc[:PATH_BARS]
    a_bars = [x[1] for x in feed(to_bars(df)) if x[1] is not None]
    f_bars = [x[1] for x in feed_f(to_bars(stretch(df))) if x[1] is not None]
    assert len(a_bars) == PATH_BARS // 4
    assert [_ohlc(b) for b in f_bars] == [_ohlc(b) for b in a_bars]


def test_the_same_wave_bars_give_a_s_wave_states_bar_for_bar():
    """Spec 6's core test. Every state after one of A's bars must equal F's state after the same bar, in every field."""
    df = a_frame().iloc[:PATH_BARS]
    a, f = WaveEngine(WaveParams()), WaveEngineF(WaveParams())
    fed = feed_f(to_bars(stretch(df)))
    n_counts = 0
    ws = None
    for k, xa in enumerate(feed(to_bars(df))):
        if k % 4 == 0:
            f.on_1h_close(*next(fed))  # the flat minute that opens the 5m bar: A's path has no such bar
        ws = a.on_1h_close(*xa)
        assert view(ws, idx_a) == view(f.on_1h_close(*next(fed)), idx_f)
        n_counts += len(ws.counts)
    assert next(fed, None) is None
    assert ws is not None and ws.ready and ws.pivots4h and n_counts > 0  # the comparison saw real structure


def test_a_s_wave_engine_refuses_1m_bars_and_f_takes_them():
    bars = to_bars(stretch(a_frame().iloc[:4]))
    bar5m = _agg_4h(bars)
    with pytest.raises(ValueError, match="UTC 4h bucket"):
        WaveEngine(WaveParams()).on_1h_close(bars[4], bar5m, tuple(bars))
    ws = WaveEngineF(WaveParams()).on_1h_close(bars[4], bar5m, tuple(bars))
    assert ws.closed_4h and ws.i4h == 0 and ws.ts == bars[4].ts


def test_f_refuses_a_5m_bar_this_1m_bar_does_not_close():
    bars = to_bars(stretch(a_frame().iloc[:8]))
    first = _agg_4h(bars[:5])
    with pytest.raises(ValueError, match="UTC 5m bar"):
        WaveEngineF(WaveParams()).on_1h_close(bars[3], first, tuple(bars[:5]))  # not the 5m bar's last minute
    with pytest.raises(ValueError, match="UTC 5m bar"):
        WaveEngineF(WaveParams()).on_1h_close(bars[9], first, tuple(bars[5:10]))  # another 5m bar's last minute
    with pytest.raises(ValueError, match="sub_bars"):
        WaveEngineF(WaveParams()).on_1h_close(bars[4], first, tuple(bars[:4]))


def test_only_the_last_minute_closes_a_wave_bar():
    w = WaveEngineF(WaveParams())
    states = [w.on_1h_close(*x) for x in feed_f(to_bars(stretch(a_frame().iloc[:8])))]
    assert [s.closed_4h for s in states] == [False, False, False, False, True] * 2
    assert [s.i4h for s in states] == [-1, -1, -1, -1, 0, 0, 0, 0, 0, 1]
```

- [ ] **Step 2: 실패 확인**

```bash
uv run --group live pytest tests/stratf/test_waves.py -q
```

예상: 수집 오류 `ModuleNotFoundError: No module named 'ewbot.stratf.waves'`.

- [ ] **Step 3: 구현**

파일: `src/ewbot/stratf/waves.py`
```python
"""WaveEngineF: A's WaveEngine with 5m structure bars and 1m atoms (Plan 8 spec 3).

The only line of WaveEngine that knows a bar length is the check at waves/engine.py:146-149, which refuses anything but a 1h
bar closing a 4h bucket; everything else compares prices and orders ts values. on_1h_close below is A's method with that
check at F's sizes. A's name stays so the engine's call reads the same, and so do the names in WaveState: atr4h, pivots4h,
new_pivot_4h, closed_4h and i4h carry 5m values here, and warmup_4h counts 5m bars (200 = about 16.7 hours). A's file is
fingerprinted, so this copy cannot drift from it."""

from __future__ import annotations

from collections.abc import Sequence

from ewbot.stratf.config import M1, WAVE_BAR
from ewbot.types import Bar
from ewbot.waves.engine import WaveEngine, WaveState
from ewbot.waves.zigzag import Pivot


class WaveEngineF(WaveEngine):
    def on_1h_close(self, bar1m: Bar, bar5m: Bar | None, sub_bars: Sequence[Bar] | None) -> WaveState:
        zz = self._zz
        new_piv: Pivot | None = None
        if bar5m is not None:
            if (bar1m.ts + M1) % WAVE_BAR != 0 or bar5m.ts != bar1m.ts + M1 - WAVE_BAR:
                raise ValueError("bar5m must be the UTC 5m bar that bar1m closes")
            if sub_bars is not None and (len(sub_bars) == 0 or sub_bars[-1].ts != bar1m.ts):
                raise ValueError("sub_bars must end with the 1m bar that closes the 5m bar")
        self._hist.push(bar1m)
        if bar5m is not None:
            self._i4h += 1
            new_piv = zz.update(self._i4h, bar5m, sub_bars)
            if new_piv is not None:
                self._pivots_t = tuple(zz.pivots)
                self._on_new_pivot()
            if zz.leg_dir != 0:
                self._E_live, self._E_live_ts = zz.E, zz.E_sub_ts
                self._rescore()
        elif zz.leg_dir != 0:
            ext = bar1m.high if zz.leg_dir > 0 else bar1m.low
            if zz.leg_dir * (ext - self._E_live) > 0:
                self._E_live, self._E_live_ts = ext, bar1m.ts
        if zz.leg_dir != 0:
            self._kill(bar1m)
        state = self._state(bar1m.ts, new_piv, bar5m is not None)
        self._prev_top = state.top
        return state
```

- [ ] **Step 4: 통과 확인**

```bash
uv run --group live pytest tests/stratf/test_waves.py -q
```

예상: `5 passed`.

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratf/waves.py tests/stratf/test_waves.py
git commit -m "feat(stratf): wave engine on 5m bars and 1m atoms; the same wave bars give A's wave states bar for bar" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: 1분 브로커 (펀딩 1/60, 금요일은 실제 시각)

**Files:**
- Create: `src/ewbot/stratf/broker.py`
- Test: `tests/stratf/test_broker.py`

**Interfaces:**
- Consumes: `ewbot.execution.paper.PaperBroker(costs: CostParams, equity: float)`, `Fill`, `PaperPosition`, `ewbot.execution.costs.fee`, `funding_per_hour(price, contracts, direction, costs)`, `slip`, `stratf.clock.futures_closed(ts, tf)`, `stratf.config.M1`.
- Produces: `stratf.broker.BrokerF(PaperBroker)` with `process_bar(self, bar: Bar) -> list[Fill]`, `_charge_funding(self, bar: Bar) -> None`.

- [ ] **Step 1: 실패하는 테스트 작성**

파일: `tests/stratf/test_broker.py`
```python
from __future__ import annotations

import pytest

from ewbot.config import CostParams
from ewbot.data import candles
from ewbot.execution.costs import funding_per_hour, slip
from ewbot.execution.paper import PaperBroker
from ewbot.stratf.broker import BrokerF
from ewbot.stratf.config import M1
from ewbot.types import H1, Bar
from tests.stratf.helpers import ny

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


def test_sixty_1m_bars_pay_one_wall_clock_hour_of_funding():
    f, a = long_10(BrokerF(C, EQUITY)), long_10(PaperBroker(C, EQUITY))
    for k in range(60):
        f.process_bar(bar(MONDAY + k * M1))
    a.process_bar(bar(MONDAY))
    hour = funding_per_hour(PX, 10, 1, C)
    assert a.position.funding == pytest.approx(hour)
    assert f.position.funding == pytest.approx(hour)  # not 60 x the hour, as relabelled timestamps would pay


def test_one_1m_bar_pays_a_sixtieth_and_a_short_receives_it():
    f = BrokerF(C, EQUITY)
    f.queue_entry(-1, 10, 21_000.0, 19_000.0, 18_000.0, 5)
    f.process_bar(bar(MONDAY))
    assert f.position.funding == pytest.approx(funding_per_hour(PX, 10, -1, C) / 60)
    assert f.position.funding < 0.0


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
    f_fills, f_cash = _replay(BrokerF(NO_FUNDING, EQUITY), rows, exit_after, M1)
    a_fills, a_cash = _replay(PaperBroker(NO_FUNDING, EQUITY), rows, exit_after, H1)
    assert [kinds(x) for x in f_fills] == want
    assert fill_view(f_fills) == fill_view(a_fills) and f_cash == a_cash


def test_friday_close_is_at_the_true_wall_clock_time():
    f = long_10(BrokerF(C, EQUITY))
    assert kinds(f.process_bar(bar(ny(*FRIDAY, 16, 58)))) == ["ENTRY"]
    at_1659 = ny(*FRIDAY, 16, 59)
    assert candles.futures_closed(at_1659) is True  # A's 1h calendar would stop here
    assert kinds(f.process_bar(bar(at_1659, h=21_100.0))) == ["TP1"]
    for minute in range(60):
        assert f.process_bar(bar(ny(*FRIDAY, 17, minute), lo=18_900.0)) == []  # the stop passes while closed
    fills = f.process_bar(bar(ny(*FRIDAY, 18)))
    assert kinds(fills) == ["STOP"] and fills[0].price == slip(PX, -1, C) and fills[0].contracts == 5


def test_a_queued_entry_waits_for_the_reopen():
    f = long_10(BrokerF(C, EQUITY))
    for minute in range(60):
        assert f.process_bar(bar(ny(*FRIDAY, 17, minute))) == []
    assert kinds(f.process_bar(bar(ny(*FRIDAY, 18)))) == ["ENTRY"]


def test_funding_keeps_running_while_the_market_is_closed():
    f = long_10(BrokerF(C, EQUITY))
    f.process_bar(bar(ny(*FRIDAY, 16, 59)))
    before = f.position.funding
    for minute in range(60):
        f.process_bar(bar(ny(*FRIDAY, 17, minute)))
    assert f.position.funding - before == pytest.approx(funding_per_hour(PX, 10, 1, C))
```

- [ ] **Step 2: 실패 확인**

```bash
uv run --group live pytest tests/stratf/test_broker.py -q
```

예상: 수집 오류 `ModuleNotFoundError: No module named 'ewbot.stratf.broker'`.

- [ ] **Step 3: 구현**

파일: `src/ewbot/stratf/broker.py`
```python
"""BrokerF: A's PaperBroker on 1m bars (Plan 8 spec 3).

Two things in A's broker assume a 1h bar: the calendar check of process_bar (execution/paper.py:114; a 1m bar opening at
16:01 New York would count as closed) and one hour of funding per bar (187-192). process_bar below is A's with F's calendar,
and _charge_funding charges a sixtieth of A's hourly funding per bar, so the money per wall-clock hour is A's. A's file is
fingerprinted, so this copy cannot drift from it."""

from __future__ import annotations

from ewbot.execution.costs import fee, funding_per_hour, slip
from ewbot.execution.paper import Fill, PaperBroker, PaperPosition
from ewbot.stratf.clock import futures_closed
from ewbot.stratf.config import M1
from ewbot.types import H1, Bar


class BrokerF(PaperBroker):
    def process_bar(self, bar: Bar) -> list[Fill]:
        fills: list[Fill] = []
        if futures_closed(bar.ts, M1):
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

        # (4) funding for the minute
        self._charge_funding(bar)
        return fills

    def _charge_funding(self, bar: Bar) -> None:
        pos = self.position
        if pos is None:
            return
        cost = funding_per_hour(bar.close, pos.contracts, pos.direction, self.costs) * (M1 / H1)
        self.cash -= cost
        pos.funding += cost
```

- [ ] **Step 4: 통과 확인**

```bash
uv run --group live pytest tests/stratf/test_broker.py -q
```

예상: `9 passed`.

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratf/broker.py tests/stratf/test_broker.py
git commit -m "feat(stratf): 1m broker, a sixtieth of the hourly funding per bar, Friday windows at their real times" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: 1분 엔진, 시간당 진행 곡선, 손 계산 경로

**Files:**
- Create: `src/ewbot/stratf/engine.py`
- Test: `tests/stratf/test_engine.py`

**Interfaces:**
- Consumes: `ewbot.engine.loop`의 `_Run(cfg, strategy, wave, choch, start_ts)`, `TradingEngine(cfg, strategy, *, wave_engine=None, choch=None)`, `EngineResult(trades, equity, signals, events)`, `EquityPoint(ts, equity)`, `_agg_4h`; `ewbot.data.candles`의 `clip_wicks(df, atr1h, k) -> tuple[pd.DataFrame, int]`, `fill_gaps(df, tf=H1)`, `to_bars(df) -> list[Bar]`; `ewbot.indicators.atr_series(high, low, close, n=14)`; `stratf.broker.BrokerF`, `stratf.clock.entry_blackout`, `stratf.waves.WaveEngineF`, `stratf.config`.
- Produces:
  - `stratf.engine.RunF(_Run)`: `execute(self, bars: list[Bar], clipped: list[Bar], n_clipped: int) -> EngineResult`, `_may_enter(self, bar: Bar, t_close: int) -> bool`, `_finish(self, ot, exit_ts: int, reason: str) -> None`, `_release_cooldown(self, ws: WaveState) -> None`
  - `stratf.engine.TradingEngineF(TradingEngine)`: `run(self, df1m: pd.DataFrame, start_ts: int | None = None, end_ts: int | None = None) -> EngineResult`
  - `stratf.engine._clipped_bars(bars: list[Bar], df: pd.DataFrame, clipped: pd.DataFrame) -> list[Bar]`

- [ ] **Step 1: 실패하는 테스트 작성**

손 계산의 기준: 비용 0, 자본 10,000, 위험 1%, 진입 50,000, 손절 49,000이면 계약당 위험은 1,000 × 0.01 = 10달러이므로 100 / 10 = 10계약이다.

파일: `tests/stratf/test_engine.py`
```python
from __future__ import annotations

import random
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from ewbot.config import BotConfig, CostParams, with_overrides
from ewbot.data.candles import clip_wicks, fill_gaps, to_bars
from ewbot.engine.loop import TradingEngine, _Run
from ewbot.execution.costs import funding_per_hour
from ewbot.indicators import atr_series
from ewbot.strategy.base import Signal
from ewbot.strategy.choch import ChochDetector
from ewbot.stratf.broker import BrokerF
from ewbot.stratf.config import M1, WAVE_BAR, base_config
from ewbot.stratf.engine import RunF, TradingEngineF, _clipped_bars
from ewbot.stratf.waves import WaveEngineF
from ewbot.types import H1, Bar
from ewbot.waves.engine import WaveEngine, WaveState
from tests.stratf.helpers import a_frame, idx_a, idx_f, is_flat_minute, ny, stretch, view

T = ny(2023, 11, 13, 0)  # a Monday, on the hour
FLAT = (50_000.0, 50_100.0, 49_900.0, 50_000.0)
CFG = BotConfig(costs=CostParams(fee_rate=0.0, slippage=0.0, funding_apr_long=0.0))  # 10 contracts, see the plan


def ts(i: int, t0: int = T) -> int:
    return t0 + i * M1


def frame1m(n: int, rows: dict[int, tuple[float, float, float, float]] | None = None, t0: int = T) -> pd.DataFrame:
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

    def on_1h_close(self, bar1m: Bar, bar5m: Bar | None, sub_bars: Any) -> WaveState:
        self.calls.append((bar1m.ts, bar5m, tuple(sub_bars) if sub_bars is not None else None))
        return WaveState(
            ts=bar1m.ts, ready=True, counts=(), probs=(), top=None, q_top=0.0, s_rev=0.0, lineages=frozenset(),
            E_live=bar1m.close, E_live_ts=bar1m.ts, leg_dir=1, leg_start=None, pivots4h=(), new_pivot_4h=None,
            atr4h=400.0, closed_4h=bar5m is not None, i4h=0,
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
    res = TradingEngineF(cfg, strat, wave_engine=wave, choch=NullChoch()).run(df, start_ts=start_ts)
    return res, strat, wave


def recording(cls: type[WaveEngine]) -> type[WaveEngine]:
    """A real wave engine that keeps its states and wave bars."""

    class Recording(cls):
        def __init__(self, params: Any) -> None:
            super().__init__(params)
            self.states: list[WaveState] = []
            self.wave_bars: list[Bar] = []

        def on_1h_close(self, bar: Bar, wave_bar: Bar | None, sub_bars: Any) -> WaveState:
            ws = super().on_1h_close(bar, wave_bar, sub_bars)
            self.states.append(ws)
            if wave_bar is not None:
                self.wave_bars.append(wave_bar)
            return ws

    return Recording


# ----------------------------------------------------------------------------- 1m bars


def test_a_s_engine_raises_on_1m_bars():
    with pytest.raises(ValueError):
        TradingEngine(CFG, FakeStrategy({}), wave_engine=StubWave(), choch=NullChoch()).run(frame1m(10))


def test_every_1m_bar_reaches_the_wave_engine_and_five_make_a_5m_bar():
    rows = {0: (100.0, 110.0, 95.0, 105.0), 1: (105.0, 130.0, 100.0, 120.0), 2: (120.0, 125.0, 90.0, 92.0),
            3: (92.0, 99.0, 91.0, 98.0), 4: (98.0, 99.0, 97.0, 98.0)}
    _, _, wave = run(frame1m(10, rows), {})
    assert [c[0] for c in wave.calls] == [ts(i) for i in range(10)]
    assert [c[1] is not None for c in wave.calls] == [False, False, False, False, True] * 2
    assert wave.calls[4][1] == Bar(T, 100.0, 130.0, 90.0, 98.0, 5.0, False)
    assert [b.ts for b in wave.calls[4][2]] == [ts(i) for i in range(5)]


def test_a_missing_1m_bar_is_filled_not_dropped():
    _, _, wave = run(frame1m(10).drop(index=[2]).reset_index(drop=True), {})
    assert [c[0] for c in wave.calls] == [ts(i) for i in range(10)]
    assert [b.synthetic for b in wave.calls[4][2]] == [False, False, True, False, False]
    assert wave.calls[4][1].synthetic is True


def test_a_5m_bar_the_data_enters_midway_makes_no_wave_bar():
    _, _, wave = run(frame1m(9, t0=T + M1), {})
    assert [c[1] is not None for c in wave.calls] == [False] * 8 + [True]
    assert wave.calls[8][1].ts == T + WAVE_BAR


def test_bars_off_the_1m_grid_are_refused():
    with pytest.raises(ValueError, match="1-minute"):
        run(frame1m(10, t0=T + 7), {})


def test_the_run_uses_f_s_broker():
    assert isinstance(RunF(CFG, FakeStrategy({}), StubWave(), NullChoch(), None).broker, BrokerF)


def test_unclipped_bars_are_shared_and_the_values_are_those_of_to_bars():
    df = fill_gaps(frame1m(40, {30: (50_000.0, 60_000.0, 49_900.0, 50_000.0)}), tf=M1)
    atr = atr_series(df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), 14)
    clipped, n = clip_wicks(df, atr, 6.0)
    assert n == 1
    bars = to_bars(df)
    out = _clipped_bars(bars, df, clipped)
    assert out == to_bars(clipped)
    assert out[29] is bars[29] and out[30] is not bars[30] and bars[30].high == 60_000.0


# ----------------------------------------------------------------------------- spec 6: the wave states through the engine


def test_the_engine_gives_f_a_s_wave_bars_and_wave_states():
    """No signal, so no order: funding, the Friday windows and the breakers cannot reach the wave engine. The zero funding and
    the breakers off are spec 6's conditions all the same. The path includes A's three missing hours."""
    cfg = with_overrides(base_config(), **{"costs.funding_apr_long": 0.0, "breakers.enabled": False})
    df = a_frame()
    a, f = recording(WaveEngine)(cfg.wave), recording(WaveEngineF)(cfg.wave)
    res_a = TradingEngine(cfg, FakeStrategy({}), wave_engine=a, choch=ChochDetector(3)).run(df)
    res_f = TradingEngineF(cfg, FakeStrategy({}), wave_engine=f, choch=ChochDetector(3)).run(stretch(df))
    assert res_a.events[0][1] == res_f.events[0][1] == "WICKS_CLIPPED:0"  # the wick clip's ATR differs, so it must not cut
    assert [(b.open, b.high, b.low, b.close) for b in f.wave_bars] == [(b.open, b.high, b.low, b.close) for b in a.wave_bars]
    states = [s for s in f.states if not is_flat_minute(s.ts)]
    assert len(states) == len(a.states) == 2 * 8760
    assert [view(s, idx_f) for s in states] == [view(s, idx_a) for s in a.states]
    assert a.states[-1].ready and a.states[-1].pivots4h


def test_a_wave_state_does_not_depend_on_later_minutes():
    """The wave state at a minute inside a 5m bar is the same whatever the rest of the bar does: an unfinished wave bar is
    never used. Minute 5_002 is the third minute of its 5m bar."""
    cfg = with_overrides(base_config(), **{"breakers.enabled": False})
    base = stretch(a_frame()).iloc[:6_000].reset_index(drop=True)
    changed = base.copy()
    changed.loc[changed.index >= 5_002, ["open", "high", "low", "close"]] += 5_000.0
    a, b = recording(WaveEngineF)(cfg.wave), recording(WaveEngineF)(cfg.wave)
    TradingEngineF(cfg, FakeStrategy({}), wave_engine=a, choch=ChochDetector(3)).run(base)
    TradingEngineF(cfg, FakeStrategy({}), wave_engine=b, choch=ChochDetector(3)).run(changed)
    assert a.states[:5_002] == b.states[:5_002]
    assert a.states[5_002:] != b.states[5_002:]
    assert not a.states[5_001].closed_4h and a.states[5_004].closed_4h


# ----------------------------------------------------------------------------- equity curve


def test_equity_points_are_hourly_and_marked_at_the_hour_s_close():
    rows = {119: (50_000.0, 50_150.0, 49_900.0, 50_100.0), 179: (50_000.0, 50_250.0, 49_900.0, 50_200.0)}
    res, _, _ = run(frame1m(180, rows), {ts(2): sig(ts(2))})
    assert [p.ts for p in res.equity] == [T, T + H1, T + 2 * H1]
    assert [p.equity for p in res.equity] == pytest.approx([10_000.0, 10_010.0, 10_020.0])


def test_start_ts_starts_the_curve_at_that_hour():
    res, _, _ = run(frame1m(180), {}, start_ts=ts(60))
    assert [p.ts for p in res.equity] == [T + H1, T + 2 * H1]


def test_data_ending_mid_hour_still_books_the_last_close():
    res, _, _ = run(frame1m(200, {199: (50_000.0, 50_250.0, 49_900.0, 50_200.0)}), {ts(2): sig(ts(2))})
    assert [p.ts for p in res.equity] == [T, T + H1, T + 2 * H1, T + 3 * H1]
    assert res.equity[-1].equity == pytest.approx(10_020.0) and res.trades[0].exit_reason == "END"


# ----------------------------------------------------------------------------- hand-computed paths


def test_a_signal_fills_at_the_next_1m_open_and_ends_at_the_last_close():
    rows = {3: (50_050.0, 50_100.0, 49_900.0, 50_000.0), 11: (50_000.0, 50_250.0, 49_900.0, 50_200.0)}
    res, _, _ = run(frame1m(12, rows), {ts(2): sig(ts(2))})
    [t] = res.trades
    assert (t.entry_ts, t.exit_ts, t.contracts, t.exit_reason) == (ts(3), ts(11), 10, "END")
    assert t.entry_price == 50_050.0 and t.exit_price_avg == 50_200.0
    assert t.pnl == pytest.approx(15.0)  # 150 x 10 contracts x 0.01 BTC


def test_a_stop_fills_at_its_price_and_costs_one_r():
    rows = {3: (50_000.0, 50_100.0, 49_900.0, 50_000.0), 6: (50_000.0, 50_050.0, 48_900.0, 49_500.0)}
    res, _, _ = run(frame1m(12, rows), {ts(2): sig(ts(2))})
    [t] = res.trades
    assert (t.entry_ts, t.exit_ts, t.exit_reason) == (ts(3), ts(6), "STOP")
    assert t.exit_price_avg == 49_000.0
    assert t.pnl == pytest.approx(-100.0) and t.r_multiple == pytest.approx(-1.0)  # 1,000 x 10 x 0.01 = 1% of 10,000


def test_a_gap_through_the_stop_fills_at_the_open():
    rows = {6: (48_500.0, 48_600.0, 48_400.0, 48_500.0)}
    res, _, _ = run(frame1m(12, rows), {ts(2): sig(ts(2))})
    [t] = res.trades
    assert (t.exit_ts, t.exit_reason, t.exit_price_avg) == (ts(6), "STOP", 48_500.0)
    assert t.pnl == pytest.approx(-150.0)


def test_a_time_stop_of_12_bars_is_one_wall_clock_hour():
    res, _, _ = run(frame1m(80), {ts(4): sig(ts(4), time_stop_4h=12)})
    [t] = res.trades
    assert (t.entry_ts, t.exit_ts, t.exit_reason) == (ts(5), ts(65), "TIME")
    assert t.exit_ts - t.entry_ts == 12 * WAVE_BAR == H1


def test_a_trade_pays_funding_per_wall_clock_hour():
    costs = CostParams(fee_rate=0.0, slippage=0.0)
    res, _, _ = run(frame1m(64), {ts(3): sig(ts(3))}, cfg=BotConfig(costs=costs))
    [t] = res.trades
    assert (t.entry_ts, t.exit_ts) == (ts(4), ts(63))  # 60 bars held = 1 hour
    assert t.funding == pytest.approx(funding_per_hour(50_000.0, 10, 1, costs))


def test_entries_are_decided_by_the_true_friday_blackout():
    day = (2023, 11, 17)
    _, strat, _ = run(frame1m(126, t0=ny(*day, 15, 57)), {})  # 15:57 .. 18:02 New York
    allowed = [(15, 57), (15, 58), (18, 0), (18, 1)]  # 15:59 fills at 16:00; 18:02 is the last bar
    assert strat.calls == [ny(*day, h, m) for h, m in allowed]


# ----------------------------------------------------------------------------- the cooldown index


def test_the_indexed_cooldown_release_is_a_s_rule():
    """RunF's _release_cooldown touches one lineage, A's rebuilds the whole set every 1m bar. Same rule, same set."""
    rng = random.Random(0)
    cfg = with_overrides(CFG, **{"breakers.enabled": False})
    a = _Run(cfg, FakeStrategy({}), StubWave(), NullChoch(), None)
    f = RunF(cfg, FakeStrategy({}), StubWave(), NullChoch(), None)
    released = 0
    for step in range(3_000):
        lineage = ("W5", rng.choice((1, -1)), rng.randrange(6))
        phase = rng.randrange(4)
        roll = rng.random()
        if roll < 0.3:
            reason = rng.choice(("STOP", "STOP", "TP2"))
            instance = None if rng.random() < 0.1 else (*lineage, phase)
            ot = SimpleNamespace(sig=SimpleNamespace(instance=instance), to_trade=lambda ts, r: SimpleNamespace(pnl=0.0))
            a._finish(ot, step, reason)
            f._finish(ot, step, reason)
        elif roll < 0.35:
            a._release_cooldown(SimpleNamespace(top=None))
            f._release_cooldown(SimpleNamespace(top=None))
        else:
            before = len(a.cooldown)
            ws = SimpleNamespace(top=SimpleNamespace(lineage=lineage, phase=phase))
            a._release_cooldown(ws)
            f._release_cooldown(ws)
            released += before - len(a.cooldown)
        assert f.cooldown == a.cooldown
    assert released > 100
```

- [ ] **Step 2: 실패 확인**

```bash
uv run --group live pytest tests/stratf/test_engine.py -q
```

예상: 수집 오류 `ModuleNotFoundError: No module named 'ewbot.stratf.engine'`.

- [ ] **Step 3: 구현**

파일: `src/ewbot/stratf/engine.py`
```python
"""Strategy F's engine (Plan 8 spec 3): A's loop on 1m trigger bars and 5m wave bars, at the real wall-clock times.

The timestamps are never relabelled: funding, the Friday windows and the breakers' New York days stay on the real clock.
Compared with engine/loop.py, TradingEngineF.run fills gaps on the 1m grid, and RunF changes the four places that know a bar
length: the wave-bar bucket (loop.py:146-152), the close instant (163), the equity point (177) and the blackout check (265).
The trigger bar is the 1m bar, so the ATR of the wick clip is the 1m ATR. Everything else is inherited.

The wave bar and the equity point close at different times here: every 5 minutes and every hour. Equity points are hourly, so
A's metrics (annualised with 8760 hours) and evaluate_folds apply unchanged: EquityPoint(hour open ts, equity at the close of
the hour's last 1m bar). The last bar of the data also writes one, so an END close inside an hour is booked. The breakers are
observed at every 1m close, as A observes them at every trigger bar.

Two things differ from A in how, not in what, because of the 3.68M bars:
- raw and clipped bars share every Bar object clip_wicks left alone (_clipped_bars), and the frames are dropped before the
  loop: about 1.1 GB less per worker. Bars are frozen.
- _release_cooldown looks at one lineage's entries instead of rebuilding the whole cooldown set every 1m bar (loop.py:255,
  whose cost grows with bars x stops, since a finished lineage's entries stay). _finish keeps the index."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from ewbot.config import BotConfig
from ewbot.data.candles import clip_wicks, fill_gaps, to_bars
from ewbot.engine.loop import EngineResult, EquityPoint, TradingEngine, _agg_4h, _Run
from ewbot.indicators import atr_series
from ewbot.strategy.base import Strategy
from ewbot.strategy.choch import ChochDetector
from ewbot.stratf.broker import BrokerF
from ewbot.stratf.clock import entry_blackout
from ewbot.stratf.config import HOUR_LAST, M1, TRIGGER_BARS_PER_WAVE, WAVE_BAR, WAVE_LAST
from ewbot.stratf.waves import WaveEngineF
from ewbot.types import H1, Bar
from ewbot.waves.engine import WaveEngine, WaveState


class RunF(_Run):
    def __init__(
        self,
        cfg: BotConfig,
        strategy: Strategy,
        wave: WaveEngine,
        choch: ChochDetector,
        start_ts: int | None,
    ) -> None:
        super().__init__(cfg, strategy, wave, choch, start_ts)
        self.broker = BrokerF(cfg.costs, cfg.initial_equity)
        self._cooldown_index: dict[tuple, set[tuple]] = {}  # lineage -> its entries of self.cooldown

    def execute(self, bars: list[Bar], clipped: list[Bar], n_clipped: int) -> EngineResult:
        """bars: raw 1m bars (broker, marks, END close); clipped: the same bars after clip_wicks
        (5m aggregation, wave engine, trigger, strategy). Same length and ts."""
        self.events.append((bars[0].ts, f"WICKS_CLIPPED:{n_clipped}"))
        bucket: list[Bar] = []
        last = len(bars) - 1
        for idx, (bar, cbar) in enumerate(zip(bars, clipped)):
            if bucket and bucket[0].ts // WAVE_BAR != cbar.ts // WAVE_BAR:
                bucket = []
            bucket.append(cbar)
            bar5m: Bar | None = None
            sub: tuple[Bar, ...] | None = None
            if cbar.ts % WAVE_BAR == WAVE_LAST:
                if len(bucket) == TRIGGER_BARS_PER_WAVE:
                    sub = tuple(bucket)
                    bar5m = _agg_4h(sub)
                bucket = []

            for f in self.broker.process_bar(bar):  # 1-2 (raw prices)
                self._on_fill(f)
            ws = self.wave.on_1h_close(cbar, bar5m, sub)  # 3
            self.choch.update(cbar)  # 4
            self._manage(ws, closed_4h=bar5m is not None)  # 5

            t_close = bar.ts + M1  # 6
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
            if (bar.ts % H1 == HOUR_LAST or idx == last) and (self.start_ts is None or hour_open >= self.start_ts):
                self.equity.append(EquityPoint(hour_open, self.broker.equity(bar.close)))

        return EngineResult(tuple(self.trades), tuple(self.equity), tuple(self.signals), tuple(self.events))

    def _may_enter(self, bar: Bar, t_close: int) -> bool:
        if self.start_ts is not None and bar.ts < self.start_ts:
            return False
        if self.pending is not None:
            return False
        if not self.breakers.can_enter(t_close):
            return False
        return not (entry_blackout(bar.ts, M1) or entry_blackout(bar.ts + M1, M1))

    def _finish(self, ot, exit_ts: int, reason: str) -> None:
        super()._finish(ot, exit_ts, reason)
        if reason == "STOP" and ot.sig.instance is not None:
            instance = tuple(ot.sig.instance)
            self._cooldown_index.setdefault(tuple(instance[:3]), set()).add(instance)

    def _release_cooldown(self, ws: WaveState) -> None:
        """A's rule: an entry leaves when the top count is of its lineage and in another phase."""
        top = ws.top
        if top is None or not self.cooldown:
            return
        entries = self._cooldown_index.get(tuple(top.lineage))
        if not entries:
            return
        gone = {i for i in entries if i[3] != top.phase}
        if gone:
            entries -= gone
            self.cooldown -= gone


def _clipped_bars(bars: list[Bar], df: pd.DataFrame, clipped: pd.DataFrame) -> list[Bar]:
    """to_bars(clipped), with every bar clip_wicks left alone shared with ``bars``."""
    high, low = clipped["high"].to_numpy(), clipped["low"].to_numpy()
    out = list(bars)
    for i in np.flatnonzero((high != df["high"].to_numpy()) | (low != df["low"].to_numpy())):
        out[i] = replace(bars[i], high=float(high[i]), low=float(low[i]))
    return out


class TradingEngineF(TradingEngine):
    def run(self, df1m: pd.DataFrame, start_ts: int | None = None, end_ts: int | None = None) -> EngineResult:
        if len(df1m) and (df1m["ts"] % M1 != 0).any():
            raise ValueError("every bar must open on a 1-minute boundary")
        df = fill_gaps(df1m, tf=M1)  # A's default tf is 1h and raises on 1m bars
        if end_ts is not None:
            df = df[df["ts"] < end_ts]
        df = df.reset_index(drop=True)
        if df.empty:
            return EngineResult((), (), (), ())
        atr1m = atr_series(
            df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), self.cfg.wave.atr_len
        )
        clipped, n_clipped = clip_wicks(df, atr1m, self.cfg.wave.wick_clip_atr1h)
        wave = self._wave_in if self._wave_in is not None else WaveEngineF(self.cfg.wave)
        choch = (
            self._choch_in
            if self._choch_in is not None
            else ChochDetector(self.cfg.strategy.choch_bars)
        )
        run = RunF(self.cfg, self.strategy, wave, choch, start_ts)
        bars = to_bars(df)
        cbars = _clipped_bars(bars, df, clipped)
        del df, clipped, atr1m  # 3.68M rows: the loop needs the bars only
        return run.execute(bars, cbars, n_clipped)
```

- [ ] **Step 4: 통과 확인**

```bash
uv run --group live pytest tests/stratf/test_engine.py -q
```

예상: `19 passed`.

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratf/engine.py tests/stratf/test_engine.py
git commit -m "feat(stratf): 1m engine with 5m wave bars and hourly equity points; hand-computed entry, stop and time-stop paths" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: 1분봉 수집과 캐시, 2024 가드, 빠진 수

**Files:**
- Create: `src/ewbot/stratf/data.py`
- Test: `tests/stratf/test_data.py`

**Interfaces:**
- Consumes: `ewbot.backtest.runner.utc_ts(date: str) -> int`, `ewbot.backtest.walkforward.HOLDOUT_START`, `WF_START`, `ewbot.data.coinbase_history`의 `API`, `COLUMNS`, `MAX_PER_REQUEST`, `_get_json(session, url, params) -> list`, `stratf.config.M1`, `ewbot.types.H1`. `stratb/data15.py`는 import하지 않는다(요청 방식만 같고, 캐시 저장 방식은 페이지마다 전체를 다시 쓰므로 따르지 않는다).
- Produces (`stratf.data`):
  - 상수: `PRODUCT`, `START_TS`, `END_TS`, `LAST_TS`, `GRID_BARS = 3_680_640`, `MISSING_LIMIT = 184_032`, `EDGE_SLACK = 3600`
  - `DataWindowError(ValueError)`, `MissingBarsError(DataWindowError)`
  - `cache_file(cache_dir: Path) -> Path`, `parts_dir(cache_dir: Path) -> Path`
  - `update_cache_1m(cache_dir: Path, *, session: requests.Session | None = None, sleep_s: float = 0.15, start_ts: int = START_TS, end_ts: int = END_TS) -> Path`
  - `load_1m(cache_dir: Path) -> pd.DataFrame`, `refuse_2024(df: pd.DataFrame) -> None`
  - `CacheReport(rows: int, first_ts: int | None, last_ts: int | None, off_grid: int, missing: int)` with `synthetic_share: float`
  - `report_from_ts(ts: np.ndarray) -> CacheReport`, `cache_report(cache_dir: Path) -> CacheReport`, `check_cache(rep: CacheReport) -> None`, `data_record(rep: CacheReport) -> dict[str, Any]`, `report_lines(rep: CacheReport) -> list[str]`

- [ ] **Step 1: 실패하는 테스트 작성**

파일: `tests/stratf/test_data.py`
```python
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import requests

from ewbot.backtest.walkforward import HOLDOUT_START as HS
from ewbot.stratf import data
from ewbot.stratf.config import M1
from ewbot.stratf.data import (
    END_TS,
    GRID_BARS,
    LAST_TS,
    MISSING_LIMIT,
    START_TS,
    CacheReport,
    DataWindowError,
    MissingBarsError,
    cache_file,
    cache_report,
    check_cache,
    data_record,
    load_1m,
    parts_dir,
    refuse_2024,
    report_from_ts,
    report_lines,
    update_cache_1m,
)

ISO = "%Y-%m-%dT%H:%M:%SZ"
PAGE = 300 * M1


def epoch(s: str) -> int:
    return int(datetime.strptime(s, ISO).replace(tzinfo=UTC).timestamp())


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, rows: list[list[float]]) -> None:
        self._rows = rows

    def json(self) -> list[list[float]]:
        return self._rows


class FakeSession:
    """Answers like Coinbase: every minute of [start, end] that is not a hole, newest first, as
    [time, low, high, open, close, volume]. Records each request."""

    def __init__(self, holes: set[int] | None = None, fail_at: int | None = None, extra: list | None = None) -> None:
        self.holes = holes or set()
        self.fail_at = fail_at
        self.extra = extra or []
        self.calls: list[dict] = []

    def get(self, url: str, params: dict, timeout: float) -> FakeResponse:
        self.calls.append(dict(params))
        if len(self.calls) == self.fail_at:
            raise RuntimeError("Coinbase candles request failed: HTTP 500")
        start, end = epoch(params["start"]), epoch(params["end"])
        rows = [[t, 1.0, 3.0, 2.0, 2.5, 7.0] for t in range(end, start - 1, -M1) if t not in self.holes]
        return FakeResponse(self.extra + rows)


class RefuseSession:
    def get(self, *a: object, **k: object) -> None:
        raise AssertionError("a request was made")


def frame(ts: list[int]) -> pd.DataFrame:
    prices = {c: 1.0 for c in ("open", "high", "low", "close", "volume")}
    return pd.DataFrame({"ts": pd.Series(ts, dtype="int64"), **prices})


def full() -> np.ndarray:
    return np.arange(START_TS, END_TS, M1, dtype=np.int64)


# ----------------------------------------------------------------------------- the fixed values


def test_the_fixed_values():
    assert (START_TS, END_TS, LAST_TS) == (1_483_228_800, HS, HS - 60)
    assert GRID_BARS == 3_680_640 == 2_556 * 1_440 == (END_TS - START_TS) // M1
    assert MISSING_LIMIT == 184_032 == GRID_BARS * 5 // 100
    assert data.PRODUCT == "BTC-USD"


def test_the_cache_file_is_new_and_separate(tmp_path):
    assert cache_file(tmp_path) == tmp_path / "BTC-USD_1m.parquet"
    assert parts_dir(tmp_path) == tmp_path / "BTC-USD_1m.parts"


# ----------------------------------------------------------------------------- the fetch


def test_a_window_reaching_2024_is_refused_before_any_request(tmp_path):
    with pytest.raises(DataWindowError, match="2024"):
        update_cache_1m(tmp_path, session=RefuseSession(), sleep_s=0, start_ts=HS - PAGE, end_ts=HS + M1)
    assert not cache_file(tmp_path).exists() and not parts_dir(tmp_path).exists()


def test_requests_hold_at_most_300_bars_and_stop_at_the_last_minute_of_2023(tmp_path):
    s = FakeSession()
    update_cache_1m(tmp_path, session=s, sleep_s=0, start_ts=HS - 2 * PAGE)
    assert [(c["granularity"], c["start"], c["end"]) for c in s.calls] == [
        (60, "2023-12-31T14:00:00Z", "2023-12-31T18:59:00Z"),
        (60, "2023-12-31T19:00:00Z", "2023-12-31T23:59:00Z"),
    ]


def test_the_whole_window_is_12269_pages():
    assert len(data._page_starts(START_TS, END_TS)) == 12_269


def test_a_row_from_2024_in_a_response_is_never_stored(tmp_path):
    s = FakeSession(extra=[[HS, 1.0, 3.0, 2.0, 2.5, 7.0]])
    update_cache_1m(tmp_path, session=s, sleep_s=0, start_ts=HS - PAGE)
    raw = pd.read_parquet(cache_file(tmp_path))
    assert int(raw["ts"].max()) == HS - M1 and len(raw) == 300


def test_only_the_bars_the_exchange_gave_are_stored(tmp_path):
    t0 = HS - PAGE
    update_cache_1m(tmp_path, session=FakeSession(holes={t0 + 60, t0 + 120}), sleep_s=0, start_ts=t0)
    raw = pd.read_parquet(cache_file(tmp_path))
    assert len(raw) == 298 and raw["ts"].is_monotonic_increasing
    assert list(raw.columns) == ["ts", "open", "high", "low", "close", "volume"]
    row = raw.iloc[0]
    assert (row["open"], row["high"], row["low"], row["close"], row["volume"]) == (2.0, 3.0, 1.0, 2.5, 7.0)


def test_a_stopped_download_goes_on_from_the_last_saved_page(tmp_path):
    t0 = HS - 4 * PAGE
    first = FakeSession(fail_at=3)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        update_cache_1m(tmp_path, session=first, sleep_s=0, start_ts=t0)
    assert not cache_file(tmp_path).exists()  # the cache file exists only when the download is complete
    assert sorted(p.name for p in parts_dir(tmp_path).glob("*.parquet")) == [f"{t0}.parquet", f"{t0 + PAGE}.parquet"]
    second = FakeSession()
    update_cache_1m(tmp_path, session=second, sleep_s=0, start_ts=t0)
    assert [epoch(c["start"]) for c in second.calls] == [t0 + 2 * PAGE, t0 + 3 * PAGE]
    raw = pd.read_parquet(cache_file(tmp_path))
    assert raw["ts"].tolist() == list(range(t0, HS, M1))
    assert not parts_dir(tmp_path).exists()


def test_a_page_with_no_bars_is_saved_and_not_requested_again(tmp_path):
    t0 = HS - 2 * PAGE
    first = FakeSession(holes=set(range(t0, t0 + PAGE, M1)), fail_at=2)
    with pytest.raises(RuntimeError):
        update_cache_1m(tmp_path, session=first, sleep_s=0, start_ts=t0)
    second = FakeSession()
    update_cache_1m(tmp_path, session=second, sleep_s=0, start_ts=t0)
    assert [epoch(c["start"]) for c in second.calls] == [t0 + PAGE]
    assert int(pd.read_parquet(cache_file(tmp_path))["ts"].min()) == t0 + PAGE


def test_a_finished_cache_is_not_fetched_again(tmp_path):
    update_cache_1m(tmp_path, session=FakeSession(), sleep_s=0, start_ts=HS - PAGE)
    update_cache_1m(tmp_path, session=RefuseSession(), sleep_s=0, start_ts=HS - PAGE)


def test_a_connection_error_is_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(data.time, "sleep", lambda s: None)

    class Flaky(FakeSession):
        def get(self, url: str, params: dict, timeout: float) -> FakeResponse:
            if not self.calls:
                self.calls.append({})
                raise requests.ConnectionError("reset")
            return super().get(url, params, timeout)

    s = Flaky()
    update_cache_1m(tmp_path, session=s, sleep_s=0, start_ts=HS - PAGE)
    assert len(pd.read_parquet(cache_file(tmp_path))) == 300


# ----------------------------------------------------------------------------- reading


def test_the_read_never_returns_a_bar_from_2024_on(tmp_path):
    frame([HS - 2 * M1, HS - M1, HS, HS + M1]).to_parquet(cache_file(tmp_path), index=False)
    assert load_1m(tmp_path)["ts"].tolist() == [HS - 2 * M1, HS - M1]


def test_the_report_never_counts_a_bar_from_2024_on(tmp_path):
    frame([HS - M1, HS, HS + M1]).to_parquet(cache_file(tmp_path), index=False)
    rep = cache_report(tmp_path)
    assert (rep.rows, rep.last_ts) == (1, HS - M1)


def test_a_frame_with_a_2024_bar_is_refused():
    refuse_2024(frame([HS - M1]))
    refuse_2024(frame([]))
    with pytest.raises(DataWindowError, match="2024"):
        refuse_2024(frame([HS - M1, HS]))


def test_the_missing_count_reads_the_ts_column_only(tmp_path, monkeypatch):
    frame([START_TS, LAST_TS]).to_parquet(cache_file(tmp_path), index=False)
    seen: dict = {}
    real = pd.read_parquet

    def spy(path: Path, **kw: object) -> pd.DataFrame:
        seen.update(kw)
        return real(path, **kw)

    monkeypatch.setattr(pd, "read_parquet", spy)
    cache_report(tmp_path)
    assert seen["columns"] == ["ts"]


# ----------------------------------------------------------------------------- the range and the missing count


def test_a_full_grid_passes_and_nothing_is_missing():
    rep = report_from_ts(full())
    check_cache(rep)
    assert (rep.rows, rep.missing, rep.synthetic_share) == (GRID_BARS, 0, 0.0)


def test_interior_holes_count_even_when_both_ends_are_there():
    ts = full()
    at_limit = report_from_ts(np.delete(ts, slice(1_000, 1_000 + MISSING_LIMIT)))
    assert (at_limit.first_ts, at_limit.last_ts, at_limit.missing) == (START_TS, LAST_TS, 184_032)
    check_cache(at_limit)
    over = report_from_ts(np.delete(ts, slice(1_000, 1_000 + MISSING_LIMIT + 1)))
    assert (over.first_ts, over.last_ts, over.missing) == (START_TS, LAST_TS, 184_033)
    with pytest.raises(MissingBarsError, match="184033.*184032"):
        check_cache(over)


def test_scattered_holes_count_too():
    ts = full()
    rep = report_from_ts(np.delete(ts, np.arange(1, 2 * (MISSING_LIMIT + 1), 2)))
    assert rep.missing == MISSING_LIMIT + 1
    with pytest.raises(MissingBarsError):
        check_cache(rep)


def test_a_quiet_first_or_last_minute_is_counted_not_refused():
    ts = full()
    for cut in (ts[1:], ts[:-1], ts[59:], ts[:-59]):
        check_cache(report_from_ts(cut))
    assert report_from_ts(ts[59:]).missing == 59


def test_a_cache_cut_short_is_refused_under_the_limit():
    ts = full()
    for cut in (ts[60:], ts[:-60], ts[:-100_000]):
        rep = report_from_ts(cut)
        assert rep.missing <= MISSING_LIMIT
        with pytest.raises(DataWindowError, match="2017-01-01"):
            check_cache(rep)


def test_an_empty_cache_is_refused():
    rep = report_from_ts(np.array([], dtype=np.int64))
    assert (rep.rows, rep.first_ts, rep.missing) == (0, None, GRID_BARS)
    with pytest.raises(DataWindowError, match="2017-01-01"):
        check_cache(rep)


def test_bars_off_the_minute_grid_are_refused():
    ts = full()
    ts[10] += 7
    rep = report_from_ts(ts)
    assert rep.off_grid == 1 and rep.missing == 1
    with pytest.raises(DataWindowError, match="off the 1-minute grid"):
        check_cache(rep)


def test_duplicate_rows_do_not_hide_a_hole():
    ts = full()
    ts[10] = ts[9]
    rep = report_from_ts(ts)
    assert (rep.rows, rep.missing) == (GRID_BARS - 1, 1)


def test_the_record_and_the_lines_show_the_synthetic_share():
    rep = CacheReport(rows=GRID_BARS - 36_806, first_ts=START_TS, last_ts=LAST_TS, off_grid=0, missing=36_806)
    assert data_record(rep) == {"rows": GRID_BARS - 36_806, "first_ts": START_TS, "last_ts": LAST_TS, "grid_bars": GRID_BARS,
                                "missing": 36_806, "missing_limit": MISSING_LIMIT, "synthetic_share": 36_806 / GRID_BARS}
    text = "\n".join(report_lines(rep))
    assert "36806 of 3680640 minutes (1.000%)" in text and "limit 184032" in text
```

- [ ] **Step 2: 실패 확인**

```bash
uv run --group live pytest tests/stratf/test_data.py -q
```

예상: 수집 오류 `ImportError: cannot import name 'data' from 'ewbot.stratf'`.

- [ ] **Step 3: 구현**

파일: `src/ewbot/stratf/data.py`
```python
"""Strategy F's 1m bars (Plan 8 spec 4): fetch, cache, the 2024 guard and the missing count.

The cache is <cache_dir>/BTC-USD_1m.parquet, 2017-01-01 00:00 to 2023-12-31 23:59 UTC, with only the bars the exchange gave.
The endpoint and the request pattern are those of ewbot.data.coinbase_history and stratb/data15.py (Coinbase Exchange, no
auth; GET .../candles returns at most 300 rows [time, low, high, open, close, volume], newest first, start and end inclusive
bar-open times; no row for a minute without a trade), with granularity 60. The public limit is 10 requests per second per
IP; 0.15 s between requests stays under 7.

Downloads resume, but not as stratb/data15.py does: rewriting the whole file per page would write hundreds of GB over the
12,269 pages. Each page goes to its own file in BTC-USD_1m.parts/ (a temp file, then renamed), also when it has no bars, so an
interrupted download goes on from the next page. When all pages are there they are joined into the cache file and the
parts are removed, so the cache file exists only when the download is complete.

2024 guard: update_cache_1m refuses a window that reaches 2024-01-01 before any request and never stores a row at or after
it; every read filters ts < 2024-01-01. Nothing in this module can open 2024: holdout-f.lock belongs to a later plan.

The missing count (spec 4): cache_report reads the ts column only, against the whole grid of 3,680,640 minutes, so holes
inside the window and at its ends all count. More than 184,032 ends strategy F before stage 1; check_cache refuses it."""

from __future__ import annotations

import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

from ewbot.backtest.runner import utc_ts
from ewbot.backtest.walkforward import HOLDOUT_START, WF_START
from ewbot.data.coinbase_history import API, COLUMNS, MAX_PER_REQUEST, _get_json
from ewbot.stratf.config import M1
from ewbot.types import H1

PRODUCT = "BTC-USD"
START_TS = utc_ts(WF_START)
END_TS = HOLDOUT_START  # exclusive
LAST_TS = END_TS - M1
GRID_BARS = 3_680_640  # 2,556 days of minutes
MISSING_LIMIT = 184_032  # 5% of the grid: spec 4; not an option
EDGE_SLACK = H1  # a first or last bar this far from the window's end means a cut cache, not a quiet minute
FETCH_TRIES = 3  # per page, on a connection error or a timeout
FETCH_BACKOFF_S = 1.0  # waits 1 s, then 2 s


class DataWindowError(ValueError):
    """The bars reach 2024, or do not cover the window."""


class MissingBarsError(DataWindowError):
    """More bars are missing than spec 4 allows: strategy F ends before stage 1."""


def cache_file(cache_dir: Path) -> Path:
    return Path(cache_dir) / f"{PRODUCT}_1m.parquet"


def parts_dir(cache_dir: Path) -> Path:
    return Path(cache_dir) / f"{PRODUCT}_1m.parts"


def _empty() -> pd.DataFrame:
    return pd.DataFrame({"ts": pd.Series(dtype="int64"), **{c: pd.Series(dtype="float64") for c in COLUMNS[1:]}})


def _standard(df: pd.DataFrame) -> pd.DataFrame:
    df = df[COLUMNS].astype({"ts": "int64", **{c: "float64" for c in COLUMNS[1:]}})
    return df.drop_duplicates("ts").sort_values("ts", kind="stable").reset_index(drop=True)


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows_frame(rows: list[list[float]], first: int, last_open: int) -> pd.DataFrame:
    if not rows:
        return _empty()
    df = pd.DataFrame(rows, columns=["ts", "low", "high", "open", "close", "volume"])
    df["ts"] = df["ts"].astype("int64")
    return _standard(df[(df["ts"] >= first) & (df["ts"] <= last_open) & (df["ts"] < END_TS)])


def _get_page(sess: requests.Session, url: str, params: dict[str, object]) -> list:
    """_get_json, also retried on a connection error or a timeout (FETCH_TRIES attempts in all)."""
    failed = 0
    while True:
        try:
            return _get_json(sess, url, params)
        except (requests.ConnectionError, requests.Timeout):
            failed += 1
            if failed >= FETCH_TRIES:
                raise
            time.sleep(FETCH_BACKOFF_S * 2 ** (failed - 1))


def _save(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def _page_starts(start_ts: int, end_ts: int) -> list[int]:
    return list(range(start_ts, end_ts, MAX_PER_REQUEST * M1))


def update_cache_1m(
    cache_dir: Path,
    *,
    session: requests.Session | None = None,
    sleep_s: float = 0.15,
    start_ts: int = START_TS,
    end_ts: int = END_TS,
) -> Path:
    """Fetch [start_ts, end_ts) into the cache file, page by page, going on from any page already saved. The command has no
    window option; the two bounds are here for the tests. DataWindowError, before any request, when the window reaches 2024."""
    if end_ts > END_TS:
        raise DataWindowError("the window reaches 2024-01-01: strategy F's stages 0-2 request nothing from 2024 on")
    path = cache_file(cache_dir)
    if path.exists():
        return path
    parts = parts_dir(cache_dir)
    parts.mkdir(parents=True, exist_ok=True)
    sess = session if session is not None else requests.Session()
    url = API.format(product=PRODUCT)
    starts = _page_starts(start_ts, end_ts)
    requested = 0
    for n, w_start in enumerate(starts, 1):
        part = parts / f"{w_start}.parquet"
        if part.exists():
            continue
        if requested and sleep_s > 0:
            time.sleep(sleep_s)
        w_end = min(w_start + (MAX_PER_REQUEST - 1) * M1, end_ts - M1)
        rows = _get_page(sess, url, {"granularity": M1, "start": _iso(w_start), "end": _iso(w_end)})
        _save(_rows_frame(rows, w_start, w_end), part)
        requested += 1
        if requested % 500 == 0:
            print(f"[fetch] page {n}/{len(starts)}", file=sys.stderr, flush=True)
    frames = [pd.read_parquet(parts / f"{s}.parquet") for s in starts]
    frames = [f for f in frames if len(f)]
    joined = _standard(pd.concat(frames, ignore_index=True)) if frames else _empty()
    _save(joined[joined["ts"] < END_TS], path)
    shutil.rmtree(parts)
    return path


def load_1m(cache_dir: Path) -> pd.DataFrame:
    """The cached bars before 2024-01-01 (standard schema, sorted, unique ts): the parquet read filters out the rest."""
    df = _standard(pd.read_parquet(cache_file(cache_dir), filters=[("ts", "<", END_TS)]))
    refuse_2024(df)
    return df


def refuse_2024(df: pd.DataFrame) -> None:
    if len(df) and int(df["ts"].max()) >= END_TS:
        raise DataWindowError("the bars reach 2024-01-01: strategy F's stages 0-2 read nothing from 2024 on")


@dataclass(frozen=True, slots=True)
class CacheReport:
    rows: int
    first_ts: int | None
    last_ts: int | None
    off_grid: int  # bars that do not open on a minute
    missing: int  # minutes of the grid with no bar

    @property
    def synthetic_share(self) -> float:
        """The share of the grid that fill_gaps fills with flat bars."""
        return self.missing / GRID_BARS


def report_from_ts(ts: np.ndarray) -> CacheReport:
    uniq = np.unique(np.asarray(ts, dtype=np.int64))
    on_grid = uniq[uniq % M1 == 0]
    inside = on_grid[(on_grid >= START_TS) & (on_grid < END_TS)]
    return CacheReport(
        rows=int(uniq.size),
        first_ts=int(uniq[0]) if uniq.size else None,
        last_ts=int(uniq[-1]) if uniq.size else None,
        off_grid=int(uniq.size - on_grid.size),
        missing=GRID_BARS - int(inside.size),
    )


def cache_report(cache_dir: Path) -> CacheReport:
    """Reads the ts column only, and no row from 2024 on: no price is touched."""
    df = pd.read_parquet(cache_file(cache_dir), columns=["ts"], filters=[("ts", "<", END_TS)])
    return report_from_ts(df["ts"].to_numpy(dtype=np.int64))


def check_cache(rep: CacheReport) -> None:
    """Spec 6: the window and the number of holes, not the two ends alone. Either failure comes before any run."""
    first, last = rep.first_ts, rep.last_ts
    in_range = first is not None and last is not None and START_TS <= first < START_TS + EDGE_SLACK and (
        LAST_TS - EDGE_SLACK < last <= LAST_TS
    )
    if not in_range:
        raise DataWindowError(
            f"the cache must run from 2017-01-01 00:00 to 2023-12-31 23:59 UTC (within an hour at each end), got ts {first} .. "
            f"{last}: the count needs the whole window"
        )
    if rep.off_grid:
        raise DataWindowError(f"{rep.off_grid} bars are off the 1-minute grid")
    if rep.missing > MISSING_LIMIT:
        raise MissingBarsError(
            f"{rep.missing} bars are missing of {GRID_BARS}, over the limit of {MISSING_LIMIT} (5%): spec 4 ends strategy F "
            "here, before stage 1; no higher limit, no other exchange's data"
        )


def data_record(rep: CacheReport) -> dict[str, Any]:
    return {"rows": rep.rows, "first_ts": rep.first_ts, "last_ts": rep.last_ts, "grid_bars": GRID_BARS, "missing": rep.missing,
            "missing_limit": MISSING_LIMIT, "synthetic_share": rep.synthetic_share}


def report_lines(rep: CacheReport) -> list[str]:
    return [
        f"1m cache: {rep.rows} rows, ts {rep.first_ts} .. {rep.last_ts}, {rep.off_grid} off the minute grid",
        f"missing bars: {rep.missing} of {GRID_BARS} minutes ({rep.synthetic_share:.3%}); limit {MISSING_LIMIT} (5%)",
    ]
```

- [ ] **Step 4: 통과 확인**

```bash
uv run --group live pytest tests/stratf/test_data.py tests/stratf/test_isolation.py -q
```

예상: `37 passed`. 네트워크를 쓰지 않는다.

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratf/data.py tests/stratf/test_data.py
git commit -m "feat(stratf): 1m fetch with a file per page, the 2024 guard, a missing count over the whole grid from ts alone" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: 프로세스 풀과 횟수 점검

**Files:**
- Create: `src/ewbot/stratf/pool.py`, `src/ewbot/stratf/count.py`
- Test: `tests/stratf/test_count.py`

**Interfaces:**
- Consumes: `ewbot.backtest.walkforward`의 `GRID`, `WF_END`, `WF_START`, `Combo`, `config_for(cfg, combo)`, `grid_combos() -> list[Combo]`; `ewbot.config.with_overrides(cfg, **dotted)`; `ewbot.strategy.hybrid.make_strategy(sp)`; `stratf.engine.TradingEngineF`; `stratf.config.base_config`; `stratf.data`의 `START_TS`, `END_TS`, `CacheReport`, `DataWindowError`, `data_record`, `refuse_2024`.
- Produces:
  - `stratf.pool.run_combos(fn: Callable[[Any], tuple[Any, Any]], setup: Callable[..., Any], initargs: tuple[Any, ...], combos: Sequence[Any], workers: int, on_done: Callable[[Any, Any], None] | None = None) -> dict[Any, Any]`
  - `stratf.count`: `MONTHS = 84`, `THRESHOLD_ENTRIES = 336`, `THRESHOLD_MONTHLY = 4.0`, `REPORT_KIND = "ewbot-stratf-count"`, `PROCEED`, `STOP`, `ByYear = dict[int, dict[str, int]]`, `tally_entries(trades: Iterable[Any]) -> ByYear`, `_setup_worker(df1m: pd.DataFrame) -> None`, `_count_combo(combo: Combo) -> tuple[Combo, ByYear]`, `run_count(df1m: pd.DataFrame, workers: int = 1, combos: Sequence[Combo] | None = None, on_done: Callable[[Combo, ByYear], None] | None = None) -> dict[Combo, ByYear]`, `checkpoint_path(ckpt_dir: Path, combo: Combo) -> Path`, `save_done(ckpt_dir: Path, combo: Combo, by_year: ByYear, data: Mapping[str, Any]) -> None`, `load_done(ckpt_dir: Path, data: Mapping[str, Any]) -> dict[Combo, ByYear]`, `decide(totals: Mapping[Combo, int]) -> dict[str, Any]`, `build_report(results: Mapping[Combo, ByYear], rep: CacheReport) -> dict[str, Any]`, `report_lines(rep: Mapping[str, Any]) -> list[str]`, `count_stage(df1m: pd.DataFrame, rep: CacheReport, workers: int, ckpt_dir: Path) -> dict[str, Any]`

- [ ] **Step 1: 실패하는 테스트 작성**

파일: `tests/stratf/test_count.py`
```python
from __future__ import annotations

import ast
import json
import os
import re
from collections import Counter
from concurrent.futures.process import BrokenProcessPool
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
from ewbot.stratf import count
from ewbot.stratf.config import M1, base_config
from ewbot.stratf.data import GRID_BARS, MISSING_LIMIT, CacheReport, DataWindowError
from ewbot.stratf.engine import TradingEngineF
from ewbot.stratf.pool import run_combos
from tests.stratf.helpers import a_frame, stretch

FIRST, LAST = utc_ts("2017-01-01"), HOLDOUT_START - M1
FRAME = pd.DataFrame({"ts": pd.Series([FIRST, LAST], dtype="int64")})
REP = CacheReport(rows=2, first_ts=FIRST, last_ts=LAST, off_grid=0, missing=GRID_BARS - 2)
SHORT = (2.0, 0.5, 0.0)
BAD = (-1.0, 0.5, 0.0)  # ZigZag refuses it inside the worker


def fake_trade(entry_ts: int, setup: str) -> Trade:
    return Trade(entry_ts=entry_ts, exit_ts=entry_ts + 60, setup=setup, direction=1, contracts=1, entry_price=100.0,
                 exit_price_avg=101.0, stop0=95.0, tp1=105.0, tp2=110.0, pnl=12.0, fees=0.1, funding=0.2, r_multiple=1.2,
                 exit_reason="STOP", snapshot={})


def totals(n: int) -> dict[tuple[float, ...], int]:
    return {c: n for c in grid_combos()}


def results(n: int) -> dict[tuple[float, ...], dict[int, dict[str, int]]]:
    return {c: {2019: {"W3": n}} for c in grid_combos()}


# ----------------------------------------------------------------------------- counting


def test_the_fixed_values():
    assert (count.MONTHS, count.THRESHOLD_ENTRIES, count.THRESHOLD_MONTHLY) == (84, 336, 4.0)
    assert count.THRESHOLD_ENTRIES == count.MONTHS * count.THRESHOLD_MONTHLY
    assert len(grid_combos()) == 4 * 5 * 3 == 60


def test_tally_groups_by_the_utc_year_of_the_entry_and_the_setup():
    boundary = utc_ts("2018-01-01")
    trades = [fake_trade(boundary - M1, "W3"), fake_trade(boundary, "W3"), fake_trade(boundary, "C_END"),
              fake_trade(boundary + M1, "W3")]
    assert count.tally_entries(trades) == {2017: {"W3": 1}, 2018: {"W3": 2, "C_END": 1}}


def test_the_worker_runs_breakers_off_over_the_window_and_returns_counts_only(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_run(self, df1m, start_ts, end_ts):
        seen.update(cfg=self.cfg, start=start_ts, end=end_ts, df=df1m)
        trades = (fake_trade(utc_ts("2019-03-01"), "W3"), fake_trade(utc_ts("2019-04-01"), "W3"),
                  fake_trade(utc_ts("2021-05-01"), "C_END"))
        return EngineResult(trades, (), (), ())

    monkeypatch.setattr(count.TradingEngineF, "run", fake_run)
    marker = object()
    count._setup_worker(marker)
    combo = (2.5, 0.55, 0.25)
    assert count._count_combo(combo) == (combo, {2019: {"W3": 2}, 2021: {"C_END": 1}})
    cfg = seen["cfg"]
    assert cfg.breakers.enabled is False and (cfg.wave.K4, cfg.strategy.Q_MIN, cfg.strategy.S_MIN) == combo
    assert (seen["start"], seen["end"]) == (utc_ts("2017-01-01"), utc_ts("2024-01-01")) and seen["df"] is marker


def test_a_frame_reaching_2024_is_refused_before_any_run():
    with pytest.raises(DataWindowError, match="2024"):
        count.run_count(pd.DataFrame({"ts": [HOLDOUT_START]}), combos=[SHORT])


def test_counts_are_the_entries_of_one_engine_run():
    df = stretch(a_frame())
    combo = SHORT
    got = count.run_count(df, combos=[combo])
    cfg = config_for(with_overrides(base_config(), **{"breakers.enabled": False}), combo)
    res = TradingEngineF(cfg, make_strategy(cfg.strategy)).run(df, count.START_TS, count.END_TS)
    want = Counter((datetime.fromtimestamp(t.entry_ts, UTC).year, t.setup) for t in res.trades)
    assert sum(want.values()) >= 3
    assert Counter({(y, s): n for y, by_setup in got[combo].items() for s, n in by_setup.items()}) == want


def test_two_worker_processes_engine_counts_match_one_process():
    df = stretch(a_frame()).iloc[:3_000]
    combos = [SHORT, (3.0, 0.6, 0.25)]
    assert count.run_count(df, workers=2, combos=combos) == count.run_count(df, workers=1, combos=combos)


# ----------------------------------------------------------------------------- a failed worker (spec 6)


def test_a_failing_worker_aborts_the_run():
    """The real pool: the worker for BAD raises inside the engine, and the run raises. BAD never becomes zero entries."""
    saved: list[tuple[float, ...]] = []
    with pytest.raises(ValueError, match="k must be > 0"):
        count.run_count(stretch(a_frame()).iloc[:500], workers=2, combos=[SHORT, BAD], on_done=lambda c, by: saved.append(c))
    assert BAD not in saved


def test_a_failure_in_one_process_aborts_the_run():
    saved: list[tuple[float, ...]] = []
    with pytest.raises(ValueError, match="k must be > 0"):
        count.run_count(stretch(a_frame()).iloc[:500], workers=1, combos=[SHORT, BAD], on_done=lambda c, by: saved.append(c))
    assert saved == [SHORT]


def test_a_killed_worker_aborts_the_run():
    """A worker that dies (out of memory, say) leaves no exception to pickle: the pool breaks and the run still raises."""
    with pytest.raises(BrokenProcessPool):
        run_combos(os._exit, len, ([],), [3], workers=2)


def test_results_come_back_in_the_order_of_the_combos():
    def fn(combo: int) -> tuple[int, int]:
        return combo, combo * 2

    seen: list[int] = []
    out = run_combos(fn, len, ([],), [3, 1, 2], workers=1, on_done=lambda c, v: seen.append(c))
    assert list(out.items()) == [(3, 6), (1, 2), (2, 4)] and seen == [3, 1, 2]


# ----------------------------------------------------------------------------- going on after a stop (spec 5.1)


def test_a_saved_combo_is_read_back_and_a_file_holds_counts_only(tmp_path):
    data = {"rows": 2}
    count.save_done(tmp_path, SHORT, {2019: {"W3": 2}}, data)
    assert count.checkpoint_path(tmp_path, SHORT) == tmp_path / "2.0_0.5_0.0.json"
    assert count.load_done(tmp_path, data) == {SHORT: {2019: {"W3": 2}}}
    saved = json.loads(count.checkpoint_path(tmp_path, SHORT).read_text(encoding="utf-8"))
    assert set(saved) == {"combo", "by_year", "data"}


def test_a_stopped_count_goes_on_with_the_combos_not_yet_saved(tmp_path, monkeypatch):
    data = count.data_record(REP)
    for c in grid_combos()[:7]:
        count.save_done(tmp_path, c, {2019: {"W3": 336}}, data)
    seen: dict[str, Any] = {}

    def fake_run(df1m, workers, combos, on_done):
        seen.update(combos=list(combos), workers=workers)
        out = {c: {2019: {"W3": 336}} for c in combos}
        for c, by_year in out.items():
            on_done(c, by_year)
        return out

    monkeypatch.setattr(count, "run_count", fake_run)
    rep = count.count_stage(FRAME, REP, 5, tmp_path)
    assert seen == {"combos": grid_combos()[7:], "workers": 5}
    assert len(rep["combos"]) == 60 and rep["verdict"] == count.PROCEED
    assert len(list(tmp_path.glob("*.json"))) == 60


def test_a_finished_count_runs_nothing(tmp_path, monkeypatch):
    data = count.data_record(REP)
    for c in grid_combos():
        count.save_done(tmp_path, c, {2019: {"W3": 1}}, data)

    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("a run was started")

    monkeypatch.setattr(count, "run_count", boom)
    assert count.count_stage(FRAME, REP, 5, tmp_path)["verdict"] == count.STOP


def test_saved_counts_of_another_cache_are_refused(tmp_path):
    count.save_done(tmp_path, SHORT, {2019: {"W3": 2}}, {"rows": 1})
    with pytest.raises(DataWindowError, match="another cache"):
        count.load_done(tmp_path, {"rows": 2})


def test_a_broken_saved_file_stops_the_stage_and_is_not_zero(tmp_path):
    count.checkpoint_path(tmp_path, SHORT).write_text('{"combo": [2.0', encoding="utf-8")
    with pytest.raises(ValueError):
        count.load_done(tmp_path, {"rows": 2})


def test_a_frame_that_is_not_the_reported_cache_is_refused(tmp_path):
    with pytest.raises(DataWindowError, match="not the cache"):
        count.count_stage(FRAME.iloc[:1], REP, 1, tmp_path)


# ----------------------------------------------------------------------------- the gate


def test_a_median_of_exactly_336_entries_proceeds():
    out = count.decide(totals(336))
    assert out == {"threshold_entries": 336, "threshold_monthly": 4.0, "n_combos": 60, "median_entries": 336,
                   "median_monthly": 4.0, "passed": True, "verdict": count.PROCEED}


def test_an_even_median_is_the_mean_of_the_middle_two():
    t = {c: (335 if k < 30 else 337) for k, c in enumerate(grid_combos())}
    assert count.decide(t)["verdict"] == count.PROCEED
    t = {c: (335 if k < 30 else 336) for k, c in enumerate(grid_combos())}
    out = count.decide(t)
    assert out["median_entries"] == 335.5 and out["verdict"] == count.STOP


def test_below_the_threshold_it_stops_and_there_is_no_fallback():
    out = count.decide(totals(335))
    assert out["passed"] is False and out["verdict"] == count.STOP
    high_q_low = totals(0)
    high_q_low.update({(k4, 0.5, 0.0): 1_000 for k4 in GRID["wave.K4"]})  # D's fallback combos
    assert count.decide(high_q_low)["verdict"] == count.STOP
    assert not hasattr(count, "fallback_combos")


def test_the_gate_takes_the_60_combos_only():
    with pytest.raises(ValueError, match="60"):
        count.decide({SHORT: 500})


# ----------------------------------------------------------------------------- the report


def test_the_report_has_per_combo_counts_the_median_the_verdict_and_the_synthetic_share():
    res = results(84)
    res[SHORT] = {2023: {"W5": 2}, 2017: {"W3": 100, "C_END": 8}}
    rep = count.build_report(res, REP)
    row = next(r for r in rep["combos"] if r["combo"] == [2.0, 0.5, 0.0])
    assert row == {"combo": [2.0, 0.5, 0.0], "entries": 110, "monthly": 110 / 84,
                   "by_year": {"2017": {"C_END": 8, "W3": 100}, "2023": {"W5": 2}}}
    assert list(row["by_year"]) == ["2017", "2023"]
    assert len(rep["combos"]) == 60 and rep["keys"] == list(GRID)
    assert rep["kind"] == count.REPORT_KIND
    assert rep["window"] == {"start": "2017-01-01", "end": "2024-01-01", "months": 84}
    assert rep["data"] == {"rows": 2, "first_ts": FIRST, "last_ts": LAST, "grid_bars": GRID_BARS, "missing": GRID_BARS - 2,
                           "missing_limit": MISSING_LIMIT, "synthetic_share": (GRID_BARS - 2) / GRID_BARS}
    assert rep["median_monthly"] == 1.0 and rep["verdict"] == count.STOP
    json.dumps(rep)


def test_the_printed_lines_show_each_combo_the_median_the_share_and_the_verdict():
    rep = count.build_report(results(336), CacheReport(GRID_BARS - 100, FIRST, LAST, 0, 100))
    lines = count.report_lines(rep)
    assert "K4=2.0 Q_MIN=0.5 S_MIN=0.0: 336 entries, 4.00 per month" in lines
    assert sum(line.startswith("K4=") for line in lines) == 60
    assert any("median of 60 combos = 336 entries, 4.000 per month" in line for line in lines)
    assert any(line.startswith("synthetic bars: 100 of 3680640 minutes (0.003%)") for line in lines)
    assert "verdict: PROCEED" in lines


# ----------------------------------------------------------------------------- no performance value (spec 5.1, 6)

ALLOWED_KEYS = {"kind", "window", "start", "end", "months", "data", "first_ts", "last_ts", "rows", "grid_bars", "missing",
                "missing_limit", "synthetic_share", "keys", "combos", "combo", "entries", "monthly", "by_year",
                "threshold_entries", "threshold_monthly", "n_combos", "median_entries", "median_monthly", "passed", "verdict",
                "W3", "W5", "W5_REV", "C_END"}
PERFORMANCE_WORDS = re.compile(
    r"\b(pnl|profit|loss|returns?|sharpe|sortino|calmar|drawdown|max_dd|cagr|win_rate|avg_r|r_multiple|equity|exposure|"
    r"exit_reason)\b", re.I)
PERFORMANCE_MODULES = {"metrics", "report", "benchmarks", "holdout", "research"}
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
        "from ewbot.stratf import research",
        "compute_metrics(a, b)",
        "t.pnl",
        "res.equity",
        "t.r_multiple",
    ],
)
def test_the_performance_guard_catches(src):
    assert _performance_refs(ast.parse(src))


def test_count_py_refers_to_no_performance_code_and_no_trade_result():
    """Direct references only. backtest.walkforward loads backtest.metrics as its own import, which cannot change without
    editing that file; the next test shows a run never calls it."""
    tree = ast.parse(Path(count.__file__).read_text(encoding="utf-8"))
    assert _performance_refs(tree) == []


def test_a_count_engine_run_never_calls_the_metrics(monkeypatch):
    def boom(*args: Any, **kw: Any) -> None:
        raise AssertionError("the count called a performance function")

    monkeypatch.setattr("ewbot.backtest.metrics.compute_metrics", boom)
    monkeypatch.setattr("ewbot.backtest.walkforward.compute_metrics", boom)
    count.run_count(stretch(a_frame()).iloc[:2_000], combos=[SHORT])


def _keys(o: Any) -> set[str]:
    if isinstance(o, dict):
        return set(o) | {k for v in o.values() for k in _keys(v)}
    if isinstance(o, (list, tuple)):
        return {k for v in o for k in _keys(v)}
    return set()


@pytest.mark.parametrize("n", [336, 335], ids=["proceed", "stop"])
def test_the_report_and_the_printed_lines_hold_no_performance_value(n):
    rep = count.build_report(results(n), REP)
    years = {k for k in _keys(rep) if re.fullmatch(r"\d{4}", k)}
    assert _keys(rep) - years <= ALLOWED_KEYS
    assert not PERFORMANCE_WORDS.search(json.dumps(rep))
    assert not PERFORMANCE_WORDS.search("\n".join(count.report_lines(rep)))
```

- [ ] **Step 2: 실패 확인**

```bash
uv run --group live pytest tests/stratf/test_count.py -q
```

예상: 수집 오류 `ImportError: cannot import name 'count' from 'ewbot.stratf'`.

- [ ] **Step 3: 구현**

파일: `src/ewbot/stratf/pool.py`
```python
"""The process pool of both stages (Plan 8 spec 6): one task per combo, and any failure stops the stage.

The workers are spawned, which needs the main guard of the `ewbot` entry point. 12 workers hold about 30 GB, see
config.MAX_DEFAULT_WORKERS."""

from __future__ import annotations

import multiprocessing as mp
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any


def run_combos(
    fn: Callable[[Any], tuple[Any, Any]],
    setup: Callable[..., Any],
    initargs: tuple[Any, ...],
    combos: Sequence[Any],
    workers: int,
    on_done: Callable[[Any, Any], None] | None = None,
) -> dict[Any, Any]:
    """{combo: the value of fn(combo) = (combo, value)}, in the order of ``combos``. An exception in a worker or a worker that
    dies raises here and cancels what has not started: a failed combo never becomes an empty result. on_done runs in this
    process for each finished combo, as it finishes."""
    out: dict[Any, Any] = {}
    t0 = time.perf_counter()

    def done(combo: Any, value: Any) -> None:
        out[combo] = value
        if on_done is not None:
            on_done(combo, value)
        print(f"[stratf] {len(out)}/{len(combos)} {combo} {time.perf_counter() - t0:.0f}s", file=sys.stderr, flush=True)

    if workers <= 1:
        setup(*initargs)
        for c in combos:
            done(*fn(c))
    elif combos:
        with ProcessPoolExecutor(
            max_workers=min(workers, len(combos)),
            mp_context=mp.get_context("spawn"),
            initializer=setup,
            initargs=initargs,
        ) as pool:
            futures = [pool.submit(fn, c) for c in combos]
            try:
                for future in as_completed(futures):
                    done(*future.result())
            except BaseException:
                pool.shutdown(wait=True, cancel_futures=True)
                raise
    return {c: out[c] for c in combos}
```

파일: `src/ewbot/stratf/count.py`
```python
"""Stage 1 of strategy F (Plan 8 spec 5.1): how many trades, and nothing else.

Every combo runs once through the engine over [2017-01-01, 2024-01-01) with the breakers off, as in the walk-forward: one
position at a time, flips and cooldowns depend on the path, so counting signals would be wrong. A worker reads the entry time
and the setup of each trade and returns counts by UTC year and setup. It never reads what a trade made or the equity curve,
and this module refers to no performance code (backtest.walkforward loads backtest.metrics as its own import; nothing here
calls it).

The gate: a median over the 60 combos of at least 336 entries (4.0 per month over 84 months) proceeds. Otherwise strategy F
ends: there is no fallback. The window and the threshold are constants, not options.

Each finished combo is saved to its own file, so a stopped count goes on with the rest. The files hold counts only. They are
bound to the cache they were counted on. A worker that fails stops the stage and no report is written.

Run it through `ewbot stratf count`: the workers are spawned, which needs the main guard of that entry point."""

from __future__ import annotations

import json
import os
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ewbot.backtest.walkforward import GRID, WF_END, WF_START, Combo, config_for, grid_combos
from ewbot.config import with_overrides
from ewbot.strategy.hybrid import make_strategy
from ewbot.stratf.config import base_config
from ewbot.stratf.data import END_TS, START_TS, CacheReport, DataWindowError, data_record, refuse_2024
from ewbot.stratf.engine import TradingEngineF
from ewbot.stratf.pool import run_combos

MONTHS = 84  # 2017-01 .. 2023-12
THRESHOLD_ENTRIES = 336  # the median over the 60 combos
THRESHOLD_MONTHLY = 4.0
REPORT_KIND = "ewbot-stratf-count"
PROCEED = "PROCEED"
STOP = "STOP"
_NEXT = {
    PROCEED: "next: report to the user; spec 5.2: stage 2 follows without a further approval",
    STOP: "spec 5.1: strategy F ends here; no walk-forward, no other variant",
}
_FLAT_NOTE = "flat bars can lower the 1m ATR, the wick clip and the 5m ATR of the ZigZag"

ByYear = dict[int, dict[str, int]]

_WORKER: dict[str, Any] = {}  # per-process, set by the pool initializer


def tally_entries(trades: Iterable[Any]) -> ByYear:
    """{UTC year of the entry: {setup: entries}}. Reads entry_ts and setup only."""
    out: ByYear = {}
    for t in trades:
        by_setup = out.setdefault(datetime.fromtimestamp(t.entry_ts, UTC).year, {})
        by_setup[t.setup] = by_setup.get(t.setup, 0) + 1
    return out


def _setup_worker(df1m: pd.DataFrame) -> None:
    _WORKER["df1m"] = df1m


def _count_combo(combo: Combo) -> tuple[Combo, ByYear]:
    """One engine run of one combo, breakers off. Returns the counts and nothing else."""
    cfg = config_for(with_overrides(base_config(), **{"breakers.enabled": False}), combo)
    res = TradingEngineF(cfg, make_strategy(cfg.strategy)).run(_WORKER["df1m"], START_TS, END_TS)
    return combo, tally_entries(res.trades)


def run_count(
    df1m: pd.DataFrame,
    workers: int = 1,
    combos: Sequence[Combo] | None = None,
    on_done: Callable[[Combo, ByYear], None] | None = None,
) -> dict[Combo, ByYear]:
    """Every combo once (default: the 60 of GRID). Refuses bars from 2024 on before any run; a failed worker raises."""
    refuse_2024(df1m)
    todo = grid_combos() if combos is None else list(combos)
    return run_combos(_count_combo, _setup_worker, (df1m,), todo, workers, on_done)


def checkpoint_path(ckpt_dir: Path, combo: Combo) -> Path:
    return Path(ckpt_dir) / ("_".join(str(x) for x in combo) + ".json")


def save_done(ckpt_dir: Path, combo: Combo, by_year: ByYear, data: Mapping[str, Any]) -> None:
    path = checkpoint_path(ckpt_dir, combo)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"combo": list(combo), "by_year": by_year, "data": dict(data)}), encoding="utf-8")
    os.replace(tmp, path)


def load_done(ckpt_dir: Path, data: Mapping[str, Any]) -> dict[Combo, ByYear]:
    """The combos already counted on this cache. A file of another cache or a broken file raises: it is never a zero."""
    out: dict[Combo, ByYear] = {}
    for c in grid_combos():
        path = checkpoint_path(ckpt_dir, c)
        if not path.exists():
            continue
        rec = json.loads(path.read_text(encoding="utf-8"))
        if rec["data"] != dict(data):
            raise DataWindowError(f"{path} was counted on another cache: remove the folder {ckpt_dir} to count again")
        out[c] = {int(y): dict(by_setup) for y, by_setup in rec["by_year"].items()}
    return out


def decide(totals: Mapping[Combo, int]) -> dict[str, Any]:
    """Spec 5.1 on each combo's entries over the window: a whole number of entries, so no float can fall a hair below."""
    combos = grid_combos()
    if set(totals) != set(combos):
        raise ValueError(f"the gate needs the {len(combos)} grid combos, got {len(totals)}")
    median = statistics.median(totals.values())
    passed = median >= THRESHOLD_ENTRIES
    return {
        "threshold_entries": THRESHOLD_ENTRIES,
        "threshold_monthly": THRESHOLD_MONTHLY,
        "n_combos": len(combos),
        "median_entries": median,
        "median_monthly": median / MONTHS,
        "passed": passed,
        "verdict": PROCEED if passed else STOP,
    }


def build_report(results: Mapping[Combo, ByYear], rep: CacheReport) -> dict[str, Any]:
    totals = {c: sum(n for by_setup in by_year.values() for n in by_setup.values()) for c, by_year in results.items()}
    gate = decide(totals)
    rows = [
        {
            "combo": list(c),
            "entries": totals[c],
            "monthly": totals[c] / MONTHS,
            "by_year": {str(y): dict(sorted(results[c][y].items())) for y in sorted(results[c])},
        }
        for c in grid_combos()
    ]
    return {
        "kind": REPORT_KIND,
        "window": {"start": WF_START, "end": WF_END, "months": MONTHS},
        "data": data_record(rep),
        "keys": list(GRID),
        "combos": rows,
        **gate,
    }


def report_lines(rep: Mapping[str, Any]) -> list[str]:
    win, data = rep["window"], rep["data"]
    lines = [
        f"strategy F entries, {win['start']} .. {win['end']} (end exclusive), {win['months']} months",
        f"synthetic bars: {data['missing']} of {data['grid_bars']} minutes ({data['synthetic_share']:.3%}); {_FLAT_NOTE}",
    ]
    for row in rep["combos"]:
        k4, q, s = row["combo"]
        lines.append(f"K4={k4} Q_MIN={q} S_MIN={s}: {row['entries']} entries, {row['monthly']:.2f} per month")
    lines.append(f"median of {rep['n_combos']} combos = {rep['median_entries']:g} entries, {rep['median_monthly']:.3f} per month "
                 f"(needs >= {rep['threshold_entries']} entries, {rep['threshold_monthly']} per month)")
    lines.append(f"verdict: {rep['verdict']}")
    lines.append(_NEXT[rep["verdict"]])
    return lines


def count_stage(df1m: pd.DataFrame, rep: CacheReport, workers: int, ckpt_dir: Path) -> dict[str, Any]:
    """The whole stage on the checked cache: the combos not yet saved, then the report over all 60."""
    if len(df1m) != rep.rows:
        raise DataWindowError(f"the frame has {len(df1m)} rows, the report {rep.rows}: this is not the cache that was checked")
    data = data_record(rep)
    results = load_done(ckpt_dir, data)
    todo = [c for c in grid_combos() if c not in results]
    if todo:
        results.update(run_count(df1m, workers, todo, on_done=lambda c, by_year: save_done(ckpt_dir, c, by_year, data)))
    return build_report(results, rep)
```

- [ ] **Step 4: 통과 확인**

```bash
uv run --group live pytest tests/stratf/test_count.py tests/stratf/test_isolation.py -q
```

예상: `47 passed`.

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratf/pool.py src/ewbot/stratf/count.py tests/stratf/test_count.py
git commit -m "feat(stratf): stage-1 count: a failed worker stops the stage, each combo is saved, the gate has no fallback, the report has the synthetic share and no performance value" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: 워크포워드, CLI, 전체 검증

**Files:**
- Create: `src/ewbot/stratf/research.py`
- Modify: `src/ewbot/cli.py` (4곳: 모듈 설명, 파서, 처리 함수, `_COMMANDS`)
- Test: `tests/stratf/test_research.py`, `tests/stratf/test_cli_stratf.py`

**Interfaces:**
- Consumes: `ewbot.backtest.walkforward`의 `WF_END`, `WF_START`, `Combo`, `config_for`, `evaluate_folds(results, fold_list, equity0, min_trades=12) -> dict[str, Any]`, `folds() -> list[tuple[int, int, int, int]]`, `grid_combos`; `ewbot.backtest.metrics.compute_metrics(equity, trades, start_ts=None, end_ts=None) -> Metrics`; `ewbot.engine.loop.EngineResult`; `stratf.pool.run_combos`, `stratf.engine.TradingEngineF`, `stratf.data`, `stratf.count.REPORT_KIND`, `stratf.count.PROCEED`. `ewbot.cli`의 `build_parser()`, `main(argv=None)`, `_refuse(msg) -> NoReturn`(종료 코드 2), `_b_write_json(path, obj)`, `_COMMANDS`.
- Produces:
  - `stratf.research`: `REPORT_KIND = "ewbot-stratf-walkforward"`, `PROCEED`, `STOP`, `StageError(ValueError)`, `_setup_worker(df1m, start_ts, end_ts) -> None`, `_run_combo(combo: Combo) -> tuple[Combo, EngineResult]`, `run_grid_f(df1m: pd.DataFrame, start_ts: int, end_ts: int, workers: int, combos: Sequence[Combo] | None = None) -> dict[Combo, EngineResult]`, `proceeds(wf: Mapping[str, Any]) -> bool`, `build_report(results: Mapping[Combo, EngineResult], rep: CacheReport) -> dict[str, Any]`, `report_lines(rep: Mapping[str, Any]) -> list[str]`, `require_count_pass(path: Path, rep: CacheReport) -> None`, `walk_forward_f(df1m: pd.DataFrame, rep: CacheReport, workers: int) -> dict[str, Any]`
  - `ewbot stratf fetch [--cache-dir data/cache]`
  - `ewbot stratf count [--cache-dir data/cache] [--workers N] [--out reports/stratf-count.json] [--checkpoint-dir reports/stratf-count.d]`
  - `ewbot stratf walkforward [--cache-dir data/cache] [--workers N] [--count-report reports/stratf-count.json] [--out reports/stratf-walkforward.json]`
  - `ewbot.cli._f_fetch`, `_f_count`, `_f_walkforward`, `_f_checked_cache`, `cmd_stratf(args: argparse.Namespace) -> int`. 판정과 상관없이 정상으로 끝나면 종료 코드 0, 데이터·순서 거부는 2, 캐시 파일이 없으면 메시지와 함께 1, 작업자 실패는 예외로 끝나 보고서를 쓰지 않는다.

- [ ] **Step 1: 실패하는 테스트 작성**

파일: `tests/stratf/test_research.py`
```python
from __future__ import annotations

import dataclasses
import json
from typing import Any

import pandas as pd
import pytest

from ewbot.backtest.report import to_jsonable
from ewbot.backtest.runner import utc_ts
from ewbot.backtest.walkforward import HOLDOUT_START, config_for, folds, grid_combos
from ewbot.config import with_overrides
from ewbot.engine.loop import EngineResult, EquityPoint, Trade
from ewbot.strategy.hybrid import make_strategy
from ewbot.stratf import research
from ewbot.stratf.config import M1, base_config
from ewbot.stratf.data import GRID_BARS, CacheReport, DataWindowError, data_record
from ewbot.stratf.engine import TradingEngineF
from ewbot.types import H1
from tests.stratf.helpers import a_frame, stretch

FIRST, LAST = utc_ts("2017-01-01"), HOLDOUT_START - M1
REP = CacheReport(rows=2, first_ts=FIRST, last_ts=LAST, off_grid=0, missing=1_000)
DAY = 24 * H1
SHORT = (2.0, 0.5, 0.0)
BAD = (-1.0, 0.5, 0.0)


def fake_trade(entry_ts: int) -> Trade:
    return Trade(entry_ts=entry_ts, exit_ts=entry_ts + H1, setup="W3", direction=1, contracts=1, entry_price=100.0,
                 exit_price_avg=101.0, stop0=95.0, tp1=105.0, tp2=110.0, pnl=1.0, fees=0.0, funding=0.0, r_multiple=0.2,
                 exit_reason="TP2", snapshot={})


def fake_result(step: float, last_trade: str | None = "2024-01-01") -> EngineResult:
    """A curve moving by ``step`` a day (daily points are enough for the folds), and a trade a week up to last_trade."""
    days = range((HOLDOUT_START - FIRST) // DAY)
    equity = tuple(EquityPoint(FIRST + d * DAY, 10_000.0 + step * d) for d in days)
    end = FIRST if last_trade is None else utc_ts(last_trade)
    trades = tuple(fake_trade(t) for t in range(FIRST, end, 7 * DAY))
    return EngineResult(trades, equity, (), ())


def fake_results(step: float, last_trade: str | None = "2024-01-01") -> dict[tuple[float, ...], EngineResult]:
    res = fake_result(step, last_trade)
    return {c: res for c in grid_combos()}


# ----------------------------------------------------------------------------- the grid run


def test_a_s_ten_folds():
    fs = folds()
    assert len(fs) == 10
    assert (fs[0][0], fs[0][2]) == (utc_ts("2017-01-01"), utc_ts("2019-01-01"))
    assert (fs[-1][2], fs[-1][3]) == (utc_ts("2023-07-01"), utc_ts("2024-01-01"))


def test_the_worker_runs_breakers_off_and_returns_a_light_result(monkeypatch):
    seen: dict[str, Any] = {}
    point = EquityPoint(FIRST, 10_000.0)

    def fake_run(self, df1m, start_ts, end_ts):
        seen.update(cfg=self.cfg, start=start_ts, end=end_ts, df=df1m)
        heavy = dataclasses.replace(fake_trade(FIRST), snapshot={"counts": [1, 2, 3]})
        return EngineResult((heavy,), (point,), ("signal",), ((FIRST, "event"),))

    monkeypatch.setattr(research.TradingEngineF, "run", fake_run)
    marker = object()
    research._setup_worker(marker, 1, 2)
    combo = (2.5, 0.55, 0.25)
    got_combo, res = research._run_combo(combo)
    cfg = seen["cfg"]
    assert got_combo == combo and (seen["start"], seen["end"], seen["df"]) == (1, 2, marker)
    assert cfg.breakers.enabled is False and (cfg.wave.K4, cfg.strategy.Q_MIN, cfg.strategy.S_MIN) == combo
    assert res.signals == () and res.events == () and res.equity == (point,)
    assert res.trades[0].snapshot == {} and res.trades[0].pnl == 1.0


def test_a_frame_reaching_2024_is_refused_before_any_run():
    with pytest.raises(DataWindowError, match="2024"):
        research.walk_forward_f(pd.DataFrame({"ts": [HOLDOUT_START]}), CacheReport(1, HOLDOUT_START, HOLDOUT_START, 0, 0), 1)


def test_a_frame_that_is_not_the_reported_cache_is_refused():
    with pytest.raises(DataWindowError, match="not the cache"):
        research.walk_forward_f(pd.DataFrame({"ts": [FIRST]}), REP, 1)


def test_a_grid_run_is_the_engine_run_with_hourly_points():
    df = stretch(a_frame())
    start, end = int(df["ts"].iloc[0]), int(df["ts"].iloc[-1]) + M1
    got = research.run_grid_f(df, start, end, 1, combos=[SHORT])[SHORT]
    cfg = config_for(with_overrides(base_config(), **{"breakers.enabled": False}), SHORT)
    res = TradingEngineF(cfg, make_strategy(cfg.strategy)).run(df, start, end)
    assert [(t.entry_ts, t.exit_ts, t.pnl) for t in got.trades] == [(t.entry_ts, t.exit_ts, t.pnl) for t in res.trades]
    assert got.equity == res.equity and len(got.trades) >= 3
    assert all(p.ts % H1 == 0 for p in got.equity)
    assert [b.ts - a.ts for a, b in zip(got.equity, got.equity[1:])] == [H1] * (len(got.equity) - 1)


def test_bars_at_or_after_the_end_are_dropped_before_the_run(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_combos(fn, setup, initargs, combos, workers, on_done=None):
        seen["df"] = initargs[0]
        return {}

    monkeypatch.setattr(research, "run_combos", fake_combos)
    df = pd.DataFrame({"ts": [100, 160, 220]})
    research.run_grid_f(df, 100, 220, 1, combos=[])
    assert seen["df"]["ts"].tolist() == [100, 160]


def test_a_failing_worker_aborts_the_grid():
    with pytest.raises(ValueError, match="k must be > 0"):
        research.run_grid_f(stretch(a_frame()).iloc[:500], FIRST, HOLDOUT_START, 2, combos=[SHORT, BAD])


# ----------------------------------------------------------------------------- the rule to proceed (spec 5.2)


def test_a_positive_return_with_a_pick_in_the_last_fold_proceeds():
    rep = research.build_report(fake_results(1.0), REP)
    assert rep["verdict"] == research.PROCEED and rep["proceed"] is True
    assert rep["stitched_metrics"]["total_return"] > 0.0
    assert rep["chosen_final"] == SHORT and rep["chosen_final_from_fold"] == 9 and rep["chosen_final_fallback"] is False


def test_a_negative_return_stops():
    rep = research.build_report(fake_results(-1.0), REP)
    assert rep["stitched_metrics"]["total_return"] < 0.0
    assert rep["verdict"] == research.STOP and rep["proceed"] is False


def test_a_return_of_exactly_zero_stops():
    rep = research.build_report(fake_results(0.0), REP)
    assert rep["stitched_metrics"]["total_return"] == 0.0 and rep["chosen_final"] == SHORT
    assert rep["verdict"] == research.STOP


def test_a_fallback_pick_stops_even_with_a_positive_return():
    """Trades end in June 2021: the last training window, from July 2021, has none, and an earlier fold made the last pick."""
    rep = research.build_report(fake_results(1.0, last_trade="2021-06-30"), REP)
    assert rep["stitched_metrics"]["total_return"] > 0.0
    assert rep["chosen_final_fallback"] is True and rep["chosen_final_from_fold"] < 9
    assert rep["verdict"] == research.STOP


def test_no_pick_at_all_stops():
    rep = research.build_report(fake_results(1.0, last_trade=None), REP)
    assert rep["chosen_final"] is None and rep["chosen_final_fallback"] is False
    assert rep["verdict"] == research.STOP


def test_the_rule_on_its_own():
    good = {"chosen_final": SHORT, "chosen_final_fallback": False, "stitched_metrics": {"total_return": 0.01}}
    assert research.proceeds(good) is True
    assert research.proceeds({**good, "chosen_final": None}) is False
    assert research.proceeds({**good, "chosen_final_fallback": True}) is False
    assert research.proceeds({**good, "stitched_metrics": {"total_return": 0.0}}) is False


def test_the_walk_forward_needs_the_60_combos():
    with pytest.raises(ValueError, match="60"):
        research.build_report({SHORT: fake_result(1.0)}, REP)


# ----------------------------------------------------------------------------- the report


def test_the_report_is_json_with_the_folds_the_share_and_no_trade_list():
    rep = research.build_report(fake_results(1.0), REP)
    assert rep["kind"] == research.REPORT_KIND and rep["window"] == {"start": "2017-01-01", "end": "2024-01-01"}
    assert rep["data"]["missing"] == 1_000 and rep["data"]["synthetic_share"] == 1_000 / GRID_BARS
    assert rep["breakers"] == "off" and "oos_trades" not in rep
    assert len(rep["folds"]) == 10 and len(rep["combo_table"]) == 60
    assert rep["keys"] == ["wave.K4", "strategy.Q_MIN", "strategy.S_MIN"]
    json.dumps(to_jsonable(rep))


def test_the_printed_lines_show_the_folds_the_return_the_share_and_the_verdict():
    lines = research.report_lines(research.build_report(fake_results(1.0), REP))
    text = "\n".join(lines)
    assert sum(line.startswith("test 20") for line in lines) == 10
    assert "synthetic bars: 1000 of 3680640 minutes (0.027%)" in text
    assert "chosen_final = (2.0, 0.5, 0.0), from fold 9, fallback = False" in text
    assert "verdict: PROCEED" in lines
    stop = research.report_lines(research.build_report(fake_results(1.0, last_trade=None), REP))
    assert "verdict: STOP" in stop and any("spec 5.2: strategy F ends here" in line for line in stop)


# ----------------------------------------------------------------------------- stage order


def _count_report(tmp_path, **kw: Any):
    path = tmp_path / "count.json"
    path.write_text(json.dumps({"kind": "ewbot-stratf-count", "verdict": "PROCEED", "data": data_record(REP), **kw}),
                    encoding="utf-8")
    return path


def test_the_walk_forward_needs_a_passed_count_on_the_same_cache(tmp_path):
    research.require_count_pass(_count_report(tmp_path), REP)
    with pytest.raises(research.StageError, match="not found"):
        research.require_count_pass(tmp_path / "none.json", REP)
    with pytest.raises(research.StageError, match="STOP"):
        research.require_count_pass(_count_report(tmp_path, verdict="STOP"), REP)
    with pytest.raises(research.StageError, match="not a stage-1 report"):
        research.require_count_pass(_count_report(tmp_path, kind="other"), REP)
    with pytest.raises(research.StageError, match="another cache"):
        research.require_count_pass(_count_report(tmp_path, data={"rows": 1}), REP)
```

파일: `tests/stratf/test_cli_stratf.py`
```python
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ewbot.backtest.runner import utc_ts
from ewbot.backtest.walkforward import HOLDOUT_START, grid_combos
from ewbot.cli import build_parser, main
from ewbot.stratf.config import M1
from ewbot.stratf.data import cache_file, cache_report, data_record
from tests.stratf.helpers import a_frame, stretch

FIRST, LAST = utc_ts("2017-01-01"), HOLDOUT_START - M1
TUNING = (["--threshold", "3"], ["--params", "2,0.5,0"], ["--start", "2018-01-01"], ["--end", "2023-01-01"], ["--q-min", "0.5"],
          ["--limit", "200000"])

# Run under spawn with a main guard, as the `ewbot` console script is; only the cache check is off for the short cache.
DRIVER = '''\
import sys

import ewbot.stratf.data as data
from ewbot.cli import main

if __name__ == "__main__":
    data.check_cache = lambda rep: None
    sys.exit(main(sys.argv[1:]))
'''


def _cache(tmp_path: Path, rows: int = 8) -> Path:
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    stretch(a_frame()).iloc[:rows].to_parquet(cache_file(cache), index=False)
    return cache


def _code(argv: list[str]) -> int:
    """main's exit code, also when a refusal exits through SystemExit (cli._refuse)."""
    try:
        return main(argv)
    except SystemExit as exc:
        return int(exc.code)


def _no_check(monkeypatch) -> None:
    monkeypatch.setattr("ewbot.stratf.data.check_cache", lambda rep: None)


def _passed_count(tmp_path: Path, cache: Path, verdict: str = "PROCEED") -> Path:
    path = tmp_path / "count.json"
    rep = {"kind": "ewbot-stratf-count", "verdict": verdict, "data": data_record(cache_report(cache))}
    path.write_text(json.dumps(rep), encoding="utf-8")
    return path


# ----------------------------------------------------------------------------- the parser


def test_the_parser_has_the_three_commands_and_no_tuning_options():
    p = build_parser()
    ns = p.parse_args(["stratf", "fetch"])
    assert (ns.cmd, ns.f_cmd, ns.cache_dir) == ("stratf", "fetch", Path("data/cache"))
    ns = p.parse_args(["stratf", "count"])
    assert (ns.f_cmd, ns.cache_dir, ns.workers) == ("count", Path("data/cache"), None)
    assert (ns.out, ns.checkpoint_dir) == (Path("reports/stratf-count.json"), Path("reports/stratf-count.d"))
    assert p.parse_args(["stratf", "count", "--workers", "3"]).workers == 3
    ns = p.parse_args(["stratf", "walkforward"])
    assert (ns.f_cmd, ns.workers, ns.count_report) == ("walkforward", None, Path("reports/stratf-count.json"))
    assert ns.out == Path("reports/stratf-walkforward.json")
    for cmd in ("fetch", "count", "walkforward"):
        for extra in TUNING:
            with pytest.raises(SystemExit):
                p.parse_args(["stratf", cmd, *extra])


def test_the_cli_loads_stratf_only_for_stratf_commands():
    """`ewbot live …` and `ewbot paper …` run with the same modules as before strategy F."""
    code = "import sys, ewbot.cli; print(sorted(m for m in sys.modules if m.startswith('ewbot.stratf')))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


# ----------------------------------------------------------------------------- fetch


def test_fetch_prints_the_missing_count(tmp_path, monkeypatch, capsys):
    cache = _cache(tmp_path, rows=8)
    monkeypatch.setattr("ewbot.stratf.data.update_cache_1m", lambda d: cache_file(d))
    assert main(["stratf", "fetch", "--cache-dir", str(cache)]) == 0
    printed = capsys.readouterr().out
    assert "1m cache: 8 rows" in printed and "missing bars: 3680632 of 3680640 minutes" in printed


# ----------------------------------------------------------------------------- count


def test_count_writes_the_report_and_prints_the_verdict(tmp_path, monkeypatch, capsys):
    _no_check(monkeypatch)
    seen = {}

    def fake_run(df1m, workers, combos, on_done):
        seen.update(workers=workers, rows=len(df1m))
        return {c: {2019: {"W3": 336}} for c in combos}

    monkeypatch.setattr("ewbot.stratf.count.run_count", fake_run)
    out = tmp_path / "reports" / "count.json"
    argv = ["stratf", "count", "--cache-dir", str(_cache(tmp_path)), "--workers", "3", "--out", str(out), "--checkpoint-dir",
            str(tmp_path / "ckpt")]
    assert main(argv) == 0
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["verdict"] == "PROCEED" and len(rep["combos"]) == 60 and rep["combos"][0]["combo"] == [2.0, 0.5, 0.0]
    assert seen == {"workers": 3, "rows": 8}
    printed = capsys.readouterr().out
    assert "missing bars: 3680632 of 3680640 minutes" in printed
    assert "K4=2.0 Q_MIN=0.5 S_MIN=0.0: 336 entries, 4.00 per month" in printed
    assert "median of 60 combos = 336 entries, 4.000 per month" in printed
    assert "verdict: PROCEED" in printed and f"json: {out}" in printed


def test_count_defaults_to_the_worker_count_that_fits(tmp_path, monkeypatch):
    _no_check(monkeypatch)
    seen = {}

    def fake_run(df1m, workers, combos, on_done):
        seen["workers"] = workers
        return {c: {2019: {"W3": 1}} for c in combos}

    monkeypatch.setattr("ewbot.stratf.count.run_count", fake_run)
    monkeypatch.setattr("ewbot.stratf.config.default_workers", lambda: 12)
    argv = ["stratf", "count", "--cache-dir", str(_cache(tmp_path)), "--out", str(tmp_path / "c.json"), "--checkpoint-dir",
            str(tmp_path / "ckpt")]
    assert main(argv) == 0 and seen["workers"] == 12


def test_a_cache_cut_short_is_refused_and_writes_nothing(tmp_path, capsys):
    out = tmp_path / "count.json"
    argv = ["stratf", "count", "--cache-dir", str(_cache(tmp_path)), "--out", str(out), "--checkpoint-dir", str(tmp_path / "c")]
    assert _code(argv) == 2
    assert "2017-01-01" in capsys.readouterr().err and not out.exists()


def test_too_many_missing_bars_are_refused_before_prices_are_read(tmp_path, monkeypatch, capsys):
    cache = tmp_path / "cache"
    cache.mkdir()
    ts = np.array([FIRST, LAST], dtype=np.int64)
    prices = {c: 1.0 for c in ("open", "high", "low", "close", "volume")}
    pd.DataFrame({"ts": ts, **prices}).to_parquet(cache_file(cache), index=False)

    def boom(*a: object) -> None:
        raise AssertionError("prices were read")

    monkeypatch.setattr("ewbot.stratf.data.load_1m", boom)
    out = tmp_path / "count.json"
    argv = ["stratf", "count", "--cache-dir", str(cache), "--out", str(out), "--checkpoint-dir", str(tmp_path / "c")]
    assert _code(argv) == 2
    io = capsys.readouterr()
    assert "missing bars: 3680638 of 3680640 minutes" in io.out  # reported before the refusal
    assert "over the limit of 184032" in io.err and not out.exists()


def test_a_missing_cache_says_so(tmp_path):
    with pytest.raises(SystemExit, match="1m cache not found"):
        main(["stratf", "count", "--cache-dir", str(tmp_path / "none"), "--out", str(tmp_path / "count.json")])


def test_a_failed_count_writes_no_report(tmp_path, monkeypatch):
    _no_check(monkeypatch)

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("a worker failed")

    monkeypatch.setattr("ewbot.stratf.count.run_count", boom)
    out = tmp_path / "count.json"
    argv = ["stratf", "count", "--cache-dir", str(_cache(tmp_path)), "--out", str(out), "--checkpoint-dir", str(tmp_path / "c")]
    with pytest.raises(RuntimeError, match="a worker failed"):
        main(argv)
    assert not out.exists()


def test_count_runs_engine_workers_under_spawn_with_a_main_guard(tmp_path):
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    out = tmp_path / "count.json"
    argv = [sys.executable, str(driver), "stratf", "count", "--cache-dir", str(_cache(tmp_path, rows=400)), "--workers", "2",
            "--out", str(out), "--checkpoint-dir", str(tmp_path / "ckpt")]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert len(rep["combos"]) == 60 and rep["verdict"] == "STOP"  # 80 wave bars: still in the warm-up, no entry
    assert len(list((tmp_path / "ckpt").glob("*.json"))) == 60


# ----------------------------------------------------------------------------- walkforward


def test_the_walk_forward_is_refused_without_a_passed_count(tmp_path, monkeypatch, capsys):
    _no_check(monkeypatch)

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("a run was started")

    monkeypatch.setattr("ewbot.stratf.research.run_grid_f", boom)
    cache = _cache(tmp_path)
    out = tmp_path / "wf.json"
    base = ["stratf", "walkforward", "--cache-dir", str(cache), "--out", str(out)]
    assert _code([*base, "--count-report", str(tmp_path / "none.json")]) == 2
    assert "not found" in capsys.readouterr().err
    assert _code([*base, "--count-report", str(_passed_count(tmp_path, cache, verdict="STOP"))]) == 2
    assert "STOP" in capsys.readouterr().err and not out.exists()


def test_the_walk_forward_writes_the_report_and_prints_the_verdict(tmp_path, monkeypatch, capsys):
    _no_check(monkeypatch)
    from ewbot.engine.loop import EngineResult

    def fake_grid(df1m, start_ts, end_ts, workers):
        assert (start_ts, end_ts, workers) == (FIRST, HOLDOUT_START, 3)
        return {c: EngineResult((), (), (), ()) for c in grid_combos()}

    monkeypatch.setattr("ewbot.stratf.research.run_grid_f", fake_grid)
    cache = _cache(tmp_path)
    out = tmp_path / "reports" / "wf.json"
    argv = ["stratf", "walkforward", "--cache-dir", str(cache), "--workers", "3", "--count-report",
            str(_passed_count(tmp_path, cache)), "--out", str(out)]
    assert main(argv) == 0
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["kind"] == "ewbot-stratf-walkforward" and rep["verdict"] == "STOP" and rep["chosen_final"] is None
    printed = capsys.readouterr().out
    assert "verdict: STOP" in printed and f"json: {out}" in printed


def test_the_walk_forward_runs_engine_workers_under_spawn_with_a_main_guard(tmp_path):
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    cache = _cache(tmp_path, rows=400)
    out = tmp_path / "wf.json"
    argv = [sys.executable, str(driver), "stratf", "walkforward", "--cache-dir", str(cache), "--workers", "2", "--count-report",
            str(_passed_count(tmp_path, cache)), "--out", str(out)]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert len(rep["combo_table"]) == 60 and rep["verdict"] == "STOP"
```

- [ ] **Step 2: 실패 확인**

```bash
uv run --group live pytest tests/stratf/test_research.py tests/stratf/test_cli_stratf.py -q
```

예상: 수집 오류 `ImportError: cannot import name 'research' from 'ewbot.stratf'`.

- [ ] **Step 3: `research.py` 구현**

파일: `src/ewbot/stratf/research.py`
```python
"""Stage 2 of strategy F (Plan 8 spec 5.2): A's walk-forward on F's runs.

A's frame is imported and not edited: folds() (24 months of training, 6 of test, rolling by 6), evaluate_folds (pick_plateau
on each training window: at least 12 trades, a finite Calmar, the neighbours' mean Calmar; the stitched out-of-sample curve;
the pick of the last fold that picked anything) and compute_metrics. A's run_grid cannot run F's engine, so run_grid_f is F's
own pool around the same single run per combo with the breakers off. F's equity points are hourly, so A's code applies as is.

Proceed: a stitched out-of-sample return > 0 and a final combo picked in the last fold, not a fallback. Anything else ends
strategy F. A's chosen_final_fallback is False when no fold picked anything, so the rule also needs a final combo.

Spec 1, 5: a failed stage ends F, so this stage refuses to run without a passed stage-1 report on the same cache. Nothing is
saved per combo: a failed worker stops the stage and it starts over.

Run it through `ewbot stratf walkforward`: the workers are spawned, which needs the main guard of that entry point."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ewbot.backtest.metrics import compute_metrics
from ewbot.backtest.walkforward import WF_END, WF_START, Combo, config_for, evaluate_folds, folds, grid_combos
from ewbot.config import with_overrides
from ewbot.engine.loop import EngineResult
from ewbot.strategy.hybrid import make_strategy
from ewbot.stratf import count as stage1
from ewbot.stratf.config import base_config
from ewbot.stratf.data import END_TS, START_TS, CacheReport, DataWindowError, data_record, refuse_2024
from ewbot.stratf.engine import TradingEngineF
from ewbot.stratf.pool import run_combos

REPORT_KIND = "ewbot-stratf-walkforward"
PROCEED = "PROCEED"
STOP = "STOP"
_NEXT = {
    PROCEED: "next: report to the user before stage 3 gets its plan (holdout-f.lock, one run after the user's approval)",
    STOP: "spec 5.2: strategy F ends here; no holdout, no other variant",
}

_WORKER: dict[str, Any] = {}  # per-process, set by the pool initializer


class StageError(ValueError):
    """Stage 2 was asked to run without a passed stage 1 on this cache."""


def _setup_worker(df1m: pd.DataFrame, start_ts: int, end_ts: int) -> None:
    _WORKER.update(df1m=df1m, start=start_ts, end=end_ts)


def _run_combo(combo: Combo) -> tuple[Combo, EngineResult]:
    """One engine run of one combo, breakers off. The folds need the trades and the hourly points; dropping the snapshots, the
    signals and the events keeps 60 results well under a GB in the parent."""
    cfg = config_for(with_overrides(base_config(), **{"breakers.enabled": False}), combo)
    res = TradingEngineF(cfg, make_strategy(cfg.strategy)).run(_WORKER["df1m"], _WORKER["start"], _WORKER["end"])
    trades = tuple(dataclasses.replace(t, snapshot={}) for t in res.trades)
    return combo, EngineResult(trades, res.equity, (), ())


def run_grid_f(
    df1m: pd.DataFrame, start_ts: int, end_ts: int, workers: int, combos: Sequence[Combo] | None = None
) -> dict[Combo, EngineResult]:
    """Every combo ONCE over [start_ts, end_ts) (default: the 60 of GRID); folds later slice these single runs. Bars at/after
    end_ts are dropped before any run. A failed worker raises."""
    df1m = df1m[df1m["ts"] < end_ts].reset_index(drop=True)
    todo = grid_combos() if combos is None else list(combos)
    return run_combos(_run_combo, _setup_worker, (df1m, start_ts, end_ts), todo, workers)


def proceeds(wf: Mapping[str, Any]) -> bool:
    return (
        wf["chosen_final"] is not None
        and not wf["chosen_final_fallback"]
        and wf["stitched_metrics"]["total_return"] > 0.0
    )


def build_report(results: Mapping[Combo, EngineResult], rep: CacheReport) -> dict[str, Any]:
    combos = grid_combos()
    if set(results) != set(combos):  # a missing combo would change its neighbours' plateau without a word
        raise ValueError(f"the walk-forward needs the {len(combos)} grid combos, got {len(results)}")
    ordered = {c: results[c] for c in combos}
    wf = evaluate_folds(ordered, folds(), base_config().initial_equity)
    full = {c: compute_metrics(r.equity, r.trades, START_TS, END_TS) for c, r in ordered.items()}
    passed = proceeds(wf)
    return {
        "kind": REPORT_KIND,
        "window": {"start": WF_START, "end": WF_END},
        "data": data_record(rep),
        "breakers": "off",
        **{k: v for k, v in wf.items() if k != "oos_trades"},
        "combo_table": [
            {"combo": c, "calmar": m.calmar, "sharpe": m.sharpe, "n_trades": m.n_trades} for c, m in full.items()
        ],
        "proceed": passed,
        "verdict": PROCEED if passed else STOP,
    }


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def report_lines(rep: Mapping[str, Any]) -> list[str]:
    data, m = rep["data"], rep["stitched_metrics"]
    lines = [
        f"strategy F walk-forward, {rep['window']['start']} .. {rep['window']['end']} (end exclusive), breakers off",
        f"synthetic bars: {data['missing']} of {data['grid_bars']} minutes ({data['synthetic_share']:.3%})",
    ]
    for f in rep["folds"]:
        oos = f["oos"]
        tail = "no pick" if oos is None else f"oos return {oos['total_return']:+.2%}, {oos['n_trades']} trades"
        lines.append(f"test {_day(f['test'][0])} .. {_day(f['test'][1])}: chosen = {f['chosen']}, {tail}")
    lines.append(f"stitched out-of-sample: return {m['total_return']:+.2%}, max_dd {m['max_dd']:.2%}, sharpe {m['sharpe']:.2f}, "
                 f"calmar {m['calmar']:.2f}, {m['n_trades']} trades")
    lines.append(f"chosen_final = {rep['chosen_final']}, from fold {rep['chosen_final_from_fold']}, "
                 f"fallback = {rep['chosen_final_fallback']}")
    lines.append(f"verdict: {rep['verdict']}")
    lines.append(_NEXT[rep["verdict"]])
    return lines


def require_count_pass(path: Path, rep: CacheReport) -> None:
    """Spec 5: stage 2 runs only after stage 1 passed, on the same cache."""
    path = Path(path)
    if not path.exists():
        raise StageError(f"{path} not found: stage 2 needs the report of a passed `ewbot stratf count`")
    count_rep = json.loads(path.read_text(encoding="utf-8"))
    if count_rep.get("kind") != stage1.REPORT_KIND:
        raise StageError(f"{path} is not a stage-1 report")
    if count_rep.get("verdict") != stage1.PROCEED:
        raise StageError(f"stage 1 ended with {count_rep.get('verdict')}: spec 5.1 ends strategy F, no walk-forward")
    if count_rep.get("data") != data_record(rep):
        raise StageError(f"{path} was counted on another cache")


def walk_forward_f(df1m: pd.DataFrame, rep: CacheReport, workers: int) -> dict[str, Any]:
    """The whole stage on the checked cache: the 60 runs, then A's folds."""
    refuse_2024(df1m)
    if len(df1m) != rep.rows:
        raise DataWindowError(f"the frame has {len(df1m)} rows, the report {rep.rows}: this is not the cache that was checked")
    return build_report(run_grid_f(df1m, START_TS, END_TS, workers), rep)
```

- [ ] **Step 4: `cli.py` 수정 (4곳)**

수정 1, 모듈 설명. 찾을 문자열:
```text
inside its handlers, so `ewbot live …` and `ewbot paper …` never load it."""
```
바꿀 문자열:
```text
inside its handlers, so `ewbot live …` and `ewbot paper …` never load it.
`stratf fetch | count | walkforward` (Plan 8) is stages 0-2 of strategy F (ewbot.stratf): the BTC 1m cache before 2024, the
entries of the 60 combos with no performance value, and the walk-forward; ewbot.stratf is imported only inside its handlers."""
```

수정 2, 파서. 찾을 문자열:
```text
    p_eps.add_argument("--ledger-dir", type=Path, required=True)
    return parser
```
바꿀 문자열:
```text
    p_eps.add_argument("--ledger-dir", type=Path, required=True)

    p_f = sub.add_parser("stratf", help="strategy F (Plan 8): A's rules on 5m waves and a 1m trigger; stages 0-2")
    f_sub = p_f.add_subparsers(dest="f_cmd", required=True)
    workers_help = "default: 12, or the CPU count if lower (about 2.5 GB per worker)"
    p_ff = f_sub.add_parser("fetch", help="stage 0: the BTC 1m cache, 2017-01..2023-12; resumes; prints the missing count")
    p_ff.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    p_fc = f_sub.add_parser("count", help="stage 1: entries per month of the 60 combos, 2017-01..2023-12 (counts only)")
    p_fc.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    p_fc.add_argument("--workers", type=int, default=None, help=workers_help)
    p_fc.add_argument("--out", type=Path, default=Path("reports/stratf-count.json"))
    p_fc.add_argument("--checkpoint-dir", type=Path, default=Path("reports/stratf-count.d"))
    p_fw = f_sub.add_parser("walkforward", help="stage 2: A's walk-forward on F's runs; needs a passed stage 1")
    p_fw.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    p_fw.add_argument("--workers", type=int, default=None, help=workers_help)
    p_fw.add_argument("--count-report", type=Path, default=Path("reports/stratf-count.json"))
    p_fw.add_argument("--out", type=Path, default=Path("reports/stratf-walkforward.json"))
    return parser
```

수정 3, 처리 함수. 찾을 문자열:
```text
_COMMANDS = {
```
바꿀 문자열:
```text
# ----------------------------------------------------------------------------- strategy F (Plan 8)
# ewbot.stratf is imported inside these handlers only: `ewbot live …` and `ewbot paper …` never load it.


def _f_fetch(args: argparse.Namespace) -> int:
    from ewbot.stratf import data

    path = data.update_cache_1m(args.cache_dir)
    for line in data.report_lines(data.cache_report(args.cache_dir)):
        print(line)
    print(f"cache: {path}")
    return 0


def _f_checked_cache(args: argparse.Namespace):
    """The cache's report from the ts column alone, printed first (spec 4), then the refusals. No price is read yet."""
    from ewbot.stratf import data

    path = data.cache_file(args.cache_dir)
    if not path.exists():
        raise SystemExit(f"1m cache not found: {path} - run `ewbot stratf fetch` first")
    rep = data.cache_report(args.cache_dir)
    for line in data.report_lines(rep):
        print(line, flush=True)
    try:
        data.check_cache(rep)
    except data.DataWindowError as exc:
        _refuse(str(exc))
    return rep


def _f_count(args: argparse.Namespace) -> int:
    from ewbot.stratf import config, count, data

    rep = _f_checked_cache(args)
    workers = args.workers or config.default_workers()
    try:
        out = count.count_stage(data.load_1m(args.cache_dir), rep, workers, args.checkpoint_dir)
    except data.DataWindowError as exc:
        _refuse(str(exc))
    _b_write_json(args.out, out)
    for line in count.report_lines(out):
        print(line)
    print(f"json: {args.out}")
    return 0


def _f_walkforward(args: argparse.Namespace) -> int:
    from ewbot.stratf import config, data, research

    rep = _f_checked_cache(args)
    workers = args.workers or config.default_workers()
    try:
        research.require_count_pass(args.count_report, rep)
        out = research.walk_forward_f(data.load_1m(args.cache_dir), rep, workers)
    except (data.DataWindowError, research.StageError) as exc:
        _refuse(str(exc))
    _b_write_json(args.out, out)
    for line in research.report_lines(out):
        print(line)
    print(f"json: {args.out}")
    return 0


def cmd_stratf(args: argparse.Namespace) -> int:
    return {"fetch": _f_fetch, "count": _f_count, "walkforward": _f_walkforward}[args.f_cmd](args)


_COMMANDS = {
```

수정 4, 명령 표. 찾을 문자열:
```text
    "aeth": cmd_aeth,
}
```
바꿀 문자열:
```text
    "aeth": cmd_aeth,
    "stratf": cmd_stratf,
}
```

- [ ] **Step 5: 통과 확인**

```bash
uv run --group live pytest tests/stratf/test_research.py tests/stratf/test_cli_stratf.py -q
```

예상: `30 passed`. spawn 테스트 두 개는 작업자를 띄우므로 각각 수 초 걸린다.

- [ ] **Step 6: 커밋**

```bash
git add src/ewbot/stratf/research.py src/ewbot/cli.py tests/stratf/test_research.py tests/stratf/test_cli_stratf.py
git commit -m "feat(stratf): stage-2 walk-forward on A's folds and ewbot stratf fetch, count, walkforward behind the CLI's main guard" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 7: 가드와 전체 테스트, A 지문, 바뀐 파일 확인**

```bash
uv run --group live pytest tests/stratf tests/paper/test_fingerprint.py tests/paper/test_static.py tests/stratb/test_isolation.py tests/aeth/test_isolation.py -q
uv run --group live pytest -q
uv run --group live python -c "from ewbot.paper.fingerprint import current_fingerprint as f; print(f())"
git diff --name-only "$(git merge-base HEAD master)"
git ls-files --others --exclude-standard
```

예상: 첫째 줄과 둘째 줄의 실패 0건. 전체 테스트 수는 기준선 1,528개(`pytest --collect-only`, 2026-11-04)에 `tests/stratf`의 144개를 더한 1,672개다. 셋째 줄은 `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1`을 출력한다. 넷째와 다섯째 줄의 경로는 `docs/superpowers/`, `src/ewbot/stratf/`, `tests/stratf/`, `src/ewbot/cli.py`뿐이다. 다른 경로가 보이면 커밋하지 말고 되돌린다. `data/cache/`와 `reports/`는 git 제외 대상이므로 나오지 않는다.

## 실제 실행 (Task 7이 끝나고 검토를 통과한 뒤, 사용자의 지시로 순서대로)

Task들은 코드만 만든다. 떨어지면 F가 영구히 끝나는 판정이므로 "명세의 모호점" 1~6번을 사용자가 확인한 뒤에 실행한다. 단계마다 결과를 사용자에게 보고하고, 어떤 단계도 다시 조정하지 않는다.

1. **수집.** 네트워크로 약 1~1.5시간 걸린다. 중단되면 같은 명령을 다시 실행하면 저장된 페이지 다음부터 받는다.

   ```bash
   uv run ewbot stratf fetch
   ```

   마지막 두 줄의 빠진 수와 비율을 사용자에게 보고한다. 184,032를 넘으면 명세 4절대로 F는 여기서 끝난다.

2. **① 횟수 점검.** 중단되면 같은 명령을 다시 실행한다. 저장된 조합은 다시 돌리지 않는다. 작업자가 실패하면 보고서 없이 멈추므로 원인을 보고한다.

   ```bash
   uv run ewbot stratf count
   ```

   판정이 STOP이면 끝이다. 명세 7절대로 명세 끝에 결과와 날짜를 적고 README의 전략 표에 한 줄을 더한다.

3. **② 워크포워드.** ①이 PROCEED이면 추가 승인 없이 진행한다(명세 1절 결정 3). ①의 보고서가 없으면 거부한다.

   ```bash
   uv run ewbot stratf walkforward
   ```

   PROCEED이면 ③ 계획을 쓰기 전에 사용자에게 보고한다. STOP이면 명세 7절대로 기록하고 끝낸다.
