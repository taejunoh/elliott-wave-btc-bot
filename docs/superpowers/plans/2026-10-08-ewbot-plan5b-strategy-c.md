# ewbot Plan 5b — 전략 C(B2를 한 시간대 위로: 4시간 구조·1시간 확인) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 전략 A와 B를 전혀 바꾸지 않고, 전략 B2의 규칙(4시간 추세, 피보나치·매물대 후보 구간, RSI 다이버전스와 돌파 확인)을 한 시간대 위로 옮긴 전략 C를 BTC·ETH 한 계좌에 만들고, A·B와 같은 절차(2017~2023 워크포워드 → 2024-01-01 이후 홀드아웃 1회 → 모의매매)로 **한 번만** 검증할 연구 코드(1부)와, 홀드아웃에 합격했을 때만 만드는 모의매매 런타임(2부)을 만든다.

**Architecture:** 새 코드는 `src/ewbot/stratc/`(전략·연구, C 지문 대상)와 `src/ewbot/paperc/`(모의매매, 2부)에만 둔다. 전략 B의 시간대와 무관한 부품 — 순수 규칙 함수, 15분 프레임과 1시간봉, 15분 포트폴리오 엔진, 매수 후 보유·무작위 대조군, 워크포워드 규칙, 합격 기준, 캐시 경로, 모의매매의 두 자산 판단 변환 — 과 전략 A의 모듈은 import만 한다. C가 새로 쓰는 것은 1시간봉 지표와 4시간봉(추세와 기준 구간), 1시간 확인 스캔, 추세 걸러내기와 C 표기를 더한 엔진 호출, `holdout-c.lock`에 묶인 2024 가드, C의 잠금·홀드아웃·모의매매다. B의 15분 경로를 시간으로 4배 늘린 합성 경로에서 C가 B와 똑같은 신호를 내는지 테스트해, C가 "한 시간대 위의 B"임을 고정한다.

**Tech Stack:** Python 3.12 (uv), 기존 의존성만(numpy, pandas, pyarrow, requests, PyYAML); pytest; (2부) GitHub Actions(`actions/checkout@v7`, `astral-sh/setup-uv` v10.2.0 커밋 `c18668ad3cf93ea998bef934396af7bb5c839dc7`, `actions/cache@v6`), Coinbase Exchange 공개 캔들 API(granularity 900, `BTC-USD`·`ETH-USD`), Telegram Bot API(A와 같은 봇·시크릿).

**Spec:** `docs/superpowers/specs/2026-10-08-ewbot-plan5b-strategy-c-design.md`(사용자 결정 2026-10-07/08을 원문으로 담음; 이 계획과 함께 C의 어떤 결과보다 먼저 커밋한 사전 등록). 상위 명세: Plan 4 `2026-10-07-ewbot-plan4-strategy-b-design.md`(전략 B와 12절 종료 기록; B2의 규칙은 병합하지 않은 커밋 `9a78644`·`df45473`), Plan 1 `2026-10-01-elliott-wave-btc-bot-design.md`, Plan 2 `2026-10-02-ewbot-plan2-paper-trading-design.md`.

## Global Constraints

- 작업 브랜치: 1부(Task 1~9)는 `feat/strategy-c`(명세·계획 커밋 위), 2부(Task 10~15)는 홀드아웃 합격 뒤 `master`에서 새로 만드는 `feat/strategy-c-paper`. 저장소 루트에서 실행한다. Python 3.12(uv). 테스트 명령은 `uv run --group live pytest …`. 기준선(`master` `cbf57ea`): `1380 passed, 1 skipped`(`data/cache/BTC-USD_1h.parquet`가 있으면 `1381 passed`; 아래의 모든 전체 테스트 수도 같은 방식으로 1 차이가 난다).
- **A의 지문 대상은 절대 수정하지 않고 그 안에 파일을 더하지도 않는다**: `src/ewbot/types.py`, `config.py`, `indicators.py`, `data/candles.py`, `waves/**`, `strategy/**`, `engine/**`, `execution/**`, `risk/**`. A 지문은 `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1`로 유지된다.
- 다음도 수정하지 않는다: `holdout.lock`, `src/ewbot/backtest/**`, `src/ewbot/data/coinbase_history.py`, `src/ewbot/paper/**`, `src/ewbot/live/**`, `.github/workflows/paper.yml`·`ci.yml`·`paper-b.yml`, `scripts/**`, `ops/**`, `deploy/**`, `pyproject.toml`, `uv.lock`, `README.md`, **그리고 전략 B 전체**: `src/ewbot/stratb/**`, `src/ewbot/paperb/**`, `tests/stratb/**`, `tests/paperb/**`(B는 끝난 기록이다; C는 import만 한다). 새 의존성을 추가하지 않는다.
- 만들 수 있는 것: `src/ewbot/stratc/**`, `tests/stratc/**`, `docs/strategy-c.md`, (2부) `src/ewbot/paperc/**`, `tests/paperc/**`, `.github/workflows/paper-c.yml`(휴면). 수정할 수 있는 것: `src/ewbot/cli.py`(`ewbot stratc …`만, 아래의 자리에서만), `tests/paper/test_static.py`(파일 끝에 C의 CI 검사; 2부에서 워크플로 목록 한 줄).
- **A-ETH(Plan 5a, 브랜치 `feat/aeth`)와의 병합.** 두 계획이 모두 `cli.py`와 `tests/paper/test_static.py`를 고친다. C는 `cli.py`의 모듈 설명과 import 부분을 고치지 않고(`ewbot.stratc`·`ewbot.paperc`는 처리 함수 안에서만 import한다), 파서의 `p_b = sub.add_parser("stratb", …)` 바로 앞, 처리 함수의 `# ---- strategy B (Plan 4)` 머리 줄 바로 앞, `_COMMANDS`의 `"live": cmd_live,` 바로 다음 줄에만 더한다. `test_static.py`에는 파일 끝에만 더한다. A-ETH 계획(커밋 `2a17593`)의 `cli.py`·`test_static.py` 수정 9개를 적용한 사본과 이 계획의 수정을 git으로 병합해 확인했다: `cli.py`는 충돌이 없고, 충돌은 2부의 워크플로 목록 한 줄뿐이다(Task 13에 두 경우의 정확한 내용을 적었다).
- C 지문 = `src/ewbot/stratc/` 아래 모든 `*.py`의 sha256(A와 같은 형식: `ewbot.paper.fingerprint.fingerprint_of`). `paperc/`와 `cli.py`는 포함하지 않는다. `deps_sha256` = `stratc/`가 직접 또는 다른 모듈을 거쳐 import하는 `stratc/` 밖의 모든 모듈(A와 B의 모듈)의 해시(해석 8). 잠금이 둘을 묶는다.
- **2부는 `stratc/`를 바꾸지 않는다.** 바꾸면 C 지문이 잠금과 달라져 모의매매가 새 구간을 열고, 그 구간은 실거래 전환 판정에서 빠진다. 2부에 필요한 `stratc`의 함수는 모두 1부에 있다.
- **2024-01-01 00:00 UTC(`HOLDOUT_START = 1704067200`) 이후 데이터는 `holdout-c.lock`이 생기기 전에 C를 위해 받거나 읽을 수 없다**(해석 5). 2024 이후 봉을 여는 키워드는 `with_holdout`이고, 리터럴 `with_holdout=True`는 `stratc/holdout.py`와 `paperc/runner.py`에서만 쓰며 변수로 넘기는 곳은 없다. C 코드는 `allow_holdout`·`include_holdout`·`unlock_2024` 키워드, A의 `_init_worker`·`_map_tasks`·`check_before_holdout` 이름, 문자열 `"allow_holdout"`을 쓰지 않는다(A·B·A-ETH의 정적 검사가 `src/ewbot` 전체를 본다).
- 결정성: 판단은 마감된 1시간봉으로(체결은 15분봉으로)만 하고, 데이터의 마지막 봉에서는 진입을 평가하지 않는다. 시각 t에서 자른 데이터의 판단은 전체 데이터의 t까지 판단과 같아야 한다.
- 고정값(명세 1절 "이 명세가 정한 값", 사전 등록): RSI 14·피벗 좌우 3봉(1시간 봉), ATR 14, 매물대 ±0.25×ATR4h·조회 2,880시간(120일), 손절 여유 0.2×ATR1h, 최대 손절 2.5×ATR4h, 확인 기한 288시간(12일), 시간 손절 192시간(8일), 추세 지그재그 k 2.0. 그리드 32개는 B의 `GRID_B` 그대로: `zz_k` (2.0, 3.0) × 되돌림 구간 ((0.382, 0.618), (0.5, 0.786)) × `tp1_r` (1.0, 1.5) × `e` (1.272, 1.618) × `remainder` (breakeven, original).
- 자산·위험·비용·초기 자금은 B 그대로: BTC(`BTC-USD` 신호, `BIP-20DEC30-CDE` 0.01 BTC)와 ETH(`ETH-USD` 신호, `ETP-20DEC30-CDE` 0.1 ETH), 파라미터 한 벌, 자산마다 포지션 하나, 거래당 0.5%, 포지션마다 3배와 합계 3배, A의 `BreakerParams()`(하루 −3%, 전체 −15%, 5연패 24시간 휴지)를 계좌 단위로, A의 `CostParams()`에 자산별 계약 크기, 10,000 USD.
- `REPLAY_ANCHOR` = 2023-07-01 00:00 UTC(`1688169600`): 홀드아웃 실행과 모의매매 재계산의 시작점.
- 테스트는 네트워크, 실제 `data/cache`, 실제 `holdout-c.lock`·`holdout.lock`을 쓰지 않는다(합성 15분봉, 임시 git 저장소, 가짜 세션·발송기·수집기). 실제 데이터는 Task 9(수동, 컨트롤러와 사용자)에서만 쓴다.
- 파일을 만들 때 상위 폴더가 없으면 만든다. "수정"의 찾을 부분은 그 파일 안에 정확히 한 번 있다.
- 모든 커밋 메시지는 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` 줄로 끝난다(`git commit -m "<제목>" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"`).

## 명세 해석 (이 계획이 정함, 모든 작업에 구속력)

1. **B에서 import하는 것(시간대와 무관한 부품).** `stratb.rules`의 순수 함수(RSI, 피벗, 되돌림 구간, 확장 목표, 매물대 겹침, 다이버전스, 돌파 수준, 손절, 1차 목표, 걸러내기 사유), `stratb.signals`의 `build_frame`·`build_frames`(누락 봉을 채운 15분 격자, 완성된 1시간봉, 자산 끝 맞추기)와 `Frame15`·`SignalB`, `stratb.engine`의 `run_engine`·`BResult`·`asset_of`, `stratb.baselines`의 `buy_hold_b`·`random_control`, `stratb.research`의 `evaluate_folds_b`(A의 워크포워드 규칙을 `GRID_B`에 구현한 것)·`refuse_holdout_bars`·`trades_by_asset`·`wf_data_record`, `stratb.holdout`의 `evaluate_criteria_b`·`COST_MULT`·`data_fingerprint_b`, `stratb.config`의 그리드·자산·비용 도구, `stratb.data15`의 캐시 경로와 수집 상수, (2부) `paperb.decisions`의 `build_decisions_b`·`split_result_b`·`open_positions_b`·`key_of`와 `paperb.notify.position_lines_b`. B는 끝났으므로 이 코드는 바뀌지 않으며, 바뀌더라도 `deps_sha256`이 잡는다. 이 재사용으로 C의 체결·수량·비용·손실 한도·지표·워크포워드·합격 기준은 B와 정확히 같고, C의 새 코드는 시간대에 관한 부분뿐이다.
2. **프레임(명세 4).** `FrameC`는 B의 15분 프레임(`f15`: 엔진, 매수 후 보유, 무작위 대조군이 쓴다)에 1시간봉 배열(`ts`·`open`·`high`·`low`·`close`는 `f15.h_*`, `synthetic` = 그 시간의 15분봉 중 하나라도 합성, `end15` = 그 시간의 마지막 15분봉 인덱스 `f15.h_end`, 1시간 종가 RSI, 1시간 피벗, `atr1h` = `f15.h_atr`), 4시간봉 배열(UTC 4시간 경계에서 시작하는 연속한 1시간봉 4개: `q_*`, 4시간봉마다 마지막 1시간의 인덱스 `q_end`, `q_atr`), 1시간봉마다 그 마감까지 마감된 마지막 4시간봉의 `atr4h`와 `trend`를 더한 것이다. `len(FrameC)`는 1시간봉 수다. B의 `build_frame`이 계산하는 15분봉 RSI·피벗은 C가 쓰지 않는다(B의 코드를 바꾸지 않기 위해 그대로 둔다). 신호는 B의 `SignalB`이고 `i` = 확인 1시간봉의 `end15`, `ts` = 그 15분봉의 시각(hh:45), `p1_ts`·`p2_ts` = 두 피벗의 1시간봉 시각이다. 그래서 B의 엔진은 다음 15분봉 시가, 곧 다음 1시간봉 시가에 진입한다.
3. **추세(B2 그대로).** B2의 코드(`df45473`)는 `master`에 없으므로 `stratc/signals.py`가 B2의 `trend_of`(엄격 비교, 종류마다 마지막 두 개, 두 개 미만이면 NONE)와 4시간 추세 상태를 그대로 옮겨 갖고, 테스트는 B2의 테스트처럼 pandas로 따로 계산한 기준과 비교한다. 걸러내기는 신호 시점에 `run_engine_c`가 `split_by_trend`로 한다: B2에서는 `generate_signals`가 신호에 표시하고 B2의 엔진이 버렸는데, `master`의 B 엔진에는 그 기능이 없으므로 C의 어댑터가 B 엔진 앞에서 버린다. 효과는 같다: 그 신호는 기준 구간을 소모했고, 엔진이 평가했을 시점(`ts ≥ start_ts`, 마지막 봉이 아님)이면 자산이 무엇을 들고 있든 `(신호 ts, 자산, "SIGNAL_FILTERED:trend")`를 기록한다. 이 이벤트는 같은 봉의 엔진 이벤트 뒤에, 자산은 설정 순서로 놓인다. 무작위 대조군에는 걸러내기가 없다(진입 시점만 무작위).
4. **실행(명세 6.2).** `run_engine_c` = `split_by_trend` → B의 `run_engine`(15분 프레임, 남은 신호, `engine_config(cfg)`, `start_ts`) → 거래의 `setup`을 `C-<자산>`으로. `engine_config`는 C의 위험·손실 한도·비용·초기 자금·TP1 비율·자산·합계 상한과, B 엔진이 읽는 두 파라미터(`time_stop_hours` 192, `remainder`)를 담은 `StratBParams`로 `BConfig`를 만든다. B 엔진이 그 밖의 파라미터를 읽지 않는다는 것을 테스트로 고정한다.
5. **데이터와 2024 가드(명세 6.1).** C는 B의 캐시 파일(`stratb.data15.cache_paths`)을 그대로 쓰고, 가드는 `stratc/data.py`의 것이다: `holdout-c.lock`(종류 `ewbot-stratc-holdout`, `sha256`이 B와 같은 15개 묶음 필드의 다이제스트)이 없으면 `fetch_15m_c`는 2024-01-01에 닿는 구간을 요청 전에 거부하고, `update_cache(s)_c`는 그 앞에서 멈추며, `load_15m_c`는 parquet을 읽을 때 2024 이후 봉을 걸러내고, `load_15m_c(with_holdout=True)`는 `HoldoutGuardError`를 낸다. 수집 방식(공개 엔드포인트, 300봉 페이지, 페이지마다 저장해 이어 받기, 연결 오류·시간 초과 재시도)은 B와 같다. B의 잠금은 C의 2024를 열지 않고, C가 같은 파일에 2024 이후 봉을 덧붙여도 B의 읽기는 계속 걸러낸다.
6. **워크포워드(명세 6.3).** `walk_forward_c` = `run_grid_c`(C의 32개 실행, 손실 한도 끔) + B의 `evaluate_folds_b(results, folds(), 10_000)`. `GRID_C`는 `GRID_B` 객체 그 자체이므로(테스트) B의 plateau 규칙이 그대로 적용된다(2단계 그리드: 17개 이상 적격일 때 자신의 Calmar 최대 — Plan 4 해석 2). `proceed`가 거짓이면 C는 끝난다. 출력은 B와 같은 키에 `wf_data`·`code_sha256`(C 지문)·`deps_sha256`.
7. **잠금과 홀드아웃(명세 6.4~6.5).** Plan 4 해석 11의 절차를 C의 잠금·지문·deps로 그대로 한다. 결과의 지표 키는 `C`, `BH`, `C_cost_x1_5`이고, 매수 후 보유와 무작위 대조군은 15분 프레임에서 `engine_config(cfg)`로, 판정은 B의 `evaluate_criteria_b`로 한다.
8. **`deps_sha256`의 범위.** `stratc/`의 import만 따라간다. `paperc/`의 import(A의 `paper/` 모듈, B의 `paperb/` 모듈)는 넣지 않는다: 2부는 잠금 뒤에 쓰이므로 넣으면 잠금이 아직 없던 코드에 묶이고, 모의매매의 판단은 `stratc`의 재계산이 정한다. (B는 `paperb/`의 import까지 묶었는데, B의 모의매매 코드는 잠금 전에 있었다.)
9. **모의매매(2부, 명세 7).** B의 실행기(`paperb/runner.py`)는 B의 잠금·데이터·지문에 묶여 있으므로 같은 흐름을 `paperc/runner.py`에 C의 잠금 사전 점검, C의 가드 읽기, `backtest_c`, C 지문으로 옮긴다. 장부는 A의 스키마를 C 잠금에 묶고(필요 30건, 홀드아웃 거래 수 키 `C`), 판단은 B의 `build_decisions_b`에 C 표기를 쓰며(B는 SIGNAL의 `setup`을 `B-<자산>`으로 쓴다), 메시지 접두어는 `[ewbot paper-C]`다. git 동기화는 `paperb/sync.py`의 규칙을 `paper-c` 브랜치로 옮긴다(B의 모듈은 브랜치 이름이 고정이다). SIGNAL의 시각은 15분봉(hh:45)이고 A의 `known_at`은 그것을 1시간 뒤로 셈하므로 APPEARED 검사만 늦어진다(B와 같음).
10. **CLI.** `ewbot.stratc`·`ewbot.paperc`는 처리 함수 안에서만 import한다. 잠금 경로는 `_c_lock()`이 호출 시점에 `stratc.data.DEFAULT_LOCK_PATH`를 읽어 정한다(테스트가 그 값을 바꾼다). `--params`는 B의 그리드이므로 B의 `_b_params`를 쓴다.
11. **"한 시간대 위의 B" 테스트.** `tests/stratc/helpers.py`의 `dilate`는 B의 15분봉 하나를, 그 봉의 시가에서 종가로 가는 직선 위의 15분봉 4개(봉의 위·아래 꼬리를 그대로)로 바꾼다. 가격이 2진 격자 위에 있어 계산이 정확하고, 기준 시각이 4시간 경계면 C의 1시간봉은 B의 15분봉과, C의 4시간봉은 B의 1시간봉과 같다. 그러므로 C의 기준 구간과 걸러내기 전 신호는 B의 것과 같아야 한다(봉 `4i+3`, 같은 가격, 늘어난 피벗 시각). 근삿값이 아니라 정확히 같음을 확인한다.
12. **C의 합성 셋업.** B의 `LONG_SETUP`을 늘리면 4시간 저점이 내려가므로(1039.5 → 999.5) 추세가 NONE이고 신호가 걸러진다(이것도 테스트한다). `LONG_SETUP_C`는 첫 저점만 959.5로 바꿔(첫 구간 목표 960) 4시간 HH·HL(UP)을 만들고, 같은 롱이 1시간봉 177(15분봉 711)에서 같은 가격으로 확인된다. `world_dfs_c`는 이것을 반복하고(반복마다 232시간) ETH는 그 거울이다.
13. **1부와 2부.** 1부는 Task 1~9다. 2부(Task 10~15)는 Task 9의 홀드아웃이 `PASSED`일 때만, `master`(1부와 `first_run`이 기록된 `holdout-c.lock`이 병합된 상태)에서 새 브랜치로 한다. 탈락이나 워크포워드 미진행이면 2부는 영원히 하지 않는다.

## File Structure

| 파일 | 작업 | 책임 |
|---|---|---|
| `src/ewbot/stratc/__init__.py` | 1 | 패키지 표시 |
| `src/ewbot/stratc/config.py` | 1 | `StratCParams`(고정값), `GRID_C`(= `GRID_B`), `CConfig`, 조합 → 파라미터·설정, B 엔진용 `engine_config` |
| `src/ewbot/stratc/fingerprint.py` | 1 | C 지문, `stratc/`가 import하는 A·B 모듈의 해시(deps) |
| `src/ewbot/stratc/data.py` | 2 | B의 캐시 파일에 대한 C의 2024 가드(잠금 다이제스트), 수집·이어 받기·읽기, `REPLAY_ANCHOR` |
| `src/ewbot/stratc/signals.py` | 3 | `FrameC`(15분 + 1시간 + 4시간), 4시간 추세(B2), 4시간 기준 구간, 1시간 확인, 추세 걸러내기 |
| `src/ewbot/stratc/engine.py` | 4 | B의 15분 엔진 + 추세 걸러내기 + C 표기 |
| `src/ewbot/stratc/research.py` | 5 | 2024 가드, 단일 백테스트, 그리드, 워크포워드(B의 규칙) |
| `src/ewbot/stratc/holdout.py` | 6 | `holdout-c.lock`, 1회 홀드아웃, B의 합격 기준 |
| `src/ewbot/cli.py` (수정) | 7, 12 | `ewbot stratc fetch | backtest | walkforward | holdout lock | holdout run`(1부), `paper run | paper status`(2부) |
| `docs/strategy-c.md` | 7, 13 | 운영 문서(1부: 연구·홀드아웃, 2부: 모의매매) |
| `tests/paper/test_static.py` (수정) | 1, 13 | C의 CI 검사(1부 1개, 2부 1개와 워크플로 목록) |
| `src/ewbot/paperc/__init__.py`, `adapters.py` | 10 | (2부) 장부·판단·메시지 어댑터 |
| `src/ewbot/paperc/runner.py` | 11 | (2부) 모의매매 1회 실행 |
| `src/ewbot/paperc/sync.py`, `.github/workflows/paper-c.yml` | 13 | (2부) `paper-c` 동기화, 휴면 워크플로 |
| `tests/stratc/*`, `tests/paperc/*` | 1–13 | 테스트와 합성 데이터 도우미 |

## 작업 개요

| 작업 | 구성 | 새 테스트 |
|---|---|---|
| **1부** | **연구 코드 (지금)** | |
| 1 | C 설정·지문, A·B 보호 검사(+ CI 검사 1개) | 17 |
| 2 | B의 캐시 파일에 대한 C의 2024 가드 | 8 |
| 3 | 프레임, 4시간 추세, 기준 구간, 1시간 확인(+ 합성 데이터 도우미) | 22 |
| 4 | 실행: B의 엔진 + 추세 걸러내기 + C 표기 | 9 |
| 5 | 백테스트 가드, 그리드, 워크포워드 | 4 |
| 6 | 잠금, 1회 홀드아웃, 합격 기준 | 12 |
| 7 | CLI `ewbot stratc …`, 운영 문서 | 7 |
| 8 | 1부 최종 검증 | — |
| 9 | 수동: 병합, 워크포워드, 잠금, 승인, 홀드아웃 1회 (컨트롤러와 사용자만) | — |
| **2부** | **모의매매 (홀드아웃 합격 후에만)** | |
| 10 | 어댑터(장부, 판단, 메시지) | 6 |
| 11 | 모의매매 실행기(+ 도우미) | 9 |
| 12 | CLI `ewbot stratc paper …` | 6 |
| 13 | `paper-c` 동기화, 휴면 워크플로, CI 검사, 운영 문서 | 11 |
| 14 | 2부 최종 검증 | — |
| 15 | 수동: 모의매매 시작 (사용자) | — |

1부 뒤 전체: `1459 passed, 1 skipped` — 기준선보다 79개 많다(`tests/stratc` 78개, `tests/paper/test_static.py` 1개). 2부 뒤 전체: `1491 passed, 1 skipped` — 1부 뒤보다 32개 많다(`tests/paperc` 31개, `tests/paper/test_static.py` 1개). 각 작업의 Step 4는 그 작업의 테스트만 돌린다. 새 테스트는 1부 약 15초, 2부 약 20초, 전체는 약 2분 30초.

---

# 1부 — 연구 코드

### Task 1: C 설정과 지문, A·B 보호 검사

**Files:**
- Create: `src/ewbot/stratc/__init__.py`
- Create: `src/ewbot/stratc/config.py`
- Create: `src/ewbot/stratc/fingerprint.py`
- Test: `tests/stratc/__init__.py`
- Test: `tests/stratc/test_config.py`
- Test: `tests/stratc/test_fingerprint_c.py`
- Test: `tests/stratc/test_isolation.py`
- Modify: `tests/paper/test_static.py` (파일 끝에 CI 검사 1개)

**Interfaces:**
- Consumes: `ewbot.config`: `BreakerParams`, `CostParams`, `RiskProfile`. `ewbot.stratb.config`: `ASSET_SPECS`, `GRID_B`, `REMAINDER_MODES`, `AssetSpec`, `BConfig(params, risk, breakers, costs, initial_equity, tp1_frac, assets, max_total_leverage)`, `Combo`, `StratBParams`, `normalize_combo(values) -> Combo`(그리드 밖이면 `ValueError`), `grid_combos() -> list[Combo]`(32개). `ewbot.paper.fingerprint`: `PROJECT_ROOT`, `PKG_PREFIX`(`"src/ewbot/"`), `fingerprint_of(files) -> str`, `current_fingerprint(repo_root=None) -> str`, `is_fingerprinted(rel) -> bool`, `tree_files(repo_root=None) -> dict[str, bytes]`.
- Produces: `ewbot.stratc.config`: `GRID_C`(`GRID_B` 그 자체), `TREND_ZZ_K = 2.0`; `@dataclass(frozen=True, slots=True) StratCParams(zz_k=2.0, zone=(0.382, 0.618), tp1_r=1.0, ext=1.272, remainder="breakeven", rsi_len=14, pivot_w=3, confluence_atr=0.25, stop_buf_atr=0.2, max_stop_atr4h=2.5, confirm_hours=288, time_stop_hours=192, atr_len=14, confluence_lookback_hours=2880)`(잘못된 `remainder`·`zone`은 `ValueError`); `RISK_C = RiskProfile("stratc", 0.005, 3.0)`; `@dataclass(frozen=True, slots=True) CConfig(params=StratCParams(), risk=RISK_C, breakers=BreakerParams(), costs=CostParams(), initial_equity=10_000.0, tp1_frac=0.5, assets=ASSET_SPECS, max_total_leverage=3.0)`; `params_for(combo) -> StratCParams`; `combo_of(p) -> Combo`; `config_for(combo, *, breakers_enabled=True) -> CConfig`; `engine_config(cfg: CConfig) -> BConfig`.
  `ewbot.stratc.fingerprint`: `STRATC_PREFIX = "src/ewbot/stratc/"`, `C_PACKAGE = "stratc"`, `stratc_files(repo_root=None) -> dict[str, bytes]`, `stratc_fingerprint(repo_root=None) -> str`(파일이 없으면 `FileNotFoundError`), `deps_files(repo_root=None) -> dict[str, bytes]`, `deps_fingerprint(repo_root=None) -> str`(없으면 `FileNotFoundError`).
  `tests/stratc/test_isolation.py`: 이후 모든 작업에서 A·B 보호를 지키는 검사.

C의 모든 매개변수를 `stratc/config.py`에 둔다. 그리드는 B의 것 그대로이고(`GRID_C is GRID_B`), 그래서 조합 도구(`normalize_combo`, `combo_to_json`, `grid_combos`)와 워크포워드의 plateau 규칙을 B의 것으로 쓴다. 고정값은 B의 값을 한 시간대 위로 올린 것이다(확인 기한·시간 손절·매물대 조회는 정확히 4배임을 테스트한다). 실행 설정은 B의 것과 같은 위험·손실 한도·비용·자산이며, `engine_config`가 C의 시간 손절과 잔량 규칙을 B 엔진에 넘긴다(해석 4). C 지문은 `stratc/` 아래 파일만 해시하고, `deps_fingerprint`는 `stratc/`가 직접 또는 다른 모듈을 거쳐 import하는 바깥 모듈 — A의 모듈과 B의 모듈 — 을 해시한다(B의 `stratb/fingerprint.py`와 같은 ast 방식; 함수 안의 import, 상대 import, 패키지 `__init__.py` 포함; `paperc/`의 import는 넣지 않는다: 해석 8). `tests/stratc/test_isolation.py`는 이 계획 전체의 안전장치다: A 지문 고정값, `stratc/`·`paperc/`가 A 지문 대상 밖인지, `master`와의 merge-base 이후 C의 경로를 바꾼 브랜치가 A·B의 경로를 하나도 바꾸지 않았는지(추적되지 않은 새 파일 포함; 얕은 클론에서는 건너뜀), 리터럴 `with_holdout=True`를 쓰는 모듈이 `stratc/holdout.py`·`paperc/runner.py`뿐이고 변수로 넘기는 곳이 없는지, C 코드가 A·B·A-ETH의 2024 키워드를 쓰지 않는지, A·B의 산출물 이름을 문자열로 쓰지 않는지 확인한다. 집합 검사는 부분집합이라 아직 없는 모듈이 있어도 통과한다. CI에서 도는 `tests/paper/test_static.py`에는 C가 A 지문 밖이라는 검사 1개를 파일 끝에 더한다.

- [ ] **Step 1: 실패하는 테스트 작성**

테스트 폴더와 파일 4개를 만들고 `tests/paper/test_static.py` 끝에 검사 1개를 더한다. `src/ewbot/stratc/` 폴더는 아직 만들지 않는다(만들면 네임스페이스 패키지로 읽혀 Step 2의 메시지가 달라진다).

```bash
mkdir -p tests/stratc
```

**파일 `tests/stratc/__init__.py`** (새 파일, 전체 내용)

```python
"""Tests for ewbot.stratc (Plan 5b)."""
```

**파일 `tests/stratc/test_config.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import dataclasses

import pytest

from ewbot.config import BreakerParams, CostParams
from ewbot.stratb.config import ASSET_SPECS, GRID_B, BConfig, StratBParams, grid_combos
from ewbot.stratc.config import (
    GRID_C,
    RISK_C,
    TREND_ZZ_K,
    CConfig,
    StratCParams,
    combo_of,
    config_for,
    engine_config,
    params_for,
)


def test_the_grid_is_strategy_b_s_32_combos():
    assert GRID_C is GRID_B and list(GRID_C) == ["zz_k", "zone", "tp1_r", "ext", "remainder"]
    assert GRID_C["zz_k"] == (2.0, 3.0) and GRID_C["zone"] == ((0.382, 0.618), (0.5, 0.786))
    assert GRID_C["tp1_r"] == (1.0, 1.5) and GRID_C["ext"] == (1.272, 1.618)
    assert GRID_C["remainder"] == ("breakeven", "original") and len(grid_combos()) == 32


def test_defaults_are_the_first_combo_and_the_pre_registered_fixed_values():
    p = StratCParams()
    assert combo_of(p) == grid_combos()[0] and TREND_ZZ_K == 2.0
    assert (p.rsi_len, p.pivot_w, p.atr_len) == (14, 3, 14)
    assert (p.confluence_atr, p.stop_buf_atr, p.max_stop_atr4h) == (0.25, 0.2, 2.5)
    assert (p.confirm_hours, p.time_stop_hours, p.confluence_lookback_hours) == (288, 192, 2880)
    b = StratBParams()  # four times strategy B's windows (one timeframe up)
    assert (p.confirm_hours, p.time_stop_hours, p.confluence_lookback_hours) == (
        4 * b.confirm_hours, 4 * b.time_stop_hours, 4 * b.confluence_lookback_hours)


def test_params_round_trip_every_combo_and_refuse_off_grid_values():
    for c in grid_combos():
        assert combo_of(params_for(c)) == c
    assert params_for([3, [0.5, 0.786], 1.5, 1.618, "original"]) == StratCParams(3.0, (0.5, 0.786), 1.5, 1.618, "original")
    with pytest.raises(ValueError):
        params_for([2.5, [0.382, 0.618], 1.0, 1.272, "breakeven"])
    with pytest.raises(ValueError):
        StratCParams(remainder="trail")
    with pytest.raises(ValueError):
        StratCParams(zone=(0.7, 0.5))


def test_the_run_config_is_strategy_b_s_risk_breakers_costs_assets_and_equity():
    cfg = config_for(grid_combos()[0])
    b = BConfig()
    assert (RISK_C.risk_pct, RISK_C.max_leverage) == (b.risk.risk_pct, b.risk.max_leverage) == (0.005, 3.0)
    assert cfg.risk == RISK_C and cfg.breakers == BreakerParams() and cfg.costs == CostParams()
    assert (cfg.initial_equity, cfg.tp1_frac, cfg.max_total_leverage) == (10_000.0, 0.5, 3.0)
    assert cfg.assets == ASSET_SPECS == b.assets and [s.contract_size for s in cfg.assets] == [0.01, 0.1]
    assert config_for(grid_combos()[0], breakers_enabled=False).breakers.enabled is False
    assert CConfig().params == StratCParams()


def test_the_engine_config_carries_c_s_time_stop_and_remainder_to_b_s_engine():
    cfg = dataclasses.replace(config_for(grid_combos()[-1]), initial_equity=5_000.0)
    e = engine_config(cfg)
    assert isinstance(e, BConfig)
    assert (e.params.time_stop_hours, e.params.remainder) == (192, "original")
    assert (e.risk, e.breakers, e.costs, e.initial_equity, e.tp1_frac, e.assets, e.max_total_leverage) == (
        cfg.risk, cfg.breakers, cfg.costs, 5_000.0, cfg.tp1_frac, cfg.assets, cfg.max_total_leverage)
```

**파일 `tests/stratc/test_fingerprint_c.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import shutil

import pytest

from ewbot.paper.fingerprint import PROJECT_ROOT, current_fingerprint, fingerprint_of
from ewbot.stratb.fingerprint import stratb_fingerprint
from ewbot.stratc.fingerprint import STRATC_PREFIX, deps_files, deps_fingerprint, stratc_files, stratc_fingerprint


def _copy(tmp_path):
    shutil.copytree(PROJECT_ROOT / "src" / "ewbot", tmp_path / "src" / "ewbot",
                    ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


def _tree(root, files):
    for rel, text in files.items():
        f = root / "src" / "ewbot" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return root


def test_the_file_set_is_every_python_file_below_stratc():
    files = stratc_files()
    pkg = PROJECT_ROOT / "src" / "ewbot" / "stratc"
    assert set(files) == {STRATC_PREFIX + f.relative_to(pkg).as_posix() for f in pkg.rglob("*.py")}
    assert "src/ewbot/stratc/config.py" in files and all(k.startswith(STRATC_PREFIX) for k in files)


def test_an_edit_an_addition_and_a_removal_change_it_and_a_b_and_c_are_independent(tmp_path):
    root = _copy(tmp_path)
    a0, b0, c0 = current_fingerprint(root), stratb_fingerprint(root), stratc_fingerprint(root)
    assert c0 == stratc_fingerprint(PROJECT_ROOT)
    cfg = root / "src/ewbot/stratc/config.py"
    original = cfg.read_bytes()
    cfg.write_bytes(original + b"# comment\n")
    assert stratc_fingerprint(root) != c0 and (current_fingerprint(root), stratb_fingerprint(root)) == (a0, b0)
    cfg.write_bytes(original)
    assert stratc_fingerprint(root) == c0
    (root / "src/ewbot/stratc/extra.py").write_text("x = 1\n", encoding="utf-8")
    assert stratc_fingerprint(root) != c0
    (root / "src/ewbot/stratc/extra.py").unlink()
    loop = root / "src/ewbot/engine/loop.py"
    loop.write_bytes(loop.read_bytes() + b"# one line\n")
    sig = root / "src/ewbot/stratb/signals.py"
    sig.write_bytes(sig.read_bytes() + b"# one line\n")
    assert stratc_fingerprint(root) == c0 and current_fingerprint(root) != a0 and stratb_fingerprint(root) != b0
    (root / "src/ewbot/stratc/fingerprint.py").unlink()
    assert stratc_fingerprint(root) != c0


def test_an_empty_tree_has_no_fingerprint(tmp_path):
    with pytest.raises(FileNotFoundError):
        stratc_fingerprint(tmp_path)
    with pytest.raises(FileNotFoundError):
        deps_fingerprint(_tree(tmp_path, {"stratc/__init__.py": ""}))


def test_deps_are_the_modules_stratc_imports_directly_or_through_others(tmp_path):
    root = _tree(tmp_path, {
        "__init__.py": "", "config.py": "", "cli.py": "from ewbot.stratc import x\n",
        "stratc/__init__.py": "", "stratc/y.py": "def f():\n    from ewbot.stratb import rules\n",
        "stratc/x.py": "from ewbot.backtest.metrics import m\nimport ewbot.risk.sizing\nfrom ewbot.stratc import y\n",
        "paperc/__init__.py": "from ewbot.paper import ledger\n",
        "stratb/__init__.py": "", "stratb/rules.py": "from ewbot.config import C\n", "stratb/unused.py": "",
        "backtest/__init__.py": "", "backtest/metrics.py": "from ewbot.engine.loop import E\nfrom . import report\n",
        "backtest/report.py": "", "backtest/unused.py": "", "engine/__init__.py": "", "engine/loop.py": "",
        "risk/__init__.py": "", "risk/sizing.py": "", "paper/__init__.py": "", "paper/ledger.py": "",
        "live/__init__.py": "", "live/app.py": "from ewbot.stratc import x\n",
    })
    files = deps_files(root)
    assert sorted(files) == ["src/ewbot/" + r for r in (
        "__init__.py", "backtest/__init__.py", "backtest/metrics.py", "backtest/report.py", "config.py",
        "engine/__init__.py", "engine/loop.py", "risk/__init__.py", "risk/sizing.py", "stratb/__init__.py",
        "stratb/rules.py")]
    base = deps_fingerprint(root)
    assert base == fingerprint_of(files)
    for rel in ("cli.py", "live/app.py", "backtest/unused.py", "stratb/unused.py", "paperc/__init__.py",
                "paper/ledger.py", "stratc/y.py"):
        f = root / "src" / "ewbot" / rel
        f.write_text(f.read_text(encoding="utf-8") + "# not imported by stratc/, or C's own\n", encoding="utf-8")
    assert deps_fingerprint(root) == base
    (root / "src/ewbot/stratb/rules.py").write_text("R = 2\n", encoding="utf-8")
    assert deps_fingerprint(root) != base


def test_the_real_deps_are_strategy_a_and_b_modules():
    """The modules stratc/ has so far import (later tasks add B's rules, engine, baselines and holdout: Task 6's
    lock test checks those)."""
    files = deps_files()
    assert {"src/ewbot/config.py", "src/ewbot/stratb/config.py", "src/ewbot/paper/fingerprint.py"} <= set(files)
    assert not [p for p in files if p.startswith(("src/ewbot/stratc/", "src/ewbot/paperc/", "src/ewbot/paperb/",
                                                  "src/ewbot/live/"))]
    assert "src/ewbot/cli.py" not in files and deps_fingerprint() == fingerprint_of(files)
```

**파일 `tests/stratc/test_isolation.py`** (새 파일, 전체 내용)

```python
"""Protection of strategies A and B (Plan 5b spec 2, 9): strategy C must not change anything A's paper, live or
go-live count depends on, nor strategy B's ended code. These run in the full suite; tests/paper/test_static.py (run by
ci.yml) carries the CI part."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

import ewbot
from ewbot.paper.fingerprint import PROJECT_ROOT, current_fingerprint, is_fingerprinted, tree_files

SRC = Path(ewbot.__file__).resolve().parent
FROZEN_A = "e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1"
C_FOLDERS = ("stratc", "paperc")
# Strategy A's files (fingerprinted code, research code, runtimes, lock, workflows, scripts, lockfiles) and strategy
# B's (ended, kept as a record; C imports it, never changes it).
PROTECTED = (
    "src/ewbot/types.py", "src/ewbot/config.py", "src/ewbot/indicators.py", "src/ewbot/data/",
    "src/ewbot/waves/", "src/ewbot/strategy/", "src/ewbot/engine/", "src/ewbot/execution/", "src/ewbot/risk/",
    "src/ewbot/backtest/", "src/ewbot/paper/", "src/ewbot/live/", "src/ewbot/stratb/", "src/ewbot/paperb/",
    "holdout.lock", ".github/workflows/paper.yml", ".github/workflows/ci.yml", ".github/workflows/paper-b.yml",
    "scripts/", "ops/", "deploy/", "uv.lock", "pyproject.toml", "tests/stratb/", "tests/paperb/",
)
C_PATHS = ("src/ewbot/stratc/", "src/ewbot/paperc/", "tests/stratc/", "tests/paperc/", ".github/workflows/paper-c.yml",
           "docs/strategy-c.md", "holdout-c.lock")
WITH_TRUE = {"stratc/holdout.py", "paperc/runner.py"}  # the only callers that may read 2024+ candles for C
# Runtime artifacts of strategy A and strategy B that strategy C code must never name.
FOREIGN = {"paper.yml", "paper-b.yml", "holdout.lock", "holdout-b.lock", "refs/heads/paper", "origin/paper", "paper-b",
           "BTC-USD_1h.parquet", "btc-usd-1h-", "stratb-15m-"}


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], capture_output=True, text=True, timeout=60)


def _merge_base() -> str | None:
    for ref in ("master", "origin/master"):
        r = _git("merge-base", "HEAD", ref)
        if r.returncode == 0:
            return r.stdout.strip()
    return None


def _modules(folders: tuple[str, ...] | None = None):
    roots = [SRC] if folders is None else [SRC / f for f in folders if (SRC / f).is_dir()]
    for root in roots:
        for f in sorted(root.rglob("*.py")):
            yield f.relative_to(SRC).as_posix(), ast.parse(f.read_text(encoding="utf-8"), filename=str(f))


def test_strategy_a_fingerprint_is_unchanged():
    assert current_fingerprint() == FROZEN_A


def test_strategy_c_code_is_outside_strategy_a_fingerprinted_folders():
    for folder in C_FOLDERS:
        assert not is_fingerprinted(f"{folder}/x.py") and not is_fingerprinted(f"{folder}/sub/y.py")
    assert [rel for rel, _ in _modules(("stratc",))]
    assert not [p for p in tree_files() if p.startswith(("src/ewbot/stratc/", "src/ewbot/paperc/"))]


def test_strategy_c_work_changes_no_strategy_a_or_b_path():
    """Since the merge-base with master (tracked changes and new untracked files): a branch that touches strategy C
    touches no strategy-A or strategy-B path. A branch that does not touch strategy C is not concerned."""
    base = _merge_base()
    if base is None:
        pytest.skip("no master branch in this clone (shallow CI checkout)")
    diff = _git("diff", "--name-only", base)
    new = _git("ls-files", "--others", "--exclude-standard")
    assert diff.returncode == 0 and new.returncode == 0
    changed = set(diff.stdout.split()) | set(new.stdout.split())
    if any(p.startswith(C_PATHS) for p in changed):
        assert sorted(p for p in changed if p.startswith(PROTECTED)) == []


def test_only_the_c_holdout_and_the_paper_c_runner_unlock_2024_candles():
    literal_true, other = set(), set()
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "with_holdout":
                        if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                            literal_true.add(rel)
                        elif not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                            other.add(rel)
    assert literal_true <= WITH_TRUE
    assert other == set()


def test_strategy_c_never_unlocks_the_holdout_of_a_or_b():
    """C reads 2024+ candles only through its own lock: it never passes A's allow_holdout or B's include_holdout, and
    never A-ETH's unlock_2024 either."""
    for rel, tree in _modules(C_FOLDERS):
        kws = {kw.arg for n in ast.walk(tree) if isinstance(n, ast.Call) for kw in n.keywords}
        assert not kws & {"allow_holdout", "include_holdout", "unlock_2024"}, rel


def test_strategy_c_code_never_names_strategy_a_or_b_runtime_artifacts():
    for rel, tree in _modules(C_FOLDERS):
        names = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert not names & FOREIGN, rel
```

**수정 `tests/paper/test_static.py`** (1/1) — 다음 부분을 찾아:

```python
                if str(s.get("uses", "")).startswith(SETUP_UV)]
        assert pins == [PYTHON_PATCH], path.name
```

바꿀 내용:

```python
                if str(s.get("uses", "")).startswith(SETUP_UV)]
        assert pins == [PYTHON_PATCH], path.name


# ----------------------------------------------------------------------------- strategy C (Plan 5b)


def test_strategy_c_code_is_outside_the_strategy_a_fingerprint():
    """Plan 5b spec 2, 9 (A protection, run by ci.yml): strategy C lives in stratc/ and paperc/ only, and none of its
    files is in A's fingerprinted set (A's fingerprint itself is pinned in test_fingerprint.py)."""
    from ewbot.paper.fingerprint import is_fingerprinted, tree_files

    c_files = [f.relative_to(SRC).as_posix() for d in ("stratc", "paperc") for f in (SRC / d).rglob("*.py")]
    assert c_files and not [r for r in c_files if is_fingerprinted(r)]
    assert not [p for p in tree_files() if p.startswith(("src/ewbot/stratc/", "src/ewbot/paperc/"))]
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratc tests/paper/test_static.py -q
```

예상: `No module named 'ewbot.stratc'`
예상: `2 errors`

`ewbot.stratc`가 아직 없어 `test_config.py`와 `test_fingerprint_c.py`가 수집 단계에서 실패하고 실행이 중단된다.

- [ ] **Step 3: 최소 구현**

**파일 `src/ewbot/stratc/__init__.py`** (새 파일, 전체 내용)

```python
"""Strategy C (Plan 5b): strategy B2's rules one timeframe up - 4h structure and trend, 1h confirmation entries -
on BTC and ETH, executed by strategy B's 15-minute portfolio engine.

Every *.py file below this folder is hashed by ewbot.stratc.fingerprint (the C fingerprint)."""
```

**파일 `src/ewbot/stratc/config.py`** (새 파일, 전체 내용)

```python
"""Strategy-C parameters (Plan 5b spec 4-5): strategy B's 32-combo grid with C's fixed values, and the run config.

Strategy C is strategy B2 one timeframe up (spec 1). The grid is B's (GRID_C is GRID_B), so B's grid_combos,
normalize_combo and combo_to_json, and the walk-forward's plateau rule over GRID_B, apply unchanged; combos are
tuples (zz_k, (z_lo, z_hi), tp1_r, ext, remainder). The fixed values are B's scaled to the higher timeframes (spec 5).
Assets, risk, breakers, costs and the initial equity are B's (spec 1). Separate from ewbot.config (strategy A) and
ewbot.stratb.config (strategy B): their types and values are only imported, never changed.

Execution is strategy B's 15-minute portfolio engine (stratb.engine.run_engine, spec 6.2). It reads only
time_stop_hours and remainder of its params; engine_config() is the BConfig that carries C's values to it."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Sequence

from ewbot.config import BreakerParams, CostParams, RiskProfile
from ewbot.stratb.config import (
    ASSET_SPECS,
    GRID_B,
    REMAINDER_MODES,
    AssetSpec,
    BConfig,
    Combo,
    StratBParams,
    normalize_combo,
)

GRID_C: dict[str, tuple[Any, ...]] = GRID_B  # spec 5: the same 32 combos as strategy B
TREND_ZZ_K = 2.0  # spec 4.1: ATR multiple of the 4h trend ZigZag (B2's value); fixed, not a grid value


@dataclass(frozen=True, slots=True)
class StratCParams:
    """Grid values (defaults = the first grid combo) and the fixed values of spec 5, which are not in the grid:
    RSI 14 and 3/3 pivots on 1h bars, confluence +/- 0.25 x ATR4h with the 4h pivots of the 120 days before L,
    stop buffer 0.2 x ATR1h, at most 2.5 x ATR4h of stop, a 288-hour (12-day) confirmation window, a 192-hour
    (8-day) time stop, ATR 14."""

    zz_k: float = 2.0
    zone: tuple[float, float] = (0.382, 0.618)
    tp1_r: float = 1.0
    ext: float = 1.272
    remainder: str = "breakeven"
    rsi_len: int = 14
    pivot_w: int = 3
    confluence_atr: float = 0.25
    stop_buf_atr: float = 0.2
    max_stop_atr4h: float = 2.5
    confirm_hours: int = 288
    time_stop_hours: int = 192
    atr_len: int = 14
    confluence_lookback_hours: int = 2880

    def __post_init__(self) -> None:
        if self.remainder not in REMAINDER_MODES:
            raise ValueError(f"remainder must be one of {REMAINDER_MODES}, got {self.remainder!r}")
        if not 0.0 < self.zone[0] < self.zone[1] < 1.0:
            raise ValueError(f"zone must satisfy 0 < z_lo < z_hi < 1, got {self.zone}")


RISK_C = RiskProfile("stratc", 0.005, 3.0)  # spec 1: 0.5% of equity per trade, leverage cap 3x per position


@dataclass(frozen=True, slots=True)
class CConfig:
    """Everything a strategy-C run depends on; all but the params are strategy B's values (spec 1): breakers
    daily -3%, total -15%, 5 losses -> 24h pause on the one account; A's costs with each asset's contract size;
    10,000 USD; half off at TP1; BTC and ETH (B's AssetSpecs); 3x of the equity for all positions together."""

    params: StratCParams = StratCParams()
    risk: RiskProfile = RISK_C
    breakers: BreakerParams = BreakerParams()
    costs: CostParams = CostParams()
    initial_equity: float = 10_000.0
    tp1_frac: float = 0.5
    assets: tuple[AssetSpec, ...] = ASSET_SPECS
    max_total_leverage: float = 3.0


def params_for(combo: Sequence[Any]) -> StratCParams:
    return StratCParams(**dict(zip(GRID_C, normalize_combo(combo))))


def combo_of(p: StratCParams) -> Combo:
    return tuple(getattr(p, k) for k in GRID_C)


def config_for(combo: Sequence[Any], *, breakers_enabled: bool = True) -> CConfig:
    """The run config of a grid combo. Walk-forward grid runs pass breakers_enabled=False (as A's and B's)."""
    return CConfig(params=params_for(combo), breakers=dataclasses.replace(BreakerParams(), enabled=breakers_enabled))


def engine_config(cfg: CConfig) -> BConfig:
    """The BConfig under which stratb.engine.run_engine (and B's buy & hold and random control) execute a C run:
    C's risk, breakers, costs, equity, TP1 share, assets and total cap, and StratBParams carrying C's time stop and
    remainder rule - the only params run_engine reads (its other fields are B's defaults and unused)."""
    return BConfig(
        params=StratBParams(remainder=cfg.params.remainder, time_stop_hours=cfg.params.time_stop_hours),
        risk=cfg.risk, breakers=cfg.breakers, costs=cfg.costs, initial_equity=cfg.initial_equity,
        tp1_frac=cfg.tp1_frac, assets=cfg.assets, max_total_leverage=cfg.max_total_leverage,
    )
```

**파일 `src/ewbot/stratc/fingerprint.py`** (새 파일, 전체 내용)

```python
"""Strategy-C fingerprints (Plan 5b spec 3, 6.4, 7), in strategy A's format (ewbot.paper.fingerprint.fingerprint_of:
sorted repo-relative paths, each path NUL bytes NUL).

stratc_fingerprint: every *.py file below src/ewbot/stratc. stratc/ is outside A's fingerprinted folders; paperc/ and
cli.py are not part of it. Any byte change, addition, removal or rename below stratc/ changes it.

deps_fingerprint: the code outside stratc/ that strategy C's results depend on - every module outside stratc/ that a
module of stratc/ imports, directly or through other modules, with the __init__.py of their packages: strategy A's
modules (config defaults, costs, sizing, breakers, ZigZag, indicators, metrics, buy & hold, walk-forward windows,
data hashing, ...) and strategy B's (rules, frames, engine, baselines, walk-forward rule, criteria - B's code is
frozen since B ended). The imports are found statically (ast), as in stratb.fingerprint: every `import ewbot...` and
`from ewbot... import ...` (relative imports too) anywhere in a file. paperc/'s own imports are not part of it: the
paper runtime is bookkeeping around stratc's recompute and is written after the lock (spec 7). holdout-c.lock binds
it (deps_sha256): the holdout and paper-C refuse a change."""

from __future__ import annotations

import ast
from pathlib import Path

from ewbot.paper.fingerprint import PKG_PREFIX, PROJECT_ROOT, fingerprint_of

STRATC_PREFIX = "src/ewbot/stratc/"
C_PACKAGE = "stratc"


def stratc_files(repo_root: Path | None = None) -> dict[str, bytes]:
    """{"src/ewbot/stratc/<rel>": bytes} for every *.py file below src/ewbot/stratc."""
    pkg = (PROJECT_ROOT if repo_root is None else Path(repo_root)) / "src" / "ewbot" / C_PACKAGE
    return {STRATC_PREFIX + f.relative_to(pkg).as_posix(): f.read_bytes() for f in pkg.rglob("*.py") if f.is_file()}


def stratc_fingerprint(repo_root: Path | None = None) -> str:
    files = stratc_files(repo_root)
    if not files:
        raise FileNotFoundError(f"no strategy-C source files below {repo_root or PROJECT_ROOT}/src/ewbot/stratc")
    return fingerprint_of(files)


def _module_file(pkg: Path, parts: tuple[str, ...]) -> Path | None:
    """The file of module ewbot.<parts> (a .py file or a package's __init__.py), or None."""
    base = pkg.joinpath(*parts)
    for f in ((base.with_name(base.name + ".py"),) if parts else ()) + (base / "__init__.py",):
        if f.is_file():
            return f
    return None


def _imported(f: Path, pkg: Path) -> set[tuple[str, ...]]:
    """Every ewbot module a file may import (its parts below ewbot), wherever the import statement is."""
    here = f.relative_to(pkg).parts[:-1]
    out: set[tuple[str, ...]] = set()
    for node in ast.walk(ast.parse(f.read_bytes(), filename=str(f))):
        if isinstance(node, ast.Import):
            out.update(tuple(a.name.split("."))[1:] for a in node.names if a.name.split(".")[0] == "ewbot")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level - 1 > len(here):
                    continue
                mod = here[: len(here) - node.level + 1] + (tuple(node.module.split(".")) if node.module else ())
            elif node.module and node.module.split(".")[0] == "ewbot":
                mod = tuple(node.module.split("."))[1:]
            else:
                continue
            out.add(mod)
            out.update(mod + (a.name,) for a in node.names)
    return out


def deps_files(repo_root: Path | None = None) -> dict[str, bytes]:
    """{"src/ewbot/<rel>": bytes} of the modules outside stratc/ that stratc/ imports (module docstring)."""
    pkg = (PROJECT_ROOT if repo_root is None else Path(repo_root)) / "src" / "ewbot"
    todo = [f for f in sorted((pkg / C_PACKAGE).rglob("*.py")) if f.is_file()]
    seen = set(todo)
    while todo:
        for parts in _imported(todo.pop(), pkg):
            for k in range(len(parts) + 1):
                g = _module_file(pkg, parts[:k])
                if g is not None and g not in seen:
                    seen.add(g)
                    todo.append(g)
    rels = (f.relative_to(pkg).as_posix() for f in seen)
    return {PKG_PREFIX + r: (pkg / r).read_bytes() for r in rels if r.split("/")[0] != C_PACKAGE}


def deps_fingerprint(repo_root: Path | None = None) -> str:
    files = deps_files(repo_root)
    if not files:
        raise FileNotFoundError(f"no module imported by strategy C below {repo_root or PROJECT_ROOT}/src/ewbot")
    return fingerprint_of(files)
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratc tests/paper/test_static.py -q
```

예상: `44 passed`

`tests/stratc` 16개와 `tests/paper/test_static.py` 28개(기존 27개 + 새 CI 검사 1개)다.

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratc/__init__.py src/ewbot/stratc/config.py src/ewbot/stratc/fingerprint.py tests/stratc/__init__.py tests/stratc/test_config.py tests/stratc/test_fingerprint_c.py tests/stratc/test_isolation.py tests/paper/test_static.py
git commit -m "feat(stratc): strategy-C params on B's grid, run config, C and deps fingerprints, A/B protection checks" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: B의 15분봉 캐시 파일과 C의 2024 가드

**Files:**
- Create: `src/ewbot/stratc/data.py`
- Test: `tests/stratc/test_data.py`

**Interfaces:**
- Consumes: `ewbot.data.coinbase_history`: `API`, `COLUMNS`, `MAX_PER_REQUEST`, `_get_json(session, url, params) -> list`(429·5xx 재시도; B도 쓰는 함수). `ewbot.stratb.data15`: `FETCH_BACKOFF_S = 1.0`, `FETCH_TRIES = 3`, `PRODUCTS = {"BTC": "BTC-USD", "ETH": "ETH-USD"}`, `START_TS`(2017-01-01), `cache_path(cache_dir, product) -> Path`, `cache_paths(cache_dir) -> dict[str, Path]`(`<cache_dir>/BTC-USD_15m.parquet`, `ETH-USD_15m.parquet`). `ewbot.backtest.walkforward.HOLDOUT_START`, `ewbot.backtest.runner.utc_ts`, `ewbot.paper.fingerprint.PROJECT_ROOT`, `ewbot.stratb.config.M15`.
- Produces: `ewbot.stratc.data`: `REPLAY_ANCHOR = 1688169600`(2023-07-01), `DEFAULT_LOCK_PATH = PROJECT_ROOT / "holdout-c.lock"`, `LOCK_KIND = "ewbot-stratc-holdout"`, `LOCK_BOUND`(`kind`, `version`, `keys`, `params`, `config`, `grid`, `holdout_start`, `replay_anchor`, `last_bar_ts`, `random_runs`, `random_seed`, `code_sha256`, `deps_sha256`, `wf_data`, `wf_report_sha256`), `class HoldoutGuardError(RuntimeError)`, `lock_digest_c(lock) -> str`, `lock_exists_c(lock_path) -> bool`, `fetch_15m_c(product, start_ts, end_ts, session=None, sleep_s=0.15, *, lock_path=DEFAULT_LOCK_PATH, on_page=None) -> pd.DataFrame`, `update_cache_c(cache_dir, *, product, lock_path=DEFAULT_LOCK_PATH, start_ts=START_TS, now_ts=None, session=None, sleep_s=0.15) -> Path`, `update_caches_c(cache_dir, *, lock_path=DEFAULT_LOCK_PATH, start_ts=START_TS, now_ts=None, session=None, sleep_s=0.15) -> dict[str, Path]`, `load_15m_c(path, *, with_holdout=False, lock_path=DEFAULT_LOCK_PATH) -> pd.DataFrame`, `load_caches_c(cache_dir) -> dict[str, pd.DataFrame]`.

C는 B의 캐시 파일을 그대로 읽고 늘린다(해석 5). 수집·이어 받기·재시도는 B의 `stratb/data15.py`와 같은 방식이지만, B의 가드가 `holdout-b.lock`에 묶여 있어(B는 잠금 없이 끝났다) C의 2024를 열 수 없으므로 같은 가드를 C의 잠금에 묶어 다시 둔다. 잠금이 "있다"는 것은 이름이나 모양이 아니라 다이제스트가 맞는 C 종류의 잠금이라는 뜻이다. 테스트는 가짜 세션으로 300봉 페이지, 잠금 전 2024 구간 거부(B 종류의 잠금으로도 안 열림), 재시도 대기, 중단 뒤 이어 받기, 잠금 전후의 캐시 갱신, 같은 파일에서 B의 읽기·갱신이 2024를 여전히 막는지, 읽기 가드, 잠금 판정을 확인한다.

- [ ] **Step 1: 실패하는 테스트 작성**

**파일 `tests/stratc/test_data.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import json
from datetime import UTC, datetime

import pandas as pd
import pytest
import requests

from ewbot.data import coinbase_history as ch
from ewbot.stratb.data15 import cache_path, cache_paths, load_15m, update_caches_15m
from ewbot.stratc import data as cd
from ewbot.stratc.data import (
    LOCK_BOUND,
    LOCK_KIND,
    REPLAY_ANCHOR,
    HoldoutGuardError,
    fetch_15m_c,
    load_15m_c,
    lock_digest_c,
    lock_exists_c,
    update_cache_c,
    update_caches_c,
)

HS = 1_704_067_200  # 2024-01-01 00:00 UTC
M15 = 900


def _row(ts: int) -> list[float]:
    base = 40_000.0 + (ts - HS) / M15
    return [ts, base - 5.0, base + 10.0, base, base + 1.0, 1.5]  # [time, low, high, open, close, volume]


class FakeSession:
    """Honours start/end (inclusive), at most 300 rows, newest first; records every request. Raises the queued
    exceptions first (one per request), and every request from fail_from on fails."""

    def __init__(self, first_ts: int, last_ts: int, errors=(), fail_from: int | None = None):
        self.first, self.last = first_ts, last_ts
        self.errors, self.fail_from = list(errors), fail_from
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, **params})
        if self.errors:
            raise self.errors.pop(0)
        if self.fail_from is not None and len(self.calls) > self.fail_from:
            raise requests.ConnectionError("coinbase down")
        assert params["granularity"] == 900
        s = int(datetime.strptime(params["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())
        e = int(datetime.strptime(params["end"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())
        tss = [t for t in range(s, e + 1, M15) if self.first <= t <= self.last]
        assert len(tss) <= 300

        class R:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return [_row(t) for t in reversed(tss)]

        return R()


def _lock(path, kind=LOCK_KIND, **fields):
    """A lock file whose sha256 is the digest of its bound fields (the rest of a real lock does not matter here)."""
    lock = {"kind": kind, "last_bar_ts": HS + 3600, **fields}
    lock["sha256"] = lock_digest_c(lock)
    path.write_text(json.dumps(lock), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(ch.time, "sleep", lambda s: None)


def test_constants_and_strategy_b_s_cache_files():
    assert REPLAY_ANCHOR == 1_688_169_600  # 2023-07-01 00:00 UTC
    assert cd.DEFAULT_LOCK_PATH.name == "holdout-c.lock" and LOCK_KIND == "ewbot-stratc-holdout"
    assert {a: p.name for a, p in cache_paths("data/cache").items()} == {
        "BTC": "BTC-USD_15m.parquet", "ETH": "ETH-USD_15m.parquet"}


def test_fetch_pages_300_bars_and_refuses_a_2024_window_without_the_c_lock(tmp_path):
    t0 = HS - 1000 * M15
    sess = FakeSession(t0, HS + 50 * M15)
    df = fetch_15m_c("BTC-USD", t0, t0 + 700 * M15, session=sess, sleep_s=0, lock_path=tmp_path / "none.lock")
    assert len(sess.calls) == 3 and sess.calls[0]["url"].endswith("/products/BTC-USD/candles")
    assert df["ts"].tolist() == [t0 + k * M15 for k in range(700)] and df.loc[0, "open"] == _row(t0)[3]
    with pytest.raises(HoldoutGuardError):
        fetch_15m_c("ETH-USD", HS - 10 * M15, HS + M15, session=sess, sleep_s=0, lock_path=tmp_path / "none.lock")
    with pytest.raises(HoldoutGuardError):  # strategy B's lock kind does not open 2024 for C
        fetch_15m_c("ETH-USD", HS - 10 * M15, HS + M15, session=sess, sleep_s=0,
                    lock_path=_lock(tmp_path / "b.lock", kind="ewbot-stratb-holdout"))
    assert len(sess.calls) == 3
    df = fetch_15m_c("ETH-USD", HS - 10 * M15, HS + M15, session=sess, sleep_s=0, lock_path=_lock(tmp_path / "c.lock"))
    assert int(df["ts"].max()) == HS


def test_connection_errors_and_timeouts_are_retried_with_backoff(monkeypatch, tmp_path):
    waits = []
    monkeypatch.setattr(cd.time, "sleep", waits.append)
    t0 = HS - 400 * M15
    sess = FakeSession(t0, HS, errors=[requests.ConnectionError("reset"), requests.Timeout("slow")])
    assert len(fetch_15m_c("BTC-USD", t0, t0 + 10 * M15, session=sess, sleep_s=0)) == 10
    assert waits == [1.0, 2.0] and len(sess.calls) == 3
    sess = FakeSession(t0, HS, errors=[requests.ConnectionError("x")] * 3)
    with pytest.raises(requests.ConnectionError):
        fetch_15m_c("BTC-USD", t0, t0 + 10 * M15, session=sess, sleep_s=0)


def test_an_interrupted_download_keeps_its_pages_and_resumes(tmp_path):
    t0 = HS - 1000 * M15
    none = tmp_path / "none.lock"
    with pytest.raises(requests.ConnectionError):
        update_cache_c(tmp_path, product="BTC-USD", lock_path=none, start_ts=t0, now_ts=HS,
                       session=FakeSession(t0, HS, fail_from=2), sleep_s=0)
    assert len(pd.read_parquet(cache_path(tmp_path, "BTC-USD"))) == 600
    sess = FakeSession(t0, HS)
    path = update_cache_c(tmp_path, product="BTC-USD", lock_path=none, now_ts=HS, session=sess, sleep_s=0)
    assert sess.calls[0]["start"] == datetime.fromtimestamp(t0 + 600 * M15, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert pd.read_parquet(path)["ts"].tolist() == [t0 + k * M15 for k in range(1000)]


def test_without_the_c_lock_the_caches_stop_before_2024_and_with_it_they_go_on(tmp_path):
    sess = FakeSession(HS - 50 * M15, HS + 500 * M15)
    paths = update_caches_c(tmp_path, lock_path=tmp_path / "none.lock", start_ts=HS - 50 * M15,
                            now_ts=HS + 400 * M15, session=sess, sleep_s=0)
    assert paths == cache_paths(tmp_path) and [c["url"].split("/")[-2] for c in sess.calls] == ["BTC-USD", "ETH-USD"]
    assert all(c["end"] < "2024-01-01" for c in sess.calls)
    for p in paths.values():
        assert int(pd.read_parquet(p)["ts"].max()) == HS - M15
    lock = _lock(tmp_path / "holdout-c.lock")
    update_caches_c(tmp_path, lock_path=lock, now_ts=HS + 10 * M15 + 899, session=sess, sleep_s=0)
    for p in paths.values():
        df = pd.read_parquet(p)
        assert int(df["ts"].max()) == HS + 9 * M15 and df["ts"].is_unique and len(df) == 60
    assert sess.calls[-1]["start"] == "2024-01-01T00:00:00Z"


def test_the_files_are_strategy_b_s_and_b_still_never_reads_2024_from_them(tmp_path):
    sess = FakeSession(HS - 50 * M15, HS + 500 * M15)
    update_caches_c(tmp_path, lock_path=_lock(tmp_path / "holdout-c.lock"), start_ts=HS - 50 * M15,
                    now_ts=HS + 20 * M15, session=sess, sleep_s=0)
    for p in cache_paths(tmp_path).values():
        assert int(load_15m(p)["ts"].max()) == HS - M15  # B's loader filters C's 2024 bars out
        assert len(pd.read_parquet(p)) == 70
    before = len(sess.calls)
    update_caches_15m(tmp_path, lock_path=tmp_path / "holdout-b.lock", now_ts=HS + 40 * M15, session=sess, sleep_s=0)
    assert len(sess.calls) == before  # B's updater asks for nothing: its guard stops at 2024


def test_load_never_returns_2024_bars_without_with_holdout(tmp_path):
    p = tmp_path / "c.parquet"
    pd.DataFrame([_row(t) for t in range(HS - 4 * M15, HS + 4 * M15, M15)],
                 columns=["ts", "low", "high", "open", "close", "volume"]).to_parquet(p, index=False)
    df = load_15m_c(p, lock_path=_lock(tmp_path / "l.lock"))
    assert int(df["ts"].max()) == HS - M15 and len(df) == 4
    with pytest.raises(HoldoutGuardError):
        load_15m_c(p, with_holdout=True, lock_path=tmp_path / "missing.lock")
    (tmp_path / "bogus.lock").write_text("{}", encoding="utf-8")
    with pytest.raises(HoldoutGuardError):
        load_15m_c(p, with_holdout=True, lock_path=tmp_path / "bogus.lock")
    assert len(load_15m_c(p, with_holdout=True, lock_path=tmp_path / "l.lock")) == 8


def test_lock_exists_needs_a_strategy_c_lock(tmp_path):
    assert not lock_exists_c(tmp_path / "x")
    (tmp_path / "x").write_text("not json", encoding="utf-8")
    assert not lock_exists_c(tmp_path / "x")
    assert not lock_exists_c(_lock(tmp_path / "a", kind="ewbot-stratb-holdout"))
    assert lock_exists_c(_lock(tmp_path / "b"))
    (tmp_path / "c").write_text(json.dumps({"kind": LOCK_KIND, "sha256": "ab" * 32}), encoding="utf-8")
    assert not lock_exists_c(tmp_path / "c")  # the right shape is not enough: the digest must match
    edited = json.loads((tmp_path / "b").read_text(encoding="utf-8"))
    edited["last_bar_ts"] += 900
    (tmp_path / "d").write_text(json.dumps(edited), encoding="utf-8")
    assert not lock_exists_c(tmp_path / "d") and "last_bar_ts" in LOCK_BOUND
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratc/test_data.py -q
```

예상: `cannot import name 'data' from 'ewbot.stratc'`
예상: `1 error`

- [ ] **Step 3: 최소 구현**

**파일 `src/ewbot/stratc/data.py`** (새 파일, 전체 내용)

```python
"""15-minute Coinbase spot candles for strategy C (Plan 5b spec 6.1): strategy B's cache files under C's own 2024
guard.

C reads and extends the very files of B's cache (stratb.data15.cache_paths: <cache_dir>/BTC-USD_15m.parquet and
ETH-USD_15m.parquet; the same public endpoint, granularity 900, pages of 300 bars, retries and page-by-page saves as
stratb.data15), so C's walk-forward sees the bars B's did and nothing is downloaded twice. B's own guard waits for
holdout-b.lock, which B never wrote, so it never opens 2024 - for B or for C. C guards 2024 with its own lock:

Until holdout-c.lock exists, fetch_15m_c refuses a window that reaches HOLDOUT_START (2024-01-01), update_caches_c
never asks for one, and load_15m_c never returns a bar at or after it - the parquet read itself filters them out;
load_15m_c(with_holdout=True) raises HoldoutGuardError until the lock exists. A lock "exists" only when it is a JSON
object of kind LOCK_KIND whose sha256 is the digest of its bound fields (lock_digest_c, defined here for
stratc.holdout, which writes the lock). After C's lock the files also hold 2024+ bars; B's loader still filters them
out for B. REPLAY_ANCHOR (2023-07-01) is where the holdout run and paper-C recompute from (spec 6.4)."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd
import requests

from ewbot.backtest.runner import utc_ts
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.data.coinbase_history import API, COLUMNS, MAX_PER_REQUEST, _get_json
from ewbot.paper.fingerprint import PROJECT_ROOT
from ewbot.stratb.config import M15
from ewbot.stratb.data15 import FETCH_BACKOFF_S, FETCH_TRIES, PRODUCTS, START_TS, cache_path, cache_paths

REPLAY_ANCHOR = utc_ts("2023-07-01")  # spec 6.4: holdout and paper runs both recompute from here
DEFAULT_LOCK_PATH = PROJECT_ROOT / "holdout-c.lock"
LOCK_KIND = "ewbot-stratc-holdout"
LOCK_BOUND = ("kind", "version", "keys", "params", "config", "grid", "holdout_start", "replay_anchor", "last_bar_ts",
              "random_runs", "random_seed", "code_sha256", "deps_sha256", "wf_data", "wf_report_sha256")  # sha256 binds


class HoldoutGuardError(RuntimeError):
    """Candles at/after 2024-01-01 were requested before holdout-c.lock exists."""


def lock_digest_c(lock: Mapping[str, Any]) -> str:
    """sha256 of the sorted compact JSON of the lock's LOCK_BOUND fields."""
    blob = json.dumps({k: lock.get(k) for k in LOCK_BOUND}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def lock_exists_c(lock_path: Path) -> bool:
    """True only for a strategy-C holdout lock: a JSON object of kind LOCK_KIND whose sha256 is the digest of its
    bound fields - not for a file that merely has the name or the shape."""
    p = Path(lock_path)
    if not p.is_file():
        return False
    try:
        lock = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(lock, dict) and lock.get("kind") == LOCK_KIND and lock.get("sha256") == lock_digest_c(lock)


def _empty() -> pd.DataFrame:
    return pd.DataFrame({"ts": pd.Series(dtype="int64"), **{c: pd.Series(dtype="float64") for c in COLUMNS[1:]}})


def _standard(df: pd.DataFrame) -> pd.DataFrame:
    df = df[COLUMNS].astype({"ts": "int64", **{c: "float64" for c in COLUMNS[1:]}})
    return df.drop_duplicates("ts").sort_values("ts", kind="stable").reset_index(drop=True)


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _page(sess: requests.Session, url: str, params: dict[str, object]) -> list:
    """A's _get_json (429 / 5xx retried there), also retried on a connection error or a timeout (FETCH_TRIES)."""
    failed = 0
    while True:
        try:
            return _get_json(sess, url, params)
        except (requests.ConnectionError, requests.Timeout):
            failed += 1
            if failed >= FETCH_TRIES:
                raise
            time.sleep(FETCH_BACKOFF_S * 2 ** (failed - 1))


def _rows(rows: list[list[float]], first: int, last_open: int) -> pd.DataFrame:
    if not rows:
        return _empty()
    df = pd.DataFrame(rows, columns=["ts", "low", "high", "open", "close", "volume"])
    df["ts"] = df["ts"].astype("int64")
    return _standard(df[(df["ts"] >= first) & (df["ts"] <= last_open)])


def fetch_15m_c(
    product: str,
    start_ts: int,
    end_ts: int,
    session: requests.Session | None = None,
    sleep_s: float = 0.15,
    *,
    lock_path: Path = DEFAULT_LOCK_PATH,
    on_page: Callable[[pd.DataFrame], None] | None = None,
) -> pd.DataFrame:
    """Closed 15m bars with open time in [start_ts, end_ts), ascending, standard schema (maybe empty); on_page gets
    each window of 300 bars as it arrives. HoldoutGuardError, before any request, when the window reaches
    2024-01-01 and holdout-c.lock does not exist."""
    if end_ts > HOLDOUT_START and not lock_exists_c(lock_path):
        raise HoldoutGuardError(f"{product}: bars from 2024-01-01 on cannot be fetched before the strategy-C lock")
    first = -(-start_ts // M15) * M15
    last_open = (end_ts - 1) // M15 * M15
    if first > last_open:
        return _empty()
    sess = session if session is not None else requests.Session()
    url = API.format(product=product)
    rows: list[list[float]] = []
    w_start = first
    while w_start <= last_open:
        w_end = min(w_start + (MAX_PER_REQUEST - 1) * M15, last_open)
        page = _page(sess, url, {"granularity": M15, "start": _iso(w_start), "end": _iso(w_end)})
        rows.extend(page)
        if on_page is not None:
            on_page(_rows(page, w_start, w_end))
        w_start = w_end + M15
        if w_start <= last_open and sleep_s > 0:
            time.sleep(sleep_s)
    return _rows(rows, first, last_open)


def _save(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def update_cache_c(
    cache_dir: Path,
    *,
    product: str,
    lock_path: Path = DEFAULT_LOCK_PATH,
    start_ts: int = START_TS,
    now_ts: int | None = None,
    session: requests.Session | None = None,
    sleep_s: float = 0.15,
) -> Path:
    """Create (from start_ts) or extend one product's 15m cache file (B's file) up to the last CLOSED bar - and,
    while holdout-c.lock does not exist, never past HOLDOUT_START. Every page is saved as it arrives, so a failed
    download keeps what it got and the next call goes on from there."""
    path = cache_path(cache_dir, product)
    path.parent.mkdir(parents=True, exist_ok=True)
    cur = _standard(pd.read_parquet(path)) if path.exists() else _empty()
    now = int(time.time()) if now_ts is None else int(now_ts)
    end = now // M15 * M15  # exclusive: the bar in progress is never stored
    if not lock_exists_c(lock_path):
        end = min(end, HOLDOUT_START)
    begin = int(cur["ts"].max()) + M15 if len(cur) else int(start_ts)
    if not path.exists():
        _save(cur, path)
    if begin >= end:
        return path

    def append(page: pd.DataFrame) -> None:
        nonlocal cur
        if len(page):
            cur = _standard(pd.concat([cur, page], ignore_index=True))
            _save(cur, path)

    fetch_15m_c(product, begin, end, session=session, sleep_s=sleep_s, lock_path=lock_path, on_page=append)
    return path


def update_caches_c(
    cache_dir: Path,
    *,
    lock_path: Path = DEFAULT_LOCK_PATH,
    start_ts: int = START_TS,
    now_ts: int | None = None,
    session: requests.Session | None = None,
    sleep_s: float = 0.15,
) -> dict[str, Path]:
    """update_cache_c for every asset, with one clock: {asset: cache file}. Network: only here (`stratc fetch`,
    `stratc holdout run`, the paper-C runner, which starts an empty cache at REPLAY_ANCHOR)."""
    now = int(time.time()) if now_ts is None else int(now_ts)
    return {
        a: update_cache_c(cache_dir, product=p, lock_path=lock_path, start_ts=start_ts, now_ts=now, session=session,
                          sleep_s=sleep_s)
        for a, p in PRODUCTS.items()
    }


def load_15m_c(path: Path, *, with_holdout: bool = False, lock_path: Path = DEFAULT_LOCK_PATH) -> pd.DataFrame:
    """The cached 15m bars (standard schema, sorted, unique ts). Without with_holdout the parquet read filters out
    every bar at/after HOLDOUT_START; with_holdout=True needs holdout-c.lock."""
    if with_holdout and not lock_exists_c(lock_path):
        raise HoldoutGuardError(
            f"{lock_path} does not exist: candles from 2024-01-01 on cannot be read before the strategy-C lock"
        )
    filters = None if with_holdout else [("ts", "<", HOLDOUT_START)]
    return _standard(pd.read_parquet(Path(path), filters=filters))


def load_caches_c(cache_dir: Path) -> dict[str, pd.DataFrame]:
    """Every asset's bars before 2024-01-01 (research runs): {asset: load_15m_c(its cache)}."""
    return {a: load_15m_c(p) for a, p in cache_paths(cache_dir).items()}
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratc/test_data.py -q
```

예상: `8 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratc/data.py tests/stratc/test_data.py
git commit -m "feat(stratc): strategy B's 15m cache files under C's own 2024 guard (holdout-c.lock)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: 프레임, 4시간 추세, 4시간 기준 구간, 1시간 확인

**Files:**
- Create: `src/ewbot/stratc/signals.py`
- Test: `tests/stratc/helpers.py`
- Test: `tests/stratc/test_signals.py`

**Interfaces:**
- Consumes: Task 1의 `StratCParams`, `TREND_ZZ_K`. `ewbot.stratb.signals`: `Frame15`(필드 `ts`, `open`, `high`, `low`, `close`, `synthetic`, `rsi`, `atr15`, `piv_lo`, `piv_hi`, `h_ts`, `h_open`, `h_high`, `h_low`, `h_close`, `h_atr`, `h_end`, `last_hour`), `SignalB(i, ts, direction, entry_ref, stop, tp1, tp2, leg_start=0.0, leg_end=0.0, p1_ts=0, p2_ts=0, level=0.0, reason="")`, `build_frame(df15, p: StratBParams) -> Frame15`, `build_frames(dfs, p) -> dict[str, Frame15]`(모든 자산을 같은 마지막 봉에서 끝냄). `ewbot.stratb.rules`: `rsi_series`, `pivot_lows`, `pivot_highs`, `retracement_zone(start, end, z_lo, z_hi)`, `extension_target(start, end, e)`, `has_confluence(zone, levels, tol)`, `is_divergence(direction, price1, price2, rsi1, rsi2)`, `breakout_level(direction, high, low, p1, p2)`, `stop_level(direction, pivot_price, atr, buf_atr)`, `first_target(entry_ref, stop, tp1_r)`, `skip_reason(direction, entry_ref, stop, tp1, tp2, atr_big, max_stop_atr) -> str | None`. `ewbot.waves.zigzag.ZigZag(k, atr_len)`(`update(i, bar) -> Pivot | None`, `pivots`, `atr`), `ewbot.indicators.atr_series`, `ewbot.types`: `H1`, `H4`, `Bar`.
- Produces: `ewbot.stratc.signals`: `@dataclass(frozen=True) FrameC(f15, ts, open, high, low, close, synthetic, end15, rsi, atr1h, piv_lo, piv_hi, q_ts, q_open, q_high, q_low, q_close, q_end, q_atr, atr4h, trend)`(`len()` = 1시간봉 수); `@dataclass(frozen=True, slots=True) LegC(known_j, direction, start, end, start_ts, end_ts, end_j, atr4h, prior)`; `trend_of(highs, lows) -> int`; `frame_from_15m(f15, p=StratCParams()) -> FrameC`; `build_frame_c(df15, p=StratCParams()) -> FrameC`; `build_frames_c(dfs, p=StratCParams()) -> dict[str, FrameC]`; `reference_legs_c(f, zz_k, p=StratCParams()) -> list[LegC]`; `generate_signals_c(f, p, legs=None) -> list[SignalB]`(추세 걸러내기 전); `trend_at(f, sig) -> int`(신호가 어떤 시간의 마지막 15분봉이 아니면 `ValueError`); `split_by_trend(f, sigs) -> tuple[list[SignalB], list[SignalB]]`(추세 방향인 신호, 나머지).
  `tests/stratc/helpers.py`: `LONG_SETUP_C`, `SIGNAL_HOUR = 177`, `SIGNAL_15M = 711`, `REP = 232`, `dilate(df, ref) -> pd.DataFrame`, `long_setup_c_df(t0=T0)`, `world_dfs_c(t0, reps) -> dict[str, pd.DataFrame]`, `save_world(cache_dir, dfs)`; B의 도우미에서 `RANDOM_T0`, `T0`, `mirror`, `random_df`를 다시 내보낸다.

C의 핵심이다(명세 4, 해석 2·3·11·12). 프레임은 B의 15분 프레임에 1시간봉 지표와 4시간봉을 더하고, 1시간봉마다 그 마감까지 마감된 마지막 4시간봉의 ATR과 추세를 붙인다. 추세는 B2의 규칙 그대로다. 기준 구간은 4시간 지그재그(`zz_k`)의 확정 피벗에서, 매물대는 같은 지그재그의 앞선 4시간 피벗(120일)에서, 확인은 1시간 피벗·RSI·종가 돌파로 하며, 손절·걸러내기·목표는 B의 순수 함수를 1시간·4시간 ATR로 부른다. `generate_signals_c`는 추세 걸러내기 전의 모든 확인을 내고, `split_by_trend`가 추세를 적용한다(엔진이 부른다, Task 4). 테스트는 (1) B의 경로를 4배로 늘린 경로에서 C의 프레임·기준 구간·신호가 B의 것과 정확히 같은지(손으로 만든 롱 셋업, 무작위 경로 3개 × 조합 2개), (2) C의 셋업에서 롱과 그 거울 숏의 수준과 추세, (3) 매물대 없음·1시간 종가 무효화·둘째 피벗 깨짐·최대 손절·TP2 걸러내기·기한(마지막 봉 포함)·합성 봉 차단, (4) B2의 추세 테스트(진리표, 계단과 거울, 섞인 스윙, 스윙 부족, 4시간 버킷, 4시간 마감에만 바뀜)를 pandas 기준 계산과, (5) 자른 데이터의 신호·추세·4시간 ATR·걸러내기가 전체 데이터와 같은지, (6) 두 자산의 독립된 추세와 같은 마지막 봉을 확인한다.

- [ ] **Step 1: 실패하는 테스트 작성**

**파일 `tests/stratc/helpers.py`** (새 파일, 전체 내용)

```python
"""Synthetic 15m candles for the strategy-C tests (no network, no real cache).

dilate() stretches strategy B's 15m test paths four times in time: every B bar becomes one hour of four 15m bars
whose 1h bar is exactly that B bar (open, high, low, close; prices stay on a binary grid, so the arithmetic is
exact). The 1h bars of a dilated path are then B's 15m bars and its 4h bars B's 1h bars, so strategy C - B's rules
one timeframe up - must find exactly B's signals on it (tests/stratc/test_signals.py checks this).

LONG_SETUP_C is B's LONG_SETUP with a deeper first low (959.5 instead of 1039.5): the 4h ZigZag confirms the lows
959.5 and 999.5 and the highs 1100.5 and 1200.5 before the signal, so the 4h trend is UP there and the long at 1h
bar 177 (15m bar 711 of the dilated path) is kept. world_dfs_c() is the two-asset world of the holdout and CLI tests:
BTC repeats LONG_SETUP_C (232 hours = 928 15m bars per repetition), ETH is its mirror, so every repetition has one
BTC long (trend UP) and one ETH short (trend DOWN) on the same bar."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ewbot.stratb.data15 import cache_paths
from tests.stratb.helpers import LONG_START, RANDOM_T0, T0, frame_df, mirror, path, random_df

__all__ = ["LONG_SETUP_C", "RANDOM_T0", "REP", "SIGNAL_15M", "SIGNAL_HOUR", "T0", "dilate", "long_setup_c_df",
           "mirror", "random_df", "save_world", "world_dfs_c"]

M15, H1 = 900, 3600
LONG_SETUP_C = [(40, 960), (30, 1100), (25, 1000), (25, 1200), (40, 1110), (5, 1125), (7, 1105), (20, 1205),
                (40, 1180)]
SIGNAL_HOUR = 177  # the confirming 1h bar of one repetition
SIGNAL_15M = 4 * SIGNAL_HOUR + 3  # its last 15m bar: the signal's index in the 15m frame
REP = 232  # hours per repetition


def dilate(df: pd.DataFrame, ref: int) -> pd.DataFrame:
    """B's 15m bars -> C's: the bar at ts becomes the hour at ref + 4 (ts - ref), four 15m bars on the straight line
    from its open to its close, each with the bar's upper and lower wicks (ref must be 4h-aligned for B's hours
    to become C's 4h bars). Missing B bars stay missing."""
    rows = []
    for t, o, h, lo, c, v in df[["ts", "open", "high", "low", "close", "volume"]].itertuples(index=False):
        up, dn = h - max(o, c), min(o, c) - lo
        for m in range(4):
            a, b = o + (c - o) * m / 4, o + (c - o) * (m + 1) / 4
            rows.append((ref + 4 * (int(t) - ref) + M15 * m, a, max(a, b) + up, min(a, b) - dn, b, v / 4))
    return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).astype({"ts": "int64"})


def long_setup_c_df(t0: int = T0) -> pd.DataFrame:
    """One repetition of LONG_SETUP_C as C's 15m bars from t0 (4h-aligned)."""
    return dilate(frame_df(path(LONG_START, LONG_SETUP_C), t0), t0)


def world_dfs_c(t0: int, reps: int) -> dict[str, pd.DataFrame]:
    """BTC: LONG_SETUP_C repeated `reps` times from t0 (4h-aligned); ETH: its mirror."""
    btc = frame_df(path(LONG_START, LONG_SETUP_C * reps), t0)
    return {"BTC": dilate(btc, t0), "ETH": dilate(mirror(btc), t0)}


def save_world(cache_dir: Path, dfs: dict[str, pd.DataFrame]) -> None:
    """Each asset's bars into its 15m cache file below cache_dir (B's file names, which C shares)."""
    for a, p in cache_paths(cache_dir).items():
        dfs[a].to_parquet(p, index=False)
```

**파일 `tests/stratc/test_signals.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd
import pytest

from ewbot.stratb.config import StratBParams
from ewbot.stratb.signals import build_frame, generate_signals, reference_legs
from ewbot.stratc.config import TREND_ZZ_K, StratCParams
from ewbot.stratc.signals import (
    build_frame_c,
    build_frames_c,
    generate_signals_c,
    reference_legs_c,
    split_by_trend,
    trend_at,
    trend_of,
)
from ewbot.types import Bar
from ewbot.waves.zigzag import ZigZag
from tests.stratb.helpers import long_setup_df, path
from tests.stratc.helpers import (
    RANDOM_T0,
    SIGNAL_15M,
    SIGNAL_HOUR,
    T0,
    dilate,
    long_setup_c_df,
    mirror,
    random_df,
)

M15, H1, H4 = 900, 3600, 14400


def _key(s):
    return (s.i, s.ts, s.direction, s.entry_ref, s.stop, s.tp1, s.tp2, s.p1_ts, s.p2_ts, s.level)


def _b_key(s, ref):
    """B's signal as C must find it on the dilated path: the hour of B's bar i closes at 15m bar 4 i + 3."""
    t = ref + 4 * (s.ts - ref) + 3 * M15
    return (4 * s.i + 3, t, s.direction, s.entry_ref, s.stop, s.tp1, s.tp2, ref + 4 * (s.p1_ts - ref),
            ref + 4 * (s.p2_ts - ref), s.level)


def _oracle_trend(f):
    """Per 1h bar: pandas 4h buckets of the gap-filled 15m bars with 16 bars, A's ZigZag(2.0, 14), trend_of after
    each 4h close, carried forward to every later 1h close."""
    g = pd.DataFrame({"ts": f.f15.ts, "o": f.f15.open, "h": f.f15.high, "lo": f.f15.low, "c": f.f15.close})
    agg = g.groupby(g["ts"] // H4).agg(first=("ts", "first"), last=("ts", "last"), n=("ts", "size"),
                                         o=("o", "first"), h=("h", "max"), lo=("lo", "min"), c=("c", "last"))
    agg = agg[agg["n"] == 16]
    zz = ZigZag(2.0, 14)
    swings: dict[int, list[float]] = {1: [], -1: []}
    at_close: dict[int, int] = {}
    for q, r in enumerate(agg.itertuples()):
        piv = zz.update(q, Bar(int(r.first), float(r.o), float(r.h), float(r.lo), float(r.c), 0.0))
        if piv is not None:
            swings[piv.kind].append(piv.price)
        at_close[int(r.last)] = trend_of(swings[1], swings[-1])
    want, cur = np.zeros(len(f), dtype=np.int64), 0
    for j, k in enumerate(f.end15.tolist()):
        for t in range(int(f.f15.ts[k]) - 3 * M15, int(f.f15.ts[k]) + 1, M15):
            cur = at_close.get(t, cur)
        want[j] = cur
    return want, zz.pivots, [Bar(int(r.first), float(r.o), float(r.h), float(r.lo), float(r.c), 0.0)
                             for r in agg.itertuples()]


def _moves(moves, start=10_000.0):
    """15m bars along straight segments of (bars, price change), 6.25 per bar: every 4h bar spans ~100."""
    segs, px = [], start
    for n, dp in moves:
        px += dp
        segs.append((n, px))
    from tests.stratb.helpers import frame_df

    return frame_df(path(start, segs), T0)


@pytest.fixture(scope="module")
def setup():
    p = StratCParams(zz_k=2.0)
    f = build_frame_c(long_setup_c_df(), p)
    return p, f, reference_legs_c(f, p.zz_k, p)


def test_the_c_frame_is_b_s_frame_one_timeframe_up():
    df = long_setup_df()
    b = build_frame(df)
    c = build_frame_c(dilate(df, T0))
    assert len(c.f15) == 4 * len(b) and len(c) == len(b) and np.array_equal(c.end15, 4 * np.arange(len(b)) + 3)
    assert np.array_equal(c.ts, T0 + 4 * (b.ts - T0)) and (c.ts % H1 == 0).all() and (c.q_ts % H4 == 0).all()
    for x, y in ((c.open, b.open), (c.high, b.high), (c.low, b.low), (c.close, b.close), (c.rsi, b.rsi),
                 (c.atr1h, b.atr15), (c.piv_lo, b.piv_lo), (c.piv_hi, b.piv_hi), (c.q_high, b.h_high),
                 (c.q_low, b.h_low), (c.q_close, b.h_close), (c.q_atr, b.h_atr), (c.q_end, b.h_end)):
        assert np.array_equal(x, y, equal_nan=True)
    hour = b.last_hour
    assert np.array_equal(c.atr4h[hour >= 0], b.h_atr[hour[hour >= 0]], equal_nan=True) and np.isnan(c.atr4h[hour < 0]).all()


def test_hours_and_4h_bars_are_the_complete_utc_buckets_of_the_gap_filled_bars():
    df = random_df(400, 9, t0=RANDOM_T0 + 5 * M15).drop(index=[100]).reset_index(drop=True)  # mid-4h start, a gap
    f = build_frame_c(df)
    assert f.f15.synthetic.sum() == 1 and f.synthetic.sum() == 1 and f.synthetic[int(np.searchsorted(f.end15, 100))]
    assert f.ts[0] == RANDOM_T0 + 2 * H1 and (f.f15.ts[f.end15] == f.ts + 3 * M15).all()
    _, _, want = _oracle_trend(f)
    assert [Bar(int(t), o, h, lo, c, 0.0) for t, o, h, lo, c in zip(f.q_ts, f.q_open, f.q_high, f.q_low, f.q_close)] == want
    assert len(want) == (400 - 11) // 16 and f.q_ts[0] == RANDOM_T0 + H4  # 11 bars to 04:00 UTC
    assert np.array_equal(f.ts[f.q_end], f.q_ts + 3 * H1)


def test_legs_are_b_s_legs_one_timeframe_up_and_the_c_setup_has_its_own(setup):
    b = build_frame(long_setup_df())
    c = build_frame_c(dilate(long_setup_df(), T0))
    for zz in (2.0, 3.0):
        lb, lc = reference_legs(b, zz, StratBParams()), reference_legs_c(c, zz, StratCParams())
        assert [(g.known_j, g.direction, g.start, g.end, g.end_j, g.atr4h, g.prior) for g in lc] == [
            (g.known_i, g.direction, g.start, g.end, g.end_i, g.atr1h, g.prior) for g in lb]
        assert [(g.start_ts, g.end_ts) for g in lc] == [(T0 + 4 * (g.start_ts - T0), T0 + 4 * (g.end_ts - T0)) for g in lb]
    _, f, legs = setup
    assert [g.direction for g in legs] == [1, -1, 1, -1, 1]
    up = legs[2]
    assert (up.start, up.end, up.known_j, up.prior) == (999.5, 1200.5, 139, (959.5, 1100.5))
    assert f.high[up.end_j] == 1200.5 and up.known_j % 4 == 3 and f.ts[up.known_j] % H4 == 3 * H1


def test_the_c_long_setup_gives_one_signal_with_the_spec_levels_in_an_up_trend(setup):
    p, f, legs = setup
    (s,) = generate_signals_c(f, p, legs)
    assert (s.i, s.ts, s.direction, s.entry_ref, s.level) == (SIGNAL_15M, T0 + SIGNAL_15M * M15, 1, 1130.0, 1125.5)
    assert (s.p1_ts, s.p2_ts) == (int(f.ts[160]), int(f.ts[172])) and f.low[172] == 1104.5 and f.rsi[172] > f.rsi[160]
    assert s.stop == pytest.approx(1104.5 - 0.2 * f.atr1h[SIGNAL_HOUR])
    assert s.tp1 == pytest.approx(1130.0 + (1130.0 - s.stop)) and s.tp2 == pytest.approx(999.5 + 1.272 * 201.0)
    assert 1130.0 - s.stop <= 2.5 * f.atr4h[SIGNAL_HOUR] and s.reason.startswith("long 1h div")
    assert trend_at(f, s) == 1 and split_by_trend(f, [s]) == ([s], [])
    assert [x.i for x in generate_signals_c(build_frame_c(long_setup_c_df()), StratCParams(zz_k=3.0))] == [SIGNAL_15M]


def test_the_mirrored_path_gives_the_mirrored_short_in_a_down_trend(setup):
    p, _, _ = setup
    f = build_frame_c(mirror(long_setup_c_df()), p)
    (s,) = generate_signals_c(f, p)
    assert (s.i, s.direction, s.entry_ref, s.level) == (SIGNAL_15M, -1, 2400 - 1130.0, 2400 - 1125.5)
    assert s.stop == pytest.approx(2400 - (1104.5 - 0.2 * f.atr1h[SIGNAL_HOUR]))
    assert s.tp2 == pytest.approx(2400 - (999.5 + 1.272 * 201.0)) and trend_at(f, s) == -1


@pytest.mark.parametrize("seed", [0, 3, 4])
def test_on_a_dilated_path_c_finds_exactly_b_s_signals(seed):
    """Strategy C is strategy B one timeframe up: on B's random path stretched four times, C's signals before the
    trend filter are B's, at the same prices."""
    df = random_df(6000, seed)
    for pb, pc in ((StratBParams(zz_k=2.0), StratCParams(zz_k=2.0)),
                   (StratBParams(zz_k=3.0, zone=(0.5, 0.786), tp1_r=1.5, ext=1.618),
                    StratCParams(zz_k=3.0, zone=(0.5, 0.786), tp1_r=1.5, ext=1.618))):
        want = [_b_key(s, RANDOM_T0) for s in generate_signals(build_frame(df, pb), pb)]
        got = [_key(s) for s in generate_signals_c(build_frame_c(dilate(df, RANDOM_T0), pc), pc)]
        assert want and got == want, seed


def test_no_confluence_no_signal(setup):
    p, f, legs = setup
    assert generate_signals_c(f, p, [dataclasses.replace(g, prior=(1300.0,)) for g in legs]) == []


def test_a_1h_close_below_l_invalidates_and_an_undercut_kills_the_pair(setup):
    p, f, legs = setup
    close = f.close.copy()
    close[170] = 999.0  # below L = 999.5, between the leg's confirmation (139) and the signal
    assert generate_signals_c(dataclasses.replace(f, close=close), p, legs) == []
    close[170] = 1000.0
    assert len(generate_signals_c(dataclasses.replace(f, close=close), p, legs)) == 1
    low = f.low.copy()
    low[175] = 1104.0  # below the second pivot low 1104.5 after it formed
    assert generate_signals_c(dataclasses.replace(f, low=low), p, legs) == []


def test_filters_and_expiry(setup):
    p, f, legs = setup
    assert generate_signals_c(f, dataclasses.replace(p, max_stop_atr4h=0.5), legs) == []  # stop too far
    assert generate_signals_c(f, dataclasses.replace(p, tp1_r=5.0), legs) == []  # TP2 not beyond TP1
    assert generate_signals_c(f, dataclasses.replace(p, confirm_hours=10), legs) == []  # 288 h -> 10 h: expired


def test_the_confirmation_window_includes_its_last_hour(setup):
    p, f, legs = setup
    q = dataclasses.replace(p, confirm_hours=10)
    up = next(k for k, g in enumerate(legs) if g.known_j == 139)

    def leg_known_at(j):
        return [dataclasses.replace(g, known_j=j) if k == up else g for k, g in enumerate(legs)]

    assert [s.i for s in generate_signals_c(f, q, leg_known_at(SIGNAL_HOUR - 10))] == [SIGNAL_15M]
    assert generate_signals_c(f, q, leg_known_at(SIGNAL_HOUR - 11)) == []


def test_a_gap_filled_15m_bar_blocks_the_confirmation():
    df = long_setup_c_df().drop(index=[4 * 174 + 1]).reset_index(drop=True)
    p = StratCParams()
    f = build_frame_c(df, p)
    assert f.synthetic[174] and f.synthetic.sum() == 1 and generate_signals_c(f, p) == []


def test_trend_of_needs_two_higher_or_two_lower_swings_of_each_kind():
    assert TREND_ZZ_K == 2.0
    assert trend_of([100.0, 110.0], [90.0, 95.0]) == 1  # HH and HL: UP
    assert trend_of([110.0, 100.0], [95.0, 90.0]) == -1  # LH and LL: DOWN
    assert trend_of([100.0, 110.0], [95.0, 90.0]) == trend_of([110.0, 100.0], [90.0, 95.0]) == 0  # mixed
    assert trend_of([100.0, 100.0], [90.0, 95.0]) == trend_of([110.0, 100.0], [90.0, 90.0]) == 0  # equal: neither
    assert trend_of([100.0], [90.0, 95.0]) == trend_of([100.0, 110.0], [90.0]) == trend_of([], []) == 0
    assert trend_of([500.0, 100.0, 110.0], [10.0, 90.0, 95.0]) == 1  # only the last two of each kind count


def test_rising_stairs_are_up_and_their_mirror_is_down():
    up = _moves([(64, 400.0), (48, -300.0)] * 10)  # each swing high and low 100 above the one before
    f = build_frame_c(up)
    want, pivots, _ = _oracle_trend(f)
    highs, lows = [q.price for q in pivots if q.kind > 0], [q.price for q in pivots if q.kind < 0]
    assert len(highs) >= 3 and len(lows) >= 3 and highs == sorted(highs) and lows == sorted(lows)
    assert np.array_equal(f.trend, want) and f.trend[-1] == 1 and f.trend[0] == 0 and set(f.trend) == {0, 1}
    assert np.array_equal(build_frame_c(mirror(up, ref=10_000.0)).trend, -f.trend)


@pytest.mark.parametrize("moves", [
    [(48, 300.0), (64, -400.0), (80, 500.0), (96, -600.0), (112, 700.0), (128, -800.0), (144, 900.0), (160, -1000.0)],
    [(160, 1000.0), (144, -900.0), (128, 800.0), (112, -700.0), (96, 600.0), (80, -500.0), (64, 400.0), (48, -300.0)],
], ids=["higher_highs_lower_lows", "lower_highs_higher_lows"])
def test_mixed_swings_are_none(moves):
    f = build_frame_c(_moves(moves))
    want, pivots, _ = _oracle_trend(f)
    assert sum(q.kind > 0 for q in pivots) >= 2 and sum(q.kind < 0 for q in pivots) >= 2
    assert np.array_equal(f.trend, want) and not f.trend.any()


def test_b_s_long_setup_has_no_4h_trend_so_its_signal_is_dropped():
    """B's LONG_SETUP (first low 1039.5, then 999.5) stretched: the 4h lows fall while the highs rise - NONE."""
    f = build_frame_c(dilate(long_setup_df(), T0))
    want, _, _ = _oracle_trend(f)
    (s,) = generate_signals_c(f, StratCParams())
    assert np.array_equal(f.trend, want) and trend_at(f, s) == 0 and split_by_trend(f, [s]) == ([], [s])


def test_the_trend_of_a_random_path_changes_only_at_4h_closes():
    df = random_df(3000, 9, t0=RANDOM_T0 + 5 * M15).drop(index=[700]).reset_index(drop=True)
    f = build_frame_c(df)
    want, _, _ = _oracle_trend(f)
    assert np.array_equal(f.trend, want) and set(f.trend.tolist()) == {-1, 0, 1}
    changes = np.flatnonzero(np.diff(f.trend)) + 1
    assert changes.size and (f.ts[changes] % H4 == 3 * H1).all()


@pytest.mark.parametrize("seed", [7, 13])
def test_signals_trend_and_filter_up_to_t_never_change_when_later_bars_are_cut(seed):
    df = dilate(random_df(4000, seed), RANDOM_T0)
    kinds: set[bool] = set()
    for p in (StratCParams(zz_k=2.0), StratCParams(zz_k=3.0, zone=(0.5, 0.786))):
        f = build_frame_c(df, p)
        full = generate_signals_c(f, p)
        kept, _ = split_by_trend(f, full)
        kinds |= {s in kept for s in full}
        changes = np.flatnonzero(np.diff(f.trend)) + 1
        cuts = sorted({int(f.end15[c]) + d for c in changes for d in (-1, 0)} | {s.i + d for s in full for d in (-4, -1, 0)}
                      | {4000, 9999})
        for t in cuts:
            g = build_frame_c(df.iloc[: t + 1], p)
            n = len(g)
            assert np.array_equal(g.trend, f.trend[:n]) and np.array_equal(g.atr4h, f.atr4h[:n], equal_nan=True)
            got = generate_signals_c(g, p)
            assert [_key(s) for s in got] == [_key(s) for s in full if s.i <= t], (seed, t)
            assert [s in split_by_trend(g, got)[0] for s in got] == [s in kept for s in full if s.i <= t]
    assert kinds == {True, False}, "the seed must have kept and dropped signals"


def test_two_assets_get_independent_trends_and_end_on_the_same_bar():
    btc, eth = long_setup_c_df(), mirror(long_setup_c_df()).iloc[:-12]  # ETH's last 12 bars are missing
    frames = build_frames_c({"BTC": btc, "ETH": eth})
    f, g = frames["BTC"], frames["ETH"]
    assert list(frames) == ["BTC", "ETH"] and len(f.f15) == len(g.f15) == len(btc) and len(f) == len(g)
    assert g.f15.synthetic[-12:].all() and g.synthetic[-3:].all() and not g.synthetic[:-3].any()
    assert np.array_equal(f.trend, build_frame_c(btc).trend) and np.array_equal(g.trend[:-3], build_frame_c(eth).trend)
    assert [s.direction for s in generate_signals_c(f, StratCParams())] == [1]
    assert [s.direction for s in generate_signals_c(g, StratCParams())] == [-1]
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratc/test_signals.py -q
```

예상: `No module named 'ewbot.stratc.signals'`
예상: `1 error`

- [ ] **Step 3: 최소 구현**

**파일 `src/ewbot/stratc/signals.py`** (새 파일, 전체 내용)

```python
"""Strategy-C signals (Plan 5b spec 4): strategy B2's rules one timeframe up, one frame per asset (the same rules
and params for BTC and ETH). Decisions at a 1h bar's CLOSE from bars up to it only; the output for bars <= t never
depends on later bars (tested by truncation). B's pure rule functions (stratb.rules) are used unchanged.

0. Bars: strategy B's frame of the gap-filled 15m bars (stratb.signals.build_frames: every asset ends on the same
   bar) and its complete UTC 1h bars (4 x 15m). A 1h bar is synthetic when any of its 15m bars is. Complete UTC 4h
   bars are 4 consecutive 1h bars from a 00/04/08/12/16/20 boundary - B2's 16 x 15m bars; gap-filled bars count.
1. Trend (spec 4.1, B2's rule): every 4h bar goes into A's ATR ZigZag with k = TREND_ZZ_K (2.0, ATR 14, no
   sub-bars). After each 4h close the last two confirmed swing highs and the last two confirmed swing lows give the
   trend (trend_of): both higher -> UP (+1), longs only; both lower -> DOWN (-1), shorts only; otherwise, or fewer
   than two of either, NONE (0): no entries. A 1h bar has the trend of the latest 4h bar closed at its close.
2. Structure (spec 4.2): the 4h bars also feed A's ZigZag with k = zz_k (the grid value). A pivot confirmed at the
   close of 4h bar q makes the leg (previous pivot -> new pivot) a reference from the 1h bar closing with q on: a
   confirmed high ends an up leg L -> H, the reference of long setups; a confirmed low ends a down leg H -> L, the
   reference of short setups. Each side keeps its latest leg until the next leg of the same direction.
3. Candidate (spec 4.2): retracement zone [H - z_hi (H - L), H - z_lo (H - L)] (mirrored for shorts) and a 4h pivot
   of the same ZigZag before the leg's start, at most confluence_lookback_hours (120 days) earlier, inside the zone
   +/- confluence_atr x ATR4h (the ZigZag's ATR at the leg's confirmation). No overlap -> the leg is never traded.
4. Life: from the 1h bar at whose close the leg is known for confirm_hours 1h bars (288 = 12 days), until the next
   leg of the same direction, or until a 1h close beyond the start (below L for a long) invalidates it.
5. Confirmation (spec 4.3): two consecutive 1h pivots of the trade's kind (lows for a long, 3 bars each side) after
   the 1h bar holding the leg's extreme make a lower low with a higher RSI(14) low (1h closes), the second inside the
   zone; the latest such pair stays the setup until a newer pair replaces it or a later 1h bar undercuts its second
   pivot. Confirmed at a 1h close beyond the swing between the two pivots (highest 1h high for a long) with no
   synthetic 1h bar from p1 - 3 to the confirming bar, whose levels pass the filters of item 6.
6. Levels (spec 4.4): entry_ref = the confirming 1h close; stop = second pivot -/+ 0.2 x ATR1h; TP1 = entry +
   tp1_r x R; TP2 = start + e (end - start). A close beyond the level is skipped when R > 2.5 x ATR4h (of the latest
   4h bar closed at that close) or TP2 is not beyond TP1; the pair and the reference stay alive, so a later close
   within the reference's life may confirm (B's rule). A signal consumes its reference: one signal per leg; on the
   (rare) 1h bar where a long and a short both confirm, the newer leg's signal.
7. A signal is a stratb SignalB at the 15m bar that closes its 1h bar (i = end15[j], ts = that 15m bar's open time),
   so B's engine fills its entry at the next 15m open, which is the next 1h open (spec 4.3); p1_ts / p2_ts are the
   pivots' 1h bar times. generate_signals returns items 2-6; split_by_trend applies item 1: a signal whose direction
   is not its 1h bar's trend is dropped by the engine (engine.run_engine_c, SIGNAL_FILTERED:trend) after it consumed
   its reference. Open positions never look at the trend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from ewbot.indicators import atr_series
from ewbot.stratb.config import StratBParams
from ewbot.stratb.rules import (
    breakout_level,
    extension_target,
    first_target,
    has_confluence,
    is_divergence,
    pivot_highs,
    pivot_lows,
    retracement_zone,
    rsi_series,
    skip_reason,
    stop_level,
)
from ewbot.stratb.signals import Frame15, SignalB, build_frame, build_frames
from ewbot.stratc.config import TREND_ZZ_K, StratCParams
from ewbot.types import H1, H4, Bar
from ewbot.waves.zigzag import ZigZag


@dataclass(frozen=True)
class FrameC:
    """One asset: B's 15m frame (the engine, buy & hold and the random control run on it), its complete 1h bars with
    their indicators, the complete 4h bars built from those, and the 4h trend and ATR known at each 1h close."""

    f15: Frame15
    ts: np.ndarray  # 1h bars: open time
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    synthetic: np.ndarray  # any of the hour's 15m bars gap-filled
    end15: np.ndarray  # index into f15 of the hour's last 15m bar (its close is the hour's close)
    rsi: np.ndarray
    atr1h: np.ndarray
    piv_lo: np.ndarray
    piv_hi: np.ndarray
    q_ts: np.ndarray  # 4h bars: open time
    q_open: np.ndarray
    q_high: np.ndarray
    q_low: np.ndarray
    q_close: np.ndarray
    q_end: np.ndarray  # 1h index of each 4h bar's last hour
    q_atr: np.ndarray
    atr4h: np.ndarray  # per 1h bar: ATR4h of the latest 4h bar closed at its close, NaN before the first
    trend: np.ndarray  # per 1h bar: the 4h trend known at its close, +1 UP, -1 DOWN, 0 NONE

    def __len__(self) -> int:
        return int(self.ts.size)


@dataclass(frozen=True, slots=True)
class LegC:
    known_j: int  # 1h bar at whose close the leg is known (the last hour of the confirming 4h bar)
    direction: int  # +1: up leg, long setups; -1: down leg, short setups
    start: float  # L of an up leg, H of a down leg
    end: float  # H of an up leg, L of a down leg
    start_ts: int  # 4h bar of the start pivot
    end_ts: int  # 4h bar of the end pivot
    end_j: int  # 1h bar of the end pivot's extreme (the first one reaching it)
    atr4h: float  # the ZigZag's ATR4h at confirmation
    prior: tuple[float, ...]  # 4h pivot prices before the start pivot, within the lookback


def trend_of(highs: Sequence[float], lows: Sequence[float]) -> int:
    """B2's trend from the confirmed swing highs and swing lows, oldest first: +1 (UP) when the last two highs and
    the last two lows both rise (HH and HL), -1 (DOWN) when both fall (LH and LL), else 0 (NONE) - also with fewer
    than two of either. Strict: an equal high or low is neither higher nor lower."""
    if len(highs) < 2 or len(lows) < 2:
        return 0
    dh, dl = highs[-1] - highs[-2], lows[-1] - lows[-2]
    if dh > 0.0 and dl > 0.0:
        return 1
    if dh < 0.0 and dl < 0.0:
        return -1
    return 0


def _bars(ts: np.ndarray, o: np.ndarray, h: np.ndarray, lo: np.ndarray, c: np.ndarray) -> list[Bar]:
    return [Bar(int(t), float(a), float(b), float(x), float(y), 0.0) for t, a, b, x, y in zip(ts, o, h, lo, c)]


def _trend_states(bars: Sequence[Bar], atr_len: int) -> np.ndarray:
    """trend_of after each 4h close (item 1)."""
    zz = ZigZag(TREND_ZZ_K, atr_len)
    swings: dict[int, list[float]] = {1: [], -1: []}
    out = np.zeros(len(bars), dtype=np.int64)
    for q, bar in enumerate(bars):
        seen = len(zz.pivots)
        zz.update(q, bar)
        for piv in zz.pivots[seen:]:
            swings[piv.kind].append(piv.price)
        out[q] = trend_of(swings[1], swings[-1])
    return out


def frame_from_15m(f15: Frame15, p: StratCParams = StratCParams()) -> FrameC:
    """The C frame over B's 15m frame f15 (built with the same rsi_len, pivot_w and atr_len)."""
    ends = f15.h_end
    syn = np.zeros(ends.size, dtype=bool)
    for k in range(4):
        syn |= f15.synthetic[ends - k]
    ts, o, h, lo, c = f15.h_ts, f15.h_open, f15.h_high, f15.h_low, f15.h_close
    qe = np.flatnonzero(ts % H4 == H4 - H1)
    qe = qe[(qe >= 3) & (ts[np.maximum(qe - 3, 0)] == ts[qe] - (H4 - H1))]
    qh = np.maximum.reduce([h[qe - k] for k in range(4)]) if qe.size else np.zeros(0)
    ql = np.minimum.reduce([lo[qe - k] for k in range(4)]) if qe.size else np.zeros(0)
    qts, qo, qc = ts[qe - 3], o[qe - 3], c[qe]
    q_atr = atr_series(qh, ql, qc, p.atr_len)
    last_q = np.searchsorted(qe, np.arange(ts.size), side="right") - 1
    known = last_q >= 0
    atr4h = np.full(ts.size, np.nan)
    atr4h[known] = q_atr[last_q[known]]
    trend = np.zeros(ts.size, dtype=np.int64)
    trend[known] = _trend_states(_bars(qts, qo, qh, ql, qc), p.atr_len)[last_q[known]]
    return FrameC(
        f15=f15, ts=ts, open=o, high=h, low=lo, close=c, synthetic=syn, end15=ends,
        rsi=rsi_series(c, p.rsi_len), atr1h=f15.h_atr, piv_lo=pivot_lows(lo, p.pivot_w),
        piv_hi=pivot_highs(h, p.pivot_w), q_ts=qts, q_open=qo, q_high=qh, q_low=ql, q_close=qc, q_end=qe,
        q_atr=q_atr, atr4h=atr4h, trend=trend,
    )


def _b_params(p: StratCParams) -> StratBParams:
    """B's frame params with C's indicator lengths (B's 15m RSI and pivots are computed but unused by C)."""
    return StratBParams(rsi_len=p.rsi_len, pivot_w=p.pivot_w, atr_len=p.atr_len)


def build_frame_c(df15: pd.DataFrame, p: StratCParams = StratCParams()) -> FrameC:
    """One asset's frame from its 15m bars (gap-filled by B's build_frame)."""
    return frame_from_15m(build_frame(df15, _b_params(p)), p)


def build_frames_c(dfs: Mapping[str, pd.DataFrame], p: StratCParams = StratCParams()) -> dict[str, FrameC]:
    """One frame per asset ({asset: 15m bars}), all ending at the latest bar of any asset (B's build_frames pads an
    asset whose bars end earlier with flat synthetic bars)."""
    return {a: frame_from_15m(f, p) for a, f in build_frames(dfs, _b_params(p)).items()}


def reference_legs_c(f: FrameC, zz_k: float, p: StratCParams = StratCParams()) -> list[LegC]:
    """Every leg the 4h ZigZag (k = zz_k) confirms, in order (item 2)."""
    zz = ZigZag(zz_k, p.atr_len)
    legs: list[LegC] = []
    lookback = p.confluence_lookback_hours * H1
    for q, bar in enumerate(_bars(f.q_ts, f.q_open, f.q_high, f.q_low, f.q_close)):
        piv = zz.update(q, bar)
        if piv is None or len(zz.pivots) < 2:
            continue
        prev = zz.pivots[-2]
        d = piv.kind
        first = int(f.q_end[piv.i]) - 3
        seg = f.high[first : first + 4] if d > 0 else f.low[first : first + 4]
        k = int(np.argmax(seg)) if d > 0 else int(np.argmin(seg))
        prior = tuple(x.price for x in zz.pivots[:-2] if x.sub_ts >= prev.sub_ts - lookback)
        legs.append(LegC(int(f.q_end[q]), d, prev.price, piv.price, prev.sub_ts, piv.sub_ts, first + k,
                         float(zz.atr), prior))
    return legs


def _scan(f: FrameC, p: StratCParams, leg: LegC, last_j: int) -> SignalB | None:
    d, w = leg.direction, p.pivot_w
    zone = retracement_zone(leg.start, leg.end, *p.zone)
    if not has_confluence(zone, leg.prior, p.confluence_atr * leg.atr4h):
        return None
    piv = f.piv_lo if d > 0 else f.piv_hi
    ext = f.low if d > 0 else f.high
    tp2 = extension_target(leg.start, leg.end, p.ext)
    prev: int | None = None  # latest 1h pivot of the trade's kind after the leg's extreme
    pair: tuple[int, int] | None = None  # latest consecutive divergence pair, second pivot in the zone

    def add(j: int) -> None:
        nonlocal prev, pair
        in_zone = zone[0] <= ext[j] <= zone[1]
        if prev is not None and in_zone and is_divergence(d, ext[prev], ext[j], f.rsi[prev], f.rsi[j]):
            pair = (prev, j)
        prev = j

    for j in range(leg.end_j + 1, leg.known_j - w):
        if piv[j]:
            add(j)
    for i in range(leg.known_j, last_j + 1):
        if d * (f.close[i] - leg.start) < 0.0:
            return None  # invalidated: a 1h close beyond the leg's start
        j = i - w
        if j > leg.end_j and piv[j]:
            add(j)
        if pair is None:
            continue
        p1, p2 = pair
        x2 = float(ext[p2])
        after = ext[p2 + 1 : i + 1]
        if after.size and d * (float(after.min() if d > 0 else after.max()) - x2) < 0.0:
            pair = None  # the second pivot was undercut: wait for a new pair
            continue
        level = breakout_level(d, f.high, f.low, p1, p2)
        if d * (f.close[i] - level) <= 0.0 or f.synthetic[max(p1 - w, 0) : i + 1].any():
            continue
        atr1h, atr4h = float(f.atr1h[i]), float(f.atr4h[i])
        if not (np.isfinite(atr1h) and np.isfinite(atr4h)):
            continue
        entry = float(f.close[i])
        stop = stop_level(d, x2, atr1h, p.stop_buf_atr)
        tp1 = first_target(entry, stop, p.tp1_r)
        if skip_reason(d, entry, stop, tp1, tp2, atr4h, p.max_stop_atr4h) is not None:
            continue
        k = int(f.end15[i])
        p1_ts, p2_ts = int(f.ts[p1]), int(f.ts[p2])
        return SignalB(i=k, ts=int(f.f15.ts[k]), direction=d, entry_ref=entry, stop=stop, tp1=tp1, tp2=tp2,
                       leg_start=leg.start, leg_end=leg.end, p1_ts=p1_ts, p2_ts=p2_ts, level=level,
                       reason=f"{'long' if d > 0 else 'short'} 1h div {p1_ts}/{p2_ts} break {level:.2f}")
    return None


def generate_signals_c(f: FrameC, p: StratCParams, legs: list[LegC] | None = None) -> list[SignalB]:
    """Every confirmation of items 2-6 over the frame, in bar order, BEFORE the trend filter (split_by_trend).
    Longs use the latest up leg and shorts the latest down leg, each until the next leg of the same direction; at
    most one signal per leg, and on a 1h bar where a long and a short both confirm, the newer leg's signal."""
    legs = reference_legs_c(f, p.zz_k, p) if legs is None else legs
    n = len(f)
    found: list[tuple[int, int, SignalB]] = []
    for d in (1, -1):
        mine = [leg for leg in legs if leg.direction == d]
        for k, leg in enumerate(mine):
            nxt = mine[k + 1].known_j if k + 1 < len(mine) else n
            sig = _scan(f, p, leg, min(nxt - 1, leg.known_j + p.confirm_hours, n - 1))
            if sig is not None:
                found.append((sig.i, -leg.known_j, sig))
    out: list[SignalB] = []
    for i, _, sig in sorted(found, key=lambda x: (x[0], x[1])):
        if not out or out[-1].i != i:
            out.append(sig)
    return out


def trend_at(f: FrameC, sig: SignalB) -> int:
    """The 4h trend of the 1h bar a signal closes (its 15m index is that hour's last 15m bar)."""
    j = int(np.searchsorted(f.end15, sig.i))
    if j >= len(f) or int(f.end15[j]) != sig.i:
        raise ValueError(f"signal bar {sig.i} is not the last 15m bar of a 1h bar")
    return int(f.trend[j])


def split_by_trend(f: FrameC, sigs: Sequence[SignalB]) -> tuple[list[SignalB], list[SignalB]]:
    """(the signals in the direction of their bar's 4h trend, the others) - item 1; each list in the input order."""
    kept: list[SignalB] = []
    dropped: list[SignalB] = []
    for s in sigs:
        (kept if trend_at(f, s) == s.direction else dropped).append(s)
    return kept, dropped
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratc/test_signals.py -q
```

예상: `22 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratc/signals.py tests/stratc/helpers.py tests/stratc/test_signals.py
git commit -m "feat(stratc): B2's rules one timeframe up - 4h trend and reference legs, 1h divergence and breakout confirmation" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: 실행 — B의 15분 엔진, 추세 걸러내기, C 표기

**Files:**
- Create: `src/ewbot/stratc/engine.py`
- Test: `tests/stratc/test_engine.py`

**Interfaces:**
- Consumes: Task 1의 `CConfig`, `StratCParams`, `engine_config`; Task 3의 `FrameC`, `split_by_trend`, `trend_at`, `build_frame_c`, `build_frames_c`, `generate_signals_c`. `ewbot.stratb.engine`: `run_engine(frames: Mapping[str, Frame15], signals: Mapping[str, Iterable[SignalB]], cfg: BConfig, start_ts=None) -> BResult`, `BResult(trades, equity, signals, orders, fills, stops, end_fills, events)`, `asset_of(trade) -> str`. `ewbot.engine.loop.Trade`(frozen dataclass).
- Produces: `ewbot.stratc.engine`: `SETUP = "C"`, `setup_of(asset) -> str`(`"C-BTC"`, `"C-ETH"`), `run_engine_c(frames: Mapping[str, FrameC], signals: Mapping[str, Sequence[SignalB]], cfg: CConfig, start_ts: int | None = None) -> BResult`.

해석 3·4대로 B의 엔진을 바꾸지 않고 쓴다. 테스트는 B 엔진 결과와의 일치(표기만 다름), 다음 1시간봉 시가 진입과 192시간 시간 손절(B의 48시간과 대비), 잔량 규칙 전달, 역추세·NONE 신호의 걸러냄과 기록(포지션 중에도; 열린 포지션은 이후 추세를 보지 않음), 시작 시각과 마지막 봉의 기록 규칙, 시간 경계가 아닌 신호의 거부, 두 자산(ETH가 20시간 앞서 같은 경로)의 자른 데이터 판단 일치(체결·주문·손절 이동·이벤트), B 엔진이 시간 손절과 잔량 규칙 밖의 파라미터를 읽지 않음을 확인한다.

- [ ] **Step 1: 실패하는 테스트 작성**

**파일 `tests/stratc/test_engine.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd
import pytest

from ewbot.stratb.config import BConfig, StratBParams
from ewbot.stratb.engine import asset_of, run_engine
from ewbot.stratb.signals import SignalB
from ewbot.stratc.config import CConfig, StratCParams, engine_config
from ewbot.stratc.engine import run_engine_c, setup_of
from ewbot.stratc.signals import build_frame_c, build_frames_c, generate_signals_c, split_by_trend, trend_at
from tests.stratc.helpers import RANDOM_T0, REP, SIGNAL_15M, T0, dilate, random_df, world_dfs_c

M15, H1 = 900, 3600


def flat(hours, px=20_000.0, trend=1):
    """A flat 15m path of `hours` hours whose 4h trend is forced to `trend` (flat bars have none of their own)."""
    df = pd.DataFrame([(T0 + k * M15, px, px + 5, px - 5, px, 1.0) for k in range(4 * hours)],
                      columns=["ts", "open", "high", "low", "close", "volume"])
    f = build_frame_c(df)
    return dataclasses.replace(f, trend=np.full(len(f), trend, dtype=np.int64))


def sig(f, hour, stop, tp1, tp2, d=1):
    """A signal at the close of 1h bar `hour` (its last 15m bar)."""
    i = int(f.end15[hour])
    return SignalB(i=i, ts=int(f.f15.ts[i]), direction=d, entry_ref=float(f.close[hour]), stop=stop, tp1=tp1, tp2=tp2,
                   reason="test")


def test_it_is_b_s_engine_on_the_trend_kept_signals_with_c_s_labels():
    frames = build_frames_c(world_dfs_c(T0, 3))
    p = StratCParams()
    sigs = {a: generate_signals_c(f, p) for a, f in frames.items()}
    res = run_engine_c(frames, sigs, CConfig())
    b = run_engine({a: f.f15 for a, f in frames.items()}, sigs, engine_config(CConfig()))
    assert all(split_by_trend(frames[a], sigs[a]) == (sigs[a], []) for a in frames)  # every signal in trend
    assert [(o.ts, o.asset, o.direction) for o in res.orders] == [
        (int(frames["BTC"].f15.ts[r * 4 * REP + SIGNAL_15M]), a, d) for r in range(3) for a, d in (("BTC", 1), ("ETH", -1))]
    assert (res.fills, res.orders, res.stops, res.equity, res.events) == (b.fills, b.orders, b.stops, b.equity, b.events)
    assert [t.setup for t in res.trades] == [setup_of(asset_of(t)) for t in b.trades] and {t.setup for t in res.trades} == {
        "C-BTC", "C-ETH"}
    assert [dataclasses.replace(t, setup=x.setup) for t, x in zip(res.trades, b.trades)] == list(b.trades)
    assert res.trades[0].entry_ts == int(frames["BTC"].ts[177 + 1])  # the next 1h open after the 1h signal bar


def test_entries_fill_at_the_next_1h_open_and_the_time_stop_is_192_hours():
    f = flat(210)
    res = run_engine_c({"BTC": f}, {"BTC": [sig(f, 2, 19_900.0, 20_100.0, 20_300.0)]}, CConfig())
    (t,) = res.trades
    assert res.orders[0].contracts == 41 and t.entry_ts == int(f.ts[3]) and t.setup == "C-BTC"
    assert t.exit_reason == "TIME" and t.exit_ts == t.entry_ts + 192 * H1
    b = run_engine({"BTC": f.f15}, {"BTC": [sig(f, 2, 19_900.0, 20_100.0, 20_300.0)]}, BConfig())
    assert b.trades[0].exit_ts == b.trades[0].entry_ts + 48 * H1  # B's own 48 h, for contrast


def test_the_remainder_rule_reaches_b_s_engine():
    rows = [(20_000.0, 20_005.0, 19_995.0, 20_000.0)] * 16 + [(20_000.0, 20_150.0, 19_950.0, 20_120.0),
                                                            (20_120.0, 20_120.0, 19_850.0, 19_860.0)]
    rows += [(19_860.0, 19_865.0, 19_855.0, 19_860.0)] * 6
    df = pd.DataFrame([(T0 + k * M15, *r, 1.0) for k, r in enumerate(rows)],
                      columns=["ts", "open", "high", "low", "close", "volume"])
    f = build_frame_c(df)
    f = dataclasses.replace(f, trend=np.ones(len(f), dtype=np.int64))
    s = [sig(f, 2, 19_900.0, 20_100.0, 20_300.0)]
    be = run_engine_c({"BTC": f}, {"BTC": s}, CConfig())
    orig = run_engine_c({"BTC": f}, {"BTC": s}, CConfig(params=StratCParams(remainder="original")))
    assert [x.kind for x in be.fills] == ["ENTRY", "TP1", "STOP"] and len(be.stops) == 1
    assert orig.stops == () and [x.kind for x in orig.fills] == ["ENTRY", "TP1", "STOP"]
    assert be.fills[2].price > orig.fills[2].price


def test_a_counter_trend_signal_is_dropped_and_recorded_whatever_the_asset_holds():
    f = flat(12)
    up = sig(f, 2, 19_900.0, 20_100.0, 20_300.0)
    short = sig(f, 5, 20_100.0, 19_900.0, 19_700.0, d=-1)  # against the forced UP trend
    res = run_engine_c({"BTC": f}, {"BTC": [up, short]}, CConfig())
    assert [(o.ts, o.direction) for o in res.orders] == [(up.ts, 1)] and len(res.trades) == 1
    assert res.events == ((short.ts, "BTC", "SIGNAL_FILTERED:trend"),)  # recorded while BTC holds the long
    alone = run_engine_c({"BTC": f}, {"BTC": [short]}, CConfig())
    assert alone.orders == () and alone.trades == () and alone.events == res.events
    none = dataclasses.replace(f, trend=np.zeros(len(f), dtype=np.int64))
    assert run_engine_c({"BTC": none}, {"BTC": [up]}, CConfig()).orders == ()  # NONE: no entry at all
    later = dataclasses.replace(f, trend=np.where(np.arange(len(f)) >= 3, -1, 1).astype(np.int64))
    kept = run_engine_c({"BTC": later}, {"BTC": [up, short]}, CConfig())
    assert [o.direction for o in kept.orders] == [1] and kept.trades == res.trades  # the open long ignores the trend
    assert kept.events == ()  # the short is now in trend, but BTC holds the long: dropped by B's engine, unrecorded


def test_filtered_signals_are_recorded_from_the_start_and_never_on_the_last_bar():
    f = flat(12, trend=-1)
    early, last = sig(f, 2, 19_900.0, 20_100.0, 20_300.0), sig(f, 11, 19_900.0, 20_100.0, 20_300.0)
    assert run_engine_c({"BTC": f}, {"BTC": [early, last]}, CConfig()).events == ((early.ts, "BTC",
                                                                                   "SIGNAL_FILTERED:trend"),)
    assert run_engine_c({"BTC": f}, {"BTC": [early]}, CConfig(), start_ts=early.ts + M15).events == ()


def test_a_signal_must_sit_on_an_hour_s_last_15m_bar():
    f = flat(6)
    s = dataclasses.replace(sig(f, 2, 19_900.0, 20_100.0, 20_300.0), i=int(f.end15[2]) - 1)
    with pytest.raises(ValueError, match="not the last 15m bar"):
        trend_at(f, s)


@pytest.mark.parametrize("seed", [7, 18])
def test_decisions_up_to_t_never_change_when_later_bars_are_cut(seed):
    """ETH follows BTC's path 20 hours earlier: positions overlap, ETH starts first and is padded at the end; both
    seeds have trend-filtered signals."""
    dfs = {"BTC": dilate(random_df(4000, seed), RANDOM_T0),
           "ETH": dilate(random_df(4000, seed, t0=RANDOM_T0 - 20 * M15), RANDOM_T0)}
    p = StratCParams(zz_k=3.0, zone=(0.5, 0.786))

    def run_all(d):
        frames = build_frames_c(d, p)
        return run_engine_c(frames, {a: generate_signals_c(f, p) for a, f in frames.items()}, CConfig(params=p))

    full = run_all(dfs)
    btc, eth = ([t for t in full.trades if asset_of(t) == a] for a in ("BTC", "ETH"))
    assert any(b.entry_ts < e.exit_ts and e.entry_ts < b.exit_ts for b in btc for e in eth)
    filtered = [e[0] for e in full.events if e[2] == "SIGNAL_FILTERED:trend"]
    assert filtered
    t0 = int(dfs["BTC"]["ts"].iloc[0])
    for t in sorted({x.ts // M15 * M15 for x in full.fills} | {t0 + 9999 * M15} | {x + M15 for x in filtered}):
        cut = run_all({a: df[df["ts"] <= t] for a, df in dfs.items()})
        assert [e for e in cut.events if e[0] < t] == [e for e in full.events if e[0] < t]
        assert list(cut.fills) == [x for x in full.fills if x.ts <= t]
        assert list(cut.stops) == [x for x in full.stops if x.ts <= t]
        assert list(cut.orders) == [o for o in full.orders if o.ts < t]


def test_b_s_engine_reads_only_the_time_stop_and_the_remainder_of_its_params():
    frames = build_frames_c({"BTC": dilate(random_df(4000, 18), RANDOM_T0)})
    p = StratCParams(zz_k=3.0, zone=(0.5, 0.786))
    sigs = {"BTC": split_by_trend(frames["BTC"], generate_signals_c(frames["BTC"], p))[0]}
    f15 = {"BTC": frames["BTC"].f15}
    e = engine_config(CConfig(params=p))
    other = dataclasses.replace(e, params=StratBParams(zz_k=3.0, zone=(0.5, 0.786), tp1_r=1.5, ext=1.618,
                                                      confirm_hours=1, max_stop_atr1h=0.1, time_stop_hours=192))
    assert run_engine(f15, sigs, e) == run_engine(f15, sigs, other) and sigs["BTC"]
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratc/test_engine.py -q
```

예상: `No module named 'ewbot.stratc.engine'`
예상: `1 error`

- [ ] **Step 3: 최소 구현**

**파일 `src/ewbot/stratc/engine.py`** (새 파일, 전체 내용)

```python
"""Strategy-C execution (Plan 5b spec 4.4, 6.2): strategy B's 15-minute portfolio engine (stratb.engine.run_engine),
imported and run unchanged on C's signals - one account for BTC and ETH, at most one position per asset, entries at
the next 15m open after the signal bar (= the next 1h open), stop before target in the same 15m bar, TP1 half,
remainder rule, time stop, 0.5% sizing with the 3x caps, A's breakers and costs, hourly equity. engine_config(cfg)
carries C's time stop (192 h) and remainder rule to it.

run_engine_c adds what is C's own:
  - the trend filter (signals.split_by_trend): a signal whose direction is not its 1h bar's 4h trend is dropped
    before the engine sees it, whatever the asset holds, and recorded as (signal ts, asset,
    "SIGNAL_FILTERED:trend") when the engine would have evaluated it (ts >= start_ts, not the last bar). These
    events follow the engine's own events of the same bar, assets in config order;
  - the trade label: Trade.setup is "C-BTC" / "C-ETH" (B's engine writes "B-<asset>")."""

from __future__ import annotations

import dataclasses
from typing import Mapping, Sequence

from ewbot.engine.loop import Trade
from ewbot.stratb.engine import BResult, asset_of, run_engine
from ewbot.stratb.signals import SignalB
from ewbot.stratc.config import CConfig, engine_config
from ewbot.stratc.signals import FrameC, split_by_trend

SETUP = "C"


def setup_of(asset: str) -> str:
    """Trade.setup of a strategy-C trade, "C-BTC" or "C-ETH"."""
    return f"{SETUP}-{asset}"


def _relabel(t: Trade) -> Trade:
    return dataclasses.replace(t, setup=setup_of(asset_of(t)))


def run_engine_c(
    frames: Mapping[str, FrameC],
    signals: Mapping[str, Sequence[SignalB]],
    cfg: CConfig,
    start_ts: int | None = None,
) -> BResult:
    """One run over the frames ({asset: FrameC} of assets of cfg, built together by build_frames_c) and their
    signals before the trend filter ({asset: generate_signals_c output}). Entries only from start_ts."""
    kept: dict[str, list[SignalB]] = {}
    filtered: list[tuple[int, str, str]] = []
    for a in [s.asset for s in cfg.assets if s.asset in frames]:
        f = frames[a]
        kept[a], dropped = split_by_trend(f, signals.get(a, ()))
        last = len(f.f15) - 1
        filtered += [(s.ts, a, "SIGNAL_FILTERED:trend") for s in dropped
                     if s.i < last and (start_ts is None or s.ts >= start_ts)]
    res = run_engine({a: f.f15 for a, f in frames.items()}, kept, engine_config(cfg), start_ts)
    events = tuple(sorted(res.events + tuple(filtered), key=lambda e: e[0]))
    return dataclasses.replace(res, trades=tuple(_relabel(t) for t in res.trades), events=events)
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratc/test_engine.py -q
```

예상: `9 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratc/engine.py tests/stratc/test_engine.py
git commit -m "feat(stratc): run strategy B's 15m portfolio engine on C's signals with the 4h trend filter and C labels" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: 백테스트 가드, 그리드, 워크포워드

**Files:**
- Create: `src/ewbot/stratc/research.py`
- Test: `tests/stratc/test_research.py`

**Interfaces:**
- Consumes: Task 1~4. `ewbot.stratb.research`: `evaluate_folds_b(results, fold_list, equity0, min_trades=12) -> dict`(키 `keys`, `folds`, `stitched_equity`, `stitched_metrics`, `oos_trades`, `chosen_final`, `chosen_final_from_fold`, `chosen_final_fallback`, `proceed`), `refuse_holdout_bars(df15, end_ts, include_holdout=False)`(C는 앞의 두 인자만 넘긴다), `trades_by_asset(trades) -> dict[str, int]`, `wf_data_record(dfs_wf) -> dict`. `ewbot.backtest.walkforward`: `HOLDOUT_START`, `WF_START`, `folds()`. `ewbot.backtest.metrics.compute_metrics`, `ewbot.backtest.runner.utc_ts`, `ewbot.stratb.config`: `ASSETS`, `Combo`, `combo_to_json`, `grid_combos`.
- Produces: `ewbot.stratc.research`: `backtest_c(dfs, cfg, start_ts=None, end_ts=None, *, with_holdout=False) -> BResult`(`with_holdout`이 아니면 2024 이후 봉을 볼 실행은 `ValueError`), `wf_provenance_c(dfs_wf) -> dict`(`wf_data`, `code_sha256`, `deps_sha256`), `run_grid_c(dfs, start_ts, end_ts, workers=1) -> dict[Combo, BResult]`(손실 한도 끔, `GRID_B` 순서), `walk_forward_c(dfs, workers=1) -> dict`(`evaluate_folds_b`의 출력 + `combo_table` + `wf_provenance_c`).

해석 6대로 워크포워드는 B가 구현한 A의 규칙을 그대로 쓴다. 그리드는 프레임을 한 번 만들고(프레임은 그리드 값에 의존하지 않는다) 자산·`zz_k`마다 기준 구간을 한 번 계산한다(작업자 프로세스마다). 테스트는 2024 가드, 백테스트가 C 엔진과 같음, 그리드가 32개 조합을 한 번씩(작업자 2개도 같은 결과, 한 조합은 단일 백테스트와 같음), 워크포워드가 2024 이후 봉을 자르고 A의 구간과 B의 규칙을 쓰며 쓴 봉과 코드를 기록하는지 확인한다.

- [ ] **Step 1: 실패하는 테스트 작성**

**파일 `tests/stratc/test_research.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import pandas as pd
import pytest

from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.engine.loop import EquityPoint
from ewbot.stratb.config import grid_combos
from ewbot.stratb.engine import BResult
from ewbot.stratb.research import trades_by_asset, wf_data_record
from ewbot.stratc import research
from ewbot.stratc.config import CConfig, StratCParams, config_for
from ewbot.stratc.engine import run_engine_c
from ewbot.stratc.fingerprint import deps_fingerprint, stratc_fingerprint
from ewbot.stratc.research import backtest_c, run_grid_c, walk_forward_c, wf_provenance_c
from ewbot.stratc.signals import build_frames_c, generate_signals_c
from tests.stratc.helpers import REP, SIGNAL_15M, T0, RANDOM_T0, dilate, random_df, world_dfs_c

H1, M15 = 3600, 900
COMBOS = grid_combos()


def _result(points):
    return BResult((), tuple(EquityPoint(t, e) for t, e in points), (), (), (), (), (), ())


def test_research_runs_refuse_2024_bars():
    early = random_df(400, 2, t0=HOLDOUT_START - 600 * M15)  # ETH ends before 2024, BTC does not
    dfs = {"BTC": random_df(400, 1, t0=HOLDOUT_START - 200 * M15), "ETH": early}
    with pytest.raises(ValueError):
        backtest_c(dfs, CConfig())
    with pytest.raises(ValueError):
        backtest_c({"ETH": early}, CConfig(), end_ts=HOLDOUT_START + M15)
    with pytest.raises(ValueError):
        run_grid_c(dfs, int(early["ts"].iloc[0]), HOLDOUT_START + M15)
    assert backtest_c(dfs, CConfig(), end_ts=HOLDOUT_START).equity[-1].ts < HOLDOUT_START
    assert backtest_c(dfs, CConfig(), with_holdout=True).equity[-1].ts >= HOLDOUT_START


def test_a_backtest_is_the_c_engine_on_each_asset_s_signals():
    dfs = world_dfs_c(T0, 2)
    p = StratCParams(zz_k=2.0)
    res = backtest_c(dfs, CConfig(params=p))
    frames = build_frames_c(dfs, p)
    assert res == run_engine_c(frames, {a: generate_signals_c(f, p) for a, f in frames.items()}, CConfig(params=p))
    bars = [r * 4 * REP + SIGNAL_15M for r in range(2)]
    assert [(s.i, o.asset, o.direction) for s, o in zip(res.signals, res.orders)] == [
        (b, a, d) for b in bars for a, d in (("BTC", 1), ("ETH", -1))]
    assert trades_by_asset(res.trades) == {"BTC": 2, "ETH": 2} and {t.setup for t in res.trades} == {"C-BTC", "C-ETH"}


def test_the_grid_runs_every_combo_once_on_both_assets_with_the_breakers_off():
    dfs = {"BTC": dilate(random_df(3000, 8), RANDOM_T0), "ETH": dilate(random_df(3000, 11), RANDOM_T0)}
    start, end = int(dfs["BTC"]["ts"].iloc[1600]), int(dfs["BTC"]["ts"].iloc[-1]) + M15
    out = run_grid_c(dfs, start, end, workers=1)
    assert list(out) == COMBOS and all(r.equity[0] == EquityPoint(start - H1, 10_000.0) for r in out.values())
    assert {a for r in out.values() for a, n in trades_by_asset(r.trades).items() if n} == {"BTC", "ETH"}
    c = COMBOS[-1]
    assert out[c] == backtest_c(dfs, config_for(c, breakers_enabled=False), start, end)
    assert run_grid_c(dfs, start, end, workers=2) == out


def test_walk_forward_cuts_2024_and_uses_strategy_a_folds_with_b_s_rule(monkeypatch):
    seen = {}

    def fake_grid(dfs, start, end, workers):
        assert sorted(dfs) == ["BTC", "ETH"]
        seen.update(max_ts=max(int(df["ts"].max()) for df in dfs.values()), start=start, end=end)
        return {c: _result([(start, 10_000.0), (end - H1, 10_000.0)]) for c in COMBOS}

    monkeypatch.setattr(research, "run_grid_c", fake_grid)
    df = pd.concat([random_df(50, 1, t0=HOLDOUT_START - 50 * M15), random_df(50, 2, t0=HOLDOUT_START)])
    out = walk_forward_c({"BTC": df, "ETH": df}, workers=1)
    assert seen == {"max_ts": HOLDOUT_START - M15, "start": 1_483_228_800, "end": HOLDOUT_START}
    assert len(out["folds"]) == 10 and out["folds"][0]["test"][0] == 1_546_300_800  # 2019-01-01
    assert out["keys"] == ["zz_k", "zone", "tp1_r", "ext", "remainder"]
    assert out["chosen_final"] is None and out["proceed"] is False and len(out["combo_table"]) == 32
    assert out["combo_table"][0]["n_trades_by_asset"] == {"BTC": 0, "ETH": 0}
    assert out["wf_data"] == wf_data_record({a: df[df["ts"] < HOLDOUT_START] for a in ("BTC", "ETH")})
    assert (out["code_sha256"], out["deps_sha256"]) == (stratc_fingerprint(), deps_fingerprint())
    assert {k: out[k] for k in ("wf_data", "code_sha256", "deps_sha256")} == wf_provenance_c(
        {a: df[df["ts"] < HOLDOUT_START] for a in ("BTC", "ETH")})
    with pytest.raises(ValueError, match="every asset"):
        walk_forward_c({"BTC": df}, workers=1)
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratc/test_research.py -q
```

예상: `cannot import name 'research' from 'ewbot.stratc'`
예상: `1 error`

- [ ] **Step 3: 최소 구현**

**파일 `src/ewbot/stratc/research.py`** (새 파일, 전체 내용)

```python
"""Strategy-C research runs (Plan 5b spec 6.3): one backtest, the 32-combo grid, the walk-forward. Every run is one
portfolio over BTC and ETH ({asset: 15m bars}; one shared combo; engine.run_engine_c).

The walk-forward is strategy A's rule as strategy B implemented it, imported unchanged (stratb.research): A's folds()
(24-month training, 6-month test, rolling by 6 months, tests 2019-01 .. 2023-12, 10 folds, all inside
[2017-01-01, 2024-01-01)); every combo runs ONCE over that span with the circuit breakers disabled and the folds slice
the runs; eligible in a training window: >= 12 trades and a finite Calmar; pick_plateau_b over GRID_B (= GRID_C):
with two values per axis every combo neighbours every other, so the pick is the highest own Calmar among >= 17
eligible combos; stitched out-of-sample curve; chosen_final = the pick of the last fold that picked;
proceed = a final pick from the last fold and a stitched out-of-sample total return >= 0 (evaluate_folds_b). A
walk-forward that does not proceed ends strategy C (spec 6.3, one attempt). The output also records what it ran on
(wf_provenance_c: every asset's bars before 2024 - B's wf_data_record -, the C fingerprint and deps_sha256);
holdout.create_lock_c checks them against the bars and the code at lock time."""

from __future__ import annotations

import multiprocessing as mp
import sys
from typing import Any, Mapping

import pandas as pd

from ewbot.backtest.metrics import compute_metrics
from ewbot.backtest.runner import utc_ts
from ewbot.backtest.walkforward import HOLDOUT_START, WF_START, folds
from ewbot.stratb.config import ASSETS, Combo, combo_to_json, grid_combos
from ewbot.stratb.engine import BResult
from ewbot.stratb.research import evaluate_folds_b, refuse_holdout_bars, trades_by_asset, wf_data_record
from ewbot.stratc.config import CConfig, StratCParams, config_for
from ewbot.stratc.engine import run_engine_c
from ewbot.stratc.fingerprint import deps_fingerprint, stratc_fingerprint
from ewbot.stratc.signals import FrameC, LegC, build_frames_c, generate_signals_c, reference_legs_c


def _cut(dfs: Mapping[str, pd.DataFrame], end_ts: int | None) -> dict[str, pd.DataFrame]:
    return {a: (df if end_ts is None else df[df["ts"] < end_ts]).reset_index(drop=True) for a, df in dfs.items()}


def backtest_c(
    dfs: Mapping[str, pd.DataFrame],
    cfg: CConfig,
    start_ts: int | None = None,
    end_ts: int | None = None,
    *,
    with_holdout: bool = False,
) -> BResult:
    """One strategy-C run over every asset's bars ({asset: 15m bars}) before end_ts; entries from start_ts. Without
    with_holdout (only the C holdout and the paper-C runner pass it) a run that would see a bar at/after
    HOLDOUT_START is refused (ValueError, B's refuse_holdout_bars)."""
    if not with_holdout:
        for df in dfs.values():
            refuse_holdout_bars(df, end_ts)
    frames = build_frames_c(_cut(dfs, end_ts), cfg.params)
    return run_engine_c(frames, {a: generate_signals_c(f, cfg.params) for a, f in frames.items()}, cfg, start_ts)


def wf_provenance_c(dfs_wf: Mapping[str, pd.DataFrame]) -> dict[str, Any]:
    """What a walk-forward runs on: wf_data (every asset's bars), code_sha256 (C fingerprint), deps_sha256."""
    return {"wf_data": wf_data_record(dfs_wf), "code_sha256": stratc_fingerprint(), "deps_sha256": deps_fingerprint()}


# ----------------------------------------------------------------------------- grid

_POOL: dict[str, Any] = {}  # per process: the frames, the start and the legs per (asset, zz_k)


def _pool_init(frames: dict[str, FrameC], start_ts: int) -> None:
    _POOL.clear()
    _POOL.update(frames=frames, start=start_ts, legs={})


def _pool_run(combo: Combo) -> tuple[Combo, BResult]:
    cfg = config_for(combo, breakers_enabled=False)
    frames: dict[str, FrameC] = _POOL["frames"]
    legs: dict[tuple[str, float], list[LegC]] = _POOL["legs"]
    sigs = {}
    for a, f in frames.items():
        key = (a, cfg.params.zz_k)
        if key not in legs:
            legs[key] = reference_legs_c(f, cfg.params.zz_k, cfg.params)
        sigs[a] = generate_signals_c(f, cfg.params, legs[key])
    return combo, run_engine_c(frames, sigs, cfg, _POOL["start"])


def run_grid_c(dfs: Mapping[str, pd.DataFrame], start_ts: int, end_ts: int, workers: int = 1) -> dict[Combo, BResult]:
    """Every grid combo once over every asset's bars before end_ts (refused past HOLDOUT_START), breakers off."""
    for df in dfs.values():
        refuse_holdout_bars(df, end_ts)
    frames = build_frames_c(_cut(dfs, end_ts), StratCParams())
    combos = grid_combos()
    out: dict[Combo, BResult] = {}
    if workers <= 1:
        _pool_init(frames, start_ts)
        for n, c in enumerate(combos, 1):
            out[c] = _pool_run(c)[1]
            print(f"[grid-c] {n}/{len(combos)} {combo_to_json(c)}", file=sys.stderr, flush=True)
    else:
        with mp.Pool(min(workers, len(combos)), initializer=_pool_init, initargs=(frames, start_ts)) as pool:
            for n, (c, res) in enumerate(pool.imap_unordered(_pool_run, combos), 1):
                out[c] = res
                print(f"[grid-c] {n}/{len(combos)} {combo_to_json(c)}", file=sys.stderr, flush=True)
    return {c: out[c] for c in combos}


def walk_forward_c(dfs: Mapping[str, pd.DataFrame], workers: int = 1) -> dict[str, Any]:
    """The full walk-forward over [2017-01-01, 2024-01-01) on the BTC + ETH portfolio (both assets required). Bars
    at/after 2024-01-01 are dropped here. The output carries wf_provenance_c of the bars."""
    if sorted(dfs) != sorted(ASSETS):
        raise ValueError(f"the walk-forward runs on every asset {list(ASSETS)}, got {sorted(dfs)}")
    start, end = utc_ts(WF_START), HOLDOUT_START
    provenance = wf_provenance_c(_cut(dfs, end))
    results = run_grid_c(_cut(dfs, end), start, end, workers)
    out = evaluate_folds_b(results, folds(), CConfig().initial_equity)
    full = {c: compute_metrics(r.equity, r.trades, start, end) for c, r in results.items()}
    out["combo_table"] = [
        {"combo": combo_to_json(c), "calmar": m.calmar, "sharpe": m.sharpe, "n_trades": m.n_trades,
         "n_trades_by_asset": trades_by_asset([t for t in results[c].trades if start <= t.entry_ts < end]),
         "total_return": m.total_return}
        for c, m in full.items()
    ]
    out.update(provenance)
    return out
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratc/test_research.py -q
```

예상: `4 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratc/research.py tests/stratc/test_research.py
git commit -m "feat(stratc): guarded backtest, 32-combo grid and walk-forward with strategy A's rule as B implemented it" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: `holdout-c.lock`, 1회 홀드아웃, 합격 기준

**Files:**
- Create: `src/ewbot/stratc/holdout.py`
- Test: `tests/stratc/test_holdout.py`

**Interfaces:**
- Consumes: Task 1~5. `ewbot.backtest.holdout`: `data_fingerprint(df, start_ts, last_ts) -> str`, `git_state() -> dict`. `ewbot.backtest.report.to_jsonable`. `ewbot.stratb.baselines`: `buy_hold_b(frames, start_ts, end_ts, cfg: BConfig) -> (equity, trades)`, `random_control(frames, cfg: BConfig, b_trades, start_ts, end_ts, runs=1000, seed=0, workers=1) -> dict`. `ewbot.stratb.holdout`: `COST_MULT = 1.5`, `data_fingerprint_b(dfs, start_ts, last_ts) -> str`, `evaluate_criteria_b(b, bh, rnd, cost) -> dict`(`passed` 등). `ewbot.stratb.config`: `ASSETS`, `M15`, `combo_to_json`, `normalize_combo`, `scale_costs`. `tests/paper/helpers.git`.
- Produces: `ewbot.stratc.holdout`: `LOCK_VERSION = 1`, `RANDOM_RUNS = 1000`, `class HoldoutCError(RuntimeError)`, `read_lock_c(path) -> dict`, `committed_lock_c(path) -> dict`, `lock_mismatch_c(lock) -> list[str]`, `create_lock_c(report_path, dfs_wf, *, now_ts, lock_path=DEFAULT_LOCK_PATH, random_runs=RANDOM_RUNS, random_seed=0) -> dict`, `run_holdout_c(*, confirm, cache_dir, lock_path=DEFAULT_LOCK_PATH, workers=1, fingerprint=stratc_fingerprint, deps=deps_fingerprint) -> dict`(`lock_path`, `result`, `first_run`; `first_run.metrics`의 키는 `C`, `BH`, `C_cost_x1_5`).

해석 7대로 B의 잠금·1회 실행 절차를 C의 잠금으로 한다. 합성 세계(2023-07-01부터 반복 56번, 잠금 시각 2024-09-27)에서 홀드아웃 거래는 두 자산 28건씩 56건이고 모두 이기므로 네 기준을 통과한다. 테스트는 잠금 전 2024 읽기 거부, 잠금이 묶는 필드(설정의 C 고정값과 deps의 B 모듈 포함), 잠금 거부(그리드 밖, 2024 데이터, 한 자산, 이른 시각, 진행하지 않는 보고서, 다른 봉·코드·deps로 만든 보고서), 묶음 필드 편집과 B 종류의 잠금, 승인과 커밋, 코드·deps·봉 변경 거부, 이 코드의 것이 아닌 잠금 필드 거부, 더러운 작업 트리, 1회 실행과 기록, 끝나지 않은 시도, 마지막 봉에 못 미치는 캐시를 확인한다.

- [ ] **Step 1: 실패하는 테스트 작성**

**파일 `tests/stratc/test_holdout.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest

from ewbot.stratb.data15 import cache_paths
from ewbot.stratb.holdout import data_fingerprint_b
from ewbot.stratc.data import LOCK_BOUND, REPLAY_ANCHOR, HoldoutGuardError, load_15m_c, load_caches_c, lock_digest_c
from ewbot.stratc.fingerprint import deps_files, deps_fingerprint, stratc_fingerprint
from ewbot.stratc.holdout import HoldoutCError, create_lock_c, read_lock_c, run_holdout_c
from ewbot.stratc.research import wf_provenance_c
from tests.paper.helpers import git
from tests.stratc.helpers import save_world, world_dfs_c

HS = 1_704_067_200
NOW = HS + 270 * 86_400 + 1_234  # the lock's clock: last bar 2024-09-27 00:45 UTC
WORLD_REPS = 56  # 2023-07-01 .. 2024-12-24: a BTC long and an ETH short every 232 hours
COMBO = [2.0, [0.382, 0.618], 1.0, 1.272, "breakeven"]


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    cache = tmp_path / "cache"
    cache.mkdir()
    save_world(cache, world_dfs_c(REPLAY_ANCHOR, WORLD_REPS))
    return repo, cache, repo / "holdout-c.lock"


def _report(world, dfs=None, **edit):
    """A walk-forward report as `ewbot stratc walkforward` writes it: it proceeds with COMBO and was made on the
    cache's bars (or dfs) and this code."""
    repo, cache, _ = world
    rep = {"chosen_final": COMBO, "chosen_final_fallback": False, "proceed": True,
           **wf_provenance_c(load_caches_c(cache) if dfs is None else dfs), **edit}
    path = repo.parent / "wf.json"  # outside the repo, like the gitignored reports/
    path.write_text(json.dumps(rep), encoding="utf-8")
    return path


def _lock(world, **kw):
    _, cache, lock_path = world
    args = dict(now_ts=NOW, lock_path=lock_path, random_runs=4)
    args.update(kw)
    return create_lock_c(_report(world), load_caches_c(cache), **args)


def _commit(world):
    repo, _, lock_path = world
    git(repo, "add", lock_path.name)
    git(repo, "commit", "-q", "-m", "lock")


def _recommit(world, edit):
    """Edit the committed lock (its sha256 recomputed, so only the code checks can catch it) and commit it."""
    lock = json.loads(world[2].read_text())
    edit(lock)
    lock["sha256"] = lock_digest_c(lock)
    world[2].write_text(json.dumps(lock))
    _commit(world)


def test_2024_bars_are_unreadable_until_the_c_lock_exists(world):
    _, cache, lock_path = world
    for p in cache_paths(cache).values():
        with pytest.raises(HoldoutGuardError):
            load_15m_c(p, with_holdout=True, lock_path=lock_path)
    _lock(world)
    for p in cache_paths(cache).values():
        assert int(load_15m_c(p, with_holdout=True, lock_path=lock_path)["ts"].max()) > HS


def test_the_lock_binds_params_code_data_and_window_and_is_written_once(world):
    lock = _lock(world)
    assert lock == read_lock_c(world[2]) and lock["kind"] == "ewbot-stratc-holdout"
    assert lock["params"] == COMBO and lock["sha256"] == lock_digest_c(lock)
    assert lock["last_bar_ts"] == NOW // 3600 * 3600 - 900
    assert (lock["holdout_start"], lock["replay_anchor"]) == (HS, REPLAY_ANCHOR)
    assert lock["code_sha256"] == stratc_fingerprint() and lock["random_runs"] == 4 and lock["first_run"] is None
    assert lock["deps_sha256"] == deps_fingerprint() and {"code_sha256", "deps_sha256", "wf_data"} <= set(LOCK_BOUND)
    deps = set(deps_files())
    assert {f"src/ewbot/{m}" for m in ("config.py", "backtest/metrics.py", "backtest/walkforward.py", "risk/sizing.py",
            "risk/breakers.py", "waves/zigzag.py", "stratb/engine.py", "stratb/baselines.py", "stratb/holdout.py",
            "stratb/rules.py", "stratb/signals.py", "stratb/research.py")} <= deps
    assert [(a, w["last_ts"]) for a, w in lock["wf_data"].items()] == [("BTC", HS - 900), ("ETH", HS - 900)]
    assert lock["config"]["params"]["confirm_hours"] == 288 and lock["config"]["params"]["time_stop_hours"] == 192
    assert lock["config"]["risk"]["risk_pct"] == 0.005 and lock["config"]["max_total_leverage"] == 3.0
    assert [a["future"] for a in lock["config"]["assets"]] == ["BIP-20DEC30-CDE", "ETP-20DEC30-CDE"]
    with pytest.raises(HoldoutCError, match="written once"):
        _lock(world)


def test_the_lock_refuses_off_grid_params_2024_or_one_asset_data_and_an_early_clock(world):
    _, cache, lock_path = world
    wf = load_caches_c(cache)
    with pytest.raises(HoldoutCError, match="not a combo"):
        create_lock_c(_report(world, chosen_final=[2.5, [0.382, 0.618], 1.0, 1.272, "breakeven"]), wf, now_ts=NOW,
                      lock_path=lock_path)
    full = {a: pd.read_parquet(p) for a, p in cache_paths(cache).items()}
    with pytest.raises(HoldoutCError, match="before 2024"):
        create_lock_c(_report(world), full, now_ts=NOW, lock_path=lock_path)
    with pytest.raises(HoldoutCError, match="every asset"):
        create_lock_c(_report(world), {"BTC": wf["BTC"]}, now_ts=NOW, lock_path=lock_path)
    with pytest.raises(HoldoutCError, match="after 2024"):
        create_lock_c(_report(world), wf, now_ts=HS + 600, lock_path=lock_path)
    assert not lock_path.exists()


def test_the_lock_comes_from_a_proceeding_walk_forward_report_of_these_bars_and_this_code(world):
    repo, cache, lock_path = world
    wf = load_caches_c(cache)
    for edit, match in (({"proceed": False}, "proceed is false"), ({"chosen_final_fallback": True}, "proceed is false"),
                        ({"chosen_final": None}, "proceed is false"), ({"code_sha256": "0" * 64}, r"\['code_sha256'\]"),
                        ({"deps_sha256": "0" * 64}, r"\['deps_sha256'\]")):
        with pytest.raises(HoldoutCError, match=match):
            create_lock_c(_report(world, **edit), wf, now_ts=NOW, lock_path=lock_path)
    with pytest.raises(HoldoutCError, match=r"\['wf_data'\]"):  # the walk-forward saw other bars
        create_lock_c(_report(world, {a: df.iloc[1:] for a, df in wf.items()}), wf, now_ts=NOW, lock_path=lock_path)
    with pytest.raises(HoldoutCError, match="unreadable"):
        create_lock_c(repo.parent / "missing.json", wf, now_ts=NOW, lock_path=lock_path)
    assert not lock_path.exists()
    report = _report(world)
    lock = create_lock_c(report, wf, now_ts=NOW, lock_path=lock_path, random_runs=4)
    assert lock["wf_report_sha256"] == hashlib.sha256(report.read_bytes()).hexdigest() and "wf_report_sha256" in LOCK_BOUND
    assert lock["params_source"] == f"walkforward:{report}#chosen_final" and read_lock_c(lock_path) == lock


def test_an_edited_bound_field_or_a_b_lock_is_not_a_c_lock(world):
    _lock(world)
    lock = json.loads(world[2].read_text())
    lock["params"][2] = 1.5
    world[2].write_text(json.dumps(lock))
    with pytest.raises(HoldoutCError, match="digest"):
        read_lock_c(world[2])
    lock["kind"] = "ewbot-stratb-holdout"
    world[2].write_text(json.dumps(lock))
    with pytest.raises(HoldoutCError, match="not a strategy-C holdout lock"):
        read_lock_c(world[2])


def test_the_holdout_needs_confirm_and_the_committed_lock(world):
    _, cache, lock_path = world
    _lock(world)
    with pytest.raises(HoldoutCError, match="single-use"):
        run_holdout_c(confirm=False, cache_dir=cache, lock_path=lock_path)
    with pytest.raises(HoldoutCError, match="not committed"):
        run_holdout_c(confirm=True, cache_dir=cache, lock_path=lock_path)


def test_a_c_code_change_changed_imported_modules_or_other_walk_forward_bars_are_refused(world):
    _, cache, lock_path = world
    _lock(world)
    _commit(world)
    with pytest.raises(HoldoutCError, match="code changed"):
        run_holdout_c(confirm=True, cache_dir=cache, lock_path=lock_path, fingerprint=lambda: "0" * 64)
    with pytest.raises(HoldoutCError, match="modules strategy C imports"):
        run_holdout_c(confirm=True, cache_dir=cache, lock_path=lock_path, deps=lambda: "0" * 64)
    eth = cache_paths(cache)["ETH"]
    df = pd.read_parquet(eth)
    df.loc[5, "close"] += 1.0
    df.to_parquet(eth, index=False)
    with pytest.raises(HoldoutCError, match=r"\['ETH'\] 15m bars before 2024-01-01 differ"):
        run_holdout_c(confirm=True, cache_dir=cache, lock_path=lock_path)
    assert read_lock_c(lock_path)["attempts"] == []


def test_the_holdout_refuses_a_lock_that_is_not_this_codes(world):
    _, cache, lock_path = world
    _lock(world)
    _commit(world)
    edits = {"config": lambda x: x["config"]["params"].update(confirm_hours=72),
             "grid": lambda x: x["grid"]["zz_k"].append(4.0), "keys": lambda x: x["keys"].reverse(),
             "holdout_start": lambda x: x.update(holdout_start=HS + 3600),
             "replay_anchor": lambda x: x.update(replay_anchor=REPLAY_ANCHOR - 86_400)}
    for field, edit in edits.items():
        original = lock_path.read_text()
        _recommit(world, edit)
        with pytest.raises(HoldoutCError, match=rf"\['{field}'\] are not this code's"):
            run_holdout_c(confirm=True, cache_dir=cache, lock_path=lock_path)
        _recommit(world, lambda x: x.clear() or x.update(json.loads(original)))
    assert read_lock_c(lock_path)["attempts"] == []


def test_a_dirty_tracked_tree_is_refused_at_lock_and_run_but_not_the_lock_itself(world, tmp_path):
    repo, cache, lock_path = world
    (repo / "notes.txt").write_text("a\n")
    git(repo, "add", "notes.txt")
    git(repo, "commit", "-q", "-m", "notes")
    (repo / "notes.txt").write_text("b\n")
    with pytest.raises(HoldoutCError, match="uncommitted changes"):
        _lock(world)
    assert not lock_path.exists()
    git(repo, "checkout", "-q", "notes.txt")
    _lock(world)
    _commit(world)
    (repo / "notes.txt").write_text("b\n")
    with pytest.raises(HoldoutCError, match="uncommitted changes"):
        run_holdout_c(confirm=True, cache_dir=cache, lock_path=lock_path)
    git(repo, "checkout", "-q", "notes.txt")
    lock = json.loads(lock_path.read_text())
    lock["attempts"].append({"started": "x", "data_sha256": "f" * 64})  # an uncommitted change of the lock itself
    lock_path.write_text(json.dumps(lock))
    with pytest.raises(HoldoutCError, match="other holdout bars"):  # past the clean-tree check
        run_holdout_c(confirm=True, cache_dir=cache, lock_path=lock_path)
    with pytest.raises(HoldoutCError, match="not a git working tree"):
        create_lock_c(_report(world), load_caches_c(cache), now_ts=NOW, lock_path=tmp_path / "holdout-c.lock")


def test_the_holdout_runs_once_and_records_the_criteria(world):
    _, cache, lock_path = world
    _lock(world)
    _commit(world)
    out = run_holdout_c(confirm=True, cache_dir=cache, lock_path=lock_path)
    lock = read_lock_c(lock_path)
    fr = lock["first_run"]
    assert fr == out["first_run"] and fr["end_ts"] == lock["last_bar_ts"] + 900
    assert fr["data"]["start_ts"] == REPLAY_ANCHOR and len(lock["attempts"]) == 1 and lock["attempts"][0]["completed"]
    win = {a: df[df["ts"] <= lock["last_bar_ts"]] for a, df in world_dfs_c(REPLAY_ANCHOR, WORLD_REPS).items()}
    assert fr["data"]["sha256"] == data_fingerprint_b(win, REPLAY_ANCHOR, lock["last_bar_ts"])
    crit = fr["criteria"]
    assert set(fr["metrics"]) == {"C", "BH", "C_cost_x1_5"} and fr["random"]["runs"] == 4
    assert fr["metrics"]["C"]["n_trades"] == crit["n_trades"] == 56
    assert fr["n_trades_by_asset"] == fr["random"]["n_signals"] == {"BTC": 28, "ETH": 28}
    assert crit["passed"] is True  # the synthetic setups win every time: all four criteria hold
    assert {t.setup for t in out["result"].trades} == {"C-BTC", "C-ETH"}
    assert all(HS <= t.entry_ts <= lock["last_bar_ts"] + 900 for t in out["result"].trades)
    with pytest.raises(HoldoutCError, match="already run once"):
        run_holdout_c(confirm=True, cache_dir=cache, lock_path=lock_path)


def test_an_unfinished_attempt_must_have_seen_the_same_bars(world):
    _, cache, lock_path = world
    _lock(world)
    _commit(world)
    lock = json.loads(lock_path.read_text())
    lock["attempts"].append({"started": "x", "data_sha256": "f" * 64})
    lock_path.write_text(json.dumps(lock))
    with pytest.raises(HoldoutCError, match="other holdout bars"):
        run_holdout_c(confirm=True, cache_dir=cache, lock_path=lock_path)


def test_a_cache_short_of_the_last_bar_is_refused(world):
    _, cache, lock_path = world
    _lock(world, now_ts=HS + 600 * 86_400)  # a last bar after the world's end
    _commit(world)
    with pytest.raises(HoldoutCError, match="ends before the lock's last bar"):
        run_holdout_c(confirm=True, cache_dir=cache, lock_path=lock_path)
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratc/test_holdout.py -q
```

예상: `No module named 'ewbot.stratc.holdout'`
예상: `1 error`

- [ ] **Step 3: 최소 구현**

**파일 `src/ewbot/stratc/holdout.py`** (새 파일, 전체 내용)

```python
"""Strategy-C holdout lock and its one-shot run (Plan 5b spec 6.4-6.5), strategy B's procedure with C's lock.

create_lock_c() writes <project root>/holdout-c.lock BEFORE any 2024+ candle can be read (stratc.data refuses them
until a lock exists), from the walk-forward report (`ewbot stratc walkforward` JSON): only one that proceeds (spec
6.3) and was made on the same bars, C code and imported modules (its wf_data, code_sha256 and deps_sha256 equal
research.wf_provenance_c of the bars and the code now). The lock holds the report's final combo and sha256
(wf_report_sha256), the full run config (assets and C's fixed values included), the grid, the C fingerprint,
deps_sha256 (fingerprint.deps_fingerprint: the strategy-A and strategy-B modules stratc/ imports), the sha256 of each
asset's pre-2024 15m bars the walk-forward used (wf_data), the holdout start, the replay anchor (2023-07-01), the
1,000-run random control and its seed, and last_bar_ts - the last 15m bar of the last complete hour at lock time,
where the holdout ends. It refuses when a tracked file of the lock's git working tree has uncommitted changes. The
user commits it.

run_holdout_c() runs the holdout ONCE after the user's approval (confirm=True) and records the result in first_run.
It refuses (HoldoutCError) unless: the lock's sha256 is the digest of its own bound fields and equals the committed
version's (git HEAD); first_run is empty there and in the file (one completed run, ever); the running C fingerprint
equals code_sha256; the lock's keys, grid, config, holdout_start and replay_anchor are what this code writes for its
params (lock_mismatch_c); the imported modules hash to deps_sha256; no tracked file but the lock itself has
uncommitted changes; every asset's pre-2024 bars hash to its wf_data sha256; every asset's data reaches last_bar_ts;
an unfinished earlier attempt saw the same holdout bars. Each attempt is recorded in the lock before it runs.

Criteria (spec 6.5 = strategy B's portfolio criteria, stratb.holdout.evaluate_criteria_b, all four): Sharpe above
the 50/50 buy & hold and above the 95th percentile of the random control (B's random_control: each asset's own entry
frequency and trade shapes on random 15m bars, B's engine with C's config); max drawdown <= 15%; >= 50 trades of
both assets together; total return > 0 with fees and slippage x 1.5. All four from the equity at the holdout start."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

from ewbot.backtest.holdout import data_fingerprint, git_state
from ewbot.backtest.metrics import compute_metrics
from ewbot.backtest.report import to_jsonable
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.stratb.baselines import buy_hold_b, random_control
from ewbot.stratb.config import ASSETS, M15, combo_to_json, normalize_combo, scale_costs
from ewbot.stratb.data15 import cache_paths
from ewbot.stratb.holdout import COST_MULT, data_fingerprint_b, evaluate_criteria_b
from ewbot.stratb.research import trades_by_asset, wf_data_record
from ewbot.stratc.config import GRID_C, config_for, engine_config
from ewbot.stratc.data import DEFAULT_LOCK_PATH, LOCK_KIND, REPLAY_ANCHOR, load_15m_c, lock_digest_c
from ewbot.stratc.engine import run_engine_c
from ewbot.stratc.fingerprint import deps_fingerprint, stratc_fingerprint
from ewbot.stratc.research import backtest_c, wf_provenance_c
from ewbot.stratc.signals import build_frames_c, generate_signals_c
from ewbot.types import H1

LOCK_VERSION = 1
RANDOM_RUNS = 1000


class HoldoutCError(RuntimeError):
    """The strategy-C lock cannot be written, or the holdout may not run."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write(path: Path, lock: dict[str, Any]) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(to_jsonable(lock), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_lock_c(path: Path) -> dict[str, Any]:
    try:
        lock = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HoldoutCError(f"{path} is missing or unreadable: {exc}") from exc
    if not isinstance(lock, dict) or lock.get("kind") != LOCK_KIND:
        raise HoldoutCError(f"{path} is not a strategy-C holdout lock")
    if lock_digest_c(lock) != lock.get("sha256"):
        raise HoldoutCError(f"{path}: sha256 is not the digest of its bound fields (edited by hand?)")
    return lock


def committed_lock_c(path: Path) -> dict[str, Any]:
    """The version of the lock in git HEAD (the lock must be committed before the holdout runs)."""
    p = Path(path)
    try:
        r = subprocess.run(["git", "-C", str(p.parent), "show", f"HEAD:./{p.name}"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HoldoutCError(f"git is not available to check {p}: {exc}") from exc
    if r.returncode != 0:
        raise HoldoutCError(f"{p} is not committed: commit holdout-c.lock before the holdout")
    try:
        return json.loads(r.stdout.decode("utf-8"))
    except ValueError as exc:
        raise HoldoutCError(f"the committed {p.name} is not JSON") from exc


def _dirty(path: Path) -> list[str]:
    """Tracked files of the lock's git working tree with uncommitted changes, the lock itself excepted."""
    p = Path(path)
    cmd = ["git", "-C", str(p.parent), "status", "--porcelain", "--untracked-files=no", "--", ":/", f":!{p.name}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HoldoutCError(f"git is not available to check the working tree: {exc}") from exc
    if r.returncode != 0:
        raise HoldoutCError(f"{p.parent} is not a git working tree (the lock is committed there): {r.stderr.strip()}")
    return [line[3:] for line in r.stdout.splitlines() if line.strip()]


def _refuse_dirty(path: Path) -> None:
    dirty = _dirty(path)
    if dirty:
        raise HoldoutCError(f"uncommitted changes to tracked files {dirty[:5]}{' ...' if len(dirty) > 5 else ''}: "
                            "the lock is written and the holdout runs on a clean tree (commit or discard them)")


def lock_mismatch_c(lock: Mapping[str, Any]) -> list[str]:
    """The lock fields that are not what this code writes for the lock's params: keys, grid, config
    (config_for(params)), holdout_start, replay_anchor; ["params"] when the params are not a grid combo."""
    try:
        combo = normalize_combo(lock.get("params") or [])
    except (TypeError, ValueError):
        return ["params"]
    want = {"keys": list(GRID_C), "grid": to_jsonable(GRID_C),
            "config": to_jsonable(dataclasses.asdict(config_for(combo))), "holdout_start": HOLDOUT_START,
            "replay_anchor": REPLAY_ANCHOR}
    return [k for k, v in want.items() if lock.get(k) != v]


_PROVENANCE = {"wf_data": "15m bars", "code_sha256": "C code", "deps_sha256": "modules strategy C imports"}


def create_lock_c(
    report_path: Path,
    dfs_wf: Mapping[str, pd.DataFrame],
    *,
    now_ts: int,
    lock_path: Path = DEFAULT_LOCK_PATH,
    random_runs: int = RANDOM_RUNS,
    random_seed: int = 0,
) -> dict[str, Any]:
    """Write the lock (once) from the walk-forward report at report_path. dfs_wf: every asset's walk-forward 15m
    bars, all before 2024-01-01 (the report must have been made on them)."""
    path = Path(lock_path)
    if path.exists():
        raise HoldoutCError(f"{path} exists: the strategy-C lock is written once")
    try:
        raw = Path(report_path).read_bytes()
        report = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise HoldoutCError(f"{report_path}: unreadable walk-forward report: {exc}") from exc
    if (not isinstance(report, dict) or not report.get("proceed") or not report.get("chosen_final")
            or report.get("chosen_final_fallback")):
        raise HoldoutCError(f"{report_path}: proceed is false (spec 6.3: negative out-of-sample, fallback or no "
                            "pick); strategy C ends without a holdout")
    try:
        combo = normalize_combo(report["chosen_final"])
    except (TypeError, ValueError) as exc:
        raise HoldoutCError(str(exc)) from exc
    if any(len(df) and int(df["ts"].max()) >= HOLDOUT_START for df in dfs_wf.values()):
        raise HoldoutCError("the walk-forward data must end before 2024-01-01")
    try:
        made_on = wf_provenance_c(dfs_wf)
    except ValueError as exc:
        raise HoldoutCError(str(exc)) from exc
    stale = [k for k in _PROVENANCE if report.get(k) != made_on[k]]
    if stale:
        raise HoldoutCError(f"{report_path} was made on other {stale} ({', '.join(_PROVENANCE[k] for k in stale)}) "
                            "than now: re-run `ewbot stratc walkforward`")
    last_bar = int(now_ts) // H1 * H1 - M15
    if last_bar < HOLDOUT_START:
        raise HoldoutCError("the lock must be written after 2024-01-01")
    if int(random_runs) < 1:
        raise HoldoutCError("random_runs must be >= 1 (criterion 1 compares with the random control)")
    _refuse_dirty(path)
    lock: dict[str, Any] = {
        "kind": LOCK_KIND,
        "version": LOCK_VERSION,
        "keys": list(GRID_C),
        "params": combo_to_json(combo),
        "config": to_jsonable(dataclasses.asdict(config_for(combo))),
        "grid": to_jsonable(GRID_C),
        "holdout_start": HOLDOUT_START,
        "replay_anchor": REPLAY_ANCHOR,
        "last_bar_ts": last_bar,
        "random_runs": int(random_runs),
        "random_seed": int(random_seed),
        "code_sha256": made_on["code_sha256"],
        "deps_sha256": made_on["deps_sha256"],
        "wf_data": made_on["wf_data"],
        "wf_report_sha256": hashlib.sha256(raw).hexdigest(),
    }
    lock.update(sha256=lock_digest_c(lock), params_source=f"walkforward:{report_path}#chosen_final", locked_at=_now(),
                **git_state(), attempts=[], first_run=None)
    _write(path, lock)
    return lock


def run_holdout_c(
    *,
    confirm: bool,
    cache_dir: Path,
    lock_path: Path = DEFAULT_LOCK_PATH,
    workers: int = 1,
    fingerprint: Callable[[], str] = stratc_fingerprint,
    deps: Callable[[], str] = deps_fingerprint,
) -> dict[str, Any]:
    """The one-shot holdout over [2024-01-01, last_bar_ts], recomputed from REPLAY_ANCHOR (2023-07-01)."""
    if not confirm:
        raise HoldoutCError("the holdout is single-use: pass confirm=True (CLI: --confirm) after the user's approval")
    path = Path(lock_path)
    lock = read_lock_c(path)
    head = committed_lock_c(path)
    if head.get("sha256") != lock["sha256"]:
        raise HoldoutCError(f"{path} differs from the committed lock: commit the lock as written by `holdout lock`")
    if lock.get("first_run") is not None or head.get("first_run") is not None:
        raise HoldoutCError("the strategy-C holdout has already run once: no rerun, no other combo (spec 6.4)")
    if fingerprint() != lock["code_sha256"]:
        raise HoldoutCError("the strategy-C code changed after the lock (C fingerprint differs): refusing")
    stale = lock_mismatch_c(lock)
    if stale:
        raise HoldoutCError(f"the lock's {stale} are not this code's (keys, grid, config_for(params), holdout start, "
                            "replay anchor): refusing")
    if deps() != lock.get("deps_sha256"):
        raise HoldoutCError("the strategy-A/B modules strategy C imports changed after the lock (deps_sha256 "
                            "differs): refusing")
    _refuse_dirty(path)
    dfs = {a: load_15m_c(p, with_holdout=True, lock_path=path) for a, p in cache_paths(cache_dir).items()}
    try:
        wf = wf_data_record({a: df[df["ts"] < HOLDOUT_START] for a, df in dfs.items()})
    except ValueError as exc:
        raise HoldoutCError(str(exc)) from exc
    changed = [a for a in ASSETS if wf[a]["sha256"] != lock["wf_data"][a]["sha256"]]
    if changed:
        raise HoldoutCError(f"the {changed} 15m bars before 2024-01-01 differ from the ones the walk-forward used")
    last = int(lock["last_bar_ts"])
    short = [a for a, df in dfs.items() if df.empty or int(df["ts"].iloc[-1]) < last]
    if short:
        raise HoldoutCError(f"the {short} cache ends before the lock's last bar {last}: run `ewbot stratc fetch` first")
    win = {a: df[(df["ts"] >= REPLAY_ANCHOR) & (df["ts"] <= last)].reset_index(drop=True) for a, df in dfs.items()}
    data = {"start_ts": REPLAY_ANCHOR, "last_bar_ts": last, "n_bars": {a: int(len(df)) for a, df in win.items()},
            "sha256": data_fingerprint_b(win, REPLAY_ANCHOR, last),
            "assets": {a: data_fingerprint(df, REPLAY_ANCHOR, last) for a, df in win.items()}}
    for a in lock["attempts"]:
        if a["data_sha256"] != data["sha256"]:
            raise HoldoutCError("an earlier unfinished attempt saw other holdout bars: refusing (inspect by hand)")
    attempt = {"started": _now(), "data_sha256": data["sha256"], **git_state()}
    lock["attempts"].append(attempt)
    _write(path, lock)

    cfg = config_for(normalize_combo(lock["params"]))
    end = last + M15
    res = backtest_c(win, cfg, HOLDOUT_START, end, with_holdout=True)
    frames = build_frames_c(win, cfg.params)
    f15 = {a: f.f15 for a, f in frames.items()}
    ecfg = engine_config(cfg)
    c = compute_metrics(res.equity, res.trades, HOLDOUT_START, end)
    bh_eq, bh_trades = buy_hold_b(f15, HOLDOUT_START, end, ecfg)
    bh = compute_metrics(bh_eq, bh_trades, HOLDOUT_START, end)
    rnd = random_control(f15, ecfg, res.trades, HOLDOUT_START, end, runs=int(lock["random_runs"]),
                         seed=int(lock["random_seed"]), workers=workers)
    dear = dataclasses.replace(cfg, costs=scale_costs(cfg.costs, COST_MULT))
    signals = {a: generate_signals_c(f, cfg.params) for a, f in frames.items()}
    res_dear = run_engine_c(frames, signals, dear, HOLDOUT_START)
    cost = compute_metrics(res_dear.equity, res_dear.trades, HOLDOUT_START, end)
    criteria = evaluate_criteria_b(c, bh, rnd, cost)
    attempt["completed"] = _now()
    lock["first_run"] = to_jsonable({
        "started": attempt["started"], "completed": attempt["completed"], "start_ts": HOLDOUT_START, "end_ts": end,
        "last_bar_ts": last, "data": data,
        "metrics": {"C": dataclasses.asdict(c), "BH": dataclasses.asdict(bh), "C_cost_x1_5": dataclasses.asdict(cost)},
        "n_trades_by_asset": trades_by_asset([t for t in res.trades if HOLDOUT_START <= t.entry_ts < end]),
        "random": rnd, "criteria": criteria,
    })
    _write(path, lock)
    return {"lock_path": str(path), "result": res, "first_run": lock["first_run"]}
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratc/test_holdout.py -q
```

예상: `12 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratc/holdout.py tests/stratc/test_holdout.py
git commit -m "feat(stratc): holdout-c.lock from the walk-forward report, the one-shot holdout and B's portfolio criteria" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: CLI `ewbot stratc …`와 운영 문서

**Files:**
- Modify: `src/ewbot/cli.py` (파서의 `stratb` 블록 앞, 처리 함수의 "strategy B" 절 앞, `_COMMANDS`)
- Create: `docs/strategy-c.md`
- Test: `tests/stratc/test_cli_stratc.py`

**Interfaces:**
- Consumes: Task 1~6. `cli.py`에 이미 있는 것: `_b_params(text) -> tuple`(B의 그리드 조합; 아니면 종료 코드 2), `_requested_window(args)`, `_print_comparison`, `_refuse(msg)`(종료 코드 2), `_b_write_json(path, obj)`, `compute_metrics`, `WF_END`, `argparse`, `dataclasses`, `json`, `os`, `sys`, `time`, `Path`, `pd`. `ewbot.stratb.data15`: `cache_paths`, `gap_report`.
- Produces: `ewbot stratc fetch | backtest --params … [--start] [--end] [--out] | walkforward [--workers] [--out] | holdout lock --params-from … | holdout run --confirm [--workers]`(모두 `--cache-dir`, 기본 `data/cache`); `cli._c_lock() -> Path`(호출 시점의 `stratc.data.DEFAULT_LOCK_PATH`), `_c_dfs(args)`, `_c_fetch`, `_c_backtest`, `_c_walkforward`, `_c_holdout_lock`, `_c_holdout_run`, `cmd_stratc(args) -> int`. 출력 기본값 `reports/stratc-backtest.json`, `reports/stratc-walkforward.json`.

해석 10대로 C의 처리 함수 안에서만 `ewbot.stratc`를 import하고, `cli.py`의 수정은 A-ETH와 겹치지 않는 세 자리에만 둔다(Global Constraints). 테스트는 파서, `ewbot.cli`를 불러와도 `ewbot.stratc`가 올라오지 않음(새 프로세스에서 확인), 가드된 수집 보고, 2024에 닿지 않는 백테스트와 그리드 밖 조합의 거부, 워크포워드 출력과 종료 안내, 진행하는 보고서에서만 잠금, 승인과 진짜 C 잠금이 필요한 홀드아웃 실행을 확인한다. 운영 문서는 1부의 내용(연구와 홀드아웃)만 담고, 2부가 모의매매 절을 덧붙인다.

- [ ] **Step 1: 실패하는 테스트 작성**

**파일 `tests/stratc/test_cli_stratc.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from ewbot.cli import build_parser, main
from ewbot.stratb.data15 import cache_paths
from ewbot.stratc import data as cdata
from ewbot.stratc import research as cresearch
from ewbot.stratc.data import REPLAY_ANCHOR, load_caches_c
from ewbot.stratc.holdout import read_lock_c
from ewbot.stratc.research import wf_provenance_c
from tests.paper.helpers import git
from tests.stratc.helpers import save_world, world_dfs_c

HS = 1_704_067_200
COMBO = [2.0, [0.382, 0.618], 1.0, 1.272, "breakeven"]


@pytest.fixture
def cache(tmp_path):
    c = tmp_path / "cache"
    c.mkdir()
    save_world(c, world_dfs_c(REPLAY_ANCHOR, 24))  # 2023-07-01 .. 2024-01-28
    return c


def test_the_parser_has_the_spec_subcommands():
    p = build_parser()
    assert p.parse_args(["stratc", "fetch"]).c_cmd == "fetch"
    ns = p.parse_args(["stratc", "backtest", "--params", "2,0.382,0.618,1,1.272,breakeven"])
    assert (ns.c_cmd, ns.start, ns.end, str(ns.out)) == ("backtest", None, None, "reports/stratc-backtest.json")
    assert p.parse_args(["stratc", "walkforward", "--workers", "2"]).workers == 2
    assert p.parse_args(["stratc", "holdout", "lock", "--params-from", "wf.json"]).ch_cmd == "lock"
    assert p.parse_args(["stratc", "holdout", "run", "--confirm"]).confirm is True
    with pytest.raises(SystemExit):
        p.parse_args(["stratc", "holdout", "run", "--params", "1"])


def test_the_cli_loads_strategy_c_only_inside_its_handlers():
    code = "import sys, ewbot.cli; print(sorted(m for m in sys.modules if m.startswith('ewbot.stratc')))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True)
    assert out.stdout.strip() == "[]"


def test_fetch_reports_both_guarded_caches(tmp_path, cache, monkeypatch, capsys):
    monkeypatch.setattr(cdata, "DEFAULT_LOCK_PATH", tmp_path / "none.lock")
    seen = {}

    def fake_update(c, lock_path):
        seen["lock"] = lock_path
        return cache_paths(c)

    monkeypatch.setattr(cdata, "update_caches_c", fake_update)
    assert main(["stratc", "fetch", "--cache-dir", str(cache)]) == 0
    out = capsys.readouterr().out
    assert seen["lock"] == tmp_path / "none.lock"
    assert "BTC bars before 2024-01-01: 17664" in out and "ETH bars before 2024-01-01: 17664" in out
    assert "stops at 2024-01-01 (no lock)" in out


def test_backtest_writes_json_and_never_reaches_into_2024(tmp_path, cache):
    out = tmp_path / "bt.json"
    assert main(["stratc", "backtest", "--params", "2,0.382,0.618,1,1.272,breakeven", "--cache-dir", str(cache),
                 "--out", str(out)]) == 0
    res = json.loads(out.read_text())
    assert res["params"] == COMBO and res["window"][1] == HS and res["metrics"]["n_trades"] == len(res["trades"]) == 38
    assert all(t["entry_ts"] < HS for t in res["trades"]) and res["n_trades_by_asset"] == {"BTC": 19, "ETH": 19}
    assert {t["setup"] for t in res["trades"]} == {"C-BTC", "C-ETH"}
    with pytest.raises(SystemExit) as exc:
        main(["stratc", "backtest", "--params", "2,0.382,0.618,1,1.272,breakeven", "--cache-dir", str(cache),
              "--end", "2024-02-01"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main(["stratc", "backtest", "--params", "2.5,0.382,0.618,1,1.272,breakeven", "--cache-dir", str(cache)])
    assert exc.value.code == 2


def test_walkforward_writes_json_and_says_whether_to_proceed(tmp_path, cache, monkeypatch, capsys):
    def fake(dfs, workers):
        assert sorted(dfs) == ["BTC", "ETH"] and all(int(df["ts"].max()) < HS for df in dfs.values())
        return {"folds": [], "stitched_metrics": {k: 0.0 for k in ("total_return", "cagr", "max_dd", "sharpe", "sortino",
                "calmar", "win_rate", "avg_r")} | {"n_trades": 0, "exposure": 0.0}, "oos_trades": [],
                "chosen_final": None, "chosen_final_fallback": False, "proceed": False}

    monkeypatch.setattr(cresearch, "walk_forward_c", fake)
    out = tmp_path / "wf.json"
    assert main(["stratc", "walkforward", "--cache-dir", str(cache), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["proceed"] is False and "oos_trades" not in json.loads(out.read_text())
    assert "strategy C ends here" in capsys.readouterr().err


def test_holdout_lock_needs_a_walk_forward_that_proceeds(tmp_path, cache, monkeypatch, capsys):
    (tmp_path / "repo").mkdir()
    git(tmp_path / "repo", "init", "-q")
    lock_path = tmp_path / "repo" / "holdout-c.lock"
    monkeypatch.setattr(cdata, "DEFAULT_LOCK_PATH", lock_path)
    wf = tmp_path / "wf.json"
    wf.write_text(json.dumps({"chosen_final": COMBO, "chosen_final_fallback": False, "proceed": False}))
    with pytest.raises(SystemExit) as exc:
        main(["stratc", "holdout", "lock", "--params-from", str(wf), "--cache-dir", str(cache)])
    assert exc.value.code == 2 and not lock_path.exists()
    wf.write_text(json.dumps({"chosen_final": COMBO, "chosen_final_fallback": False, "proceed": True,
                              **wf_provenance_c(load_caches_c(cache))}))
    assert main(["stratc", "holdout", "lock", "--params-from", str(wf), "--cache-dir", str(cache)]) == 0
    lock = read_lock_c(lock_path)
    assert lock["params"] == COMBO and lock["random_runs"] == 1000 and lock["params_source"] == f"walkforward:{wf}#chosen_final"
    assert "Commit it NOW" in capsys.readouterr().out


def test_holdout_run_needs_confirm_and_a_c_lock(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cdata, "DEFAULT_LOCK_PATH", tmp_path / "holdout-c.lock")
    assert main(["stratc", "holdout", "run"]) == 2
    assert "--confirm" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        main(["stratc", "holdout", "run", "--confirm"])
    assert exc.value.code == 2 and "missing or not a valid strategy-C lock" in capsys.readouterr().err
    (tmp_path / "holdout-c.lock").write_text(json.dumps({"kind": "ewbot-stratc-holdout", "sha256": "ab" * 32}))
    with pytest.raises(SystemExit) as exc:  # a hand-made file with the lock's shape is not a lock
        main(["stratc", "holdout", "run", "--confirm"])
    assert exc.value.code == 2 and "missing or not a valid strategy-C lock" in capsys.readouterr().err
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratc/test_cli_stratc.py -q
```

예상: `invalid choice: 'stratc'`
예상: `6 failed, 1 passed`

`stratc` 하위 명령이 아직 없어 파서가 거부한다. `ewbot.cli`가 `ewbot.stratc`를 불러오지 않는다는 테스트 1개는 지금도 통과한다.

- [ ] **Step 3: 최소 구현**

**수정 `src/ewbot/cli.py`** (1/3) — 다음 부분을 찾아:

```python
    p_bk.add_argument("--out", type=Path, required=True)

    p_b = sub.add_parser("stratb", help="strategy B (Plan 4): 15m data, research, one-shot holdout, paper trading")
```

바꿀 내용:

```python
    p_bk.add_argument("--out", type=Path, required=True)

    # strategy C (Plan 5b): its handlers are in the "strategy C" section below (lazy imports of ewbot.stratc)
    p_c = sub.add_parser("stratc", help="strategy C (Plan 5b): B2's rules on 4h/1h bars, research, one-shot holdout")
    c_sub = p_c.add_subparsers(dest="c_cmd", required=True)
    c_cache = argparse.ArgumentParser(add_help=False)
    c_cache.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    c_sub.add_parser("fetch", parents=[c_cache], help="update the BTC and ETH 15m caches (B's files; before 2024 until "
                     "holdout-c.lock)")
    p_cb = c_sub.add_parser("backtest", parents=[c_cache], help="one strategy-C backtest (BTC + ETH) before 2024")
    p_cb.add_argument("--params", required=True, help="zz_k,z_lo,z_hi,tp1_r,ext,remainder e.g. 2.0,0.382,0.618,1.0,1.272,breakeven")
    p_cb.add_argument("--start", default=None, help="UTC date YYYY-MM-DD (default: first bar)")
    p_cb.add_argument("--end", default=None, help=f"UTC date YYYY-MM-DD, exclusive, at most {WF_END}")
    p_cb.add_argument("--out", type=Path, default=Path("reports/stratc-backtest.json"))
    p_cw = c_sub.add_parser("walkforward", parents=[c_cache], help="32-combo walk-forward 2017-01..2023-12 (A's rules)")
    p_cw.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p_cw.add_argument("--out", type=Path, default=Path("reports/stratc-walkforward.json"))
    p_ch = c_sub.add_parser("holdout", help="holdout-c.lock and the ONE-SHOT strategy-C holdout")
    ch_sub = p_ch.add_subparsers(dest="ch_cmd", required=True)
    p_chl = ch_sub.add_parser("lock", parents=[c_cache], help="write holdout-c.lock from the walk-forward (commit it)")
    p_chl.add_argument("--params-from", type=Path, required=True, help="reports/stratc-walkforward.json")
    p_chr = ch_sub.add_parser("run", parents=[c_cache], help="run the holdout ONCE (the user's approval: --confirm)")
    p_chr.add_argument("--confirm", action="store_true")
    p_chr.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))

    p_b = sub.add_parser("stratb", help="strategy B (Plan 4): 15m data, research, one-shot holdout, paper trading")
```

**수정 `src/ewbot/cli.py`** (2/3) — 다음 부분을 찾아:

```python
# ----------------------------------------------------------------------------- strategy B (Plan 4)
```

바꿀 내용:

```python
# ----------------------------------------------------------------------------- strategy C (Plan 5b)
# `stratc fetch | backtest | walkforward | holdout lock | holdout run` is strategy C (ewbot.stratc) on BTC and ETH: it
# shares strategy B's 15m cache files under its own lock (holdout-c.lock), and its research commands never read a bar
# from 2024-01-01 on (stratc.data filters them out). ewbot.stratc is imported inside these handlers only, so the other
# commands never load it. Its combos are strategy B's grid (_b_params).


def _c_lock() -> Path:
    from ewbot.stratc import data

    return data.DEFAULT_LOCK_PATH


def _c_dfs(args: argparse.Namespace) -> dict[str, pd.DataFrame]:
    """Every asset's 15m bars before 2024-01-01 (stratc.data filters the rest out while reading)."""
    from ewbot.stratb.data15 import cache_paths, gap_report
    from ewbot.stratc.data import load_caches_c

    for path in cache_paths(args.cache_dir).values():
        if not path.exists():
            raise SystemExit(f"15m cache not found: {path} - run `ewbot stratc fetch` first")
    dfs = load_caches_c(args.cache_dir)
    for a, df in dfs.items():
        rep = gap_report(df)
        print(f"[data] {a}: {rep.n} 15m bars before {WF_END}, {len(rep.missing_ts)} missing", file=sys.stderr)
    return dfs


def _c_fetch(args: argparse.Namespace) -> int:
    from ewbot.stratb.data15 import gap_report
    from ewbot.stratc.data import load_caches_c, lock_exists_c, update_caches_c

    lock = _c_lock()
    paths = update_caches_c(args.cache_dir, lock_path=lock)
    for a, df in load_caches_c(args.cache_dir).items():
        rep = gap_report(df)
        print(f"{a} cache: {paths[a]}\n{a} bars before {WF_END}: {rep.n}  missing: {len(rep.missing_ts)}  duplicate: "
              f"{len(rep.duplicate_ts)}")
    print(f"fetch window: {'2024+ included (holdout-c.lock exists)' if lock_exists_c(lock) else f'stops at {WF_END} (no lock)'}")
    return 0


def _c_backtest(args: argparse.Namespace) -> int:
    from ewbot.stratb.config import combo_to_json
    from ewbot.stratb.research import trades_by_asset
    from ewbot.stratc.config import config_for
    from ewbot.stratc.research import backtest_c

    combo = _b_params(args.params)
    start, end = _requested_window(args)
    dfs = _c_dfs(args)
    start = start if start is not None else min(int(df["ts"].iloc[0]) for df in dfs.values())
    end = end if end is not None else max(int(df["ts"].iloc[-1]) for df in dfs.values()) + 900
    res = backtest_c(dfs, config_for(combo), start, end)
    m = compute_metrics(res.equity, res.trades, start, end)
    _print_comparison({"C": dataclasses.asdict(m)})
    by_asset = trades_by_asset([t for t in res.trades if start <= t.entry_ts < end])
    print(f"trades by asset: {by_asset}")
    _b_write_json(args.out, {"params": combo_to_json(combo), "window": [start, end], "metrics": m,
                             "n_trades_by_asset": by_asset, "trades": list(res.trades), "events": list(res.events)})
    print(f"json: {args.out}")
    return 0


def _c_walkforward(args: argparse.Namespace) -> int:
    from ewbot.stratb.config import combo_to_json
    from ewbot.stratc.research import walk_forward_c

    t0 = time.perf_counter()
    wf = walk_forward_c(_c_dfs(args), args.workers)
    _b_write_json(args.out, {k: v for k, v in wf.items() if k != "oos_trades"})
    _print_comparison({"WF-OOS": wf["stitched_metrics"]})
    chosen = None if wf["chosen_final"] is None else combo_to_json(wf["chosen_final"])
    print(f"walk-forward C: {len(wf['folds'])} folds in {time.perf_counter() - t0:.0f}s; chosen_final = {chosen}; "
          f"fallback = {wf['chosen_final_fallback']}; proceed = {wf['proceed']}; json: {args.out}")
    if not wf["proceed"]:
        print("spec 6.3: no holdout - the stitched out-of-sample return is negative or there is no final combo "
              "from the last fold; strategy C ends here (one attempt, spec 1)", file=sys.stderr)
    return 0


def _c_holdout_lock(args: argparse.Namespace) -> int:
    """The lock from the walk-forward report; stratc.holdout.create_lock_c refuses one that does not proceed or was
    made on other bars or code, and a dirty tracked tree."""
    from ewbot.stratc.holdout import HoldoutCError, create_lock_c

    lock_path = _c_lock()
    try:
        lock = create_lock_c(Path(args.params_from), _c_dfs(args), now_ts=int(time.time()), lock_path=lock_path)
    except HoldoutCError as exc:
        _refuse(str(exc))
    print(f"holdout-c.lock written: {lock_path} (sha256 {lock['sha256'][:12]}, params {lock['params']}, "
          f"last bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(lock['last_bar_ts']))} UTC).\n"
          "Commit it NOW, before the holdout runs:\n"
          f'  git add "{lock_path}"\n  git commit -m "chore(stratc): lock the strategy-C holdout {lock["sha256"][:12]}"')
    return 0


def _c_holdout_run(args: argparse.Namespace) -> int:
    from ewbot.stratc.data import lock_exists_c, update_caches_c
    from ewbot.stratc.holdout import HoldoutCError, run_holdout_c

    if not args.confirm:
        print("refusing: the strategy-C holdout runs once, after the user's approval: re-run with --confirm",
              file=sys.stderr)
        return 2
    lock_path = _c_lock()
    if not lock_exists_c(lock_path):
        _refuse(f"{lock_path} is missing or not a valid strategy-C lock (its sha256 must be the digest of its fields): "
                "`ewbot stratc holdout lock` first")
    update_caches_c(args.cache_dir, lock_path=lock_path)
    try:
        out = run_holdout_c(confirm=True, cache_dir=args.cache_dir, lock_path=lock_path, workers=args.workers)
    except HoldoutCError as exc:
        _refuse(str(exc))
    fr = out["first_run"]
    _print_comparison({k: fr["metrics"][k] for k in ("C", "BH", "C_cost_x1_5")})
    print(f"trades by asset: {fr['n_trades_by_asset']}")
    print(json.dumps(fr["criteria"], indent=1))
    print(f"holdout {'PASSED' if fr['criteria']['passed'] else 'FAILED - strategy C ends (spec 6.5)'}; result in "
          f"{lock_path} first_run. Commit it:\n  git add \"{lock_path}\"\n"
          '  git commit -m "chore(stratc): record the strategy-C holdout run"')
    return 0


def cmd_stratc(args: argparse.Namespace) -> int:
    if args.c_cmd == "holdout":
        return _c_holdout_lock(args) if args.ch_cmd == "lock" else _c_holdout_run(args)
    return {"fetch": _c_fetch, "backtest": _c_backtest, "walkforward": _c_walkforward}[args.c_cmd](args)


# ----------------------------------------------------------------------------- strategy B (Plan 4)
```

**수정 `src/ewbot/cli.py`** (3/3) — 다음 부분을 찾아:

```python
    "live": cmd_live,
    "stratb": cmd_stratb,
```

바꿀 내용:

```python
    "live": cmd_live,
    "stratc": cmd_stratc,
    "stratb": cmd_stratb,
```

**파일 `docs/strategy-c.md`** (새 파일, 전체 내용)

````markdown
# Strategy C (Plan 5b) — research, one-shot holdout, paper trading

Spec: `docs/superpowers/specs/2026-10-08-ewbot-plan5b-strategy-c-design.md`. Plan: `docs/superpowers/plans/2026-10-08-ewbot-plan5b-strategy-c.md`.

Strategy C is strategy B2 one timeframe up. The trend comes from 4h bars (A's ATR ZigZag, k 2.0: longs only after a higher high and a higher low, shorts only after a lower high and a lower low), the reference leg and its Fibonacci retracement zone (with an earlier 4h swing inside it) from the same 4h bars (ZigZag k 2.0 or 3.0), and the entry from 1h bars: an RSI(14) divergence of two 1h pivots with the second inside the zone, then a 1h close beyond the swing between them, entering at the next 1h open. Half is taken at TP1, the rest runs to the leg's extension with a breakeven or original stop, everything is closed after 8 days. It trades BTC and ETH with one shared parameter set, exactly as strategy B: at most one position per asset, 0.5% of the shared account equity per trade, leverage cap 3x per position and 3x for all positions together, A's breakers (-3% day, -15% total) on the account, and A's costs with each asset's contract size. Fills are simulated by strategy B's 15-minute engine.

**One attempt (spec 1):** if the walk-forward does not proceed or the holdout fails, strategy C ends permanently. No other variant is tried.

## Assets and contracts

| Asset | Signals (Coinbase spot) | Modelled contract | Contract size | Tick |
|---|---|---|---|---|
| BTC | `BTC-USD` | `BIP-20DEC30-CDE` | 0.01 BTC | $5 |
| ETH | `ETH-USD` | `ETP-20DEC30-CDE` | 0.1 ETH | $0.5 |

## Isolation from strategies A and B

- C's code lives in `src/ewbot/stratc/` (the C fingerprint hashes every `*.py` there) and `src/ewbot/paperc/` (the paper-C runtime). A's files and B's files (`src/ewbot/stratb/`, `src/ewbot/paperb/`, `paper-b.yml`) are never changed; C only imports from them.
- The modules outside `stratc/` that it imports, directly or through other modules (A's config, costs, sizing, breakers, ZigZag, metrics, walk-forward windows, data hashing; B's rules, frames, engine, baselines, walk-forward rule and criteria), are hashed into `deps_sha256` (`ewbot.stratc.fingerprint.deps_fingerprint`). `holdout-c.lock` binds it: after the lock a change to any of them makes the holdout and paper-C refuse to run. Check with `uv run python -c "from ewbot.stratc.fingerprint import deps_files; print(*sorted(deps_files()), sep=chr(10))"`.
- C shares B's 15m cache files (`data/cache/BTC-USD_15m.parquet`, `data/cache/ETH-USD_15m.parquet`) under its own 2024 guard: nothing from 2024-01-01 on is downloaded or read for C before `holdout-c.lock` exists. B's own loader keeps filtering 2024+ out of the same files.
- C has its own lock (`holdout-c.lock`), ledger branch (`paper-c`), workflow (`.github/workflows/paper-c.yml`, dormant) and Actions cache key (`stratc-15m-`).
- `tests/stratc/test_isolation.py` checks A's fingerprint, that C is outside A's fingerprinted folders and that no A or B path changed since `master`; `tests/paper/test_static.py` (run by `ci.yml`) carries the CI part.

## Commands

```bash
uv run ewbot stratc fetch                                   # BTC and ETH 15m caches (B's files); stop at 2024-01-01 until holdout-c.lock exists
uv run ewbot stratc backtest --params 2.0,0.382,0.618,1.0,1.272,breakeven   # one BTC + ETH run, 2017..2023 only
uv run ewbot stratc walkforward                             # 32 combos on the BTC + ETH portfolio, A's windows and rules -> reports/stratc-walkforward.json
uv run ewbot stratc holdout lock --params-from reports/stratc-walkforward.json   # writes holdout-c.lock
uv run ewbot stratc holdout run --confirm                   # ONCE, after your approval
```

`--params` is `zz_k,z_lo,z_hi,tp1_r,ext,remainder` (strategy B's grid).

## Research and holdout (in this order)

1. `uv run ewbot stratc fetch`. If strategy B's caches are already there, nothing new is downloaded. Check the printed missing-bar counts.
2. `uv run ewbot stratc walkforward`. If `proceed` is false, strategy C ends here (spec 6.3; one attempt).
3. On a clean working tree: `uv run ewbot stratc holdout lock --params-from reports/stratc-walkforward.json`, then commit `holdout-c.lock` right away.
4. Ask the user for approval. Only then, with a clean tree, `HEAD` at the lock commit and the protection tests passing (`uv run --group live pytest tests/paper/test_fingerprint.py tests/paper/test_static.py tests/stratc/test_isolation.py tests/stratb/test_isolation.py -q`): `uv run ewbot stratc holdout run --confirm`, and commit `holdout-c.lock` **immediately** (it now holds `first_run`). Keep `data/cache/` until paper-C has reproduced the holdout.
5. If `first_run.criteria.passed` is false, strategy C ends (spec 6.5): no paper trading, no retuning, no further variant.
````

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratc/test_cli_stratc.py -q
```

예상: `7 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/cli.py docs/strategy-c.md tests/stratc/test_cli_stratc.py
git commit -m "feat(cli): ewbot stratc fetch | backtest | walkforward | holdout lock | holdout run, strategy-C ops guide" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: 1부 최종 검증 (전체 테스트, A 지문, A·B 보호, 바뀐 파일)

**Files:** 없음. 코드를 바꾸지 않고 병합 전에 확인만 한다.

**Interfaces:**
- Consumes: Task 1~7의 결과 전체.
- Produces: 병합 가능 여부의 판단 근거(전체 테스트 수, A 지문 값, 보호 검사, 바뀐 파일 목록).

- [ ] **Step 1: 전체 테스트**

```bash
uv run --group live pytest -q
```

예상: `1459 passed, 1 skipped`

`data/cache/BTC-USD_1h.parquet`가 있으면 `1460 passed`다. 기준선보다 정확히 79개가 많아야 한다.

- [ ] **Step 2: A 지문이 그대로인지 확인**

```bash
uv run python -c "from ewbot.paper.fingerprint import current_fingerprint; print(current_fingerprint())"
```

예상: `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1`

- [ ] **Step 3: A·B 보호 검사를 건너뜀 없이 실행**

```bash
uv run --group live pytest tests/stratc/test_isolation.py tests/stratb/test_isolation.py tests/paper/test_fingerprint.py tests/paper/test_static.py -q -rs
```

예상: `58 passed`

출력에 `SKIPPED`가 없어야 한다: `master`(또는 `origin/master`)가 있는 작업 트리에서는 merge-base 검사 두 개와 A 지문의 git 기준선 검사가 실제로 돈다.

- [ ] **Step 4: `master` 대비 바뀐 파일이 허용 목록뿐인지 확인**

```bash
git diff --name-only "$(git merge-base HEAD origin/master)" -- . ':(exclude)docs/superpowers'
```

예상 출력(정확히 이 목록):

```text
docs/strategy-c.md
src/ewbot/cli.py
src/ewbot/stratc/__init__.py
src/ewbot/stratc/config.py
src/ewbot/stratc/data.py
src/ewbot/stratc/engine.py
src/ewbot/stratc/fingerprint.py
src/ewbot/stratc/holdout.py
src/ewbot/stratc/research.py
src/ewbot/stratc/signals.py
tests/paper/test_static.py
tests/stratc/__init__.py
tests/stratc/helpers.py
tests/stratc/test_cli_stratc.py
tests/stratc/test_config.py
tests/stratc/test_data.py
tests/stratc/test_engine.py
tests/stratc/test_fingerprint_c.py
tests/stratc/test_holdout.py
tests/stratc/test_isolation.py
tests/stratc/test_research.py
tests/stratc/test_signals.py
```

목록에 A나 B의 경로가 하나라도 있으면 병합하지 않는다.

- [ ] **Step 5: 작업 트리가 깨끗한지 확인**

```bash
test -z "$(git status --porcelain)" && echo clean
```

예상: `clean`

---

### Task 9: 수동 절차 — 병합, 워크포워드, 잠금, 승인, 홀드아웃 1회 (컨트롤러와 사용자만, 서브에이전트 금지)

이 작업은 실제 네트워크와 실제 데이터를 쓰고, 되돌릴 수 없는 1회 실행을 포함한다. 각 단계의 결과를 사용자에게 보고하고, 표시된 곳에서는 사용자의 명시적 승인을 받은 뒤에만 진행한다(명세 10절).

- [ ] **Step 1: PR과 병합(사용자)**

```bash
git push -u origin feat/strategy-c
gh pr create --base master --title "Plan 5b: strategy C research (B2 one timeframe up)" --body-file - <<'BODY'
Strategy C (Plan 5b), part 1: B2's rules on 4h structure and 1h confirmation for BTC and ETH, executed by strategy B's 15m engine; strategy B's 15m cache files under C's own 2024 guard; walk-forward with A's rule as B implemented it; one-shot holdout lock. Spec and plan were committed before any strategy-C result. Strategy A's fingerprint e0435223… is unchanged; no strategy-A or strategy-B file, workflow, lock or ledger is modified.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
```

`ci` 체크(A 지문·정적 검사)가 통과해야 병합한다.

- [ ] **Step 2: 연구 브랜치와 15분봉 확인(네트워크)**

```bash
git switch master && git pull --ff-only && git switch -c chore/stratc-holdout
uv run ewbot stratc fetch
```

B의 캐시(`data/cache/BTC-USD_15m.parquet`, `ETH-USD_15m.parquet`)가 이미 있으면 새로 받을 것이 거의 없다(없으면 자산마다 약 820번의 공개 API 요청). 출력의 자산별 `bars before 2024-01-01`과 `missing`을 기록한다. 잠금 전이므로 2024 이후 봉은 받지 않는다(`fetch window: stops at 2024-01-01 (no lock)`).

- [ ] **Step 3: 워크포워드 실행과 보고(2024 데이터 없음)**

```bash
uv run ewbot stratc walkforward
```

사용자에게 보고한다: 구간별 선택 조합, 이어 붙인 표본 외 성과(`WF-OOS` 줄), 조합표(`reports/stratc-walkforward.json`의 `combo_table`: 조합별 Calmar·샤프·거래 수·자산별 거래 수·수익률), `chosen_final`, `chosen_final_fallback`, `proceed`, 그리고 두 자산 합계 월 거래 수(명세 6.7의 결정 사항 추정 월 1.5~3건, 그리고 같은 절의 주의 — 척도만 늘린 규칙이라 B2의 약 1/4인 월 0.6건 안팎일 수 있고 그러면 홀드아웃 기준 3(50건)에서 탈락할 가능성이 크다는 거친 추정 — 과 비교; 정보로만, 규칙은 바꾸지 않는다). **`proceed`가 거짓이면 C를 여기서 영구히 끝낸다**(명세 6.3, 1회 시도): 잠금을 만들지 않고, 다른 변형을 시도하지 않는다. 이때 사용자에게 명세 끝에 종료 기록을 더하는 문서 커밋을 제안한다.

- [ ] **Step 4: 잠금 작성과 즉시 커밋**

```bash
git status --porcelain --untracked-files=no
uv run ewbot stratc holdout lock --params-from reports/stratc-walkforward.json
git add holdout-c.lock
git commit -m "chore(stratc): lock the strategy-C holdout" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git rev-parse HEAD
```

첫 줄의 출력이 비어 있어야 한다(더러운 작업 트리는 거부된다). 마지막 줄의 커밋(잠금 커밋)을 사용자 보고에 적는다. 잠금의 `last_bar_ts`(지금 시각의 마지막 완성 시간의 마지막 15분봉)가 홀드아웃의 끝이다.

- [ ] **Step 5: 사용자 승인(필수)**

사용자에게 잠금 내용(조합, `last_bar_ts`, 자산별 데이터 해시, 무작위 대조군 1,000회)과 합격 기준 4개(명세 6.5)를 보여 주고, 홀드아웃을 **1회** 실행해도 되는지 명시적 승인을 받는다. 승인이 없으면 멈춘다.

- [ ] **Step 6: 홀드아웃 1회 실행과 즉시 커밋(네트워크)**

```bash
git status --porcelain --untracked-files=no
test "$(git rev-parse HEAD)" = "$(git log -1 --format=%H -- holdout-c.lock)" && echo "HEAD is the lock commit"
uv run --group live pytest tests/paper/test_fingerprint.py tests/paper/test_static.py tests/stratc/test_isolation.py tests/stratb/test_isolation.py -q
uv run ewbot stratc holdout run --confirm
git add holdout-c.lock
git commit -m "chore(stratc): record the strategy-C holdout run" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

실행 전 확인: 첫 줄의 출력이 비어 있고, 둘째 줄이 `HEAD is the lock commit`을 출력하고, 보호 테스트가 모두 통과해야 한다. 하나라도 어긋나면 실행하지 않고 보고한다. 실행이 끝나면 **바로** `first_run`을 커밋한다(커밋 전에 그 파일을 되돌리거나 지우지 않는다). `data/cache/`는 지우지 않는다(홀드아웃 구간의 봉은 이 캐시에만 있다). 출력의 기준 4개, 자산별 거래 수, `PASSED`/`FAILED`를 그대로 사용자에게 보고한다. 다시 실행하거나, 기준을 낮추거나, 다른 조합을 시험하지 않는다.

- [ ] **Step 7: 결과 병합(사용자)과 갈림길**

```bash
git push -u origin chore/stratc-holdout
gh pr create --base master --title "chore(stratc): strategy-C holdout lock and its one run" --body "holdout-c.lock with first_run. 🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

`FAILED`여도 잠금은 영구 기록으로 병합하고 **C를 영구히 끝낸다**(2부를 하지 않는다; 명세 끝에 종료 기록을 더하는 문서 커밋을 제안한다). `PASSED`이고 병합되었을 때만 2부로 간다.

---

# 2부 — 모의매매 (Task 9의 홀드아웃이 `PASSED`이고 그 잠금이 `master`에 병합된 뒤에만)

2부를 시작하기 전에(컨트롤러):

```bash
git switch master && git pull --ff-only
git log -1 --format=%H -- holdout-c.lock
uv run python -c "import json; fr = json.load(open('holdout-c.lock'))['first_run']; print(fr['criteria']['passed'])"
git switch -c feat/strategy-c-paper
```

둘째 줄이 잠금의 결과 커밋을 가리키고 셋째 줄이 `True`여야 한다. 2부는 `src/ewbot/stratc/`를 바꾸지 않는다(Global Constraints).

### Task 10: 모의매매 어댑터 — 장부, 판단, 메시지

**Files:**
- Create: `src/ewbot/paperc/__init__.py`
- Create: `src/ewbot/paperc/adapters.py`
- Test: `tests/paperc/__init__.py`
- Test: `tests/paperc/test_adapters.py`

**Interfaces:**
- Consumes: 1부의 `run_engine_c`, `SETUP`, `CConfig`, `build_frame_c`. `ewbot.paper.ledger`: `Ledger`, `current_epoch`, `ensure_epoch`, `replace_epoch_trades`, `load_ledger`, `save_ledger`. `ewbot.paper.notify`: `HEAD`(`"[ewbot paper]"`), `close_text`, `stop_move_text`, `failure_text`, `make_event`. `ewbot.paper.decisions.verify_decisions`. `ewbot.paperb.decisions`: `build_decisions_b(res) -> list[dict]`, `key_of(trade) -> int`, `split_result_b(res)`, `open_positions_b(still, res, epoch_start)`. `ewbot.paperb.notify.position_lines_b(open_position) -> list[str]`.
- Produces: `ewbot.paperc.adapters`: `PAPER_C_REQUIRED = 30`, `HEAD_C = "[ewbot paper-C]"`, `holdout_summary_c(lock) -> dict`(`n_trades` = `{"C": …}`), `new_ledger_c(lock) -> Ledger`(구간 1의 이유 `"baseline: holdout-c.lock"`), `paper_count_c(ledger) -> dict`(`epoch_id`, `eligible`, `paper`, `required`), `build_decisions_c(res) -> list[dict]`, `c_head(text) -> str`, `entry_text_c(trade) -> str`, `close_text_c(trade, n_at, required, counted) -> str`, `holdout_trades_text_c(expected, got) -> str`, `daily_text_c(ny_date, lines) -> str`, `decision_events_c(fresh, trades, open_keys, epoch_start, count, cur_closed) -> list[dict]`.

해석 9대로 A의 장부 스키마와 B의 두 자산 판단 변환을 import하고, C의 것(C 잠금 묶기, SIGNAL의 C 표기, `[ewbot paper-C]` 메시지)만 쓴다. 테스트는 장부, 카운트(새 C 지문이면 리셋), 판단(B와 같고 표기만 C, A의 대조 규칙으로 깨끗이 재현되고 바뀐 판단은 UNEXPLAINED), 같은 봉에 진입한 두 자산의 서로 다른 판단·메시지, 접두어, 진입·본절 이동·청산 이벤트를 확인한다. 프레임의 추세는 테스트에서 고정해 신호가 걸러지지 않게 한다.

- [ ] **Step 1: 실패하는 테스트 작성**

```bash
mkdir -p tests/paperc
```

**파일 `tests/paperc/__init__.py`** (새 파일, 전체 내용)

```python
"""Tests for ewbot.paperc (Plan 5b)."""
```

**파일 `tests/paperc/test_adapters.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd
import pytest

from ewbot.paper import notify as a_notify
from ewbot.paper.decisions import verify_decisions
from ewbot.paper.ledger import Ledger, ensure_epoch, load_ledger, replace_epoch_trades, save_ledger
from ewbot.paperb.decisions import build_decisions_b, key_of, open_positions_b, split_result_b
from ewbot.paperb.notify import position_lines_b
from ewbot.paperc.adapters import (
    HEAD_C,
    PAPER_C_REQUIRED,
    build_decisions_c,
    c_head,
    close_text_c,
    daily_text_c,
    decision_events_c,
    entry_text_c,
    holdout_summary_c,
    new_ledger_c,
    paper_count_c,
)
from ewbot.stratb.signals import SignalB
from ewbot.stratc.config import CConfig
from ewbot.stratc.engine import run_engine_c
from ewbot.stratc.signals import build_frame_c

T0 = 1_700_006_400
M15 = 900
LOCK = {
    "sha256": "ab" * 32, "params": [2.0, [0.382, 0.618], 1.0, 1.272, "breakeven"], "holdout_start": 1_704_067_200,
    "replay_anchor": 1_688_169_600, "last_bar_ts": 1_727_398_800 - 900, "code_sha256": "cd" * 32, "git_head": "f00",
    "first_run": {"data": {"sha256": "ef" * 32}, "metrics": {"C": {"n_trades": 56}}, "criteria": {"passed": True}},
}
FLAT = [(20_000.0, 20_005.0, 19_995.0, 20_000.0)]
TP1_THEN_BE = FLAT * 5 + [(20_000.0, 20_150.0, 19_950.0, 20_120.0), (20_120.0, 20_120.0, 19_850.0, 19_860.0)] + FLAT * 9


def _frame(rows, trend=1):
    f = build_frame_c(pd.DataFrame([(T0 + k * M15, o, h, lo, c, 1.0) for k, (o, h, lo, c) in enumerate(rows)],
                                   columns=["ts", "open", "high", "low", "close", "volume"]))
    return dataclasses.replace(f, trend=np.full(len(f), trend, dtype=np.int64))


def _sig(f, d=1, scale=1.0):
    """A signal at the close of the first hour (15m bar 3): the entry fills at the next hour's open (15m bar 4)."""
    return SignalB(i=3, ts=int(f.f15.ts[3]), direction=d, entry_ref=20_000.0 * scale,
                   stop=(20_000.0 - d * 100.0) * scale, tp1=(20_000.0 + d * 100.0) * scale,
                   tp2=(20_000.0 + d * 300.0) * scale, reason="long 1h div 1/2 break 3.00")


def _run(rows):
    f = _frame(rows)
    return f, run_engine_c({"BTC": f}, {"BTC": [_sig(f)]}, CConfig())


def test_the_ledger_is_strategy_a_schema_bound_to_the_c_lock(tmp_path):
    led = new_ledger_c(LOCK)
    assert led.baseline == {"fingerprint": "cd" * 32, "commit": "f00"}
    assert led.epochs == [{"id": 1, "fingerprint": "cd" * 32, "started_ts": LOCK["last_bar_ts"],
                           "reason": "baseline: holdout-c.lock", "through_ts": LOCK["last_bar_ts"]}]
    assert holdout_summary_c(LOCK)["n_trades"] == {"C": 56} and holdout_summary_c(LOCK)["passed"] is True
    save_ledger(tmp_path, led)
    assert isinstance(load_ledger(tmp_path), Ledger) and load_ledger(tmp_path).holdout == led.holdout


def test_the_paper_count_needs_30_and_resets_with_a_new_c_fingerprint():
    led = new_ledger_c(LOCK)
    _, res = _run(TP1_THEN_BE)
    replace_epoch_trades(led, 1, [res.trades[0]])
    assert paper_count_c(led) == {"epoch_id": 1, "eligible": True, "paper": 1, "required": PAPER_C_REQUIRED}
    ensure_epoch(led, "99" * 32, LOCK["last_bar_ts"] + 3600)
    assert paper_count_c(led) == {"epoch_id": 2, "eligible": False, "paper": 0, "required": 30}


def test_decisions_are_b_s_records_with_c_s_label_and_replay_cleanly():
    f, res = _run(TP1_THEN_BE)
    dec = build_decisions_c(res)
    assert [d["kind"] for d in dec] == ["SIGNAL", "ENTRY", "TP1", "BREAKEVEN", "STOP"]
    assert {d["setup"] for d in dec} == {"C-BTC"} and build_decisions_b(res)[0]["setup"] == "B-BTC"
    assert [{**d, "setup": "x"} for d in dec] == [{**d, "setup": "x"} for d in build_decisions_b(res)]
    sig, entry, tp1, be, stop = dec
    t3, t4 = int(f.f15.ts[3]), int(f.f15.ts[4])
    assert (sig["ts"], sig["contracts"], sig["levels"], sig["trade_key"]) == (t3, 41, [19_900.0, 20_100.0, 20_300.0], t3)
    assert entry["id"] == f"ENTRY:{t4}:{t4}" and entry["reason"] == "C-BTC" and t4 % 3600 == 0
    assert (be["contracts"], be["levels"], be["price"]) == (21, [19_900.0], entry["price"])
    assert stop["reason"] == "STOP" and tp1["contracts"] == 20
    epoch = {"id": 1, "started_ts": 0, "through_ts": int(f.f15.ts[-1])}
    rec = [{**d, "epoch_id": 1} for d in dec]
    assert verify_decisions(rec, dec, epoch, lambda d: False, lambda d: False) == []
    changed = [dict(d) for d in dec]
    changed[2]["price"] += 1.0
    (disc,) = verify_decisions(rec, changed, epoch, lambda d: False, lambda d: False)
    assert (disc["type"], disc["cause"]) == ("CHANGED", "UNEXPLAINED")


def test_two_assets_entering_on_the_same_bar_have_distinct_decisions_and_messages():
    f = _frame(FLAT * 16)
    g = _frame([tuple(x / 10 for x in FLAT[0])] * 16, trend=-1)
    res = run_engine_c({"BTC": f, "ETH": g}, {"BTC": [_sig(f)], "ETH": [_sig(g, d=-1, scale=0.1)]}, CConfig())
    t4 = int(f.f15.ts[4])
    dec = build_decisions_c(res)
    assert [(d["id"], d["setup"], d["direction"]) for d in dec] == [
        (f"SIGNAL:{t4 - M15}:{t4 - M15}", "C-BTC", 1), (f"SIGNAL:{t4 - M15}:{t4 - M15 + 1}", "C-ETH", -1),
        (f"ENTRY:{t4}:{t4}", "C-BTC", 1), (f"ENTRY:{t4}:{t4 + 1}", "C-ETH", -1)]
    closed, still = split_result_b(res)
    pos = open_positions_b(still, res, epoch_start=0)
    assert closed == [] and list(pos) == ["BTC", "ETH"]
    assert [x.split(" · ")[0] for x in position_lines_b(pos)] == ["포지션: C-BTC 롱 41/41계약", "포지션: C-ETH 숏 41/41계약"]
    events = decision_events_c(dec, {key_of(t): t for t in res.trades}, {key_of(t) for t in still}, 0, {"required": 30}, [])
    assert [e["id"] for e in events] == [f"ENTRY:{t4}:{t4}", f"ENTRY:{t4}:{t4 + 1}"]
    assert "C-BTC 롱" in events[0]["text"] and "C-ETH 숏" in events[1]["text"]


def test_messages_carry_the_paper_c_prefix():
    _, res = _run(TP1_THEN_BE)
    t = res.trades[0]
    assert entry_text_c(t).startswith(f"{HEAD_C} 신규 진입\nC-BTC 롱 · ") and "근거: long 1h div 1/2 break 3.00" in entry_text_c(t)
    assert close_text_c(t, 3, 30, True).startswith(f"{HEAD_C} 청산 · STOP") and "3/30" in close_text_c(t, 3, 30, True)
    assert c_head(a_notify.failure_text("data", "x", None)) == f"{HEAD_C} 실패 · 단계 data\nx"
    assert daily_text_c("2024-09-30", ["a", "b"]) == f"{HEAD_C} 일일 요약 2024-09-30 (뉴욕)\na\nb"
    with pytest.raises(ValueError):
        c_head("[LIVE] x")


def test_decision_events_announce_entries_breakeven_moves_and_closes():
    _, res = _run(TP1_THEN_BE)
    dec = build_decisions_c(res)
    trades = {key_of(t): t for t in res.trades}
    events = decision_events_c(dec, trades, set(), epoch_start=0, count={"required": 30}, cur_closed=list(res.trades))
    assert [e["kind"] for e in events] == ["ENTRY", "BREAKEVEN", "CLOSE"]
    assert all(e["text"].startswith(HEAD_C) for e in events) and "1/30" in events[-1]["text"]
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/paperc/test_adapters.py -q
```

예상: `No module named 'ewbot.paperc'`
예상: `1 error`

- [ ] **Step 3: 최소 구현**

**파일 `src/ewbot/paperc/__init__.py`** (새 파일, 전체 내용)

```python
"""Paper trading of strategy C (Plan 5b spec 7): an hourly deterministic recompute of BTC and ETH on the `paper-c`
branch.

Outside the C fingerprint (stratc/ only) and outside strategy A's and B's runtimes (paper/ and paperb/ are imported,
never changed)."""
```

**파일 `src/ewbot/paperc/adapters.py`** (새 파일, 전체 내용)

```python
"""Paper-C adapters (Plan 5b spec 7): the ledger bound to holdout-c.lock, the decisions of one recompute and the
Telegram messages - strategy A's ledger schema, decision format and delivery (ewbot.paper), and strategy B's
two-asset decision mapping (ewbot.paperb.decisions), imported unchanged.

Ledger: epoch 1 starts at the lock's last bar with the lock's C fingerprint; another C fingerprint opens a new epoch
(A's ensure_epoch) and the count starts again. Paper trades are the current epoch's closed trades of both assets that
entered after its start (A's paper_trades); 30 are needed, BTC and ETH together (spec 7.1). Holdout trades never
count. open_position holds {asset: position} (or None).

Decisions: B's build_decisions_b on C's run (C's engine is B's): SIGNAL, ENTRY, TP1, TP2, STOP, EXIT (TIME, BREAKER),
BREAKEVEN; the trade key is the entry ts (the signal ts for SIGNAL) plus the asset's index in ASSETS. B names a
SIGNAL's setup "B-<asset>"; build_decisions_c writes C's "C-<asset>" (fills already carry the trade's C label).
A SIGNAL is dated by its 15m bar (the hour's last 15m bar); A's known_at() dates it one hour later, which only ever
delays its APPEARED check.

Messages: A's formats with the prefix `[ewbot paper-C]`; the entry message is C's own. Every trade and position line
names its asset through the trade's setup ("C-BTC", "C-ETH")."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from ewbot.engine.loop import Trade
from ewbot.paper import notify as a_notify
from ewbot.paper.ledger import Ledger, current_epoch
from ewbot.paper.notify import make_event
from ewbot.paperb.decisions import build_decisions_b, key_of
from ewbot.stratb.engine import BResult
from ewbot.stratc.engine import SETUP

PAPER_C_REQUIRED = 30
HEAD_C = "[ewbot paper-C]"


# ----------------------------------------------------------------------------- ledger


def holdout_summary_c(lock: dict[str, Any]) -> dict[str, Any]:
    """The part of holdout-c.lock the ledger is bound to (the precheck validated the keys)."""
    fr = lock["first_run"]
    return {
        "lock_sha256": lock["sha256"],
        "params": lock["params"],
        "holdout_start": int(lock["holdout_start"]),
        "replay_anchor": int(lock["replay_anchor"]),
        "last_bar_ts": int(lock["last_bar_ts"]),
        "data_sha256": fr["data"]["sha256"],
        "n_trades": {"C": int(fr["metrics"]["C"]["n_trades"])},
        "passed": bool(fr["criteria"]["passed"]),
    }


def new_ledger_c(lock: dict[str, Any]) -> Ledger:
    hs = holdout_summary_c(lock)
    start = hs["last_bar_ts"]
    fp = lock["code_sha256"]
    epoch = {"id": 1, "fingerprint": fp, "started_ts": start, "reason": "baseline: holdout-c.lock", "through_ts": start}
    return Ledger(baseline={"fingerprint": fp, "commit": str(lock.get("git_head") or "holdout-c.lock")}, holdout=hs,
                  epochs=[epoch])


def paper_count_c(ledger: Ledger) -> dict[str, Any]:
    cur = current_epoch(ledger)
    n = sum(1 for t in ledger.trades if t["epoch_id"] == cur["id"])
    return {"epoch_id": int(cur["id"]), "eligible": cur["fingerprint"] == ledger.baseline["fingerprint"], "paper": n,
            "required": PAPER_C_REQUIRED}


# ----------------------------------------------------------------------------- decisions


def build_decisions_c(res: BResult) -> list[dict[str, Any]]:
    """B's decisions of the run with C's setup label on every one ("B-BTC" -> "C-BTC" on the SIGNALs)."""
    out = build_decisions_b(res)
    for d in out:
        if d["setup"].startswith("B-"):
            d["setup"] = f"{SETUP}-{d['setup'][2:]}"
    return out


# ----------------------------------------------------------------------------- messages


def c_head(text: str) -> str:
    """A's message with the paper-C prefix in place of `[ewbot paper]`."""
    if not text.startswith(a_notify.HEAD):
        raise ValueError(f"not a paper message: {text[:40]!r}")
    return HEAD_C + text[len(a_notify.HEAD):]


def _utc(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d %H:%M UTC")


def entry_text_c(t: Trade) -> str:
    snap = t.snapshot or {}
    return "\n".join([
        f"{HEAD_C} 신규 진입",
        f"{t.setup} {'롱' if t.direction > 0 else '숏'} · {_utc(t.entry_ts)}",
        f"진입 {t.entry_price:,.2f} · 손절 {t.stop0:,.2f} · {t.contracts}계약",
        f"TP1 {t.tp1:,.2f} (절반) · TP2 {float(t.tp2 or 0.0):,.2f}",
        f"근거: {snap.get('reason', '-')}",
    ])


def close_text_c(t: Trade, n_at: int, required: int, counted: bool) -> str:
    return c_head(a_notify.close_text(t, {"total": n_at, "required": required}, counted))


def holdout_trades_text_c(expected: int, got: int) -> str:
    return "\n".join([
        f"{HEAD_C} 경고: 홀드아웃 재현 불일치",
        f"재계산한 홀드아웃 구간 C 거래 {got}건 · 잠금 파일 {expected}건",
        "실행은 계속함 (모의 카운트는 잠금 이후 진입만 셈, 재현성 손상 기록)",
    ])


def daily_text_c(ny_date: str, lines: Sequence[str]) -> str:
    return "\n".join([f"{HEAD_C} 일일 요약 {ny_date} (뉴욕)", *lines])


def decision_events_c(
    fresh: Sequence[dict[str, Any]],
    trades: Mapping[int, Trade],
    open_keys: set[int],
    epoch_start: int,
    count: Mapping[str, Any],
    cur_closed: Sequence[Trade],
) -> list[dict[str, Any]]:
    """ENTRY, BREAKEVEN and CLOSE events for the decisions first recorded in this run (A's rules). `trades` maps the
    trade key (paperb.decisions.key_of) to every trade of the run; open_keys are the open positions'. A close's n/30
    counts the epoch's closed trades up to it in (exit ts, trade key) order."""
    out: list[dict[str, Any]] = []
    for d in fresh:
        t = trades.get(d["trade_key"])
        if t is None:
            continue  # SIGNAL: its ENTRY fill carries the message
        if d["kind"] == "ENTRY":
            out.append(make_event("ENTRY", d["ts"], d["trade_key"], entry_text_c(t)))
        elif d["kind"] == "BREAKEVEN":
            out.append(make_event("BREAKEVEN", d["ts"], d["trade_key"], c_head(a_notify.stop_move_text(d, t))))
        elif d["kind"] in ("STOP", "TP1", "TP2", "EXIT") and key_of(t) not in open_keys and t.exit_ts == d["ts"]:
            counted = t.entry_ts > int(epoch_start)
            n_at = sum(1 for x in cur_closed if (x.exit_ts, key_of(x)) <= (t.exit_ts, key_of(t)))
            out.append(make_event("CLOSE", d["ts"], d["trade_key"], close_text_c(t, n_at, count["required"], counted)))
    return out
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/paperc/test_adapters.py -q
```

예상: `6 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/paperc/__init__.py src/ewbot/paperc/adapters.py tests/paperc/__init__.py tests/paperc/test_adapters.py
git commit -m "feat(paperc): paper-C ledger bound to holdout-c.lock, B's two-asset decisions with C labels, [ewbot paper-C] messages" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: 모의매매 실행기

**Files:**
- Create: `src/ewbot/paperc/runner.py`
- Test: `tests/paperc/helpers.py`
- Test: `tests/paperc/conftest.py`
- Test: `tests/paperc/test_runner.py`

**Interfaces:**
- Consumes: Task 10, 1부(`config_for`, `REPLAY_ANCHOR`, `DEFAULT_LOCK_PATH`, `load_15m_c`, `update_caches_c`, `stratc_fingerprint`, `deps_fingerprint`, `HoldoutCError`, `read_lock_c`, `committed_lock_c`, `lock_mismatch_c`, `create_lock_c`, `run_holdout_c`, `backtest_c`, `wf_provenance_c`, `load_caches_c`, `tests.stratc.helpers.world_dfs_c`, `save_world`). `ewbot.paper.decisions`: `decisions_to_record`, `known_at`, `paper_trades`, `verify_decisions`. `ewbot.paper.environment`: `environment_digest`, `runtime_environment`. `ewbot.paper.ledger`: `RUNS_LOG`, `append_log_line`, … `ewbot.paper.notify`: `NY`, `Sender`, `daily_summary_due`, `data_hash_text`, `deliver`, `discrepancy_text`, `epoch_text`, `queue_events`. `ewbot.stratb.holdout.data_fingerprint_b`. `tests.paper.helpers`: `Clock`, `FakeSender`, `git`.
- Produces: `ewbot.paperc.runner`: `FETCH_ATTEMPTS = 3`, `DAILY_HOUR_NY = 9`, `class PaperCRunError(RuntimeError)`(`stage`, `message`), `default_fetch_c(cache_dir, lock_path) -> dict[str, Path]`, `@dataclass PaperCEnv(ledger_dir, lock_path=DEFAULT_LOCK_PATH, cache_dir=Path("data/cache"), fetch=default_fetch_c, sender=None, clock=…, fingerprint=stratc_fingerprint, deps=deps_fingerprint, sleep=time.sleep, fetch_backoff_s=10.0, environment=runtime_environment)`, `@dataclass(frozen=True) RunOutcomeC(run_id, last_bar_ts, epoch_id, new_decisions, new_discrepancies, events, sent, pending, notes, count)`, `precheck_lock_c(lock_path, deps=deps_fingerprint) -> dict`, `status_lines_c(ledger, count) -> list[str]`, `run_paper_c(env) -> RunOutcomeC`.
  `tests/paperc/helpers.py`: `HS`, `LOCK_NOW`(2024-09-27 00:20:34 UTC), `LAST_BAR`, `WORLD_REPS = 56`, `HOLDOUT_TRADES = 56`, `COMBO`, `world()`, `build_passed_lock(tmp) -> str`, `commit_lock_c(repo, text) -> Path`, `class FakeFetchC`, `make_env(tmp_path, lock_text, now, source=None, **kw) -> (PaperCEnv, Clock, FakeSender)`; `tests/paperc/conftest.py`: 세션 픽스처 `lock_text`(진짜 합성 홀드아웃을 한 번 돌린 잠금).

B의 실행기 흐름을 C의 잠금·데이터·재계산·지문으로 옮긴다(해석 9). 테스트 세계는 홀드아웃 테스트와 같고(합격한 잠금, 홀드아웃 56건), 잠금 뒤 하루씩 25번 돌리면 두 자산의 모의 거래가 4건 넘게 쌓인다. 테스트는 사전 점검(없음·편집·미커밋·미실행·탈락·이 코드의 것이 아닌 설정·deps 변경), 첫 실행의 홀드아웃 재현과 C 장부만 씀, 하루 단위 실행의 카운트와 `[ewbot paper-C]` 알림(새 장부로 다시 계산해도 같은 판단), 두 자산이 모두 가진 마지막 봉, 바뀐 기록 판단(UNEXPLAINED), 새 C 지문(새 구간, 카운트 리셋), 홀드아웃 구간 데이터 수정(한 번 경고, DATA_REVISION), 수집 재시도, 뉴욕 9시 일일 요약을 확인한다.

- [ ] **Step 1: 실패하는 테스트 작성**

**파일 `tests/paperc/helpers.py`** (새 파일, 전체 내용)

```python
"""Shared helpers for tests/paperc: one synthetic two-asset world (2023-07-01 .. 2024-12-24, a BTC long and an ETH
short on the same bar every 232 hours: tests.stratc.helpers.world_dfs_c), a strategy-C lock whose holdout really ran
on it, a fake 15m fetcher of both assets. A's fake sender, clock and git helper (tests.paper.helpers) are reused. No
network, no real cache, no real lock."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ewbot.paperc.runner import PaperCEnv
from ewbot.stratb.data15 import cache_paths
from ewbot.stratc.data import REPLAY_ANCHOR, load_caches_c
from ewbot.stratc.holdout import create_lock_c, run_holdout_c
from ewbot.stratc.research import wf_provenance_c
from tests.paper.helpers import Clock, FakeSender, git
from tests.stratc.helpers import save_world, world_dfs_c

HS = 1_704_067_200
LOCK_NOW = HS + 270 * 86_400 + 1_234  # the lock's clock: last bar 2024-09-27 00:45 UTC
LAST_BAR = LOCK_NOW // 3600 * 3600 - 900
WORLD_REPS = 56
HOLDOUT_TRADES = 56  # 28 per asset
COMBO = [2.0, [0.382, 0.618], 1.0, 1.272, "breakeven"]
M15 = 900


def world() -> dict[str, pd.DataFrame]:
    return world_dfs_c(REPLAY_ANCHOR, WORLD_REPS)


def build_passed_lock(tmp: Path) -> str:
    """The text of holdout-c.lock after a real (synthetic) holdout run that passed."""
    repo, cache = tmp / "repo", tmp / "cache"
    repo.mkdir()
    cache.mkdir()
    git(repo, "init", "-q")
    save_world(cache, world())
    lp = repo / "holdout-c.lock"
    report = tmp / "wf.json"  # a walk-forward report that proceeds with COMBO, made on these bars and this code
    report.write_text(json.dumps({"chosen_final": COMBO, "chosen_final_fallback": False, "proceed": True,
                                  **wf_provenance_c(load_caches_c(cache))}), encoding="utf-8")
    create_lock_c(report, load_caches_c(cache), now_ts=LOCK_NOW, lock_path=lp, random_runs=2)
    git(repo, "add", lp.name)
    git(repo, "commit", "-q", "-m", "lock")
    run_holdout_c(confirm=True, cache_dir=cache, lock_path=lp)
    lock = json.loads(lp.read_text(encoding="utf-8"))
    assert lock["first_run"]["criteria"]["passed"] is True
    return lp.read_text(encoding="utf-8")


def commit_lock_c(repo: Path, text: str) -> Path:
    """Write <repo>/holdout-c.lock and commit it (initialising the repo if needed)."""
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        git(repo, "init", "-q")
    p = repo / "holdout-c.lock"
    p.write_text(text, encoding="utf-8")
    git(repo, "add", p.name)
    if subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"]).returncode != 0:
        git(repo, "commit", "-q", "-m", "lock")
    return p


class FakeFetchC:
    """Stands in for update_caches_c: writes each asset's bars of `source` closed by clock() into its cache."""

    def __init__(self, source: dict[str, pd.DataFrame] | Callable[[], dict[str, pd.DataFrame]],
                 clock: Callable[[], int], fail: int = 0):
        self.source, self.clock, self.fail, self.calls = source, clock, fail, 0

    def __call__(self, cache_dir: Path, lock_path: Path) -> dict[str, Path]:
        self.calls += 1
        if self.fail > 0:
            self.fail -= 1
            raise ConnectionError("coinbase down")
        dfs = self.source() if callable(self.source) else self.source
        paths = cache_paths(cache_dir)
        for a, p in paths.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            df = dfs[a]
            df[df["ts"] < int(self.clock()) // M15 * M15].reset_index(drop=True).to_parquet(p, index=False)
        return paths


def make_env(tmp_path: Path, lock_text: str, now: int, source: Any = None, **kw: Any) -> tuple[PaperCEnv, Clock, FakeSender]:
    clock = Clock(now)
    sender = FakeSender()
    env = PaperCEnv(
        ledger_dir=tmp_path / "ledger", lock_path=commit_lock_c(tmp_path / "repo", lock_text),
        cache_dir=tmp_path / "cache", fetch=FakeFetchC(world() if source is None else source, clock),
        sender=sender, clock=clock, sleep=lambda s: None, environment=lambda: {"python": "3.12.13"},
    )
    for k, v in kw.items():
        setattr(env, k, v)
    return env, clock, sender
```

**파일 `tests/paperc/conftest.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import pytest

from tests.paperc.helpers import build_passed_lock


@pytest.fixture(scope="session")
def lock_text(tmp_path_factory) -> str:
    """holdout-c.lock after one real synthetic holdout run (computed once per session)."""
    return build_passed_lock(tmp_path_factory.mktemp("paperc-lock"))
```

**파일 `tests/paperc/test_runner.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import json
from datetime import datetime

import pytest

from ewbot.paper.decisions import known_at
from ewbot.paper.ledger import load_ledger
from ewbot.paper.notify import NY
from ewbot.paperc.adapters import HEAD_C
from ewbot.paperc.runner import PaperCRunError, precheck_lock_c, run_paper_c
from ewbot.stratc.data import lock_digest_c
from ewbot.stratc.fingerprint import deps_fingerprint
from tests.paper.helpers import git
from tests.paperc.helpers import HOLDOUT_TRADES, LAST_BAR, FakeFetchC, commit_lock_c, make_env, world

H1 = 3600


def _ny(y, m, d, h, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=NY).timestamp())


def test_the_precheck_needs_the_committed_passed_lock_of_this_code(tmp_path, lock_text):
    with pytest.raises(PaperCRunError, match="missing"):
        precheck_lock_c(tmp_path / "x" / "holdout-c.lock")
    p = commit_lock_c(tmp_path / "a", lock_text)
    assert precheck_lock_c(p)["first_run"]["criteria"]["passed"] is True
    lock = json.loads(lock_text)
    p.write_text(json.dumps({**lock, "note": "edited"}), encoding="utf-8")
    with pytest.raises(PaperCRunError, match="differs from the committed"):
        precheck_lock_c(p)
    (tmp_path / "b").mkdir()
    git(tmp_path / "b", "init", "-q")
    (tmp_path / "b" / "holdout-c.lock").write_text(lock_text, encoding="utf-8")
    with pytest.raises(PaperCRunError, match="not committed"):
        precheck_lock_c(tmp_path / "b" / "holdout-c.lock")
    with pytest.raises(PaperCRunError, match="has not run"):
        precheck_lock_c(commit_lock_c(tmp_path / "c", json.dumps({**lock, "first_run": None})))
    failed = json.loads(lock_text)
    failed["first_run"]["criteria"]["passed"] = False
    with pytest.raises(PaperCRunError, match="strategy C ended"):
        precheck_lock_c(commit_lock_c(tmp_path / "d", json.dumps(failed)))
    other = json.loads(lock_text)
    other["config"]["params"]["time_stop_hours"] = 48
    other["sha256"] = lock_digest_c(other)
    with pytest.raises(PaperCRunError, match=r"\['config'\] are not this code's"):
        precheck_lock_c(commit_lock_c(tmp_path / "e", json.dumps(other)))
    assert lock["deps_sha256"] == deps_fingerprint()
    with pytest.raises(PaperCRunError, match="modules strategy C imports"):
        precheck_lock_c(commit_lock_c(tmp_path / "g", lock_text), deps=lambda: "0" * 64)
    env, _, _ = make_env(tmp_path / "f", lock_text, LAST_BAR + 900 + 60, deps=lambda: "0" * 64)
    with pytest.raises(PaperCRunError, match="modules strategy C imports") as exc:
        run_paper_c(env)
    assert exc.value.stage == "precheck" and not env.ledger_dir.exists()


def test_the_first_run_reproduces_the_holdout_and_writes_only_the_c_ledger(tmp_path, lock_text):
    env, _, _ = make_env(tmp_path, lock_text, LAST_BAR + 900 + H1 + 60)
    out = run_paper_c(env)
    led = load_ledger(env.ledger_dir)
    assert (out.epoch_id, out.last_bar_ts, out.notes) == (1, LAST_BAR + H1, [])
    assert led.runs[0]["holdout_trades_recomputed"] == led.holdout["n_trades"]["C"] == HOLDOUT_TRADES
    assert led.runs[0]["warnings"] == [] and all(known_at(d) > LAST_BAR for d in led.decisions)
    assert sorted(p.name for p in env.ledger_dir.iterdir()) == ["ledger.json", "runs.log"]
    assert "paper=0/30" in (env.ledger_dir / "runs.log").read_text()


def test_daily_runs_count_paper_trades_and_notify_with_the_c_prefix(tmp_path, lock_text):
    env, clock, sender = make_env(tmp_path, lock_text, LAST_BAR + 900 + 60)
    for k in range(25):
        clock.now = LAST_BAR + 900 + 60 + k * 24 * H1
        out = run_paper_c(env)
        assert out.new_discrepancies == 0
    led = load_ledger(env.ledger_dir)
    assert out.count["paper"] >= 4 and all(t["entry_ts"] > LAST_BAR for t in led.trades)
    assert {t["setup"] for t in led.trades} == {"C-BTC", "C-ETH"}
    assert {d["setup"] for d in led.decisions} == {"C-BTC", "C-ETH"}
    for asset in ("C-BTC 롱", "C-ETH 숏"):
        assert any(x.startswith(f"{HEAD_C} 신규 진입") and asset in x for x in sender.texts)
        assert any(x.startswith(f"{HEAD_C} 청산") and asset in x for x in sender.texts)
    assert all(x.startswith(HEAD_C) for x in sender.texts) and led.discrepancies == []
    fresh, _, _ = make_env(tmp_path / "fresh", lock_text, clock.now)
    run_paper_c(fresh)
    assert {d["id"] for d in load_ledger(fresh.ledger_dir).decisions} == {d["id"] for d in led.decisions}


def test_a_run_ends_at_the_last_bar_both_assets_have(tmp_path, lock_text):
    now = LAST_BAR + 900 + 6 * H1 + 60
    newest = now // 900 * 900 - 900  # the newest closed bar (hh:45)
    full = world()
    late = {"BTC": full["BTC"], "ETH": full["ETH"][full["ETH"]["ts"] < newest]}  # ETH's newest bar is not out yet
    env, clock, _ = make_env(tmp_path, lock_text, now, source=late)
    out = run_paper_c(env)
    led = load_ledger(env.ledger_dir)
    assert out.last_bar_ts == led.runs[0]["last_bar_ts"] == newest - 900  # BTC's newest bar waits for ETH's
    assert all(d["ts"] <= newest - 900 for d in led.decisions) and "ETH" in out.notes[0]
    env.fetch = FakeFetchC(full, clock)
    clock.now += H1
    out = run_paper_c(env)
    assert out.last_bar_ts == newest + H1 and out.notes == [] and out.new_discrepancies == 0


def test_a_changed_recorded_decision_is_an_unexplained_discrepancy(tmp_path, lock_text):
    env, clock, sender = make_env(tmp_path, lock_text, LAST_BAR + 900 + 12 * 86_400)
    run_paper_c(env)
    raw = json.loads((env.ledger_dir / "ledger.json").read_text())
    assert raw["decisions"]
    raw["decisions"][0]["price"] += 1.0
    (env.ledger_dir / "ledger.json").write_text(json.dumps(raw))
    clock.now += H1
    out = run_paper_c(env)
    (disc,) = load_ledger(env.ledger_dir).discrepancies
    assert out.new_discrepancies == 1 and (disc["type"], disc["cause"]) == ("CHANGED", "UNEXPLAINED")
    assert sender.texts[-1].startswith(f"{HEAD_C} 경고: 결정 불일치 1건")


def test_a_new_c_fingerprint_opens_a_new_epoch_and_resets_the_count(tmp_path, lock_text):
    env, clock, sender = make_env(tmp_path, lock_text, LAST_BAR + 900 + 16 * 86_400)
    first = run_paper_c(env)
    assert first.count["paper"] >= 1
    env.fingerprint = lambda: "9" * 64
    clock.now += H1
    out = run_paper_c(env)
    assert out.epoch_id == 2 and out.count == {"epoch_id": 2, "eligible": False, "paper": 0, "required": 30}
    assert any(x.startswith(f"{HEAD_C} 경고: 전략 지문 변경 · 구간 2") for x in sender.texts)


def test_revised_holdout_bars_warn_once_and_changed_decisions_are_data_revisions(tmp_path, lock_text):
    env, clock, sender = make_env(tmp_path, lock_text, LAST_BAR + 900 + 12 * 86_400)
    run_paper_c(env)
    raw = json.loads((env.ledger_dir / "ledger.json").read_text())
    raw["decisions"][0]["price"] += 1.0  # a recorded decision that will not reappear unchanged
    (env.ledger_dir / "ledger.json").write_text(json.dumps(raw))
    dfs = world()
    eth = dfs["ETH"]
    eth.loc[(eth["ts"] - LAST_BAR).abs().idxmin() - 500, "close"] += 0.5  # an ETH bar inside the holdout window
    env.fetch = FakeFetchC(dfs, clock)
    for _ in range(2):
        clock.now += H1
        out = run_paper_c(env)
    led = load_ledger(env.ledger_dir)
    assert [r["holdout_data_ok"] for r in led.runs] == [True, False, False]
    assert out.notes and out.notes[0].startswith("holdout data hash ")
    assert raw["decisions"][0]["id"] in {d["decision_id"] for d in led.discrepancies}
    assert {d["cause"] for d in led.discrepancies} == {"DATA_REVISION"}
    warned = [x for x in sender.texts if "홀드아웃 구간 데이터 해시 불일치" in x]
    assert len(warned) == 1 and warned[0].startswith(f"{HEAD_C} 경고")


def test_candle_updates_are_retried_three_times(tmp_path, lock_text):
    env, clock, _ = make_env(tmp_path, lock_text, LAST_BAR + 900 + 60)
    env.fetch = FakeFetchC(world(), clock, fail=2)
    run_paper_c(env)
    env.fetch = FakeFetchC(world(), clock, fail=3)
    with pytest.raises(PaperCRunError) as exc:
        run_paper_c(env)
    assert exc.value.stage == "data" and env.fetch.calls == 3


def test_one_daily_summary_per_new_york_date_from_9_am(tmp_path, lock_text):
    env, clock, sender = make_env(tmp_path, lock_text, _ny(2024, 9, 30, 8, 30))
    run_paper_c(env)
    assert not any("일일 요약" in x for x in sender.texts)
    clock.now = _ny(2024, 9, 30, 9, 5)
    run_paper_c(env)
    (daily,) = [x for x in sender.texts if "일일 요약" in x]
    assert daily.startswith(f"{HEAD_C} 일일 요약 2024-09-30 (뉴욕)") and "모의 " in daily and "/30" in daily
    assert "1. 홀드아웃 합격: 충족" in daily and "3. 원인 불명 불일치 0건" in daily
    assert "실거래 전환 조건 (Plan 5b 명세 7.1)" in daily
    clock.now = _ny(2024, 9, 30, 10, 5)
    run_paper_c(env)
    assert len([x for x in sender.texts if "일일 요약" in x]) == 1
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/paperc/test_runner.py -q
```

예상: `No module named 'ewbot.paperc.runner'`

`tests/paperc/conftest.py`가 불러오는 도우미가 `ewbot.paperc.runner`를 import하므로 수집 전에 실패한다.

- [ ] **Step 3: 최소 구현**

**파일 `src/ewbot/paperc/runner.py`** (새 파일, 전체 내용)

```python
"""One `ewbot stratc paper run` (Plan 5b spec 7, 8), strategy B's paper runtime with C's lock, data and recompute:
precheck -> data -> fingerprint/epoch -> full recompute from REPLAY_ANCHOR -> replay check -> ledger and runs.log ->
Telegram. BTC and ETH run together in one runtime and ledger: one portfolio recompute, one count of 30 trades of both
assets, messages that name the asset. A run ends at the latest bar BOTH assets have: when one asset's newest bar is
not out yet, the other's newer bars wait for the next run, and the run records a warning.

Inputs are frozen by holdout-c.lock: the params and the whole run config come from it (the precheck refuses a lock
whose sha256 is not the digest of its fields, that is not committed unchanged, whose holdout has not run or failed -
strategy C ended -, whose keys, grid, config, holdout start or replay anchor are not this code's
(stratc.holdout.lock_mismatch_c), or whose deps_sha256 is not the hash of the modules stratc/ imports now - a changed
A or B module is refused like a lock problem, not a new epoch). The recompute starts at REPLAY_ANCHOR (2023-07-01)
and trades from HOLDOUT_START, exactly like the holdout run, so it reproduces the holdout and continues it; trades
entering after the lock's last bar are paper trades. When the holdout window's bars (both assets) no longer hash to
first_run.data.sha256 the run is NOT refused: like A and B it warns once per distinct hash (DATA_HASH event) and goes
on, and a recorded decision that changes because of it is classified DATA_REVISION by A's replay check. A different C
fingerprint opens a new epoch. Decisions, discrepancies, the environment record and Telegram delivery are strategy
A's (paper/ imported, never changed).

Every step runs inside a named stage: any failure raises PaperCRunError(stage); the CLI sends the failure alert.
Nothing is written before the write stage."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.paper.decisions import decisions_to_record, known_at, paper_trades, verify_decisions
from ewbot.paper.environment import environment_digest, runtime_environment
from ewbot.paper.ledger import (
    RUNS_LOG,
    Ledger,
    LedgerError,
    append_log_line,
    ensure_epoch,
    load_ledger,
    replace_epoch_trades,
    save_ledger,
)
from ewbot.paper.notify import (
    NY,
    Sender,
    daily_summary_due,
    data_hash_text,
    deliver,
    discrepancy_text,
    epoch_text,
    make_event,
    queue_events,
)
from ewbot.paperb.decisions import key_of, open_positions_b, split_result_b
from ewbot.paperb.notify import position_lines_b
from ewbot.paperc.adapters import (
    build_decisions_c,
    c_head,
    daily_text_c,
    decision_events_c,
    holdout_summary_c,
    holdout_trades_text_c,
    new_ledger_c,
    paper_count_c,
)
from ewbot.stratb.config import ASSETS, normalize_combo
from ewbot.stratb.holdout import data_fingerprint_b
from ewbot.stratc.config import config_for
from ewbot.stratc.data import DEFAULT_LOCK_PATH, REPLAY_ANCHOR, load_15m_c, update_caches_c
from ewbot.stratc.fingerprint import deps_fingerprint, stratc_fingerprint
from ewbot.stratc.holdout import HoldoutCError, committed_lock_c, lock_mismatch_c, read_lock_c
from ewbot.stratc.research import backtest_c
from ewbot.types import H1

FETCH_ATTEMPTS = 3
DAILY_HOUR_NY = 9


class PaperCRunError(RuntimeError):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"[{stage}] {message}")
        self.stage = stage
        self.message = message


def default_fetch_c(cache_dir: Path, lock_path: Path) -> dict[str, Path]:
    """Extend both 15m caches (an empty one starts at REPLAY_ANCHOR: the recompute needs nothing older)."""
    return update_caches_c(Path(cache_dir), lock_path=Path(lock_path), start_ts=REPLAY_ANCHOR)


def _wall_clock() -> int:
    return int(time.time())


@dataclass
class PaperCEnv:
    """Everything a run touches; tests inject fakes (no network, temp git repos)."""

    ledger_dir: Path
    lock_path: Path = DEFAULT_LOCK_PATH
    cache_dir: Path = Path("data/cache")
    fetch: Callable[[Path, Path], dict[str, Path]] = default_fetch_c
    sender: Sender | None = None
    clock: Callable[[], int] = _wall_clock
    fingerprint: Callable[[], str] = stratc_fingerprint
    deps: Callable[[], str] = deps_fingerprint
    sleep: Callable[[float], None] = time.sleep
    fetch_backoff_s: float = 10.0
    environment: Callable[[], dict[str, Any]] = runtime_environment


@dataclass(frozen=True)
class RunOutcomeC:
    run_id: str
    last_bar_ts: int
    epoch_id: int
    new_decisions: int
    new_discrepancies: int
    events: list[str]
    sent: list[str]
    pending: int
    notes: list[str]
    count: dict[str, Any]


@contextmanager
def _stage(name: str, *, detail: bool = True) -> Iterator[None]:
    try:
        yield
    except PaperCRunError:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure is reported with its stage
        if detail:
            raise PaperCRunError(name, f"{type(exc).__name__}: {exc}") from exc
        raise PaperCRunError(name, f"{type(exc).__name__} (details withheld: may contain the token)") from None


def precheck_lock_c(lock_path: Path, deps: Callable[[], str] = deps_fingerprint) -> dict[str, Any]:
    """The committed, passed strategy-C lock of this code: its config is this code's config for its params, and the
    modules stratc/ imports still hash to its deps_sha256."""
    try:
        lock = read_lock_c(lock_path)
        if committed_lock_c(lock_path) != lock:
            raise PaperCRunError("precheck", f"{lock_path} differs from the committed version")
    except HoldoutCError as exc:
        raise PaperCRunError("precheck", str(exc)) from exc
    fr = lock.get("first_run")
    if not isinstance(fr, dict) or "criteria" not in fr or "data" not in fr:
        raise PaperCRunError("precheck", "the strategy-C holdout has not run: paper trading starts after it passes")
    if not fr["criteria"].get("passed"):
        raise PaperCRunError("precheck", "the strategy-C holdout failed (spec 6.5): strategy C ended, no paper trading")
    stale = lock_mismatch_c(lock)
    if stale:
        raise PaperCRunError("precheck", f"the lock's {stale} are not this code's (keys, grid, config_for(params), "
                                         "holdout start, replay anchor)")
    if deps() != lock.get("deps_sha256"):
        raise PaperCRunError("precheck", "the strategy-A/B modules strategy C imports changed since the lock "
                                         "(deps_sha256 differs): refusing")
    return lock


def _open_ledger(ledger_dir: Path, lock: dict[str, Any]) -> Ledger:
    ledger_dir.mkdir(parents=True, exist_ok=True)
    try:
        ledger = load_ledger(ledger_dir)
    except LedgerError as exc:
        raise PaperCRunError("ledger", str(exc)) from exc
    if ledger is None:
        return new_ledger_c(lock)
    bound = holdout_summary_c(lock)
    diff = sorted(k for k in bound.keys() | ledger.holdout.keys() if ledger.holdout.get(k) != bound.get(k))
    if diff:
        raise PaperCRunError("precheck", f"ledger.json was started from another holdout-c.lock (differs in: {diff})")
    return ledger


def _load_data(env: PaperCEnv) -> dict[str, pd.DataFrame]:
    paths: dict[str, Path] | None = None
    last: Exception | None = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            paths = {a: Path(p) for a, p in env.fetch(Path(env.cache_dir), Path(env.lock_path)).items()}
            break
        except Exception as exc:  # noqa: BLE001 - network / API errors are retried
            last = exc
            if attempt < FETCH_ATTEMPTS - 1:
                env.sleep(env.fetch_backoff_s * 2**attempt)
    if paths is None:
        raise PaperCRunError("data", f"candle update failed {FETCH_ATTEMPTS} times: {type(last).__name__}: {last}")
    with _stage("data"):
        if sorted(paths) != sorted(ASSETS):
            raise PaperCRunError("data", f"the candle update returned {sorted(paths)}, not every asset {list(ASSETS)}")
        dfs: dict[str, pd.DataFrame] = {}
        for a in ASSETS:
            df = load_15m_c(paths[a], with_holdout=True, lock_path=env.lock_path)
            dfs[a] = df[df["ts"] >= REPLAY_ANCHOR].reset_index(drop=True)
            if dfs[a].empty:
                raise PaperCRunError("data", f"{paths[a]} has no bars from the replay anchor on")
        return dfs


def _recording_floor(ledger: Ledger) -> int:
    if len(ledger.epochs) == 1:
        return int(ledger.epochs[0]["started_ts"])
    return int(ledger.epochs[-2]["through_ts"])


def _run_id(now: int) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y%m%dT%H%M%SZ")


def status_lines_c(ledger: Ledger, count: dict[str, Any]) -> list[str]:
    """Go-live checklist (spec 7.1), Korean, for the daily summary and `paper status`."""
    cur = int(count["epoch_id"])
    causes = [d["cause"] for d in ledger.discrepancies if int(d["epoch_id"]) == cur]
    unexplained = causes.count("UNEXPLAINED")
    lines = ["실거래 전환 조건 (Plan 5b 명세 7.1)"]
    if not count["eligible"]:
        lines.append(f"현재 구간 {cur}의 C 지문이 잠금 파일과 다름: 실거래 전환 판정 대상 아님")

    def mark(ok: bool) -> str:
        return "충족" if ok else "미충족"

    lines.append(f"1. 홀드아웃 합격: {mark(bool(ledger.holdout.get('passed')))}")
    lines.append(f"2. 모의 거래 {count['paper']}/{count['required']}: {mark(count['paper'] >= count['required'])}")
    lines.append(f"3. 원인 불명 불일치 {unexplained}건 (전체 {len(causes)}건): {mark(unexplained == 0)}")
    lines.append("4. 사용자 승인: 수동")
    return lines


def run_paper_c(env: PaperCEnv) -> RunOutcomeC:
    with _stage("precheck"):
        now = int(env.clock())
        run_id = _run_id(now)
        lock = precheck_lock_c(Path(env.lock_path), env.deps)
        cfg = config_for(normalize_combo(lock["params"]))
        ledger_dir = Path(env.ledger_dir)
        run_env = dict(env.environment())
        env_sha = environment_digest(run_env)
    with _stage("ledger"):
        ledger = _open_ledger(ledger_dir, lock)
        hs = ledger.holdout
    dfs = _load_data(env)
    with _stage("data"):
        ends = {a: int(df["ts"].iloc[-1]) for a, df in dfs.items()}
        early = {a: e for a, e in ends.items() if e < hs["last_bar_ts"]}
        if early:
            raise PaperCRunError("data", f"data ends at {early}, before the lock's last bar {hs['last_bar_ts']}")
        last_bar = min(ends.values())  # the latest bar both assets have: no flat padding of a late asset's bar
        late = [a for a in ASSETS if ends[a] == last_bar < max(ends.values())]
        dfs = {a: df[df["ts"] <= last_bar].reset_index(drop=True) for a, df in dfs.items()}
        holdout_sha = data_fingerprint_b(dfs, REPLAY_ANCHOR, hs["last_bar_ts"])
        holdout_ok = holdout_sha == hs["data_sha256"]
    with _stage("fingerprint"):
        epoch, created = ensure_epoch(ledger, env.fingerprint(), last_bar)
        floor = _recording_floor(ledger)
        if created:
            epoch["through_ts"] = floor
    with _stage("recompute"):
        res = backtest_c(dfs, cfg, HOLDOUT_START, None, with_holdout=True)
        closed, still = split_result_b(res)
        all_trades = closed + still
        by_key = {key_of(t): t for t in all_trades}
        holdout_n = sum(1 for t in all_trades if HOLDOUT_START <= t.entry_ts <= hs["last_bar_ts"])
        cur_closed = paper_trades(closed, int(epoch["started_ts"]))
        replace_epoch_trades(ledger, epoch["id"], cur_closed)
        start = int(epoch["started_ts"])
        ledger.open_position = open_positions_b(still, res, start)
        memo: dict[int, str] = {}

        def prefix(ts: int) -> str:
            if ts not in memo:
                memo[ts] = data_fingerprint_b(dfs, 0, int(ts))
            return memo[ts]

        ok_runs = sorted((r for r in ledger.runs if r.get("status") == "ok"), key=lambda r: r["last_bar_ts"])
        prev_env = ok_runs[-1].get("env_sha256") if ok_runs else None

        def first_knowing(r: dict[str, Any]) -> dict[str, Any] | None:
            return next((x for x in ok_runs if x["last_bar_ts"] >= known_at(r)), None)

        def appeared_revised(r: dict[str, Any]) -> bool:
            run = first_knowing(r)
            return run is not None and prefix(run["last_bar_ts"]) != run["data_sha256"]

        def appeared_env(r: dict[str, Any]) -> str | None:
            run = first_knowing(r)
            return None if run is None else run.get("env_sha256")

        recomputed = build_decisions_c(res)
        found = verify_decisions(ledger.decisions, recomputed, epoch, lambda d: prefix(d["ts"]) != d["data_sha256"],
                                 appeared_revised, appeared_after=floor, env_sha256=env_sha, appeared_env=appeared_env,
                                 previous_env_sha256=prev_env)
        known = {d["id"] for d in ledger.discrepancies}
        new_disc = [{**d, "epoch_id": epoch["id"], "detected_ts": last_bar, "detected_run": run_id}
                    for d in found if d["id"] not in known]
        ledger.discrepancies.extend(new_disc)
        fresh = decisions_to_record(recomputed, epoch, {d["id"] for d in ledger.decisions})
        for d in fresh:
            d.update(run_id=run_id, data_sha256=prefix(d["ts"]), epoch_id=epoch["id"], env_sha256=env_sha)
        ledger.decisions.extend(fresh)
        epoch["through_ts"] = max(int(epoch["through_ts"]), last_bar)
    with _stage("events"):
        count = paper_count_c(ledger)
        events: list[dict[str, Any]] = []
        warnings: list[str] = []
        lock_n = int(hs["n_trades"]["C"])
        if late:
            newest = datetime.fromtimestamp(max(ends.values()), UTC).isoformat(timespec="minutes")
            warnings.append(f"{late} data ends before the newest bar {newest}: the run ends at the last bar both "
                            "assets have")
        if not holdout_ok:  # one message per distinct revised hash (the event id carries it)
            warnings.append(f"holdout data hash {holdout_sha[:12]} != lock {hs['data_sha256'][:12]}")
            text = c_head(data_hash_text(hs["data_sha256"], holdout_sha))
            events.append(make_event("DATA_HASH", hs["last_bar_ts"], holdout_sha[:12], text))
        if holdout_n != lock_n:
            warnings.append(f"holdout trades recomputed {holdout_n} != lock {lock_n}")
            events.append(make_event("HOLDOUT_TRADES", hs["last_bar_ts"], holdout_n, holdout_trades_text_c(lock_n, holdout_n)))
        if created:
            events.append(make_event("EPOCH", epoch["started_ts"], epoch["id"], c_head(epoch_text(epoch))))
        events += decision_events_c(fresh, by_key, {key_of(t) for t in still}, int(epoch["started_ts"]), count,
                                    cur_closed)
        if new_disc:
            text = c_head(discrepancy_text(new_disc))
            events.append(make_event("DISCREPANCY", last_bar, len(ledger.discrepancies), text))
        data_sha = prefix(last_bar)
        run: dict[str, Any] = {
            "id": run_id, "ts": now, "status": "ok", "last_bar_ts": last_bar, "data_sha256": data_sha,
            "holdout_data_ok": holdout_ok, "holdout_trades_recomputed": holdout_n, "epoch_id": epoch["id"],
            "paper_trades": count["paper"],
            "new_decisions": len(fresh), "new_discrepancies": len(new_disc), "env": run_env, "env_sha256": env_sha,
            "warnings": warnings,
        }
        ledger.runs.append(run)
        due = daily_summary_due(now, ledger.daily_summary_date)
        if due is not None:
            eq = {p.ts: p.equity for p in res.equity}
            base = eq.get(hs["last_bar_ts"] // H1 * H1, cfg.initial_equity)
            last_eq = res.equity[-1].equity if res.equity else cfg.initial_equity
            lines = [*position_lines_b(ledger.open_position),
                     f"자산 {last_eq:,.2f} USD (잠금 이후 {last_eq / base - 1:+.2%})",
                     f"모의 {count['paper']}/{count['required']} · 구간 {epoch['id']}",
                     *status_lines_c(ledger, count)]
            y, m, d = (int(x) for x in due.split("-"))
            day_ts = int(datetime(y, m, d, DAILY_HOUR_NY, tzinfo=NY).timestamp())
            events.append(make_event("DAILY", day_ts, 0, daily_text_c(due, lines)))
            ledger.daily_summary_date = due
        queued = queue_events(ledger, events)
    with _stage("write"):
        save_ledger(ledger_dir, ledger)
        append_log_line(
            ledger_dir / RUNS_LOG,
            f"{datetime.fromtimestamp(now, UTC).isoformat(timespec='seconds')}\tok\tlast_bar="
            f"{datetime.fromtimestamp(last_bar, UTC).isoformat(timespec='minutes')}\tdata={data_sha[:12]}\t"
            f"paper={count['paper']}/{count['required']}\tepoch={epoch['id']}\tnew_decisions={len(fresh)}",
        )
    with _stage("notify", detail=False):
        remaining, sent, errors = deliver(ledger.pending_events, ledger.sent_event_ids, env.sender, sleep=env.sleep)
        ledger.pending_events = remaining
        ledger.sent_event_ids.extend(sent)
        run["notify"] = {"sent": len(sent), "pending": len(remaining), "errors": errors}
    with _stage("write"):
        save_ledger(ledger_dir, ledger)
    return RunOutcomeC(run_id, last_bar, int(epoch["id"]), len(fresh), len(new_disc), queued, sent, len(remaining),
                       warnings + errors, count)
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/paperc/test_runner.py -q
```

예상: `9 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/paperc/runner.py tests/paperc/helpers.py tests/paperc/conftest.py tests/paperc/test_runner.py
git commit -m "feat(paperc): one paper-C run - precheck, data, epoch, recompute from 2023-07-01, replay check, ledger, Telegram" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: CLI `ewbot stratc paper run | paper status`

**Files:**
- Modify: `src/ewbot/cli.py` (C의 파서 블록 끝, C 절의 머리 주석, `cmd_stratc`)
- Test: `tests/paperc/test_cli_paper_c.py`

**Interfaces:**
- Consumes: Task 10~11. `cli.py`에 이미 있는 것: `_c_lock`, `sender_from_env`, `failure_text`, `deliver`, `make_event`, `RUN_URL_ENV`, `FAILURE_MARKER_ENV`, `load_ledger`, `LedgerError`, `current_epoch`, `traceback`, `Any`.
- Produces: `ewbot stratc paper run --ledger-dir … [--cache-dir]`, `ewbot stratc paper status --ledger-dir …`; `cli._c_paper_env(args) -> PaperCEnv`(테스트가 바꾸는 운영 연결: `lock_path=_c_lock()`), `_c_alert_failure(env, stage, message)`, `_c_paper_run(args) -> int`, `_c_paper_status(args) -> int`.

B의 모의매매 명령과 같은 모양이다. `cli.py`의 수정은 1부에서 더한 C의 자리 안에서만 한다. 테스트는 파서, `ewbot.cli`가 `ewbot.paperc`도 불러오지 않음, 운영 연결(`holdout-c.lock`, 시크릿 없으면 발송기 없음), 실행 뒤 상태(홀드아웃 재현 56건), 실패 알림의 접두어와 표시 파일, 장부 없는 상태 조회를 확인한다.

- [ ] **Step 1: 실패하는 테스트 작성**

**파일 `tests/paperc/test_cli_paper_c.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from ewbot import cli
from ewbot.cli import build_parser, main
from ewbot.paperc.adapters import HEAD_C
from ewbot.stratc.data import DEFAULT_LOCK_PATH
from tests.paperc.helpers import HOLDOUT_TRADES, LAST_BAR, make_env


@pytest.fixture
def senders(tmp_path, monkeypatch, lock_text):
    built = []

    def factory(args):
        env, _, sender = make_env(tmp_path / "w", lock_text, LAST_BAR + 900 + 12 * 86_400)
        env.ledger_dir = args.ledger_dir
        built.append((env, sender))
        return env

    monkeypatch.setattr(cli, "_c_paper_env", factory)
    return built


def test_the_parser_has_the_paper_subcommands():
    p = build_parser()
    assert p.parse_args(["stratc", "paper", "run", "--ledger-dir", "x"]).cp_cmd == "run"
    assert p.parse_args(["stratc", "paper", "status", "--ledger-dir", "x"]).cp_cmd == "status"
    with pytest.raises(SystemExit):
        p.parse_args(["stratc", "paper", "run", "--ledger-dir", "x", "--params", "1"])


def test_the_cli_loads_paper_c_only_inside_its_handlers():
    code = "import sys, ewbot.cli; print(sorted(m for m in sys.modules if m.startswith(('ewbot.stratc', 'ewbot.paperc'))))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True)
    assert out.stdout.strip() == "[]"


def test_production_wiring_uses_the_repo_lock_and_env_secrets(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    env = cli._c_paper_env(build_parser().parse_args(["stratc", "paper", "run", "--ledger-dir", str(tmp_path)]))
    assert env.lock_path == DEFAULT_LOCK_PATH and DEFAULT_LOCK_PATH.name == "holdout-c.lock" and env.sender is None
    assert env.ledger_dir == tmp_path and env.cache_dir == Path("data/cache")


def test_paper_c_run_then_status(senders, tmp_path, capsys):
    ledger_dir = tmp_path / "paper-c"
    assert main(["stratc", "paper", "run", "--ledger-dir", str(ledger_dir)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("paper-C run ") and "epoch 1" in out and "/30" in out
    assert main(["stratc", "paper", "status", "--ledger-dir", str(ledger_dir)]) == 0
    out = capsys.readouterr().out
    assert "실거래 전환 조건 (Plan 5b 명세 7.1)" in out and "1. 홀드아웃 합격: 충족" in out
    assert f"holdout data ok, holdout trades recomputed {HOLDOUT_TRADES} (lock: {HOLDOUT_TRADES})" in out
    assert "discrepancies 0" in out and "C fingerprint" in out


def test_a_failed_run_alerts_with_the_paper_c_prefix(senders, tmp_path, monkeypatch, capsys):
    marker = tmp_path / "notified"
    monkeypatch.setenv("EWBOT_RUN_URL", "https://github.com/o/r/actions/runs/9")
    monkeypatch.setenv("EWBOT_FAILURE_MARKER", str(marker))
    real = cli._c_paper_env

    def failing(args):
        env = real(args)
        env.fetch.fail = 3
        return env

    monkeypatch.setattr(cli, "_c_paper_env", failing)
    assert main(["stratc", "paper", "run", "--ledger-dir", str(tmp_path / "pc")]) == 1
    assert "failed at stage data" in capsys.readouterr().err
    (_, sender), = senders
    assert sender.texts == [f"{HEAD_C} 실패 · 단계 data\ncandle update failed 3 times: ConnectionError: coinbase down\n"
                            "로그: https://github.com/o/r/actions/runs/9"]
    assert marker.read_text() == "data" and not (tmp_path / "pc" / "ledger.json").exists()


def test_status_without_a_ledger_fails(tmp_path, capsys):
    assert main(["stratc", "paper", "status", "--ledger-dir", str(tmp_path)]) == 1
    assert "no ledger.json" in capsys.readouterr().err
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/paperc/test_cli_paper_c.py -q
```

예상: `invalid choice: 'paper'`
예상: `3 failed, 1 passed, 2 errors`

`paper` 하위 명령이 아직 없어 파서가 거부하고(`cli._c_paper_env`를 직접 부르는 테스트도 실패), 그 함수를 바꾸는 픽스처를 쓰는 테스트 2개는 준비 단계에서 오류가 난다. `ewbot.cli`가 C를 불러오지 않는다는 테스트 1개는 지금도 통과한다.

- [ ] **Step 3: 최소 구현**

**수정 `src/ewbot/cli.py`** (1/3) — 다음 부분을 찾아:

```python
    p_chr.add_argument("--confirm", action="store_true")
    p_chr.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
```

바꿀 내용:

```python
    p_chr.add_argument("--confirm", action="store_true")
    p_chr.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p_cp = c_sub.add_parser("paper", help="paper trading of strategy C (ledger on the paper-c branch)")
    cp_sub = p_cp.add_subparsers(dest="cp_cmd", required=True)
    p_cpr = cp_sub.add_parser("run", parents=[c_cache], help="one paper-C run; params come from holdout-c.lock only")
    p_cpr.add_argument("--ledger-dir", type=Path, required=True)
    p_cps = cp_sub.add_parser("status", help="print the paper-C ledger's status (read-only, no network)")
    p_cps.add_argument("--ledger-dir", type=Path, required=True)
```

**수정 `src/ewbot/cli.py`** (2/3) — 다음 부분을 찾아:

```python
# `stratc fetch | backtest | walkforward | holdout lock | holdout run` is strategy C (ewbot.stratc) on BTC and ETH: it
# shares strategy B's 15m cache files under its own lock (holdout-c.lock), and its research commands never read a bar
# from 2024-01-01 on (stratc.data filters them out). ewbot.stratc is imported inside these handlers only, so the other
# commands never load it. Its combos are strategy B's grid (_b_params).
```

바꿀 내용:

```python
# `stratc fetch | backtest | walkforward | holdout lock | holdout run | paper run | paper status` is strategy C
# (ewbot.stratc, ewbot.paperc) on BTC and ETH: it shares strategy B's 15m cache files under its own lock
# (holdout-c.lock) and ledger (paper-c branch), and its research commands never read a bar from 2024-01-01 on
# (stratc.data filters them out). ewbot.stratc and ewbot.paperc are imported inside these handlers only, so the other
# commands never load them. Its combos are strategy B's grid (_b_params).
```

**수정 `src/ewbot/cli.py`** (3/3) — 다음 부분을 찾아:

```python
def cmd_stratc(args: argparse.Namespace) -> int:
    if args.c_cmd == "holdout":
        return _c_holdout_lock(args) if args.ch_cmd == "lock" else _c_holdout_run(args)
    return {"fetch": _c_fetch, "backtest": _c_backtest, "walkforward": _c_walkforward}[args.c_cmd](args)
```

바꿀 내용:

```python
def _c_paper_env(args: argparse.Namespace) -> Any:
    """Production wiring (tests replace this function): real lock, Coinbase, git and Telegram."""
    from ewbot.paperc.runner import PaperCEnv

    return PaperCEnv(ledger_dir=args.ledger_dir, lock_path=_c_lock(), cache_dir=args.cache_dir, sender=sender_from_env())


def _c_alert_failure(env: Any, stage: str, message: str) -> None:
    from ewbot.paperc.adapters import c_head

    if env.sender is None:
        return
    text = c_head(failure_text(stage, message, os.environ.get(RUN_URL_ENV)))
    _, sent, errors = deliver([make_event("FAILURE", env.clock(), stage, text)], [], env.sender, sleep=env.sleep)
    if not sent:
        print(f"failure alert not sent: {errors}", file=sys.stderr)
        return
    marker = os.environ.get(FAILURE_MARKER_ENV)
    if marker:
        Path(marker).write_text(stage, encoding="utf-8")


def _c_paper_run(args: argparse.Namespace) -> int:
    from ewbot.paperc.runner import PaperCRunError, run_paper_c

    env = _c_paper_env(args)
    try:
        out = run_paper_c(env)
    except PaperCRunError as exc:
        print(f"paper-C run failed at stage {exc.stage}: {exc.message}", file=sys.stderr)
        _c_alert_failure(env, exc.stage, exc.message)
        return 1
    except Exception as exc:  # noqa: BLE001 - alert on anything, then fail the step
        traceback.print_exc()
        _c_alert_failure(env, "unexpected", f"{type(exc).__name__}: {exc}")
        return 1
    print(f"paper-C run {out.run_id}: last bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(out.last_bar_ts))} UTC, "
          f"epoch {out.epoch_id}, paper {out.count['paper']}/{out.count['required']}, {out.new_decisions} new "
          f"decision(s), {out.new_discrepancies} new discrepancy(ies), {len(out.sent)} sent, {out.pending} pending")
    for note in out.notes:
        print(f"note: {note}", file=sys.stderr)
    return 0


def _c_paper_status(args: argparse.Namespace) -> int:
    from ewbot.paperb.notify import position_lines_b
    from ewbot.paperc.adapters import paper_count_c
    from ewbot.paperc.runner import status_lines_c

    try:
        led = load_ledger(args.ledger_dir)
    except LedgerError as exc:
        print(f"ledger unreadable: {exc}", file=sys.stderr)
        return 1
    if led is None:
        print(f"no ledger.json in {args.ledger_dir}", file=sys.stderr)
        return 1
    for line in status_lines_c(led, paper_count_c(led)) + position_lines_b(led.open_position):
        print(line)
    cur = current_epoch(led)
    print(f"epoch {cur['id']} of {len(led.epochs)}: C fingerprint {cur['fingerprint'][:12]} "
          f"(lock {led.baseline['fingerprint'][:12]})")
    if led.runs:
        r = led.runs[-1]
        print(f"last run {r['id']}: last bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['last_bar_ts']))} UTC, "
              f"holdout data {'ok' if r['holdout_data_ok'] else 'MISMATCH (warned, not refused)'}, "
              f"holdout trades recomputed {r['holdout_trades_recomputed']} (lock: {led.holdout['n_trades']['C']})")
    print(f"decisions {len(led.decisions)}, discrepancies {len(led.discrepancies)}, "
          f"pending events {len(led.pending_events)}")
    return 0


def cmd_stratc(args: argparse.Namespace) -> int:
    if args.c_cmd == "holdout":
        return _c_holdout_lock(args) if args.ch_cmd == "lock" else _c_holdout_run(args)
    if args.c_cmd == "paper":
        return _c_paper_status(args) if args.cp_cmd == "status" else _c_paper_run(args)
    return {"fetch": _c_fetch, "backtest": _c_backtest, "walkforward": _c_walkforward}[args.c_cmd](args)
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/paperc/test_cli_paper_c.py -q
```

예상: `6 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/cli.py tests/paperc/test_cli_paper_c.py
git commit -m "feat(cli): ewbot stratc paper run | paper status" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: `paper-c` 동기화, 휴면 워크플로, CI 검사, 운영 문서

**Files:**
- Create: `src/ewbot/paperc/sync.py`
- Create: `.github/workflows/paper-c.yml`
- Modify: `tests/paper/test_static.py` (워크플로 목록 한 줄, 파일 끝에 CI 검사 1개)
- Modify: `docs/strategy-c.md` (모의매매 절 추가)
- Test: `tests/paperc/test_sync.py`
- Test: `tests/paperc/test_workflow.py`

**Interfaces:**
- Consumes: Task 12: `uv run ewbot stratc paper run --ledger-dir "$PAPER_C_DIR"`, 접두어 `[ewbot paper-C]`, 환경변수 `EWBOT_RUN_URL`·`EWBOT_FAILURE_MARKER`(A·B와 같은 이름, `cli.py`가 읽음).
- Produces: `ewbot.paperc.sync`: `BRANCH = "paper-c"`, `LEDGER_FILES = ("ledger.json", "runs.log")`, `ATTEMPTS = 3`, `class SyncError`, `origin_branch(cwd) -> str`, `checkout(repo, paper_dir) -> None`, `commit_ledger(paper_dir) -> bool`, `push(paper_dir, recompute, *, attempts=3, backoff=5.0, sleep=time.sleep) -> str`, `main(argv=None) -> int`(`python -m ewbot.paperc.sync checkout | push RECOMPUTE...`, 환경변수 `PAPER_C_DIR`, `PAPER_C_PUSH_BACKOFF`). `.github/workflows/paper-c.yml`(휴면: `vars.PAPER_C_ENABLED == 'true'`; 예비 크론 `7 */3 * * *`; 동시성 그룹 `paper-c`; 캐시 키 `stratc-15m-`). `docs/strategy-c.md`의 모의매매·실패·실거래 절(Cloudflare Worker 코드 포함).

git 동기화는 B의 `paperb/sync.py`와 같은 규칙이고(브랜치가 없을 때만 orphan으로 시작, 원격이 그대로면 같은 커밋을 다시 푸시, 원격이 움직였을 때만 한 번 다시 계산) 브랜치 이름과 환경변수만 C의 것이다. 워크플로는 `paper-b.yml`과 같은 모양으로 휴면이며, 시크릿은 실행·푸시·실패 알림 단계에만 간다. `tests/paper/test_static.py`의 워크플로 목록에 `paper-c.yml`을 더하고, 파일 끝(1부의 C 검사 다음)에 `paper-c.yml`이 A를 건드리지 않는다는 CI 검사를 둔다. **A-ETH와의 병합:** A-ETH(Plan 5a)가 먼저 병합되어 워크플로 목록 테스트가 `def test_the_repository_has_exactly_the_paper_ci_paper_b_and_paper_aeth_workflows():`와 `["ci.yml", "paper-aeth.yml", "paper-b.yml", "paper.yml"]`로 바뀌어 있으면, 수정 (1/2)의 찾을 부분 대신 그 목록 줄을 찾아 `["ci.yml", "paper-aeth.yml", "paper-b.yml", "paper-c.yml", "paper.yml"]`로 바꾼다(함수 이름은 그대로 둔다). 이 계획을 그대로 적용한 `master`(`cbf57ea` 기준)에서는 아래의 수정이 맞다.

- [ ] **Step 1: 실패하는 테스트 작성**

**파일 `tests/paperc/test_sync.py`** (새 파일, 전체 내용)

```python
"""ewbot.paperc.sync against local bare repositories (no network): a bare origin that already has strategy A's
`paper` and strategy B's `paper-b` branches, the workflow's master checkout, the paper-c worktree and another writer."""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

from ewbot.paperc import sync
from ewbot.paperc.sync import BRANCH, SyncError, checkout, commit_ledger, push


def _git_version() -> tuple[int, ...]:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True).stdout
    return tuple(int(x) for x in re.findall(r"\d+", out)[:2])


pytestmark = pytest.mark.skipif(_git_version() < (2, 42), reason="needs git >= 2.42 (worktree --orphan)")


def g(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    for k in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(k)
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = t\n\temail = t@t\n[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = master\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin, work, other = tmp_path / "origin.git", tmp_path / "work", tmp_path / "other"
    g(tmp_path, "init", "-q", "--bare", str(origin))
    g(tmp_path, "init", "-q", str(work))
    (work / "README.md").write_text("code\n")
    g(work, "add", "README.md")
    g(work, "commit", "-q", "-m", "code")
    g(work, "remote", "add", "origin", str(origin))
    g(work, "push", "-q", "origin", "master")
    for other_ledger in ("paper", "paper-b"):  # strategy A's and strategy B's ledger branches exist on origin
        g(work, "switch", "-q", "--orphan", other_ledger)
        (work / "ledger.json").write_text(f'{{"{other_ledger}": 1}}\n')
        g(work, "add", "ledger.json")
        g(work, "commit", "-q", "-m", f"{other_ledger} ledger")
        g(work, "push", "-q", "origin", other_ledger)
    g(work, "switch", "-q", "master")
    g(tmp_path, "clone", "-q", str(origin), str(other))
    return {"origin": origin, "work": work, "other": other, "paper": tmp_path / "rt" / "paper-c", "tmp": tmp_path}


def _write(d, n):
    (d / "ledger.json").write_text(f'{{"run": {n}}}\n')
    with open(d / "runs.log", "a") as fh:
        fh.write(f"run {n}\n")


def _other_writer(w):
    d = w["other"]
    g(d, "fetch", "-q", "origin")
    g(d, "checkout", "-q", "-B", BRANCH, f"origin/{BRANCH}")
    _write(d, 99)
    g(d, "add", "ledger.json", "runs.log")
    g(d, "commit", "-q", "-m", "other")
    g(d, "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")


def test_checkout_starts_an_orphan_paper_c_then_tracks_it_and_leaves_paper_and_paper_b_alone(world):
    before = {b: g(world["origin"], "rev-parse", f"refs/heads/{b}") for b in ("paper", "paper-b")}
    assert BRANCH == "paper-c"
    checkout(world["work"], world["paper"])
    assert g(world["paper"], "branch", "--show-current") == BRANCH
    _write(world["paper"], 1)
    assert push(world["paper"], lambda: 0, backoff=0).startswith("pushed")
    files = g(world["origin"], "ls-tree", "--name-only", f"refs/heads/{BRANCH}").split()
    assert sorted(files) == ["ledger.json", "runs.log"]
    assert {b: g(world["origin"], "rev-parse", f"refs/heads/{b}") for b in ("paper", "paper-b")} == before
    g(world["work"], "worktree", "remove", "--force", str(world["paper"]))
    checkout(world["work"], world["paper"])
    assert (world["paper"] / "runs.log").read_text() == "run 1\n"


def test_an_unreadable_origin_never_starts_a_new_branch(world):
    g(world["work"], "remote", "set-url", "origin", str(world["tmp"] / "missing.git"))
    with pytest.raises(SyncError, match="not starting a new paper-c"):
        checkout(world["work"], world["paper"])


def test_nothing_to_commit_is_a_success(world):
    checkout(world["work"], world["paper"])
    _write(world["paper"], 1)
    push(world["paper"], lambda: 0, backoff=0)
    assert push(world["paper"], lambda: 0, backoff=0) == "no ledger change: nothing to push"
    assert commit_ledger(world["paper"]) is False


def test_a_moved_origin_means_one_recompute_on_top_of_it(world):
    checkout(world["work"], world["paper"])
    _write(world["paper"], 1)
    push(world["paper"], lambda: 0, backoff=0)
    _write(world["paper"], 2)
    _other_writer(world)
    calls = []

    def recompute():
        calls.append(1)
        _write(world["paper"], 3)
        return 0

    out = push(world["paper"], recompute, backoff=0)
    assert out.startswith("pushed") and len(calls) == 1
    log = g(world["origin"], "show", f"refs/heads/{BRANCH}:runs.log").split("\n")
    assert log == ["run 1", "run 99", "run 3"]


def test_failed_pushes_retry_the_same_commit_and_give_up_after_three(world):
    checkout(world["work"], world["paper"])
    hooks = world["tmp"] / "hooks"
    hooks.mkdir()
    (hooks / "pre-push").write_text("#!/bin/sh\necho no >&2\nexit 1\n")
    (hooks / "pre-push").chmod(0o755)
    g(world["paper"], "config", "core.hooksPath", str(hooks))
    _write(world["paper"], 1)
    waits = []
    with pytest.raises(SyncError, match="push failed 3 times"):
        push(world["paper"], lambda: pytest.fail("no recompute when origin did not move"), backoff=2.0,
             sleep=waits.append)
    assert waits == [2.0, 4.0]


def test_main_needs_paper_c_dir_and_a_known_command(world, monkeypatch, capsys):
    monkeypatch.delenv("PAPER_C_DIR", raising=False)
    assert sync.main(["checkout"]) == 1 and "PAPER_C_DIR is not set" in capsys.readouterr().err
    monkeypatch.setenv("PAPER_C_DIR", str(world["paper"]))
    monkeypatch.chdir(world["work"])
    assert sync.main(["checkout"]) == 0
    _write(world["paper"], 1)
    monkeypatch.setenv("PAPER_C_PUSH_BACKOFF", "0")
    assert sync.main(["push", sys.executable, "-c", "pass"]) == 0
    assert sync.main(["status"]) == 1
```

**파일 `tests/paperc/test_workflow.py`** (새 파일, 전체 내용)

```python
"""Static checks of .github/workflows/paper-c.yml: the shape of strategy B's paper-b.yml, nothing of A's or B's."""

from __future__ import annotations

from pathlib import Path

import yaml

import ewbot

ROOT = Path(ewbot.__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "paper-c.yml"
SETUP_UV = "astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7"
SECRETS = {"TELEGRAM_BOT_TOKEN": "${{ secrets.TELEGRAM_BOT_TOKEN }}", "TELEGRAM_CHAT_ID": "${{ secrets.TELEGRAM_CHAT_ID }}"}
RUN = 'uv run ewbot stratc paper run --ledger-dir "$PAPER_C_DIR"'


def _wf():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return wf, wf["jobs"]["paper-c"]["steps"]


def _step(steps, step_id):
    return next(s for s in steps if s.get("id") == step_id)


def test_triggers_concurrency_permissions_runner_and_dormancy():
    wf, _ = _wf()
    assert wf[True] == {"schedule": [{"cron": "7 */3 * * *"}], "workflow_dispatch": None}
    assert wf["concurrency"] == {"group": "paper-c", "cancel-in-progress": False}
    assert wf["permissions"] == {"contents": "write"} and wf["jobs"]["paper-c"]["runs-on"] == "ubuntu-24.04"
    assert wf["jobs"]["paper-c"]["if"] == "vars.PAPER_C_ENABLED == 'true'"  # dormant until the user switches it on


def test_steps_run_the_c_runtime_and_its_sync_on_python_3_12_13():
    wf, steps = _wf()
    assert [s["uses"] for s in steps if "uses" in s] == ["actions/checkout@v7", SETUP_UV, "actions/cache@v6"]
    assert steps[0]["with"] == {"ref": "master"} and steps[1]["with"]["python-version"] == "3.12.13"
    assert _step(steps, "paper-c")["run"] == RUN
    assert _step(steps, "push")["run"] == f"uv run python -m ewbot.paperc.sync push {RUN}"
    assert "uv run python -m ewbot.paperc.sync checkout" in _step(steps, "checkout-paper-c")["run"]
    cache = next(s for s in steps if s.get("uses") == "actions/cache@v6")["with"]
    assert cache["path"] == "data/cache" and cache["restore-keys"].strip() == "stratc-15m-"
    budget = sum(s.get("timeout-minutes", 0) for s in steps)
    assert wf["jobs"]["paper-c"]["timeout-minutes"] >= budget + 10


def test_secrets_reach_only_the_steps_that_send_and_the_alert_has_the_c_prefix():
    _, steps = _wf()
    with_secrets = [s.get("id") for s in steps if "secrets." in yaml.safe_dump(s)]
    assert with_secrets == ["paper-c", "push", "alert"]
    for i in with_secrets:
        assert {k: v for k, v in _step(steps, i)["env"].items() if "secrets." in v} == SECRETS
    alert = _step(steps, "alert")
    assert alert["if"] == "failure() || cancelled()" and "[ewbot paper-C] 실패" in alert["run"]


def test_nothing_of_strategy_a_or_b_is_referenced():
    text = WORKFLOW.read_text(encoding="utf-8")
    for other in ("paper_sync.sh", "btc-usd-1h", "ewbot paper run", "group: paper\n", "refs/heads/paper\n",
                  "holdout.lock", "holdout-b.lock", "stratb", "paperb", "PAPER_B", "group: paper-b"):
        assert other not in text
```

**수정 `tests/paper/test_static.py`** (1/2) — 다음 부분을 찾아:

```python
    assert sorted(p.name for p in WORKFLOWS.iterdir()) == ["ci.yml", "paper-b.yml", "paper.yml"]
```

바꿀 내용:

```python
    assert sorted(p.name for p in WORKFLOWS.iterdir()) == ["ci.yml", "paper-b.yml", "paper-c.yml", "paper.yml"]
```

**수정 `tests/paper/test_static.py`** (2/2) — 다음 부분을 찾아:

```python
    assert c_files and not [r for r in c_files if is_fingerprinted(r)]
    assert not [p for p in tree_files() if p.startswith(("src/ewbot/stratc/", "src/ewbot/paperc/"))]
```

바꿀 내용:

```python
    assert c_files and not [r for r in c_files if is_fingerprinted(r)]
    assert not [p for p in tree_files() if p.startswith(("src/ewbot/stratc/", "src/ewbot/paperc/"))]


def test_the_paper_c_workflow_never_touches_strategy_a():
    text = (WORKFLOWS / "paper-c.yml").read_text(encoding="utf-8")
    assert yaml.safe_load(text)["concurrency"]["group"] == "paper-c"
    for a_thing in ("paper_sync.sh", "btc-usd-1h", "ewbot paper run", "holdout.lock"):
        assert a_thing not in text
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/paperc/test_sync.py tests/paperc/test_workflow.py tests/paper/test_static.py -q
```

예상: `cannot import name 'sync' from 'ewbot.paperc'`
예상: `1 error`

`ewbot.paperc.sync`가 아직 없어 `test_sync.py`가 수집 단계에서 실패하고 실행이 중단된다.

- [ ] **Step 3: 최소 구현**

**파일 `src/ewbot/paperc/sync.py`** (새 파일, 전체 내용)

```python
"""Git side of .github/workflows/paper-c.yml (Plan 5b spec 7): strategy A's scripts/paper_sync.sh rules (as strategy
B's paperb.sync implements them) for the orphan branch `paper-c` (A's `paper` and B's `paper-b` branches are never
touched). Run from the master checkout:

  python -m ewbot.paperc.sync checkout
      worktree of origin's paper-c at $PAPER_C_DIR (shallow), or an orphan paper-c when origin has none (ls-remote
      exit 2); any other ls-remote failure fails: a network or auth error must not start a fresh ledger.
  python -m ewbot.paperc.sync push RECOMPUTE...
      commit ledger.json and runs.log in $PAPER_C_DIR and push to origin's paper-c, 3 pushes at most. After a failed
      push: origin unmoved or unreadable -> the same commit again after a wait (no recompute, so no Telegram message
      twice); origin already at our commit -> done; origin moved -> reset to it, run RECOMPUTE (`uv run ewbot stratc
      paper run ...`), commit and push on top. Never recomputes after the last push; fails when every push failed.
Environment: PAPER_C_DIR (required); PAPER_C_PUSH_BACKOFF = seconds, retry n waits n times it (default 5)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Sequence

BRANCH = "paper-c"
LEDGER_FILES = ("ledger.json", "runs.log")
ATTEMPTS = 3


class SyncError(RuntimeError):
    """A git step of the paper-C sync failed."""


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=300)
    if check and r.returncode != 0:
        raise SyncError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def origin_branch(cwd: Path) -> str:
    """The sha of origin's paper-c, "" when origin has none; SyncError on any other ls-remote failure."""
    r = _git(cwd, "ls-remote", "--exit-code", "origin", f"refs/heads/{BRANCH}", check=False)
    if r.returncode == 0:
        return r.stdout.split()[0]
    if r.returncode == 2:
        return ""
    raise SyncError(f"git ls-remote origin failed (exit {r.returncode}): not starting a new {BRANCH} branch")


def checkout(repo: Path, paper_dir: Path) -> None:
    if origin_branch(repo):
        _git(repo, "fetch", "--no-tags", "--depth=1", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}")
        _git(repo, "worktree", "add", "-B", BRANCH, str(paper_dir), f"origin/{BRANCH}")
    else:
        print(f"origin has no {BRANCH} branch: starting an orphan one")
        _git(repo, "worktree", "add", "--orphan", "-b", BRANCH, str(paper_dir))


def commit_ledger(paper_dir: Path) -> bool:
    """Commit the ledger files; False when nothing changed."""
    _git(paper_dir, "add", "--", *[f for f in LEDGER_FILES if (paper_dir / f).exists()])
    if _git(paper_dir, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    _git(paper_dir, "commit", "-q", "-m", f"paper-c: run {datetime.now(UTC).strftime('%Y-%m-%dT%H:%MZ')}")
    return True


def push(
    paper_dir: Path,
    recompute: Callable[[], int],
    *,
    attempts: int = ATTEMPTS,
    backoff: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    if not commit_ledger(paper_dir):
        return "no ledger change: nothing to push"
    for attempt in range(1, attempts + 1):
        if _git(paper_dir, "push", "origin", f"HEAD:refs/heads/{BRANCH}", check=False).returncode == 0:
            return f"pushed {_git(paper_dir, 'rev-parse', '--short', 'HEAD').stdout.strip()} (attempt {attempt})"
        head = _git(paper_dir, "rev-parse", "HEAD").stdout.strip()
        parent = _git(paper_dir, "rev-parse", "-q", "--verify", "HEAD^", check=False).stdout.strip()
        try:
            remote: str | None = origin_branch(paper_dir)
        except SyncError:
            remote = None
        if remote == head:
            return f"origin/{BRANCH} is at our commit: the push landed despite the error"
        if attempt >= attempts:
            raise SyncError(f"push failed {attempts} times")
        if remote is None or remote == parent:
            sleep(backoff * attempt)
            continue
        if not remote:
            raise SyncError(f"origin's {BRANCH} branch disappeared")
        print(f"push rejected (attempt {attempt}): origin/{BRANCH} moved to {remote[:12]}, recompute on top of it")
        _git(paper_dir, "fetch", "--no-tags", "--depth=1", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}")
        _git(paper_dir, "reset", "-q", "--hard", f"origin/{BRANCH}")
        rc = recompute()
        if rc != 0:
            raise SyncError(f"recompute failed (exit {rc})")
        if not commit_ledger(paper_dir):
            return "no ledger change after the recompute: nothing to push"
    raise SyncError(f"push failed {attempts} times")


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    paper_dir = os.environ.get("PAPER_C_DIR", "")
    try:
        if not paper_dir:
            raise SyncError("PAPER_C_DIR is not set")
        if args == ["checkout"]:
            checkout(Path.cwd(), Path(paper_dir))
            return 0
        if len(args) > 1 and args[0] == "push":
            backoff = float(os.environ.get("PAPER_C_PUSH_BACKOFF", "5"))
            print(push(Path(paper_dir), lambda: subprocess.run(args[1:]).returncode, backoff=backoff))
            return 0
    except SyncError as exc:
        print(f"paper-c sync: {exc}", file=sys.stderr)
        return 1
    print("usage: python -m ewbot.paperc.sync checkout | push RECOMPUTE...", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

**파일 `.github/workflows/paper-c.yml`** (새 파일, 전체 내용)

```yaml
# ewbot paper trading of strategy C (Plan 5b spec 7): one deterministic recompute of BTC and ETH per run.
# Code comes from master; ledger.json / runs.log live on the orphan `paper-c` branch, which only this
# workflow writes (docs/strategy-c.md: never push to it by hand). Strategy A's workflow (paper.yml) and
# strategy B's (paper-b.yml), their branches, sync code and candle cache keys are never touched.
# The git side (paper-c worktree, commit, push and retry) is `python -m ewbot.paperc.sync`.
name: paper-c

# Primary trigger: a Cloudflare Workers cron calls workflow_dispatch at minute 7 of every hour
# (docs/strategy-c.md, user step). `schedule` is only a sparse backup (minute 7, every 3 hours).
on:
  schedule:
    - cron: "7 */3 * * *"
  workflow_dispatch:

concurrency:
  group: paper-c
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  paper-c:
    # Dormant until the user sets the repository variable PAPER_C_ENABLED=true after a passed holdout
    # (docs/strategy-c.md): a skipped job uses no runner minutes and sends nothing.
    if: vars.PAPER_C_ENABLED == 'true'
    runs-on: ubuntu-24.04
    timeout-minutes: 60
    env:
      EWBOT_RUN_URL: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}
    steps:
      - name: Check out master
        uses: actions/checkout@v7
        with:
          ref: master

      - name: Install uv (with its cache)
        uses: astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7 # v10.2.0
        with:
          python-version: "3.12.13"
          enable-cache: true
          cache-dependency-glob: uv.lock

      - name: Sync dependencies
        timeout-minutes: 10
        run: uv sync --frozen

      - name: Check out the paper-c branch (orphan on first use)
        id: checkout-paper-c
        timeout-minutes: 5
        run: |
          set -euo pipefail
          echo "PAPER_C_DIR=$RUNNER_TEMP/paper-c" >> "$GITHUB_ENV"
          echo "EWBOT_FAILURE_MARKER=$RUNNER_TEMP/ewbot-c-failure-notified" >> "$GITHUB_ENV"
          git config --global user.name "ewbot-paper-c"
          git config --global user.email "ewbot-paper-c@users.noreply.github.com"
          PAPER_C_DIR="$RUNNER_TEMP/paper-c" uv run python -m ewbot.paperc.sync checkout

      - name: Restore the BTC and ETH 15m candle caches
        uses: actions/cache@v6
        with:
          path: data/cache
          key: stratc-15m-${{ github.run_id }}-${{ github.run_attempt }}
          restore-keys: |
            stratc-15m-

      - name: Paper-C run
        id: paper-c
        timeout-minutes: 15
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: uv run ewbot stratc paper run --ledger-dir "$PAPER_C_DIR"

      - name: Commit and push the ledger
        id: push
        timeout-minutes: 15
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: uv run python -m ewbot.paperc.sync push uv run ewbot stratc paper run --ledger-dir "$PAPER_C_DIR"

      - name: Telegram failure alert (fallback when ewbot did not send one)
        id: alert
        if: failure() || cancelled()
        timeout-minutes: 2
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          JOB_STATUS: ${{ job.status }}
        run: |
          set -euo pipefail
          if [ -f "${EWBOT_FAILURE_MARKER:-/nonexistent}" ]; then
            echo "ewbot already sent the failure alert"
            exit 0
          fi
          if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
            echo "no Telegram secrets: skipping the alert"
            exit 0
          fi
          text="[ewbot paper-C] 실패 · 단계 workflow (checkout / setup / sync / timeout / push) · ${JOB_STATUS}"$'\n'"로그: ${EWBOT_RUN_URL}"
          curl -sS --fail-with-body --retry 3 --max-time 20 -X POST \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=${text}"
          echo
```

**수정 `docs/strategy-c.md`** (1/1) — 다음 부분을 찾아:

```markdown
5. If `first_run.criteria.passed` is false, strategy C ends (spec 6.5): no paper trading, no retuning, no further variant.
```

바꿀 내용:

````markdown
5. If `first_run.criteria.passed` is false, strategy C ends (spec 6.5): no paper trading, no retuning, no further variant.

## Paper trading (only after a passed holdout)

What runs: `.github/workflows/paper-c.yml` recomputes both assets from 2023-07-01 (the replay anchor) with the params in the committed `holdout-c.lock`, records the decisions in `ledger.json` and `runs.log` on the orphan branch `paper-c` (only the workflow writes it: never push to it by hand), and sends Telegram messages prefixed `[ewbot paper-C]` with the same bot and secrets as strategy A; each trade message names its asset (`C-BTC`, `C-ETH`). Paper trades are the trades of either asset that enter after the lock's last bar; 30 are needed in all (spec 7.1), with no unexplained discrepancy. A change of any file in `src/ewbot/stratc/` opens a new epoch and restarts the count; a change of the modules strategy C imports (`deps_sha256`) stops paper-C at the precheck instead. Each run ends at the latest 15m bar both assets have.

```bash
uv run ewbot stratc paper status --ledger-dir <paper-c worktree>   # read-only
```

One-time setup (you):

1. Make sure `master` has the committed, passed `holdout-c.lock` (the workflow reads it from `master`). The workflow is dormant: its job only runs when the repository variable `PAPER_C_ENABLED` is `true`.
2. Switch it on: `gh variable set PAPER_C_ENABLED --body true` (to stop paper-C later: `gh variable delete PAPER_C_ENABLED`).
3. Hourly trigger: create one more Cloudflare Worker (the existing workers stay as they are: each dispatches its own workflow). In **Workers & Pages** → **Create Worker**, name it `ewbot-paper-c-trigger`, paste the code below, deploy; add the secret `GITHUB_TOKEN` (the same fine-grained token as A's worker: this repository, Actions read and write); add the cron trigger `7 * * * *` (seven minutes after every hour: strategy C decides at 1h closes).

   ```js
   const DISPATCH_URL =
     "https://api.github.com/repos/taejunoh/elliott-wave-btc-bot/actions/workflows/paper-c.yml/dispatches";

   export default {
     async scheduled(event, env, ctx) {
       const response = await fetch(DISPATCH_URL, {
         method: "POST",
         headers: {
           Authorization: `Bearer ${env.GITHUB_TOKEN}`,
           Accept: "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28",
           "User-Agent": "ewbot-paper-c-trigger",
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

   The workflow's own `schedule` (`7 */3 * * *`) is only a backup.
4. Start the first run by hand: `gh workflow run paper-c.yml --ref master`, then `gh run watch`.
5. Actions usage (spec 7): after the first week, compare the minutes used by `paper-c` with the free allowance at <https://github.com/settings/billing>. If the month would run over, change the worker's cron to `7 */2 * * *`: decisions do not change (every run recomputes everything), only the messages come later.

## Failures

- A failed run sends `[ewbot paper-C] 실패 · 단계 <stage>` with the log link (the workflow's fallback alert covers checkout, setup and push failures). Nothing is committed after a failed run; the next run recomputes everything.
- Stage `precheck`: `holdout-c.lock` is missing, not committed, edited, not run, failed, its keys, grid, config, holdout start or replay anchor are not the code's, or the modules strategy C imports changed since the lock (`deps_sha256`: revert the change; paper-C has no re-lock). Stage `data`: the candle update failed three times, or an asset's data ends before the lock's last bar.
- `[ewbot paper-C] 경고: 홀드아웃 구간 데이터 해시 불일치`: the holdout window's 15m bars no longer hash to the holdout run's. The run is not refused: it warns once per distinct hash, and any recorded decision that changes because of it is classified `DATA_REVISION`.
- Discrepancies (`[ewbot paper-C] 경고: 결정 불일치`) are classified like A's: `DATA_REVISION`, `FINGERPRINT_CHANGE`, `ENVIRONMENT_CHANGE`, `UNEXPLAINED`. Any `UNEXPLAINED` one blocks the go-live condition.

## Go-live (never automatic)

Holdout passed, at least 30 paper trades of BTC and ETH together after the lock's last bar, zero unexplained discrepancies in the paper period, and your explicit approval. Live trading of C needs its own plan and its own Coinbase account or portfolio.
````

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/paperc/test_sync.py tests/paperc/test_workflow.py tests/paper/test_static.py -q
```

예상: `39 passed`

`test_sync.py` 6개, `test_workflow.py` 4개, `tests/paper/test_static.py` 29개(1부 뒤 28개 + 새 CI 검사 1개)다. git이 2.42보다 낮으면 `test_sync.py`의 6개가 건너뛰어진다(`33 passed, 6 skipped`).

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/paperc/sync.py .github/workflows/paper-c.yml tests/paperc/test_sync.py tests/paperc/test_workflow.py tests/paper/test_static.py docs/strategy-c.md
git commit -m "feat(paperc): paper-c git sync, dormant paper-c workflow, CI check, paper-C ops guide" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: 2부 최종 검증

**Files:** 없음.

**Interfaces:**
- Consumes: Task 10~13의 결과 전체.
- Produces: 2부 병합 가능 여부의 판단 근거.

- [ ] **Step 1: 전체 테스트**

```bash
uv run --group live pytest -q
```

예상: `1491 passed, 1 skipped`

1부 뒤보다 정확히 32개가 많아야 한다(`data/cache/BTC-USD_1h.parquet`가 있으면 `1492 passed`).

- [ ] **Step 2: A 지문과 C 지문이 그대로인지 확인**

```bash
uv run python -c "from ewbot.paper.fingerprint import current_fingerprint; print(current_fingerprint())"
git diff --quiet "$(git merge-base HEAD origin/master)" -- src/ewbot/stratc && echo "stratc unchanged"
```

예상: `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1`
예상: `stratc unchanged`

두 번째 줄이 출력되지 않으면 2부가 `stratc/`를 바꾼 것이다: 병합하지 않는다(C 지문이 잠금과 달라진다).

- [ ] **Step 3: A·B 보호 검사**

```bash
uv run --group live pytest tests/stratc/test_isolation.py tests/stratb/test_isolation.py tests/paper/test_fingerprint.py tests/paper/test_static.py -q -rs
```

예상: `59 passed`

- [ ] **Step 4: 2부가 바꾼 파일이 허용 목록뿐인지 확인**

```bash
git diff --name-only "$(git merge-base HEAD origin/master)" -- . ':(exclude)docs/superpowers'
```

예상 출력(정확히 이 목록):

```text
.github/workflows/paper-c.yml
docs/strategy-c.md
src/ewbot/cli.py
src/ewbot/paperc/__init__.py
src/ewbot/paperc/adapters.py
src/ewbot/paperc/runner.py
src/ewbot/paperc/sync.py
tests/paper/test_static.py
tests/paperc/__init__.py
tests/paperc/conftest.py
tests/paperc/helpers.py
tests/paperc/test_adapters.py
tests/paperc/test_cli_paper_c.py
tests/paperc/test_runner.py
tests/paperc/test_sync.py
tests/paperc/test_workflow.py
```

- [ ] **Step 5: 작업 트리가 깨끗한지 확인**

```bash
test -z "$(git status --porcelain)" && echo clean
```

예상: `clean`

---

### Task 15: 수동 절차 — 모의매매 시작 (사용자)

- [ ] **Step 1: PR과 병합(사용자)**

```bash
git push -u origin feat/strategy-c-paper
gh pr create --base master --title "Plan 5b part 2: paper-C runtime (dormant workflow)" --body "Paper trading of strategy C after its passed holdout: paperc runtime, paper-c sync, dormant paper-c.yml (PAPER_C_ENABLED). stratc/ unchanged since the lock. 🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

- [ ] **Step 2: 모의매매 켜기(사용자)**

`docs/strategy-c.md`의 "Paper trading" 절차를 따른다: `gh variable set PAPER_C_ENABLED --body true`, 새 Cloudflare Worker(`ewbot-paper-c-trigger`, 크론 `7 * * * *`), 첫 실행 `gh workflow run paper-c.yml --ref master`와 `gh run watch`. 첫 실행 뒤 `paper-c` 브랜치의 상태를 확인한다:

```bash
git fetch origin paper-c
git worktree add "$TMPDIR/paper-c" origin/paper-c
uv run ewbot stratc paper status --ledger-dir "$TMPDIR/paper-c"
git worktree remove "$TMPDIR/paper-c"
```

`holdout data ok`이고 `holdout trades recomputed`가 잠금의 거래 수와 같아야 한다. 그 뒤에야 `data/cache/`를 정리할 수 있다.

- [ ] **Step 3: 첫 주 사용량 확인(사용자)**

1주 뒤 GitHub Billing에서 `paper-c`의 Actions 사용 시간을 확인하고, 무료 한도를 넘을 것 같으면 Worker의 크론을 `7 */2 * * *`로 바꾼다(판단은 같고 알림만 늦어진다).

---

## 자기 검토 (Self-Review)

**1. 명세 대응표**

| 명세 | 요구 | 작업 |
|---|---|---|
| 1 결정 사항(원문) | BTC+ETH, B의 상품·계약·위험·상한·손실 한도·비용 | 1(`CConfig`), 4(B의 엔진) |
| 1 결정 사항 | 4시간 추세 B2 그대로(k 2.0, 마지막 두 고점·저점, NONE이면 진입 없음) | 3(`trend_of`, 추세 테스트), 4(걸러내기) |
| 1 결정 사항 | 4시간 기준 구간(`zz_k` 2.0·3.0), 되돌림 구간, 매물대 ±0.25×ATR4h·120일, 1시간 종가 무효화 | 3 |
| 1 결정 사항 | 1시간 RSI 다이버전스(3/3, 둘째 피벗 구간 안), 1시간 종가 돌파, 다음 1시간 시가 진입, 12일 기한, 8일 시간 손절, 손절 −0.2×ATR1h, 최대 2.5×ATR4h, TP1·TP2·잔량 그리드, TP2가 가까우면 제외, 걸러진 돌파 뒤에도 쌍 유지 | 1(고정값), 3, 4 |
| 1 결정 사항 | 32개 조합, A의 규칙(B의 구현) 워크포워드, B의 포트폴리오 합격 기준, 모의 30건·불일치 0·승인, 1회 시도 | 5, 6, 9, 11(`status_lines_c`) |
| 1 결정 사항 | 연구 무결성 주의, 예상 빈도 | 명세 6.6·6.7, 9(보고) |
| 1 결정 사항 | 데이터: B의 15분봉 캐시 재사용, 2024 가드, 1시간·4시간 재표본 | 2(해석 5), 3 |
| 1 이 명세가 정한 값 | 288시간, 192시간, 2,880시간, `REPLAY_ANCHOR` 2023-07-01, B의 엔진·대조군, 표기, 모의매매 이름·시각 | 1, 2, 4, 6, 10~13 |
| 2 격리 | `stratc/`·`paperc/`만, A·B는 import만, 키워드 `with_holdout`, CLI 지연 import와 A-ETH와 겹치지 않는 자리, CI 검사 | 1(`test_isolation`, CI 검사), 7, 12, 13 |
| 3 구조 | 모듈 배치 | 1~7, 10~13 |
| 4.1~4.4 매매 규칙 | 추세, 구조·후보, 확인·진입, 손절·걸러내기·청산·수량 | 3, 4 |
| 4.5 B·B2와의 대응 | 4배로 늘린 경로에서 B와 같은 신호 | 3(`test_on_a_dilated_path_c_finds_exactly_b_s_signals` 외) |
| 5 그리드 | B와 같은 32개, 포트폴리오 평가 | 1(`GRID_C is GRID_B`), 5 |
| 6.1 데이터 | B의 캐시 파일, C의 가드, 누락 봉, 이어 받기, 재시도 | 2 |
| 6.2 체결 | B의 15분 엔진 그대로, `engine_config`, 다음 1시간 시가 | 1, 4 |
| 6.3 워크포워드 | B의 `evaluate_folds_b`, `proceed`, 출처 기록, 미진행이면 종료 | 5, 9 |
| 6.4 잠금·1회 | 보고서에서만, 묶는 필드, 깨끗한 트리, 승인 후 1회, 즉시 커밋 | 6, 7, 9 |
| 6.5 합격 기준 | B의 네 기준, 대조군에 추세 걸러내기 없음 | 6 |
| 7 모의매매 | 합격 후 2부, `stratc/` 불변, 재계산·장부·분류·구간·알림·휴면 워크플로·트리거 | 10~15 |
| 8 오류 처리 | 수집 재시도, 누락 봉, 잠금 불일치 거부, 데이터 해시 경고, A·B 무충돌 | 2, 3, 6, 11, 13, 1 |
| 9 테스트 | 표의 모든 항목 | 1~7, 10~13 |
| 10 진행 절차 | 사전 등록 커밋 → 1부 → 워크포워드 → 잠금 → 승인 → 홀드아웃 → (합격) 2부 | 이 계획의 순서, 9, 15 |

**2. 자리 표시자 검사:** "TBD", "TODO", "나중에", "적절히 처리", "Task N과 같이" 같은 표현이 없다. 모든 코드 단계에 전체 코드가 있고, 모든 명령에 예상 출력이 있다. Task 13의 A-ETH 병합 경우는 정확한 줄 내용을 적었다.

**3. 타입 일관성:** 조합은 B의 `Combo` 튜플이고 JSON과는 B의 `normalize_combo`/`combo_to_json`으로만 오간다. 여러 자산의 데이터는 어디서나 `ASSETS` 순서의 `{자산: …}`이다. 신호는 B의 `SignalB`이고 `i`는 15분 프레임(`FrameC.f15`)의 인덱스, `ts`는 그 15분봉의 시각이다. 실행 결과는 B의 `BResult`이고 거래의 `setup`만 `C-<자산>`이다. 잠금 경로는 `stratc.data.DEFAULT_LOCK_PATH`(CLI에서는 `_c_lock()`), 잠금 함수는 `create_lock_c`·`read_lock_c`·`committed_lock_c`·`lock_mismatch_c`·`run_holdout_c`, 모의매매는 `PaperCEnv`·`run_paper_c`·`precheck_lock_c`로 Task 6·7·11·12에서 같다.

**4. 검증:** 이 계획의 모든 코드는 작성 중 개발 사본(`master` `cbf57ea`의 클론)에서 1부·2부의 전체 테스트를 통과시킨 것을 그대로 옮겼다(문서의 코드 블록은 그 파일에서 생성했다). 커밋 전의 발견용 재생에서 두 가지를 바로잡았다: Task 1의 deps 테스트가 아직 없는 모듈(Task 3~6에서 생김)을 요구하던 것(그 확인은 Task 6의 잠금 테스트에 있다)과, 실제와 다르던 Step 2 메시지 네 곳.

**재생 검증(2026-10-08):** 명세·계획 커밋 `ef5cd47`을 새 클론(Python 3.12.13, 실제 데이터 캐시 없음)에 문서 그대로 적용했다: 모든 파일과 수정을 적힌 대로 쓰고 모든 명령을 실행했다(1부 Task 1~8; 그다음 1부가 병합된 것으로 흉내 내어 `origin/master`를 1부의 끝으로 옮기고 새 브랜치 `feat/strategy-c-paper`에서 2부 Task 10~14; 수동 Task 9·15는 제외). `예상` 항목 43개가 모두 맞았다: 각 Step 2는 적힌 메시지와 수로 실패했고 각 Step 4는 적힌 수로 통과했으며(테스트 수 앞에 다른 숫자가 붙지 않는지까지 확인), Task 8의 전체 테스트 `1459 passed, 1 skipped`, Task 14의 `1491 passed, 1 skipped`, A 지문 `e0435223…` 그대로, 보호 검사 `58 passed`·`59 passed`(건너뜀 없음), 바뀐 파일 목록 두 개 일치, 2부 뒤 `stratc/` 불변, 작업 트리 깨끗함, 작업 커밋 11개 성공. 적용 결과의 `src/`, `tests/`, `docs/strategy-c.md`, `.github/workflows/paper-c.yml`은 개발 사본과 바이트 단위로 같았다. 같은 트리에서 Python 3.13.15로도 전체 테스트가 통과했다(`1491 passed, 1 skipped`).

**A-ETH와의 병합 검증:** A-ETH 계획(`2a17593`)의 `cli.py`·`test_static.py` 수정 9개를 `master`에 적용한 브랜치와 이 계획의 결과를 git으로 병합했다. 1부의 두 파일은 충돌 없이 병합되었고, 2부는 `test_static.py`의 워크플로 목록 한 줄만 충돌한다(Task 13에 해결 내용을 적었다; `cli.py`는 2부에서도 충돌 없음).
